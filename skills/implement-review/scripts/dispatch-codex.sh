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

# Run codex exec with prompt via stdin (NOT positional arg; not codex exec review).
#
# --sandbox danger-full-access aligns Auto-terminal's trust model with
# Terminal-relay: the user invoked /implement-review on their own machine
# and Codex has the same fs / network / shell access it would have in an
# interactive Codex terminal. This also sidesteps Codex's workspace-write
# sandbox CreateProcessAsUserW failed: 1312 bug on Windows 0.130.0, where
# Codex's own shell runner could not spawn git / grep / pwsh subprocesses
# so the review came back as "could not access files". Scope discipline
# (review-only, save to Review-Codex.md, no commits / pushes) is enforced
# at the prompt level, identical to Terminal-relay. For CI / shared
# environments where this trust posture is too broad, override via
# CODEX_DISPATCH_SANDBOX (default: danger-full-access).
CODEX_BIN="${CODEX_BIN:-codex}"
CODEX_DISPATCH_SANDBOX="${CODEX_DISPATCH_SANDBOX:-danger-full-access}"

# Reviewer isolation (default on; set CODEX_DISPATCH_ISOLATE_MCP=off to opt out).
# A user-level Codex MCP server must not auto-start inside the headless
# reviewer. Live repro (agent-config#1): a configured MCP server (node_repl)
# auto-started under `codex exec`, spawned a nested codex.exe, looped on
# "ERROR: Reconnecting... N/5", and hung 15+ minutes with an empty review --
# the same recursion the Claude reviewer backend already guards against with
# --strict-mcp-config. `--ignore-user-config` is the only reliable stop: the
# narrower `-c mcp_servers={}` is deep-merged by codex 0.139 and leaves the
# configured servers running (verified live -- node_repl still spawned). It
# drops the user's MCP servers, plugins, and hooks; the model still defaults to
# codex's built-in recommended model and auth still uses
# CODEX_HOME, but reasoning effort drops to "none", so re-pass it via
# -c model_reasoning_effort (default xhigh; override CODEX_DISPATCH_REASONING)
# to avoid a silent reviewer downgrade. What is NOT re-passed: service_tier and
# any custom model_provider / base_url. service_tier is left to codex's default
# on purpose -- hardcoding the maintainer's "fast" tier would make every round
# fail for a consumer whose account lacks it, and Codex's built-in model
# default already matches the common recommended config. A review that genuinely
# needs a custom provider or a specific tier should set
# CODEX_DISPATCH_ISOLATE_MCP=off; full config-preserving
# isolation (a temp CODEX_HOME holding a copy of config.toml minus the MCP
# tables) is the documented follow-up. Isolation args go before the trailing
# `-` so stdin stays the final positional. The `off` sentinel matches the
# dispatch-codex.ps1 opt-out (case-insensitive) for cross-shell parity. Note:
# the .ps1 copy of this note is kept short on purpose -- a longer comment
# beside its cmdBody construction trips a Windows-AV heuristic (Bitdefender
# AMSI parse block), so the full rationale lives here, not there.
CODEX_DISPATCH_REASONING="${CODEX_DISPATCH_REASONING:-xhigh}"
CODEX_ISOLATE_ARGS=(--ignore-user-config -c "model_reasoning_effort=$CODEX_DISPATCH_REASONING")
if [ "$(printf '%s' "${CODEX_DISPATCH_ISOLATE_MCP:-}" | tr '[:upper:]' '[:lower:]')" = "off" ]; then
    CODEX_ISOLATE_ARGS=()
fi
"$CODEX_BIN" exec --sandbox "$CODEX_DISPATCH_SANDBOX" ${CODEX_ISOLATE_ARGS[@]+"${CODEX_ISOLATE_ARGS[@]}"} - < "$PROMPT_FILE" > "$STATE_DIR/tail" 2>&1
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
