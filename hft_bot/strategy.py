"""
OFI (Order Flow Imbalance) + TFI (Trade Flow Imbalance) signal engine — v3.

Changes from v1
---------------
1. Trade Flow Imbalance (TFI) confirmation gate: subscribes to the `trades`
   WebSocket channel (actual executed trades, not just book quotes). A BUY
   signal now requires BOTH positive L2 OFI (book pressure) AND positive TFI
   (real buy volume) over the window. This is the primary profitability fix:
   on a thin market a single bot's quote change moves OFI with no real
   conviction behind it. TFI from actual fills filters those out.

2. Signal persistence gate: OFI must exceed threshold for OFI_PERSISTENCE_TICKS
   consecutive book updates. Set to 1 on testnet (tick ~570ms), 3+ on mainnet.

3. Spread liquidity filter: signals suppressed when spread > MAX_SPREAD_BPS.
   Wide spreads indicate thin-book conditions where OFI is unreliable.

4. Anti-flap protection: opposite direction blocked for SIGNAL_COOLDOWN_MS * 2
   after each signal, eliminating back-to-back buy/sell that destroy P&L.

5. 800ms cooldown (up from 200ms in v1): v1 generated 30 signals in 3 minutes
   with many contradictory pairs within 1-2 seconds. 800ms allows 2-4 quality
   signals per minute which is optimal for the 0.001 BTC position size.

OFI core algorithm (Cont-Kukanov-O'Hara 2014)
----------------------------------------------
  For bid level i:
    price up   ->  +size_new    (bid queue advanced, buying pressure)
    price down ->  -size_old    (bid queue retreated, selling pressure)
    price same ->  size_new - size_old

  For ask level i:
    price down ->  +size_new    (ask queue advanced, buying pressure)
    price up   ->  -size_old    (ask queue retreated, selling pressure)
    price same ->  -(size_new - size_old)

  OFI_delta = sum_bid_deltas - sum_ask_deltas
"""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, List, Optional, Tuple

import config
from state import BotState, Level, OrderBook

logger = logging.getLogger("ofi_strategy")

_MIN_DEPTH = 1e-6

# Gate suppression counters — reset and logged periodically by stats_logger.
_gate_counts: dict[str, int] = {}


def _suppress(gate: str) -> None:
    _gate_counts[gate] = _gate_counts.get(gate, 0) + 1


def get_and_reset_gate_stats() -> dict[str, int]:
    """Return current suppression counts and reset to zero."""
    counts = dict(_gate_counts)
    _gate_counts.clear()
    return counts


# ---------------------------------------------------------------------------
# Level-OFI helpers
# ---------------------------------------------------------------------------

def _level_ofi_bid(prev: Optional[Level], curr: Optional[Level]) -> float:
    if prev is None and curr is None:
        return 0.0
    if prev is None:
        return curr.size  # type: ignore[union-attr]
    if curr is None:
        return -prev.size
    if curr.price > prev.price:
        return curr.size
    if curr.price < prev.price:
        return -prev.size
    return curr.size - prev.size


def _level_ofi_ask(prev: Optional[Level], curr: Optional[Level]) -> float:
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
    return -(curr.size - prev.size)


def _book_depth(bids: List[Level], asks: List[Level], n: int) -> float:
    return sum(l.size for l in bids[:n]) + sum(l.size for l in asks[:n])


# ---------------------------------------------------------------------------
# Public OFI computation
# ---------------------------------------------------------------------------

def compute_ofi_delta(
    prev_bids: List[Level],
    prev_asks: List[Level],
    curr_bids: List[Level],
    curr_asks: List[Level],
    n_levels: int,
) -> float:
    n = max(1, min(n_levels, max(len(curr_bids), 1), max(len(curr_asks), 1)))
    ofi = 0.0
    for i in range(n):
        p_bid = prev_bids[i] if i < len(prev_bids) else None
        c_bid = curr_bids[i] if i < len(curr_bids) else None
        p_ask = prev_asks[i] if i < len(prev_asks) else None
        c_ask = curr_asks[i] if i < len(curr_asks) else None
        ofi += _level_ofi_bid(p_bid, c_bid)
        ofi += _level_ofi_ask(p_ask, c_ask)
    return ofi


def _now_ms() -> int:
    return int(time.monotonic_ns() // 1_000_000)


def compute_microprice(book) -> Optional[float]:
    """Weighted mid price using opposite-side sizes as weights.
    microprice > mid → bid-heavy book → buy pressure confirmed.
    microprice < mid → ask-heavy book → sell pressure confirmed."""
    if not book.bids or not book.asks:
        return book.mid_price()
    bid_px, bid_sz = book.bids[0].price, book.bids[0].size
    ask_px, ask_sz = book.asks[0].price, book.asks[0].size
    total = bid_sz + ask_sz
    if total < 1e-9:
        return (bid_px + ask_px) / 2
    return (ask_px * bid_sz + bid_px * ask_sz) / total


def compute_queue_imbalance(book) -> Optional[float]:
    """L1 queue imbalance: bid_sz / (bid_sz + ask_sz).
    > 0.5 → bid-heavy (buy pressure); < 0.5 → ask-heavy (sell pressure)."""
    if not book.bids or not book.asks:
        return None
    bid_sz = book.bids[0].size
    ask_sz = book.asks[0].size
    total = bid_sz + ask_sz
    if total < 1e-9:
        return None
    return bid_sz / total


def compute_price_trend(state: BotState) -> Optional[float]:
    """
    Returns the price change (in $) over the last PRICE_TREND_WINDOW_MS.
    Positive → price rising; Negative → price falling; None → insufficient history.
    Used as a macro regime gate before acting on OFI/TFI micro-signals.
    """
    trend_ms = getattr(config, "PRICE_TREND_WINDOW_MS", 3000)
    now_ms   = _now_ms()
    cutoff   = now_ms - trend_ms

    # Prune history outside the window.
    while state.mid_history and state.mid_history[0][0] < cutoff:
        state.mid_history.popleft()

    if len(state.mid_history) < 2:
        return None

    oldest_mid = state.mid_history[0][1]
    newest_mid = state.mid_history[-1][1]
    return newest_mid - oldest_mid


def compute_dynamic_tp_pct(state: BotState) -> float:
    """ATR-adaptive TP: sample 1-min price changes from mid_history_5m.
    ATR < $30 -> 0.20%  |  $30-50 -> 0.30%  |  >$50 -> 0.35%
    config.TAKE_PROFIT_PCT acts as a floor — .env override is always respected."""
    base = config.TAKE_PROFIT_PCT
    history = state.mid_history_5m
    if len(history) < 200:
        return base
    now_ms = history[-1][0]
    one_min = 60_000
    sampled, j = [], 0
    for target in [now_ms - i * one_min for i in range(5, -1, -1)]:
        while j < len(history) - 1 and history[j][0] < target:
            j += 1
        sampled.append(history[j][1])
    if len(sampled) < 3:
        return base
    changes = [abs(sampled[i] - sampled[i - 1]) for i in range(1, len(sampled))]
    atr = sum(changes) / len(changes)
    if atr < 30:
        atr_tp = 0.0020
    elif atr < 50:
        atr_tp = 0.0030
    else:
        atr_tp = 0.0035
    return max(base, atr_tp)


def compute_5min_trend(state: BotState) -> Optional[float]:
    """Price change ($) over ~5 min using the capped mid_history_5m deque."""
    history = state.mid_history_5m
    if len(history) < 100:
        return None
    if _now_ms() - history[0][0] < 60_000:
        return None
    return history[-1][1] - history[0][1]


def compute_atr(state: BotState) -> Optional[float]:
    """1-min ATR from mid_history_5m: avg absolute price change per minute, last 10 min."""
    history = state.mid_history_5m
    if len(history) < 200:
        return None
    now_ms = history[-1][0]
    one_min = 60_000
    sampled, j = [], 0
    for target in [now_ms - i * one_min for i in range(10, -1, -1)]:
        while j < len(history) - 1 and history[j][0] < target:
            j += 1
        sampled.append(history[j][1])
    if len(sampled) < 3:
        return None
    changes = [abs(sampled[i] - sampled[i - 1]) for i in range(1, len(sampled))]
    return sum(changes) / len(changes)


def process_book_update(state: BotState, new_book: OrderBook) -> Optional[float]:
    """
    Ingest a new L2 snapshot, update the rolling OFI window, and return the
    spot-depth-normalised OFI signal in [-1, +1].  Returns None on the first tick.
    """
    now_ms = _now_ms()

    # Track mid for trend gate.
    mid = new_book.mid_price()
    if mid is not None:
        state.mid_history.append((now_ms, mid))
        state.mid_history_5m.append((now_ms, mid))

    if state.prev_bids or state.prev_asks:
        delta = compute_ofi_delta(
            state.prev_bids, state.prev_asks,
            new_book.bids,  new_book.asks,
            config.OFI_LEVELS,
        )
        state.ofi_window.append((now_ms, delta))

    state.prev_bids = list(new_book.bids[: config.OFI_LEVELS])
    state.prev_asks = list(new_book.asks[: config.OFI_LEVELS])
    state.book = new_book

    cutoff_ms = now_ms - config.OFI_WINDOW_MS
    while state.ofi_window and state.ofi_window[0][0] < cutoff_ms:
        state.ofi_window.popleft()

    if not state.ofi_window:
        return None

    raw_ofi    = sum(d for _, d in state.ofi_window)
    spot_depth = max(_book_depth(new_book.bids, new_book.asks, config.OFI_LEVELS), _MIN_DEPTH)
    normalised = max(-1.0, min(1.0, raw_ofi / spot_depth))

    logger.debug(
        "OFI raw=%.4f norm=%.4f depth=%.4f entries=%d",
        raw_ofi, normalised, spot_depth, len(state.ofi_window),
    )
    return normalised


# ---------------------------------------------------------------------------
# Trade Flow Imbalance (TFI)
# ---------------------------------------------------------------------------

def ingest_trade(state: BotState, trade: dict) -> None:
    """Add an executed trade to the rolling window with price for VWAP computation."""
    try:
        side   = trade.get("side", "")
        sz     = float(trade.get("sz", 0))
        px     = float(trade.get("px", 0))
        ts     = int(trade.get("time", _now_ms()))
        signed = sz if side == "B" else -sz
        state.trade_window.append((ts, signed, px))
    except Exception:
        pass


def _prune_trade_window(state: BotState) -> None:
    now_ms = _now_ms()
    cutoff = now_ms - config.OFI_WINDOW_MS
    while state.trade_window and state.trade_window[0][0] < cutoff:
        state.trade_window.popleft()


def compute_tfi(state: BotState) -> Optional[float]:
    """Normalised Trade Flow Imbalance in [-1, +1] over OFI_WINDOW_MS."""
    _prune_trade_window(state)
    if not state.trade_window:
        return None
    buy_vol  = sum(v for _, v, _ in state.trade_window if v > 0)
    sell_vol = sum(-v for _, v, _ in state.trade_window if v < 0)
    total    = buy_vol + sell_vol
    if total < 1e-9:
        return None
    return (buy_vol - sell_vol) / total


def compute_vwap_deviation(state: BotState) -> Optional[float]:
    """VWAP of recent trades minus current mid price.
    Positive → recent trades above mid → buy pressure.
    Negative → recent trades below mid → sell pressure."""
    _prune_trade_window(state)
    mid = state.book.mid_price()
    if not state.trade_window or mid is None:
        return None
    total_vol = sum(abs(v) for _, v, _ in state.trade_window)
    if total_vol < 1e-9:
        return None
    vwap = sum(abs(v) * px for _, v, px in state.trade_window) / total_vol
    return vwap - mid


# ---------------------------------------------------------------------------
# Signal evaluation with all quality gates
# ---------------------------------------------------------------------------

def evaluate_signal(state: BotState, ofi: float) -> Optional[str]:
    """
    Applies quality gates before emitting a BUY or SELL signal:
      1. Cooldown          — minimum ms between any two signals
      2. Spread filter     — suppress if spread > MAX_SPREAD_BPS of mid
      3. TFI confirmation  — actual trade volume must agree with OFI direction
      4. Trend gate        — BUY suppressed in falling price regime, SELL in rising
      5. Persistence       — OFI must exceed threshold on N consecutive ticks
      6. Anti-flap         — opposite direction blocked for 2x cooldown period

    Returns 'buy', 'sell', or None.
    """
    now_ms = _now_ms()

    # 1. Global cooldown
    if now_ms - state.last_signal_ms < config.SIGNAL_COOLDOWN_MS:
        return None

    # 1.5. Time-of-day gate — block during low-activity UTC hours (e.g. EU lull)
    block_start = getattr(config, "TRADE_BLOCK_UTC_START", -1)
    block_end   = getattr(config, "TRADE_BLOCK_UTC_END", -1)
    if block_start >= 0 and block_end >= 0 and block_start < block_end:
        hour_utc = datetime.now(timezone.utc).hour
        if block_start <= hour_utc < block_end:
            _suppress("time_block")
            return None

    # 2. Spread liquidity filter
    spread = state.book.spread()
    mid    = state.book.mid_price()
    if spread is not None and mid and mid > 0:
        spread_bps = spread / mid * 10_000
        if spread_bps > config.MAX_SPREAD_BPS:
            _suppress("spread")
            return None

    # 2.5. ATR minimum gate — require minimum realized volatility to trade.
    # Prevents entries during dead-flat markets where TP is nearly unreachable.
    atr = compute_atr(state)
    atr_min = getattr(config, "ATR_MIN_TRADE_USD", 0.0)
    if atr is not None and atr_min > 0 and atr < atr_min:
        _suppress("atr")
        return None

    # Count OFI threshold crossings that passed pre-gate filters (cooldown/spread/ATR).
    # These are the candidates that enter the quality-gate funnel.
    if ofi >= config.OFI_BUY_THRESHOLD:
        _suppress("candidate_buy")
    elif ofi <= config.OFI_SELL_THRESHOLD:
        _suppress("candidate_sell")

    # OFI exhaustion gate: suppress if strong opposite OFI in last N ticks before signal.
    # Catches local price extremes where OFI briefly spikes but immediately reverts.
    _ex_ticks = getattr(config, "OFI_CONSISTENCY_TICKS", 5)
    _ex_thresh = getattr(config, "OFI_CONSISTENCY_THRESHOLD", 0.80)
    _recent = list(state.ofi_recent)[-_ex_ticks:] if len(state.ofi_recent) >= _ex_ticks else []
    if _recent:
        if ofi <= config.OFI_SELL_THRESHOLD and max(_recent) > _ex_thresh:
            _suppress("ofi_exhaustion_sell")
            return None
        if ofi >= config.OFI_BUY_THRESHOLD and min(_recent) < -_ex_thresh:
            _suppress("ofi_exhaustion_buy")
            return None

    # 2.6. Microprice gate — instantaneous book-pressure confirmation.
    # microprice > mid = bid-heavy book = buy pressure. Contradicting microprice
    # means OFI signal and current book snapshot disagree; skip.
    mp = compute_microprice(state.book)
    _mid = state.book.mid_price()
    if mp is not None and _mid is not None and _mid > 0:
        if ofi >= config.OFI_BUY_THRESHOLD and mp < _mid:
            _suppress("microprice_buy")
            return None
        if ofi <= config.OFI_SELL_THRESHOLD and mp > _mid:
            _suppress("microprice_sell")
            return None

    # 2.7. Queue imbalance gate — L1 bid/ask size ratio confirmation.
    # Requires book to be directionally skewed (>55% one side) to avoid 50/50 entries.
    qi = compute_queue_imbalance(state.book)
    qi_thresh = getattr(config, "QUEUE_IMBAL_THRESHOLD", 0.55)
    if qi is not None:
        if ofi >= config.OFI_BUY_THRESHOLD and qi < (1.0 - qi_thresh):
            _suppress("queue_imbal_buy")
            return None
        if ofi <= config.OFI_SELL_THRESHOLD and qi > qi_thresh:
            _suppress("queue_imbal_sell")
            return None

    # 2.8. VWAP deviation gate — recent trades must be on the signal side of mid.
    # VWAP > mid means buyers paid above mid (buy pressure). Graceful: skip if no data.
    vwap_dev = compute_vwap_deviation(state)
    if vwap_dev is not None:
        if ofi >= config.OFI_BUY_THRESHOLD and vwap_dev < 0:
            _suppress("vwap_buy")
            return None
        if ofi <= config.OFI_SELL_THRESHOLD and vwap_dev > 0:
            _suppress("vwap_sell")
            return None

    # 3. TFI confirmation gate.
    # When no trades have been seen yet (startup or dead market), skip this
    # check and rely on OFI alone (graceful degradation).
    tfi = compute_tfi(state)
    min_tfi = getattr(config, "MIN_TFI_STRENGTH", 0.0)
    if tfi is not None:
        ofi_wants_buy  = ofi >= config.OFI_BUY_THRESHOLD
        ofi_wants_sell = ofi <= config.OFI_SELL_THRESHOLD
        if ofi_wants_buy  and tfi <= min_tfi:
            _suppress("tfi_buy")
            return None
        if ofi_wants_sell and tfi >= -min_tfi:
            _suppress("tfi_sell")
            return None

    # 4. Short-term price trend gate.
    # Suppress BUY if price has been falling, and SELL if price has been rising.
    # This aligns micro OFI signals with the macro regime over the last few seconds.
    trend = compute_price_trend(state)
    if trend is not None:
        ofi_wants_buy_trend  = ofi >= config.OFI_BUY_THRESHOLD
        ofi_wants_sell_trend = ofi <= config.OFI_SELL_THRESHOLD
        if ofi_wants_buy_trend  and trend < 0:
            _suppress("trend_buy")
            return None
        if ofi_wants_sell_trend and trend > 0:
            _suppress("trend_sell")
            return None

    # 5a. 5-min momentum gate: OFI impulse must be backed by 5-min price trend.
    trend_5m = compute_5min_trend(state)
    if trend_5m is not None and mid:
        min_trend_5m = getattr(config, "TREND_5MIN_PCT", 0.001) * mid
        if ofi >= config.OFI_BUY_THRESHOLD and trend_5m < min_trend_5m:
            _suppress("trend5m_buy")
            return None
        if ofi <= config.OFI_SELL_THRESHOLD and trend_5m > -min_trend_5m:
            _suppress("trend5m_sell")
            return None

    # 4.5. Funding rate bias gate.
    # High positive funding = longs overcrowded = add friction to BUY signals.
    # Requires 0.10 higher OFI to enter long when funding is extreme.
    fr = getattr(state, "funding_rate", 0.0)
    fr_thresh = getattr(config, "FUNDING_BIAS_THRESHOLD", 0.00015)
    if fr != 0.0 and fr_thresh > 0:
        if ofi >= config.OFI_BUY_THRESHOLD and fr > fr_thresh:
            if ofi < config.OFI_BUY_THRESHOLD + 0.10:
                _suppress("funding_buy")
                return None
        elif ofi <= config.OFI_SELL_THRESHOLD and fr < -fr_thresh:
            if ofi > config.OFI_SELL_THRESHOLD - 0.10:
                _suppress("funding_sell")
                return None

    # 5. Persistence counter
    if not hasattr(state, "_persist_buy"):
        state._persist_buy  = 0  # type: ignore[attr-defined]
        state._persist_sell = 0  # type: ignore[attr-defined]

    candidate = None
    if ofi >= config.OFI_BUY_THRESHOLD and state.can_buy():
        state._persist_buy  += 1  # type: ignore[attr-defined]
        state._persist_sell  = 0  # type: ignore[attr-defined]
        if state._persist_buy >= config.OFI_PERSISTENCE_TICKS:  # type: ignore[attr-defined]
            candidate = "buy"
    elif ofi <= config.OFI_SELL_THRESHOLD and state.can_sell():
        state._persist_sell += 1  # type: ignore[attr-defined]
        state._persist_buy   = 0  # type: ignore[attr-defined]
        if state._persist_sell >= config.OFI_PERSISTENCE_TICKS:  # type: ignore[attr-defined]
            candidate = "sell"
    else:
        state._persist_buy  = 0  # type: ignore[attr-defined]
        state._persist_sell = 0  # type: ignore[attr-defined]

    if candidate is None:
        return None

    # 6. Anti-flap: block opposite direction for 2x cooldown after last signal
    last_dir = getattr(state, "_last_signal_dir", None)
    if last_dir is not None and last_dir != candidate:
        if now_ms - state.last_signal_ms < config.SIGNAL_COOLDOWN_MS * 2:
            _suppress("anti_flap")
            return None

    state.last_signal_ms = now_ms
    state._last_signal_dir  = candidate  # type: ignore[attr-defined]
    state._persist_buy  = 0  # type: ignore[attr-defined]
    state._persist_sell = 0  # type: ignore[attr-defined]
    _suppress("signal_fired")

    tfi_str  = f"{tfi:+.3f}"  if tfi      is not None else "N/A"
    qi_str   = f"{qi:.3f}"   if qi       is not None else "N/A"
    vwap_str = f"{vwap_dev:+.2f}$" if vwap_dev is not None else "N/A"
    fr_str   = f"{fr*100:.4f}%" if fr != 0.0 else "0"
    if candidate == "buy":
        logger.info("BUY  signal | OFI=%+.4f TFI=%s QI=%s VWAP=%s FR=%s spread=%.2f$",
                    ofi, tfi_str, qi_str, vwap_str, fr_str, spread or 0)
    else:
        logger.info("SELL signal | OFI=%+.4f TFI=%s QI=%s VWAP=%s FR=%s spread=%.2f$",
                    ofi, tfi_str, qi_str, vwap_str, fr_str, spread or 0)

    return candidate


def evaluate_exit_signal(state: BotState, ofi: float) -> Optional[str]:
    """
    Simplified exit check used when holding a position (paused_inventory).
    Lower OFI threshold, no trend gate, no anti-flap — just OFI+TFI agreement.
    Returns 'sell' (close long) or 'buy' (close short), or None.
    """
    if abs(state.inventory_btc) < 1e-8:
        return None

    now_ms = _now_ms()
    if now_ms - state.last_exit_ms < config.EXIT_COOLDOWN_MS:
        return None

    tfi = compute_tfi(state)
    min_tfi = getattr(config, "EXIT_MIN_TFI_STRENGTH", config.MIN_TFI_STRENGTH)
    threshold = config.EXIT_OFI_THRESHOLD

    if state.inventory_btc > 0:  # long — need SELL to close
        if ofi <= -threshold:
            if tfi is not None and tfi > -min_tfi:
                return None
            state.last_exit_ms = now_ms
            tfi_str = f"{tfi:+.3f}" if tfi is not None else "N/A"
            logger.info("EXIT SELL | OFI=%+.4f TFI=%s (closing LONG %.4f BTC)", ofi, tfi_str, state.inventory_btc)
            return "sell"

    elif state.inventory_btc < 0:  # short — need BUY to close
        if ofi >= threshold:
            if tfi is not None and tfi < min_tfi:
                return None
            state.last_exit_ms = now_ms
            tfi_str = f"{tfi:+.3f}" if tfi is not None else "N/A"
            logger.info("EXIT BUY | OFI=%+.4f TFI=%s (closing SHORT %.4f BTC)", ofi, tfi_str, abs(state.inventory_btc))
            return "buy"

    return None
