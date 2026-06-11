"""Vet scanner candidates: frozen v2.1 strategy on each coin's FULL history
(HL daily candles), honest costs. A candidate must show the trend profile
(beats buy-and-hold risk-adjusted) on its own history to earn a watchlist spot.

Selection-bias warning (printed with results): liquid alts are liquid BECAUSE
they pumped — backtesting a coin that survived inflates expectations. Treat
results as a screen, not proof; position sizes for alts should be smaller than
majors and history < ~2.5y is flagged UNRELIABLE.

Usage: python vet_candidates.py HYPE ZEC WLD NEAR ...
"""
from __future__ import annotations
import math, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import trend_bot
from trend_bot import _post_info

COST = 0.00045
FUND = 0.08

def vet(coin: str) -> dict | None:
    import time
    now = int(time.time() * 1000)
    data = _post_info({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1d",
        "startTime": now - 4000 * 86_400_000, "endTime": now}})
    c = [float(x["c"]) for x in data]
    n = len(c)
    if n < trend_bot.MIN_HISTORY_D + 60:
        return {"coin": coin, "days": n, "verdict": "INSUFFICIENT HISTORY"}
    eq = 1.0; peak = 1.0; mdd = 0.0; rets = []; prev = 0.0
    bh_peak = c[trend_bot.MIN_HISTORY_D]; bh_mdd = 0.0
    start = trend_bot.MIN_HISTORY_D
    for i in range(start, n - 1):
        frac = trend_bot.ensemble_fraction(c[:i+1])
        scale = trend_bot.vol_scale(c[:i+1])
        p = frac * scale
        r = (c[i+1]-c[i])/c[i]*p - COST*abs(p-prev) - (FUND/365)*p
        eq *= 1+r; rets.append(r)
        peak = max(peak, eq); mdd = max(mdd, 1-eq/peak)
        bh_peak = max(bh_peak, c[i+1]); bh_mdd = max(bh_mdd, 1-c[i+1]/bh_peak)
        prev = p
    yrs = (n - start) / 365.25
    mu = sum(rets)/len(rets); sd = math.sqrt(sum((x-mu)**2 for x in rets)/len(rets))
    sharpe = mu/sd*math.sqrt(365) if sd else 0
    cagr = eq**(1/yrs)-1
    bh_cagr = (c[-1]/c[start])**(1/yrs)-1
    reliable = yrs >= 2.5
    good = sharpe > 0.7 and mdd < bh_mdd
    verdict = ("WATCHLIST" if good else "REJECT") + ("" if reliable else " (UNRELIABLE <2.5y)")
    return {"coin": coin, "days": n, "yrs": yrs, "cagr": cagr, "mdd": mdd,
            "sharpe": sharpe, "bh_cagr": bh_cagr, "bh_mdd": bh_mdd, "verdict": verdict}

if __name__ == "__main__":
    coins = sys.argv[1:] or ["HYPE"]
    print(f"{'coin':8} {'yrs':>4} {'CAGR%':>8} {'MaxDD%':>7} {'Sharpe':>7} "
          f"{'B&H CAGR%':>10} {'B&H DD%':>8}  verdict")
    for coin in coins:
        try:
            r = vet(coin)
        except Exception as exc:
            print(f"{coin:8} error: {exc}"); continue
        if "yrs" not in r:
            print(f"{r['coin']:8} {r['days']}d — {r['verdict']}"); continue
        print(f"{r['coin']:8} {r['yrs']:>4.1f} {r['cagr']*100:>8.1f} "
              f"{r['mdd']*100:>7.1f} {r['sharpe']:>7.2f} {r['bh_cagr']*100:>10.1f} "
              f"{r['bh_mdd']*100:>8.1f}  {r['verdict']}")
    print("\nNOTE: survivorship/selection bias — liquid alts are liquid because "
          "they pumped. Screen, not proof.")
