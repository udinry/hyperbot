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


def test_drift_verdict_bands():
    import forward_test as ft
    assert "INSUFFICIENT" in ft.drift_verdict([0.001] * 5)
    assert "FLAT RECORD" in ft.drift_verdict([0.0] * 30)
    # consistent with ~21%/yr: small positive daily returns with noise
    import random
    random.seed(7)
    exp = (1 + 0.21) ** (1 / 365) - 1
    ok = [exp + random.gauss(0, 0.01) for _ in range(90)]
    assert "WITHIN EXPECTATION" in ft.drift_verdict(ok)
    # severe underperformance: strongly negative mean, tiny noise
    bad = [-0.004 + random.gauss(0, 0.002) for _ in range(90)]
    assert "DRIFT WARNING" in ft.drift_verdict(bad)
    hot = [0.01 + random.gauss(0, 0.002) for _ in range(90)]
    assert "ABOVE EXPECTATION" in ft.drift_verdict(hot)


# ---- shadow book ----
def test_shadow_book_log_and_resolve(tmp_path, monkeypatch):
    import shadow_book as sb
    monkeypatch.setattr(sb, "BOOK", tmp_path / "shadow.csv")
    # fake scanner: one skipped long candidate, one traded coin, one flat
    import scan as scan_mod
    monkeypatch.setattr(scan_mod, "scan", lambda top=20, min_vol_usd=2e6: [
        {"coin": "ALT", "signal": 1.0, "target": 0.3, "mark": 100.0},
        {"coin": "BTC", "signal": 1.0, "target": 0.3, "mark": 60000.0},
        {"coin": "DEAD", "signal": 0.0, "target": 0.0, "mark": 1.0},
    ])
    added = sb.log_skipped(traded_coins={"BTC"})
    assert added == 1                       # only ALT (BTC traded, DEAD flat)
    assert sb.log_skipped(traded_coins={"BTC"}) == 0   # same-day dedup

    # age the entry 10 days and give ALT a +20% path -> resolves at T+7
    rows = sb._load()
    import datetime as real_dt
    old = (real_dt.datetime.now(real_dt.timezone.utc).date()
           - real_dt.timedelta(days=10)).strftime("%Y-%m-%d")
    with open(sb.BOOK, "w", newline="") as fh:
        import csv as _csv
        w = _csv.writer(fh); w.writerow(sb.COLUMNS)
        w.writerow([old, "ALT", "100.0", "1.0", "0.3", "test"])
    monkeypatch.setattr(sb.trend_bot, "fetch_daily_closes",
                        lambda coin, days=120: [100.0 + i for i in range(60)])
    rep = sb.report()
    assert "T+ 7d: n=1" in rep
    assert "insufficient resolved data" in rep   # n<20 -> no filter verdict yet


# ---- patient execution ----
class _ExecFake:
    def __init__(self, alo_reject=False):
        self.orders = []; self.cancels = []
        self.alo_reject = alo_reject
    def order(self, coin, is_buy, sz, px, order_type=None, reduce_only=False):
        self.orders.append((coin, is_buy, sz, px, order_type))
        if "limit" in order_type and order_type["limit"]["tif"] == "Alo":
            if self.alo_reject:
                return {"response": {"data": {"statuses": [{"error": "would cross"}]}}}
            return {"response": {"data": {"statuses": [{"resting": {"oid": 77}}]}}}
        return {"response": {"data": {"statuses": [{"filled": {"oid": 78}}]}}}
    def cancel(self, coin, oid):
        self.cancels.append(oid)


def _patch_book(monkeypatch, bid=60000.0, ask=60001.0):
    monkeypatch.setattr(tb, "_best_bid_ask", lambda coin: (bid, ask))


def test_patient_exec_maker_fill(monkeypatch):
    monkeypatch.setattr(tb, "TREND_EXEC", "patient")
    _patch_book(monkeypatch)
    monkeypatch.setattr(tb, "_order_open", lambda addr, oid: False)  # fills fast
    ex = _ExecFake()
    rec = tb.execute_rebalance(ex, "BTC", 0.001, 60000.5, sleep_fn=lambda s: None)
    assert rec["style"] == "maker filled"
    assert ex.orders[0][4] == {"limit": {"tif": "Alo"}}
    assert ex.orders[0][3] == 60000.0          # joined the bid
    assert not ex.cancels


def test_patient_exec_timeout_falls_back_to_ioc(monkeypatch):
    monkeypatch.setattr(tb, "TREND_EXEC", "patient")
    monkeypatch.setattr(tb, "TREND_EXEC_WAIT_S", 30)
    monkeypatch.setattr(tb, "TREND_EXEC_POLL_S", 15)
    _patch_book(monkeypatch)
    monkeypatch.setattr(tb, "_order_open", lambda addr, oid: True)   # never fills
    ex = _ExecFake()
    rec = tb.execute_rebalance(ex, "BTC", 0.001, 60000.5, sleep_fn=lambda s: None)
    assert rec["style"].startswith("ioc (timeout")
    assert ex.cancels == [77]
    assert ex.orders[-1][4] == {"limit": {"tif": "Ioc"}}


def test_patient_exec_alo_rejected_goes_ioc(monkeypatch):
    monkeypatch.setattr(tb, "TREND_EXEC", "patient")
    _patch_book(monkeypatch)
    ex = _ExecFake(alo_reject=True)
    rec = tb.execute_rebalance(ex, "BTC", -0.001, 60000.5, sleep_fn=lambda s: None)
    assert rec["style"].startswith("ioc (alo rejected")


def test_exec_configured_ioc(monkeypatch):
    monkeypatch.setattr(tb, "TREND_EXEC", "ioc")
    ex = _ExecFake()
    rec = tb.execute_rebalance(ex, "BTC", 0.001, 60000.0, sleep_fn=lambda s: None)
    assert rec["style"] == "ioc (configured)"
    assert ex.orders[0][4] == {"limit": {"tif": "Ioc"}}


def test_scan_liquidity_floor_excludes_thin_markets(monkeypatch):
    import sys, io, contextlib
    import scan as scan_mod
    monkeypatch.setattr(scan_mod, "universe_by_volume", lambda mv=2e6: [
        {"coin": "BIGML", "day_vol_usd": 80e6, "mark": 100.0, "funding_hr": 0.0},
        {"coin": "THINX", "day_vol_usd": 5e6, "mark": 1.0, "funding_hr": 0.0},
    ])
    monkeypatch.setattr(scan_mod.trend_bot, "fetch_daily_closes",
                        lambda coin, days=400: _series(50_000, 100_000, 200))
    monkeypatch.setattr(scan_mod.trend_bot, "MIN_HISTORY_D", 151)
    monkeypatch.setattr(sys, "argv", ["scan.py"])   # clean argparse
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        scan_mod.main()
    out = buf.getvalue()
    assert "BIGML" in out
    assert "THINX LONG signal but" in out and "NOT a candidate" in out
    assert "LONG candidates (validated signal): BIGML" in out
