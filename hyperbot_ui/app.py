from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, render_template

WALLET_ADDRESS = "0x8d5BaFE283380554d3e669Ad2D6aa109Bf60458e"
HL_API_URL = "https://api.hyperliquid.xyz/info"

BOT_SERVICE = "hyperbot-bot"
BOT_LOG = Path("/opt/hyperbot/hft_bot/bot.log")

app = Flask(__name__)

_FILL_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2})\.\d+ \[INFO\] executor \| FILL \| "
    r"oid=(\d+) side=(\w+) px=([\d.]+) sz=([\d.]+) closedPnl=([+\-\d.]+)\$"
)
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


def _parse_log():
    fills = []
    last_state = {}
    pnl_series = []
    cum = 0.0

    if not BOT_LOG.exists():
        return fills, last_state, pnl_series

    try:
        with open(BOT_LOG, "r", errors="replace") as fh:
            for line in fh:
                m = _FILL_RE.search(line)
                if m:
                    ts, oid, side, px, sz, cpnl = m.groups()
                    cum += float(cpnl)
                    fills.append({
                        "time": ts,
                        "oid": int(oid),
                        "side": side,
                        "price": float(px),
                        "size": float(sz),
                        "closed_pnl": float(cpnl),
                        "cum_pnl": round(cum, 4),
                    })
                    pnl_series.append({"t": ts, "v": round(cum, 4)})

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

    return fills, last_state, pnl_series


@app.route("/hyperbot/")
@app.route("/hyperbot")
def index():
    return render_template("hyperbot.html")


@app.route("/hyperbot/api/status")
def api_status():
    _, last_state, _ = _parse_log()
    return jsonify({"service": _svc_status(), "state": last_state})


@app.route("/hyperbot/api/trades")
def api_trades():
    fills, _, _ = _parse_log()
    return jsonify({"trades": list(reversed(fills[-500:]))})


@app.route("/hyperbot/api/pnl")
def api_pnl():
    _, _, series = _parse_log()
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
