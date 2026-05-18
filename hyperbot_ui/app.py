from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, render_template

WALLET_ADDRESS = "0x70C780d4e1497598eEB0ae54CCA6011CD55FF89D"
HL_API_URL = "https://api.hyperliquid.xyz/info"

BOT_SERVICE = "hyperbot-bot"
BOT_LOG = Path("/opt/hyperbot/hft_bot/bot.log")

app = Flask(__name__)

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


def _hl_post(payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HL_API_URL, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _fetch_exchange_fills() -> list:
    """Pull all BTC fills for the master account from Hyperliquid REST.
    Returns oldest-first list with running cumulative PnL."""
    raw = _hl_post({"type": "userFills", "user": WALLET_ADDRESS})
    fills = []
    cum = 0.0
    for f in raw:
        if f.get("coin") != "BTC":
            continue
        cpnl = float(f.get("closedPnl", 0))
        fee  = float(f.get("fee", 0))
        cum += cpnl
        fills.append({
            "time_ms":    int(f.get("time", 0)),
            "oid":        int(f.get("oid", 0)),
            "side":       "BUY" if f.get("side") == "B" else "SELL",
            "dir":        f.get("dir", ""),
            "price":      float(f.get("px", 0)),
            "size":       float(f.get("sz", 0)),
            "fee":        round(fee, 4),
            "closed_pnl": round(cpnl, 4),
            "cum_pnl":    round(cum, 4),
        })
    return fills  # oldest-first; frontend reverses for display


def _parse_log():
    last_state = {}

    if not BOT_LOG.exists():
        return last_state

    try:
        with open(BOT_LOG, "r", errors="replace") as fh:
            for line in fh:
                m2 = _STATE_RE.search(line)
                if m2:
                    ts, status, inv, entry, mid_s, unreal, real, fills_n, oo = m2.groups()
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
def index():
    return render_template("hyperbot.html")


@app.route("/hyperbot/api/status")
def api_status():
    last_state = _parse_log()
    return jsonify({"service": _svc_status(), "state": last_state})


@app.route("/hyperbot/api/trades")
def api_trades():
    try:
        fills = _fetch_exchange_fills()
    except Exception as e:
        return jsonify({"error": str(e), "trades": []}), 500
    return jsonify({"trades": fills[:500], "total": len(fills)})


@app.route("/hyperbot/api/pnl")
def api_pnl():
    try:
        fills = _fetch_exchange_fills()
    except Exception as e:
        return jsonify({"error": str(e), "series": []}), 500
    series = [{"t": f["time"], "v": f["cum_pnl"]} for f in reversed(fills)]
    return jsonify({"series": series})


@app.route("/hyperbot/api/start", methods=["POST"])
def api_start():
    subprocess.run(["sudo", "systemctl", "start", BOT_SERVICE], timeout=5)
    return jsonify({"ok": True})


@app.route("/hyperbot/api/stop", methods=["POST"])
def api_stop():
    subprocess.run(["sudo", "systemctl", "stop", BOT_SERVICE], timeout=5)
    return jsonify({"ok": True})


@app.route("/hyperbot/api/portfolio")
def api_portfolio():
    try:
        payload = json.dumps({"type": "clearinghouseState", "user": WALLET_ADDRESS}).encode()
        req = urllib.request.Request(
            HL_API_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        account_value = float(data["marginSummary"]["accountValue"])
        withdrawable  = float(data.get("withdrawable", 0))

        position = None
        for ap in data.get("assetPositions", []):
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
            "account_value": round(account_value, 2),
            "withdrawable":  round(withdrawable, 2),
            "position":      position,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/hyperbot/api/log")
def api_log():
    lines = []
    if BOT_LOG.exists():
        try:
            with open(BOT_LOG, "r", errors="replace") as fh:
                lines = fh.readlines()[-100:]
        except Exception:
            pass
    return jsonify({"lines": [l.rstrip() for l in lines]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
