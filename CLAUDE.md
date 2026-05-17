# hyperbot

Finance tooling: a Flask news-aggregator backend and the **Monoth** real-time dashboard (React/TypeScript).

## Architecture

```
hyperbot/
├── udbhav_app.py          # Flask backend — RSS feeds, Hyperliquid vault data, Google OAuth
├── live_udbhav_ui/        # Alternate Flask UI (app.py + templates/)
├── live_server_config/    # systemd + nginx config for production server
├── monoth_src/            # Monoth dashboard (React 18 + Vite + Vercel serverless)
├── hyperliquid-python-sdk/ # Local copy of HL Python SDK
└── quotes.csv             # Quote data used by Flask backend
```

## Stack

| Layer | Tech |
|---|---|
| Backend | Python 3, Flask, feedparser, google-auth |
| Dashboard | React 18 (React 19 dep), TypeScript, Tailwind 4, Vite 8, Vercel |
| Data APIs | Finnhub, FRED, Hyperliquid, Polymarket, Kalshi |
| Auth (optional) | Google OAuth2, Supabase |
| Billing (optional) | Stripe |

## Commands

```powershell
# Flask backend
cd C:\Users\Udbhav\hyperbot
python udbhav_app.py

# Monoth dashboard (dev)
cd C:\Users\Udbhav\hyperbot\monoth_src
npm start           # starts both API (port 3002) and Vite dev server

# Monoth dashboard (prod build)
npm run build
```

## Key env vars (`hyperbot/.env` — never commit)
| Var | Purpose |
|---|---|
| `PRIVATE_KEY` | Hyperliquid wallet key — DO NOT EXPOSE |
| `FLASK_SECRET_KEY` | Flask session signing |
| `GOOGLE_CLIENT_ID/SECRET` | OAuth2 |
| `HYPERLIQUID_API_URL` | Defaults to `https://api.hyperliquid.xyz` |

## monoth_src — Monoth dashboard
- 60+ finance panels: equities, crypto, forex, macro, prediction markets
- Vercel serverless API in `api/`; local dev via `dev-server.ts` (port 3002)
- `src/pages/`, `src/components/`, `src/services/`, `src/stores/`
- Env vars needed: at minimum `FINNHUB_API_KEY` and `FRED_API_KEY` (free tier)
- See `monoth_src/.env.example` for full list

## Production deployment (live_server_config/)
- `udbhav-markets.service` — runs Flask backend via systemd
- `udbhav-ui.service` — runs the UI
- `nginx_udbhav-ui.conf` — reverse proxy config
- `udbhav-markets-healthcheck.*` — systemd timer for health checks

## Migrated from Codex → Claude
- No Codex-specific instructions remain; use standard Claude Code patterns
- Avoid OpenAI SDK imports; this project does not use the OpenAI API
