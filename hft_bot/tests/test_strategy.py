"""Unit tests for the OFI/TFI signal engine — especially the clock-domain fix:
trade timestamps must come from clock.now_ms(), never from the exchange's
epoch-ms 'time' field, or the rolling window never prunes."""
import math

import pytest

import clock
import config
import strategy
from state import BotState, Level, OrderBook


def _book(bid_px=100_000.0, bid_sz=1.0, ask_px=100_001.0, ask_sz=1.0) -> OrderBook:
    return OrderBook(
        bids=[Level(bid_px, bid_sz)],
        asks=[Level(ask_px, ask_sz)],
        timestamp_ms=0,
    )


# ---------------------------------------------------------------------------
# OFI level math (Cont–Kukanov–O'Hara)
# ---------------------------------------------------------------------------

def test_ofi_bid_price_up_adds_new_size():
    assert strategy._level_ofi_bid(Level(100, 2.0), Level(101, 3.0)) == 3.0

def test_ofi_bid_price_down_subtracts_old_size():
    assert strategy._level_ofi_bid(Level(100, 2.0), Level(99, 3.0)) == -2.0

def test_ofi_bid_same_price_takes_size_delta():
    assert strategy._level_ofi_bid(Level(100, 2.0), Level(100, 3.5)) == 1.5

def test_ofi_ask_price_down_adds_new_size():
    assert strategy._level_ofi_ask(Level(100, 2.0), Level(99, 3.0)) == 3.0

def test_ofi_ask_price_up_subtracts_old_size():
    assert strategy._level_ofi_ask(Level(100, 2.0), Level(101, 3.0)) == -2.0

def test_ofi_ask_same_price_takes_negative_size_delta():
    assert strategy._level_ofi_ask(Level(100, 2.0), Level(100, 3.5)) == -1.5


# ---------------------------------------------------------------------------
# Trade window clock-domain fix
# ---------------------------------------------------------------------------

def test_ingest_trade_uses_local_clock_not_exchange_time(fake_clock):
    state = BotState()
    # Exchange sends epoch ms (~1.78e12) — far larger than any monotonic value.
    strategy.ingest_trade(state, {"side": "B", "sz": "1.0", "px": "100000",
                                  "time": 1_780_000_000_000})
    ts, _, _ = state.trade_window.entries[0]
    assert ts == fake_clock.ms, "trade must be stamped with the local clock"


def test_trade_window_prunes_after_window_elapses(fake_clock):
    state = BotState()
    strategy.ingest_trade(state, {"side": "B", "sz": "1.0", "px": "100000",
                                  "time": 1_780_000_000_000})
    assert strategy.compute_tfi(state) == 1.0
    # Advance beyond OFI_WINDOW_MS: the trade must drop out of the window.
    fake_clock.advance(config.OFI_WINDOW_MS + 1)
    assert strategy.compute_tfi(state) is None, \
        "stale trades must be pruned (the original v3 TFI bug kept them forever)"


def test_tfi_value(fake_clock):
    state = BotState()
    strategy.ingest_trade(state, {"side": "B", "sz": "2.0", "px": "100000"})
    strategy.ingest_trade(state, {"side": "A", "sz": "1.0", "px": "100000"})
    assert strategy.compute_tfi(state) == pytest.approx((2.0 - 1.0) / 3.0)


def test_vwap_deviation(fake_clock):
    state = BotState()
    state.book = _book(bid_px=99_999.5, ask_px=100_000.5)   # mid = 100000
    strategy.ingest_trade(state, {"side": "B", "sz": "1.0", "px": "100010"})
    strategy.ingest_trade(state, {"side": "A", "sz": "1.0", "px": "100030"})
    assert strategy.compute_vwap_deviation(state) == pytest.approx(20.0)


def test_malformed_trade_does_not_crash(fake_clock):
    state = BotState()
    strategy.ingest_trade(state, {"side": "B", "sz": "garbage", "px": "x"})
    assert len(state.trade_window) == 0


# ---------------------------------------------------------------------------
# process_book_update — OFI normalisation and window pruning
# ---------------------------------------------------------------------------

def test_process_book_update_first_tick_returns_none(fake_clock):
    state = BotState()
    assert strategy.process_book_update(state, _book()) is None


def test_process_book_update_buy_pressure_positive_ofi(fake_clock):
    state = BotState()
    strategy.process_book_update(state, _book(bid_sz=1.0, ask_sz=1.0))
    fake_clock.advance(50)
    # Bid size grows, ask size shrinks: buying pressure.
    ofi = strategy.process_book_update(state, _book(bid_sz=2.0, ask_sz=0.5))
    assert ofi is not None and ofi > 0


def test_ofi_window_prunes_with_clock(fake_clock):
    state = BotState()
    strategy.process_book_update(state, _book(bid_sz=1.0))
    fake_clock.advance(50)
    strategy.process_book_update(state, _book(bid_sz=2.0))
    assert len(state.ofi_window) == 1
    fake_clock.advance(config.OFI_WINDOW_MS + 1)
    strategy.process_book_update(state, _book(bid_sz=2.0))
    # the old delta is outside the window; only the newest remains
    assert len(state.ofi_window) <= 1


# ---------------------------------------------------------------------------
# evaluate_signal gates
# ---------------------------------------------------------------------------

@pytest.fixture
def permissive_config(monkeypatch):
    """Disable every optional gate so individual gates can be tested."""
    monkeypatch.setattr(config, "SIGNAL_COOLDOWN_MS", 100)
    monkeypatch.setattr(config, "OFI_BUY_THRESHOLD", 0.5)
    monkeypatch.setattr(config, "OFI_SELL_THRESHOLD", -0.5)
    monkeypatch.setattr(config, "OFI_PERSISTENCE_TICKS", 1)
    monkeypatch.setattr(config, "MIN_TFI_STRENGTH", 0.0)
    monkeypatch.setattr(config, "MAX_SPREAD_BPS", 9999.0)
    monkeypatch.setattr(config, "ATR_MIN_TRADE_USD", 0.0)
    monkeypatch.setattr(config, "ATR_MAX_TRADE_USD", 0.0)
    monkeypatch.setattr(config, "POST_SL_COOLDOWN_MS", 0)
    monkeypatch.setattr(config, "TRADE_BLOCK_UTC_START", -1)
    monkeypatch.setattr(config, "TRADE_BLOCK_UTC_END", -1)
    monkeypatch.setattr(config, "TREND_5MIN_PCT", 0.0)
    monkeypatch.setattr(config, "FUNDING_BIAS_THRESHOLD", 0.0)
    return config


def _running_state(fake_clock, bid_sz=5.0, ask_sz=1.0) -> BotState:
    """RUNNING state with a bid-heavy book (microprice favours BUY)."""
    state = BotState()
    state.set_running()
    state.book = _book(bid_sz=bid_sz, ask_sz=ask_sz)
    state.last_signal_ms = fake_clock.ms - 1_000_000
    return state


def test_signal_fires_on_strong_ofi(permissive_config, fake_clock):
    state = _running_state(fake_clock)
    assert strategy.evaluate_signal(state, 0.9) == "buy"


def test_cooldown_blocks_second_signal(permissive_config, fake_clock):
    state = _running_state(fake_clock)
    assert strategy.evaluate_signal(state, 0.9) == "buy"
    fake_clock.advance(10)   # inside cooldown
    assert strategy.evaluate_signal(state, 0.9) is None


def test_lockout_blocks_signal(permissive_config, fake_clock):
    state = _running_state(fake_clock)
    state.lockout_until_ms = fake_clock.ms + 5_000
    assert strategy.evaluate_signal(state, 0.9) is None
    fake_clock.advance(5_001)
    assert strategy.evaluate_signal(state, 0.9) == "buy"


def test_anti_flap_blocks_immediate_reversal(permissive_config, fake_clock):
    state = _running_state(fake_clock)
    assert strategy.evaluate_signal(state, 0.9) == "buy"
    # After cooldown but inside 2x anti-flap window: SELL must be suppressed.
    fake_clock.advance(config.SIGNAL_COOLDOWN_MS + 10)
    state.book = _book(bid_sz=1.0, ask_sz=5.0)   # ask-heavy so microprice favours SELL
    assert strategy.evaluate_signal(state, -0.9) is None


def test_persistence_requires_consecutive_ticks(permissive_config, fake_clock, monkeypatch):
    monkeypatch.setattr(config, "OFI_PERSISTENCE_TICKS", 2)
    state = _running_state(fake_clock)
    assert strategy.evaluate_signal(state, 0.9) is None      # tick 1 of 2
    assert strategy.evaluate_signal(state, 0.9) == "buy"     # tick 2 of 2


def _fill_atr_history(state: BotState, fake_clock, swing_usd: float):
    """11 minutes of mid history alternating ±swing_usd per minute."""
    px = 100_000.0
    for i in range(660):   # one entry per second for 11 min
        fake_clock.advance(1_000)
        px += (swing_usd / 60.0) * (1 if (i // 60) % 2 == 0 else -1)
        state.mid_history_5m.append((fake_clock.ms, px))


def test_atr_ceiling_gate_suppresses_in_spike_regime(permissive_config, fake_clock, monkeypatch):
    monkeypatch.setattr(config, "ATR_MAX_TRADE_USD", 50.0)
    state = _running_state(fake_clock)
    _fill_atr_history(state, fake_clock, swing_usd=300.0)   # ATR ~ $300/min
    state.last_signal_ms = fake_clock.ms - 1_000_000
    atr = strategy.compute_atr(state)
    assert atr is not None and atr > 50.0
    assert strategy.evaluate_signal(state, 0.9) is None


def test_atr_ceiling_gate_allows_calm_regime(permissive_config, fake_clock, monkeypatch):
    monkeypatch.setattr(config, "ATR_MAX_TRADE_USD", 50.0)
    monkeypatch.setattr(config, "TREND_5MIN_PCT", 0.0)
    state = _running_state(fake_clock)
    _fill_atr_history(state, fake_clock, swing_usd=10.0)    # ATR ~ $10/min
    state.last_signal_ms = fake_clock.ms - 1_000_000
    state.mid_history.clear()
    strategy.get_and_reset_gate_stats()   # isolate from previous tests
    strategy.evaluate_signal(state, 0.9)
    # the ATR ceiling must not be the suppressor in a calm regime
    assert "atr_high_vol" not in strategy._gate_counts
    strategy.get_and_reset_gate_stats()
