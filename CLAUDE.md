# CLAUDE.md

Context for Claude Code working in this repo. Read this first; the README has the user-facing details.

## What this project is

Home Assistant integration for a **JVC KD-SX38MBT** car receiver, over its Bluetooth:

1. **The JVC Remote protocol** (Bluetooth Classic SPP, RFCOMM channel 16), decoded from the app's
   logcat: `PROTOCOL.md`. `tools/jvc-probe.py talk` speaks it; `tools/decode-logcat.py` decodes captures.
2. **Radio audio** (`radio/`, `ENABLE_RADIO_AUDIO`): Music Assistant → Squeezelite → BlueALSA → A2DP → radio.
   `radio-link.sh` reconnects A2DP; `avrcp-player.py` registers a BlueZ media player that mirrors HA's
   `media_player.car_radio` (state, track) to the radio's display and sends the radio's buttons back to
   HA. It needs an HA long-lived token in `/etc/jvc2mqtt/ha-token` (mode 600, asked in `--configure`,
   never in the config).
3. **The `jvc2mqtt` bridge** (to do): radio control and status as HA entities over MQTT.

`install.sh` installs it on a Debian/DietPi host: config `/etc/jvc2mqtt/jvc2mqtt.conf`, code in
`/opt/jvc2mqtt`, services `jvc2mqtt-radio-link` / `jvc2mqtt-radio-avrcp`, commands `jvc-probe`,
`jvc2mqtt-install`, `jvc2mqtt-check`.

The owner is Adam (GitHub `A7QG59F2`). He runs things from a **Windows PC**, so PowerShell for anything
on the PC side.

## Where it runs

An **Odroid C2** on DietPi (Debian 13 trixie) with a **TP-Link UB500** (RTL8761BU, `hci0`). The board
itself (firmware, BlueZ power-up, Wi-Fi, the dashboard kiosk, the optional BLE proxy) is set up by the
separate repo **A7QG59F2/ODroid-C2-bluetooth-proxy**; this repo only does the radio. Don't move board
setup in here.

| Part | Notes |
|---|---|
| JVC KD-SX38MBT | BT MAC `88:E0:34:B7:98:6C`. Accepts one remote at a time (the phone's Bluetooth must be off). |
| First board | Odroid C2 at 192.168.1.127 (Wi-Fi). HA at 192.168.1.3:8123. bt-proxy **off**. |
| Adam's phone | Samsung SM-S938U1; adb needs **Auto Blocker off** |

## Key decisions (don't undo without asking)

- **No BLE proxy on the same dongle.** bt-proxy's constant BLE scanning makes outgoing BR/EDR connects
  from the RTL8761BU fail with "Host is down" (an already-open link survives scanning). install.sh and
  `--check` warn when `bt-proxy` is active. A second dongle only if ever needed.
- **SPP and A2DP share one link** to the radio; the bridge must coexist with radio audio.
- **This computer owns the radio's clock**: set it (`3b 35` / `3b 37`) from local time on every connect,
  later also periodically. The host's time zone must be right.
- The HA token lives only in `/etc/jvc2mqtt/ha-token`, never in the config or on a command line.
- **Moved out of the Odroid repo on 2026-11-17** (started fresh, no history carried over). install.sh
  migrates a board once: copies `JVC_MAC`, `JVC_CHANNEL`, `ENABLE_RADIO_AUDIO`, `RADIO_*`, `HA_API_URL`
  (or derives it from `HA_URL`) and the token from `/etc/odroid-ha/`, deletes those keys (and
  `ENABLE_JVC_TOOLS`) from `odroid-ha.conf`, and replaces the `odroid-ha-radio-*` units. Keep that
  migration until every board has run it.

## Status (as of 2026-11-17)

- Protocol decoded (2026-11-11); `jvc-probe talk` handles the hello, ACKs and the clock.
- **Radio audio verified** (2026-11-11): Music Assistant plays through the radio (player `Car-Radio`,
  HA `media_player.car_radio`, MA's Squeezelite provider added); A2DP comes back ~18 s after the radio
  returns. Keep test volume low.
- The split from the Odroid repo hasn't been run on the first board yet.

## Next work

1. **Move the first board over**: on the Odroid, `odroid-ha-install` (turns on its `ENABLE_BLUETOOTH`),
   then this repo's `install.sh`; check `jvc2mqtt-check`.
2. **AVRCP player on the radio**: confirm the display leaves STOP, shows the track, and the radio's
   buttons control Music Assistant.
3. **Frame builder + live tests** (open points in PROTOCOL.md): subwoofer level scale (`3d 43` / `3d 44`),
   other `3d 14` parameters for the second color zone / night colors, key codes `09`+ (power?),
   Up/Down and play/pause on BT audio. With adb you can screenshot the phone (`adb shell screencap`) and
   tap the app (`adb shell input tap/swipe`), but ask first and put back any radio setting you change.
4. **The `jvc2mqtt` bridge**: a Python systemd service that holds the RFCOMM link (reconnecting; it drops
   on its own now and then), maps MQTT topics to commands, publishes the notifications, uses MQTT
   Discovery, and sets the clock on connect and periodically (drift, DST).

## Conventions

- Shell scripts must pass `shellcheck -S warning` and `bash -n`. Keep `install.sh` idempotent and re-runnable.
- New settings go in `config/jvc2mqtt.conf.example` **and** the README settings table, with safe defaults
  in the scripts (`${VAR:-default}`). install.sh appends any example key missing from the live config.
  If a new setting is a per-install choice, also ask it in `configure()`.
- Switching a part off must undo what it set up; `--check` only reports enabled parts.
- Don't commit logcat captures (they contain whatever the phone was playing).
