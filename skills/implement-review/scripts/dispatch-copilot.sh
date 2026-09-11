#!/usr/bin/env bash
# dispatch-copilot.sh -- Auto-terminal Copilot-backend dispatch for implement-review skill.
# See skills/implement-review/SKILL.md > Auto-terminal Copilot backend for the contract.
#
# Cross-vendor reviewer path: when Claude Code is unavailable and Codex (or the
# user) is the primary implementer, this dispatches GitHub Copilot CLI as the
# reviewer. Mirrors the dispatch-codex.sh state-dir / STATE-DIR / stall-watch
# contract so the same auto-watch, health-check, and Phase 2.0 machinery ingest
# the review with only the expected-review-file name swapped.
#
# Args (named):
#   --prompt-file <path>           Path to file containing the review prompt
#   --round <N>                    Round number (positive integer)
#   --expected-review-file <name>  Review file the reviewer is expected to write
#                                  (resolved relative to cwd for pre-mtime snapshot;
#                                   Review-GitHub-Copilot.md for the Copilot backend)
#
# Env:
#   COPILOT_BIN                    Copilot binary name or path (default: copilot)
#   GH_BIN                         gh binary name or path for the fallback path
#                                  (default: gh; used only when copilot is absent)
#   COPILOT_PREFLIGHT              auto (default), force, or off. auto runs a
#                                  live auth + git-shell probe for official
#                                  copilot / gh executables and skips wrappers.
#   COPILOT_PREFLIGHT_TIMEOUT_SECONDS
#                                  Live preflight timeout (default: 60)
#   TMPDIR                         Temp dir for state-dir (default: /tmp)
#
# Stdout:
#   First (and only) machine-readable line: STATE-DIR <abs-path>
#
# Stderr:
#   Dispatch diagnostics + last 80 lines of copilot combined stdout+stderr
#
# Exit code:
#   Propagates copilot's exit code unchanged.
#   Returns 2 on usage errors (missing/invalid args).
#   Returns 70 when preflight/postflight rejects an unusable review, 124 when
#   preflight times out, and 127 when no Copilot executable is available.

set -u

# Windows cannot replace a shell script while bash has it open. The review
# child may still invoke the consumer bootstrap despite the dispatch-specific
# instruction below, so release the deployed path before doing any work. A
# command-string parent waits for the copied script, removes it after the child
# exits, and propagates the child's status. The sentinel prevents re-exec loops.
if [ "${IMPLEMENT_REVIEW_DISPATCH_REEXEC:-}" != "1" ]; then
    REEXEC_TMP_BASE="${TMPDIR:-/tmp}"
    REEXEC_TMP_BASE="${REEXEC_TMP_BASE%/}"
    REEXEC_DIR="${REEXEC_TMP_BASE}/implement-review-dispatch-copilot-reexec-$$"
    REEXEC_COPY="${REEXEC_DIR}/dispatch-copilot.sh"

    umask 077
    if ! mkdir "$REEXEC_DIR"; then
        echo "dispatch-copilot: failed to create re-exec dir: $REEXEC_DIR" >&2
        exit 2
    fi
    if ! cp -- "$0" "$REEXEC_COPY"; then
        rmdir -- "$REEXEC_DIR" 2>/dev/null || true
        echo "dispatch-copilot: failed to create re-exec copy: $REEXEC_COPY" >&2
        exit 2
    fi
    chmod u+x "$REEXEC_COPY" 2>/dev/null || true

    export IMPLEMENT_REVIEW_DISPATCH_REEXEC=1
    export IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR
    IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR=$(dirname -- "$0")

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
    ' dispatch-copilot-reexec "$REEXEC_COPY" "$@"

    echo "dispatch-copilot: failed to launch re-exec copy: $REEXEC_COPY" >&2
    rm -f -- "$REEXEC_COPY"
    rmdir -- "$REEXEC_DIR" 2>/dev/null || true
    exit 2
fi

PROMPT_FILE=""
ROUND=""
EXPECTED_REVIEW_FILE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --prompt-file)
            PROMPT_FILE="$2"; shift 2 ;;
        --round)
            ROUND="$2"; shift 2 ;;
        --expected-review-file)
            EXPECTED_REVIEW_FILE="$2"; shift 2 ;;
        *)
            echo "dispatch-copilot: unknown argument: $1" >&2
            echo "Usage: dispatch-copilot.sh --prompt-file <path> --round <N> --expected-review-file <name>" >&2
            exit 2 ;;
    esac
done

if [ -z "$PROMPT_FILE" ] || [ -z "$ROUND" ] || [ -z "$EXPECTED_REVIEW_FILE" ]; then
    echo "dispatch-copilot: missing required argument" >&2
    echo "Usage: dispatch-copilot.sh --prompt-file <path> --round <N> --expected-review-file <name>" >&2
    exit 2
fi

if [ ! -f "$PROMPT_FILE" ]; then
    echo "dispatch-copilot: prompt file not found: $PROMPT_FILE" >&2
    exit 2
fi

case "$ROUND" in
    ''|*[!0-9]*)
        echo "dispatch-copilot: --round must be a positive integer, got: $ROUND" >&2
        exit 2 ;;
esac

# Build unique state-dir under TMPDIR
TMP_BASE="${TMPDIR:-/tmp}"
# Strip trailing slashes for clean concat
TMP_BASE="${TMP_BASE%/}"

# Repo-hash from cwd (short 8-char prefix of sha256)
if command -v sha256sum >/dev/null 2>&1; then
    REPO_HASH=$(pwd | sha256sum 2>/dev/null | cut -c1-8)
elif command -v shasum >/dev/null 2>&1; then
    REPO_HASH=$(pwd | shasum -a 256 2>/dev/null | cut -c1-8)
else
    REPO_HASH="nohash"
fi

# Nonce: 8 random bytes hex (16 chars)
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

STATE_DIR="${TMP_BASE}/implement-review-copilot-${REPO_HASH}-round${ROUND}-$$-${NONCE}"
mkdir -p "$STATE_DIR" || {
    echo "dispatch-copilot: failed to create state-dir: $STATE_DIR" >&2
    exit 2
}

# Record pre-dispatch mtime of any existing expected review file (Unix epoch seconds)
if [ -f "$EXPECTED_REVIEW_FILE" ]; then
    if PRE_MTIME=$(stat -c %Y "$EXPECTED_REVIEW_FILE" 2>/dev/null); then
        :
    elif PRE_MTIME=$(stat -f %m "$EXPECTED_REVIEW_FILE" 2>/dev/null); then
        :
    else
        PRE_MTIME="0"
    fi
else
    PRE_MTIME="0"
fi
printf '%s\n' "$PRE_MTIME" > "$STATE_DIR/pre-mtime"

# Record dispatch start wall time (Unix epoch seconds)
NOW_UNIX=$(date +%s)
printf '%s\n' "$NOW_UNIX" > "$STATE_DIR/timestamp"

# Emit STATE-DIR on stdout (first and only machine-readable line)
printf 'STATE-DIR %s\n' "$STATE_DIR"

# Resolve copilot binary, with gh-copilot fallback ------------------------
# Standalone `copilot` is preferred; if it is not resolvable, fall back to the
# built-in `gh copilot` entry point (both verified equivalent in the probe).
COPILOT_BIN="${COPILOT_BIN:-copilot}"
USE_GH=0
if COPILOT_RESOLVED=$(command -v "$COPILOT_BIN" 2>/dev/null); then
    COPILOT_CMD="$COPILOT_RESOLVED"
else
    GH_BIN="${GH_BIN:-gh}"
    if GH_RESOLVED=$(command -v "$GH_BIN" 2>/dev/null); then
        COPILOT_CMD="$GH_RESOLVED"
        USE_GH=1
    else
        printf '%s\n' \
            "dispatch-copilot: no runnable Copilot CLI found (tried '$COPILOT_BIN' and '$GH_BIN')." \
            "Install GitHub Copilot CLI or set COPILOT_BIN / GH_BIN to a runnable executable." \
            > "$STATE_DIR/tail"
        cat "$STATE_DIR/tail" >&2
        exit 127
    fi
fi

GH_PREFIX=()
if [ "$USE_GH" -eq 1 ]; then
    # `--` prevents gh from consuming flags intended for Copilot.
    GH_PREFIX=(copilot --)
fi

# Run a small live probe before the full review. It validates the exact stored
# Copilot credential, network path, JSON streaming flags, and git shell runner.
# Custom wrappers are skipped in auto mode because their CLI contract is opaque;
# COPILOT_PREFLIGHT=force opts them in.
PREFLIGHT_MODE=$(printf '%s' "${COPILOT_PREFLIGHT:-auto}" | tr '[:upper:]' '[:lower:]')
case "$PREFLIGHT_MODE" in
    auto|force|off) ;;
    *)
        echo "dispatch-copilot: COPILOT_PREFLIGHT must be auto, force, or off (got: $PREFLIGHT_MODE)" >&2
        exit 2 ;;
esac

OFFICIAL_CLI=0
case "$(basename -- "$COPILOT_CMD")" in
    copilot|copilot.exe|gh|gh.exe) OFFICIAL_CLI=1 ;;
esac
RUN_PREFLIGHT=$OFFICIAL_CLI
STRICT_CHECKS=$OFFICIAL_CLI
if [ "$PREFLIGHT_MODE" = "force" ]; then
    RUN_PREFLIGHT=1
    STRICT_CHECKS=1
elif [ "$PREFLIGHT_MODE" = "off" ]; then
    RUN_PREFLIGHT=0
fi

PREFLIGHT_TIMEOUT="${COPILOT_PREFLIGHT_TIMEOUT_SECONDS:-60}"
case "$PREFLIGHT_TIMEOUT" in
    ''|*[!0-9]*)
        echo "dispatch-copilot: COPILOT_PREFLIGHT_TIMEOUT_SECONDS must be a positive integer" >&2
        exit 2 ;;
esac
if [ "$PREFLIGHT_TIMEOUT" -lt 1 ]; then
    echo "dispatch-copilot: COPILOT_PREFLIGHT_TIMEOUT_SECONDS must be a positive integer" >&2
    exit 2
fi

if [ "$RUN_PREFLIGHT" -eq 1 ]; then
    PREFLIGHT_TAIL="$STATE_DIR/preflight-tail"
    PREFLIGHT_TIMEOUT_MARKER="$STATE_DIR/preflight-timeout"
    PREFLIGHT_PROMPT='Run git --version exactly once. If it exits 0, reply exactly COPILOT_PREFLIGHT_OK. Do not modify files.'

    GIT_PAGER=cat "$COPILOT_CMD" ${GH_PREFIX[@]+"${GH_PREFIX[@]}"} \
        -C "$STATE_DIR" -p "$PREFLIGHT_PROMPT" \
        --add-dir "$STATE_DIR" --allow-tool='shell(git:*)' \
        --no-ask-user --no-auto-update --no-custom-instructions \
        --disable-builtin-mcps --stream on --output-format json --no-color \
        > "$PREFLIGHT_TAIL" 2>&1 &
    PREFLIGHT_PID=$!

    (
        sleep "$PREFLIGHT_TIMEOUT"
        if kill -0 "$PREFLIGHT_PID" 2>/dev/null; then
            : > "$PREFLIGHT_TIMEOUT_MARKER"
            kill -TERM "$PREFLIGHT_PID" 2>/dev/null || true
            sleep 2
            kill -KILL "$PREFLIGHT_PID" 2>/dev/null || true
        fi
    ) >/dev/null 2>&1 &
    PREFLIGHT_TIMER_PID=$!

    wait "$PREFLIGHT_PID"
    PREFLIGHT_EXIT=$?
    kill "$PREFLIGHT_TIMER_PID" 2>/dev/null || true
    wait "$PREFLIGHT_TIMER_PID" 2>/dev/null || true

    if [ -f "$PREFLIGHT_TIMEOUT_MARKER" ]; then
        PREFLIGHT_EXIT=124
    fi

    if [ "$PREFLIGHT_EXIT" -ne 0 ] || \
       ! grep -Eq '"type":"tool\.execution_complete".*"success":true' "$PREFLIGHT_TAIL" || \
       ! grep -Eq '"type":"assistant\.message".*"content":"COPILOT_PREFLIGHT_OK"' "$PREFLIGHT_TAIL"; then
        cp "$PREFLIGHT_TAIL" "$STATE_DIR/tail" 2>/dev/null || : > "$STATE_DIR/tail"
        if [ "$PREFLIGHT_EXIT" -eq 124 ]; then
            echo "dispatch-copilot: preflight timed out after ${PREFLIGHT_TIMEOUT}s; refusing to spend a review round." >&2
        elif grep -Eqi 'Authentication token found but could not be validated|Failed to fetch OAuth user login|authentication failed|not authenticated|unauthorized' "$PREFLIGHT_TAIL"; then
            echo "dispatch-copilot: preflight could not validate Copilot authentication; refusing to spend a review round." >&2
            echo "Check network/proxy access first, then run 'copilot login' or provide COPILOT_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN." >&2
        elif grep -Fqi "unknown option '--no-warnings'" "$PREFLIGHT_TAIL"; then
            echo "dispatch-copilot: Copilot's shell runner rejected its internal --no-warnings flag; refusing to start." >&2
            echo "Run 'copilot update', start a fresh process, and retry. Dispatch auto-update is disabled with --no-auto-update." >&2
        else
            echo "dispatch-copilot: preflight could not run a successful git shell command (exit $PREFLIGHT_EXIT); refusing to start." >&2
        fi
        tail -n 40 "$PREFLIGHT_TAIL" >&2 2>/dev/null || true
        if [ "$PREFLIGHT_EXIT" -eq 0 ]; then
            PREFLIGHT_EXIT=70
        fi
        exit "$PREFLIGHT_EXIT"
    fi
fi

# Start stall-watch only for the full review. The live JSON stream below makes
# tail growth a meaningful liveness signal instead of a completion-only write.
SCRIPT_DIR="${IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR:-$(dirname -- "$0")}"
STALL_WATCH="${SCRIPT_DIR}/stall-watch.sh"
STALL_WATCH_PID=""
if [ -x "$STALL_WATCH" ]; then
    "$STALL_WATCH" --state-dir "$STATE_DIR" --parent-pid $$ >/dev/null 2>&1 &
    STALL_WATCH_PID=$!
fi
unset IMPLEMENT_REVIEW_DISPATCH_REEXEC IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR

# Run copilot with the prompt referenced as a file (`-p "@<prompt>"`), NOT via
# stdin and NOT as a long literal argument (a large literal -p arg fails).
# JSON streaming records reasoning and tool lifecycle events as they occur, so
# stall-watch can observe progress and postflight can reject hidden tool denial.
# GIT_PAGER=cat keeps Copilot's own `git diff` from stalling on a pager.
# --allow-all gives the reviewer the same unattended verification capability as
# the other backends; the review prompt keeps commit/push/publish/destructive
# operations out of scope. Copilot writes Review-GitHub-Copilot.md itself.
REPO="$(pwd)"
GIT_PAGER=cat "$COPILOT_CMD" ${GH_PREFIX[@]+"${GH_PREFIX[@]}"} \
    -C "$REPO" -p "@$PROMPT_FILE" \
    --allow-all \
    --no-ask-user --no-auto-update --stream on --output-format json --no-color \
    > "$STATE_DIR/tail" 2>&1
COPILOT_EXIT=$?

# Official CLI runs must not look complete when auth, the internal shell
# runner, permissions, or the review save contract failed quietly.
if [ "$STRICT_CHECKS" -eq 1 ]; then
    if grep -E '^[^{]|"type":"tool\.execution_complete".*"success":false|"type":"error"' "$STATE_DIR/tail" | \
       grep -Fqi "unknown option '--no-warnings'"; then
        echo "dispatch-copilot: Copilot emitted the known --no-warnings shell-runner failure; review rejected." >&2
        COPILOT_EXIT=70
    elif grep -E '^[^{]|"type":"tool\.execution_complete".*"success":false|"type":"error"' "$STATE_DIR/tail" | \
         grep -Eqi 'Authentication token found but could not be validated|Failed to fetch OAuth user login|authentication failed|not authenticated|unauthorized'; then
        echo "dispatch-copilot: Copilot authentication failed during dispatch; review rejected. Run 'copilot login' after checking network/proxy access." >&2
        COPILOT_EXIT=70
    elif grep -Eqi '"type":"tool\.execution_complete".*"success":false.*(permission|denied|not allowed|blocked)|(permission|denied|not allowed|blocked).*"type":"tool\.execution_complete".*"success":false' "$STATE_DIR/tail"; then
        echo "dispatch-copilot: a review tool was denied by Copilot's permission layer; review rejected instead of accepting unverified output." >&2
        COPILOT_EXIT=70
    fi

    if [ "$COPILOT_EXIT" -eq 0 ]; then
        REVIEW_PATH="$EXPECTED_REVIEW_FILE"
        case "$REVIEW_PATH" in
            /*) ;;
            *) REVIEW_PATH="$REPO/$REVIEW_PATH" ;;
        esac

        REVIEW_FAILURE=""
        if [ ! -f "$REVIEW_PATH" ]; then
            REVIEW_FAILURE="expected review file was not created: $EXPECTED_REVIEW_FILE"
        else
            REVIEW_SIZE=$(wc -c < "$REVIEW_PATH" 2>/dev/null || printf '0')
            REVIEW_FIRST_LINE=$(head -n 1 "$REVIEW_PATH" 2>/dev/null | tr -d '\r')
            if REVIEW_MTIME=$(stat -c %Y "$REVIEW_PATH" 2>/dev/null); then
                :
            elif REVIEW_MTIME=$(stat -f %m "$REVIEW_PATH" 2>/dev/null); then
                :
            else
                REVIEW_MTIME=0
            fi
            FRESH_FLOOR=$PRE_MTIME
            if [ "$NOW_UNIX" -gt "$FRESH_FLOOR" ]; then
                FRESH_FLOOR=$NOW_UNIX
            fi
            if [ "$REVIEW_SIZE" -lt 500 ]; then
                REVIEW_FAILURE="expected review file is only ${REVIEW_SIZE} bytes: $EXPECTED_REVIEW_FILE"
            elif [ "$REVIEW_FIRST_LINE" != "<!-- Round $ROUND -->" ]; then
                REVIEW_FAILURE="expected review file lacks the current round marker: $EXPECTED_REVIEW_FILE"
            elif [ "$REVIEW_MTIME" -le "$FRESH_FLOOR" ]; then
                REVIEW_FAILURE="expected review file was not refreshed by this dispatch: $EXPECTED_REVIEW_FILE"
            fi
        fi

        if [ -n "$REVIEW_FAILURE" ]; then
            echo "dispatch-copilot: $REVIEW_FAILURE; review rejected." >&2
            COPILOT_EXIT=70
        fi
    fi
fi

# Pipe last 80 lines of tail to stderr for caller visibility
tail -n 80 "$STATE_DIR/tail" >&2 2>/dev/null || true

# Do NOT force-kill stall-watch. It polls our PID via `kill -0` and exits
# silently on its next interval after we die. Stopping it on the hot path can
# erase a stall period that crossed the threshold during the final poll window.

exit "$COPILOT_EXIT"
