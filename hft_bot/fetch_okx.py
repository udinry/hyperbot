"""Fetch deep 1-minute BTC history from OKX (perp) for backtesting.

Hyperliquid's candleSnapshot caps at ~5000 candles (~3.5 days). OKX BTC-USDT-SWAP
tracks HL's BTC perp to within a few bps and paginates back months, giving the
regime diversity (downtrends, crashes, chop) that a single HL window cannot.

Output JSON is the same shape backtest.py --file expects (HL candle format):
  [{"t": ms, "o","h","l","c": str, "v": float_btc, "n": int}, ...]

Usage:  python fetch_okx.py --days 365 --out okx_btc_1m_1y.json
"""
from __future__ import annotations
import argparse, json, time, datetime as dt
import requests

URL = "https://www.okx.com/api/v5/market/history-candles"

def fetch(inst: str, days: int, out: str, time_cap_s: int = 600) -> None:
    s = requests.Session(); s.headers.update({"User-Agent": "Mozilla/5.0"})
    rows, seen, after, t0 = [], set(), None, time.time()
    while True:
        p = {"instId": inst, "bar": "1m", "limit": "300"}
        if after: p["after"] = after
        data = None
        for attempt in range(5):
            try:
                j = s.get(URL, params=p, timeout=15).json()
                if j.get("code") == "0": data = j["data"]; break
            except Exception: pass
            time.sleep(0.5 * (attempt + 1))
        if not data: break
        new = [d for d in data if d[0] not in seen]
        if not new: break
        for d in new: seen.add(d[0])
        rows.extend(new); after = data[-1][0]
        span = (int(rows[0][0]) - int(data[-1][0])) / 86400000
        if span >= days: break
        if time.time() - t0 > time_cap_s:
            print(f"  time cap hit at {span:.1f} days"); break
        time.sleep(0.06)
    rows.sort(key=lambda d: int(d[0]))
    # OKX row: [ts,o,h,l,c,vol(contracts),volCcy(BTC),volCcyQuote,confirm]
    hl = [{"t": int(d[0]), "o": d[1], "h": d[2], "l": d[3], "c": d[4],
           "v": float(d[6]), "n": int(float(d[5]))} for d in rows]
    json.dump(hl, open(out, "w"))
    if hl:
        o = dt.datetime.utcfromtimestamp(hl[0]["t"]/1000)
        n = dt.datetime.utcfromtimestamp(hl[-1]["t"]/1000)
        print(f"saved {len(hl)} candles -> {out} | {o} -> {n} "
              f"| {(hl[-1]['t']-hl[0]['t'])/86400000:.1f} days")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--inst", default="BTC-USDT-SWAP")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default="okx_btc_1m.json")
    ap.add_argument("--time-cap", type=int, default=600)
    a = ap.parse_args()
    fetch(a.inst, a.days, a.out, a.time_cap)
