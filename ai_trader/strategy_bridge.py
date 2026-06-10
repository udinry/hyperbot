"""Bridge to the VALIDATED strategy. The AI does not invent edge — it reads the
signal computed by trend_bot's walk-forward-validated ensemble and reasons about
execution around it.

Keeping this as the single source of "what the quant model says" means the LLM's
job is operation and judgment within proven bounds, not market prediction (which
it cannot backtest and must not be trusted to do).
"""
from __future__ import annotations

import sys
from pathlib import Path

# the validated strategy lives in hft_bot/trend_bot.py
_HFT = Path(__file__).resolve().parents[1] / "hft_bot"
sys.path.insert(0, str(_HFT))

import trend_bot  # noqa: E402


def strategy_signal(coin: str, days: int = 400) -> dict:
    """Return the validated model's current view for one coin.

    signal_fraction in {0,.25,.5,.75,1} (long conviction), vol_scale in (0,1],
    and target_fraction = the product = fraction of the coin's capital slice the
    model wants deployed long. This is the GROUND TRUTH the agent must respect;
    the AI may choose to defer/scale a rebalance but should not invert the sign.
    """
    closes = trend_bot.fetch_daily_closes(coin, days=days)
    frac = trend_bot.ensemble_fraction(closes)
    scale = trend_bot.vol_scale(closes)
    return {
        "coin": coin,
        "signal_fraction": round(frac, 3),
        "vol_scale": round(scale, 3),
        "target_fraction": round(frac * scale, 4),
        "last_close": closes[-1] if closes else None,
        "interpretation": _interpret(frac, scale),
    }


def _interpret(frac: float, scale: float) -> str:
    if frac == 0:
        return "Downtrend — model is FLAT (cash). Do not initiate longs."
    target = frac * scale
    bias = "full" if frac == 1 else "partial"
    vol = "vol-reduced" if scale < 0.95 else "normal-vol"
    return (f"Uptrend — model wants {bias} long, {vol} "
            f"(deploy ~{target*100:.0f}% of this coin's slice).")
