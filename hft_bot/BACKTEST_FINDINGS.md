# Backtest Findings — why the candle numbers can't certify profitability

This file records what the honest backtesting found, so the conclusions aren't
lost. Tools: `fetch_okx.py` (deep history) + `honest_backtest.py` (realistic
fills/fees/path). Run on 60 days of OKX BTC-USDT-SWAP 1-minute data spanning
uptrend, chop, downtrend, and a −17.6% crash week.

## 1. The built-in candle backtest (backtest.py Mode A) is doubly flawed

**Optimistic accounting.** It fills every signal, credits half-spread + maker
rebate to every trade, and ignores the real TP/SL path. On the same 60 days it
reported **+$774**; honest accounting (real ALO fills, taker fees on non-TP
exits, actual path) on the *same signals* gave **−$156 to −$376** across every
hold-time / SL-TP / tie-break combination. The sign flips entirely on accounting.

**Look-ahead in the accuracy metric.** Mode A scores P&L from the *signal bar's
own mid*, which bakes in that bar's forward move. Measured from the realistic
entry (the next bar's open, where you'd actually fill), directional accuracy is:

| Horizon | Momentum (current) | Mean-reversion (fade) |
|---|---|---|
| T+1min | 46.1% | 52.7% |
| T+5min | 45.8% | 53.1% |

The current momentum proxy is **worse than a coin flip** at realistic entry; the
reported 82.7%/84.7% was the artifact.

## 2. No 1-minute directional edge survives costs (either direction)

Fading the signal is genuinely better than chance (~53%), but the raw edge is
**< 1 basis point** (median +0.3 to +0.7 bps), while the round-trip cost floor is:
- maker+maker ≈ 0 to −2 bps (after +2 bps rebate, before adverse selection)
- maker+taker ≈ −2.5 bps
- taker+taker ≈ −7 bps

A sub-1-bp edge cannot beat a >2-bp cost floor. Honest P&L for the mean-reversion
variant was **−$19 to −$33/day** despite the positive directional accuracy —
more signals just means more cost bleed. **Conclusion: liquid BTC has no
exploitable directional edge at 1-minute resolution after transaction costs.**

## 2b. Confirmed across a full year (381 days, all regimes)

Re-running on 381 days of OKX BTC 1m (May 2025 → Jun 2026: strong uptrends
Jul'25/Apr'26, crashes Nov'25 −17.6%, Feb'26 −15%, Jun'26 −17%, plus chop):

- Realistic-entry directional accuracy: **45.8–47.1%** at every horizon (still
  sub-coin-flip); median edge negative everywhere.
- Honest P&L, every exit policy: **−$5.74 to −$9.84/day** (12,690 signals,
  9,378 fills). The no-1-min-edge result holds across a full market cycle.

## 2c. Live tick reality: the real strategy barely fills

~55 min of mainnet paper trading (exact 14-gate strategy on real ticks, three
sessions) produced ~9 signals and **0 fills** — zero round trips, hence zero
real P&L evidence. Certifying the real signal would take months of live data.

## 3. What this does NOT prove

The live strategy uses **sub-second L2 order-flow imbalance**, a signal that
candle data fundamentally cannot represent. The OFI edge (if any) lives at the
250 ms–2 s horizon and can only be validated on **recorded tick data**
(`paper_trader.py --record` → `backtest.py replay`). The candle backtests should
be treated as INDICATIVE plumbing checks only, never as evidence for the real
signal.

## Bottom line for go-live

Do **not** trust any candle-backtest profitability number. Certify the real
signal only via weeks of recorded-tick replay across mixed regimes (or
`REAL_TEST_MODE=1` at 0.001 BTC), reading net-of-fee P&L from
`real_test_report.md`.
