"""
Single time source for the whole bot.

Every rolling window, cooldown, and lockout in the bot compares timestamps.
Those comparisons are only valid if every timestamp comes from the SAME clock.
Live trading uses the local monotonic clock (immune to NTP steps / wall-clock
jumps).  Replay backtests inject the recorded event time so that windows and
cooldowns advance with the *recorded* tape, not with how fast the CPU happens
to chew through the file.

Rules:
  - Exchange epoch-ms timestamps must NEVER be stored in a window that is
    pruned against now_ms().  Stamp data with now_ms() at ingest instead.
  - Durations (cooldowns, timeouts) are always measured in this clock domain.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

_source: Optional[Callable[[], int]] = None


def set_source(fn: Optional[Callable[[], int]]) -> None:
    """Install a custom time source (replay backtests). None restores live clock."""
    global _source
    _source = fn


def now_ms() -> int:
    if _source is not None:
        return _source()
    return int(time.monotonic_ns() // 1_000_000)
