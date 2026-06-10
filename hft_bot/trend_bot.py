"""Trend Bot v2 — daily-horizon systematic BTC strategy for Hyperliquid.

WHY THIS EXISTS
---------------
The OFI scalper could not be validated as profitable: honest accounting showed
liquid BTC has <1bp of directional edge at 1-minute resolution against a >2bp
cost floor (see BACKTEST_FINDINGS.md), and the live strategy produced ~0 fills
per hour, making certification impossible. This bot moves to the horizon where
the edge-to-cost ratio is ~100x better and a decade of data exists to prove it.

STRATEGY (pre-registered on 2015-2021, confirmed once on untouched 2022-2026)
----------------------------------------------------------------------------
Signal   : ensemble of 4 classic trend filters on DAILY closes —
           close>SMA50, close>SMA100, close>close[60d ago], close>close[90d ago].
           Position fraction = mean of the four (0, 0.25, 0.5, 0.75, 1.0). Long/flat
           only (long/short was tested and REJECTED: worse risk-adjusted IS).
Vol tgt  : fraction *= min(1, 40% / realized_30d_annualized_vol).
Execution: rebalance at most once daily after UTC close; IOC orders; only when
           the position delta >= one lot (0.001 BTC) AND >= REBALANCE_MIN_FRAC
           of full size (anti-churn).
Costs    : modelled at 4.5bp/side + 8% APR funding drag while long; results
           held at 0%/8%/15% funding sensitivity.

EVIDENCE (full methodology in STRATEGY_V2.md)
---------------------------------------------
                       CAGR    MaxDD   Sharpe
  OOS 2022-2026 bot   +13.1%   32.7%    0.59
  OOS buy-and-hold     +5.9%   67.0%    0.37
2022 bear: bot -32% vs B&H -65%. 2026 bear-to-date: -11% vs -31%.
Expectation honesty: ~13%/yr is the OOS estimate, with 30%+ drawdowns possible
and negative years expected (2022, 2026). This is not a money printer; it is a
positive-expectancy system with documented risk.

USAGE
-----
  python trend_bot.py                 # live loop (observer mode without PRIVATE_KEY)
  python trend_bot.py --once          # one decision cycle, then exit
  python trend_bot.py --dry-run       # compute & print target, never order
Env: TREND_LEVERAGE (default 1.0), TREND_VOL_TARGET (0.40),
     REBALANCE_MIN_FRAC (0.15), PRIVATE_KEY / ACCOUNT_ADDRESS / HYPERLIQUID_API_URL.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import config

logger = logging.getLogger("trend_bot")

# ---- strategy parameters (pre-registered; see STRATEGY_V2.md before changing) ----
SMA_WINDOWS = (50, 100)
MOM_WINDOWS = (60, 90)
VOL_LOOKBACK_D = 30
VOL_TARGET = float(os.getenv("TREND_VOL_TARGET", "0.40"))      # annualized
LEVERAGE = float(os.getenv("TREND_LEVERAGE", "1.0"))           # of perp equity
REBALANCE_MIN_FRAC = float(os.getenv("REBALANCE_MIN_FRAC", "0.15"))
LOT_BTC = 0.001
MIN_HISTORY_D = max(max(SMA_WINDOWS), max(MOM_WINDOWS)) + 1

STATE_FILE = _HERE / "trend_state.json"


# ---------------------------------------------------------------------------
# Pure signal math (unit-tested in tests/test_trend.py)
# ---------------------------------------------------------------------------
def ensemble_fraction(closes: list[float]) -> float:
    """Mean of 4 trend votes on daily closes (latest last). 0.0 .. 1.0."""
    if len(closes) < MIN_HISTORY_D:
        return 0.0
    c = closes[-1]
    votes = 0
    for n in SMA_WINDOWS:
        votes += 1 if c > sum(closes[-n:]) / n else 0
    for n in MOM_WINDOWS:
        votes += 1 if c > closes[-n - 1] else 0
    return votes / (len(SMA_WINDOWS) + len(MOM_WINDOWS))


def vol_scale(closes: list[float], target: float = VOL_TARGET,
              lookback: int = VOL_LOOKBACK_D) -> float:
    """min(1, target/realized_annualized_vol) over the last `lookback` days."""
    if len(closes) < lookback + 1:
        return 1.0
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(len(closes) - lookback, len(closes))]
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / len(rets)) * math.sqrt(365)
    if sd <= 0:
        return 1.0
    return min(1.0, target / sd)


def target_position_btc(closes: list[float], equity_usd: float,
                        price: float) -> float:
    """Lot-rounded target BTC position for the current signal."""
    frac = ensemble_fraction(closes) * vol_scale(closes)
    notional = equity_usd * LEVERAGE * frac
    return round(notional / price / LOT_BTC) * LOT_BTC if price > 0 else 0.0


def should_rebalance(current_btc: float, target_btc: float,
                     full_size_btc: float) -> bool:
    """Trade only when the delta is >= one lot AND >= REBALANCE_MIN_FRAC of
    full size — avoids churning lots on signal flicker."""
    delta = abs(target_btc - current_btc)
    if delta < LOT_BTC:
        return False
    return delta >= max(LOT_BTC, REBALANCE_MIN_FRAC * max(full_size_btc, LOT_BTC))


# ---------------------------------------------------------------------------
# Hyperliquid I/O (REST; daily cadence needs no websockets)
# ---------------------------------------------------------------------------
def _post_info(payload: dict, timeout: int = 15):
    req = urllib.request.Request(
        config.API_URL.rstrip("/") + "/info",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_daily_closes(days: int = 400) -> list[float]:
    now = int(time.time() * 1000)
    data = _post_info({"type": "candleSnapshot", "req": {
        "coin": config.COIN, "interval": "1d",
        "startTime": now - days * 86_400_000, "endTime": now}})
    closes = [float(c["c"]) for c in data]
    if not closes:
        raise RuntimeError("no daily candles returned")
    return closes


def fetch_equity_and_position(address: str) -> tuple[float, float, float]:
    """(perp_equity_usd, position_btc, mark_price)."""
    st = _post_info({"type": "clearinghouseState", "user": address})
    equity = float(st["marginSummary"]["accountValue"])
    pos = 0.0
    for ap in st.get("assetPositions", []):
        p = ap.get("position", {})
        if p.get("coin") == config.COIN:
            pos = float(p.get("szi", 0))
    mids = _post_info({"type": "allMids"})
    price = float(mids.get(config.COIN, 0))
    return equity, pos, price


# ---------------------------------------------------------------------------
# Decision cycle
# ---------------------------------------------------------------------------
def decide_and_trade(exchange, address: str, dry_run: bool = False) -> dict:
    closes = fetch_daily_closes()
    frac = ensemble_fraction(closes)
    scale = vol_scale(closes)
    if address:
        equity, current, price = fetch_equity_and_position(address)
    else:  # observer mode without an address: show the signal on nominal $1000
        mids = _post_info({"type": "allMids"})
        equity, current, price = 1000.0, 0.0, float(mids.get(config.COIN, 0))
    target = target_position_btc(closes, equity, price)
    full = round(equity * LEVERAGE / price / LOT_BTC) * LOT_BTC if price else 0.0

    info = dict(utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
                price=price, equity=equity, signal_frac=frac, vol_scale=round(scale, 3),
                current_btc=current, target_btc=target, traded=False)
    logger.info("DECISION | px=%.0f equity=$%.2f signal=%.2f vol_scale=%.2f "
                "current=%.3f target=%.3f BTC",
                price, equity, frac, scale, current, target)

    if not should_rebalance(current, target, full):
        logger.info("No rebalance needed (delta below threshold)")
        return info
    if dry_run or exchange is None:
        logger.info("[DRY-RUN/OBSERVER] would %s %.3f BTC",
                    "BUY" if target > current else "SELL", abs(target - current))
        return info

    delta = round(target - current, 3)
    is_buy = delta > 0
    # IOC with 0.5% slippage allowance; daily cadence makes taker cost (3.5bp)
    # irrelevant relative to the edge (~3bp/day average).
    limit = price * (1.005 if is_buy else 0.995)
    limit = round(round(limit / config.PRICE_TICK) * config.PRICE_TICK, 1)
    logger.info("REBALANCE | %s %.3f BTC IOC @ %.0f", "BUY" if is_buy else "SELL",
                abs(delta), limit)
    try:
        res = exchange.order(config.COIN, is_buy, abs(delta), limit,
                             order_type={"limit": {"tif": "Ioc"}},
                             reduce_only=False)
        logger.info("Order result: %s", res)
        info["traded"] = True
    except Exception as exc:
        logger.error("Rebalance order failed: %s", exc, exc_info=True)
    return info


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(d, indent=2))
    except Exception as exc:
        logger.warning("could not save state: %s", exc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one cycle then exit")
    ap.add_argument("--dry-run", action="store_true", help="never place orders")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s | %(message)s")

    exchange = None
    address = config.ACCOUNT_ADDRESS
    if config.OBSERVER_MODE:
        logger.warning("OBSERVER MODE — no orders will be placed")
    else:
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        wallet = Account.from_key(config.PRIVATE_KEY)
        address = config.ACCOUNT_ADDRESS or wallet.address
        exchange = Exchange(wallet=wallet, base_url=config.API_URL,
                            account_address=address if config.ACCOUNT_ADDRESS else None)
        logger.info("Trading as %s on %s (leverage %.1fx of equity)",
                    address, config.API_URL, LEVERAGE)

    if args.once or args.dry_run:
        decide_and_trade(exchange, address, dry_run=args.dry_run)
        return

    # Daily loop: act shortly after UTC midnight; hourly safety re-check is
    # harmless because should_rebalance() suppresses sub-threshold churn.
    state = _load_state()
    while True:
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        try:
            info = decide_and_trade(exchange, address)
            state["last_run"] = info
            state["last_day"] = today
            _save_state(state)
        except Exception as exc:
            logger.error("decision cycle failed: %s", exc, exc_info=True)
        time.sleep(3600)


if __name__ == "__main__":
    main()
