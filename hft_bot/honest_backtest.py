"""Honest-accounting backtest for the OFI strategy on 1-minute candles.

The built-in candle backtest (backtest.py Mode A) is structurally OPTIMISTIC:
it fills every signal, credits half-spread + maker rebate to every trade, and
scores direction at a fixed horizon. On 60 days of regime-diverse data it
reported +$774 while honest accounting on the SAME signals showed a loss — the
difference is entirely fills, fees, and exit path.

This harness keeps the same Mode-A proxy SIGNALS but models P&L realistically:
  ENTRY  : ALO maker. Fills only if the next bar trades to our limit (price
           comes to us) — this models both the partial fill rate and adverse
           selection (we fill when the market first moves against the signal).
  EXIT   : pluggable policy (see ExitPolicy). Maker exits earn the rebate and
           fill only when a bar trades to the limit; a queue haircut drops a
           fraction of would-be maker fills (queue position risk). Taker exits
           (hard stop, timeout-at-market) pay the taker fee.

It is candle-resolution, so it cannot run the exact tick-level OFI/TFI engine —
treat absolute numbers as indicative, but the RELATIVE comparison between exit
policies and the sign of the edge after costs are the decision-grade outputs.
"""
from __future__ import annotations
import argparse, json, sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

REBATE = 0.0001    # +0.01% maker rebate per leg (earned)
TAKER  = 0.00035   # -0.035% taker fee per leg (paid)
TICK   = config.PRICE_TICK

# ---- signal proxy params (mirror backtest.py Mode A) ----
OFI_TH, MIN_TFI, TREND_BARS, PERSIST, COOLDOWN, VOLW = 0.60, 0.10, 3, 2, 2, 20


@dataclass
class ExitPolicy:
    name: str
    tp_bps: float          # maker take-profit distance (bps of entry)
    stop_bps: float        # hard stop distance (bps of entry); taker exit
    max_hold_min: int      # cancel & flatten after this many minutes
    maker_tp: bool = True  # True: TP is a resting ALO (rebate); False: taker
    maker_fill_haircut: float = 0.30  # frac of would-be maker TP fills lost to queue
    timeout_taker: bool = True         # timeout flatten pays taker (market)


def load(path):
    c = json.load(open(path))
    return [(int(x["t"]), float(x["o"]), float(x["h"]),
             float(x["l"]), float(x["c"]), float(x["v"])) for x in c]


def gen_signals(bars):
    n = len(bars); sig = []; pb = ps = 0; last = -999
    for i in range(max(VOLW, TREND_BARS), n - 70):
        t, o, h, l, cl, v = bars[i]; rng = h - l + 1e-6; sm = (cl - o) / rng
        av = sum(bars[k][5] for k in range(i - VOLW, i)) / VOLW
        if av > 0 and v / av < 1.0:
            pb = ps = 0; continue
        trend = bars[i][4] - bars[i - TREND_BARS][4]
        if sm >= OFI_TH: pb += 1; ps = 0
        elif sm <= -OFI_TH: ps += 1; pb = 0
        else: pb = ps = 0
        if i - last < COOLDOWN: continue
        d = None
        if pb >= PERSIST and trend > 0 and sm >= MIN_TFI: d = "buy"
        elif ps >= PERSIST and trend < 0 and sm <= -MIN_TFI: d = "sell"
        if not d: continue
        last = i; pb = ps = 0; sig.append((i, d))
    return sig


def run(bars, sig, pol: ExitPolicy, size_btc=None):
    size = size_btc if size_btc else config.ORDER_SIZE_BTC
    n = len(bars)
    pnl = 0.0; filled = 0; tp = sl = to = 0; wins = 0
    fill_ctr = 0
    for i, d in sig:
        nb = bars[i + 1]
        if d == "buy":
            limit = nb[1] - TICK; fill = nb[3] <= limit; entry = min(limit, nb[1])
        else:
            limit = nb[1] + TICK; fill = nb[2] >= limit; entry = max(limit, nb[1])
        if not fill:
            continue
        filled += 1
        notional = size * entry
        tp_px = entry * (1 + pol.tp_bps/1e4) if d == "buy" else entry * (1 - pol.tp_bps/1e4)
        st_px = entry * (1 - pol.stop_bps/1e4) if d == "buy" else entry * (1 + pol.stop_bps/1e4)
        ex = rs = None
        for j in range(i + 1, min(i + 1 + pol.max_hold_min, n)):
            bh, bl = bars[j][2], bars[j][3]
            hit_sl = (bl <= st_px) if d == "buy" else (bh >= st_px)
            hit_tp = (bh >= tp_px) if d == "buy" else (bl <= tp_px)
            # pessimistic: check stop before tp within a bar
            if hit_sl:
                ex, rs = st_px, "sl"; break
            if hit_tp:
                # queue haircut: some maker TPs don't fill; treat as continue
                if pol.maker_tp and pol.maker_fill_haircut > 0:
                    fill_ctr += 1
                    if (fill_ctr * 2654435761) % 100 < pol.maker_fill_haircut * 100:
                        continue
                ex, rs = tp_px, "tp"; break
        if ex is None:
            k = min(i + pol.max_hold_min, n - 1)
            ex = (bars[k][2] + bars[k][3]) / 2; rs = "to"
        dm = 1 if d == "buy" else -1
        gross = dm * (ex - entry) * size
        entry_fee = REBATE * notional   # ALO entry earns rebate
        if rs == "tp":
            exit_fee = (REBATE if pol.maker_tp else -TAKER) * size * ex
        elif rs == "sl":
            exit_fee = -TAKER * size * ex
        else:
            exit_fee = (-TAKER if pol.timeout_taker else REBATE) * size * ex
        net = gross + entry_fee + exit_fee
        pnl += net; wins += net > 0
        if rs == "tp": tp += 1
        elif rs == "sl": sl += 1
        else: to += 1
    days = (bars[-1][0] - bars[0][0]) / 86400000
    return dict(name=pol.name, signals=len(sig), fills=filled,
                fill_pct=filled/len(sig)*100 if sig else 0,
                win_pct=wins/filled*100 if filled else 0,
                tp=tp, sl=sl, to=to, net=pnl,
                per_fill=pnl/filled if filled else 0,
                per_day=pnl/days if days else 0, days=days)


def fmt(r):
    return (f"  {r['name']:<22} fills={r['fills']:>4} ({r['fill_pct']:>2.0f}%) "
            f"win={r['win_pct']:>4.1f}% TP/SL/TO={r['tp']}/{r['sl']}/{r['to']:<5} "
            f"NET=${r['net']:>+8.2f}/{r['days']:.0f}d  ${r['per_day']:>+6.2f}/day "
            f"${r['per_fill']:>+.4f}/fill")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args()
    bars = load(a.file); sig = gen_signals(bars)
    print(f"Loaded {len(bars)} bars (~{(bars[-1][0]-bars[0][0])/86400000:.0f} days), "
          f"{len(sig)} signals\n")
    if a.sweep:
        policies = [
            ExitPolicy("baseline taker 1%/0.5%", 100, 50, 10, maker_tp=False, maker_fill_haircut=0),
            ExitPolicy("maker-tp 1%/0.5%",       100, 50, 10),
            ExitPolicy("maker-scalp 8/12bps",      8, 12, 10),
            ExitPolicy("maker-scalp 5/10bps",      5, 10, 10),
            ExitPolicy("maker-scalp 4/8bps",       4,  8,  5),
            ExitPolicy("maker-scalp 6/8bps",       6,  8,  8),
            ExitPolicy("maker-scalp 10/15bps",    10, 15, 15),
        ]
        for p in policies:
            print(fmt(run(bars, sig, p)))
