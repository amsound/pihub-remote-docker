import asyncio
import logging

import pytest

from pihub.dispatcher import Dispatcher
from pihub.validation import DEFAULT_MS_WHITELIST, parse_ms, parse_ms_whitelist


class _Cfg:
    pass


class _BT:
    def key_down(self, **_kwargs) -> None:
        return None

    def key_up(self, **_kwargs) -> None:
        return None


@pytest.mark.parametrize("value", [37, 123, "250"])
def test_keymap_min_hold_ms_accepts_permissive_values(value) -> None:
    dispatcher = Dispatcher(cfg=_Cfg(), send_cmd=lambda **_kwargs: True, bt_le=_BT())
    captured = {}

    async def _fake_schedule(**kwargs) -> None:
        captured["min_hold_ms"] = kwargs["min_hold_ms"]

    dispatcher._schedule_hold_emit = _fake_schedule  # type: ignore[assignment]
    action = {"do": "emit", "text": "ok", "min_hold_ms": value}
    asyncio.run(dispatcher._do_action(action, "down", rem_key="rem_ok", action_index=0))
    assert captured["min_hold_ms"] == int(value)


def test_parse_ms_bad_string_logs_and_defaults(caplog) -> None:
    caplog.set_level(logging.WARNING)
    result = parse_ms("fast", default=0, context="keymap.min_hold_ms")
    assert result == 0
    assert any("Invalid ms value" in record.message for record in caplog.records)


def test_parse_ms_whitelist_rejects_non_whitelisted_value(caplog) -> None:
    caplog.set_level(logging.WARNING)
    result = parse_ms_whitelist(37, allowed=DEFAULT_MS_WHITELIST, default=40, context="cmd.hold_ms")
    assert result == 40
    assert any("Non-whitelisted ms value" in record.message for record in caplog.records)
