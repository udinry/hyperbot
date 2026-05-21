"""
Main entry-point for the OFI+TFI HFT bot.

Architecture
------------
                            +---------------------+
  Hyperliquid WS --------->|  WebSocket Thread   |
  (l2Book, trades,         |  (SDK WebsocketMgr) |
   userFills,              +--------+------------+
   orderUpdates)                    | call_soon_threadsafe
                                    v
                            +---------------------+
                            |  asyncio Queue      |
                            +--------+------------+
                                     | await queue.get()
                                     v
                            +---------------------+
                            |  Main async loop    |
                            |  process_book()     |
                            |  ingest_trade()     |
                            |  compute OFI+TFI    |
                            |  evaluate signal    |
                            |  place/cancel order |
                            +--------+------------+
                                     | run_in_executor
                                     v
                            +---------------------+
                            |  ThreadPoolExecutor |
                            |  (blocking HTTP)    |
                            +---------------------+

Key improvements over v1
-------------------------
- Trades WebSocket subscribed for TFI signal confirmation
- IOC execution when spread > WIDE_SPREAD_BPS (fills guaranteed on wide books)
- 800ms cooldown + anti-flap gate (v1 had 200ms causing 30 oscillating signals)
- Spread-adaptive limit pricing (IOC at ask/bid, ALO at best bid/ask)
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import time
from typing import Optional

import config

config.validate()


def _setup_logging() -> None:
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    try:
        fh = logging.handlers.RotatingFileHandler(
            config.LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as exc:
        logging.warning("Could not open log file %s: %s", config.LOG_FILE, exc)


_setup_logging()
logger = logging.getLogger("main")

from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

from executor import OrderExecutor
from state import BotState, BotStatus, Level, OrderBook
from strategy import evaluate_exit_signal, evaluate_signal, ingest_trade, process_book_update, compute_dynamic_tp_pct, get_and_reset_gate_stats

_exchange = None
_account_address: str = ""   # master account address used for all reads
if not config.OBSERVER_MODE:
    from eth_account import Account
    from hyperliquid.exchange import Exchange

    _wallet = Account.from_key(config.PRIVATE_KEY)
    # If ACCOUNT_ADDRESS is set, the private key is an API agent signing on behalf
    # of the master account.  All reads (positions, fills) must use master address.
    _account_address = config.ACCOUNT_ADDRESS or _wallet.address
    logger.info("Signing wallet: %s", _wallet.address)
    logger.info("Account (master): %s", _account_address)
    _exchange = Exchange(
        wallet=_wallet,
        base_url=config.API_URL,
        account_address=_account_address if config.ACCOUNT_ADDRESS else None,
    )
    logger.info("Exchange initialised on %s", config.API_URL)
else:
    logger.warning("OBSERVER MODE — no orders will be placed")
    _wallet = None

_MSG_BOOK   = "book"
_MSG_FILLS  = "fills"
_MSG_ORDERS = "orders"
_MSG_TRADE  = "trade"


class WSManager:
    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        self.queue = queue
        self.loop = loop
        self._info: Optional[Info] = None
        self._sub_ids: list = []

    def start(self) -> None:
        self._info = Info(base_url=config.API_URL, skip_ws=False)
        logger.info("Info client connected to %s", config.API_URL)

        sid1 = self._info.subscribe(
            {"type": "l2Book", "coin": config.COIN},
            self._on_book,
        )
        self._sub_ids.append(sid1)

        sid_trades = self._info.subscribe(
            {"type": "trades", "coin": config.COIN},
            self._on_trades,
        )
        self._sub_ids.append(sid_trades)

        if not config.OBSERVER_MODE and _wallet:
            sid2 = self._info.subscribe(
                {"type": "userFills", "user": _account_address},
                self._on_fills,
            )
            self._sub_ids.append(sid2)

            sid3 = self._info.subscribe(
                {"type": "orderUpdates", "user": _account_address},
                self._on_order_updates,
            )
            self._sub_ids.append(sid3)

        logger.info("WebSocket subscriptions active (ids=%s)", self._sub_ids)

    def stop(self) -> None:
        if self._info:
            try:
                self._info.disconnect_websocket()
            except Exception:
                pass
            self._info = None
        self._sub_ids.clear()

    def is_alive(self) -> bool:
        if self._info is None:
            return False
        mgr = getattr(self._info, "ws_manager", None)
        return mgr is not None and mgr.is_alive()

    def _on_book(self, msg: dict) -> None:
        try:
            data   = msg["data"]
            levels = data["levels"]
            ts_ms  = int(data.get("time", time.time() * 1000))
            book   = OrderBook(
                bids=[Level.from_ws(l) for l in levels[0][: config.OFI_LEVELS + 3]],
                asks=[Level.from_ws(l) for l in levels[1][: config.OFI_LEVELS + 3]],
                timestamp_ms=ts_ms,
            )
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (_MSG_BOOK, book))
        except Exception as exc:
            logger.error("_on_book parse error: %s | msg=%s", exc, msg)

    def _on_fills(self, msg: dict) -> None:
        try:
            data = msg["data"]
            # Hyperliquid sends an isSnapshot=True burst of historical fills on
            # subscribe — skip it; we trust _reconcile_position for startup state.
            if data.get("isSnapshot"):
                logger.info("userFills snapshot (%d fills) — skipped", len(data.get("fills", [])))
                return
            fills = data.get("fills", [])
            for fill in fills:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, (_MSG_FILLS, fill))
        except Exception as exc:
            logger.error("_on_fills parse error: %s | msg=%s", exc, msg)

    def _on_order_updates(self, msg: dict) -> None:
        try:
            for upd in msg.get("data", []):
                self.loop.call_soon_threadsafe(self.queue.put_nowait, (_MSG_ORDERS, upd))
        except Exception as exc:
            logger.error("_on_order_updates parse error: %s | msg=%s", exc, msg)

    def _on_trades(self, msg: dict) -> None:
        try:
            for t in msg.get("data", []):
                self.loop.call_soon_threadsafe(self.queue.put_nowait, (_MSG_TRADE, t))
        except Exception as exc:
            logger.error("_on_trades parse error: %s | msg=%s", exc, msg)


async def _refresh_order_size(state: BotState, info, wallet_address: str) -> None:
    """Fetch live account balance and resize order_size_btc proportionally.
    Runs in the thread pool so it never blocks the event loop."""
    loop = asyncio.get_running_loop()
    try:
        from concurrent.futures import ThreadPoolExecutor as _TPE
        data = await loop.run_in_executor(None, info.user_state, wallet_address)
        perp_equity = float(data["marginSummary"]["accountValue"])
        mid = state.mid_price()
        if not mid or mid <= 0:
            return
        # On HL, spot USDC IS the trading balance — perp accountValue is the subset
        # locked as margin for open positions. Always fetch spot to get the true total.
        import json as _json, urllib.request as _ur
        def _spot():
            payload = _json.dumps({"type": "spotClearinghouseState", "user": wallet_address}).encode()
            req = _ur.Request(
                config.API_URL.rstrip("/") + "/info",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=5) as resp:
                return _json.loads(resp.read())
        spot_usdc = 0.0
        try:
            spot_data = await loop.run_in_executor(None, _spot)
            for b in spot_data.get("balances", []):
                if b.get("coin") == "USDC":
                    spot_usdc = float(b.get("total", 0))
                    break
        except Exception as exc:
            logger.warning("spot balance fetch failed: %s", exc)
        balance = max(perp_equity, spot_usdc)
        logger.debug("Balance: perp_equity=$%.2f spot_usdc=$%.2f → using $%.2f", perp_equity, spot_usdc, balance)
        if balance < 5.0:
            logger.warning(
                "Effective balance $%.2f too low to trade — fund the account on Hyperliquid",
                balance,
            )
            return
        # margin = balance × risk_pct; notional = margin × leverage; btc = notional / mid
        raw = (balance * config.POSITION_RISK_PCT * config.LEVERAGE) / mid
        # Safety cap at 0.1 BTC (~$7700 notional) — sanity limit only, not risk limit.
        new_size = round(max(0.001, min(0.1, raw)), 3)
        if new_size != state.order_size_btc:
            logger.info(
                "Position resize | balance=$%.2f mid=%.2f → %.4f BTC (was %.4f)",
                balance, mid, new_size, state.order_size_btc,
            )
            state.order_size_btc    = new_size
            state.max_inventory_btc = new_size  # inventory limit tracks order size
        new_tp = compute_dynamic_tp_pct(state)
        if new_tp != state.dynamic_tp_pct:
            logger.info("ATR-adaptive TP: %.4f → %.4f (%.2f%%)", state.dynamic_tp_pct, new_tp, new_tp * 100)
            state.dynamic_tp_pct = new_tp
    except Exception as exc:
        logger.warning("refresh_order_size failed: %s — keeping %.4f BTC", exc, state.order_size_btc)


def _fetch_exchange_position() -> Optional[dict]:
    """Direct REST call using MASTER account address — returns position dict or None.
    Must NOT use the agent address (_wallet.address) which has no positions."""
    import urllib.request as _ur, json as _json
    payload = _json.dumps({
        "type": "clearinghouseState",
        "user": _account_address,
    }).encode()
    req = _ur.Request(
        config.API_URL.rstrip("/") + "/info",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _ur.urlopen(req, timeout=5) as resp:
        data = _json.loads(resp.read())
    for ap in data.get("assetPositions", []):
        pos = ap.get("position", {})
        if pos.get("coin") == config.COIN:
            szi = float(pos.get("szi", 0))
            if abs(szi) > 1e-8:
                return {
                    "szi":      szi,
                    "entry_px": float(pos["entryPx"]) if pos.get("entryPx") else None,
                    "unreal":   float(pos.get("unrealizedPnl", 0)),
                }
    return None


async def _cancel_stale_reduce_only_orders(executor: "OrderExecutor") -> None:
    """On startup, cancel any lingering reduce-only BTC orders left from a previous session.
    Without this, every restart leaves an orphaned SL on the exchange."""
    import urllib.request as _ur, json as _json
    loop = asyncio.get_running_loop()

    def _fetch_open():
        payload = _json.dumps({"type": "openOrders", "user": _account_address}).encode()
        req = _ur.Request(
            config.API_URL.rstrip("/") + "/info",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=5) as resp:
            return _json.loads(resp.read())

    try:
        orders = await loop.run_in_executor(None, _fetch_open)
        stale = [o for o in orders if o.get("coin") == config.COIN and o.get("reduceOnly")]
        if stale:
            logger.warning(
                "Startup: cancelling %d stale reduce-only order(s) from previous session", len(stale)
            )
            for o in stale:
                await executor._cancel_sl(int(o["oid"]))
        else:
            logger.info("Startup: no stale reduce-only orders found")
    except Exception as exc:
        logger.warning("_cancel_stale_reduce_only_orders failed: %s", exc)


async def _reconcile_position(
    state: BotState, executor: "OrderExecutor"
) -> None:
    """On startup, sync bot inventory with any existing exchange position and arm SL.
    Uses direct REST (not SDK) to avoid SDK assetPositions bug."""
    loop = asyncio.get_running_loop()
    try:
        pos = await loop.run_in_executor(None, _fetch_exchange_position)
        if pos:
            state.inventory_btc = pos["szi"]
            state.entry_price   = pos["entry_px"]
            logger.info(
                "Startup reconcile | synced %.4f BTC @ entry %.2f (unrealPnL=%.2f$)",
                pos["szi"], pos["entry_px"] or 0, pos["unreal"],
            )
            if pos["entry_px"]:
                sl_oid = await executor.place_stop_loss(
                    abs(pos["szi"]), pos["entry_px"], pos["szi"] > 0
                )
                state.sl_oid = sl_oid
                tp_oid = await executor.place_take_profit(
                    abs(pos["szi"]), pos["entry_px"], pos["szi"] > 0
                )
                state.tp_oid = tp_oid
        else:
            logger.info("Startup reconcile | no open %s position", config.COIN)
    except Exception as exc:
        logger.warning("Position reconciliation failed: %s", exc)


async def exchange_sync(state: BotState, executor: "OrderExecutor") -> None:
    """Every 30s: sync inventory from exchange to catch any fills missed via WS.
    If exchange shows a position the bot doesn't know about, update state and arm SL.
    If exchange shows no position but bot thinks it has one, clear bot state."""
    while state.status not in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
        for _ in range(30):
            if state.status in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
                break
            await asyncio.sleep(1)
        if state.status in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
            break
        if config.OBSERVER_MODE or not _wallet:
            continue
        loop = asyncio.get_running_loop()
        try:
            pos = await loop.run_in_executor(None, _fetch_exchange_position)
            ex_inv = pos["szi"] if pos else 0.0
            if abs(ex_inv - state.inventory_btc) > 1e-6:
                logger.warning(
                    "Exchange sync mismatch | bot=%.4f BTC, exchange=%.4f BTC — syncing",
                    state.inventory_btc, ex_inv,
                )
                # Pause trading during sync so a stale signal can't fire and create
                # a phantom position while state.inventory_btc is being corrected.
                was_running = state.is_running()
                if was_running:
                    state.set_paused_inventory()
                state.inventory_btc = ex_inv
                state.entry_price   = pos["entry_px"] if pos else None
                # Re-arm SL for the real position
                await executor._manage_sl_tp()
                # Resume only if within inventory limit
                if was_running and abs(ex_inv) < state.max_inventory_btc:
                    state.set_running()
                elif abs(ex_inv) >= state.max_inventory_btc:
                    logger.warning(
                        "Exchange sync: inventory %.4f BTC at limit — pausing", ex_inv
                    )
        except Exception as exc:
            logger.debug("Exchange sync failed: %s", exc)
    logger.info("Exchange sync exiting")


async def funding_monitor(state: BotState) -> None:
    """Poll Hyperliquid's per-asset funding rate every 15 minutes.
    Stored in state.funding_rate for use as a directional bias in evaluate_signal."""
    import json as _json, urllib.request as _ur
    loop = asyncio.get_running_loop()

    async def _fetch_once():
        def _req():
            payload = _json.dumps({"type": "metaAndAssetCtxs"}).encode()
            req = _ur.Request(
                config.API_URL.rstrip("/") + "/info",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=10) as resp:
                return _json.loads(resp.read())
        data = await loop.run_in_executor(None, _req)
        meta_list = data[0]["universe"]
        ctxs = data[1]
        for i, m in enumerate(meta_list):
            if m["name"] == config.COIN:
                return float(ctxs[i]["funding"])
        return None

    while state.status not in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
        try:
            rate = await _fetch_once()
            if rate is not None and rate != state.funding_rate:
                logger.info(
                    "Funding rate: %.6f%%/hr (was %.6f%%/hr) | bias=%s",
                    rate * 100, state.funding_rate * 100,
                    "SHORT" if rate > config.FUNDING_BIAS_THRESHOLD else
                    ("LONG" if rate < -config.FUNDING_BIAS_THRESHOLD else "NEUTRAL"),
                )
                state.funding_rate = rate
        except Exception as exc:
            logger.debug("funding_monitor: %s", exc)
        for _ in range(900):
            if state.status in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
                break
            await asyncio.sleep(1)
    logger.info("Funding monitor exiting")


async def position_sizer(state: BotState, info, wallet_address: str) -> None:
    """Background task: re-check account balance every 5 minutes and resize."""
    while state.status not in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
        for _ in range(300):
            if state.status in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
                break
            await asyncio.sleep(1)
        if state.status in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
            break
        await _refresh_order_size(state, info, wallet_address)
    logger.info("Position sizer exiting")


async def risk_monitor(state: BotState, executor: OrderExecutor) -> None:
    while state.status not in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
        await asyncio.sleep(0.1)

        if state.inventory_btc != 0.0 and state.entry_price:
            unrealised     = state.unrealized_pnl_usd()
            position_value = abs(state.inventory_btc) * state.entry_price
            if position_value > 0:
                loss_pct = -unrealised / position_value
                if loss_pct >= config.STOP_LOSS_PCT:
                    logger.warning(
                        "STOP-LOSS triggered | loss=%.2f%% > %.2f%% | inv=%.4f BTC",
                        loss_pct * 100, config.STOP_LOSS_PCT * 100, state.inventory_btc,
                    )
                    await executor.emergency_close("stop_loss")

        # Position time limit: if position held longer than MAX_POSITION_HOLD_MS,
        # close at market. OFI signal half-life is 10-30s; stale positions add
        # directional risk with no remaining edge.
        max_hold_ms = getattr(config, "MAX_POSITION_HOLD_MS", 0)
        if (max_hold_ms > 0 and state.position_open_ms is not None
                and state.inventory_btc != 0.0):
            import time as _time
            held_ms = int(_time.monotonic_ns() // 1_000_000) - state.position_open_ms
            if held_ms >= max_hold_ms:
                logger.warning(
                    "POSITION TIME LIMIT | held=%ds > %ds | closing at market",
                    held_ms // 1000, max_hold_ms // 1000,
                )
                await executor.emergency_close("time_limit")

        if state.daily_pnl_usd <= -state.max_daily_loss_usd:
            logger.critical(
                "CIRCUIT BREAKER | daily_pnl=%.2f$ <= -%.2f$",
                state.daily_pnl_usd, state.max_daily_loss_usd,
            )
            state.set_circuit_breaker()
            await executor.cancel_all_orders()
            await executor.emergency_close("circuit_breaker")
            break

        if state.status == BotStatus.RUNNING:
            if (state.inventory_btc >= state.max_inventory_btc or
                    state.inventory_btc <= -state.max_inventory_btc):
                state.set_paused_inventory()
                logger.warning("INVENTORY LIMIT | inv=%.4f BTC | pausing", state.inventory_btc)

        elif state.status == BotStatus.PAUSED_INVENTORY:
            if abs(state.inventory_btc) <= state.max_inventory_btc * 0.8:
                state.set_running()
                logger.info("INVENTORY RESUMED | inv=%.4f BTC", state.inventory_btc)

    logger.info("Risk monitor exiting (status=%s)", state.status.value)


async def ws_health_monitor(
    ws_manager_holder: list,
    queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    state: BotState,
) -> None:
    while state.status not in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
        await asyncio.sleep(5)

        ws_mgr = ws_manager_holder[0]
        if not ws_mgr.is_alive():
            if state.ws_reconnect_count >= config.WS_MAX_RECONNECTS:
                logger.critical("WS dead after %d reconnects — giving up", config.WS_MAX_RECONNECTS)
                state.set_stopped()
                break

            state.ws_reconnect_count += 1
            delay = min(config.WS_RECONNECT_DELAY_S * (2 ** (state.ws_reconnect_count - 1)), 30)
            logger.warning(
                "WS thread dead — reconnect attempt %d/%d in %.1fs",
                state.ws_reconnect_count, config.WS_MAX_RECONNECTS, delay,
            )
            await asyncio.sleep(delay)

            ws_mgr.stop()
            new_mgr = WSManager(queue, loop)
            try:
                new_mgr.start()
                ws_manager_holder[0] = new_mgr
                logger.info("WS reconnected successfully")
            except Exception as exc:
                logger.error("WS reconnect failed: %s", exc, exc_info=True)
        else:
            state.ws_reconnect_count = 0

    logger.info("WS health monitor exiting")


async def stats_logger(state: BotState) -> None:
    _gate_log_interval = 600  # log gate suppression counts every 10 minutes
    _last_gate_log = 0.0
    while state.status not in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
        await asyncio.sleep(2)
        summary = state.summary()
        if config.LIVE_TEST_SCALE != 1.0:
            scaled_pnl = state.daily_pnl_usd * config.LIVE_TEST_SCALE
            scaled_upnl = state.unrealized_pnl_usd() * config.LIVE_TEST_SCALE
            summary += f" [×{config.LIVE_TEST_SCALE:.0f} → realPnL≈{scaled_pnl:+.2f}$ uPnL≈{scaled_upnl:+.2f}$]"
        logger.info("STATE | %s", summary)

        now = time.monotonic()
        if now - _last_gate_log >= _gate_log_interval:
            counts = get_and_reset_gate_stats()
            if counts:
                parts = " | ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                logger.info("GATE STATS (10m) | %s", parts)
            _last_gate_log = now


async def main_loop(
    state: BotState,
    executor: OrderExecutor,
    queue: asyncio.Queue,
) -> None:
    logger.info("Main event loop started")

    while state.status not in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
        try:
            msg_type, payload = await asyncio.wait_for(queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.debug("Queue idle (no WS messages in 5s)")
            continue

        if msg_type == _MSG_BOOK:
            await _handle_book(state, executor, payload)
        elif msg_type == _MSG_FILLS:
            executor.handle_fill(payload)
            logger.debug("State after fill: %s", state.summary())
        elif msg_type == _MSG_ORDERS:
            _handle_order_update(state, payload)
        elif msg_type == _MSG_TRADE:
            ingest_trade(state, payload)

    logger.info("Main event loop exiting (status=%s)", state.status.value)


async def _handle_book(
    state: BotState,
    executor: OrderExecutor,
    book: OrderBook,
) -> None:
    ofi = process_book_update(state, book)
    if ofi is None:
        return

    spread     = book.spread() or 0.0
    mid        = book.mid_price() or 0.0
    spread_bps = (spread / mid * 10_000) if mid > 0 else 9999
    use_ioc    = spread_bps > config.WIDE_SPREAD_BPS or config.ENTRY_IOC

    # IOC slippage buffer: 50 ticks above best_ask / below best_bid to handle stale L2 cache.
    _IOC_SLIP = 200 * config.PRICE_TICK

    # --- Entry signals (only when running with room to add) ---
    if state.is_running():
        signal = evaluate_signal(state, ofi)
        if signal is not None:
            if signal == "buy":
                best_bid = book.best_bid()
                best_ask = book.best_ask()
                if best_ask is None:
                    return
                if use_ioc:
                    price = best_ask.price + _IOC_SLIP
                else:
                    price = (best_bid.price + config.PRICE_TICK) if best_bid else (best_ask.price - config.PRICE_TICK)
                    if price >= best_ask.price:
                        price = best_ask.price - config.PRICE_TICK
                await executor.place_limit_order(is_buy=True, price=price, size=state.order_size_btc, spread=spread, force_ioc=use_ioc)
                state.last_signal_ms = int(time.monotonic_ns() // 1_000_000) + config.LIMIT_ORDER_TIMEOUT_MS

            elif signal == "sell":
                best_bid = book.best_bid()
                best_ask = book.best_ask()
                if best_bid is None:
                    return
                if use_ioc:
                    price = best_bid.price - _IOC_SLIP
                else:
                    price = (best_ask.price - config.PRICE_TICK) if best_ask else (best_bid.price + config.PRICE_TICK)
                    if price <= best_bid.price:
                        price = best_bid.price + config.PRICE_TICK
                await executor.place_limit_order(is_buy=False, price=price, size=state.order_size_btc, spread=spread, force_ioc=use_ioc)
                state.last_signal_ms = int(time.monotonic_ns() // 1_000_000) + config.LIMIT_ORDER_TIMEOUT_MS

    # --- Exit signals (when holding a position, check OFI for early close) ---
    elif state.status == BotStatus.PAUSED_INVENTORY:
        # Skip exit signal when uPnL is positive but too small to cover IOC round-trip fees.
        # IOC-IOC costs ~0.07% notional; exiting at <$0.54 profit on 0.01 BTC is net negative.
        _upnl = (mid - (state.entry_price or mid)) * state.inventory_btc
        _ioc_fee_breakeven = 2 * 0.00035 * abs(state.inventory_btc) * mid
        _skip_exit = 0 < _upnl < _ioc_fee_breakeven
        exit_sig = None if _skip_exit else evaluate_exit_signal(state, ofi)
        if exit_sig is not None:
            sz = abs(state.inventory_btc)
            # Block re-evaluation until fill arrives — prevents double-exit race condition.
            _now_ms = int(time.monotonic_ns() // 1_000_000)
            state.last_exit_ms = _now_ms + config.LIMIT_ORDER_TIMEOUT_MS
            if exit_sig == "sell":  # close long — force IOC reduce-only sell
                best_bid = book.best_bid()
                if best_bid is None:
                    return
                price = best_bid.price - _IOC_SLIP
                await executor.place_limit_order(
                    is_buy=False, price=price, size=sz, spread=spread,
                    reduce_only=True, force_ioc=True,
                )
            elif exit_sig == "buy":  # close short — force IOC reduce-only buy
                best_ask = book.best_ask()
                if best_ask is None:
                    return
                price = best_ask.price + _IOC_SLIP
                await executor.place_limit_order(
                    is_buy=True, price=price, size=sz, spread=spread,
                    reduce_only=True, force_ioc=True,
                )


def _handle_order_update(state: BotState, update: dict) -> None:
    try:
        order_data = update.get("order", {})
        status     = update.get("status", "")
        oid        = int(order_data.get("oid", 0))

        if status in ("canceled", "rejected", "marginCanceled"):
            order = state.open_orders.pop(oid, None)
            if order:
                logger.info(
                    "Order %s oid=%d (was %.4f BTC @ %.2f)",
                    status, oid, order.size, order.price,
                )
                state.total_orders_cancelled += 1
    except Exception as exc:
        logger.error("_handle_order_update error: %s | data=%s", exc, update)


def _install_signal_handlers(state: BotState, executor: OrderExecutor, loop: asyncio.AbstractEventLoop) -> None:
    async def _shutdown():
        logger.warning("Shutdown signal received — cancelling orders and closing positions")
        state.set_stopped()
        await executor.cancel_all_orders()
        # Verify against exchange before deciding whether to close — bot state may be stale.
        if not config.OBSERVER_MODE and _wallet:
            try:
                pos = await loop.run_in_executor(None, _fetch_exchange_position)
                if pos:
                    state.inventory_btc = pos["szi"]
            except Exception as exc:
                logger.error("Shutdown position fetch failed: %s", exc)
        if state.inventory_btc != 0.0:
            if state.tp_oid is not None and state.sl_oid is not None:
                # TP+SL resting on exchange protect the position — safe to leave managed.
                logger.info(
                    "Shutdown: TP+SL both active on exchange — leaving %.4f BTC position managed",
                    state.inventory_btc,
                )
            else:
                logger.warning(
                    "Shutdown: %.4f BTC open with no active TP+SL — closing at market",
                    state.inventory_btc,
                )
                await executor.emergency_close("graceful_shutdown")

    def _handler():
        loop.create_task(_shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            pass


async def run() -> None:
    loop  = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)

    state    = BotState()
    executor = OrderExecutor(exchange=_exchange, state=state, loop=loop, account_address=_account_address)

    _install_signal_handlers(state, executor, loop)

    await executor.set_leverage()

    # Dynamic position sizing: fetch balance once before first trade.
    # Refreshed every 5 min by position_sizer task. Skipped in observer mode.
    _info_rest = Info(base_url=config.API_URL, skip_ws=True)
    if not config.OBSERVER_MODE and _wallet:
        await _refresh_order_size(state, _info_rest, _account_address)
        await _cancel_stale_reduce_only_orders(executor)
        await _reconcile_position(state, executor)
    logger.info("Order size: %.4f BTC", state.order_size_btc)

    ws_mgr = WSManager(queue, loop)
    ws_mgr.start()
    ws_manager_holder = [ws_mgr]

    logger.info("Waiting for initial L2 book snapshot...")
    deadline = time.monotonic() + 15
    while state.book.timestamp_ms == 0:
        if time.monotonic() > deadline:
            logger.error("Timed out waiting for initial book snapshot — aborting")
            ws_mgr.stop()
            return
        try:
            msg_type, payload = await asyncio.wait_for(queue.get(), timeout=2.0)
            if msg_type == _MSG_BOOK:
                process_book_update(state, payload)
                logger.info("Initial book received: mid=%.2f", state.mid_price() or 0)
        except asyncio.TimeoutError:
            pass

    # Re-run sizing now that mid price is known — startup call returned early (mid=None then)
    if not config.OBSERVER_MODE and _wallet:
        await _refresh_order_size(state, _info_rest, _account_address)
        logger.info("Order size after book: %.4f BTC", state.order_size_btc)

    state.set_running()
    logger.info("Bot is LIVE | %s", state.summary())

    sizer_task = (
        loop.create_task(position_sizer(state, _info_rest, _account_address), name="position_sizer")
        if not config.OBSERVER_MODE and _wallet else None
    )

    tasks = [
        loop.create_task(main_loop(state, executor, queue),          name="main_loop"),
        loop.create_task(risk_monitor(state, executor),              name="risk_monitor"),
        loop.create_task(ws_health_monitor(ws_manager_holder, queue, loop, state), name="ws_health"),
        loop.create_task(stats_logger(state),                        name="stats_logger"),
        loop.create_task(exchange_sync(state, executor),             name="exchange_sync"),
        loop.create_task(funding_monitor(state),                     name="funding_monitor"),
    ]
    if sizer_task:
        tasks.append(sizer_task)

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Tasks cancelled — shutting down")
    finally:
        for t in tasks:
            t.cancel()
        ws_manager_holder[0].stop()
        logger.info("Final state: %s", state.summary())
        scale_str = ""
        if config.LIVE_TEST_SCALE != 1.0:
            scale_str = f" | projected×{config.LIVE_TEST_SCALE:.0f}={state.daily_pnl_usd * config.LIVE_TEST_SCALE:+.2f}$"
        logger.info(
            "Session summary | orders=%d fills=%d cancelled=%d buys=%d sells=%d | realised_PnL=%.2f$%s",
            state.total_orders_placed, state.total_orders_filled, state.total_orders_cancelled,
            state.total_buys_filled, state.total_sells_filled, state.daily_pnl_usd, scale_str,
        )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
