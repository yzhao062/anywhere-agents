#!/usr/bin/env bash
# gather.sh -- prun result collector (Bash variant).
# Generalized from implement-review/scripts/auto-watch.sh.
#
# Polls a fixed list of result files every 5s and emits `DONE <abs-path>` for
# each as it lands: it exists, is non-empty, and has been quiet for the stable
# window (so a file still being written does not fire early). Exits 0 once every
# file has landed, or 2 on timeout.
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
#   DONE <abs-path>          (one line per file, as it lands)
#   TIMEOUT remaining=<k>    (if the timeout hits before all land)

set -eu

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
        printf 'DONE %s/%s\n' "$abs_dir" "$(basename "$f")"
        DONE[$i]=1
        remaining=$((remaining - 1))
      fi
    fi
    i=$((i + 1))
  done

  [ "$remaining" -gt 0 ] && sleep "$POLL"
done

exit 0
