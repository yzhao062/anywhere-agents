#!/usr/bin/env bash
# Auto-watch -- implement-review Phase 1d watcher (Bash variant).
#
# Polls every 5s until a Review-<reviewer>.md file lands that satisfies
# all three trigger conditions, then emits ``DONE <abs-path>`` on stdout
# and exits 0. Times out after 60 minutes by default with ``TIMEOUT``
# and exit 2.
#
# Stdout starts with WATCH-START and ends with one terminal line. A confirmed
# automatic retry adds one visible AUTO-REDISPATCH line between them:
#   WATCH-START round=<N> reviewers=<csv> timeout=<seconds>s
#   AUTO-REDISPATCH attempt=<N>/<total> reason=codex-response-stream-disconnected state-dir=<abs-path>
#   DONE <abs-path> | TIMEOUT | REAP-UNKNOWN <state-dir>
#     | STREAM-DEAD <state-dir> | STALL <state-dir>
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
#                       ``Codex,Antigravity``). The watcher only fires
#                       on a file whose name (after stripping ``Review-``
#                       and ``.md``) is in this set.
#
# Env:
#   AGENT_CONFIG_AUTO_WATCH_TIMEOUT  override timeout in seconds. Used by
#                                    tests; production paths use 3600.
#   AGENT_CONFIG_AUTO_WATCH_POLL     override poll interval in seconds. Used by
#                                    tests; production paths use 5.
#   IMPLEMENT_REVIEW_STATE_DIR       absolute path to the state directory the
#                                    dispatcher already printed. When set and
#                                    present, it is watched directly and the
#                                    age-based discovery below is skipped, so
#                                    the two processes agree by handoff rather
#                                    than by clock. Discovery remains the
#                                    fallback for a caller that has no path.
#   IMPLEMENT_REVIEW_STREAM_RETRY_LIMIT
#                                    maximum automatic redispatches after a
#                                    confirmed stream death (default 1; 0
#                                    disables). The dispatcher records the
#                                    effective value in the state directory.
#
# Exit codes: 0 = DONE, 2 = TIMEOUT or REAP-UNKNOWN, 3 = dispatch failure/stall
# surfaced. Both exit-2 lines are checkpoints for the orchestrating agent, which
# resolves the round with await-review.py rather than declaring a failure.
# A retry keeps the original state directory so the dispatcher's single
# STATE-DIR line stays valid. Failed-attempt files are archived under
# <state-dir>/attempt-N before the same logical dispatch starts again.

set -eu

# Release the deployed path before either session can refresh the shared
# scripts. Exec preserves the caller-visible background PID. A janitor waits
# for that PID to exit, then removes the temporary copy and directory.
if [ "${IMPLEMENT_REVIEW_AUTO_WATCH_REEXEC:-}" != "1" ]; then
  REEXEC_TMP_BASE="${TMPDIR:-/tmp}"
  REEXEC_TMP_BASE="${REEXEC_TMP_BASE%/}"
  REEXEC_DIR="${REEXEC_TMP_BASE}/implement-review-auto-watch-reexec-$$"
  REEXEC_COPY="${REEXEC_DIR}/auto-watch.sh"

  umask 077
  if ! mkdir "$REEXEC_DIR"; then
    echo "auto-watch: failed to create re-exec dir: $REEXEC_DIR" >&2
    exit 2
  fi
  if ! cp -- "$0" "$REEXEC_COPY"; then
    rmdir -- "$REEXEC_DIR" 2>/dev/null || true
    echo "auto-watch: failed to create re-exec copy: $REEXEC_COPY" >&2
    exit 2
  fi
  chmod u+x "$REEXEC_COPY" 2>/dev/null || true

  export IMPLEMENT_REVIEW_AUTO_WATCH_REEXEC=1
  export IMPLEMENT_REVIEW_AUTO_WATCH_SOURCE_DIR
  IMPLEMENT_REVIEW_AUTO_WATCH_SOURCE_DIR=$(dirname -- "$0")

  "${BASH:-bash}" -c '
    watched_pid=$1
    reexec_copy=$2
    reexec_dir=$3
    while kill -0 "$watched_pid" 2>/dev/null; do sleep 1; done
    rm -f -- "$reexec_copy"
    rmdir -- "$reexec_dir" 2>/dev/null || true
  ' auto-watch-cleanup "$$" "$REEXEC_COPY" "$REEXEC_DIR" >/dev/null 2>&1 &

  # Let a failed exec reach the cleanup below. Bash exits a noninteractive
  # shell on exec failure by default, so the diagnostic and the removal of
  # the private directory were unreachable. A successful exec replaces this
  # shell, so neither option change reaches normal execution.
  set +e
  shopt -s execfail

  exec "${BASH:-bash}" "$REEXEC_COPY" "$@"

  echo "auto-watch: failed to launch re-exec copy: $REEXEC_COPY" >&2
  rm -f -- "$REEXEC_COPY"
  rmdir -- "$REEXEC_DIR" 2>/dev/null || true
  exit 2
fi

SCRIPT_DIR="${IMPLEMENT_REVIEW_AUTO_WATCH_SOURCE_DIR:-$(dirname -- "$0")}"
unset IMPLEMENT_REVIEW_AUTO_WATCH_REEXEC IMPLEMENT_REVIEW_AUTO_WATCH_SOURCE_DIR

FILE_GLOB="${1:?usage: auto-watch.sh FILE_GLOB ROUND EXPECTED_REVIEWERS}"
ROUND="${2:?usage: auto-watch.sh FILE_GLOB ROUND EXPECTED_REVIEWERS}"
REVIEWERS="${3:?usage: auto-watch.sh FILE_GLOB ROUND EXPECTED_REVIEWERS}"
TIMEOUT="${AGENT_CONFIG_AUTO_WATCH_TIMEOUT:-3600}"
POLL="${AGENT_CONFIG_AUTO_WATCH_POLL:-5}"
STABLE_WINDOW=10
# Grace for the dispatcher's stream-death handoff, shared with await-review.
REAP_GRACE=30

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
STATEFILE="$(mktemp -t auto-watch-state.XXXXXX 2>/dev/null || mktemp)"
RETRYFILE="$(mktemp -t auto-watch-retry.XXXXXX 2>/dev/null || mktemp)"
trap 'rm -f "$SNAPFILE" "$STATEFILE" "$RETRYFILE"' EXIT INT TERM
for f in $FILE_GLOB; do
  [ -e "$f" ] || continue
  printf '%s\t%s\n' "$f" "$(_mtime "$f")" >> "$SNAPFILE"
done

_pre_mtime() {
  awk -F'\t' -v f="$1" '$1==f{print $2; exit}' "$SNAPFILE" 2>/dev/null
}

_state_counter() {
  local path="$1" value
  [ -f "$path" ] || return 1
  value="$(head -n 1 "$path" 2>/dev/null | tr -d '[:space:]')"
  case "$value" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s\n' "$value"
}

printf 'WATCH-START round=%s reviewers=%s timeout=%ss\n' \
  "$ROUND" "$REVIEWERS" "$TIMEOUT"

start_epoch="$(date +%s)"

# Auto-terminal dispatch creates its state directory immediately before this
# watcher starts. The dispatcher prints that path, so a caller that has it can
# hand it over directly and both processes agree without consulting a clock.
if [ -n "${IMPLEMENT_REVIEW_STATE_DIR:-}" ] && \
   [ -d "${IMPLEMENT_REVIEW_STATE_DIR}" ]; then
  printf '%s\n' "$IMPLEMENT_REVIEW_STATE_DIR" >> "$STATEFILE"
else
  # Otherwise discover it: capture only same-repo/same-round directories
  # started within 30 seconds, so old rounds and parallel work in other
  # repositories cannot trigger a failure signal. Terminal-relay captures none.
  repo_hash=""
  if command -v sha256sum >/dev/null 2>&1; then
    repo_hash="$(pwd | sha256sum 2>/dev/null | cut -c1-8)"
  elif command -v shasum >/dev/null 2>&1; then
    repo_hash="$(pwd | shasum -a 256 2>/dev/null | cut -c1-8)"
  fi
  tmp_base="${TMPDIR:-/tmp}"
  tmp_base="${tmp_base%/}"
  if [ -n "$repo_hash" ]; then
    for state_dir in "$tmp_base"/implement-review-*-${repo_hash}-round${ROUND}-*; do
      [ -d "$state_dir" ] || continue
      [ -f "$state_dir/timestamp" ] || continue
      state_start="$(head -n 1 "$state_dir/timestamp" 2>/dev/null | tr -d '[:space:]')"
      case "$state_start" in ''|*[!0-9]*) continue ;; esac
      if [ "$state_start" -ge $((start_epoch - 30)) ] && \
         [ "$state_start" -le $((start_epoch + 5)) ]; then
        printf '%s\n' "$state_dir" >> "$STATEFILE"
      fi
    done
  fi
fi

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

  while IFS= read -r state_dir; do
    [ -n "$state_dir" ] || continue
    retry_limit="$(_state_counter "$state_dir/stream-retry-limit" || true)"
    retry_count="$(_state_counter "$state_dir/stream-retry-count" || true)"
    if [ -n "$retry_limit" ] && [ -n "$retry_count" ] && \
       [ "$retry_count" -gt 0 ]; then
      retry_key="${state_dir}\t${retry_count}"
      if ! grep -Fqx -- "$retry_key" "$RETRYFILE" 2>/dev/null; then
        printf '%s\n' "$retry_key" >> "$RETRYFILE"
        printf 'AUTO-REDISPATCH attempt=%s/%s reason=codex-response-stream-disconnected state-dir=%s\n' \
          "$((retry_count + 1))" "$((retry_limit + 1))" "$state_dir"
      fi
    fi
    if [ -f "$state_dir/stream-death" ]; then
      # A retry-capable dispatcher archives the failed attempt and clears the
      # request before incrementing stream-retry-count. Give that handoff
      # REAP_GRACE seconds; while it runs, neither branch below has anything
      # final to say. await-review.py suppresses the same two states.
      if [ -f "$state_dir/stream-retry-request" ] && \
         [ -n "$retry_limit" ] && [ -n "$retry_count" ] && \
         [ "$retry_count" -lt "$retry_limit" ]; then
        request_mtime="$(_mtime "$state_dir/stream-retry-request")"
        request_age=$((now - request_mtime))
        if [ "$request_mtime" -gt 0 ] && [ "$request_age" -lt "$REAP_GRACE" ]; then
          continue
        fi
      fi
      # The death marker is written before the reap runs, so on its own it does
      # not say the worker tree is gone. Phase 2 turns STREAM-DEAD into a
      # runtime failure and a sticky downgrade, which is too heavy a
      # consequence to hang on a half-finished handshake.
      if [ ! -f "$state_dir/stream-reap-complete" ]; then
        # The stall watcher can lose that write and never retry it. Waiting on
        # a marker that is no longer coming would hold the round to its full
        # timeout, so report what is known and let the agent resolve the round.
        death_mtime="$(_mtime "$state_dir/stream-death")"
        if [ "$death_mtime" -gt 0 ] && \
           [ $((now - death_mtime)) -gt "$REAP_GRACE" ]; then
          printf 'REAP-UNKNOWN %s\n' "$state_dir"
          exit 2
        fi
        continue
      fi
      printf 'STREAM-DEAD %s\n' "$state_dir"
      exit 3
    fi
    if [ -f "$state_dir/stall-warning" ]; then
      printf 'STALL %s\n' "$state_dir"
      exit 3
    fi
  done < "$STATEFILE"

  sleep "$POLL"
done
