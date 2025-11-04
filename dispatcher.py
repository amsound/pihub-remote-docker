# pihub/dispatcher.py
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from contextlib import suppress
from typing import Any, Awaitable, Callable, Dict, List, Optional

# Global repeat knobs (WS only; BLE never repeats)
REPEAT_INITIAL_MS = int(os.getenv("REPEAT_INITIAL_MS", "400"))
REPEAT_RATE_MS    = int(os.getenv("REPEAT_RATE_MS", "400"))

EdgeCB = Callable[[str, str], Awaitable[None]] | Callable[[str, str], None]


class Dispatcher:
    """
    Routes remote key edges to actions defined per-activity in keymap.json:
      - { "do": "emit", "text": "<pihub.cmd text>", ...extras, "when"?: "down"|"up", "repeat"?: true }
      - { "do": "ble",  "usage": "keyboard"|"consumer", "code": "<hid-name>" }
    """

    def __init__(self, cfg: Any, send_cmd: Callable[..., Awaitable[None]], bt_le: Any) -> None:
        self._cfg = cfg
        self._send_cmd = send_cmd
        self._bt = bt_le

        # Load full keymap document, then split into parts we use
        km = self._load_keymap()
        try:
            self._scancode_map: Dict[str, str] = dict(km["scancode_map"])
            self._bindings: Dict[str, Dict[str, List[Dict[str, Any]]]] = dict(km["activities"])
            if not isinstance(self._scancode_map, dict) or not isinstance(self._bindings, dict):
                raise TypeError
        except Exception as e:
            raise ValueError(
                "keymap.json schema invalid: expected 'scancode_map' (dict) and 'activities' (dict)."
            ) from e

        self._activity: Optional[str] = None
        self._repeat_tasks: Dict[str, asyncio.Task] = {}

        # Summary: count activities and scancodes
        acts = len(self._bindings)
        scan_total = len(self._scancode_map)
        print(f"[dispatcher] keymap loaded: {acts} activities, {scan_total} scancodes")

    @property
    def scancode_map(self) -> Dict[str, str]:
        """Public accessor for the logical rem_* scancode map."""
        return self._scancode_map

    # Activity comes from HA (ha_ws)
    async def on_activity(self, text: str) -> None:
        self._activity = text

    # USB edges come from UnifyingReader
    async def on_usb_edge(self, rem_key: str, edge: str) -> None:
        # Stop any repeat when the key goes up
        if edge == "up":
            await self._stop_repeat(rem_key)

        actions = (self._bindings.get(self._activity, {}) or {}).get(rem_key, [])
        for a in actions:
            await self._do_action(a, edge, rem_key=rem_key)

    # ---- Repeat helpers (WS only) ----
    async def _start_repeat(self, rem_key: str, text: str, extras: dict) -> None:
        if rem_key in self._repeat_tasks:
            return

        async def _runner():
            try:
                await asyncio.sleep(REPEAT_INITIAL_MS / 1000.0)
                while True:
                    await self._send_cmd(text=text, **extras)
                    await asyncio.sleep(REPEAT_RATE_MS / 1000.0)
            except asyncio.CancelledError:
                pass

        self._repeat_tasks[rem_key] = asyncio.create_task(_runner(), name=f"repeat:{rem_key}")

    async def _stop_repeat(self, rem_key: str) -> None:
        t = self._repeat_tasks.pop(rem_key, None)
        if t:
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t

    # ---- Action executor ----
    async def _do_action(self, a: dict, edge: str, *, rem_key: Optional[str] = None) -> None:
        kind = a.get("do")

        # Optional edge filter for non-BLE actions (defaults to 'up')
        when = a.get("when", "down")
        if kind != "ble" and edge != when:
            return

        # BLE: edge-accurate, never repeat
        if kind == "ble":
            usage = a.get("usage")
            code  = a.get("code")
            if not (isinstance(usage, str) and isinstance(code, str)):
                return
            if edge == "down":
                self._bt.key_down(usage=usage, code=code)
            elif edge == "up":
                self._bt.key_up(usage=usage, code=code)
            return

        # WS emit: optional repeat ONLY on 'down' with when='down'
        if kind == "emit":
            text = a.get("text")
            if not isinstance(text, str):
                return
            extras = {k: v for k, v in a.items() if k not in {"do", "when", "text", "repeat"}}
            want_repeat = bool(a.get("repeat"))

            if edge == "up" and when == "up":
                await self._send_cmd(text=text, **extras)
                return

            if edge == "down" and when == "down":
                await self._send_cmd(text=text, **extras)
                if want_repeat and rem_key:
                    await self._start_repeat(rem_key, text, extras)
                return

            return

        # Unknown action -> ignore
        return

    # ---- Keymap loader ----
    def _load_keymap(self) -> dict:
        """
        Load remote key bindings.

        Order:
          1) cfg.keymap_path (self._cfg)
          2) KEYMAP_PATH env
          3) packaged default: /app/pihub/assets/keymap.json
             (with a module-relative assets fallback for dev runs)
        """
        cfg_path = getattr(self._cfg, "keymap_path", None)
        env_path = (os.getenv("KEYMAP_PATH") or "").strip()

        candidates: List[Path] = []
        if cfg_path:
            candidates.append(Path(cfg_path).expanduser())
        if env_path:
            candidates.append(Path(env_path).expanduser())
        # Packaged default (absolute path used in the container)
        candidates.append(Path("/app/pihub/assets/keymap.json"))
        # Module-relative fallback (useful when running from source)
        candidates.append(Path(__file__).resolve().parent.parent / "assets" / "keymap.json")

        for p in candidates:
            if p.is_file():
                doc = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(doc, dict) or "scancode_map" not in doc or "activities" not in doc:
                    raise ValueError(
                        f"keymap.json at {p} missing required keys: 'scancode_map' and 'activities'"
                    )
                return doc

        tried = "\n  - " + "\n  - ".join(str(p) for p in candidates)
        raise FileNotFoundError(
            "keymap.json not found in any of the expected locations:" + tried +
            "\nSet KEYMAP_PATH or cfg.keymap_path to an absolute file path, "
            "or bake /app/pihub/assets/keymap.json into the image."
        )