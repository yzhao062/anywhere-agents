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
date +%s > "$STATE_DIR/timestamp"

# Emit STATE-DIR on stdout (first and only machine-readable line)
printf 'STATE-DIR %s\n' "$STATE_DIR"

# Launch stall-watch in background if present (shared with the Codex backend)
STALL_WATCH="$(dirname -- "$0")/stall-watch.sh"
STALL_WATCH_PID=""
if [ -x "$STALL_WATCH" ]; then
    "$STALL_WATCH" --state-dir "$STATE_DIR" --parent-pid $$ >/dev/null 2>&1 &
    STALL_WATCH_PID=$!
fi

# Resolve copilot binary, with gh-copilot fallback ------------------------
# Standalone `copilot` is preferred; if it is not resolvable, fall back to the
# built-in `gh copilot` entry point (both verified equivalent in the probe).
COPILOT_BIN="${COPILOT_BIN:-copilot}"
USE_GH=0
if command -v "$COPILOT_BIN" >/dev/null 2>&1; then
    COPILOT_CMD="$COPILOT_BIN"
else
    GH_BIN="${GH_BIN:-gh}"
    if command -v "$GH_BIN" >/dev/null 2>&1; then
        COPILOT_CMD="$GH_BIN"
        USE_GH=1
    else
        # Nothing resolvable; let the invocation surface its own error to tail.
        COPILOT_CMD="$COPILOT_BIN"
    fi
fi

# Run copilot with the prompt referenced as a file (`-p "@<prompt>"`), NOT via
# stdin and NOT as a long literal argument (a large literal -p arg fails).
# GIT_PAGER=cat keeps Copilot's own `git diff` from stalling on a pager. The
# narrow allow-list (read + write + git, scoped to the repo via --add-dir) is
# tighter than the Codex backend's danger-full-access; Copilot writes
# Review-GitHub-Copilot.md itself per the prompt's save contract.
REPO="$(pwd)"
if [ "$USE_GH" -eq 1 ]; then
    GIT_PAGER=cat "$COPILOT_CMD" copilot -C "$REPO" -p "@$PROMPT_FILE" \
        --add-dir "$REPO" \
        --allow-tool=read --allow-tool=write --allow-tool='shell(git:*)' \
        --no-ask-user --silent --stream off --no-color \
        > "$STATE_DIR/tail" 2>&1
else
    GIT_PAGER=cat "$COPILOT_CMD" -C "$REPO" -p "@$PROMPT_FILE" \
        --add-dir "$REPO" \
        --allow-tool=read --allow-tool=write --allow-tool='shell(git:*)' \
        --no-ask-user --silent --stream off --no-color \
        > "$STATE_DIR/tail" 2>&1
fi
COPILOT_EXIT=$?

# Pipe last 80 lines of tail to stderr for caller visibility
tail -n 80 "$STATE_DIR/tail" >&2 2>/dev/null || true

# Do NOT force-kill stall-watch. It polls our PID via `kill -0` and exits
# silently on its next interval after we die. Stopping it on the hot path can
# erase a stall period that crossed the threshold during the final poll window.

exit "$COPILOT_EXIT"
