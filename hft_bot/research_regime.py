import json, math, datetime as dt
D=json.load(open("data_btc_daily_2015_2026.json"))
ts=[x["t"] for x in D]; c=[float(x["c"]) for x in D]; N=len(c)
dates=[dt.datetime.utcfromtimestamp(t/1000).date() for t in ts]
COST=0.00045
def idx(y):
    for i,d in enumerate(dates):
        if d>=dt.date(y,1,1): return i
    return N-1
def ens(i):
    if i<101: return 0.0
    v=0
    for w in (50,100): v+= 1 if c[i]>sum(c[i-w+1:i+1])/w else 0
    for w in (60,90): v+= 1 if c[i]>c[i-w] else 0
    return v/4
def vscale(i,tgt=0.40,lb=30):
    if i<lb: return 1.0
    r=[(c[k]-c[k-1])/c[k-1] for k in range(i-lb+1,i+1)]
    mu=sum(r)/len(r); sd=math.sqrt(sum((x-mu)**2 for x in r)/len(r))*math.sqrt(365)
    return min(1.0,tgt/sd) if sd>0 else 1.0
def regime_ok(i,w):  # close above w-day SMA = bull regime
    if i<w: return True
    return c[i] > sum(c[i-w+1:i+1])/w
def bt(posf,start,end,fund=0.08):
    eq=1.0;peak=1.0;mdd=0;rets=[];prev=0.0
    for i in range(start,end):
        p=posf(i)
        r=(c[i+1]-c[i])/c[i]*p - COST*abs(p-prev) - (fund/365)*p
        eq*=1+r; rets.append(r); peak=max(peak,eq); mdd=max(mdd,1-eq/peak); prev=p
    yrs=(ts[end]-ts[start])/86400000/365.25
    mu=sum(rets)/len(rets); sd=math.sqrt(sum((x-mu)**2 for x in rets)/len(rets))
    return eq**(1/yrs)-1, mdd, (mu/sd*math.sqrt(365) if sd else 0)
base=lambda i: ens(i)*vscale(i)
print("IN-SAMPLE 2015-2021: does a regime filter on the ensemble help?")
print(f"{'variant':28} {'CAGR%':>7} {'MaxDD%':>7} {'Sharpe':>7}")
for name,f in [("base ensemble",base),
               ("+ above 150d SMA", lambda i: base(i) if regime_ok(i,150) else 0.0),
               ("+ above 200d SMA", lambda i: base(i) if regime_ok(i,200) else 0.0)]:
    cagr,mdd,sh=bt(f,101,idx(2022))
    print(f"{name:28} {cagr*100:>7.1f} {mdd*100:>7.1f} {sh:>7.2f}")
print("\nOUT-OF-SAMPLE 2022-2026 confirmation:")
print(f"{'variant':28} {'CAGR%':>7} {'MaxDD%':>7} {'Sharpe':>7}")
for name,f in [("base ensemble",base),
               ("+ above 200d SMA", lambda i: base(i) if regime_ok(i,200) else 0.0)]:
    cagr,mdd,sh=bt(f,idx(2022),N-1)
    print(f"{name:28} {cagr*100:>7.1f} {mdd*100:>7.1f} {sh:>7.2f}")
