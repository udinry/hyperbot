#!/usr/bin/env python3
"""
Hyperbot Telegram monitor -- always-on VPS service.
Tails bot logs and sends instant alerts + hourly summaries.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Load .env from repo root so this script works both directly and as a service.
_env_path = Path(__file__).resolve().parents[1] / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")

if not TG_TOKEN or not TG_CHAT:
    sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
HL_API   = "https://api.hyperliquid.xyz/info"
WALLET   = "0x70C780d4e1497598eEB0ae54CCA6011CD55FF89D"

ALERT_RE = re.compile(
    r"FILL|signal|EMERGENCY|circuit|SL placed|SL cancelled"
    r"|ERROR|CRITICAL|WARNING|resize|disconnect|reconnect"
    r"|exiting|Traceback|killed|balance"
)
SUPPRESS_RE = re.compile(
    r"refresh_order_size|userFills snapshot|Startup:|bot\.log"
)


def tg(msg: str) -> None:
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:
        print(f"[tg error] {exc}")


def hl_post(payload: dict):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HL_API, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def fetch_status():
    perp = hl_post({"type": "clearinghouseState", "user": WALLET})
    equity = float(perp["marginSummary"]["accountValue"])

    spot_usdc = 0.0
    try:
        spot = hl_post({"type": "spotClearinghouseState", "user": WALLET})
        for b in spot.get("balances", []):
            if b.get("coin") == "USDC":
                spot_usdc = float(b.get("total", 0))
                break
    except Exception:
        pass

    balance = max(equity, spot_usdc)

    position = None
    for ap in perp.get("assetPositions", []):
        pos = ap.get("position", {})
        if pos.get("coin") == "BTC":
            szi = float(pos.get("szi", 0))
            if abs(szi) > 0:
                position = {
                    "size":  szi,
                    "entry": float(pos["entryPx"]) if pos.get("entryPx") else None,
                    "upnl":  float(pos.get("unrealizedPnl", 0)),
                    "liq":   pos.get("liquidationPx"),
                }
            break

    today_ms = int(
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp() * 1000
    )
    fills_raw = hl_post({"type": "userFills", "user": WALLET})
    today = [
        f for f in fills_raw
        if f.get("coin") == "BTC" and int(f.get("time", 0)) >= today_ms
    ]
    gross = sum(float(f.get("closedPnl", 0)) for f in today)
    fees  = sum(abs(float(f.get("fee", 0))) for f in today)
    net   = gross - fees

    return balance, position, gross, net, fees, len(today)


def hourly_loop() -> None:
    time.sleep(60)
    while True:
        try:
            balance, pos, gross, net, fees, fills = fetch_status()
            ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
            lines = [
                f"Hyperbot Status -- {ts}",
                f"Balance: ${balance:.2f}",
                f"Today: {fills} fills | Gross ${gross:+.2f} | Net ${net:+.2f} | Fees ${fees:.2f}",
            ]
            if pos:
                direction = "LONG" if pos["size"] > 0 else "SHORT"
                lines.append(
                    f"Position: {direction} {abs(pos['size']):.4f} BTC"
                    f" @ ${pos['entry']:.0f}"
                    f" | uPnL ${pos['upnl']:+.2f}"
                )
                if pos["liq"]:
                    lines.append(f"Liquidation: ${float(pos['liq']):.0f}")
            else:
                lines.append("Position: flat")
            tg("\n".join(lines))
        except Exception as exc:
            tg(f"[WARNING] Status fetch failed: {exc}")
        time.sleep(3600)


def tail_log() -> None:
    proc = subprocess.Popen(
        ["journalctl", "-u", "hyperbot-bot", "-f", "--no-pager", "-n", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    for raw in proc.stdout:
        line = raw.rstrip()
        if not ALERT_RE.search(line):
            continue
        if SUPPRESS_RE.search(line):
            continue
        # Strip journalctl prefix, timestamp, and log level — leaves "module | message"
        msg = re.sub(r".*python\[\d+\]: \d{2}:\d{2}:\d{2}\.\d+ \[\w+\] ", "", line)
        if any(w in line for w in ["EMERGENCY", "ERROR", "CRITICAL", "circuit", "killed", "Traceback"]):
            prefix = "[CRITICAL]"
        elif "WARNING" in line:
            prefix = "[WARNING]"
        elif "FILL" in line or "signal" in line:
            prefix = "[TRADE]"
        elif "SL" in line:
            prefix = "[SL]"
        else:
            prefix = "[INFO]"
        tg(f"{prefix} {msg}")


if __name__ == "__main__":
    tg("[ON] Hyperbot monitor online -- alerts active")
    threading.Thread(target=hourly_loop, daemon=True).start()
    tail_log()
