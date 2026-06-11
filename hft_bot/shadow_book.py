"""Shadow book — tracks the trades we DON'T take, so the filters stay honest.

Every day, raw long signals from the market scanner that the system declines
(coin not in TREND_COINS, failed vetting, unreliable history) are logged with
the price at the time. Later, forward returns at 7/14/30 days are resolved
against live prices. If the skipped trades are consistently profitable, the
filters are costing more than they protect and must be re-examined — the
report says so explicitly, in both directions.

This guards against the failure mode the user called out: a system that looks
disciplined but is actually just too scared to make money.

Usage:
  python shadow_book.py            # log today's skipped candidates
  python shadow_book.py --report   # resolve forward returns + verdict
(also runs automatically from forward_test.py's daily mark)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import trend_bot

BOOK = _HERE / "shadow_book.csv"
COLUMNS = ["date", "coin", "price", "signal", "target", "reason_skipped"]
HORIZONS_D = (7, 14, 30)


def _load() -> list[dict]:
    if not BOOK.exists():
        return []
    with open(BOOK, newline="") as fh:
        return list(csv.DictReader(fh))


def log_skipped(top: int = 20, min_vol_usd: float = 2e6,
                traded_coins: set[str] | None = None) -> int:
    """Scan the liquid universe; log every raw long signal we are NOT trading."""
    import scan as scan_mod
    traded = traded_coins if traded_coins is not None else set(trend_bot.TREND_COINS)
    rows = _load()
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    seen_today = {(r["date"], r["coin"]) for r in rows}
    added = 0
    new_header = not BOOK.exists()
    with open(BOOK, "a", newline="") as fh:
        w = csv.writer(fh)
        if new_header:
            w.writerow(COLUMNS)
        for r in scan_mod.scan(top=top, min_vol_usd=min_vol_usd):
            sig = r.get("signal")
            if not sig or r.get("target", 0) <= 0:
                continue
            if r["coin"] in traded:
                continue   # we'd actually trade this one; not a skip
            if (today, r["coin"]) in seen_today:
                continue
            reason = ("not in TREND_COINS (failed vetting or unvetted "
                      "short-history alt)")
            w.writerow([today, r["coin"], f"{r['mark']:.6g}", sig,
                        r["target"], reason])
            added += 1
    return added


def report() -> str:
    rows = _load()
    if not rows:
        return "Shadow book empty — run daily logging first."
    today = dt.datetime.now(dt.timezone.utc).date()
    lines = []
    stats = {h: [] for h in HORIZONS_D}
    closes_cache: dict[str, list] = {}

    for r in rows:
        d0 = dt.datetime.strptime(r["date"], "%Y-%m-%d").date()
        age = (today - d0).days
        entry = float(r["price"])
        for h in HORIZONS_D:
            if age < h:
                continue
            coin = r["coin"]
            if coin not in closes_cache:
                try:
                    closes_cache[coin] = trend_bot.fetch_daily_closes(coin, days=120)
                except Exception:
                    closes_cache[coin] = []
            closes = closes_cache[coin]
            if not closes:
                continue
            # close h days after logging ≈ closes[-(age-h+1)]
            idx = -(age - h + 1)
            if -len(closes) <= idx <= -1:
                fwd = closes[idx]
                stats[h].append((fwd - entry) / entry)

    lines.append(f"Shadow book: {len(rows)} skipped signals logged")
    worst_flag = False
    for h in HORIZONS_D:
        s = stats[h]
        if not s:
            lines.append(f"  T+{h:>2}d: no resolved entries yet")
            continue
        pos = sum(1 for x in s if x > 0) / len(s) * 100
        avg = sum(s) / len(s) * 100
        lines.append(f"  T+{h:>2}d: n={len(s)}  win-rate {pos:.0f}%  avg {avg:+.1f}%")
        if len(s) >= 20 and pos > 60 and avg > 2:
            worst_flag = True
    if worst_flag:
        lines.append("VERDICT: SKIPPED TRADES ARE WINNING — the vetting filter "
                     "is likely too strict; re-examine (e.g. lower the history "
                     "bar or add a small 'explorer' sleeve).")
    elif any(len(stats[h]) >= 20 for h in HORIZONS_D):
        lines.append("VERDICT: filters are not leaving significant money on "
                     "the table (skipped trades ~coin-flip or worse).")
    else:
        lines.append("VERDICT: insufficient resolved data (<20 per horizon) — "
                     "keep accruing before judging the filters.")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.report:
        print(report())
    else:
        n = log_skipped()
        print(f"logged {n} skipped signal(s) to {BOOK.name}")
