#!/bin/bash
# Install the Mark Screener launchd schedule on macOS: Monday-Friday at the time
# set in run_scheduled.sh (19:30 IST), plus a catch-up check at login.
#
# The LaunchAgent plist is generated here from this script's own directory, so
# the project can live anywhere (nothing in it names /Users/<someone>); it is
# written straight to ~/Library/LaunchAgents and never into the project.
#
# Usage: ./schedule_setup.sh                install (or reinstall) the schedule
#        ./schedule_setup.sh --print-plist  print the generated plist only; changes nothing

set -e

LABEL="com.paresh.markscreener"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_FILE="$TARGET_DIR/$LABEL.plist"
LOG_DIR="$SCRIPT_DIR/logs"

# The schedule itself lives in run_scheduled.sh (one place for both scripts).
read -r SCHEDULE_HOUR SCHEDULE_MINUTE SCHEDULE_WEEKDAYS <<< "$(/bin/bash "$SCRIPT_DIR/run_scheduled.sh" --print-schedule)"

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

generate_plist() {
    local dir runner out err weekday
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
    for weekday in $SCHEDULE_WEEKDAYS; do
        cat <<EOF
        <dict>
            <key>Weekday</key>
            <integer>$weekday</integer>
            <key>Hour</key>
            <integer>$SCHEDULE_HOUR</integer>
            <key>Minute</key>
            <integer>$SCHEDULE_MINUTE</integer>
        </dict>
EOF
    done
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

if [ "${1:-}" = "--print-plist" ]; then
    generate_plist
    exit 0
fi

mkdir -p "$TARGET_DIR" "$LOG_DIR"
chmod +x "$SCRIPT_DIR/Mark_Python.sh" "$SCRIPT_DIR/run_scheduled.sh"

TEMP_FILE=$(mktemp "$TARGET_DIR/.$LABEL.XXXXXX")
trap 'rm -f "$TEMP_FILE"' EXIT
generate_plist > "$TEMP_FILE"
plutil -lint "$TEMP_FILE" >/dev/null

DOMAIN="gui/$(id -u)"
# Remove a loaded copy first (new launchctl, then the legacy command).
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null \
    || launchctl unload "$TARGET_FILE" 2>/dev/null \
    || true

mv -f "$TEMP_FILE" "$TARGET_FILE"
chmod 644 "$TARGET_FILE"

loaded=0
for attempt in 1 2 3; do
    if launchctl bootstrap "$DOMAIN" "$TARGET_FILE" 2>/dev/null; then
        loaded=1
        break
    fi
    sleep 1   # a just-removed service can take a moment to leave the domain
done
if [ "$loaded" = "0" ]; then
    launchctl load -w "$TARGET_FILE"
fi

printf -v SLOT '%02d:%02d' "$SCHEDULE_HOUR" "$SCHEDULE_MINUTE"
echo "======================================================="
echo "✅ Mark Screener schedule installed: Monday-Friday at $SLOT (machine local time, IST)."
echo "✅ Mark Screener શેડ્યૂલ સફળતાપૂર્વક સેટ થઈ ગયું છે!"
echo "સોમવારથી શુક્રવાર $SLOT વાગ્યે આ ટૂલ ઑટોમેટિક રન થશે."
echo "જો તે સમયે મશીન બંધ હશે, તો મશીન શરૂ થતાં જ બાકી રન તરત થઈ જશે."
echo "Plist: $TARGET_FILE"
echo "Logs:  $LOG_DIR/"
echo "======================================================="
