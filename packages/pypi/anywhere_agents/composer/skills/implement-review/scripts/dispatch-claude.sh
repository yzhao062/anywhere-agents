#!/usr/bin/env bash
# dispatch-claude.sh -- Auto-terminal Claude Code (`claude -p`) reviewer backend.
# See skills/implement-review/SKILL.md > Auto-terminal Claude backend for the contract.
#
# Cross-vendor reviewer path: when Codex (or the user) is the primary
# implementer and Claude is preferred as the reviewer voice, this dispatches
# headless Claude Code (`claude -p`) as the reviewer. Mirrors the
# dispatch-codex.sh / dispatch-copilot.sh state-dir / STATE-DIR / stall-watch
# contract so the same auto-watch, health-check, and Phase 2.0 machinery ingest
# the review with only the expected-review-file name swapped.
#
# Self-review guard: refuses to dispatch when the invoking orchestrator is
# Claude Code itself (would be self-review, which the fungibility principle
# disallows). See the env-check block below.
#
# Args (named):
#   --prompt-file <path>           Path to file containing the review prompt
#   --round <N>                    Round number (positive integer)
#   --expected-review-file <name>  Review file the reviewer is expected to write
#                                  (resolved relative to cwd for pre-mtime snapshot;
#                                   Review-Claude-Code.md for the Claude backend)
#
# Env:
#   CLAUDE_BIN                       Claude binary name or path (default: claude)
#   IMPLEMENT_REVIEW_ORCHESTRATOR    Who is driving this dispatch: claude / codex / user.
#                                    When 'claude' (case-insensitive), refuses to
#                                    dispatch with exit 2. When unset/empty AND
#                                    CLAUDECODE=1, also refuses (Claude Code's
#                                    documented subprocess marker means the call
#                                    originated from a Claude Code session).
#   CLAUDECODE                       Set to '1' by Claude Code in its Bash / PowerShell
#                                    / hook / tmux / status-line subprocesses.
#                                    Used as the implicit fall-through guard signal.
#   CLAUDE_PREFLIGHT                  auto (default), force, or off. auto runs a
#                                    no-model auth check for the official Claude
#                                    CLI and skips opaque wrappers.
#   CLAUDE_PREFLIGHT_TIMEOUT_SECONDS  Preflight timeout (default: 60).
#   TMPDIR                           Temp dir for state-dir (default: /tmp)
#
# Stdout:
#   First (and only) machine-readable line: STATE-DIR <abs-path>
#
# Stderr:
#   Dispatch diagnostics + last 80 lines of claude combined stdout+stderr
#
# Exit code:
#   Propagates claude's exit code unchanged.
#   Returns 2 on usage errors (missing/invalid args) or self-review refusal.
#   Returns 70 when preflight/postflight rejects an unusable review, 124 when
#   preflight times out, and 127 when Claude is unavailable.

set -u

# Release the deployed path before the reviewer can follow project startup
# instructions that refresh this skill. A command-string parent waits for the
# copied script, removes it after the child exits, and preserves its status.
if [ "${IMPLEMENT_REVIEW_DISPATCH_REEXEC:-}" != "1" ]; then
    REEXEC_TMP_BASE="${TMPDIR:-/tmp}"
    REEXEC_TMP_BASE="${REEXEC_TMP_BASE%/}"
    REEXEC_DIR="${REEXEC_TMP_BASE}/implement-review-dispatch-claude-reexec-$$"
    REEXEC_COPY="${REEXEC_DIR}/dispatch-claude.sh"

    umask 077
    if ! mkdir "$REEXEC_DIR"; then
        echo "dispatch-claude: failed to create re-exec dir: $REEXEC_DIR" >&2
        exit 2
    fi
    if ! cp -- "$0" "$REEXEC_COPY"; then
        rmdir -- "$REEXEC_DIR" 2>/dev/null || true
        echo "dispatch-claude: failed to create re-exec copy: $REEXEC_COPY" >&2
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
    ' dispatch-claude-reexec "$REEXEC_COPY" "$@"

    echo "dispatch-claude: failed to launch re-exec copy: $REEXEC_COPY" >&2
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
            echo "dispatch-claude: unknown argument: $1" >&2
            echo "Usage: dispatch-claude.sh --prompt-file <path> --round <N> --expected-review-file <name>" >&2
            exit 2 ;;
    esac
done

if [ -z "$PROMPT_FILE" ] || [ -z "$ROUND" ] || [ -z "$EXPECTED_REVIEW_FILE" ]; then
    echo "dispatch-claude: missing required argument" >&2
    echo "Usage: dispatch-claude.sh --prompt-file <path> --round <N> --expected-review-file <name>" >&2
    exit 2
fi

if [ ! -f "$PROMPT_FILE" ]; then
    echo "dispatch-claude: prompt file not found: $PROMPT_FILE" >&2
    exit 2
fi

case "$ROUND" in
    ''|*[!0-9]*)
        echo "dispatch-claude: --round must be a positive integer, got: $ROUND" >&2
        exit 2 ;;
esac

# Self-review guard (orchestrator detection) --------------------------------
# Refuse to dispatch when ANY of these holds:
#   - IMPLEMENT_REVIEW_ORCHESTRATOR=claude (case-insensitive), OR
#   - IMPLEMENT_REVIEW_ORCHESTRATOR is unset/empty AND CLAUDECODE=1
# IMPLEMENT_REVIEW_ORCHESTRATOR=codex / user proceed regardless of CLAUDECODE.
ORCH_RAW="${IMPLEMENT_REVIEW_ORCHESTRATOR:-}"
ORCH_LC=$(printf '%s' "$ORCH_RAW" | tr '[:upper:]' '[:lower:]')
if [ "$ORCH_LC" = "claude" ]; then
    echo "dispatch-claude: refusing to dispatch (orchestrator=claude; self-review)" >&2
    exit 2
fi
if [ -z "$ORCH_LC" ] && [ "${CLAUDECODE:-}" = "1" ]; then
    echo "dispatch-claude: refusing to dispatch (orchestrator=claude; self-review)" >&2
    exit 2
fi

# Build unique state-dir under TMPDIR
TMP_BASE="${TMPDIR:-/tmp}"
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

STATE_DIR="${TMP_BASE}/implement-review-claude-${REPO_HASH}-round${ROUND}-$$-${NONCE}"
mkdir -p "$STATE_DIR" || {
    echo "dispatch-claude: failed to create state-dir: $STATE_DIR" >&2
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
date +%s > "$STATE_DIR/timestamp"

# Emit STATE-DIR on stdout (first and only machine-readable line)
printf 'STATE-DIR %s\n' "$STATE_DIR"

# Resolve claude binary -----------------------------------------------------
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
if CLAUDE_RESOLVED=$(command -v "$CLAUDE_BIN" 2>/dev/null); then
    CLAUDE_CMD="$CLAUDE_RESOLVED"
else
    printf '%s\n' \
        "dispatch-claude: no runnable Claude CLI found: $CLAUDE_BIN" \
        "Install Claude Code or set CLAUDE_BIN to a runnable executable." \
        > "$STATE_DIR/tail"
    cat "$STATE_DIR/tail" >&2
    exit 127
fi

# Refuse to spend a review round when the official CLI cannot authenticate.
# `claude auth status` does not invoke a model. API-key, gateway, and hosted-
# provider modes use an availability check because their credentials are not
# represented by the first-party login status. Opaque wrappers are skipped in
# auto mode; CLAUDE_PREFLIGHT=force opts them into the auth check.
PREFLIGHT_MODE=$(printf '%s' "${CLAUDE_PREFLIGHT:-auto}" | tr '[:upper:]' '[:lower:]')
case "$PREFLIGHT_MODE" in
    auto|force|off) ;;
    *)
        echo "dispatch-claude: CLAUDE_PREFLIGHT must be auto, force, or off (got: $PREFLIGHT_MODE)" >&2
        exit 2 ;;
esac

OFFICIAL_CLI=0
case "$(basename -- "$CLAUDE_CMD")" in
    claude|claude.exe|claude.cmd) OFFICIAL_CLI=1 ;;
esac
RUN_PREFLIGHT=$OFFICIAL_CLI
STRICT_CHECKS=$OFFICIAL_CLI
if [ "$PREFLIGHT_MODE" = "force" ]; then
    RUN_PREFLIGHT=1
    STRICT_CHECKS=1
elif [ "$PREFLIGHT_MODE" = "off" ]; then
    RUN_PREFLIGHT=0
fi

PREFLIGHT_TIMEOUT="${CLAUDE_PREFLIGHT_TIMEOUT_SECONDS:-60}"
case "$PREFLIGHT_TIMEOUT" in
    ''|*[!0-9]*|0)
        echo "dispatch-claude: CLAUDE_PREFLIGHT_TIMEOUT_SECONDS must be a positive integer" >&2
        exit 2 ;;
esac

if [ "$RUN_PREFLIGHT" -eq 1 ]; then
    PREFLIGHT_KIND=auth
    if [ "${CLAUDE_DISPATCH_BARE:-}" = "1" ] ||
       [ -n "${ANTHROPIC_API_KEY:-}" ] ||
       [ -n "${ANTHROPIC_BASE_URL:-}" ] ||
       [ -n "${CLAUDE_CODE_USE_BEDROCK:-}" ] ||
       [ -n "${CLAUDE_CODE_USE_VERTEX:-}" ] ||
       [ -n "${CLAUDE_CODE_USE_FOUNDRY:-}" ]; then
        PREFLIGHT_KIND=availability
    fi

    PREFLIGHT_RAW="$STATE_DIR/preflight-raw"
    PREFLIGHT_TAIL="$STATE_DIR/preflight-tail"
    PREFLIGHT_TIMEOUT_MARKER="$STATE_DIR/preflight-timeout"
    if [ "$PREFLIGHT_KIND" = "auth" ]; then
        "$CLAUDE_CMD" auth status --json </dev/null > "$PREFLIGHT_RAW" 2>&1 &
    else
        "$CLAUDE_CMD" --version </dev/null > "$PREFLIGHT_RAW" 2>&1 &
    fi
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

    PREFLIGHT_OK=0
    if [ "$PREFLIGHT_EXIT" -eq 0 ]; then
        if [ "$PREFLIGHT_KIND" = "auth" ]; then
            if grep -Eq '"loggedIn"[[:space:]]*:[[:space:]]*true' "$PREFLIGHT_RAW"; then
                PREFLIGHT_OK=1
            fi
        elif [ -s "$PREFLIGHT_RAW" ]; then
            PREFLIGHT_OK=1
        fi
    fi

    printf 'CLAUDE_PREFLIGHT kind=%s status=%s exit=%s\n' \
        "$PREFLIGHT_KIND" "$([ "$PREFLIGHT_OK" -eq 1 ] && printf ok || printf failed)" \
        "$PREFLIGHT_EXIT" > "$PREFLIGHT_TAIL"
    rm -f -- "$PREFLIGHT_RAW"

    if [ "$PREFLIGHT_OK" -ne 1 ]; then
        cp "$PREFLIGHT_TAIL" "$STATE_DIR/tail" 2>/dev/null || : > "$STATE_DIR/tail"
        if [ "$PREFLIGHT_EXIT" -eq 124 ]; then
            echo "dispatch-claude: preflight timed out after ${PREFLIGHT_TIMEOUT}s; refusing to spend a review round." >&2
        elif [ "$PREFLIGHT_KIND" = "auth" ]; then
            echo "dispatch-claude: Claude authentication is unavailable; refusing to spend a review round. Run 'claude auth login' and retry." >&2
        else
            echo "dispatch-claude: Claude CLI availability preflight failed; refusing to spend a review round." >&2
        fi
        cat "$PREFLIGHT_TAIL" >&2
        if [ "$PREFLIGHT_EXIT" -eq 124 ]; then
            exit 124
        fi
        exit 70
    fi
fi

# Build a Claude-specific relay prompt. Claude only reads context and returns
# the complete review on stdout; this wrapper writes stdout to the expected
# review file after Claude exits. That avoids unattended Write/Edit permission
# prompts and keeps the auto-terminal path non-interactive.
RELAY_PROMPT_FILE="$STATE_DIR/prompt-relay"
EMPTY_MCP_CONFIG_FILE="$STATE_DIR/empty-mcp-config.json"
DIFF_FILE="$STATE_DIR/staged-diff"
GIT_DIFF_STDERR="$STATE_DIR/git-diff.stderr"
VALIDATION_DIR="$(pwd)"
STAGED_SNAPSHOT_DIR="$STATE_DIR/staged-snapshot"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if ! git diff --cached --no-ext-diff > "$DIFF_FILE" 2> "$GIT_DIFF_STDERR"; then
        printf '%s\n' "dispatch-claude: git diff --cached failed; see state-dir/git-diff.stderr" > "$DIFF_FILE"
    fi
    mkdir -p "$STAGED_SNAPSHOT_DIR"
    # core.longpaths as a per-command override: the state-dir prefix plus a
    # long index path crosses the 260-character Windows limit, which failed
    # the export. Inert off Windows, and the user's git config is untouched.
    if git -c core.longpaths=true checkout-index -a --prefix="$STAGED_SNAPSHOT_DIR/" >> "$GIT_DIFF_STDERR" 2>&1; then
        VALIDATION_DIR="$STAGED_SNAPSHOT_DIR"
    else
        printf '%s\n' "dispatch-claude: staged snapshot export failed; validation commands run in the original repo" >> "$GIT_DIFF_STDERR"
    fi
else
    printf '%s\n' "(not a git worktree)" > "$DIFF_FILE"
fi

{
    printf '%s\n' "Backend note for Auto-terminal Claude reviewer:"
    printf '%s\n' "- The dispatch wrapper saves your final answer to ${EXPECTED_REVIEW_FILE}; do not write that review file yourself."
    printf '%s\n' "- You have unattended tool permission. Run relevant tests, experiments, benchmarks, shell commands, and network verification needed to support the review."
    printf '%s\n' "- The current working directory is a disposable staged snapshot when git export succeeds: ${VALIDATION_DIR}"
    printf '%s\n' "- You may create or modify generated files inside the disposable snapshot as part of verification."
    printf '%s\n' "- Do not commit, push, publish, alter external systems, or perform destructive cleanup."
    printf '%s\n' "- Return the complete review as your final answer, starting with the required Round marker."
    printf '\n%s\n\n' "Original review prompt:"
    cat "$PROMPT_FILE"
    printf '\n\n%s\n\n' "Dispatcher-provided staged diff:"
    cat "$DIFF_FILE"
} > "$RELAY_PROMPT_FILE"
printf '%s\n' '{"mcpServers":{}}' > "$EMPTY_MCP_CONFIG_FILE"

# Run claude -p with the relay prompt fed via stdin (mirrors Codex's
# `< prompt-file` shape, avoiding ARG_MAX traps on long prompts). Claude gets
# all built-in tools under bypassPermissions. The wrapper handles the review
# file write. Claude runs in a disposable staged snapshot when git export
# succeeds, so experiments can execute without touching the source checkout.
# No `--sandbox` flag (that is Codex-only).
#
# `--bare` is OPT-IN via CLAUDE_DISPATCH_BARE=1. Claude Code 2.1.153 documents
# bare mode as API-key/apiKeyHelper auth only: OAuth and keychain auth are
# disabled when --bare is set. Defaulting to --bare would break the typical
# subscription user. Set CLAUDE_DISPATCH_BARE=1 only in environments that
# provide ANTHROPIC_API_KEY or an explicit apiKeyHelper.
#
# Array expansion uses the `${arr[@]+"${arr[@]}"}` idiom so the empty-array
# default does not trip `set -u` on bash 3.2 (macOS system bash). bash 4.4+
# (Linux, Git Bash on Windows) tolerates empty `${arr[@]}` under nounset, but
# bash 3.2 treats it as unbound. The `+` operator expands the inner array only
# when at least one element is set; otherwise it expands to nothing.
CLAUDE_BARE_ARGS=()
if [ "${CLAUDE_DISPATCH_BARE:-}" = "1" ]; then
    CLAUDE_BARE_ARGS=(--bare)
fi

# Start stall-watch only for the full review. Bash redirects both output streams
# to files as Claude runs, so tail growth is a live liveness signal.
SCRIPT_DIR="${IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR:-$(dirname -- "$0")}"
STALL_WATCH="${SCRIPT_DIR}/stall-watch.sh"
STALL_WATCH_PID=""
if [ -x "$STALL_WATCH" ]; then
    "$STALL_WATCH" --state-dir "$STATE_DIR" --parent-pid $$ >/dev/null 2>&1 &
    STALL_WATCH_PID=$!
fi
unset IMPLEMENT_REVIEW_DISPATCH_REEXEC IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR

(
    cd "$VALIDATION_DIR" || exit 2
    GIT_PAGER=cat "$CLAUDE_CMD" -p \
        --permission-mode bypassPermissions \
        --tools default \
        --add-dir "$VALIDATION_DIR" \
        --setting-sources project,local \
        --strict-mcp-config --mcp-config "$EMPTY_MCP_CONFIG_FILE" \
        ${CLAUDE_BARE_ARGS[@]+"${CLAUDE_BARE_ARGS[@]}"} \
        --output-format text \
        < "$RELAY_PROMPT_FILE" > "$STATE_DIR/tail" 2> "$STATE_DIR/tail.stderr-tmp"
)
CLAUDE_EXIT=$?

# Claude text mode has no structured terminal-result frame. Keep publication
# behind process success, then validate a same-directory candidate and rename it
# into place so auto-watch can never mistake a partial file for a finished one.
if [ "$CLAUDE_EXIT" -eq 0 ] && [ "$STRICT_CHECKS" -eq 1 ] &&
   grep -Eqi 'not logged in|please run .?login|authentication (failed|required)|unauthorized|invalid api key|api error|stream (disconnected|closed) before completion|econn(reset|refused)|etimedout|socket hang up|connection (reset|refused|timed out)|rate limit|overloaded_error' \
       "$STATE_DIR/tail.stderr-tmp" 2>/dev/null; then
    printf '%s\n' 'CLAUDE-TRANSPORT-FAILURE stderr matched a terminal auth/transport pattern' \
        > "$STATE_DIR/transport-failure"
    echo "dispatch-claude: Claude reported an authentication or transport failure; review rejected." >&2
    CLAUDE_EXIT=70
fi

if [ "$CLAUDE_EXIT" -eq 0 ]; then
    MARKER="<!-- Round ${ROUND} -->"
    REVIEW_DIR=$(dirname -- "$EXPECTED_REVIEW_FILE")
    REVIEW_BASE=$(basename -- "$EXPECTED_REVIEW_FILE")
    REVIEW_CANDIDATE="${REVIEW_DIR}/.${REVIEW_BASE}.dispatch-claude-$$-${NONCE}.tmp"
    REVIEW_FAILURE=""

    if [ ! -s "$STATE_DIR/tail" ]; then
        REVIEW_FAILURE="Claude exited 0 but produced no review output"
    elif awk -v marker="$MARKER" '
        { sub(/\r$/, "") }
        found || $0 == marker { found = 1; print }
        END { if (!found) exit 1 }
    ' "$STATE_DIR/tail" > "$REVIEW_CANDIDATE"; then
        :
    else
        {
            printf '%s\n\n' "$MARKER"
            sed 's/\r$//' "$STATE_DIR/tail"
        } > "$REVIEW_CANDIDATE"
    fi

    if [ -z "$REVIEW_FAILURE" ]; then
        REVIEW_SIZE=$(wc -c < "$REVIEW_CANDIDATE" 2>/dev/null || printf '0')
        REVIEW_FIRST_LINE=$(head -n 1 "$REVIEW_CANDIDATE" 2>/dev/null | tr -d '\r')
        if [ "$REVIEW_FIRST_LINE" != "$MARKER" ]; then
            REVIEW_FAILURE="normalized review lacks the current round marker"
        elif [ "$STRICT_CHECKS" -eq 1 ] && [ "$REVIEW_SIZE" -lt 500 ]; then
            REVIEW_FAILURE="normalized review is only ${REVIEW_SIZE} bytes"
        fi
    fi

    if [ -n "$REVIEW_FAILURE" ]; then
        rm -f -- "$REVIEW_CANDIDATE"
        echo "dispatch-claude: $REVIEW_FAILURE; review rejected." >&2
        CLAUDE_EXIT=70
    elif ! mv -f -- "$REVIEW_CANDIDATE" "$EXPECTED_REVIEW_FILE"; then
        rm -f -- "$REVIEW_CANDIDATE"
        echo "dispatch-claude: failed to atomically publish expected review file: $EXPECTED_REVIEW_FILE" >&2
        CLAUDE_EXIT=2
    fi
fi

# Pipe last 80 lines of stdout/stderr tails to stderr for caller visibility
tail -n 80 "$STATE_DIR/tail" >&2 2>/dev/null || true
tail -n 80 "$STATE_DIR/tail.stderr-tmp" >&2 2>/dev/null || true

# Do NOT force-kill stall-watch. It polls our PID via `kill -0` and exits
# silently on its next interval after we die.

exit "$CLAUDE_EXIT"
