"""
Paper trader — continuous live shadow mode on Hyperliquid mainnet.

Position lifecycle
------------------
  Signal fires → post virtual ALO limit 1 tick inside spread.
  Fill check (live trade stream):
    BUY  fills when seller-initiated trade prints at ≤ our limit
    SELL fills when buyer-initiated trade prints at ≥ our limit
  Order expires after LIMIT_ORDER_TIMEOUT_MS if not filled.

  On fill: virtual position opens, SL and TP levels computed.
  Position closes when:
    - TP level hit (exit signal or TP price crossed by trade)
    - SL level hit (price crosses SL trigger)
    - Exit signal fires (OFI reversal via evaluate_exit_signal)

Fee model
---------
  ALO entry+exit: earn MAKER_REBATE (−0.01%) both legs → +0.02% per round-trip
  SL/TP closes use IOC: pay TAKER_FEE (0.035%) one leg

Telegram
--------
  Sends [PAPER] alerts for fills, closes, and hourly summaries.

Usage
-----
  python paper_trader.py                    # runs forever
  python paper_trader.py --duration 3600    # 1h session then exit
  python paper_trader.py --record out.jsonl # record WS stream
"""
from __future__ import annotations

import argparse
import asyncio
import json as _json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import config
from state import BotState, Level, OrderBook, BotStatus
from strategy import (
    compute_price_trend, compute_tfi, evaluate_signal,
    evaluate_exit_signal, ingest_trade, process_book_update,
)

PAPER_API_URL = "https://api.hyperliquid.xyz"
MAKER_REBATE  = 0.0001   # −0.01% per leg (Hyperliquid maker, earned)
TAKER_FEE     = 0.00035  # +0.035% per leg (Hyperliquid taker, paid)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("paper_trader")


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PendingOrder:
    direction:   str
    limit_price: float
    signal_ms:   int
    expire_ms:   int
    ofi:         float
    tfi:         Optional[float]


@dataclass
class VirtualPosition:
    direction:   str       # 'buy' (long) | 'sell' (short)
    entry_price: float
    size_btc:    float
    entry_ms:    int
    sl_price:    float
    tp_price:    float


@dataclass
class ClosedTrade:
    direction:   str
    entry_price: float
    exit_price:  float
    size_btc:    float
    pnl_usd:     float     # net including fees
    close_reason: str      # 'tp' | 'sl' | 'exit_signal' | 'expired'


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────

class PaperTrader:
    def __init__(self) -> None:
        self.state        = BotState()
        self.state.status = BotStatus.RUNNING

        self._pending:  List[PendingOrder]  = []
        self._position: Optional[VirtualPosition] = None
        self._closed:   List[ClosedTrade]   = []

        self._session_start_ms = int(time.time() * 1000)
        self._last_hourly_ms   = self._session_start_ms
        self._signals = 0
        self._fills   = 0
        self._expires = 0

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _virtual_pnl(self) -> float:
        """Running virtual P&L: closed trades + open position uPnL at mid."""
        closed = sum(t.pnl_usd for t in self._closed)
        if self._position and self.state.book:
            mid = self.state.book.mid_price() or self._position.entry_price
            dm  = 1 if self._position.direction == "buy" else -1
            open_upnl = dm * (mid - self._position.entry_price) * self._position.size_btc
        else:
            open_upnl = 0.0
        return closed + open_upnl

    def _close_position(self, exit_price: float, reason: str) -> ClosedTrade:
        pos = self._position
        assert pos is not None
        dm      = 1 if pos.direction == "buy" else -1
        raw_pnl = dm * (exit_price - pos.entry_price) * pos.size_btc
        notional = pos.size_btc * exit_price
        # Entry: ALO (earn rebate), Exit: IOC for SL/signal, ALO for TP
        if reason == "tp":
            fee = -2 * MAKER_REBATE * notional   # earn both legs
        else:
            fee = -(MAKER_REBATE * notional) + TAKER_FEE * notional  # earn entry, pay exit
        net_pnl = raw_pnl + fee
        ct = ClosedTrade(
            direction    = pos.direction,
            entry_price  = pos.entry_price,
            exit_price   = exit_price,
            size_btc     = pos.size_btc,
            pnl_usd      = net_pnl,
            close_reason = reason,
        )
        self._closed.append(ct)
        self._position = None

        direction_str = "LONG" if pos.direction == "buy" else "SHORT"
        total_pnl = self._virtual_pnl()
        logger.info(
            "[PAPER] CLOSE %s @ $%.2f | reason=%s | net=$%+.4f | totalVPnL=$%+.4f",
            direction_str, exit_price, reason, net_pnl, total_pnl,
        )
        return ct

    # ── Book update ──────────────────────────────────────────────────────────

    def on_book(self, book: OrderBook) -> None:
        now_ms = int(time.time() * 1000)
        mid    = book.mid_price()

        # 1. Expire stale pending orders.
        still_pending = []
        for order in self._pending:
            if now_ms >= order.expire_ms:
                self._expires += 1
                logger.debug("ALO expired: %s @ %.2f", order.direction, order.limit_price)
            else:
                still_pending.append(order)
        self._pending = still_pending

        # 2. Check SL/TP price triggers for open position.
        if self._position and mid is not None:
            pos = self._position
            if pos.direction == "buy":
                if mid <= pos.sl_price:
                    self._close_position(pos.sl_price, "sl")
                elif mid >= pos.tp_price:
                    self._close_position(pos.tp_price, "tp")
            else:
                if mid >= pos.sl_price:
                    self._close_position(pos.sl_price, "sl")
                elif mid <= pos.tp_price:
                    self._close_position(pos.tp_price, "tp")

        # 3. Compute OFI.
        ofi = process_book_update(self.state, book)
        if ofi is None:
            return

        # 4. Check exit signal for open position.
        if self._position:
            self.state.inventory_btc = (
                self._position.size_btc if self._position.direction == "buy"
                else -self._position.size_btc
            )
            exit_sig = evaluate_exit_signal(self.state, ofi)
            if exit_sig is not None and mid is not None:
                self._close_position(mid, "exit_signal")
                self.state.inventory_btc = 0.0
        else:
            self.state.inventory_btc = 0.0

        # 5. New entry signal (only when flat).
        if self._position or self._pending:
            return

        direction = evaluate_signal(self.state, ofi)
        if direction is None:
            return

        best_bid = book.best_bid()
        best_ask = book.best_ask()
        if best_bid is None or best_ask is None:
            return

        if direction == "buy":
            limit_price = best_bid.price + config.PRICE_TICK
            if limit_price >= best_ask.price:
                limit_price = best_ask.price - config.PRICE_TICK
        else:
            limit_price = best_ask.price - config.PRICE_TICK
            if limit_price <= best_bid.price:
                limit_price = best_bid.price + config.PRICE_TICK

        tfi   = compute_tfi(self.state)
        trend = compute_price_trend(self.state)

        order = PendingOrder(
            direction   = direction,
            limit_price = limit_price,
            signal_ms   = now_ms,
            expire_ms   = now_ms + config.LIMIT_ORDER_TIMEOUT_MS,
            ofi         = ofi,
            tfi         = tfi,
        )
        self._pending.append(order)
        self._signals += 1

        tfi_str   = f"{tfi:+.3f}"   if tfi   is not None else "N/A"
        trend_str = f"{trend:+.2f}" if trend is not None else "N/A"
        logger.info(
            "[PAPER] SIGNAL %s OFI=%+.4f TFI=%s trend=%s limit=%.2f",
            direction.upper(), ofi, tfi_str, trend_str, limit_price,
        )

        # 6. Hourly stats.
        if now_ms - self._last_hourly_ms >= 3600_000:
            self._send_hourly_stats(mid)
            self._last_hourly_ms = now_ms

    # ── Trade update ─────────────────────────────────────────────────────────

    def on_trade(self, trade: dict) -> None:
        ingest_trade(self.state, trade)

        if not self._pending:
            return

        try:
            side     = trade.get("side", "")
            px       = float(trade.get("px", 0))
            trade_ms = int(trade.get("time", time.time() * 1000))
        except (ValueError, TypeError):
            return

        still_pending = []
        now_ms = int(time.time() * 1000)

        for order in self._pending:
            filled = False
            if order.direction == "buy"  and side == "A" and px <= order.limit_price:
                filled = True
            elif order.direction == "sell" and side == "B" and px >= order.limit_price:
                filled = True

            if filled and self._position is None:
                actual_fill = (
                    min(px, order.limit_price) if order.direction == "buy"
                    else max(px, order.limit_price)
                )
                size_btc = config.ORDER_SIZE_BTC

                sl_pct = config.STOP_LOSS_PCT
                tp_pct = config.TAKE_PROFIT_PCT
                if order.direction == "buy":
                    sl_price = round(actual_fill * (1 - sl_pct), 1)
                    tp_price = round(actual_fill * (1 + tp_pct), 1)
                else:
                    sl_price = round(actual_fill * (1 + sl_pct), 1)
                    tp_price = round(actual_fill * (1 - tp_pct), 1)

                self._position = VirtualPosition(
                    direction   = order.direction,
                    entry_price = actual_fill,
                    size_btc    = size_btc,
                    entry_ms    = now_ms,
                    sl_price    = sl_price,
                    tp_price    = tp_price,
                )
                self._fills += 1

                direction_str = "LONG" if order.direction == "buy" else "SHORT"
                logger.info(
                    "[PAPER] FILL %s %.4f BTC @ $%.2f | SL=$%.2f TP=$%.2f | latency=%dms",
                    direction_str, size_btc, actual_fill, sl_price, tp_price,
                    now_ms - order.signal_ms,
                )
            else:
                still_pending.append(order)

        self._pending = still_pending

    # ── Hourly stats ─────────────────────────────────────────────────────────

    def _send_hourly_stats(self, mid: Optional[float]) -> None:
        from datetime import datetime, timezone
        ts      = datetime.now(timezone.utc).strftime("%H:%M UTC")
        vpnl    = self._virtual_pnl()
        n_wins  = sum(1 for t in self._closed if t.pnl_usd > 0)
        n_loss  = sum(1 for t in self._closed if t.pnl_usd <= 0)
        fill_rt = (self._fills / self._signals * 100) if self._signals else 0

        pos_str = "flat"
        if self._position:
            pm   = mid or self._position.entry_price
            dm   = 1 if self._position.direction == "buy" else -1
            upnl = dm * (pm - self._position.entry_price) * self._position.size_btc
            pos_str = (
                f"{'LONG' if dm>0 else 'SHORT'} {self._position.size_btc:.4f} BTC"
                f" @ ${self._position.entry_price:,.2f}  uPnL={'+' if upnl>=0 else ''}${upnl:.4f}"
            )

        lines = [
            f"[PAPER] Hourly report — {ts}",
            f"Session VPnL: {'+' if vpnl>=0 else ''}${vpnl:.4f}",
            f"Signals: {self._signals}  Fills: {self._fills} ({fill_rt:.0f}%)  Expires: {self._expires}",
            f"Closed: {len(self._closed)} trades  W/L: {n_wins}/{n_loss}",
            f"Position: {pos_str}",
        ]
        logger.info(" | ".join(lines))

    def final_report(self) -> None:
        mid = self.state.book.mid_price() if self.state.book else None
        self._send_hourly_stats(mid)

        sep = "═" * 60
        logger.info(sep)
        logger.info("  PAPER SESSION COMPLETE")
        logger.info(sep)
        for i, t in enumerate(self._closed, 1):
            dm  = 1 if t.direction == "buy" else -1
            wl  = "W" if t.pnl_usd > 0 else "L"
            logger.info(
                "  %3d. %-5s entry=%.2f exit=%.2f pnl=%+.4f %-3s reason=%s",
                i, t.direction.upper(), t.entry_price, t.exit_price, t.pnl_usd, wl, t.close_reason,
            )
        total = sum(t.pnl_usd for t in self._closed)
        logger.info("  Total closed VPnL: %+.4f", total)
        logger.info(sep)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket runner
# ─────────────────────────────────────────────────────────────────────────────

def run_paper_trader(duration_s: float = 0.0, record_file: Optional[str] = None) -> None:
    """Run the paper trader. duration_s=0 means run until killed (Ctrl-C)."""
    from hyperliquid.info import Info

    trader = PaperTrader()
    loop   = asyncio.new_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    rec_fh = open(record_file, "w") if record_file else None

    forever = duration_s <= 0

    logger.info("Paper trader starting | %s | duration=%s | recording=%s",
                PAPER_API_URL,
                "forever" if forever else f"{duration_s:.0f}s",
                record_file or "off")
    logger.info("Paper shadow mode — no real orders will be placed")

    info = Info(base_url=PAPER_API_URL, skip_ws=False)

    def _record(kind: str, raw_msg: dict) -> None:
        if rec_fh:
            rec_fh.write(_json.dumps({
                "type": kind,
                "wall_ms": int(time.time() * 1000),
                "data": raw_msg.get("data", raw_msg),
            }) + "\n")

    def on_book_msg(msg: dict) -> None:
        try:
            _record("book", msg)
            data   = msg["data"]
            levels = data["levels"]
            ts_ms  = int(data.get("time", time.time() * 1000))
            book   = OrderBook(
                bids=[Level.from_ws(l) for l in levels[0][: config.OFI_LEVELS + 3]],
                asks=[Level.from_ws(l) for l in levels[1][: config.OFI_LEVELS + 3]],
                timestamp_ms=ts_ms,
            )
            loop.call_soon_threadsafe(queue.put_nowait, ("book", book))
        except Exception as exc:
            logger.debug("book parse: %s", exc)

    def on_trades_msg(msg: dict) -> None:
        try:
            for t in msg.get("data", []):
                _record("trade", {"data": t})
                loop.call_soon_threadsafe(queue.put_nowait, ("trade", t))
        except Exception as exc:
            logger.debug("trades parse: %s", exc)

    info.subscribe({"type": "l2Book",  "coin": config.COIN}, on_book_msg)
    info.subscribe({"type": "trades",  "coin": config.COIN}, on_trades_msg)

    async def _run() -> None:
        deadline = time.monotonic() + duration_s if not forever else float("inf")
        while time.monotonic() < deadline:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            if kind == "book":
                trader.on_book(payload)
            elif kind == "trade":
                trader.on_trade(payload)

    try:
        loop.run_until_complete(_run())
    except KeyboardInterrupt:
        logger.info("Paper trader stopped by user")
    finally:
        info.disconnect_websocket()
        if rec_fh:
            rec_fh.close()
        trader.final_report()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hyperbot paper trader (shadow mode)")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="Session length in seconds. 0 = run forever (default)")
    parser.add_argument("--record", metavar="FILE",
                        help="Save raw WS stream to JSONL for replay backtest")
    args = parser.parse_args()
    run_paper_trader(duration_s=args.duration, record_file=args.record)
