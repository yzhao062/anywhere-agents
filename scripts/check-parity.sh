#!/usr/bin/env bash
# scripts/check-parity.sh - Maintainer-only.
#
# Compares shared-core files between agent-config (this repo) and
# anywhere-agents (expected sibling clone). Replaces the manual "check 5"
# eyeball sweep in anywhere-agents/RELEASING.md before release cuts, and
# catches drift as it accumulates between releases.
#
# Two categories:
#
#   STRICT      must be byte-identical between ac and aa. Any difference
#               is drift and fails the check. Covers: _python (Python
#               wrapper that finds a real interpreter and avoids the
#               Windows Store shim), guard.py, session_bootstrap.py,
#               generate_agent_configs.py, pre-push-smoke.sh,
#               remote-smoke.sh, check-parity.sh (this script - both
#               sides carry an identical copy so the maintainer can run
#               it from either repo), .claude/settings.json,
#               .githooks/pre-push, .github/workflows/real-agent-smoke.yml,
#               .github/workflows/validate.yml,
#               .claude/commands/*.md for each of the 4 shipped skills,
#               skills/{implement-review,ci-mockup-figure,readme-polish}
#               as recursive trees.
#
#   BY-DESIGN   expected to differ (sanitized mirror). Must still exist
#               on both sides; a missing file fails the check because the
#               release gate needs the mirror to be present, just with
#               different contents. Reports a +/- line delta per file so
#               unusual drift is visible. A byte-for-byte match is a
#               warning (sanitization may have been skipped during
#               backport). Covers: AGENTS.md (USC / Overleaf / PyCharm
#               stripping), bootstrap/bootstrap.sh and .ps1
#               (default-upstream + CRLF-config stripping),
#               user/settings.json (additionalDirectories stripping),
#               skills/my-router (routing-table rewrite with extension
#               guidance for forks).
#
# Usage:
#   bash scripts/check-parity.sh                           # default sibling path
#   bash scripts/check-parity.sh /path/to/anywhere-agents  # explicit
#
# Exit 0: STRICT clean and every BY-DESIGN mirror present. By-design
#         summary shown for eyeball.
# Exit 1: STRICT drift, or a required BY-DESIGN mirror missing. Fix
#         before tagging.
# Exit 2: usage error (anywhere-agents clone not found).

set -uo pipefail

SCRIPT_DIR="$( cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd )"
AC_ROOT="$(dirname "$SCRIPT_DIR")"
AA_ROOT="${1:-$AC_ROOT/../anywhere-agents}"

if [ ! -d "$AA_ROOT" ]; then
  printf 'error: anywhere-agents clone not found at %s\n' "$AA_ROOT" >&2
  printf 'usage: %s [/path/to/anywhere-agents]\n' "$0" >&2
  exit 2
fi

exit_code=0

fail() {
  printf '  DRIFT: %s\n' "$1"
  exit_code=1
}

# ---- STRICT: byte-identical top-level files ----
printf '\n== strict byte-identical ==\n'
strict_files=(
  scripts/_python
  scripts/guard.py
  scripts/session_bootstrap.py
  scripts/generate_agent_configs.py
  scripts/pre-push-smoke.sh
  scripts/remote-smoke.sh
  scripts/check-parity.sh
  .claude/settings.json
  .githooks/pre-push
  .github/workflows/real-agent-smoke.yml
  .github/workflows/validate.yml
)
for f in "${strict_files[@]}"; do
  if [ ! -f "$AC_ROOT/$f" ] || [ ! -f "$AA_ROOT/$f" ]; then
    fail "$f (missing on one side)"
    continue
  fi
  if ! diff -q "$AC_ROOT/$f" "$AA_ROOT/$f" >/dev/null 2>&1; then
    fail "$f"
  fi
done

# ---- (v0.4.0) shipped .claude/commands pointers dropped from STRICT ----
# Since aa v0.4.0, the 4 shipped pointer files (implement-review,
# my-router, ci-mockup-figure, readme-polish) are pack-emitted outputs
# of scripts/packs/handlers/skill.py (via the kind: skill dispatch),
# not aa-core source files requiring byte-identical parity with ac.
# The pointers still exist in both trees for the PyYAML-missing fallback
# path in bootstrap, but STRICT byte-identity is no longer enforced
# here per pack-architecture.md § "STRICT parity trajectory" (v0.4.0
# row drops these four entries). See
# docs/anywhere-agents.md mirror-policy table for the updated status.

# ---- STRICT: shared skills (recursive; my-router excluded - BY-DESIGN) ----
printf '\n== shared skills (recursive byte-identical) ==\n'
for skill in implement-review ci-mockup-figure readme-polish; do
  if [ ! -d "$AC_ROOT/skills/$skill" ] || [ ! -d "$AA_ROOT/skills/$skill" ]; then
    fail "skills/$skill/ (missing on one side)"
    continue
  fi
  if ! diff -rq "$AC_ROOT/skills/$skill" "$AA_ROOT/skills/$skill" >/dev/null 2>&1; then
    fail "skills/$skill/"
    diff -rq "$AC_ROOT/skills/$skill" "$AA_ROOT/skills/$skill" 2>&1 | sed 's/^/    /'
  fi
done

# ---- BY-DESIGN: files expected to differ (summary only; not blocking unless missing) ----
printf '\n== expected to differ by design (summary; eyeball if delta is unusual) ==\n'
by_design_files=(
  AGENTS.md
  bootstrap/bootstrap.sh
  bootstrap/bootstrap.ps1
  user/settings.json
)
for f in "${by_design_files[@]}"; do
  if [ ! -f "$AC_ROOT/$f" ] || [ ! -f "$AA_ROOT/$f" ]; then
    fail "$f (missing on one side; expected sanitized mirror)"
    continue
  fi
  if diff -q "$AC_ROOT/$f" "$AA_ROOT/$f" >/dev/null 2>&1; then
    printf '  WARN: %s matches byte-for-byte (expected to differ; sanitization may have been skipped)\n' "$f"
  else
    # Plain `diff` emits changed lines with `<` (only in first arg = ac) and
    # `>` (only in second arg = aa). Count each prefix to summarize direction.
    raw_diff=$(diff "$AC_ROOT/$f" "$AA_ROOT/$f")
    in_aa=$(printf '%s\n' "$raw_diff" | grep -c '^>' || true)
    in_ac=$(printf '%s\n' "$raw_diff" | grep -c '^<' || true)
    printf '  differs: %s (+%d lines only in aa, -%d lines only in ac)\n' "$f" "$in_aa" "$in_ac"
  fi
done

# skills/my-router as a recursive tree
if [ ! -d "$AC_ROOT/skills/my-router" ] || [ ! -d "$AA_ROOT/skills/my-router" ]; then
  fail "skills/my-router/ (missing on one side; expected sanitized mirror)"
else
  my_router_diff=$(diff -rq "$AC_ROOT/skills/my-router" "$AA_ROOT/skills/my-router" 2>&1)
  if [ -z "$my_router_diff" ]; then
    printf '  WARN: skills/my-router/ matches byte-for-byte (expected to differ; sanitization may have been skipped)\n'
  else
    count=$(printf '%s\n' "$my_router_diff" | wc -l)
    printf '  differs: skills/my-router/ (%d path-level deltas)\n' "$count"
  fi
fi

# ---- Summary ----
if [ "$exit_code" -eq 0 ]; then
  printf '\n== check-parity: STRICT clean + BY-DESIGN mirrors present. ==\n'
else
  printf '\n== check-parity: DRIFT or MISSING MIRROR (fix before tagging) ==\n'
fi

exit "$exit_code"
