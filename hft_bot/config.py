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

# Master account address for READ operations (positions, fills, balance).
# If using an API agent wallet (PRIVATE_KEY belongs to the agent, not the main account),
# set this to the main account address. Reads (clearinghouseState, userFills WS) must
# use the master address — the agent address has no balance or positions of its own.
ACCOUNT_ADDRESS: str = os.getenv("ACCOUNT_ADDRESS", "")

# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------
COIN: str = os.getenv("COIN", "BTC")

# BTC lot size on Hyperliquid — each order is this many BTC.
# Overridden at runtime by dynamic sizing (balance × POSITION_RISK_PCT / leverage / mid).
ORDER_SIZE_BTC: float = float(os.getenv("ORDER_SIZE_BTC", "0.01"))

# Fraction of account balance to use as margin per trade for dynamic sizing.
# 0.48 → at $160 account and 10x leverage: margin = $160×0.48 = $76.80 → 0.010 BTC at $77k.
# Scales automatically: if balance drops to $130 → 0.008 BTC; grows to $200 → 0.010 BTC (capped).
POSITION_RISK_PCT: float = float(os.getenv("POSITION_RISK_PCT", "0.48"))

# ---------------------------------------------------------------------------
# OFI strategy tuning
# ---------------------------------------------------------------------------
# Rolling window length in milliseconds over which OFI and TFI are accumulated.
# 400ms: wider than 250ms (too few deltas per window on testnet tick ~570ms),
# narrower than 500ms (avoids unnecessary lag on fast mainnet ticks ~20ms).
OFI_WINDOW_MS: int = int(os.getenv("OFI_WINDOW_MS", "400"))

# Normalised OFI thresholds in [-1, +1].
# Mainnet tuning: 0.70 generates ~10-15 signals/3 min on liquid BTC — enough
# statistical sample while avoiding the lowest-conviction false triggers at 0.65.
OFI_BUY_THRESHOLD: float = float(os.getenv("OFI_BUY_THRESHOLD", "0.70"))
OFI_SELL_THRESHOLD: float = float(os.getenv("OFI_SELL_THRESHOLD", "-0.70"))

# Number of top book levels fed into the OFI calculation.
OFI_LEVELS: int = int(os.getenv("OFI_LEVELS", "2"))

# Signal persistence: consecutive ticks that must exceed the OFI threshold.
# Mainnet tick is ~20-50ms — require 2 consecutive ticks to filter single-tick spikes.
OFI_PERSISTENCE_TICKS: int = int(os.getenv("OFI_PERSISTENCE_TICKS", "2"))

# Minimum time between consecutive signals (ms).
# 1500ms: T+1000ms is the profitable horizon on mainnet — spacing signals 1.5s apart
# avoids chasing and keeps the 2x anti-flap guard meaningful.
SIGNAL_COOLDOWN_MS: int = int(os.getenv("SIGNAL_COOLDOWN_MS", "1000"))

# Minimum absolute TFI required for signal confirmation.
# Requires trade flow to be at least 10% imbalanced — filters near-zero TFI (0.048)
# that represent essentially random noise in the trade window.
MIN_TFI_STRENGTH: float = float(os.getenv("MIN_TFI_STRENGTH", "0.10"))

# Short-term price trend gate: look-back window in ms.
# A BUY signal is suppressed if mid has been FALLING over this window;
# a SELL signal is suppressed if mid has been RISING.
# This prevents buying into a downtrend or selling into an uptrend.
PRICE_TREND_WINDOW_MS: int = int(os.getenv("PRICE_TREND_WINDOW_MS", "1500"))

# Maximum allowed spread in basis-points of mid-price before a signal is
# suppressed.  On mainnet BTC the spread is 0.01-0.03 bps; set to 9999 to
# disable and trade regardless of spread (needed on testnet).
MAX_SPREAD_BPS: float = float(os.getenv("MAX_SPREAD_BPS", "9999"))

# If spread > WIDE_SPREAD_BPS, use IOC (guaranteed fill) instead of ALO
# (post-only, cheaper but may never fill on a wide-spread book).
# On mainnet: 5 bps is approx $3.85 on $77k BTC.
WIDE_SPREAD_BPS: float = float(os.getenv("WIDE_SPREAD_BPS", "5.0"))

# ---------------------------------------------------------------------------
# Order execution
# ---------------------------------------------------------------------------
# How long (ms) a resting ALO limit order is allowed to live before auto-cancel.
# Reduced from 1500ms to 800ms: get out faster if the market has moved on.
LIMIT_ORDER_TIMEOUT_MS: int = int(os.getenv("LIMIT_ORDER_TIMEOUT_MS", "800"))

# Tick size for BTC on Hyperliquid perp (price precision).
PRICE_TICK: float = float(os.getenv("PRICE_TICK", "1.0"))

# Size decimal places for BTC on Hyperliquid perp.
SIZE_DECIMALS: int = int(os.getenv("SIZE_DECIMALS", "3"))

# ---------------------------------------------------------------------------
# Risk limits — loaded from risk.yaml so ops can tune without code changes.
# ---------------------------------------------------------------------------
_RISK_FILE = Path(os.getenv("RISK_CONFIG", Path(__file__).parent / "risk.yaml"))

try:
    with open(_RISK_FILE) as _fh:
        _risk: dict = yaml.safe_load(_fh)
except FileNotFoundError:
    raise SystemExit(f"[config] Risk file not found: {_RISK_FILE}")

# ---------------------------------------------------------------------------
# Take-profit and exit-signal parameters
# ---------------------------------------------------------------------------
# ALO reduce-only limit placed immediately after fill opens a position.
# 0.25% on $77k BTC = $192.50 notional move = $1.93 profit on 0.01 BTC (before rebate).
TAKE_PROFIT_PCT: float = float(os.getenv("TAKE_PROFIT_PCT", "0.0025"))

# OFI threshold for the simplified exit signal (lower than entry threshold).
# When paused_inventory, fires a reduce-only IOC to close if OFI flips strongly.
EXIT_OFI_THRESHOLD: float = float(os.getenv("EXIT_OFI_THRESHOLD", "0.55"))

# Minimum ms between consecutive exit-signal evaluations.
EXIT_COOLDOWN_MS: int = int(os.getenv("EXIT_COOLDOWN_MS", "2000"))

# Minimum |TFI| for exit signal confirmation — kept lower than entry MIN_TFI_STRENGTH
# so exits trigger quickly on OFI reversal without requiring as strong trade flow.
EXIT_MIN_TFI_STRENGTH: float = float(os.getenv("EXIT_MIN_TFI_STRENGTH", "0.10"))

TREND_5MIN_PCT: float = float(os.getenv("TREND_5MIN_PCT", "0.001"))

# Hourly funding rate (from Hyperliquid metaAndAssetCtxs) above which a long bias
# is considered overstretched and BUY signals require stronger OFI confirmation.
# 0.00015 = 0.015%/hr ≈ 13% APR — clearly overcrowded longs territory.
FUNDING_BIAS_THRESHOLD: float = float(os.getenv("FUNDING_BIAS_THRESHOLD", "0.00015"))

# Queue imbalance gate threshold: require L1 bid_sz/(bid_sz+ask_sz) to exceed this
# for BUY signals, and (1-threshold) for SELL signals.
QUEUE_IMBAL_THRESHOLD: float = float(os.getenv("QUEUE_IMBAL_THRESHOLD", "0.55"))

# Maximum position hold time in milliseconds. 0 = disabled.
# OFI signal half-life is 10-30s — after this limit, close at market regardless of P&L.
# Prevents stale directional exposure when TP/SL are far from current price.
MAX_POSITION_HOLD_MS: int = int(os.getenv("MAX_POSITION_HOLD_MS", "0"))

# Minimum 1-min ATR ($/min) required to enter a trade. 0 = disabled.
# Prevents entries during dead-flat markets where the TP target is unreachable.
ATR_MIN_TRADE_USD: float = float(os.getenv("ATR_MIN_TRADE_USD", "0.0"))

# UTC hour range [start, end) during which ALL signals are suppressed.
# Set both to -1 (default) to disable. Example: start=8, end=12 blocks EU lull.
TRADE_BLOCK_UTC_START: int = int(os.getenv("TRADE_BLOCK_UTC_START", "-1"))
TRADE_BLOCK_UTC_END:   int = int(os.getenv("TRADE_BLOCK_UTC_END",   "-1"))

# Use IOC (market-taker) for entry orders instead of ALO (maker).
# IOC enters immediately at the ask/bid price, avoiding the ALO fill-at-extreme timing issue.
# Adds ~0.045% fee per entry vs ALO but entries happen on the OFI signal, not a counter-move.
ENTRY_IOC: bool = os.getenv("ENTRY_IOC", "").lower() in ("1", "true", "yes")

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
# Live-test scale factor
# ---------------------------------------------------------------------------
# When running with minimum position size (0.001 BTC) to validate strategy
# with real exchange mechanics, set LIVE_TEST_SCALE to the ratio of the
# intended full-size position to the test size.
# Example: full size 0.010 BTC, test size 0.001 BTC → LIVE_TEST_SCALE=10
# All P&L logs then show both actual and projected-at-scale numbers.
LIVE_TEST_SCALE: float = float(os.getenv("LIVE_TEST_SCALE", "1.0"))

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
    assert OFI_PERSISTENCE_TICKS >= 1
    assert MAX_INVENTORY_BTC > 0
    assert 0 < STOP_LOSS_PCT < 1
    assert MAX_DAILY_LOSS_USD > 0
    if OBSERVER_MODE:
        import logging
        logging.warning("[config] No PRIVATE_KEY set — running in OBSERVER MODE (no orders will be placed)")
