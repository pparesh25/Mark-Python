#!/bin/bash
# Install the Mark Screener launchd schedule on macOS: the slots set in
# run_scheduled.sh (Monday-Friday 19:30 India time), plus a catch-up check at login.
#
# The LaunchAgent plist is generated here from this script's own directory, so
# the project can live anywhere (nothing in it names /Users/<someone>); it is
# written straight to ~/Library/LaunchAgents and never into the project.
# launchd fires calendar slots in the machine's local time, so 19:30 India time
# is converted to local time here (on an IST machine it stays 19:30).  Rerun
# this script after moving the machine to another time zone, or when local
# daylight saving time starts or ends; run_scheduled.sh decides in India time
# either way, and the login check catches a slot the calendar missed.
#
# Usage: ./schedule_setup.sh                 install (or reinstall) the schedule
#        ./schedule_setup.sh --force         reinstall even while a run is in progress (stops it)
#        ./schedule_setup.sh --print-plist   print the generated plist only; changes nothing

set -e

LABEL="com.paresh.markscreener"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_FILE="$TARGET_DIR/$LABEL.plist"
LOG_DIR="$SCRIPT_DIR/logs"

PRINT_ONLY=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --print-plist) PRINT_ONLY=1 ;;
        --force) FORCE=1 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

# The schedule itself lives in run_scheduled.sh (one place for both scripts).
read -r SCHEDULE_HOUR SCHEDULE_MINUTE SCHEDULE_TZ SCHEDULE_WEEKDAYS <<< "$(/bin/bash "$SCRIPT_DIR/run_scheduled.sh" --print-schedule)"
SLOT_TEXT=$(printf '%02d:%02d' "$SCHEDULE_HOUR" "$SCHEDULE_MINUTE")

# "WEEKDAY HOUR MINUTE" in the machine's local time for each schedule weekday:
# the slot of the coming week converted from SCHEDULE_TZ.
local_slots() {
    local now back day weekday slot
    now=$(date +%s)
    for back in 0 1 2 3 4 5 6; do
        weekday=$(TZ="$SCHEDULE_TZ" date -j -v+"${back}"d -r "$now" '+%u')
        case " $SCHEDULE_WEEKDAYS " in
            *" $weekday "*) ;;
            *) continue ;;
        esac
        day=$(TZ="$SCHEDULE_TZ" date -j -v+"${back}"d -r "$now" '+%Y-%m-%d')
        slot=$(TZ="$SCHEDULE_TZ" date -j -f "%Y-%m-%d %H:%M:%S" "$day $SLOT_TEXT:00" '+%s')
        date -j -r "$slot" '+%u %H %M'
    done | sort -n
}

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

generate_plist() {
    local dir runner out err weekday hour minute
    dir=$(xml_escape "$SCRIPT_DIR")
    runner=$(xml_escape "$SCRIPT_DIR/run_scheduled.sh")
    out=$(xml_escape "$LOG_DIR/scheduler_stdout.log")
    err=$(xml_escape "$LOG_DIR/scheduler_stderr.log")
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>WorkingDirectory</key>
    <string>$dir</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$runner</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
EOF
    while read -r weekday hour minute; do
        cat <<EOF
        <dict>
            <key>Weekday</key>
            <integer>$weekday</integer>
            <key>Hour</key>
            <integer>$((10#$hour))</integer>
            <key>Minute</key>
            <integer>$((10#$minute))</integer>
        </dict>
EOF
    done <<< "$(local_slots)"
    cat <<EOF
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$out</string>
    <key>StandardErrorPath</key>
    <string>$err</string>
</dict>
</plist>
EOF
}

describe_local_slots() {
    local names=(x Mon Tue Wed Thu Fri Sat Sun) weekday hour minute text=""
    while read -r weekday hour minute; do
        text="$text ${names[$weekday]} $hour:$minute"
    done <<< "$(local_slots)"
    echo "${text# }"
}

if [ "$PRINT_ONLY" = "1" ]; then
    generate_plist
    exit 0
fi

DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"

# Never stop a run in progress unless asked to.
if STATE=$(launchctl print "$SERVICE" 2>/dev/null); then
    LOADED=1
    if printf '%s\n' "$STATE" | grep -Eq '^[[:space:]]*state = running'; then
        PID=$(printf '%s\n' "$STATE" | sed -nE 's/^[[:space:]]*pid = ([0-9]+).*/\1/p' | head -n 1)
        if [ "$FORCE" != "1" ]; then
            echo "Mark Screener is running right now (pid ${PID:-?}). Rerun ./schedule_setup.sh when it has" >&2
            echo "finished, or pass --force to stop it and reinstall." >&2
            exit 1
        fi
        echo "--force: stopping the running job (pid ${PID:-?})."
    fi
else
    LOADED=0
fi

mkdir -p "$TARGET_DIR" "$LOG_DIR"
chmod +x "$SCRIPT_DIR/Mark_Python.sh" "$SCRIPT_DIR/run_scheduled.sh"

TEMP_FILE=$(mktemp "$TARGET_DIR/.$LABEL.XXXXXX")
trap 'rm -f "$TEMP_FILE"' EXIT
generate_plist > "$TEMP_FILE"
plutil -lint "$TEMP_FILE" >/dev/null

if [ "$LOADED" = "1" ]; then
    if ! ERROR=$(launchctl bootout "$SERVICE" 2>&1); then
        echo "launchctl bootout $SERVICE failed: $ERROR; trying launchctl unload." >&2
        launchctl unload "$TARGET_FILE" || true
    fi
fi

mv -f "$TEMP_FILE" "$TARGET_FILE"
chmod 644 "$TARGET_FILE"

loaded=0
for attempt in 1 2 3; do
    if ERROR=$(launchctl bootstrap "$DOMAIN" "$TARGET_FILE" 2>&1); then
        loaded=1
        break
    fi
    echo "launchctl bootstrap $DOMAIN $TARGET_FILE failed (attempt $attempt): $ERROR" >&2
    sleep 1   # a just-removed service can take a moment to leave the domain
done
if [ "$loaded" = "0" ]; then
    echo "Trying the legacy launchctl load." >&2
    launchctl load -w "$TARGET_FILE" || true
fi

if ! launchctl print "$SERVICE" >/dev/null 2>&1; then
    echo "ERROR: the LaunchAgent $LABEL is not loaded after installing $TARGET_FILE." >&2
    echo "Check the messages above, then run: launchctl bootstrap $DOMAIN \"$TARGET_FILE\"" >&2
    exit 1
fi

echo "======================================================="
echo "✅ Mark Screener schedule installed: $SLOT_TEXT $SCHEDULE_TZ on weekdays $SCHEDULE_WEEKDAYS (1 = Monday)."
echo "   In this machine's local time: $(describe_local_slots)"
echo "✅ Mark Screener શેડ્યૂલ સફળતાપૂર્વક સેટ થઈ ગયું છે!"
echo "સોમવારથી શુક્રવાર $SLOT_TEXT (ભારતીય સમય) વાગ્યે આ ટૂલ ઑટોમેટિક રન થશે."
echo "જો તે સમયે મશીન બંધ હશે, તો મશીન શરૂ થતાં જ બાકી રન તરત થઈ જશે."
echo "Plist: $TARGET_FILE"
echo "Logs:  $LOG_DIR/"
echo "======================================================="
