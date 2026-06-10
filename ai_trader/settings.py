"""Configuration for the AI trader. All risk limits are env-overridable but ship
with conservative defaults sized for a small (~$160) account."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from risk_engine import RiskLimits

# load .env from repo root if present (no hard dependency on python-dotenv)
_ROOT = Path(__file__).resolve().parents[1]
_env = _ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


@dataclass(frozen=True)
class Config:
    api_url: str
    private_key: str
    account_address: str
    observer_mode: bool
    anthropic_api_key: str
    model: str
    risk_limits: RiskLimits


def load() -> Config:
    api_url = os.getenv("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz").rstrip("/")
    pk = os.getenv("PRIVATE_KEY", "")
    coins = tuple(c.strip().upper() for c in
                  os.getenv("AI_TRADER_COINS", "BTC").split(",") if c.strip())
    limits = RiskLimits(
        max_position_usd=float(os.getenv("AI_MAX_POSITION_USD", "200")),
        max_total_exposure_usd=float(os.getenv("AI_MAX_TOTAL_EXPOSURE_USD", "200")),
        max_order_usd=float(os.getenv("AI_MAX_ORDER_USD", "120")),
        max_daily_loss_usd=float(os.getenv("AI_MAX_DAILY_LOSS_USD", "10")),
        max_leverage=float(os.getenv("AI_MAX_LEVERAGE", "1.5")),
        allowed_coins=coins,
        max_orders_per_day=int(os.getenv("AI_MAX_ORDERS_PER_DAY", "6")),
        min_order_usd=float(os.getenv("AI_MIN_ORDER_USD", "10")),
    )
    return Config(
        api_url=api_url,
        private_key=pk,
        account_address=os.getenv("ACCOUNT_ADDRESS", ""),
        observer_mode=not bool(pk),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        # Fable 5 for reasoning-heavy daily decisions; override via env.
        model=os.getenv("AI_TRADER_MODEL", "claude-fable-5"),
        risk_limits=limits,
    )
