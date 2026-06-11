# Literature review — what published research says about our design

Compiled 2026-06-11. Each entry: finding → implication for Trend Bot v2.1.

## Core strategy class
- **Moskowitz, Ooi & Pedersen — "Time Series Momentum"** (JFE 2012) and
  **Hurst, Ooi & Pedersen — "A Century of Evidence on Trend-Following"**:
  TSMOM positive in essentially every decade since 1880 across 60+ markets.
  → Our ensemble (SMA + lookback momentum, 50–100d) is the canonical
  implementation; parameters sit in the documented effective range.
- **Crypto TSMOM** (AUT working paper; Erasmus thesis; Liu & Tsyvinski NBER
  w24877; ScienceDirect "Dynamic TSMOM of cryptocurrencies"): momentum
  effects confirmed in crypto out-of-sample, lookbacks weeks–months.
  → Independent confirmation of the 60/90d momentum windows.

## Risk management overlays
- **Moreira & Muir — "Volatility-Managed Portfolios"** (JF 2017, NBER w22208):
  scaling exposure by inverse realized variance raises Sharpe across factors.
  → Our 40% vol target is this, confirmed by our own IS/OOS results.
- **Wang & Yan — "Downside risk and the performance of volatility-managed
  portfolios"** (JBF 2021): scaling by DOWNSIDE semi-deviation beats total
  vol in 89/94 anomalies. → TESTABLE on our system (strong prior; see
  RESEARCH_AGENDA #1).
- **Barroso & Santa-Clara — momentum crash management**: variance scaling
  nearly doubles momentum Sharpe. → Same family as above.

## What the literature does NOT support for us
- Short-side crypto momentum is notoriously squeeze-prone; long/flat dominates
  long/short in most crypto TSMOM studies → matches our two IS rejections.
- Sub-daily crypto "momentum" largely fails after costs in honest studies →
  matches BACKTEST_FINDINGS.md (the OFI post-mortem).

Sources: see links in STATUS.md research log (web-searched 2026-06-11):
arxiv.org/pdf/2602.11708, nber.org w24877 & w22208, lehigh.edu Wang-Yan PDF,
ssrn.com 2659431, sciencedirect.com S0378426621001576 & S1062940821000590.
