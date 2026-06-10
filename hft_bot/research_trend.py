"""Reproducible research for Trend Bot v2 (see STRATEGY_V2.md).

Usage:
    python research_trend.py [daily_candles.json]

Defaults to the committed data_btc_daily_2015_2026.json (Coinbase BTC-USD daily).
To refresh the dataset, see fetch_okx.py for the OKX intraday fetcher; daily
candles came from Coinbase /products/BTC-USD/candles (granularity=86400),
paginated 295 days per request since 2015-07-20.

Discipline notes:
- In-sample period 2015-2021: all tuning happened here.
- Out-of-sample 2022+: touched exactly twice (raw ensemble; vol-target confirm),
  both reported in STRATEGY_V2.md. Do not iterate further on OOS.
"""
"""Daily-horizon BTC strategy research with honest accounting + walk-forward.
Costs: 4.5bp/side (taker 3.5 + slippage 1) on every position change.
Funding drag while long perp: configurable APR (sensitivity-tested).
IS = 2015-2021 (tuning allowed). OOS = 2022+ (touched once, at the end).
"""
import json, math, datetime as dt

import sys, urllib.request, time as _t
_DATA = sys.argv[1] if len(sys.argv) > 1 else str(__import__("pathlib").Path(__file__).parent / "data_btc_daily_2015_2026.json")
D = json.load(open(_DATA))
ts = [x["t"] for x in D]
c  = [float(x["c"]) for x in D]
h  = [float(x["h"]) for x in D]
l  = [float(x["l"]) for x in D]
dates = [dt.datetime.utcfromtimestamp(t/1000).date() for t in ts]
N = len(c)

COST = 0.00045          # per side
def backtest(pos, fund_apr=0.08, start=0, end=None):
    """pos[i] in {0,1} decided on close[i], applied to return close[i]->close[i+1]."""
    end = end or N-1
    eq = 1.0; peak = 1.0; mdd = 0.0
    rets = []
    prev = 0
    daily_fund = fund_apr/365
    for i in range(start, end):
        p = pos[i]
        r = (c[i+1]-c[i])/c[i] * p
        cost = COST*2*abs(p-prev)      # entry+exit legs charged on change... per side each change
        r -= COST*abs(p-prev)          # one side per unit change
        if p>0: r -= daily_fund
        eq *= (1+r); rets.append(r)
        peak = max(peak, eq); mdd = max(mdd, 1-eq/peak)
        prev = p
    yrs = (ts[end]-ts[start])/86400000/365.25
    cagr = eq**(1/yrs)-1 if yrs>0 else 0
    mu = sum(rets)/len(rets); sd = (sum((x-mu)**2 for x in rets)/len(rets))**0.5
    sharpe = mu/sd*math.sqrt(365) if sd>0 else 0
    return dict(eq=eq, cagr=cagr, mdd=mdd, sharpe=sharpe,
                trades=sum(1 for i in range(start+1,end) if pos[i]!=pos[i-1]))

def sma_pos(n):
    pos=[0]*N
    s=0.0
    for i in range(N):
        s+=c[i]
        if i>=n: s-=c[i-n]
        if i>=n-1 and c[i] > s/n: pos[i]=1
    return pos

def mom_pos(n):
    return [1 if i>=n and c[i]>c[i-n] else 0 for i in range(N)]

def donchian_pos(n_in, n_out):
    pos=[0]*N; inpos=False
    for i in range(N):
        if i< n_in: continue
        hh=max(c[i-n_in:i]); ll=min(c[i-max(n_out,1):i])
        if not inpos and c[i]>=hh: inpos=True
        elif inpos and c[i]<=ll: inpos=False
        pos[i]=1 if inpos else 0
    return pos

def idx_of(year):  # first index with date >= Jan1 year
    for i,d in enumerate(dates):
        if d>=dt.date(year,1,1): return i
    return N-1

IS_END = idx_of(2022)
print(f"data: {dates[0]} -> {dates[-1]} ({N}d) | IS=2015-2021 ({IS_END}d), OOS=2022+ ({N-1-IS_END}d)")
print(f"\n--- IN-SAMPLE 2015-2021 (tuning allowed) | fund drag 8% APR while long ---")
print(f"{'strategy':18} {'CAGR%':>7} {'MaxDD%':>7} {'Sharpe':>7} {'trades':>6}")
bh = backtest([1]*N, fund_apr=0.0, start=0, end=IS_END)
print(f"{'buy&hold(spot)':18} {bh['cagr']*100:>7.1f} {bh['mdd']*100:>7.1f} {bh['sharpe']:>7.2f} {'-':>6}")
results={}
for n in [20,50,100,150,200,300]:
    r = backtest(sma_pos(n), start=0, end=IS_END)
    results[f"SMA{n}"]=r
    print(f"{'SMA'+str(n):18} {r['cagr']*100:>7.1f} {r['mdd']*100:>7.1f} {r['sharpe']:>7.2f} {r['trades']:>6}")
for n in [20,30,60,90,120,180]:
    r = backtest(mom_pos(n), start=0, end=IS_END)
    results[f"MOM{n}"]=r
    print(f"{'MOM'+str(n):18} {r['cagr']*100:>7.1f} {r['mdd']*100:>7.1f} {r['sharpe']:>7.2f} {r['trades']:>6}")
for a,b in [(20,10),(55,20),(100,50)]:
    r = backtest(donchian_pos(a,b), start=0, end=IS_END)
    results[f"DON{a}/{b}"]=r
    print(f"{f'DON{a}/{b}':18} {r['cagr']*100:>7.1f} {r['mdd']*100:>7.1f} {r['sharpe']:>7.2f} {r['trades']:>6}")

# ---------- PRE-REGISTERED CHOICE (made on IS only): ensemble of plateau centers ----------
def ensemble_pos():
    ps=[sma_pos(50), sma_pos(100), mom_pos(60), mom_pos(90)]
    return [sum(p[i] for p in ps)/4 for i in range(N)]

def backtest_frac(pos, fund_apr=0.08, start=0, end=None):
    end = end or N-1
    eq=1.0; peak=1.0; mdd=0.0; rets=[]; prev=0.0; tr=0
    for i in range(start, end):
        p=pos[i]
        r=(c[i+1]-c[i])/c[i]*p - COST*abs(p-prev) - (fund_apr/365)*p
        eq*=(1+r); rets.append(r)
        peak=max(peak,eq); mdd=max(mdd,1-eq/peak)
        if abs(p-prev)>1e-9: tr+=1
        prev=p
    yrs=(ts[end]-ts[start])/86400000/365.25
    mu=sum(rets)/len(rets); sd=(sum((x-mu)**2 for x in rets)/len(rets))**0.5
    return dict(eq=eq, cagr=eq**(1/yrs)-1, mdd=mdd,
                sharpe=mu/sd*math.sqrt(365) if sd>0 else 0, trades=tr)

ens = ensemble_pos()
print("\n--- OUT-OF-SAMPLE 2022-2026 (untouched until now) ---")
print(f"{'strategy':22} {'fund':>5} {'CAGR%':>7} {'MaxDD%':>7} {'Sharpe':>7} {'trades':>6} {'$1000->':>9}")
oos0=IS_END
bh = backtest_frac([1.0]*N, fund_apr=0.0, start=oos0)
print(f"{'buy&hold(spot)':22} {'0%':>5} {bh['cagr']*100:>7.1f} {bh['mdd']*100:>7.1f} {bh['sharpe']:>7.2f} {'-':>6} {1000*bh['eq']:>9.0f}")
for fa in [0.0, 0.08, 0.15]:
    r = backtest_frac(ens, fund_apr=fa, start=oos0)
    print(f"{'ENSEMBLE(50/100/60/90)':22} {f'{fa*100:.0f}%':>5} {r['cagr']*100:>7.1f} {r['mdd']*100:>7.1f} {r['sharpe']:>7.2f} {r['trades']:>6} {1000*r['eq']:>9.0f}")
for nm,p in [("SMA50",sma_pos(50)),("MOM60",mom_pos(60))]:
    r = backtest_frac([float(x) for x in p], fund_apr=0.08, start=oos0)
    print(f"{nm:22} {'8%':>5} {r['cagr']*100:>7.1f} {r['mdd']*100:>7.1f} {r['sharpe']:>7.2f} {r['trades']:>6} {1000*r['eq']:>9.0f}")

print("\n--- OOS year by year (ensemble, 8% funding drag) ---")
for y in range(2022, 2027):
    a=idx_of(y); b=min(idx_of(y+1), N-1)
    if a>=b: continue
    r=backtest_frac(ens, fund_apr=0.08, start=a, end=b)
    bhr=(c[b]-c[a])/c[a]*100
    print(f"  {y}: strategy {(r['eq']-1)*100:+7.1f}%   buy&hold {bhr:+7.1f}%   maxDD {r['mdd']*100:.1f}%")

# ---------- Vol targeting (standard, decided ex ante): pos *= min(1, tgt/realized) ----------
def vol_scaled(pos, tgt_ann=0.40, lookback=30):
    out=[0.0]*N
    for i in range(N):
        if i<lookback: continue
        rets=[(c[k]-c[k-1])/c[k-1] for k in range(i-lookback+1,i+1)]
        mu=sum(rets)/len(rets)
        sd=(sum((x-mu)**2 for x in rets)/len(rets))**0.5*math.sqrt(365)
        scale=min(1.0, tgt_ann/sd) if sd>0 else 1.0
        out[i]=pos[i]*scale
    return out

print("\n--- Vol-targeted ensemble: IN-SAMPLE check (8% funding) ---")
for tgt in [0.30,0.40,0.50]:
    vs=vol_scaled(ens,tgt)
    r=backtest_frac(vs,fund_apr=0.08,start=0,end=IS_END)
    print(f"  tgt {tgt*100:.0f}%: CAGR {r['cagr']*100:6.1f}%  MaxDD {r['mdd']*100:5.1f}%  Sharpe {r['sharpe']:.2f}")
r=backtest_frac(ens,fund_apr=0.08,start=0,end=IS_END)
print(f"  raw    : CAGR {r['cagr']*100:6.1f}%  MaxDD {r['mdd']*100:5.1f}%  Sharpe {r['sharpe']:.2f}")

print("\n--- Vol-targeted ensemble 40%: SINGLE OOS confirmation (8% funding) ---")
vs=vol_scaled(ens,0.40)
r=backtest_frac(vs,fund_apr=0.08,start=IS_END)
print(f"  OOS 2022-2026: CAGR {r['cagr']*100:+.1f}%  MaxDD {r['mdd']*100:.1f}%  Sharpe {r['sharpe']:.2f}  $1000->{1000*r['eq']:.0f}")
for y in range(2022,2027):
    a=idx_of(y); b=min(idx_of(y+1),N-1)
    if a>=b: continue
    ry=backtest_frac(vs,fund_apr=0.08,start=a,end=b)
    print(f"    {y}: {(ry['eq']-1)*100:+7.1f}%  maxDD {ry['mdd']*100:.1f}%")

# Long/short variant — IS only (expect worse in crypto; documenting why rejected)
def ls_pos():
    return [p*2-1 for p in ens]   # 1->long, 0->short (fractional in between)
r=backtest_frac(ls_pos(),fund_apr=0.04,start=0,end=IS_END)
print(f"\n  [IS-only] long/short ensemble: CAGR {r['cagr']*100:.1f}% MaxDD {r['mdd']*100:.1f}% Sharpe {r['sharpe']:.2f} -> {'REJECT' if r['sharpe']<1.6 else 'consider'}")
