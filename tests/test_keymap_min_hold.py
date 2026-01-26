from pihub.dispatcher import Dispatcher


class _Cfg:
    def __init__(self, keymap_path: str) -> None:
        self.keymap_path = keymap_path


class _BT:
    def key_down(self, **_kwargs) -> None:
        return None

    def key_up(self, **_kwargs) -> None:
        return None


def test_min_hold_ms_invalid_value_is_safe(tmp_path) -> None:
    keymap = tmp_path / "keymap.json"
    keymap.write_text(
        """
        {
          "scancode_map": {"KEY_ENTER": "rem_ok"},
          "activities": {
            "watch": {
              "rem_ok": [{ "do": "emit", "text": "ok", "min_hold_ms": "nope" }]
            }
          }
        }
        """,
        encoding="utf-8",
    )

    async def _send_cmd(**_kwargs) -> None:
        return None

    dispatcher = Dispatcher(cfg=_Cfg(str(keymap)), send_cmd=_send_cmd, bt_le=_BT())
    dispatcher._activity = "watch"
    import asyncio

    asyncio.run(dispatcher.on_usb_edge("rem_ok", "down"))
