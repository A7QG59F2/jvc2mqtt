# jvc2mqtt – JVC KD-SX38MBT car receiver ↔ Home Assistant

Control a **JVC KD-SX38MBT** car receiver from Home Assistant, and play Music Assistant through it,
over the receiver's Bluetooth. It runs on a small Linux computer with Bluetooth; here that's an
Odroid C2 with a TP-Link UB500 dongle, set up by
[ODroid-C2-bluetooth-proxy](https://github.com/A7QG59F2/ODroid-C2-bluetooth-proxy).

| Part | Status |
|---|---|
| **Protocol** of the JVC Remote app (Bluetooth Classic SPP) | Decoded: [`PROTOCOL.md`](PROTOCOL.md) |
| **`jvc-probe`**: pair, scan, listen, and talk to the radio | Works (`talk` answers the hello, ACKs, sets the clock, sends commands) |
| **Radio audio**: Music Assistant player through the radio (Squeezelite → BlueALSA → A2DP) | Works |
| **Track display + buttons** (AVRCP player mirroring HA's `media_player`) | Being tested |
| **`jvc2mqtt` bridge**: radio as HA entities over MQTT | To do |

## Install

On the computer that will talk to the radio (Debian/DietPi, Bluetooth adapter on `hci0`):

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
```

On the Odroid, turn on **Bluetooth** (the UB500) in `odroid-ha-install --configure` first, and leave the
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

**The radio's clock** is set from this computer's local time on every connect, so the time zone must
be right (`timedatectl`; on the Odroid, `TIMEZONE` in its config).

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
HA `media_player`. Without a token the radio shows STOP.

## Settings (`/etc/jvc2mqtt/jvc2mqtt.conf`)

| Key | Default | Meaning |
|---|---|---|
| `JVC_MAC` | empty | The radio's Bluetooth address (asked by `--configure`) |
| `JVC_CHANNEL` | `16` | RFCOMM channel of the JVC Remote (SPP) service |
| `ENABLE_RADIO_AUDIO` | `1` | Music Assistant player that plays through the radio |
| `RADIO_AUDIO_NAME` | `Car-Radio` | Player name in Music Assistant |
| `RADIO_AUDIO_SERVER` | empty (auto) | Music Assistant host, if auto-discovery fails |
| `RADIO_RECONNECT_SECONDS` | `15` | Retry interval while the radio is off |
| `RADIO_HA_ENTITY` | empty (`media_player.<name>`) | HA media_player the radio's display and buttons follow. Needs the HA token `--configure` asks for (kept in `/etc/jvc2mqtt/ha-token`, mode 600, never in the config) |
| `HA_API_URL` | empty (`http://homeassistant.local:8123`) | Home Assistant base URL for the API, e.g. `http://192.168.1.3:8123` |

## Bridge (to do)

`jvc2mqtt` will:

- keep the RFCOMM link open, answer the hello, ACK every frame, reconnect when the link drops
- map MQTT topics to commands (keys, source select, EQ/loudness/bass settings)
- publish status from the radio's notifications (volume, source, frequency, RDS, track info, EQ)
- set the radio's clock on every connect and periodically (drift, DST)
- use MQTT Discovery so HA creates the entities on its own
