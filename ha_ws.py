# pihub/ha_ws.py
from __future__ import annotations

import asyncio
import json
import random
from typing import Any, Awaitable, Callable, Optional

import aiohttp
import contextlib

OnActivity = Callable[[str], Awaitable[None]] | Callable[[str], None]
OnCmd      = Callable[[dict], Awaitable[None]] | Callable[[dict], None]


class HAWS:
    """
    Home Assistant WebSocket client with jittered reconnect.
    - Prints [ws] connected/disconnected.
    - Prints [activity] <value> on seed and on every change.
    - Drops sends when offline (no queue, no acks).
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

    # ── Public API ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Run until stop() is called. Reconnect with exponential backoff + jitter.
        """
        delay = 1.0
        while not self._stopping.is_set():
            try:
                await self._connect_once()            # returns on disconnect
                delay = 1.0                           # reset on a clean pass
                if not self._stopping.is_set():
                    await asyncio.sleep(random.uniform(0.2, 0.8))
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                jitter = random.uniform(0.75, 1.25)
                timeout = min(60.0, delay) * jitter
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=timeout)
                    break
                except asyncio.TimeoutError:
                    delay = min(delay * 2.0, 60.0)
                    continue

    async def stop(self) -> None:
        self._stopping.set()
        await self._close_ws()

    async def send_cmd(self, text: str, **extra: Any) -> bool:
        """
        Fire an event to HA (dest:'ha'). No acks, no buffering.
        Returns False if offline.
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
        self._session = aiohttp.ClientSession()

        try:
            ws = await self._session.ws_connect(self._url, heartbeat=30, autoping=True)
        except Exception:
            await self._close_ws()
            raise

        self._ws = ws
        try:
            await self._auth(ws)

            print("[ws] connected")  # log *before* seed so order is consistent

            # Subscribe first to avoid missing a quick change during seed.
            await self._subscribe(ws, "state_changed")
            await self._subscribe(ws, self._event_name)

            # Seed activity: ALWAYS print seed (even if same as last)
            await self._seed_activity(ws)

            # Receive until closed
            await self._recv_loop(ws)

        finally:
            print("[ws] disconnected")
            await self._close_ws()

    async def _auth(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        msg = await ws.receive_json()
        mtype = msg.get("type")
        if mtype == "auth_ok":
            return
        if mtype != "auth_required":
            raise RuntimeError(f"unexpected handshake: {mtype}")
        await ws.send_json({"type": "auth", "access_token": self._token})
        msg = await ws.receive_json()
        if msg.get("type") != "auth_ok":
            raise RuntimeError("auth failed")

    async def _seed_activity(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """
        Fetch current activity once; ALWAYS print + callback, then cache.
        """
        req_id = self._next_id()
        await ws.send_json({"id": req_id, "type": "get_states"})
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "result" and msg.get("id") == req_id and msg.get("success"):
                states = msg.get("result") or []
                for st in states:
                    if st.get("entity_id") == self._activity_entity:
                        val = str(st.get("state", "") or "").strip()
                        if val:
                            print(f"[activity] {val}")   # always print on (re)connect
                            self._last_activity = val
                            res = self._on_activity(val)
                            if asyncio.iscoroutine(res):
                                await res
                return
            # ignore interleaved messages until our result arrives

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse, event_type: str) -> None:
        await ws.send_json({"id": self._next_id(), "type": "subscribe_events", "event_type": event_type})

    async def _recv_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not self._stopping.is_set():
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue

                if data.get("type") == "event":
                    ev = data.get("event") or {}
                    ev_type = ev.get("event_type")
                    edata = ev.get("data") or {}

                    if ev_type == "state_changed":
                        ent = edata.get("entity_id") or ((edata.get("old_state") or {}).get("entity_id"))
                        if ent == self._activity_entity:
                            new_state = (edata.get("new_state") or {}).get("state")
                            if isinstance(new_state, str) and new_state:
                                if new_state != self._last_activity:
                                    print(f"[activity] {new_state}")
                                    self._last_activity = new_state
                                res = self._on_activity(new_state)
                                if asyncio.iscoroutine(res):
                                    await res

                    elif ev_type == self._event_name:
                        if edata.get("dest") == "pi":
                            t = edata.get("text", "?")
                            if t == "macro":
                                print(f"[cmd] macro {edata.get('name', '?')}")
                            elif t == "ble_key":
                                print(f"[cmd] ble_key {edata.get('usage', '?')}/{edata.get('code', '?')} "
                                      f"hold={int(edata.get('hold_ms', 40))}ms")
                            else:
                                print(f"[cmd] {t}")
                            res = self._on_cmd(edata)
                            if asyncio.iscoroutine(res):
                                await res

            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break  # reconnect

    def _next_id(self) -> int:
        i = self._msg_id
        self._msg_id += 1
        return i

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        sess, self._session = self._session, None
        if ws:
            with contextlib.suppress(Exception):
                await ws.close()
        if sess:
            with contextlib.suppress(Exception):
                await sess.close()