#!/bin/bash
# =============================================================================
#  jvc2mqtt  →  JVC KD-SX38MBT car receiver  ↔  Home Assistant
#  Target: any Debian / DietPi / Raspberry Pi OS box with BlueZ and a
#          Bluetooth adapter in range of the radio (BT_ADAPTER, default hci0).
#          The first host is an Odroid C2 with a TP-Link UB500, but nothing
#          here depends on it.
#
#  Usage (as root, from a clone of this repo):
#      sudo ./install.sh              # install / update
#      sudo ./install.sh --configure  # (re)answer the questions, then install
#      sudo ./install.sh --check      # just print a health report
#
#  Installs the JVC tools (jvc-probe, protocol in PROTOCOL.md) and, if switched
#  on, the Music Assistant player that plays through the radio and the jvc2mqtt
#  bridge (the radio's settings as Home Assistant entities). Safe to re-run.
#  Settings: /etc/jvc2mqtt/jvc2mqtt.conf
#
#  The Bluetooth adapter's firmware and power-up belong to the host's own
#  setup (on an Odroid: ENABLE_BLUETOOTH in ODroid-C2-bluetooth-proxy).
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/A7QG59F2/jvc2mqtt"
CONF_DIR=/etc/jvc2mqtt
CONF="$CONF_DIR/jvc2mqtt.conf"
HA_TOKEN_FILE="$CONF_DIR/ha-token"
MQTT_PASSWORD_FILE="$CONF_DIR/mqtt-password"
APP_DIR=/opt/jvc2mqtt
LOG=/var/log/jvc2mqtt-install.log
# Where these parts lived before they moved out of the Odroid repo
OLD_CONF=/etc/odroid-ha/odroid-ha.conf
OLD_TOKEN=/etc/odroid-ha/ha-token
OLD_APP_DIR=/opt/odroid-ha

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_off=$'\e[0m'
say()  { echo "${c_ok}==>${c_off} $*"; }
warn() { echo "${c_warn}[!]${c_off} $*"; }
die()  { echo "${c_err}[x]${c_off} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo $0"

# ─── Settings ─────────────────────────────────────────────────────────────────
load_conf() {
    # shellcheck source=/dev/null
    if [ -r "$CONF" ]; then . "$CONF"; fi
    : "${JVC_MAC:=}" "${JVC_CHANNEL:=16}" "${BT_ADAPTER:=hci0}"
    : "${SQUEEZELITE_BUFFER:=}" "${SQUEEZELITE_ALSA:=}" "${SQUEEZELITE_MAC:=}"
    : "${ENABLE_BRIDGE:=0}" "${MQTT_HOST:=}" "${MQTT_PORT:=1883}" "${MQTT_USER:=}"
    : "${RADIO_ENTITY_PREFIX:=car_radio}" "${VOLUME_LIMIT:=25}"
    : "${ENABLE_RADIO_AUDIO:=1}" "${RADIO_AUDIO_NAME:=Car-Radio}" "${RADIO_AUDIO_SERVER:=}"
}
on() { [ "${!1:-0}" = "1" ]; }
# The adapter's own address, so bluetoothctl looks at BT_ADAPTER and not just the default one
adapter_addr() { cat "/sys/class/bluetooth/$BT_ADAPTER/address" 2>/dev/null || true; }
onoff() { on "$1" && echo on || echo off; }

# ─── Health report ────────────────────────────────────────────────────────────
check() {
    echo "── jvc2mqtt ──"
    echo "  host:        $(hostname)  adapter $BT_ADAPTER $(adapter_addr)"
    echo "  radio:       ${JVC_MAC:-(JVC_MAC not set)}  channel ${JVC_CHANNEL}"
    echo "  radio-audio: $(onoff ENABLE_RADIO_AUDIO)"
    echo "  bridge:      $(onoff ENABLE_BRIDGE)"
    echo "  time zone:   $(readlink -f /etc/localtime 2>/dev/null | sed 's|^.*/zoneinfo/||')  ($(date '+%H:%M'); the radio's clock follows it)"
    echo "── Bluetooth ──"
    if [ -n "$(adapter_addr)" ]; then
        # shellcheck disable=SC2046  # no address = default controller
        bluetoothctl show $(adapter_addr) 2>/dev/null | grep -E 'Controller|Powered|Discovering' | sed 's/^/  /'
    else
        echo "  ${c_warn}no Bluetooth adapter $BT_ADAPTER (set BT_ADAPTER, see hciconfig / ls /sys/class/bluetooth)${c_off}"
    fi
    if [ -n "$JVC_MAC" ]; then
        echo "  radio paired: $(bluetoothctl info "$JVC_MAC" 2>/dev/null | grep -q 'Paired: yes' && echo yes || echo 'no (see README: pair the radio)')"
    fi
    if systemctl is-active --quiet bt-proxy 2>/dev/null; then
        echo "  ${c_warn}bt-proxy is running: its BLE scanning blocks connecting to the radio${c_off}"
    fi
    echo "── Services ──"
    printf "  %-11s %s\n" bluetooth "$(systemctl is-active bluetooth 2>/dev/null || true)"
    printf "  %-11s %s\n" jvc-probe "$([ -x "$APP_DIR/tools/jvc-probe.py" ] && echo installed || echo missing)"
    if on ENABLE_RADIO_AUDIO; then
        printf "  %-11s %s\n" bluealsa "$(systemctl is-active bluealsa 2>/dev/null || true)"
        printf "  %-11s %s\n" radio-link "$(systemctl is-active jvc2mqtt-radio-link 2>/dev/null || true)"
        printf "  %-11s %s\n" "radio A2DP" "$(bluealsa-aplay -L 2>/dev/null | grep -qi "DEV=$JVC_MAC,PROFILE=a2dp" && echo connected || echo 'not connected (radio off or out of range?)')"
        printf "  %-11s %s\n" squeezelite "$(systemctl is-active squeezelite 2>/dev/null || true)"
        printf "  %-11s %s\n" "MA server" "$(ss -tn 2>/dev/null | grep -q ':3483 ' && echo connected || echo 'not connected (add the Squeezelite provider in Music Assistant)')"
        if [ -s "$HA_TOKEN_FILE" ]; then
            printf "  %-11s %s\n" radio-avrcp "$(systemctl is-active jvc2mqtt-radio-avrcp 2>/dev/null || true)"
        else
            printf "  %-11s %s\n" radio-avrcp "off (no Home Assistant token; jvc2mqtt-install --configure)"
        fi
        printf "  %-11s %s\n" "MA player" "$(sq_mac)  (Music Assistant player ID; pin it with SQUEEZELITE_MAC)"
    fi
    if on ENABLE_BRIDGE; then
        printf "  %-11s %s\n" jvc2mqtt "$(systemctl is-active jvc2mqtt 2>/dev/null || true)"
        printf "  %-11s %s\n" MQTT "${MQTT_HOST:-homeassistant.local}:$MQTT_PORT user ${MQTT_USER:-(none)}$([ -s "$MQTT_PASSWORD_FILE" ] || echo ', no password saved')"
        echo "  entities:   number.${RADIO_ENTITY_PREFIX}_{volume,fader,balance,eq_band_01…13}"
        echo "              select.${RADIO_ENTITY_PREFIX}_{eq_preset,loudness,bass_boost,source}"
        echo "              switch.${RADIO_ENTITY_PREFIX}_{bass_extend,remote_link}"
        journalctl -u jvc2mqtt -n 3 --no-pager -o cat 2>/dev/null | sed 's/^/  log: /' || true
    fi
}
# Squeezelite's player MAC = Music Assistant's player ID: SQUEEZELITE_MAC, or
# else the first network interface's address (what Squeezelite picks itself)
sq_mac() {
    if [ -n "$SQUEEZELITE_MAC" ]; then echo "$SQUEEZELITE_MAC"; return; fi
    local i
    for i in /sys/class/net/*; do
        [ "${i##*/}" = lo ] && continue
        cat "$i/address" 2>/dev/null && return
    done
    echo "?"
}
if [ "${1:-}" = "--check" ]; then
    load_conf; check; exit 0
fi

# ─── If run standalone, fetch the repo ────────────────────────────────────────
if [ ! -f "$SRC_DIR/tools/jvc-probe.py" ]; then
    say "Repo files not next to the script, cloning $REPO_URL"
    command -v git >/dev/null || { apt-get update; apt-get install -y git; }
    rm -rf "$APP_DIR/src"
    git clone --depth 1 "$REPO_URL" "$APP_DIR/src"
    exec bash "$APP_DIR/src/install.sh" "$@"
fi

# ─── Config helpers ───────────────────────────────────────────────────────────
# KEY="value" into a config file, replacing the existing line or appending one.
set_conf_in() {
    local file=$1 key=$2 val=$3
    val=${val//\\/\\\\}; val=${val//\"/\\\"}; val=${val//\$/\\\$}; val=${val//\`/\\\`}
    if grep -q "^$key=" "$file"; then
        K="$key" V="$val" awk 'BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"] }
            index($0, k "=") == 1 { print k "=\"" v "\""; next } { print }' "$file" >"$file.tmp"
        cat "$file.tmp" >"$file"; rm -f "$file.tmp"
    else
        printf '%s="%s"\n' "$key" "$val" >>"$file"
    fi
}
set_conf() { set_conf_in "$CONF" "$1" "$2"; }
ask_yn() {   # $1: variable  $2: question   (Enter keeps the current value)
    local var=$1 cur=${!1:-0} ans hint
    [ "$cur" = 1 ] && hint="Y/n" || hint="y/N"
    while true; do
        read -rp "$2 [$hint] " ans
        case "${ans,,}" in
            "")     break ;;
            y|yes)  cur=1; break ;;
            n|no)   cur=0; break ;;
        esac
    done
    printf -v "$var" '%s' "$cur"; set_conf "$var" "$cur"
}
ask_str() {  # $1: variable  $2: question
    local var=$1 ans
    read -rp "$2 [${!1:-}] " ans
    [ -n "$ans" ] && printf -v "$var" '%s' "$ans"
    set_conf "$var" "${!var:-}"
}
# Home Assistant long-lived token for the radio's track display and buttons.
# Kept only in its own root-only file, never in the config.
read_ha_token() {
    local change=y tok
    echo "  The radio can show Music Assistant's track and use its buttons with a Home"
    echo "  Assistant long-lived access token (HA → your profile → Security → Long-lived"
    echo "  access tokens). Empty = skip; the radio then just shows STOP."
    if [ -s "$HA_TOKEN_FILE" ]; then
        read -rp "  A token is already saved. Change it? [y/N] " change
    fi
    case "${change,,}" in y|yes) ;; *) return 0 ;; esac
    read -rsp "  Token: " tok; echo
    [ -n "$tok" ] || return 0
    (umask 077; printf '%s\n' "$tok" >"$HA_TOKEN_FILE")
    chmod 600 "$HA_TOKEN_FILE"
}
# MQTT password for the bridge, kept only in its own root-only file.
read_mqtt_password() {
    local change=y pw
    if [ -s "$MQTT_PASSWORD_FILE" ]; then
        read -rp "  An MQTT password is already saved. Change it? [y/N] " change
    fi
    case "${change,,}" in y|yes) ;; *) return 0 ;; esac
    read -rsp "  MQTT password: " pw; echo
    [ -n "$pw" ] || return 0
    (umask 077; printf '%s\n' "$pw" >"$MQTT_PASSWORD_FILE")
    chmod 600 "$MQTT_PASSWORD_FILE"
}
configure() {
    [ -t 0 ] || die "--configure needs a terminal (use: ssh -t root@<host> jvc2mqtt-install --configure)"
    echo
    echo "JVC radio setup. Press Enter to keep the value in [brackets]."
    echo
    while true; do
        ask_str JVC_MAC "Radio's Bluetooth address (bluetoothctl devices)"
        [[ $JVC_MAC =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] && break
        warn "That isn't a Bluetooth address like 88:E0:34:B7:98:6C."
    done
    ask_yn ENABLE_RADIO_AUDIO "Play Music Assistant through the radio over Bluetooth?"
    if on ENABLE_RADIO_AUDIO; then
        ask_str RADIO_AUDIO_NAME "  Player name in Music Assistant (no spaces)"
        RADIO_AUDIO_NAME=$(echo "$RADIO_AUDIO_NAME" | tr ' ' '-' | tr -cd 'A-Za-z0-9._-')
        RADIO_AUDIO_NAME=${RADIO_AUDIO_NAME:-Car-Radio}
        set_conf RADIO_AUDIO_NAME "$RADIO_AUDIO_NAME"
        ask_str HA_API_URL "  Home Assistant address (empty = http://homeassistant.local:8123)"
        read_ha_token
    fi
    ask_yn ENABLE_BRIDGE "Run the jvc2mqtt bridge (radio settings as Home Assistant entities over MQTT)?"
    if on ENABLE_BRIDGE; then
        echo "  The bridge holds the radio's remote link, so the JVC Remote phone app can't connect"
        echo "  while it does. Turn switch.${RADIO_ENTITY_PREFIX}_remote_link off in HA to let the app in."
        ask_str MQTT_HOST "  MQTT broker (empty = homeassistant.local)"
        ask_str MQTT_PORT "  MQTT port"
        ask_str MQTT_USER "  MQTT user (a Home Assistant user works with the Mosquitto app)"
        read_mqtt_password
    fi
    echo
    say "Saved to $CONF. Change it any time with:  jvc2mqtt-install --configure"
    echo
}

# ─── Config (and a one-time move from the Odroid repo's config) ──────────────
MOVED_KEYS="JVC_MAC JVC_CHANNEL ENABLE_RADIO_AUDIO RADIO_AUDIO_NAME RADIO_AUDIO_SERVER RADIO_RECONNECT_SECONDS RADIO_HA_ENTITY HA_API_URL"
mkdir -p "$CONF_DIR"
NEW_CONF=0
if [ ! -f "$CONF" ]; then
    cp "$SRC_DIR/config/jvc2mqtt.conf.example" "$CONF"
    NEW_CONF=1
    if [ -r "$OLD_CONF" ] && grep -q '^JVC_MAC=' "$OLD_CONF"; then
        say "Moving the JVC settings over from $OLD_CONF"
        old_val() {
            (
                set +u
                # shellcheck source=/dev/null
                . "$OLD_CONF"
                printf '%s' "${!1:-}"
            )
        }
        for k in $MOVED_KEYS; do
            grep -q "^$k=" "$OLD_CONF" && set_conf "$k" "$(old_val "$k")"
        done
        # The Odroid repo had radio audio default off; keep the board's choice
        grep -q '^ENABLE_RADIO_AUDIO=' "$OLD_CONF" || set_conf ENABLE_RADIO_AUDIO 0
        # HA_API_URL used to fall back to the kiosk's HA_URL
        if [ -z "$(old_val HA_API_URL)" ] && [ -n "$(old_val HA_URL)" ]; then
            set_conf HA_API_URL "$(old_val HA_URL | sed -E 's|^([a-z]+://[^/]+).*|\1|')"
        fi
        if [ -s "$OLD_TOKEN" ] && [ ! -s "$HA_TOKEN_FILE" ]; then
            (umask 077; cp "$OLD_TOKEN" "$HA_TOKEN_FILE"); chmod 600 "$HA_TOKEN_FILE"
            rm -f "$OLD_TOKEN"
        fi
        NEW_CONF=0
    fi
fi
# The old keys are ours now: take them out of the Odroid config so there's one place to edit
if [ -w "$OLD_CONF" ] && grep -qE "^(ENABLE_JVC_TOOLS|${MOVED_KEYS// /|})=" "$OLD_CONF"; then
    sed -i -E "/^(ENABLE_JVC_TOOLS|${MOVED_KEYS// /|})=/d" "$OLD_CONF"
    say "Removed the JVC settings from $OLD_CONF (they live in $CONF now)"
fi
# Settings added to the repo after this was set up: append them with their defaults
ADDED=""
while IFS= read -r line; do
    key=${line%%=*}
    if ! grep -q "^$key=" "$CONF"; then
        [ -z "$ADDED" ] && printf '\n# Added by install.sh (new settings, defaults from the repo)\n' >>"$CONF"
        echo "$line" >>"$CONF"; ADDED="$ADDED $key"
    fi
done < <(grep -E '^[A-Z_][A-Z0-9_]*=' "$SRC_DIR/config/jvc2mqtt.conf.example")
load_conf
[ -n "$ADDED" ] && say "New settings added to $CONF:$ADDED"
if [ "${1:-}" = "--configure" ] || { [ "$NEW_CONF" = 1 ] && [ -t 0 ]; }; then
    configure
    load_conf
elif [ "$NEW_CONF" = 1 ]; then
    warn "Created $CONF with defaults. Run  jvc2mqtt-install --configure  to set the radio's address."
fi
if on ENABLE_RADIO_AUDIO && [ -z "$JVC_MAC" ]; then
    warn "ENABLE_RADIO_AUDIO is on but JVC_MAC is empty; radio audio is skipped. Set it with  jvc2mqtt-install --configure"
    ENABLE_RADIO_AUDIO=0
fi

exec > >(tee -a "$LOG") 2>&1
say "Install started $(date)  (log: $LOG)"

export DEBIAN_FRONTEND=noninteractive
APT="apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"

# ─── 1. Packages ──────────────────────────────────────────────────────────────
say "Installing packages"
apt-get update
$APT install bluez rfkill python3 git ca-certificates
on ENABLE_RADIO_AUDIO && $APT install bluez-alsa-utils libasound2-plugin-bluez alsa-utils squeezelite python3-dbus python3-gi
on ENABLE_BRIDGE && $APT install python3-paho-mqtt

# ─── 2. Bluetooth on ──────────────────────────────────────────────────────────
rfkill unblock bluetooth 2>/dev/null || true
systemctl enable --now bluetooth
[ -n "$(adapter_addr)" ] || warn "No Bluetooth adapter $BT_ADAPTER found. Check the dongle and its firmware (on an Odroid: ENABLE_BLUETOOTH), or set BT_ADAPTER."
if systemctl is-active --quiet bt-proxy 2>/dev/null; then
    warn "bt-proxy is running: its BLE scanning stops the dongle from connecting to the radio. Turn the proxy off on this board."
fi

# ─── 3. Moved from the Odroid repo: remove the old copies ────────────────────
for u in odroid-ha-radio-avrcp odroid-ha-radio-link; do
    if [ -f "/etc/systemd/system/$u.service" ]; then
        say "Removing the old $u.service (now jvc2mqtt-${u#odroid-ha-})"
        systemctl disable --now "$u" 2>/dev/null || true
        rm -f "/etc/systemd/system/$u.service"
        systemctl daemon-reload
    fi
done
rm -rf "$OLD_APP_DIR/jvc" "$OLD_APP_DIR/radio"

# ─── 4. JVC tools ─────────────────────────────────────────────────────────────
say "Installing the JVC tools"
install -d "$APP_DIR/tools"
install -m 755 "$SRC_DIR/tools/jvc-probe.py" "$APP_DIR/tools/jvc-probe.py"
install -m 755 "$SRC_DIR/tools/decode-logcat.py" "$APP_DIR/tools/decode-logcat.py"
install -m 755 "$SRC_DIR/tools/jvc-tests.sh" "$APP_DIR/tools/jvc-tests.sh"
install -m 644 "$SRC_DIR/PROTOCOL.md" "$APP_DIR/PROTOCOL.md"
ln -sf "$APP_DIR/tools/jvc-probe.py" /usr/local/sbin/jvc-probe
ln -sf "$APP_DIR/tools/jvc-tests.sh" /usr/local/sbin/jvc-tests

# ─── 5. Radio audio: Music Assistant player through the radio ────────────────
# Squeezelite (found by Music Assistant's Squeezelite provider) plays into
# BlueALSA, which streams A2DP to the radio. radio-link.sh reconnects the
# radio's audio whenever it comes back (car started).
SQ_DEFAULTS=/etc/default/squeezelite
RADIO_UNIT=/etc/systemd/system/jvc2mqtt-radio-link.service
AVRCP_UNIT=/etc/systemd/system/jvc2mqtt-radio-avrcp.service
rm_avrcp_unit() {
    [ -f "$AVRCP_UNIT" ] || return 0
    systemctl disable --now jvc2mqtt-radio-avrcp 2>/dev/null || true
    rm -f "$AVRCP_UNIT"
    systemctl daemon-reload
}
if on ENABLE_RADIO_AUDIO; then
    say "Setting up radio audio (player \"$RADIO_AUDIO_NAME\")"
    install -d "$APP_DIR/radio"
    install -m 755 "$SRC_DIR/radio/radio-link.sh" "$APP_DIR/radio/radio-link.sh"
    install -m 755 "$SRC_DIR/radio/avrcp-player.py" "$APP_DIR/radio/avrcp-player.py"
    cat >"$RADIO_UNIT" <<EOF
# Generated by jvc2mqtt install.sh
[Unit]
Description=Keep the JVC radio's Bluetooth audio connected
After=bluetooth.service bluealsa.service
Wants=bluealsa.service

[Service]
ExecStart=$APP_DIR/radio/radio-link.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    # Keep the distro's original once, so switching the part off can put it back
    # (a board moved from the Odroid repo already has it)
    [ -f "$SQ_DEFAULTS" ] && [ ! -f "$SQ_DEFAULTS.dietpi-orig" ] && cp "$SQ_DEFAULTS" "$SQ_DEFAULTS.dietpi-orig"
    # -C 5: let go of the radio 5 s after playback stops. -r/-R: the A2DP link
    # runs at 48 kHz, so resample (only) tracks that aren't 48 kHz.
    # -b / -a: stream/output and ALSA (BlueALSA) buffers, from the config, for
    # skips at the start of playback (underruns while the A2DP stream starts).
    SQ_NAME=$(echo "$RADIO_AUDIO_NAME" | tr ' ' '-' | tr -cd 'A-Za-z0-9._-')
    SQ_BUF=$(echo "$SQUEEZELITE_BUFFER" | tr -cd '0-9:')
    SQ_ALSA=$(echo "$SQUEEZELITE_ALSA" | tr -cd '0-9:')
    SQ_MAC=$(echo "$SQUEEZELITE_MAC" | tr -cd '0-9A-Fa-f:')
    cat >"$SQ_DEFAULTS" <<EOF
# Generated by jvc2mqtt install.sh: Music Assistant → Squeezelite → BlueALSA → JVC radio
ARGS="-W -C 5 -n ${SQ_NAME:-Car-Radio} -o bluealsa:DEV=$JVC_MAC,PROFILE=a2dp -r 48000 -R mE${SQ_BUF:+ -b $SQ_BUF}${SQ_ALSA:+ -a $SQ_ALSA}${SQ_MAC:+ -m $SQ_MAC}${RADIO_AUDIO_SERVER:+ -s $RADIO_AUDIO_SERVER}"
EOF
    # The radio's track display and buttons (AVRCP), mirrored from Home
    # Assistant's media_player for the Music Assistant player. Needs a token.
    if [ -s "$HA_TOKEN_FILE" ]; then
        chmod 600 "$HA_TOKEN_FILE"
        cat >"$AVRCP_UNIT" <<EOF
# Generated by jvc2mqtt install.sh
[Unit]
Description=Show Music Assistant's track on the JVC radio and take its buttons (AVRCP)
After=bluetooth.service network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $APP_DIR/radio/avrcp-player.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
    else
        warn "No Home Assistant token in $HA_TOKEN_FILE: the radio will show STOP and its buttons won't control Music Assistant."
        rm_avrcp_unit
    fi
    systemctl daemon-reload
    systemctl enable --now bluealsa
    systemctl enable jvc2mqtt-radio-link squeezelite
    systemctl restart jvc2mqtt-radio-link squeezelite
    if [ -f "$AVRCP_UNIT" ]; then
        systemctl enable jvc2mqtt-radio-avrcp
        systemctl restart jvc2mqtt-radio-avrcp
    fi
else
    rm_avrcp_unit
    if [ -f "$RADIO_UNIT" ]; then
        systemctl disable --now jvc2mqtt-radio-link 2>/dev/null || true
        rm -f "$RADIO_UNIT"
        systemctl daemon-reload
        systemctl disable --now squeezelite bluealsa 2>/dev/null || true
        [ -f "$SQ_DEFAULTS.dietpi-orig" ] && mv -f "$SQ_DEFAULTS.dietpi-orig" "$SQ_DEFAULTS"
    fi
    rm -rf "$APP_DIR/radio"
fi

# ─── 6. The jvc2mqtt bridge (radio settings ↔ Home Assistant over MQTT) ──────
BRIDGE_UNIT=/etc/systemd/system/jvc2mqtt.service
if on ENABLE_BRIDGE; then
    say "Setting up the jvc2mqtt bridge"
    install -d "$APP_DIR/bridge" /var/lib/jvc2mqtt
    install -m 644 "$SRC_DIR/bridge/jvcproto.py" "$APP_DIR/bridge/jvcproto.py"
    install -m 755 "$SRC_DIR/bridge/jvc2mqtt.py" "$APP_DIR/bridge/jvc2mqtt.py"
    if [ -s "$MQTT_PASSWORD_FILE" ]; then
        chmod 600 "$MQTT_PASSWORD_FILE"
    elif [ -n "$MQTT_USER" ]; then
        warn "MQTT_USER is set but there's no password in $MQTT_PASSWORD_FILE (jvc2mqtt-install --configure)"
    fi
    cat >"$BRIDGE_UNIT" <<EOF
# Generated by jvc2mqtt install.sh
[Unit]
Description=JVC radio <-> Home Assistant over MQTT (jvc2mqtt)
After=bluetooth.service network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $APP_DIR/bridge/jvc2mqtt.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable jvc2mqtt
    systemctl restart jvc2mqtt
else
    if [ -f "$BRIDGE_UNIT" ]; then
        systemctl disable --now jvc2mqtt 2>/dev/null || true
        rm -f "$BRIDGE_UNIT"
        systemctl daemon-reload
    fi
    rm -rf "$APP_DIR/bridge"
fi

# ─── 7. Convenience commands ──────────────────────────────────────────────────
install -m 755 "$SRC_DIR/install.sh" /usr/local/sbin/jvc2mqtt-install
cat > /usr/local/bin/jvc2mqtt-check <<'EOF'
#!/bin/sh
exec sudo /usr/local/sbin/jvc2mqtt-install --check
EOF
chmod 755 /usr/local/bin/jvc2mqtt-check

say "Done."
check
echo
echo "Next steps"
if [ -n "$JVC_MAC" ] && ! bluetoothctl info "$JVC_MAC" 2>/dev/null | grep -q 'Paired: yes'; then
    echo "  - Pair the radio: README.md \"1. Pair the computer with the radio\"."
fi
if on ENABLE_RADIO_AUDIO; then
    echo "  - Music Assistant → Settings → Providers: add \"Squeezelite (slimproto)\"; the player"
    echo "    \"$RADIO_AUDIO_NAME\" appears once the radio is on and connected."
fi
cat <<EOF
  - Talk to the radio:      jvc-probe talk --do "11 05 08"   (Vol+; stop the bridge first)
  - Protocol tests:         jvc-tests balance|sub|colors|key NN|btkeys
  - Change settings:        jvc2mqtt-install --configure
  - Health report:          jvc2mqtt-check
  - Pull latest + re-apply: jvc2mqtt-install
EOF
