#!/usr/bin/env bash
# Auto-watch -- implement-review Phase 1d watcher (Bash variant).
#
# Polls every 5s until a Review-<reviewer>.md file lands that satisfies
# all three trigger conditions, then emits ``DONE <abs-path>`` on stdout
# and exits 0. Times out after 60 minutes by default with ``TIMEOUT``
# and exit 2.
#
# Stdout schema is exactly two lines:
#   WATCH-START round=<N> reviewers=<csv> timeout=<seconds>s
#   DONE <abs-path>     |     TIMEOUT
#
# Usage:
#   auto-watch.sh FILE_GLOB ROUND EXPECTED_REVIEWERS
#
#   FILE_GLOB           literal name or shell glob, e.g. ``Review-Codex.md``
#                       or ``Review-*.md``. Resolved relative to cwd.
#   ROUND               integer N. The first line of a candidate file must
#                       equal ``<!-- Round N -->`` after stripping any
#                       trailing ``\r`` and whitespace.
#   EXPECTED_REVIEWERS  comma-separated normalized reviewer names from
#                       SKILL.md Phase 1c (e.g. ``Codex`` or
#                       ``Codex,GitHub-Copilot``). The watcher only fires
#                       on a file whose name (after stripping ``Review-``
#                       and ``.md``) is in this set.
#
# Env:
#   AGENT_CONFIG_AUTO_WATCH_TIMEOUT  override timeout in seconds. Used by
#                                    tests; production paths use 3600.

set -eu

FILE_GLOB="${1:?usage: auto-watch.sh FILE_GLOB ROUND EXPECTED_REVIEWERS}"
ROUND="${2:?usage: auto-watch.sh FILE_GLOB ROUND EXPECTED_REVIEWERS}"
REVIEWERS="${3:?usage: auto-watch.sh FILE_GLOB ROUND EXPECTED_REVIEWERS}"
TIMEOUT="${AGENT_CONFIG_AUTO_WATCH_TIMEOUT:-3600}"
POLL=5
STABLE_WINDOW=10

# Cross-OS stat: GNU coreutils (Linux + Git Bash MSYS) vs BSD (macOS).
if stat -c %Y . >/dev/null 2>&1; then
  _mtime() { stat -c %Y "$1" 2>/dev/null || echo 0; }
elif stat -f %m . >/dev/null 2>&1; then
  _mtime() { stat -f %m "$1" 2>/dev/null || echo 0; }
else
  printf 'auto-watch: no compatible stat\n' >&2
  exit 2
fi

# Reviewer-name membership check (POSIX-compatible; no associative arrays
# so the script runs on macOS bash 3.2 without Homebrew bash).
_in_expected() {
  case ",${REVIEWERS}," in
    *",$1,"*) return 0 ;;
    *)        return 1 ;;
  esac
}

# Snapshot existing matching files' mtimes so we only fire on a strict
# advance. Without this, a file that already exists with the matching
# round marker (e.g., user re-launched a round mid-way) would fire
# immediately, defeating the "wait for the reviewer to write" intent.
SNAPFILE="$(mktemp -t auto-watch.XXXXXX 2>/dev/null || mktemp)"
trap 'rm -f "$SNAPFILE"' EXIT INT TERM
for f in $FILE_GLOB; do
  [ -e "$f" ] || continue
  printf '%s\t%s\n' "$f" "$(_mtime "$f")" >> "$SNAPFILE"
done

_pre_mtime() {
  awk -F'\t' -v f="$1" '$1==f{print $2; exit}' "$SNAPFILE" 2>/dev/null
}

printf 'WATCH-START round=%s reviewers=%s timeout=%ss\n' \
  "$ROUND" "$REVIEWERS" "$TIMEOUT"

start_epoch="$(date +%s)"
while :; do
  now="$(date +%s)"
  if [ $((now - start_epoch)) -ge "$TIMEOUT" ]; then
    printf 'TIMEOUT\n'
    exit 2
  fi

  for f in $FILE_GLOB; do
    [ -f "$f" ] || continue

    base="$(basename "$f")"
    name="${base#Review-}"
    name="${name%.md}"
    _in_expected "$name" || continue

    cur="$(_mtime "$f")"
    pre="$(_pre_mtime "$f")"
    pre="${pre:-0}"

    # (a) mtime advanced past the watcher's startup snapshot.
    [ "$cur" -gt "$pre" ] || continue
    # (b) mtime has been quiet for STABLE_WINDOW seconds. Functionally
    # equivalent to "10s since last write" since an in-flight writer
    # keeps mtime fresh.
    [ $((now - cur)) -ge "$STABLE_WINDOW" ] || continue
    # (c) first line equals "<!-- Round N -->" after \r + trailing-
    # whitespace stripping (Windows newline tolerance).
    first="$(head -n 1 "$f" 2>/dev/null | tr -d '\r' | sed -e 's/[[:space:]]*$//')"
    [ "$first" = "<!-- Round ${ROUND} -->" ] || continue

    abs_dir="$(cd "$(dirname "$f")" 2>/dev/null && pwd -P)"
    [ -n "$abs_dir" ] || abs_dir="$(dirname "$f")"
    printf 'DONE %s/%s\n' "$abs_dir" "$(basename "$f")"
    exit 0
  done

  sleep "$POLL"
done
