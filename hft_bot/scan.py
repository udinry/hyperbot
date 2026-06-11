"""Market scanner — runs the validated v2.1 trend signal across the liquid
Hyperliquid universe and reports where the trades are.

This is how the operator "finds trades" without freelancing: the signal logic
is frozen (same ensemble, regime filter and vol targeting that were walk-forward
validated on BTC and transfer-tested on ETH/SOL); the scanner just evaluates it
everywhere liquid. A coin showing LONG here is a *candidate* — STRATEGY_V2.md
discipline says vet it on deep history (frozen params) before adding it to
TREND_COINS, never trade it straight off the scan.

Also reports, per coin:
  - trigger distance: % move needed to flip the regime filter (the gate that
    matters in a downtrend) — i.e. where the market would have to go for the
    system to start buying;
  - funding APR (carry watch: extreme positive funding = potential carry setup).

Usage:
  python scan.py                 # top-20 by 24h volume
  python scan.py --top 40
  python scan.py --min-vol 5e6   # min 24h notional volume USD
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import trend_bot
from trend_bot import _post_info  # retry-hardened


def universe_by_volume(min_vol_usd: float = 2e6) -> list[dict]:
    """Liquid perp universe with 24h volume, mark price and funding."""
    data = _post_info({"type": "metaAndAssetCtxs"})
    out = []
    for meta, ctx in zip(data[0]["universe"], data[1]):
        try:
            vol = float(ctx.get("dayNtlVlm", 0))
            if meta.get("isDelisted") or vol < min_vol_usd:
                continue
            out.append({
                "coin": meta["name"],
                "day_vol_usd": vol,
                "mark": float(ctx.get("markPx", 0)),
                "funding_hr": float(ctx.get("funding", 0)),
            })
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: -x["day_vol_usd"])
    return out


def regime_trigger_pct(closes: list[float]) -> float | None:
    """% move from last close needed to cross the regime SMA (negative = price
    is already above it)."""
    n = trend_bot.REGIME_FILTER_DAYS
    if n <= 0 or len(closes) < n:
        return None
    sma = sum(closes[-n:]) / n
    return (sma - closes[-1]) / closes[-1] * 100


def scan(top: int = 20, min_vol_usd: float = 2e6) -> list[dict]:
    rows = []
    for asset in universe_by_volume(min_vol_usd)[:top]:
        coin = asset["coin"]
        try:
            closes = trend_bot.fetch_daily_closes(coin, days=400)
        except Exception:
            continue
        if len(closes) < trend_bot.MIN_HISTORY_D:
            rows.append({**asset, "history_d": len(closes), "signal": None})
            continue
        frac = trend_bot.ensemble_fraction(closes)
        scale = trend_bot.vol_scale(closes)
        rows.append({**asset,
                     "history_d": len(closes),
                     "signal": frac,
                     "vol_scale": round(scale, 2),
                     "target": round(frac * scale, 3),
                     "regime_trigger_pct": regime_trigger_pct(closes)})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-vol", type=float, default=2e6)
    args = ap.parse_args()

    rows = scan(args.top, args.min_vol)
    print(f"{'coin':8} {'24h vol $M':>10} {'signal':>7} {'target':>7} "
          f"{'to regime flip':>14} {'funding APR':>12}")
    longs = []
    for r in rows:
        if r.get("signal") is None:
            print(f"{r['coin']:8} {r['day_vol_usd']/1e6:>10.1f} "
                  f"{'n/a (' + str(r['history_d']) + 'd)':>15}")
            continue
        trig = r["regime_trigger_pct"]
        trig_s = f"{trig:+.1f}%" if trig is not None else "n/a"
        apr = r["funding_hr"] * 24 * 365 * 100
        print(f"{r['coin']:8} {r['day_vol_usd']/1e6:>10.1f} {r['signal']:>7.2f} "
              f"{r['target']:>7.3f} {trig_s:>14} {apr:>11.1f}%")
        if r["target"] and r["target"] > 0:
            longs.append(r["coin"])
    print()
    if longs:
        print(f"LONG candidates (validated signal): {', '.join(longs)}")
        print("Discipline: vet on deep history (frozen params) before adding "
              "to TREND_COINS — see STRATEGY_V2.md.")
    else:
        print("No long signals anywhere in the scanned universe — broad "
              "downtrend. The correct trade is cash.")


if __name__ == "__main__":
    main()
