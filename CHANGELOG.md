# Changelog

All notable changes to `anywhere-agents` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version tags apply uniformly to the repo content **and** the matching `anywhere-agents` PyPI / npm packages — they share one release stream. Consumers pinned to a specific tag get a stable snapshot; consumers on `main` receive ongoing updates.

## [Unreleased]

### Added

- **`skills/implement-review/SKILL.md` gains a "When to plan-review first" section.** Formalizes plan-review as Phase 0 before the existing staged-change review loop, for complex tasks where the shape of the work precedes and constrains execution (code refactors, paper outlines, proposal structure, data-pipeline redesigns, migration plans, release-process changes, etc.). Process: write `PLAN-<identifier>.md` in the most natural location for the task (repo root for code, paper-repo root for Overleaf-style docs, local scratch directory for non-git work), send to Codex as a pre-execution design review that reads the plan path rather than `git diff --cached`, iterate until clean, then execute and run the normal review cycle on the staged output. In the agent-config 0.1.9 release cycle, two plan-review rounds caught a High-severity design flaw before implementation; the later execution-review rounds were limited to documentation and test polish, avoiding a likely post-ship hotfix. Will ship with the next real code release.

## [0.1.9] — 2026-04-18

Stabilization of 0.1.8's mechanical enforcement. 0.1.8 shipped writing-style and banner gates backed by user-level global flag files at `~/.claude/hooks/`, which caused three production regressions within hours: multi-session ping-pong between different consumer projects, `Skill` / `Task` / `TodoWrite` dispatch blocked on turn-1 slash commands, and source-repo maintenance friction. 0.1.9 moves the banner-gate state per-project, expands the exempt list to cover observation and dispatch tools, and tightens the ack-file path exemption to exact equality.

### Fixed

- **Multi-session ping-pong** between different consumer projects. Flag files move from `~/.claude/hooks/session-event.json` / `banner-emitted.json` (global) to `<project-root>/.agent-config/session-event.json` / `banner-emitted.json` (per-project). `<project-root>` is resolved by walking up from `os.getcwd()` until a directory with `.agent-config/bootstrap.{sh,ps1}` is found. Same helper duplicated in `guard.py` and `session_bootstrap.py` so both ends of the contract agree. Opening Claude Code in two different consumer repos no longer cross-invalidates each other's banner acks.
- **Banner gate blocked slash-command dispatch on turn 1.** `BANNER_GATE_EXEMPT_TOOLS` now also exempts `Skill`, `Task`, `TodoWrite`, `BashOutput`, `WebFetch`, `WebSearch`, `ToolSearch`, `LS`, `NotebookRead`. `/implement-review`, `/loop`, `/schedule` and similar slash commands dispatch without a forced round-trip. User-visible write tools (`Bash`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `KillShell`, MCP mutating tools) remain gated.
- **Source-repo maintenance hit the banner gate.** `_find_consumer_root()` returns `None` in `agent-config` / `anywhere-agents` themselves (no `.agent-config/` at the root), so the banner gate skips and maintainers can edit without friction. The writing-style gate is unchanged — `.md` / `.tex` / `.rst` / `.txt` writes still block banned AI-tell words, in source repos as well as consumer repos.
- **Ack-file path exemption tightened.** 0.1.8 exempted any `Write`/`Edit`/`MultiEdit` whose path ended in `.agent-config/banner-emitted.json`, which allowed off-root or cross-project ack writes to bypass the gate. 0.1.9 resolves `consumer_root` first and requires exact normalized equality (`normcase(normpath(abspath(...)))`) with `<consumer_root>/.agent-config/banner-emitted.json`.

### Added

- **`tests/test_session_bootstrap.py`** — subprocess-based tests covering per-project event write from cwd and nested-cwd launches, no-op behavior in unrelated directories, legacy-flag cleanup with temp `HOME`/`USERPROFILE`, and source-repo no-event-write.
- **Expanded banner-gate tests in `tests/test_guard.py`** — per-project isolation across two tmp consumer dirs, walk-up from nested cwd, exact-path ack exemption (with off-root and cross-project denial), source-repo gate skip, and the new exempt tools (`Skill`, `Task`, `TodoWrite`, `LS`, `NotebookRead`).

### Changed

- **`scripts/session_bootstrap.py`** — now writes `session-event.json` at the walked-up consumer root (not raw `os.getcwd()`), so a nested-cwd launch still places the event file where `guard.py` will look for it. Also runs a one-time cleanup of 0.1.8's orphan global flag files.

### Compatibility

- **Transition note.** The first Claude Code session after upgrading from 0.1.8 to 0.1.9 may not be mechanically banner-gated. Mechanism: Claude Code's `SessionStart` hook invokes the old `session_bootstrap.py` (still 0.1.8 at the moment of the hook fire), which writes the legacy global flag; the old `session_bootstrap.py` then runs `bootstrap.sh`, which pulls upstream and deploys the new 0.1.9 `guard.py` + `session_bootstrap.py` to `~/.claude/hooks/`. For the remainder of that session, the new `guard.py` reads the per-project ack file, which has not been created yet by the (old) `session_bootstrap.py`, so the gate silently passes. Next `SessionStart` runs the new `session_bootstrap.py`, writes the per-project event, and the gate resumes normal operation. The banner rule still fires at prompt level during the transition session, so the banner itself should still appear.
- **0.1.8's global flag files** under `~/.claude/hooks/` are obsoleted by 0.1.9. `session_bootstrap.py` cleans them up on each run. No user action required.
- **`AGENT_CONFIG_GATES=off` escape hatch** still works identically.

## [0.1.8] — 2026-04-17

Mechanical enforcement upgrade. Adds two `PreToolUse` gates to `scripts/guard.py` (deployed to `~/.claude/hooks/guard.py` by bootstrap) so that writing-style rules and the session-start banner are now enforced by hooks, not just prompt-level compliance. Session banner also re-emits on resume / compact / clear via a flag-file mechanism. Closes multiple observed gaps where 0.1.7's rules were skipped in practice.

### Added

- **Writing-style gate in `scripts/guard.py`.** `PreToolUse` hook now denies any `Write` / `Edit` / `MultiEdit` to `.md` / `.tex` / `.rst` / `.txt` files when the outgoing content contains a banned AI-tell word from `AGENTS.md` Writing Defaults. The deny message lists the offending words so the agent can revise. Code files (`.py`, `.js`, etc.) are not checked — banned words rarely appear naturally in code, and docstring false positives would be a usability regression. Close-variant matching via word boundaries.
- **Banner emission gate in `scripts/guard.py`.** `PreToolUse` hook now denies any tool call (other than `Read`, `Grep`, `Glob`, or a `Write` to `~/.claude/hooks/banner-emitted.json`) while `~/.claude/hooks/session-event.json.ts > ~/.claude/hooks/banner-emitted.json.ts` — i.e., while a SessionStart event is pending but the banner has not been emitted for it. Forces the agent to emit the banner before doing real work; the gate lifts after the agent writes the acknowledgment file. Read/Grep/Glob remain exempt so the agent can still inspect state.
- **`session_bootstrap.py` writes `~/.claude/hooks/session-event.json`** on every SessionStart hook fire (fresh startup, resume, clear, compact all produce a fresh timestamp). The file contains a single `{"ts": <unix-ts>}`, overwritten each time. Combined with the banner gate, a fresh SessionStart event mechanically blocks work until the banner is re-emitted.
- **Dual-runtime turn-start banner procedure in `AGENTS.md`.**
  - *Claude Code branch:* read `session-event.json` and `banner-emitted.json` before each response. If the event timestamp is newer than the emitted timestamp (or only `session-event.json` exists), emit the banner as the literal first content of the response and write the event `ts` into `banner-emitted.json`. The flag-file mechanism covers all four SessionStart lifecycle events; the banner gate in `guard.py` enforces it mechanically.
  - *Codex branch:* Codex has no `SessionStart` hook equivalent and no guard.py hook runs during a Codex invocation. Each Codex invocation is a new session; emit the banner on the turn with no prior assistant turns in context (the first response of the invocation) and skip on subsequent turns. Enforcement remains prompt-level for Codex.
- **Mechanical Enforcement section in `AGENTS.md`** documenting the gates, their tool scope, triggers, and actions; plus the `AGENT_CONFIG_GATES=off` escape hatch.
- **`RELEASING.md` check #6: dual-OS pre-release test.** Maintainer runs the full test suite on the Spark release-gate box (ARM64 Ubuntu) via SSH before tagging, using the shared-core agent-config clone. Windows-only local coverage misses POSIX path handling and shell differences; CI runs x86_64 Ubuntu, so Spark adds ARM64. Command + interpretation documented inline.

### Changed

- **`scripts/guard.py` is no longer Bash-only.** The hook now dispatches by `tool_name`, runs the two new gates first (for tools they cover), and falls through to the existing Bash-only checks (compound-cd, destructive git, destructive gh). Legacy hook payloads without `tool_name` fall through to the Bash path for backward compatibility.

### Fixed

- **Writing-style rules are now enforced, not only prompt-level.** Prior releases listed ~40 banned AI-tell words in `AGENTS.md` Writing Defaults but relied on agent compliance. Observed behavior showed occasional slips. The new writing-style gate blocks at tool-call time so the banned words cannot reach prose files.
- **Banner fires on task-oriented first prompts.** 0.1.6 addressed `superpowers:using-superpowers` skill-first behavior, but did not cover the plain "user types a task and agent jumps in" case. 0.1.7 sessions still occasionally missed the banner for that reason (observed in a real fresh-session screenshot). The new banner gate + the checklist-style turn-start procedure make the emission unambiguous, and the gate mechanically blocks any tool-based progress until the banner lands.
- **Banner re-appears on resume / compact / clear in Claude Code**, not only on turn 1 of a fresh conversation. SessionStart hook fires on all four lifecycle events; the flag-file mechanism + banner gate now route each event into a fresh banner emission.

### Escape hatch

Set `AGENT_CONFIG_GATES=off` (or `0` / `disabled` / `false` / `no`) via the `env` block in `~/.claude/settings.json` to disable the two new gates. The compound-cd / destructive-git / destructive-gh checks remain active. Useful when working on meta-documentation that quotes banned words as examples, or to bypass a false positive while a real fix is in flight.

### Compatibility

- Existing consumers on 0.1.7 caches: self-update pulls the 0.1.8 bootstrap on next session. The updated `session_bootstrap.py` starts writing `session-event.json` on every SessionStart fire; the updated `AGENTS.md` rule takes effect on the next session after bootstrap. No user action required.

## [0.1.7] — 2026-04-17

Session-start banner now surfaces Claude Code + Codex version status (current → latest + auto-update state). Bootstrap heals a Claude Code auto-update gotcha left over from npm/winget-era installs.

### Added

- **Version-aware session banner.** The Claude Code and Codex lines now show current version, latest version (drift indicated with ` → `), and Claude Code's auto-update state:

  ![session-start banner example](https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/docs/session-banner.png)

  Text form:

  ```
  📦 anywhere-agents active
     ├── OS: win32
     ├── Claude Code: 2.1.112 → 2.1.115 (auto-update: on) · Opus 4.7 · effort=max
     ├── Codex: 0.121.0 → 0.122.0 · gpt-5.4 · xhigh · fast · fast_mode=true
     ├── Skills: 4 shared (ci-mockup-figure, implement-review, my-router, readme-polish)
     ├── Hooks: PreToolUse guard.py, SessionStart session_bootstrap.py
     └── Session check: all clear
  ```

  When versions match, the ` → <latest>` half is omitted and the banner just shows `Claude Code: 2.1.115 …`. `auto-update: off` appears when `autoUpdates: false` is still present in `~/.claude.json` (see Fixed below) or `DISABLE_AUTOUPDATER=1` is set in the effective env.

- **`session_bootstrap.py` version cache.** The SessionStart hook now refreshes `~/.claude/hooks/version-cache.json` from the npm registry (`@anthropic-ai/claude-code` and `@openai/codex`) once per 24 hours. The banner reads this cache; on cache hit the session starts with zero extra latency. On network failure, the cache keeps the last-known values and the banner still shows current versions without the `→ latest` half.

### Fixed

- **Bootstrap heals legacy `autoUpdates: false` in `~/.claude.json`.** Consumers who migrated from npm or winget to the native Claude Code installer may have a stale `"autoUpdates": false` flag blocking the native updater daemon from spawning at launch (observed behavior: `autoUpdatesProtectedForNative: true` does not actually neutralize it in that path). Bootstrap now flips the stale flag to `true` on every run. To genuinely disable auto-updates, use `DISABLE_AUTOUPDATER=1` via the `env` block in `~/.claude/settings.json` — that takes precedence and is the only supported opt-out path going forward.
- **`AGENTS.md` Environment Notes updated** to match the real fix path: the prior claim that `autoUpdatesProtectedForNative` neutralizes the legacy flag has been replaced with the observed behavior and the new bootstrap heal.

### Compatibility

- Existing consumers on 0.1.6 caches: self-update pulls the 0.1.7 bootstrap on next session. On the run after that, the autoUpdates heal fires if needed and the version cache populates. No user action required.

## [0.1.6] — 2026-04-17

Fork-friendly bootstrap — pass your upstream as the bootstrap argv, env var, or persisted file. Forkers no longer have to edit bootstrap scripts to point consumers at their fork; one command per consumer now carries the upstream for the life of that project. Also fixes a session-start-banner suppression by `superpowers` and a stale-origin bug on subsequent runs.

### Added

- **Upstream cascade in `bootstrap/bootstrap.{ps1,sh}`.** Resolution order is argv > env var (`AGENT_CONFIG_UPSTREAM`) > persisted file (`.agent-config/upstream`) > hardcoded default. Whichever value resolves is persisted to `.agent-config/upstream`, so any of the three entrypoints seeds the consumer's long-term upstream choice — you only pass it once per consumer project. Setting the env var on a later run updates the persisted value for all subsequent hook-triggered runs; it is not transient.
- **Fork instructions in the README now include the concrete install command** with the `<your-user>/<your-repo>` argv, in both Bash and PowerShell.

### Changed

- **Curl and `git clone` URLs inside bootstrap scripts are now parameterized** against the resolved upstream instead of hardcoded. The hardcoded default remains `yzhao062/anywhere-agents`, so consumers who never pass argv / env var / persisted file behave identically to 0.1.5.

### Fixed

- **Banner rule in `AGENTS.md` Session Start Check now explicitly overrides any skill's "invoke before responding" rule.** When a plugin like `superpowers:using-superpowers` fired a skill (e.g. `brainstorming`) as the first action on turn 1, the banner was silently dropped and replaced by the skill's output. The updated rule makes banner-first mandatory and allows the skill to run on the same turn after the banner text.
- **Sparse-clone origin now follows the resolved upstream on every run.** Prior versions only used the resolved upstream for the initial `git clone`; on subsequent runs (hook-triggered refreshes after an argv/env-var upstream switch), `git pull` fetched against whatever `origin` was set at first clone, so AGENTS.md came from the new upstream but skills/hooks/settings came from the old one. Both scripts now `git remote set-url origin "$REPO_URL"` before pulling.
- **Bash cascade tolerates an empty persisted upstream file.** If `.agent-config/upstream` exists but is empty (e.g. after a failed or interrupted write), resolution now falls through to the hardcoded default instead of producing a malformed URL. PowerShell already handled this via `Trim()`.

### Compatibility

- Existing consumers on 0.1.5 caches: self-update pulls the 0.1.6 bootstrap on next session. With no argv, env var, or persisted upstream file, the cascade falls through to the same hardcoded default, so behavior is unchanged. No user action required.
- Forkers: stop editing URLs inside bootstrap scripts. Tell consumers to install with `bash .agent-config/bootstrap.sh <your-user>/<your-repo>` (or the PowerShell equivalent).

## [0.1.5] — 2026-04-17

Bootstrap self-update — cached `.agent-config/bootstrap.{ps1,sh}` now copies itself forward from the sparse clone at the end of every run, so future bootstrap improvements reach existing consumers without a manual re-download.

### Fixed

- **Bootstrap self-update** in `bootstrap/bootstrap.ps1` and `bootstrap/bootstrap.sh`. At the end of each run, the cached entrypoint (`.agent-config/bootstrap.ps1` / `.agent-config/bootstrap.sh`) is overwritten with the fresh version pulled via sparse clone (`.agent-config/repo/bootstrap/bootstrap.{ps1,sh}`). Without this, any consumer who bootstrapped before a bootstrap-script change was permanently frozen on the old version and would never receive future improvements (e.g., the 0.1.3 generator step that creates `CLAUDE.md` and `agents/codex.md`).
- **Sparse-checkout now includes `bootstrap/`.** Prior versions limited the sparse clone to `skills .claude scripts user`, so the self-update source (`.agent-config/repo/bootstrap/bootstrap.{ps1,sh}`) did not exist in the sparse tree and the guard was silently a no-op. Caught in post-commit Codex review.
- **Self-update is best-effort.** PowerShell wraps `Copy-Item` in `try/catch` with `Write-Warning`; Bash uses `|| printf '...' >&2`. An anti-virus lock or read-only cache no longer turns a successful refresh into a reported bootstrap failure.

### Rollout note

The self-update block can only run once a consumer already has a bootstrap script containing that block. Existing consumers whose `.agent-config/bootstrap.ps1` or `.agent-config/bootstrap.sh` predates 0.1.5 need one seed refresh — run the bootstrap block in `AGENTS.md`, re-invoke `pipx run anywhere-agents` / `npx anywhere-agents`, or manually re-download the raw bootstrap script from `main`. After that single seed update, the cached entrypoint self-refreshes automatically on every subsequent session.

## [0.1.4] — 2026-04-16

User-visible session start banner, real-agent smoke (local + CI), pre-push safety hook, broadened validate matrix (macOS + Python 3.9-3.13), and published-package registry smoke. No breaking changes to the install flow.

### Added

- **Session Start banner** in `AGENTS.md` Session Start Check. Agents are now required to emit a structured banner as the first lines of their first response, showing `📦 anywhere-agents active`, OS, Agent, Codex config summary, Skills count + names, Hooks status, and a Session check line. Makes "bootstrap actually ran" visible to the user instead of silent.
- **`scripts/remote-smoke.sh`** — real-agent smoke for post-publish / published-install verification. Bootstraps a throwaway project via the published `pipx run anywhere-agents`, `npx anywhere-agents`, or raw-shell install, verifies expected files + user-level hooks deploy, then runs `claude -p` and `codex exec` non-interactively and asserts each response mentions the four shipped skills. Auto-detects install method (pipx → npx → raw curl). Distinct from `scripts/pre-push-smoke.sh`, which validates the release-candidate checkout before tagging. Validated on Windows daily-driver and on the Ubuntu DGX Spark via `ssh -6 spark 'bash -s' < scripts/remote-smoke.sh`.
- **`.githooks/pre-push`** — runs `scripts/pre-push-smoke.sh` when a push includes agent-critical files (`AGENTS.md`, `bootstrap/`, `scripts/`, `skills/`). `pre-push-smoke.sh` validates the CURRENT checkout (generator determinism + `claude -p` + `codex exec` against committed rule files) — distinct from `scripts/remote-smoke.sh`, which validates the published install path. Pure doc / test / CI-workflow pushes skip the smoke automatically and push fast. Bypass with `git push --no-verify`. Enable per-clone with `git config core.hooksPath .githooks`.
- **`.github/workflows/real-agent-smoke.yml`** — CI workflow that installs Claude Code and Codex CLIs on ubuntu-latest, invokes them against the committed `CLAUDE.md` / `agents/codex.md` using API key secrets, and asserts each response lists the shipped skills. Narrow triggers (`release: published` + `workflow_dispatch`) keep per-token API cost low (~$0.04 per run). Requires `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` repo secrets.
- **`.github/workflows/package-smoke.yml`** — triggered on `release: published` + weekly cron + manual dispatch. Installs the published PyPI and npm artifacts on a cross-OS × cross-Python/Node matrix (ubuntu × py 3.9/3.12/3.13, ubuntu × node 18/20/22, plus latest on Windows + macOS) and asserts `--version`, `--help`, `--dry-run` all succeed. Catches registry drift and cross-runtime install regressions that unit tests cannot see.

### Changed

- **`.github/workflows/validate.yml` matrix expanded** from 2 OS × Python 3.12 to 3 OS × Python 3.9-3.13 (9 jobs: ubuntu × 3.9/3.10/3.11/3.12/3.13, windows × 3.12/3.13, macos × 3.12/3.13). Added a separate **`docs-strict-build`** job (Ubuntu, Python 3.12) that runs `mkdocs build --strict --clean` on every push to catch Read-the-Docs regressions before they hit the live site.
- README stars badge (English and Simplified Chinese) carries `cacheSeconds=300`, shortening Shields.io server-side cache from the default (up to 1 hour) to 5 minutes. Users see new star counts reflected faster.
- `CONTRIBUTING.md` documents the pre-push hook enable step and lists the four shipped skills (previously listed only two — drive-by correction).
- `agent-config/docs/anywhere-agents.md` release workflow gains step 6 (real-agent smoke before tagging, with cross-reference to the private DGX setup doc and the CI equivalent). Subsequent steps renumbered.

### Fixed

- **`scripts/remote-smoke.sh` stdin leak** — when invoked via `ssh 'bash -s' < script`, `claude -p` and `codex exec` consumed the remaining stdin (the rest of the script), silently aborting later steps with a misleading exit code 0. Now redirects stdin from `/dev/null` on both agent calls.
- **`scripts/remote-smoke.sh` argv parsing** — `$INSTALL_CMD` was unquoted, word-splitting multi-command strings like `mkdir -p X && curl …` into literal argv for `mkdir`. Now uses `eval "$INSTALL_CMD"` so shell operators in the install string parse correctly.
- **`scripts/remote-smoke.sh` bootstrap file check** was hardcoded to `bootstrap.sh`, which failed on Windows Git Bash where the npm shim downloads `bootstrap.ps1` instead. Now accepts either platform-appropriate variant.
- **Session Start Check "not configured" phrasing** originally said `not configured — see Codex MCP Integration below`, which leaked a broken self-reference into the generated `CLAUDE.md` (Codex MCP section is stripped there). Shortened to `not configured`; regression test in `test_generator.py` asserts the strip invariant.

### Review history

0.1.4 passed `implement-review` with Codex before release. Resolved findings:

- **Medium** — pre-push hook initially invoked `scripts/remote-smoke.sh`, which tests the published package; this was a false-positive gate that could pass while the release-candidate checkout was broken. Fixed by adding `scripts/pre-push-smoke.sh` (validates the current checkout via generator determinism + `claude -p` / `codex exec` against the committed rule files) and pointing the pre-push hook at it. `remote-smoke.sh` retained for post-publish / published-install verification.
- **Medium** — `.github/workflows/package-smoke.yml` did not pin the install spec to the release tag, so a release event could pass while testing an older version the registry still served as latest. Fixed: workflow now resolves the expected version from `github.event.release.tag_name` (or `inputs.version` for manual dispatch), pins the install, and asserts the CLI's `--version` output contains the expected version.
- **Medium** — `CHANGELOG.md` lost the `## [0.1.3] — 2026-04-16` heading when the 0.1.4 section was inserted, folding old 0.1.3 content under 0.1.4. Fixed: heading restored.
- **Medium** — `RELEASING.md` pre-tag gate and `agent-config/docs/anywhere-agents.md` release-workflow step 6 pointed at `scripts/remote-smoke.sh` (published-package path) rather than the candidate checkout. Fixed: both now invoke `pre-push-smoke.sh` for the candidate, with `remote-smoke.sh` documented separately for post-publish verification.
- **Low** — stale `remote-smoke.sh` references in `.githooks/pre-push` header comment and skip message, `CONTRIBUTING.md` pre-push section (including wrong prerequisites), `agent-config/README.md` pre-push subsection, and the `CHANGELOG.md` 0.1.4 bullet describing `remote-smoke.sh` as "local / pre-tag validation." Fixed: all references distinguish the two scripts correctly.
- **Low** — `agent-config/docs/anywhere-agents.md` said the CI real-agent smoke runs "on every push," contradicting the narrow `release: published` + `workflow_dispatch` triggers in `real-agent-smoke.yml`. Fixed.
- **Low** — cost estimate for the real-agent CI workflow said `~$0.02` in the workflow header and `~$0.04` in the CHANGELOG. Fixed: both say `~$0.04 per run` (two short API calls).

No High-priority findings at any round.

## [0.1.3] — 2026-04-16

Central `AGENTS.md` → per-agent file generator (`CLAUDE.md`, `agents/codex.md`), Claude Code SessionStart hook that enforces bootstrap automatically, Scenario E in the README for the "you are running suboptimal defaults without knowing" pitch, and a 1:1 Simplified Chinese README.

### Added

- **Central source + per-agent generator.** `AGENTS.md` becomes the single source of truth for agent rule files. New HTML-comment markers (`<!-- agent:claude -->` / `<!-- agent:codex -->`) tag agent-specific sections. `scripts/generate_agent_configs.py` reads `AGENTS.md` and emits `CLAUDE.md` (Claude Code auto-loads this natively) and `agents/codex.md`. Each generated file carries a `GENERATED FILE` header and a documented precedence ladder. Bootstrap re-runs the generator on every session.
- **Hand-authored file protection.** The generator preserves any `CLAUDE.md` / `agents/codex.md` that lacks the `GENERATED FILE` header and prints a loud warning until the user resolves it. To keep a custom rule file, rename to `CLAUDE.local.md` — which wins over the generated file in the precedence ladder.
- **SessionStart hook enforces bootstrap.** `scripts/session_bootstrap.py` deploys to `~/.claude/hooks/session_bootstrap.py` on first bootstrap run. On every subsequent Claude Code session, the hook runs `.agent-config/bootstrap.sh` (or `.ps1`) if present, no-op otherwise. Users no longer need to type a reminder to keep the config fresh — for Claude Code, updates are fully automatic.
- **Configuration Precedence section in `AGENTS.md`.** Documents the three config layers (rule files, Claude Code settings, env vars) with explicit precedence rules.
- **Scenario E in README** — "The settings you did not know you were missing." Makes the selling point explicit: most Claude Code / Codex users never touch effort levels, model selection, or Codex MCP config; `anywhere-agents` ships the recommended default stack in one install.
- **README Install section** now documents the verbal fallback for non-Claude agents (Codex, Cursor, etc.) that lack SessionStart hook support — tell the agent `read @AGENTS.md to run bootstrap, session checks, and task routing` on the first message of each session.
- **`README.zh-CN.md`** — a 1:1 Simplified Chinese translation of `README.md`. Language switcher at the top of both files (`English · 中文`). Code blocks, Mermaid diagram labels, file paths, URLs, and skill names stay in English for consistency; narrative, section titles, table contents, and callouts are translated.

### Changed

- `user/settings.json` declares a `SessionStart` hook alongside the existing `PreToolUse` guard hook.
- `bootstrap/bootstrap.sh` and `bootstrap.ps1` run the generator after fetching `AGENTS.md`, and deploy `session_bootstrap.py` alongside `guard.py` to `~/.claude/hooks/`.
- `AGENTS.md` "What gets shared" table lists the new generated files and the user-level hook set (guard + session bootstrap).
- `AGENTS.md` "Environment Notes" Claude Code install + effort-level bullets are now tagged with `<!-- agent:claude -->` so Codex does not see the noise.
- `AGENTS.md` "Codex MCP Integration" section is tagged with `<!-- agent:codex -->` so Claude Code does not see the Codex setup noise.

### Fixed

- **Claude Code settings precedence wording in `AGENTS.md` Configuration Precedence section** — the ordering was reversed relative to Claude Code's documented behavior. Corrected to `managed policy > command-line args > .claude/settings.local.json > .claude/settings.json > ~/.claude/settings.json`. Regenerated `CLAUDE.md` and `agents/codex.md` so the correction propagates.
- **SessionStart hook noise** — `scripts/session_bootstrap.py` now captures subprocess stdout and emits one concise line (`anywhere-agents: bootstrap refreshed`) to avoid flooding Claude Code's session-start context with `git pull` status, clone progress, or generator messages. Errors surface to stderr with the last ~2 KB of child output for debugging.
- **Generator preserve-warning path for nested outputs** — the rename hint previously dropped the `agents/` prefix for `agents/codex.md`. Now the warning includes the full relative path, and a regression test covers the nested case.
- **Whitespace normalization in generator** — `extract_for()` now strips trailing whitespace on every line, so generated files do not inherit whitespace-only source lines that fail `git diff --cached --check`.

### Review history

0.1.3 passed `implement-review` with Codex before release. Resolved findings:

- **Medium** — Claude Code settings precedence wording in `AGENTS.md` reversed managed-policy order; corrected to match the documented `managed policy > command-line args > .local > project > user` chain.
- **Medium** — `scripts/session_bootstrap.py` forwarded raw subprocess stdout into Claude Code's session-start context; now captures and emits one concise summary line.
- **Low** — Generator preserve-warning dropped the `agents/` prefix for nested outputs; fixed, with a regression test.
- **Low** — Private repo `AGENTS.md` source had a whitespace-only line that propagated into generated files and failed `git diff --cached --check`; source corrected and generator now normalizes trailing whitespace on every line.
- **Low** — Private repo `AGENTS.md` "What gets shared" table did not yet list the new generated files and SessionStart hook ownership; added.
- **Medium** — `README.zh-CN.md` translated comments inside code blocks, violating the "keep code blocks verbatim in English" contract; restored English comments inside fences and kept translation outside.
- **Low** — Both READMEs introduced the scenarios as "Four" after Scenario E was added; corrected to "Five concrete scenarios" / "五个具体场景".
- **Low** — Repo-layout tree in the collapsible was stale (missed `CLAUDE.md`, `agents/codex.md`, `scripts/generate_agent_configs.py`, `scripts/session_bootstrap.py`); updated in both READMEs.
- **Low** — Chinese README used half-width punctuation in prose (`,`, `:`, `(`, `)` between Chinese characters); converted to full-width Chinese punctuation (`，` `。` `；` `：` `（）`) in prose while leaving code blocks, URLs, file paths, badge IDs, and English literals unchanged.
- No High-priority findings.

## [0.1.2] — 2026-04-16

Two new shipped skills (`ci-mockup-figure`, `readme-polish`), Read the Docs site launch, scenario-first README, reframed hero (project is the subject, author credentials become supporting evidence), Scenario B visualized as a left-to-right flowchart, and the usual round of Codex-driven corrections.

### Added

- **Skill: `ci-mockup-figure`** — build HTML mockups of systems, dashboards, and timelines, then capture as space-efficient PNG / PDF figures via headless Chrome. Includes an abstract-diagram path using TikZ or skia-canvas for architecture figures that need arrow routing between non-adjacent nodes. Covers tool selection, design principles, capture workflow, and LaTeX / Markdown insertion.
- **Skill: `readme-polish`** — audit a GitHub README and rewrite using modern 2025-2026 patterns: centered header, Shield.io badges, dot-separated nav, hero image, `> [!NOTE]` / `> [!TIP]` callouts, emoji-prefixed feature bullets, collapsible `<details>` for reference material, Mermaid diagrams, tables over dense prose. Ships with a patterns reference catalog and a pre-publish audit checklist.
- **Read the Docs site** — [anywhere-agents.readthedocs.io](https://anywhere-agents.readthedocs.io/). MkDocs + Material with a custom USC cardinal palette (`#990000`). Covers install, per-skill deep docs (via `mkdocs-include-markdown-plugin` so each skill page pulls directly from its `SKILL.md`), an `AGENTS.md` section-by-section reference, and a collapsible FAQ. Changelog is mirrored from the repo. New repo files: `.readthedocs.yaml`, `mkdocs.yml`, `docs/requirements.txt`, `docs/stylesheets/extra.css`, and `docs/*.md` content. Dependencies are upper-bounded so a future MkDocs 2.0 release cannot silently break the build.
- **`docs/skills/references/`** — pass-through pages for `review-lenses.md`, `routing-table.md`, `patterns.md`, and `checklist.md`. Links from inside each `SKILL.md` now resolve to real docs pages on RTD. `mkdocs build --strict --clean` is clean.
- **README RTD docs badge** — next to PyPI / npm / License / CI / Stars.
- **README "How to update" section** inside Install explaining that re-running the install command updates, plus the one-liner force refresh for mid-session.

### Changed

- **README restructured around four scenarios.** Replaces the adjacent "The agentic workflow this encodes" principle table and "What you get after setup" feature list with **What it does in practice** — A: add to any project, B: review before you push (left-to-right flowchart), C: writing that does not sound like an AI (with highlighted banned words and a before / after), D: Git safety catches mistakes. Reference-shaped content (day-to-day table, "what is opinionated" table) moves into collapsibles. Dot-nav points at Scenarios / Docs.
- **Hero reframed.** The project is visually primary; the author avatar is removed; PyOD credentials are supporting evidence under a "Built by…" line with PyOD described in context ("a widely used Python anomaly detection library") and numbers smaller and muted. Sig-strip pill changed from "What you get" to "Condensed experience" with the lead line "distilled from daily use since early 2026 across research, paper writing, and dev work." Panel 4 shows dispatch across the four shipped skills. Panel 5 removes `rm -rf` (not guard-scoped) and shows `git rebase` instead. Footer shows `4 shipped skills · anywhere-agents.readthedocs.io`.
- **Maintainer callout reframed** from "Maintained by [Yue Zhao]…" opener to "**Condensed from daily use.**" opener, with credentials moved to the end as backing evidence.
- **Scenario B visual** changed from a sequence diagram to a left-to-right flowchart with a loop-back arrow. Actor lanes disappear; the flow reads as one linear pipeline with an explicit `{clean?}` decision.
- **`skills/my-router/references/routing-table.md`** lists concrete keyword, file-type, and directory rules for all four shipped skills (previously only `implement-review`).
- **`skills/my-router/SKILL.md`** intro reflects the four-skill shipped set and includes dispatch examples for `ci-mockup-figure` and `readme-polish`.
- **`AGENTS.md`** "What gets shared" table lists all four shipped skills.
- **`.gitignore`** — added `site/` so local `mkdocs build` output does not leak into `git add -A`.

### Fixed

- **MkDocs strict build broken on included-link warnings** (Round 1, Medium). Included `SKILL.md` bodies referenced `references/*.md` that were not docs pages. Resolved by adding pass-through pages under `docs/skills/references/`, setting `rewrite_relative_urls: false` on the include-markdown plugin, and reorganizing the nav so reference pages appear as sub-items under each skill. `mkdocs build --strict --clean` now completes with zero warnings.
- **`tests/test_repo.py:260` stale two-skill-era failure message** (Round 1, Low). Replaced with a version-agnostic message pointing the maintainer at `SHIPPED_SKILLS` and the public docs together.
- **CHANGELOG scenario order mismatch** (Round 1, Low). Parenthetical listed scenarios as A-C-B-D; corrected to A-B-C-D.
- **Docs build dependency drift risk** (Round 2, Medium). `docs/requirements.txt` now carries upper bounds on every pin (`mkdocs<2.0`, `mkdocs-material<10.0`, `mkdocs-include-markdown-plugin<8.0`, `pymdown-extensions<11.0`) so a future RTD rebuild cannot pick up a breaking major.
- **Generated `site/` directory not ignored** (Round 2, Low). Added to `.gitignore` under a "MkDocs build output" comment.

### Review history

0.1.2 passed two rounds of `implement-review` with Codex before release:

- **Round 1**: 5 findings (2 Medium + 3 Low) covering MkDocs broken links, stale test message, CHANGELOG scenario order, and stale two-skill framing in the private relationship doc. All Fixed.
- **Round 2**: 3 findings (2 Medium + 1 Low) covering docs build dependency bounds, `site/` in `.gitignore`, and adding `mkdocs build --strict --clean` to the private release gate. All Fixed.
- No High-priority findings in either round.

## [0.1.1] — 2026-04-16

Release-hygiene follow-up. Documentation and layout improvements since 0.1.0, and package source is now fully reproducible from the repository.

### Added

- `docs/hero.png` + `docs/hero.html` + `docs/avatar.jpg` — README hero image with a 6-panel feature grid (cardinal-red branding), self-contained HTML source for regeneration, and vendored avatar so the hero does not depend on an external URL.
- README "The agentic workflow this encodes" section — educational narrative covering git-as-substrate, implementer + gatekeeper pattern, and IDE / MCP tradeoffs across operating systems.
- Mermaid review-loop sequence diagram (collapsed by default).
- Agent-friendly Install section with PyPI, npm, and raw-shell paths; `> [!TIP]` callout explains the "ask your agent to install" pattern.
- Package-local LICENSE files (`packages/pypi/LICENSE`, `packages/npm/LICENSE`) so published artifacts include the Apache-2.0 text.
- `packages/pypi/` and `packages/npm/` directories in the public repo so package source lives in the repo (was previously in an external scratch workspace — see 0.1.0 "Not included").

### Changed

- README restructured for scannability: centered header with badges and dot-nav; tables replaced dense bullet lists where the content was reference-like; collapsibles hide detail from first-read while keeping it one click away.
- Maintainer paragraph now sits inside a `> [!NOTE]` callout and is roughly half its previous length.
- CLI version reads from a single source of truth:
  - Python: `anywhere_agents.cli` imports `__version__` from `anywhere_agents/__init__.py`.
  - Node.js: `bin/anywhere-agents.js` reads `version` from its sibling `package.json` at runtime.
- Release workflow in the private relationship doc reflects the new `packages/` layout and the single-source version pattern.

### Fixed

- Guard-hook scope claim corrected in README, CHANGELOG, and hero source: `rm -rf` goes through Claude Code permission prompts via settings, not through `guard.py`. The `STOP! HAMMER TIME!` warning is for guard-covered Git/GitHub commands only.
- Raw shell install path now creates `.agent-config/` before downloading the bootstrap script on both macOS/Linux and Windows PowerShell. The install section also shows both shells (previously only Bash).
- CHANGELOG version numbering unified: one release stream covers both repo content and PyPI/npm packages.

## [0.1.0] — 2026-04-16

Initial public release. The sanitized downstream of the author's private daily-driver agent config, refined over months across many repositories, machines, and workflows. This release covers both the GitHub repo content (bootstrap, skills, guard hook, settings, tests, docs) and the matching PyPI / npm CLI packages.

### Added — repository

- **Bootstrap** (`bootstrap/bootstrap.sh`, `bootstrap/bootstrap.ps1`) — idempotent sync scripts for macOS, Linux, and Windows. Fetch `AGENTS.md`, sparse-clone skills, merge settings, deploy the guard hook, update `.gitignore`. Safe to run every session.
- **`AGENTS.md`** — opinionated agent configuration covering:
  - Source-vs-consumer repo detection.
  - Session start checks (OS, model and effort level, Codex config, GitHub Actions version pins).
  - User profile placeholder (intended for customization in forks).
  - Agent roles (Claude Code implementer + Codex reviewer).
  - Task routing via `my-router`.
  - Codex MCP integration guide.
  - Writing defaults (~40 AI-tell words to avoid, punctuation rules, format preservation).
  - Formatting defaults, Git safety, shell command style.
  - GitHub Actions version standards (Node.js 24 minimums).
  - Environment notes.
  - Local skills precedence and cross-tool skill sharing conventions.
- **Skill: `implement-review`** — structured dual-agent review loop with content-type-specific lenses (code via Google eng-practices, paper via NeurIPS/ICLR/ICML, proposal via NSF Merit Review or NIH Simplified Peer Review), focused sub-lenses (code/security, paper/formatting, proposal/compliance, etc.), multi-target reviews, round history tracking, and reviewer save contract. Includes example reviews covering code, paper, and proposal tracks.
- **Skill: `my-router`** — context-aware skill dispatcher shipped as a template. Ships with `implement-review` as the only concrete routing rule plus an extension template so users register their own skills in a fork.
- **Guard hook** (`scripts/guard.py`) — PreToolUse hook that intercepts destructive Git and GitHub commands (`git push`, `git commit`, `git reset --hard`, `git merge`, `git rebase`, `gh pr merge`, `gh pr create`, etc.) and compound `cd <path> && <cmd>` chains with deliberately memorable warnings ("STOP! HAMMER TIME!", etc.) to prevent muscle-memory auto-approval. Tuned to keep read-only operations fast. Shell deletes (`rm -rf`) go through Claude Code's built-in permission prompts via the user-level `ask` settings, not the guard hook itself.
- **Claude Code commands** (`.claude/commands/`) — pointer files for both shipped skills (local-first, bootstrap fallback lookup).
- **Claude Code settings** (`.claude/settings.json`) — curated project-level permissions.
- **User-level settings** (`user/settings.json`) — permissions, guard hook wiring, and `CLAUDE_CODE_EFFORT_LEVEL=max` env default.
- **Tests** (`tests/`) — bootstrap contract validation, skill layout checks, settings merge preservation, and Windows + Linux bootstrap smoke tests running in GitHub Actions CI.
- **CI** (`.github/workflows/validate.yml`) — validation on `ubuntu-latest` and `windows-latest` with `actions/checkout@v6` and `actions/setup-python@v6`.
- **README** with problem framing, "What you get" benefit list, install paths (PyPI / npm / raw shell), day-to-day usage notes, collapsible reference sections, and maintainer context. Includes a hero image (`docs/hero.png`, with `docs/hero.html` source and a vendored avatar at `docs/avatar.jpg`), a Mermaid review-loop sequence diagram, the "agentic workflow this encodes" educational section, and GitHub-style `> [!NOTE]` / `> [!TIP]` callouts.
- **`CONTRIBUTING.md`** — scope and process for PRs, bug reports, and customizations (customizations go in a fork; upstream takes bug fixes and clear improvements).
- **`LICENSE`** — Apache 2.0.

### Added — packages

- **PyPI `anywhere-agents` 0.1.0** — installable via `pip install anywhere-agents` or `pipx run anywhere-agents`. Ships a thin CLI (`anywhere_agents.cli:main`) that downloads the latest shell bootstrap from the repo and runs it in the current directory. Supports `--dry-run`, `--version`, `--help`.
- **npm `anywhere-agents` 0.1.0** — installable via `npx anywhere-agents` or `npm install -g anywhere-agents`. Same behavior as the PyPI CLI, implemented in Node.js.
- **Agent-native install path**: users can tell their AI agent _"install anywhere-agents in this project"_ and the agent will pick whichever command (pipx, npx, or raw shell) matches the environment. The packages exist purely as agent-friendly entry points; install logic stays single-source in the shell bootstrap scripts.

### Not included (out of scope for 0.1.0)

- No YAML manifest or config file — files in the repo are the configuration.
- No selective-update tooling — Git is the subscription engine (`git pull upstream main`, `git cherry-pick`).
- No environment auto-install — `AGENTS.md` documents required tools; users install them.
- No multi-agent expansion beyond Claude Code + Codex — forks can add Cursor, Aider, Gemini CLI support.
- No profiles system — there is one configuration; forks are how other "profiles" exist.
- No marketplace, registry, or web UI.

### Review history

0.1.0 passed multiple rounds of `implement-review` with Codex before release. Resolved findings:

- **High** — Bootstrap scripts were silently running `git config --global core.autocrlf false`, reaching beyond the consuming repo. Removed; regression test added.
- **High** — Raw shell install path in README missed `mkdir -p .agent-config` and omitted the Windows PowerShell variant; fixed with both shells in a collapsible.
- **Medium** — `AGENTS.md` "What gets shared" table listed unshipped skills. Corrected to the actually-shipped set (`implement-review`, `my-router`).
- **Medium** — README maintainer paragraph overstated this repo's role relative to the private canonical source. Revised to describe this as the "sanitized public release of the working agent config."
- **Medium** — README / CHANGELOG / hero overstated the guard hook's scope by listing `rm -rf` alongside Git/GitHub commands. Corrected to distinguish guard-covered commands from settings-based permission prompts.
- **Low** — Trailing whitespace in `AGENTS.md`; `docs/hero.html` external avatar URL (vendored to `docs/avatar.jpg` for reproducibility). Both fixed.

[Unreleased]: https://github.com/yzhao062/anywhere-agents/compare/v0.1.9...HEAD
[0.1.9]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.9
[0.1.8]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.8
[0.1.7]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.7
[0.1.6]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.6
[0.1.5]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.5
[0.1.4]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.4
[0.1.3]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.3
[0.1.2]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.2
[0.1.1]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.1
[0.1.0]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.0
