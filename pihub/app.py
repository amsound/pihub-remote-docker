"""Home Assistant WebSocket integration with resilient reconnects + subscribe_trigger."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .validation import DEFAULT_MS_WHITELIST, parse_ms_whitelist

OnActivity = Callable[[Optional[str]], Awaitable[None]] | Callable[[Optional[str]], None]
OnCmd      = Callable[[dict], Awaitable[None]] | Callable[[dict], None]

logger = logging.getLogger(__name__)

WS_RECV_TIMEOUT_S = 20.0
RECONNECT_JITTER = 0.2


class HAWS:
    """
    Uses subscribe_trigger to receive only the target entity's changes.
    Why: reduce WS noise/CPU on constrained devices.
    """

    def __init__(
        self,
        *,
        url: str,
        token: str,
        activity_entity: str,
        event_name: str,
        on_activity: OnActivity,
        on_cmd: OnCmd,
    ) -> None:
        self._url = url
        self._token = token or ""
        self._activity_entity = activity_entity
        self._event_name = event_name
        self._on_activity = on_activity
        self._on_cmd = on_cmd

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._stopping = asyncio.Event()
        self._msg_id = 1
        self._last_activity: Optional[str] = None

    @property
    def is_connected(self) -> bool:
        """Return True when the websocket is currently open."""

        ws = self._ws
        return bool(ws and not ws.closed)

    @property
    def last_activity(self) -> Optional[str]:
        """Expose the most recent activity reported by Home Assistant."""

        return self._last_activity

    # ── Public API ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Run until stop() is called. Reconnect with exponential backoff + jitter."""
        delay = 1.0
        while not self._stopping.is_set():
            try:
                await self._connect_once()
                delay = 1.0
                if not self._stopping.is_set():
                    await asyncio.sleep(random.uniform(0.2, 0.8))
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stopping.is_set():
                    logger.warning("[ws] error: %r", exc)
                jitter = random.uniform(1.0 - RECONNECT_JITTER, 1.0 + RECONNECT_JITTER)
                timeout = min(60.0, delay) * jitter
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=timeout)
                    break
                except asyncio.TimeoutError:
                    delay = min(delay * 2.0, 60.0)
                    continue

    async def stop(self) -> None:
        """Signal the client to stop and close the socket."""
        self._stopping.set()
        await self._close_ws()
        await self._close_session()

    async def send_cmd(self, text: str, **extra: Any) -> bool:
        """
        Fire an event to HA (dest:'ha'). No acks, no buffering.
        """
        ws = self._ws
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_json({
                "id": self._next_id(),
                "type": "fire_event",
                "event_type": self._event_name,
                "event_data": {"dest": "ha", "text": text, **extra},
            })
            return True
        except Exception:
            return False

    # ── Internals ───────────────────────────────────────────────────────────

    async def _connect_once(self) -> None:
        """One lifecycle: connect → auth → subscribe → seed → recv loop → close."""
        await self._close_ws()
        session = await self._ensure_session()

        try:
            ws = await session.ws_connect(self._url, heartbeat=30, autoping=True)
        except Exception:
            await self._close_ws()
            raise

        self._ws = ws
        try:
            await self._auth(ws)
            if self._stopping.is_set():
                return

            logger.info("[ws] connected")  # log *before* seed so order is consistent

            # Subscribe to ONLY the target entity via trigger.
            await self._subscribe_trigger_entity(ws, self._activity_entity)

            # Keep custom event bus subscription unchanged (e.g., "pihub.cmd").
            await self._subscribe(ws, self._event_name)

            # Seed activity from current states once.
            await self._seed_activity(ws)
            if self._stopping.is_set():
                return

            # Receive until closed.
            await self._recv_loop(ws)

        finally:
            logger.info("[ws] disconnected")
            await self._close_ws()

    async def _auth(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=WS_RECV_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            logger.warning("[ws] auth timeout waiting for handshake (timeout=%.1fs)", WS_RECV_TIMEOUT_S)
            raise exc
        mtype = msg.get("type")
        if mtype == "auth_ok":
            return
        if mtype != "auth_required":
            raise RuntimeError(f"unexpected handshake: {mtype}")
        await ws.send_json({"type": "auth", "access_token": self._token})
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=WS_RECV_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            logger.warning("[ws] auth timeout waiting for auth_ok (timeout=%.1fs)", WS_RECV_TIMEOUT_S)
            raise exc
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"auth failed: {msg}")
    async def _recv_json_with_timeout(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        *,
        timeout_s: float,
    ) -> dict:
        """Receive a JSON message with timeout (no logging; callers preserve semantics)."""
        return await asyncio.wait_for(ws.receive_json(), timeout=timeout_s)

    async def _seed_activity(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Fetch the current state of the activity entity so we start consistent."""
        if self._stopping.is_set():
            return

        req_id = self._next_id()
        await ws.send_json({
            "id": req_id,
            "type": "get_states",
        })
        try:
            msg = await self._recv_json_with_timeout(ws, timeout_s=WS_RECV_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("[ws] timeout seeding activity entity")
            return

        if msg.get("type") == "result" and msg.get("id") == req_id and msg.get("success"):
            states = msg.get("result") or []
            for st in states:
                if st.get("entity_id") == self._activity_entity:
                    new_state = self._normalize_activity_state((st.get("state") or "").strip())
                    await self._apply_activity(new_state)
                    return
    async def _recv_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Receive loop: parse → route trigger/cmd events; reconnect on errors."""
        while not self._stopping.is_set():
            try:
                msg = await self._recv_json_with_timeout(ws, timeout_s=WS_RECV_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.debug("[ws] recv timeout; reconnecting")
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[ws] receive error; reconnecting", exc_info=True)
                break

            if msg.get("type") == "event":
                ev = msg.get("event") or {}
                res = self._handle_event_message(ev)
                if asyncio.iscoroutine(res):
                    await res
                continue

            if msg.get("type") in {"auth_required", "auth_ok", "result"}:
                continue

    async def _handle_event_message(self, ev: dict) -> None:
        if not isinstance(ev, dict):
            return
        ev_type = ev.get("event_type")
        edata = ev.get("data") or {}
        if not isinstance(edata, dict):
            edata = {}

        if await self._handle_trigger_event(ev_type, edata):
            return

        await self._handle_cmd_event(ev_type, edata)

    async def _handle_trigger_event(self, ev_type: Any, edata: dict) -> bool:
        """Return True if handled as activity trigger event."""
        if ev_type != "trigger":
            return False
        if edata.get("platform") != "state":
            return False
        if edata.get("entity_id") != self._activity_entity:
            return False

        new_state = self._normalize_activity_state(edata.get("to_state"))
        await self._apply_activity(new_state)
        return True

    async def _handle_cmd_event(self, ev_type: Any, edata: dict) -> None:
        """Handle custom command events addressed to the Pi."""
        if ev_type != self._event_name:
            return
        if edata.get("dest") != "pi":
            return

        t = edata.get("text", "?")
        if t == "macro":
            logger.debug("[ha_cmd] macro %s", edata.get("name", "?"))
        elif t == "ble_key":
            hold_ms = parse_ms_whitelist(
                edata.get("hold_ms"),
                allowed=DEFAULT_MS_WHITELIST,
                default=40,
                context="ha_ws.hold_ms",
            )
            edata["hold_ms"] = hold_ms
            logger.debug(
                "[ha_cmd] ble_key %s/%s hold=%sms",
                edata.get("usage", "?"),
                edata.get("code", "?"),
                hold_ms,
            )
        else:
            logger.debug("[ha_cmd] unknown %s", t)

        res = self._on_cmd(edata)
        if asyncio.iscoroutine(res):
            await res


    def _next_id(self) -> int:
        i = self._msg_id
        self._msg_id += 1
        return i

    def _normalize_activity_state(self, state: Any) -> Optional[str]:
        if state is None:
            return None
        text = state if isinstance(state, str) else str(state)
        val = text.strip()
        if not val or val in {"unknown", "unavailable"}:
            return None
        return val

    async def _apply_activity(self, new_state: Optional[str]) -> None:
        # Only notify on actual change (including change to/from None)
        if new_state == self._last_activity:
            return

        prior = self._last_activity
        logger.info("[activity] %s -> %s", prior, new_state)
        self._last_activity = new_state

        res = self._on_activity(new_state)
        if asyncio.iscoroutine(res):
            await res
    async def _await_result(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        req_id: int,
        *,
        context: str,
    ) -> None:
        while True:
            if self._stopping.is_set():
                return
            try:
                msg = await self._recv_json_with_timeout(ws, timeout_s=WS_RECV_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                logger.warning(
                    "[ws] timeout waiting for %s result (timeout=%.1fs)",
                    context,
                    WS_RECV_TIMEOUT_S,
                )
                raise exc

            if msg.get("type") == "result" and msg.get("id") == req_id:
                if msg.get("success"):
                    return
                logger.error("[ws] %s failed: %s", context, msg)
                raise RuntimeError(f"{context} failed: {msg}")


    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws:
            with contextlib.suppress(Exception):
                await ws.close()

    async def _close_session(self) -> None:
        sess, self._session = self._session, None
        if sess:
            with contextlib.suppress(Exception):
                await sess.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        session = self._session
        if session is None or session.closed:
            self._session = session = aiohttp.ClientSession()
        return session
