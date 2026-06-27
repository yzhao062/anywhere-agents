#!/usr/bin/env bash
# monitor.sh -- prun active stall/fail/done monitor for dispatched units (Bash variant).
#
# The prun coordinator is turn-based: it only acts on a user message, a task-completion
# notification, or a scheduled wakeup. A background unit that STALLS never completes, so it
# never wakes the coordinator. This monitor turns a stall into a wake event: it runs in the
# background and COMPLETES (printing a per-unit digest) on the first actionable event:
#   - all units done (every unit has a real, non-FALLBACK result), or
#   - any unit stalled (its tail showed no growth for >= PRUN_STALL_THRESHOLD), or
#   - any unit failed (a FALLBACK result, or a dead dispatch process with no result).
# Reuses the tail size+mtime liveness logic from implement-review's stall-watch.
#
# Args (positional): one or more <state-dir> paths emitted by dispatch-task (the
#   `STATE-DIR <abs-path>` line). Each provides tail (growth), result-file (done/fail),
#   and dispatch-pid (liveness).
#
# Env:
#   PRUN_STALL_THRESHOLD        no-growth seconds before "stalled" (default 600 = 10 min;
#                               code-writing units run long, so the default is generous)
#   PRUN_MONITOR_POLL           poll interval seconds (default 15)
#   PRUN_MONITOR_TIMEOUT        hard timeout seconds (default 3600)
#   PRUN_MONITOR_STABLE_WINDOW  result-quiet seconds before "done" (default 10; matches gather)
#
# Stdout:
#   MONITOR-START units=N stall-threshold=Ts timeout=Ss
#   MONITOR-EVENT <all-done|stall|fail|timeout>
#   UNIT <state-dir-basename> <status>      (one per unit; status is done / failed(...) /
#                                            stalled(Ns) / growing / pending)
# Exit:  0 all units done; 3 attention needed (stall or fail); 2 timeout or usage error.
#
# Never kills any process (only `kill -0` for liveness, like stall-watch).

set -u

if [ "$#" -lt 1 ]; then
    echo "monitor: need at least one <state-dir>" >&2
    echo "Usage: monitor.sh <state-dir> [<state-dir> ...]" >&2
    echo "MONITOR-EVENT usage-error"
    exit 2
fi

THRESHOLD="${PRUN_STALL_THRESHOLD:-600}"
POLL="${PRUN_MONITOR_POLL:-15}"
TIMEOUT="${PRUN_MONITOR_TIMEOUT:-3600}"
STABLE_WINDOW="${PRUN_MONITOR_STABLE_WINDOW:-10}"

_now() { date +%s 2>/dev/null || echo 0; }
_size() { sz=$(wc -c < "$1" 2>/dev/null | tr -d ' '); case "${sz:-0}" in ''|*[!0-9]*) echo 0 ;; *) echo "$sz" ;; esac; }
_mtime() { if m=$(stat -c %Y "$1" 2>/dev/null); then echo "$m"; elif m=$(stat -f %m "$1" 2>/dev/null); then echo "$m"; else echo 0; fi; }

# Per-unit state in index-parallel arrays (bash 3.2 safe; no associative arrays).
N=0
for sd in "$@"; do
    STATE_DIRS[$N]="$sd"
    LAST_SIZE[$N]=-1
    LAST_MTIME[$N]=0
    LAST_GROWTH[$N]=$(_now)
    STATUS[$N]="pending"
    N=$((N + 1))
done

printf 'MONITOR-START units=%d stall-threshold=%ds timeout=%ds\n' "$N" "$THRESHOLD" "$TIMEOUT"

emit_and_exit() {
    printf 'MONITOR-EVENT %s\n' "$1"
    j=0
    while [ "$j" -lt "$N" ]; do
        printf 'UNIT %s %s\n' "$(basename "${STATE_DIRS[$j]}")" "${STATUS[$j]}"
        j=$((j + 1))
    done
    exit "$2"
}

START=$(_now)
while :; do
    all_done=1
    has_fail=0
    has_stall=0
    i=0
    while [ "$i" -lt "$N" ]; do
        sd="${STATE_DIRS[$i]}"
        now=$(_now)
        rf=""
        [ -f "$sd/result-file" ] && rf=$(head -n 1 "$sd/result-file" 2>/dev/null)

        # Terminal? Result file present, non-empty, and quiet for the stable window.
        terminal=0
        result_present=0
        if [ -n "$rf" ] && [ -s "$rf" ]; then
            result_present=1
            rmt=$(_mtime "$rf")
            if [ $((now - rmt)) -ge "$STABLE_WINDOW" ]; then
                terminal=1
                # FALLBACK only when the dispatch-task backstop HEADER is on line 1,
                # never merely the word appearing inside a real worker's result body.
                if head -n 1 "$rf" 2>/dev/null | grep -q 'result (FALLBACK, worker wrote no result file)'; then
                    STATUS[$i]="failed(fallback)"; has_fail=1
                else
                    STATUS[$i]="done"
                fi
            fi
        fi

        if [ "$terminal" -eq 0 ]; then
            all_done=0
            if [ "$result_present" -eq 1 ]; then
                # A non-empty result is already written; it is only stabilizing toward
                # done/fallback. Never stall- or dead-classify a unit that produced a result.
                STATUS[$i]="finishing"
            else
                tail_f="$sd/tail"
                csize=$(_size "$tail_f")
                cmt=0; [ -f "$tail_f" ] && cmt=$(_mtime "$tail_f")
                if [ "$csize" -gt "${LAST_SIZE[$i]}" ] || [ "$cmt" -gt "${LAST_MTIME[$i]}" ]; then
                    LAST_SIZE[$i]="$csize"; LAST_MTIME[$i]="$cmt"; LAST_GROWTH[$i]="$now"
                    STATUS[$i]="growing"
                else
                    elapsed=$((now - ${LAST_GROWTH[$i]}))
                    if [ "$elapsed" -ge "$THRESHOLD" ]; then
                        pid=""; [ -f "$sd/dispatch-pid" ] && pid=$(head -n 1 "$sd/dispatch-pid" 2>/dev/null)
                        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
                            STATUS[$i]="failed(dispatch-dead)"; has_fail=1
                        else
                            STATUS[$i]="stalled(${elapsed}s)"; has_stall=1
                        fi
                    else
                        STATUS[$i]="growing"
                    fi
                fi
            fi
        fi
        i=$((i + 1))
    done

    [ "$has_fail" -eq 1 ] && emit_and_exit "fail" 3
    [ "$has_stall" -eq 1 ] && emit_and_exit "stall" 3
    [ "$all_done" -eq 1 ] && emit_and_exit "all-done" 0

    now=$(_now)
    [ $((now - START)) -ge "$TIMEOUT" ] && emit_and_exit "timeout" 2

    sleep "$POLL"
done
