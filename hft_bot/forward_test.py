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

import notify
import trend_bot


def fetch_funding(coins: list[str]) -> dict[str, float]:
    """Current hourly funding per coin — accumulated daily, this becomes the
    history the funding-carry study needs (no public source goes back far)."""
    try:
        import json, urllib.request, config
        req = urllib.request.Request(
            config.API_URL.rstrip("/") + "/info",
            data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        names = [m["name"] for m in data[0]["universe"]]
        return {c: float(data[1][names.index(c)]["funding"])
                for c in coins if c in names}
    except Exception:
        return {}

LOG = _HERE / "forward_test_log.csv"
COLUMNS = ["date", "coin", "close", "signal_fraction", "vol_scale",
           "target_fraction", "prev_close", "day_return_pct",
           "strategy_day_return_pct", "equity", "funding_hr"]
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
    funding = fetch_funding(coins)
    summary_lines: list[str] = []
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
                        f"{day_ret*100:.4f}", f"{strat_ret*100:.4f}", f"{equity:.4f}",
                        f"{funding.get(coin, 0.0):.8f}"])
            summary_lines.append(f"{coin}: tgt={target} eq={equity:.0f}")
            print(f"{today} {coin}: close={close:.0f} target={target} "
                  f"day={day_ret*100:+.2f}% strat={strat_ret*100:+.2f}% equity={equity:.2f}")
    if summary_lines:
        notify.send(f"[FWD-TEST {today}] " + " | ".join(summary_lines))


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
    # drift check on the pooled portfolio record (equal-weight across coins)
    by_date: dict = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(
            float(r["strategy_day_return_pct"]) / 100)
    pooled = [sum(v) / len(v) for d, v in sorted(by_date.items())][1:]
    print(f"\nDrift check: {drift_verdict(pooled)}")
    print(f"Log: {LOG}  (backtest expectation: ~21% CAGR / 22% DD — STRATEGY_V2.md)")


# Backtest expectation (v2.1 OOS): used by the drift check.
EXPECT_ANN_RETURN = 0.21
EXPECT_DAILY_VOL = 0.40 / math.sqrt(365) * 0.55   # vol-targeted ~40% ann * avg deployment


def drift_verdict(daily_rets: list[float],
                  expect_ann: float = EXPECT_ANN_RETURN) -> str:
    """Compare the live forward record against the backtest expectation.

    Uses a z-test on the mean daily return vs expectation, with the LIVE
    realized vol as the noise estimate. Honest framing: with <60 days the test
    has almost no power — verdicts before ~2 months mean 'keep collecting'."""
    n = len(daily_rets)
    if n < 14:
        return f"INSUFFICIENT DATA ({n}d < 14d) — keep collecting."
    mu = sum(daily_rets) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in daily_rets) / n)
    if sd == 0:
        return ("FLAT RECORD — model has been in cash the whole period "
                "(consistent with a downtrend regime; not evidence of drift).")
    expect_daily = (1 + expect_ann) ** (1 / 365) - 1
    z = (mu - expect_daily) / (sd / math.sqrt(n))
    if z < -2.0:
        return (f"DRIFT WARNING (z={z:.2f}, n={n}d): live returns are "
                f"significantly below the backtest expectation. Do not scale "
                f"up; investigate before trusting the model.")
    if z > 2.0:
        return (f"ABOVE EXPECTATION (z={z:.2f}, n={n}d): running hot — likely "
                f"a favourable regime, not skill. Do not extrapolate.")
    return (f"WITHIN EXPECTATION (z={z:.2f}, n={n}d): live record is "
            f"statistically consistent with the backtest.")


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
