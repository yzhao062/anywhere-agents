# Changelog

All notable changes to `anywhere-agents` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version tags apply uniformly to the repo content **and** the matching `anywhere-agents` PyPI / npm packages — they share one release stream. Consumers pinned to a specific tag get a stable snapshot; consumers on `main` receive ongoing updates.

## [Unreleased]

### Changed

- **Codex defaults track the current flagship model.** Session checks and setup guidance now recommend GPT-5.6 Sol, require Codex CLI 0.144.0 or newer for the GPT-5.6 family (0.142.5 and 0.143.0 return an upgrade-required HTTP 400), and set reasoning effort to `max` (maximum single-agent reasoning). `ultra` is documented as an explicit opt-in that keeps maximum reasoning but additionally enables automatic task delegation, so it is not merely a higher effort. The model is now stated as a policy rather than a frozen pin, so it does not go stale on each release. Main-tracking consumers receive the update on their next bootstrap; no package release is required.

## [0.7.8] — 2026-06-29

### Added

- **`prun` self-healing dispatch watchdog.** `dispatch-task.{sh,ps1}` now reaps a hung Codex worker instead of leaking it as a zombie. If the worker's tail capture stops growing for `PRUN_STALL_THRESHOLD` seconds (default `600`), or an optional hard wall-clock cap `CODEX_DISPATCH_TIMEOUT` (default `0` = disabled) is exceeded, the dispatcher kills the worker's whole process tree, exits `124`, and falls through to the existing result-loss backstop so `gather` still receives a non-empty `FALLBACK` result naming the trigger (`idle-stall` or `hard-timeout`). A non-empty result the worker already wrote is preserved, never clobbered. On Windows the watch-and-kill runs in a new sibling `reap-watch.ps1`: a single `.ps1` that launches a hidden worker, polls it, and force-kills its tree trips some Windows AV (e.g. Bitdefender) AMSI heuristics and is blocked at parse, so `dispatch-task.ps1` only launches the worker and spawns the watcher, mirroring the `implement-review` `dispatch-codex` + `stall-watch` split. The `.sh` keeps the watchdog inline (POSIX shells are not AMSI-scanned). `tests/test_dispatch_task.py` gains idle-stall, hard-timeout, and fallback-salvage coverage and is promoted into the STRICT cross-repo parity set; `SKILL.md` documents the new env knobs and the monitor-observes / dispatcher-reaps split.
- **`ci-mockup-figure` README / Markdown hero path.** The skill adds an agent-runnable headless-Chrome capture path (render an HTML mockup to a PNG and embed it in a README) alongside the existing LaTeX target, a scientific-vs-AI-product aesthetic callout (Helvetica/Arial plus an NPG/ggsci palette, with an explicit avoid-the-AI-tell note), a pre-flight checklist near the top, an SVG-in-flex capture-gotchas note, and README-hero readability/density guidance distinct from paper figures.

### Versions

- PyPI `anywhere-agents` `__version__` and `pyproject.toml` `version`: 0.7.7 -> 0.7.8
- npm `anywhere-agents` `package.json` `version`: 0.7.7 -> 0.7.8

SemVer: 0.7.7 -> 0.7.8, released as a patch. Adds prun's dispatch-side self-heal and ci-mockup-figure's README-hero path; no pack-manifest schema change and no change to the other packs.

## [0.7.7] — 2026-06-27

### Added

- **`prun` parallel-delegation skill.** A new shared skill that lets the coordinating session fan a task out into independent units that run in parallel on Codex (`codex exec`) and Sonnet workers, so the heavy work spends Codex and Sonnet quota rather than the coordinator's. Units may read or write code; code-writing units run inside a throwaway local clone with its remote removed, so a worker cannot commit or push to the real repository. The coordinator gathers each unit's result and diff, integrates the wanted changes, and asks before any commit. Ships `scripts/dispatch-task.{sh,ps1}` for Codex dispatch and `scripts/gather.{sh,ps1}` for result collection, with `tests/test_dispatch_task.py` and `tests/test_gather.py` covering both. Its web-access guidance routes a content fetch that a cloud fetcher may be blocked on to a Codex unit, which runs on the user's local machine and may reach the page from a local or residential-network IP, while Sonnet's `WebSearch` stays the path for broad discovery.
- **`prun` active stall monitoring plus a result-loss backstop (issue #5).** `dispatch-task.{sh,ps1}` now salvages a worker's raw output into its result file when the worker writes no structured result, so a dispatched unit is never silently lost, and records its dispatch PID for liveness. A new `monitor.{sh,ps1}` runs in the background and completes on the first actionable event (all units done, any stall, or any failure), waking the turn-based coordinator that would otherwise stay idle while a unit hangs. `tests/test_monitor.py` covers the done, fallback, stall, dead-dispatch, mixed, and timeout paths. `SKILL.md` adds a reconcile-before-integrate step and dependency-based autonomous concurrency guidance.
- **`prun` registered in the `aa-core-skills` pack.** prun now composes into `.claude/skills/prun/` on claude-code consumers like the other four core skills (`implement-review`, `my-router`, `ci-mockup-figure`, `readme-polish`), instead of resolving only through the bootstrap fetch cache. Its skill directory and command pointer join the wheel-bundled composer mirror at `packages/pypi/anywhere_agents/composer/`, and `scripts/check-parity.sh` gains prun in its shared-skill and wheel-mirror lists.

### Versions

- PyPI `anywhere-agents` `__version__` and `pyproject.toml` `version`: 0.7.6 -> 0.7.7
- npm `anywhere-agents` `package.json` `version`: 0.7.6 -> 0.7.7

SemVer: 0.7.6 -> 0.7.7, released as a patch. Adds prun's monitor and backstop (on `main` since v0.7.6) and registers prun into the existing `aa-core-skills` pack; no pack-manifest schema change and no change to the other packs.

## [0.7.6] — 2026-06-14

Patch release: bundles the `implement-review` review-depth and reviewer-channel work (on `main` since v0.7.5) with two `anywhere-agents` issue fixes (#12 composer crash on `=1`, #13 router extension point for consuming repos).

### Added

- **Code-review lens gains depth dimensions.** The `implement-review` code lens in `references/review-lenses.md` now covers complexity and over-engineering (YAGNI, speculative configurability, single-implementation indirection), long-term maintainability for durable code (hidden coupling, magic constants, implicit invariants, debuggability), and temporary-artifact hygiene (debug prints, commented-out code, dead scaffolding, scratch and backup files, stray TODOs). The maintainability dimension is down-weighted for short-lived code, and the lens states its longevity assumption.
- **Reviewers may parallelize and web-verify (opt-in, capability-gated).** The review prompt now invites a reviewer whose runtime supports it to parallelize a large multi-file review across sub-agents, and to use web search to confirm any finding that rests on a checkable external fact (citation, link, library or API behavior, version) before asserting it. The headless Claude reviewer backend (`dispatch-claude`) gains the built-in `WebSearch` and `WebFetch` tools; the empty-MCP isolation still holds because those are built-in tools, not MCP servers. The Copilot fallback backend stays offline by design (its `url()` permission is all-or-nothing, too broad for an auto-launched reviewer) and a contract test pins that.
- **Router extension point for consuming repos** (anywhere-agents#13). `my-router` now consults a bootstrap-proof consumer-local routing file (`routing-table.local.md` at the repo root, or a `## Routing` section in `AGENTS.local.md`) and merges those rows on top of the shipped table, with local rows winning on conflict. The prior "extend it in your fork (or in consuming projects)" guidance pointed at on-disk router copies that every bootstrap and `pack verify --fix` reverts, so a consuming repo had no durable place to register a project-local skill with the router.

### Fixed

- **`ANYWHERE_AGENTS_UPDATE=1` no longer aborts compose** (anywhere-agents#12). `compose_packs.py prompt_user_for_updates()` now accepts common truthy spellings (`1`, `true`, `yes`, `y`, `on`, plus the unset or empty default) as `apply`, and falsy spellings (`0`, `false`, `no`, `n`, `off`) as `skip`, case-insensitively and whitespace-trimmed. Previously any value other than the exact words `apply`, `skip`, or `fail` raised a `ValueError` and stopped composition, so a natural `=1` crashed with a raw traceback on the one consumer whose pack-lock happened to be stale. The exact word `fail` still raises `PackLockDriftAborted`; no truthy or falsy alias maps to that fail-closed path. Genuine typos still raise `ValueError`, now naming every accepted spelling. The wheel-bundled composer mirror is updated byte-identically and six regression tests pin the behavior.
- **Check 8 dispatch-tail scan cuts its near-100% false-positive rate.** The `implement-review` Auto-terminal health-check gains a line-level echo classifier (it strips line-numbered source citations and literal regex-source lines while preserving every real failure line) plus an intrinsic and generic two-tier split. Intrinsic failure forms such as HTTP or status 429/5xx and the Windows 1312 sandbox-runner error count on their own; generic words such as a bare "rate limit" count only when an error-frame token sits on the same or an adjacent line. A self-referential review that only echoes the failure vocabulary stops tripping the warning, while a real terminal, OS, or network failure still surfaces.

### Versions

- PyPI `anywhere-agents` `__version__` and `pyproject.toml` `version`: 0.7.5 -> 0.7.6
- npm `anywhere-agents` `package.json` `version`: 0.7.5 -> 0.7.6

SemVer: 0.7.5 -> 0.7.6, released as a patch. Reviewer-depth and channel additions to a shipped skill plus two robustness fixes; no API or pack-manifest schema change.

## [0.7.5] — 2026-06-13

Patch release: closes three `agent-config` issues and bumps the bundled `agent-style` pin to `v0.3.6` so the default composer pin matches the shipped RULE-07 pack.

### Fixed

- **Codex reviewer no longer hangs on a user MCP server** (agent-config#1). The `implement-review` Auto-terminal Codex backend now runs `codex exec` with `--ignore-user-config` (default on; opt out with `CODEX_DISPATCH_ISOLATE_MCP=off`), so a user-level Codex MCP server (for example `node_repl`), plugin, or hook can no longer auto-start inside the headless reviewer, spawn a nested `codex`, and hang the round with an empty review. This is the Codex-side parallel to the v0.7.2 Claude-backend isolation. A narrower `-c mcp_servers={}` was tried first, but codex 0.139 deep-merges it and the configured servers still start. Because `--ignore-user-config` also resets the reasoning effort, the dispatcher re-passes `-c model_reasoning_effort` (default `xhigh`; override `CODEX_DISPATCH_REASONING`) so the reviewer retains a nonzero reasoning level. Here `xhigh` is the dispatcher's cross-model compatibility floor; the model stayed on Codex's built-in default (`gpt-5.5` at that release). The user's `service_tier` and any custom `model_provider` are not re-passed (hardcoding a tier would make every round fail for a consumer whose account lacks it); a review that depends on them should opt out, with full config-preserving isolation (a temp `CODEX_HOME` minus the MCP tables) tracked as a follow-up. New contract tests freeze the default-on isolation, the reasoning re-pass, and the opt-out across `dispatch-codex.sh` and `dispatch-codex.ps1`.
- **Quota readout is now self-describing** (agent-config#2, agent-config#3). `scripts/agent-quota.py` labels each window as `94% left` rather than a bare `94%`, and the `implement-review` Prerequisites step states that the figures are remaining headroom. A high figure now reads as quota still available, which removes the inverted reading that had nearly driven a wrong channel-downgrade decision.

### Changed

- **Bundled `agent-style` pin moves to `v0.3.6`.** `bootstrap/packs.yaml` and the wheel composer mirror now default the `agent-style` pack to `v0.3.6`, matching the user-level and project pins that already moved. Consumers that fall back to the bundled default stop seeing the version-mismatch warning against a `v0.3.6` user pin. The RULE-07 antithesis wording shipped in `agent-style v0.3.6`.

### Versions

- PyPI `anywhere-agents` `__version__` and `pyproject.toml` `version`: 0.7.4 -> 0.7.5
- npm `anywhere-agents` `package.json` `version`: 0.7.4 -> 0.7.5

SemVer: 0.7.4 -> 0.7.5, released as a patch. Two bug fixes to shipped scripts and the reviewer dispatch, plus a bundled-pin bump; no API or pack-manifest schema change.

## [0.7.4] — 2026-06-13

Patch release: mirrors the agent-config antithesis writing rule into the bundled `AGENTS.md`. It also extends `implement-review` so prose diffs run the writing-rules audit before review.

### Added

- **Antithesis rule in Formatting Defaults.** `AGENTS.md`, `CLAUDE.md`, and `agents/codex.md` now tell agents to avoid emphatic antithesis. Examples include "X, not Y", "not just X, but Y", and "it is not X, it is Y". The rule keeps negation available when it rejects a specific alternative.
- **Writing-rules audit for prose diffs.** The Pre-Review checklist adds a Prose row for `agent-style review --audit-only` mechanical hits. It sends semantic RULE-07 checks to the style-review host pass. If that pass is unavailable, it falls back to template grep. `references/review-lenses.md` now applies General-lens item 7 to any prose diff.

### Compatibility

- No CLI, install-flow, or pack-manifest change. The bundled `agent-style` pack pin stays at `v0.3.5`. Consumers that specifically want RULE-07 wording from the `agent-style` pack block can pin `agent-style` to `v0.3.6` in `agent-config.yaml`.

## [0.7.3] — 2026-06-01

Patch release: removes a hardcoded maintainer-specific Python path from two shipped scripts and replaces it with environment-derived conda discovery.

### Fixed

- **`scripts/_python` and `skills/implement-review/scripts/health-check.ps1` no longer hardcode a `miniforge3/envs/py312` interpreter path** (#10). Both ship in the package and tripped the release leak sweep. They now discover a conda/Miniforge interpreter from the environment (`CONDA_PREFIX`, then `CONDA_ROOT`, a resolvable `conda`/`mamba` launcher, and a `$HOME` `conda-meta` signature scan), with no hardcoded env name or install dir, before falling through to `py -3` / `python3` / `python` on PATH (WindowsApps shims skipped). Verified end-to-end through the implement-review loop (Codex review, two rounds), cross-repo STRICT parity, and a clean leak sweep.

### Changed

- **`_python` interpreter preference is now environment-derived, not pinned to a `py312` env.** A hook or tool that relied on `_python` resolving a *specific* conda env (for example, one carrying an editable package) should set `ANYWHERE_AGENTS_PYTHON` to that env's interpreter; it is `_python`'s explicit override, honored before any discovery. With `CONDA_PREFIX` unset and several envs present, discovery prefers the conda base interpreter, which is correct for the stdlib-only first-party hooks but will not carry a package installed only in a named env.

### Versions

- PyPI `anywhere-agents` `__version__` and `pyproject.toml` `version`: 0.7.2 -> 0.7.3
- npm `anywhere-agents` `package.json` `version`: 0.7.2 -> 0.7.3

SemVer: 0.7.2 -> 0.7.3, released as a patch. Leak-hygiene fix to shipped scripts plus a documented interpreter-preference change with the `ANYWHERE_AGENTS_PYTHON` escape hatch; no API change.

## [0.7.2] — 2026-05-31

Ships everything that accumulated on `main` after v0.7.1: the cross-agent `implement-review` reviewer backends (Claude Code and GitHub Copilot), the cross-agent quota / usage view, and the Windows fixes that make the Claude reviewer reliable under the Codex-primary configuration.

### Added

- **`implement-review` Claude Code reviewer backend** (#8): when Codex (or the user) is the primary implementer, the Auto-terminal can dispatch headless `claude -p` as the reviewer. Claude reviews the staged snapshot with `Read,Bash` tool access and the dispatcher saves its final answer to `Review-Claude-Code.md`, avoiding the unattended Windows hangs seen with path-scoped `Write(...)` / `Edit(...)` and `--allowedTools` preapproval patterns. A sibling `_claude_guard.ps1` refuses to dispatch when the orchestrator is Claude Code itself (self-review).
- **`implement-review` GitHub Copilot CLI reviewer backend** for the Auto-terminal channel, mirroring the Codex backend's state-dir / stall-watch / health-check contract.
- **Cross-agent quota / usage view**: surfaces Claude Max and Codex 5h / weekly quota together, with portable deploy and implement-review surfacing.
- **Bootstrap auto-updates the Codex npm CLI** during the session-start refresh.

### Fixed

- **Claude reviewer invocation isolation** (Windows): headless `claude -p` now runs with `--strict-mcp-config --mcp-config <empty>` plus `--setting-sources project,local`, so a user-level Codex MCP server and user hooks can no longer load inside the reviewer. This closes the `Codex -> dispatch-claude -> claude -p -> Codex MCP -> codex.exe` recursion that hung the dispatch and produced an empty review.
- **Windows PowerShell dispatcher** launches `claude -p` through `ProcessStartInfo` instead of a transient `.cmd` helper (removes path/redirect quoting fragility) and pins `StandardInputEncoding` to UTF-8 (no BOM) so non-ASCII staged diffs reach the reviewer intact.
- **`dispatch-claude.sh`** uses a bash 3.2-safe empty-array expansion under `set -u` (macOS CI).
- **Pack pointer 3-path lookup** (`_POINTER_TEMPLATE`, #6): generated command pointers resolve skills through the `skills/` then `.claude/skills/` then `.agent-config/repo/skills/` order.
- **SessionStart banner** (#7): source-aware debounce and a first-arm-only guard stop the banner from re-firing on rapid SessionStart events.

### Docs

- 3-path skill lookup documented as a cross-agent rule; new tool-use reliability, memory-persistence, and copy-paste formatting guidance in `AGENTS.md`; a Codex `conversationDetailMode = "DEFAULT"` recommendation and a "do not nest `pwsh -Command` with `$` variables" shell rule.

### Internal

- `package-smoke` npm / PyPI install retry budget widened to absorb CDN lag.
- Cross-repo (`agent-config` and `anywhere-agents`) STRICT parity and source-to-wheel composer-mirror parity verified clean for the implement-review tree via `scripts/check-parity.sh`.

### Versions

- PyPI `anywhere-agents` `__version__` and `pyproject.toml` `version`: 0.7.1 -> 0.7.2
- npm `anywhere-agents` `package.json` `version`: 0.7.1 -> 0.7.2

SemVer: 0.7.1 -> 0.7.2, released as a patch. A strict reading would be a minor bump, since this release debuts the Claude Code and GitHub Copilot reviewer backends and the cross-agent quota view (new user-visible capabilities) that landed on `main` after v0.7.1; they are bundled here with the Windows reviewer-isolation and stdin-encoding fixes. No breaking changes.

## [0.7.1] — 2026-05-21

A **tool-agnostic approval guard**. The PreToolUse `guard.py` now classifies risk for both the `Bash` and `PowerShell` tools, so the approval prompt fires only for the dangerous set (destructive git, gh publish/release, package publish, file/device destruction) and routine PowerShell no longer prompts once `PowerShell(*)` is allowed. This patch also ships the OIDC auto-publish workflow that landed on `main` after the v0.7.0 tag.

### Added

- **`PowerShell(*)` in the user allow-list** (`user/settings.json`): pairs with the existing `Bash(*)` so the native permission layer is allow-by-default on both shells and `guard.py` becomes the sole risk arbiter. Ships together with the PowerShell file-destruction classifier below, never before it, so no silent-delete window opens.
- **Tool-agnostic risk classifier** (`scripts/guard.py`): one classifier runs for `Bash` and `PowerShell`. It keys on the exact leading token of each sub-command (split on `;` / `&&` / `||` / `|`), never a substring scan, so quoted strings like `echo "rm -rf"` stay safe. It strips transparent prefix runners (`sudo`, `doas`, `env`, `command`, `nohup`, `setsid`, inline `VAR=VALUE`) and sees through command-carrying wrappers (`ssh`, `bash`/`sh`/`zsh -c`, `docker exec`/`run`, `pwsh`/`powershell -Command`, Windows `cmd /c`/`/k`, `timeout`, `xargs`) up to `MAX_WRAPPER_DEPTH`, asking when nesting exceeds it.
- **New mandatory ask classes**: package publish (`npm`/`pnpm`/`yarn publish`, `twine upload`, `python -m twine upload` including versioned interpreters), `gh release create/delete/upload/edit`, and PowerShell `Remove-Item` (+ aliases `rm`/`del`/`rd`/`rmdir`) recursive deletes. `git checkout --` joins the destructive-git set.

### Changed

- **Mandatory ask set is non-bypassable by any env var**: destructive git, destructive/publish gh, package publishes, and file/device destruction have no agent-side reroute, so the `ask` prompt stays the contract. Encoded PowerShell (`-EncodedCommand`) fails closed to ask. `python -c`, the low-frequency prefixes `nice`/`ionice`/`stdbuf`/`time`, and custom/private wrappers stay opaque documented non-goals.

### Internal

- **OIDC auto-publish workflow** (`be6ce22`, `fd07356`, `777da77`, first shipped in this release): `.github/workflows/publish.yml` uploads to PyPI and npm via OIDC Trusted Publishing on `release: published`, dropping long-lived tokens from the happy path. `RELEASING.md` documents the flow.
- **STRICT byte-identical mirrors** (cross-repo `agent-config` and `anywhere-agents`): `scripts/guard.py` + `tests/test_guard.py` (+549 / +496 net over five code-review rounds). The classifier passed 290 guard tests on Windows and ARM64 Linux.

## [0.7.0] — 2026-05-19

The visible v0.7.0 theme is **Noise audit (Round 6 reroute criterion)**, bundled with **agent-fungibility Phase 0.5** and a **bootstrap git preflight**. Three slices in one release: keep `deny` decisions on writing-style and compound-cd guards (instead of demoting to `ask`) and add inline `Suggested rewrite:` reroutes that let autonomous flows lift the block in one model turn; promote the cross-vendor resilience principle from `agent-config/CLAUDE.local.md` (maintainer-local) into shared `AGENTS.md`; reject pre-2.25 git up front in `bootstrap.{sh,ps1}` so a real consumer's cryptic `unknown option 'sparse'` failure becomes a one-line actionable error.

### Added

- **Per-guard escape envs for the noise-audit guards** (Slice A): `AGENT_STYLE_HOOK=off` disables the writing-style guard only; `AGENT_COMPOUND_CD_HOOK=off` disables the compound-`cd` guard only. The legacy `AGENT_CONFIG_GATES=off` keeps its existing blanket scope (writing-style + banner). Useful in meta-discussion writes (a style-guide document quoting banned words; a CHANGELOG citing one as an example) without disabling unrelated guards. Destructive git / gh approval is **not** bypassable by any env var; those have no agent-side reroute, so the `ask` prompt remains the contract. The advertised set lives in `scripts/guard.py:_ESCAPE_HATCH_ENV_NAMES` and a static literal-scan test forbids future `AGENT_*_HOOK` literals outside the constant.
- **`Suggested rewrite:` lines in deny messages** (Slice A): writing-style denies include a `\`word\` -> alts; ...` rewrite block built from a per-word alternative dictionary (~46 entries; e.g., `encompass -> cover, include`; `pivotal -> key, central`). Compound-`cd` denies include `Suggested rewrite: git -C <path> <cmd>` for git, a split-into-steps suggestion for mixed `&&` / `||`, or a failure-handler restatement for `cd <path> || <cmd>`. The deny stays a hard block (no `ask` demotion) so an autonomous flow gets a deterministic NO plus the concrete alternative and reroutes in one model turn instead of inferring it.
- **Composer noise-budget gate** for third-party packs (Slice A, `scripts/packs/noise_budget.py` + wheel mirror): at compose time, third-party `kind: hook` entries with `decision: deny` AND empty `reroute_hint` AND impact `low` / `medium` raise a non-blocking warning in the compose summary. Per-pack silencer: `noise-audit-override: accept-deny` in `agent-config.yaml`. First-party `guard.py` is bootstrap-deployed outside the pack system in v0.7.0; the first-party hook invariant is deferred to v1.0 with `guard.py` extraction to `agent-behave`.
- **`reroute_hint` manifest field** (Slice A, `packs/schema.py` all three copies): optional `reroute_hint: str` on `kind: hook` entries. Documented as the source-of-truth string for the hook's `Suggested rewrite:` deny output so the manifest declaration and runtime deny message stay in lock-step. Non-hook entries with `reroute_hint` fail validation; the v0.5.4 manifest-absent default-name fallback remains BC-preserved.
- **`AGENTS.md` § Agent Fungibility** (Slice B; immediately after § Agent Roles, ~10 lines): cross-vendor resilience principle. Not 1:1 replication; core functions must work when one agent is absent (service outage, regional block, quota exhaustion, hardware-induced refusal) or when roles are reversed (Codex-primary + Claude-gatekeeper as a supported configuration). Where an ergonomic helper exists for one agent only, the function must still be reachable via underlying primitives. Promoted from `agent-config/CLAUDE.local.md` (maintainer-local placeholder) to the shared file so every consumer receives the same intent through bootstrap. Generated `CLAUDE.md` and `agents/codex.md` regenerated to match. Phase 1+ of the fungibility refactor (SKILL.md reviewer-agnostic; `dispatch-claude.{sh,ps1}`; health-check Claude-tail patterns) stays open.
- **`bootstrap/bootstrap.{sh,ps1}` git preflight** (Slice C): `check_git_preflight` / `Invoke-GitPreflight` runs before any `git` invocation in both bootstrap paths (fresh sparse-clone AND existing-repo refresh: `git -C remote set-url`, `git -C pull --ff-only`, `git -C sparse-checkout set`). Hard-fails only on confirmed `git < 2.25` (the `--sparse` flag floor; `--filter=blob:none` is the older partial-clone option from Git 2.19+). Platform-specific install lines: macOS `brew install git`; Debian / Ubuntu `sudo apt update && sudo apt install -y git`; Windows `https://git-scm.com/download/win`. Default-pass with stderr warning on parse failure so unusual `git --version` strings (alpha builds, `2.30.1.windows.1`, `(Apple Git-141)`, `2.50.0.rc1`) do not block already-modern systems. Escape hatch: `AGENT_CONFIG_SKIP_GIT_PREFLIGHT=1`.
- **`docs/install.md` and `docs/faq.md` git prerequisite + FAQ** (Slice C): install Prerequisites entry now spells out `git clone --filter=blob:none --sparse` and the 2.25 floor with platform install commands; FAQ adds a new entry covering the `git is not installed` / `git X.Y is too old` exit messages and the `AGENT_CONFIG_SKIP_GIT_PREFLIGHT=1` escape.
- **`tests/test_bootstrap_preflight.py`** (Slice C, STRICT mirror with `agent-config`): 13 cases covering missing-git binary, `2.24.0` / `1.9.5` fail, `2.25.0` boundary, modern `2.50.0`, suffix forms (`2.30.1.windows.1`, `2.34.1 (Apple Git-141)`, `2.50.0.rc1`), parse-fail default-pass, empty-version default-pass, existing-repo-still-gated, skip-env, and platform-hint coverage. POSIX uses a per-test sandbox bin dir with curated tool symlinks (no `git`) so the missing-git scenario survives CI runners that ship git in `/usr/local/bin` or `/opt/homebrew/bin`. `scripts/check-parity.sh` adds the file to the cross-repo STRICT shared-contract tests list.

### Changed

- **Round 6 supersedes the FP-rate criterion** (`pack-architecture.md` v0.7.0 section + Round 2 noise-budget decision): the autonomous-mode reroute criterion replaces the false-positive-rate heuristic for choosing `deny` vs `ask`. A guard with a concrete agent-side reroute stays `deny` and the deny message embeds the reroute; only guards without a reroute (destructive git / gh) remain `ask`. The composer warns rather than hard-fails on noisy third-party packs, silenceable per-pack with `noise-audit-override: accept-deny`. No `deny -> ask` demotions of writing-style or compound-cd hooks.
- **`manifest_path.exists()` gate in `compose_packs.py`** (Slice A): distinguishes a missing `pack.yaml` (silent bundled-default fallback for default-name packs, BC-preserved from v0.5.4) from a malformed `pack.yaml` (raises `ComposeError("failed schema validation")`). Restores the v0.6.0 parse-time rejection that v0.5.0 silently dropped. Round 2 regression caught a `except schema.ParseError: remote_manifest = None` that swallowed schema errors on the inline-source default-name path.

### Fixed

- **Compound-`cd` splitter** (Slice A, `scripts/guard.py:_quote_aware_split_on_operators`): a character-walker that honors `'...'`, `"..."`, and `\<char>` escapes replaces the prior quote-blind regex. `cd "a || b"` no longer misclassifies the literal path as a compound chain; `cd /tmp || echo nope && ls` no longer drops the success branch. The deny path branches on detected operator set (mixed `&&` + `||`, `||` failure handler, `&&` git, generic), producing a Suggested rewrite tailored to each shape rather than pretending `echo nope` is the command to run inside the directory.
- **`schema.py` 3-copy mirror gap** (Slice A): the `reroute_hint` validation block lives in all three locations (`scripts/packs/schema.py` active composer, `packages/pypi/anywhere_agents/composer/scripts/packs/schema.py` wheel mirror, `packages/pypi/anywhere_agents/packs/schema.py` CLI helper). Round 1 review caught a Medium where only the CLI-helper copy was updated; the active composer would have accepted malformed manifests at install time.
- **Generator `--root` invocation** (Slice A R4 lesson, carried into Slice B): `scripts/generate_agent_configs.py` defaults `--root=Path.cwd()`; cross-repo regeneration must pass `--root <target>` explicitly. Slice A R4 caught a state where aa `CLAUDE.md` / `agents/codex.md` were stale relative to staged aa `AGENTS.md` because the generator was invoked from `agent-config`'s cwd. Slice B uses explicit `--root` for both regenerations.

### Internal

- **STRICT byte-identical mirrors** (cross-repo `agent-config` <-> `anywhere-agents`): `scripts/guard.py` + `tests/test_guard.py` (Slice A, +376 / +404 net); `bootstrap/bootstrap.{sh,ps1}` (Slice C; promoted to STRICT in v0.6.1, now carry the preflight helpers); `scripts/check-parity.sh` (Slice C, adds preflight test to the strict_test_files list); `tests/test_bootstrap_preflight.py` (Slice C, new STRICT entry).
- **STRICT (aa-internal) source<->wheel parity**: `scripts/packs/noise_budget.py` + `packages/pypi/anywhere_agents/composer/scripts/packs/noise_budget.py` (Slice A, new entry); `scripts/compose_packs.py` + wheel mirror; all three `schema.py` copies (Slice A).
- **5-round implement-review on Slice A** (Codex via Auto-terminal): R1 caught noise_budget dead-code wiring, schema 3-copy mirror gap, `AGENTS.md` mechanical-enforcement contract drift, compound-cd parser quote-blind and mixed-operator bug, and a low-priority quote-agnostic env-literal scan. R2 reopened two of them on regression repros. R3 caught architecture-doc drift in the noise-budget contract. R4 caught the generator-cwd gotcha (stale aa generated files) plus residual em-dash sites. R5 returned 0 findings (commit-ready). 3-round implement-review on Slice B+C: R1 caught a POSIX `_stripped_env` PATH-stripping bug (test fail-open on hosts where Git Bash's `usr/bin` is not pre-included), version-floor wording overstating the 2.25 cutoff for `--filter=blob:none`, and an em-dash. R2 reopened em-dash on the mirrored test docstring. R3 returned 0 findings (commit-ready).
- **CI hotfix after merge** (`tests/test_bootstrap_preflight.py` POSIX sandbox): ac CI on `macos-latest` caught a fail-open in `test_missing_git_binary_fails` because `_stripped_env` left `/usr/local/bin` (where macOS runners ship brew git) in the test PATH. Fix builds a per-test sandbox bin dir with curated symlinks (sed, uname, printf, tr, grep, cat, mkdir, echo, rm, dirname, basename, test, ls, head, tail, sh) and uses ONLY that dir on POSIX. Windows already guarded the analogous path via per-dir reject list (`Git\cmd`, `Git\bin`, `Git\mingw64\bin`). Pushed as a hotfix on top of the v0.7.0 commits.

### Versions

- PyPI `anywhere-agents` `__version__` and `pyproject.toml` `version`: 0.6.1 -> 0.7.0
- npm `anywhere-agents` `package.json` `version`: 0.6.1 -> 0.7.0

SemVer: 0.6.1 -> 0.7.0 (minor). Three new user-visible capabilities (per-guard escape envs + Suggested rewrite lines; agent-fungibility principle in shared `AGENTS.md`; bootstrap git preflight) plus new manifest schema field (`reroute_hint` on hook entries) and new composer behavior (noise-budget warning block). No breaking changes: the existing single `AGENT_CONFIG_GATES=off` escape keeps its v0.6.x scope; existing manifests without `reroute_hint` parse unchanged; existing bootstrap behavior on git `>= 2.25` is unchanged; consumers on git `< 2.25` get a one-line install command instead of the prior cryptic sparse-clone failure.

## [0.6.1] — 2026-05-18

The visible v0.6.1 theme is **implement-review Auto-terminal channel + portable statusLine deploy**. Operational hardening and additive capability between v0.6.0 (canonical apply command) and the named v0.7.0 noise audit. No closed-decision scope from `pack-architecture.md` is touched.

### Added

- **Implement-review Auto-terminal channel (opt-in `/implement-review auto`)**: Third sub-channel for the implement-review skill alongside Terminal-relay (default) and IDE Plugin. Dispatches Codex via `codex exec --sandbox danger-full-access` as a background subprocess so review rounds run without manual copy-paste. Path selection: 4-tier trigger (default Terminal-relay; slash-arg opt-in `/implement-review {cli,auto,auto-terminal}` plus case-insensitive plain-phrase opt-in with a 4-word negation guard; IDE Plugin; MCP forward-compat slot). Cross-platform: 7 dispatch scripts (`dispatch-codex.{sh,ps1}`, `stall-watch.{sh,ps1}`, `health-check.{sh,ps1,py}`) under `skills/implement-review/scripts/`. Byte-identical prompt invariant across all three channels.
- **Phase 2.0 Health-check prologue (Auto-terminal only)**: 9 structural Health checks (review file exists / mtime later than pre-dispatch + state-dir snapshot / round marker matches / size threshold / Verification notes present / file scope matches prompt / review-text suspicious-phrase scan / dispatch-tail tool-failure scan / stall-warning absent) plus 3 Substance heuristics (time-to-completion floor, anchor density, scope-challenge engagement). Phase 1d coordination silent-intake rule: silent-advance only when all 7 conditions hold; any WARN blocks silent advance with a one-line note and Proceed / Downgrade prompt. Documented false-positive tuning principle with known-noise shapes (Check 8 in `http|status|limit|createprocessasuserw` shape = SKILL.md echo meta-noise; WSL-stub-bash 1312 burst on Windows = graceful-fallback noise) and per-pattern breakdown emission so downstream recognizers can fast-Proceed on known shapes.
- **Phase 2.5 verify-factual-claims doctrine** (codified in SKILL.md): High-priority findings with checkable factual claims trigger verification (citation existence, code behavior, link reachability, page-limit, compile error, anonymization leak). Medium findings produced via the embedded-diff retry channel (sandbox-strict fallback path) are also auto-verified. Outcomes (Verified / Refuted / Inconclusive) feed back into the next round's Prior findings block.
- **`scripts/statusline.py`**: Python statusLine renderer for the Claude Code status row. Reads `rate_limits` from statusLine stdin (Claude Code v2.1.80+, Pro/Max subscribers) for 5-hour and weekly quota remaining plus reset countdown. Tails the most recent `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` for Codex's `payload.rate_limits` (primary=5h, secondary=7d) and renders Codex quota inline. Stale windows flagged `(stale)`; missing data renders as em-dash. Cross-platform: tested byte-identical output on Windows 11 (Git Bash + Miniforge Python, ~155 ms per render) and Ubuntu 24.04 ARM64 / DGX Spark (~194 ms). Example output:

  ```text
  🤖 Opus 4.7 · 5h 78% (3h 4m) · 7d 51% (15h 4m)  |  Codex 5h 89% (3h 25m) · 7d 90% (4d 23h)
  ```
- **`bootstrap/bootstrap.{sh,ps1}`** deploy block (mirror of the `guard.py` pattern) copying `scripts/statusline.py` to `~/.claude/statusline.py`. `user/settings.json` gains a `statusLine` block wiring the renderer via the existing `_python` wrapper, so the Windows Store python shim cannot capture the invocation. The user-settings merger lands the command on every consumer's `~/.claude/settings.json` without overwriting unrelated keys.
- **Phase 0 size-gate fixture (`tests/test_bootstrap_size.py`)**: subprocess-invokes `scripts/generate_agent_configs.py` against a tmp consumer seeded with upstream `AGENTS.md` (baseline only, no passive packs); loops over `AGENT_FILE_CEILINGS` asserting each per-agent file is under the configured hard ceiling (default 75 KB). Subset assertion against `generate_agent_configs.AGENTS` enforces agent-fungibility: a new agent added without a ceiling entry fails the test loudly. Soft-warning tiers at 50 KB (Pragmatic) and 40 KB (Aggressive); env overrides via `ANYWHERE_AGENTS_SIZE_HARD_CEILING_KB`, `_PRAGMATIC_WARN_KB`, `_AGGRESSIVE_WARN_KB`. Discovered automatically by `.github/workflows/validate.yml` matrix; no new workflow.
- **`scripts/remote-smoke.sh`** "User-level statusLine deployed" step asserts the renderer file at `~/.claude/statusline.py`, the `statusLine` entry in the merged `~/.claude/settings.json`, the `.claude/statusline.py` command reference, AND a post-install mtime gate that fails if `~/.claude/settings.json` was not rewritten by the install (catches reused-`$HOME` stale-entry false-pass on release-gate machines).
- **`scripts/check-parity.sh`**: `scripts/statusline.py` added to the STRICT byte-identical list. aa-side auto-detect added: when invoked from an `anywhere-agents` checkout with a sibling `agent-config/` directory present, the script swaps roots so the comparison is genuinely cross-repo (closes the silent self-comparison failure mode where `bash scripts/check-parity.sh` from aa would always pass without checking ac).
- **Cross-repo STRICT shared-contract tests block** in `scripts/check-parity.sh`: 4 shared-contract test files (`tests/test_{dispatch_codex,health_check,guard,prompt_byte_parity}.py`) added to the STRICT byte-identical list. Restores the property that a shared-script contract change must mirror tests in the same commit; closes the drift class where every shared-skill change broke aa CI until manual `cp` re-aligned the tests.
- **Example reviews in RTD nav** (`docs/skills/references/example-reviews/`): 4 example-review wrappers (`code-phased`, `paper-verification`, `paper-multi-target`, `proposal-nsf`) exposed via `include-markdown`, pulling from the skill source at `skills/implement-review/references/example-reviews/`. SKILL.md's "See example-reviews for expected depth" pointers now resolve inside the RTD nav instead of requiring a click-through to GitHub.

### Changed

- **`AGENTS.md` Phase 1.A inline trim** (-4 KB / -11%): three cuts without new files or bootstrap mechanics changes. Pack-deployment algorithm compressed to a tight sketch (preserves paths + count algorithms + emit format). Codex MCP Integration trimmed (kept registration command + `config.toml` block + Windows PATH note + approval-policy rationale). One-time `npm`/`winget` autoUpdates-false caveat removed (bootstrap now self-heals via `autoUpdatesProtectedForNative`). Measured deltas via the new Phase 0 size-gate: `AGENTS.md` 35.5 KB → 31.6 KB; `CLAUDE.md` 31.6 KB → 30.2 KB (Claude Code 40 KB warning headroom +1.4 KB); `agents/codex.md` 33.7 KB → 30.2 KB.
- **`bootstrap/bootstrap.{sh,ps1}` promoted to STRICT parity** (was BY-DESIGN): both bootstrap variants are now byte-identical between `agent-config` and `anywhere-agents`, locked by `scripts/check-parity.sh`. Forces the maintainer to keep the two repos' bootstrap behavior aligned at the line level rather than at the "same shape, different defaults" level.
- **`docs/skills/implement-review.md` mermaid diagram refresh**: filename `CodexReview.md` renamed to `Review-Codex.md` throughout; entry node shows `/implement-review auto` (the canonical invocation); reviewer node names all three channels (Auto-terminal / Terminal-relay / IDE Plugin); diagram branches on Auto-terminal to gate the Phase 2.0 health-check node (matches SKILL.md Auto-terminal-only scope, with non-Auto-terminal paths flowing directly to Phase 2.1 intake); Phase 2.5 is a separate post-intake node with explicit trigger doctrine spelled out below the diagram.

### Fixed

- **Codex Windows 1312 sandbox bypass**: Auto-terminal dispatch passes `--sandbox danger-full-access` to `codex exec`, bypassing Codex 0.130.0's `workspace-write` sandbox `CreateProcessAsUserW failed: 1312` bug (Codex spawning its own git / grep / pwsh subprocesses lost the elevation token through `CreateProcessAsUserW`). `CODEX_DISPATCH_SANDBOX` env var overrides for CI / sandbox-strict environments. Trust model aligned with Terminal-relay (which already runs with full access via the user's own Codex window). SKILL.md doctrine: sandbox-flag is primary, embedded-diff retry is sandbox-strict fallback; Phase 2.5 trigger extended to Medium-from-retry-channel.
- **Auto-terminal PowerShell dispatch hardening**: PowerShell variant now writes a transient `<state-dir>/run-codex.cmd` helper that executes `codex exec --sandbox <mode> - > <tail> 2>&1 < <prompt-file>` and invokes it via the call operator. `cmd`'s shell-level handle redirections preserve the byte-identical prompt invariant; the `CreateProcess` spawn route (vs the prior `CreateProcessAsUserW` via `Start-Process -RedirectStandardInput`) lets Codex's child processes inherit the logon-session token cleanly. Helper is UTF-8 with a `chcp 65001` prefix so non-ASCII paths survive `cmd`'s codepage layer; `%` chars in path values escape to `%%` so `cmd` does not env-expand them.
- **`scripts/guard.py` PowerShell hardening**: four review rounds closed 13 bypass shapes (env-value semicolons, double-quoted `$()` / `$env:` in env values and script paths, splatting, redirect tokens `>` `2>` `<`, and `LF` / `CRLF` / `CR` newline statement-breaks). Auto-allow for the implement-review Auto-terminal helper script (`auto-watch.ps1`).
- **`bootstrap/bootstrap.{sh,ps1}` composer-missing fallback**: pre-flight check falls through to the verbatim-`AGENTS.md` path when neither `compose_packs.py` nor `compose_rule_packs.py` is present in the sparse clone. Defense in depth for any future case where a sparse-clone misses both composers.
- **`tests/test_dispatch_codex.py::test_codex_invoked_exec_dash_not_review` assertion shape**: accepts the new `exec [flags] -` argv shape (changed from `args[1] == '-'` to `args[-1] == '-'`). The dispatcher now puts `--sandbox <mode>` between `exec` and `-` after the sandbox-flag fix.
- **Py3.9 compat for `tempfile.TemporaryDirectory` cleanup** in tests: the `_temp_dir()` helper paper-walls the Py3.10+ `ignore_cleanup_errors` kwarg so the Ubuntu py3.9 CI lane stays green.

### Internal

- **Cross-repo STRICT mirror sync** (aa-internal wheel-bundled composer at `packages/pypi/anywhere_agents/composer/`): Phase B+C scripts mirrored (`dispatch-codex.{sh,ps1}`, `stall-watch.{sh,ps1}`, `health-check.{sh,ps1,py}`); guard-hardening + sandbox-flag mirrors for `composer/skills/implement-review/SKILL.md` and `composer/skills/implement-review/scripts/dispatch-codex.ps1`; closes a pre-existing aa source ↔ wheel drift in the `SKILL.md` mirror. `.claude/commands/implement-review.md` `$ARGUMENTS` slash-arg forwarding (frontmatter + body) also caught up to ac state. `scripts/check-parity.sh` aa-internal block comment fix and cross-repo STRICT header inventory cleanup.

### Versions

- PyPI `anywhere-agents` `__version__` and `pyproject.toml` `version`: 0.6.0 → 0.6.1
- npm `anywhere-agents` `package.json` `version`: 0.6.0 → 0.6.1

SemVer: 0.6.0 → 0.6.1 (patch). All changes are additive (new opt-in Auto-terminal channel, new statusLine renderer, new size-gate test, new STRICT entries), operational hardening, or fixes / docs. No breaking changes to the existing Terminal-relay flow, existing bootstrap deploys, or the v0.6.0 canonical apply command.

This release was itself reviewed via the new `/implement-review auto` channel across 5 rounds total (ac 2, aa 3) for the statusLine + freshness-gate slice; `bash scripts/check-parity.sh` STRICT clean from both ac and aa checkouts; pre-push real-agent smoke (Codex + Claude Code) passed on both repos.

## [0.6.0] — 2026-05-03

The visible v0.6.0 theme is **bare `anywhere-agents` is the canonical apply command**. Bootstrap, deploy, drift apply, and generator regen all collapse into a single verb. `pack verify --fix` and `pack update` survive as compatibility aliases through all v0.x. Prompt-policy drift on mutable refs applies inline by default with a stderr summary line; per-run skip is available via `ANYWHERE_AGENTS_UPDATE=skip` (v0.5.0 contract preserved) and the new `--no-apply-drift` CLI flag. Bundled-default policy table flips to `agent-style → auto` (silent refresh) and `aa-core-skills → prompt` (apply-by-default). `update_policy: auto` on active entries is rejected at parse with an actionable error, restoring the documented manifest contract. BC-guard refinement bundles same-ref source-path switching, honoring the v0.5.7 commitment to old full-body consumers. aa-internal STRICT block lands in `scripts/check-parity.sh`.

### Changed

- **Bare `anywhere-agents` is the canonical apply command.** One verb runs bootstrap, deploys declared state, applies prompt-policy drift on mutable refs, and regenerates `CLAUDE.md` / `agents/codex.md`. `pack verify --fix` and `pack update` become compatibility aliases that print a single stderr notice (`note: 'pack verify --fix' is now an alias for 'anywhere-agents'; the bare command does the same thing`) and dispatch to the canonical apply path. Aliases retain full behavior through all v0.x; removal is allowed at v1.0 only with explicit CI-migration guidance. README quickstart leads with `anywhere-agents`; aliases are demoted to a Legacy Aliases footnote.
- **Inline prompt-policy drift apply (Q1).** The canonical bare-command path now applies prompt-policy drift on mutable refs by default and prints a stderr summary line per affected pack: `applied 1 update for <pack> @ <ref>: <old_short> -> <new_short>`. Same-ref source-path drift surfaces as `migrated 1 path for <pack> @ <ref>: <old_path> -> <new_path>`. Multi-pack runs aggregate one line per pack. Per-run skip overrides: `ANYWHERE_AGENTS_UPDATE=skip` env var (v0.5.0 contract preserved) and the new `--no-apply-drift` CLI flag (the flag wins when both are set). Durable fail-closed: `update_policy: locked` in `agent-config.yaml`. The misleading v0.5.x message (`ℹ N updates available — run pack verify --fix`) is replaced by the new stderr summary path.
- **Bundled-default policy table (Q3).** `bootstrap/packs.yaml` flips to: `agent-style` (first-party passive) → `auto` (silent refresh + stderr summary on changes); `aa-core-skills` (first-party active) → `prompt` (apply-by-default per Q1 + stderr summary). Third-party packs default to `prompt`. The wheel pins the bundled refs, so silent refresh on `agent-style` is acceptable; apply-by-default on `aa-core-skills` is acceptable because the apply path emits an audit trail.
- **Schema rejection: `update_policy: auto` on active entries (parse-time error).** Restoration of the documented manifest contract from the trust-model paragraph at `pack-architecture.md` line 208 ("Active entries never use `auto`; attempting to set it on an active entry is a manifest error"). v0.5.0 silently dropped the parser check; v0.6.0 restores it as a hard parse rejection. The error names the pack, the active entry's `files[].to` path, the offending policy literal, and the required rewrite — `update_policy: prompt` for default-apply behavior, `update_policy: locked` for fail-closed. Maintainer-project scan ahead of the Phase 1 PR confirmed zero hits, so no real consumer was caught by the rejection. Existing `tests/test_packs_schema.py::test_active_entry_accepts_auto_policy` is renamed `test_active_entry_rejects_auto_policy`. Manifests that paired `active: true` with `update_policy: auto` will now fail to parse and require an edit before `anywhere-agents` will run.

### Fixed

- **BC-guard refinement (`random` reproduction).** `_has_explicit_default_override` in `cli.py` now treats only entries with real shape signals (`passive` / `active` keys, or `ref` / `update_policy` deviating from the bundled-manifest default) as user-explicit pins. Entries byte-equivalent to what aa's auto-reconciliation (`_user_only_rule_pack_entry` / `_project_only_user_pack_entry`) would produce are no longer mis-classified. v0.5.7's coarser guard caused `pack verify --fix` to short-circuit on auto-reconciled minimal `{name, source: {url, ref}}` entries even when the bundled default had advanced. v0.6.0's refined classifier requires positive shape signals; deliberate user pins are preserved, while reconciliation residue gets re-derived from the bundled manifest.
- **Same-ref source-path switching (v0.5.7 § Compatibility commitment).** `scripts/compose_packs.py` detects when the bundled-manifest `from:` differs from the lock's recorded `source_path` for the same `requested_ref` and routes through the drift-and-migrate flow instead of failing closed or staying on stale full-body content. A consumer pinned to `agent-style v0.3.2` whose lock points at the old `docs/rule-pack.md` source path now auto-migrates to `docs/rule-pack-compact.md` on the next bare-command run. Fulfills the v0.5.7 § Compatibility line that consumers requiring same-ref source-path switching should stay on aa v0.5.6 until aa v0.6.0.
- **Host-aware bundled-default seeding (codex onboarding).** `aa-core-skills` declares `hosts: [claude-code]` in `bootstrap/packs.yaml`. v0.5.x and pre-fix v0.6.0 both auto-seeded it under every host, so codex consumers running the canonical command hit a hard host-mismatch error on first run; v0.6.0's promotion of bare `anywhere-agents` to canonical made the trap visible on the first command a fresh codex user types. New `_default_v2_selections_for_host(host)` helper in `scripts/compose_packs.py` (and the wheel-bundled composer mirror) plus a matching `_default_v2_seed_for_host(host)` + `_active_host()` pair in `packages/pypi/anywhere_agents/cli.py` filter claude-only entries from the auto-seed when the active host is not `claude-code`. The full `DEFAULT_V2_SELECTIONS` / `_DEFAULT_V2_SELECTIONS` list stays canonical for identity-check, BC-guard, and drift-detection call sites, so a user-pinned `aa-core-skills` row under codex still resolves to the synthetic bundled identity rather than a sourceless sentinel. Removes the v0.5.x `AGENT_CONFIG_PACKS=-aa-core-skills` workaround. New `_CLAUDE_ONLY_DEFAULTS = frozenset({'aa-core-skills'})` constant centralizes the host-restricted-default mapping; extending it is the only change required for a future bundled default that gates on host.

### Tests

- **Phase-aligned test modules.** New `tests/test_compose_packs_v0_6.py` with 11 tests covering the bundled-default policy table flip, the BC-guard classifier across the four shape cases (minimal auto-reconciled / shape-override / ref-override / update-policy-override), and the same-ref source-path switching path. New `tests/test_packs_cli_v0_6.py` with 13 tests covering inline drift apply, env-var and CLI-flag skip overrides, locked-policy fail-closed semantics, alias notice emission, alias dispatch byte-identity, selective `pack update <pack>` named-pack apply, and `pack verify` (no-`--fix`) inspection-only behavior. The v0.5.0-era `test_active_entry_accepts_auto_policy` is renamed `test_active_entry_rejects_auto_policy` and inverted to assert the parse rejection plus actionable-error contents (pack name, files[].to path, policy literal, required rewrite).
- **Smoke item 28: auto-reconciled minimal-entry fixture + parallel override-preservation fixture.** Item 28 sets up a fixture project with a stale `agent-config.yaml` `source.ref: v0.3.2` for `agent-style` (no `passive` / `active` / `update_policy` keys), a stale `.agent-config/pack-lock.json` `requested_ref: v0.3.2` and `source_path: docs/rule-pack.md`, and a deployed full-body `AGENTS.md`. Bare `anywhere-agents` advances `agent-config.yaml` past the stale ref, advances the lock to `source_path: docs/rule-pack-compact.md`, and shrinks `AGENTS.md` to the v0.5.7 compact target. The parallel override-preservation fixture sets the same scenario but with genuine user-authored `passive` / `active` / `update_policy` keys; bare `anywhere-agents` leaves those keys unchanged.
- **aa-internal STRICT block in `scripts/check-parity.sh`.** New STRICT array (separate from the cross-repo block) covers `scripts/compose_packs.py`, `scripts/compose_rule_packs.py`, `scripts/packs/*.py` (recursive, `__pycache__/` excluded), `scripts/generate_agent_configs.py`, `bootstrap/packs.yaml`, the four shipped skill directories (`implement-review`, `my-router`, `ci-mockup-figure`, `readme-polish`), and the four matching `.claude/commands/*.md` pointers. Each path must match its `packages/pypi/anywhere_agents/composer/<path>` mirror byte-for-byte. New `tests/test_check_parity.py::test_aa_internal_strict_detects_drift` synthesizes a one-byte drift in a mirrored file and asserts the script exits nonzero with the offending path. Replaces the manual `diff -rq` gate that has been the v0.5.6 release contract.
- **Test counts.** Full broader suite at 919 passed (893 + 14 host-gate tests added in the post-review fix + 12 from intermediate review rounds). Phase-aligned coverage: 22 new tests in `test_packs_cli_v0_6.py` (13 Phase 4 / 5 + 9 host-aware verify-seeding), 16 new tests in `test_compose_packs_v0_6.py` (11 Phase 2 + 5 host-aware compose-seeding), plus the renamed schema test and the new parity-script test.
- **Host-aware seeding test surface.** New `TestDefaultV2SelectionsForHost` and `TestComposeUnderCodexHostSkipsClaudeOnlyDefaults` in `test_compose_packs_v0_6.py` plus `TestVerifySeedHostAware` and `TestLoadProjectObservationsHostAware` in `test_packs_cli_v0_6.py` cover the four-way matrix: helper-filter behavior under claude-code / codex / unknown-host / identity-lookup-untouched, end-to-end resolver-under-codex compose, env-var-driven `_active_host` reads (codex / unset / unknown-fallback), and `_load_project_observations` under both hosts including the user-explicit pin path (codex consumer who deliberately keeps `aa-core-skills` in `agent-config.yaml`).

### Notes

- **Existing v0.5.x consumers heal on next `anywhere-agents` invocation.** Two-command upgrade — `pipx install --force anywhere-agents==0.6.0` then `anywhere-agents` from any project root. The wheel-bundled v0.6.0 composer's BC-guard refinement re-derives auto-reconciled minimal entries from the bundled manifest; the same-ref source-path switching path migrates the lock and `AGENTS.md` body in the same run; the inline-apply path applies any prompt-policy drift on mutable refs and prints the stderr summary. Real-project sandbox across `usc-admin`, `usc-email`, `usc-slides`, `random`, and `yzhao062.github.io` lands all five in the expected post-v0.6.0 state.
- **Documentation refresh.** `README.md` and `README.zh-CN.md` quickstarts lead with `anywhere-agents`; aliases live in a Legacy Aliases footnote. `docs/install.md` adds the v0.6.0 schema-rejection error wording and the same-ref source-path migration section. `docs/faq.md` adds Q&A entries for the inline-apply default, the per-run skip overrides, the alias retention, and the schema rejection. `docs/rule-pack-composition.md` updates the `update_policy` boundary so passive `auto` is documented as accepted while active `auto` is documented as rejected at parse.
- **PLAN file `PLAN-aa-v0.6.0-update-flow-coherence.md`** archives the eight-phase scope, the five-round Codex plan-review trail (Round 5 closure 2026-05-03), and the validation matrix (smoke items 27 and 28, five-project sandbox, cross-platform CI).

### Compatibility

- **Compatibility-alias retention through all v0.x.** `pack verify --fix` and `pack update` are full-fidelity dispatch paths into the canonical apply, not deprecation stubs. CI scripts using `pack verify --fix --yes` or `pack update --all` continue to work and pick up the new inline-apply default automatically because that default lives in the canonical path the aliases call. Removal is allowed only at v1.0 with explicit CI-migration guidance.
- **`ANYWHERE_AGENTS_UPDATE=skip` v0.5.0 contract preserved.** The pre-existing env-var contract (drift reported, not applied) is honored by the v0.6.0 apply path; the new `--no-apply-drift` CLI flag has the same effect, and the flag wins when both are set.
- **CI-output BC.** The v0.5.x message format has changed: where `pack verify` previously emitted `ℹ N pack(s) have updates available — run pack verify --fix to apply` and `pack verify --fix` then printed `--fix: nothing to repair`, v0.6.0 emits one `applied 1 update for <pack> @ <ref>: <old> -> <new>` stderr summary line per affected pack on the canonical apply path, and the alias dispatch prints a one-line stderr notice pointing at `anywhere-agents`. CI scripts that grepped on the old strings need migration to the new summary format. The migration window is generous since aliases stay through all v0.x.
- **STRICT mirror.** `scripts/compose_packs.py`, `scripts/packs/schema.py`, `scripts/packs/*.py`, `scripts/generate_agent_configs.py`, `bootstrap/packs.yaml`, the four shipped skill directories, and the four `.claude/commands/*.md` pointers are kept byte-identical with their wheel-bundled mirrors at `packages/pypi/anywhere_agents/composer/`. Phase 6's STRICT block in `scripts/check-parity.sh` mechanically enforces the mirror; any drift fails the pre-push gate.

## [0.5.8] — 2026-04-30

### Fixed

- **Drift-gate adopts pre-existing skill-dir contents (Item 1).** `_build_prior_pack_outputs` in `scripts/compose_packs.py` (and the v0.5.6 wheel-bundled mirror) now walks every file under recorded directory `output_paths` in `pack-lock.json`, gated by a Merkle `_dir_sha256(path)` match against the pack's current `input_sha256` or any value in a new optional `historical_input_sha256` ring (FIFO-capped at 5). Reproduces on `usc-admin`/`usc-email` running plain `anywhere-agents`: aa main `6d156fe → fc248ab` landed a 33-line `skills/implement-review/SKILL.md` change, the v0.5.7 gate fell to `PRESTATE_UNMANAGED` and aborted with `pack composition failed`, leaving `CLAUDE.md` stale at 135 KB while `AGENTS.md` was 70 KB. v0.5.8 walks the dir, finds the on-disk merkle in the lock's known-shas set, classifies as `PRESTATE_PACK_OUTPUT`, and writes the new content cleanly. User edits inside a managed skill directory are still protected: when the on-disk dir-merkle matches no known sha, every file under it falls to `PRESTATE_UNMANAGED` and the gate aborts as before. Pack-lock schema gains an optional `historical_input_sha256: list[str]` field per pack file entry; old locks without the field load cleanly (treated as empty ring); the field is written only when populated.

- **Generator runs after every composer attempt (Item 2).** `bootstrap/bootstrap.{sh,ps1}` and a new `_invoke_composer_with_gen_fallback` wrapper in `packages/pypi/anywhere_agents/cli.py` now run `scripts/generate_agent_configs.py --root . --quiet` in finally-style after the composer, regardless of composer rc. On rc=0 behavior is unchanged. On rc≠0, the generator still runs so `CLAUDE.md`/`agents/codex.md` reflect on-disk `AGENTS.md`, then the original composer rc is preserved (no rc=0 conversion); a recovery line is printed to stderr explaining that the generator refreshed the generated files but pack composition did not complete. The wrapper replaces direct `_invoke_composer` calls at the four CLI callsites: `pack add`, `pack update`, and both branches of `_pack_verify_fix`. **`bootstrap.ps1` rc preservation also fixed**: the `$composerRc` expression now uses `if ($LASTEXITCODE -ne $null) { [int]$LASTEXITCODE } elseif (-not $?) { 1 } else { 0 }`, so non-1 exit codes (`2`, `7`, `10`, `11`, …) propagate correctly instead of being coerced to `1` by the prior `$? -and …` short-circuit.

- **`pack verify --fix` materializes declared state (Item 3).** After `_pack_verify_fix` makes any repair / reconcile write or detects a deployable broken state, the composer now runs automatically via `_invoke_composer_with_gen_fallback` to materialize the declared state. New `--no-deploy` flag opts out for CI / offline / inspect-only use (default: auto-deploy). Banner half-clauses unchanged — "run `pack verify --fix`" remains correct because `--fix` now finishes end-to-end. Prompt-policy update drift on a healthy project is **not** auto-applied; that decision belongs to v0.6.0 (Q1 update-flow split). Regression test guards the boundary: a `deployed` project with `latest_known_head != resolved_commit` reports "nothing to repair" without invoking the composer.

- **Git Bash on Windows fetches `.sh` (Item 4).** New `_detect_windows_shell()` in `cli.py` detects an active bash shell on Windows via the `BASH_VERSION` env var (Git Bash sets it) or `MSYSTEM` matching `MINGW*` (case-insensitive). When bash is detected, `choose_script()` selects `bootstrap.sh` and locates `bash` via `shutil.which`. PowerShell remains the default when neither signal is set. Bash wins even when the parent process was PowerShell — the CLI honors the shell the user is currently typing into.

### Tests

- 33 new tests across `tests/test_drift_gate_skill_dir.py` (drift-gate adoption, dir-sha gating, ring eviction, drift abort regression for user edits inside managed dirs), `tests/test_cli_robustness.py` (gen-fallback rc preservation, recovery message, missing-generator no-crash, callsite migration, plus 3 subprocess-driven `bootstrap.{sh,ps1}` rc-preservation tests against a stub composer exiting `7`), `tests/test_cli_bootstrap_fetch.py` (12 detection cases for `_detect_windows_shell` × `choose_script` × bash-on-PATH permutations), `tests/test_packs_cli_v0_5.py::V058VerifyFixAutoDeployTests` (`--no-deploy` opt-out, deployable-state path, prompt-policy drift no-auto-deploy boundary), and `tests/integration/test_robustness_v0_5_8.py::UscAdminReproductionTest` (end-to-end heal with the real `generate_agent_configs.py` against a synthesized stale-CLAUDE.md project; verifies size drop and `GENERATED FILE` marker after composer success and after composer failure). Gap A/B extension: 6 additional tests: `BootstrapMainFallThroughTests` (3: fall-through recovery, recovery-failure rc preservation, success path), `V058VerifyFixAutoDeployTests` (2: deployed-but-stale generator heals, `--no-deploy` skips generator), `UpgraderHealTest` (1: end-to-end integration against real generator on an all-DEPLOYED project with stale CLAUDE.md; confirms size drop). Two prior v0.5.4 tests updated to reflect the new always-run reconcile behavior (renamed from "skips" to "runs"). Round 3 additions (2 new tests): `test_bootstrap_success_with_reconcile_failure_preserves_reconcile_rc` (pins the previously untested rc-matrix quadrant: bootstrap rc=0 + reconcile rc≠0), `test_bootstrap_failure_with_no_clone_preserves_bootstrap_rc` (regression for evidence-based recovery: bootstrap rc=7, no project clone or AGENTS.md, reconcile no-op returns 0, `_bootstrap_main` must return 7). `test_dir_sha_in_historical_ring_allows_walk` rewritten to prove the true historical-ring-only positive path (on-disk sha matches ring entry, not current input_sha256). Full suite: 859 passed, 5 skipped.

### Notes

- **Existing v0.5.7 consumers heal on next `anywhere-agents` invocation.** Two-command upgrade — `pipx install --force anywhere-agents==0.5.8` then `anywhere-agents` from any project root. The wheel-bundled v0.5.8 composer's drift-gate fix prevents the abort that left projects stuck; the v0.5.7 bootstrap.sh's existing success-path generator step (still in play during this transition) re-derives `CLAUDE.md` / `agents/codex.md` from current `AGENTS.md`. Once the v0.5.8 bootstrap.sh is on `main`, the same heal runs end-to-end via Item 2's finally-style wrapper. Reproduction-confirmed on `usc-admin` / `usc-email` sandbox copies: `CLAUDE.md 135390B → 66608B`, `agents/codex.md 137104B → 68322B` post-compose.
- **`pack verify --fix` now ensures generator coherence on "deployed but stale" projects (Gap B fix).** The prior v0.5.8 Item 3 implementation ran the generator only when the composer was invoked (pack-level structural repair). A project that passed pack-level health checks but had a stale `CLAUDE.md` (e.g., from a `pipx install --force` upgrade followed by no full composer run) would exit `--fix` with "nothing to repair" without healing. Fixed: `_pack_verify_fix` now calls `_run_generator_only` as a final coherence step on every exit path that is not gated by `--no-deploy`. The generator is idempotent when files are already in sync. The `--no-deploy` flag continues to opt out of all file writes, including the generator step. Upgrade flow for stuck-CLAUDE.md: `pipx install --force anywhere-agents==0.5.8` then `anywhere-agents pack verify --fix` — the generator heals `CLAUDE.md`/`agents/codex.md` from the current `AGENTS.md` even when the pack-lock is fully `deployed`. The bare `anywhere-agents` command also heals via the same path (see Gap A note below). **Generator now wheel-bundled (Round 3).** `scripts/generate_agent_configs.py` is copied to `packages/pypi/anywhere_agents/composer/scripts/generate_agent_configs.py` (STRICT mirror, byte-identical). `_run_generator_only` prefers the bundled copy; the project-clone path (`.agent-config/repo/scripts/generate_agent_configs.py`) is the fallback. This means `pack verify --fix` can heal stale generated files even on a project that has no `.agent-config/repo/` — the wheel carries the generator directly. Projects with an existing `.agent-config/repo/` continue to use whichever copy the bundled-path check finds first.
- **Bare `anywhere-agents` falls through to wheel-side recovery when bootstrap.sh exits non-zero (Gap A fix).** The prior v0.5.8 `_bootstrap_main` returned immediately on `bootstrap.sh` rc≠0, never reaching the post-bootstrap reconcile (`pack verify --fix --yes`) that uses the wheel-bundled v0.5.8 composer and generator. This meant an upgrader whose cloned bootstrap.sh called the v0.5.7 composer (which could still abort on the pre-Item-1 drift gate) would be left stuck. Fixed: `_bootstrap_main` now always continues to the wheel-side reconcile pass regardless of `bootstrap_rc`. If `bootstrap_rc != 0` and the reconcile succeeds (rc=0) with evidence of deployment (project clone or `AGENTS.md` present), `_bootstrap_main` returns 0. If bootstrap fails and no evidence of deployment exists (e.g., an opt-out project with `rule_packs: []` and no clone), the original `bootstrap_rc` is preserved — reconcile returning 0 from the "nothing to repair" branch is not credited as recovery. If both fail, `bootstrap_rc` is preserved. Upgrade flow: `pipx install --force anywhere-agents==0.5.8` then `anywhere-agents`. On a real upgrader project with a repo clone, both the bare command and `pack verify --fix` end in a coherent state. On an opt-out project with no clone, a bootstrap failure is preserved faithfully. The universal repair path for any stuck project remains `anywhere-agents` (re-runs bootstrap + reconcile end-to-end).
- **Prompt-policy drift on `agent-pack @ main` ("1 update available") is unchanged.** v0.5.8 explicitly defers the BC-guard refinement and the `pack verify --fix` vs `pack update` split to v0.6.0 (Q1 update-flow coherence).
- **Documentation refresh.** PLAN file `PLAN-aa-v0.5.8-basic-command-robustness.md` archives the four-item scope, Round 1 + 2 plan-review decisions, and the validation matrix. `pack-architecture.md` § "aa v0.5.8 — Basic command robustness" already records the release narrative.

### Compatibility

- **Pack-lock schema additive.** The new optional `historical_input_sha256: list[str]` field on file entries is forward-compatible: v0.5.7 and earlier locks load cleanly under v0.5.8 (treated as empty ring), and v0.5.8 locks read by older composers ignore the unknown key per the existing JSON-extra-fields tolerance.
- **STRICT mirror.** `scripts/compose_packs.py`, `scripts/packs/state.py`, and the newly bundled `scripts/generate_agent_configs.py` are updated in lockstep with their wheel-bundled mirrors at `packages/pypi/anywhere_agents/composer/scripts/`. All three pairs verified byte-identical.
- **`bootstrap.sh` rc preservation unchanged from v0.5.7** (the bash script never had the `$?` short-circuit bug); only `bootstrap.ps1` semantics shifted.

## [0.5.7] — 2026-04-29

### Changed

- **Bundled `agent-style` switched to compact + ref bumped to `v0.3.5`.** Two-axis update in `bootstrap/packs.yaml` and the v0.5.6 wheel-bundled mirror at `packages/pypi/anywhere_agents/composer/bootstrap/packs.yaml` (aa-internal STRICT): `ref: v0.3.2 → v0.3.5` and `from: docs/rule-pack.md → docs/rule-pack-compact.md`. Existing aa 0.5.6 projects upgrade with two commands — `pipx install --force anywhere-agents==0.5.7` then `anywhere-agents pack verify --fix` — and see the inlined `agent-style` body in `AGENTS.md` drop from ~89 KB (full rule-pack: 21 rules + metadata bullets + 5+ BAD/GOOD pairs + rationale) to ~21 KB (compact rule-pack: 21 rules + directive + 1 illustrative BAD/GOOD pair). Total `AGENTS.md` size drops ~50% on a maintainer-style project (5 packs, base + agent-style + agent-pack/{acad,paper,profile}). `pack-lock.json` advances `agent-style.resolved_commit == v0.3.5` and `source_path == docs/rule-pack-compact.md`.

### Notes

- **`update_policy: locked` unchanged.** Bundled manifest keeps `update_policy: locked`. The policy change (to `update_policy: prompt`, banner-driven update notifications) is deferred to **aa v0.6.0** because it requires the `pack verify --fix` inline-apply UX fix to make `prompt` policy ergonomic for consumers; see `agent-config/docs/pack-architecture.md` § "aa v0.6.0 — Update-UX revisit". Shipping the policy change without the UX fix would extend the existing `agent-pack` prompt-drift confusion (reproduced on `usc-slides`) to a second source.
- **`pack verify` does not migrate old explicit pins; a full composer/bootstrap run does.** This release is asymmetric across the two repair entry points. The `pack verify --fix` path leaves an existing project alone when it already has an explicit `agent-style` override at an older ref with full-body `passive.files[].from: docs/rule-pack.md` — verify reports the pinned ref and the inlined body stays as last composed. However, a full composer / bootstrap run on aa v0.5.7 (e.g., `anywhere-agents` with no args, or any direct composer invocation) re-derives the pack definition from the bundled `bootstrap/packs.yaml` because `agent-style` ships no `pack.yaml` of its own. The bundled definition now points at `docs/rule-pack-compact.md`, so compact output is expected on a bootstrap run regardless of the consumer-side `passive` override. Net effect: a v0.5.6 consumer with an old full-body explicit pin gets compact on the next bootstrap, not on the next `pack verify --fix`. Consumer-side compact-to-full switching as a first-class supported action is deferred to aa v0.6.0 (same-ref source-path switching). Consumers who must keep the bundled full-body default should stay on aa v0.5.6 until v0.6.0.
- **Documentation refresh.** `docs/rule-pack-composition.md` (3 illustrative example places), `compose_rule_packs.py` user-help snippet, and `bootstrap/bootstrap.{ps1,sh}` dry-helper hint all show `v0.3.5` in their examples to match the new bundled default.

### Compatibility

- **Existing explicit pins are only verify-stable.** `pack verify --fix` leaves a project with `name: agent-style` and an older `ref` alone when its deployed output still matches the lock. A full composer/bootstrap run on aa v0.5.7 re-derives the pack definition from the bundled manifest and can rewrite that project to compact, as noted above. Consumers that need the old full-body bundled output should stay on aa v0.5.6 until same-ref source-path switching lands in aa v0.6.0.
- **Lock-file ref bump under `update_policy: locked` is allowed** because v0.5.7 declares a new bundled default; `locked` policy gates *upstream HEAD drift on the same ref*, not maintainer-declared bundled-default updates. The composer resolves `agent-style` from the current bundled manifest and writes the lock entry with the new ref/source_path.
- **Content equivalence.** Compact and full both ship the same 21 rule directives and the same rule headings/order. Compact drops per-rule metadata bullets (source / agent-instruction evidence / severity / scope / enforcement), additional BAD/GOOD pairs, and the `Rationale for AI Agent` subsection. Rules apply identically at runtime.
- **Tests pinned to v0.3.2 fixtures stay v0.3.2.** Test fixtures in `tests/test_compose_*` and `tests/test_banner_updates.py` reference `v0.3.2` as known-state input; not changed by this release.
- **Legacy markerless content detection (`cli.py:1234`)** continues to recognize v0.3.2-era AGENTS.md content; not changed.

## [0.5.6] — 2026-04-28

### Fixed

- **Package-owned composer for repair commands.** `pack add`, `pack verify --fix`, `pack remove`, and `pack update` now invoke the composer shipped with the installed PyPI package after confirming the project is already bootstrapped. This fixes the v0.5.5 failure mode where `pipx install --force anywhere-agents==0.5.5` upgraded the CLI, but `pack verify --fix` still ran an older `.agent-config/repo/scripts/compose_packs.py` and left `aa-core-skills` missing.
- **Bundled pack sources are read from the composer owner.** The composer now reads bundled manifests and bundled active-pack files from the source tree that owns the running composer. In a normal bootstrap this is `.agent-config/repo`; in the PyPI repair path this is the wheel's bundled `anywhere_agents/composer` tree. Stale project caches can no longer keep the current CLI from writing current bundled defaults.

### Tests

- Added direct coverage that `_invoke_composer` prefers the package-bundled composer when a project-local composer exists, while still returning the existing "Run bootstrap first" error when the project has not been bootstrapped.

## [0.5.5] — 2026-04-27

### Highlights

- **Idempotent multi-repo refresh.** v0.5.4 still let a broken post-migration project lose bundled-default rows on the next compose run because the composer used the old fallback resolver path. v0.5.5 switches the composer to the four-layer resolver, seeds bundled defaults on every run, and proves the refresh path against real broken project copies without hand-editing those repos.
- **Windows cache self-heal.** Real migrations also exposed malformed cache slots on Windows: read-only git pack files and long asset paths could make stale-slot deletion fail, after which `shutil.move` nested an `aa-clone-*` directory inside the old cache slot. v0.5.5 makes stale cache removal strict and Windows-aware, and can recover a single nested clone inside an already URL-and-commit-keyed cache slot.

### Changed

- **Composer resolver migration** (`scripts/compose_packs.py`, `scripts/packs/config.py`). `_do_compose_v2` now reads all durable layers through `resolved_for_project(..., default_selections=DEFAULT_V2_SELECTIONS, force_defaults=True)`, so user-level packs and bundled defaults merge deterministically. Explicit empty project config still clears non-default user packs while preserving bundled defaults.
- **Pack verify default visibility** (`packages/pypi/anywhere_agents/cli.py`). `pack verify` now displays every bundled default with deployment status from lock and disk, even when user/project config does not mention it. `pack verify --fix` no longer returns rc=0 while bundled defaults are missing or deployed-but-not-locked.
- **Bundled-default remove warning** (`packages/pypi/anywhere_agents/cli.py`). `pack remove <name>` warns when the requested name is one of the bundled defaults because the next composer run will materialize it again unless defaults are changed at the composer level.
- **Source-fetch cache hardening** (`scripts/packs/source_fetch.py`, `packages/pypi/anywhere_agents/packs/source_fetch.py`). Cache cleanup now retries after clearing read-only bits, uses Windows long-path forms for deep asset trees, falls back to manual recursive deletion when platform `rmtree` fails, and raises if stale-slot cleanup still leaves files behind. Cached reads recover exactly one nested `aa-clone-*` archive root with `pack.yaml` or `.git`; zero or multiple candidates keep fail-loud behavior.

### Internals

- **Lock metadata preservation.** `_build_ctx` preserves prior `latest_known_head` and `fetched_at` only when `(source_url, requested_ref, resolved_commit)` still matches. Invalid SHA metadata is dropped, and uppercase SHA input is canonicalized to lowercase before writing.
- **Agent-style disk detection.** The verify classifier recognizes legacy markerless `agent-style v0.3.2` content by requiring all three upstream section signatures, with source comments documenting the coupling and revisit trigger.
- **Tests and verification.** Added resolver, verify, cache-recovery, long-path, and metadata-canonicalization coverage. Local validation for the release candidate: focused pytest `236 passed, 4 skipped`; non-integration pytest `790 passed, 5 skipped`; temp-copy validation across three real broken projects with inherited cache and cold cache all reached a deterministic five-pack lock without modifying the original repos. `implement-review` Round 4 reported no blocking findings and recommended shipping v0.5.5.

### Migration

- Existing v0.5.4 users should upgrade and rerun `anywhere-agents` or `anywhere-agents pack verify --fix --yes` in each project. The command refreshes project config and pack-lock state from the current user-level config plus bundled defaults. If a Windows cache slot was malformed by an earlier run, v0.5.5 either recovers the single nested clone or deletes and refetches the stale slot; no manual cache cleanup should be required.

## [0.5.4] — 2026-04-27

### Highlights

- **End-to-end migration in one shot.** v0.5.3's drift-gate adopt-on-match closed the byte-identical case, but four remaining gaps still blocked AC->AA+AP migration: (a) composer aborted when an upstream lacked the v0.4.0 `pack.yaml` even for bundled-default names like `agent-style v0.3.x`; (b) `pack verify --fix` reconciled bundled-default packs across layers when the user-level entry carried a non-bundled URL, propagating the URL form into project YAML on every run; (c) `pack add` left duplicate `name` rows in user-level config when the dedup logic predated the current shape; (d) running `anywhere-agents` after migration deployed only the bundled defaults, leaving user-level packs (e.g., `agent-pack` profile / paper-workflow / acad-skills) un-deployed until a separate `pack verify --fix` run. v0.5.4 closes all four. End-to-end test: a legacy AC project with a corrupt user-level config and stale `.claude/commands/*.md` files lands at the AA+AP target deployment after one `anywhere-agents` invocation.

### Changed

- **Composer bundled fallback for missing upstream `pack.yaml`** (`scripts/compose_packs.py`). When the inline-source path fetches an archive that has no `pack.yaml` at the root OR has one that doesn't declare the requested name, the composer now falls back to the bundled `bootstrap/packs.yaml` pack-def for `DEFAULT_V2_SELECTIONS` names. The fetched `archive_dir` is still threaded into `DispatchContext.pack_source_dir` so passive handlers read file bytes from the local archive instead of the v0.4.0 raw-URL fetch fallback. Closes the `agent-style v0.3.x` "manifest not found" abort that surfaced when the user-level config carried the URL form for a bundled-default name. Non-bundled packs without a usable `pack.yaml` still raise `ComposeError` with a clearer error message.
- **`pack verify --fix` never reconciles bundled-default packs** (`packages/pypi/anywhere_agents/cli.py`). `_is_bundled_default_row` now returns `True` for any name in `DEFAULT_V2_SELECTIONS` regardless of the layer's URL form. v0.5.3 still reconciled when the user explicitly named a non-bundled URL, which let a lock-side URL form (emitted because the bundled pack-def points to an upstream source for passive content) propagate into user-level config and back into project YAML on every subsequent run. The composer always materializes `agent-style` and `aa-core-skills` from the bundled manifest; reconciling them across layers is churn that v0.5.4 removes.
- **AC->AA migration moves `.claude/commands/` aside** (`packages/pypi/anywhere_agents/cli.py`). `_migrate_legacy_ac` now renames any non-empty `.claude/commands/` directory to `.claude/commands.bak-<UTC-timestamp>/` before AA's bootstrap reclones. AC's bootstrap copies pointer files into `.claude/commands/`; AA's `aa-core-skills` pack writes pointer files at the same paths but with bytes that may differ. Without the move, the drift gate refused to clobber the AC-leftover pointers as `PRESTATE_UNMANAGED` and the migration aborted. Backup directory preserves user-customized files for manual merge.
- **`anywhere-agents` auto-runs `pack verify --fix --yes` after bootstrap** (`packages/pypi/anywhere_agents/cli.py`). When the bootstrap script returns rc=0 and a user-level config exists, `_bootstrap_main` re-enters `main(["pack", "verify", "--fix", "--yes"])` so user-level packs reconcile to project `agent-config.yaml` and the composer subprocess deploys them in the same shot. Skipped when bootstrap fails (intermediate state is preserved for retry) or when no user-level config file exists (fast no-op for fresh installs without registered packs). The reconcile failure is logged as a warning; the bootstrap rc still wins.
- **User-level config self-heal dedup** (`packages/pypi/anywhere_agents/cli.py`). New `_dedup_user_packs()` helper drops later occurrences of the same `name`, keeping the first. Plumbed through `_load_or_create_user_config` (silent dedup at every read; next write persists the cleaned list) and `_load_user_observations` (verify sees deduped data). `_pack_verify_fix` runs an explicit early dedup-and-rewrite pass so the on-disk file is normalized even when no reconcile is otherwise needed. Defensive against earlier `pack add` versions and concurrent invocations that could leave duplicates.

### Internals

- **`_bundled_pack_def(bundled_manifest, name)`** factored out of `_process_selection`. Shared by the bundled-lookup path and the v0.5.4 inline-source fallback so the lookup is byte-identical between the two entry points.
- **Tests**: `tests/test_compose_packs_v0_5.py` adds three `_process_selection` cases (no upstream `pack.yaml` -> bundled fallback; upstream `pack.yaml` present but missing the name -> bundled fallback for default names, `ComposeError` for non-default; future-bundled non-default name -> fallback NOT consumed, gated on `DEFAULT_V2_SELECTION_NAMES`). `tests/test_packs_cli_v0_5.py` adds a `V054DedupTests` class (helper, loader, and `--fix` self-heal), a `V054BundledDefaultsNotReconciledTests` class (URL form for `agent-style` does not appear in the planned project YAML write), a `V054MigrateLegacyAcTests` class (`.claude/commands/` backup + skip-when-absent), and a `V054BootstrapAutoReconcileTests` class (bootstrap success invokes `main(["pack", "verify", "--fix", "--yes"])`; bootstrap failure or missing user-config skips it). 13 new tests, all green; full suite at 769 passed.

### Migration

- **Existing AA users**: re-run `anywhere-agents` to pick up the post-bootstrap reconcile. Existing `pack-lock.json` entries are unchanged; the bundled-default no-reconcile rule applies on the next `pack verify --fix`.
- **Legacy AC consumers**: re-run `anywhere-agents` from the project root. The CLI detects the AC bootstrap state (`.agent-config/upstream` containing `yzhao062/agent-config` or `.agent-config/repo/.git/config` pointing at `agent-config`), backs up `.claude/commands/` to `.claude/commands.bak-<timestamp>/`, re-clones from `anywhere-agents`, deploys bundled defaults, then auto-reconciles user-level packs. User-customized files in the backup directory can be manually re-applied if needed.
- **User-level config corruption**: any duplicate-name entries are silently dropped on the next `pack verify [--fix]` or `pack add` invocation. The `--fix` path explicitly logs the drop count.

## [0.5.3] — 2026-04-27

### Highlights

- **Drift gate adopt-on-match.** v0.5.2's drift gate rejects any pre-existing unmanaged file at a target path, blocking AC→AA migrations, interrupted `pack add` resumptions, team-clone first runs (where `.claude/skills/` is committed but `.agent-config/pack-lock.json` is gitignored), and manual / sibling-tool deploys to skill paths. v0.5.3 keeps the rejection for content drift but adopts the file into `pack-lock.json` when its on-disk sha256 already matches what the pack would write. The protection against silent user-edit clobber is unchanged: hash mismatch still aborts with the same `DriftAbort` and recovery line.

### Changed

- **`Transaction._validate_prestate` (`scripts/packs/transaction.py`).** The `PRESTATE_UNMANAGED` branch now compares the on-disk sha256 against the staged op's `new_content_sha256`. Match → record path on the new `Transaction.adopted_paths` list and skip the drift; subsequent `_apply_op` rewrites byte-identical content, so the lockfile entry the dispatcher already recorded becomes the authoritative ownership record. Mismatch (or `new_content_sha256` missing) → keep the v0.5.2 reject behavior. `OP_DELETE` and the four non-unmanaged categories are unchanged.
- **Composer adoption logging (`scripts/compose_packs.py`).** After commit, when `Transaction.adopted_paths` is non-empty, the composer emits one stdout line — `ℹ composer adopted N pre-existing file(s) into pack-lock.json (content matched pack output):` — followed by the absolute paths in the same 2-space-indent style as the drift error. Silent adoption is avoided so the user sees what crossed the gate.

### Internals

- **`Transaction.adopted_paths`.** New instance attribute, initialized empty in `__init__`. Populated by `_validate_prestate` on every gate run; the same field is used by single-file collisions and per-file directory-handler scenarios (the `kind: skill` handler stages one op per file under `.claude/skills/<name>/`, so a 19-file skill that fully matches reports 19 entries).
- **Five new tests in `TransactionDriftGateTests`.** `test_unmanaged_collision_matching_content_adopted` asserts the new adoption path; `test_unmanaged_collision_mismatched_content_rejected` pins the negative case; `test_unmanaged_collision_partial_match_rejects_only_mismatch` covers the per-file directory scenario where some files match and some are user-edited; `test_adopted_paths_initialized_empty` and `test_unmanaged_no_collision_clean_install_no_adopt` pin the no-adopt baseline.

### Migration

- v0.5.2 users hitting the drift gate on AC→AA migration / interrupted `pack add` / team-clone first run / manual deploy can now upgrade to v0.5.3 and rerun the original command. No backup / move dance required when content already matches what the pack would write. If the pre-existing file's content differs (genuine user edit), the recovery path is unchanged: back up local edits, then rerun.

## [0.5.2] — 2026-04-27

### Highlights

- **One-shot pack lifecycle.** v0.5.1's nine-command pack-deploy dance collapses into one invocation per command. `anywhere-agents pack add <url>` now writes user-level config, project-level `agent-config.yaml`, and runs the composer subprocess in a single shot, replacing the v0.5.1 split where `pack add` only registered. `pack verify --fix` similarly reconciles user ↔ project configs bidirectionally and invokes the composer; `pack remove <name>` deletes from both configs and runs the new single-pack composer uninstall mode. Identity rules apply at every layer: same `(name, normalized_url, ref)` is idempotent; same `name` with different identity returns rc=1 with no writes.
- **AC→AA migration on `anywhere-agents` (no subcommand).** When the cwd has a legacy `agent-config` bootstrap state — `.agent-config/repo/.git/config` pointing at `yzhao062/agent-config` OR `.agent-config/upstream` containing `yzhao062/agent-config` — the CLI silently wipes the cached repo + bootstrap files and re-clones from anywhere-agents. Removes the v0.5.1 silent no-op where the existing-dir check skipped re-clone and the AA composer never landed.
- **Cross-OS bootstrap entries.** Both `bootstrap/bootstrap.sh` and `bootstrap/bootstrap.ps1` now copy each other from the sparse clone after the clone completes. A Windows user running Git Bash / WSL on a project bootstrapped from PowerShell no longer hits "No such file or directory" on `.agent-config/bootstrap.sh`, and vice versa.
- **Banner item 7 reports pack updates.** Session start banner item 7 now shows both `gap_count` (user-level packs not deployed in project) and `update_count` (packs whose remote HEAD has moved past `resolved_commit`). The composer records `latest_known_head = resolved_commit` at fetch time; `pack verify` runs `git ls-remote --exit-code` opportunistically (5s per-pack timeout, mutable refs only — 40-char SHAs skip the network call) and lock-bracket-merges the resolved head into `.agent-config/pack-lock.json`.

### Added

- **`scripts/packs/transaction.py` drift gate.** `Transaction.commit()` accepts an `expected_prestate` map keyed by absolute target path; each value is a `(category, recorded_sha256)` tuple. Five categories: pack outputs (recorded sha256 must match current), internal state files / composer-owned core / declared JSON merge targets (stage-time hash must equal commit-time hash — optimistic concurrency), and unmanaged (existing file → reject as collision; absent → allow). The composer builds the map after every staged write, before commit. Mismatches raise `DriftAbort` with a per-path reason; the staging dir is rolled back so no on-disk target is mutated. Empty `expected_prestate` (the v0.4.0/v0.5.0 default) preserves the previous behavior unchanged.
- **`scripts/packs/uninstall.py:run_uninstall_pack(name)`.** Single-pack uninstall path used by `pack remove`. Filters user-state owners by composite key `(repo_id, pack)` so two repos installing the same pack name from different upstreams don't decrement each other; computes remaining-output-claims from other lock entries before deleting shared outputs; preserves drift-safe retry semantics (any owned output whose hash drifted leaves the pack's lock + state ownership records intact). File-like user-level outputs follow the empty-owners-AND-hash-match delete rule; active-permission entries only prune state-side owner records and never touch `~/.claude/settings.json` (JSON unmerge stays out of v0.5.2 scope).
- **Lock schema fields `latest_known_head` + `fetched_at`.** Both optional. Composer populates them at fetch time (head equals `resolved_commit` because the fetch just happened); `pack verify` updates `latest_known_head` later. Old locks without these fields parse cleanly and contribute zero to `update_count`.
- **`compose_packs.py uninstall <name>` mode.** New subcommand on the composer subprocess that drives `run_uninstall_pack`. Used by `pack remove`. Composer self-locks; the CLI does not hold outer locks across the subprocess invocation.

### Changed

- **`pack add` behavior split**. Outside a bootstrapped project (no `.agent-config/repo/scripts/compose_packs.py` and no bootstrap scripts in `.agent-config/`) the CLI registers user-level only and prints `ℹ Registered globally. Run anywhere-agents in a bootstrapped project to deploy.` In-project the CLI also writes `agent-config.yaml` and invokes the composer.
- **`pack verify --fix` is now bidirectional.** User-only rows write to `agent-config.yaml`; project-only rows write to user-level config; mismatch rows return rc=1 with no writes. Bundled-default packs (`agent-style`, `aa-core-skills`) are excluded from reconcile to avoid churn since the composer always materializes them via `DEFAULT_V2_SELECTIONS`. After config writes, the composer subprocess runs.
- **`pack remove` is cascade delete.** Now removes from user-level config + project `rule_packs:` + invokes composer's uninstall mode for the single pack. v0.5.1's user-level-only behavior is gone. Not-found (in any of user / project / lock) now returns rc=1 instead of rc=0.
- **`pack verify` writes the lock.** Round-tripping `git ls-remote` results into `pack-lock.json` is the only side effect; the audit logic itself is unchanged. Network failures and timeouts skip per-pack so an offline `pack verify` still classifies the local state cleanly.

### Internals

- **DispatchContext gains `pack_latest_known_head` + `pack_fetched_at`.** Both default to `None`; composer populates them when threading inline-source archives. `finalize_pack_lock` writes the optional fields into the per-pack lock entry.
- **Five-category drift-gate classification in `_do_compose_v2`.** Pack outputs (from prior `pack-state.json`), internal state files (`pack-lock.json`, `pack-state.json`, user-level `pack-state.json`), composer-owned core (`AGENTS.md`), declared JSON merge targets (user-level `settings.json` from `active-permission` entries), and unmanaged. The fifth category prevents the permission handler's full-file rewrite of an existing `settings.json` from being rejected as an unmanaged collision.
- **CLI banner item 7 update path.** `_pack_verify` snapshots the lock before issuing `git ls-remote`, then takes the repo lock, re-reads, and merges per-entry by `(source_url, requested_ref, resolved_commit)` identity tuple. Concurrent composer / pack add writes between snapshot and merge cause that result to be skipped — no overwrite of a moved `resolved_commit`.

### Migration

- v0.5.1 users who scripted `pack add` then `pack verify --fix` then `bootstrap.sh` see redundant work but no failure. The new one-shot `pack add` does it all in one invocation; the legacy three-step recipe still works.
- A composer drift-abort after CLI's config writes have landed leaves a "registered but not deployed" intermediate state. Recovery: back up local edits to managed files, then rerun `pack add` (idempotent on configs) or `pack verify --fix`. Cross-command atomicity (true WAL-style commit-or-fail-together) is deferred to v0.5.3+.

## [0.5.1] — 2026-04-27

### Added

- `anywhere-agents pack verify [--fix]`: audits pack deployment state across user-level config, project-level `rule_packs:` in `agent-config.yaml`, and `pack-lock.json`, by `(name, source_url, requested_ref)` identity. Seven priority-ordered states: `deployed`, `user-level only`, `config mismatch`, `declared, not bootstrapped`, `broken state`, `lock schema stale`, `orphan`. Exit codes: 0 (all deployed), 1 (any gap), 2 (parse error). `--fix` writes matching `rule_packs:` entries to `agent-config.yaml` for `user-level only` packs under the project repo lock; the lock-held write re-gathers state and exits with rc=1 + named packs if any `user_only`, `mismatch`, `broken`, `orphan`, or `lock_stale` row remains after the write. Atomic write via temp + `os.replace`; never modifies `pack-lock.json` or generated outputs. Credential URLs (`https://TOKEN@host/...`) are case-insensitively rejected before any print or write so token-bearing entries cannot leak to stdout or get persisted.
- **Session Start Check item 7 — pack-deployment audit.** The session-start banner gains a Session check line entry that compares user-level pack identity tuples against project-level identity tuples (after URL normalization via `normalize_pack_source_url`) and emits `⚠ N user-level pack(s) not deployed (run anywhere-agents pack verify)` when the count is non-zero. Mirrors byte-for-byte across aa+ac `AGENTS.md` plus the generated `CLAUDE.md` and `agents/codex.md` per repo (six files total). `tests/test_check_parity.py` pins the cross-variant equality.

### Internals

- Vendored `scripts/packs/locks.py` into the PyPI wheel so `pipx install anywhere-agents` always has a working repo-lock helper. `scripts/vendor-packs.py` now lists `locks.py` alongside `auth.py`, `source_fetch.py`, and `schema.py`.
- `auth.reject_credential_url` and `auth.redact_url_userinfo` are now case-insensitive on URL schemes (`HTTPS://`, `Git+SSH://`, etc.) per RFC 3986; uppercase token URLs are rejected and redacted the same as lowercase forms.
- `source_fetch.normalize_pack_source_url` lowercases GitHub host case and owner/repo case for identity-tuple comparison; non-GitHub hosts get a minimal lowercase-host normalization.
- `_load_project_observations` preserves same-file duplicate names so two `profile` rows in `agent-config.yaml` with different refs surface as `config mismatch` instead of last-wins collapse. Cross-file local-overrides-tracked semantics preserved.
- `_identity_for_default_selection` reads `.agent-config/repo/bootstrap/packs.yaml` so default-seeded packs (`agent-style`, `aa-core-skills`) compare against the same source/ref the composer writes into the lock. Malformed bundled `packs.yaml` now propagates as exit 2 instead of silent fallback.

## [0.5.0] — 2026-04-26

### Highlights

- **Direct-URL pack consumption.** `agent-config.yaml` entries can now point at any GitHub URL with `source.url` and `source.ref`. The 4-method auth chain (SSH agent, `gh` CLI token, `GITHUB_TOKEN` env, anonymous fallback) negotiates access automatically; private repos work without manual checkouts. Public URLs succeed on anonymous; private URLs succeed on whichever authenticated method the host has configured.
- **Trust-model shift.** The default `update_policy` flipped from `locked` to `prompt`. Each bootstrap surfaces upstream drift via a banner listing the affected packs and files; the consumer applies or skips per-run. `ANYWHERE_AGENTS_UPDATE=apply` short-circuits the prompt for CI / scripted refresh; `update_policy: locked` remains available as an explicit per-pack opt-in for content that must never auto-refresh.
- **agent-pack v0.1.0 one-line install.** `anywhere-agents pack add https://github.com/yzhao062/agent-pack --ref v0.1.0` is the v0.5.0 acceptance test for the direct-URL path. Installs the three packs declared in the upstream `pack.yaml` (`profile`, `paper-workflow`, `acad-skills`) by default; `--pack <name>` filters to a subset.
- **CLI additions.** `anywhere-agents pack update <name>` performs an auth-aware ref refresh against the configured source. `anywhere-agents pack list --drift` runs a read-only scan against `.agent-config/pack-lock.json` and reports any pack whose resolved commit or input hash has drifted from the lock.
- **Internals.** `scripts/packs/{auth,source_fetch,schema,config,reconciliation}.py` are new. The `reconcile_orphans` orchestrator wrapper is now invoked by the composer's bootstrap entry path before the main compose step. The PyPI wheel vendors `auth`, `source_fetch`, and `schema` so `pipx install anywhere-agents` exposes the full CLI surface without requiring a sibling source clone.
- **Deferred to v0.6.0: end-to-end cmd-log harness for stricter CI token-leak coverage.** v0.5.0 ships the redaction primitives (`auth.redact_url_userinfo` + `auth.redact_secret_text`) and the `tests/test_packs_auth_chain.TestRedact*` unit assertions as the v0.5.0 token-leak guard. The CI smoke step that previously gated on `.agent-config/.test-cmd-log.jsonl` was a no-op because no v0.5.0 code wrote that file; the harness will land in v0.6.0.

### Migration

See `MIGRATIONS.md` for the 2026-04-26 entry. Existing bootstrap caches must seed-refresh once; `bash .agent-config/bootstrap.sh` (or the PowerShell equivalent) refreshes the cache and picks up the new env wirings.

### Also in this release

- **Docs overhaul: README, README.zh-CN, and docs/index.md aligned to v0.4.0 pack-architecture positioning.** Replaces the v0.3-era scenario-first narrative with a 4-paragraph Why section (multi-agent, many-repos, review loop, writing rules), a 4-paragraph How It Works that explicitly carries the v0.4.0 vs v0.4.x boundary (CLI writes user-level `packs:` today; bootstrap consumes user-level + project-level `packs:` in v0.4.x; legacy project-level `rule_packs:` remains the only bootstrap-active project key in v0.4.0), a new "What This Looks Like" section with five examples in five different visual formats (session-banner screenshot, post-bootstrap repo tree, dual-agent generation Mermaid, Without/With writing-style HTML table, guard-deny terminal mock), a Pack Management CLI section with embedded `docs/pack-cli-demo.gif`, and a What's Next paragraph. zh-CN README mirrors the same structure with warm conversational tone (你, not 您) and English technical terms preserved (`pack`, `composer`, `bootstrap`, `skill`, `hook`, `guard`). Title Case applied globally to README + docs/index.md headings per RULE-G.
- **Visual identity: USC cardinal `#990000` → warm burgundy `#8b2635`.** Hero PNG (`docs/hero.html` + re-rendered `docs/hero.png`), session-banner PNG (`docs/banner.html` + re-rendered `docs/session-banner.png`), README badge colors, Mermaid theme variables in README and three docs Mermaid blocks, and `docs/stylesheets/extra.css` for the Material light + slate schemes. Slate-mode body link contrast tuned to `#d36b77` / `#e4939b` for WCAG AA pass (4.71:1 / 6.86:1). New `docs/_render_hero.py` and `docs/_render_banner.py` helpers keep both PNGs reproducible.
- **CLI demo: `docs/pack-cli-demo.gif` (vhs).** New 18.8s GIF demonstrating `pack list` → `pack add aa-core-skills --ref v0.4.0` → `pack list` → `pack remove aa-core-skills` → `pack list`, rendered via the official `ghcr.io/charmbracelet/vhs` image pinned by digest. Tape (`docs/pack-cli-demo.tape`) + render helper (`docs/_render_gif.sh`) + demo wrapper scripts (`docs/_demo-helpers/`) committed for reproducibility.
- **README cleanup.** Dropped the Day-to-Day Usage collapsible (3 of 4 rows duplicated the Install section). Removed the duplicate "Claude Code + Codex primary support" bullet from Limitations and Caveats (already in What This Is Not). Updated Repo Layout to cover the v0.4.0 pack architecture (`bootstrap/packs.yaml`, `scripts/compose_packs.py`, `scripts/packs/`). Updated What This Is Not to reflect the v0.4.0 CLI + YAML manifest reality.

## [0.4.0] — 2026-04-23

### Added

- **Unified pack manifest (v2 schema).** `bootstrap/packs.yaml` replaces `bootstrap/rule-packs.yaml` as the canonical manifest; the old path continues to work as a loader alias through v0.5.x. The v2 schema adds an `active:` list alongside the existing `passive:` list, with four active `kind:` values (`skill`, `hook`, `permission`, `command`) and per-entry `hosts:` / `required:` semantics. Every v1 (legacy passive-only) manifest keeps parsing unchanged; consumer-visible `AGENTS.md` output is byte-identical for unchanged pack content.
- **Four active-kind handlers.** `kind: skill` deep-copies a skill directory into `.claude/skills/<name>/` and auto-emits a canonical `.claude/commands/<name>.md` pointer unless the manifest supplies an explicit pointer mapping (the four aa-shipped skills keep their custom pointer content via explicit mappings). `kind: hook` deploys hook files under `~/.claude/hooks/<pack>/NN-<name>.py` with manifest-order prefixes and merges an `owners:` list into `~/.claude/pack-state.json`. `kind: permission` merges declarative JSON into `~/.claude/settings.json`; distinct permission values from different packs coexist freely, matching values join owners, and the same logical output with different expected content fails closed with `user-level-output-conflict`. `kind: command` is a forward-compat slot (parse + warn + no-op in v0.4.0).
- **Pack-emitted command pointers for the four shipped skills.** `implement-review`, `my-router`, `ci-mockup-figure`, and `readme-polish` convert to `kind: skill` entries in `bootstrap/packs.yaml` bundled under the `aa-core-skills` pack. The four `.claude/commands/*.md` pointer files remain in the aa source tree during v0.4.0 for the no-PyYAML fallback path and drop from `scripts/check-parity.sh`'s STRICT list on the ac mirror side.
- **State files tracking pack lifecycle.** `.agent-config/pack-lock.json` records per-pack source identity (URL, requested ref, resolved commit) + per-file sha256 for every composed output. `.agent-config/pack-state.json` records project-local outputs (skill directories, command pointers). `~/.claude/pack-state.json` records shared user-level outputs (hooks, settings.json merge targets) with an `owners:` list keyed by `(kind, target_path)` so two consumer repos installing the same pack coexist safely. Full schema in `docs/pack-architecture.md` § "pack-lock.json schema".
- **Recoverable staged-transaction primitives.** The transaction journal (`transaction.json`), staging directory, per-file atomic rename (`os.replace`), and Windows 2-retry fallback for AV / IDE sharing violations ship as building blocks in `scripts/packs/transaction.py`, and composition routes output writes through them. Per-user and per-repo file locks (`flock` POSIX, `msvcrt.locking` Windows) and orphan reconciliation helpers (`scripts/packs/reconciliation.py`, classifying `LIVE` / `ROLLBACK_OK` / `ROLLFORWARD_OK` / `PARTIAL` / `DRIFT` / `MALFORMED`) are implemented and unit-tested with cross-process contention smokes, but **automatic startup reconciliation and composer-side multi-lock wiring are not yet enabled in `scripts/compose_packs.py`** — that wiring is a v0.4.x follow-up.
- **User-level config groundwork (CLI-writable; composer not yet consuming).** User-level config at `$XDG_CONFIG_HOME/anywhere-agents/config.yaml` (or `$HOME/.config/anywhere-agents/config.yaml`) on POSIX and `%APPDATA%\anywhere-agents\config.yaml` on Windows is writable via the new CLI (see below). `scripts/packs/config.py` implements the full 4-layer resolver (user-level → project-tracked `agent-config.yaml` → project-local `agent-config.local.yaml` → `AGENT_CONFIG_PACKS` env var) with explicit `packs: []` clear semantics and names-only env-var grammar (`AGENT_CONFIG_PACKS="name1,-name2"` with `-name` subtract). **In v0.4.0, `scripts/compose_packs.py` still resolves bootstrap selections through the legacy project-tracked / project-local / env path**; wiring the composer to the new 4-layer resolver so user-level entries actually drive consumer bootstrap is a v0.4.x follow-up. Writing to user-level config via the CLI is useful now for pipx / pip users who want to stage their pack preferences ahead of the composer-side wiring.
- **CLI subcommands: `pack add/remove/list` + `uninstall --all`.** `anywhere-agents pack add <source> [--name NAME] [--ref REF]` writes to the user-level config file atomically (temp + `os.replace`) with malformed-YAML refuse-to-rewrite. First-add default preservation: on an empty user-level config, `pack add` seeds `[{name: agent-style}, {user pack}]` so the default rule pack is not silently dropped. Legacy `rule_packs:` keys are migrated to `packs:` on first write. `anywhere-agents uninstall --all` delegates to the composer's internal uninstall engine (invoked via `.agent-config/repo/scripts/packs/uninstall.py`) and maps six typed outcomes to CLI exit codes: `0` (clean or no-op), `10` (lock timeout), `20` (drift), `30` (malformed state), `40` (partial cleanup). Drift is fail-closed: state files stay unchanged so the user can retry after manually resolving the drift.
- **Auth safety preconditions.** Credential-bearing source URLs are rejected at parse time across every config layer: HTTP(S) userinfo (`user@`, `user:pass@`, `<token>@`) and SSH URLs with a password component (`ssh://user:secret@host`) both reject; SSH transport usernames (`git@host:path`, `ssh://git@host/path`) pass through. Composer subprocess env sets `GIT_TERMINAL_PROMPT=0` and `GIT_SSH_COMMAND=ssh -o BatchMode=yes -o ConnectTimeout=10` so missing credentials surface as a bounded fetch error instead of hanging on an interactive prompt. GitHub URL normalization (`github.com` only) extracts the canonical `<owner>/<repo>` identity from the three URL shapes so the v0.5.0 auth chain can retry alternate methods on the same identity.
- **npm CLI pack-command delegation.** `npx anywhere-agents pack add/remove/list` and `npx anywhere-agents uninstall --all` shell out to the pipx-installed Python entry point (or `python -m anywhere_agents.cli` fallback). npm-only users who only invoke the bootstrap path see no behavior change; users who need pack management get an install hint pointing at `pipx install anywhere-agents`.

### Changed

- **`bootstrap/bootstrap.sh` and `bootstrap/bootstrap.ps1` call `compose_packs.py`** (the new unified composer) with a BC fallback to `compose_rule_packs.py` on pre-v0.4.0 sparse clones that predate the new script.
- **Generator emits LF on all platforms.** `scripts/generate_agent_configs.py` now passes `newline="\n"` to `write_text()` so regenerated `CLAUDE.md` / `agents/codex.md` match their committed LF-normalized form on Windows without relying on `.gitattributes` checkout conversion. Removes a false-positive `diff -q` failure in `scripts/pre-push-smoke.sh` on Windows.
- **Deprecation warnings.** `rule_packs:` key in `agent-config.yaml` and `AGENT_CONFIG_RULE_PACKS` env var are accepted with a `DeprecationWarning` through v0.6.x. Behavior preserved for v0.3.x consumers; a v1.0.0 release will promote these to hard-fail with an actionable migration error. `rule-packs.yaml` path in `.agent-config/repo/bootstrap/` similarly continues to work as a loader alias with `packs.yaml` taking precedence when both exist.

### Not yet available

- **Private-source packs.** The v2 schema accepts structured `source: {repo, ref}` URLs but v0.4.0's composer rejects any private source (SSH, explicit `auth:` field) at parse time with a "v0.5.0 feature" message. The noninteractive fetch env + URL normalization helpers ship in this release as preparation; the actual auth chain (SSH agent → `gh` CLI token → `GITHUB_TOKEN` → anonymous fallback) activates in v0.5.0.
- **User-level config + 4-layer resolver in consumer bootstrap.** The resolver exists in `scripts/packs/config.py` and the CLI writes the user-level file; bootstrap composition still resolves selections through the legacy project-tracked / project-local / env path. Wiring `scripts/compose_packs.py` to the new resolver (so user-level entries drive bootstrap) is a v0.4.x follow-up.
- **Composer-side lifecycle wiring.** Per-user and per-repo locks around the composition transaction, plus automatic startup orphan reconciliation, are implemented as primitives in Phase 2 and fully unit-tested but not yet invoked by `scripts/compose_packs.py`. Single-process composition in a well-behaved environment works correctly; concurrent-composer and mid-crash recovery scenarios are protected by the transaction layer's per-file atomic rename but not by the lock + reconciliation outer layer until the wiring lands in v0.4.x.
- **Default switch to `agent-style-field`.** The 9-rule slim variant planned for v0.4.x depends on `agent-style` shipping the slim pack first; `DEFAULT_SELECTIONS` stays on the full `agent-style` pack in v0.4.0.

### Review history

Five combined implementation phases (Phases 1-4+5 per `docs/pack-architecture.md`) went through `implement-review` Phase 0 plan-review (3 rounds on `archive/plans/PLAN-aa-v0.4.0.md`) plus per-phase code review with Codex (every phase) and GitHub Copilot (Phases 3 + 4+5 portions). Total findings: 14 High + 9 Medium + 4 Low across all rounds, every one Fixed or Deferred-with-rationale and closed before the corresponding phase commit. Copilot's first-pass review on Phase 3 suffered 10 hallucinated findings against non-existent files; a model switch for subsequent rounds resolved the issue. Codex was the only reviewer for Phase 4+5 after Copilot hit usage limits.

470 tests pass on Windows + Linux CI at the release-candidate commit, covering `os.replace` atomicity, cross-platform lock primitives (`fcntl.flock` POSIX / `msvcrt.locking` Windows), merkle `dir-sha256` computation on forward-slash and backslash path separators, and multiprocessing-based cross-process contention tests.

## [0.3.0] — 2026-04-21

### Added

- **Rule-pack composition in bootstrap.** Bootstrap can now stitch external always-on instruction bundles ("rule packs") into the composed `AGENTS.md` at install time. First rule pack: [`agent-style`](https://github.com/yzhao062/agent-style) (21 writing rules, pinned at `v0.3.2`), enabled by default. Consumers opt out with `rule_packs: []` in `agent-config.yaml` at the project root; customize with `rule_packs: - name: agent-style` plus optional `ref:`, or layer with `agent-config.local.yaml` (gitignored machine-local override) or `AGENT_CONFIG_RULE_PACKS` env var (transient one-run). Composition requires Python 3 + PyYAML; bootstrap attempts a best-effort `pip install --user pyyaml` when missing, and falls back to the verbatim upstream `AGENTS.md` plus a one-line tip when Python or PyYAML still are not available — no hard error. Covers the `bash` and `powershell` bootstrap paths symmetrically with matching CLI contracts (`--rule-packs PACK` dry helper, `--no-cache` refetch flag, `--help` usage).
- **`bootstrap/rule-packs.yaml` manifest** registering known rule packs. Adding a second rule pack is a PR that adds a manifest entry plus the pack author publishing `docs/rule-pack.md` at a stable git ref.
- **`scripts/compose_rule_packs.py` helper** implementing manifest parsing, config resolution across the four opt-in layers (tracked / local / env / dry helper flag), raw-GitHub fetch with SHA-256 cache, routing-marker validation regex as conservative superset of the per-agent generator grammar, and atomic temp-plus-rename write of the composed `AGENTS.md`.
- **`tests/test_compose_rule_packs.py`** with 90+ tests covering parser, composition golden-file, cache semantics (fetch-first / fallback / `--no-cache` always errors), path-traversal regression (user-controlled ref percent-encoded in cache filename), PyYAML-missing fallback, and CLI contracts for both `bootstrap.sh` and `bootstrap.ps1`.
- **`docs/rule-pack-composition.md`** long-form spec: rule-pack vs skill-pack layering, default behavior, opt-in precedence, pack-author anatomy, composition flow, manifest schema, dependency contract, cache and offline behavior, failure modes, registration process, and a note on the historical `.agent-config/` scratch directory name.
- **README `Rule packs` section** with opt-out, pin-ref, dry-helper recipes, plus a collapsible Historical naming block.

### Changed

- **`bootstrap/bootstrap.sh` and `bootstrap/bootstrap.ps1` reordered the sparse clone** to happen before the root `AGENTS.md` write so the composer helper and manifest are available inside `.agent-config/repo/` at compose time. Runs that set `rule_packs: []` as an explicit opt-out, or that fall back because Python / PyYAML are unavailable, still receive a verbatim upstream `AGENTS.md`; the default no-config path now attempts rule-pack composition.
- **`agent-config.local.yaml` auto-gitignored** alongside `.agent-config/` on every bootstrap run, so machine-local rule-pack overrides do not leak into commits.

## [0.2.0] — 2026-04-18

README and RTD redesign on top of the plan-first workflow in `implement-review`. The README is re-centered around three pillars (portable sync, review workflow, mechanical enforcement) earned from daily use, and the 0.1.8 PreToolUse gates are surfaced as a first-class feature via a reframed Scenario D covering four reader-facing gate families. The RTD `/skills/` rendering bug (Material icon shortcodes showing as literal text) is fixed.

### Added

- **`skills/implement-review/SKILL.md` "When to plan-review first" section.** Formalizes plan-review as Phase 0 before the existing staged-change review loop, for complex tasks where the shape of the work precedes and constrains execution (code refactors, paper outlines, proposal structure, data-pipeline redesigns, migration plans, release-process changes, etc.). Process: write `PLAN-<identifier>.md` in the most natural location for the task (repo root for code, paper-repo root for Overleaf-style docs, local scratch directory for non-git work), send to Codex as a pre-execution design review that reads the plan path rather than `git diff --cached`, iterate until clean, then execute and run the normal review cycle on the staged output. Scenario B in both READMEs gains a one-sentence mention.
- **Pre-scenarios "A day in this config" prelude** in `README.md` and `README.zh-CN.md`. Light phase-of-day rhythm (morning setup, midday review, afternoon drafting, evening safety check, session defaults in the background) so the scenarios read as daily behavior, not a feature list.
- **Section 0 benefit-preview sentence**: *"It is not only a style guide: hooks stop risky commands from proceeding silently and block flagged prose writes before they land."* Sets up the enforcement story before the scenarios. Covers both ask-behavior (destructive Git/GitHub) and deny-behavior (compound `cd`, writing-style, banner).
- **Scenario C enforcement cross-reference.** One sentence noting the ~40-word AI-tell ban is enforced by a PreToolUse hook on `.md`/`.tex`/`.rst`/`.txt` writes; pointer to Scenario D for the mechanism.
- **Limitations escape-hatch entry.** `AGENT_CONFIG_GATES=off` documented in both the English and Chinese Limitations sections for discoverability when a false positive blocks real work. Pairs with the Scenario D footer coverage.

### Changed

- **Section 0 "Why you want this" rewrite** in `README.md` and `README.zh-CN.md`. Keeps the cross-project drift opener (scattered per-repo `CLAUDE.md`, copy-paste divergence, only-in-your-head) and adds a daily-use evolution paragraph naming the three shipped pillars (portable sync, review workflow, mechanical enforcement) as the output of daily use rather than a curated feature list.
- **Scenario D renamed: "Git safety catches mistakes before they happen" → "Mechanical enforcement."** Restructured around four reader-facing gate families: destructive Git/GitHub asks for confirmation; compound `cd` is denied; writing-style banned-word writes on `.md`/`.tex`/`.rst`/`.txt` are denied; user-visible mutating tool calls before the session banner lands are denied (read-only and dispatch tools like `Read`, `Grep`, `Skill`, `Task` stay available so the agent can inspect state). Organizing idea: *what the hook intercepts before an agent action proceeds.* Keeps the force-push deny example as the vivid lead.
- **Scenario E default-stack table.** Guard-hook row expanded to describe all four gate families (asks for confirmation on destructive Git/GitHub; denies compound `cd`, writing-style banned words, and pre-banner user-visible mutating tool calls) instead of only "muscle-memory destructive commands."
- **`mkdocs.yml` adds `pymdownx.emoji` block** with Material's `emoji_index` and `emoji_generator`. Required by mkdocs-material to render `:material-*:` and `:octicons-*:` shortcodes as SVG icons; without it, `attr_list` alone leaves the shortcodes as literal text.
- **Reference-section sweep (both READMEs).** Repo layout now lists `packages/` (PyPI + npm CLI sources), `.githooks/`, all 5 files in `scripts/` (including `pre-push-smoke.sh` and `remote-smoke.sh`), root-level `CHANGELOG.md`/`CONTRIBUTING.md`/`RELEASING.md`/`LICENSE`/`mkdocs.yml`/`.readthedocs.yaml`, and the expanded CI matrix (Ubuntu + Windows + macOS, Python 3.9-3.13, 3 workflows). `scripts/guard.py` description upgraded from "blocks destructive commands" to the four-family framing; `skills/implement-review/` description mentions Phase 0 plan-review. "What is opinionated and why" table refreshed: safety-first row acknowledges the `AGENT_CONFIG_GATES=off` escape hatch; dual-agent row mentions optional Phase 0 plan-review; writing-style row notes PreToolUse enforcement. Scenario A "what appears in your project" tree adds `CLAUDE.md` and `agents/codex.md` (generated at bootstrap since 0.1.3).
- **`README.zh-CN.md` Fork step 3 catch-up.** The Chinese README had been carrying a pre-0.1.6 one-liner ("改他们 bootstrap 块里的 URL"). Replaced with the full argv / `AGENT_CONFIG_UPSTREAM` env var / persisted-file cascade explanation already in the English README since 0.1.6.

### Fixed

- **RTD `/skills/` page icon-shortcode rendering.** Material icon shortcodes in the skills card grid (`:material-magnify-scan:`, `:material-routes:`, `:material-image-frame:`, `:material-book-open-outline:`, `:octicons-arrow-right-24:`) rendered as literal text. Adding the `pymdownx.emoji` block restores icons on all four skill cards and the "Deep docs" arrows.

### Review history

0.2.0 went through two rounds of `implement-review` Phase 0 plan-review (plan-first mode against `agent-config/PLAN-readme-redesign.md`) plus the execution-phase staged-diff review.

- **Plan-review Round 1**: 5 findings (2 Medium + 3 Low) covering release-scope inconsistency on plan-first, "three gates" undercounting the canonical Mechanical Enforcement table, escape-hatch discoverability, Section 0 implementation-first wording, and a `zh-CN` scope-statement mismatch. All Fixed in Revision 2 of the plan.
- **Plan-review Round 2**: 5 findings (2 Medium + 3 Low) covering Section 0 over-centering on enforcement (dropping the portability story and the self-defeating "eventually give up" phrasing), prelude restating scenarios too literally, Move 3 (origin markers on scenario headings) not worth the cost, benefit-sentence accuracy across ask/deny families, and collapsing release-scope branches once 0.2.0 was accepted. All Fixed in Revision 3; Move 3 deferred.
- **Maintainer spot-check** (between Round 2 and execution review): caught stale content in reference collapsibles that the plan-review rounds did not scope — the Repo layout tree, the "What is opinionated and why" Safety-first row, the Scenario A project tree (both EN + zh-CN), and `README.zh-CN.md` Fork step 3 which had been carrying a pre-0.1.6 one-liner. Fixed in-place before the execution review.
- **Execution review Round 3**: 2 findings (1 Medium + 1 Low) covering banner-gate wording that overstated "all tool calls" (canonical behavior exempts read-only and dispatch tools like `Read`, `Grep`, `Skill`, `Task`, so only user-visible mutating tools are denied) and this review-history list's completeness. All Fixed.
- No High-priority findings in any round.

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

[Unreleased]: https://github.com/yzhao062/anywhere-agents/compare/v0.7.8...HEAD
[0.7.8]: https://github.com/yzhao062/anywhere-agents/compare/v0.7.7...v0.7.8
[0.7.7]: https://github.com/yzhao062/anywhere-agents/compare/v0.7.6...v0.7.7
[0.7.6]: https://github.com/yzhao062/anywhere-agents/compare/v0.7.5...v0.7.6
[0.7.5]: https://github.com/yzhao062/anywhere-agents/compare/v0.7.4...v0.7.5
[0.7.4]: https://github.com/yzhao062/anywhere-agents/compare/v0.7.3...v0.7.4
[0.7.3]: https://github.com/yzhao062/anywhere-agents/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/yzhao062/anywhere-agents/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/yzhao062/anywhere-agents/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/yzhao062/anywhere-agents/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/yzhao062/anywhere-agents/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.6...v0.6.0
[0.5.6]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/yzhao062/anywhere-agents/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/yzhao062/anywhere-agents/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/yzhao062/anywhere-agents/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.2.0
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
