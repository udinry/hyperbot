"""
Live mutable state for the OFI bot.

Deliberately kept as plain dataclasses / mutables so that strategy.py and
executor.py can read / write with zero serialisation overhead.  All asyncio
tasks share a single BotState instance on the same thread; no locks needed.
"""
from __future__ import annotations

import asyncio
import csv
import datetime
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import clock
import config

logger = logging.getLogger("state")

# Daily realized-PnL ledger, persisted so a restart cannot reset the circuit
# breaker and so "daily" actually means per-UTC-day, not per-session.
PNL_STATE_FILE = Path(__file__).parent / "daily_pnl_state.json"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class BotStatus(Enum):
    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED_INVENTORY = "paused_inventory"   # inventory limit hit
    CIRCUIT_BREAKER = "circuit_breaker"     # daily-loss limit hit
    STOPPED = "stopped"


# ---------------------------------------------------------------------------
# Order book data structures
# ---------------------------------------------------------------------------
@dataclass
class Level:
    price: float
    size: float

    @classmethod
    def from_ws(cls, raw: dict) -> "Level":
        return cls(price=float(raw["px"]), size=float(raw["sz"]))


@dataclass
class OrderBook:
    """Top-N snapshot of the BTC L2 book, refreshed on every WebSocket push."""
    bids: List[Level] = field(default_factory=list)   # best bid first (desc)
    asks: List[Level] = field(default_factory=list)   # best ask first (asc)
    timestamp_ms: int = 0

    def best_bid(self) -> Optional[Level]:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> Optional[Level]:
        return self.asks[0] if self.asks else None

    def mid_price(self) -> Optional[float]:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid and ask:
            return round((bid.price + ask.price) / 2, 2)
        return None

    def spread(self) -> Optional[float]:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid and ask:
            return ask.price - bid.price
        return None


# ---------------------------------------------------------------------------
# Open-order tracking
# ---------------------------------------------------------------------------
@dataclass
class OpenOrder:
    oid: int
    cloid: str
    is_buy: bool
    price: float
    size: float
    placed_at_ms: int
    cancel_task: Optional[asyncio.Task] = field(default=None, compare=False, repr=False)


# ---------------------------------------------------------------------------
# OFI rolling-window entry
# ---------------------------------------------------------------------------
OFIEntry = Tuple[int, float]         # (timestamp_ms, signed_value)
TradeEntry = Tuple[int, float, float]  # (timestamp_ms, signed_volume, price)


class RollingOFIWindow:
    """Time-pruned window of OFI deltas with an O(1) running sum.

    All timestamps must come from clock.now_ms() — one clock domain only.
    """
    __slots__ = ("entries", "total", "maxlen")

    def __init__(self, maxlen: int = 2000) -> None:
        self.entries: Deque[OFIEntry] = deque()
        self.total: float = 0.0
        self.maxlen = maxlen

    def append(self, ts_ms: int, delta: float) -> None:
        if len(self.entries) >= self.maxlen:
            _, old = self.entries.popleft()
            self.total -= old
        self.entries.append((ts_ms, delta))
        self.total += delta

    def prune(self, cutoff_ms: int) -> None:
        entries = self.entries
        while entries and entries[0][0] < cutoff_ms:
            _, old = entries.popleft()
            self.total -= old
        if not entries:
            self.total = 0.0   # kill float drift whenever the window empties

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)


class RollingTradeWindow:
    """Time-pruned window of executed trades with O(1) running sums for
    TFI (buy/sell volume) and VWAP (px*vol, vol).

    All timestamps must come from clock.now_ms() — never exchange epoch ms.
    """
    __slots__ = ("entries", "buy_vol", "sell_vol", "px_vol", "maxlen")

    def __init__(self, maxlen: int = 2000) -> None:
        self.entries: Deque[TradeEntry] = deque()
        self.buy_vol: float = 0.0
        self.sell_vol: float = 0.0
        self.px_vol: float = 0.0     # sum(|vol| * px) for VWAP
        self.maxlen = maxlen

    def _remove(self, entry: TradeEntry) -> None:
        _, signed, px = entry
        if signed > 0:
            self.buy_vol -= signed
        else:
            self.sell_vol -= -signed
        self.px_vol -= abs(signed) * px

    def append(self, ts_ms: int, signed_vol: float, px: float) -> None:
        if len(self.entries) >= self.maxlen:
            self._remove(self.entries.popleft())
        self.entries.append((ts_ms, signed_vol, px))
        if signed_vol > 0:
            self.buy_vol += signed_vol
        else:
            self.sell_vol += -signed_vol
        self.px_vol += abs(signed_vol) * px

    def prune(self, cutoff_ms: int) -> None:
        entries = self.entries
        while entries and entries[0][0] < cutoff_ms:
            self._remove(entries.popleft())
        if not entries:
            self.buy_vol = self.sell_vol = self.px_vol = 0.0

    @property
    def total_vol(self) -> float:
        return self.buy_vol + self.sell_vol

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)


# ---------------------------------------------------------------------------
# Master bot state
# ---------------------------------------------------------------------------
@dataclass
class BotState:
    # --- Order book ---
    book: OrderBook = field(default_factory=OrderBook)

    # --- Position / inventory ---
    inventory_btc: float = 0.0
    entry_price: Optional[float] = None

    # --- Open orders ---
    open_orders: Dict[int, OpenOrder] = field(default_factory=dict)

    # --- PnL (net of fees, persisted per UTC day) ---
    daily_pnl_usd: float = 0.0
    daily_fees_usd: float = 0.0
    daily_date: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    )
    session_start_ts: float = field(default_factory=time.time)

    # --- Trade journal accumulator (aggregates partial closing fills into one row) ---
    _journal_entry_px: float = field(default=0.0, repr=False)
    _journal_direction: str = field(default="", repr=False)
    _journal_pnl_acc: float = field(default=0.0, repr=False)
    _journal_exit_px: float = field(default=0.0, repr=False)
    _journal_sz_acc: float = field(default=0.0, repr=False)

    # Latest normalised OFI in [-1, +1]; updated on every book tick.
    latest_ofi: Optional[float] = None
    # Ring buffer of the most recent normalised OFI values (for exhaustion gate).
    ofi_recent: Deque[float] = field(default_factory=lambda: deque(maxlen=20))

    # --- OFI rolling window (O(1) running sum, pruned to OFI_WINDOW_MS) ---
    ofi_window: RollingOFIWindow = field(default_factory=RollingOFIWindow)
    # Previous book snapshot used to compute OFI deltas.
    prev_bids: List[Level] = field(default_factory=list)
    prev_asks: List[Level] = field(default_factory=list)

    # --- Trade flow window (for TFI + VWAP signals; O(1) running sums) ---
    trade_window: RollingTradeWindow = field(default_factory=RollingTradeWindow)

    # --- Mid-price history for short-term trend gate ---
    # Each entry: (timestamp_ms, mid_price).  Used by compute_price_trend().
    mid_history: Deque[OFIEntry] = field(
        default_factory=lambda: deque(maxlen=500)
    )

    # --- 5-min mid-price history for momentum confirmation gate ---
    # Capped at 2000 entries (~5 min at typical WS tick rate).
    mid_history_5m: Deque[OFIEntry] = field(
        default_factory=lambda: deque(maxlen=2000)
    )

    # --- Dynamic position sizing (refreshed from live balance every 5 min) ---
    order_size_btc: float = field(default_factory=lambda: config.ORDER_SIZE_BTC)

    # P&L projection multiplier (REAL_TEST_MODE computes this automatically as
    # intended-full-size / actual-test-size; otherwise config.LIVE_TEST_SCALE).
    live_test_scale: float = field(default_factory=lambda: config.LIVE_TEST_SCALE)

    # ATR-adaptive TP pct, updated every 5 min by position sizer. 0=use static config.
    dynamic_tp_pct: float = field(default_factory=lambda: config.TAKE_PROFIT_PCT)
    # Inventory limit tracks order_size_btc: pauses quoting when |inventory| >= this.
    max_inventory_btc: float = field(default_factory=lambda: config.MAX_INVENTORY_BTC)
    # Circuit breaker threshold: 2 stop-losses worth, scales with position size.
    max_daily_loss_usd: float = field(default_factory=lambda: config.MAX_DAILY_LOSS_USD)

    # --- Bot lifecycle ---
    status: BotStatus = BotStatus.INITIALIZING
    last_signal_ms: int = 0
    # Entry lockout: no new entry signals until this clock.now_ms() timestamp.
    # Set after placing an entry order so we don't stack entries while one is
    # resting (previously done by writing a future value into last_signal_ms).
    lockout_until_ms: int = 0
    ws_reconnect_count: int = 0
    # clock.now_ms() of the last processed book update — data-staleness watchdog.
    last_book_arrival_ms: int = 0

    # --- Signal persistence counters (previously injected dynamically) ---
    persist_buy: int = 0
    persist_sell: int = 0
    last_signal_dir: Optional[str] = None

    # --- Exchange-side stop-loss and take-profit resting orders ---
    sl_oid: Optional[int] = None
    tp_oid: Optional[int] = None
    sl_trailed: bool = False  # True once SL has been moved to break-even this position

    # --- Position open timestamp (ms) for time-limit exit ---
    position_open_ms: Optional[int] = None

    # --- Post-SL cooldown: ms timestamp of last loss stop-loss hit ---
    last_sl_ms: int = 0

    # --- Exit signal cooldown (ms timestamp of last OFI-based exit) ---
    last_exit_ms: int = 0

    # --- Funding rate (hourly, updated every 15 min from metaAndAssetCtxs) ---
    funding_rate: float = 0.0

    # --- Statistics ---
    total_orders_placed: int = 0
    total_orders_filled: int = 0
    total_orders_cancelled: int = 0
    total_buys_filled: int = 0
    total_sells_filled: int = 0

    # ---------------------------------------------------------------------------
    # Derived helpers
    # ---------------------------------------------------------------------------
    def mid_price(self) -> Optional[float]:
        return self.book.mid_price()

    def unrealized_pnl_usd(self) -> float:
        mid = self.mid_price()
        if mid is None or self.entry_price is None or self.inventory_btc == 0.0:
            return 0.0
        return self.inventory_btc * (mid - self.entry_price)

    def total_pnl_usd(self) -> float:
        return self.daily_pnl_usd + self.unrealized_pnl_usd()

    def is_running(self) -> bool:
        return self.status == BotStatus.RUNNING

    def can_buy(self) -> bool:
        return self.is_running() and self.inventory_btc < self.max_inventory_btc

    def can_sell(self) -> bool:
        return self.is_running() and self.inventory_btc > -self.max_inventory_btc

    # ---------------------------------------------------------------------------
    # Inventory update after a fill
    # ---------------------------------------------------------------------------
    def _append_trade_journal(self, direction: str, entry_px: float, exit_px: float,
                               size: float, closed_pnl: float) -> None:
        journal = Path(__file__).parent / "trades.csv"
        write_header = not journal.exists()
        # Read last cumulative_pnl from CSV so it persists across restarts.
        prev_cumulative = 0.0
        if journal.exists():
            try:
                with open(journal, newline="") as _fh:
                    rows = list(csv.reader(_fh))
                if len(rows) > 1:
                    prev_cumulative = float(rows[-1][-1])
            except (ValueError, IndexError):
                pass
        with open(journal, "a", newline="") as fh:
            w = csv.writer(fh)
            if write_header:
                w.writerow(["utc_time", "direction", "entry_px", "exit_px",
                             "size_btc", "closed_pnl", "cumulative_pnl"])
            w.writerow([
                datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                direction,
                f"{entry_px:.2f}",
                f"{exit_px:.2f}",
                f"{size:.4f}",
                f"{closed_pnl:.4f}",
                f"{prev_cumulative + closed_pnl:.4f}",
            ])

    def record_fill(self, is_buy: bool, fill_px: float, fill_sz: float,
                    closed_pnl: float, fee: float = 0.0) -> None:
        signed_sz = fill_sz if is_buy else -fill_sz
        net_pnl = closed_pnl - fee   # closedPnl from HL excludes fees; subtract them

        # Accumulate closing-fill data; flush to journal only when position reaches 0.
        # Prevents multiple CSV rows when an SL/TP order fills in several partials.
        if abs(closed_pnl) > 1e-9 and self.entry_price is not None and self.inventory_btc != 0.0:
            if abs(self._journal_pnl_acc) < 1e-12:
                self._journal_entry_px  = self.entry_price
                self._journal_direction = "LONG" if self.inventory_btc > 0 else "SHORT"
            self._journal_pnl_acc += net_pnl
            self._journal_exit_px  = fill_px
            self._journal_sz_acc  += fill_sz

        if self.inventory_btc == 0.0 or (self.inventory_btc > 0) == is_buy:
            if self.entry_price is None:
                self.entry_price = fill_px
            else:
                total = abs(self.inventory_btc) + fill_sz
                self.entry_price = (
                    abs(self.inventory_btc) * self.entry_price + fill_sz * fill_px
                ) / total
        else:
            if abs(signed_sz) >= abs(self.inventory_btc):
                remaining = abs(signed_sz) - abs(self.inventory_btc)
                self.entry_price = fill_px if remaining > 0 else None

        prev_inv = self.inventory_btc
        self.inventory_btc += signed_sz
        if abs(self.inventory_btc) < 1e-8:
            self.inventory_btc = 0.0
            if abs(self._journal_pnl_acc) > 1e-9:
                self._append_trade_journal(
                    self._journal_direction, self._journal_entry_px,
                    self._journal_exit_px, self._journal_sz_acc, self._journal_pnl_acc,
                )
                if self._journal_pnl_acc < -0.20:
                    self.last_sl_ms = clock.now_ms()
                self._journal_pnl_acc = 0.0
                self._journal_sz_acc  = 0.0
            self.entry_price = None
            self.position_open_ms = None
            self.sl_trailed = False
        elif abs(prev_inv) < 1e-8 and abs(self.inventory_btc) > 1e-8:
            self.position_open_ms = clock.now_ms()

        self.daily_pnl_usd += net_pnl
        self.daily_fees_usd += fee
        self.persist_daily_pnl()

        if is_buy:
            self.total_buys_filled += 1
        else:
            self.total_sells_filled += 1
        self.total_orders_filled += 1

    # ---------------------------------------------------------------------------
    # Daily-PnL persistence — survives restarts, resets at UTC midnight.
    # Without this, a restart silently re-arms the circuit breaker.
    # ---------------------------------------------------------------------------
    def _utc_today(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    def load_daily_pnl(self) -> None:
        """Restore today's realized PnL from disk (call once at startup)."""
        try:
            data = json.loads(PNL_STATE_FILE.read_text())
            if data.get("date") == self._utc_today():
                self.daily_pnl_usd  = float(data.get("realized_net", 0.0))
                self.daily_fees_usd = float(data.get("fees", 0.0))
                self.daily_date     = data["date"]
                logger.info(
                    "Restored daily PnL ledger: %s net=%.2f$ fees=%.2f$",
                    self.daily_date, self.daily_pnl_usd, self.daily_fees_usd,
                )
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Could not load daily PnL state: %s", exc)

    def persist_daily_pnl(self) -> None:
        try:
            tmp = PNL_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "date": self.daily_date,
                "realized_net": round(self.daily_pnl_usd, 6),
                "fees": round(self.daily_fees_usd, 6),
            }))
            tmp.replace(PNL_STATE_FILE)
        except Exception as exc:
            logger.warning("Could not persist daily PnL state: %s", exc)

    def roll_daily_pnl_if_new_day(self) -> bool:
        """Reset the daily ledger at UTC midnight. Returns True if it rolled."""
        today = self._utc_today()
        if today == self.daily_date:
            return False
        logger.info(
            "UTC day rollover %s → %s | closing ledger: net=%.2f$ fees=%.2f$",
            self.daily_date, today, self.daily_pnl_usd, self.daily_fees_usd,
        )
        self.daily_date = today
        self.daily_pnl_usd = 0.0
        self.daily_fees_usd = 0.0
        self.persist_daily_pnl()
        return True

    # ---------------------------------------------------------------------------
    # Status transitions
    # ---------------------------------------------------------------------------
    def set_running(self) -> None:
        self.status = BotStatus.RUNNING

    def set_paused_inventory(self) -> None:
        if self.status == BotStatus.RUNNING:
            self.status = BotStatus.PAUSED_INVENTORY

    def set_circuit_breaker(self) -> None:
        self.status = BotStatus.CIRCUIT_BREAKER

    def set_stopped(self) -> None:
        self.status = BotStatus.STOPPED

    # ---------------------------------------------------------------------------
    # Summary for logging
    # ---------------------------------------------------------------------------
    def summary(self) -> str:
        mid = self.mid_price()
        mid_str = f"{mid:.2f}" if mid else "N/A"
        ofi_str = f" ofi={self.latest_ofi:+.3f}" if self.latest_ofi is not None else ""
        prot_str = ""
        if self.inventory_btc != 0.0 and self.entry_price is not None:
            entry = self.entry_price
            is_long = self.inventory_btc > 0
            sl_trigger = entry if self.sl_trailed else (
                entry * (1 - config.STOP_LOSS_PCT) if is_long else entry * (1 + config.STOP_LOSS_PCT)
            )
            tp_target = (
                entry * (1 + self.dynamic_tp_pct) if is_long else entry * (1 - self.dynamic_tp_pct)
            )
            trail_flag = "T" if self.sl_trailed else ""
            prot_str = f" sl={sl_trigger:.0f}{trail_flag} tp={tp_target:.0f}"
        return (
            f"status={self.status.value} "
            f"inv={self.inventory_btc:+.4f}BTC "
            f"entry={self.entry_price or 0:.2f} "
            f"mid={mid_str} "
            f"unrealPnL={self.unrealized_pnl_usd():+.2f}$ "
            f"realPnL={self.daily_pnl_usd:+.2f}$ "
            f"fills={self.total_orders_filled} "
            f"open_orders={len(self.open_orders)}"
            f" fees={self.daily_fees_usd:.2f}$"
            f"{prot_str}"
            f"{ofi_str}"
        )
