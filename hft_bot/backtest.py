"""
Backtest the OFI+TFI strategy using two data sources:

  Mode A  --candles   (default)
    Uses 1-minute OHLCV from Hyperliquid REST API.
    OFI proxy  = signed price change strength × relative volume
    TFI proxy  = candle body ratio (close vs open position in range)
    Trend gate = 3-bar price change > 0
    Persistence= 2 consecutive confirming bars
    Horizon    = 1, 3, 5 minutes forward
    Limitation : no tick-level granularity; spread is approximated as $1.

  Mode B  --replay FILE
    Uses a JSONL file recorded by  paper_trader.py --record FILE.
    Runs the exact strategy (same evaluate_signal / ingest_trade code)
    on the raw l2Book + trades snapshots.  Perfect replication.

  Record live data for Mode B:
    python paper_trader.py --record session.jsonl --duration 3600

Usage:
    cd hft_bot
    python backtest.py                        # candle backtest, last 7 days
    python backtest.py --candles --days 3
    python backtest.py --replay session.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import config

MAKER_REBATE = 0.0001   # -0.01% per leg
SPREAD       = 1.0      # typical mainnet BTC spread in $
TICK         = config.PRICE_TICK


# ─────────────────────────────────────────────────────────────────────────────
# Candle-based backtest (Mode A)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CandleBar:
    ts_ms: int
    open:  float
    high:  float
    low:   float
    close: float
    vol:   float     # BTC volume
    n:     int       # number of trades


def fetch_candles(days: int = 7) -> List[CandleBar]:
    """Download 1-minute candles from Hyperliquid mainnet."""
    import requests
    now_ms  = int(time.time() * 1000)
    start   = now_ms - days * 86_400_000
    url     = "https://api.hyperliquid.xyz/info"
    payload = {"type": "candleSnapshot", "req": {
        "coin": config.COIN, "interval": "1m",
        "startTime": start, "endTime": now_ms,
    }}
    resp = requests.post(url, json=payload, timeout=20)
    resp.raise_for_status()
    raw  = resp.json()
    return [
        CandleBar(
            ts_ms=c["t"], open=float(c["o"]), high=float(c["h"]),
            low=float(c["l"]), close=float(c["c"]), vol=float(c["v"]), n=c["n"],
        )
        for c in raw
    ]


def run_candle_backtest(bars: List[CandleBar]) -> None:
    """
    Signal logic derived from 1-minute bars.

    OFI proxy:  signed_move = (close - open) / (high - low + 1e-6)
                in [-1, +1]; measures where price closed within the bar.
    TFI proxy:  same (assumes higher close = more buy-initiated flow).
    Trend gate: mid_change over last 3 bars > 0 for BUY, < 0 for SELL.
    Persistence: 2 consecutive bars both exceeding OFI threshold.
    Cooldown:   2 bars (≈ 2 minutes) between signals.
    """
    OFI_THRESH    = 0.60    # |signed_move| > 0.60 to fire
    MIN_TFI       = 0.10    # |tfi| > 0.10 (roughly same as MIN_TFI_STRENGTH)
    VOL_MULT      = 1.0     # minimum relative volume (vs 20-bar avg)
    TREND_BARS    = 3       # look-back for trend gate
    PERSIST       = 2       # consecutive confirming bars
    COOLDOWN_BARS = 2       # minimum bars between signals
    HORIZONS      = [1, 3, 5]   # forward minutes to measure PnL

    n = len(bars)
    avg_vol_window = 20

    signals: List[dict] = []
    persist_buy = persist_sell = 0
    last_signal_bar = -999

    sep = "═" * 72
    print(sep)
    print(f"  CANDLE BACKTEST  —  {config.COIN}  1-minute bars  n={n}")
    print(f"  Period: {_ms_to_str(bars[0].ts_ms)} → {_ms_to_str(bars[-1].ts_ms)}")
    print(f"  OFI_THRESH={OFI_THRESH}  PERSIST={PERSIST}  COOLDOWN={COOLDOWN_BARS}bars")
    print(sep)

    for i in range(max(avg_vol_window, TREND_BARS), n - max(HORIZONS)):
        bar = bars[i]
        mid = (bar.high + bar.low) / 2
        rng = bar.high - bar.low + 1e-6

        # ─── OFI proxy
        signed_move = (bar.close - bar.open) / rng

        # ─── Volume gate
        avg_vol = sum(b.vol for b in bars[i - avg_vol_window: i]) / avg_vol_window
        if avg_vol > 0 and (bar.vol / avg_vol) < VOL_MULT:
            persist_buy = persist_sell = 0
            continue

        # ─── Trend gate
        trend = bars[i].close - bars[i - TREND_BARS].close
        bullish_trend = trend > 0
        bearish_trend = trend < 0

        # ─── Persistence counters
        if signed_move >= OFI_THRESH:
            persist_buy  += 1
            persist_sell  = 0
        elif signed_move <= -OFI_THRESH:
            persist_sell += 1
            persist_buy   = 0
        else:
            persist_buy = persist_sell = 0

        # ─── Cooldown
        if i - last_signal_bar < COOLDOWN_BARS:
            continue

        direction = None
        if persist_buy >= PERSIST and bullish_trend and signed_move >= MIN_TFI:
            direction = "buy"
        elif persist_sell >= PERSIST and bearish_trend and signed_move <= -MIN_TFI:
            direction = "sell"

        if direction is None:
            continue

        last_signal_bar = i
        persist_buy = persist_sell = 0

        # ─── ALO fill model: enter at next bar's open ±tick
        next_bar = bars[i + 1]
        if direction == "buy":
            fill_px = next_bar.open - (SPREAD / 2 - TICK)   # bid+tick ≈ mid-$0.40
        else:
            fill_px = next_bar.open + (SPREAD / 2 - TICK)   # ask-tick ≈ mid+$0.40

        notional = config.ORDER_SIZE_BTC * fill_px

        # ─── Forward returns
        fwd = {}
        for h in HORIZONS:
            future   = bars[i + h]
            fwd_mid  = (future.high + future.low) / 2
            dm       = 1 if direction == "buy" else -1
            pnl_raw  = (dm * (fwd_mid - mid) + SPREAD / 2) * config.ORDER_SIZE_BTC
            rebate   = 2 * MAKER_REBATE * notional
            pnl_net  = pnl_raw + rebate
            bps      = (pnl_net / notional) * 10_000
            fwd[h]   = dict(pnl_raw=pnl_raw, pnl_net=pnl_net, bps=bps, fwd_mid=fwd_mid)

        signals.append(dict(
            bar_i=i, ts_ms=bar.ts_ms, direction=direction,
            mid=mid, fill_px=fill_px, notional=notional,
            ofi=signed_move, trend=trend, fwd=fwd,
        ))

    # ─── Report
    ns = len(signals)
    if ns == 0:
        print("  No signals generated.")
        print(sep)
        return

    buys  = [s for s in signals if s["direction"] == "buy"]
    sells = [s for s in signals if s["direction"] == "sell"]
    avg_notional = sum(s["notional"] for s in signals) / ns

    print(f"  Signals: {ns} total  (buys={len(buys)}  sells={len(sells)})")
    print(f"  Avg notional/trade: ${avg_notional:.2f}")
    print(f"  Signal rate: {ns / (len(bars) / 60 / 24):.1f} / day")
    print()
    print(f"  {'Horizon':>10}  {'n':>5}  {'Acc%':>6}  {'avg_raw$':>10}  {'avg_net$':>10}  "
          f"{'total_net$':>12}  {'bps':>7}  {'PF':>6}  {'Kelly%':>8}")
    print("  " + "─" * 72)

    for h in HORIZONS:
        pnl_raw_list = [s["fwd"][h]["pnl_raw"] for s in signals]
        pnl_net_list = [s["fwd"][h]["pnl_net"] for s in signals]
        bps_list     = [s["fwd"][h]["bps"]     for s in signals]

        correct = 0
        for s in signals:
            fwd_mid = s["fwd"][h]["fwd_mid"]
            dm      = 1 if s["direction"] == "buy" else -1
            if dm * (fwd_mid - s["mid"]) > 0:
                correct += 1

        acc      = correct / ns * 100
        avg_raw  = sum(pnl_raw_list) / ns
        avg_net  = sum(pnl_net_list) / ns
        total    = sum(pnl_net_list)
        avg_bps  = sum(bps_list) / ns

        wins  = [p for p in pnl_net_list if p > 0]
        loses = [p for p in pnl_net_list if p <= 0]
        pf    = sum(wins) / (-sum(loses)) if loses and sum(loses) < 0 else float("inf")

        w_rate = acc / 100
        avg_w  = sum(wins) / len(wins) if wins else 0
        avg_l  = abs(sum(loses) / len(loses)) if loses else 0
        b_r    = (avg_w / avg_l) if avg_l > 0 else float("inf")
        kelly  = max(0, w_rate - (1 - w_rate) / b_r) * 100 if b_r != float("inf") else w_rate * 100

        pf_s = f"{pf:.2f}" if pf != float("inf") else "∞"
        print(f"  {f'T+{h}min':>10}  {ns:>5}  {acc:>6.1f}  {avg_raw:>10.4f}  {avg_net:>10.4f}  "
              f"{total:>12.4f}  {avg_bps:>7.2f}  {pf_s:>6}  {kelly:>7.1f}%")

    print()

    # ─── Monthly PnL estimate
    h1  = HORIZONS[0]
    monthly_signals = ns / (len(bars) / 60 / 24 / 30)
    monthly_net     = (sum(s["fwd"][h1]["pnl_net"] for s in signals) / ns) * monthly_signals
    deployed_cap    = avg_notional * 5
    monthly_ret_pct = (monthly_net / deployed_cap) * 100

    print(f"  Monthly estimate ({h1}min horizon):")
    print(f"    Signals/month : ~{monthly_signals:.0f}")
    print(f"    Net PnL/month : ${monthly_net:.4f}")
    print(f"    Capital       : ${deployed_cap:.2f} deployed  →  monthly ret: {monthly_ret_pct:.1f}%")

    # ─── Fee comparison
    ex = signals[0]
    n_ex = ex["notional"]
    alo_earn = (2 * MAKER_REBATE * n_ex + SPREAD * config.ORDER_SIZE_BTC)
    ioc_cost = (2 * 0.00035 * n_ex + SPREAD * config.ORDER_SIZE_BTC)
    print()
    print(f"  Fee/trade  ALO=+${alo_earn:.4f} (+{alo_earn/n_ex*10000:.2f}bps)  "
          f"vs  IOC=-${ioc_cost:.4f} (-{ioc_cost/n_ex*10000:.2f}bps)")
    print(sep)

    # ─── Per-day P&L curve
    day_pnl: dict = {}
    for s in signals:
        day = _ms_to_str(s["ts_ms"])[:10]
        day_pnl.setdefault(day, 0.0)
        day_pnl[day] += s["fwd"][HORIZONS[0]]["pnl_net"]

    print(f"  Daily P&L  (T+{HORIZONS[0]}min, ALO net):")
    cumulative = 0.0
    for day, pnl in sorted(day_pnl.items()):
        cumulative += pnl
        bar_chr = "▲" if pnl >= 0 else "▼"
        print(f"    {day}  {bar_chr}  net={pnl:+.4f}$  cumulative={cumulative:+.4f}$")
    print(sep)


def _ms_to_str(ms: int) -> str:
    import datetime
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


# ─────────────────────────────────────────────────────────────────────────────
# Replay-based backtest (Mode B)
# ─────────────────────────────────────────────────────────────────────────────

def run_replay_backtest(jsonl_path: str) -> None:
    """
    Replay a JSONL session recorded by  paper_trader.py --record FILE
    through the exact live strategy.  Produces the same report as the
    live paper trader.
    """
    import clock
    from state import BotState, Level, OrderBook
    from strategy import compute_price_trend, compute_tfi, evaluate_signal, ingest_trade, process_book_update

    MAKER_REBATE_R = 0.0001
    HORIZONS_R = [250, 500, 1000, 2000]

    state = BotState()
    state.status = __import__("state").BotStatus.RUNNING

    pending: list = []    # (PendingAlo-like dicts)
    filled:  list = []
    expired: list = []
    fwd_pending: list = []

    path = Path(jsonl_path)
    if not path.exists():
        print(f"ERROR: {jsonl_path} not found")
        return

    lines = path.read_text().splitlines()
    print(f"Replay: {len(lines)} events from {jsonl_path}")

    # Drive the strategy's clock with RECORDED time, not CPU time. Without
    # this, the OFI/TFI windows and every cooldown advance with how fast the
    # replay loop runs — hours of tape collapse into a 400ms window and the
    # replayed strategy is not the live strategy.
    _replay_now = {"ms": 0}
    clock.set_source(lambda: _replay_now["ms"])

    for raw in lines:
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue

        kind    = evt.get("type")
        wall_ms = evt.get("wall_ms", 0)
        if wall_ms:
            _replay_now["ms"] = wall_ms

        # ── Forward return resolution
        if filled:
            for ft in fwd_pending:
                trade, horizons = ft[0], ft[1]
                remaining = []
                for h, dl in horizons:
                    mid_now = state.book.mid_price()
                    if wall_ms >= dl:
                        trade["forward"][h] = mid_now
                    else:
                        remaining.append((h, dl))
                ft[1] = remaining
            fwd_pending[:] = [f for f in fwd_pending if f[1]]

        if kind == "book":
            d      = evt["data"]
            lvls   = d["levels"]
            ts_ms  = int(d.get("time", wall_ms))
            book   = OrderBook(
                bids=[Level.from_ws(l) for l in lvls[0][: config.OFI_LEVELS + 3]],
                asks=[Level.from_ws(l) for l in lvls[1][: config.OFI_LEVELS + 3]],
                timestamp_ms=ts_ms,
            )

            # Expire pending ALO
            still = []
            for order in pending:
                if wall_ms >= order["expire_ms"]:
                    expired.append(order)
                else:
                    still.append(order)
            pending[:] = still

            ofi = process_book_update(state, book)
            if ofi is None:
                continue

            direction = evaluate_signal(state, ofi)
            if direction is None:
                continue

            bb, ba = book.best_bid(), book.best_ask()
            if not bb or not ba:
                continue
            mid = book.mid_price() or 0.0

            if direction == "buy":
                lp = bb.price + config.PRICE_TICK
                if lp >= ba.price:
                    lp = ba.price - config.PRICE_TICK
            else:
                lp = ba.price - config.PRICE_TICK
                if lp <= bb.price:
                    lp = bb.price + config.PRICE_TICK

            pending.append(dict(
                direction=direction, limit_price=lp,
                notional=config.ORDER_SIZE_BTC * lp,
                ofi=ofi, spread_at=book.spread() or 1.0,
                mid_at=mid, signal_ms=wall_ms,
                expire_ms=wall_ms + config.LIMIT_ORDER_TIMEOUT_MS,
            ))

        elif kind == "trade":
            t     = evt["data"]
            ingest_trade(state, t)
            side  = t.get("side", "")
            try:
                px = float(t.get("px", 0))
            except (ValueError, TypeError):
                continue

            still = []
            for order in pending:
                filled_flag = (
                    (order["direction"] == "buy"  and side == "A" and px <= order["limit_price"]) or
                    (order["direction"] == "sell" and side == "B" and px >= order["limit_price"])
                )
                if filled_flag:
                    actual = min(px, order["limit_price"]) if order["direction"] == "buy" \
                             else max(px, order["limit_price"])
                    mid_now = state.book.mid_price()
                    ft = dict(
                        direction=order["direction"], fill_price=actual,
                        notional=config.ORDER_SIZE_BTC * actual,
                        spread_at=order["spread_at"],
                        mid_at_fill=mid_now or actual,
                        fill_ms=wall_ms, forward={},
                    )
                    filled.append(ft)
                    fwd_pending.append([ft, [(h, wall_ms + h) for h in HORIZONS_R]])
                else:
                    still.append(order)
            pending[:] = still

    clock.set_source(None)   # restore the live clock

    # ── Report (same format as live paper_trader)
    n_sig    = len(filled) + len(expired) + len(pending)
    n_filled = len(filled)
    fill_rate = n_filled / n_sig * 100 if n_sig else 0

    sep = "═" * 72
    print(sep)
    print("  REPLAY BACKTEST  —  exact strategy on recorded data")
    print(sep)
    print(f"  Signals: {n_sig}  Filled: {n_filled} ({fill_rate:.0f}%)  Expired: {len(expired)}")
    if n_filled == 0:
        print("  No fills.")
        print(sep)
        return

    for h in HORIZONS_R:
        resolved = [t for t in filled if t["forward"].get(h) is not None]
        if not resolved:
            continue
        net_list, bps_list, correct = [], [], 0
        for t in resolved:
            fwd = t["forward"][h]
            ret = fwd - t["mid_at_fill"]
            dm  = 1 if t["direction"] == "buy" else -1
            pnl = (dm * ret + t["spread_at"] / 2) * config.ORDER_SIZE_BTC + 2 * MAKER_REBATE_R * t["notional"]
            bps = (pnl / t["notional"]) * 10_000
            net_list.append(pnl)
            bps_list.append(bps)
            if (ret > 0 and dm == 1) or (ret < 0 and dm == -1):
                correct += 1
        nr = len(net_list)
        acc = correct / nr * 100
        avg_net = sum(net_list) / nr
        total   = sum(net_list)
        avg_bps = sum(bps_list) / nr
        wins    = [p for p in net_list if p > 0]
        loses   = [p for p in net_list if p <= 0]
        pf      = sum(wins) / (-sum(loses)) if loses and sum(loses) < 0 else float("inf")
        pf_s    = f"{pf:.2f}" if pf != float("inf") else "∞"
        print(f"  T+{h:4d}ms | n={nr:3d} | acc={acc:5.1f}% | avg_net=${avg_net:.4f} | "
              f"total=${total:.4f} | {avg_bps:.2f}bps | PF={pf_s}")
    print(sep)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode")

    ca = sub.add_parser("candles", help="1-min candle backtest (default)")
    ca.add_argument("--days", type=int, default=7)
    ca.add_argument("--file", help="local JSON file instead of API fetch")

    rp = sub.add_parser("replay", help="exact replay from recorded JSONL")
    rp.add_argument("file")

    args = parser.parse_args()

    if args.mode == "replay":
        run_replay_backtest(args.file)
    else:
        # default: candle mode
        days = getattr(args, "days", 7)
        local = getattr(args, "file", None)
        if local:
            with open(local) as f:
                raw = json.load(f)
            bars = [CandleBar(ts_ms=c["t"], open=float(c["o"]), high=float(c["h"]),
                              low=float(c["l"]), close=float(c["c"]),
                              vol=float(c["v"]), n=c["n"]) for c in raw]
        else:
            print(f"Fetching {days}-day 1-min candles from mainnet…")
            bars = fetch_candles(days)
            print(f"  Downloaded {len(bars)} bars.")
        run_candle_backtest(bars)
