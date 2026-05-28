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

set -u

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

# Launch stall-watch in background if present (shared with the Codex/Copilot backends)
STALL_WATCH="$(dirname -- "$0")/stall-watch.sh"
STALL_WATCH_PID=""
if [ -x "$STALL_WATCH" ]; then
    "$STALL_WATCH" --state-dir "$STATE_DIR" --parent-pid $$ >/dev/null 2>&1 &
    STALL_WATCH_PID=$!
fi

# Resolve claude binary -----------------------------------------------------
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
if command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    CLAUDE_CMD="$CLAUDE_BIN"
else
    # Not resolvable; let the invocation surface its own error to tail.
    CLAUDE_CMD="$CLAUDE_BIN"
fi

# Run claude -p with prompt fed via stdin (mirrors Codex's `< prompt-file` shape,
# avoiding ARG_MAX traps on long prompts). The narrow allow-list is path-scoped
# to Review-Claude-Code.md so the unattended subprocess cannot write any other
# file. GIT_PAGER=cat keeps claude's own `git diff` from stalling on a pager.
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

REPO="$(pwd)"
GIT_PAGER=cat "$CLAUDE_CMD" -p \
    --permission-mode dontAsk \
    --allowedTools "Read,Write(/Review-Claude-Code.md),Edit(/Review-Claude-Code.md)" \
    --add-dir "$REPO" \
    ${CLAUDE_BARE_ARGS[@]+"${CLAUDE_BARE_ARGS[@]}"} \
    --output-format text \
    < "$PROMPT_FILE" > "$STATE_DIR/tail" 2>&1
CLAUDE_EXIT=$?

# Pipe last 80 lines of tail to stderr for caller visibility
tail -n 80 "$STATE_DIR/tail" >&2 2>/dev/null || true

# Do NOT force-kill stall-watch. It polls our PID via `kill -0` and exits
# silently on its next interval after we die.

exit "$CLAUDE_EXIT"
