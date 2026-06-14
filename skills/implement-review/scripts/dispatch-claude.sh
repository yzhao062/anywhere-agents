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
    if git checkout-index -a --prefix="$STAGED_SNAPSHOT_DIR/" >> "$GIT_DIFF_STDERR" 2>&1; then
        VALIDATION_DIR="$STAGED_SNAPSHOT_DIR"
    else
        printf '%s\n' "dispatch-claude: staged snapshot export failed; validation commands run in the original repo" >> "$GIT_DIFF_STDERR"
    fi
else
    printf '%s\n' "(not a git worktree)" > "$DIFF_FILE"
fi

{
    printf '%s\n' "Backend note for Auto-terminal Claude reviewer:"
    printf '%s\n' "- Do not use Write or Edit tools. The dispatch wrapper saves your final answer to ${EXPECTED_REVIEW_FILE}."
    printf '%s\n' "- You may run Bash verification commands, tests, grep/rg, and other read/validation tools in the current working directory."
    printf '%s\n' "- The current working directory is a disposable staged snapshot when git export succeeds: ${VALIDATION_DIR}"
    printf '%s\n' "- You may use the built-in WebSearch and WebFetch tools to verify checkable external facts (citations, links, library or API behavior, versions)."
    printf '%s\n' "- Do not run mutating, publishing, package-install, cleanup, or destructive commands, and do not make network calls through shell commands such as curl or wget; use WebFetch or WebSearch for any network lookup."
    printf '%s\n' "- Return the complete review as your final answer, starting with the required Round marker."
    printf '\n%s\n\n' "Original review prompt:"
    cat "$PROMPT_FILE"
    printf '\n\n%s\n\n' "Dispatcher-provided staged diff:"
    cat "$DIFF_FILE"
} > "$RELAY_PROMPT_FILE"
printf '%s\n' '{"mcpServers":{}}' > "$EMPTY_MCP_CONFIG_FILE"

# Run claude -p with the relay prompt fed via stdin (mirrors Codex's
# `< prompt-file` shape, avoiding ARG_MAX traps on long prompts). Claude gets
# Read+Bash only; the wrapper handles the single review-file write. Claude runs
# in a disposable staged snapshot when git export succeeds, so verification
# tools can execute without touching the source checkout. No `--sandbox` flag
# (that is Codex-only).
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

(
    cd "$VALIDATION_DIR" || exit 2
    GIT_PAGER=cat "$CLAUDE_CMD" -p \
        --permission-mode bypassPermissions \
        --tools "Read,Bash,WebSearch,WebFetch" \
        --add-dir "$VALIDATION_DIR" \
        --setting-sources project,local \
        --strict-mcp-config --mcp-config "$EMPTY_MCP_CONFIG_FILE" \
        ${CLAUDE_BARE_ARGS[@]+"${CLAUDE_BARE_ARGS[@]}"} \
        --output-format text \
        < "$RELAY_PROMPT_FILE" > "$STATE_DIR/tail" 2> "$STATE_DIR/tail.stderr-tmp"
)
CLAUDE_EXIT=$?

# Save Claude's final answer to the expected review file. Stderr stays in the
# state-dir for diagnostics and is not copied into the review body.
if [ "$CLAUDE_EXIT" -eq 0 ] && [ -s "$STATE_DIR/tail" ]; then
    MARKER="<!-- Round ${ROUND} -->"
    NORMALIZED_REVIEW="$STATE_DIR/review-normalized"
    if grep -Fq "$MARKER" "$STATE_DIR/tail"; then
        awk -v marker="$MARKER" 'found || $0 == marker { found = 1; print }' \
            "$STATE_DIR/tail" > "$NORMALIZED_REVIEW"
    else
        {
            printf '%s\n\n' "$MARKER"
            cat "$STATE_DIR/tail"
        } > "$NORMALIZED_REVIEW"
    fi
    if ! cp "$NORMALIZED_REVIEW" "$EXPECTED_REVIEW_FILE"; then
        echo "dispatch-claude: failed to write expected review file: $EXPECTED_REVIEW_FILE" >&2
        CLAUDE_EXIT=2
    fi
fi

# Pipe last 80 lines of stdout/stderr tails to stderr for caller visibility
tail -n 80 "$STATE_DIR/tail" >&2 2>/dev/null || true
tail -n 80 "$STATE_DIR/tail.stderr-tmp" >&2 2>/dev/null || true

# Do NOT force-kill stall-watch. It polls our PID via `kill -0` and exits
# silently on its next interval after we die.

exit "$CLAUDE_EXIT"
