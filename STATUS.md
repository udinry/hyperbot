# Project status & research backlog

Living handoff doc. Read this first to resume work in any new session.
Branch: `claude/hyperbot-code-audit-vtbvod`.

## Where things stand (2026-06-10)

**The honest arc:** the OFI scalper could not be validated (sub-1bp edge vs
>2bp costs at 1-min, ~0 live fills/hr — see `hft_bot/BACKTEST_FINDINGS.md`). We
pivoted to a horizon where edge >> costs and a decade of data exists.

**Validated, shippable today — Trend Bot v2.1** (`hft_bot/trend_bot.py`,
`hft_bot/STRATEGY_V2.md`):
- Daily trend ensemble (SMA50/100 + 60/90d momentum), 40% vol target,
  **150d regime filter**, long/flat.
- OOS 2022–2026: **+21.2% CAGR, 22.2% MaxDD, Sharpe 0.88** vs buy-and-hold
  +5.9%/67%/0.37. Frozen-parameter transfer to ETH/SOL holds.
- Multi-asset (`TREND_COINS`), tested live (currently flat — BTC below 150d SMA,
  correct for the downtrend).

**AI operator — `ai_trader/`**: Claude drives the validated strategy through a
deterministic risk engine it cannot override. Dry-run default, full audit trail,
21 tests + 51 hft_bot tests passing.

## Deploy everything in one shot (VPS)

```bash
cd /opt/hyperbot && git pull && bash deploy_trend_stack.sh
```
Installs the forward-test timer + observer-mode trend bot (paper only), after
running both test suites. Drift verdict: `python hft_bot/forward_test.py --report`.

## How to run a live forward-test (recommended next real step)

This is the evidence that settles everything — deploy in dry-run and let it
build a track record:
```bash
# on the VPS, observer/dry-run (no key needed, no risk)
cd /opt/hyperbot/hft_bot && python trend_bot.py --dry-run         # one look
# or continuous decision logging:
systemctl enable --now hyperbot-trend        # (ExecStart has no --live)
# AI-narrated version:
cd /opt/hyperbot/ai_trader && python agent.py status
```

## Research backlog (priority order) — pick up here

1. ~~Forward-test harness~~ **DONE** — `hft_bot/forward_test.py` + daily
   systemd timer; first live marks logged 2026-06-11.
2. **Funding-carry sleeve**: market-neutral income when funding is extreme.
   UNBLOCKED: forward_test now logs hourly funding per coin daily (funding_hr
   column) — history accumulates automatically; revisit once weeks of data exist.
3. ~~Cross-asset portfolio sizing~~ **DONE** — inverse-vol (risk-parity)
   weights, OOS Sharpe 0.74→0.78 vs equal weight; default TREND_WEIGHTING=invvol.
4. ~~Regime-filter robustness~~ **DONE** — plateau confirmed (100–250d all
   beat no-filter, Sharpe 0.70–0.93); frozen 150d transfers to ETH/SOL.
5. ~~Telegram integration~~ **DONE** — `hft_bot/notify.py`; trend_bot alerts on
   every (dry-run or live) rebalance + errors; forward_test sends a daily summary.
6. **Quarterly re-validation**: re-run `research_trend.py` + `research_regime.py`
   as new data arrives; watch for parameter drift.

## Live operator log

**2026-06-11 — market-wide scan + candidate vetting (scan.py / vet_candidates.py):**
- Majors BTC/ETH/SOL all FLAT (downtrend; need +18% / +34% / +38% to flip the
  150d regime gate). Correct trade across the board: cash.
- Scanner found 10 raw long signals in alts; vetting on full history with the
  frozen strategy REJECTED 8. Only HYPE (Sharpe 1.57) and VVV (1.90) pass, and
  BOTH have <1.5y history → UNRELIABLE, not addable. Most "long" alts fail
  because their history is short and selection-biased (liquid = they pumped).
- Decision: no new positions. Watchlist (HYPE, VVV) revisited once each has
  ≥2.5y history. This is the scanner working: it found candidates AND the
  discipline filter correctly refused all of them.

## Counterfactual audit (user-requested, 2026-06-11)

`hft_bot/shadow_book.py` logs every liquid-universe long signal the system
DECLINES (daily, via the forward-test timer) and resolves their forward
returns at 7/14/30d. If skipped trades show >60% win-rate and >+2% avg on
n>=20, the report flags THE FILTER IS TOO STRICT — the system audits its own
caution in both directions. First live log: 8 skipped signals (HYPE, ZEC,
WLD, NEAR, ...). Check: `python shadow_book.py --report`.

## Discipline rules (do not violate)

- Tune only on ≤2021 data; touch 2022+ once per idea, report it.
- Prefer parameter plateaus over peaks; reject ideas that only work on one cell.
- The AI never predicts price freehand — it executes the validated signal.
- One bot per account. Dry-run until the live audit matches the backtest.
