"""
Low-latency order executor for Hyperliquid.

All SDK calls (HTTP) are blocking; we run them in a ThreadPoolExecutor so
the asyncio event loop stays free for the next book update / signal.

Order lifecycle
---------------
1. place_limit_order()  →  SDK Exchange.order()  →  returns oid
2. A cancel_task is armed: sleep(timeout) then call cancel_order(oid)
3. On fill event (from userFills WS):  cancel the cancel_task, record_fill()
4. On stop-loss / circuit-breaker:  emergency_close()
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

# Shared thread pool for blocking Exchange HTTP calls.
# Two workers: one for order placement, one for cancellations.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hl_exec")


def _now_ms() -> int:
    return int(time.monotonic_ns() // 1_000_000)


def _round_price(px: float) -> float:
    """Round price to Hyperliquid BTC tick size."""
    tick = config.PRICE_TICK
    return round(round(px / tick) * tick, 1)


def _round_size(sz: float) -> float:
    """Round size to BTC lot precision."""
    return round(sz, config.SIZE_DECIMALS)


# ---------------------------------------------------------------------------
# OrderExecutor
# ---------------------------------------------------------------------------

class OrderExecutor:
    """
    Wraps the Hyperliquid Exchange for async order management.

    exchange: an initialised hyperliquid.exchange.Exchange instance (or None
              in observer mode).
    state:    the shared BotState.
    loop:     the running asyncio event loop (passed so cancel tasks can be
              scheduled from any thread).
    """

    def __init__(self, exchange, state: BotState, loop: asyncio.AbstractEventLoop) -> None:
        self.exchange = exchange
        self.state = state
        self.loop = loop

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_in_executor(self, fn, *args):
        """Run a blocking SDK call in the shared thread pool."""
        return await self.loop.run_in_executor(_POOL, fn, *args)

    def _make_cloid(self) -> str:
        return str(uuid.uuid4())

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_limit_order(
        self,
        is_buy: bool,
        price: float,
        size: float,
    ) -> Optional[int]:
        """
        Place a post-only (ALO) limit order.

        Returns the oid on success, or None on failure.
        An async cancel task is armed to fire after LIMIT_ORDER_TIMEOUT_MS.
        """
        if config.OBSERVER_MODE:
            direction = "BUY " if is_buy else "SELL"
            logger.info("[OBSERVER] Would place %s limit %.4f BTC @ %.2f", direction, size, price)
            return None

        px = _round_price(price)
        sz = _round_size(size)
        cloid = self._make_cloid()

        # ALO = "Add Liquidity Only" (post-only)
        tif = "Alo" if config.POST_ONLY else "Gtc"
        order_type = {"limit": {"tif": tif}}

        direction = "BUY " if is_buy else "SELL"
        logger.info("Placing %s limit %.4f BTC @ %.2f (cloid=%s tif=%s)", direction, sz, px, cloid[:8], tif)

        def _place():
            return self.exchange.order(
                config.COIN,
                is_buy,
                sz,
                px,
                order_type=order_type,
                reduce_only=False,
                cloid=cloid,
            )

        try:
            result = await self._run_in_executor(_place)
        except Exception as exc:
            logger.error("Order placement failed: %s", exc, exc_info=True)
            return None

        # Parse oid from response
        oid = self._extract_oid(result, cloid)
        if oid is None:
            logger.warning("Could not extract oid from response: %s", result)
            return None

        logger.info("Order placed  oid=%d dir=%s px=%.2f sz=%.4f", oid, direction, px, sz)

        # Record in state
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

        # Arm auto-cancel task
        cancel_task = self.loop.create_task(
            self._auto_cancel(oid, config.LIMIT_ORDER_TIMEOUT_MS)
        )
        open_order.cancel_task = cancel_task

        return oid

    # ------------------------------------------------------------------
    # Order cancellation
    # ------------------------------------------------------------------

    async def cancel_order(self, oid: int) -> bool:
        """Cancel a single resting order by oid.  Returns True on success."""
        if config.OBSERVER_MODE:
            logger.info("[OBSERVER] Would cancel oid=%d", oid)
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
        """Cancel every tracked open order. Used for circuit-breaker / shutdown."""
        oids = list(self.state.open_orders.keys())
        if not oids:
            return
        logger.warning("Cancelling ALL open orders (%d)", len(oids))
        tasks = [self.cancel_order(oid) for oid in oids]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Emergency market close
    # ------------------------------------------------------------------

    async def emergency_close(self, reason: str) -> None:
        """
        Immediate market close of the full position via IoC limit at slippage
        price.  Used for stop-loss and circuit-breaker events.
        """
        pos = self.state.inventory_btc
        if abs(pos) < 1e-6:
            logger.info("emergency_close called but no position (reason: %s)", reason)
            return

        logger.warning(
            "EMERGENCY CLOSE | reason=%s | pos=%.4f BTC", reason, pos
        )

        # Cancel all open orders first.
        await self.cancel_all_orders()

        if config.OBSERVER_MODE:
            logger.info("[OBSERVER] Would market-close %.4f BTC", pos)
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

    # ------------------------------------------------------------------
    # Set leverage
    # ------------------------------------------------------------------

    async def set_leverage(self) -> None:
        if config.OBSERVER_MODE:
            return

        def _set():
            return self.exchange.update_leverage(
                config.LEVERAGE, config.COIN, is_cross=True
            )

        try:
            result = await self._run_in_executor(_set)
            logger.info("Leverage set to %dx: %s", config.LEVERAGE, result)
        except Exception as exc:
            logger.warning("Could not set leverage: %s", exc)

    # ------------------------------------------------------------------
    # On fill event (called by main loop from WS userFills callback)
    # ------------------------------------------------------------------

    def handle_fill(self, fill: dict) -> None:
        """
        Process a confirmed fill from the userFills WebSocket channel.
        Updates state and cancels the auto-cancel task if the order is fully filled.
        """
        try:
            oid = int(fill.get("oid", 0))
            fill_px = float(fill["px"])
            fill_sz = float(fill["sz"])
            side = fill.get("side", "")
            is_buy = side == "B"
            closed_pnl = float(fill.get("closedPnl", 0.0))

            logger.info(
                "FILL | oid=%d side=%s px=%.2f sz=%.4f closedPnl=%.4f$",
                oid, "BUY" if is_buy else "SELL", fill_px, fill_sz, closed_pnl,
            )

            self.state.record_fill(is_buy, fill_px, fill_sz, closed_pnl)

            # Cancel the pending auto-cancel task for this order.
            order = self.state.open_orders.pop(oid, None)
            if order and order.cancel_task and not order.cancel_task.done():
                order.cancel_task.cancel()

        except Exception as exc:
            logger.error("handle_fill error: %s | raw=%s", exc, fill, exc_info=True)

    # ------------------------------------------------------------------
    # Internal: auto-cancel after timeout
    # ------------------------------------------------------------------

    async def _auto_cancel(self, oid: int, timeout_ms: int) -> None:
        """Waits timeout_ms then fires a cancel request if the order still lives."""
        await asyncio.sleep(timeout_ms / 1000.0)
        if oid in self.state.open_orders:
            logger.info("Order timeout oid=%d (> %dms) — cancelling", oid, timeout_ms)
            await self.cancel_order(oid)

    # ------------------------------------------------------------------
    # Internal: parse oid from SDK response
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_oid(result: dict, cloid: str) -> Optional[int]:
        """
        The SDK returns a dict like:
          {"status": "ok", "response": {"type": "order", "data": {"statuses": [...]}}}

        Each status entry is either:
          {"resting": {"oid": <int>}}
          or {"filled": {"oid": <int>, ...}}
          or {"error": "..."}
        """
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
