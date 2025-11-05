# pihub/bt_le/controller.py
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Callable

# Import module so we can read the runtime singleton AFTER start_hid()
from . import hid_device as _hd
from .hid_client import HIDClient


class HIDTransportBLE:
    """
    BLE transport. Owns bring-up/teardown and exposes notify_* for HIDClient.
    Reads the live HID GATT service from hid_device after start_hid().
    """

    # Send only report-mode keyboard by default (tvOS/iOS subs to report input).
    SEND_BOTH_KB = False

    def __init__(self, *, adapter: str, device_name: str, debug: bool = False) -> None:
        self._adapter = adapter
        self._device_name = device_name
        self._debug = debug

        self._shutdown: Optional[Callable[[], asyncio.Future]] = None
        self._hid_service = None  # set in start()

    async def start(self) -> None:
        cfg = SimpleNamespace(adapter_name=self._adapter, device_name=self._device_name, appearance=0x03C1)
        # bring up BlueZ + GATT app (HID/BAS/DIS). start_hid() must set _hid_service_singleton.
        _, shutdown = await _hd.start_hid(cfg)
        self._shutdown = shutdown

        # pull service AFTER start_hid()
        self._hid_service = getattr(_hd, "_hid_service_singleton", None)
        if self._hid_service is None:
            raise RuntimeError(
                "HID service not available after start_hid(); "
                "ensure start_hid() sets _hid_service_singleton = hid"
            )

        if self._debug:
            print(f'[hid] advertising registered as "{self._device_name}" on "{self._adapter}"', flush=True)

    async def stop(self) -> None:
        if self._shutdown is None:
            return
        res = self._shutdown()
        if asyncio.iscoroutine(res):
            await res
        self._shutdown = None
        self._hid_service = None

    # --- notifications used by HIDClient -------------------------------------

    def notify_keyboard(self, report: bytes) -> None:
        """
        Send an 8-byte keyboard report.
        Primary: Report-mode Keyboard Input (0x2A4D). RID is provided via Report Ref descriptor.
        Optional: Boot Keyboard Input (0x2A22) if SEND_BOTH_KB=True.
        """
        svc = self._hid_service
        if not svc:
            return

        rep = getattr(svc, "input_keyboard", None)
        if rep is not None and hasattr(rep, "changed"):
            try:
                rep.changed(report)
                if self._debug:
                    print("[bt] keyboard report changed", flush=True)
            except Exception as e:
                if self._debug:
                    print(f"[bt] keyboard report changed error: {e}", flush=True)

        if self.SEND_BOTH_KB:
            boot = getattr(svc, "boot_keyboard_input", None)
            if boot is not None and hasattr(boot, "changed"):
                try:
                    boot.changed(report)
                    if self._debug:
                        print("[bt] keyboard boot changed", flush=True)
                except Exception as e:
                    if self._debug:
                        print(f"[bt] keyboard boot changed error: {e}", flush=True)

    def notify_consumer(self, usage_id: int, pressed: bool) -> None:
        """
        Send 2-byte Consumer Control usage via Report-mode Consumer Input (0x2A4D, RID=2).
        """
        svc = self._hid_service
        if not svc:
            return

        payload = (usage_id if pressed else 0).to_bytes(2, "little")
        cons = getattr(svc, "input_consumer", None)
        if cons is not None and hasattr(cons, "changed"):
            try:
                cons.changed(payload)
                if self._debug:
                    edge = "down" if pressed else "up"
                    print(f"[bt] consumer changed 0x{usage_id:04X} {edge}", flush=True)
            except Exception as e:
                if self._debug:
                    print(f"[bt] consumer changed error: {e}", flush=True)


class BTLEController:
    """Thin orchestrator: transport + encoder."""
    def __init__(self, *, adapter: str, device_name: str, debug: bool = False) -> None:
        self._tx = HIDTransportBLE(adapter=adapter, device_name=device_name, debug=debug)
        self._client = HIDClient(hid=self._tx, debug=debug)
        self._available = False

    async def start(self) -> None:
        try:
            await self._tx.start()
            self._available = True
        except asyncio.CancelledError:
            self._available = False
            raise
        except Exception as e:
            self._available = False
            print(f"[bt] start failed: {e}", flush=True)

    async def stop(self) -> None:
        await self._tx.stop()
        self._available = False

    # Edge-level passthroughs used by Dispatcher for true key down/up
    def key_down(self, *, usage: str, code: str) -> None:
        if not self._available:
            return
        self._client.key_down(usage=usage, code=code)

    def key_up(self, *, usage: str, code: str) -> None:
        if not self._available:
            return
        self._client.key_up(usage=usage, code=code)

    # Tap (used by WS/macros)
    async def send_key(self, *, usage: str, code: str, hold_ms: int = 40) -> None:
        if not self._available:
            return
        await self._client.send_key(usage=usage, code=code, hold_ms=hold_ms)

    async def run_macro(
        self,
        steps: List[Dict[str, Any]],
        *,
        default_hold_ms: int = 40,
        inter_delay_ms: int = 400,
    ) -> None:
        if not self._available:
            return
        await self._client.run_macro(steps, default_hold_ms=default_hold_ms, inter_delay_ms=inter_delay_ms)
