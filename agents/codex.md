<!--
GENERATED FILE -- do not edit by hand.

This file is regenerated from AGENTS.md by scripts/generate_agent_configs.py.
Bootstrap re-runs the generator on every session, so edits here are lost.

Precedence for agent rule files (most specific wins):
  1. agents/codex.local.md      your per-agent, per-project overrides
  2. AGENTS.local.md   your cross-agent, per-project overrides
  3. agents/codex.md       generated from AGENTS.md (this file)
  4. AGENTS.md         upstream baseline

To customize just for Codex in this project, create agents/codex.local.md
(the generator never touches it). To customize for every agent in this
project, edit AGENTS.local.md. To change upstream rules for everyone,
edit AGENTS.md in your fork.
-->

# Shared agent rules (anywhere-agents)

This file holds rules. A rule says what to do, plus one clause on why when the rule is counter-intuitive. Longer rationale, measurements, version history, and how-to material go to the docs linked at the end or to `CHANGELOG.md`, never here. Tests enforce the size of this file.

<!-- Byte-identical in anywhere-agents and agent-config (check-parity STRICT); size gate in tests/test_bootstrap_size.py; maintainer-only lines go in AGENTS.local.md. -->

> **Determine your context before proceeding.**
>
> **Dispatched reviewer:** A dispatched reviewer whose task, lens, and response format are supplied is exempt from this file's bootstrap, router, and coordinator-skill discovery instructions; it applies substantive project policy, including applicable local overrides, reads missing instruction files once, and may read any of these files when they are themselves in scope or answer a concrete verification question.
>
> **Source repo test:** If `bootstrap/bootstrap.sh`, `bootstrap/bootstrap.ps1`, `scripts/generate_agent_configs.py`, and `skills/` all exist at the repo root, you are in a source repo (`anywhere-agents`, `agent-config`, or a fork with the same layout). Skip the bootstrap block below and proceed directly to `## Session Start Check`. Do not execute the bootstrap script in a source repo; it would try to bootstrap itself.
>
> **Consumer repo path:** Otherwise, you are in a project that consumes the shared config. **You MUST execute the matching bootstrap block below at the start of every session, before responding to any user request or running session checks.** Use the PowerShell block on Windows and the Bash block on macOS/Linux. Bootstrap is idempotent: run it every session even if `.agent-config/` already exists, so shared config, skills, and settings are refreshed from upstream before any work begins. After running, report the refresh result in one line (for example, "bootstrap: refreshed AGENTS.md, skills, settings").

## Bootstrap

Project repos carry this block at the top of their `AGENTS.md`; bootstrap rewrites it there on every run, so it is both the reference and the runtime command.

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

Read and follow the rules in `.agent-config/AGENTS.md` as baseline defaults. Any rule in `AGENTS.local.md` overrides the shared default.
````

Rules that follow from the run:

- Bootstrap rewrites the consuming repo's root `AGENTS.md` on every run, composing the shared copy with the configured passive packs (by default `agent-style`, which needs Python 3 and PyYAML), then regenerates `CLAUDE.md` and `agents/codex.md` from it. When packs are configured but composition cannot run, an existing composed `AGENTS.md` is preserved rather than replaced by the bare shared copy; that run warns and records `completed: false` in `.agent-config/last-run.json`.
- Put repo-local overrides in `AGENTS.local.md`, never in the generated files. Read `AGENTS.local.md` after `AGENTS.md`; its rules win. Codex does not discover it on its own, which is why this sentence exists.
- Shared command pointers are copied into `.claude/commands/`; the copy overwrites same-named files only and does not delete unrelated project-local commands.
- Shared keys of `.claude/settings.json` (`permissions`, `attribution`, `effortLevel`, and the like) are merged on every run and project-only keys are preserved. Override a shared key locally in `.claude/settings.local.json`.
- Bootstrap also refreshes user-level files: `~/.claude/hooks/guard.py` (PreToolUse), `~/.claude/hooks/session_bootstrap.py` (SessionStart), the statusline scripts, and the shared `env` entries of `~/.claude/settings.json`, including `CLAUDE_CODE_EFFORT_LEVEL=xhigh`.
- `.agent-config/`, `agent-config.local.yaml`, and the three generated files (`AGENTS.md`, `CLAUDE.md`, `agents/codex.md`) are gitignored in a consumer; a generated file is never untracked automatically once a repo tracks it; moving one out of the index is an operator decision. `agent-config.yaml` selects packs under the `packs:` key (`rule_packs:` is deprecated). `todo/` holds files a person drops in for an agent; what an agent generates goes to the session scratchpad, not there.

### Configuration Precedence

Rule files, most specific wins:

| Layer | File | Scope |
|---|---|---|
| 1 | `CLAUDE.local.md` / `agents/codex.local.md` | Per-agent, project-local, hand-authored; bootstrap never touches it |
| 2 | `AGENTS.local.md` | Cross-agent, project-local, hand-authored; bootstrap never touches it |
| 3 | `CLAUDE.md` / `agents/codex.md` | Per-agent, generated from `AGENTS.md` by `scripts/generate_agent_configs.py` |
| 4 | `AGENTS.md` | Cross-agent, synced from upstream on every bootstrap |

A hand-authored `CLAUDE.md` or `agents/codex.md` without the `GENERATED FILE` header is preserved with a warning, never overwritten; to adopt the upstream rules, rename it to the layer-1 file. Claude Code loads `CLAUDE.md` and `CLAUDE.local.md` itself; Codex loads only `AGENTS.override.md` or `AGENTS.md` per directory, so the `agents/codex*.md` files apply only where a rule or a person points at them. Claude Code settings follow Claude Code's own order (managed policy, command line, `.claude/settings.local.json`, `.claude/settings.json`, `~/.claude/settings.json`). Effort level: managed policy, then `CLAUDE_CODE_EFFORT_LEVEL`, then the persisted `effortLevel`, then the default.

## Agent Roles

- **Claude Code** is the primary workhorse: drafting, implementation, research, and heavy-lifting tasks.
- **Codex** is the gatekeeper: review, feedback, and quality checks on work produced by Claude Code or the user, reached through `/vet`.
- This division is a default. The user may reverse it, and two scenarios must stay workable: one agent absent (outage, quota, regional block) and roles deliberately reversed to test drift. Core functions (the review loop, structured dispatch, the health check) must work either way, even where an ergonomic helper exists for one agent only.
- A skill, hook, or script that hard-codes one agent's CLI (`codex exec`, `claude -p`) documents or wires the other side's equivalent at the same time, even if that half ships later. A doc that names one agent in its steps names the cross-vendor equivalent once near the top. When the deferred half ships later, the principle is satisfied; do not block the primary half on parity.

## Git Safety and Mechanical Gates

**Never run `git commit` or `git push` without explicit user approval.** Show the proposed command and ask first. This covers every variant: `git commit -m`, `git commit --amend`, `git push`, `git push --force`, `gh pr create` (which pushes), and the rest. Approval in one context does not carry to the next.

`scripts/guard.py`, deployed by bootstrap as a `PreToolUse` hook, enforces these gates before every tool call:

| Gate | Tool scope | Trigger | Action |
|---|---|---|---|
| Writing-style | `Write`, `Edit`, `MultiEdit` on `.md` / `.tex` / `.rst` / `.txt`, excluding an `agent-io` path that resolves under a temp root outside any git worktree | Outgoing content contains a banned AI-tell word (see Writing Defaults) | **deny**, with the hit list and an inline `Suggested rewrite:` line |
| agent-style advisory | Same tools and extensions, excluding an `agent-io` path anywhere | `agent_style` is importable and its mechanical detectors report findings (RULE-05, 06, 12, B, D, I) | **advisory only**: up to 5 findings reported to the model and the user, no permission decision |
| Banner emission | Any tool except `Read`, `Grep`, `Glob`, `Skill`, `Task`, `TodoWrite`, `BashOutput`, `WebFetch`, `WebSearch`, `ToolSearch`, `LS`, `NotebookRead`, and a `Write`/`Edit`/`MultiEdit` whose target is exactly `<project-root>/.agent-config/banner-emitted.json` | `session-event.json.ts` newer than `banner-emitted.json.ts` in the consumer root found by walking up to `.agent-config/bootstrap.{sh,ps1}`; source repos and unrelated directories are not gated | **deny** on the first arm (no acknowledgement file yet) with the instruction to emit the banner and write the acknowledgement; a stale acknowledgement passes through with a `[banner-gate]` advisory |
| Compound `cd` | `Bash` | `cd <path> && <cmd>` or `cd <path>; <cmd>` | **deny** with a `Suggested rewrite:` line (`git -C <path>`, or the path as an argument) |
| Nested `git init` | `Bash`, `PowerShell` | A `git init` whose target is already inside a git worktree | **deny** with a `Suggested rewrite:` line pointing at the session scratch directory |
| Destructive git | `Bash`, `PowerShell` | `git push`, `git commit`, `git merge`, `git rebase`, `git reset --hard`, `git clean`, `git branch -d/-D`, `git checkout --`, `git tag -d`, `git stash drop/clear` | **ask** |
| Destructive or publishing gh | `Bash`, `PowerShell` | `gh pr create/merge/close`, `gh repo delete`, `gh release create/delete/upload/edit` | **ask** |
| Publish | `Bash`, `PowerShell` | `npm publish`, `npm unpublish`, `twine upload`, `python -m twine upload` | **ask** |
| File or device destruction | `Bash`, `PowerShell` | Bash `rm -rf`/`-fr`/`-r -f`, `dd`, `mkfs*`, `shred`; PowerShell `Remove-Item` and its aliases with `-Recurse`/`-r`/`/s` | **ask** |

The four `ask` rows are one classifier that keys on the exact leading token of each sub-command, sees through built-in wrappers (`ssh`, `bash -c`, `docker exec`, `pwsh -Command`, `cmd /c`, `timeout`, `xargs`), and treats `python -c` and private wrappers as opaque. It is not bypassable by any env var: those operations have no agent-side reroute, and human approval is the contract. A gate that has an obvious reroute is a `deny` with the reroute inline, because an unattended loop can take it in one turn where an `ask` would stall.

Escape hatches, set in the `env` block of `~/.claude/settings.json` (disable values `off` / `0` / `disabled` / `false` / `no`):

| Env var | Disables |
|---|---|
| `AGENT_STYLE_HOOK=off` | Writing-style gate and its agent-style advisory |
| `AGENT_COMPOUND_CD_HOOK=off` | Compound-cd gate only |
| `AGENT_NESTED_GIT_INIT_HOOK=off` | Nested `git init` gate only (set it to create a submodule or a deliberate inner repository) |
| `AGENT_CONFIG_GATES=off` | Legacy blanket: writing-style + banner only |

Use the narrowest escape only for a legitimate write that quotes a banned word as an example (a style guide, a CHANGELOG entry), and remove the override after the write. Text an agent carries rather than writes, such as a dispatch prompt or a captured review, belongs under an `agent-io` directory in the session scratchpad, which both writing guards skip; an unmarked path is still scanned. Fan-out (unrequested subagents or Workflow runs) is deliberately a written rule with no gate, because whether the user asked is a judgement about the conversation.

## Shell Command Style

- Avoid `cd <path> && <command>` chains. Use `git -C <path> <subcommand>` for git in another repo, and pass the target path as an argument otherwise (`ls <path>`, `python <path>/script.py`), or use separate tool calls.
- Read-only invocations that need no approval: `git status`, `git diff`, `git log`, `git branch` (no flags), `git show`, `git stash list`, `git remote -v`, `git submodule status`, `git ls-files`, `git tag --list`, filesystem reads (`ls`, `cat`), and benign local operations (`mkdir`).
- Invocations that always need explicit approval: `git commit`, `git push`, `git reset`, `git checkout`, `git rebase`, `git merge`, `git branch -d`, `git remote add/remove`, `git tag <name>`, `git stash drop`.
- `cp` and `mv` are fine for scratch and temporary files. A move or rename of a git-tracked file is reviewed before executing.
- Do not wrap PowerShell inside PowerShell with inline `-Command` when the payload contains `$` variables; run the body directly or write a temporary `.ps1` and invoke it with `-File`. The outer shell expands `$f`, `$_`, and friends first.
- Do not delete a scratch directory before rewriting it (`rm -rf <dir>; cp -r <src> <dir>` prompts on every run). Copy into a fresh name or let the consuming script create the directory.
- Do not pass backslash-heavy or commented content through a shell command. Some command transports collapse doubled backslashes before the shell sees them, even inside a quoted heredoc. In a one-line `python -c`, a `#` comments out the rest of the line. Create the `.py` or `.ps1` with a file-writing tool and run that.
- Wait for a long job in a background command that exits when it ends; no model-turn status checks.

## Communication

The first module governs a reply to a person; the other two govern a file, and differ on purpose.

### Talking to the User

- Reason objectively. Flattery and manufactured opposition are both postures standing in for judgement. Open with neither; acknowledge a sound point when useful, and add only what helps the task. Keep what you verified apart from what you assumed.
- After any required banner, lead with the answer and let length track the question. Cut what carries no information: a compliment, an announcement of what you are about to do in place of doing it, a closing paragraph that repeats the reply. Prefer the ordinary word where the technical one adds nothing.
- Decide routine details. Ask when missing intent, constraints, or approval blocks a sound choice, with one recommendation rather than a menu.
- Say what is running when work will take a while, and report the outcome without being asked.
- Take a correction and apply it, without repeated apology or a re-audit of statements that nothing depends on.
- Reply in the language the user wrote in. Code, paths, commands, quoted tool output, and terms with no settled translation keep their own form inside a sentence that follows the user. Do not drift as the content turns technical.

### Writing Defaults

- Use scientifically accessible language. Do not oversimplify unless asked. Keep meaningful technical detail, factual accuracy, and clarity in scientific contexts.
- In papers and proposals, state each pattern with its key magnitude; keep material scope and uncertainty with the claim, full results in tables, and general limitations in their own section.
- Use consistent terms; define an abbreviation once.
- If citing papers, verify that they exist; when citations are requested, give BibTeX entries ready for a `.bib` file.
- Provide code only when necessary, and confirm it is correct and runs as written.
- Avoid the following words and close variants unless the user explicitly asks for them (a default AI-tell list; trim or extend in your fork): `encompass`, `burgeoning`, `pivotal`, `realm`, `keen`, `adept`, `endeavor`, `uphold`, `imperative`, `profound`, `ponder`, `cultivate`, `hone`, `delve`, `embrace`, `pave`, `embark`, `monumental`, `scrutinize`, `vast`, `versatile`, `paramount`, `foster`, `necessitates`, `provenance`, `multifaceted`, `nuance`, `obliterate`, `articulate`, `acquire`, `underpin`, `underscore`, `harmonize`, `garner`, `undermine`, `gauge`, `facet`, `bolster`, `groundbreaking`, `game-changing`, `reimagine`, `turnkey`, `intricate`, `trailblazing`, `unprecedented`.

### Formatting Defaults

- Preserve the original format when the input is LaTeX, Markdown, or reStructuredText. Do not convert paragraphs into bullet points unless asked.
- Prefer full forms such as `it is` and `he would` over contractions. `e.g.,` and `i.e.,` are fine. Do not use Unicode `U+202F`.
- Do not use em or en dashes as casual sentence punctuation; prefer commas, semicolons, colons, or parentheses. En dashes in numeric ranges (`1–3`, `2020–2025`), paired names, or citations are fine, as is ordinary hyphenation (`command-line`, `co-PI`, `zero-shot`).
- Split long or nested sentences. Vary sentence length and structure; do not open consecutive sentences with the same word, overuse transitions such as "Additionally", or close every paragraph with a summary.
- Do not stage claims as "X, not Y" antithesis for emphasis ("not just X, but Y"; "it is not X, it is Y"). State the claim directly; keep the negation only when the rejected alternative is specific and informs the reader.
- Text meant to be copied into an external destination (an email reply, a chat message, a table cell, a document) goes in a fenced code block. Inside such a block, treat hard line breaks as semantic: one paragraph or one list item is a single unbroken line however long it runs. Do not wrap to a display width, and do not indent continuation lines, because each destination applies its own wrapping and an added newline becomes a permanent break. Keep a blank line between paragraphs and keep the breaks that carry meaning, such as the lines of a postal address or a signature block.
- A draft long enough to be a document (an email, a letter, a passage of prose) goes in a `.md` file instead of the terminal. Give the path and say what the file holds. The same line-break rule applies inside the file. Markdown pasted as plain text shows literal `**` and `-` in Outlook or Gmail. When formatting matters, say so and point the user at a rendered view or an artifact to copy from.

## Skills

- Resolve a skill by name in this order, first hit wins: `skills/<name>/SKILL.md` (project-local), then `.claude/skills/<name>/SKILL.md` (pack-deployed by `anywhere-agents pack install`; the `.claude/` prefix is historical, the contents are agent-agnostic), then `.agent-config/repo/skills/<name>/SKILL.md` (bootstrapped from upstream). Claude Code plugins add a fourth source; prefer the more specific skill.
- A project-local skill is the source of truth for that project. Read its local `references/`, `scripts/`, and `assets/` before any global copy, say briefly that the local copy is in use, and do not modify a global copy that a local skill shadows unless asked.
- `SKILL.md` is the single source for a skill; agent-specific files such as `agents/openai.yaml` are thin wrappers, and there are no agent-specific forks. Edit `SKILL.md` and its `references/` or `scripts/` directly.
- Claude Code reaches a skill through a slash-command pointer at `.claude/commands/<name>.md` that references the `SKILL.md` and carries a one-sentence `description:`; Codex and other agents reach the same file through the lookup order. A new skill gets both the `skills/<name>/SKILL.md` structure and a pointer. A long-named skill may carry an alias pointer whose frontmatter sets `alias-of: <skill-name>` and whose lookup line names the target's three paths (`vet` is the alias for `implement-review`).

## Task Routing

- Before starting a task, read the router skill (`my-router`, resolved through the lookup order) to pick the domain skill; it inspects prompt keywords, file types, and project structure. Do not ask the user which skill to use when the routing table gives a clear match; when several skills could apply, state the detected context and the proposed skill, then ask. If the `superpowers` plugin is active, it runs the outer workflow and the router dispatches inside the execution phase.
- `/vet` (alias of `implement-review`) is the review entry point for staged changes and for plan review.
- Do not fan work out across subagents or a Workflow run on your own initiative; those workers bill the account the session runs on. When parallel work would help, propose it and let the user choose between `prun` (units run on Agy) and a Workflow. Honor a route the user already chose for the current task without asking again. A single helper agent for a bounded lookup is not a fan-out.

## Memory and Persistence

Version-controlled files are the memory that travels across agents, sessions, accounts, and machines: the project README, a `docs/` note, a `PLAN-*.md` file, a `CHANGELOG`, or `AGENTS.local.md`. Use an agent's private memory only for short, agent-local convenience, and never as the sole home of project state, decisions, or records.

## Tool-Use Reliability

Treat a tool's "cannot open / encrypted / unreadable" report on a file as a possible false positive. Before telling the user a file cannot be read, retry once and try an alternate path (a page range, `pdftotext`, render to an image, a different tool), and report failure only after that also fails, naming the paths tried. Apply the same one-retry rule to other transient-looking failures unless the failure is clearly deterministic.

## Environment

- Prefer a Miniforge-managed Python interpreter. Prefer `mamba` for install and create operations and fall back to `conda` only for commands mamba lacks. If the fork or the project names a preferred interpreter in `AGENTS.local.md`, use it first. Do not conclude that Python is unavailable because `python`, `python3`, or `py` fails in `PATH`; those may be shims or store aliases. Inspect Miniforge environments (`%USERPROFILE%\miniforge3\envs\<env>\python.exe`, `$HOME/miniforge3/envs/<env>/bin/python`) and IDE settings before reporting that Python is missing.
- GitHub CLI (`gh`) drives PR and issue work. If it is missing, remind the user to install it (`winget install GitHub.cli`, `brew install gh`, or the distribution package) and run `gh auth login`.
- Claude Code: prefer the native installer, which auto-updates (`claude doctor`, `claude update`). Effort: `CLAUDE_CODE_EFFORT_LEVEL=xhigh` in the `env` block of `~/.claude/settings.json` is the persistent default that bootstrap installs; it outranks `--effort` and `/effort`. Claude reviews dispatched by `/vet` skip user settings and pass `--effort max`.
- Codex: `gpt-6.1-sol` at `service_tier = "standard"` with `[features] fast_mode = false` is the interactive default. `model_reasoning_effort = "xhigh"` is the default and `max` a valid dial-up, while `ultra` also enables automatic task delegation and is chosen only when that is wanted. `approval_policy = "on-request"` for interactive sessions. Set `project_doc_max_bytes = 262144` in `~/.codex/config.toml`: the default injects only the first 32 KiB of a project's `AGENTS.md` and drops the rest without notice. By default a dispatched `/vet` review runs under `--ignore-user-config` and passes the model, the byte budget, and the reasoning floor itself. It takes the model from `config.toml`, so a model switch there reaches reviews without a repository edit. It uses the model named here instead when that file sets none or sets a `model_provider` other than `openai`. `CODEX_DISPATCH_ISOLATE_MCP=off` restores the user config while keeping the explicit byte budget.
- GitHub Actions: keep workflow pins at or above the first Node.js 24 major: `actions/checkout@v5`, `actions/setup-python@v6`, `actions/setup-node@v5`, `actions/upload-artifact@v6`, `actions/download-artifact@v7`. Flag a SHA pin for manual review rather than suggesting a tag, treat a jump to the newest major as a separate manual upgrade, and remind self-hosted runner owners that these actions need a runner that supports Node.js 24.

## Session Start Check

Before the first content of a reply, resolve the consumer root by walking up to `.agent-config/bootstrap.{sh,ps1}`. In Claude Code, when `.agent-config/session-event.json` is newer than `banner-emitted.json` or has no acknowledgement, read `.agent-config/banner.txt`. Accept it only when its leading metadata comment records the pending event's timestamp and the `run_id` of the current `last-run.json`; then skip that comment, print the banner lines after it verbatim as the first lines of the reply with `<model>` replaced by your model id, and copy the event `ts` into `banner-emitted.json`. Missing, unparseable, or mismatched metadata selects this fallback instead, acknowledged the same way:

```
📦 anywhere-agents active
   ├── Agent: <model>
   └── Session check: checks unavailable (run bootstrap or read .agent-config/last-run.json)
```

The `compact` and `resume` sources and the debounce do not re-fire the banner: a continuing conversation has no first reply to print it on. The one resume that finds no usable event and no acknowledgement still writes one, so a repository that became a consumer mid-conversation, or whose event was left unreadable, is gated once. Codex hooks (0.153.3 and later) are not wired here yet, so Codex and other invocation-based agents print the banner on the first reply after running bootstrap, without touching Claude's acknowledgement file: a failed bootstrap attempt selects the fallback regardless of any existing report, and a completed attempt requires the report carrying that attempt's `run_id`. A report for an incomplete refresh says so in its check line. In a source repo, run `scripts/render_banner.py` with the resolved Python interpreter and print its output on the first reply, or the fallback if it cannot run. A dispatched reviewer told to skip the banner keeps skipping it.

## User Profile

Describe this fork's user here or in `AGENTS.local.md` (role, field, common tasks); the maintainer's profile reaches consumers through the `profile` pack.

## Reference

Open these when a task touches the mechanism; they carry the rationale this file does not.

- What bootstrap shares, how settings merge, the consumer repo layout, the `todo/` convention, and which files each agent discovers: https://anywhere-agents.readthedocs.io/en/latest/agents-md/
- Banner fields, the flag-file mechanism, and the fallback: https://anywhere-agents.readthedocs.io/en/latest/session-banner/
- Why each guard gate exists and how it decides: https://anywhere-agents.readthedocs.io/en/latest/guard-hook/
- Codex configuration, effort ladder, service tiers, CLI floors, hooks status: https://anywhere-agents.readthedocs.io/en/latest/codex/
- Installing and updating Claude Code and Codex: https://anywhere-agents.readthedocs.io/en/latest/install/
