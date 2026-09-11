#!/usr/bin/env bash
# scripts/check-parity.sh - Maintainer-only.
#
# Compares shared-core files between agent-config (this repo) and
# anywhere-agents (expected sibling clone). Replaces the manual "check 5"
# eyeball sweep in anywhere-agents/RELEASING.md before release cuts, and
# catches drift as it accumulates between releases.
#
# Three categories:
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
#               .github/workflows/validate.yml, bootstrap/bootstrap.sh
#               and bootstrap/bootstrap.ps1 (promoted from BY-DESIGN
#               when ac/bootstrap was re-synced to aa's canonical
#               composer-aware version; ac's bootstrap snippet still
#               curls from ac but the file served is now byte-identical
#               to aa and includes the AC->AA migration block),
#               skills/{implement-review,ci-mockup-figure,readme-polish,
#               prun,editable-figure} as recursive trees, and the shared-contract test files
#               tests/test_{dispatch_codex,dispatch_copilot,dispatch_claude,
#               health_check,guard,session_bootstrap,pointer_files,
#               prompt_byte_parity,bootstrap_preflight,
#               dispatch_path_resolution,codex_usage}.py (added
#               incrementally since 2026-05-16 to close the drift gap that
#               broke aa CI on every shared-skill change; see the comment
#               block above the strict_test_files loop for the rationale).
#
#               (v0.4.0 dropped the four shipped .claude/commands/*.md
#               pointers from cross-repo STRICT; see the block-comment
#               at "shipped .claude/commands pointers dropped from
#               STRICT" below. The pointers still appear under
#               STRICT (aa-internal) where they are checked against the
#               wheel-bundled mirror.)
#
#   STRICT (aa-internal)
#               aa source vs wheel-bundled composer mirror at
#               packages/pypi/anywhere_agents/composer/. Independent of
#               the cross-repo STRICT block above; runs whenever $AA_ROOT
#               points at an aa tree that contains the wheel mirror dir
#               (which is the case for the default ac-to-sibling-aa
#               invocation -- the block fires from ac too, not just from
#               aa). Skipped only when $AA_ROOT lacks the mirror dir.
#               Covers
#               compose_packs.py, compose_rule_packs.py,
#               generate_agent_configs.py, bootstrap/packs.yaml,
#               scripts/packs/ recursive (excluding __pycache__/),
#               skills/{implement-review,my-router,ci-mockup-figure,
#               readme-polish,prun,editable-figure}/ recursive, the six
#               shipped .claude/commands/*.md pointers, and the vet.md alias
#               pointer for implement-review. v0.6.0 promotes this
#               from the v0.5.x manual diff -rq gate to a release gate.
#
#   BY-DESIGN   expected to differ (sanitized mirror). Must still exist
#               on both sides; a missing file fails the check because the
#               release gate needs the mirror to be present, just with
#               different contents. Reports a +/- line delta per file so
#               unusual drift is visible. A byte-for-byte match is a
#               warning (sanitization may have been skipped during
#               backport). Covers: AGENTS.md (USC / Overleaf / PyCharm
#               stripping), user/settings.json (additionalDirectories
#               stripping), skills/my-router (routing-table rewrite
#               with extension guidance for forks).
#
# Usage:
#   bash scripts/check-parity.sh                           # default sibling path
#   bash scripts/check-parity.sh /path/to/anywhere-agents  # explicit
#   bash scripts/check-parity.sh --aa-internal-only [path] # wheel mirror only
#
# Exit 0: STRICT clean and every BY-DESIGN mirror present. By-design
#         summary shown for eyeball.
# Exit 1: STRICT drift, or a required BY-DESIGN mirror missing. Fix
#         before tagging.
# Exit 2: usage error (anywhere-agents clone not found).

set -uo pipefail

SCRIPT_DIR="$( cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd )"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# --aa-internal-only runs the wheel-mirror block and nothing else. That block
# needs one tree where every other block needs two, and CI checks out
# anywhere-agents on its own. Without the flag such a run has to aim both roots
# at the same tree, which is the vacuous self-comparison the guard below
# refuses; the flag says so explicitly instead.
AA_INTERNAL_ONLY=false
if [ "${1:-}" = "--aa-internal-only" ]; then
  AA_INTERNAL_ONLY=true
  shift
fi

# When invoked from an anywhere-agents checkout with a sibling agent-config/
# present, swap roots so the comparison is genuinely cross-repo. Without this
# guard, AC_ROOT defaulted to the script's own repo and AA_ROOT resolved back
# to the same path, turning the script into a silent self-comparison that
# always passes.
if $AA_INTERNAL_ONLY; then
  AA_ROOT="${1:-$REPO_ROOT}"
  AC_ROOT="$AA_ROOT"
elif [ "$(basename "$REPO_ROOT")" = "anywhere-agents" ] && [ -d "$REPO_ROOT/../agent-config" ]; then
  AC_ROOT="$REPO_ROOT/../agent-config"
  AA_ROOT="${1:-$REPO_ROOT}"
else
  AC_ROOT="$REPO_ROOT"
  AA_ROOT="${1:-$AC_ROOT/../anywhere-agents}"
fi

if [ ! -d "$AA_ROOT" ]; then
  printf 'error: anywhere-agents clone not found at %s\n' "$AA_ROOT" >&2
  printf 'usage: %s [/path/to/anywhere-agents]\n' "$0" >&2
  exit 2
fi

# The argument names the anywhere-agents clone. Passing the agent-config one
# instead points both roots at the same tree, and every comparison below then
# passes for the same reason the swap guard above exists: a self-comparison
# cannot fail. Two separate runs during the v0.7.15 review reported STRICT
# clean this way, one of them the reviewer's own verification, so refuse it
# rather than printing a result that means nothing.
if ! $AA_INTERNAL_ONLY &&
   [ "$(cd "$AC_ROOT" 2>/dev/null && pwd -P)" = "$(cd "$AA_ROOT" 2>/dev/null && pwd -P)" ]; then
  printf 'error: both roots resolve to %s\n' "$(cd "$AA_ROOT" && pwd -P)" >&2
  printf 'the argument is the path to the anywhere-agents clone, not agent-config\n' >&2
  printf 'usage: %s [/path/to/anywhere-agents]\n' "$0" >&2
  exit 2
fi

exit_code=0

fail() {
  printf '  DRIFT: %s\n' "$1"
  exit_code=1
}

# ---- STRICT: byte-identical top-level files ----
# Every cross-repo block below is silenced under --aa-internal-only, where
# there is only one tree and any answer it produced would be about that tree
# compared with itself.
$AA_INTERNAL_ONLY || printf '\n== strict byte-identical ==\n'
strict_files=(
  scripts/_python
  scripts/guard.py
  scripts/session_bootstrap.py
  scripts/statusline.py
  scripts/agent-quota.py
  scripts/generate_agent_configs.py
  # Both bootstrap entry points execute this, so it is shared runtime code by
  # the same argument as the helpers above.
  scripts/merge_settings.py
  scripts/pre-push-smoke.sh
  scripts/remote-smoke.sh
  scripts/check-parity.sh
  .claude/settings.json
  .githooks/pre-push
  .github/workflows/real-agent-smoke.yml
  .github/workflows/validate.yml
  bootstrap/bootstrap.sh
  bootstrap/bootstrap.ps1
  # Seeded into every consumer's todo/ by both entry points, so the two
  # copies have to agree the way the entry points themselves do.
  bootstrap/todo-readme.md
)
for f in "${strict_files[@]}"; do
  # break rather than an emptied array: macOS ships bash 3.2, where
  # expanding an empty array under `set -u` is an unbound-variable
  # error rather than an empty list.
  $AA_INTERNAL_ONLY && break
  if [ ! -f "$AC_ROOT/$f" ] || [ ! -f "$AA_ROOT/$f" ]; then
    fail "$f (missing on one side)"
    continue
  fi
  if ! diff -q "$AC_ROOT/$f" "$AA_ROOT/$f" >/dev/null 2>&1; then
    fail "$f"
  fi
done

# ---- STRICT: shared-contract test files (pin runtime behavior of shared scripts) ----
# These tests assert the public contract of shared scripts that are themselves
# in STRICT (dispatch-codex, dispatch-copilot, dispatch-task + prun reap-watch,
# health-check, guard, session
# bootstrap event/banner state, on-disk shape of every committed
# .claude/commands/*.md pointer, prompt body byte preservation, bootstrap
# preflight). Before this block landed, tests/ was aa-local and drifted: aa
# CI ran stale assertions against fresh shared code, and every substantive
# shared-skill change broke aa CI until a manual cp re-aligned the tests
# (e.g. aa 1295c60). Gating these files restores the property that a
# shared-contract change proposed in either repo must mirror tests in the
# same commit. Each repo may still have its own non-shared tests (aa:
# test_compose_packs.py, test_pack_*.py; ac: test_repo.py,
# test_check_parity.py); those stay aa-local and ac-local respectively.
$AA_INTERNAL_ONLY || printf '\n== strict shared-contract tests ==\n'
strict_test_files=(
  # Not a test: the module every spawning test imports to give its children a
  # console with no window on Windows. It is shared test infrastructure for
  # shared tests, so it belongs under the same gate.
  tests/_quiet_spawn.py
  tests/test_dispatch_codex.py
  tests/test_dispatch_gemini.py
  tests/test_dispatch_task_agy.py
  tests/test_dispatch_copilot.py
  tests/test_dispatch_claude.py
  tests/test_dispatch_task.py
  tests/test_health_check.py
  tests/test_guard.py
  tests/test_session_bootstrap.py
  tests/test_pointer_files.py
  tests/test_prompt_byte_parity.py
  tests/test_bootstrap_preflight.py
  # Added after the v0.7.9 incident. Both test STRICT-shared code
  # (dispatch-codex.{sh,ps1} and scripts/statusline.py) but were ac-local, so
  # aa CI never ran them. A dispatch-codex change that aborted on machines
  # without a discoverable Python interpreter passed aa CI on windows-latest
  # and shipped; the same commit turned ac CI red on nine fixtures. A test of
  # strict-shared code has to live on both sides or it guards only one repo.
  tests/test_dispatch_path_resolution.py
  tests/test_codex_usage.py
  # v0.7.11: guards the byte-comparison gates themselves. A CRLF working
  # copy makes this script's own `diff -q` report drift on every line of
  # a file whose content is identical.
  tests/test_line_endings.py
  # Both pin SKILL.md and the scripts beside it, which are STRICT-shared, so
  # the rule above applies to them: a copy that lives on one side only guards
  # one repo. test_skill_md_contract.py shipped without being registered here
  # and was identical on both sides by hand until this line took over.
  tests/test_skill_md_contract.py
  tests/test_await_review.py
  # auto-watch and stall-watch are the producer and the consumer of the
  # stream-death handshake that ends a round. Both scripts are recursively
  # STRICT; their tests were not gated, and the auto-watch emission had no test
  # on either side until the handshake was tightened.
  tests/test_auto_watch.py
  tests/test_stall_watch.py
  # Both cover skills/prun/scripts/prun_state.py, which is STRICT-shared under
  # the recursive prun tree, so the rule above reaches them: a copy living on
  # one side only guards one repo. The POSIX permission cases skip on Windows,
  # so the ubuntu and macos legs of both matrices are where they run at all.
  tests/test_prun_report.py
  tests/test_prun_snapshot.py
  # style-audit.py sits under the STRICT-shared implement-review tree and had
  # no entry here, so its test was kept identical by hand. Same rule, same fix.
  tests/test_style_audit.py
)
for f in "${strict_test_files[@]}"; do
  $AA_INTERNAL_ONLY && break
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
# __pycache__/ is excluded for the same reason it is under scripts/packs/:
# bytecode is environment-specific, and a cache directory appears whenever an
# agent or a test imports a helper module out of a skill tree. Six Python
# helpers already ship under skills/, three of them beside this script's own
# implement-review scripts, so the gap was reachable well before prun added
# one; it simply went unnoticed until a verification run left a cache here.
$AA_INTERNAL_ONLY || printf '\n== shared skills (recursive byte-identical) ==\n'
cross_repo_skills="implement-review ci-mockup-figure readme-polish prun editable-figure"
$AA_INTERNAL_ONLY && cross_repo_skills=""
for skill in $cross_repo_skills; do
  if [ ! -d "$AC_ROOT/skills/$skill" ] || [ ! -d "$AA_ROOT/skills/$skill" ]; then
    fail "skills/$skill/ (missing on one side)"
    continue
  fi
  if ! diff -rq --exclude=__pycache__ "$AC_ROOT/skills/$skill" "$AA_ROOT/skills/$skill" >/dev/null 2>&1; then
    fail "skills/$skill/"
    diff -rq --exclude=__pycache__ "$AC_ROOT/skills/$skill" "$AA_ROOT/skills/$skill" 2>&1 | sed 's/^/    /'
  fi
done

# ---- STRICT (aa-internal): wheel-bundled composer mirror byte-identity ----
#
# The aa wheel ships a sanitized composer mirror at
# ``packages/pypi/anywhere_agents/composer/`` so consumers installing via
# pipx / pip get the composer without cloning the repo. Since v0.5.6 the
# mirror has been a manual ``diff -rq`` gate; from v0.6.0 onward it is
# script-enforced because each release adds mirror entries (any drift at
# release time silently ships a stale composer to consumers).
#
# This block is independent of the cross-repo STRICT block above:
# - cross-repo STRICT compares ac vs aa.
# - aa-internal STRICT compares aa source vs the wheel-bundled mirror,
#   both of which live inside the $AA_ROOT clone. The block fires from
#   ac as well (against the sibling aa clone's mirror), not only from
#   aa. Skipped only when $AA_ROOT points at an aa tree without the
#   wheel composer dir.
#
# Drift policy: any byte-level difference fails the script with the
# offending source-side path, matching the cross-repo STRICT exit shape.
# __pycache__/ is excluded (Python bytecode is environment-specific).
if [ -d "$AA_ROOT/packages/pypi/anywhere_agents/composer" ]; then
  printf '\n== aa-internal STRICT: wheel-bundled composer mirror ==\n'
  AA_MIRROR="$AA_ROOT/packages/pypi/anywhere_agents/composer"
  aa_internal_files=(
    scripts/compose_packs.py
    scripts/compose_rule_packs.py
    scripts/generate_agent_configs.py
    bootstrap/packs.yaml
    .claude/commands/implement-review.md
    .claude/commands/vet.md
    .claude/commands/my-router.md
    .claude/commands/ci-mockup-figure.md
    .claude/commands/readme-polish.md
    .claude/commands/prun.md
    .claude/commands/editable-figure.md
  )
  for f in "${aa_internal_files[@]}"; do
    src="$AA_ROOT/$f"
    mirror="$AA_MIRROR/$f"
    # compose_rule_packs.py was added in v0.5.x and may be removed
    # before v0.7.0 (compose_packs.py supersedes it). Skip cleanly when
    # the source file is gone on both sides; fail when only one side
    # carries it (genuine drift state).
    if [ ! -f "$src" ] && [ ! -f "$mirror" ]; then
      continue
    fi
    if [ ! -f "$src" ] || [ ! -f "$mirror" ]; then
      fail "$f (missing on one side: aa source vs wheel mirror)"
      continue
    fi
    if ! diff -q "$src" "$mirror" >/dev/null 2>&1; then
      fail "$f (aa source vs wheel mirror)"
    fi
  done
  # scripts/packs/ — recursive, exclude __pycache__/
  if [ ! -d "$AA_ROOT/scripts/packs" ] || [ ! -d "$AA_MIRROR/scripts/packs" ]; then
    fail "scripts/packs/ (missing on one side: aa source vs wheel mirror)"
  else
    if ! diff -rq --exclude=__pycache__ "$AA_ROOT/scripts/packs" "$AA_MIRROR/scripts/packs" >/dev/null 2>&1; then
      fail "scripts/packs/ (aa source vs wheel mirror)"
      diff -rq --exclude=__pycache__ "$AA_ROOT/scripts/packs" "$AA_MIRROR/scripts/packs" 2>&1 | sed 's/^/    /'
    fi
  fi
  # All six shipped skill trees, including my-router.
  for skill in implement-review my-router ci-mockup-figure readme-polish prun editable-figure; do
    if [ ! -d "$AA_ROOT/skills/$skill" ] || [ ! -d "$AA_MIRROR/skills/$skill" ]; then
      fail "skills/$skill/ (missing on one side: aa source vs wheel mirror)"
      continue
    fi
    if ! diff -rq --exclude=__pycache__ "$AA_ROOT/skills/$skill" "$AA_MIRROR/skills/$skill" >/dev/null 2>&1; then
      fail "skills/$skill/ (aa source vs wheel mirror)"
      diff -rq --exclude=__pycache__ "$AA_ROOT/skills/$skill" "$AA_MIRROR/skills/$skill" 2>&1 | sed 's/^/    /'
    fi
  done
fi

# ---- BY-DESIGN: files expected to differ (summary only; not blocking unless missing) ----
$AA_INTERNAL_ONLY || printf '\n== expected to differ by design (summary; eyeball if delta is unusual) ==\n'
by_design_files=(
  AGENTS.md
  user/settings.json
)
for f in "${by_design_files[@]}"; do
  $AA_INTERNAL_ONLY && break
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

# skills/my-router as a recursive tree. Its two copies route to different
# skill inventories: ac's table names the research skills under
# reference-skills/, aa's names the six it ships plus fork guidance. That is
# a functional split rather than a sanitization one, so it stays BY-DESIGN.
# skills/editable-figure was BY-DESIGN for one release-preparation session
# and is now STRICT above: the maintainer's decision is that the private
# awarded-proposal and working-paper locators ship as written, which removes
# the divergence and the standing re-sanitization obligation with it.
if $AA_INTERNAL_ONLY; then
  :
elif [ ! -d "$AC_ROOT/skills/my-router" ] || [ ! -d "$AA_ROOT/skills/my-router" ]; then
  fail "skills/my-router/ (missing on one side; expected sanitized mirror)"
else
  my_router_diff=$(diff -rq --exclude=__pycache__ "$AC_ROOT/skills/my-router" "$AA_ROOT/skills/my-router" 2>&1)
  if [ -z "$my_router_diff" ]; then
    printf '  WARN: skills/my-router/ matches byte-for-byte (expected to differ; sanitization may have been skipped)\n'
  else
    count=$(printf '%s\n' "$my_router_diff" | wc -l)
    printf '  differs: skills/my-router/ (%d path-level deltas)\n' "$count"
  fi
fi

# ---- Summary ----
if [ "$exit_code" -eq 0 ] && $AA_INTERNAL_ONLY; then
  printf '\n== check-parity: aa-internal mirror clean (cross-repo blocks not run). ==\n'
elif [ "$exit_code" -eq 0 ]; then
  printf '\n== check-parity: STRICT clean + BY-DESIGN mirrors present. ==\n'
else
  printf '\n== check-parity: DRIFT or MISSING MIRROR (fix before tagging) ==\n'
fi

exit "$exit_code"
