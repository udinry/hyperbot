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


def test_regime_filter_blocks_long_below_sma():
    # uptrend for 200d then a sharp drop below the 150d SMA on the last days
    closes = _series(50_000, 100_000, 200) + _series(100_000, 60_000, 30)
    # ensemble votes may be mixed, but regime gate must force flat
    if tb.REGIME_FILTER_DAYS > 0:
        assert tb.regime_ok(closes) is False
        assert tb.ensemble_fraction(closes) == 0.0


def test_regime_ok_true_in_uptrend():
    closes = _series(50_000, 100_000, 200)
    assert tb.regime_ok(closes) is True


def test_regime_filter_disabled_when_zero(monkeypatch):
    monkeypatch.setattr(tb, "REGIME_FILTER_DAYS", 0)
    closes = _series(100_000, 60_000, 200)  # downtrend
    assert tb.regime_ok(closes) is True   # filter off => always ok


# ---- forward_test harness ----
def test_forward_test_compounds_and_dedups(tmp_path, monkeypatch):
    import forward_test as ft
    monkeypatch.setattr(ft, "LOG", tmp_path / "fwd.csv")
    closes = {"v": _series(50_000, 100_000, 200)}   # uptrend -> long
    monkeypatch.setattr(ft.trend_bot, "fetch_daily_closes",
                        lambda coin, days=400: closes["v"])
    fake_day = {"v": "2026-01-01"}

    import datetime as _real_dt
    _RealDateTime = _real_dt.datetime   # capture before patching the module
    _TZ_UTC = _real_dt.timezone.utc

    class _FakeDT:
        @staticmethod
        def now(tz=None):
            return _RealDateTime.strptime(fake_day["v"], "%Y-%m-%d").replace(
                tzinfo=_TZ_UTC)
    monkeypatch.setattr(ft.dt, "datetime", _FakeDT)

    ft.run_once(["BTC"])
    ft.run_once(["BTC"])              # same day -> dedup, still 1 data row
    rows = ft._load_rows()
    assert len(rows) == 1

    # next day: price +1%, was fully long (target 1.0 in calm uptrend)
    fake_day["v"] = "2026-01-02"
    closes["v"] = [c for c in closes["v"]] + [closes["v"][-1] * 1.01]
    ft.run_once(["BTC"])
    rows = ft._load_rows()
    assert len(rows) == 2
    prev_target = float(rows[0]["target_fraction"])
    strat = float(rows[1]["strategy_day_return_pct"]) / 100
    eq = float(rows[1]["equity"])
    # strat return ≈ prev_target * 1% - funding drag (no target change cost if same)
    assert strat == pytest.approx(prev_target * 0.01
                                  - ft.FUND_DRAG_DAILY * prev_target, abs=2e-4)
    assert eq == pytest.approx(1000 * (1 + strat), abs=0.05)


def test_risk_parity_weights_inverse_vol_and_cap():
    # cap not binding: weights exactly inverse to vol
    w = tb.risk_parity_weights({"BTC": 0.40, "ETH": 0.60, "SOL": 0.80})
    assert w["BTC"] == pytest.approx(1.5 * w["ETH"], rel=1e-6)   # 0.60/0.40
    assert w["BTC"] == pytest.approx(2.0 * w["SOL"], rel=1e-6)   # 0.80/0.40
    assert sum(w.values()) == pytest.approx(1.0)
    # extreme vol gap: cap binds and excess redistributes, cap still respected
    w = tb.risk_parity_weights({"BTC": 0.10, "ETH": 5.0, "SOL": 5.0})
    assert w["BTC"] == pytest.approx(0.5)
    assert max(w.values()) <= 0.5 + 1e-9
    assert sum(w.values()) == pytest.approx(1.0)
    assert tb.risk_parity_weights({}) == {}
