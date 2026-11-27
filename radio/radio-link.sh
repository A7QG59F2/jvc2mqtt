#!/bin/bash
# Keep the JVC radio's Bluetooth audio (A2DP) connected, so the Music Assistant
# player (Squeezelite → BlueALSA) always has somewhere to play. When the radio
# is off or out of range the connect attempt just fails, and we try again later.
# Run by jvc2mqtt-radio-link.service; settings come from the jvc2mqtt config.
set -u
CONF=/etc/jvc2mqtt/jvc2mqtt.conf
# shellcheck source=/dev/null
[ -r "$CONF" ] && . "$CONF"
MAC=${JVC_MAC:-}
INTERVAL=${RADIO_RECONNECT_SECONDS:-15}
[ -n "$MAC" ] || { echo "JVC_MAC is not set in $CONF" >&2; exit 1; }

A2DP_SINK=0000110b-0000-1000-8000-00805f9b34fb

a2dp_up() {   # BlueALSA has a playback PCM for the radio
    bluealsa-aplay -L 2>/dev/null | grep -qi "DEV=$MAC,PROFILE=a2dp"
}

# Prints "hciN ADDRESS" for BT_ADAPTER, which is a name (hci0) or the adapter's own address
# (stays the same when Linux renumbers adapters). Fails if that adapter isn't there.
find_adapter() {
    local want=${BT_ADAPTER:-hci0} sys name addr
    for sys in /sys/class/bluetooth/hci*; do
        name=${sys##*/}
        [[ $name =~ ^hci[0-9]+$ ]] || continue
        addr=$(busctl --system get-property org.bluez "/org/bluez/$name" \
            org.bluez.Adapter1 Address 2>/dev/null | cut -d'"' -f2)
        [ -n "$addr" ] || continue
        if [ "$name" = "$want" ] || [ "${addr^^}" = "${want^^}" ]; then
            echo "$name $addr"
            return 0
        fi
    done
    return 1
}

last=""
while true; do
    if a2dp_up; then
        state=connected
    elif ! adapter=$(find_adapter); then   # looked up every time: the number can change
        state="waiting for Bluetooth adapter ${BT_ADAPTER:-hci0}"
    else
        DEV_PATH=/org/bluez/${adapter%% *}/dev_${MAC//:/_}
        # Connect just the audio profile; works whether or not the control
        # (SPP) link is already up. Fails after a few seconds if the radio is off.
        timeout 20 dbus-send --system --print-reply --dest=org.bluez "$DEV_PATH" \
            org.bluez.Device1.ConnectProfile "string:$A2DP_SINK" >/dev/null 2>&1 || true
        sleep 2
        a2dp_up && state=connected || state=unreachable
    fi
    if [ "$state" != "$last" ]; then
        echo "radio $MAC audio: $state"
        last=$state
    fi
    sleep "$INTERVAL"
done
