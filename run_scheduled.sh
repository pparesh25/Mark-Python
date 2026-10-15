#!/bin/bash
# Scheduled run of the Mark Screener (macOS launchd), with catch-up for a missed slot.
#
# launchd starts this script Monday-Friday at SCHEDULE_HOUR:SCHEDULE_MINUTE
# (machine local time, IST) and once at login (RunAtLoad).  It runs the pipeline
# only when the last successful run is older than the most recent scheduled
# slot that has already passed -- the latest Monday-Friday 19:30 at or before
# now (Monday morning -> Friday 19:30).  So a weekend boot after Friday's run
# and a second login on the same evening do nothing, while a machine that was
# off at 19:30 catches up at its next start.
#
# Why 19:30 and not 18:00 (2026-10): 18:00 IST is the readiness cutoff, and the
# NSE/BSE files are often published later.  A run at exactly 18:00 then
# screened the previous session from local data while another machine running
# later screened today's -- one session flipped 35 of 293 qualifiers.
#
# Usage: run_scheduled.sh [--force] [--dry-run] [--print-schedule] [run_screener.py args...]
#   --force           run now whatever the last run was
#   --dry-run         print the decision and exit without running
#   --print-schedule  print "HOUR MINUTE WEEKDAY..." (read by schedule_setup.sh)
# Environment (for tests):
#   MARK_SCHEDULER_NOW=<epoch seconds>   pretend it is this time
#   MARK_SCHEDULER_DRY_RUN=1             same as --dry-run
#   MARK_SCHEDULER_LAST_RUN_FILE=<path>  read/write the last-run record here

# --- The one place the schedule is defined --------------------------------
SCHEDULE_HOUR=19
SCHEDULE_MINUTE=30
SCHEDULE_WEEKDAYS="1 2 3 4 5"   # date +%u: 1 = Monday ... 7 = Sunday
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LAST_RUN_FILE="${MARK_SCHEDULER_LAST_RUN_FILE:-$SCRIPT_DIR/logs/.last_scheduled_run}"
STAMP_FORMAT="%Y-%m-%d %H:%M:%S"

FORCE=0
DRY_RUN=0
[ "${MARK_SCHEDULER_DRY_RUN:-0}" = "1" ] && DRY_RUN=1
PASSTHROUGH=()
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --print-schedule)
            echo "$SCHEDULE_HOUR $SCHEDULE_MINUTE $SCHEDULE_WEEKDAYS"
            exit 0
            ;;
        *) PASSTHROUGH+=("$arg") ;;
    esac
done

NOW="${MARK_SCHEDULER_NOW:-$(date +%s)}"

stamp() { date -j -r "$1" "+$STAMP_FORMAT %a"; }
now_stamp() { date "+$STAMP_FORMAT"; }

# The most recent scheduled slot at or before epoch $1 (empty if none in a week).
latest_slot() {
    local now="$1" back day weekday slot
    local slot_time
    slot_time=$(printf '%02d:%02d:00' "$SCHEDULE_HOUR" "$SCHEDULE_MINUTE")
    for back in 0 1 2 3 4 5 6 7; do
        weekday=$(date -j -v-"${back}"d -r "$now" '+%u')
        case " $SCHEDULE_WEEKDAYS " in
            *" $weekday "*) ;;
            *) continue ;;
        esac
        day=$(date -j -v-"${back}"d -r "$now" '+%Y-%m-%d')
        slot=$(date -j -f "$STAMP_FORMAT" "$day $slot_time" '+%s' 2>/dev/null) || continue
        if [ "$slot" -le "$now" ]; then
            echo "$slot"
            return 0
        fi
    done
    return 1
}

decide() {
    if [ "$FORCE" = "1" ]; then
        DECISION=run; REASON="--force given"
        return
    fi
    if [ ! -f "$LAST_RUN_FILE" ]; then
        DECISION=run; REASON="no prior successful run recorded"
        return
    fi
    local last_text last_epoch slot
    last_text=$(head -n 1 "$LAST_RUN_FILE" 2>/dev/null)
    last_epoch=$(date -j -f "$STAMP_FORMAT" "$last_text" '+%s' 2>/dev/null) || last_epoch=0
    slot=$(latest_slot "$NOW") || slot=""
    if [ -z "$slot" ]; then
        DECISION=skip; REASON="no scheduled slot in the last week (SCHEDULE_WEEKDAYS='$SCHEDULE_WEEKDAYS')"
        return
    fi
    if [ "$last_epoch" -lt "$slot" ]; then
        DECISION=run
        REASON="last successful run (${last_text:-unreadable}) is older than the slot $(stamp "$slot")"
    else
        DECISION=skip
        REASON="last successful run ($last_text) already covers the slot $(stamp "$slot")"
    fi
}

do_run() {
    echo "=========================================================="
    echo "▶ [$(now_stamp)] Starting Mark Screener..."
    echo "=========================================================="
    "$SCRIPT_DIR/Mark_Python.sh" "$@"
    local exit_code=$?
    if [ $exit_code -eq 0 ]; then
        mkdir -p "$(dirname -- "$LAST_RUN_FILE")"
        now_stamp > "$LAST_RUN_FILE"
        echo "✓ [$(now_stamp)] Mark Screener successfully completed."
    else
        echo "✗ [$(now_stamp)] Mark Screener failed with exit code $exit_code."
    fi
    return $exit_code
}

decide
if [ "$DRY_RUN" = "1" ]; then
    echo "now $(stamp "$NOW") | decision: $DECISION | $REASON"
    exit 0
fi

cd "$SCRIPT_DIR" || exit 1
if [ "$DECISION" = "run" ]; then
    echo "[$(now_stamp)] Running: $REASON."
    do_run "${PASSTHROUGH[@]}"
    exit $?
fi
echo "[$(now_stamp)] Skipping: $REASON."
exit 0
