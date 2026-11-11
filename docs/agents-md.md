# AGENTS.md Reference

The shared `AGENTS.md` is the file every consuming project reads to inherit its defaults. The current source is always [AGENTS.md on GitHub](https://github.com/yzhao062/anywhere-agents/blob/main/AGENTS.md). Since the 2026-09 rewrite it is one file of about 24 KB, byte-identical in `anywhere-agents` and in the maintainer's `agent-config` source repo, and it holds rules only. This page is the section-by-section reference, and it holds the mechanism and the rationale the file itself leaves out.

## Editing Contract

A rule in the file says what to do, plus one clause on why when the rule is counter-intuitive. Longer rationale, measurements, version history, and how-to material go to this docs site or to `CHANGELOG.md`, never to the file. `tests/test_bootstrap_size.py` enforces the size in both source repos. The rewrite was held to 24,576 bytes. The routine ceiling per file is the measured size plus ten percent, rounded up to the next 512 bytes: 26,624 bytes for `AGENTS.md` and 27,136 for each generated file. A larger budget is a recorded decision with a comment in that test. `tests/test_composed_consumer_size.py` composes the file with fixed copies of the passive packs the maintainer's consumers load. It holds the result under 61,440 bytes.

The file carries no agent-tagged block. The generator still supports `<!-- agent:claude -->` and `<!-- agent:codex -->` blocks, which reach only that agent's generated file, and `tests/test_generator.py` exercises them on a synthetic fixture. Nothing in the shared file is agent-specific enough to tag, and a new tagged block needs its reason recorded here.

Why the diet: the file had grown by patching to 74.6 KB in one repo and 66.0 KB in the other, with 81 lines of drift between them. Codex limits the combined discovered project instructions to `project_doc_max_bytes`, 32 KiB by default. Reviews using that default with the pre-rewrite baseline lost Writing Defaults, Git Safety, and every appended pack. Most of the loaded bytes were rationale no session acted on. See [Codex](codex.md) for the budget.

## Section by Section

**Preamble.** A source-versus-consumer test. When `bootstrap/bootstrap.sh`, `bootstrap/bootstrap.ps1`, `scripts/generate_agent_configs.py`, and `skills/` all exist at the repo root, the agent is in a source repo and skips bootstrap. Otherwise it must run the bootstrap block before anything else, every session, and report the result in one line. The markers hold in both source repos, so the same file serves both.

**Bootstrap.** The copyable block that consuming projects paste into their own `AGENTS.md`, and the six rules that follow from a run. The root `AGENTS.md` is rewritten and composed every run, and a composed file is preserved when packs are configured but composition cannot run. Overrides go in `AGENTS.local.md`, which Codex does not discover on its own. Command pointers are copied non-destructively, shared settings keys are merged, and user-level files are refreshed. `.agent-config/`, `agent-config.local.yaml`, and the three generated files are gitignored; `packs:` is the manifest key and `todo/` the drop box. The mechanism behind each rule is in the sections below.

**Configuration Precedence.** The four-layer table: per-agent local file, `AGENTS.local.md`, the generated per-agent file, the shared file. A hand-authored `CLAUDE.md` or `agents/codex.md` without the `GENERATED FILE` header is preserved. The section also says which files each agent loads on its own, and how settings and effort take precedence.

**Agent Roles.** Claude Code implements, Codex reviews through `/vet`, and the division is a default that must stay workable with one agent absent or the roles reversed. A skill or script that hard-codes one agent's CLI documents or wires the other side's equivalent.

**Git Safety and Mechanical Gates.** The commit-and-push approval rule, the gate table for `guard.py`, the one-classifier note, and the escape hatches. Why each gate has the shape it has is in [Guard hook](guard-hook.md).

**Shell Command Style, Writing Defaults, Formatting Defaults.** The seven shell rules: no compound `cd`, the read-only and always-confirm lists, scratch moves, PowerShell nesting, scratch directories, inline Python. The banned-word list is a default AI-tell list to trim or extend in a fork. The formatting rules include the copy-paste block rule and the rule that a document-length draft goes in a file.

**Skills, Task Routing, Memory and Persistence, Tool-Use Reliability.** The three-path skill lookup order, the single-source rule for `SKILL.md`, and the slash-command pointers and aliases. Then the router and the no-fan-out rule, version-controlled files as the memory that travels, and the one-retry rule before reporting a file unreadable.

**Environment.** Python discovery and `gh`. The Claude Code native installer and effort persistence ([Install](install.md)). The Codex policy lines ([Codex](codex.md)). The GitHub Actions minimums the session check compares pins against.

**Session Start Check.** The rule that prints the banner from a rendered report, the fallback, and the Codex branch. The renderer, the report file, and the acceptance rule are in [Session banner](session-banner.md).

**User Profile.** A placeholder that tells a fork to describe its user; the maintainer's own profile reaches consumers through the `profile` pack of [agent-pack](https://github.com/yzhao062/agent-pack).

**Reference.** Links to the five pages on this site that carry the rationale.

## What Gets Shared

| Content | Source | How fetched |
|---|---|---|
| The rules: bootstrap, precedence, roles, gates, shell, writing, formatting, skills, routing, environment, session check | `AGENTS.md` | `curl` or `Invoke-WebRequest` of the raw file |
| Per-agent rule files (`CLAUDE.md`, `agents/codex.md`) | Generated from `AGENTS.md` by `scripts/generate_agent_configs.py` | Regenerated locally on every bootstrap; a hand-authored file is preserved with a warning |
| Shared skills (`agent-models`, `ci-mockup-figure`, `editable-figure`, `implement-review`, `my-router`, `prun`, `readme-polish`) | `skills/` | Sparse `git clone` into `.agent-config/repo/` |
| The model registry (`user/agent-models.json`), its resolver (`scripts/agent_models.py`), and the `agent-models` skill | `user/`, `scripts/`, and `skills/agent-models/` | Sparse `git clone` into `.agent-config/repo/`. The skill also deploys through the `aa-core-skills` pack, like the other shared skills, while the resolver and the registry stay in the clone. Bootstrap runs the resolver's `sync` (opt out with `AGENT_MODELS_SYNC=off`) to write the interactive Codex and Agy values into their own configs, and dispatchers resolve review and worker values from the registry at run time |
| Slash-command pointers for the shared skills, plus the `vet` alias | `.claude/commands/` | Sparse clone, then a non-destructive copy into the project's `.claude/commands/` |
| Project defaults (`permissions`, `attribution`, and the like) | `.claude/settings.json` | Sparse clone, then a key-level merge into the project's `.claude/settings.json` on every run |
| User-level hooks and scripts (`guard.py`, `session_bootstrap.py`, `statusline.py`, `agent-quota.py`) and settings | `scripts/` and `user/settings.json` | Hooks copied to `~/.claude/hooks/`, the statusline scripts to `~/.claude/`; settings merged into `~/.claude/settings.json` (shared permissions, the PreToolUse guard, the SessionStart hook, the statusLine command, and the model and effort keys the registry renders) |
| The session banner renderer and its helper (`render_banner.py`, `pack_identity.py`) | `scripts/` | Run from the sparse clone by both bootstrap entry points and the SessionStart hook; the wheel carries a vendored copy beside the composer for the `anywhere-agents` command |
| Passive packs (`agent-style` by default) | The pack's own repo | Fetched by the composer and appended to the root `AGENTS.md` between begin and end markers |

The skill roster above is what `scripts/pre-push-smoke.sh`, `scripts/remote-smoke.sh`, and `real-agent-smoke.yml` ask an agent to list at release time. The shared file names the lookup paths rather than the roster, so the probe points the agent at the tree: `skills/*/SKILL.md` in a source checkout, `.claude/skills/*/SKILL.md` in a bootstrapped project. A second probe, run with every tool disabled, asks which skill `/vet` aliases; only the rules file says `implement-review`, so that answer proves the file itself was loaded.

## Consumer Repo Layout

Bootstrap maintains this layout on every run, so a new project inherits it on its first session rather than by copying files from an older one.

| Path | State | Who writes it |
|---|---|---|
| `AGENTS.md`, `CLAUDE.md`, `agents/codex.md` | untracked, gitignored | regenerated by bootstrap every run |
| `AGENTS.local.md`, `CLAUDE.local.md`, `agents/codex.local.md` | tracked | hand-authored; bootstrap never touches them |
| `agent-config.yaml` | tracked | the project's pack selection |
| `agent-config.local.yaml` | untracked, gitignored | machine-local override |
| `.agent-config/` | untracked, gitignored | the fetched upstream copy, the ledger, the banner report |
| `todo/README.md` | tracked | seeded by bootstrap when absent |
| `todo/` contents | untracked, gitignored | whatever a person drops in |

**The three generated files are not tracked.** Their bytes depend on which packs the machine resolved and on whether composition ran. Two machines that are both current therefore produce different content, and each sees the other's as a diff to commit. Committing them also trains a reader to skim diffs in exactly the files where a degraded run shows up. A repo that already tracks one is left alone, because `.gitignore` does not untrack a path git already follows. Moving it out of the index is an operator decision, since the resulting commit removes the file for every other clone. Set `AGENT_CONFIG_TRACK_GENERATED` before bootstrap to keep all three out of `.gitignore`.

**`agent-config.yaml` uses the `packs:` key.** `rule_packs:` is a deprecated alias whose warning reads "accepted through v0.6.x", and the composer hard-fails on it at v1.0.0. The two are equivalent until then, and `packs:` wins when a file carries both.

## The `todo/` Drop Box

`todo/` is where a person hands a file to an agent. They copy something in, point an agent at it with `@todo/<name>`, and the agent reads it, moves it to where it belongs in the repo, or deletes it. The resting state is empty. Its own `README.md` carries the full convention. Bootstrap creates the folder and seeds that README when it is missing. It never rewrites one that is already there, so a repo whose filing rules are specific to its own work can say so in place. Set `AGENT_CONFIG_NO_TODO_DROPBOX=1` before bootstrap to suppress the folder and its gitignore entries. What an agent generates on its own belongs in the session scratchpad, not here.

## Settings Merge, User-Level Files, and the Ledger

The project merge takes the shared keys of `.agent-config/repo/.claude/settings.json` (`permissions`, `attribution`, `effortLevel`, and the like) into the project's `.claude/settings.json`; shared keys are updated on every run and project-only keys are preserved. The Bash entry point runs `scripts/merge_settings.py`, so it needs Python and leaves the file untouched without it; the PowerShell entry point falls back to an in-script merge. Since v0.8.0 the merge publishes and takes its backup only when the canonical bytes differ. A session-start refresh that changes nothing leaves the file and its backup history alone. Override a shared key locally in `.claude/settings.local.json`.

The user-level step copies `scripts/guard.py` and `scripts/session_bootstrap.py` to `~/.claude/hooks/`, the statusline renderer and the quota readout to `~/.claude/`, and merges `user/settings.json` into `~/.claude/settings.json`: shared permissions, the hook wiring, the statusLine command, and the model and effort keys (`model`, `effortLevel`, and the `env` entries `CLAUDE_CODE_EFFORT_LEVEL` and `CLAUDE_CODE_SUBAGENT_MODEL`), whose values `scripts/agent_models.py render` takes from `user/agent-models.json`. The same bootstrap step then runs `agent_models.py sync`, which writes the interactive Codex and Agy values into `~/.codex/config.toml` and the Antigravity settings (set `AGENT_MODELS_SYNC=off` to skip it). A fork that does not want user-level changes removes that section from the bootstrap script.

Every run writes `.agent-config/last-run.json`, a machine-readable record of the phases the bootstrap completed and the files each one wrote. `run_id` identifies the run. `completed: false` means the refresh did not complete every required step. Either the script stopped early, or it reached finalization after a degraded step. A composition that could not run and left the previous composed file in place is the common degraded case. `last_phase` names the latest recorded phase. Each entry in `steps` carries a phase, a status (`ok`, `skipped`, or `failed`), and a reason or return code, which is where the failure or skip is explained. The ledger covers the bootstrap script only. The wheel-side `pack verify` heal pass that the `anywhere-agents` command runs afterwards is recorded in `.agent-config/pack-lock.json`. The session banner reads the ledger to name a failed phase or a skipped step in its check line; see [Session banner](session-banner.md).

## Which Files Each Agent Discovers

Claude Code loads `CLAUDE.md` and `CLAUDE.local.md` itself, so the generated `CLAUDE.md` is what a Claude session sees. Codex loads `AGENTS.override.md` or `AGENTS.md` per directory, from the repo root down to the working directory, and nothing else. It never reads `AGENTS.local.md` or `agents/codex.md` on its own. The rule in the shared file tells an agent to read `AGENTS.local.md` after `AGENTS.md`, and the `agents/codex*.md` files apply only where a rule or a person points at them. Other agents follow their own discovery; the bootstrap block and the lookup-order rules are written so that any agent that reads `AGENTS.md` can follow them.

## Skill Pointers and Aliases

Claude Code reaches a skill through a slash-command pointer at `.claude/commands/<name>.md`. Each pointer names the skill's three lookup paths in order and carries a one-sentence `description:` in its frontmatter. Claude Code lists every command in its system prompt by that description; without one it shows the first body line, which says nothing about what the skill does. The committed pointers carry theirs by hand. The composer's `kind: skill` handler derives one for a generated pointer from the first sentence of the skill's own `SKILL.md` frontmatter. The value is quoted, so a colon or a hash inside it cannot change the block's meaning. `tests/test_pointer_files.py` requires every committed pointer to carry a description of one sentence and at most 160 characters.

A skill whose canonical name is long may carry a short alias pointer. That is a second `.claude/commands/<alias>.md` whose frontmatter sets `alias-of: <skill-name>` and whose lookup line names the target's three paths rather than its own. `vet` is the alias for `implement-review`. Renaming the skill instead is right only when nothing depends on its name. `implement-review` is the counter-example. The name is also the dispatch state-directory prefix that `auto-watch` globs for and the stem of the `IMPLEMENT_REVIEW_*` environment variables. Nothing prunes a skill directory that vanishes upstream, so the old name would linger in every consumer. The same test enforces the rules: the target must exist, and a pointer without the key must match its own filename.
