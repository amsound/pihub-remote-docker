#!/usr/bin/env python3
"""BlueZ HID service definitions and lifecycle helpers."""

import asyncio
import os
import contextlib
import inspect
import logging
import time

from bluez_peripheral.util import get_message_bus, Adapter, is_bluez_available
from bluez_peripheral.advert import Advertisement
from bluez_peripheral.agent import NoIoAgent
from bluez_peripheral.gatt.service import Service, ServiceCollection
from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as CharFlags
from bluez_peripheral.gatt.descriptor import DescriptorFlags as DescFlags

from dbus_fast.constants import MessageType
from dbus_fast import Variant
from dataclasses import dataclass

logger = logging.getLogger(__name__)

async def ensure_controller_baseline(bus, adapter_name: str, *, adapter_proxy=None) -> None:
    """Re-apply the minimum controller state we need for reliable (re)pair + reconnect.

    Why: toggling Powered (or restarting bluetoothd) can silently reset Pairable/Discoverable
    and timeouts. Apple TV is very sensitive to this during reconnect.
    """
    try:
        from dbus_fast import Variant  # already a dependency elsewhere in this file
    except Exception:
        Variant = None  # type: ignore

    path = f"/org/bluez/{adapter_name}"

    # Build a proxy if caller didn't pass one
    if adapter_proxy is None:
        try:
            xml = await bus.introspect("org.bluez", path)
            adapter_proxy = bus.get_proxy_object("org.bluez", path, xml)
        except Exception as exc:
            logger.warning("[hid] Baseline: couldn't introspect %s: %s", path, exc)
            return

    props = adapter_proxy.get_interface("org.freedesktop.DBus.Properties")

    async def _set(prop: str, sig: str, val):
        if Variant is None:
            return
        try:
            await props.call_set("org.bluez.Adapter1", prop, Variant(sig, val))
        except Exception as exc:
            # Some properties may be read-only depending on controller/BlueZ build
            logger.debug("[hid] Baseline: set %s=%r failed: %s", prop, val, exc)

    # Keep these "sticky" across restarts and power cycles
    await _set("Powered", "b", True)
    await _set("PairableTimeout", "u", 0)
    await _set("DiscoverableTimeout", "u", 0)
    await _set("Pairable", "b", True)
    await _set("Discoverable", "b", True)


_hid_service_singleton = None  # set inside start_hid()
_advertising_state = False

# --------------------------
# Device identity / advert
# --------------------------

APPEARANCE   = 0x03C1  # Keyboard

# Report IDs
RID_KEYBOARD = 0x01
RID_CONSUMER = 0x02

# --------------------------
# HID Report Map (Keyboard + Consumer bitfield)
# --------------------------
REPORT_MAP = bytes([
    # Keyboard (Boot) – Report ID 1
    0x05,0x01, 0x09,0x06, 0xA1,0x01,
      0x85,0x01,              # REPORT_ID (1)
      0x05,0x07,              #   USAGE_PAGE (Keyboard)
      0x19,0xE0, 0x29,0xE7,   #   USAGE_MIN/MAX (modifiers)
      0x15,0x00, 0x25,0x01,   #   LOGICAL_MIN 0 / MAX 1
      0x75,0x01, 0x95,0x08,   #   REPORT_SIZE 1, COUNT 8  (mod bits)
      0x81,0x02,              #   INPUT (Data,Var,Abs)
      0x95,0x01, 0x75,0x08,   #   reserved byte
      0x81,0x01,              #   INPUT (Const,Array,Abs)
      0x95,0x06, 0x75,0x08,   #   6 keys
      0x15,0x00, 0x25,0x65,   #   key range 0..0x65
      0x19,0x00, 0x29,0x65,   #   USAGE_MIN/MAX (keys)
      0x81,0x00,              #   INPUT (Data,Array,Abs)
    0xC0,

    # Consumer Control – 16‑bit *array* usage (Report ID 2) — 2‑byte value
    0x05,0x0C, 0x09,0x01, 0xA1,0x01,
      0x85,0x02,              # REPORT_ID (2)
      0x15,0x00,              # LOGICAL_MIN 0
      0x26,0xFF,0x03,         # LOGICAL_MAX 0x03FF
      0x19,0x00,              # USAGE_MIN 0x0000
      0x2A,0xFF,0x03,         # USAGE_MAX 0x03FF
      0x75,0x10,              # REPORT_SIZE 16
      0x95,0x01,              # REPORT_COUNT 1 (one slot)
      0x81,0x00,              # INPUT (Data,Array,Abs)
    0xC0,
])
    

async def _cleanup_stale_adverts(bus, adapter_name: str, base_path: str = "/com/spacecheese/bluez_peripheral/advert", max_ids: int = 8) -> None:
    """Best-effort cleanup for advertisements that can be left registered if we crashed mid-startup."""
    from contextlib import suppress

    try:
        mgr = await _get_adv_manager(bus, adapter_name)
    except Exception:
        return

    for i in range(max_ids):
        path = f"{base_path}{i}"
        with suppress(Exception):
            await mgr.call_unregister_advertisement(path)

async def _adv_unregister(bus, advert) -> bool:
    """
    Unregister/stop advertising. Idempotent + hardened.
    Returns True if we attempted something.
    """
    attempted = False
    # reflect intent immediately so health/logic doesn't lie if unregister errors
    _set_advertising_state(False)

    try:
        if advert is None:
            return False

        # Prefer stop (if available) then unregister; some stacks behave better this way
        if hasattr(advert, "stop"):
            attempted = True
            try:
                await advert.stop()
            except Exception:
                # ignore stop failures; unregister may still work
                pass

        if hasattr(advert, "unregister"):
            attempted = True
            sig = inspect.signature(advert.unregister)
            try:
                if "bus" in sig.parameters:
                    await advert.unregister(bus)
                else:
                    await advert.unregister()
            except Exception as e:
                # Treat common “already gone” / “not permitted” cases as non-fatal
                logger.warning("[hid] adv unregister error: %r", e)

        return attempted

    except Exception as e:
        logger.warning("[hid] adv unregister error: %r", e)
        return attempted

def _make_advert(device_name: str, appearance: int):
    """
    Create a fresh Advertisement object with sane defaults for Apple TV:
    - connectable
    - general discoverable (via adv flags in the advert implementation)
    - runs indefinitely while idle (duration=0)
    """
    return Advertisement(
        localName=device_name,
        serviceUUIDs=["1812", "180F", "180A"],
        appearance=appearance,
        timeout=0,     # keep as your lib expects
        duration=0,    # IMPORTANT: do NOT auto-stop after 2 seconds
        discoverable=True,
    )

async def _adv_register_and_start(bus, advert) -> str:
    """
    (Re)register (+ start if supported). Returns a short mode label:
    'registered+started', 'registered', or 'noop'. Only logs on error.
    """
    try:
        did_register = False
        if hasattr(advert, "register"):
            sig = inspect.signature(advert.register)
            if "bus" in sig.parameters:
                await advert.register(bus)
            else:
                await advert.register()
            did_register = True

        if hasattr(advert, "start"):
            await advert.start()
            _set_advertising_state(True)
            return "registered+started" if did_register else "started"

        if did_register:
            _set_advertising_state(True)
        return "registered" if did_register else "noop"
    except Exception as e:
        logger.warning("[hid] adv register/start error: %r", e)
        _set_advertising_state(False)
        return "error"

# --------------------------
# BlueZ object manager helpers
# --------------------------
def _get_bool(v):  # unwrap dbus_next.Variant or use raw bool
    return bool(v.value) if isinstance(v, Variant) else bool(v)

def _get_str(v):  # unwrap dbus_next.Variant or use raw str
    if v is None:
        return ""
    return str(v.value) if isinstance(v, Variant) else str(v)


def _set_advertising_state(active: bool) -> None:
    global _advertising_state
    _advertising_state = bool(active)

def advertising_active() -> bool:
    return _advertising_state

async def trust_device(bus, device_path):
    """Set org.bluez.Device1.Trusted = True for the connected peer."""
    try:
        root_xml = await bus.introspect("org.bluez", device_path)
        dev_obj = bus.get_proxy_object("org.bluez", device_path, root_xml)
        props = dev_obj.get_interface("org.freedesktop.DBus.Properties")
        await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
    except Exception:
        pass
        
async def _get_managed_objects(bus):
    root_xml = await bus.introspect("org.bluez", "/")
    root = bus.get_proxy_object("org.bluez", "/", root_xml)
    om = root.get_interface("org.freedesktop.DBus.ObjectManager")
    return await om.call_get_managed_objects()

async def wait_for_any_connection(
    bus,
    adapter_name: str,
    timeout_s: float | None = None,
) -> str | None:
    """Return the DBus object path of the first connected Device1 on this adapter.

    Centrals (including Apple TV) will often connect *before* the user confirms the pairing popup,
    just to browse GATT and begin the bonding flow. That is expected.

    If timeout_s is None, wait forever.
    """
    adapter_path = f"/org/bluez/{adapter_name}"
    deadline = None if timeout_s is None else (time.monotonic() + float(timeout_s))

    while True:
        managed = await _get_managed_objects(bus)
        for path, ifaces in managed.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            if _get_str(dev.get("Adapter")) != adapter_path:
                continue
            if _get_bool(dev.get("Connected", False)):
                return path

        if deadline is not None and time.monotonic() >= deadline:
            return None

        await asyncio.sleep(0.2)
async def wait_until_services_resolved(bus, device_path, timeout_s=30, poll_interval=0.25):
    """Wait for Device1.ServicesResolved == True for this device."""
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        objs = await _get_managed_objects(bus)
        dev = objs.get(device_path, {}).get("org.bluez.Device1")
        if dev and _get_bool(dev.get("ServicesResolved", False)):
            return True
        await asyncio.sleep(poll_interval)
    return False

async def wait_for_disconnect(bus, device_path, poll_interval=0.5):
    """Block until this device disconnects."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def handler(msg):
        if msg.message_type is not MessageType.SIGNAL:
            return
        if msg.member != "PropertiesChanged" or msg.path != device_path:
            return
        iface, changed, _ = msg.body
        if iface == "org.bluez.Device1" and "Connected" in changed and not _get_bool(changed["Connected"]) and not fut.done():
            fut.set_result(None)

    bus.add_message_handler(handler)
    try:
        while True:
            if fut.done():
                await fut
                return
            objs = await _get_managed_objects(bus)
            dev = objs.get(device_path, {}).get("org.bluez.Device1")
            if not dev or not _get_bool(dev.get("Connected", False)):
                return
            await asyncio.sleep(poll_interval)
    finally:
        bus.remove_message_handler(handler)
        
async def _get_device_alias_or_name(bus, device_path) -> str:
    try:
        root_xml = await bus.introspect("org.bluez", device_path)
        dev_obj = bus.get_proxy_object("org.bluez", device_path, root_xml)
        props = dev_obj.get_interface("org.freedesktop.DBus.Properties")

        alias = await props.call_get("org.bluez.Device1", "Alias")
        name  = await props.call_get("org.bluez.Device1", "Name")

        def _unwrap(v): 
            from dbus_fast import Variant
            return v.value if isinstance(v, Variant) else v

        return _unwrap(alias) or _unwrap(name) or ""
    except Exception:
        return ""



async def wait_until_bonded(
    bus,
    device_path: str,
    timeout_s: float = 60.0,
) -> bool:
    """Wait for Device1.Bonded==True on a specific device path."""
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        managed = await _get_managed_objects(bus)
        dev = managed.get(device_path, {}).get("org.bluez.Device1")
        if not dev:
            return False
        if bool(dev.get("Bonded", False)):
            return True
        await asyncio.sleep(0.2)
    return False
async def watch_link(bus, adapter_name: str, advert, hid):
    """Supervise advertising + incoming connections for this HID-over-GATT peripheral."""
    fail_window_s = 120.0
    fail_threshold = 3
    fail_count = 0
    fail_window_start = 0.0

    connect_wait_s = 180.0
    ready_deadline_s = 120.0
    cccd_grace_s = 90.0

    async def _note_failure(reason: str):
        nonlocal fail_count, fail_window_start
        now = time.monotonic()
        if fail_window_start == 0.0 or (now - fail_window_start) > fail_window_s:
            fail_window_start = now
            fail_count = 0
        fail_count += 1
        logger.warning("[hid] Link failure (%s). failures=%d/%d", reason, fail_count, fail_threshold)

        if fail_count >= fail_threshold:
            logger.warning("[hid] Too many failures; power-cycling adapter and re-baselining.")
            try:
                await _power_cycle_adapter(bus, adapter_name)
            except Exception as e:
                logger.warning("[hid] Adapter power-cycle failed: %s", e)
            try:
                await ensure_controller_baseline(bus, adapter_name)
            except Exception as e:
                logger.warning("[hid] Baseline after power-cycle failed: %s", e)
            fail_window_start = time.monotonic()
            fail_count = 0

    while True:
        try:
            await ensure_controller_baseline(bus, adapter_name)
        except Exception as e:
            logger.warning("[hid] Baseline ensure failed: %s", e)

            try:
                # Avoid double-registering advertisements (can hit BlueZ "Maximum advertisements reached").
                # We track advertising state via BlueZ ActiveInstances (and our helper), not the advert object's
                # internal "registered" flag (which may not be maintained by all libraries).
                if not await _advertising_active(adapter_name):
                    await _adv_register_and_start(bus, advert, adapter_name)
            except Exception as e:
                logger.warning("[hid] Advertisement register failed: %s", e)
                await asyncio.sleep(1.0)
                continue

        logger.info("[hid] advertising (waiting for central)…")
        dev_path = await wait_for_any_connection(bus, adapter_name, timeout_s=connect_wait_s)
        if not dev_path:
            continue

        logger.info("[hid] connected: %s", dev_path)

        ready_deadline = time.monotonic() + ready_deadline_s
        have_services = False
        have_bond = False
        have_cccd = False

        while time.monotonic() < ready_deadline:
            managed = await _get_managed_objects(bus)
            dev = managed.get(dev_path, {}).get("org.bluez.Device1")
            if not dev or not bool(dev.get("Connected", False)):
                await _note_failure("disconnected during setup")
                break

            if not have_services:
                have_services = await wait_until_services_resolved(bus, dev_path, timeout_s=0.5)

            if not have_bond:
                have_bond = await wait_until_bonded(bus, dev_path, timeout_s=0.5)

            have_cccd = bool(getattr(hid, "_notif_state", {}))

            if have_services and have_bond and have_cccd:
                logger.info("[hid] ready (services+bond+cccd).")
                break

            if have_services and have_bond and not have_cccd:
                if (ready_deadline - time.monotonic()) < cccd_grace_s:
                    ready_deadline = time.monotonic() + cccd_grace_s

            await asyncio.sleep(0.2)
        else:
            await _note_failure("setup timeout")

        managed = await _get_managed_objects(bus)
        dev = managed.get(dev_path, {}).get("org.bluez.Device1", {})
        if not bool(dev.get("Connected", False)):
            await asyncio.sleep(0.5)
            continue

        if have_bond:
            try:
                if getattr(advert, "registered", False):
                    await advert.unregister(bus)
            except Exception as e:
                logger.warning("[hid] Advertisement unregister failed: %s", e)

        logger.info("[hid] link active; waiting for disconnect…")
        await wait_until_disconnected(bus, dev_path)
        logger.info("[hid] disconnected; restarting advertising loop")
        await asyncio.sleep(0.5)
class BatteryService(Service):
    def __init__(self, initial_level: int = 100):
        super().__init__("180F", True)
        lvl = max(0, min(100, int(initial_level)))
        self._level = bytearray([lvl])

    @characteristic("2A19", CharFlags.READ | CharFlags.NOTIFY)
    def battery_level(self, _):
        # 0..100
        return bytes(self._level)

    # Convenience: call this to update + notify
    def set_level(self, pct: int):
        pct = max(0, min(100, int(pct)))
        if self._level[0] != pct:
            self._level[0] = pct
            try:
                self.battery_level.changed(bytes(self._level))
            except Exception:
                pass

class DeviceInfoService(Service):
    def __init__(self, manufacturer="PiKB Labs", model="PiKB-1", vid=0xFFFF, pid=0x0001, ver=0x0100):
        super().__init__("180A", True)
        self._mfg   = manufacturer.encode("utf-8")
        self._model = model.encode("utf-8")
        self._pnp   = bytes([0x02, vid & 0xFF, (vid>>8)&0xFF, pid & 0xFF, (pid>>8)&0xFF, ver & 0xFF, (ver>>8)&0xFF])

    @characteristic("2A29", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def manufacturer_name(self, _):
        return self._mfg

    @characteristic("2A24", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def model_number(self, _):
        return self._model

    @characteristic("2A50", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def pnp_id(self, _):
        return self._pnp

class HIDService(Service):
    def __init__(self):
        super().__init__("1812", True)
        self._proto = bytearray([1])  # Report Protocol
        self._link_ready: bool = False

    # -------- subscription helpers --------
    def _is_subscribed(self, char) -> bool:
        # Supports both property and method styles found in bluez_peripheral
        for attr in ("is_notifying", "notifying"):
            if hasattr(char, attr):
                v = getattr(char, attr)
                return v() if callable(v) else bool(v)
        # If the library doesn’t expose state, assume subscribed
        return True

    def _notif_state(self) -> tuple[bool, bool, bool]:
        kb   = self._is_subscribed(self.input_keyboard)
        boot = self._is_subscribed(self.boot_keyboard_input)
        cc   = self._is_subscribed(self.input_consumer)
        return (kb, boot, cc)

    # ---------------- GATT Characteristics ----------------
    # Protocol Mode (2A4E): READ/WRITE (encrypted both)
    @characteristic("2A4E", CharFlags.READ | CharFlags.WRITE | CharFlags.ENCRYPT_READ | CharFlags.ENCRYPT_WRITE)
    def protocol_mode(self, _):
        return bytes(self._proto)
    @protocol_mode.setter
    def protocol_mode_set(self, value, _):
        self._proto[:] = value

    # HID Information (2A4A): READ (encrypted)
    @characteristic("2A4A", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def hid_info(self, _):
        return bytes([0x11, 0x01, 0x00, 0x03])  # bcdHID=0x0111, country=0, flags=0x03

    # HID Control Point (2A4C): WRITE (encrypted)
    @characteristic("2A4C", CharFlags.WRITE | CharFlags.WRITE_WITHOUT_RESPONSE | CharFlags.ENCRYPT_WRITE)
    def hid_cp(self, _):
        return b""
    @hid_cp.setter
    def hid_cp_set(self, _value, _):
        pass

    # Report Map (2A4B): READ (encrypted)
    @characteristic("2A4B", CharFlags.READ | CharFlags.ENCRYPT_READ)
    def report_map(self, _):
        return REPORT_MAP

    # Keyboard input (Report-mode, RID 1) — 8-byte payload
    @characteristic("2A4D", CharFlags.READ | CharFlags.NOTIFY)
    def input_keyboard(self, _):
        return bytes([0,0,0,0,0,0,0,0])
    @input_keyboard.descriptor("2908", DescFlags.READ)
    def input_keyboard_ref(self, _):
        return bytes([RID_KEYBOARD, 0x01])

    # Consumer input (RID 2) — 2-byte payload (16-bit usage)
    @characteristic("2A4D", CharFlags.READ | CharFlags.NOTIFY)
    def input_consumer(self, _):
        return bytes([0,0])
    @input_consumer.descriptor("2908", DescFlags.READ)
    def input_consumer_ref(self, _):
        return bytes([RID_CONSUMER, 0x01])

    # Boot Keyboard Input (2A22) — 8-byte payload (no report ID)
    @characteristic("2A22", CharFlags.READ | CharFlags.NOTIFY)
    def boot_keyboard_input(self, _):
        return bytes([0,0,0,0,0,0,0,0])

    # ---------------- Send helpers ---------------- 
    @staticmethod
    def _kb_payload(keys=(), modifiers=0) -> bytes:
        keys = list(keys)[:6] + [0] * (6 - len(keys))
        return bytes([modifiers, 0] + keys)  # 8-byte boot/report keyboard frame
    
    def send_keyboard(self, payload: bytes) -> None:
        if not self._link_ready:
            return
        try:
            # Protocol Mode: 0x01 = Report (default), 0x00 = Boot
            if getattr(self, "_proto", b"\x01")[0] == 0x01:
                self.input_keyboard.changed(payload)        # Report
            else:
                self.boot_keyboard_input.changed(payload)   # Boot
        except Exception:
            pass
    
    def send_consumer(self, payload: bytes) -> None:
        if not self._link_ready:
            return
        try:
            self.input_consumer.changed(payload)
        except Exception:
            pass

    async def key_tap(self, usage, hold_ms=40, modifiers=0):
        down = self._kb_payload([usage], modifiers)
        self.send_keyboard(down)
        await asyncio.sleep(hold_ms / 1000)
        up = self._kb_payload([], 0)
        self.send_keyboard(up)

    def cc_payload_usage(self, usage_id: int) -> bytes:
        return bytes([usage_id & 0xFF, (usage_id >> 8) & 0xFF])

    async def consumer_tap(self, usage_id, hold_ms=60):
        self.send_consumer(self.cc_payload_usage(usage_id))
        await asyncio.sleep(hold_ms/1000)
        self.send_consumer(self.cc_payload_usage(0))

    def release_all(self):
        self.send_keyboard(self._kb_payload([], 0))
        self.send_consumer(self.cc_payload_usage(0))

@dataclass
class HidRuntime:
    bus: any
    adapter: any
    advert: any
    hid: any
    tasks: list

async def start_hid(config) -> tuple[HidRuntime, callable]:
    """
    Start the BLE HID server. Returns (runtime, shutdown) where shutdown is an async callable.
    - config.device_name   : BLE local name (string)
    - config.appearance    : GAP appearance (int, default 0x03C1)
    """
    import contextlib

    device_name = getattr(config, "device_name", None) or os.uname().nodename
    appearance  = int(getattr(config, "appearance", APPEARANCE))

    bus = await get_message_bus()
    if not await is_bluez_available(bus):
        raise RuntimeError("BlueZ not available on system DBus.")

    # Adapter
    adapter_name = getattr(config, "adapter_name", getattr(config, "adapter", "hci0"))
    try:
        xml = await bus.introspect("org.bluez", f"/org/bluez/{adapter_name}")
    except Exception as exc:
        raise RuntimeError(f"Bluetooth adapter {adapter_name} not found") from exc

    proxy = bus.get_proxy_object("org.bluez", f"/org/bluez/{adapter_name}", xml)
    adapter = Adapter(proxy)
    # Establish a stable baseline BEFORE we touch name/advertising.
    await ensure_controller_baseline(bus, adapter_name, adapter_proxy=proxy)
    await adapter.set_alias(device_name)

    # Agent
    agent = NoIoAgent()
    await agent.register(bus, default=True)

    await ensure_controller_baseline(bus, adapter_name, adapter_proxy=proxy)
    # Services
    dis = DeviceInfoService()
    bas = BatteryService(initial_level=100)
    hid = HIDService()

    global _hid_service_singleton
    _hid_service_singleton = hid

    app = ServiceCollection()
    app.add_service(dis)
    app.add_service(bas)
    app.add_service(hid)

    async def _power_cycle_adapter():
        """Toggle adapter power and then re-apply our baseline settings."""
        try:
            await adapter.set_powered(False)
            await asyncio.sleep(0.4)
            await adapter.set_powered(True)
            await asyncio.sleep(0.8)
        except Exception as e:
            logger.warning("[hid] Bluetooth adapter power-cycle failed: %s", e)
        # Power-cycling can reset Pairable/Discoverable; re-assert them immediately.
        await ensure_controller_baseline(bus, adapter_name, adapter_proxy=proxy)
        # Alias sometimes gets cleared after a controller reset on some stacks.
        with contextlib.suppress(Exception):
            await adapter.set_alias(device_name)

    # --- Register GATT application (with one retry) ---
    try:
        await app.register(bus, adapter=adapter)
    except Exception as e:
        logger.warning("[hid] BTLE service register failed: %s — retrying after power-cycle", e)
        await _power_cycle_adapter()
        await adapter.set_alias(device_name)
        try:
            await app.register(bus, adapter=adapter)
        except Exception as e2:
            raise RuntimeError(f"GATT application register failed after retry: {e2}") from e2

    # --- Register + start advertising (with one retry + fresh advert object) ---
    advert = _make_advert(device_name, appearance)
    mode = await _adv_register_and_start(bus, advert)
    if mode in ("error", "noop"):
        logger.warning("[hid] advert register/start failed (%s) — retrying after power-cycle", mode)

        with contextlib.suppress(Exception):
            await _adv_unregister(bus, advert)

        await _power_cycle_adapter()
        await adapter.set_alias(device_name)

        # Fresh advert object (fresh DBus path)
        advert = _make_advert(device_name, appearance)
        mode = await _adv_register_and_start(bus, advert)
        if mode in ("error", "noop"):
            with contextlib.suppress(Exception):
                await app.unregister()
            raise RuntimeError(f"Advertising register failed after retry: mode={mode}")

    logger.info("[hid] advertising registered as %s on %s", device_name, adapter_name)

    # Watcher / startup sanity
    try:
        link_task = asyncio.create_task(watch_link(bus, adapter_name, advert, hid))
        tasks = [link_task]
        # Make sure we start from a clean HID state (harmless on first boot)
        with contextlib.suppress(Exception):
            hid.release_all()
    except Exception:
        # If anything fails after we registered the app/advertisement, clean up so we
        # don't leak advertisements (which leads to 'Maximum advertisements reached').
        with contextlib.suppress(Exception):
            await _adv_unregister(bus, advert)
        with contextlib.suppress(Exception):
            await app.unregister()
        with contextlib.suppress(Exception):
            hid.release_all()
        raise

    async def shutdown():
        for t in list(tasks):
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)

        with contextlib.suppress(Exception):
            await _adv_unregister(bus, advert)

        with contextlib.suppress(Exception):
            await app.unregister()

        with contextlib.suppress(Exception):
            hid.release_all()

        hid._link_ready = False

    runtime = HidRuntime(bus=bus, adapter=adapter, advert=advert, hid=hid, tasks=tasks)
    return runtime, shutdown
