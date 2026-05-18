# hyperbot

Finance tooling: a Flask news-aggregator backend, the **Monoth** real-time dashboard (React/TypeScript), and an **OFI HFT bot** for Bitcoin on Hyperliquid.

## Architecture

```
hyperbot/
├── udbhav_app.py          # Flask backend — RSS feeds, Hyperliquid vault data, Google OAuth
├── live_udbhav_ui/        # Alternate Flask UI (app.py + templates/)
├── live_server_config/    # systemd + nginx config for production server
├── monoth_src/            # Monoth dashboard (React 18 + Vite + Vercel serverless)
├── hyperliquid-python-sdk/ # Local copy of HL Python SDK
├── hft_bot/               # OFI+TFI HFT bot for BTC on Hyperliquid (see below)
└── quotes.csv             # Quote data used by Flask backend
```

---

## HFT Bot (`hft_bot/`)

### Files

| File | Purpose |
|---|---|
| `config.py` | All tuning parameters; reads `.env` + `risk.yaml` |
| `risk.yaml` | Hot-reloadable risk limits (send SIGHUP to reload) |
| `state.py` | `BotState` dataclass — book, inventory, PnL, OFI window, trade window |
| `strategy.py` | 6-gate OFI+TFI signal engine |
| `executor.py` | Spread-adaptive order placement (ALO/IOC) |
| `main.py` | Async event loop, WebSocket manager, risk monitor |
| `paper_trader.py` | Live paper trading with realistic ALO fill simulation |
| `backtest.py` | Two-mode backtest: candle (Mode A) or recorded replay (Mode B) |

### Strategy — OFI + TFI (v3)

**OFI** (Order Flow Imbalance) — Cont-Kukanov-O'Hara 2014:
- Per bid level: price up → +size_new; price down → −size_old; same → size_new−size_old
- Per ask level: price down → +size_new; price up → −size_old; same → −(size_new−size_old)
- Normalised to [−1, +1] by spot book depth

**TFI** (Trade Flow Imbalance) from live `trades` WebSocket:
- `(buy_vol − sell_vol) / total_vol` over the OFI window
- Requires `|TFI| > MIN_TFI_STRENGTH (0.10)` to confirm signal

**6-gate signal filter** (in order):
1. **Cooldown** — min 1500ms between any two signals
2. **Spread filter** — suppress if spread > MAX_SPREAD_BPS
3. **TFI gate** — trade flow must agree with OFI direction (|TFI| > 0.10)
4. **Trend gate** — BUY suppressed if price falling over last 3s; SELL suppressed if rising
5. **Persistence** — OFI must exceed threshold for 2 consecutive book ticks
6. **Anti-flap** — opposite direction blocked for 2× cooldown after last signal

### Current Config (`config.py` defaults)

| Parameter | Value | Notes |
|---|---|---|
| `ORDER_SIZE_BTC` | 0.01 | $770 notional at ~$77k BTC |
| `OFI_BUY_THRESHOLD` | 0.70 | normalised OFI in [−1,+1] |
| `OFI_SELL_THRESHOLD` | −0.70 | |
| `OFI_LEVELS` | 2 | top N book levels |
| `OFI_WINDOW_MS` | 400 | rolling accumulation window |
| `OFI_PERSISTENCE_TICKS` | 2 | consecutive ticks above threshold |
| `SIGNAL_COOLDOWN_MS` | 1500 | min ms between signals |
| `MIN_TFI_STRENGTH` | 0.10 | min |TFI| for confirmation |
| `PRICE_TREND_WINDOW_MS` | 3000 | look-back for trend gate |
| `WIDE_SPREAD_BPS` | 5.0 | switch IOC above this spread |
| `LIMIT_ORDER_TIMEOUT_MS` | 800 | ALO auto-cancel after this long |
| `PRICE_TICK` | 0.1 | $0.10 tick size on HL BTC perp |

### Current Risk (`risk.yaml`)

| Parameter | Value | Notes |
|---|---|---|
| `max_inventory_btc` | 0.01 | pauses quoting at limit |
| `stop_loss_pct` | 0.003 | 0.3% → $2.31 loss per trade |
| `max_daily_loss_usd` | 5.0 | circuit breaker (~2 stop-losses) |
| `leverage` | 10 | margin per trade ≈ $77 (48% of $160) |

### Execution Model

- **Always ALO** on mainnet (spread ~1.3 bps); IOC only when spread > 5 bps
- ALO earns **+2.13 bps** per round-trip (maker rebate −0.01%/leg + half-spread)
- IOC costs **−7.13 bps** per round-trip (taker fee +0.035%/leg) — avoid
- BUY orders: limit = best_bid + $0.10 (1 tick inside spread)
- SELL orders: limit = best_ask − $0.10
- ALO fill detection via live trade stream: BUY fills when side='A' trade prints at ≤ limit

### Backtested Performance (7-day candle backtest, 0.01 BTC)

- **Signals**: ~104 over 7 days (~15/day), balanced buys/sells
- **Accuracy T+1min**: 82.7%
- **Best day**: +$13.91 | **All 5 sampled days**: profitable
- **Max bad day**: −$5.00 (hard-capped by circuit breaker)
- **ALO breakeven**: need $15.40 adverse move on $770 notional to lose (very wide buffer)
- Period was May 2026, moderately bullish BTC — verify over more diverse market conditions

### Commands

```bash
cd hft_bot

# Live paper trade (no real orders, uses mainnet data)
python paper_trader.py --duration 300

# Record a session for replay backtest
python paper_trader.py --record session.jsonl --duration 3600

# Candle backtest (downloads fresh 7-day OHLCV from mainnet)
python backtest.py candles --days 7

# Replay backtest (exact strategy on recorded data)
python backtest.py replay session.jsonl

# Live trading (requires PRIVATE_KEY in .env)
python main.py
```

### Key env vars for HFT bot

| Var | Purpose |
|---|---|
| `PRIVATE_KEY` | Hyperliquid wallet key — **DO NOT EXPOSE** |
| `HYPERLIQUID_API_URL` | Set to `https://api.hyperliquid.xyz` for mainnet |
| `ORDER_SIZE_BTC` | Override position size (default 0.01) |
| `OFI_BUY_THRESHOLD` | Override buy threshold (default 0.70) |
| `LOG_LEVEL` | DEBUG for verbose tick logging |

### Architecture

```
Hyperliquid WS (l2Book, trades, userFills, orderUpdates)
    │ call_soon_threadsafe
    ▼
asyncio Queue
    │ await queue.get()
    ▼
Main async loop → process_book() → compute OFI+TFI → evaluate_signal() → place/cancel
    │ run_in_executor
    ▼
ThreadPoolExecutor (blocking HTTP calls to HL REST API)
```

Concurrent tasks: `main_loop`, `risk_monitor` (100ms poll), `ws_health_monitor` (5s poll), `stats_logger` (10s).

### Known issues / gotchas

- `data.hyperliquid.xyz` DNS unavailable from cloud environments — use `api.hyperliquid.xyz` for candle data
- `historicalTrades` REST endpoint unreliable; use `candleSnapshot` for backtest data
- OFI threshold 0.80 + persistence 3 generates 0 signals on live data (too tight); 0.70 + 2 is the sweet spot
- ALO fill rate ~25-39% (orders expire when market runs away in predicted direction — expected, no loss on expiry)
- Candle backtest OFI proxy uses (close−open)/(high−low) which is noisier than tick-level OFI; treat accuracy numbers as indicative, not exact
- 5-day backtest sample is small; need weeks of live paper trading to confirm 82.7% figure

---

## Flask / Monoth Stack

### Stack

| Layer | Tech |
|---|---|
| Backend | Python 3, Flask, feedparser, google-auth |
| Dashboard | React 18 (React 19 dep), TypeScript, Tailwind 4, Vite 8, Vercel |
| Data APIs | Finnhub, FRED, Hyperliquid, Polymarket, Kalshi |
| Auth (optional) | Google OAuth2, Supabase |
| Billing (optional) | Stripe |

### Commands

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

### Key env vars

| Var | Purpose |
|---|---|
| `PRIVATE_KEY` | Hyperliquid wallet key — DO NOT EXPOSE |
| `FLASK_SECRET_KEY` | Flask session signing |
| `GOOGLE_CLIENT_ID/SECRET` | OAuth2 |
| `HYPERLIQUID_API_URL` | Defaults to `https://api.hyperliquid.xyz` |

### monoth_src — Monoth dashboard
- 60+ finance panels: equities, crypto, forex, macro, prediction markets
- Vercel serverless API in `api/`; local dev via `dev-server.ts` (port 3002)
- `src/pages/`, `src/components/`, `src/services/`, `src/stores/`
- Env vars needed: at minimum `FINNHUB_API_KEY` and `FRED_API_KEY` (free tier)
- See `monoth_src/.env.example` for full list

### Production deployment (live_server_config/)
- `udbhav-markets.service` — runs Flask backend via systemd
- `udbhav-ui.service` — runs the UI
- `nginx_udbhav-ui.conf` — reverse proxy config
- `udbhav-markets-healthcheck.*` — systemd timer for health checks

## General
- Avoid OpenAI SDK imports; this project does not use the OpenAI API
- Always use mainnet (`https://api.hyperliquid.xyz`), never testnet
- Never commit `.env`; `PRIVATE_KEY` must never be exposed
- Always push directly to master
