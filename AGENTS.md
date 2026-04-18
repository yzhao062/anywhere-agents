<!-- Quick start: In Claude Code, read @AGENTS.md to run bootstrap, session checks, and task routing -->

> **Claude Code / Codex / other agents -- determine your context before proceeding:**
>
> **Source repo test:** If `bootstrap/bootstrap.sh`, `bootstrap/bootstrap.ps1`, and `skills/` all exist at the repo root, you are in the `anywhere-agents` source repo (or a fork with the same layout). **Skip the bootstrap block below** and proceed directly to `## Session Start Check`. Do not execute the bootstrap script in the source repo; it would try to bootstrap itself.
>
> **Consumer repo path:** Otherwise, you are in a project that consumes the shared config. **You MUST execute the matching bootstrap block below at the start of every session, before responding to any user request or running session checks.** Use the PowerShell block on Windows and the Bash block on macOS/Linux. Bootstrap is idempotent: run it every session even if `.agent-config/` already exists, so shared config, skills, and settings are refreshed from upstream before any work begins. Do not treat the block below as reference documentation -- it is a required runtime command. After running, report the refresh result in one line (for example, "bootstrap: refreshed AGENTS.md, skills, settings").

## Bootstrap: Shared Config and Skills

This file is the shared agent configuration hosted at `https://github.com/yzhao062/anywhere-agents`.
Project repos should include the following bootstrap block in their own `AGENTS.md` so that agents automatically pull the latest shared defaults and skills before starting any task. Because bootstrap overwrites the consumer's root `AGENTS.md` with this source on every run, the block below ends up serving two roles in consumer repos: it is both the embedded reference for how the setup works and the runtime command the agent executes each session (see the top-of-file note for the source-vs-consumer test that decides which role applies in the current repo).

### Bootstrap block for project repos

Copy the block below into the top of each project repo's `AGENTS.md`. Because bootstrap overwrites `AGENTS.md` on every run, put project-local overrides in `AGENTS.local.md`.

````
## Shared Agent Config (auto-fetched)

Before starting any task, fetch the shared agent configuration and skills. Use the block for your shell.

PowerShell (Windows):

```powershell
New-Item -ItemType Directory -Force -Path .agent-config, .claude, .claude/commands | Out-Null
Invoke-WebRequest -UseBasicParsing -Uri https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/bootstrap/bootstrap.ps1 -OutFile .agent-config/bootstrap.ps1
& .\.agent-config\bootstrap.ps1
```

Bash (macOS/Linux):

```bash
mkdir -p .agent-config .claude/commands
curl -sfL https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/bootstrap/bootstrap.sh -o .agent-config/bootstrap.sh
bash .agent-config/bootstrap.sh
```

This bootstrap flow refreshes the consuming repo's root `AGENTS.md` to match the shared copy. If a project later needs repo-local overrides, put them in `AGENTS.local.md`.

Read and follow the rules in `.agent-config/AGENTS.md` as baseline defaults. Any rule in `AGENTS.local.md` overrides the shared default.
When a skill is invoked, read its SKILL.md from `.agent-config/repo/skills/<skill-name>/SKILL.md`.
If a local `skills/<skill-name>/SKILL.md` exists in the project repo, the local copy takes precedence.
Copying `.agent-config/repo/.claude/commands/*.md` only overwrites command files with the same name as the shared repo and does not delete unrelated project-local commands.
Merge shared Claude project defaults (e.g., `permissions`, `attribution`) from `.agent-config/repo/.claude/settings.json` into the project `.claude/settings.json`. Shared keys are updated on every bootstrap run; project-only keys are preserved. Merge requires Python; if unavailable the existing file is left untouched.
Add `.agent-config/` to the project's `.gitignore` so fetched files are not committed.
Bootstrap also sets up user-level config: it copies `scripts/guard.py` to `~/.claude/hooks/` (a PreToolUse hook that guards against destructive commands) and merges `user/settings.json` into `~/.claude/settings.json` (shared permissions, hook wiring, and the `CLAUDE_CODE_EFFORT_LEVEL=max` env entry that sets the default effort level). Remove the user-level section from the bootstrap script if this is not wanted.
````

### What gets shared

| Content | Source | How fetched |
|---------|--------|-------------|
| User profile, writing defaults, formatting rules, environment notes | `AGENTS.md` (this file) | `curl` raw file |
| Per-agent rule files (`CLAUDE.md`, `agents/codex.md`) | Generated from `AGENTS.md` by `scripts/generate_agent_configs.py` | Regenerated locally on every bootstrap; hand-authored files preserved + warned |
| Shared skills (`implement-review`, `my-router`, `ci-mockup-figure`, `readme-polish`) | `skills/` directory (committed only) | sparse `git clone` |
| Claude pointer commands for shared skills | `.claude/commands/` | sparse `git clone` plus non-destructive copy into the project `.claude/commands/` |
| Claude project defaults (`permissions`, `attribution`, etc.) | `.claude/settings.json` | sparse `git clone` plus key-level merge into the project `.claude/settings.json` on every run |
| User-level hooks (`guard.py`, `session_bootstrap.py`) + settings | `scripts/` + `user/settings.json` | Scripts copied to `~/.claude/hooks/`; settings merged into `~/.claude/settings.json` (shared permissions, PreToolUse guard, SessionStart bootstrap hook, `CLAUDE_CODE_EFFORT_LEVEL=max`) |

### Override rules

- If `AGENTS.local.md` exists in the project root, read and follow it after `AGENTS.md`. Rules in `AGENTS.local.md` override the shared defaults.
- Rules in `AGENTS.local.md` always win over shared defaults. Do not edit the root `AGENTS.md` for local overrides, as bootstrap will overwrite it.
- Project-local `skills/<name>/SKILL.md` always wins over the shared copy of the same skill.
- Shared keys in `.claude/settings.json` are updated on every bootstrap run. Project-only keys are preserved. To override a shared key locally, use `.claude/settings.local.json`.
- If a shared skill does not exist locally, the agent should use the fetched copy from `.agent-config/repo/skills/`.

### Configuration Precedence

Three independent configuration layers, each with its own precedence rules. When two rules conflict, the more specific source wins.

**1. Agent rule files (Markdown)** — most specific wins:

| Layer | File | Scope |
|---|---|---|
| 1 | `CLAUDE.local.md` / `agents/codex.local.md` | Per-agent + project-local. Hand-authored; never touched by bootstrap. |
| 2 | `AGENTS.local.md` | Cross-agent + project-local. Hand-authored; never touched by bootstrap. |
| 3 | `CLAUDE.md` / `agents/codex.md` | Per-agent, generated from `AGENTS.md` by `scripts/generate_agent_configs.py`. |
| 4 | `AGENTS.md` | Cross-agent, synced from upstream on every bootstrap. |

The generated `CLAUDE.md` and `agents/codex.md` carry a `GENERATED FILE` header. If a consumer project has a hand-authored `CLAUDE.md` (or `agents/codex.md`) without that header, the generator preserves it and warns loudly — it never silently overrides user work. To adopt upstream rules in that case, rename the hand-authored file to `CLAUDE.local.md` (which still wins via layer 1).

**2. Claude Code settings (`settings.json`)** — follow Claude Code's own precedence: `managed policy` > `command-line arguments` > `.claude/settings.local.json` > `.claude/settings.json` > `~/.claude/settings.json`. Bootstrap only writes to the project-shared and user-level layers, and merges shared keys while preserving project-only keys.

**3. Environment variables** — for effort level specifically: `managed policy > CLAUDE_CODE_EFFORT_LEVEL env var > persisted effortLevel > default`.

---

<!-- Everything above this line is bootstrap setup instructions. -->
<!-- Everything below this line contains the shared rules that agents should read and follow. -->

## Session Start Check

**Mandatory turn-start procedure.** Before generating the first content of any response, apply the branch that matches your runtime.

**In Claude Code:** the flag files are per-project. `<project-root>` is the consumer-repo root: walk up from `cwd` until a directory with `.agent-config/bootstrap.sh` or `.agent-config/bootstrap.ps1` is found. Read `<project-root>/.agent-config/session-event.json` and `<project-root>/.agent-config/banner-emitted.json`.

1. If `session-event.json.ts > banner-emitted.json.ts`, OR `session-event.json` exists but `banner-emitted.json` does not: emit the session start banner as the **literal first content of your response**, then write the event `ts` into `<project-root>/.agent-config/banner-emitted.json`. Only after the banner text may you address the user's request on the same turn.
2. Otherwise (emitted `ts` is already current, or neither file exists): skip the banner this turn.

`session_bootstrap.py` writes `session-event.json` on every SessionStart hook fire (fresh startup, resume, clear, compact), so the banner reappears across all four lifecycle events. Flag files are per-project, so opening multiple Claude Code windows in different consumer repos does not cause cross-session interference.

**In a source repo (`agent-config` or `anywhere-agents`, no `.agent-config/` at the root):** the banner gate in `guard.py` is not active and the flag-file mechanism does not apply. Emit the banner on the first response of the session (turn with no prior assistant turns in context); skip on subsequent turns. Compact / resume / clear cannot be mechanically distinguished here.

**In Codex:** Codex has no `SessionStart` hook equivalent; `session-event.json` is not written during a Codex invocation. Each Codex invocation is a new session. Emit the banner as the literal first content of your response on the turn where there are no prior assistant turns in context (i.e., the first response of the invocation). On subsequent turns in the same invocation, skip. No flag files are involved for Codex.

**Both runtimes:** this procedure overrides any other "skill-first" or "task-first" behavior. Even when the user's first message is a task prompt like "read the project" or "fix this bug," or when a skill such as `superpowers:using-superpowers` would otherwise fire before the response, emit the banner first; the task response or skill output comes after the banner on the same turn. Do not let task pressure, skill invocations, or brevity guidance suppress the banner.

### Format

```
📦 anywhere-agents active
   ├── OS: <platform>
   ├── Claude Code: <version>[ → <latest>] (auto-update: <on|off>) · <model> · effort=<level>
   ├── Codex: <version>[ → <latest>] · <model> · <reasoning> · <tier> · fast_mode=<bool>
   ├── Skills: <N> local (<names>) + <M> shared (<names>)
   ├── Hooks: PreToolUse <guard.py>, SessionStart <session_bootstrap.py>
   └── Session check: all clear
```

If anything is off, replace `all clear` with a semicolon-separated list of concrete issues, each actionable in one short clause (e.g., `⚠ actions/checkout@v4 in .github/workflows/validate.yml:17 — bump to v5; Codex config.toml missing model key`). Keep the whole banner to six lines plus the check line. The skills row may wrap visually when many names are present; do not omit a local or shared bucket just to preserve terminal width.

### How to populate each field

1. **OS** — read from the session environment (`win32`, `darwin`, `linux`). Use this elsewhere to pick platform-specific behavior (terminal review path on Windows, MCP on macOS/Linux, `.ps1` vs `.sh`).
2. **Claude Code** — format: `Claude Code <current>[ → <latest>] (auto-update: <on|off>) · <model> · effort=<level>`. Current version comes from Claude Code's startup header or `claude --version`. Read `~/.claude/hooks/version-cache.json` for `claude_latest`; render ` → <latest>` **only when current differs** from latest. Determine `auto-update: on` when `DISABLE_AUTOUPDATER` is not `1` in the effective env (OS env or `env` block in `~/.claude/settings.json`) AND `~/.claude.json` top-level `autoUpdates` is not explicitly `false` — a missing key counts as `on` because native installs auto-update by default. Only explicit `autoUpdates: false` (which bootstrap heals on the next run) or the disable env var means `off`. User prefers the highest available model at max effort; flag any drift once in the banner, not every turn.
3. **Codex** — format: `Codex <current>[ → <latest>] · <model> · <reasoning> · <tier> · fast_mode=<bool>`. Current version from `codex --version`. Latest from `~/.claude/hooks/version-cache.json` `codex_latest` (render ` → <latest>` only when current differs). Config from `~/.codex/config.toml` (or `%USERPROFILE%\.codex\config.toml` on Windows): `model` · `model_reasoning_effort` · `service_tier` · `[features].fast_mode`. Expected values: `model = "gpt-5.4"` (or latest), `model_reasoning_effort = "xhigh"`, `service_tier = "fast"`, `[features] fast_mode = true`. If the binary is not on PATH, show `Codex: not installed`. If the binary exists but `config.toml` is missing, show version + `not configured` in place of the config summary.
4. **Skills** — list both active sets. Count directories under `skills/` (project-local) and `.agent-config/repo/skills/` (bootstrapped). For the shared count/list, exclude any shared skill whose name also exists under project-local `skills/`, because project-local overrides shared on name conflict. Format: `<N> local (<names>) + <M> shared (<names>)`. Omit either half if empty (e.g., `4 shared (...)` when the consumer has no project-local `skills/`).
5. **Hooks** — check `~/.claude/hooks/` for `guard.py` (PreToolUse) and `session_bootstrap.py` (SessionStart). If one is missing, include it in the Session check line as an issue.
6. **Session check** — scan `.github/workflows/*.yml` for action version pins below the minimums in the GitHub Actions Standards section. Combine with any Codex-config or hook drift detected above. Emit `all clear` only when nothing needs attention.

## User Profile

- These are user-level defaults that can be reused across projects unless a local repo rule or task-specific instruction is stricter.
- **Customize this section in your fork of `anywhere-agents`** to describe your role, domain, and common task types. Agents read this to tailor their work (e.g., a researcher vs. a backend engineer vs. a data scientist will get different defaults).
- If your fork serves multiple use cases, keep the description general ("developer working on infrastructure and research tooling") rather than overspecifying.

## Agent Roles

- **Claude Code** is the primary workhorse: drafting, implementation, research, and heavy-lifting tasks.
- **Codex** is the gatekeeper: review, feedback, and quality checks on work produced by Claude Code or the user.
- When both agents are available, default to this division of labor unless the user overrides it.

## Task Routing

- Before starting a task, read the router skill to determine which domain skill to use. Look for it in this order: `skills/my-router/SKILL.md` (repo-local), then `.agent-config/repo/skills/my-router/SKILL.md` (bootstrapped from shared config).
- The router inspects prompt keywords, file types, and project structure to dispatch automatically. Do not ask the user which skill to use when the routing table provides a clear match.
- If the `superpowers` plugin is active, the router operates during the execution phase. Superpowers handles the outer workflow (brainstorm, plan, execute, verify); the router handles inner dispatch to the right domain skill.
- If routing is ambiguous (multiple skills could apply), state the detected context and proposed skill, then ask the user to confirm.

<!-- agent:codex -->
## Codex MCP Integration

- Codex is available to Claude Code as an MCP server. Register it once at the user level so it applies to all projects and terminals (including PyCharm):
  ```
  claude mcp add codex -s user -- codex mcp-server -c approval_policy=on-failure
  ```
- This writes to `~/.claude.json` top-level `mcpServers`. A session restart is required after registration for `/mcp` to pick it up.
- **Migrating an existing registration:** If Codex was registered without `-c approval_policy=on-failure`, remove and re-add:
  ```
  claude mcp remove codex -s user
  claude mcp add codex -s user -- codex mcp-server -c approval_policy=on-failure
  ```
  On Windows, adjust the path as shown below.
- **Gotcha:** Do not register under a project scope (e.g., from a specific working directory without `-s user`). That creates a project-scoped entry under `projects["<path>"].mcpServers` in `~/.claude.json`, which does not propagate to other directories.
- Prerequisites: Node.js installed, Codex CLI installed (`npm install -g @openai/codex`), and `OPENAI_API_KEY` set.
- **Recommended Codex defaults (as of April 2026):** Add or update these keys in `~/.codex/config.toml` on macOS/Linux or `%USERPROFILE%\.codex\config.toml` on Windows (create the file if it does not exist) so that both interactive sessions and the MCP server use the recommended default model with fast inference:
  ```toml
  model = "gpt-5.4"
  model_reasoning_effort = "xhigh"
  service_tier = "fast"

  [features]
  fast_mode = true
  ```
  `service_tier = "fast"` selects the fast inference tier (1.5x speed, no quality reduction). For ChatGPT-authenticated users this costs 2x credits; API-key users pay standard API pricing. The `[features].fast_mode` flag gates the feature and defaults to `true`; set it explicitly alongside `service_tier` to persist the default in `config.toml`. Omit both if you prefer lower cost over latency. The MCP server reads the same `config.toml`, so these settings apply to both interactive sessions and MCP. These settings work identically on macOS, Linux, and Windows.
- MCP tools available after registration: `codex` (new prompt) and `codex-reply` (continue an existing session).
- **Windows note:** Claude Code launches MCP servers through bash, not cmd or PowerShell. This means `.cmd` wrappers and PowerShell variables like `$env:APPDATA` do not work. If `codex` is not on `PATH`, use the full path with forward slashes and **no `.cmd` extension** (npm installs a bash-compatible script alongside the `.cmd`):
  ```
  claude mcp add codex -s user -- C:/Users/<you>/AppData/Roaming/npm/codex mcp-server -c approval_policy=on-failure
  ```
  Run `where codex` (cmd) or `Get-Command codex` (PowerShell) to find the actual path.
- **MCP approval policy:** By default the Codex MCP server prompts for approval on every shell command, which surfaces as "MCP server requests your input" dialogs in Claude Code. Pass `-c approval_policy=on-failure` in the registration command (shown above) so commands auto-approve and only prompt on failures. The same key can be set in `config.toml` (`approval_policy = "on-failure"`) for interactive sessions.
- **Bitdefender false positives (Windows):** Bitdefender Advanced Threat Defense may flag Codex and Claude Code shell commands as "Malicious command lines detected." To suppress this, add exceptions in Bitdefender → Protection → Manage Exceptions. For each exception, enable the **Advanced Threat Defense** toggle (not just Antivirus). Recommended exceptions:
  - `C:\Program Files\nodejs\node.exe` (process)
  - `C:\Users\<you>\.local\bin\claude.exe` (process)
  - `C:\Users\<you>\AppData\Roaming\npm\codex` (process)
  - `C:\Users\<you>\AppData\Roaming\npm\codex.cmd` (process)
  - `C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe` (process, if Codex invokes PowerShell)
- **Windows recommendation: use the terminal path.** On Windows (11 Build 26200+), the MCP path still has rough edges — residual approval prompts and Bitdefender false positives add friction even after the mitigations above. The terminal path (relay reviews via the Codex interactive terminal window) avoids both issues. Prefer the terminal path on Windows; use MCP on macOS/Linux where it works smoothly.
<!-- /agent:codex -->

## Writing Defaults

- Use scientifically accessible language.
- Do not oversimplify unless the user asks for simplification.
- Keep meaningful technical detail.
- Keep factual accuracy and clarity high in scientific contexts.
- Use consistent terms. If an abbreviation is defined once, do not define it again later.
- If citing papers, verify that they exist.
- When paper citations are requested, provide BibTeX entries that can be copied into a `.bib` file.
- Provide code only when necessary. Confirm that the code is correct and can run as written.
- Avoid the following words and close variants unless the user explicitly asks for them (a default AI-tell list; trim or extend in your fork): `encompass`, `burgeoning`, `pivotal`, `realm`, `keen`, `adept`, `endeavor`, `uphold`, `imperative`, `profound`, `ponder`, `cultivate`, `hone`, `delve`, `embrace`, `pave`, `embark`, `monumental`, `scrutinize`, `vast`, `versatile`, `paramount`, `foster`, `necessitates`, `provenance`, `multifaceted`, `nuance`, `obliterate`, `articulate`, `acquire`, `underpin`, `underscore`, `harmonize`, `garner`, `undermine`, `gauge`, `facet`, `bolster`, `groundbreaking`, `game-changing`, `reimagine`, `turnkey`, `intricate`, `trailblazing`, `unprecedented`.

## Formatting Defaults

- Preserve the original format when the input is in LaTeX, Markdown, or reStructuredText.
- Do not convert paragraphs into bullet points unless the user asks for that format.
- Prefer full forms such as `it is` and `he would` rather than contractions.
- `e.g.,` and `i.e.,` are fine when appropriate.
- Do not use Unicode character `U+202F`.
- Avoid heavy dash use. Do not use em dashes (`—`) or en dashes (`–`) as casual sentence punctuation. Prefer commas, semicolons, colons, or parentheses instead. En dashes in numeric ranges (e.g., `1–3`, `2020–2025`), paired names, or citations are fine. Normal hyphenation in compound words and technical terms (e.g., `command-line`, `co-PI`, `zero-shot`) is fine and should not be avoided.
- Break extremely long or complex sentences into shorter, more readable ones. If a sentence has multiple clauses or nested qualifications, split it.
- Vary sentence length and structure. Prefer not to start several consecutive sentences with the same word or phrase. Avoid overusing transition words like "Additionally" or "Furthermore." Not every paragraph needs a tidy summary sentence at the end. Mix short, direct sentences with longer ones to keep the writing natural.

## Git Safety

- **Never run `git commit` or `git push` without explicit user approval.** Always show the proposed action and ask for confirmation before executing.
- This rule is non-negotiable and applies to all projects that consume this shared config.
- This includes any variant: `git commit -m`, `git commit --amend`, `git push`, `git push --force`, `gh pr create` (which pushes), etc.

## Mechanical Enforcement

Bootstrap deploys `scripts/guard.py` to `~/.claude/hooks/guard.py` and wires it as a `PreToolUse` hook in `~/.claude/settings.json`. The hook runs before every tool call and mechanically enforces the following:

| Gate | Tool scope | Trigger | Action |
|---|---|---|---|
| Writing-style | `Write`, `Edit`, `MultiEdit` on `.md` / `.tex` / `.rst` / `.txt` | Outgoing content contains a banned AI-tell word (see Writing Defaults list) | **deny** with hit list |
| Banner emission | Any tool except `Read`, `Grep`, `Glob`, `Skill`, `Task`, `TodoWrite`, `BashOutput`, `WebFetch`, `WebSearch`, `ToolSearch`, `LS`, `NotebookRead`; plus `Write`/`Edit`/`MultiEdit` whose target path exactly equals `<project-root>/.agent-config/banner-emitted.json` after absolute-path normalization and Windows case folding | `<project-root>/.agent-config/session-event.json.ts > <project-root>/.agent-config/banner-emitted.json.ts`. `<project-root>` is found by walking up from `cwd` until `.agent-config/bootstrap.{sh,ps1}` is present. Source repos (no `.agent-config/`) and unrelated directories skip the gate entirely | **deny** with instruction to emit banner + write acknowledgment to the per-project ack file |
| Compound `cd` | `Bash` | Command contains `cd <path> && <cmd>` or `cd <path>; <cmd>` | **deny** with suggestion to use `git -C` or path arguments |
| Destructive git | `Bash` | `git push`, `git commit`, `git merge`, `git rebase`, `git reset --hard`, `git clean`, `git branch -d/-D`, `git tag -d`, `git stash drop/clear` | **ask** (user confirms) |
| Destructive gh | `Bash` | `gh pr create`, `gh pr merge`, `gh pr close`, `gh repo delete` | **ask** (user confirms) |

**Escape hatch:** set env var `AGENT_CONFIG_GATES=off` (or `0`/`disabled`/`false`) via the `env` block in `~/.claude/settings.json` to disable the two new gates (writing-style and banner). The compound-cd / destructive-git / destructive-gh checks remain active regardless, since they guard against muscle-memory mistakes that do not tolerate false positives.

Setting the escape hatch is the right move when a legitimate write has a banned word in *meta-discussion* context (for example, a style-guide document that quotes banned words as examples of what to avoid), or when a prompt-layer failure is blocking legitimate work. Fix the false positive, then remove the override.

## Shell Command Style

- **Avoid compound `cd <path> && <command>` chains.** Claude Code's hardcoded compound-command protection prompts for approval on these even when both commands are individually allowed. Use alternatives that keep each tool call to a single command:
  - For git in another repo: use `git -C <path> <subcommand>` instead of `cd <path> && git <subcommand>`.
  - For non-git commands: pass the target path as an argument (e.g., `ls <path>`, `python <path>/script.py`) or use separate tool calls.
- Examples of read-only invocations that should not require approval: `git status`, `git diff`, `git log`, `git branch` (no flags), `git show`, `git stash list`, `git remote -v`, `git submodule status`, `git ls-files`, `git tag --list`. Filesystem reads (`ls`, `cat`) and benign local operations (`mkdir`) are also fine.
- Examples of invocations that always require explicit approval: `git commit`, `git push`, `git reset`, `git checkout`, `git rebase`, `git merge`, `git branch -d`, `git remote add/remove`, `git tag <name>` (creating/deleting), `git stash drop`.
- Filesystem commands like `cp` and `mv` are fine for scratch and temporary files. Moves or renames that affect git-tracked files should be reviewed before executing.
- **Avoid inline Python with `#` comments in quoted arguments.** Claude Code flags "newline followed by `#` inside a quoted argument" as a path-hiding risk and prompts for approval. Instead, write the code to a `.py` file and run `python <script>.py`.

## GitHub Actions Standards

GitHub is deprecating Node.js 20 actions. Runners begin using Node.js 24 by default on June 2, 2026, and GitHub's public changelog currently says Node.js 20 removal will happen later in fall 2026. Keep workflow action pins at or above the first Node.js 24 major for the GitHub-maintained actions below:

| Action | Minimum version (Node.js 24) | Replaces |
|--------|------------------------------|----------|
| `actions/checkout` | **v5** | v3, v4 |
| `actions/setup-python` | **v6** | v5 |
| `actions/setup-node` | **v5** | v4 |
| `actions/upload-artifact` | **v6** | v4, v5 |
| `actions/download-artifact` | **v7** | v4, v5, v6 |

When the session start check (item 4) detects older versions, list the affected files and suggest the minimum Node.js 24 version from this table. If a repository intentionally wants the latest major instead of the minimum compatible major, flag that as a separate manual upgrade because later majors can include behavior changes. If a workflow pins a SHA instead of a tag (e.g., `actions/checkout@abc123`), flag it for manual review rather than auto-suggesting a tag. For self-hosted runners, also remind the user that these Node.js 24 actions require an Actions Runner version that supports Node.js 24.

## Environment Notes

- Do not conclude that Python is unavailable just because `python`, `python3`, or `py` fails in `PATH`; those may resolve to shims, store aliases, or the wrong interpreter. Inspect common environment managers (Miniforge/Conda, pyenv, uv, venv) before reporting Python as missing.
- If the user's fork sets a preferred Python interpreter path in `AGENTS.local.md`, use that first.
- GitHub CLI (`gh`) is used for PR and issue workflows. If `gh` is not found, remind the user to install it (`winget install GitHub.cli` on Windows, `brew install gh` on macOS, `gh` from the distro package manager on Linux) and authenticate with `gh auth login`.
<!-- agent:claude -->
- **Claude Code installation**: Prefer the **native installer**. Migrate off npm and winget when possible.
  - macOS: `curl -fsSL https://claude.ai/install.sh | sh`
  - Windows (PowerShell, no admin): `irm https://claude.ai/install.ps1 | iex` (requires Git for Windows)
  - To migrate from npm: `npm uninstall -g @anthropic-ai/claude-code` first. From winget: `winget uninstall Anthropic.ClaudeCode` first.
  - Native installs auto-update in the background by default. Use `/config` inside Claude Code to set the release channel (`latest` or `stable`). Run `claude doctor` to inspect updater status, and `claude update` to force an immediate update check.
  - To disable auto-updates, set `DISABLE_AUTOUPDATER=1` in the environment or add `"env": {"DISABLE_AUTOUPDATER": "1"}` to `~/.claude/settings.json`. The env var takes precedence regardless of other flags. **Caveat:** if you migrated from npm or winget, an earlier install may have left `"autoUpdates": false` at the top level of `~/.claude.json`. Observed behavior is that the native updater daemon never spawns when that flag was already false at launch, even with `autoUpdatesProtectedForNative: true`. Bootstrap now heals this by flipping the stale flag to `true` on every run, so the env-var path is the only supported way to opt out.
- **Claude Code effort level**: As of Claude Code v2.1.111, the `/effort` slider exposes five levels: `low`, `medium`, `high`, `xhigh`, `max`. The persisted `effortLevel` key in `settings.json` accepts `low`, `medium`, `high`, and `xhigh` (v2.1.111 added `xhigh` as a valid persisted value). `max` remains session-only: selecting `max` via `/effort` silently does not persist. To get `max` as a persistent default across every project and session, set the env var `CLAUDE_CODE_EFFORT_LEVEL=max` in `~/.claude/settings.json` under `"env"`. The shared `user/settings.json` in this repo sets the env var, and bootstrap merges it into `~/.claude/settings.json`, so running bootstrap once on any consuming project lands the user-level default. Runtime precedence: managed policy > `CLAUDE_CODE_EFFORT_LEVEL` env var > persisted `effortLevel` (local > project > user) > Claude Code's built-in default. When the env var is set, it outranks `--effort` at launch and `/effort` inside a session; the slash command prints a warning that the env var is overriding the live effort. When the env var is unset, `--effort <level>` at launch is a session-only override, `/effort low|medium|high|xhigh` updates the persisted user setting, and `/effort max` is session-only.
<!-- /agent:claude -->

## Local Skills Precedence

- If the workspace contains a `skills/` directory, treat repo-local skills as the default source of truth for that project.
- When a task matches a skill name and both a repo-local `skills/<skill-name>/SKILL.md` and an installed global skill exist, prefer the repo-local skill.
- When using a repo-local skill, read `skills/<skill-name>/SKILL.md` and its local `references/`, `scripts/`, and `assets/` before falling back to any globally installed copy.
- Do not modify a globally installed skill when a repo-local skill of the same name exists, unless the user explicitly asks to update the global copy too.
- If a repo-local skill overrides a global skill, state briefly that the local project copy is being used.

## Cross-Tool Skill Sharing

- Skills under `skills/` are shared between coding agents (Codex, Claude Code, and any future agent).
- `skills/<skill-name>/SKILL.md` is the single source of truth for each skill. Agent-specific config files (e.g., `agents/openai.yaml`) are thin wrappers and must not duplicate or override the logic in `SKILL.md`.
- Claude Code accesses these skills via pointer commands in `.claude/commands/`. Each pointer file references the corresponding `SKILL.md` rather than duplicating its content.
- Bootstrap sync should copy only the shared repo's `.claude/commands/*.md` files into the project `.claude/commands/` directory and should not delete unrelated project-local commands.
- When editing a skill, modify `SKILL.md` and its `references/` or `scripts/` directly. Do not create agent-specific forks of the same content.
- If a new skill is added, create both the `skills/<skill-name>/SKILL.md` structure and a matching `.claude/commands/<skill-name>.md` pointer so both agents can use it immediately.
