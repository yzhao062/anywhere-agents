#!/usr/bin/env bash
# dispatch-task.sh -- prun generic task dispatch (Codex worker, Bash variant).
# Generalized from implement-review/scripts/dispatch-codex.sh.
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
#   CODEX_DISPATCH_REASONING    reasoning effort re-pass (default: xhigh)
#   PRUN_SCRATCH_CWD            explicit scratch working dir (default <state-dir>/work)
#
# Stdout:  first and only machine-readable line: STATE-DIR <abs-path>
# Stderr:  diagnostics + last 80 lines of codex combined stdout+stderr
# Exit:    propagates codex exec's exit code; 2 on usage error.

set -u

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
PROMPT_FILE="$(cd "$(dirname "$PROMPT_FILE")" && pwd)/$(basename "$PROMPT_FILE")"

# unit-id is interpolated into a path; allow only safe characters.
case "$UNIT_ID" in
    ''|*[!A-Za-z0-9_-]*)
        echo "dispatch-task: --unit-id must be alphanumeric/dash/underscore, got: $UNIT_ID" >&2
        exit 2 ;;
esac

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

# Emit STATE-DIR on stdout (first and only machine-readable line).
printf 'STATE-DIR %s\n' "$STATE_DIR"

CODEX_BIN="${CODEX_BIN:-codex}"
CODEX_DISPATCH_SANDBOX="${CODEX_DISPATCH_SANDBOX:-danger-full-access}"
CODEX_DISPATCH_REASONING="${CODEX_DISPATCH_REASONING:-xhigh}"

# Reviewer isolation (default on; CODEX_DISPATCH_ISOLATE_MCP=off opts out).
# --ignore-user-config stops a user-level Codex MCP server from auto-starting
# inside the headless worker and recursing into a nested codex (agent-config#1);
# it also resets reasoning to "none", so re-pass model_reasoning_effort.
CODEX_ISOLATE_ARGS=(--ignore-user-config -c "model_reasoning_effort=$CODEX_DISPATCH_REASONING")
if [ "$(printf '%s' "${CODEX_DISPATCH_ISOLATE_MCP:-}" | tr '[:upper:]' '[:lower:]')" = "off" ]; then
    CODEX_ISOLATE_ARGS=()
fi

# Run codex from the scratch cwd. Prompt via stdin (-), never positional
# (ARG_MAX) and never `codex exec review` (that injects codex's own template).
# --skip-git-repo-check is required because the scratch cwd is intentionally
# NOT a git repo (and --ignore-user-config drops the trusted-projects list);
# without it codex refuses with "Not inside a trusted directory".
( cd "$SCRATCH_CWD" && "$CODEX_BIN" exec --sandbox "$CODEX_DISPATCH_SANDBOX" \
    --skip-git-repo-check \
    ${CODEX_ISOLATE_ARGS[@]+"${CODEX_ISOLATE_ARGS[@]}"} - < "$PROMPT_FILE" ) \
    > "$STATE_DIR/tail" 2>&1
CODEX_EXIT=$?

tail -n 80 "$STATE_DIR/tail" >&2 2>/dev/null || true

# Result-loss backstop: if the unit did not write a non-empty result file (a
# worker's own result-write can fail, e.g. a fragile shell heredoc on Windows),
# salvage the captured stdout+stderr into the result file so the unit is never
# SILENTLY missing when gather polls. The orchestrator still gets a non-empty
# result; the FALLBACK header flags that the structured result is absent and the
# body is raw worker output to review or re-dispatch.
if [ ! -s "$RESULT_FILE" ]; then
    mkdir -p "$(dirname "$RESULT_FILE")" 2>/dev/null || true
    {
        printf '# %s result (FALLBACK, worker wrote no result file)\n' "$UNIT_ID"
        printf 'Conclusion: INCOMPLETE; structured result missing, raw worker output salvaged from the dispatch tail below.\n'
        printf 'Files: unknown\n'
        printf 'Open items: worker did not write its result file; review the raw output below or re-dispatch this unit.\n'
        printf 'Verification: none (salvaged by dispatch-task, not by the worker)\n\n'
        cat "$STATE_DIR/tail" 2>/dev/null || true
    } > "$RESULT_FILE" 2>/dev/null || true
fi

exit "$CODEX_EXIT"
