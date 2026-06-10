import sys
from pathlib import Path

import pytest

# hft_bot modules use bare imports (import config, import state, ...)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clock  # noqa: E402


@pytest.fixture
def fake_clock():
    """Injectable test clock. Usage: fake_clock.advance(500) — ms."""
    class _FakeClock:
        def __init__(self):
            self.ms = 1_000_000

        def advance(self, delta_ms: int):
            self.ms += delta_ms

    fc = _FakeClock()
    clock.set_source(lambda: fc.ms)
    yield fc
    clock.set_source(None)
