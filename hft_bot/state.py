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
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import config


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
OFIEntry = Tuple[int, float]   # (timestamp_ms, signed_value)


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

    # --- PnL ---
    daily_pnl_usd: float = 0.0
    session_start_ts: float = field(default_factory=time.time)

    # --- OFI rolling window ---
    # Each entry: (timestamp_ms, ofi_delta).  Pruned to OFI_WINDOW_MS.
    ofi_window: Deque[OFIEntry] = field(
        default_factory=lambda: deque(maxlen=2000)
    )
    # Previous book snapshot used to compute OFI deltas.
    prev_bids: List[Level] = field(default_factory=list)
    prev_asks: List[Level] = field(default_factory=list)

    # --- Trade flow window (for TFI signal confirmation) ---
    # Each entry: (timestamp_ms, signed_volume)  positive=buy-initiated, negative=sell
    trade_window: Deque[OFIEntry] = field(
        default_factory=lambda: deque(maxlen=2000)
    )

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

    # ATR-adaptive TP pct, updated every 5 min by position sizer. 0=use static config.
    dynamic_tp_pct: float = field(default_factory=lambda: config.TAKE_PROFIT_PCT)
    # Inventory limit tracks order_size_btc: pauses quoting when |inventory| >= this.
    max_inventory_btc: float = field(default_factory=lambda: config.MAX_INVENTORY_BTC)
    # Circuit breaker threshold: 2 stop-losses worth, scales with position size.
    max_daily_loss_usd: float = field(default_factory=lambda: config.MAX_DAILY_LOSS_USD)

    # --- Bot lifecycle ---
    status: BotStatus = BotStatus.INITIALIZING
    last_signal_ms: int = 0
    ws_reconnect_count: int = 0

    # --- Exchange-side stop-loss and take-profit resting orders ---
    sl_oid: Optional[int] = None
    tp_oid: Optional[int] = None

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
                f"{self.daily_pnl_usd + closed_pnl:.4f}",
            ])

    def record_fill(self, is_buy: bool, fill_px: float, fill_sz: float, closed_pnl: float) -> None:
        signed_sz = fill_sz if is_buy else -fill_sz

        # Write closing fills to persistent trade journal before state changes
        if abs(closed_pnl) > 1e-9 and self.entry_price is not None and self.inventory_btc != 0.0:
            direction = "LONG" if self.inventory_btc > 0 else "SHORT"
            self._append_trade_journal(direction, self.entry_price, fill_px, fill_sz, closed_pnl)

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

        self.inventory_btc += signed_sz
        if abs(self.inventory_btc) < 1e-8:
            self.inventory_btc = 0.0
            self.entry_price = None

        self.daily_pnl_usd += closed_pnl

        if is_buy:
            self.total_buys_filled += 1
        else:
            self.total_sells_filled += 1
        self.total_orders_filled += 1

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
        return (
            f"status={self.status.value} "
            f"inv={self.inventory_btc:+.4f}BTC "
            f"entry={self.entry_price or 0:.2f} "
            f"mid={mid_str} "
            f"unrealPnL={self.unrealized_pnl_usd():+.2f}$ "
            f"realPnL={self.daily_pnl_usd:+.2f}$ "
            f"fills={self.total_orders_filled} "
            f"open_orders={len(self.open_orders)}"
        )
