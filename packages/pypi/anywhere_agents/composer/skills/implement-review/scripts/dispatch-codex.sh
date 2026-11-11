#!/usr/bin/env bash
# dispatch-codex.sh -- Auto-terminal channel dispatch for implement-review skill.
# See skills/implement-review/SKILL.md > Phase 1c Auto-terminal path for the contract.
#
# Self-review guard: refuses to dispatch when the invoking orchestrator is
# Codex itself (would be self-review, which the fungibility principle
# disallows). See the env-check block below.
#
# Args (named):
#   --prompt-file <path>           Path to file containing the review prompt
#   --round <N>                    Round number (positive integer)
#   --expected-review-file <name>  Review file the reviewer is expected to write
#                                  (resolved relative to cwd for pre-mtime snapshot)
#
# Env:
#   CODEX_BIN                      Codex binary name or path (default: codex)
#   IMPLEMENT_REVIEW_ORCHESTRATOR  Who is driving this dispatch: claude / codex / user.
#                                  When 'codex' (case-insensitive), refuses to
#                                  dispatch with exit 2. When unset/empty AND
#                                  either CODEX_THREAD_ID or CODEX_SESSION_ID is
#                                  set to a non-empty value, also refuses. Any
#                                  other value (claude / user) proceeds.
#   CODEX_THREAD_ID                Set by Codex in its tool subprocesses (observed
#                                  with codex-cli 0.156.1). Fall-through guard
#                                  signal; child processes inherit it.
#   CODEX_SESSION_ID               Same as CODEX_THREAD_ID, for the session ID.
#   ANYWHERE_AGENTS_PYTHON         Explicit Python interpreter override
#   CODEX_DISPATCH_MODEL           One-run override of the reviewer model.
#   CODEX_DISPATCH_REASONING       One-run override of the reviewer effort.
#   AGENT_MODELS_FILE              Alternate registry file (tests, one-off runs).
#   TMPDIR                         Temp dir for state-dir (default: /tmp)
#
# Model and effort: the registry (user/agent-models.json, read through
# scripts/agent_models.py) names both; see the isolation note below for the
# order of sources. <state-dir>/review-model records what was requested, the
# source of each, and what the codex header reports after the run.
#
# Stdout:
#   First (and only) machine-readable line: STATE-DIR <abs-path>
#
# Stderr:
#   Dispatch diagnostics + last 80 lines of codex-exec combined stdout+stderr.
#   MODEL-MISMATCH when the run did not use the requested model or effort.
#
# Exit code:
#   Propagates codex exec's exit code unchanged.
#   Returns 2 on usage errors (missing/invalid args) or self-review refusal.
#   Returns 70 when no source names the reviewer model and effort.

set -u

# Windows cannot replace a shell script while bash has it open. The review
# child may still invoke the consumer bootstrap despite the dispatch-specific
# instruction below, so release the deployed path before doing any work. A
# command-string parent waits for the copied script, removes it after the child
# exits, and propagates the child's status. The sentinel prevents re-exec loops.
if [ "${IMPLEMENT_REVIEW_DISPATCH_REEXEC:-}" != "1" ]; then
    REEXEC_TMP_BASE="${TMPDIR:-/tmp}"
    REEXEC_TMP_BASE="${REEXEC_TMP_BASE%/}"
    REEXEC_DIR="${REEXEC_TMP_BASE}/implement-review-dispatch-codex-reexec-$$"
    REEXEC_COPY="${REEXEC_DIR}/dispatch-codex.sh"

    umask 077
    if ! mkdir "$REEXEC_DIR"; then
        echo "dispatch-codex: failed to create re-exec dir: $REEXEC_DIR" >&2
        exit 2
    fi
    if ! cp -- "$0" "$REEXEC_COPY"; then
        rmdir -- "$REEXEC_DIR" 2>/dev/null || true
        echo "dispatch-codex: failed to create re-exec copy: $REEXEC_COPY" >&2
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
    ' dispatch-codex-reexec "$REEXEC_COPY" "$@"

    echo "dispatch-codex: failed to launch re-exec copy: $REEXEC_COPY" >&2
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

# Self-review guard (orchestrator detection) --------------------------------
# Refuse to dispatch when ANY of these holds:
#   - IMPLEMENT_REVIEW_ORCHESTRATOR=codex (case-insensitive), OR
#   - IMPLEMENT_REVIEW_ORCHESTRATOR is unset/empty AND either CODEX_THREAD_ID
#     or CODEX_SESSION_ID is set to a non-empty value.
# IMPLEMENT_REVIEW_ORCHESTRATOR=claude / user proceed regardless of
# CODEX_THREAD_ID / CODEX_SESSION_ID.
ORCH_RAW="${IMPLEMENT_REVIEW_ORCHESTRATOR:-}"
ORCH_LC=$(printf '%s' "$ORCH_RAW" | tr '[:upper:]' '[:lower:]')
if [ "$ORCH_LC" = "codex" ]; then
    echo "dispatch-codex: refusing to dispatch (orchestrator=codex; self-review)" >&2
    exit 2
fi
if [ -z "$ORCH_LC" ] && { [ -n "${CODEX_THREAD_ID:-}" ] || [ -n "${CODEX_SESSION_ID:-}" ]; }; then
    echo "dispatch-codex: refusing to dispatch (orchestrator=codex; self-review)" >&2
    exit 2
fi

# Resolve a Python interpreter for the reviewer before spending a Codex round.
# A path/name check alone is insufficient on Windows: App Execution Aliases are
# discoverable zero-length reparse points. Each candidate must execute isolated
# code, report an absolute sys.executable, and pass a second probe through that
# reported executable. The nonempty-file and WindowsApps checks are additional
# guards, not substitutes for execution.
probe_python_candidate() {
    [ "$#" -gt 0 ] || return 1
    case "$1" in
        *WindowsApps*|*windowsapps*) return 1 ;;
    esac

    local probe_output resolved resolved_real
    probe_output=$("$@" -I -c 'import os, sys; print("IMPLEMENT_REVIEW_PYTHON=" + sys.executable); print("IMPLEMENT_REVIEW_REALPATH=" + os.path.realpath(sys.executable))' 2>/dev/null) || return 1
    resolved=$(printf '%s\n' "$probe_output" | sed -n 's/^IMPLEMENT_REVIEW_PYTHON=//p' | tail -n 1 | tr -d '\r')
    resolved_real=$(printf '%s\n' "$probe_output" | sed -n 's/^IMPLEMENT_REVIEW_REALPATH=//p' | tail -n 1 | tr -d '\r')
    [ -n "$resolved" ] || return 1
    [ -n "$resolved_real" ] || return 1
    case "$resolved" in
        /*|[A-Za-z]:[\\/]*|\\\\*) ;;
        *) return 1 ;;
    esac
    case "$resolved" in
        *WindowsApps*|*windowsapps*) return 1 ;;
    esac
    # Canonicalization is only an alias-location check. Keep the selected
    # sys.executable unchanged so POSIX venv symlinks retain their environment.
    case "$resolved_real" in
        *WindowsApps*|*windowsapps*) return 1 ;;
    esac
    [ -s "$resolved" ] || return 1
    "$resolved" -I -c 'import sys; sys.exit(0)' >/dev/null 2>&1 || return 1
    printf '%s\n' "$resolved"
}

try_python_path() {
    local candidate=$1 resolved
    [ -n "$candidate" ] || return 1
    [ -f "$candidate" ] || return 1
    resolved=$(probe_python_candidate "$candidate") || return 1
    PYTHON_BIN=$resolved
    return 0
}

PYTHON_BIN=""

# 1. Explicit shared override. A configured but broken override is an error;
# silently falling through would hide a machine configuration defect.
if [ -n "${ANYWHERE_AGENTS_PYTHON:-}" ]; then
    if ! PYTHON_BIN=$(probe_python_candidate "$ANYWHERE_AGENTS_PYTHON"); then
        echo "dispatch-codex: ANYWHERE_AGENTS_PYTHON is not an executable Python interpreter: $ANYWHERE_AGENTS_PYTHON" >&2
        exit 2
    fi
fi

# 2. Project virtual environments, then an activated virtual environment.
if [ -z "$PYTHON_BIN" ]; then
    for candidate in \
        "$PWD/.venv/Scripts/python.exe" "$PWD/.venv/bin/python" \
        "$PWD/venv/Scripts/python.exe" "$PWD/venv/bin/python"; do
        try_python_path "$candidate" && break
    done
fi
if [ -z "$PYTHON_BIN" ] && [ -n "${VIRTUAL_ENV:-}" ]; then
    for candidate in \
        "$VIRTUAL_ENV/Scripts/python.exe" "$VIRTUAL_ENV/bin/python"; do
        try_python_path "$candidate" && break
    done
fi

# 3. Active conda environment, then discovered Miniforge/conda roots and envs.
CONDA_ROOTS=()
if [ -z "$PYTHON_BIN" ] && [ -n "${CONDA_PREFIX:-}" ]; then
    for candidate in \
        "$CONDA_PREFIX/python.exe" "$CONDA_PREFIX/bin/python" \
        "$CONDA_PREFIX/bin/python3"; do
        try_python_path "$candidate" && break
    done
    case "$CONDA_PREFIX" in
        */envs/*) CONDA_ROOTS+=("${CONDA_PREFIX%/envs/*}") ;;
        *) CONDA_ROOTS+=("$CONDA_PREFIX") ;;
    esac
fi
[ -n "${CONDA_ROOT:-}" ] && CONDA_ROOTS+=("$CONDA_ROOT")
[ -n "${MAMBA_ROOT_PREFIX:-}" ] && CONDA_ROOTS+=("$MAMBA_ROOT_PREFIX")
for manager in conda mamba; do
    manager_path=$(command -v "$manager" 2>/dev/null || true)
    [ -n "$manager_path" ] && CONDA_ROOTS+=("$(dirname -- "$(dirname -- "$manager_path")")")
done
if [ -n "${HOME:-}" ]; then
    for home_child in "$HOME"/*/; do
        [ -d "${home_child}conda-meta" ] && CONDA_ROOTS+=("${home_child%/}")
    done
fi
if [ -z "$PYTHON_BIN" ]; then
    for root in ${CONDA_ROOTS[@]+"${CONDA_ROOTS[@]}"}; do
        [ -n "$root" ] || continue
        for candidate in "$root/python.exe" "$root/bin/python" "$root/bin/python3"; do
            try_python_path "$candidate" && break 2
        done
        for env_dir in "$root"/envs/*/; do
            [ -d "$env_dir" ] || continue
            for candidate in \
                "${env_dir}python.exe" "${env_dir}bin/python" \
                "${env_dir}bin/python3"; do
                try_python_path "$candidate" && break 3
            done
        done
    done
fi

# 4. Windows Python launcher. Resolve it to sys.executable so the reviewer gets
# an absolute interpreter path rather than a launcher command.
if [ -z "$PYTHON_BIN" ]; then
    while IFS= read -r candidate; do
        [ -n "$candidate" ] || continue
        if PYTHON_BIN=$(probe_python_candidate "$candidate" -3); then
            break
        fi
    done < <(type -a -p py 2>/dev/null || true)
fi

# 5. Every PATH entry, not only the first alias hit. Execution probing remains
# authoritative; the path filter only avoids launching a known Store redirector.
if [ -z "$PYTHON_BIN" ]; then
    for command_name in python3 python; do
        while IFS= read -r candidate; do
            [ -n "$candidate" ] || continue
            if PYTHON_BIN=$(probe_python_candidate "$candidate"); then
                break 2
            fi
        done < <(type -a -p "$command_name" 2>/dev/null || true)
    done
fi

# Not finding an interpreter is a degradation, not a dispatch failure. The
# probe exists to keep a reviewer from claiming verification it never ran, and
# health check 10 already enforces that independently by refusing a PASS or
# BLOCK verdict without a VERIFIED status. Reviews also run on repositories
# whose verification is not Python at all (LaTeX, Node, docs), so a missing
# interpreter must not make those repositories unreviewable.
if [ -z "$PYTHON_BIN" ]; then
    echo "dispatch-codex: no working Python interpreter found (checked ANYWHERE_AGENTS_PYTHON, project virtualenvs, conda/Miniforge, py -3, and non-WindowsApps PATH entries); dispatching without a pre-resolved interpreter" >&2
fi

# Probe pwsh structurally because a Store execution alias succeeds here but can
# fail only from Codex's spawn context, so executing it would prove nothing.
# Unlike the Python probe, this performs only PATH lookups and file stat checks.
PWSH_BIN=""
PWSH_REJECTED=""
while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    case "$candidate" in
        *WindowsApps*|*windowsapps*)
            [ -n "$PWSH_REJECTED" ] || PWSH_REJECTED=$candidate
            continue ;;
    esac
    if [ ! -s "$candidate" ]; then
        [ -n "$PWSH_REJECTED" ] || PWSH_REJECTED=$candidate
        continue
    fi
    PWSH_BIN=$candidate
    break
done < <(type -a -p pwsh 2>/dev/null || true)
if [ -z "$PWSH_BIN" ]; then
    echo "dispatch-codex: no usable pwsh found${PWSH_REJECTED:+ (rejected Store-alias or zero-length candidate: $PWSH_REJECTED)}; the reviewer will be told to avoid pwsh" >&2
fi

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

# One logical dispatch may retry only a confirmed response-stream death. The
# state directory stays stable so the single STATE-DIR stdout contract remains
# valid; failed attempts are archived under attempt-N.
STREAM_RETRY_LIMIT="${IMPLEMENT_REVIEW_STREAM_RETRY_LIMIT:-1}"
case "$STREAM_RETRY_LIMIT" in
    ''|*[!0-9]*) STREAM_RETRY_LIMIT=1 ;;
esac
STREAM_RETRY_LIMIT=$((10#$STREAM_RETRY_LIMIT))
STREAM_RETRY_COUNT=0
printf '%s\n' "$STREAM_RETRY_LIMIT" > "$STATE_DIR/stream-retry-limit"
printf '0\n' > "$STATE_DIR/stream-retry-count"

# Record exactly what was passed to the reviewer for caller diagnostics.
# Absence of the marker means the probe resolved nothing. Writing a sentinel
# instead would let a reader mistake it for a path.
if [ -n "$PYTHON_BIN" ]; then
    printf '%s\n' "$PYTHON_BIN" > "$STATE_DIR/python-interpreter"
fi
if [ -n "$PWSH_BIN" ]; then
    printf '%s\n' "$PWSH_BIN" > "$STATE_DIR/pwsh-interpreter"
fi

# Emit STATE-DIR on stdout (first and only machine-readable line)
printf 'STATE-DIR %s\n' "$STATE_DIR"

# Launch stall-watch in background if present (B2 will land the script)
SCRIPT_DIR="${IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR:-$(dirname -- "$0")}"
STALL_WATCH="${SCRIPT_DIR}/stall-watch.sh"
STALL_WATCH_PID=""
_start_stall_watch() {
    STALL_WATCH_PID=""
    if [ -x "$STALL_WATCH" ]; then
        "$STALL_WATCH" --state-dir "$STATE_DIR" --parent-pid $$ >/dev/null 2>&1 &
        STALL_WATCH_PID=$!
    fi
}
_start_stall_watch
unset IMPLEMENT_REVIEW_DISPATCH_REEXEC IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR

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
# drops the user's MCP servers, plugins, and hooks; auth still uses
# CODEX_HOME. It also drops the configured model, leaving codex's built-in
# recommended one (server-chosen, so it can change under us), and reasoning
# effort drops to "none". Re-pass -c model and -c model_reasoning_effort to
# keep reviews on the chosen model and effort.
# The registry (user/agent-models.json, read through scripts/agent_models.py)
# names both, and no literal lives here: one would outlive a registry change.
# The model comes from, in order: CODEX_DISPATCH_MODEL (e.g. for an account
# without the usual model); the registry; the top-level `model` in
# $CODEX_HOME/config.toml (default ~/.codex), read back because
# --ignore-user-config drops it; then failure. The effort follows the same
# order with CODEX_DISPATCH_REASONING, the registry, and the top-level
# `model_reasoning_effort`. config.toml is the third source because bootstrap
# writes the registry's values into it. A config value is taken only when
# quoted and made of [A-Za-z0-9._:/-], so a stray config line cannot reach the
# -c argument or the .ps1's generated cmd helper; CODEX_DISPATCH_MODEL and
# CODEX_DISPATCH_REASONING are the operator's own values and pass as given.
# A config model is skipped when a top-level model_provider other than openai
# is set: isolation drops that provider, and its model names (ollama's
# name:tag, a gateway's vendor/model) would reach the default provider. The
# parser runs under LC_ALL=C so every awk strips a UTF-8 BOM byte-wise.
# <state-dir>/review-model records the model, the effort, and the source of
# each; after the run it also holds the model and effort the codex header
# reports (`agent_models.py actual`), and MODEL-MISMATCH reaches stderr when
# they differ from the request. With isolation off nothing is passed, codex
# reads its own config, and the record holds only the actual values, checked
# against the registry when it answers.
# What is NOT re-passed: service_tier and any custom model_provider / base_url.
# service_tier is left to codex's default on purpose -- hardcoding the maintainer's "fast" tier would make every round
# fail for a consumer whose account lacks it. A review that genuinely
# needs a custom provider or a specific tier should set
# CODEX_DISPATCH_ISOLATE_MCP=off; full config-preserving
# isolation (a temp CODEX_HOME holding a copy of config.toml minus the MCP
# tables) is the documented follow-up. Isolation args go before the trailing
# `-` so stdin stays the final positional. The `off` sentinel matches the
# dispatch-codex.ps1 opt-out (case-insensitive) for cross-shell parity. Note:
# the .ps1 copy of this note is kept short on purpose -- a longer comment
# beside its cmdBody construction trips a Windows-AV heuristic (Bitdefender
# AMSI parse block), so the full rationale lives here, not there.
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
REGISTRY_MODEL=""
REGISTRY_EFFORT=""
REGISTRY_WHY="scripts/agent_models.py or a working Python interpreter was not found"
if [ -n "$PYTHON_BIN" ] && [ -n "$AGENT_MODELS_PY" ] && [ -f "$AGENT_MODELS_PY" ]; then
    REGISTRY_WHY="the resolver did not answer"
    if REGISTRY_ROW=$("$PYTHON_BIN" "$AGENT_MODELS_PY" resolve --family codex --role reviewer 2> "$STATE_DIR/registry-error"); then
        read -r REGISTRY_MODEL REGISTRY_EFFORT <<< "$REGISTRY_ROW"
    fi
    if [ -s "$STATE_DIR/registry-error" ]; then
        REGISTRY_WHY=$(tr '\n' ' ' < "$STATE_DIR/registry-error")
    else
        rm -f -- "$STATE_DIR/registry-error"
    fi
fi
case "$REGISTRY_MODEL" in ''|*[!A-Za-z0-9._:/-]*) REGISTRY_MODEL="" ;; esac
case "$REGISTRY_EFFORT" in ''|*[!A-Za-z0-9._:/-]*) REGISTRY_EFFORT="" ;; esac
CODEX_ISOLATE_ARGS=()
CODEX_EXPECT_ARGS=()
if [ "$(printf '%s' "${CODEX_DISPATCH_ISOLATE_MCP:-}" | tr '[:upper:]' '[:lower:]')" != "off" ]; then
    CODEX_REVIEW_MODEL=""
    CODEX_REVIEW_SOURCE=""
    CODEX_REVIEW_EFFORT=""
    CODEX_EFFORT_SOURCE=""
    if [ -n "${CODEX_DISPATCH_MODEL:-}" ]; then
        CODEX_REVIEW_MODEL="$CODEX_DISPATCH_MODEL"
        CODEX_REVIEW_SOURCE="env"
    elif [ -n "$REGISTRY_MODEL" ]; then
        CODEX_REVIEW_MODEL="$REGISTRY_MODEL"
        CODEX_REVIEW_SOURCE="registry"
    fi
    if [ -n "${CODEX_DISPATCH_REASONING:-}" ]; then
        CODEX_REVIEW_EFFORT="$CODEX_DISPATCH_REASONING"
        CODEX_EFFORT_SOURCE="env"
    elif [ -n "$REGISTRY_EFFORT" ]; then
        CODEX_REVIEW_EFFORT="$REGISTRY_EFFORT"
        CODEX_EFFORT_SOURCE="registry"
    fi
    if [ -z "$CODEX_REVIEW_MODEL" ] || [ -z "$CODEX_REVIEW_EFFORT" ]; then
        CODEX_HOME_DIR="${CODEX_HOME:-${HOME:-}/.codex}"
        CODEX_CONFIG_FILE="$CODEX_HOME_DIR/config.toml"
        CODEX_CONFIG_ROWS=""
        if [ -f "$CODEX_CONFIG_FILE" ] && [ -r "$CODEX_CONFIG_FILE" ]; then
            CODEX_CONFIG_ROWS=$(LC_ALL=C awk '
NR == 1 { sub(/^\357\273\277/, "") }
/^[[:space:]]*\[/ { exit }
/^[[:space:]]*(model|model_provider|model_reasoning_effort)[[:space:]]*=[[:space:]]*["\047]/ {
    key = $0
    sub(/^[[:space:]]*/, "", key)
    sub(/[[:space:]]*=.*/, "", key)
    line = $0
    sub(/^[^=]*=[[:space:]]*/, "", line)
    q = substr(line, 1, 1)
    sub(/^./, "", line)
    idx = index(line, q)
    if (idx > 0) {
        val = substr(line, 1, idx - 1)
        if (key == "model_provider") {
            provider = val
        } else if (key == "model_reasoning_effort") {
            if (effort == "" && val ~ /^[A-Za-z0-9._:\/-]+$/) effort = val
        } else if (model == "" && val ~ /^[A-Za-z0-9._:\/-]+$/) {
            model = val
        }
    }
}
END {
    if (provider != "" && provider != "openai") model = ""
    print model
    print effort
}
' "$CODEX_CONFIG_FILE" 2>/dev/null || true)
        fi
        CONFIG_MODEL=$(printf '%s\n' "$CODEX_CONFIG_ROWS" | sed -n 1p)
        CONFIG_EFFORT=$(printf '%s\n' "$CODEX_CONFIG_ROWS" | sed -n 2p)
        if [ -z "$CODEX_REVIEW_MODEL" ] && [ -n "$CONFIG_MODEL" ]; then
            CODEX_REVIEW_MODEL="$CONFIG_MODEL"
            CODEX_REVIEW_SOURCE="config"
        fi
        if [ -z "$CODEX_REVIEW_EFFORT" ] && [ -n "$CONFIG_EFFORT" ]; then
            CODEX_REVIEW_EFFORT="$CONFIG_EFFORT"
            CODEX_EFFORT_SOURCE="config"
        fi
    fi
    if [ -z "$CODEX_REVIEW_MODEL" ] || [ -z "$CODEX_REVIEW_EFFORT" ]; then
        printf '%s\n' \
            "dispatch-codex: cannot determine the reviewer model and effort. The registry (user/agent-models.json, resolved by scripts/agent_models.py) did not answer (${REGISTRY_WHY}), and the top-level model and model_reasoning_effort of ${CODEX_CONFIG_FILE} do not supply the rest." \
            "Fix the registry or the Python setup (see the agent-models skill), or set CODEX_DISPATCH_MODEL and CODEX_DISPATCH_REASONING for this run. Refusing to spend a review round." \
            > "$STATE_DIR/tail"
        cat "$STATE_DIR/tail" >&2
        exit 70
    fi
    printf 'model=%s source=%s effort=%s effort_source=%s\n' \
        "$CODEX_REVIEW_MODEL" "$CODEX_REVIEW_SOURCE" "$CODEX_REVIEW_EFFORT" "$CODEX_EFFORT_SOURCE" > "$STATE_DIR/review-model"
    CODEX_ISOLATE_ARGS=(--ignore-user-config -c "model=$CODEX_REVIEW_MODEL" -c "model_reasoning_effort=$CODEX_REVIEW_EFFORT")
    CODEX_EXPECT_ARGS=(--expect-model "$CODEX_REVIEW_MODEL" --expect-effort "$CODEX_REVIEW_EFFORT")
else
    printf 'model=- source=user-config effort=- effort_source=user-config\n' > "$STATE_DIR/review-model"
    if [ -n "$REGISTRY_MODEL" ] && [ -n "$REGISTRY_EFFORT" ]; then
        CODEX_EXPECT_ARGS=(--expect-model "$REGISTRY_MODEL" --expect-effort "$REGISTRY_EFFORT")
    fi
fi
# The parent session already refreshed the consumer repo at startup. Repeating
# that setup inside every review child adds latency and can mutate the deployed
# dispatcher mid-run. This developer instruction outranks AGENTS.md's generic
# new-session bootstrap rule while preserving every other project instruction.
if [ -n "$PYTHON_BIN" ]; then
    PYTHON_INSTRUCTION="A working Python interpreter was execution-probed before dispatch. Use this exact absolute path for every Python command: $PYTHON_BIN. Do not invoke bare python, python3, or py."
else
    PYTHON_INSTRUCTION="No Python interpreter could be pre-resolved on this machine. If a verification command needs Python, locate a working interpreter yourself first; if none exists, verify by whatever means this repository actually uses."
fi
if [ -n "$PWSH_BIN" ]; then
    PWSH_INSTRUCTION="PowerShell 7 was resolved at this absolute path: $PWSH_BIN. If a command needs it, invoke that exact path rather than bare pwsh."
else
    PWSH_INSTRUCTION="No usable PowerShell 7 was found on this machine; a bare pwsh call may be a Microsoft Store execution alias that fails instantly under this spawn context and retries forever. Do not shell out to pwsh. Use bash, Windows PowerShell 5.1 (powershell.exe), or plain git commands instead."
fi

CHILD_SESSION_INSTRUCTIONS="The parent agent session already completed the repository bootstrap at startup. Skip bootstrap and shared configuration refresh commands in this child review session. Skip the session-start banner as well: no human reads a dispatched child's terminal, so rendering it only spends shell spawns. Use the shared configuration currently on disk. This session is an assigned reviewer. Its task, lens, and response format are already supplied. Skip router dispatch and coordinator-workflow discovery at all skill lookup locations (skills/implement-review/, .claude/skills/implement-review/, .agent-config/repo/skills/implement-review/, .agents/skills/implement-review/). Do not load coordinator skill files or example reviews to discover the workflow or response format. Files under review remain readable. A targeted read outside the review scope is permitted when it supplies missing context, resolves truncation, or answers a concrete verification question. Apply the substantive project instructions supplied in context, including applicable local overrides. Read an applicable instruction file once only when its rules have not been supplied. For a staged-change review, obtain the selected diff once with the command in the request; if the request embeds that diff, use it and do not obtain another copy. For a plan review, the named plan and evidence files are the review input: read each once from the repository or from the supplied copy, as the request directs. Do not read the same content twice. $PYTHON_INSTRUCTION $PWSH_INSTRUCTION Before issuing a PASS or BLOCK commit verdict, execute relevant verification commands. In $EXPECTED_REVIEW_FILE, add one standalone line exactly 'Verification status: VERIFIED' if at least one relevant verification command completed, otherwise add 'Verification status: UNVERIFIED'. If the status is UNVERIFIED, write 'Commit verdict: UNVERIFIED'; never issue PASS or BLOCK. Verification notes must list the exact commands and outcomes."
# Codex injects at most project_doc_max_bytes of discovered instruction files
# (AGENTS.md and the like) and silently truncates the rest; the default is
# 32 KiB, which a consumer's composed AGENTS.md exceeds once the passive packs
# are in. Raise the budget here, outside the isolation branch, so it reaches
# the reviewer whether or not --ignore-user-config drops the user's config.
# 256 KiB is a ceiling for stacked packs plus a large local file, not a size
# to write toward.
CODEX_CHILD_ARGS=(-c "developer_instructions=$CHILD_SESSION_INSTRUCTIONS" -c project_doc_max_bytes=262144)
_archive_stream_attempt() {
    local attempt_number="$1" attempt_dir name
    local active_files="tail tail.stderr-tmp stall-warning stream-death stream-retry-request stream-reap-complete worker-roots reap-targets reap-reason reap-worker.cmd"
    attempt_dir="$STATE_DIR/attempt-$attempt_number"
    mkdir "$attempt_dir" 2>/dev/null || return 1

    for name in pre-mtime timestamp stream-retry-limit stream-retry-count; do
        cp -- "$STATE_DIR/$name" "$attempt_dir/$name" 2>/dev/null || return 1
    done
    for name in $active_files; do
        [ -f "$STATE_DIR/$name" ] || continue
        cp -- "$STATE_DIR/$name" "$attempt_dir/$name" 2>/dev/null || return 1
    done
    for name in tail stream-death stream-retry-request stream-reap-complete; do
        [ -f "$attempt_dir/$name" ] || return 1
    done
    for name in $active_files; do
        rm -f -- "$STATE_DIR/$name"
    done
    return 0
}

while :; do
    "$CODEX_BIN" exec --sandbox "$CODEX_DISPATCH_SANDBOX" ${CODEX_ISOLATE_ARGS[@]+"${CODEX_ISOLATE_ARGS[@]}"} ${CODEX_CHILD_ARGS[@]+"${CODEX_CHILD_ARGS[@]}"} - < "$PROMPT_FILE" > "$STATE_DIR/tail" 2>&1
    CODEX_EXIT=$?

    if [ ! -f "$STATE_DIR/stream-death" ] || \
       [ ! -f "$STATE_DIR/stream-retry-request" ] || \
       [ "$STREAM_RETRY_COUNT" -ge "$STREAM_RETRY_LIMIT" ]; then
        break
    fi

    handoff_wait=0
    while [ ! -f "$STATE_DIR/stream-reap-complete" ] && \
          [ "$handoff_wait" -lt 10 ]; do
        sleep 1
        handoff_wait=$((handoff_wait + 1))
    done
    if [ ! -f "$STATE_DIR/stream-reap-complete" ]; then
        printf 'AUTO-REDISPATCH-SKIPPED reason=stream-reap-handoff-timeout state-dir=%s\n' \
            "$STATE_DIR" >&2
        break
    fi
    if [ -n "$STALL_WATCH_PID" ]; then
        wait "$STALL_WATCH_PID" 2>/dev/null || true
    fi
    if ! _archive_stream_attempt "$((STREAM_RETRY_COUNT + 1))"; then
        printf 'AUTO-REDISPATCH-SKIPPED reason=attempt-archive-failed state-dir=%s\n' \
            "$STATE_DIR" >&2
        break
    fi

    STREAM_RETRY_COUNT=$((STREAM_RETRY_COUNT + 1))
    printf '%s\n' "$STREAM_RETRY_COUNT" > "$STATE_DIR/stream-retry-count"
    date +%s > "$STATE_DIR/timestamp"
    printf 'AUTO-REDISPATCH attempt=%s/%s reason=codex-response-stream-disconnected state-dir=%s\n' \
        "$((STREAM_RETRY_COUNT + 1))" "$((STREAM_RETRY_LIMIT + 1))" "$STATE_DIR" >&2
    _start_stall_watch
done

# Pipe last 80 lines of tail to stderr for caller visibility
tail -n 80 "$STATE_DIR/tail" >&2 2>/dev/null || true

# Effective, not configured: the header codex prints (`model:` and `reasoning
# effort:`) is in the combined tail. Append what it says to the record; a
# mismatch also prints MODEL-MISMATCH on stderr (from the resolver), and a
# header that cannot be read prints MODEL-UNVERIFIED.
ACTUAL_LINE=""
if [ -n "$PYTHON_BIN" ] && [ -n "$AGENT_MODELS_PY" ] && [ -f "$AGENT_MODELS_PY" ]; then
    ACTUAL_LINE=$("$PYTHON_BIN" "$AGENT_MODELS_PY" actual --family codex --stderr-file "$STATE_DIR/tail" \
        ${CODEX_EXPECT_ARGS[@]+"${CODEX_EXPECT_ARGS[@]}"}) || ACTUAL_LINE=""
fi
if [ -z "$ACTUAL_LINE" ]; then
    ACTUAL_LINE="actual=unknown status=unknown"
    echo "dispatch-codex: MODEL-UNVERIFIED the run could not be read back (no Python interpreter or resolver)" >&2
fi
printf '%s\n' "$ACTUAL_LINE" >> "$STATE_DIR/review-model"

# Do NOT force-kill stall-watch. It checks our PID and captured process-start
# identity, then exits silently on its next interval after we die. Stopping it
# on the hot path can erase a stall period that crossed the threshold during
# the final poll window, leaving Phase 2.0 Health check 9 with no record.
# The cost is one extra polling interval of lingering observer; the benefit is
# preserving the Check 9 signal that justified stall-watch's existence.

exit "$CODEX_EXIT"
