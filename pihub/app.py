# pihub/app.py
from __future__ import annotations

import asyncio
import contextlib
import signal

try:
    import uvloop as _uvloop  # type: ignore
    _uvloop.install()
except Exception:
    pass

from .config import Config
from .ha_ws import HAWS
from .dispatcher import Dispatcher
from .input_unifying import UnifyingReader
from .bt_le.controller import BTLEController
from .macros import MACROS

async def main() -> None:
    cfg = Config.load()
    token = cfg.load_token()

    bt = BTLEController(adapter=cfg.ble_adapter, device_name=cfg.ble_device_name, debug=cfg.debug_bt)

    async def _on_activity(activity: str) -> None:
        await DispatcherRef.on_activity(activity)  # set below

    async def _on_cmd(data: dict) -> None:
        """
        Accept exactly two message shapes (HA → Pi):
    
          1) Single BLE key (tap):
             {
               "text": "ble_key",
               "usage": "keyboard" | "consumer",
               "code": "<symbolic_code>",
               "hold_ms": 40               # optional, default 40ms
             }
    
          2) Macro by name (timed sequence, local to Pi):
             {
               "text": "macro",
               "name": "<macro_name>",     # must exist in MACROS
               "tap_ms": 40,               # optional per-key hold, default 40ms
               "inter_delay_ms": 400       # optional gap, default 400ms
             }
        """
        text = (data or {}).get("text")
    
        if text == "ble_key":
            usage = data.get("usage")
            code = data.get("code")
            hold_ms = int(data.get("hold_ms", 40))
            if isinstance(usage, str) and isinstance(code, str):
                # single-shot via HIDClient (macros use run_macro below)
                await bt.send_key(usage=usage, code=code, hold_ms=hold_ms)
            return
    
        if text == "macro":
            name = str(data.get("name") or "")
            steps = MACROS.get(name, [])
            if steps:
                tap = int(data.get("tap_ms", 40))              # per-key hold within macro
                inter = int(data.get("inter_delay_ms", 400))   # gap between steps
                await bt.run_macro(steps, default_hold_ms=tap, inter_delay_ms=inter)
            return
    
        # Unknown command -> drop silently by design
        return

    ws = HAWS(
        url=cfg.ha_ws_url,
        token=token,
        activity_entity=cfg.ha_activity,
        event_name=cfg.ha_cmd_event,
        on_activity=_on_activity,
        on_cmd=_on_cmd,
    )

    async def _send_cmd(text: str, **extra) -> bool:
        return await ws.send_cmd(text, **extra)

    DispatcherRef = Dispatcher(cfg=cfg, send_cmd=_send_cmd, bt_le=bt)

    reader = UnifyingReader(
        device_path=cfg.usb_receiver,
        scancode_map=DispatcherRef.scancode_map,
        on_edge=DispatcherRef.on_usb_edge,
        grab=cfg.usb_grab,
    )

    print(
        f'[app] ws={cfg.ha_ws_url} event={cfg.ha_cmd_event} '
        f'activity={cfg.ha_activity}'
    )

    ws_task = asyncio.create_task(ws.start(), name="ha_ws")
    await bt.start()
    await reader.start()

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()

    await reader.stop()
    await ws.stop()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await ws_task
    await bt.stop()


if __name__ == "__main__":
    asyncio.run(main())
