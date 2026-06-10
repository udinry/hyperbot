from __future__ import annotations

import hmac
import json
import logging
import os
import re
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

WALLET_ADDRESS = os.getenv("HYPERBOT_WALLET_ADDRESS", "0x70C780d4e1497598eEB0ae54CCA6011CD55FF89D")
HL_API_URL = "https://api.hyperliquid.xyz/info"

BOT_SERVICE = "hyperbot-bot"
BOT_LOG = Path("/opt/hyperbot/hft_bot/bot.log")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# App-level auth. This service can start/stop a live trading bot — it must not
# rely solely on the reverse proxy being configured correctly. Set
# HYPERBOT_UI_USER / HYPERBOT_UI_PASSWORD in the service environment; when
# unset, a prominent warning is logged and behaviour is unchanged (back-compat
# with the existing nginx basic-auth deployment).
# ---------------------------------------------------------------------------
_UI_USER = os.getenv("HYPERBOT_UI_USER", "")
_UI_PASS = os.getenv("HYPERBOT_UI_PASSWORD", "")
if not (_UI_USER and _UI_PASS):
    logging.getLogger(__name__).warning(
        "HYPERBOT_UI_USER/HYPERBOT_UI_PASSWORD not set — management UI relies "
        "entirely on the reverse proxy for authentication. Set them in the "
        "hyperbot-ui.service environment for defence in depth."
    )


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if _UI_USER and _UI_PASS:
            auth = request.authorization
            ok = (
                auth is not None
                and hmac.compare_digest(auth.username or "", _UI_USER)
                and hmac.compare_digest(auth.password or "", _UI_PASS)
            )
            if not ok:
                return Response(
                    "Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="hyperbot"'},
                )
        return fn(*args, **kwargs)
    return wrapper

_STATE_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2})\.\d+ \[INFO\] main \| STATE \| "
    r"status=(\w+) inv=([+\-\d.]+)BTC entry=([\d.]+) mid=([\d.]+|N/A) "
    r"unrealPnL=([+\-\d.]+)\$ realPnL=([+\-\d.]+)\$ fills=(\d+) open_orders=(\d+)"
)


def _svc_status() -> str:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", BOT_SERVICE],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip()
    except Exception:
        return "unknown"


def _hl_post(payload: dict):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HL_API_URL, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _period_start_ms(days: int) -> int:
    """Unix ms for start of period. days=0 → all time. days=1 → UTC midnight today."""
    if days == 0:
        return 0
    if days == 1:
        now = datetime.now(timezone.utc)
        sod = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(sod.timestamp() * 1000)
    start = datetime.now(timezone.utc) - timedelta(days=days)
    return int(start.timestamp() * 1000)


def _fetch_exchange_fills(since_ms: int = 0) -> list:
    """Pull BTC fills for the master account. Returns oldest-first with cumulative PnL (gross and net)."""
    raw = _hl_post({"type": "userFills", "user": WALLET_ADDRESS})
    # HL returns newest-first; sort ascending so cumulative sums are chronological
    raw = sorted(raw, key=lambda f: int(f.get("time", 0)))
    fills = []
    cum_gross = 0.0
    cum_fees  = 0.0
    cum_net   = 0.0
    for f in raw:
        if f.get("coin") != "BTC":
            continue
        t = int(f.get("time", 0))
        if since_ms and t < since_ms:
            continue
        cpnl = float(f.get("closedPnl", 0))
        fee  = abs(float(f.get("fee", 0)))   # always positive cost
        net  = cpnl - fee
        cum_gross += cpnl
        cum_fees  += fee
        cum_net   += net
        fills.append({
            "time_ms":    t,
            "oid":        int(f.get("oid", 0)),
            "side":       "BUY" if f.get("side") == "B" else "SELL",
            "dir":        f.get("dir", ""),
            "price":      float(f.get("px", 0)),
            "size":       float(f.get("sz", 0)),
            "fee":        round(fee, 4),
            "closed_pnl": round(cpnl, 4),
            "net_pnl":    round(net, 4),
            "cum_gross":  round(cum_gross, 4),
            "cum_fees":   round(cum_fees, 4),
            "cum_net":    round(cum_net, 4),
        })
    return fills  # oldest-first; frontend reverses for display


def _parse_log():
    last_state = {}
    if not BOT_LOG.exists():
        return last_state
    try:
        with open(BOT_LOG, "r", errors="replace") as fh:
            for line in fh:
                m = _STATE_RE.search(line)
                if m:
                    ts, status, inv, entry, mid_s, unreal, real, fills_n, oo = m.groups()
                    last_state = {
                        "time": ts,
                        "status": status,
                        "inventory": float(inv),
                        "entry_price": float(entry) if entry != "0.00" else None,
                        "mid": float(mid_s) if mid_s != "N/A" else None,
                        "unrealized_pnl": float(unreal),
                        "realized_pnl": float(real),
                        "fills": int(fills_n),
                        "open_orders": int(oo),
                    }
    except Exception:
        pass
    return last_state


@app.route("/hyperbot/")
@app.route("/hyperbot")
@require_auth
def index():
    return render_template("hyperbot.html")


@app.route("/hyperbot/api/status")
@require_auth
def api_status():
    last_state = _parse_log()
    try:
        fills = _fetch_exchange_fills(since_ms=_period_start_ms(1))
        if fills:
            last_state["realized_pnl"]  = fills[-1]["cum_gross"]
            last_state["net_pnl"]        = fills[-1]["cum_net"]
            last_state["total_fees"]     = fills[-1]["cum_fees"]
            last_state["fills"]          = len(fills)
        else:
            last_state.setdefault("realized_pnl", 0.0)
            last_state.setdefault("net_pnl", 0.0)
            last_state.setdefault("total_fees", 0.0)
    except Exception:
        pass
    return jsonify({"service": _svc_status(), "state": last_state})


@app.route("/hyperbot/api/trades")
@require_auth
def api_trades():
    days = request.args.get("days", 1, type=int)
    try:
        fills = _fetch_exchange_fills(since_ms=_period_start_ms(days))
    except Exception as e:
        return jsonify({"error": str(e), "trades": []}), 500
    return jsonify({"trades": fills, "total": len(fills)})


@app.route("/hyperbot/api/pnl")
@require_auth
def api_pnl():
    days = request.args.get("days", 1, type=int)
    try:
        fills = _fetch_exchange_fills(since_ms=_period_start_ms(days))
    except Exception as e:
        return jsonify({"error": str(e), "series": []}), 500
    series = [{"t": f["time_ms"], "gross": f["cum_gross"], "net": f["cum_net"]} for f in fills]
    return jsonify({"series": series})


@app.route("/hyperbot/api/start", methods=["POST"])
@require_auth
def api_start():
    subprocess.run(["sudo", "systemctl", "start", BOT_SERVICE], timeout=5)
    return jsonify({"ok": True})


@app.route("/hyperbot/api/stop", methods=["POST"])
@require_auth
def api_stop():
    subprocess.run(["sudo", "systemctl", "stop", BOT_SERVICE], timeout=5)
    return jsonify({"ok": True})


@app.route("/hyperbot/api/portfolio")
@require_auth
def api_portfolio():
    try:
        perp = _hl_post({"type": "clearinghouseState", "user": WALLET_ADDRESS})
        perp_equity  = float(perp["marginSummary"]["accountValue"])
        withdrawable = float(perp.get("withdrawable", 0))

        # Spot USDC balance (separate from the perp trading account)
        spot_usdc = 0.0
        try:
            spot = _hl_post({"type": "spotClearinghouseState", "user": WALLET_ADDRESS})
            for b in spot.get("balances", []):
                if b.get("coin") == "USDC":
                    spot_usdc = float(b.get("total", 0))
                    break
        except Exception:
            pass

        position = None
        for ap in perp.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == "BTC":
                szi = float(pos.get("szi", 0))
                if abs(szi) > 0:
                    position = {
                        "size":           szi,
                        "entry_price":    float(pos["entryPx"]) if pos.get("entryPx") else None,
                        "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                        "position_value": float(pos.get("positionValue", 0)),
                        "liquidation_px": pos.get("liquidationPx"),
                    }
                break

        return jsonify({
            "account_value": round(perp_equity, 2),
            "spot_usdc":     round(spot_usdc, 2),
            "withdrawable":  round(withdrawable, 2),
            "position":      position,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/hyperbot/api/log")
@require_auth
def api_log():
    lines = []
    if BOT_LOG.exists():
        try:
            with open(BOT_LOG, "r", errors="replace") as fh:
                lines = fh.readlines()[-100:]
        except Exception:
            pass
    return jsonify({"lines": [l.rstrip() for l in lines]})


PAPER_SERVICE = "hyperbot-paper"

_PAPER_FILL_RE  = re.compile(
    r"(\d{2}:\d{2}:\d{2}).*\[PAPER\] FILL (LONG|SHORT) ([\d.]+) BTC @ \$([\d.,]+) \| SL=\$([\d.,]+) TP=\$([\d.,]+)"
)
_PAPER_CLOSE_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}).*\[PAPER\] CLOSE (LONG|SHORT) @ \$([\d.,]+) \| reason=(\w+) \| net=\$([+\-\d.]+) \| totalVPnL=\$([+\-\d.]+)"
)
_PAPER_SIGNAL_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}).*\[PAPER\] SIGNAL (BUY|SELL) OFI=([+\-\d.]+)"
)


def _paper_logs(n: int = 500) -> list[str]:
    try:
        r = subprocess.run(
            ["journalctl", "-u", PAPER_SERVICE, "--no-pager", "-n", str(n)],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.splitlines()
    except Exception:
        return []


def _parse_paper_state() -> dict:
    lines = _paper_logs(2000)
    fills  = []
    closes = []
    signals = 0
    vpnl   = None

    for line in lines:
        if (m := _PAPER_FILL_RE.search(line)):
            fills.append({
                "time": m.group(1),
                "dir":  m.group(2),
                "size": float(m.group(3)),
                "price": float(m.group(4).replace(",", "")),
                "sl":    float(m.group(5).replace(",", "")),
                "tp":    float(m.group(6).replace(",", "")),
            })
        elif (m := _PAPER_CLOSE_RE.search(line)):
            closes.append({
                "time":   m.group(1),
                "dir":    m.group(2),
                "exit":   float(m.group(3).replace(",", "")),
                "reason": m.group(4),
                "net":    float(m.group(5)),
                "vpnl":   float(m.group(6)),
            })
            vpnl = float(m.group(6))
        elif _PAPER_SIGNAL_RE.search(line):
            signals += 1

    current_position = None
    if len(fills) > len(closes):
        f = fills[-1]
        current_position = f

    wins  = sum(1 for c in closes if c["net"] > 0)
    losses = len(closes) - wins

    return {
        "service":  "active" if _svc_status_paper() == "active" else "inactive",
        "signals":  signals,
        "fills":    len(fills),
        "closes":   len(closes),
        "wins":     wins,
        "losses":   losses,
        "vpnl":     vpnl or 0.0,
        "position": current_position,
        "history":  closes[-50:],
    }


def _svc_status_paper() -> str:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", PAPER_SERVICE],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip()
    except Exception:
        return "unknown"


@app.route("/hyperbot/paperbot")
@app.route("/hyperbot/paperbot/")
@require_auth
def paperbot_index():
    return render_template("paperbot.html")


@app.route("/hyperbot/api/paper/status")
@require_auth
def api_paper_status():
    return jsonify(_parse_paper_state())


@app.route("/hyperbot/api/paper/log")
@require_auth
def api_paper_log():
    lines = _paper_logs(200)
    paper_lines = [l for l in lines if "[PAPER]" in l or "SIGNAL" in l or "FILL" in l or "CLOSE" in l]
    return jsonify({"lines": paper_lines[-100:]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
