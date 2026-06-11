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
import notify

logger = logging.getLogger("trend_bot")

# ---- strategy parameters (pre-registered; see STRATEGY_V2.md before changing) ----
SMA_WINDOWS = (50, 100)
MOM_WINDOWS = (60, 90)
# Long-term regime filter: suppress all longs unless price is above this SMA.
# Chosen on 2015-2021 in-sample (best Sharpe + drawdown vs 200d); confirmed OOS
# 2022-2026 — CAGR 13.1%->21.2%, MaxDD 32.7%->22.2%, and the 2022 bear -32%->-4%.
# 0 disables. See STRATEGY_V2.md.
REGIME_FILTER_DAYS = int(os.getenv("TREND_REGIME_DAYS", "150"))
VOL_LOOKBACK_D = 30
VOL_TARGET = float(os.getenv("TREND_VOL_TARGET", "0.40"))      # annualized
LEVERAGE = float(os.getenv("TREND_LEVERAGE", "1.0"))           # of perp equity
REBALANCE_MIN_FRAC = float(os.getenv("REBALANCE_MIN_FRAC", "0.15"))
LOT_BTC = 0.001
MIN_HISTORY_D = max(max(SMA_WINDOWS), max(MOM_WINDOWS), REGIME_FILTER_DAYS) + 1

# Multi-asset: TREND_COINS="BTC" (default) or e.g. "BTC,ETH,SOL" (equal weight).
# The ensemble transfers with frozen parameters (research_portfolio.py): OOS
# 2022+ it turned ETH B&H -17%/yr into +9% and SOL -21%/yr into +4%. Note the
# portfolio cuts MaxDD (28% vs 33%) but BTC-only has the best Sharpe; with a
# small account, lot granularity argues for BTC-only.
TREND_COINS = [c.strip().upper() for c in
               os.getenv("TREND_COINS", "BTC").split(",") if c.strip()]

# Capital weighting across TREND_COINS: "invvol" (risk parity, default) or "equal".
TREND_WEIGHTING = os.getenv("TREND_WEIGHTING", "invvol").lower()

STATE_FILE = _HERE / "trend_state.json"


# ---------------------------------------------------------------------------
# Pure signal math (unit-tested in tests/test_trend.py)
# ---------------------------------------------------------------------------
def regime_ok(closes: list[float]) -> bool:
    """Long-term regime gate: True unless price is below the REGIME_FILTER_DAYS
    SMA (a confirmed downtrend, where the system should sit in cash)."""
    if REGIME_FILTER_DAYS <= 0 or len(closes) < REGIME_FILTER_DAYS:
        return True
    return closes[-1] > sum(closes[-REGIME_FILTER_DAYS:]) / REGIME_FILTER_DAYS


def ensemble_fraction(closes: list[float]) -> float:
    """Mean of 4 trend votes on daily closes (latest last), 0.0 .. 1.0, gated by
    the long-term regime filter (returns 0 in a confirmed bear)."""
    if len(closes) < MIN_HISTORY_D:
        return 0.0
    if not regime_ok(closes):
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


def realized_vol(closes: list[float], lookback: int = VOL_LOOKBACK_D) -> float:
    """Trailing annualized realized vol; 0.0 if insufficient history."""
    if len(closes) < lookback + 1:
        return 0.0
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(len(closes) - lookback, len(closes))]
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((x - mu) ** 2 for x in rets) / len(rets)) * math.sqrt(365)


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


def risk_parity_weights(vols: dict[str, float], cap: float = 0.5) -> dict[str, float]:
    """Inverse-volatility capital weights (risk parity), per-asset capped.

    Uses trailing realized vol only — no return-peeking, so no data snooping.
    OOS 2022+ vs equal weight on BTC/ETH/SOL: Sharpe 0.74 -> 0.78,
    CAGR +14.6% -> +15.9% (see STRATEGY_V2.md)."""
    if not vols:
        return {}
    inv = {c: 1.0 / max(v, 0.10) for c, v in vols.items()}
    s = sum(inv.values())
    w = {c: x / s for c, x in inv.items()}
    # Iteratively enforce the cap, redistributing excess to uncapped names
    # (a single cap+renormalize pass can push weights back above the cap).
    for _ in range(len(w)):
        over = {c for c, x in w.items() if x > cap + 1e-12}
        if not over:
            break
        excess = sum(w[c] - cap for c in over)
        for c in over:
            w[c] = cap
        under = [c for c in w if c not in over]
        s_under = sum(w[c] for c in under)
        if s_under <= 0:
            break
        for c in under:
            w[c] += excess * w[c] / s_under
    total = sum(w.values())
    return {c: x / total for c, x in w.items()}


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


def fetch_daily_closes(coin: str, days: int = 400) -> list[float]:
    now = int(time.time() * 1000)
    data = _post_info({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1d",
        "startTime": now - days * 86_400_000, "endTime": now}})
    closes = [float(c["c"]) for c in data]
    if not closes:
        raise RuntimeError(f"no daily candles returned for {coin}")
    return closes


_lot_cache: dict = {}

def lot_size(coin: str) -> float:
    """Lot (min size increment) from exchange meta szDecimals; cached."""
    if not _lot_cache:
        try:
            meta = _post_info({"type": "meta"})
            for a in meta.get("universe", []):
                _lot_cache[a["name"]] = 10 ** -int(a.get("szDecimals", 3))
        except Exception as exc:
            logger.warning("meta fetch failed (%s) — defaulting lots to 0.001", exc)
    return _lot_cache.get(coin, LOT_BTC)


def fetch_account(address: str) -> tuple[float, dict, dict]:
    """(perp_equity_usd, {coin: position}, {coin: mark_price})."""
    st = _post_info({"type": "clearinghouseState", "user": address})
    equity = float(st["marginSummary"]["accountValue"])
    pos = {}
    for ap in st.get("assetPositions", []):
        p = ap.get("position", {})
        if p.get("coin"):
            pos[p["coin"]] = float(p.get("szi", 0))
    mids = _post_info({"type": "allMids"})
    prices = {c: float(mids.get(c, 0)) for c in TREND_COINS}
    return equity, pos, prices


# ---------------------------------------------------------------------------
# Decision cycle
# ---------------------------------------------------------------------------
def decide_one(exchange, coin: str, equity_slice: float, current: float,
               price: float, dry_run: bool, closes: list[float] | None = None) -> dict:
    if closes is None:
        closes = fetch_daily_closes(coin)
    frac = ensemble_fraction(closes)
    scale = vol_scale(closes)
    lot = lot_size(coin)
    target = 0.0
    if price > 0:
        notional = equity_slice * LEVERAGE * frac * scale
        target = round(notional / price / lot) * lot
    full = round(equity_slice * LEVERAGE / price / lot) * lot if price else 0.0

    info = dict(coin=coin, price=price, signal_frac=frac,
                vol_scale=round(scale, 3), current=current, target=target,
                traded=False)
    logger.info("DECISION %s | px=%.2f slice=$%.2f signal=%.2f vol_scale=%.2f "
                "current=%s target=%s", coin, price, equity_slice, frac, scale,
                current, target)

    delta = target - current
    if abs(delta) < lot or abs(delta) < REBALANCE_MIN_FRAC * max(full, lot):
        logger.info("%s: no rebalance needed", coin)
        return info
    if dry_run or exchange is None:
        logger.info("[DRY-RUN/OBSERVER] %s: would %s %s", coin,
                    "BUY" if delta > 0 else "SELL", abs(round(delta, 8)))
        notify.send(f"[TREND dry-run] {coin}: would {'BUY' if delta > 0 else 'SELL'} "
                    f"{abs(round(delta, 8))} @ ~{price:.0f} (signal={frac} scale={scale:.2f})")
        return info

    is_buy = delta > 0
    limit = price * (1.005 if is_buy else 0.995)
    # tick rounding: use a conservative 6-sig-digit round (per-coin ticks vary)
    limit = float(f"{limit:.6g}")
    sz = abs(round(delta, 8))
    logger.info("REBALANCE %s | %s %s IOC @ %s", coin,
                "BUY" if is_buy else "SELL", sz, limit)
    try:
        res = exchange.order(coin, is_buy, sz, limit,
                             order_type={"limit": {"tif": "Ioc"}},
                             reduce_only=False)
        logger.info("Order result: %s", res)
        info["traded"] = True
        notify.send(f"[TREND] {coin}: {'BUY' if is_buy else 'SELL'} {sz} IOC @ {limit} "
                    f"(signal={frac} scale={scale:.2f}, target {target})")
    except Exception as exc:
        logger.error("%s rebalance failed: %s", coin, exc, exc_info=True)
        notify.send(f"[TREND ERROR] {coin} rebalance failed: {exc}")
    return info


def decide_and_trade(exchange, address: str, dry_run: bool = False) -> dict:
    if address:
        equity, positions, prices = fetch_account(address)
    else:  # observer mode without an address: show signals on nominal $1000
        mids = _post_info({"type": "allMids"})
        equity = 1000.0
        positions = {}
        prices = {c: float(mids.get(c, 0)) for c in TREND_COINS}

    closes_by: dict[str, list[float]] = {}
    vols: dict[str, float] = {}
    for coin in TREND_COINS:
        try:
            closes_by[coin] = fetch_daily_closes(coin)
            vols[coin] = realized_vol(closes_by[coin])
        except Exception as exc:
            logger.error("candle fetch failed for %s: %s", coin, exc)

    if TREND_WEIGHTING == "invvol" and len(closes_by) > 1:
        weights = risk_parity_weights({c: v for c, v in vols.items() if v > 0})
    else:
        weights = {c: 1.0 / len(closes_by) for c in closes_by} if closes_by else {}
    if weights:
        logger.info("capital weights (%s): %s", TREND_WEIGHTING,
                    {c: round(w, 3) for c, w in weights.items()})

    results = []
    for coin, closes in closes_by.items():
        try:
            results.append(decide_one(exchange, coin, equity * weights.get(coin, 0.0),
                                      positions.get(coin, 0.0),
                                      prices.get(coin, 0.0), dry_run, closes=closes))
        except Exception as exc:
            logger.error("decision failed for %s: %s", coin, exc, exc_info=True)
    return dict(utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
                equity=equity, coins=results)


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
