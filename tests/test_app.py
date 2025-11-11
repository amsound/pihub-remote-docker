import asyncio
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pihub import app


class DummyBT:
    def __init__(self) -> None:
        self.sent_keys = []
        self.macros = []

    async def send_key(self, **kwargs):
        self.sent_keys.append(kwargs)

    async def run_macro(self, steps, **kwargs):
        self.macros.append((steps, kwargs))


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "ble_key", "usage": "keyboard", "code": "A", "hold_ms": "oops"},
        {"text": "macro", "name": "power_on", "tap_ms": "bad"},
        {"text": "macro", "name": "power_on", "inter_delay_ms": "bad"},
    ],
)
def test_on_cmd_drops_invalid_numeric_fields(payload, caplog):
    bt = DummyBT()
    handler = app._make_on_cmd(bt)

    async def _run():
        await handler(payload)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(_run())

    assert not bt.sent_keys
    assert not bt.macros
    assert any("Dropping command" in message for message in caplog.messages)
