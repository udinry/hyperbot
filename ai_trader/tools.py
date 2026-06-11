"""Tool definitions + executors for the trading agent.

Two classes of tool:
  READ  (get_market, get_account, get_strategy_signal) — always safe, no side effects.
  WRITE (place_order, halt_trading) — pass through the RiskEngine; place_order is
        additionally a no-op unless live execution is explicitly enabled.

The executors return plain dicts that are JSON-serialised back to the model. On
rejection they set {"ok": False, "error": ...} with is_error=True so the model
sees the failure and can adapt (it cannot retry its way past a hard limit).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any, Optional

import strategy_bridge
from risk_engine import OrderRequest, RiskEngine

logger = logging.getLogger("ai_trader.tools")

# ---------------------------------------------------------------------------
# Tool schemas advertised to Claude
# ---------------------------------------------------------------------------
TOOL_SCHEMAS: list[dict] = [
    {
        "name": "get_strategy_signal",
        "description": (
            "Return the VALIDATED trend model's current view for a coin "
            "(signal_fraction 0..1, vol_scale, target_fraction, interpretation). "
            "This is the ground-truth edge — backtested out-of-sample. Always "
            "consult it before proposing any trade. Do NOT trade against its sign "
            "(it is flat/cash in downtrends by design)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"coin": {"type": "string", "description": "e.g. BTC"}},
            "required": ["coin"],
        },
    },
    {
        "name": "get_account",
        "description": (
            "Return perp equity (USD), open positions per coin (signed BTC/ETH/... "
            "and USD notional), and current mark prices. Read-only."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_market",
        "description": (
            "Return recent daily price context for a coin: last close, 7d and 30d "
            "change %, and 30d realized volatility. Read-only context for reasoning."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"coin": {"type": "string"}},
            "required": ["coin"],
        },
    },
    {
        "name": "get_news",
        "description": (
            "Return recent crypto-market headlines (read-only, from public RSS). "
            "Use this for TWO purposes ONLY: (1) operational safety — if "
            "headlines indicate an exchange hack, stablecoin depeg, Hyperliquid "
            "outage, or similar infrastructure risk, call halt_trading; (2) to "
            "EXPLAIN market context in your summary. You MUST NOT let headlines "
            "change the position you take — the validated signal is the only "
            "thing that sets direction/size. News never overrides the model."
        ),
        "input_schema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "headlines to return (<=15)"}}},
    },
    {
        "name": "place_order",
        "description": (
            "Request to place a market-ish IOC order to move toward a target "
            "position. The deterministic risk engine validates size, notional, "
            "exposure, leverage, daily-loss and order-count limits BEFORE anything "
            "reaches the exchange and will REJECT anything out of bounds — propose "
            "responsibly. In dry-run mode no real order is sent. Only use to align "
            "the position with get_strategy_signal's target; never to express a "
            "personal market view that contradicts the model."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "coin": {"type": "string"},
                "side": {"type": "string", "enum": ["BUY", "SELL"]},
                "size": {"type": "number", "description": "coin units (e.g. 0.001 BTC)"},
                "reduce_only": {"type": "boolean", "default": False},
                "rationale": {"type": "string",
                              "description": "one sentence: why, tied to the signal"},
            },
            "required": ["coin", "side", "size", "rationale"],
        },
    },
    {
        "name": "halt_trading",
        "description": (
            "Immediately stop all further trading this session (kill switch). Use "
            "if data looks corrupt, the account state is surprising, or anything is "
            "unsafe. Reversible only by the human operator."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


def _post_info(api_url: str, payload: dict, timeout: int = 15, retries: int = 3):
    """POST to /info with retry+backoff — transient exchange 503s must not
    abort an agent cycle (observed live on Hyperliquid 2026-06-11)."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                api_url.rstrip("/") + "/info",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(2.0 * (2 ** attempt))
    raise last_exc


class ToolExecutor:
    """Holds runtime context and executes a tool call by name."""

    def __init__(self, api_url: str, address: str, risk: RiskEngine,
                 exchange=None, live: bool = False) -> None:
        self.api_url = api_url
        self.address = address
        self.risk = risk
        self.exchange = exchange
        self.live = live and exchange is not None
        self.audit: list[dict] = []

    # -- account snapshot used by several tools and the risk checks --
    def account_snapshot(self) -> dict:
        mids = _post_info(self.api_url, {"type": "allMids"})
        equity, positions = 0.0, {}
        if self.address:
            st = _post_info(self.api_url, {"type": "clearinghouseState",
                                           "user": self.address})
            equity = float(st["marginSummary"]["accountValue"])
            for ap in st.get("assetPositions", []):
                p = ap.get("position", {})
                if p.get("coin") and abs(float(p.get("szi", 0))) > 0:
                    positions[p["coin"]] = float(p["szi"])
        pos_usd = {c: positions[c] * float(mids.get(c, 0)) for c in positions}
        return {"equity_usd": equity, "positions": positions,
                "positions_usd": pos_usd, "mids": mids}

    def execute(self, name: str, args: dict) -> tuple[dict, bool]:
        """Return (result_dict, is_error)."""
        try:
            fn = getattr(self, f"_tool_{name}", None)
            if fn is None:
                return {"ok": False, "error": f"unknown tool {name}"}, True
            return fn(args)
        except Exception as exc:  # never let a tool crash the loop
            logger.error("tool %s failed: %s", name, exc, exc_info=True)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, True

    # ---- read tools ----
    def _tool_get_strategy_signal(self, args):
        return {"ok": True, **strategy_bridge.strategy_signal(args["coin"])}, False

    def _tool_get_account(self, args):
        snap = self.account_snapshot()
        return {"ok": True, "equity_usd": round(snap["equity_usd"], 2),
                "positions": snap["positions"],
                "positions_usd": {k: round(v, 2) for k, v in snap["positions_usd"].items()}}, False

    def _tool_get_market(self, args):
        coin = args["coin"]
        closes = strategy_bridge.trend_bot.fetch_daily_closes(coin, days=40)
        if len(closes) < 31:
            return {"ok": False, "error": "insufficient history"}, True
        import math
        rets = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(-30, 0)]
        mu = sum(rets) / len(rets)
        vol = math.sqrt(sum((x-mu)**2 for x in rets)/len(rets)) * math.sqrt(365)
        return {"ok": True, "coin": coin, "last_close": closes[-1],
                "change_7d_pct": round((closes[-1]/closes[-8]-1)*100, 2),
                "change_30d_pct": round((closes[-1]/closes[-31]-1)*100, 2),
                "realized_vol_ann_pct": round(vol*100, 1)}, False

    def _tool_get_news(self, args):
        import urllib.request, xml.etree.ElementTree as ET
        limit = min(int(args.get("limit", 8) or 8), 15)
        feeds = ["https://www.coindesk.com/arc/outboundfeeds/rss/",
                 "https://cointelegraph.com/rss"]
        heads = []
        for url in feeds:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=10).read()
                root = ET.fromstring(raw)
                for it in root.findall(".//item")[:limit]:
                    t = (it.findtext("title") or "").strip()
                    if t:
                        heads.append(t)
            except Exception as exc:
                logger.warning("news feed %s failed: %s", url, exc)
        if not heads:
            return {"ok": False, "error": "no headlines (feeds unreachable)"}, True
        # de-dupe, cap
        seen, uniq = set(), []
        for h in heads:
            if h not in seen:
                seen.add(h); uniq.append(h)
        return {"ok": True, "headlines": uniq[:limit],
                "reminder": "context/safety only — does NOT change position"}, False

    # ---- write tools (risk-gated) ----
    def _tool_place_order(self, args):
        coin = args["coin"]
        is_buy = args["side"].upper() == "BUY"
        size = float(args["size"])
        snap = self.account_snapshot()
        price = float(snap["mids"].get(coin, 0))
        reduce_only = bool(args.get("reduce_only", False))

        # Signal-consistency gate (deterministic encoding of operator rule 2):
        # the strategy is long/flat — no order may open or grow a position the
        # model doesn't want. Prompt rules are soft; this is code.
        if not reduce_only:
            if not is_buy:
                cur = snap["positions"].get(coin, 0.0)
                if cur <= 0:
                    return {"ok": False, "error":
                            "signal gate: strategy is long/flat only — SELLs "
                            "must be reduce_only (no new shorts)"}, True
            else:
                try:
                    sig = strategy_bridge.strategy_signal(coin)
                except Exception as exc:
                    return {"ok": False, "error":
                            f"signal gate: cannot verify signal ({exc}) — "
                            f"refusing to open a position blind"}, True
                if sig["target_fraction"] <= 0:
                    return {"ok": False, "error":
                            "signal gate: model is FLAT for "
                            f"{coin} (target_fraction=0) — no new longs"}, True
        req = OrderRequest(coin=coin, is_buy=is_buy, size=size, price=price,
                           reduce_only=reduce_only)
        decision = self.risk.check_order(req, snap["equity_usd"], snap["positions_usd"])
        record = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "coin": coin, "side": args["side"], "size": size,
                  "price": price, "rationale": args.get("rationale", ""),
                  "approved": decision.approved, "reason": decision.reason,
                  "live": self.live}
        if not decision.approved:
            self.audit.append({**record, "result": "REJECTED"})
            return {"ok": False, "error": f"risk engine rejected: {decision.reason}"}, True

        self.risk.record_order()
        if not self.live:
            self.audit.append({**record, "result": "DRY_RUN"})
            return {"ok": True, "dry_run": True,
                    "would_place": f"{args['side']} {size} {coin} @ ~{price}",
                    "note": "no real order sent (dry-run)"}, False

        limit = round(price * (1.005 if is_buy else 0.995), 2)
        res = self.exchange.order(coin, is_buy, size, limit,
                                  order_type={"limit": {"tif": "Ioc"}},
                                  reduce_only=req.reduce_only)
        self.audit.append({**record, "result": "SENT", "exchange_response": str(res)[:300]})
        return {"ok": True, "placed": True, "response": res}, False

    def _tool_halt_trading(self, args):
        self.risk.halt(args.get("reason", "agent requested halt"))
        self.audit.append({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "action": "HALT", "reason": args.get("reason", "")})
        return {"ok": True, "halted": True, "reason": args.get("reason", "")}, False
