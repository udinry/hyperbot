# hyperbot

Finance tooling: a Flask news-aggregator backend, the **Monoth** real-time dashboard (React/TypeScript), and an **OFI HFT bot** for Bitcoin on Hyperliquid.

## Architecture

```
hyperbot/
├── udbhav_app.py          # Flask backend — RSS feeds, Hyperliquid vault data, Google OAuth
├── live_udbhav_ui/        # Alternate Flask UI (app.py + templates/) — deployed at udbhav.uk
├── live_server_config/    # systemd + nginx config for production server
├── monoth_src/            # Monoth dashboard (React 18 + Vite + Vercel serverless)
├── hyperliquid-python-sdk/ # Local copy of HL Python SDK
├── hft_bot/               # OFI+TFI HFT bot for BTC on Hyperliquid (see below)
├── hyperbot_ui/           # Management UI — start/stop bot, PnL, trades (deployed at udbhav.uk/hyperbot)
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
- Requires `|TFI| > MIN_TFI_STRENGTH (0.40)` to confirm signal

**14-gate signal filter** (in order, GATE STATS key in parentheses):
1. **Cooldown** — min SIGNAL_COOLDOWN_MS (3000ms) between any two signals
2. **Time-of-day** — suppress during TRADE_BLOCK_UTC_START..END hours (`time_block`)
3. **Post-SL cooldown** — suppress for POST_SL_COOLDOWN_MS (30000ms) after a loss SL hit (`post_sl`)
4. **Spread** — suppress if spread > MAX_SPREAD_BPS (`spread`)
5. **ATR minimum** — suppress in flat markets if 1-min ATR < ATR_MIN_TRADE_USD=12 (`atr`)
6. **OFI exhaustion** — suppress if strong opposite OFI (>0.80) in last 5 ticks (`ofi_exhaustion_buy/sell`)
7. **Microprice** — suppress if book-pressure microprice contradicts OFI direction (`microprice_buy/sell`)
8. **VWAP directional** — suppress BUY if recent trades drove price below mid; SELL if above (`vwap_buy/sell`)
9. **VWAP overextension** — suppress BUY if VWAP deviation > VWAP_BUY_MAX_DEV (disabled, inf) (`vwap_buy`)
10. **TFI confirmation** — |TFI| must exceed MIN_TFI_STRENGTH=0.40 (`tfi_buy/sell`)
11. **3s trend** — BUY suppressed if price falling over PRICE_TREND_WINDOW_MS; SELL if rising (`trend_buy/sell`)
12. **5-min momentum** — OFI must align with 5-min price trend > TREND_5MIN_PCT=0.04% (`trend5m_buy/sell`)
13. **Funding bias** — high positive funding requires 0.10 higher OFI to buy (`funding_buy/sell`)
14. **Persistence** — OFI must exceed threshold for OFI_PERSISTENCE_TICKS=1 consecutive ticks; anti-flap blocks opposite direction for 2× cooldown (`anti_flap`)

### Current Config (`config.py` defaults)

| Parameter | Live Value | Notes |
|---|---|---|
| `ORDER_SIZE_BTC` | 0.001 | Floor; overridden at runtime by `_refresh_order_size` |
| `POSITION_RISK_PCT` | 0.007 | Min-size mode; full-size (0.48) requires user approval |
| `OFI_BUY_THRESHOLD` | 0.60 | normalised OFI in [−1,+1] |
| `OFI_SELL_THRESHOLD` | −0.60 | |
| `OFI_LEVELS` | 5 | top N book levels |
| `OFI_PERSISTENCE_TICKS` | 1 | consecutive ticks above threshold |
| `SIGNAL_COOLDOWN_MS` | 3000 | min ms between signals |
| `MIN_TFI_STRENGTH` | 0.40 | min |TFI| for confirmation |
| `POST_SL_COOLDOWN_MS` | 30000 | suppress all signals for 30s after loss SL hit |
| `PRICE_TREND_WINDOW_MS` | 30000 | 30s look-back for 3s trend gate |
| `WIDE_SPREAD_BPS` | 50 | switch IOC above this spread |
| `LIMIT_ORDER_TIMEOUT_MS` | 2000 | ALO auto-cancel after this long |
| `ENTRY_IOC` | true | forces IOC for all entries |
| `ATR_MIN_TRADE_USD` | 12 | min 1-min ATR ($) to allow entry |
| `TAKE_PROFIT_PCT` | 0.010 | 1% TP |
| `STOP_LOSS_PCT` | 0.005 | 0.5% SL |
| `SL_TRAIL_TRIGGER_PCT` | 0.005 | move SL to BE after 0.5% profit |
| `TREND_5MIN_PCT` | 0.0004 | min 5-min price move to allow signal |
| `VWAP_BUY_MAX_DEV` | inf | disabled; set to ~12 if high-VWAP-BUY loss pattern confirmed |
| `PRICE_TICK` | 0.1 | $0.10 tick size on HL BTC perp |

### Current Risk (`risk.yaml`)

| Parameter | Value | Notes |
|---|---|---|
| `max_inventory_btc` | 0.01 | pauses quoting at limit |
| `stop_loss_pct` | 0.003 | 0.3% → $2.31 loss per trade |
| `max_daily_loss_usd` | 5.0 | starting default; overridden dynamically to 2×stop-loss |
| `leverage` | 10 | margin per trade ≈ $77 (48% of $160) |

### Execution Model

- **Always ALO** on mainnet (spread ~1.3 bps); IOC only when spread > 5 bps
- ALO earns **+2.13 bps** per round-trip (maker rebate −0.01%/leg + half-spread)
- IOC costs **−7.13 bps** per round-trip (taker fee +0.035%/leg) — avoid
- BUY orders: limit = best_bid + $0.10 (1 tick inside spread)
- SELL orders: limit = best_ask − $0.10
- ALO fill detection via live trade stream: BUY fills when side='A' trade prints at ≤ limit

### Backtested Performance

> ⚠️ The previously quoted "82.7% T+1min over 7 days" described the bot
> *before* the TFI clock-domain fix (see `hft_bot/IMPROVEMENTS.md` §1.1) and was
> measured by the candle proxy (Mode A), not the live strategy. It no longer
> describes this code. **Re-validate from scratch** with `backtest.py replay`
> (exact code) and live paper/real-test sessions before trusting any number.
> Note also that candle-backtest accuracy falls sharply with horizon
> (~84% at T+1min → ~58% at T+3–5min), which is why `MAX_POSITION_HOLD_MS` now
> defaults to 10 min — the OFI edge has a short half-life.

### Commands

```bash
cd hft_bot

# Unit tests (40 tests: clock fix, gates, fee-aware PnL, idempotent close)
python -m pytest tests/

# Live paper trade (no real orders, uses mainnet data)
python paper_trader.py --duration 300

# Record a session for replay backtest
python paper_trader.py --record session.jsonl --duration 3600

# Candle backtest (downloads fresh 7-day OHLCV from mainnet) — INDICATIVE ONLY
python backtest.py candles --days 7

# Replay backtest (exact live strategy on recorded data) — the number to trust
python backtest.py replay session.jsonl

# Scaled-down REAL trading on mainnet (0.001 BTC) to test fills; writes
# real_test_report.{json,md} comparing actual vs projected-at-scale PnL
REAL_TEST_MODE=1 python main.py

# Live trading (requires PRIVATE_KEY in .env)
python main.py
```

### Key env vars for HFT bot

| Var | Purpose |
|---|---|
| `PRIVATE_KEY` | Hyperliquid wallet key — **DO NOT EXPOSE** |
| `HYPERLIQUID_API_URL` | Set to `https://api.hyperliquid.xyz` for mainnet |
| `REAL_TEST_MODE` | `1` = trade real 0.001 BTC on mainnet + scaled PnL report |
| `MAX_POSITION_HOLD_MS` | Close at market after N ms (default 600000 = 10 min) |
| `ATR_MAX_TRADE_USD` | Suppress entries when 1-min ATR exceeds this (spike guard) |
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

### Pre-live test results (2026-05-18, mainnet paper trade)

| Session | Duration | Signals | Fills | Fill rate | Net P&L | Wins/Losses |
|---|---|---|---|---|---|---|
| Session 1 | 15 min | 11 (11B/0S) | 2 | 18% | +$0.37 | 2/0 |
| Session 2 | 10 min | 7 (6B/1S) | 2 | 29% | +$0.33 | 2/0 |

- Low fill rate (18-29%) in strong uptrend is expected and correct — orders expire when market runs away (no cost, no loss)
- SELL signal confirmed firing correctly when OFI=-1.000 and TFI=-0.545 (trend gate permitted it at trend=+0.00)
- WS drop at 7 min recovered automatically; full session ran to completion

### Known issues / gotchas

- `data.hyperliquid.xyz` DNS unavailable from cloud environments — use `api.hyperliquid.xyz` for candle data
- `historicalTrades` REST endpoint unreliable; use `candleSnapshot` for backtest data
- OFI threshold 0.80 + persistence 3 generates 0 signals on live data (too tight); 0.70 + 2 is the sweet spot
- ALO fill rate ~18-29% in strong trends, up to ~39% in ranging markets — all expiries are free (no cost)
- ALO limit price must be clamped: BUY limit must be < best_ask, SELL limit must be > best_bid — crossing causes silent rejection on Hyperliquid ALO (post-only rule). Fixed in main.py.
- No SIGHUP hot-reload; risk.yaml changes require bot restart
- Candle backtest OFI proxy uses (close−open)/(high−low) which is noisier than tick-level OFI; treat accuracy numbers as indicative, not exact
- 5-day backtest sample is small; need weeks of live paper trading to confirm 82.7% figure
- Paper trader WS reconnects automatically after drop; main.py also has ws_health_monitor with exponential backoff
- `_round_price` in executor.py uses Python banker's rounding — harmless in practice because all WS prices are already tick-aligned
- Dynamic sizing: `state.order_size_btc` and `state.max_inventory_btc` are set together in `_refresh_order_size`; `can_buy`/`can_sell`/`risk_monitor` all read from state, not config — so the inventory gate scales automatically with position size
- Sizing formula: `round(max(0.001, min(0.1, balance×POSITION_RISK_PCT×leverage/mid)), 3)` — 0.1 BTC is a sanity cap only, not a risk limit
- Circuit breaker formula: `2 × STOP_LOSS_PCT × order_size_btc × mid` — always fires after exactly 2 stop-losses regardless of account size (~2.9% of balance)

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
- `udbhav-ui.service` — runs the main udbhav.uk site (port 8000, /opt/udbhav-ui/)
- `nginx_udbhav-ui.conf` — reverse proxy config
- `udbhav-markets-healthcheck.*` — systemd timer for health checks
- `hyperbot-bot.service` — runs hft_bot/main.py (start/stop only — Restart=no so circuit breaker exits cleanly)
- `hyperbot-ui.service` — management UI Flask app on port 5001 (/opt/hyperbot/hyperbot_ui/)
- `hyperbot-monitor.service` — always-on Telegram monitor (`monitor/telegram_monitor.py`); instant alerts on fills/signals/SL/errors + hourly status summary; Restart=always

### VPS: ubuntu@92.4.75.27
- `/opt/hyperbot/` — clone of this repo (owned by ubuntu)
- `/opt/hyperbot/.env` — secrets (PRIVATE_KEY, HYPERLIQUID_API_URL=mainnet, COIN=BTC)
- `/opt/hyperbot/.venv/` — Python venv with all deps
- Management UI live at `https://udbhav.uk/hyperbot` (nginx basic auth)
- Bot logs at `/opt/hyperbot/hft_bot/bot.log`
- To deploy updates: `ssh VPS "cd /opt/hyperbot && git pull && sudo systemctl restart hyperbot-ui"`
- To SSH: `ssh -i ~/Downloads/ssh-key-2026-02-03.key ubuntu@92.4.75.27`

## General
- Avoid OpenAI SDK imports; this project does not use the OpenAI API
- Always use mainnet (`https://api.hyperliquid.xyz`), never testnet
- Never commit `.env`; `PRIVATE_KEY` must never be exposed
- Always push directly to master
