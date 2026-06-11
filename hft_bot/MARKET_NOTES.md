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


## 2026-06-11 (c) — Hyperliquid venue risk → majors-only is a SAFETY rule

Studied HL's incident history and mechanics (OAK Research, Talos, DL News,
Messari, HL docs):
- **JELLY (Mar 2025):** attacker shorted a meme token, pumped spot +400%,
  threatened the HLP vault; validators emergency-delisted and settled at a
  fixed price. **XPL (Aug 2025):** pre-launch flash short-squeeze, manipulators
  made ~$46M in <1h, users lost >$60M. Both — and a third 2025 episode — hit
  THIN / pre-launch / meme markets. Deep majors (BTC/ETH) were never targeted.
- **Mechanics worth knowing:** funding settles HOURLY (granular vs 8h CEXes);
  interest component 0.01%/8h. Oracle = weighted median of 8 CEX spot prices
  (Binance 3, OKX/Bybit 2, others 1) + HL mid — robust for majors, thinner
  basis for low-liquidity coins.

**Implication for us (ACTIONED):** the manipulation surface is illiquidity, not
the venue itself. Encoded a hard $50M/24h volume floor in scan.py — a coin can
flash a perfect trend signal and still be refused as a candidate if it's thin.
This makes "majors-only" a *coded safety rule*, not just a preference, and is
independent of the AI's get_news halt (defense in depth). BTC/ETH/SOL all clear
it comfortably; the alt signals we already rejected were all thin anyway.


## 2026-06-11 (d) — Will the edge decay? Crowding & post-publication alpha

Literature (arXiv 2105.01380 "Why and how systematic strategies decay",
arXiv 2512.11913 on alpha-capacity games, McLean-Pontiff post-publication
decay, QuantPedia commodity-crowding):
- ~50% of anomaly alpha typically decays after publication; equity momentum
  fell from ~10%/yr (1990s) to ~2%/yr today. Crowding negatively predicts
  factor returns; historical returns accrued mostly in LOW-crowding periods.
- BUT: decay is slowest for (a) strategies with painful risk profiles
  (trend's years-long chop), (b) markets with retail/forced flows, and
  (c) capacity-constrained arbitrage. Crypto scores well on (a) and (b):
  leveraged liquidations and ETF redemptions are *forced*, price-insensitive
  flows that recreate trends regardless of how many quants chase them.

**Implications for us:**
1. Expect REALIZED returns below the +21% OOS point estimate — decay is the
   base case. Our stated planning range (10-20%/yr) already prices this in.
2. The drift monitor is our crowding detector: persistent DRIFT WARNING
   verdicts are how decay would actually show up. No parameter action now.
3. Diversification across assets (done) and, later, a carry sleeve are the
   standard answers to single-factor decay — not more tuning of the factor.
