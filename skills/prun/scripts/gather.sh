#!/usr/bin/env bash
# gather.sh -- prun result collector (Bash variant).
# Generalized from implement-review/scripts/auto-watch.sh.
#
# Polls a fixed list of result files every 5s and emits `DONE <abs-path>` or
# `FALLBACK <abs-path>` (when line 1 matches `^# [A-Za-z0-9_-]+ result \(FALLBACK, `)
# for each as it lands: it exists, is non-empty, and has been quiet for the
# stable window (so a file still being written does not fire early). Exits 0
# when every file landed without a FALLBACK, 3 when every file landed and at
# least one was a FALLBACK, or 2 on timeout / usage error.
#
# Unlike auto-watch, gather does NOT require an mtime advance past a startup
# snapshot. prun gives each unit a FRESH result path per run (the caller removes
# any stale file before dispatch), so a unit that finished BEFORE gather started
# must fire immediately rather than wait for a further write. Firing on
# exists + non-empty + stable handles both the fast-unit and slow-unit cases.
#
# Avoids bash-4 associative arrays so it runs on macOS bash 3.2; tracks done by
# index so duplicate paths in the list are handled.
#
# Usage:
#   gather.sh <result-file> [<result-file> ...]
#
# Env:
#   AGENT_CONFIG_GATHER_TIMEOUT  override timeout in seconds (default 3600)
#   PRUN_GATHER_POLL             poll interval seconds (default 5)
#   PRUN_GATHER_STABLE_WINDOW    quiet window seconds before firing (default 10)
#
# Stdout (schema):
#   GATHER-START count=<N> timeout=<seconds>s
#   DONE <abs-path>          (one line per non-FALLBACK file, as it lands)
#   FALLBACK <abs-path>      (one line per FALLBACK file, as it lands)
#   TIMEOUT remaining=<k>    (if the timeout hits before all land)
#
# Exit:
#   0  every file landed and none was a FALLBACK
#   3  every file landed and at least one was a FALLBACK
#   2  timeout or usage error

set -eu

# This script can run for an hour, and a shell holds a script open for as long
# as it is executing it. On Windows that refuses any rename over the deployed
# path and aborts a compose transaction (#43). Hand off to a private temp copy
# so the deployed path is free. A command-string parent removes the copy and
# propagates the status.
# Nothing here resolves a sibling relative to $0, so no source dir is handed on.
if [ "${PRUN_GATHER_REEXEC:-}" != "1" ]; then
  REEXEC_TMP_BASE="${TMPDIR:-/tmp}"
  REEXEC_TMP_BASE="${REEXEC_TMP_BASE%/}"
  REEXEC_DIR="${REEXEC_TMP_BASE}/prun-gather-reexec-$$"
  REEXEC_COPY="${REEXEC_DIR}/gather.sh"

  umask 077
  if ! mkdir "$REEXEC_DIR"; then
    echo "gather: failed to create re-exec dir: $REEXEC_DIR" >&2
    exit 2
  fi
  if ! cp -- "$0" "$REEXEC_COPY"; then
    rmdir -- "$REEXEC_DIR" 2>/dev/null || true
    echo "gather: failed to create re-exec copy: $REEXEC_COPY" >&2
    exit 2
  fi
  chmod u+x "$REEXEC_COPY" 2>/dev/null || true

  export PRUN_GATHER_REEXEC=1

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
  ' gather-reexec "$REEXEC_COPY" "$@"

  echo "gather: failed to launch re-exec copy: $REEXEC_COPY" >&2
  rm -f -- "$REEXEC_COPY"
  rmdir -- "$REEXEC_DIR" 2>/dev/null || true
  exit 2
fi

unset PRUN_GATHER_REEXEC

[ $# -ge 1 ] || { echo "usage: gather.sh <result-file> [<result-file> ...]" >&2; exit 2; }

TIMEOUT="${AGENT_CONFIG_GATHER_TIMEOUT:-3600}"
POLL="${PRUN_GATHER_POLL:-5}"
STABLE_WINDOW="${PRUN_GATHER_STABLE_WINDOW:-10}"

FILES=("$@")
N=${#FILES[@]}

# Cross-OS stat: GNU coreutils (Linux + Git Bash MSYS) vs BSD (macOS).
if stat -c %Y . >/dev/null 2>&1; then
  _mtime() { stat -c %Y "$1" 2>/dev/null || echo 0; }
elif stat -f %m . >/dev/null 2>&1; then
  _mtime() { stat -f %m "$1" 2>/dev/null || echo 0; }
else
  printf 'gather: no compatible stat\n' >&2
  exit 2
fi

# Per-file done flags (indexed array; bash 3.2 safe).
DONE=()
i=0
while [ "$i" -lt "$N" ]; do DONE[$i]=0; i=$((i + 1)); done

printf 'GATHER-START count=%s timeout=%ss\n' "$N" "$TIMEOUT"

start_epoch="$(date +%s)"
remaining="$N"
has_fallback=0
while [ "$remaining" -gt 0 ]; do
  now="$(date +%s)"
  if [ $((now - start_epoch)) -ge "$TIMEOUT" ]; then
    printf 'TIMEOUT remaining=%s\n' "$remaining"
    exit 2
  fi

  i=0
  while [ "$i" -lt "$N" ]; do
    # -s: exists AND non-empty. Skips empty touches and not-yet-written files.
    if [ "${DONE[$i]}" != "1" ] && [ -s "${FILES[$i]}" ]; then
      f="${FILES[$i]}"
      cur="$(_mtime "$f")"
      if [ $((now - cur)) -ge "$STABLE_WINDOW" ]; then
        abs_dir="$(cd "$(dirname "$f")" 2>/dev/null && pwd -P)"
        [ -n "$abs_dir" ] || abs_dir="$(dirname "$f")"
        # FALLBACK only when line 1 matches the FALLBACK header (anchored, case-sensitive).
        if head -n 1 "$f" 2>/dev/null | grep -Eq '^# [A-Za-z0-9_-]+ result \(FALLBACK, '; then
          printf 'FALLBACK %s/%s\n' "$abs_dir" "$(basename "$f")"
          has_fallback=1
        else
          printf 'DONE %s/%s\n' "$abs_dir" "$(basename "$f")"
        fi
        DONE[$i]=1
        remaining=$((remaining - 1))
      fi
    fi
    i=$((i + 1))
  done

  [ "$remaining" -gt 0 ] && sleep "$POLL"
done

if [ "$has_fallback" -eq 1 ]; then
  exit 3
fi
exit 0
