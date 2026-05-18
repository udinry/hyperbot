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
  where the mid-price moved.

Capital-risk metrics reported:
  - Notional at risk per trade  = ORDER_SIZE_BTC × fill_price
  - PnL_bps  = (PnL_USD / notional) × 10_000        (basis-points on capital)
  - Return on capital (%) net of estimated taker fees
  - Profit factor = gross_wins / gross_losses
  - Kelly criterion fraction

Usage:
  cd hft_bot
  python paper_trader.py              # runs for 120 seconds
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

PAPER_API_URL = "https://api.hyperliquid.xyz"

# Hyperliquid taker fee (IOC / market orders).
TAKER_FEE_RATE = 0.00035   # 0.035 %

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
    direction: str
    ofi: float
    tfi: Optional[float]
    trend: Optional[float]         # 3-second price trend at signal time ($)
    fill_price: float
    mid_at_signal: float
    spread_at_signal: float
    notional_usd: float            # ORDER_SIZE_BTC × fill_price
    forward: Dict[int, Optional[float]] = field(default_factory=dict)


HORIZONS_MS = [100, 250, 500, 1000]


# ---------------------------------------------------------------------------
# Paper trading engine
# ---------------------------------------------------------------------------

class PaperTrader:
    def __init__(self, duration_s: float = 120.0) -> None:
        self.duration_s = duration_s
        self.state      = BotState()
        self.state.status = __import__("state").BotStatus.RUNNING

        self._pending: List[Tuple[Signal, List[Tuple[int, int]]]] = []
        self._mid_history: Deque[Tuple[int, float]] = deque(maxlen=5000)
        self.signals: List[Signal] = []

    def on_book(self, book: OrderBook) -> None:
        mid = book.mid_price()
        if mid:
            self._mid_history.append((book.timestamp_ms, mid))

        # Resolve pending forward-return snapshots.
        now_ms = int(time.time() * 1000)
        for sig, pending_horizons in self._pending:
            still_pending = []
            for horizon_ms, deadline_ms in pending_horizons:
                if now_ms >= deadline_ms:
                    sig.forward[horizon_ms] = mid
                else:
                    still_pending.append((horizon_ms, deadline_ms))
            pending_horizons[:] = still_pending
        self._pending = [(s, ph) for s, ph in self._pending if ph]

        ofi = process_book_update(self.state, book)
        if ofi is None:
            return

        direction = evaluate_signal(self.state, ofi)
        if direction is None:
            return

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

        spread_val  = book.spread() or 0.0
        tfi         = compute_tfi(self.state)
        trend       = compute_price_trend(self.state)
        notional    = config.ORDER_SIZE_BTC * fill_px

        sig = Signal(
            ts_ms=book.timestamp_ms,
            direction=direction,
            ofi=ofi,
            tfi=tfi,
            trend=trend,
            fill_price=fill_px,
            mid_at_signal=mid or fill_px,
            spread_at_signal=spread_val,
            notional_usd=notional,
        )
        self.signals.append(sig)

        now_ms = int(time.time() * 1000)
        self._pending.append((sig, [(h, now_ms + h) for h in HORIZONS_MS]))

        logger.info(
            "%s | OFI=%+.4f TFI=%s trend=%s$ fill=%.2f spread=%.2f$ notional=$%.2f",
            direction.upper(),
            ofi,
            f"{tfi:+.3f}" if tfi is not None else "N/A",
            f"{trend:+.2f}" if trend is not None else "N/A",
            fill_px,
            spread_val,
            notional,
        )

    def on_trade(self, trade: dict) -> None:
        ingest_trade(self.state, trade)

    # ------------------------------------------------------------------
    def report(self) -> None:
        n = len(self.signals)
        sep = "=" * 72
        logger.info(sep)
        logger.info("PAPER TRADING REPORT  (signals=%d)", n)
        logger.info(sep)

        if n == 0:
            logger.info("No signals — try lower thresholds or longer duration.")
            return

        buys  = [s for s in self.signals if s.direction == "buy"]
        sells = [s for s in self.signals if s.direction == "sell"]
        avg_notional = sum(s.notional_usd for s in self.signals) / n
        logger.info("Buys: %d  Sells: %d  |  avg notional/trade: $%.2f", len(buys), len(sells), avg_notional)
        logger.info("")

        # --- Per-horizon analysis ---
        for h in HORIZONS_MS:
            resolved = [s for s in self.signals if s.forward.get(h) is not None]
            if not resolved:
                continue

            pnl_raw_list:  List[float] = []   # $ before fees
            pnl_net_list:  List[float] = []   # $ after fees
            pnl_bps_list:  List[float] = []   # bps on notional
            correct = 0

            for s in resolved:
                fwd_mid  = s.forward[h]
                if fwd_mid is None:
                    continue
                ret       = fwd_mid - s.mid_at_signal
                half_spr  = s.spread_at_signal / 2
                direction_mult = 1 if s.direction == "buy" else -1
                pnl_pts   = direction_mult * ret - half_spr    # spread-adjusted, per BTC
                pnl_usd   = pnl_pts * config.ORDER_SIZE_BTC

                # Taker fee both legs (entry + exit assumed taker).
                fee_usd   = 2 * TAKER_FEE_RATE * s.notional_usd
                pnl_net   = pnl_usd - fee_usd

                bps = (pnl_usd / s.notional_usd) * 10_000

                pnl_raw_list.append(pnl_usd)
                pnl_net_list.append(pnl_net)
                pnl_bps_list.append(bps)

                moved_right = (ret > 0 and s.direction == "buy") or (ret < 0 and s.direction == "sell")
                if moved_right:
                    correct += 1

            if not pnl_raw_list:
                continue

            nr       = len(pnl_raw_list)
            acc      = correct / nr * 100
            avg_raw  = sum(pnl_raw_list) / nr
            avg_net  = sum(pnl_net_list) / nr
            total_net = sum(pnl_net_list)
            avg_bps  = sum(pnl_bps_list) / nr

            wins  = [p for p in pnl_raw_list if p > 0]
            loses = [p for p in pnl_raw_list if p <= 0]
            pf    = sum(wins) / (-sum(loses)) if loses and sum(loses) < 0 else float("inf")

            try:
                stdev = statistics.stdev(pnl_bps_list)
                sharpe_per_trade = avg_bps / stdev if stdev > 0 else 0.0
            except Exception:
                sharpe_per_trade = 0.0

            win_rate = acc / 100
            avg_win  = sum(wins) / len(wins) if wins else 0
            avg_loss = abs(sum(loses) / len(loses)) if loses else 0
            # Kelly: f* = p - (1-p) / b   where b = avg_win / avg_loss
            b     = (avg_win / avg_loss) if avg_loss > 0 else float("inf")
            kelly = max(0.0, win_rate - (1 - win_rate) / b) if b != float("inf") else win_rate

            logger.info(
                "T+%4dms | n=%2d | acc=%.0f%% | "
                "pnl_raw=$%.4f | pnl_net=$%.4f | total_net=$%.4f | "
                "bps/trade=%.2f | PF=%.2f | kelly=%.1f%%",
                h, nr, acc,
                avg_raw, avg_net, total_net,
                avg_bps, pf, kelly * 100,
            )

        logger.info("")
        logger.info("Capital-risk breakdown (per-trade, T+1000ms focus):")

        h = 1000
        resolved = [s for s in self.signals if s.forward.get(h) is not None]
        if resolved:
            logger.info("  %-6s  %-8s  %-8s  %-8s  %-6s  %-8s  %-8s",
                        "Dir", "fill", "fwd_mid", "ret$", "bps", "pnl_raw", "pnl_net")
            for s in resolved:
                fwd = s.forward[h]
                if fwd is None:
                    continue
                ret   = fwd - s.mid_at_signal
                dm    = 1 if s.direction == "buy" else -1
                pnl_r = (dm * ret - s.spread_at_signal / 2) * config.ORDER_SIZE_BTC
                fee   = 2 * TAKER_FEE_RATE * s.notional_usd
                pnl_n = pnl_r - fee
                bps   = (pnl_r / s.notional_usd) * 10_000
                logger.info("  %-6s  %-8.2f  %-8.2f  %+7.2f  %+5.2f  %+8.4f  %+8.4f",
                            s.direction, s.fill_price, fwd, ret, bps, pnl_r, pnl_n)

        logger.info("")
        # --- ALO (maker) model ---
        # On mainnet, spread ≈ 1.3bps → live bot uses ALO (post-only).
        # ALO BUY fills at bid (earn half-spread vs paying it with IOC).
        # Maker rebate = -0.01% per leg.  Assume 75% fill rate within timeout.
        MAKER_REBATE_RATE = 0.0001   # -0.01% = earn this per filled leg
        ALO_FILL_RATE     = 0.75     # conservative: 75% of ALO orders fill

        if resolved:
            logger.info("ALO (maker) model  —  bid/ask fill, earn rebate, %.0f%% fill rate:", ALO_FILL_RATE * 100)
            alo_pnl_list = []
            for s in resolved:
                fwd = s.forward[1000]
                if fwd is None:
                    continue
                ret   = fwd - s.mid_at_signal
                dm    = 1 if s.direction == "buy" else -1
                # ALO fills at mid∓half_spread, so we earn half_spread per leg.
                half_spr = s.spread_at_signal / 2
                pnl_pts  = dm * ret + half_spr           # earn half-spread on entry
                pnl_usd  = pnl_pts * config.ORDER_SIZE_BTC
                rebate   = 2 * MAKER_REBATE_RATE * s.notional_usd  # earn both legs
                pnl_alo  = (pnl_usd + rebate) * ALO_FILL_RATE      # scale by fill rate
                bps_alo  = (pnl_alo / s.notional_usd) * 10_000
                alo_pnl_list.append((pnl_alo, bps_alo))

            if alo_pnl_list:
                avg_alo     = sum(p for p, _ in alo_pnl_list) / len(alo_pnl_list)
                total_alo   = sum(p for p, _ in alo_pnl_list)
                avg_bps_alo = sum(b for _, b in alo_pnl_list) / len(alo_pnl_list)
                logger.info("  T+1000ms | avg_pnl=$%.4f | total=$%.4f | bps/trade=%.2f | verdict: %s",
                            avg_alo, total_alo, avg_bps_alo,
                            "PROFITABLE ✓" if avg_alo > 0 else "STILL NEGATIVE ✗")

        logger.info("")
        # Notional risk context
        sample = resolved[0] if resolved else None
        if sample:
            notional_ex   = sample.notional_usd
            ioc_fee_ex    = 2 * TAKER_FEE_RATE * notional_ex
            alo_rebate_ex = 2 * MAKER_REBATE_RATE * notional_ex
            logger.info(
                "Notional/trade: $%.2f  |  IOC round-trip fee: $%.4f (%.1fbps)  |  ALO rebate: +$%.4f (%.1fbps)",
                notional_ex, ioc_fee_ex, (ioc_fee_ex / notional_ex) * 10_000,
                alo_rebate_ex, (alo_rebate_ex / notional_ex) * 10_000,
            )
            logger.info(
                "  → Break-even gross bps needed:  IOC=%.1fbps  |  ALO (earn spread)=%.1fbps",
                (ioc_fee_ex / notional_ex) * 10_000,
                max(0.0, (ioc_fee_ex - alo_rebate_ex - sample.spread_at_signal * config.ORDER_SIZE_BTC) / notional_ex * 10_000),
            )
        logger.info(sep)



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
