"""
OFI (Order Flow Imbalance) signal engine.

Algorithm
---------
For each incoming L2 book snapshot we compute a signed OFI delta per level
using the standard Cont-Kukanov-O'Hara (2014) formulation:

  For bid level i:
    - price_new > price_old  →  +size_new   (bid queue advanced upward)
    - price_new < price_old  →  -size_old   (bid queue retreated)
    - price_new == price_old →  size_new - size_old

  For ask level i:
    - price_new < price_old  →  +size_new   (ask queue advanced downward, buying pressure)
    - price_new > price_old  →  -size_old   (ask queue retreated)
    - price_new == price_old →  -(size_new - size_old)

  OFI_delta = sum_bid_deltas - sum_ask_deltas

The rolling OFI signal is the volume-normalised sum of all OFI deltas inside
the last OFI_WINDOW_MS milliseconds.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

import config
from state import BotState, Level, OrderBook

logger = logging.getLogger("ofi_strategy")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.monotonic_ns() // 1_000_000)


def _level_ofi_bid(prev: Optional[Level], curr: Optional[Level]) -> float:
    """OFI contribution from one bid level transition."""
    if prev is None and curr is None:
        return 0.0
    if prev is None:
        # Level appeared fresh → treat as full positive size
        return curr.size  # type: ignore[union-attr]
    if curr is None:
        # Level disappeared → treat as full negative withdrawal
        return -prev.size
    if curr.price > prev.price:
        return curr.size
    if curr.price < prev.price:
        return -prev.size
    return curr.size - prev.size   # same price, volume change


def _level_ofi_ask(prev: Optional[Level], curr: Optional[Level]) -> float:
    """OFI contribution from one ask level transition (sign convention: positive = buy pressure)."""
    if prev is None and curr is None:
        return 0.0
    if prev is None:
        return -curr.size  # type: ignore[union-attr]
    if curr is None:
        return prev.size
    if curr.price < prev.price:
        return curr.size
    if curr.price > prev.price:
        return -prev.size
    return -(curr.size - prev.size)   # same price, volume change (inverted for asks)


def _book_depth_volume(bids: List[Level], asks: List[Level], n: int) -> float:
    """Total notional (size) across top-n bid+ask levels for normalisation."""
    total = sum(l.size for l in bids[:n]) + sum(l.size for l in asks[:n])
    return total if total > 0 else 1.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_ofi_delta(
    prev_bids: List[Level],
    prev_asks: List[Level],
    curr_bids: List[Level],
    curr_asks: List[Level],
    n_levels: int,
) -> float:
    """
    Compute raw OFI delta between two consecutive book snapshots.

    Returns a signed float in (approximately) BTC units.
    Positive → buying pressure; Negative → selling pressure.
    """
    n = min(n_levels, len(curr_bids), len(curr_asks),
            max(len(prev_bids), 1), max(len(prev_asks), 1))
    n = max(n, 1)

    ofi = 0.0
    for i in range(n):
        p_bid = prev_bids[i] if i < len(prev_bids) else None
        c_bid = curr_bids[i] if i < len(curr_bids) else None
        p_ask = prev_asks[i] if i < len(prev_asks) else None
        c_ask = curr_asks[i] if i < len(curr_asks) else None

        ofi += _level_ofi_bid(p_bid, c_bid)
        ofi += _level_ofi_ask(p_ask, c_ask)

    return ofi


def process_book_update(state: BotState, new_book: OrderBook) -> Optional[float]:
    """
    Process a new L2 snapshot, update the OFI rolling window, and return the
    current normalised OFI signal ∈ [-1, +1].

    Returns None if the window is not yet populated (first tick).
    """
    now_ms = _now_ms()

    # --- Compute delta from previous snapshot ---
    if state.prev_bids or state.prev_asks:
        delta = compute_ofi_delta(
            state.prev_bids,
            state.prev_asks,
            new_book.bids,
            new_book.asks,
            config.OFI_LEVELS,
        )
        state.ofi_window.append((now_ms, delta))

    # --- Update previous snapshot ---
    state.prev_bids = list(new_book.bids[: config.OFI_LEVELS])
    state.prev_asks = list(new_book.asks[: config.OFI_LEVELS])
    state.book = new_book

    # --- Prune stale window entries ---
    cutoff_ms = now_ms - config.OFI_WINDOW_MS
    while state.ofi_window and state.ofi_window[0][0] < cutoff_ms:
        state.ofi_window.popleft()

    if not state.ofi_window:
        return None

    # --- Accumulate raw OFI inside window ---
    raw_ofi = sum(delta for _, delta in state.ofi_window)

    # --- Normalise by current book depth ---
    depth_vol = _book_depth_volume(new_book.bids, new_book.asks, config.OFI_LEVELS)
    normalised = raw_ofi / depth_vol

    # Clip to [-1, 1] to avoid occasional large spikes throwing off threshold logic.
    normalised = max(-1.0, min(1.0, normalised))

    logger.debug(
        "OFI raw=%.4f norm=%.4f depth=%.4f window_entries=%d",
        raw_ofi, normalised, depth_vol, len(state.ofi_window),
    )

    return normalised


def evaluate_signal(state: BotState, ofi: float) -> Optional[str]:
    """
    Compare normalised OFI against thresholds and apply cooldown.

    Returns 'buy', 'sell', or None.
    """
    now_ms = _now_ms()

    # Cooldown gate: suppress new signals if the last one was too recent.
    if now_ms - state.last_signal_ms < config.SIGNAL_COOLDOWN_MS:
        return None

    if ofi >= config.OFI_BUY_THRESHOLD and state.can_buy():
        state.last_signal_ms = now_ms
        logger.info("BUY signal  | OFI=%.4f ≥ threshold=%.4f", ofi, config.OFI_BUY_THRESHOLD)
        return "buy"

    if ofi <= config.OFI_SELL_THRESHOLD and state.can_sell():
        state.last_signal_ms = now_ms
        logger.info("SELL signal | OFI=%.4f ≤ threshold=%.4f", ofi, config.OFI_SELL_THRESHOLD)
        return "sell"

    return None
