"""
Paper trader — runs the OFI+TFI strategy against LIVE mainnet data without
placing any real orders.

Connects to the Hyperliquid mainnet WebSocket, subscribes to l2Book and
trades, then runs the exact same signal engine as the live bot.

Fill model (IOC, pessimistic):
  BUY  signal → "filled" at best ask (taker crosses the spread)
  SELL signal → "filled" at best bid

Forward return tracking:
  At T+100ms, T+250ms, T+500ms, T+1000ms relative to each signal, we record
  where the mid-price moved.  Directional accuracy = % of signals where the
  mid moved in the signal direction.  Expected PnL per trade is the T+500ms
  forward return minus the half-spread (bid-ask cost).

Usage:
  cd hft_bot
  python paper_trader.py              # runs for 120 seconds
  python paper_trader.py --duration 300
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Bootstrap path so we can import from hft_bot siblings without a package.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import config
from state import BotState, Level, OrderBook
from strategy import compute_tfi, evaluate_signal, ingest_trade, process_book_update

# Force mainnet for paper trading — we want real liquidity data.
PAPER_API_URL = "https://api.hyperliquid.xyz"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("paper_trader")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    ts_ms: int
    direction: str      # 'buy' or 'sell'
    ofi: float
    tfi: Optional[float]
    fill_price: float   # IOC fill: ask for buy, bid for sell
    mid_at_signal: float
    spread_at_signal: float
    # forward mid snapshots, keyed by horizon_ms
    forward: Dict[int, Optional[float]] = field(default_factory=dict)

HORIZONS_MS = [100, 250, 500, 1000]


@dataclass
class PaperBook:
    """Lightweight snapshot passed alongside mid price for forward tracking."""
    mid: float
    ts_ms: int


# ---------------------------------------------------------------------------
# Paper trading engine
# ---------------------------------------------------------------------------

class PaperTrader:
    def __init__(self, duration_s: float = 120.0) -> None:
        self.duration_s = duration_s
        self.state      = BotState()
        self.state.status = __import__("state").BotStatus.RUNNING

        # Pending signals waiting for forward-return snapshots.
        # list of (signal, deadline_ms_per_horizon)
        self._pending: List[Tuple[Signal, List[Tuple[int, int]]]] = []

        # Rolling mid-price history for forward return measurement.
        # (ts_ms, mid)
        self._mid_history: Deque[Tuple[int, float]] = deque(maxlen=5000)

        self.signals: List[Signal] = []

    def on_book(self, book: OrderBook) -> None:
        # Record mid for forward-return lookups.
        mid = book.mid_price()
        if mid:
            self._mid_history.append((book.timestamp_ms, mid))

        # Resolve pending forward-return snapshots.
        now_ms = int(time.time() * 1000)
        for sig, pending_horizons in self._pending:
            still_pending = []
            for horizon_ms, deadline_ms in pending_horizons:
                if now_ms >= deadline_ms:
                    sig.forward[horizon_ms] = mid  # best mid we have at deadline
                else:
                    still_pending.append((horizon_ms, deadline_ms))
            pending_horizons[:] = still_pending

        # Remove fully resolved signals.
        self._pending = [(s, ph) for s, ph in self._pending if ph]

        # Compute OFI.
        ofi = process_book_update(self.state, book)
        if ofi is None:
            return

        direction = evaluate_signal(self.state, ofi)
        if direction is None:
            return

        # Record signal with IOC fill model.
        best_bid = book.best_bid()
        best_ask = book.best_ask()
        if direction == "buy":
            if best_ask is None:
                return
            fill_px = best_ask.price
        else:
            if best_bid is None:
                return
            fill_px = best_bid.price

        spread_val = book.spread() or 0.0
        tfi = compute_tfi(self.state)

        sig = Signal(
            ts_ms=book.timestamp_ms,
            direction=direction,
            ofi=ofi,
            tfi=tfi,
            fill_price=fill_px,
            mid_at_signal=mid or fill_px,
            spread_at_signal=spread_val,
        )
        self.signals.append(sig)

        now_ms = int(time.time() * 1000)
        horizons = [(h, now_ms + h) for h in HORIZONS_MS]
        self._pending.append((sig, horizons))

        logger.info(
            "%s | OFI=%+.4f TFI=%s fill=%.2f spread=%.2f$ mid=%.2f",
            direction.upper(),
            ofi,
            f"{tfi:+.3f}" if tfi is not None else "N/A",
            fill_px,
            spread_val,
            mid or 0,
        )

    def on_trade(self, trade: dict) -> None:
        ingest_trade(self.state, trade)

    def report(self) -> None:
        n = len(self.signals)
        logger.info("=" * 60)
        logger.info("PAPER TRADING REPORT  (%d signals)", n)
        logger.info("=" * 60)

        if n == 0:
            logger.info("No signals generated — try lower thresholds or longer duration.")
            return

        buys  = [s for s in self.signals if s.direction == "buy"]
        sells = [s for s in self.signals if s.direction == "sell"]
        logger.info("Buys: %d  Sells: %d", len(buys), len(sells))

        # Per-horizon analysis.
        for h in HORIZONS_MS:
            resolved = [s for s in self.signals if s.forward.get(h) is not None]
            if not resolved:
                continue

            correct = 0
            pnl_list = []
            for s in resolved:
                fwd_mid = s.forward[h]
                if fwd_mid is None:
                    continue
                ret = fwd_mid - s.mid_at_signal
                half_spread = s.spread_at_signal / 2
                if s.direction == "buy":
                    pnl = ret - half_spread
                    correct += 1 if ret > 0 else 0
                else:
                    pnl = -ret - half_spread
                    correct += 1 if ret < 0 else 0
                # Scale to USD: 0.001 BTC position
                pnl_usd = pnl * config.ORDER_SIZE_BTC
                pnl_list.append(pnl_usd)

            if not pnl_list:
                continue

            accuracy = correct / len(resolved) * 100
            avg_pnl  = sum(pnl_list) / len(pnl_list)
            total_pnl = sum(pnl_list)
            logger.info(
                "T+%4dms | n=%d | accuracy=%.1f%% | avg_pnl=$%.4f | total_pnl=$%.4f",
                h, len(pnl_list), accuracy, avg_pnl, total_pnl,
            )

        # Spread stats.
        avg_spread = sum(s.spread_at_signal for s in self.signals) / n
        logger.info("Avg spread at signal: $%.2f", avg_spread)

        # TFI coverage.
        tfi_present = sum(1 for s in self.signals if s.tfi is not None)
        logger.info("TFI data present: %d/%d signals (%.0f%%)", tfi_present, n, tfi_present / n * 100)

        logger.info("=" * 60)


# ---------------------------------------------------------------------------
# WebSocket setup using Hyperliquid SDK
# ---------------------------------------------------------------------------

def run_paper_trader(duration_s: float = 120.0) -> None:
    from hyperliquid.info import Info

    trader = PaperTrader(duration_s=duration_s)
    loop   = asyncio.new_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    logger.info("Connecting to mainnet: %s", PAPER_API_URL)
    info = Info(base_url=PAPER_API_URL, skip_ws=False)

    # ---- l2Book callback ----
    def on_book_msg(msg: dict) -> None:
        try:
            data   = msg["data"]
            levels = data["levels"]
            ts_ms  = int(data.get("time", time.time() * 1000))
            book = OrderBook(
                bids=[Level.from_ws(l) for l in levels[0][: config.OFI_LEVELS + 3]],
                asks=[Level.from_ws(l) for l in levels[1][: config.OFI_LEVELS + 3]],
                timestamp_ms=ts_ms,
            )
            loop.call_soon_threadsafe(queue.put_nowait, ("book", book))
        except Exception as exc:
            logger.debug("book parse error: %s", exc)

    # ---- trades callback ----
    def on_trades_msg(msg: dict) -> None:
        try:
            trades_list = msg.get("data", [])
            if isinstance(trades_list, list):
                for t in trades_list:
                    loop.call_soon_threadsafe(queue.put_nowait, ("trade", t))
        except Exception as exc:
            logger.debug("trades parse error: %s", exc)

    info.subscribe({"type": "l2Book",  "coin": config.COIN}, on_book_msg)
    info.subscribe({"type": "trades",  "coin": config.COIN}, on_trades_msg)

    logger.info("Subscribed — running for %.0fs on %s", duration_s, config.COIN)

    async def _run() -> None:
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue

            if kind == "book":
                trader.on_book(payload)
            elif kind == "trade":
                trader.on_trade(payload)

    loop.run_until_complete(_run())
    info.disconnect_websocket()
    trader.report()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OFI paper trader on Hyperliquid mainnet")
    parser.add_argument("--duration", type=float, default=120.0,
                        help="How many seconds to run (default: 120)")
    args = parser.parse_args()

    run_paper_trader(duration_s=args.duration)
