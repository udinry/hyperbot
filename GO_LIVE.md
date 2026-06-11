# GO-LIVE runbook — "start trading" in one prompt

The day you want real trading, you say one thing to Claude (or do it yourself):

> **"Run preflight and start trading live."**

Claude then executes exactly this — nothing else needs to be built:

```bash
cd /opt/hyperbot/ai_trader
python agent.py preflight          # GO/NO-GO checklist (below)
python agent.py trade --live       # only if GO — one supervised live cycle
# then, to keep it running daily:
# edit hyperbot-ai-trader.service ExecStart to add --live && systemctl restart
```

## What preflight requires before it says GO

| Check | How it gets satisfied |
|---|---|
| `PRIVATE_KEY` in `/opt/hyperbot/.env` | you add it (the only secret I never touch) |
| Perp equity ≥ $50 | funds in the **perp** account (not spot) |
| Risk caps sane vs equity | defaults already conservative; auto-checked |
| Signal computable per coin | automatic |
| Both test suites green | automatic |
| **Forward-test drift verdict healthy** | accrues from the daily timer — this is the "consistently profitable" gate |

## The profitability gate, defined precisely (so "consistent" isn't vibes)

Live is justified when the forward record (paper, accruing daily on the VPS)
shows **≥30 days** with a drift verdict of `WITHIN EXPECTATION`, `ABOVE
EXPECTATION`, or `FLAT RECORD` (all-cash in a bear is *correct*, not failure).
A `DRIFT WARNING` blocks GO. Check anytime:

```bash
cd /opt/hyperbot/hft_bot && python forward_test.py --report
```

Honest note: in the current downtrend the system holds cash, so paper equity
will be flat — that IS the strategy performing (2022: −4% vs market −65%).
Profit accrues in uptrends; the regime gate decides when, not us.

## Safety that stays on after going live

Hard risk caps (per-order/position/exposure/leverage/daily-loss/order-count),
the signal-consistency gate (no longs when the model is flat, no shorts ever,
never trade blind), Telegram alerts on every order, full audit trail, and the
`halt_trading` kill switch — none of these relax in live mode.
