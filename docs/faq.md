# FAQ

??? question "Why does `anywhere-agents` apply drift automatically now?"
    v0.6.0 inline-apply default. The canonical bare-command path applies prompt-policy drift on mutable refs and prints a stderr summary line per affected pack of the form `applied 1 update for <pack> @ <ref>: <old> -> <new>`. The previous v0.5.x message (`ℹ N updates available — run pack verify --fix`) was misleading: `pack verify --fix` did not actually apply prompt-policy drift, only `pack update` did. v0.6.0 collapses the split: the canonical bare command does the apply.

    Per-run skip is available via `ANYWHERE_AGENTS_UPDATE=skip` env var (v0.5.0 contract preserved) or the new `--no-apply-drift` CLI flag (the flag wins when both are set). Durable fail-closed: pin `update_policy: locked` in `agent-config.yaml` for any pack where you want the run to fail rather than apply.

??? question "I have CI scripts using `pack verify --fix` or `pack update` — do they break?"
    No. Both commands are compatibility aliases that continue to work through all v0.x. Each alias prints a one-line stderr notice pointing at `anywhere-agents`, then executes the canonical apply path. The stdout state is byte-identical with the canonical command, so CI scripts that pipe stdout are unaffected.

    Removal is allowed only at v1.0 with explicit CI-migration guidance. The `pack update <name>` selective-apply form survives as a power-user verb that applies drift only for the named pack and prints the stderr summary for that pack alone.

??? question "How do I prevent automatic drift apply?"
    Three options, ordered from per-run to durable:

    1. **Per-run env var (v0.5.0 contract):** `ANYWHERE_AGENTS_UPDATE=skip anywhere-agents` reports drift but skips application. The pack-lock and deployed files are unchanged for the run.
    2. **Per-run CLI flag (new in v0.6.0):** `anywhere-agents --no-apply-drift` has the same effect. The flag wins when both the env var and the flag are set.
    3. **Durable per-pack pin:** `update_policy: locked` in `agent-config.yaml`. The composer fails closed on any drift for that pack with a clear delta. This is the right choice for content that must never auto-refresh.

??? question "My pack manifest fails to parse with an `update_policy: auto` error — what changed?"
    v0.6.0 restores a parse-time rejection that v0.5.0 silently dropped. `update_policy: auto` is no longer accepted on active entries (kind: `hook` | `skill` | `permission` | `command`). The trust-model paragraph at `pack-architecture.md` line 208 has always stated that active entries cannot use `auto`; the v0.6.0 release re-aligns the parser with the documented contract.

    Rewrite to `update_policy: prompt` for apply-by-default behavior (the run applies the change inline and prints a stderr summary), or `update_policy: locked` for fail-closed (the run fails with a clear delta on any drift). Passive entries with `update_policy: auto` are still accepted; the boundary is active-only.

    The error message names the pack, the active entry's `files[].to` path, the offending policy literal, and the required rewrite. Maintainer-project scan ahead of the v0.6.0 release found zero hits, so no real consumer is expected to be caught by the rejection.

??? question "Does this work with [agent X]?"
    Primary support is **Claude Code + Codex**. The `AGENTS.md` convention is standardized enough that other agents (Cursor, Aider, Gemini CLI) may read it and pick up writing defaults. Skill routing and guard hooks are tuned for Claude Code specifically. Forks can extend support to other agents.

??? question "What is the difference between `AGENTS.md` and `AGENTS.local.md`?"
    `AGENTS.md` is the shared config synced from upstream. Bootstrap overwrites it on every run — never edit it in a consuming project, or your changes will be lost on the next session.

    `AGENTS.local.md` is your per-project override. Bootstrap never touches it. Use it for project-specific permissions, domain glossaries, or opt-outs from shared defaults.

??? question "How do I disable the guard hook?"
    Three scopes (v0.7.0):

    | Env var (set in `~/.claude/settings.json` under `"env"`) | Disables |
    |---|---|
    | `AGENT_STYLE_HOOK=off` | Writing-style guard only (banned AI-tell words on `.md` / `.tex` / `.rst` / `.txt` writes) |
    | `AGENT_COMPOUND_CD_HOOK=off` | Compound-cd guard only (`cd <path> && <cmd>` Bash chains) |
    | `AGENT_CONFIG_GATES=off` | Legacy blanket: writing-style + session banner |

    Per-guard envs are useful in meta-discussion contexts (a style guide that quotes banned words, a CHANGELOG citing one as an example) without turning off the whole suite.

    **Destructive git / gh approval is NOT bypassable by any env.** No env var disables the `ask` prompt on `git commit`, `git push`, `git reset --hard`, `git merge`, `git rebase`, `gh pr merge`, `gh repo delete`, etc. Human approval is the contract; these guards have no agent-side reroute.

    Full removal: in a fork, remove the user-level section of `bootstrap/bootstrap.sh` / `bootstrap.ps1` that deploys `scripts/guard.py`. In a specific project only, remove the `hooks` entry from `~/.claude/settings.json` manually (bootstrap re-installs it on the next run from upstream).

??? question "Why does the deny message say `Suggested rewrite:`?"
    v0.7.0 (Round 6 noise audit). When a guard denies a write (banned AI-tell word, compound-cd chain), it embeds a concrete `Suggested rewrite:` line inline so the agent can lift the reroute in one model turn instead of inferring it. Example:

    ```text
    Writing-style: banned AI-tell words detected in /tmp/notes.md: pivotal.
    Suggested rewrite: `pivotal` -> key, central.
    ```

    ```text
    Compound cd command blocked. Suggested rewrite: `git -C papers/foo status`.
    ```

    This is faster than `ask`-style prompts for autonomous flows (`/implement-review auto`, headless `claude -p` runs): the agent sees a hard NO plus a concrete alternative, course-corrects, and continues. Destructive operations (git commit / push / reset / merge, gh pr merge / repo delete) still surface as `ask` because they have no automatic reroute.

??? question "Why does `git push` always ask for confirmation?"
    The `Git Safety` section of `AGENTS.md` says _"Never run `git commit` or `git push` without explicit user approval."_ This is a deliberate opinion. The guard hook enforces it: even if Claude Code has permissions set to auto-approve `git push`, the hook intercepts and requires explicit confirmation. No escape env disables this guard (v0.7.0 Round 6; destructive operations have no agent-side reroute).

    To disable, remove the Git Safety section from your fork's `AGENTS.md` and the corresponding rules from `scripts/guard.py`.

??? question "Why is `anywhere-agents` on both PyPI and npm?"
    Agent-native installs. Users can tell their agent _"install anywhere-agents in this project"_ and the agent picks whichever command matches the environment (`pipx run`, `npx`, or raw shell). The PyPI and npm packages are thin shims — both download the same shell bootstrap and run it. There is no Python / Node.js logic in the install path itself.

??? question "Can I use this without the shell bootstrap?"
    Yes — manually copy `AGENTS.md`, `skills/`, `scripts/guard.py`, and the `.claude/` / `user/` settings into your project. The bootstrap is a convenience wrapper, not a requirement.

??? question "How do I update across many projects at once?"
    Bootstrap runs on every session and pulls from upstream, so every consuming project updates automatically on its next session. No manual per-project maintenance.

    To force a refresh mid-session in one project, run `bash .agent-config/bootstrap.sh` (or `& .\.agent-config\bootstrap.ps1` on Windows).

??? question "How do I debug a skill that is not dispatching?"
    Check `my-router`'s lookup order:

    1. `skills/<name>/SKILL.md` in the project (project-local override).
    2. `.agent-config/repo/skills/<name>/SKILL.md` (bootstrapped copy).
    3. Installed agent-platform plugins (e.g., Claude Code plugin skills).

    If the skill exists but is not dispatching, verify the routing rules in `skills/my-router/references/routing-table.md`. The router prefers keyword matches over file-type matches; a too-generic keyword can accidentally match the wrong skill.

??? question "Is this maintained?"
    Yes — it is the author's daily-driver config. Changes land when the author needs them. Bug fixes and documentation improvements are accepted via PR. Feature requests that do not match the author's work should land in a fork.

??? question "What does the version number mean?"
    `anywhere-agents` uses [Semantic Versioning](https://semver.org). Repo tags, PyPI, and npm all share one version stream — a tag like `v0.1.2` reproduces exactly what is on the package registries.

    - **Major (`0.x.y → 1.0.0`)**: the user-facing install flow or config contract changes.
    - **Minor (`0.1.x → 0.2.0`)**: new shipped skills or user-visible features.
    - **Patch (`0.1.0 → 0.1.1`)**: documentation, packaging, or hygiene changes that do not change behavior.

    While in 0.x, "minor" is used loosely per SemVer's 0.x convention.

??? question "Bootstrap exits with `git is not installed` or `git X.Y is too old`. What changed?"
    v0.7.0 added a git preflight before the sparse-clone path. Bootstrap needs `git >= 2.25` because the clone uses `git clone --filter=blob:none --sparse`; `--sparse` is the Git 2.25 floor (2020-01-13), while `--filter=blob:none` is the older partial-clone option (Git 2.19+). The check runs before any `git` invocation in both the fresh-clone and existing-repo refresh paths.

    On failure, bootstrap exits non-zero with a platform-specific install line:
    - macOS: `brew install git`
    - Debian / Ubuntu: `sudo apt update && sudo apt install -y git`
    - Windows: `https://git-scm.com/download/win`

    Unparseable `git --version` strings (alpha builds, distro suffixes like `2.30.1.windows.1` or `(Apple Git-141)`) default-pass with a stderr warning so unusual systems with modern git are not blocked. If you have a modern git that the parser cannot read and the preflight still refuses to run, set `AGENT_CONFIG_SKIP_GIT_PREFLIGHT=1` as a one-shot escape hatch and file an issue with the version string the parser missed.

??? question "Where can I report bugs or propose changes?"
    - Bugs and clear fixes → [GitHub Issues](https://github.com/yzhao062/anywhere-agents/issues) or PR.
    - Feature requests that do not match the author's workflow → fork and maintain your own version; pull upstream fixes as they land.
    - Documentation improvements → always welcome via PR.

    See [CONTRIBUTING.md](https://github.com/yzhao062/anywhere-agents/blob/main/CONTRIBUTING.md).
