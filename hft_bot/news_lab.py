"""News lab — side research: how does the market react to our news feed?

Every 30-min loop cycle this logs (a) a price snapshot for BTC/ETH/SOL and
(b) any NEW headlines from the agent's news sources (CoinDesk/Cointelegraph),
tagged with a transparent keyword sentiment. Forward returns at 30m/2h/24h are
resolved from later snapshots. --report prints reaction stats per sentiment.

This is DATA COLLECTION, not a signal. Big honest caveat baked into the report:
headlines are mostly ENDOGENOUS — they describe moves that already happened
("BTC tags $63K"), so naive correlation overstates causality. The later study
must separate anticipatory headlines (ETF filings, hacks) from descriptive ones.
We collect now, decide what it means later.

Usage:
  python news_lab.py            # one collection cycle (headlines + snapshot)
  python news_lab.py --report   # reaction stats so far
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
from trend_bot import _post_info

HEADS = _HERE / "news_lab_headlines.csv"
PRICES = _HERE / "news_lab_prices.csv"
COINS = ("BTC", "ETH", "SOL")
HORIZONS_MIN = (30, 120, 1440)

# Transparent keyword sentiment — crude on purpose; auditable and stable.
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


def fetch_headlines(limit: int = 12) -> list[tuple[str, str]]:
    """[(source, title)] from the same feeds the agent uses."""
    import urllib.request, xml.etree.ElementTree as ET
    feeds = [("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
             ("cointelegraph", "https://cointelegraph.com/rss")]
    out = []
    for src, url in feeds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=10).read()
            for it in ET.fromstring(raw).findall(".//item")[:limit]:
                t = (it.findtext("title") or "").strip()
                if t:
                    out.append((src, t))
        except Exception:
            continue
    return out


def snapshot_prices() -> dict[str, float]:
    mids = _post_info({"type": "allMids"})
    return {c: float(mids.get(c, 0)) for c in COINS}


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def collect() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M")
    px = snapshot_prices()

    new_p = not PRICES.exists()
    with open(PRICES, "a", newline="") as fh:
        w = csv.writer(fh)
        if new_p:
            w.writerow(["ts"] + [c.lower() for c in COINS])
        w.writerow([ts] + [f"{px[c]:.4f}" for c in COINS])

    seen = {r["title"] for r in _read(HEADS)}
    new_h = not HEADS.exists()
    added = 0
    with open(HEADS, "a", newline="") as fh:
        w = csv.writer(fh)
        if new_h:
            w.writerow(["ts", "source", "sentiment", "title",
                        "btc_px", "eth_px", "sol_px"])
        for src, title in fetch_headlines():
            if title in seen:
                continue
            w.writerow([ts, src, classify(title), title,
                        f"{px['BTC']:.2f}", f"{px['ETH']:.4f}", f"{px['SOL']:.4f}"])
            seen.add(title)
            added += 1
    return added


def report() -> str:
    heads = _read(HEADS)
    prices = _read(PRICES)
    if not heads or len(prices) < 2:
        return f"news lab: {len(heads)} headlines, {len(prices)} snapshots — keep collecting."
    snaps = [(dt.datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M"), float(r["btc"]))
             for r in prices]
    lines = [f"news lab: {len(heads)} headlines, {len(prices)} snapshots"]
    by = {}
    for h in heads:
        t0 = dt.datetime.strptime(h["ts"], "%Y-%m-%dT%H:%M")
        p0 = float(h["btc_px"])
        for hz in HORIZONS_MIN:
            target = t0 + dt.timedelta(minutes=hz)
            later = [(abs((s - target).total_seconds()), p) for s, p in snaps if s >= target]
            if not later:
                continue
            _, p1 = min(later)
            by.setdefault((h["sentiment"], hz), []).append((p1 - p0) / p0 * 100)
    for (sent, hz), rets in sorted(by.items()):
        n = len(rets)
        avg = sum(rets) / n
        pos = sum(1 for r in rets if r > 0) / n * 100
        lines.append(f"  {sent:8} T+{hz:>4}m: n={n:<4} avg BTC move {avg:+.3f}%  "
                     f"({pos:.0f}% positive)")
    lines.append("CAVEAT: headlines are largely endogenous (describe past moves);"
                 " treat as descriptive stats, not causal signal, until studied.")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        print(report())
    else:
        n = collect()
        print(f"news lab: +{n} new headline(s) logged, snapshot taken")
