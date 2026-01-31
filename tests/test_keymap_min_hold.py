from pihub.dispatcher import Dispatcher


class _Cfg:
    pass


class _BT:
    def key_down(self, **_kwargs) -> None:
        return None

    def key_up(self, **_kwargs) -> None:
        return None


def test_min_hold_ms_invalid_value_is_safe() -> None:
    sent = []

    async def _send_cmd(**_kwargs) -> bool:
        sent.append(_kwargs)
        return True

    import asyncio

    dispatcher = Dispatcher(cfg=_Cfg(), send_cmd=_send_cmd, bt_le=_BT())
    action = {"do": "emit", "text": "ok", "min_hold_ms": "nope"}
    asyncio.run(dispatcher._do_action(action, "down", rem_key="rem_ok", action_index=0))
    assert len(sent) == 1
