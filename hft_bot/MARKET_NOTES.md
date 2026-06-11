# Market notes — running learning journal

One entry per loop cycle. Each note ends with "implication for us" so learning
feeds the system, not just curiosity. Strategy parameters NEVER change based on
news (that's discretionary creep); notes inform risk awareness and research
priors only.

---

## 2026-06-11 — Anatomy of the current bear (why our signal is flat)

Drivers per financial press (BNN Bloomberg, CNBC, Investing.com, June 2026):
1. **Macro**: sticky inflation, Fed cut uncertainty, strong USD, US–Iran
   geopolitical risk → broad risk-off; high rates favour cash/bonds/gold.
2. **ETF mechanics**: >$2B spot-ETF outflows (IBIT/FBTC/GBTC) in ~2 weeks.
   Redemptions force issuers to sell BTC immediately regardless of price —
   a *mechanical*, price-insensitive seller. New structural feature of this
   cycle vs 2022: flows data (ETF creations/redemptions) is now a visible
   pressure gauge.
3. **Liquidation cascades**: break of $65k triggered >$800M forced closures
   in 24h — leverage amplifies trends in both directions.
4. **Narrative/liquidity rotation**: speculative capital rotating to AI
   equities and IPOs; MicroStrategy's first BTC sale in ~4 years (symbolic).

**Implication for us:** this is a *textbook* environment for the regime gate —
mechanical sellers + liquidation cascades are exactly the conditions where
"price below long-term average" keeps compounding downward. The system being
100% cash is functioning as designed. Liquidation cascades also validate the
vol-targeting overlay: leverage-driven moves spike realized vol first, which
shrinks our size *before* the worst candles. No parameter action.

**Watch item:** ETF flow reversal (sustained creations) has historically led
price stabilization; when our 150d gate eventually flips, ETF inflows would be
confirming context — worth noting in the operator log at that time, never a
signal override.


## 2026-06-11 (b) — news added to the AI trader (scoped, not as a signal)

User asked whether market news was missing from the operator. It was, partially.
Added a read-only `get_news` tool (CoinDesk + Cointelegraph RSS, stdlib) with a
hard-coded role split: news may (1) trigger `halt_trading` on infra risk (hack/
depeg/outage) and (2) enrich the operator's plain-English explanation — but the
system prompt forbids it from changing the position. Direction/size come ONLY
from the validated signal.

**Why this boundary matters:** news-driven entries are unbacktestable and are
exactly the discretionary trading the architecture exists to prevent. Safety
and explanation are legitimate context roles; position-setting is not. Verified
live (headlines fetch) + 2 unit tests (dedup, unreachable-feed handling).
