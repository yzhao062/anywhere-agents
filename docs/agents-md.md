# AGENTS.md Reference

The shared `AGENTS.md` is the heart of `anywhere-agents`. It is the file every consuming project reads to inherit defaults. The current source is always at [AGENTS.md on GitHub](https://github.com/yzhao062/anywhere-agents/blob/main/AGENTS.md).

This page is the section-by-section tour for motivated readers.

## Top-of-file behavior contract

The file begins with a **source-vs-consumer detection block** that tells agents whether they are in the `anywhere-agents` repo itself (skip bootstrap, proceed to session checks) or in a consuming project (run the bootstrap block idempotently before anything else). This prevents the config repo from trying to bootstrap itself.

## Bootstrap block for project repos

A copyable block that consuming projects paste into their own `AGENTS.md`. It downloads `bootstrap.sh` or `bootstrap.ps1` at session start and runs it, which then syncs all shared content.

Because bootstrap overwrites `AGENTS.md` on every run, project-local overrides go in `AGENTS.local.md`. The override file is never touched by bootstrap.

## Session Start Check

Four checks that run at the start of every session:

1. **OS** — platform detection (`win32`, `darwin`, `linux`) for platform-specific behavior.
2. **Claude Code model and effort** — reports the session model and effort level; flags if below preferred settings.
3. **Codex config** — reads `~/.codex/config.toml` (or `%USERPROFILE%\.codex\config.toml` on Windows) and checks for the recommended keys (`model`, `model_reasoning_effort`, `service_tier`, `[features].fast_mode`).
4. **GitHub Actions versions** — scans `.github/workflows/*.yml` for outdated action pins; reports below-minimum versions to suggest batch-upgrades.

## User Profile

Placeholder for user role, domain, and common task types. Customize in your fork — agents read this to tailor their work to you specifically.

## Agent Roles

- **Claude Code** — primary implementer (drafting, research, heavy lifting).
- **Codex** — gatekeeper (review, feedback, quality checks).

When both are available, default to this division of labor.

## Task Routing

Points at `my-router` for auto-dispatch. The router inspects prompt keywords, file types, and project structure to pick the right skill without asking.

## Codex MCP Integration

Full guide for registering Codex as an MCP server inside Claude Code. Covers:

- One-time user-level registration (`claude mcp add codex -s user -- codex mcp-server -c approval_policy=never`).
- Migration from older registrations.
- Recommended Codex defaults for `config.toml` (`gpt-5.6-sol`, `max` reasoning, `fast` service tier; `ultra` is opt-in for automatic task delegation).
- Windows-specific gotchas (bash-style invocation, PATH issues, Bitdefender false positives).
- Why Windows should prefer the terminal path over MCP until the Windows MCP story smooths out.

## Writing Defaults

- Scientifically accessible but technically precise.
- Verify paper citations; provide BibTeX when citations are requested.
- **~40 banned AI-tell words** (`delve`, `pivotal`, `underscore`, `paramount`, `groundbreaking`, `trailblazing`, etc.). Customize this list in your fork.
- Provide code only when necessary; ensure it runs as written.

## Formatting Defaults

- Preserve LaTeX / Markdown / reStructuredText format. Do not convert prose to bullets.
- Full forms over contractions (`it is`, not `it's`).
- No em-dashes (`—`) or en-dashes (`–`) as casual punctuation. Prefer commas, semicolons, colons, parentheses.
- Normal hyphenation (`command-line`, `co-PI`, `zero-shot`) and numeric ranges (`1–3`, `2020–2025`) are fine.
- Vary sentence length and structure; not every paragraph needs a tidy summary sentence.

## Git Safety

**Never run `git commit` or `git push` without explicit user approval.** Non-negotiable. Applies to all projects that consume this shared config. Covers every variant: `commit -m`, `commit --amend`, `push`, `push --force`, `gh pr create` (which pushes), etc.

## Shell Command Style

- Avoid compound `cd <path> && <cmd>` chains. Use `git -C <path>` or path-arg alternatives so each tool call is a single command.
- Lists of read-only invocations that should not prompt (`git status`, `diff`, `log`, filesystem reads).
- Lists of always-confirm invocations (`commit`, `push`, `reset`, `checkout`, `rebase`, `merge`, and similar).

## GitHub Actions Standards

Table of minimum Node.js 24 versions for common GitHub-maintained actions (`checkout@v5`, `setup-python@v6`, `setup-node@v5`, `upload-artifact@v6`, `download-artifact@v7`). Session Start Check item 4 flags workflows below these minimums so the user can batch-upgrade.

## Environment Notes

- Python discovery beyond `python` / `python3` in `PATH` (Miniforge, pyenv, uv, venv).
- GitHub CLI (`gh`) prerequisite for PR and issue workflows.
- **Claude Code native installer** preference over npm or winget.
- **Effort level**: `max` persists only via the `CLAUDE_CODE_EFFORT_LEVEL=max` env var (the `/effort` slider's `max` value is session-only).

## Local Skills Precedence

Repo-local `skills/<name>/SKILL.md` always wins over pack-deployed `.claude/skills/<name>/SKILL.md` and bootstrapped `.agent-config/repo/skills/<name>/SKILL.md` copies. Do not modify global skills when a local override exists.

## Cross-Tool Skill Sharing

`SKILL.md` is the single source of truth for each skill. Agent-specific config files (`agents/openai.yaml` for Codex, `.claude/commands/<name>.md` for Claude Code) are thin wrappers that must not duplicate or override the logic in `SKILL.md`. Bootstrap copies commands non-destructively.
