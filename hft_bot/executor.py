"""
Low-latency order executor for Hyperliquid.

All SDK calls (HTTP) are blocking; we run them in a ThreadPoolExecutor so
the asyncio event loop stays free for the next book update / signal.

Execution model
---------------
- Spread <= WIDE_SPREAD_BPS: ALO (post-only, maker rebate -0.01%)
- Spread >  WIDE_SPREAD_BPS: IOC (immediate-or-cancel, taker fee 0.035%)

On mainnet BTC (spread 0.01-0.03 bps) we almost always use ALO.
On testnet    (spread 7-15 bps)        we almost always use IOC.

Order lifecycle
---------------
1. place_limit_order() -> SDK Exchange.order() -> returns oid
2. Cancel task armed:  sleep(timeout) then cancel_order(oid)
3. On fill event (userFills WS): cancel the cancel_task, record_fill()
4. On stop-loss / circuit-breaker: emergency_close()
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import config
from state import BotState, BotStatus, OpenOrder

logger = logging.getLogger("executor")

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hl_exec")


def _now_ms() -> int:
    return int(time.monotonic_ns() // 1_000_000)


def _round_price(px: float) -> float:
    tick = config.PRICE_TICK
    return round(round(px / tick) * tick, 1)


def _round_size(sz: float) -> float:
    return round(sz, config.SIZE_DECIMALS)


class OrderExecutor:
    def __init__(
        self,
        exchange,
        state: BotState,
        loop: asyncio.AbstractEventLoop,
        account_address: str = "",
    ) -> None:
        self.exchange = exchange
        self.state = state
        self.loop = loop
        self._account_address = account_address
        self._sl_lock = asyncio.Lock()  # serialise concurrent _manage_stop_loss calls
        self._sl_tp_pending = False  # debounce: absorb burst partial-fills into one re-arm

    async def _run_in_executor(self, fn, *args):
        return await self.loop.run_in_executor(_POOL, fn, *args)

    def _make_cloid(self) -> str:
        return str(uuid.uuid4())

    async def place_limit_order(
        self,
        is_buy: bool,
        price: float,
        size: float,
        spread: float = 0.0,
        reduce_only: bool = False,
        force_ioc: bool = False,
    ) -> Optional[int]:
        """
        Place a limit order with spread-adaptive TIF:
          - Spread <= WIDE_SPREAD_BPS -> ALO (post-only, maker rebate)
          - Spread >  WIDE_SPREAD_BPS -> IOC (immediate-or-cancel, guaranteed fill)
          - force_ioc=True always uses IOC (for exit orders where fill is required)
        """
        mid = self.state.mid_price() or price
        spread_bps = (spread / mid * 10_000) if mid > 0 else 0

        use_ioc = force_ioc or spread_bps > config.WIDE_SPREAD_BPS
        tif = "Ioc" if use_ioc else "Alo"
        direction = "BUY " if is_buy else "SELL"

        if config.OBSERVER_MODE:
            logger.info(
                "[OBSERVER] Would place %s %s %.4f BTC @ %.2f (spread=%.1fbps tif=%s)",
                direction, "IOC" if use_ioc else "ALO", size, price, spread_bps, tif,
            )
            return None

        px = _round_price(price)
        sz = _round_size(size)
        cloid = self._make_cloid()
        order_type = {"limit": {"tif": tif}}

        logger.info(
            "Placing %s %s %.4f BTC @ %.2f (spread=%.1fbps cloid=%s)",
            direction, tif, sz, px, spread_bps, cloid[:8],
        )

        def _place():
            return self.exchange.order(
                config.COIN,
                is_buy,
                sz,
                px,
                order_type=order_type,
                reduce_only=reduce_only,
            )

        try:
            result = await self._run_in_executor(_place)
        except Exception as exc:
            logger.error("Order placement failed: %s", exc, exc_info=True)
            return None

        oid = self._extract_oid(result, cloid)
        if oid is None:
            logger.warning("Could not extract oid from response: %s", result)
            return None

        logger.info("Order placed  oid=%d dir=%s px=%.2f sz=%.4f", oid, direction, px, sz)

        open_order = OpenOrder(
            oid=oid,
            cloid=cloid,
            is_buy=is_buy,
            price=px,
            size=sz,
            placed_at_ms=_now_ms(),
        )
        self.state.open_orders[oid] = open_order
        self.state.total_orders_placed += 1

        cancel_task = self.loop.create_task(
            self._auto_cancel(oid, config.LIMIT_ORDER_TIMEOUT_MS)
        )
        open_order.cancel_task = cancel_task

        return oid

    async def cancel_order(self, oid: int) -> bool:
        if config.OBSERVER_MODE:
            self.state.open_orders.pop(oid, None)
            return True

        def _cancel():
            return self.exchange.cancel(config.COIN, oid)

        logger.info("Cancelling order oid=%d", oid)
        try:
            result = await self._run_in_executor(_cancel)
            logger.debug("Cancel result: %s", result)
        except Exception as exc:
            logger.error("Cancel failed oid=%d: %s", oid, exc, exc_info=True)
            return False

        order = self.state.open_orders.pop(oid, None)
        if order:
            self.state.total_orders_cancelled += 1
        return True

    async def cancel_all_orders(self) -> None:
        oids = list(self.state.open_orders.keys())
        if not oids:
            return
        logger.warning("Cancelling ALL open orders (%d)", len(oids))
        await asyncio.gather(*[self.cancel_order(oid) for oid in oids], return_exceptions=True)

    async def emergency_close(self, reason: str) -> None:
        pos = self.state.inventory_btc
        if abs(pos) < 1e-6:
            return

        logger.warning("EMERGENCY CLOSE | reason=%s | pos=%.4f BTC", reason, pos)
        await self.cancel_all_orders()
        await self._cancel_all_reduce_only()
        self.state.sl_oid = None
        self.state.tp_oid = None

        if config.OBSERVER_MODE:
            self.state.inventory_btc = 0.0
            self.state.entry_price = None
            return

        def _market_close():
            return self.exchange.market_close(config.COIN, slippage=0.01)

        try:
            result = await self._run_in_executor(_market_close)
            logger.warning("Emergency close result: %s", result)
        except Exception as exc:
            logger.critical("Emergency close FAILED: %s", exc, exc_info=True)

    async def set_leverage(self) -> None:
        if config.OBSERVER_MODE:
            return

        def _set():
            return self.exchange.update_leverage(config.LEVERAGE, config.COIN, is_cross=True)

        try:
            result = await self._run_in_executor(_set)
            logger.info("Leverage set to %dx: %s", config.LEVERAGE, result)
        except Exception as exc:
            logger.warning("Could not set leverage: %s", exc)

    def handle_fill(self, fill: dict) -> None:
        try:
            oid        = int(fill.get("oid", 0))
            fill_px    = float(fill["px"])
            fill_sz    = float(fill["sz"])
            side       = fill.get("side", "")
            is_buy     = side == "B"
            closed_pnl = float(fill.get("closedPnl", 0.0))

            logger.info(
                "FILL | oid=%d side=%s px=%.2f sz=%.4f closedPnl=%.4f$",
                oid, "BUY" if is_buy else "SELL", fill_px, fill_sz, closed_pnl,
            )

            self.state.record_fill(is_buy, fill_px, fill_sz, closed_pnl)

            order = self.state.open_orders.pop(oid, None)
            if order and order.cancel_task and not order.cancel_task.done():
                order.cancel_task.cancel()

            # Refresh SL + TP once after partial-fill burst settles (debounced)
            if not self._sl_tp_pending:
                self._sl_tp_pending = True
                self.loop.create_task(self._manage_sl_tp_debounced())

        except Exception as exc:
            logger.error("handle_fill error: %s | raw=%s", exc, fill, exc_info=True)

    async def _cancel_all_reduce_only(self) -> None:
        """Fetch ALL open reduce-only orders for the coin and cancel them.
        More reliable than tracking sl_oid alone — prevents accumulation when
        the tracked oid is stale (missed cancel, crash, exchange_sync re-arm, etc.)."""
        if not self._account_address:
            # Fallback to tracked oid only
            if self.state.sl_oid is not None:
                await self._cancel_sl(self.state.sl_oid)
                self.state.sl_oid = None
            return

        import json as _json, urllib.request as _ur

        def _fetch():
            payload = _json.dumps({"type": "openOrders", "user": self._account_address}).encode()
            req = _ur.Request(
                config.API_URL.rstrip("/") + "/info",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=5) as resp:
                return _json.loads(resp.read())

        try:
            orders = await self._run_in_executor(_fetch)
            stale = [o for o in orders if o.get("coin") == config.COIN and o.get("reduceOnly")]
            if stale:
                logger.info("Cancelling %d reduce-only order(s) before re-arming SL", len(stale))
                for o in stale:
                    await self._cancel_sl(int(o["oid"]))
        except Exception as exc:
            logger.warning("_cancel_all_reduce_only failed: %s — falling back to tracked oid", exc)
            if self.state.sl_oid is not None:
                await self._cancel_sl(self.state.sl_oid)
        self.state.sl_oid = None

    async def _manage_stop_loss(self) -> None:
        """Cancel ALL exchange SL orders and place one fresh one matching current position.
        Lock serialises concurrent calls from burst fills — without it, 10 fill events
        arriving simultaneously each spawn a task, all race to place a new SL."""
        if config.OBSERVER_MODE:
            return
        async with self._sl_lock:
            await self._cancel_all_reduce_only()
            inv = self.state.inventory_btc
            entry = self.state.entry_price
            if abs(inv) < 1e-8 or entry is None:
                return
            sl_oid = await self.place_stop_loss(abs(inv), entry, is_long=inv > 0)
            self.state.sl_oid = sl_oid

    async def place_stop_loss(self, size: float, entry_price: float, is_long: bool) -> Optional[int]:
        """Place a reduce-only stop-market order on the exchange."""
        if config.OBSERVER_MODE:
            return None

        sl_pct = config.STOP_LOSS_PCT
        trigger_px = _round_price(
            entry_price * (1 - sl_pct) if is_long else entry_price * (1 + sl_pct)
        )
        is_buy_sl = not is_long
        sz = _round_size(size)

        logger.info(
            "Placing exchange SL | %s %.4f BTC @ trigger $%.2f (%.2f%% from entry $%.2f)",
            "SELL" if not is_buy_sl else "BUY", sz, trigger_px, sl_pct * 100, entry_price,
        )

        def _place():
            return self.exchange.order(
                config.COIN,
                is_buy_sl,
                sz,
                trigger_px,
                order_type={"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True,
            )

        try:
            result = await self._run_in_executor(_place)
            oid = self._extract_oid(result, "sl")
            if oid:
                logger.info("Exchange SL placed oid=%d trigger=%.2f", oid, trigger_px)
            else:
                logger.warning("SL placement: no oid in response | %s", result)
            return oid
        except Exception as exc:
            logger.error("SL placement failed: %s", exc, exc_info=True)
            return None

    async def place_take_profit(self, size: float, entry_price: float, is_long: bool) -> Optional[int]:
        """Place a reduce-only ALO (post-only, earns maker rebate) take-profit limit order."""
        if config.OBSERVER_MODE:
            return None

        tp_pct = config.TAKE_PROFIT_PCT
        tp_px = _round_price(
            entry_price * (1 + tp_pct) if is_long else entry_price * (1 - tp_pct)
        )
        is_buy_tp = not is_long
        sz = _round_size(size)

        logger.info(
            "Placing TP | %s %.4f BTC @ $%.2f (%.2f%% from entry $%.2f)",
            "SELL" if not is_buy_tp else "BUY", sz, tp_px, tp_pct * 100, entry_price,
        )

        def _place():
            return self.exchange.order(
                config.COIN,
                is_buy_tp,
                sz,
                tp_px,
                order_type={"limit": {"tif": "Alo"}},
                reduce_only=True,
            )

        try:
            result = await self._run_in_executor(_place)
            oid = self._extract_oid(result, "tp")
            if oid:
                logger.info("TP placed oid=%d @ $%.2f", oid, tp_px)
            else:
                logger.warning("TP placement: no oid | %s", result)
            return oid
        except Exception as exc:
            logger.error("TP placement failed: %s", exc, exc_info=True)
            return None

    async def _manage_sl_tp_debounced(self) -> None:
        """Wait 250ms for partial-fill burst to settle, then re-arm SL+TP exactly once."""
        await asyncio.sleep(0.25)
        self._sl_tp_pending = False
        await self._manage_sl_tp()

    async def _manage_sl_tp(self) -> None:
        """Cancel ALL reduce-only orders then atomically re-arm SL + TP for current position.
        Single lock ensures burst fills don't race to place duplicate SL/TP pairs."""
        if config.OBSERVER_MODE:
            return
        async with self._sl_lock:
            await self._cancel_all_reduce_only()
            self.state.sl_oid = None
            self.state.tp_oid = None

            inv = self.state.inventory_btc
            entry = self.state.entry_price
            if abs(inv) < 1e-8 or entry is None:
                return

            is_long = inv > 0
            sz = abs(inv)
            self.state.sl_oid = await self.place_stop_loss(sz, entry, is_long=is_long)
            self.state.tp_oid = await self.place_take_profit(sz, entry, is_long=is_long)

    async def _cancel_sl(self, sl_oid: int) -> None:
        def _cancel():
            return self.exchange.cancel(config.COIN, sl_oid)
        try:
            await self._run_in_executor(_cancel)
            logger.info("Exchange SL cancelled oid=%d", sl_oid)
        except Exception as exc:
            logger.warning("Cancel SL failed oid=%d: %s", sl_oid, exc)

    async def _auto_cancel(self, oid: int, timeout_ms: int) -> None:
        await asyncio.sleep(timeout_ms / 1000.0)
        if oid in self.state.open_orders:
            logger.info("Order timeout oid=%d (> %dms) — cancelling", oid, timeout_ms)
            await self.cancel_order(oid)

    @staticmethod
    def _extract_oid(result: dict, cloid: str) -> Optional[int]:
        try:
            statuses = result["response"]["data"]["statuses"]
            for s in statuses:
                if "resting" in s:
                    return int(s["resting"]["oid"])
                if "filled" in s:
                    return int(s["filled"]["oid"])
                if "error" in s:
                    logger.warning("Order rejected: %s", s["error"])
                    return None
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("_extract_oid parse error: %s | result=%s", exc, result)
        return None
