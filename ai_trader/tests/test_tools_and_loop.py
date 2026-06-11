"""Integration tests: tool executor + risk gating + full agent loop with a
mocked Claude client (no API credits spent)."""
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools as tools_mod
from risk_engine import RiskEngine, RiskLimits
from tools import ToolExecutor

LIMITS = RiskLimits(
    max_position_usd=200, max_total_exposure_usd=200, max_order_usd=120,
    max_daily_loss_usd=10, max_leverage=1.5, allowed_coins=("BTC",),
    max_orders_per_day=6, min_order_usd=10,
)


@pytest.fixture
def execu(monkeypatch):
    e = ToolExecutor("https://x", "0xabc", RiskEngine(LIMITS), exchange=None, live=False)
    # stub the account snapshot so no network is needed
    monkeypatch.setattr(e, "account_snapshot", lambda: {
        "equity_usd": 160.0, "positions": {}, "positions_usd": {},
        "mids": {"BTC": 60000.0}})
    # default: model is long, so the signal gate permits BUYs; individual
    # tests re-patch to 0.0 to exercise the gate itself
    monkeypatch.setattr(tools_mod.strategy_bridge, "strategy_signal",
                        lambda coin: {"target_fraction": 1.0})
    return e


def test_place_order_dry_run_approved(execu):
    out, err = execu.execute("place_order", {
        "coin": "BTC", "side": "BUY", "size": 0.001, "rationale": "signal long"})
    assert not err and out["ok"] and out["dry_run"]
    assert execu.audit[-1]["result"] == "DRY_RUN"


def test_place_order_rejected_oversize(execu):
    out, err = execu.execute("place_order", {
        "coin": "BTC", "side": "BUY", "size": 0.01, "rationale": "too big"})  # $600
    assert err and not out["ok"] and "risk engine rejected" in out["error"]
    assert execu.audit[-1]["result"] == "REJECTED"


def test_place_order_disallowed_coin(execu, monkeypatch):
    monkeypatch.setattr(execu, "account_snapshot", lambda: {
        "equity_usd": 160.0, "positions": {}, "positions_usd": {},
        "mids": {"DOGE": 0.1}})
    out, err = execu.execute("place_order", {
        "coin": "DOGE", "side": "BUY", "size": 50, "rationale": "nope"})
    assert err and "allowed_coins" in out["error"]


def test_halt_tool(execu):
    out, err = execu.execute("halt_trading", {"reason": "weird data"})
    assert not err and out["halted"]
    assert not execu.risk.check_can_trade().approved


def test_unknown_tool(execu):
    out, err = execu.execute("nonexistent", {})
    assert err and "unknown tool" in out["error"]


# ---- full agent loop with a mock anthropic client ----
class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _MockMessages:
    def __init__(self):
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        if self.calls == 1:
            return _Resp([_Block(type="tool_use", id="t1", name="place_order",
                                 input={"coin": "BTC", "side": "BUY", "size": 0.001,
                                        "rationale": "model target long"})],
                         "tool_use")
        return _Resp([_Block(type="text", text="Placed a small BTC long per the signal.")],
                     "end_turn")


class _MockClient:
    def __init__(self, *a, **k):
        self.messages = _MockMessages()


def test_full_loop_drives_tool_then_summarizes(monkeypatch):
    import agent

    # fake settings.load() to inject our limits + a fake api key
    fake_cfg = types.SimpleNamespace(
        api_url="https://x", private_key="", account_address="0xabc",
        observer_mode=True, anthropic_api_key="sk-test",
        model="claude-fable-5", risk_limits=LIMITS)
    monkeypatch.setattr(agent.settings, "load", lambda: fake_cfg)

    # stub the executor's network snapshot
    real_build = agent.build_runtime
    def patched_build(live):
        cfg, execu = real_build(live)
        execu.account_snapshot = lambda: {"equity_usd": 160.0, "positions": {},
                                          "positions_usd": {}, "mids": {"BTC": 60000.0}}
        return cfg, execu
    monkeypatch.setattr(agent, "build_runtime", patched_build)

    # model long so the signal gate permits the mocked BUY
    monkeypatch.setattr(tools_mod.strategy_bridge, "strategy_signal",
                        lambda coin: {"target_fraction": 1.0})
    # mock anthropic module
    fake_anth = types.ModuleType("anthropic")
    fake_anth.Anthropic = _MockClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_anth)
    # don't write audit to the real file
    monkeypatch.setattr(agent, "_write_audit", lambda *a, **k: None)

    out = agent.run_cycle("do today's cycle", live=False)
    assert "Placed a small BTC long" in out


def test_signal_gate_blocks_long_when_model_flat(execu, monkeypatch):
    monkeypatch.setattr(tools_mod.strategy_bridge, "strategy_signal",
                        lambda coin: {"target_fraction": 0.0})
    out, err = execu.execute("place_order", {
        "coin": "BTC", "side": "BUY", "size": 0.001, "rationale": "vibes"})
    assert err and "signal gate" in out["error"]


def test_signal_gate_allows_long_when_model_long(execu, monkeypatch):
    monkeypatch.setattr(tools_mod.strategy_bridge, "strategy_signal",
                        lambda coin: {"target_fraction": 0.75})
    out, err = execu.execute("place_order", {
        "coin": "BTC", "side": "BUY", "size": 0.001, "rationale": "model long"})
    assert not err and out["dry_run"]


def test_signal_gate_blocks_new_short(execu):
    out, err = execu.execute("place_order", {
        "coin": "BTC", "side": "SELL", "size": 0.001, "rationale": "bearish vibes"})
    assert err and "reduce_only" in out["error"]


def test_signal_gate_allows_reduce_only_exit(execu, monkeypatch):
    monkeypatch.setattr(execu, "account_snapshot", lambda: {
        "equity_usd": 160.0, "positions": {"BTC": 0.001},
        "positions_usd": {"BTC": 60.0}, "mids": {"BTC": 60000.0}})
    out, err = execu.execute("place_order", {
        "coin": "BTC", "side": "SELL", "size": 0.001, "reduce_only": True,
        "rationale": "model went flat; closing"})
    assert not err and out["dry_run"]
