"""Application entry point wiring BLE, Home Assistant and USB input."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

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
from .health import HealthServer
from .validation import DEFAULT_MS_WHITELIST, parse_ms_whitelist


def _debug_enabled() -> bool:
    value = os.getenv("DEBUG", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


logging.basicConfig(
    level=logging.DEBUG if _debug_enabled() else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)


def _make_on_cmd(bt: BTLEController):
    async def _on_cmd(data: dict) -> None:
        """
        Accept exactly two message shapes (HA → Pi):

          1) Single BLE key (tap):
             {
               "text": "ble_key",
               "usage": "keyboard" | "consumer",
               "code": "<symbolic_code>",
               "hold_ms": 40               # optional, default 40ms (whitelist)
             }

          2) Macro by name (timed sequence, local to Pi):
             {
               "text": "macro",
               "name": "<macro_name>",     # must exist in MACROS
               "tap_ms": 40,               # optional per-key hold, default 40ms (whitelist)
               "inter_delay_ms": 400       # optional gap, default 400ms (whitelist)
             }
        """
        text = (data or {}).get("text")

        if text == "ble_key":
            usage = data.get("usage")
            code = data.get("code")
            hold_ms = parse_ms_whitelist(data.get("hold_ms"), default=40, context="cmd.hold_ms")
            if isinstance(usage, str) and isinstance(code, str) and hold_ms is not None:
                # single-shot via HIDClient (macros use run_macro below)
                await bt.send_key(usage=usage, code=code, hold_ms=hold_ms)
            return

        if text == "macro":
            name = str(data.get("name") or "")
            steps = MACROS.get(name, [])
            if steps:
                tap = parse_ms_whitelist(data.get("tap_ms"), default=40, context="cmd.tap_ms")
                inter = parse_ms_whitelist(
                    data.get("inter_delay_ms"),
                    allowed=(*DEFAULT_MS_WHITELIST, 400),
                    default=400,
                    context="cmd.inter_delay_ms",
                )
                await bt.run_macro(steps, default_hold_ms=tap, inter_delay_ms=inter)
            return

        # Unknown command -> drop silently by design
        return

    return _on_cmd


async def main() -> None:
    """Run the PiHub control loop until interrupted."""
    cfg = Config.load()
    try:
        token = cfg.load_token()
    except RuntimeError as exc:
        logger.error("[app] Cannot start without Home Assistant token: %s", exc)
        raise SystemExit(1) from exc

    bt = BTLEController(adapter=cfg.ble_adapter, device_name=cfg.ble_device_name)

    async def _on_activity(activity: str) -> None:
        await DispatcherRef.on_activity(activity)  # set below

    _on_cmd = _make_on_cmd(bt)

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
        scancode_map=DispatcherRef.scancode_map,
        on_edge=DispatcherRef.on_usb_edge,
        grab=cfg.usb_grab,
        on_disconnect=DispatcherRef.on_usb_disconnect,
    )

    health = HealthServer(
        host=cfg.health_host,
        port=cfg.health_port,
        ws=ws,
        bt=bt,
        reader=reader,
    )

    logger.info(
        "[app] ws=%s event=%s activity=%s",
        cfg.ha_ws_url,
        cfg.ha_cmd_event,
        cfg.ha_activity,
    )

    stop = asyncio.Event()

    def _monitor_ws(task: asyncio.Task) -> None:
        if stop.is_set():
            return
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("[app] Home Assistant task crashed")
        else:
            logger.warning("[app] Home Assistant task exited unexpectedly")
        stop.set()

    ws_task = asyncio.create_task(ws.start(), name="ha_ws")
    ws_task.add_done_callback(_monitor_ws)

    await bt.start()
    if not await bt.wait_ready(timeout=5.0):
        logger.warning("[app] BLE adapter not yet available; continuing without HID")
    await reader.start()
    await health.start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()

    await reader.stop()
    await health.stop()
    await ws.stop()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await ws_task
    await bt.stop()


if __name__ == "__main__":
    asyncio.run(main())
