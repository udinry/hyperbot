# Monoth Markets — static dashboard

A self-contained, modern crypto markets dashboard. Pure client-side: no build
step, no backend, no secrets. It fetches live data directly from public APIs in
the browser, so it can be hosted on any static host.

## What it shows

- **Bitcoin spotlight** — live price (with up/down flash), 1h/24h/7d change,
  7-day sparkline, market cap / volume / 24h high-low, plus the live Hyperliquid mid.
- **Fear & Greed gauge** — animated SVG gauge from alternative.me.
- **Global stats** — total market cap (+24h), 24h volume, BTC dominance, active coins.
- **Top markets table** — top 15 by market cap with 1h/24h/7d changes and 7-day sparklines.
- **BTC order book** — live Hyperliquid L2 depth (bids/asks with size bars + spread).

Auto-refreshes every 45s; the order book every 6s.

## Data sources (all free, no API key)

- [CoinGecko](https://www.coingecko.com) — prices, markets, global stats
- [Alternative.me](https://alternative.me/crypto/fear-and-greed-index/) — Fear & Greed Index
- [Hyperliquid](https://hyperliquid.xyz) — live BTC mid + order book

## Hosting

The file `index.html` is fully standalone.

- **GitHub Pages**: enabled via `.github/workflows/deploy-pages.yml` on pushes to
  `master`. Live at `https://udinry.github.io/hyperbot/`.
- **Instant preview** (any public branch, no setup): open the file through
  `raw.githack.com` — e.g.
  `https://raw.githack.com/udinry/hyperbot/<commit-or-branch>/docs/index.html`.
