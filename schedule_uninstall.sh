#!/bin/bash
# Remove the Mark Screener launchd schedule installed by schedule_setup.sh.

LABEL="com.paresh.markscreener"
TARGET_FILE="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

# Unload whether or not the file is still there (new launchctl, then the legacy command).
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null \
    || { [ -f "$TARGET_FILE" ] && launchctl unload "$TARGET_FILE" 2>/dev/null; } \
    || true

if [ -f "$TARGET_FILE" ]; then
    rm -f "$TARGET_FILE"
    echo "======================================================="
    echo "✅ Mark Screener schedule removed."
    echo "✅ Mark Screener શેડ્યૂલ સફળતાપૂર્વક હટાવી દેવામાં આવ્યું છે."
    echo "======================================================="
else
    echo "No schedule file found ($TARGET_FILE)."
    echo "શેડ્યૂલ ફાઇલ મળી નથી."
fi
