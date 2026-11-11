#!/usr/bin/env bash
# dispatch-task.sh -- prun generic task dispatch (Codex worker, Bash variant).
# Generalized from implement-review/scripts/dispatch-codex.sh.
#
# Runs a prun unit on a Codex worker. Use it only when the user named Codex for
# the unit or the batch; every other unit goes through dispatch-task-agy.py.
#
# Runs `codex exec` on a task prompt from a per-unit working dir so accidental
# relative writes land outside the user's repo. By default this is a scratch dir
# (read-only units); for code-writing units the caller sets PRUN_SCRATCH_CWD to a
# throwaway local clone. The unit writes its structured result to --result-file
# per its prompt; gather.sh polls for it.
#
# Args (named):
#   --prompt-file <path>   File containing the task prompt (fed to codex on stdin)
#   --result-file <path>   File the unit will write its result to (absolute;
#                          mtime snapshotted for freshness, polled by gather)
#   --unit-id <id>         Label for this unit (alnum/dash/underscore; names the state-dir)
#
# Env:
#   CODEX_BIN                   codex binary name or path (default: codex)
#   TMPDIR                      temp base for the state-dir (default: /tmp)
#   CODEX_DISPATCH_SANDBOX      sandbox mode (default: danger-full-access)
#   CODEX_DISPATCH_ISOLATE_MCP  "off" disables --ignore-user-config isolation
#   CODEX_DISPATCH_MODEL        one-run override of the worker model
#   CODEX_DISPATCH_REASONING    one-run override of the worker effort
#   AGENT_MODELS_FILE           alternate registry file (tests, one-off runs)
#   ANYWHERE_AGENTS_PYTHON      explicit Python interpreter for the resolver
#   PRUN_SCRATCH_CWD            explicit scratch working dir (default <state-dir>/work)
#   PRUN_STALL_THRESHOLD        no-growth seconds before worker reap (default: 600)
#   CODEX_DISPATCH_TIMEOUT      hard worker seconds before reap (default: 0, disabled)
#
# Model and effort: the registry (user/agent-models.json, read through
# scripts/agent_models.py) names both, as the prun-worker row of the codex
# family. The two override variables above outrank it for one run. Nothing
# else supplies a value, and no literal lives in this file: when neither the
# overrides nor the registry answer, the dispatch stops (exit 70) and the
# message names the registry. <state-dir>/model holds the requested model.
# After the run, <state-dir>/model-actual holds
#   requested=<model> <effort> actual=<model> <effort> status=ok|mismatch|unknown
# from the header codex printed (`agent_models.py actual`); MODEL-MISMATCH or
# MODEL-UNVERIFIED reaches stderr, and neither changes the exit code.
#
# Stdout:  first and only machine-readable line: STATE-DIR <abs-path>
# Stderr:  diagnostics + last 80 lines of codex combined stdout+stderr
# Exit:    propagates codex exec's exit code; 124 when dispatch reaps the worker;
#          2 on usage error; 70 when no source names the worker model and effort.

set -u

# Windows refuses a rename over a file another process holds open without
# FILE_SHARE_DELETE, and a shell holds a script open for as long as it is
# executing it. This dispatcher waits on its worker at the bottom of the file,
# so it holds the deployed path for the whole run, minutes to tens of minutes.
# A compose that redeploys the prun pack during a live fan-out then fails its
# rename over this path and aborts the entire transaction (#43).
#
# So release the deployed path before doing any work: copy this file into a
# private temp dir, hand off to the copy, and let a command-string parent wait
# for the copy, delete it, and propagate its exit status. The sentinel stops a
# re-exec loop. Like implement-review's dispatcher (#22) the source directory is
# handed down, because the model resolver is found by walking up from it; the
# prompt and result paths arrive as arguments.
if [ "${PRUN_DISPATCH_REEXEC:-}" != "1" ]; then
    REEXEC_TMP_BASE="${TMPDIR:-/tmp}"
    REEXEC_TMP_BASE="${REEXEC_TMP_BASE%/}"
    REEXEC_DIR="${REEXEC_TMP_BASE}/prun-dispatch-task-reexec-$$"
    REEXEC_COPY="${REEXEC_DIR}/dispatch-task.sh"

    umask 077
    if ! mkdir "$REEXEC_DIR"; then
        echo "dispatch-task: failed to create re-exec dir: $REEXEC_DIR" >&2
        exit 2
    fi
    if ! cp -- "$0" "$REEXEC_COPY"; then
        rmdir -- "$REEXEC_DIR" 2>/dev/null || true
        echo "dispatch-task: failed to create re-exec copy: $REEXEC_COPY" >&2
        exit 2
    fi
    chmod u+x "$REEXEC_COPY" 2>/dev/null || true

    export PRUN_DISPATCH_REEXEC=1
    export PRUN_DISPATCH_SOURCE_DIR
    PRUN_DISPATCH_SOURCE_DIR=$(dirname -- "$0")

    # Let a failed exec reach the cleanup below. Bash exits a noninteractive
    # shell on exec failure by default, so the diagnostic and the removal of
    # the private directory were unreachable. A successful exec replaces this
    # shell, so neither option change reaches normal execution.
    set +e
    shopt -s execfail

    exec "${BASH:-bash}" -c '
        reexec_copy=$1
        shift
        "${BASH:-bash}" "$reexec_copy" "$@"
        reexec_exit=$?
        rm -f -- "$reexec_copy"
        rmdir -- "$(dirname -- "$reexec_copy")" 2>/dev/null || true
        exit "$reexec_exit"
    ' dispatch-task-reexec "$REEXEC_COPY" "$@"

    echo "dispatch-task: failed to launch re-exec copy: $REEXEC_COPY" >&2
    rm -f -- "$REEXEC_COPY"
    rmdir -- "$REEXEC_DIR" 2>/dev/null || true
    exit 2
fi

# The worker inherits this environment. Clear the sentinel so a dispatch the
# worker starts itself still releases its own deployed path.
SCRIPT_DIR="${PRUN_DISPATCH_SOURCE_DIR:-$(dirname -- "$0")}"
unset PRUN_DISPATCH_REEXEC PRUN_DISPATCH_SOURCE_DIR

PROMPT_FILE=""
RESULT_FILE=""
UNIT_ID=""

# Guard value access so a trailing option without a value exits 2 (the
# documented usage error) instead of tripping `set -u` on $2.
_need_val() { [ "$1" -ge 2 ] || { echo "dispatch-task: $2 needs a value" >&2; exit 2; }; }
while [ $# -gt 0 ]; do
    case "$1" in
        --prompt-file) _need_val "$#" --prompt-file; PROMPT_FILE="$2"; shift 2 ;;
        --result-file) _need_val "$#" --result-file; RESULT_FILE="$2"; shift 2 ;;
        --unit-id)     _need_val "$#" --unit-id; UNIT_ID="$2"; shift 2 ;;
        *)
            echo "dispatch-task: unknown argument: $1" >&2
            echo "Usage: dispatch-task.sh --prompt-file <path> --result-file <path> --unit-id <id>" >&2
            exit 2 ;;
    esac
done

if [ -z "$PROMPT_FILE" ] || [ -z "$RESULT_FILE" ] || [ -z "$UNIT_ID" ]; then
    echo "dispatch-task: missing required argument" >&2
    echo "Usage: dispatch-task.sh --prompt-file <path> --result-file <path> --unit-id <id>" >&2
    exit 2
fi

if [ ! -f "$PROMPT_FILE" ]; then
    echo "dispatch-task: prompt file not found: $PROMPT_FILE" >&2
    exit 2
fi

# Resolve the prompt path to absolute: codex reads it via stdin AFTER we cd into
# the scratch cwd below, so a relative path would otherwise open the wrong file.
PROMPT_DIR="$(cd -- "$(dirname -- "$PROMPT_FILE")" && pwd)" || {
    echo "dispatch-task: cannot resolve the prompt file directory: $PROMPT_FILE" >&2
    exit 2
}
PROMPT_FILE="$PROMPT_DIR/$(basename -- "$PROMPT_FILE")"

# The result path is recorded in <state-dir>/result-file, and report-state reads
# a relative entry against the state dir, so record it absolute, as the Agy
# dispatcher does.
mkdir -p -- "$(dirname -- "$RESULT_FILE")"
RESULT_DIR="$(cd -- "$(dirname -- "$RESULT_FILE")" && pwd)" || {
    echo "dispatch-task: cannot resolve the result file directory: $RESULT_FILE" >&2
    exit 2
}
RESULT_FILE="$RESULT_DIR/$(basename -- "$RESULT_FILE")"

# unit-id is interpolated into a path; allow only safe characters.
case "$UNIT_ID" in
    ''|*[!A-Za-z0-9_-]*)
        echo "dispatch-task: --unit-id must be alphanumeric/dash/underscore, got: $UNIT_ID" >&2
        exit 2 ;;
esac

# Worker model and effort. The registry names both (the prun-worker row of the
# codex family) through scripts/agent_models.py; CODEX_DISPATCH_MODEL and
# CODEX_DISPATCH_REASONING outrank it for one run, and no literal lives here.
# A value that no source names stops the dispatch before any state exists,
# like dispatch-task-agy.py, rather than running on whatever codex defaults to.
# Python comes from ANYWHERE_AGENTS_PYTHON, then the _python wrapper beside the
# resolver (which skips the Windows Store shim), then python3 or python.
# Walk up from this script to the resolver. A source repo and
# .agent-config/repo hold it at <A>/scripts/; a consumer runs the copy that
# bootstrap deploys under <project>/.claude/skills/, so there the resolver is
# <project>/.agent-config/repo/scripts/. The registry must sit beside it.
find_agent_models() {
    _am_dir=$(cd -- "$1" 2>/dev/null && pwd -P) || return 1
    while :; do
        if [ -f "$_am_dir/scripts/agent_models.py" ] && [ -f "$_am_dir/user/agent-models.json" ]; then
            printf '%s\n' "$_am_dir/scripts/agent_models.py"
            return 0
        fi
        if [ -f "$_am_dir/.agent-config/repo/scripts/agent_models.py" ] \
            && [ -f "$_am_dir/.agent-config/repo/user/agent-models.json" ]; then
            printf '%s\n' "$_am_dir/.agent-config/repo/scripts/agent_models.py"
            return 0
        fi
        _am_parent=$(dirname -- "$_am_dir")
        [ "$_am_parent" = "$_am_dir" ] && return 1
        _am_dir=$_am_parent
    done
}
AGENT_MODELS_PY=$(find_agent_models "$SCRIPT_DIR" || true)
AM_CMD=()
if [ -n "$AGENT_MODELS_PY" ] && [ -f "$AGENT_MODELS_PY" ]; then
    # A named interpreter is authoritative, as in dispatch-codex and _python: a
    # broken one stops the dispatch instead of falling through to another.
    if [ -n "${ANYWHERE_AGENTS_PYTHON:-}" ]; then
        if [ -x "$ANYWHERE_AGENTS_PYTHON" ]; then
            AM_CMD=("$ANYWHERE_AGENTS_PYTHON" "$AGENT_MODELS_PY")
        fi
    elif [ -f "$(dirname -- "$AGENT_MODELS_PY")/_python" ]; then
        AM_CMD=("${BASH:-bash}" "$(dirname -- "$AGENT_MODELS_PY")/_python" "$AGENT_MODELS_PY")
    elif command -v python3 >/dev/null 2>&1; then
        AM_CMD=(python3 "$AGENT_MODELS_PY")
    elif command -v python >/dev/null 2>&1; then
        AM_CMD=(python "$AGENT_MODELS_PY")
    fi
fi

# An override is the operator's own value, but it reaches the codex command line
# (and the .ps1's generated cmd helper), so hold it to the registry's alphabet.
for _override in "${CODEX_DISPATCH_MODEL:-}" "${CODEX_DISPATCH_REASONING:-}"; do
    case "$_override" in *[!A-Za-z0-9._:/-]*)
        echo "dispatch-task: CODEX_DISPATCH_MODEL and CODEX_DISPATCH_REASONING may hold only letters, digits, and . _ : / -" >&2
        exit 2 ;;
    esac
done

REGISTRY_MODEL=""
REGISTRY_EFFORT=""
REGISTRY_WHY="scripts/agent_models.py or a Python interpreter was not found"
if [ "${#AM_CMD[@]}" -gt 0 ] && { [ -z "${CODEX_DISPATCH_MODEL:-}" ] || [ -z "${CODEX_DISPATCH_REASONING:-}" ]; }; then
    REGISTRY_WHY="the resolver did not answer"
    if REGISTRY_OUT=$(${AM_CMD[@]+"${AM_CMD[@]}"} resolve --family codex --role prun-worker 2>&1); then
        read -r REGISTRY_MODEL REGISTRY_EFFORT <<< "$REGISTRY_OUT"
    else
        REGISTRY_WHY=$(printf '%s' "$REGISTRY_OUT" | tr '\n' ' ')
    fi
fi
case "$REGISTRY_MODEL" in ''|*[!A-Za-z0-9._:/-]*) REGISTRY_MODEL="" ;; esac
case "$REGISTRY_EFFORT" in ''|*[!A-Za-z0-9._:/-]*) REGISTRY_EFFORT="" ;; esac
WORKER_MODEL="${CODEX_DISPATCH_MODEL:-$REGISTRY_MODEL}"
WORKER_EFFORT="${CODEX_DISPATCH_REASONING:-$REGISTRY_EFFORT}"
if [ "$(printf '%s' "${CODEX_DISPATCH_ISOLATE_MCP:-}" | tr '[:upper:]' '[:lower:]')" != "off" ] \
    && { [ -z "$WORKER_MODEL" ] || [ -z "$WORKER_EFFORT" ]; }; then
    echo "dispatch-task: cannot determine the Codex worker model and effort. The registry (user/agent-models.json, resolved by scripts/agent_models.py) names them in its prun-worker row for the codex family, and it did not answer: ${REGISTRY_WHY}" >&2
    echo "Fix the registry or the Python setup (see the agent-models skill), or set CODEX_DISPATCH_MODEL and CODEX_DISPATCH_REASONING for this run. Refusing to start the unit." >&2
    exit 70
fi

TMP_BASE="${TMPDIR:-/tmp}"
TMP_BASE="${TMP_BASE%/}"

if command -v sha256sum >/dev/null 2>&1; then
    REPO_HASH=$(pwd | sha256sum 2>/dev/null | cut -c1-8)
elif command -v shasum >/dev/null 2>&1; then
    REPO_HASH=$(pwd | shasum -a 256 2>/dev/null | cut -c1-8)
else
    REPO_HASH="nohash"
fi

if [ -r /dev/urandom ]; then
    if command -v xxd >/dev/null 2>&1; then
        NONCE=$(head -c 8 /dev/urandom | xxd -p)
    elif command -v od >/dev/null 2>&1; then
        NONCE=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
    else
        NONCE=$(date +%s%N | tail -c 17)
    fi
else
    NONCE=$(date +%s%N | tail -c 17)
fi

STATE_DIR="${TMP_BASE}/prun-task-${REPO_HASH}-${UNIT_ID}-$$-${NONCE}"
mkdir -p "$STATE_DIR" || {
    echo "dispatch-task: failed to create state-dir: $STATE_DIR" >&2
    exit 2
}

# Per-unit scratch working dir. codex runs from here so accidental relative
# writes, downloads, and caches stay out of the user's repo.
SCRATCH_CWD="${PRUN_SCRATCH_CWD:-$STATE_DIR/work}"
mkdir -p "$SCRATCH_CWD" || {
    echo "dispatch-task: failed to create scratch cwd: $SCRATCH_CWD" >&2
    exit 2
}

# Record pre-dispatch mtime of any existing result file (Unix epoch seconds).
if [ -f "$RESULT_FILE" ]; then
    if PRE_MTIME=$(stat -c %Y "$RESULT_FILE" 2>/dev/null); then :
    elif PRE_MTIME=$(stat -f %m "$RESULT_FILE" 2>/dev/null); then :
    else PRE_MTIME="0"; fi
else
    PRE_MTIME="0"
fi
printf '%s\n' "$PRE_MTIME" > "$STATE_DIR/pre-mtime"
date +%s > "$STATE_DIR/timestamp"
printf '%s\n' "$RESULT_FILE" > "$STATE_DIR/result-file"
# Record this dispatcher's PID so monitor.sh can tell a stalled-but-alive unit
# from a dead dispatch (killed mid-run) that will never produce a result.
printf '%s\n' "$$" > "$STATE_DIR/dispatch-pid"
printf '%s\n' "${WORKER_MODEL:--}" > "$STATE_DIR/model"

# Emit STATE-DIR on stdout (first and only machine-readable line).
printf 'STATE-DIR %s\n' "$STATE_DIR"

CODEX_BIN="${CODEX_BIN:-codex}"
CODEX_DISPATCH_SANDBOX="${CODEX_DISPATCH_SANDBOX:-danger-full-access}"
PRUN_STALL_THRESHOLD="${PRUN_STALL_THRESHOLD:-600}"
CODEX_DISPATCH_TIMEOUT="${CODEX_DISPATCH_TIMEOUT:-0}"
case "$PRUN_STALL_THRESHOLD" in ''|*[!0-9]*) PRUN_STALL_THRESHOLD=600 ;; esac
case "$CODEX_DISPATCH_TIMEOUT" in ''|*[!0-9]*) CODEX_DISPATCH_TIMEOUT=0 ;; esac
DISPATCH_REAP_EXIT=124
DISPATCH_POLL=1

# Worker isolation (default on; CODEX_DISPATCH_ISOLATE_MCP=off opts out).
# --ignore-user-config stops a user-level Codex MCP server from auto-starting
# inside the headless worker and recursing into a nested codex (agent-config#1).
# It also drops the configured model and resets reasoning to "none", so both
# are re-passed from the registry row resolved above. With isolation off
# nothing is passed, codex reads its own config, and the run is still checked
# against the resolved values after it ends.
CODEX_ISOLATE_ARGS=(--ignore-user-config -c "model=$WORKER_MODEL" -c "model_reasoning_effort=$WORKER_EFFORT")
if [ "$(printf '%s' "${CODEX_DISPATCH_ISOLATE_MCP:-}" | tr '[:upper:]' '[:lower:]')" = "off" ]; then
    CODEX_ISOLATE_ARGS=()
fi

# Run codex from the scratch cwd. Prompt via stdin (-), never positional
# (ARG_MAX) and never `codex exec review` (that injects codex's own template).
# --skip-git-repo-check is required because the scratch cwd is intentionally
# NOT a git repo (and --ignore-user-config drops the trusted-projects list);
# without it codex refuses with "Not inside a trusted directory".
_now() { date +%s 2>/dev/null || echo 0; }
_size() { sz=$(wc -c < "$1" 2>/dev/null | tr -d ' '); case "${sz:-0}" in ''|*[!0-9]*) echo 0 ;; *) echo "$sz" ;; esac; }
_mtime() { if m=$(stat -c %Y "$1" 2>/dev/null); then echo "$m"; elif m=$(stat -f %m "$1" 2>/dev/null); then echo "$m"; else echo 0; fi; }

_kill_descendants() {
    parent="$1"
    children=$(pgrep -P "$parent" 2>/dev/null || true)
    for child in $children; do
        _kill_descendants "$child"
    done
    kill -TERM "$parent" 2>/dev/null || true
}

_kill_descendants_force() {
    parent="$1"
    children=$(pgrep -P "$parent" 2>/dev/null || true)
    for child in $children; do
        _kill_descendants_force "$child"
    done
    kill -KILL "$parent" 2>/dev/null || true
}

_kill_worker_tree() {
    worker_pid="$1"
    if [ "${WORKER_KILL_MODE:-}" = "pgid" ]; then
        kill -TERM "-$worker_pid" 2>/dev/null || kill -TERM "$worker_pid" 2>/dev/null || true
        sleep 2
        kill -KILL "-$worker_pid" 2>/dev/null || kill -KILL "$worker_pid" 2>/dev/null || true
    else
        _kill_descendants "$worker_pid"
        sleep 2
        _kill_descendants_force "$worker_pid"
    fi
}

# The idle watchdog is the main reaper. CODEX_DISPATCH_TIMEOUT=0 disables the
# wall-clock cap, so active long xhigh runs are not killed unless opted in.
TAIL_PATH="$STATE_DIR/tail"
START=$(_now)
LAST_SIZE=-1
LAST_MTIME=0
LAST_GROWTH="$START"
REAP_REASON=""
WORKER_KILL_MODE="children"
if command -v setsid >/dev/null 2>&1; then
    setsid bash -c 'cd "$1" || exit 1; shift; exec "$@"' dispatch-worker "$SCRATCH_CWD" \
        "$CODEX_BIN" exec --sandbox "$CODEX_DISPATCH_SANDBOX" \
        --skip-git-repo-check \
        ${CODEX_ISOLATE_ARGS[@]+"${CODEX_ISOLATE_ARGS[@]}"} - < "$PROMPT_FILE" \
        > "$TAIL_PATH" 2>&1 &
    WORKER_PID=$!
    WORKER_KILL_MODE="pgid"
else
    ( cd "$SCRATCH_CWD" && "$CODEX_BIN" exec --sandbox "$CODEX_DISPATCH_SANDBOX" \
        --skip-git-repo-check \
        ${CODEX_ISOLATE_ARGS[@]+"${CODEX_ISOLATE_ARGS[@]}"} - < "$PROMPT_FILE" ) \
        > "$TAIL_PATH" 2>&1 &
    WORKER_PID=$!
fi

while kill -0 "$WORKER_PID" 2>/dev/null; do
    now=$(_now)
    csize=$(_size "$TAIL_PATH")
    cmt=0; [ -f "$TAIL_PATH" ] && cmt=$(_mtime "$TAIL_PATH")
    if [ "$csize" -gt "$LAST_SIZE" ] || [ "$cmt" -gt "$LAST_MTIME" ]; then
        LAST_SIZE="$csize"
        LAST_MTIME="$cmt"
        LAST_GROWTH="$now"
    elif [ $((now - LAST_GROWTH)) -ge "$PRUN_STALL_THRESHOLD" ]; then
        REAP_REASON="idle-stall"
        break
    fi
    if [ "$CODEX_DISPATCH_TIMEOUT" -gt 0 ] && [ $((now - START)) -ge "$CODEX_DISPATCH_TIMEOUT" ]; then
        REAP_REASON="hard-timeout"
        break
    fi
    sleep "$DISPATCH_POLL"
done

if [ -n "$REAP_REASON" ]; then
    echo "dispatch-task: reaping worker process tree ($REAP_REASON)" >&2
    _kill_worker_tree "$WORKER_PID"
    # Bounded wait: on POSIX the kill reaps the tree and the worker exits at once. On a
    # shell without pgrep/setsid (e.g. Windows git-bash) the tree kill is best-effort, so
    # do not block indefinitely -- proceed after a short grace so the dispatcher still
    # exits and gather still gets a FALLBACK. Use dispatch-task.ps1 on Windows for reliable
    # tree reaping.
    _reap_wait=0
    while kill -0 "$WORKER_PID" 2>/dev/null && [ "$_reap_wait" -lt 10 ]; do
        sleep 1; _reap_wait=$((_reap_wait + 1))
    done
    CODEX_EXIT=$DISPATCH_REAP_EXIT
    {
        printf '\n'
        printf 'dispatch-task: worker reaped (%s); exit code %s.\n' "$REAP_REASON" "$DISPATCH_REAP_EXIT"
    } >> "$TAIL_PATH" 2>/dev/null || true
else
    wait "$WORKER_PID"
    CODEX_EXIT=$?
fi

tail -n 80 "$STATE_DIR/tail" >&2 2>/dev/null || true

# Effective, not configured: the header codex printed (`model:` and `reasoning
# effort:`) is in the combined tail. The record says what was asked for and what
# the run reports; a mismatch prints MODEL-MISMATCH on stderr (from the
# resolver), an unreadable header prints MODEL-UNVERIFIED, and the exit code
# stays the worker's.
ACTUAL_EXPECT=()
if [ -n "$WORKER_MODEL" ] && [ -n "$WORKER_EFFORT" ]; then
    ACTUAL_EXPECT=(--expect-model "$WORKER_MODEL" --expect-effort "$WORKER_EFFORT")
fi
ACTUAL_LINE=""
if [ "${#AM_CMD[@]}" -gt 0 ]; then
    ACTUAL_LINE=$(${AM_CMD[@]+"${AM_CMD[@]}"} actual --family codex --stderr-file "$STATE_DIR/tail" \
        ${ACTUAL_EXPECT[@]+"${ACTUAL_EXPECT[@]}"}) || ACTUAL_LINE=""
fi
if [ -z "$ACTUAL_LINE" ]; then
    ACTUAL_LINE="actual=unknown status=unknown"
    echo "dispatch-task: MODEL-UNVERIFIED the run could not be read back (no Python interpreter or resolver)" >&2
fi
printf 'requested=%s %s %s\n' "${WORKER_MODEL:--}" "${WORKER_EFFORT:--}" \
    "${ACTUAL_LINE/actual=unknown status=/actual=- status=}" > "$STATE_DIR/model-actual"

# Result-loss backstop: if the unit did not write a non-empty result file (a
# worker's own result-write can fail, e.g. a fragile shell heredoc on Windows),
# salvage the captured stdout+stderr into the result file so the unit is never
# SILENTLY missing when gather polls. The orchestrator still gets a non-empty
# result; the FALLBACK header flags that the structured result is absent and the
# body is raw worker output to review or re-dispatch.
if [ ! -s "$RESULT_FILE" ]; then
    mkdir -p "$(dirname "$RESULT_FILE")" 2>/dev/null || true
    if [ -n "$REAP_REASON" ]; then
        FALLBACK_CONCLUSION="INCOMPLETE; worker was reaped by dispatch-task ($REAP_REASON) before writing a result; raw worker output salvaged from the dispatch tail below."
        FALLBACK_OPEN_ITEMS="worker was reaped by dispatch-task ($REAP_REASON); review the raw output below or re-dispatch this unit."
        FALLBACK_VERIFICATION="none (worker reaped by dispatch-task; tail salvaged by dispatch-task)"
    else
        FALLBACK_CONCLUSION="INCOMPLETE; structured result missing, raw worker output salvaged from the dispatch tail below."
        FALLBACK_OPEN_ITEMS="worker did not write its result file; review the raw output below or re-dispatch this unit."
        FALLBACK_VERIFICATION="none (salvaged by dispatch-task, not by the worker)"
    fi
    {
        printf '# %s result (FALLBACK, worker wrote no result file)\n' "$UNIT_ID"
        printf 'Conclusion: %s\n' "$FALLBACK_CONCLUSION"
        printf 'Files: unknown\n'
        printf 'Open items: %s\n' "$FALLBACK_OPEN_ITEMS"
        printf 'Verification: %s\n\n' "$FALLBACK_VERIFICATION"
        cat "$STATE_DIR/tail" 2>/dev/null || true
    } > "$RESULT_FILE" 2>/dev/null || true
fi

exit "$CODEX_EXIT"
