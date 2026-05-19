#!/usr/bin/env bash
# remote-smoke.sh — real-agent smoke test for anywhere-agents / agent-config.
#
# Run this on a machine that actually has the code agents installed (e.g.,
# the Ubuntu release-gate server). CI cannot run this because GitHub Actions
# runners do not have Claude Code or Codex installed.
#
# Prerequisites:
#   - git, python3 (>=3.9), node (>=14), pipx
#   - claude CLI (Anthropic) with ANTHROPIC_API_KEY exported or saved
#   - codex CLI (OpenAI)    with OPENAI_API_KEY    exported or saved
#
# What it verifies:
#   1. `pipx run anywhere-agents` bootstraps cleanly in a fresh project.
#   2. Expected files are created: AGENTS.md, CLAUDE.md, agents/codex.md,
#      .claude/commands/*.md, .agent-config/.
#   3. User-level hooks land in ~/.claude/hooks/ (guard.py, session_bootstrap.py).
#   4. Claude Code `claude -p` single-turn call returns a response that
#      mentions the shipped skills — confirming Claude actually read
#      CLAUDE.md / AGENTS.md and sees the skill roster.
#   5. Codex `codex exec` single-turn call does the same — confirming
#      Codex actually read AGENTS.md / agents/codex.md.
#
# Exit code: 0 on full pass, non-zero on any failure. Steps 4 and 5 are
# skipped (with a SKIP notice) if the corresponding CLI is missing; they
# are not marked as failure in that case so the script is still useful
# on a machine where only one agent is installed.
#
# Usage:
#   bash scripts/remote-smoke.sh                # run against current upstream
#   ANYWHERE_AGENTS_REPO=yzhao062/anywhere-agents bash scripts/remote-smoke.sh
#
# Direct SSH invocation from another machine (with an SSH alias like `spark`
# configured in ~/.ssh/config — see agent-config/docs/dgx-spark-setup.md
# sections 8 and 17 for the canonical setup):
#   ssh spark 'bash -s' < scripts/remote-smoke.sh

set -euo pipefail

# --- Configurable ------------------------------------------------------------
EXPECTED_SKILLS=(implement-review my-router ci-mockup-figure readme-polish)
CLAUDE_PROMPT='List the shipped skills from AGENTS.md by directory name, comma-separated, no other text.'
CODEX_PROMPT=$CLAUDE_PROMPT

# Pick up user-local bin dirs that non-interactive SSH shells often miss.
for _d in "$HOME/.local/bin" "$HOME/.npm-global/bin" /usr/local/bin; do
  if [ -d "$_d" ] && [[ ":$PATH:" != *":$_d:"* ]]; then
    PATH="$_d:$PATH"
  fi
done
export PATH

# Auto-pick an install command if not overridden. Priority: pipx > npx > raw shell.
if [ -z "${INSTALL_CMD:-}" ]; then
  if command -v pipx >/dev/null 2>&1; then
    INSTALL_CMD="pipx run anywhere-agents"
  elif command -v npx >/dev/null 2>&1; then
    INSTALL_CMD="npx --yes anywhere-agents"
  else
    INSTALL_CMD='mkdir -p .agent-config && curl -sfL https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/bootstrap/bootstrap.sh -o .agent-config/bootstrap.sh && bash .agent-config/bootstrap.sh'
  fi
fi

# --- Helpers -----------------------------------------------------------------
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

step=0
next_step() {
  step=$((step + 1))
  bold ""
  bold "=== [$step] $1 ==="
}

fail() {
  red "FAIL: $*"
  exit 1
}

pass() {
  green "PASS: $*"
}

skip() {
  yellow "SKIP: $*"
}

# --- Temp project scaffolding ------------------------------------------------
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT
cd "$TMPDIR"
git init -q

next_step "Scaffold temp project at $TMPDIR"
pass "git-initialized empty project"

# Snapshot ~/.claude/settings.json mtime BEFORE install so the statusLine
# step below can prove the install actually rewrote the file. Without this
# guard, a regressed user-level merge would still false-pass on a release-gate
# machine where a prior smoke run already left a statusLine entry behind.
USER_SETTINGS="$HOME/.claude/settings.json"
if [ -f "$USER_SETTINGS" ]; then
  PRE_INSTALL_SETTINGS_MTIME=$(stat -c %Y "$USER_SETTINGS" 2>/dev/null || stat -f %m "$USER_SETTINGS" 2>/dev/null || echo 0)
else
  PRE_INSTALL_SETTINGS_MTIME=0
fi

# --- 1. Bootstrap via install command ---------------------------------------
next_step "Run install command: $INSTALL_CMD"
# Use eval so multi-command strings (with && chains) parse as shell, not
# as a single command's argv.
if ! eval "$INSTALL_CMD"; then
  fail "install command returned non-zero"
fi
pass "install command completed"

# --- 2. Check consumer files ------------------------------------------------
next_step "Consumer project files present"
CONSUMER_FILES=(
  "AGENTS.md"
  "CLAUDE.md"
  "agents/codex.md"
  ".agent-config/AGENTS.md"
)
for f in "${CONSUMER_FILES[@]}"; do
  [ -e "$f" ] || fail "missing $f"
  pass "$f"
done
# Bootstrap script is platform-appropriate: bootstrap.sh on Unix,
# bootstrap.ps1 on Windows (Git Bash / MSYS / Cygwin, where the npm
# shim downloads the PowerShell variant). Accept either.
if [ -f .agent-config/bootstrap.sh ] || [ -f .agent-config/bootstrap.ps1 ]; then
  pass ".agent-config/bootstrap.{sh,ps1}"
else
  fail "missing both .agent-config/bootstrap.sh and .agent-config/bootstrap.ps1"
fi

next_step "Skill pointer commands present"
for skill in "${EXPECTED_SKILLS[@]}"; do
  f=".claude/commands/$skill.md"
  [ -f "$f" ] || fail "missing $f"
  pass "$f"
done

next_step "User-level hooks deployed"
HOOK_FILES=(
  "$HOME/.claude/hooks/guard.py"
  "$HOME/.claude/hooks/session_bootstrap.py"
)
for f in "${HOOK_FILES[@]}"; do
  [ -f "$f" ] || fail "missing $f"
  pass "$f"
done

next_step "User-level statusLine deployed"
STATUSLINE_FILE="$HOME/.claude/statusline.py"
[ -f "$STATUSLINE_FILE" ] || fail "missing $STATUSLINE_FILE"
[ -f "$USER_SETTINGS" ] || fail "missing $USER_SETTINGS"
POST_INSTALL_SETTINGS_MTIME=$(stat -c %Y "$USER_SETTINGS" 2>/dev/null || stat -f %m "$USER_SETTINGS" 2>/dev/null || echo 0)
if [ "$POST_INSTALL_SETTINGS_MTIME" -le "$PRE_INSTALL_SETTINGS_MTIME" ]; then
  fail "$USER_SETTINGS mtime did not advance after install (post=$POST_INSTALL_SETTINGS_MTIME, pre=$PRE_INSTALL_SETTINGS_MTIME); statusLine entry below may be stale from a prior smoke run"
fi
grep -q '"statusLine"' "$USER_SETTINGS" || fail "missing statusLine entry in $USER_SETTINGS"
grep -q '\.claude/statusline\.py' "$USER_SETTINGS" || fail "statusLine command in $USER_SETTINGS does not reference ~/.claude/statusline.py"
pass "$STATUSLINE_FILE + settings entry (post-install mtime advanced)"

# --- 3. Generated-file header --------------------------------------------
next_step "Generated per-agent files carry the GENERATED FILE header"
for f in CLAUDE.md agents/codex.md; do
  grep -q "GENERATED FILE" "$f" || fail "$f missing GENERATED marker"
  pass "$f carries the marker"
done

# --- 4. Claude Code single-turn agent test -----------------------------------
next_step "Claude Code single-turn: confirm skill roster is visible"
if command -v claude >/dev/null 2>&1; then
  # Redirect stdin from /dev/null so claude -p does not consume the
  # remainder of this script when invoked via `ssh 'bash -s' < script`.
  resp=$(claude -p "$CLAUDE_PROMPT" </dev/null 2>&1 || true)
  printf 'response:\n%s\n' "$resp"
  missing=()
  for s in "${EXPECTED_SKILLS[@]}"; do
    if ! grep -q "$s" <<<"$resp"; then
      missing+=("$s")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    fail "Claude response missing: ${missing[*]}"
  fi
  pass "Claude mentioned all ${#EXPECTED_SKILLS[@]} shipped skills"
else
  skip "claude CLI not on PATH; skipping Claude agent test"
fi

# --- 5. Codex single-turn agent test ----------------------------------------
next_step "Codex single-turn: confirm skill roster is visible"
if command -v codex >/dev/null 2>&1; then
  # Same stdin-redirect guard as the Claude step; codex exec may also read
  # stdin if not redirected.
  resp=$(codex exec "$CODEX_PROMPT" </dev/null 2>&1 || true)
  printf 'response:\n%s\n' "$resp"
  missing=()
  for s in "${EXPECTED_SKILLS[@]}"; do
    if ! grep -q "$s" <<<"$resp"; then
      missing+=("$s")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    fail "Codex response missing: ${missing[*]}"
  fi
  pass "Codex mentioned all ${#EXPECTED_SKILLS[@]} shipped skills"
else
  skip "codex CLI not on PATH; skipping Codex agent test"
fi

# --- Done --------------------------------------------------------------------
bold ""
green "=== remote-smoke: ALL CHECKS PASSED ==="
