# Research agenda (self-managed) — literature-gated to control multiple testing

Rule: an idea earns ONE in-sample test only with a published prior behind it.
Wins get ONE OOS confirmation. Everything (win or kill) gets logged.

| # | Task | Prior | Status |
|---|---|---|---|
| 1 | Downside (semi-dev) vol targeting vs total vol | Wang-Yan JBF 2021 | REJECTED IS — raises CAGR but LOWERS Sharpe (1.70→1.62) + deeper DD; predicted effect (Sharpe↑) absent. OOS untouched. |
| 2 | Patient maker execution for daily rebalances | cost engineering | DONE — TREND_EXEC=patient (default): ALO at touch, 15min poll, IOC fallback; ~5.5bp/side saved when maker fills. 4 tests. |
| 3 | Funding-carry sleeve | classic basis carry | BLOCKED on accruing funding data |
| 4 | Momentum skip-window | J-T skip-month; crypto evidence mixed | DROPPED untested — prior too weak to spend a test on (multiple-testing budget) |
| 5 | Quarterly re-validation | process, not research | SCHEDULED |

Killed (do not revisit): 1-min scalping class, symmetric long/short,
regime-gated shorts, breadth scaling, downside-vol targeting. See STRATEGY_V2.md.
