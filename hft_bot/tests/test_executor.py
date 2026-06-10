"""Tests for the order executor: idempotent emergency close, cloid propagation,
fee-aware fill handling, SL placement escalation."""
import asyncio

import pytest

import config
import executor as executor_mod
from executor import OrderExecutor, _round_price, _round_size
from state import BotState


class FakeExchange:
    def __init__(self, fail_triggers=False):
        self.market_close_calls = 0
        self.cancel_calls = 0
        self.orders = []
        self.fail_triggers = fail_triggers

    def order(self, coin, is_buy, sz, px, order_type=None, reduce_only=False, cloid=None):
        if self.fail_triggers and "trigger" in (order_type or {}):
            raise RuntimeError("simulated 429")
        self.orders.append(dict(coin=coin, is_buy=is_buy, sz=sz, px=px,
                                order_type=order_type, reduce_only=reduce_only,
                                cloid=cloid))
        return {"response": {"data": {"statuses": [{"resting": {"oid": len(self.orders)}}]}}}

    def cancel(self, coin, oid):
        self.cancel_calls += 1
        return {"status": "ok"}

    def market_close(self, coin, slippage=0.01):
        self.market_close_calls += 1
        return {"status": "ok"}


@pytest.fixture
def live_mode(monkeypatch):
    monkeypatch.setattr(config, "OBSERVER_MODE", False)


def _make_executor(exchange):
    loop = asyncio.get_event_loop()
    state = BotState()
    return OrderExecutor(exchange=exchange, state=state, loop=loop), state


def test_round_price_respects_tick(monkeypatch):
    monkeypatch.setattr(config, "PRICE_TICK", 0.1)
    assert _round_price(100000.04) == 100000.0
    assert _round_price(100000.06) == 100000.1

def test_round_size():
    assert _round_size(0.0014999) == 0.001


def test_extract_oid_resting_and_filled():
    r = {"response": {"data": {"statuses": [{"resting": {"oid": 42}}]}}}
    assert OrderExecutor._extract_oid(r, "x") == 42
    r = {"response": {"data": {"statuses": [{"filled": {"oid": 7}}]}}}
    assert OrderExecutor._extract_oid(r, "x") == 7
    r = {"response": {"data": {"statuses": [{"error": "Post only would cross"}]}}}
    assert OrderExecutor._extract_oid(r, "x") is None


def test_emergency_close_is_idempotent(live_mode, fake_clock):
    async def scenario():
        ex = FakeExchange()
        execu, state = _make_executor(ex)
        state.inventory_btc = 0.005
        state.entry_price = 100_000.0
        # risk loop re-fires before the WS fill clears inventory:
        await asyncio.gather(
            execu.emergency_close("stop_loss"),
            execu.emergency_close("stop_loss"),
            execu.emergency_close("stop_loss"),
        )
        await execu.emergency_close("stop_loss")   # later re-fire inside cool-off
        return ex.market_close_calls

    calls = asyncio.get_event_loop().run_until_complete(scenario())
    assert calls == 1, f"emergency close fired {calls} times — must be exactly once"


def test_cloid_is_sent_to_exchange(live_mode):
    async def scenario():
        ex = FakeExchange()
        execu, state = _make_executor(ex)
        await execu.place_limit_order(is_buy=True, price=100_000.0, size=0.001)
        return ex.orders[0]["cloid"]

    cloid = asyncio.get_event_loop().run_until_complete(scenario())
    assert cloid is not None, "client order id must reach the exchange"
    assert str(cloid).startswith("0x") and len(str(cloid)) == 34


def test_handle_fill_subtracts_fee(live_mode, monkeypatch):
    monkeypatch.setattr(BotState, "_append_trade_journal", lambda self, *a: None)
    monkeypatch.setattr(BotState, "persist_daily_pnl", lambda self: None)

    async def scenario():
        ex = FakeExchange()
        execu, state = _make_executor(ex)
        state.inventory_btc = 0.001
        state.entry_price = 100_000.0
        execu.handle_fill({"oid": 1, "px": "100100", "sz": "0.001",
                           "side": "A", "closedPnl": "0.10", "fee": "0.035"})
        await asyncio.sleep(0.3)   # let the debounced SL/TP task settle
        return state

    state = asyncio.get_event_loop().run_until_complete(scenario())
    assert state.daily_pnl_usd == pytest.approx(0.10 - 0.035)
    assert state.daily_fees_usd == pytest.approx(0.035)


def test_sl_placement_failure_escalates_to_emergency_close(live_mode, monkeypatch):
    """If the exchange keeps rejecting the stop-loss, an unprotected position
    must be flattened rather than left naked."""
    monkeypatch.setattr(BotState, "persist_daily_pnl", lambda self: None)

    async def scenario():
        ex = FakeExchange(fail_triggers=True)
        execu, state = _make_executor(ex)
        state.inventory_btc = 0.001
        state.entry_price = 100_000.0
        await execu._manage_sl_tp()
        return ex

    ex = asyncio.get_event_loop().run_until_complete(scenario())
    assert ex.market_close_calls == 1, "naked position must be emergency-closed"
