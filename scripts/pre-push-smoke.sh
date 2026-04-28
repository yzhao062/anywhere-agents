#!/usr/bin/env bash
# pre-push-smoke.sh — real-agent smoke for the CURRENT checkout.
#
# Unlike scripts/remote-smoke.sh (which tests the published package from
# PyPI / npm), this script validates the exact commit being pushed:
#
#   1. Generator determinism: regenerate CLAUDE.md / agents/codex.md in a
#      temp dir from the committed AGENTS.md and diff against the
#      committed generated files. Catches stale generator output that
#      could silently ship.
#   2. Claude Code roster: if `claude` is on PATH, invoke `claude -p`
#      from the repo root and assert the response mentions every skill
#      under skills/. Confirms Claude actually loads the committed
#      CLAUDE.md and sees the shipped skills.
#   3. Codex roster: if `codex` is on PATH, invoke `codex exec` from the
#      repo root with the same assertion against AGENTS.md.
#
# Agent calls are SKIPPED (not failed) when the corresponding CLI is
# missing, so the script is useful on machines that have only one agent
# configured. The generator-determinism check always runs.
#
# Called by .githooks/pre-push. Can also be invoked manually:
#   bash scripts/pre-push-smoke.sh

set -uo pipefail

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

pass()  { green "PASS: $*"; }
fail()  { red   "FAIL: $*"; exit 1; }
skip()  { yellow "SKIP: $*"; }

ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"

# Auto-detect shipped skills from the skills/ directory. Keeps this
# script in sync with whatever the repo actually ships without a
# hardcoded list that could drift.
EXPECTED_SKILLS=()
if [ -d skills ]; then
  for d in skills/*/; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    [ -f "$d/SKILL.md" ] || continue
    EXPECTED_SKILLS+=("$name")
  done
fi
if [ "${#EXPECTED_SKILLS[@]}" -eq 0 ]; then
  fail "no shipped skills found under skills/"
fi

PROMPT="List the shipped skills from your agent config (CLAUDE.md or AGENTS.md) by directory name, comma-separated, no other text."

echo "== pre-push-smoke in $(pwd) =="
echo "Expected skills: ${EXPECTED_SKILLS[*]}"
echo ""

# --- 1. Generator determinism --------------------------------------------
echo "[1/3] Generator output matches committed per-agent files"
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

if [ ! -f scripts/generate_agent_configs.py ]; then
  skip "scripts/generate_agent_configs.py not found in this repo; skipping determinism check"
else
  cp AGENTS.md "$TMPDIR/AGENTS.md"
  # Prefer the shipped _python wrapper, which filters out Windows
  # Store python.exe shims that resolve ahead of real interpreters via
  # %LOCALAPPDATA%\Microsoft\WindowsApps\. Falls back to PATH discovery
  # on systems where the wrapper is missing or non-executable.
  if [ -x "scripts/_python" ]; then
    _py="scripts/_python"
  else
    _py=$(command -v python3 || command -v python || true)
  fi
  if [ -z "$_py" ]; then
    fail "python not on PATH; cannot run generator"
  fi
  "$_py" scripts/generate_agent_configs.py --root "$TMPDIR" --quiet

  for f in CLAUDE.md agents/codex.md; do
    if [ ! -f "$f" ]; then
      fail "$f is missing in the checkout (generator output is tracked)"
    fi
    if ! diff -q "$TMPDIR/$f" "$f" >/dev/null 2>&1; then
      red "FAIL: committed $f does not match generator output."
      red "      Run: python scripts/generate_agent_configs.py --root ."
      exit 1
    fi
    pass "$f matches generator output"
  done
fi

# --- 2. Claude Code roster ------------------------------------------------
echo ""
echo "[2/3] Claude Code single-turn: confirm skill roster"
if command -v claude >/dev/null 2>&1; then
  resp=$(claude -p "$PROMPT" </dev/null 2>&1 || true)
  printf 'response:\n%s\n' "$resp"
  missing=()
  for s in "${EXPECTED_SKILLS[@]}"; do
    if ! grep -q "$s" <<<"$resp"; then
      missing+=("$s")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    fail "Claude response missing skills: ${missing[*]}"
  fi
  pass "Claude mentioned all ${#EXPECTED_SKILLS[@]} shipped skills"
else
  skip "claude CLI not on PATH; skipping Claude agent test"
fi

# --- 3. Codex roster ------------------------------------------------------
echo ""
echo "[3/3] Codex single-turn: confirm skill roster"
if command -v codex >/dev/null 2>&1; then
  resp=$(codex exec "$PROMPT" </dev/null 2>&1 || true)
  printf 'response:\n%s\n' "$resp"
  missing=()
  for s in "${EXPECTED_SKILLS[@]}"; do
    if ! grep -q "$s" <<<"$resp"; then
      missing+=("$s")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    fail "Codex response missing skills: ${missing[*]}"
  fi
  pass "Codex mentioned all ${#EXPECTED_SKILLS[@]} shipped skills"
else
  skip "codex CLI not on PATH; skipping Codex agent test"
fi

echo ""
green "== pre-push-smoke: ALL PASSED =="
