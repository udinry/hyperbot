"""Tests for the trend-ensemble signal math in trend_bot.py."""
import math

import pytest

import trend_bot as tb


def _series(start, end, n):
    """Linear price path from start to end over n days."""
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def test_ensemble_full_long_in_uptrend():
    closes = _series(50_000, 100_000, 200)
    assert tb.ensemble_fraction(closes) == 1.0


def test_ensemble_flat_in_downtrend():
    closes = _series(100_000, 50_000, 200)
    assert tb.ensemble_fraction(closes) == 0.0


def test_ensemble_insufficient_history_is_flat():
    assert tb.ensemble_fraction([100.0] * 10) == 0.0


def test_ensemble_fraction_is_quantized():
    # Whatever the regime, fraction must be one of the 5 vote levels.
    closes = _series(50_000, 100_000, 120) + _series(100_000, 80_000, 80)
    assert tb.ensemble_fraction(closes) in (0.0, 0.25, 0.5, 0.75, 1.0)


def test_vol_scale_caps_at_one_in_calm_markets():
    closes = [100_000 * (1 + 0.0001) ** i for i in range(60)]  # ~0.2% ann vol
    assert tb.vol_scale(closes) == 1.0


def test_vol_scale_shrinks_in_violent_markets():
    closes = [100_000.0]
    for i in range(59):
        closes.append(closes[-1] * (1.06 if i % 2 == 0 else 0.94))  # ~115% ann vol
    s = tb.vol_scale(closes)
    assert 0 < s < 0.5


def test_target_position_lot_rounded():
    closes = _series(50_000, 100_000, 200)
    tgt = tb.target_position_btc(closes, equity_usd=1000, price=100_000)
    assert abs(tgt / tb.LOT_BTC - round(tgt / tb.LOT_BTC)) < 1e-9
    assert tgt > 0


def test_should_rebalance_threshold():
    full = 0.010
    assert not tb.should_rebalance(0.010, 0.0105, full)   # sub-lot delta
    assert not tb.should_rebalance(0.010, 0.011, full)    # 1 lot but <15% of full
    assert tb.should_rebalance(0.010, 0.013, full)        # 3 lots, 30% of full
    assert tb.should_rebalance(0.0, 0.010, full)          # full entry
    assert tb.should_rebalance(0.010, 0.0, full)          # full exit
