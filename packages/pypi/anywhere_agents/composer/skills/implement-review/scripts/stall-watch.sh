#!/usr/bin/env bash
# stall-watch.sh -- Background stall observer for implement-review Auto-terminal.
# See skills/implement-review/SKILL.md > Script contract invariants and
# Phase 2.0 Health check 9 for the contract.
#
# Spawned in background by dispatch-codex.sh. Polls <state-dir>/tail size+mtime;
# appends one STALL line to <state-dir>/stall-warning when no growth is observed
# for >= STALL_THRESHOLD_SECONDS (default 300 = 5 min). Each subsequent stall
# period (growth followed by another threshold of silence) is logged again.
#
# Args (named):
#   --state-dir <abs-path>   Directory created by dispatch-codex
#   --parent-pid <PID>       PID of dispatch-codex (informational liveness check)
#
# Env (test hooks):
#   STALL_THRESHOLD_SECONDS  Default 300 (5 min)
#   STALL_POLL_INTERVAL_SECONDS  Default 30
#
# Invariant: never kills any process. Only uses `kill -0` for liveness check.
# On any error or when --parent-pid is dead, exits silently (status 0).

set -u

STATE_DIR=""
PARENT_PID=""

while [ $# -gt 0 ]; do
    case "$1" in
        --state-dir) STATE_DIR="$2"; shift 2 ;;
        --parent-pid) PARENT_PID="$2"; shift 2 ;;
        *) exit 0 ;;
    esac
done

[ -z "$STATE_DIR" ] && exit 0
[ -z "$PARENT_PID" ] && exit 0
[ ! -d "$STATE_DIR" ] && exit 0

THRESHOLD="${STALL_THRESHOLD_SECONDS:-300}"
INTERVAL="${STALL_POLL_INTERVAL_SECONDS:-30}"

TAIL_FILE="$STATE_DIR/tail"
TAIL_STDERR_FILE="$STATE_DIR/tail.stderr-tmp"
WARN_FILE="$STATE_DIR/stall-warning"

# Both files contribute to "Codex is alive and emitting" growth. The PowerShell
# dispatch splits stdout (-> tail) from stderr (-> tail.stderr-tmp) during the
# run; without monitoring stderr we would false-positive on a Codex run that
# emits diagnostics only to stderr. Bash dispatch merges streams via `2>&1`
# so tail.stderr-tmp never exists here, making the extra probe a no-op.

last_size=-1
last_mtime=0
last_growth_time=$(date +%s 2>/dev/null || echo 0)
stalled_logged=0

# Best-effort: stop polling if anything below errors.
# When the parent dies we still run one final poll so any stall that crossed
# the threshold right before dispatch exited gets recorded, then exit. This
# closes the Phase 2.0 Health check 9 race noted in SKILL.md > Phase 2.0.
while :; do
    # Parent liveness check (informational only; never kills anything).
    if kill -0 "$PARENT_PID" 2>/dev/null; then
        parent_alive=1
    else
        parent_alive=0
    fi

    # Combined growth across watched paths: sum sizes, max mtime.
    current_size=0
    current_mtime=0
    any_exists=0
    for tf in "$TAIL_FILE" "$TAIL_STDERR_FILE"; do
        if [ ! -f "$tf" ]; then continue; fi
        any_exists=1
        sz=$(wc -c < "$tf" 2>/dev/null | tr -d ' ')
        sz="${sz:-0}"
        case "$sz" in
            ''|*[!0-9]*) sz=0 ;;
        esac
        current_size=$((current_size + sz))
        if mt=$(stat -c %Y "$tf" 2>/dev/null); then :; elif mt=$(stat -f %m "$tf" 2>/dev/null); then :; else mt=0; fi
        case "$mt" in
            ''|*[!0-9]*) mt=0 ;;
        esac
        if [ "$mt" -gt "$current_mtime" ]; then current_mtime="$mt"; fi
    done

    if [ "$any_exists" -eq 1 ]; then
        now=$(date +%s 2>/dev/null || echo 0)

        # Guard against non-numeric comparisons (defensive).
        case "$current_size$last_size$current_mtime$last_mtime$now" in
            *[!0-9-]*) sleep "$INTERVAL"; continue ;;
        esac

        if [ "$current_size" -gt "$last_size" ] || [ "$current_mtime" -gt "$last_mtime" ]; then
            last_size="$current_size"
            last_mtime="$current_mtime"
            last_growth_time="$now"
            stalled_logged=0
        else
            elapsed=$((now - last_growth_time))
            if [ "$elapsed" -ge "$THRESHOLD" ] && [ "$stalled_logged" -eq 0 ]; then
                iso=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "unknown")
                printf 'STALL %s tail-no-growth-for-%ds\n' "$iso" "$elapsed" \
                    >> "$WARN_FILE" 2>/dev/null || exit 0
                stalled_logged=1
            fi
        fi
    fi

    if [ "$parent_alive" -eq 0 ]; then
        exit 0
    fi

    sleep "$INTERVAL"
done
