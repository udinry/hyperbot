"""News lab v2 — anchored to real publication time + continuous price history.

v1 stamped each headline with SCRAPE time and the price at scrape. Across a
loop outage that is wrong: a headline published 06-13 got logged at 06-14's
price, corrupting its reaction window. v2 fixes this:

  - each headline is timestamped by its RSS pubDate (actual publication time);
  - entry price and forward prices come from a CONTINUOUS hourly BTC series
    (Hyperliquid candleSnapshot), so reactions are measured from the real
    moment of publication and missed cycles are backfilled automatically;
  - reaction = raw forward return AND excess over the all-headline baseline
    (the latter separates a true sentiment effect from market drift).

Still DATA COLLECTION, not a signal. Headlines are largely endogenous
(describe moves that already happened); only persistent nonzero EXCESS over a
large sample would hint at a real effect. No trading use until studied.

Usage:
  python news_lab.py            # fetch feed, log new headlines by pubDate
  python news_lab.py --report   # reaction stats from hourly price history
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from email.utils import parsedate_to_datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import trend_bot
from trend_bot import _post_info

HEADS = _HERE / "news_lab_headlines.csv"
COLUMNS = ["pubdate_utc", "source", "sentiment", "title"]
HORIZONS_MIN = (60, 240, 1440)   # 1h / 4h / 24h — matches hourly price grid

BULL_WORDS = ("surge", "rally", "soar", "jump", "gain", "record inflow", "buys",
              "approval", "approve", "etf inflow", "all-time high", "breakout",
              "accumulat", "bullish", "rebound", "recover")
BEAR_WORDS = ("crash", "plunge", "selloff", "sell-off", "dump", "outflow",
              "hack", "exploit", "liquidat", "lawsuit", "sues", "ban",
              "bearish", "fear", "tumble", "slump", "pain ahead", "warning")


def classify(title: str) -> str:
    t = title.lower()
    bull = any(w in t for w in BULL_WORDS)
    bear = any(w in t for w in BEAR_WORDS)
    if bull and not bear:
        return "bullish"
    if bear and not bull:
        return "bearish"
    return "neutral"


def fetch_headlines() -> list[tuple[dt.datetime, str, str]]:
    """[(pubdate_utc, source, title)] from the agent's feeds."""
    import urllib.request, xml.etree.ElementTree as ET
    feeds = [("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
             ("cointelegraph", "https://cointelegraph.com/rss")]
    out = []
    for src, url in feeds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=10).read()
            for it in ET.fromstring(raw).findall(".//item"):
                title = (it.findtext("title") or "").strip()
                pd = it.findtext("pubDate")
                if not title or not pd:
                    continue
                try:
                    when = parsedate_to_datetime(pd).astimezone(dt.timezone.utc)
                except (TypeError, ValueError):
                    continue
                out.append((when, src, title))
        except Exception:
            continue
    return out


def btc_hourly(days: int = 7) -> list[tuple[int, float]]:
    """Continuous (ts_ms, close) hourly BTC series from Hyperliquid."""
    now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    data = _post_info({"type": "candleSnapshot", "req": {
        "coin": "BTC", "interval": "1h",
        "startTime": now - days * 86_400_000, "endTime": now}})
    return [(int(c["t"]), float(c["c"])) for c in data]


def _read() -> list[dict]:
    if not HEADS.exists():
        return []
    with open(HEADS, newline="") as fh:
        return list(csv.DictReader(fh))


def collect() -> int:
    seen = {r["title"] for r in _read()}
    new_file = not HEADS.exists()
    added = 0
    with open(HEADS, "a", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(COLUMNS)
        for when, src, title in fetch_headlines():
            if title in seen:
                continue
            w.writerow([when.strftime("%Y-%m-%dT%H:%M"), src, classify(title), title])
            seen.add(title)
            added += 1
    return added


def _price_at(series: list[tuple[int, float]], ts_ms: int, forward: bool) -> float | None:
    """Closest close at-or-after (forward) / at-or-before (entry) ts_ms."""
    if forward:
        cand = [(t, p) for t, p in series if t >= ts_ms]
        return min(cand, key=lambda x: x[0])[1] if cand else None
    cand = [(t, p) for t, p in series if t <= ts_ms]
    return max(cand, key=lambda x: x[0])[1] if cand else None


def report() -> str:
    heads = _read()
    if not heads:
        return "news lab: no headlines yet."
    series = btc_hourly()
    if len(series) < 2:
        return "news lab: price history unavailable."
    lines = [f"news lab: {len(heads)} headlines, "
             f"hourly BTC {dt.datetime.utcfromtimestamp(series[0][0]/1000):%m-%d} "
             f"-> {dt.datetime.utcfromtimestamp(series[-1][0]/1000):%m-%d}"]
    by: dict = {}
    for h in heads:
        try:
            t0 = dt.datetime.strptime(h["pubdate_utc"], "%Y-%m-%dT%H:%M").replace(
                tzinfo=dt.timezone.utc)
        except (KeyError, ValueError):
            continue
        t0ms = int(t0.timestamp() * 1000)
        p0 = _price_at(series, t0ms, forward=False)
        if p0 is None:
            continue   # headline predates our price window
        for hz in HORIZONS_MIN:
            p1 = _price_at(series, t0ms + hz * 60_000, forward=True)
            if p1 is None:
                continue
            by.setdefault((h["sentiment"], hz), []).append((p1 - p0) / p0 * 100)
    baseline = {hz: (lambda r: sum(r) / len(r) if r else 0.0)(
        [x for (s, hh), rs in by.items() if hh == hz for x in rs])
        for hz in HORIZONS_MIN}
    for (sent, hz), rets in sorted(by.items()):
        n = len(rets); avg = sum(rets) / n
        pos = sum(1 for r in rets if r > 0) / n * 100
        lines.append(f"  {sent:8} T+{hz:>4}m: n={n:<4} raw {avg:+.3f}%  "
                     f"EXCESS vs drift {avg - baseline[hz]:+.3f}%  ({pos:.0f}% pos)")
    lines.append("Read EXCESS, not raw (raw includes market drift). Anchored to "
                 "RSS pubDate + continuous hourly price — backfills missed cycles.")
    lines.append("CAVEAT: headlines largely endogenous; descriptive only until studied.")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        print(report())
    else:
        print(f"news lab: +{collect()} new headline(s) logged (by pubDate)")
