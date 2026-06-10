"""AI trading agent — Claude as the operator, validated strategy as the edge,
deterministic risk engine as the seatbelt.

   You ──"start trading" / "status" / "stop"──► Agent (Claude)
                                                   │ reads tools
                          get_strategy_signal ◄────┤ (validated edge)
                          get_account / get_market ─┤ (read-only context)
                          place_order ─────────────►│ RiskEngine (hard limits)
                                                     ▼
                                               Hyperliquid

The model NEVER predicts the market freehand: it executes the trend model's
target, explains its reasoning in plain English, and is bounded by limits it
cannot override. Dry-run is the default; live trading requires both a key and
an explicit --live flag.

Usage:
  python agent.py status            # one-shot: signals + account, no trades
  python agent.py trade             # one decision cycle, DRY-RUN (no real orders)
  python agent.py trade --live      # one cycle, REAL orders (needs keys + opt-in)
  python agent.py loop --interval 86400   # recurring daily cycles
Env: ANTHROPIC_API_KEY, AI_TRADER_MODEL, plus the risk + HL vars (see config.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import settings
from risk_engine import RiskEngine
from tools import TOOL_SCHEMAS, ToolExecutor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s")
logger = logging.getLogger("ai_trader.agent")

SYSTEM_PROMPT = """You are the operator of a systematic crypto trading account on \
Hyperliquid. You are NOT a market forecaster — your edge comes entirely from a \
walk-forward-validated daily trend model, exposed via get_strategy_signal. Your \
job is disciplined execution and clear explanation, not prediction.

Rules you must follow:
1. Always call get_strategy_signal for each coin before deciding anything.
2. The model's target_fraction is ground truth. Move the position TOWARD it.
   Never take a position whose sign contradicts the model (it is flat in
   downtrends on purpose — flat means hold cash, do not invent longs).
3. Use get_account to see current positions and get_market for context. Only
   rebalance when the gap between current and target is material (≳15% of the
   coin's slice); otherwise do nothing and say so. Avoid churn.
4. Every place_order must cite the signal in its rationale. The risk engine will
   reject anything over the limits — if rejected, accept it, do not fight it.
5. If anything looks wrong (stale prices, surprising balance, contradictory
   data), call halt_trading instead of guessing.
6. Be concise. End with a plain-English summary a non-expert can audit: what the
   model said, what you did or didn't do, and why.

You are conservative by default. Doing nothing is a valid, common outcome."""


def build_runtime(live: bool):
    cfg = settings.load()
    exchange, address = None, cfg.account_address
    if live and not cfg.observer_mode:
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        wallet = Account.from_key(cfg.private_key)
        address = cfg.account_address or wallet.address
        exchange = Exchange(wallet=wallet, base_url=cfg.api_url,
                            account_address=address if cfg.account_address else None)
        logger.warning("LIVE MODE — real orders enabled for %s", address)
    elif live:
        logger.warning("--live requested but no PRIVATE_KEY — staying in dry-run")
    risk = RiskEngine(cfg.risk_limits)
    execu = ToolExecutor(cfg.api_url, address, risk, exchange=exchange, live=live)
    return cfg, execu


def run_cycle(prompt: str, live: bool, max_steps: int = 12) -> str:
    cfg, execu = build_runtime(live)
    try:
        import anthropic
    except ImportError:
        return "anthropic SDK not installed — `pip install anthropic`."
    if not cfg.anthropic_api_key:
        return ("ANTHROPIC_API_KEY not set. Showing strategy + account instead:\n"
                + _deterministic_status(execu))

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    messages = [{"role": "user", "content": prompt}]
    final_text = ""
    for _ in range(max_steps):
        resp = client.messages.create(
            model=cfg.model, max_tokens=1500, system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        text = "".join(b.text for b in resp.content if b.type == "text")
        if text:
            final_text = text
        if resp.stop_reason != "tool_use":
            break
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                logger.info("tool: %s(%s)", b.name, json.dumps(b.input))
                out, is_err = execu.execute(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out), "is_error": is_err})
        messages.append({"role": "user", "content": results})

    _write_audit(execu, final_text)
    return final_text


def _deterministic_status(execu: ToolExecutor) -> str:
    """No-API fallback: print the validated signal + account for each coin."""
    lines = []
    try:
        snap = execu.account_snapshot()
        lines.append(f"Equity: ${snap['equity_usd']:.2f} | positions: "
                     f"{snap['positions'] or 'flat'}")
    except Exception as exc:
        lines.append(f"account fetch failed: {exc}")
    for coin in execu.risk.limits.allowed_coins:
        try:
            sig, _ = execu._tool_get_strategy_signal({"coin": coin})
            lines.append(f"{coin}: target_fraction={sig['target_fraction']} "
                         f"— {sig['interpretation']}")
        except Exception as exc:
            lines.append(f"{coin}: signal failed: {exc}")
    return "\n".join(lines)


def _write_audit(execu: ToolExecutor, summary: str) -> None:
    if not execu.audit and not summary:
        return
    path = Path(__file__).resolve().parent / "audit_log.jsonl"
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "summary": summary, "actions": execu.audit}
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    logger.info("audit appended (%d action(s))", len(execu.audit))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["status", "trade", "loop"])
    ap.add_argument("--live", action="store_true", help="enable REAL orders")
    ap.add_argument("--interval", type=int, default=86400, help="loop seconds")
    args = ap.parse_args()

    coins = ", ".join(settings.load().risk_limits.allowed_coins)
    if args.command == "status":
        cfg, execu = build_runtime(live=False)
        if cfg.anthropic_api_key:
            print(run_cycle(
                f"Give me a status report on {coins}: the model's current signal "
                f"for each, my positions, and whether any rebalance is warranted. "
                f"Do not place orders.", live=False))
        else:
            print(_deterministic_status(execu))
        return

    prompt = (f"Run today's trading cycle for {coins}. For each coin: check the "
              f"strategy signal, my account, and market context; rebalance toward "
              f"the model's target only if the gap is material; otherwise hold. "
              f"Summarise what you did and why.")
    if args.command == "trade":
        print(run_cycle(prompt, live=args.live))
        return

    logger.info("Daily loop every %ds (live=%s). Ctrl-C to stop.", args.interval, args.live)
    while True:
        try:
            print(run_cycle(prompt, live=args.live))
        except Exception as exc:
            logger.error("cycle failed: %s", exc, exc_info=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
