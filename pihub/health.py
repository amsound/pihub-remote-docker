"""Lightweight HTTP health/status endpoint for external monitoring."""

from __future__ import annotations

import asyncio
import contextlib
from aiohttp import web
from typing import Optional

from .ha_ws import HAWS
from .bt_le.controller import BTLEController
from .input_unifying import UnifyingReader


class HealthServer:
    """Expose a simple JSON health snapshot for Home Assistant or probes."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        ws: HAWS,
        bt: BTLEController,
        reader: UnifyingReader,
    ) -> None:
        self._host = host
        self._port = port
        self._ws = ws
        self._bt = bt
        self._reader = reader

        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    async def start(self) -> None:
        """Start the HTTP listener if not already running."""

        if self._runner is not None:
            return

        app = web.Application()
        app.add_routes([web.get("/health", self._handle_health)])

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

    async def stop(self) -> None:
        """Stop the HTTP listener and release resources."""

        runner, self._runner = self._runner, None
        self._site = None

        if runner is None:
            return

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await runner.cleanup()

    async def _handle_health(self, _: web.Request) -> web.Response:
        snapshot = self.snapshot()
        status = 200 if snapshot["status"] == "ok" else 503
        return web.json_response(snapshot, status=status)

    def snapshot(self) -> dict:
        """Return a serialisable health snapshot."""

        ws_connected = self._ws.is_connected
        bt_available = self._bt.available
        usb_running = self._reader.is_running

        ok = ws_connected and usb_running

        return {
            "status": "ok" if ok else "degraded",
            "ws_connected": ws_connected,
            "last_activity": self._ws.last_activity,
            "ble_available": bt_available,
            "usb_reader": "running" if usb_running else "stopped",
            "usb_device": self._reader.device_path,
            "port": self._port,
        }
