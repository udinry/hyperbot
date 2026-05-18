"""
Main entry-point for the OFI HFT bot.

Architecture
------------
                            ┌─────────────────────┐
  Hyperliquid WS ──────────▶│  WebSocket Thread   │
  (l2Book, trades,          │  (SDK WebsocketMgr) │
   userFills,               └────────┬────────────┘
   orderUpdates)                     │ call_soon_threadsafe
                                     ▼
                            ┌─────────────────────┐
                            │  asyncio Queue      │
                            └────────┬────────────┘
                                     │ await queue.get()
                                     ▼
                            ┌─────────────────────┐
                            │  Main async loop     │
                            │  • process_book()    │
                            │  • compute OFI       │
                            │  • evaluate signal   │
                            │  • place/cancel ordr │
                            └────────┬────────────┘
                                     │ run_in_executor
                                     ▼
                            ┌─────────────────────┐
                            │  ThreadPoolExecutor │
                            │  (blocking HTTP)     │
                            └─────────────────────┘

Reconnection
------------
If the WS thread dies (e.g. network drop) the main loop detects it via a
periodic health-check and re-initialises the Info client and subscriptions.

Risk checks
-----------
A separate asyncio task runs every 100ms checking:
  - Stop-loss on current position
  - Max daily-loss circuit breaker
  - Inventory limit → pause/resume quoting
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

# Validate config first; will SystemExit if something is wrong.
config.validate()

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s │ %(message)s",
        datefmt="%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Rotating file handler
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

# ---------------------------------------------------------------------------
# Hyperliquid SDK imports (after logging so warnings from SDK are captured)
# ---------------------------------------------------------------------------
from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

from executor import OrderExecutor
from state import BotState, BotStatus, Level, OrderBook
from strategy import evaluate_signal, process_book_update

# ---------------------------------------------------------------------------
# Optional: Exchange import (skipped in observer mode)
# ---------------------------------------------------------------------------
_exchange = None
if not config.OBSERVER_MODE:
    from eth_account import Account
    from hyperliquid.exchange import Exchange

    _wallet = Account.from_key(config.PRIVATE_KEY)
    logger.info("Wallet address: %s", _wallet.address)
    _exchange = Exchange(
        wallet=_wallet,
        base_url=config.API_URL,
    )
    logger.info("Exchange initialised on %s", config.API_URL)
else:
    logger.warning("OBSERVER MODE — no orders will be placed")
    _wallet = None

# ---------------------------------------------------------------------------
# Event queue: bridge between WS thread and asyncio event loop
# ---------------------------------------------------------------------------
_MSG_BOOK = "book"
_MSG_FILLS = "fills"
_MSG_ORDERS = "orders"


# ---------------------------------------------------------------------------
# WebSocket subscription management
# ---------------------------------------------------------------------------

class WSManager:
    """
    Wraps the Hyperliquid Info client and its subscriptions.
    Re-created on each reconnect attempt.
    """

    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
        self.queue = queue
        self.loop = loop
        self._info: Optional[Info] = None
        self._sub_ids: list = []

    def start(self) -> None:
        self._info = Info(base_url=config.API_URL, skip_ws=False)
        logger.info("Info client connected to %s", config.API_URL)

        # Subscribe to L2 order book
        sid1 = self._info.subscribe(
            {"type": "l2Book", "coin": config.COIN},
            self._on_book,
        )
        self._sub_ids.append(sid1)

        # Subscribe to user fills (for fill confirmation & PnL tracking)
        if not config.OBSERVER_MODE and _wallet:
            sid2 = self._info.subscribe(
                {"type": "userFills", "user": _wallet.address},
                self._on_fills,
            )
            self._sub_ids.append(sid2)

            sid3 = self._info.subscribe(
                {"type": "orderUpdates", "user": _wallet.address},
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

    # ------------------------------------------------------------------
    # WS callbacks (called from the SDK's WebSocket thread)
    # ------------------------------------------------------------------

    def _on_book(self, msg: dict) -> None:
        """Received l2Book message → push to asyncio queue."""
        try:
            data = msg["data"]
            levels = data["levels"]
            ts_ms = int(data.get("time", time.time() * 1000))

            book = OrderBook(
                bids=[Level.from_ws(l) for l in levels[0][: config.OFI_LEVELS + 3]],
                asks=[Level.from_ws(l) for l in levels[1][: config.OFI_LEVELS + 3]],
                timestamp_ms=ts_ms,
            )
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (_MSG_BOOK, book))
        except Exception as exc:
            logger.error("_on_book parse error: %s | msg=%s", exc, msg)

    def _on_fills(self, msg: dict) -> None:
        """Received userFills message → push each fill to asyncio queue."""
        try:
            data = msg["data"]
            fills = data.get("fills", [])
            for fill in fills:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, (_MSG_FILLS, fill))
        except Exception as exc:
            logger.error("_on_fills parse error: %s | msg=%s", exc, msg)

    def _on_order_updates(self, msg: dict) -> None:
        """Received orderUpdates message → push to asyncio queue."""
        try:
            updates = msg.get("data", [])
            for upd in updates:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, (_MSG_ORDERS, upd))
        except Exception as exc:
            logger.error("_on_order_updates parse error: %s | msg=%s", exc, msg)


# ---------------------------------------------------------------------------
# Risk monitor task
# ---------------------------------------------------------------------------

async def risk_monitor(state: BotState, executor: OrderExecutor) -> None:
    """
    Runs every 100ms.  Checks:
      1. Stop-loss on current position
      2. Max daily-loss circuit breaker
      3. Inventory-delta limit → PAUSED / RUNNING transitions
    """
    while state.status not in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
        await asyncio.sleep(0.1)

        # 1. Stop-loss
        if state.inventory_btc != 0.0 and state.entry_price:
            unrealised = state.unrealized_pnl_usd()
            position_value = abs(state.inventory_btc) * state.entry_price
            if position_value > 0:
                loss_pct = -unrealised / position_value
                if loss_pct >= config.STOP_LOSS_PCT:
                    logger.warning(
                        "STOP-LOSS triggered | loss=%.2f%% > %.2f%% | inv=%.4f BTC | entry=%.2f",
                        loss_pct * 100, config.STOP_LOSS_PCT * 100,
                        state.inventory_btc, state.entry_price,
                    )
                    await executor.emergency_close("stop_loss")

        # 2. Circuit breaker
        if state.daily_pnl_usd <= -config.MAX_DAILY_LOSS_USD:
            logger.critical(
                "CIRCUIT BREAKER | daily_pnl=%.2f$ ≤ -%.2f$",
                state.daily_pnl_usd, config.MAX_DAILY_LOSS_USD,
            )
            state.set_circuit_breaker()
            await executor.cancel_all_orders()
            await executor.emergency_close("circuit_breaker")
            break

        # 3. Inventory limit transitions
        if state.status == BotStatus.RUNNING:
            long_limit = state.inventory_btc >= config.MAX_INVENTORY_BTC
            short_limit = state.inventory_btc <= -config.MAX_INVENTORY_BTC
            if long_limit or short_limit:
                state.set_paused_inventory()
                logger.warning(
                    "INVENTORY LIMIT | inv=%.4f BTC | pausing", state.inventory_btc
                )

        elif state.status == BotStatus.PAUSED_INVENTORY:
            # Resume when inventory pulls back to 80% of the limit.
            resume_threshold = config.MAX_INVENTORY_BTC * 0.8
            if abs(state.inventory_btc) <= resume_threshold:
                state.set_running()
                logger.info(
                    "INVENTORY RESUMED | inv=%.4f BTC", state.inventory_btc
                )

    logger.info("Risk monitor exiting (status=%s)", state.status.value)


# ---------------------------------------------------------------------------
# WS health monitor task
# ---------------------------------------------------------------------------

async def ws_health_monitor(
    ws_manager_holder: list,   # mutable wrapper [WSManager]
    queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    state: BotState,
) -> None:
    """
    Checks every 5 seconds whether the WS thread is still alive.
    If dead, creates a new WSManager and re-subscribes.
    """
    while state.status not in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
        await asyncio.sleep(5)

        ws_mgr = ws_manager_holder[0]
        if not ws_mgr.is_alive():
            if state.ws_reconnect_count >= config.WS_MAX_RECONNECTS:
                logger.critical(
                    "WS dead after %d reconnects — giving up", config.WS_MAX_RECONNECTS
                )
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
            # Reset reconnect counter on healthy tick.
            state.ws_reconnect_count = 0

    logger.info("WS health monitor exiting")


# ---------------------------------------------------------------------------
# Stats logger task
# ---------------------------------------------------------------------------

async def stats_logger(state: BotState) -> None:
    """Prints a periodic summary every 10 seconds."""
    while state.status not in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
        await asyncio.sleep(10)
        logger.info("STATE | %s", state.summary())


# ---------------------------------------------------------------------------
# Main event loop
# ---------------------------------------------------------------------------

async def main_loop(
    state: BotState,
    executor: OrderExecutor,
    queue: asyncio.Queue,
) -> None:
    """
    Core event loop: consumes messages from the asyncio queue and drives
    strategy → execution.
    """
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

    logger.info("Main event loop exiting (status=%s)", state.status.value)


async def _handle_book(
    state: BotState,
    executor: OrderExecutor,
    book: OrderBook,
) -> None:
    """Process one L2 book update: compute OFI and act on signal."""
    ofi = process_book_update(state, book)

    if ofi is None or not state.is_running():
        return

    signal = evaluate_signal(state, ofi)
    if signal is None:
        return

    # Determine limit price.
    if signal == "buy":
        # Bid at best ask - edge ticks to capture spread (maker pricing).
        best_ask = book.best_ask()
        if best_ask is None:
            logger.warning("No ask price available for BUY order")
            return
        # Price inside the spread: bid just below the current best ask.
        price = best_ask.price - (config.EDGE_TICKS * config.PRICE_TICK)
        # But not above best bid (would cross immediately, violating ALO).
        if book.best_bid():
            price = min(price, book.best_bid().price)
        await executor.place_limit_order(is_buy=True, price=price, size=config.ORDER_SIZE_BTC)

    elif signal == "sell":
        best_bid = book.best_bid()
        if best_bid is None:
            logger.warning("No bid price available for SELL order")
            return
        price = best_bid.price + (config.EDGE_TICKS * config.PRICE_TICK)
        if book.best_ask():
            price = max(price, book.best_ask().price)
        await executor.place_limit_order(is_buy=False, price=price, size=config.ORDER_SIZE_BTC)


def _handle_order_update(state: BotState, update: dict) -> None:
    """Process an orderUpdates event (cancel confirmations, etc.)."""
    try:
        order_data = update.get("order", {})
        status = update.get("status", "")
        oid = int(order_data.get("oid", 0))

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


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def _install_signal_handlers(state: BotState, executor: OrderExecutor, loop: asyncio.AbstractEventLoop) -> None:
    async def _shutdown():
        logger.warning("Shutdown signal received — cancelling orders and closing positions")
        state.set_stopped()
        await executor.cancel_all_orders()
        if state.inventory_btc != 0.0:
            await executor.emergency_close("graceful_shutdown")

    def _handler():
        loop.create_task(_shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler.
            pass


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def run() -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)

    state = BotState()
    executor = OrderExecutor(exchange=_exchange, state=state, loop=loop)

    _install_signal_handlers(state, executor, loop)

    # Set leverage before subscribing.
    await executor.set_leverage()

    # Start WebSocket subscriptions.
    ws_mgr = WSManager(queue, loop)
    ws_mgr.start()
    ws_manager_holder = [ws_mgr]

    # Wait briefly for initial book snapshot to arrive before going live.
    logger.info("Waiting for initial L2 book snapshot…")
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

    state.set_running()
    logger.info("Bot is LIVE | %s", state.summary())

    # Spawn background tasks.
    tasks = [
        loop.create_task(main_loop(state, executor, queue), name="main_loop"),
        loop.create_task(risk_monitor(state, executor), name="risk_monitor"),
        loop.create_task(ws_health_monitor(ws_manager_holder, queue, loop, state), name="ws_health"),
        loop.create_task(stats_logger(state), name="stats_logger"),
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Tasks cancelled — shutting down")
    finally:
        for t in tasks:
            t.cancel()
        ws_manager_holder[0].stop()
        logger.info("Final state: %s", state.summary())
        logger.info(
            "Session summary | orders=%d fills=%d cancelled=%d buys=%d sells=%d | realised_PnL=%.2f$",
            state.total_orders_placed,
            state.total_orders_filled,
            state.total_orders_cancelled,
            state.total_buys_filled,
            state.total_sells_filled,
            state.daily_pnl_usd,
        )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
