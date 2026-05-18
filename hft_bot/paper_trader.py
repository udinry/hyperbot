"""
Paper trader — realistic ALO simulation on live Hyperliquid mainnet data.

Fill model
----------
  Signal fires → post ALO limit 1 tick inside the spread:
    BUY  → bid + PRICE_TICK   (e.g. 76999.60 when bid=76999.50, ask=77000.50)
    SELL → ask - PRICE_TICK   (e.g. 77000.40)

  Fill tracking (via live trade stream):
    BUY  limit fills when a seller-initiated trade (side='A') prints at ≤ our price
    SELL limit fills when a buyer-initiated trade (side='B') prints at ≥ our price

  Order expires after LIMIT_ORDER_TIMEOUT_MS if no fill event received.

PnL model
----------
  Closed at T+1000ms after fill via ALO (earn maker rebate both legs).
  Fees: MAKER_REBATE = -0.01% per leg (we receive this).
  Spread contribution: buying below mid / selling above mid earns half the spread.

Usage
-----
  cd hft_bot
  python paper_trader.py              # 120s default
  python paper_trader.py --duration 300
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import statistics
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import config
from state import BotState, Level, OrderBook
from strategy import compute_price_trend, compute_tfi, evaluate_signal, ingest_trade, process_book_update

PAPER_API_URL   = "https://api.hyperliquid.xyz"
MAKER_REBATE    = 0.0001   # -0.01% per leg (Hyperliquid maker)
TAKER_FEE       = 0.00035  # +0.035% per leg (for comparison)
HORIZONS_MS     = [250, 500, 1000, 2000]

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
class PendingAlo:
    """An ALO order waiting for a fill event from the trade stream."""
    direction:   str          # 'buy' | 'sell'
    limit_price: float        # bid+tick or ask-tick
    notional:    float        # ORDER_SIZE_BTC × limit_price
    ofi:         float
    tfi:         Optional[float]
    trend:       Optional[float]
    spread_at:   float
    mid_at:      float
    signal_ms:   int
    expire_ms:   int          # signal_ms + LIMIT_ORDER_TIMEOUT_MS


@dataclass
class FilledTrade:
    """A filled ALO trade with forward-return snapshots."""
    direction:   str
    fill_price:  float
    notional:    float
    ofi:         float
    tfi:         Optional[float]
    trend:       Optional[float]
    spread_at:   float
    mid_at_fill: float
    fill_ms:     int
    forward:     Dict[int, Optional[float]] = field(default_factory=dict)

    @property
    def entry_latency_ms(self) -> int:
        return 0  # placeholder


@dataclass
class ExpiredOrder:
    direction:   str
    limit_price: float
    notional:    float
    ofi:         float
    signal_ms:   int


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────

class PaperTrader:
    def __init__(self, duration_s: float = 120.0) -> None:
        self.duration_s   = duration_s
        self.state        = BotState()
        self.state.status = __import__("state").BotStatus.RUNNING

        self._pending_alo: List[PendingAlo]  = []
        self._filled:      List[FilledTrade] = []
        self._expired:     List[ExpiredOrder] = []

        # forward-return tracking: (FilledTrade, [(horizon_ms, deadline_wall_ms)])
        self._fwd_pending: List[Tuple[FilledTrade, List[Tuple[int, int]]]] = []

        self._last_mid: Optional[float] = None

    # ── Book update ──────────────────────────────────────────────────────────

    def on_book(self, book: OrderBook) -> None:
        now_wall = int(time.time() * 1000)
        mid      = book.mid_price()

        # 1. Resolve forward-return snapshots for filled trades.
        if mid is not None:
            for trade, horizons in self._fwd_pending:
                remaining = []
                for h, deadline in horizons:
                    if now_wall >= deadline:
                        trade.forward[h] = mid
                    else:
                        remaining.append((h, deadline))
                horizons[:] = remaining
            self._fwd_pending = [(t, h) for t, h in self._fwd_pending if h]

        # 2. Expire stale ALO orders.
        still_pending = []
        for order in self._pending_alo:
            if now_wall >= order.expire_ms:
                logger.info("ALO expired  | %s @ %.2f (%.0fms)",
                            order.direction, order.limit_price,
                            now_wall - order.signal_ms)
                self._expired.append(ExpiredOrder(
                    direction   = order.direction,
                    limit_price = order.limit_price,
                    notional    = order.notional,
                    ofi         = order.ofi,
                    signal_ms   = order.signal_ms,
                ))
            else:
                still_pending.append(order)
        self._pending_alo = still_pending

        # 3. Compute OFI and evaluate signal.
        ofi = process_book_update(self.state, book)
        if ofi is None or not self.state.is_running():
            return

        direction = evaluate_signal(self.state, ofi)
        if direction is None:
            return

        best_bid = book.best_bid()
        best_ask = book.best_ask()
        if direction == "buy":
            if best_bid is None or best_ask is None:
                return
            limit_price = best_bid.price + config.PRICE_TICK
            if limit_price >= best_ask.price:   # would cross — cap just inside
                limit_price = best_ask.price - config.PRICE_TICK
        else:
            if best_bid is None or best_ask is None:
                return
            limit_price = best_ask.price - config.PRICE_TICK
            if limit_price <= best_bid.price:
                limit_price = best_bid.price + config.PRICE_TICK

        tfi   = compute_tfi(self.state)
        trend = compute_price_trend(self.state)

        order = PendingAlo(
            direction   = direction,
            limit_price = limit_price,
            notional    = config.ORDER_SIZE_BTC * limit_price,
            ofi         = ofi,
            tfi         = tfi,
            trend       = trend,
            spread_at   = book.spread() or 0.0,
            mid_at      = mid or limit_price,
            signal_ms   = now_wall,
            expire_ms   = now_wall + config.LIMIT_ORDER_TIMEOUT_MS,
        )
        self._pending_alo.append(order)

        logger.info(
            "SIGNAL %s | OFI=%+.4f TFI=%s trend=%s$ "
            "limit=%.2f bid=%.2f ask=%.2f spread=%.2f$",
            direction.upper(), ofi,
            f"{tfi:+.3f}" if tfi is not None else "N/A",
            f"{trend:+.2f}" if trend is not None else "N/A",
            limit_price,
            best_bid.price, best_ask.price,
            book.spread() or 0,
        )

    # ── Trade update ─────────────────────────────────────────────────────────

    def on_trade(self, trade: dict) -> None:
        ingest_trade(self.state, trade)

        if not self._pending_alo:
            return

        try:
            side      = trade.get("side", "")
            px        = float(trade.get("px", 0))
            trade_ms  = int(trade.get("time", time.time() * 1000))
        except (ValueError, TypeError):
            return

        still_pending = []
        now_wall      = int(time.time() * 1000)

        for order in self._pending_alo:
            filled = False
            if order.direction == "buy"  and side == "A" and px <= order.limit_price:
                filled = True
            elif order.direction == "sell" and side == "B" and px >= order.limit_price:
                filled = True

            if filled:
                # Use the trade price as the actual fill (could be better than limit).
                actual_fill = min(px, order.limit_price) if order.direction == "buy" \
                              else max(px, order.limit_price)
                mid_now = self.state.book.mid_price()

                ft = FilledTrade(
                    direction   = order.direction,
                    fill_price  = actual_fill,
                    notional    = config.ORDER_SIZE_BTC * actual_fill,
                    ofi         = order.ofi,
                    tfi         = order.tfi,
                    trend       = order.trend,
                    spread_at   = order.spread_at,
                    mid_at_fill = mid_now or actual_fill,
                    fill_ms     = now_wall,
                )
                self._filled.append(ft)
                self._fwd_pending.append(
                    (ft, [(h, now_wall + h) for h in HORIZONS_MS])
                )
                logger.info(
                    "FILL   %s | limit=%.2f actual=%.2f mid=%.2f latency=%dms",
                    order.direction.upper(), order.limit_price, actual_fill,
                    mid_now or 0, now_wall - order.signal_ms,
                )
            else:
                still_pending.append(order)

        self._pending_alo = still_pending

    # ── Report ───────────────────────────────────────────────────────────────

    def report(self) -> None:
        sep = "═" * 74
        logger.info(sep)
        logger.info("  PAPER TRADE REPORT  —  Hyperliquid mainnet BTC ALO (1-tick inside spread)")
        logger.info(sep)

        n_sig     = len(self._filled) + len(self._expired) + len(self._pending_alo)
        n_filled  = len(self._filled)
        n_expired = len(self._expired)
        n_pending = len(self._pending_alo)   # still waiting at session end
        fill_rate = n_filled / n_sig * 100 if n_sig else 0

        buys  = [t for t in self._filled if t.direction == "buy"]
        sells = [t for t in self._filled if t.direction == "sell"]

        logger.info("  Signals fired : %d  (buys=%d sells=%d)", n_sig,
                    len([o for o in self._filled+[ExpiredOrder(o.direction,0,0,0,0) for o in self._expired] if True]),
                    0)
        # Redo buy/sell counts across all outcomes
        all_dirs = (
            [t.direction for t in self._filled]
            + [e.direction for e in self._expired]
            + [p.direction for p in self._pending_alo]
        )
        n_buy_sig  = all_dirs.count("buy")
        n_sell_sig = all_dirs.count("sell")
        logger.info("  Signals       : %d total (buy=%d, sell=%d)", n_sig, n_buy_sig, n_sell_sig)
        logger.info("  Filled        : %d  (%.0f%%)  Expired: %d  Still-pending: %d",
                    n_filled, fill_rate, n_expired, n_pending)
        logger.info("  Filled buys   : %d   Filled sells: %d", len(buys), len(sells))

        if n_filled == 0:
            logger.info("  No fills — market too quiet or thresholds too high.")
            logger.info(sep)
            return

        avg_notional = sum(t.notional for t in self._filled) / n_filled
        logger.info("  Avg notional  : $%.2f / trade  (%.4f BTC @ ~$%.0f)",
                    avg_notional, config.ORDER_SIZE_BTC, avg_notional / config.ORDER_SIZE_BTC)
        logger.info("")

        # ── Per-horizon tables ────────────────────────────────────────────────
        logger.info("  %-8s  %-4s  %-6s  %-10s  %-10s  %-11s  %-7s  %-6s  %-7s",
                    "Horizon", "n", "Acc%", "avg_raw$", "avg_net$",
                    "total_net$", "bps_net", "PF", "Kelly%")
        logger.info("  " + "─" * 70)

        best_h_data = None   # for per-trade table

        for h in HORIZONS_MS:
            resolved = [t for t in self._filled if t.forward.get(h) is not None]
            if not resolved:
                continue

            raw_list, net_list, bps_list = [], [], []
            correct = 0

            for t in resolved:
                fwd   = t.forward[h]
                ret   = fwd - t.mid_at_fill
                dm    = 1 if t.direction == "buy" else -1

                # ALO: we filled below (buy) or above (sell) mid → earn half-spread
                half_spr  = t.spread_at / 2
                pnl_pts   = dm * ret + half_spr        # per BTC
                pnl_raw   = pnl_pts * config.ORDER_SIZE_BTC
                rebate    = 2 * MAKER_REBATE * t.notional
                pnl_net   = pnl_raw + rebate            # rebate is positive (we earn)
                bps       = (pnl_net / t.notional) * 10_000

                raw_list.append(pnl_raw)
                net_list.append(pnl_net)
                bps_list.append(bps)
                if (ret > 0 and dm == 1) or (ret < 0 and dm == -1):
                    correct += 1

            nr       = len(raw_list)
            acc      = correct / nr * 100
            avg_raw  = sum(raw_list) / nr
            avg_net  = sum(net_list) / nr
            total_net = sum(net_list)

            try:
                avg_bps = sum(bps_list) / nr
            except Exception:
                avg_bps = 0

            wins  = [p for p in net_list if p > 0]
            loses = [p for p in net_list if p <= 0]
            pf    = sum(wins) / (-sum(loses)) if loses and sum(loses) < 0 else float("inf")

            w_rate = acc / 100
            avg_w  = sum(wins) / len(wins) if wins else 0
            avg_l  = abs(sum(loses) / len(loses)) if loses else 0
            b_ratio = (avg_w / avg_l) if avg_l > 0 else float("inf")
            kelly   = max(0.0, w_rate - (1 - w_rate) / b_ratio) * 100 if b_ratio != float("inf") else w_rate * 100

            pf_str    = f"{pf:.2f}" if pf != float("inf") else "∞"
            kelly_str = f"{kelly:.1f}"

            logger.info("  %-8s  %-4d  %-6.1f  %-10.4f  %-10.4f  %-11.4f  %-7.2f  %-6s  %-7s",
                        f"T+{h}ms", nr, acc, avg_raw, avg_net, total_net, avg_bps, pf_str, kelly_str)

            if h == 1000 or (best_h_data is None):
                best_h_data = (h, resolved, net_list)

        # ── Per-trade table (T+1000ms) ────────────────────────────────────────
        if best_h_data:
            bh, bh_trades, bh_net = best_h_data
            logger.info("")
            logger.info("  Per-trade breakdown  (T+%dms):", bh)
            logger.info("  %-5s  %-9s  %-9s  %-9s  %+7s  %-6s  %-8s  %-8s  %-4s",
                        "Dir", "fill$", "mid@fill", "fwd_mid", "ret$", "bps",
                        "pnl_raw$", "pnl_net$", "W/L")
            logger.info("  " + "─" * 70)
            for t, pnl_net in zip(bh_trades, bh_net):
                fwd  = t.forward.get(bh)
                ret  = (fwd - t.mid_at_fill) if fwd is not None else 0
                dm   = 1 if t.direction == "buy" else -1
                pnl_raw = (dm * ret + t.spread_at / 2) * config.ORDER_SIZE_BTC
                rebate  = 2 * MAKER_REBATE * t.notional
                bps     = (pnl_net / t.notional) * 10_000
                wl      = "W" if pnl_net > 0 else "L"
                logger.info("  %-5s  %-9.2f  %-9.2f  %-9.2f  %+7.2f  %+5.2f  %+8.4f  %+8.4f  %-4s",
                            t.direction, t.fill_price, t.mid_at_fill,
                            fwd or 0, ret, bps, pnl_raw, pnl_net, wl)

            total_net = sum(bh_net)
            logger.info("  " + "─" * 70)
            logger.info("  %-35s  TOTAL net P&L: %+.4f$  (%+.2f bps avg)",
                        "", total_net,
                        (sum((p / t.notional) * 10_000 for t, p in zip(bh_trades, bh_net)) / len(bh_net) if bh_net else 0))

        # ── Fee comparison ───────────────────────────────────────────────────
        logger.info("")
        ex = self._filled[0]
        alo_fee   = -2 * MAKER_REBATE * ex.notional   # negative = we earn
        ioc_fee   =  2 * TAKER_FEE   * ex.notional
        spread_vs = ex.spread_at * config.ORDER_SIZE_BTC
        logger.info("  Fee comparison (per round-trip, $%.2f notional):", ex.notional)
        logger.info("    ALO maker  : earn $%.4f rebate + earn $%.4f spread  = +$%.4f  (%+.2f bps)",
                    -alo_fee, spread_vs, -alo_fee + spread_vs,
                    (-alo_fee + spread_vs) / ex.notional * 10_000)
        logger.info("    IOC taker  : pay  $%.4f fee   + pay  $%.4f spread  = -$%.4f  (%.2f bps)",
                    ioc_fee, spread_vs, ioc_fee + spread_vs,
                    (ioc_fee + spread_vs) / ex.notional * 10_000)

        # ── Annualised estimate ───────────────────────────────────────────────
        if best_h_data and sum(bh_net) != 0:
            logger.info("")
            filled_h  = 1000
            trades_per_session = n_filled
            session_dur_min    = self.duration_s / 60
            trades_per_day     = trades_per_session / session_dur_min * 60 * 16   # 16h/day active
            daily_pnl          = (sum(bh_net) / len(bh_net)) * trades_per_day
            annual_pnl         = daily_pnl * 252
            capital_deployed   = avg_notional * min(n_filled, 5)  # assume ≤5 concurrent
            annual_ret_pct     = (annual_pnl / capital_deployed) * 100 if capital_deployed else 0
            logger.info("  Annualised estimate (rough — same-conditions extrapolation):")
            logger.info("    Fills/day  : ~%.0f  |  Net PnL/day: $%.4f  |  Annual P&L: $%.2f",
                        trades_per_day, daily_pnl, annual_pnl)
            logger.info("    Capital    : $%.2f deployed  →  Est. annual return: %.1f%%",
                        capital_deployed, annual_ret_pct)

        logger.info(sep)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket runner
# ─────────────────────────────────────────────────────────────────────────────

def run_paper_trader(duration_s: float = 120.0, record_file: Optional[str] = None) -> None:
    from hyperliquid.info import Info

    trader  = PaperTrader(duration_s=duration_s)
    loop    = asyncio.new_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    rec_fh  = open(record_file, "w") if record_file else None

    logger.info("Connecting to %s", PAPER_API_URL)
    info = Info(base_url=PAPER_API_URL, skip_ws=False)

    def _record(kind: str, raw_msg: dict) -> None:
        if rec_fh:
            rec_fh.write(json.dumps({"type": kind, "wall_ms": int(time.time() * 1000),
                                     "data": raw_msg.get("data", raw_msg)}) + "\n")

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

    info.subscribe({"type": "l2Book", "coin": config.COIN}, on_book_msg)
    info.subscribe({"type": "trades", "coin": config.COIN}, on_trades_msg)
    logger.info("Subscribed to l2Book + trades  |  %s  |  duration=%.0fs  |  recording=%s",
                config.COIN, duration_s, record_file or "off")

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
    if rec_fh:
        rec_fh.close()
        logger.info("Session recorded to %s", record_file)
    trader.report()


if __name__ == "__main__":
    import json as _json_mod  # already imported above but kept explicit

    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=120.0,
                        help="Session length in seconds")
    parser.add_argument("--record", metavar="FILE",
                        help="Save raw WS stream to JSONL for later replay backtest")
    args = parser.parse_args()
    run_paper_trader(duration_s=args.duration, record_file=args.record)
