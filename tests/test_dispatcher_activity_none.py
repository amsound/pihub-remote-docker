import logging

from pihub.dispatcher import Dispatcher


class _Cfg:
    def __init__(self, keymap_path: str) -> None:
        self.keymap_path = keymap_path


class _BT:
    def key_down(self, **_kwargs) -> None:
        return None

    def key_up(self, **_kwargs) -> None:
        return None


def test_activity_none_logs_once(tmp_path, caplog) -> None:
    keymap = tmp_path / "keymap.json"
    keymap.write_text(
        """
        {
          "scancode_map": {"KEY_ENTER": "rem_ok"},
          "activities": {
            "watch": {
              "rem_ok": [{ "do": "emit", "text": "ok" }]
            }
          }
        }
        """,
        encoding="utf-8",
    )

    async def _send_cmd(**_kwargs) -> bool:
        return True

    dispatcher = Dispatcher(cfg=_Cfg(str(keymap)), send_cmd=_send_cmd, bt_le=_BT())

    caplog.set_level(logging.INFO, logger="pihub.dispatcher")

    def _ignored_count() -> int:
        return sum(
            1
            for record in caplog.records
            if "activity not set yet; ignoring input" in record.getMessage()
        )

    async def _run() -> None:
        await dispatcher.on_usb_edge("rem_ok", "down")
        assert dispatcher._activity_none_logged is True
        assert _ignored_count() == 1

        await dispatcher.on_usb_edge("rem_ok", "up")
        assert _ignored_count() == 1

        await dispatcher.on_activity("watch")
        assert dispatcher._activity_none_logged is False

        await dispatcher.on_activity(None)
        assert dispatcher._activity_none_logged is False

        await dispatcher.on_usb_edge("rem_ok", "down")
        assert _ignored_count() == 2

    import asyncio

    asyncio.run(_run())
    assert _ignored_count() == 2
