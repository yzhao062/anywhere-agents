#!/usr/bin/env bash
# dispatch-codex.sh -- Auto-terminal channel dispatch for implement-review skill.
# See skills/implement-review/SKILL.md > Phase 1c Auto-terminal path for the contract.
#
# Args (named):
#   --prompt-file <path>           Path to file containing the review prompt
#   --round <N>                    Round number (positive integer)
#   --expected-review-file <name>  Review file the reviewer is expected to write
#                                  (resolved relative to cwd for pre-mtime snapshot)
#
# Env:
#   CODEX_BIN                      Codex binary name or path (default: codex)
#   TMPDIR                         Temp dir for state-dir (default: /tmp)
#
# Stdout:
#   First (and only) machine-readable line: STATE-DIR <abs-path>
#
# Stderr:
#   Dispatch diagnostics + last 80 lines of codex-exec combined stdout+stderr
#
# Exit code:
#   Propagates codex exec's exit code unchanged.
#   Returns 2 on usage errors (missing/invalid args).

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
            echo "dispatch-codex: unknown argument: $1" >&2
            echo "Usage: dispatch-codex.sh --prompt-file <path> --round <N> --expected-review-file <name>" >&2
            exit 2 ;;
    esac
done

if [ -z "$PROMPT_FILE" ] || [ -z "$ROUND" ] || [ -z "$EXPECTED_REVIEW_FILE" ]; then
    echo "dispatch-codex: missing required argument" >&2
    echo "Usage: dispatch-codex.sh --prompt-file <path> --round <N> --expected-review-file <name>" >&2
    exit 2
fi

if [ ! -f "$PROMPT_FILE" ]; then
    echo "dispatch-codex: prompt file not found: $PROMPT_FILE" >&2
    exit 2
fi

case "$ROUND" in
    ''|*[!0-9]*)
        echo "dispatch-codex: --round must be a positive integer, got: $ROUND" >&2
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

STATE_DIR="${TMP_BASE}/implement-review-codex-${REPO_HASH}-round${ROUND}-$$-${NONCE}"
mkdir -p "$STATE_DIR" || {
    echo "dispatch-codex: failed to create state-dir: $STATE_DIR" >&2
    exit 2
}

# Record pre-dispatch mtime of any existing Review-Codex.md (Unix epoch seconds)
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

# Launch stall-watch in background if present (B2 will land the script)
STALL_WATCH="$(dirname -- "$0")/stall-watch.sh"
STALL_WATCH_PID=""
if [ -x "$STALL_WATCH" ]; then
    "$STALL_WATCH" --state-dir "$STATE_DIR" --parent-pid $$ >/dev/null 2>&1 &
    STALL_WATCH_PID=$!
fi

# Run codex exec with prompt via stdin (NOT positional arg; not codex exec review)
CODEX_BIN="${CODEX_BIN:-codex}"
"$CODEX_BIN" exec - < "$PROMPT_FILE" > "$STATE_DIR/tail" 2>&1
CODEX_EXIT=$?

# Pipe last 80 lines of tail to stderr for caller visibility
tail -n 80 "$STATE_DIR/tail" >&2 2>/dev/null || true

# Do NOT force-kill stall-watch. It already polls our PID via `kill -0` and
# will exit silently on its next interval after we die. Stopping it on the hot
# path can erase a stall period that crossed the threshold during the final
# poll window, leaving Phase 2.0 Health check 9 with no record.
# The cost is one extra polling interval of lingering observer; the benefit is
# preserving the Check 9 signal that justified stall-watch's existence.

exit "$CODEX_EXIT"
