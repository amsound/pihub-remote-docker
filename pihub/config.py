"""Configuration helpers for environment-driven settings."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _getenv_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # All fields are passed explicitly by Config.load(), so we don’t put per-field defaults here.
    ha_ws_url: str
    ha_token_file: str
    ha_activity: str
    ha_cmd_event: str

    usb_grab: bool

    ble_adapter: str
    ble_device_name: str

    health_host: str
    health_port: int


    @staticmethod
    def load() -> "Config":
        """Build a Config from environment (compose env)."""
        ha_ws_url     = os.getenv("HA_WS_URL", "ws://127.0.0.1:8123/api/websocket")
        ha_token_file = os.getenv("HA_TOKEN_FILE", "/run/secrets/ha_token")
        ha_activity   = os.getenv("HA_ACTIVITY", "input_select.activity")
        ha_cmd_event  = os.getenv("HA_CMD_EVENT", "pihub.cmd")

        usb_grab      = _getenv_bool("USB_GRAB", True)

        ble_adapter      = os.getenv("BLE_ADAPTER", "hci0")
        ble_device_name  = os.getenv("BLE_DEVICE_NAME", "PiHub Remote")

        health_host   = os.getenv("HEALTH_HOST", "0.0.0.0")
        try:
            health_port = int(os.getenv("HEALTH_PORT", "9123"))
        except ValueError:
            health_port = 9123

        return Config(
            ha_ws_url=ha_ws_url,
            ha_token_file=ha_token_file,
            ha_activity=ha_activity,
            ha_cmd_event=ha_cmd_event,
            usb_grab=usb_grab,
            ble_adapter=ble_adapter,
            ble_device_name=ble_device_name,
            health_host=health_host,
            health_port=health_port,
        )

    def load_token(self) -> str:
        """Return the HA token from environment or configured file."""
        # 1) explicit env wins
        env_tok = (os.getenv("HA_TOKEN") or "").strip()
        if env_tok:
            return env_tok

        # 2) fall back to file
        path = (self.ha_token_file or "").strip()
        if not path:
            raise RuntimeError("HA token unavailable: set HA_TOKEN or provide HA_TOKEN_FILE")

        try:
            with open(path, "r", encoding="utf-8") as f:
                token = f.read().strip()
        except FileNotFoundError as exc:
            raise RuntimeError(f"HA token file not found: {path}") from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to read HA token file {path}: {exc}") from exc

        if not token:
            raise RuntimeError(f"HA token file {path} is empty")

        return token
