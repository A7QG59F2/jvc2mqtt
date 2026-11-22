#!/bin/bash
# jvc-tests.sh – live tests for the open points in PROTOCOL.md.
# Run on the host that talks to the radio, with the radio on, the phone's
# Bluetooth off, the jvc2mqtt bridge stopped, and the volume low.
#
#   sudo jvc-tests balance     # fader/balance range (sets, reads back, puts center back)
#   sudo jvc-tests sub         # subwoofer level scale (puts 0a back; see below)
#   sudo jvc-tests colors      # other 3d 14 parameters (read only)
#   sudo jvc-tests key 09      # one untried key code; watch the radio while it runs
#   sudo jvc-tests btkeys      # Up/Down/Play on BT audio (switches the source to BT)
#
# Every run is appended to /var/log/jvc-tests.log.
set -euo pipefail

LOG=/var/log/jvc-tests.log
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo $0 $*" >&2; exit 1; }
command -v jvc-probe >/dev/null || { echo "jvc-probe missing: run jvc2mqtt-install" >&2; exit 1; }
if systemctl is-active --quiet jvc2mqtt 2>/dev/null; then
    echo "The jvc2mqtt bridge holds the radio's remote link. Stop it first:  systemctl stop jvc2mqtt" >&2
    exit 1
fi

talk() {   # jvc-probe talk with the given --do steps, quiet RDS, logged
    local args=()
    for step in "$@"; do args+=(--do "$step"); done
    { echo "=== $(date '+%F %T') $TEST"; jvc-probe talk --no-rds --wait 1 --listen 2 "${args[@]}"; } 2>&1 | tee -a "$LOG"
}

TEST=${1:-}
case "$TEST" in
    balance)
        # 3d 0c [00 fader 01 balance]; 0f = center, front/right are lower numbers
        talk "3d 0b" \
             "3d 0c 00 0f 01 00" "3d 0b" \
             "3d 0c 00 0f 01 1e" "3d 0b" \
             "3d 0c 00 0f 01 1f" "3d 0b" \
             "3d 0c 00 00 01 0f" "3d 0b" \
             "3d 0c 00 1e 01 0f" "3d 0b" \
             "3d 0c 00 0f 01 0f" "3d 0b"
        ;;
    sub)
        # 3d 43 get, 3d 44 set; PROTOCOL.md saw 0a with the app showing 10.
        # Ends by setting 0a again: change it on the radio afterwards if it was different.
        talk "3d 4e" "3d 42" "3d 43" \
             "3d 44 00" "3d 43" \
             "3d 44 05" "3d 43" \
             "3d 44 0f" "3d 43" \
             "3d 44 14" "3d 43" \
             "3d 44 1e" "3d 43" \
             "3d 44 0a" "3d 43"
        ;;
    colors)
        talk "3d 10" "3d 14 02 00 00" "3d 14 02 00 01" "3d 14 02 01 00" "3d 14 02 01 01" \
             "3d 14 01 00 00" "3d 14 03 00 00" "3d 12 02" "3d 12 01" "3d 12 03"
        ;;
    key)
        code=${2:-}
        [[ $code =~ ^[0-9a-fA-F]{2}$ ]] || { echo "Usage: $0 key <hex code, e.g. 09>" >&2; exit 1; }
        echo "Sending key $code. Watch the radio's display; note what it does."
        talk "sleep 1" "11 05 $code" "sleep 3"
        ;;
    btkeys)
        echo "Switches to BT audio, then sends Up, Down and Play/Pause 4 s apart."
        talk "sleep 4" "11 01 06 14" "sleep 4" "11 05 05" "sleep 4" "11 05 06" "sleep 4" \
             "11 05 01" "sleep 4" "11 05 01" "sleep 4"
        ;;
    *)
        sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
