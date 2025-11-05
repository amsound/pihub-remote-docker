# PiHub – Universal Remote Bridge (Raspberry Pi)

PiHub turns a Raspberry Pi into a tiny, fast “universal remote” bridge.
It listens to RF key events from a Logitech Unifying receiver and sends actions to:

* **Home Assistant (HA)** over WebSocket (`pihub.cmd` events)
* **BLE HID** (Consumer + Keyboard) for things like Apple TV

It’s lightweight, stateless, and tuned for **Raspberry Pi 3B+ (aarch64)**.

---

## ✨ Features

* **RF → Actions** via Linux `evdev`, mapped to canonical `rem_*` names
* **HA WebSocket**: subscribe to `input_select.activity` to change keymap and receive commands
* **BLE Output**: per-button **Consumer + Keyboard** usages (edges only)
* **Macros**: **HA-driven only** e.g. `{"text":"macro","name":"power_off"}` returning Apple TV to Home Screen
* **Precise edges**: explicit **down/up**; ignores kernel auto-repeat
* **Optional synthetic repeats** (global initial/rate, defaults 400/400ms; off unless enabled per-key)
* **Long-press**: hold for X ms then trigger
* **No queueing**: drops actions when offline; **reconnects with jitter**

---

## 🧩 Requirements

* Raspberry Pi 3B+ (tested on **aarch64** Raspberry Pi OS Lite)
* Logitech Unifying receiver
* Home Assistant reachable over WebSocket (`/api/websocket`)
* BlueZ running on the host (DBus socket exposed to the container)

---

## 🚀 Quick Start

### 1) Build (multi-stage, small runtime)

```bash
# From repo root
export DOCKER_BUILDKIT=1
docker build -f Dockerfile -t pihub:latest .
```

### 2) Run (without compose, for a quick test)

```bash
docker run --rm \
  --network host \
  -v /dev/input:/dev/input:ro \
  -v /var/run/dbus:/var/run/dbus:ro \
  -e HA_URL="ws://<ha-host>:8123/api/websocket" \
  -e HA_TOKEN="<your-long-lived-access-token>" \
  pihub:latest
```

### 3) docker-compose.yml (recommended)

```yaml
services:
  pihub:
    image: pihub:latest
    network_mode: host
    restart: unless-stopped
    cpu_shares: 2048
    device_cgroup_rules:
      - 'c 13:* r'
    environment:
      HA_TOKEN: "${HA_TOKEN}"          # ENV takes precedence
      # HA_URL: "ws://127.0.0.1:8123/api/websocket" # defaults to local
      # TOKEN_FILE: "/run/secrets/ha"    # optional fallback
      # INPUT_EVENT_DEVICE: "/dev/input/eventX"  # optional override
      # REPEAT_INITIAL_MS: "400"
      # REPEAT_RATE_MS: "400"
    volumes:
      - /dev/input:/dev/input:ro
      - /dev/input/by-id:/dev/input/by-id:ro
      - /run/dbus:/run/dbus
      - /etc/localtime:/etc/localtime:ro
      - /etc/timezone:/etc/timezone:ro
```

> ✅ Works without `--privileged`. If BlueZ features change in your distro, you may need to add capabilities later—but most setups don’t.

---

## ⚙️ Configuration

| Variable             | Description                                                   | Default / Notes                    |
| -------------------- | ------------------------------------------------------------- | ---------------------------------- |
| `HA_URL`             | Home Assistant WebSocket URL                                  | ws://127.0.0.1:8123/api/websocket  |
| `HA_TOKEN`           | HA Long-Lived Access Token                                    | **Preferred** (env takes priority) |
| `TOKEN_FILE`         | Path to a file containing the HA token                        | Fallback if `HA_TOKEN` not set     |
| `INPUT_EVENT_DEVICE` | Optional explicit evdev device (e.g., `/dev/input/event2`)    | Auto-picks first *Unifying* device |
| `REPEAT_INITIAL_MS`  | Global initial delay for synthetic repeats                    | `400`                              |
| `REPEAT_RATE_MS`     | Global repeat rate                                            | `400`                              |

**Fail-fast:** the app exits on startup if it can’t obtain an HA token from env or file.

---

## 🔌 Event Contracts

### PiHub → Home Assistant (events)

PiHub uses your existing **bidirectional** `pihub.cmd` convention and **does not change schema**.

Example:

```json
{"dest":"ha","text":"media_next"}
```

Tuning with station:

```json
{"dest":"ha","text":"tune","station":"BBC Radio 6"}
```

### Home Assistant → PiHub (commands/state)

* Activity/state: push updates for `input_select.activity` to switch keymap modes
* Commands (unchanged schema), e.g.:

**Macro (HA-driven only):**

```json
{"text":"macro","name":"power_off"}
```

**Send one BLE key:**

```json
{"text":"ble_key","usage":"consumer","code":"menu_up","hold_ms":40}
```

> BLE: per-button may use **Consumer or Keyboard** usages (edges only, **no repeats**).
> Macros: **Consumer-only**, executed from HA (Pi does not run macro sequences).

---

## ⌨️ Input Mapping

* Reads from `/dev/input` Unifying device via `evdev`
* Ignores kernel auto-repeat; uses only `down/up` edges
* Falls back to `MSC_SCAN` for stubborn keys
* Maps physical keys → canonical `rem_*` names, then keymap decides action:

  * `ha_text` → emits `{"dest":"ha","text":...}`
  * `ble_key` → sends BLE Consumer/Keyboard usage with optional `hold_ms`
  * Optional `long_press_ms`
  * Optional `repeat` (synthetic; **HA only**)

---

## 🧠 Behavior & Resilience

* **Drop when offline**: commands return `False`; nothing is queued
* **Reconnect with jitter**: automatic, capped backoff
* **Minimal logs**: quiet by default; env-gated debug available
* **Seed-then-subscribe**: fetch current activity once post-connect, then push-only

---

## 🧪 Troubleshooting

* **No input events?** Ensure the Unifying device appears under `/dev/input/by-id/*Unifying*` and that it’s bind-mounted read-only into the container.
* **BLE not reacting?** Verify BlueZ DBus socket is present (`/var/run/dbus/system_bus_socket`) and mounted read-only.
* **Offline drops?** Expected by design: when HA WS is down, send paths return `False` and do not crash the process.
* **Token issues?** Confirm `HA_TOKEN` is set (preferred) or `TOKEN_FILE` path is mounted and readable.

---

## 🏗️ Dev Notes

* Built with `aiohttp` (one session, WS pings/clean reconnect)
* Multi-stage Dockerfile for minimal runtime image: `Dockerfile`

---

## 🗺️ Roadmap

* [ ] Web Interface to override individual keys

