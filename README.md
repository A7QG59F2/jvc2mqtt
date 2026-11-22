# jvc2mqtt – JVC KD-SX38MBT car receiver ↔ Home Assistant

Control a **JVC KD-SX38MBT** car receiver from Home Assistant, and play Music Assistant through it,
over the receiver's Bluetooth. It runs on **any Linux box with Bluetooth in range of the radio**
(Debian, DietPi, Raspberry Pi OS); the adapter is a setting (`BT_ADAPTER`). The first host is an
Odroid C2 with a TP-Link UB500 dongle, whose board setup lives in
[ODroid-C2-bluetooth-proxy](https://github.com/A7QG59F2/ODroid-C2-bluetooth-proxy); nothing here
depends on it.

The radio accepts **one remote at a time**: while the bridge holds the link, the JVC Remote phone
app can't connect (turn `switch.car_radio_remote_link` off to let it in).

| Part | Status |
|---|---|
| **Protocol** of the JVC Remote app (Bluetooth Classic SPP) | Decoded: [`PROTOCOL.md`](PROTOCOL.md) |
| **`jvc-probe`**: pair, scan, listen, and talk to the radio | Works (`talk` answers the hello, ACKs, sets the clock, sends commands) |
| **Radio audio**: Music Assistant player through the radio (Squeezelite → BlueALSA → A2DP) | Works |
| **Track display + buttons** (AVRCP player mirroring HA's `media_player`) | Being tested |
| **`jvc2mqtt` bridge**: radio as HA entities over MQTT | Written, tested against a simulated radio; live test next |

## Install

On the computer that will talk to the radio (Debian/DietPi/Raspberry Pi OS with a Bluetooth adapter):

```bash
apt-get update && apt-get install -y git
git clone https://github.com/A7QG59F2/jvc2mqtt /opt/jvc2mqtt/src
cd /opt/jvc2mqtt/src && ./install.sh
```

Over SSH use `ssh -t` so it can ask its questions:

```
Radio's Bluetooth address (bluetoothctl devices) [88:E0:34:B7:98:6C]
Play Music Assistant through the radio over Bluetooth? [Y/n]
  Player name in Music Assistant (no spaces) [Car-Radio]
  Home Assistant address (empty = http://homeassistant.local:8123) []
  ...Home Assistant long-lived access token
Run the jvc2mqtt bridge (radio settings as Home Assistant entities over MQTT)? [y/N]
  MQTT broker (empty = homeassistant.local) []
  MQTT user / password
```

On an Odroid, turn on **Bluetooth** (the UB500) in `odroid-ha-install --configure` first, and leave the
**Bluetooth proxy off**: its constant BLE scanning stops the dongle from opening a connection to the
radio ("Host is down"). A link that's already open survives scanning.

Afterwards:

```bash
jvc2mqtt-check                    # health report
jvc2mqtt-install --configure      # change the answers
jvc2mqtt-install                  # pull the latest version and re-apply
```

**Moving from the Odroid repo:** these parts used to be installed by ODroid-C2-bluetooth-proxy
(`ENABLE_JVC_TOOLS`, `ENABLE_RADIO_AUDIO`). The first run here takes over: it copies those settings and
the HA token into `/etc/jvc2mqtt/`, removes them from `/etc/odroid-ha/odroid-ha.conf`, and replaces the
`odroid-ha-radio-*` services with `jvc2mqtt-radio-*`.

**The radio's clock** is set from this computer's local time on every connect (and hourly by the
bridge), so the time zone must be right (`timedatectl`; on the Odroid, `TIMEZONE` in its config).

**Moving to another host:** install there, pair the radio with it, and set `SQUEEZELITE_MAC` to the
old player ID (`jvc2mqtt-check` shows it) so Music Assistant keeps the same player.

## 1. Pair the computer with the radio

Turn the **phone's Bluetooth off** first, because the receiver only accepts one remote at a time.

```bash
sudo bluetoothctl
  power on
  agent on
  default-agent
  scan on            # wait for KD-SX38MBT  88:E0:34:B7:98:6C
  scan off
  pair 88:E0:34:B7:98:6C
  trust 88:E0:34:B7:98:6C
  quit
```

On the receiver: **[REMOTE APP] → [SELECT] → [ANDROID] → [ANDROID LIST]** and pick this computer (its hostname).

## 2. Find the SPP channel

```bash
sudo jvc-probe scan
```

Open RFCOMM channels are **7, 9 and 16**. **Channel 16 is the JVC Remote link** (`JVC_CHANNEL`).
7 and 9 are most likely hands-free (HFP) and another profile.

## 3. Talk to the radio

Every frame needs a fresh sequence number and CRC, and the radio's `3b 02` hello must be answered
within ~20 s or it drops the link (see [`PROTOCOL.md`](PROTOCOL.md) → Connect handshake). `talk`
does all of that (hello, ACKs, the app's startup queries, clock/date from local time):

```bash
sudo jvc-probe talk --do "11 05 08" --do "sleep 2" --do "11 05 07"   # Vol+, Vol−
sudo jvc-probe listen --channel 16                                     # just print what it sends
```

## 4. Capture more of the app's commands (phone + PC)

JVC Remote logs every frame it sends and receives to logcat, so no HCI snoop log or Wireshark
is needed. On the PC, with the phone on USB debugging (on Samsung, **Auto Blocker must be off**
or adb is blocked):

```powershell
adb logcat -c; adb logcat -v time > jvc-logcat.txt
```

Press buttons in JVC Remote **one at a time**, noting the order, then stop with Ctrl+C and decode:

```powershell
python tools\decode-logcat.py jvc-logcat.txt              # frames + touch events
python tools\decode-logcat.py jvc-logcat.txt --after 00:20:00 --all
```

The log contains whatever the phone is playing (track names etc.), so don't commit it.

Fallback if a future app version stops logging: **Developer options → Enable Bluetooth HCI snoop
log**, Bluetooth off/on, capture, then `adb bugreport` and open `btsnoop_hci.log` in Wireshark
(`btrfcomm || btspp`).

## Radio audio (Music Assistant through the radio)

Squeezelite plays into BlueALSA, which streams A2DP to the radio. `jvc2mqtt-radio-link` reconnects the
radio's audio whenever it comes back (car started; about 18 s after the radio is on). In Music Assistant:
**Settings → Providers → add "Squeezelite (slimproto)"**; the player (`Car-Radio`) appears once the radio
is connected, and HA gets `media_player.car_radio`.

With a Home Assistant token, `jvc2mqtt-radio-avrcp` also registers a Bluetooth media player, so the
radio's display shows the play state and track, and its play/pause/next/previous buttons control the
HA `media_player`. Without a token the radio shows STOP. Button presses are sent to HA without
waiting for its reply; while HA is still busy with two presses, further ones are dropped, not queued.

**Sound settings for radio playback are made on the radio** (the bridge's EQ, loudness, bass,
fader/balance entities), not with Music Assistant's DSP: MA restarts playback on every DSP change.

Skips at the start of playback are most likely Squeezelite underruns while the A2DP stream starts;
`SQUEEZELITE_BUFFER` (`-b`) and `SQUEEZELITE_ALSA` (`-a`) set bigger buffers (untested).

## Settings (`/etc/jvc2mqtt/jvc2mqtt.conf`)

| Key | Default | Meaning |
|---|---|---|
| `JVC_MAC` | empty | The radio's Bluetooth address (asked by `--configure`) |
| `JVC_CHANNEL` | `16` | RFCOMM channel of the JVC Remote (SPP) service |
| `BT_ADAPTER` | `hci0` | Bluetooth adapter that talks to the radio |
| `ENABLE_RADIO_AUDIO` | `1` | Music Assistant player that plays through the radio |
| `RADIO_AUDIO_NAME` | `Car-Radio` | Player name in Music Assistant |
| `RADIO_AUDIO_SERVER` | empty (auto) | Music Assistant host, if auto-discovery fails |
| `RADIO_RECONNECT_SECONDS` | `15` | Retry interval while the radio is off |
| `SQUEEZELITE_BUFFER` | `4096:8192` | Squeezelite `-b` stream:output buffer (KB); empty = Squeezelite's default |
| `SQUEEZELITE_ALSA` | `200:4` | Squeezelite `-a` BlueALSA buffer (ms):periods; empty = default (40:4) |
| `SQUEEZELITE_MAC` | empty | Player MAC = Music Assistant player ID; pin it when moving hosts |
| `RADIO_HA_ENTITY` | empty (`media_player.<name>`) | HA media_player the radio's display and buttons follow. Needs the HA token `--configure` asks for (kept in `/etc/jvc2mqtt/ha-token`, mode 600, never in the config) |
| `HA_API_URL` | empty (`http://homeassistant.local:8123`) | Home Assistant base URL for the API, e.g. `http://192.168.1.3:8123` |
| `ENABLE_BRIDGE` | `0` | The jvc2mqtt bridge (asked by `--configure`) |
| `MQTT_HOST` / `MQTT_PORT` | empty (`homeassistant.local`) / `1883` | MQTT broker (the Mosquitto app) |
| `MQTT_USER` | empty | MQTT user; the password is kept in `/etc/jvc2mqtt/mqtt-password` (mode 600), never in the config |
| `RADIO_ENTITY_PREFIX` | `car_radio` | Entity IDs are `<domain>.<prefix>_<setting>` |
| `VOLUME_LIMIT` | `25` | Highest volume HA may set (the knob still goes higher) |
| `MQTT_PREFIX` / `HA_DISCOVERY_PREFIX` | `jvc2mqtt` / `homeassistant` | Topic prefixes |

## Bridge (`jvc2mqtt.service`, `ENABLE_BRIDGE`)

Keeps the RFCOMM link open (hello, ACKs, reconnect when it drops), sets the radio's clock on connect
and hourly, and creates these entities through MQTT Discovery (device **Car Radio**):

| Entity | Radio side |
|---|---|
| `number.car_radio_volume` | reported by `12 04`; set by stepping Vol+/Vol− up to `VOLUME_LIMIT` |
| `number.car_radio_fader`, `number.car_radio_balance` | −15 … +15, + = front / right; get `3d 0b`, set `3d 0c` |
| `select.car_radio_eq_preset` | 11 presets incl. USER; `3d 0e` / `3d 0f` |
| `number.car_radio_eq_band_01` … `_13` | dB (62.5 Hz … 16 kHz), `3d 04` / `3d 05`; edits within 1 s are saved together |
| `select.car_radio_loudness` | Off, Level 1–2; `3d 4c` / `3d 4d` |
| `select.car_radio_bass_boost` | Off, Level 1–5; `3d 49` / `3d 4a` |
| `switch.car_radio_bass_extend` | `3d 40` / `3d 41` |
| `select.car_radio_source` | AUX, AM, FM, BT audio; `11 01` |
| `sensor.car_radio_tuner`, `_rds`, `_title`, `_artist` | notifications |
| `switch.car_radio_remote_link` | off = let go of the radio so the JVC Remote app can connect |
| `binary_sensor.car_radio_connected` | link up |

The ranges of fader/balance and the EQ bands' layout are still to be confirmed live
(`jvc-tests balance`). Stop the bridge before using `jvc-probe` or `jvc-tests`
(`systemctl stop jvc2mqtt`), since only one remote can talk to the radio.

The Music Assistant player for the radio is `media_player.car_radio` (MA player ID = Squeezelite's
MAC, shown by `jvc2mqtt-check`).
