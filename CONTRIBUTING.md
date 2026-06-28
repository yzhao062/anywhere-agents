# Contributing

Thank you for your interest. `anywhere-agents` is a curated, maintained configuration — it is not a framework with a broad mandate. Contribution scope is narrower than a typical open-source project. Please read this before opening an issue or PR.

## What lands upstream

- **Bug reports** for the bootstrap scripts, guard hook, skills, or tests that break on a supported platform (Linux, macOS, Windows 11)
- **Documentation fixes** (typos, broken links, unclear explanations)
- **PRs that fix clear bugs** in the shipped skills or infrastructure
- **Compatibility fixes** for updates to Claude Code, Codex, or GitHub Actions that break existing behavior

## What does not land upstream

- **Feature requests that do not match the maintainer's workflow** — fork and implement in your own copy
- **New skills** — the shipped skill set (`implement-review`, `my-router`, `ci-mockup-figure`, `readme-polish`, `prun`) is deliberately curated. Add your own skills to your fork; the router is designed to be extended.
- **Style preferences that conflict with existing `AGENTS.md` defaults** — the defaults are the product. If you disagree, fork.
- **Multi-agent expansion** — support for Cursor, Aider, Gemini CLI, etc. is out of scope for v1.x. Fork if you need it.
- **Packaging work** (CLI, YAML manifest, web UI) — out of scope by design; see README.

## How to fork and customize

This is the primary way to use `anywhere-agents`:

1. Fork `yzhao062/anywhere-agents` to your GitHub account
2. Edit:
   - `AGENTS.md` — user profile, writing defaults, environment conventions
   - `user/settings.json` — user-level permissions, env vars
   - `.claude/settings.json` — project-level permissions
   - `skills/` — add your own skill directories
   - `skills/my-router/references/routing-table.md` — register your skills with the router
   - `bootstrap/bootstrap.sh` and `bootstrap/bootstrap.ps1` — update the repo URL if different from your fork
3. Point your consumer repos at your fork (change the URL in the bootstrap block)
4. Pull upstream updates periodically: `git remote add upstream https://github.com/yzhao062/anywhere-agents.git && git pull upstream main`, resolve conflicts normally

## Filing a bug

Open an issue with:

1. **Platform** (OS version, Claude Code or Codex version, shell)
2. **Reproduction steps** (exact commands, starting state)
3. **Expected behavior** (what should have happened)
4. **Actual behavior** (what did happen, including full error output)
5. **Workaround** (if any)

## Submitting a PR

1. Fork, branch from `main`
2. Keep changes minimal and focused on a single concern
3. Run existing tests: `python -B -m unittest discover -s tests -p "test_*.py" -v`
4. Add tests for behavior changes
5. Open a PR describing the bug or clear improvement

PRs that introduce features out of scope (see above) will be closed with a pointer to fork.

## Pre-push hook (recommended for maintainers)

A pre-push hook in `.githooks/pre-push` runs `scripts/pre-push-smoke.sh` when a push includes changes to agent-critical files (`AGENTS.md`, `bootstrap/`, `scripts/`, `skills/`). `pre-push-smoke.sh` validates the **current checkout** (not the published package): it regenerates the per-agent files and diffs against the committed versions, then runs `claude -p` / `codex exec` in the repo root to confirm each agent actually sees the shipped skill roster.

Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

Pure doc / test / CI-workflow changes skip the smoke automatically and push fast. Use `git push --no-verify` for emergency bypass; the smoke must then pass via the equivalent pre-release checks in `RELEASING.md` or the CI `real-agent-smoke.yml` workflow.

Prerequisites on the pushing machine: `bash`, `python` (for the generator-determinism diff), and optionally `claude` / `codex` on `PATH` for the agent-roster checks. Agent calls are skipped gracefully if the corresponding CLI is missing — useful for maintainers who only have one agent configured on a given machine. (The separate `scripts/remote-smoke.sh` has different prerequisites — `pipx` / `npx` / `curl` — because it tests the published install path, not the current checkout.)

## Security issues

For security-sensitive issues (hook escape, unsafe command execution), please email the maintainer directly rather than opening a public issue. Contact via the email on [github.com/yzhao062](https://github.com/yzhao062).

## Code of conduct

Be direct, be kind, do not waste each other's time.
