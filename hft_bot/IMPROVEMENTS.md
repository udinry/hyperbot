# HFT Bot — Audit Response & Improvements

This document records the changes made to the OFI+TFI bot in response to the
code audit, why each one matters, and how to operate the new features. It is
the changelog *and* the operator's guide for the new behaviour.

> **Operating rule that supersedes the backtest numbers:** several of these
> changes alter what the strategy actually computes (most importantly the TFI
> clock fix). The previously reported 82.7% accuracy described the *old, buggy*
> behaviour. **Treat the post-fix bot as unvalidated** and re-run paper trading
> from scratch before scaling size.

---

## 1. Critical correctness fixes

### 1.1 The TFI/VWAP clock-domain bug (the big one)

**Symptom.** Trades were timestamped with the exchange's epoch-milliseconds
`time` field (~1.78×10¹²), but the window that prunes them compared against
`time.monotonic_ns()` (milliseconds-since-boot, a much smaller number). Epoch
ms is always greater than the cutoff, so the prune loop removed nothing: the
"400 ms" trade window silently became "the last 2000 trades" — minutes of tape.
Three of the fourteen signal gates (TFI confirmation, VWAP-directional,
VWAP-overextension) were therefore operating on the wrong horizon.

**Fix.** Introduced `clock.py`, a single time source for the whole bot. Trades
are now stamped at ingest with `clock.now_ms()`, the same clock the pruner uses.
Exchange epoch timestamps never enter a pruned window. `ingest_trade` also now
logs malformed trades instead of swallowing them with a bare `except: pass`
(which is what hid this bug for months).

**Why a single clock module.** Cooldowns and timeouts must use a monotonic
clock (immune to NTP steps); market-event ordering must use a consistent
domain. Centralising the time source means (a) every window/cooldown agrees,
and (b) the replay backtest can inject *recorded* time so windows advance with
the tape, not with how fast the CPU chews through the file.

### 1.2 Circuit breaker: fee-aware, daily, and restart-proof

The realized-PnL breaker had three holes:

- **Ignored fees.** `closedPnl` from Hyperliquid excludes fees. `record_fill`
  now subtracts the fill's `fee`, so realized P&L is net — exactly on the
  costly paths (IOC exits, emergency closes) where the old number lied.
- **Never reset.** "Daily" loss was actually session-cumulative.
  `roll_daily_pnl_if_new_day()` resets the ledger at UTC midnight.
- **Wiped on restart.** Bouncing the service zeroed the loss and re-armed the
  breaker. The ledger is now persisted to `daily_pnl_state.json` (atomic
  write) and reloaded at startup; if today's loss already exceeds the limit,
  the bot starts in `CIRCUIT_BREAKER` and refuses to trade until the next day.

### 1.3 Emergency-close idempotency

`risk_monitor` polls every 100 ms. After a stop-loss it awaited
`emergency_close`, but `inventory_btc` only clears when the WS fill arrives —
so every loop iteration in that window fired another `market_close`.
`emergency_close` now takes a lock and a 2-second cool-off: one close in
flight at a time, no duplicate market orders hammering the API during a
stop-out. A *failed* close clears the cool-off immediately so the retry isn't
masked.

### 1.4 Stop-loss placement now escalates

A position without a stop is the worst state the bot can be in.
`place_stop_loss` retries 3× with backoff (handles transient API / rate-limit
failures). If it still fails, `_manage_sl_tp` / `_manage_stop_loss` escalate to
`emergency_close("sl_placement_failed")` — flattening is cheaper than running
naked.

### 1.5 Position sizing uses perp equity only

`balance = max(perp_equity, spot_usdc)` sized positions against spot USDC,
which cannot margin a perp position on Hyperliquid without a transfer. Sizing
now uses perp `accountValue`. Spot is only checked to print an *actionable*
warning ("$X sits in spot — transfer it to perp").

---

## 2. Performance & latency

### 2.1 Book conflation + non-blocking order placement

`main_loop` now drains the queue each wake-up: it processes every event-like
message (fills, order updates, trades) in order, but collapses book snapshots
to the **newest** one. Books are *state* (a stale snapshot describes a market
that no longer exists); fills are *events* (permanent consequences) and are
never dropped. Order placement is dispatched with `asyncio.create_task` so a
slow exchange round-trip can't stall the next book tick or fill. The entry
lockout is set *before* the task is scheduled, so no second entry can fire
while one is in flight.

This also fixed a latent data bug: previously, books queued behind a blocking
order placement were processed late but stamped with a *fresh* `now_ms()`,
smearing the OFI window with stale prices carrying new timestamps.

### 2.2 O(1) rolling windows

`RollingOFIWindow` and `RollingTradeWindow` (in `state.py`) maintain running
sums (`total`, `buy_vol`, `sell_vol`, `px_vol`) updated on append/prune, so
OFI, TFI and VWAP are O(1) per tick instead of re-summing the whole deque.

### 2.3 Idempotent order placement (`cloid`)

The client order id that was generated and then thrown away is now passed to
`exchange.order(...)`. If an HTTP response is lost *after* the exchange
accepted the order, the order can be found/cancelled by our own id instead of
being discovered 30 s later by `exchange_sync`.

### 2.4 Unbounded queue + data-staleness watchdog

The bounded `maxsize=10_000` queue dropped FILL events on overflow (via
`put_nowait` raising inside the WS callback). The conflating loop drains faster
than the socket fills, so the queue is now unbounded. `ws_health_monitor` also
checks **data freshness**, not just thread liveness: a WS that is alive but
wedged (no book for 20 s) now forces a reconnect.

### 2.5 Dynamic-attribute removal

`_persist_buy`, `_persist_sell`, `_last_signal_dir` were injected onto `state`
at runtime via `hasattr`. They are now real `BotState` fields (`persist_buy`,
`persist_sell`, `last_signal_dir`), and the entry lockout has its own field
(`lockout_until_ms`) instead of overloading `last_signal_ms` with a future
value.

---

## 3. Strategy robustness

### 3.1 Signal/exit horizon mismatch → hold-time limit on by default

The audit's most important *strategy* point: the OFI signal half-life is
10–30 s (and the candle backtest shows accuracy falling from ~84% at T+1min to
~58% at T+3–5min), yet the live TP targets a move that takes hours, with the
hold-time limit disabled. A position older than the signal horizon is no longer
an OFI trade — it's an unhedged directional bet. `MAX_POSITION_HOLD_MS` now
defaults to **600000 (10 min)**; after that the position is closed at market
regardless of P&L. Set `MAX_POSITION_HOLD_MS=0` to restore the old behaviour.

### 3.2 ATR ceiling (spike-regime guard)

The existing ATR *floor* blocks dead-flat markets. The new `ATR_MAX_TRADE_USD`
ceiling blocks the opposite danger: when 1-min ATR approaches the stop
distance, the 0.5% SL sits inside ordinary noise (and gets hunted) while OFI
saturates on a depleted book. Default 0 (disabled); recommended ≈ half the
dollar stop distance (e.g. SL 0.5% on $77k ≈ $385 → `ATR_MAX_TRADE_USD≈190`).
Suppressions are counted under the `atr_high_vol` gate key in GATE STATS.

### 3.3 Replay backtest now drives the strategy clock

`backtest.py replay` installs a clock source that returns the recorded
`wall_ms` of each event. Without this, the OFI/TFI windows and every cooldown
advanced with CPU time, collapsing hours of tape into a 400 ms window — i.e.
the replay was not testing the live strategy. This is now the *only* mode whose
numbers should be quoted for performance, because it runs the exact live code.

---

## 4. Real-test mode (scaled-down live trading on mainnet)

**What it is.** `REAL_TEST_MODE=1` places **real** orders on mainnet at the
exchange-minimum size (0.001 BTC) regardless of `POSITION_RISK_PCT`, so order
placement, ALO queueing, fills, slippage and SL/TP mechanics are all exercised
with real money but minimal risk. The bot computes the scale factor
automatically (what full size *would* have been ÷ the test size), tags every
P&L log line with the projected-at-scale number, and writes a session report
on shutdown.

**How to run.**

```bash
cd hft_bot
# .env must have PRIVATE_KEY and HYPERLIQUID_API_URL=https://api.hyperliquid.xyz
REAL_TEST_MODE=1 python main.py
```

**The report.** On shutdown the bot writes `real_test_report.json` and
`real_test_report.md` comparing actual vs scaled-up P&L, fill rate, and fees:

```
| | Actual (0.001 BTC) | Projected ×10 |
|---|---|---|
| Realized P&L (net of fees) | +$0.18 | +$1.80 |
| Fees paid                  |  $0.07 |  $0.70 |
```

**Honesty caveat (printed in the report).** Fees and rebates scale linearly
with size, so the projection is fair for them. Queue position and book impact
do **not** scale linearly — a 0.01 BTC order sits further back in the queue and
moves the book more than a 0.001 BTC order. Treat scaled P&L as *optimistic*
for sizes large relative to L1 depth. This is the same reason the audit warned
that ALO fill rates and adverse selection won't extrapolate cleanly.

---

## 5. Security hardening (web stack)

These are outside `hft_bot/` but were part of the audit:

- **`hyperbot_ui/app.py`** — every route (including the `sudo systemctl
  start/stop` endpoints that control the live bot) now requires app-level basic
  auth when `HYPERBOT_UI_USER` / `HYPERBOT_UI_PASSWORD` are set, using
  constant-time comparison. Unset → a loud warning, behaviour unchanged
  (back-compat with the nginx basic-auth deployment). The hardcoded wallet
  address is now overridable via `HYPERBOT_WALLET_ADDRESS`.
- **`udbhav_app.py` / `live_udbhav_ui/app.py`** — the guessable
  `"dev-secret-change-me"` Flask secret-key default is replaced with an
  ephemeral random key (fail-safe, not fail-open) plus a warning to set
  `FLASK_SECRET_KEY`. Passwords are now stored as salted `werkzeug` hashes;
  login verifies via `check_password_hash` with a constant-time fallback for
  legacy plaintext rows so existing users keep working.

---

## 6. Tests

New `hft_bot/tests/` suite (40 tests, run `python -m pytest tests/`):

- `test_strategy.py` — OFI level math, the clock-domain fix (a trade stamped
  with epoch ms must still prune), TFI/VWAP values, and each major gate
  (cooldown, lockout, anti-flap, persistence, ATR ceiling).
- `test_state.py` — rolling-window running sums vs brute force, fee-aware P&L,
  average-entry math, daily-ledger persistence/rollover, and that `summary()`
  still matches the management-UI log regex.
- `test_executor.py` — emergency-close idempotency, `cloid` propagation,
  fee-subtracting fill handling, and SL-failure → emergency-close escalation.

`conftest.py` provides a `fake_clock` fixture built on `clock.set_source`, so
time-dependent logic is tested deterministically.

---

## 7. New / changed config keys

| Key | Default | Meaning |
|---|---|---|
| `REAL_TEST_MODE` | off | Trade real 0.001 BTC orders on mainnet; auto-scale P&L projection + write report |
| `MAX_POSITION_HOLD_MS` | 600000 | Close at market after this long (was 0/disabled) |
| `ATR_MAX_TRADE_USD` | 0 (off) | Suppress entries when 1-min ATR exceeds this (spike guard) |

New runtime files (git-ignored): `daily_pnl_state.json`, `real_test_report.json`,
`real_test_report.md`.
