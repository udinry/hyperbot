"""
Central configuration for the OFI HFT bot.
Sources: environment variables (via .env) + risk.yaml for risk limits.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env from the hyperbot repo root (one directory up from this file).
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_ROOT / ".env")

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"
MAINNET_API_URL = "https://api.hyperliquid.xyz"

# Default to TESTNET — set HYPERLIQUID_API_URL=https://api.hyperliquid.xyz in .env for mainnet.
API_URL: str = os.getenv("HYPERLIQUID_API_URL", TESTNET_API_URL)
USE_TESTNET: bool = API_URL == TESTNET_API_URL

# ---------------------------------------------------------------------------
# Wallet / credentials
# ---------------------------------------------------------------------------
PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
# If empty, bot runs in OBSERVER mode: signals are computed but no orders fire.
OBSERVER_MODE: bool = not bool(PRIVATE_KEY)

# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------
COIN: str = os.getenv("COIN", "BTC")

# BTC lot size on Hyperliquid — each order is this many BTC.
ORDER_SIZE_BTC: float = float(os.getenv("ORDER_SIZE_BTC", "0.001"))

# ---------------------------------------------------------------------------
# OFI strategy tuning
# ---------------------------------------------------------------------------
# Rolling window length in milliseconds over which OFI is accumulated.
OFI_WINDOW_MS: int = int(os.getenv("OFI_WINDOW_MS", "500"))

# Normalised OFI thresholds in [-1, +1].  Stricter = fewer but higher-quality signals.
OFI_BUY_THRESHOLD: float = float(os.getenv("OFI_BUY_THRESHOLD", "0.65"))
OFI_SELL_THRESHOLD: float = float(os.getenv("OFI_SELL_THRESHOLD", "-0.65"))

# Number of top book levels fed into the OFI calculation (1 or 2 is typical).
OFI_LEVELS: int = int(os.getenv("OFI_LEVELS", "2"))

# Minimum time between consecutive signals (ms) to suppress signal clustering.
SIGNAL_COOLDOWN_MS: int = int(os.getenv("SIGNAL_COOLDOWN_MS", "200"))

# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------
# When True bot places ALO ("post-only") limit orders; when False it may cross.
POST_ONLY: bool = os.getenv("POST_ONLY", "true").lower() == "true"

# How long (ms) a resting limit order is allowed to live before auto-cancel.
LIMIT_ORDER_TIMEOUT_MS: int = int(os.getenv("LIMIT_ORDER_TIMEOUT_MS", "1500"))

# Tick size for BTC on Hyperliquid perp (price precision).
PRICE_TICK: float = float(os.getenv("PRICE_TICK", "0.1"))

# Size decimal places for BTC on Hyperliquid perp.
SIZE_DECIMALS: int = int(os.getenv("SIZE_DECIMALS", "3"))

# How far inside the spread to price limit orders (in ticks).
# 0 = at best bid/ask; positive = deeper into the book (safer fill rate).
EDGE_TICKS: int = int(os.getenv("EDGE_TICKS", "0"))

# ---------------------------------------------------------------------------
# Risk limits — loaded from risk.yaml so ops can tune without code changes.
# ---------------------------------------------------------------------------
_RISK_FILE = Path(os.getenv("RISK_CONFIG", Path(__file__).parent / "risk.yaml"))

try:
    with open(_RISK_FILE) as _fh:
        _risk: dict = yaml.safe_load(_fh)
except FileNotFoundError:
    raise SystemExit(f"[config] Risk file not found: {_RISK_FILE}")

MAX_INVENTORY_BTC: float = float(_risk["max_inventory_btc"])
STOP_LOSS_PCT: float = float(_risk["stop_loss_pct"])
MAX_DAILY_LOSS_USD: float = float(_risk["max_daily_loss_usd"])
LEVERAGE: int = int(_risk.get("leverage", 1))

# ---------------------------------------------------------------------------
# WebSocket / reconnection
# ---------------------------------------------------------------------------
WS_RECONNECT_DELAY_S: float = float(os.getenv("WS_RECONNECT_DELAY_S", "2.0"))
WS_MAX_RECONNECTS: int = int(os.getenv("WS_MAX_RECONNECTS", "10"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = os.getenv("LOG_FILE", str(_ROOT / "hft_bot" / "bot.log"))

# ---------------------------------------------------------------------------
# Sanity-check helper
# ---------------------------------------------------------------------------
def validate() -> None:
    assert ORDER_SIZE_BTC > 0, "ORDER_SIZE_BTC must be positive"
    assert 0 < OFI_BUY_THRESHOLD <= 1, "OFI_BUY_THRESHOLD must be in (0, 1]"
    assert -1 <= OFI_SELL_THRESHOLD < 0, "OFI_SELL_THRESHOLD must be in [-1, 0)"
    assert LIMIT_ORDER_TIMEOUT_MS > 0
    assert MAX_INVENTORY_BTC > 0
    assert 0 < STOP_LOSS_PCT < 1
    assert MAX_DAILY_LOSS_USD > 0
    if OBSERVER_MODE:
        import logging
        logging.warning("[config] No PRIVATE_KEY set — running in OBSERVER MODE (no orders will be placed)")
