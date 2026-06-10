"""The risk engine is the safety boundary — it gets the most thorough tests."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from risk_engine import OrderRequest, RiskEngine, RiskLimits, RiskState

LIMITS = RiskLimits(
    max_position_usd=200, max_total_exposure_usd=300, max_order_usd=120,
    max_daily_loss_usd=10, max_leverage=1.5, allowed_coins=("BTC", "ETH"),
    max_orders_per_day=6, min_order_usd=10,
)


def eng(**state):
    return RiskEngine(LIMITS, RiskState(**state))


def buy(coin="BTC", size=0.001, price=60000, reduce_only=False):
    return OrderRequest(coin, True, size, price, reduce_only)


# -- account gate --
def test_clean_account_can_trade():
    assert eng().check_can_trade().approved


def test_daily_loss_halts():
    d = eng(realized_pnl_today_usd=-10.0).check_can_trade()
    assert not d.approved and "daily loss" in d.reason


def test_manual_halt_blocks():
    e = eng(); e.halt("manual")
    assert not e.check_can_trade().approved
    e.resume()
    assert e.check_can_trade().approved


def test_max_orders_per_day():
    assert not eng(orders_today=6).check_can_trade().approved


# -- order gate --
def test_disallowed_coin_rejected():
    d = eng().check_order(buy(coin="DOGE"), 1000, {})
    assert not d.approved and "allowed_coins" in d.reason


def test_below_min_notional_rejected():
    d = eng().check_order(buy(size=0.0001, price=60000), 1000, {})  # $6
    assert not d.approved and "minimum" in d.reason


def test_above_max_order_rejected():
    d = eng().check_order(buy(size=0.01, price=60000), 1000, {})  # $600 > $120
    assert not d.approved and "max_order_usd" in d.reason


def test_position_cap_enforced():
    # already $150 long BTC; +$100 would be $250 > $200 cap
    d = eng().check_order(buy(size=0.0016, price=60000), 1000, {"BTC": 150.0})
    assert not d.approved and "max_position_usd" in d.reason


def test_total_exposure_cap():
    # $150 ETH already; +$120 BTC keeps BTC<cap but gross $270<300 ok...
    ok = eng().check_order(buy(size=0.002, price=60000), 1000, {"ETH": 150.0})
    assert ok.approved
    # now ETH $250 -> gross would exceed 300
    d = eng().check_order(buy(size=0.002, price=60000), 1000, {"ETH": 250.0})
    assert not d.approved and "exposure" in d.reason


def test_leverage_cap():
    # equity $100, max 1.5x => $150 gross cap; a $120 order alone is fine
    ok = eng().check_order(buy(size=0.002, price=60000), 100, {})
    assert ok.approved
    # but with $80 existing, +$120 = $200 gross / $100 = 2x > 1.5x
    d = eng().check_order(buy(size=0.002, price=60000), 100, {"ETH": 80.0})
    assert not d.approved and ("leverage" in d.reason or "exposure" in d.reason)


def test_reduce_only_cannot_increase():
    d = eng().check_order(buy(coin="BTC", size=0.001, reduce_only=True), 1000, {"BTC": 60.0})
    assert not d.approved and "increase" in d.reason


def test_reduce_only_shrinks_ok():
    # short $60 BTC, reduce-only BUY $60 -> flat, allowed
    d = eng().check_order(buy(coin="BTC", size=0.001, reduce_only=True), 1000, {"BTC": -60.0})
    assert d.approved


def test_happy_path_approved():
    d = eng().check_order(buy(size=0.001, price=60000), 1000, {})
    assert d.approved and d.clamped_size == 0.001


def test_negative_inputs_rejected():
    assert not eng().check_order(buy(size=-1), 1000, {}).approved
    assert not eng().check_order(buy(price=0), 1000, {}).approved


def test_record_keeping():
    e = eng()
    e.record_order(); e.record_order()
    assert e.state.orders_today == 2
    e.record_pnl(-3.0); e.record_pnl(-2.0)
    assert e.state.realized_pnl_today_usd == -5.0
