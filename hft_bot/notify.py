"""Lightweight Telegram notifier for trend_bot / forward_test.

Silent no-op when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset, so every
caller can notify unconditionally. Never raises — an alert failure must never
break a trading decision.
"""
from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger("notify")

# Load .env from repo root (same pattern as monitor/telegram_monitor.py).
_env = Path(__file__).resolve().parents[1] / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())


def send(msg: str) -> bool:
    """Send a Telegram message. Returns True if sent, False otherwise."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as exc:
        logger.warning("telegram send failed: %s", exc)
        return False
