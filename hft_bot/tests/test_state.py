"""Tests for BotState: rolling windows, fee-aware PnL, daily-ledger persistence."""
import json
import re

import pytest

import state as state_mod
from state import BotState, RollingOFIWindow, RollingTradeWindow


# ---------------------------------------------------------------------------
# Rolling windows: O(1) running sums must equal brute-force recomputation
# ---------------------------------------------------------------------------

def test_ofi_window_sum_matches_bruteforce():
    w = RollingOFIWindow(maxlen=5)
    for i, d in enumerate([1.0, -2.5, 3.0, 0.5, -1.0, 4.0, 2.0]):   # overflows maxlen
        w.append(i * 100, d)
    assert w.total == pytest.approx(sum(d for _, d in w.entries))

def test_ofi_window_prune_updates_sum():
    w = RollingOFIWindow()
    w.append(100, 1.0)
    w.append(200, 2.0)
    w.append(300, 4.0)
    w.prune(250)
    assert w.total == pytest.approx(4.0)
    w.prune(10_000)
    assert w.total == 0.0 and len(w) == 0

def test_trade_window_sums_match_bruteforce():
    w = RollingTradeWindow(maxlen=4)
    trades = [(100, 1.0, 50000.0), (200, -2.0, 50010.0), (300, 0.5, 50020.0),
              (400, -0.25, 49990.0), (500, 3.0, 50050.0)]
    for ts, sv, px in trades:
        w.append(ts, sv, px)
    assert w.buy_vol == pytest.approx(sum(v for _, v, _ in w.entries if v > 0))
    assert w.sell_vol == pytest.approx(sum(-v for _, v, _ in w.entries if v < 0))
    assert w.px_vol == pytest.approx(sum(abs(v) * p for _, v, p in w.entries))

def test_trade_window_prune_resets_when_empty():
    w = RollingTradeWindow()
    w.append(100, 1.0, 50000.0)
    w.prune(10_000)
    assert w.buy_vol == 0.0 and w.sell_vol == 0.0 and w.px_vol == 0.0


# ---------------------------------------------------------------------------
# record_fill — fee-aware PnL, average entry, journal flush
# ---------------------------------------------------------------------------

@pytest.fixture
def no_journal(monkeypatch):
    rows = []
    monkeypatch.setattr(
        BotState, "_append_trade_journal",
        lambda self, *a: rows.append(a),
    )
    monkeypatch.setattr(BotState, "persist_daily_pnl", lambda self: None)
    return rows


def test_fee_subtracted_from_daily_pnl(no_journal):
    s = BotState()
    s.record_fill(is_buy=True, fill_px=100_000, fill_sz=0.001, closed_pnl=0.0, fee=0.035)
    s.record_fill(is_buy=False, fill_px=100_100, fill_sz=0.001, closed_pnl=0.1, fee=0.035)
    assert s.daily_pnl_usd == pytest.approx(0.1 - 0.07)
    assert s.daily_fees_usd == pytest.approx(0.07)


def test_average_entry_price_on_adds(no_journal):
    s = BotState()
    s.record_fill(True, 100_000, 0.001, 0.0)
    s.record_fill(True, 100_200, 0.001, 0.0)
    assert s.entry_price == pytest.approx(100_100)
    assert s.inventory_btc == pytest.approx(0.002)


def test_position_flat_resets_state(no_journal):
    s = BotState()
    s.record_fill(True, 100_000, 0.001, 0.0)
    assert s.position_open_ms is not None
    s.record_fill(False, 100_500, 0.001, 0.5)
    assert s.inventory_btc == 0.0
    assert s.entry_price is None
    assert s.position_open_ms is None
    assert len(no_journal) == 1   # one journal row per round trip


def test_loss_close_sets_post_sl_timestamp(no_journal, fake_clock):
    s = BotState()
    s.record_fill(True, 100_000, 0.001, 0.0)
    s.record_fill(False, 99_000, 0.001, -1.0)
    assert s.last_sl_ms == fake_clock.ms


# ---------------------------------------------------------------------------
# Daily ledger persistence — restart must not reset the circuit breaker
# ---------------------------------------------------------------------------

def test_daily_pnl_persists_across_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "PNL_STATE_FILE", tmp_path / "daily.json")
    monkeypatch.setattr(BotState, "_append_trade_journal", lambda self, *a: None)
    s1 = BotState()
    s1.record_fill(True, 100_000, 0.001, 0.0, fee=0.05)
    s1.record_fill(False, 99_000, 0.001, -1.0, fee=0.05)

    s2 = BotState()   # "restart"
    s2.load_daily_pnl()
    assert s2.daily_pnl_usd == pytest.approx(s1.daily_pnl_usd)
    assert s2.daily_fees_usd == pytest.approx(0.10)


def test_daily_pnl_resets_on_utc_rollover(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "PNL_STATE_FILE", tmp_path / "daily.json")
    s = BotState()
    s.daily_pnl_usd = -5.0
    s.daily_date = "2020-01-01"   # force a past date
    assert s.roll_daily_pnl_if_new_day() is True
    assert s.daily_pnl_usd == 0.0
    assert s.roll_daily_pnl_if_new_day() is False


def test_stale_ledger_file_ignored(tmp_path, monkeypatch):
    f = tmp_path / "daily.json"
    f.write_text(json.dumps({"date": "2020-01-01", "realized_net": -99.0, "fees": 1.0}))
    monkeypatch.setattr(state_mod, "PNL_STATE_FILE", f)
    s = BotState()
    s.load_daily_pnl()
    assert s.daily_pnl_usd == 0.0   # yesterday's losses don't carry over


# ---------------------------------------------------------------------------
# summary() must stay parseable by the management UI log regex
# ---------------------------------------------------------------------------

_UI_STATE_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2})\.\d+ \[INFO\] main \| STATE \| "
    r"status=(\w+) inv=([+\-\d.]+)BTC entry=([\d.]+) mid=([\d.]+|N/A) "
    r"unrealPnL=([+\-\d.]+)\$ realPnL=([+\-\d.]+)\$ fills=(\d+) open_orders=(\d+)"
)

def test_summary_matches_management_ui_regex():
    s = BotState()
    s.set_running()
    line = f"12:00:00.123 [INFO] main | STATE | {s.summary()}"
    assert _UI_STATE_RE.search(line), f"UI regex no longer matches: {line}"
