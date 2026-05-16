#!/usr/bin/env bash
# health-check.sh -- POSIX wrapper that delegates to health-check.py.
# See skills/implement-review/SKILL.md > Phase 2.0 prologue for the contract.
# All real logic lives in health-check.py to keep the pattern list / regex /
# code-span exclusion in one place (avoids drift between sh and ps1).

set -u

SCRIPT_DIR="$(cd "$(dirname -- "$0")" && pwd)"
PY_HELPER="$SCRIPT_DIR/health-check.py"

if [ ! -f "$PY_HELPER" ]; then
    echo "health-check: helper script missing: $PY_HELPER" >&2
    exit 2
fi

# Prefer python3 (Linux/Mac convention); fall back to python.
if command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "health-check: neither python3 nor python found on PATH" >&2
    exit 2
fi

exec "$PY" "$PY_HELPER" "$@"
