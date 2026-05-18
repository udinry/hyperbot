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
from strategy import evaluate_signal, ingest_trade, process_book_update

_exchange = None
if not config.OBSERVER_MODE:
    from eth_account import Account
    from hyperliquid.exchange import Exchange

    _wallet = Account.from_key(config.PRIVATE_KEY)
    logger.info("Wallet address: %s", _wallet.address)
    _exchange = Exchange(wallet=_wallet, base_url=config.API_URL)
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
            fills = msg["data"].get("fills", [])
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
        balance = float(data["marginSummary"]["accountValue"])
        mid = state.mid_price()
        if not mid or mid <= 0:
            return
        # margin = balance × risk_pct; notional = margin × leverage; btc = notional / mid
        raw = (balance * config.POSITION_RISK_PCT * config.LEVERAGE) / mid
        # Clamp: minimum 0.001 BTC (exchange min), maximum = inventory limit
        new_size = round(max(0.001, min(config.MAX_INVENTORY_BTC, raw)), 3)
        if new_size != state.order_size_btc:
            logger.info(
                "Position resize | balance=$%.2f mid=%.2f → %.4f BTC (was %.4f)",
                balance, mid, new_size, state.order_size_btc,
            )
            state.order_size_btc = new_size
    except Exception as exc:
        logger.warning("refresh_order_size failed: %s — keeping %.4f BTC", exc, state.order_size_btc)


async def position_sizer(state: BotState, info, wallet_address: str) -> None:
    """Background task: re-check account balance every 5 minutes and resize."""
    while state.status not in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
        await asyncio.sleep(300)
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

        if state.daily_pnl_usd <= -config.MAX_DAILY_LOSS_USD:
            logger.critical(
                "CIRCUIT BREAKER | daily_pnl=%.2f$ <= -%.2f$",
                state.daily_pnl_usd, config.MAX_DAILY_LOSS_USD,
            )
            state.set_circuit_breaker()
            await executor.cancel_all_orders()
            await executor.emergency_close("circuit_breaker")
            break

        if state.status == BotStatus.RUNNING:
            if (state.inventory_btc >= config.MAX_INVENTORY_BTC or
                    state.inventory_btc <= -config.MAX_INVENTORY_BTC):
                state.set_paused_inventory()
                logger.warning("INVENTORY LIMIT | inv=%.4f BTC | pausing", state.inventory_btc)

        elif state.status == BotStatus.PAUSED_INVENTORY:
            if abs(state.inventory_btc) <= config.MAX_INVENTORY_BTC * 0.8:
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
    while state.status not in (BotStatus.STOPPED, BotStatus.CIRCUIT_BREAKER):
        await asyncio.sleep(10)
        logger.info("STATE | %s", state.summary())


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
    if ofi is None or not state.is_running():
        return

    signal = evaluate_signal(state, ofi)
    if signal is None:
        return

    spread     = book.spread() or 0.0
    mid        = book.mid_price() or 0.0
    spread_bps = (spread / mid * 10_000) if mid > 0 else 9999
    use_ioc    = spread_bps > config.WIDE_SPREAD_BPS

    if signal == "buy":
        best_bid = book.best_bid()
        best_ask = book.best_ask()
        if best_ask is None:
            return
        if use_ioc:
            price = best_ask.price
        else:
            # ALO: post 1 tick above best bid; clamp below ask to avoid crossing.
            price = (best_bid.price + config.PRICE_TICK) if best_bid else (best_ask.price - config.PRICE_TICK)
            if price >= best_ask.price:
                price = best_ask.price - config.PRICE_TICK
        await executor.place_limit_order(is_buy=True, price=price, size=state.order_size_btc, spread=spread)

    elif signal == "sell":
        best_bid = book.best_bid()
        best_ask = book.best_ask()
        if best_bid is None:
            return
        if use_ioc:
            price = best_bid.price
        else:
            # ALO: post 1 tick below best ask; clamp above bid to avoid crossing.
            price = (best_ask.price - config.PRICE_TICK) if best_ask else (best_bid.price + config.PRICE_TICK)
            if price <= best_bid.price:
                price = best_bid.price + config.PRICE_TICK
        await executor.place_limit_order(is_buy=False, price=price, size=state.order_size_btc, spread=spread)


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
        if state.inventory_btc != 0.0:
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
    executor = OrderExecutor(exchange=_exchange, state=state, loop=loop)

    _install_signal_handlers(state, executor, loop)

    await executor.set_leverage()

    # Dynamic position sizing: fetch balance once before first trade.
    # Refreshed every 5 min by position_sizer task. Skipped in observer mode.
    _info_rest = Info(base_url=config.API_URL, skip_ws=True)
    if not config.OBSERVER_MODE and _wallet:
        await _refresh_order_size(state, _info_rest, _wallet.address)
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

    state.set_running()
    logger.info("Bot is LIVE | %s", state.summary())

    sizer_task = (
        loop.create_task(position_sizer(state, _info_rest, _wallet.address), name="position_sizer")
        if not config.OBSERVER_MODE and _wallet else None
    )

    tasks = [
        loop.create_task(main_loop(state, executor, queue),          name="main_loop"),
        loop.create_task(risk_monitor(state, executor),              name="risk_monitor"),
        loop.create_task(ws_health_monitor(ws_manager_holder, queue, loop, state), name="ws_health"),
        loop.create_task(stats_logger(state),                        name="stats_logger"),
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
        logger.info(
            "Session summary | orders=%d fills=%d cancelled=%d buys=%d sells=%d | realised_PnL=%.2f$",
            state.total_orders_placed, state.total_orders_filled, state.total_orders_cancelled,
            state.total_buys_filled, state.total_sells_filled, state.daily_pnl_usd,
        )


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
