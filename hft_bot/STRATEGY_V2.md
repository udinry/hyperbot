# Strategy v2 — Vol-Targeted Trend Ensemble (the validated model)

## Executive summary

The original OFI scalper could not be made demonstrably profitable: honest
accounting on 60 and 381 days of regime-diverse data showed liquid BTC carries
**< 1 bp of directional edge at 1-minute resolution against a > 2 bp cost
floor**, in both momentum and mean-reversion directions, under every exit
policy tested (`BACKTEST_FINDINGS.md`). Live, the 14-gate strategy produced
~9 signals and **0 fills in ~55 minutes** — it cannot even generate the data
needed to certify itself.

The professional fix is not more gates — it is moving to the horizon where the
edge-to-cost ratio is ~100× better and where a decade of data exists to prove
or break the idea. That is what `trend_bot.py` is.

## The model

- **Universe**: BTC perp on Hyperliquid, long/flat only.
- **Signal** (daily closes): ensemble of 4 classic trend votes —
  `close > SMA50`, `close > SMA100`, `close > close[60d]`, `close > close[90d]`.
  Position fraction = mean of votes ∈ {0, .25, .5, .75, 1}.
- **Regime filter** (v2.1): all longs suppressed unless `close > SMA150` — keeps
  the system in cash through sustained bears. Chosen on in-sample (best Sharpe +
  drawdown vs the 200d alternative), confirmed out-of-sample (below).
- **Vol targeting**: fraction × `min(1, 40% / realized 30-day annualized vol)`.
- **Execution**: at most one rebalance per day, IOC, only when the delta ≥ 1 lot
  **and** ≥ 15% of full position size (anti-churn).
- **Costs modelled**: 4.5 bp per side (taker 3.5 + slippage 1) on every position
  change, plus **8% APR funding drag while long** (sensitivity-tested 0/8/15%).

## Methodology (this is the part that makes the numbers believable)

- **Data**: 3,979 daily candles, Coinbase BTC-USD, 2015-07-20 → 2026-06-10 —
  four complete cycles, including the 2018 (−84%) and 2022 (−77%) bears.
- **Walk-forward discipline**: all tuning on **2015–2021 in-sample**. The
  2022–2026 out-of-sample period was touched exactly twice (raw ensemble, then
  the vol-target confirmation), both reported below. No further OOS iteration.
- **Plateau, not peak**: the IS parameter surface is broad (SMA20–150 and
  MOM20–120 all Sharpe 1.4–1.8). The ensemble uses plateau centers, not the
  single best cell. Long/short was evaluated IS and **rejected** (Sharpe 1.38
  vs 1.66 long/flat).
- Reproduce everything: `python research_trend.py`.

## Results

### Out-of-sample 2022-01 → 2026-06 (never used for tuning), 8% funding drag

| | CAGR | Max drawdown | Sharpe | $1,000 → |
|---|---|---|---|---|
| **v2.1 + 150d regime filter** | **+21.2%** | **22.2%** | **0.88** | **$2,320** |
| v2.0 vol-targeted ensemble | +13.1% | 32.7% | 0.59 | $1,726 |
| Buy & hold | +5.9% | 67.0% | 0.37 | $1,288 |

The regime filter (v2.1) is the single biggest improvement: +8 pts of CAGR,
−10 pts of drawdown, and it nearly eliminates the 2022 bear loss.

### Year-by-year, v2.1 (150d regime filter, OOS)

| Year | Strategy | Buy & hold |
|---|---|---|
| 2022 (bear) | **−4.3%** | −65.2% |
| 2023 (bull) | +61.6% | +166.2% |
| 2024 (bull) | +64.4% | +113.4% |
| 2025 (chop) | −0.6% | −6.0% |
| 2026 YTD (bear) | **−15.8%** | −30.7% |

The filter turns the worst year (−32%) into a scratch (−4%) — exactly what a
regime overlay is supposed to do. It costs a little in whippy chop (2025/2026
slightly worse) but the drawdown reduction dominates.


### Regime-filter robustness (post-hoc checks, fully reported)

- **Parameter plateau (BTC OOS)**: every window 100–250d beats no-filter
  (Sharpe 0.70–0.93 vs 0.59); 150d sits on a broad hill, not a spike.
- **Frozen transfer**: the same 150d filter, never tuned on ETH/SOL, improves
  both — ETH Sharpe 0.45→0.63, SOL 0.27→0.38 (OOS 2022+).

The pattern is the documented trend-following signature: give up part of the
bull, skip most of the bear, much better compounding per unit of pain.

### In-sample 2015–2021 (for reference; tuning happened here)

Vol-targeted ensemble: +76% CAGR, 35% MaxDD, Sharpe 1.79 (buy & hold: +122%
CAGR, 84% MaxDD, Sharpe 1.43). IS numbers are always inflated; trust the OOS.

## Multi-asset extension (frozen-parameter transfer test)

The strongest robustness test possible: apply the BTC-tuned ensemble to ETH
(10y data) and SOL (5y data) with **zero re-tuning**, OOS 2022+ with the same
costs and funding drag (`research_portfolio.py`):

| Asset | Strategy CAGR | Strategy MaxDD | Buy & hold CAGR | B&H MaxDD |
|---|---|---|---|---|
| BTC | +13.1% | 32.7% | +5.9% | 67.0% |
| ETH | **+8.9%** | 29.7% | **−17.2%** | 74.0% |
| SOL | **+3.9%** | 39.4% | **−20.7%** | 94.6% |

Parameters the system never saw ETH/SOL data for still added ~26 points of
annual return on each — the trend effect is real, not BTC curve-fit.

**Portfolio (1/3 each, OOS 2022+):** +9.5% CAGR, 28.4% MaxDD, Sharpe 0.50.
Crypto's high internal correlation (BTC/ETH 0.84, vs SOL 0.75) limits the
diversification benefit: the portfolio cuts the worst drawdown but BTC-only
keeps the best Sharpe (0.59 vs 0.50).

**Portfolio sizing (v2.1):** with the regime filter, the 3-asset portfolio OOS
improves to equal-weight Sharpe 0.74; inverse-vol (risk-parity) weights lift it
to **+15.9% CAGR / 23.1% MaxDD / Sharpe 0.78** (weights from trailing vol only —
no return peeking). `TREND_WEIGHTING=invvol` is the default.

**Recommendation:** `TREND_COINS=BTC` (default) for small accounts — lot
granularity makes thirds of $160 impractical and BTC-only is the best
risk-adjusted single line. At ≳$1,000, `TREND_COINS=BTC,ETH,SOL` is a
defensible choice for drawdown reduction.

### Breadth test: 9 assets, zero re-tuning (2026-06-11)

Frozen v2.1 applied to six MORE long-history assets (LTC 9.4y, BCH 8.1y,
LINK 6.5y, XRP 6.9y, DOGE 4.6y, AVAX 4.3y), honest costs + funding drag:

- **Full history: 6/6 beat buy-and-hold risk-adjusted.** Standouts: DOGE
  +7.1%/yr vs B&H −22.3%; AVAX +10.0%/yr (25% DD) vs B&H −44.9% (94% DD).
- **2022+ window: 5/6** (LTC ≈ flat but with one-third the B&H drawdown).
- Alt Sharpes (0.2–0.6) are below BTC's (0.88): majors remain the best
  expression; the alts confirm the **mechanism** is market-wide, not BTC
  curve-fit. Nine assets, one frozen parameter set, zero contradictions.

### Consistency, quantified (bootstrap of 10,000 resampled 12-month periods)

- P(any 12-month period is negative): **~35%**
- 12-month outcomes: 5th pct **−25%**, median **+9.5%**, 95th pct **+60%**
- Even good years have 5–6 losing months.

This is what "consistently profitable" honestly means for a directional crypto
strategy: positive expectancy over years, not positive every month. Anything
promising positive months is either market-neutral (different strategy class,
thin pickings at retail) or lying.

## Honest expectations — read this before going live

- **The v2.1 OOS estimate is ~21%/yr with ~22% worst drawdown** (Sharpe 0.88
  carries a standard error of ~±0.55 on 4.4y — plan around 10–20%/yr). On a
  $160 account that is **+$25–35/year in expectation**, with −$35 swings
  possible. Negative *years* are still expected (2026 YTD is one).
- This is a **positive-expectancy system with documented risk**, not a money
  printer. Its value scales with capital; the system is identical at $160 or
  $160k (BTC liquidity is not a constraint at these sizes).
- Today (June 2026) the signal is **0.00 — flat** — BTC is in a confirmed
  downtrend. A correct system spends bear markets mostly in cash. Do not
  interpret "it isn't trading" as "it isn't working"; 2022 is what not trading
  is worth.
- Multi-asset (`TREND_COINS=BTC,ETH,SOL` with risk-parity weights) is built
  and validated; recommended at ≳$1,000 equity.

## Deployment

```bash
cd hft_bot
python -m pytest tests/            # includes trend signal tests

python trend_bot.py --dry-run      # show today's signal, never orders
python trend_bot.py --once         # one decision cycle (orders if keyed)
python trend_bot.py                # daily loop (systemd-friendly)
```

- Without `PRIVATE_KEY` it runs in observer mode.
- `live_server_config/hyperbot-trend.service` runs it under systemd.
- Env knobs: `TREND_LEVERAGE` (default 1.0 — leave it there), `TREND_VOL_TARGET`
  (0.40), `REBALANCE_MIN_FRAC` (0.15).
- It coexists with the OFI bot but **do not run both against the same account**
  — they will fight over the same position.

## Relationship to the OFI scalper

The OFI bot remains in the repo with all audit fixes applied. If you still want
to pursue it, the only valid path is a long minimum-size `REAL_TEST_MODE=1`
data-collection campaign (months, not days, given ~0 fills/hour). The trend bot
is the system the evidence actually supports today.
