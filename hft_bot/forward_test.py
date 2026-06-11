"""Forward-test harness — accrues a LIVE track record of the validated strategy.

Backtests tell you what *would* have happened; a forward test tells you what
*is* happening, on data the model has never seen. Each run appends one row per
coin: the day's signal, the mark price, and a hypothetical equity curve that
compounds the model's daily return (vol-targeted fraction × next-day move, minus
modelled costs). Over weeks this becomes the auditable answer to "is the live
model behaving like the backtest?" — without risking a cent.

It is intentionally paper-only: no keys, no orders. Run it daily (cron/systemd)
alongside or instead of the live bot to build confidence before funding.

Usage:
  python forward_test.py            # one daily mark for each TREND_COINS coin
  python forward_test.py --report   # print the accrued track record + stats
Env: TREND_COINS (default BTC), HYPERLIQUID_API_URL.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import trend_bot

LOG = _HERE / "forward_test_log.csv"
COLUMNS = ["date", "coin", "close", "signal_fraction", "vol_scale",
           "target_fraction", "prev_close", "day_return_pct",
           "strategy_day_return_pct", "equity"]
COST_PER_CHANGE = 0.00045          # 4.5 bp per position change (modelled)
FUND_DRAG_DAILY = 0.08 / 365       # 8% APR while long


def _load_rows() -> list[dict]:
    if not LOG.exists():
        return []
    with open(LOG, newline="") as fh:
        return list(csv.DictReader(fh))


def _last_for(rows: list[dict], coin: str) -> dict | None:
    for r in reversed(rows):
        if r["coin"] == coin:
            return r
    return None


def run_once(coins: list[str]) -> None:
    rows = _load_rows()
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    new_header = not LOG.exists()
    with open(LOG, "a", newline="") as fh:
        w = csv.writer(fh)
        if new_header:
            w.writerow(COLUMNS)
        for coin in coins:
            closes = trend_bot.fetch_daily_closes(coin)
            close = closes[-1]
            frac = trend_bot.ensemble_fraction(closes)
            scale = trend_bot.vol_scale(closes)
            target = round(frac * scale, 4)

            prev = _last_for(rows, coin)
            if prev and prev["date"] != today:
                prev_close = float(prev["close"])
                prev_target = float(prev["target_fraction"])
                day_ret = (close - prev_close) / prev_close
                # strategy return = yesterday's target applied to today's move,
                # minus cost if the target changed and funding drag while long
                cost = COST_PER_CHANGE * abs(target - prev_target)
                strat_ret = prev_target * day_ret - cost - FUND_DRAG_DAILY * prev_target
                equity = float(prev["equity"]) * (1 + strat_ret)
            else:
                day_ret = 0.0
                strat_ret = 0.0
                equity = float(prev["equity"]) if prev else 1000.0

            if prev and prev["date"] == today:
                continue  # already logged today

            w.writerow([today, coin, f"{close:.2f}", frac, scale, target,
                        f"{(prev_close if prev else close):.2f}" if prev else f"{close:.2f}",
                        f"{day_ret*100:.4f}", f"{strat_ret*100:.4f}", f"{equity:.4f}"])
            print(f"{today} {coin}: close={close:.0f} target={target} "
                  f"day={day_ret*100:+.2f}% strat={strat_ret*100:+.2f}% equity={equity:.2f}")


def report(coins: list[str]) -> None:
    rows = _load_rows()
    if not rows:
        print("No forward-test history yet. Run `python forward_test.py` daily first.")
        return
    for coin in coins:
        cr = [r for r in rows if r["coin"] == coin]
        if len(cr) < 2:
            print(f"{coin}: {len(cr)} mark(s) — need at least 2 days for stats.")
            continue
        rets = [float(r["strategy_day_return_pct"]) / 100 for r in cr[1:]]
        eq = float(cr[-1]["equity"]) / 1000.0
        days = len(rets)
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / len(rets)) if len(rets) > 1 else 0
        sharpe = mu / sd * math.sqrt(365) if sd > 0 else 0
        peak = 1.0; e = 1.0; mdd = 0.0
        for r in rets:
            e *= 1 + r; peak = max(peak, e); mdd = max(mdd, 1 - e / peak)
        ann = (eq ** (365 / days) - 1) * 100 if days else 0
        print(f"{coin}: {days} live days | total {(eq-1)*100:+.2f}% | "
              f"annualized {ann:+.1f}% | Sharpe {sharpe:.2f} | MaxDD {mdd*100:.1f}% | "
              f"latest target {cr[-1]['target_fraction']}")
    print(f"\nLog: {LOG}  (compare vs STRATEGY_V2.md backtest: ~21% CAGR / 22% DD)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    import os
    coins = [c.strip().upper() for c in os.getenv("TREND_COINS", "BTC").split(",") if c.strip()]
    if args.report:
        report(coins)
    else:
        run_once(coins)


if __name__ == "__main__":
    main()
