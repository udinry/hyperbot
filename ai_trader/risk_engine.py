"""Deterministic risk engine — the seatbelt the AI cannot unbuckle.

Design principle: the LLM proposes, the risk engine disposes. Every order the
agent wants to place is checked here, in plain Python, against hard limits
loaded from config. No prompt, jailbreak, hallucination, or reasoning error can
bypass these checks — they run AFTER the model and BEFORE the exchange. If a
check fails the order is rejected and the rejection (with reason) is fed back to
the model as a tool error.

This module has zero dependencies on the LLM or the exchange so it is fully
unit-testable (see tests/test_risk_engine.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class RiskLimits:
    max_position_usd: float           # max notional per asset
    max_total_exposure_usd: float     # max gross notional across all assets
    max_order_usd: float              # max single order notional
    max_daily_loss_usd: float         # halt for the UTC day if realized loss exceeds
    max_leverage: float               # hard cap on equity multiple
    allowed_coins: tuple[str, ...]    # only these symbols may be traded
    max_orders_per_day: int           # throttle to prevent runaway loops
    min_order_usd: float = 10.0       # exchange minimum / dust guard


@dataclass
class RiskState:
    """Mutable counters the engine reads; updated by the agent runtime."""
    realized_pnl_today_usd: float = 0.0
    orders_today: int = 0
    day: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    halted: bool = False
    halt_reason: str = ""

    def roll_day_if_needed(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.realized_pnl_today_usd = 0.0
            self.orders_today = 0
            # a manual halt persists across days until explicitly cleared


@dataclass(frozen=True)
class OrderRequest:
    coin: str
    is_buy: bool
    size: float            # in coin units
    price: float           # reference/limit price (USD)
    reduce_only: bool = False


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    # echo of the (possibly clamped) order the engine would allow
    clamped_size: Optional[float] = None


class RiskEngine:
    def __init__(self, limits: RiskLimits, state: Optional[RiskState] = None) -> None:
        self.limits = limits
        self.state = state or RiskState()

    # -- account-level gate, checked once per cycle before any order --
    def check_can_trade(self) -> RiskDecision:
        self.state.roll_day_if_needed()
        if self.state.halted:
            return RiskDecision(False, f"trading halted: {self.state.halt_reason}")
        if self.state.realized_pnl_today_usd <= -abs(self.limits.max_daily_loss_usd):
            return RiskDecision(
                False,
                f"daily loss limit hit "
                f"({self.state.realized_pnl_today_usd:.2f} <= "
                f"-{self.limits.max_daily_loss_usd:.2f}) — halted for the day",
            )
        if self.state.orders_today >= self.limits.max_orders_per_day:
            return RiskDecision(
                False,
                f"max orders/day reached ({self.state.orders_today}/"
                f"{self.limits.max_orders_per_day})",
            )
        return RiskDecision(True, "ok")

    # -- order-level gate --
    def check_order(
        self,
        req: OrderRequest,
        equity_usd: float,
        current_positions_usd: dict[str, float],
    ) -> RiskDecision:
        """current_positions_usd: signed notional per coin (long +, short -)."""
        gate = self.check_can_trade()
        if not gate.approved:
            return gate

        if req.coin not in self.limits.allowed_coins:
            return RiskDecision(False, f"{req.coin} not in allowed_coins")

        if req.price <= 0 or req.size <= 0:
            return RiskDecision(False, "non-positive price or size")

        order_usd = req.size * req.price
        if order_usd < self.limits.min_order_usd:
            return RiskDecision(False, f"order ${order_usd:.2f} below minimum "
                                       f"${self.limits.min_order_usd:.2f}")
        if order_usd > self.limits.max_order_usd:
            return RiskDecision(False, f"order ${order_usd:.2f} exceeds "
                                       f"max_order_usd ${self.limits.max_order_usd:.2f}")

        # resulting position notional for this coin (reduce-only can only shrink)
        cur = current_positions_usd.get(req.coin, 0.0)
        signed_order = order_usd if req.is_buy else -order_usd
        if req.reduce_only:
            # a reduce-only order must move |position| toward zero
            if (cur > 0 and req.is_buy) or (cur < 0 and not req.is_buy):
                return RiskDecision(False, "reduce_only order would increase position")
            new_abs = max(0.0, abs(cur) - order_usd)
        else:
            new_abs = abs(cur + signed_order)

        if new_abs > self.limits.max_position_usd:
            return RiskDecision(
                False,
                f"resulting {req.coin} position ${new_abs:.2f} exceeds "
                f"max_position_usd ${self.limits.max_position_usd:.2f}",
            )

        # gross exposure across the book after this order
        gross = sum(abs(v) for k, v in current_positions_usd.items() if k != req.coin)
        gross += new_abs
        if gross > self.limits.max_total_exposure_usd:
            return RiskDecision(
                False,
                f"resulting gross exposure ${gross:.2f} exceeds "
                f"max_total_exposure_usd ${self.limits.max_total_exposure_usd:.2f}",
            )

        # leverage cap
        if equity_usd > 0 and gross / equity_usd > self.limits.max_leverage:
            return RiskDecision(
                False,
                f"resulting leverage {gross/equity_usd:.2f}x exceeds "
                f"max_leverage {self.limits.max_leverage:.2f}x",
            )

        return RiskDecision(True, "approved", clamped_size=req.size)

    # -- bookkeeping the runtime calls after a fill / order --
    def record_order(self) -> None:
        self.state.roll_day_if_needed()
        self.state.orders_today += 1

    def record_pnl(self, realized_delta_usd: float) -> None:
        self.state.roll_day_if_needed()
        self.state.realized_pnl_today_usd += realized_delta_usd

    def halt(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason

    def resume(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
