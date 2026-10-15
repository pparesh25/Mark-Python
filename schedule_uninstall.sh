#!/bin/bash
# Remove the Mark Screener launchd schedule installed by schedule_setup.sh.

LABEL="com.paresh.markscreener"
TARGET_FILE="$HOME/Library/LaunchAgents/$LABEL.plist"
SERVICE="gui/$(id -u)/$LABEL"

if launchctl print "$SERVICE" >/dev/null 2>&1; then
    if ! ERROR=$(launchctl bootout "$SERVICE" 2>&1); then
        echo "launchctl bootout $SERVICE failed: $ERROR; trying launchctl unload." >&2
        [ -f "$TARGET_FILE" ] && launchctl unload "$TARGET_FILE"
    fi
fi

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

if launchctl print "$SERVICE" >/dev/null 2>&1; then
    echo "ERROR: $LABEL is still loaded; run: launchctl bootout $SERVICE" >&2
    exit 1
fi
