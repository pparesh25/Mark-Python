#!/bin/bash
# Scheduled run of the Mark Screener (macOS launchd), with catch-up and retries.
#
# launchd starts this script at the scheduled slots and once at login
# (RunAtLoad).  A slot is SCHEDULE_HOUR:SCHEDULE_MINUTE on SCHEDULE_WEEKDAYS in
# India time (SCHEDULE_TZ), whatever the machine's own time zone.  The script
# runs the pipeline only when the last successful run started before the most
# recent slot that has already passed -- Monday morning -> Friday 19:30 -- so a
# weekend boot after Friday's run and a second login on the same evening do
# nothing, while a machine that was off at 19:30 catches up at its next start
# (on a Saturday too, for a missed Friday).
#
# run_screener.py runs with MARK_SCHEDULED=1 and then exits 3 when it screened
# data that is not current (a sync stopped, data older than the target, NSE/BSE
# as-of dates differ) and 4 when another run holds the run lock.  Neither counts
# as done: the run is retried every RETRY_INTERVAL_MINUTES until RETRY_UNTIL
# (India time, the slot's day); after that the slot is given up and the next
# slot runs as usual.  Any other failure (exit 1: typically no internet yet when
# the Mac wakes at the slot, 2026-10-22) is retried the same way, because a
# failed run leaves no success record and one missed evening would otherwise
# wait for the next login or slot.
#
# Why 19:30 and not 18:00 (2026-10): 18:00 IST is the readiness cutoff, and the
# NSE/BSE files are often published later.  A run at exactly 18:00 then
# screened the previous session from local data while another machine running
# later screened today's -- one session flipped 35 of 293 qualifiers.
#
# Usage: run_scheduled.sh [--force] [--dry-run] [--print-schedule] [run_screener.py args...]
#   --force           run now whatever the last run was
#   --dry-run         print the decision and exit without running
#   --print-schedule  print "HOUR MINUTE TZ WEEKDAY..." (read by schedule_setup.sh)
# Environment (for tests):
#   MARK_SCHEDULER_NOW=<epoch seconds>   pretend it is this time; retries then
#                                        advance this clock instead of sleeping
#   MARK_SCHEDULER_DRY_RUN=1             same as --dry-run
#   MARK_SCHEDULER_LAST_RUN_FILE=<path>  the last-success record (a .gave_up file sits beside it)

# --- The one place the schedule is defined --------------------------------
SCHEDULE_HOUR=19
SCHEDULE_MINUTE=30
SCHEDULE_WEEKDAYS="1 2 3 4 5"   # date +%u: 1 = Monday ... 7 = Sunday
SCHEDULE_TZ="Asia/Kolkata"      # the slot and all decisions are in this zone
RETRY_INTERVAL_MINUTES=30
RETRY_UNTIL="23:30"             # last retry start, SCHEDULE_TZ, on the slot's day
# ---------------------------------------------------------------------------
# Exit codes of run_screener.py that mean "try again later" (see run_screener.py).
EXIT_DATA_NOT_CURRENT=3
EXIT_BUSY=4

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LAST_RUN_FILE="${MARK_SCHEDULER_LAST_RUN_FILE:-$SCRIPT_DIR/logs/.last_scheduled_run}"
GAVE_UP_FILE="$LAST_RUN_FILE.gave_up"
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
            echo "$SCHEDULE_HOUR $SCHEDULE_MINUTE $SCHEDULE_TZ $SCHEDULE_WEEKDAYS"
            exit 0
            ;;
        *) PASSTHROUGH+=("$arg") ;;
    esac
done

SIMULATED_CLOCK=0
if [ -n "${MARK_SCHEDULER_NOW:-}" ]; then
    SIMULATED_CLOCK=1
    NOW="$MARK_SCHEDULER_NOW"
else
    NOW=$(date +%s)
fi

# Date arithmetic in the schedule's zone, never the machine's.
sdate() { TZ="$SCHEDULE_TZ" date "$@"; }
stamp() { sdate -j -r "$1" "+$STAMP_FORMAT %Z %a"; }
clock() { if [ "$SIMULATED_CLOCK" = "1" ]; then echo "$NOW"; else date +%s; fi; }

# Epoch of HH:MM (SCHEDULE_TZ) on the schedule-zone day of epoch $1.
at_time_on_day_of() {
    local day
    day=$(sdate -j -r "$1" '+%Y-%m-%d')
    sdate -j -f "$STAMP_FORMAT" "$day $2:00" '+%s' 2>/dev/null
}

# The most recent scheduled slot at or before epoch $1 (empty if none in a week).
latest_slot() {
    local now="$1" back day weekday slot slot_time
    slot_time=$(printf '%02d:%02d:00' "$SCHEDULE_HOUR" "$SCHEDULE_MINUTE")
    for back in 0 1 2 3 4 5 6 7; do
        weekday=$(sdate -j -v-"${back}"d -r "$now" '+%u')
        case " $SCHEDULE_WEEKDAYS " in
            *" $weekday "*) ;;
            *) continue ;;
        esac
        day=$(sdate -j -v-"${back}"d -r "$now" '+%Y-%m-%d')
        slot=$(sdate -j -f "$STAMP_FORMAT" "$day $slot_time" '+%s' 2>/dev/null) || continue
        if [ "$slot" -le "$now" ]; then
            echo "$slot"
            return 0
        fi
    done
    return 1
}

# Start time (epoch) of the last successful run; 0 when unknown.  The record is
# "<epoch> <readable time>"; a record from the older version holds only the
# machine's local time and is read that way.
last_success_epoch() {
    local first
    [ -f "$LAST_RUN_FILE" ] || { echo 0; return; }
    first=$(head -n 1 "$LAST_RUN_FILE" 2>/dev/null)
    case "${first%% *}" in
        ''|*[!0-9]*) date -j -f "$STAMP_FORMAT" "$first" '+%s' 2>/dev/null || echo 0 ;;
        *) echo "${first%% *}" ;;
    esac
}

decide() {
    SLOT=$(latest_slot "$NOW") || SLOT=""
    if [ "$FORCE" = "1" ]; then
        DECISION=run; REASON="--force given"
        return
    fi
    if [ -z "$SLOT" ]; then
        DECISION=skip; REASON="no scheduled slot in the last week (SCHEDULE_WEEKDAYS='$SCHEDULE_WEEKDAYS')"
        return
    fi
    local last
    last=$(last_success_epoch)
    if [ "$last" -ge "$SLOT" ]; then
        DECISION=skip
        REASON="the last successful run ($(stamp "$last")) already covers the slot $(stamp "$SLOT")"
    elif [ "$(head -n 1 "$GAVE_UP_FILE" 2>/dev/null)" = "$SLOT" ]; then
        DECISION=skip
        REASON="retries for the slot $(stamp "$SLOT") already ran out; waiting for the next slot"
    elif [ ! -f "$LAST_RUN_FILE" ]; then
        DECISION=run; REASON="no prior successful run recorded"
    else
        DECISION=run
        REASON="the last successful run ($( [ "$last" -gt 0 ] && stamp "$last" || echo unreadable)) is older than the slot $(stamp "$SLOT")"
    fi
}

wait_until() {
    if [ "$SIMULATED_CLOCK" = "1" ]; then
        NOW="$1"
        return
    fi
    local delay=$(( $1 - $(date +%s) ))
    [ "$delay" -gt 0 ] && sleep "$delay"
}

# Run until success or the end of the retry window.
run_with_retries() {
    local deadline start exit_code next
    deadline=""
    [ -n "$SLOT" ] && deadline=$(at_time_on_day_of "$SLOT" "$RETRY_UNTIL")
    while true; do
        start=$(clock)
        echo "=========================================================="
        echo "▶ [$(stamp "$start")] Starting Mark Screener..."
        echo "=========================================================="
        MARK_SCHEDULED=1 "$SCRIPT_DIR/Mark_Python.sh" "$@"
        exit_code=$?
        if [ "$exit_code" -eq 0 ]; then
            mkdir -p "$(dirname -- "$LAST_RUN_FILE")"
            # The start time: the data screened is what was there when the run began.
            echo "$start $(stamp "$start")" > "$LAST_RUN_FILE"
            rm -f "$GAVE_UP_FILE"
            echo "✓ [$(stamp "$(clock)")] Mark Screener successfully completed."
            return 0
        fi
        if [ "$exit_code" -eq "$EXIT_BUSY" ]; then
            what="another run is in progress"
        elif [ "$exit_code" -eq "$EXIT_DATA_NOT_CURRENT" ]; then
            what="the data screened is not current yet"
        else
            what="the run failed"
            echo "✗ [$(stamp "$(clock)")] Mark Screener failed with exit code $exit_code."
        fi
        next=$(( start + RETRY_INTERVAL_MINUTES * 60 ))
        if [ -n "$deadline" ] && [ "$next" -le "$deadline" ]; then
            echo "… [$(stamp "$(clock)")] Not done: $what (exit $exit_code). Retrying at $(stamp "$next")."
            wait_until "$next"
            continue
        fi
        if [ -n "$SLOT" ]; then
            mkdir -p "$(dirname -- "$GAVE_UP_FILE")"
            echo "$SLOT" > "$GAVE_UP_FILE"
        fi
        echo "✗ [$(stamp "$(clock)")] Not done: $what (exit $exit_code). No retries left for the slot" \
             "$( [ -n "$SLOT" ] && stamp "$SLOT" ) (until $RETRY_UNTIL $SCHEDULE_TZ); giving up until the next slot."
        return "$exit_code"
    done
}

decide
if [ "$DRY_RUN" = "1" ]; then
    echo "now $(stamp "$NOW") | decision: $DECISION | $REASON"
    exit 0
fi

cd "$SCRIPT_DIR" || exit 1
if [ "$DECISION" = "run" ]; then
    echo "[$(stamp "$NOW")] Running: $REASON."
    run_with_retries "${PASSTHROUGH[@]}"
    exit $?
fi
echo "[$(stamp "$NOW")] Skipping: $REASON."
exit 0
