"""Multi-asset extension of Trend Bot v2 — FROZEN parameters from BTC research.

Discipline: the ensemble (SMA50/100, MOM60/90, 40% vol target) was tuned on
BTC 2015-2021 only. It is applied here to ETH and SOL with ZERO re-tuning, so
every non-BTC result is effectively out-of-sample for parameter choice. The
portfolio is 1/3 capital per asset (unallocated thirds sit in cash before an
asset's history begins). Costs: 4.5bp/side + 8% APR funding drag while long.

Usage: python research_portfolio.py
"""
import json, math, datetime as dt
from pathlib import Path

HERE = Path(__file__).parent
COST = 0.00045; FUND_APR = 0.08
SMA_W = (50, 100); MOM_W = (60, 90); VOL_LB = 30; VOL_TGT = 0.40

def load(p):
    d = json.load(open(HERE / p))
    return ([x["t"] for x in d], [float(x["c"]) for x in d])

def positions(c):
    n = len(c); pos = [0.0]*n
    need = max(max(SMA_W), max(MOM_W)) + 1
    for i in range(need, n):
        votes = 0
        for w in SMA_W: votes += 1 if c[i] > sum(c[i-w+1:i+1])/w else 0
        for w in MOM_W: votes += 1 if c[i] > c[i-w] else 0
        frac = votes/4
        rets = [(c[k]-c[k-1])/c[k-1] for k in range(i-VOL_LB+1, i+1)]
        mu = sum(rets)/len(rets)
        sd = math.sqrt(sum((x-mu)**2 for x in rets)/len(rets))*math.sqrt(365)
        pos[i] = frac * (min(1.0, VOL_TGT/sd) if sd > 0 else 1.0)
    return pos

def daily_strategy_returns(ts, c, pos):
    """{date: net daily return} applying pos[i] over close[i]->close[i+1]."""
    out = {}; prev = 0.0
    for i in range(len(c)-1):
        r = (c[i+1]-c[i])/c[i]*pos[i] - COST*abs(pos[i]-prev) - (FUND_APR/365)*pos[i]
        out[dt.datetime.utcfromtimestamp(ts[i+1]/1000).date()] = r
        prev = pos[i]
    return out

def stats(rets, label):
    if not rets: return
    eq = 1.0; peak = 1.0; mdd = 0.0
    for r in rets:
        eq *= 1+r; peak = max(peak, eq); mdd = max(mdd, 1-eq/peak)
    yrs = len(rets)/365.25
    mu = sum(rets)/len(rets); sd = math.sqrt(sum((x-mu)**2 for x in rets)/len(rets))
    sharpe = mu/sd*math.sqrt(365) if sd else 0
    print(f"  {label:28} CAGR {(eq**(1/yrs)-1)*100:+6.1f}%  MaxDD {mdd*100:5.1f}%  "
          f"Sharpe {sharpe:5.2f}  ${1000*eq:,.0f}")
    return eq, mdd, sharpe

assets = {}
for name, path in [("BTC","data_btc_daily_2015_2026.json"),
                   ("ETH","data_eth_daily.json"), ("SOL","data_sol_daily.json")]:
    ts, c = load(path)
    assets[name] = daily_strategy_returns(ts, c, positions(c))

OOS = dt.date(2022,1,1)
print("--- Per-asset, frozen BTC params, OOS 2022+ (8% funding, 4.5bp/side) ---")
for name, dr in assets.items():
    stats([r for d,r in sorted(dr.items()) if d >= OOS], f"{name} trend ensemble")

print("\n--- Portfolio: 1/3 capital per asset, OOS 2022+ ---")
all_days = sorted(set().union(*[set(a.keys()) for a in assets.values()]))
port = []
for d in all_days:
    if d < OOS: continue
    port.append(sum(assets[a].get(d, 0.0)/3 for a in assets))
res = stats(port, "PORTFOLIO (BTC+ETH+SOL)")

print("\n--- Portfolio year-by-year (OOS) ---")
for y in range(2022, 2027):
    yr = [sum(assets[a].get(d,0.0)/3 for a in assets)
          for d in all_days if d.year == y and d >= OOS]
    if not yr: continue
    eq=1.0
    for r in yr: eq*=1+r
    neg = sum(1 for m in range(1,13)
              if (mr:=[r for d2,r in zip([d for d in all_days if d.year==y and d>=OOS], yr) if d2.month==m])
              and math.prod(1+x for x in mr) < 1)
    print(f"  {y}: {(eq-1)*100:+7.1f}%   losing months: {neg}/12")

# --- Bootstrap consistency: resample OOS portfolio daily returns ---
print("\n--- Bootstrap (10k resamples of OOS portfolio days) ---")
import random
random.seed(42)
n = len(port)
neg_year = 0; worst = []
for _ in range(10_000):
    yr = [port[random.randrange(n)] for _ in range(365)]
    eq = 1.0
    for r in yr: eq *= 1+r
    if eq < 1: neg_year += 1
    worst.append(eq)
worst.sort()
print(f"  P(negative 12-month period): {neg_year/100:.1f}%")
print(f"  5th percentile 12-mo outcome: {(worst[500]-1)*100:+.1f}%   "
      f"median: {(worst[5000]-1)*100:+.1f}%   95th: {(worst[9500]-1)*100:+.1f}%")
