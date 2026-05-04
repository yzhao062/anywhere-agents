# Install

Every install path does the same thing: install `anywhere-agents` and run the bare command in the current directory. As of v0.6.0, bare `anywhere-agents` is the canonical apply command — one verb that bootstraps the project, deploys declared state, applies prompt-policy drift on mutable refs, and regenerates `CLAUDE.md` / `agents/codex.md`. The command is idempotent — safe to run every session even if `.agent-config/` already exists.

```mermaid
flowchart LR
    A[You run<br/>install command] --> B[Download<br/>bootstrap.sh / .ps1]
    B --> C[Fetch upstream<br/>AGENTS.md]
    C --> D[Sparse-clone<br/>skills/ .claude/ scripts/ user/]
    D --> E[Copy skill pointers<br/>to .claude/commands/]
    E --> F[Merge shared<br/>settings into project]
    F --> G[Install guard.py<br/>to ~/.claude/hooks/]
    G --> H[Merge user settings<br/>into ~/.claude/]
    H --> I[Add .agent-config/<br/>to .gitignore]
    I --> J[Done — agent<br/>reads AGENTS.md]

    classDef step fill:#fff,stroke:#8b2635,stroke-width:1.5px,color:#8b2635;
    class A,B,C,D,E,F,G,H,I,J step;
```

## PyPI

Zero-install with pipx:

```bash
pipx run anywhere-agents
```

`pipx` handles the isolated environment; re-runs always fetch the latest version. Install `pipx` itself via [pipx.pypa.io](https://pipx.pypa.io/).

Two-step alternative:

```bash
pip install anywhere-agents
anywhere-agents
```

## npm

Zero-install with npx:

```bash
npx anywhere-agents
```

Requires Node 14+.

Global install alternative:

```bash
npm install -g anywhere-agents
anywhere-agents
```

## Raw shell

No package manager required. These are the commands the PyPI and npm packages delegate to internally.

### macOS / Linux

```bash
mkdir -p .agent-config
curl -sfL https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/bootstrap/bootstrap.sh -o .agent-config/bootstrap.sh
bash .agent-config/bootstrap.sh
```

### Windows (PowerShell)

```powershell
New-Item -ItemType Directory -Force -Path .agent-config | Out-Null
Invoke-WebRequest -UseBasicParsing -Uri https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/bootstrap/bootstrap.ps1 -OutFile .agent-config/bootstrap.ps1
& .\.agent-config\bootstrap.ps1
```

## What the bootstrap does

1. Fetches the latest `AGENTS.md` from upstream and copies it into the project root (also `.agent-config/AGENTS.md` as the cached source).
2. Sparse-clones `skills/`, `.claude/commands/`, `.claude/settings.json`, `scripts/guard.py`, and `user/settings.json` into `.agent-config/repo/`.
3. Copies shared `.claude/commands/*.md` into the project's `.claude/commands/`. Non-destructive — does not delete unrelated local pointer files.
4. Merges shared `.claude/settings.json` keys into the project's copy. Project-only keys are preserved.
5. Installs `scripts/guard.py` into `~/.claude/hooks/` and merges `user/settings.json` into `~/.claude/settings.json` (hook wiring, `CLAUDE_CODE_EFFORT_LEVEL=max`, user-level permissions).
6. Appends `.agent-config/` to the project's `.gitignore` if not already present.

## Pack manifest schema (v0.6.0)

Pack manifests declare passive entries (raw text injected into `AGENTS.md`) and active entries (skill files, hooks, permission rules, command pointers). v0.6.0 restores a parse-time check on the `update_policy:` field of active entries.

`update_policy:` accepts three values:

- `auto` — silent refresh on resolved-commit change. Allowed only on **passive** entries, where the wheel can pin the bundled ref and the content is plain text.
- `prompt` — apply by default with a stderr summary line on resolved-commit change. Allowed on both passive and active entries; this is the safe default for active code from third-party packs.
- `locked` — fail-closed on any drift. Allowed on both passive and active entries.

Active entries with `update_policy: auto` are rejected at parse with an error like:

```
pack 'foo': active entry at files[].to '.claude/skills/foo/' uses 'update_policy: auto'; rewrite to 'prompt' for default-apply behavior or 'locked' for fail-closed
```

The error names the offending pack, the `files[].to` path of the active entry, the policy literal, and the required rewrite. v0.5.0 silently dropped this check; v0.6.0 restores it. The trust-model rationale (silent install of arbitrary code from a mutable ref is the supply-chain risk `prompt` was designed to gate) has stood since the v0.4.0 manifest contract; see `pack-architecture.md` line 208.

## Same-ref source-path migration

Consumers pinned to `agent-style v0.3.2` whose `.agent-config/pack-lock.json` records `source_path: docs/rule-pack.md` will see the lock auto-migrate to `source_path: docs/rule-pack-compact.md` on the next bare `anywhere-agents` run. The composer detects the path mismatch against the lock for the same `requested_ref`, routes through the drift-and-migrate flow, and rewrites the deployed `AGENTS.md` body to the compact source. This honors the v0.5.7 § Compatibility commitment that consumers requiring same-ref source-path switching should stay on aa v0.5.6 until v0.6.0.

The migration prints a stderr summary line of the form:

```
migrated 1 path for agent-style @ v0.3.2: docs/rule-pack.md -> docs/rule-pack-compact.md
```

Consumers who want to keep the old full-body source must set an explicit override in `agent-config.yaml` (a `passive.files[].from: docs/rule-pack.md` block, or `update_policy: locked`); the BC-guard refinement in v0.6.0 preserves any entry with positive shape signals (`passive` / `active` keys, or `ref` / `update_policy` deviating from the bundled default).

## Prerequisites

- **`git`** — required. Windows users: Git for Windows provides `bash`, which both bootstrap paths benefit from.
- **Python 3.x** — required for the settings merge step (stdlib only, any recent version). If unavailable, bootstrap continues without merge.
- **Claude Code** or **Codex** — the agents that consume this config. See their respective docs for install instructions.
- **`pipx`** or **`npx`** — required only for the package-manager install paths, not for raw shell.

## Updating

Every new session runs bootstrap automatically and picks up upstream changes. To force a mid-session refresh:

```bash
# macOS / Linux
bash .agent-config/bootstrap.sh

# Windows (PowerShell)
& .\.agent-config\bootstrap.ps1
```

## Uninstalling

Bootstrap is idempotent and non-destructive — there is no system-wide install state beyond what `pipx` / `npm` put in their own prefixes. To remove:

1. Delete `.agent-config/` in the project root.
2. Remove `.agent-config/` from the project's `.gitignore` if desired.
3. Revert `.claude/settings.json` if desired.
4. Optionally remove `~/.claude/hooks/guard.py` and the user-level settings that were merged in from `user/settings.json`.

## Troubleshooting

!!! note "Python discovery fails on Windows"
    `python` in PATH may resolve to the Microsoft Store shim, not a real interpreter. Try `py -3` or install a real Python (Miniforge / python.org / pyenv-win). Bootstrap will also continue without Python, skipping only the settings merge step.

!!! note "Permission denied on `curl -sfL` (macOS / Linux)"
    The `-sfL` flags cause `curl` to fail silently on HTTP errors. If the URL redirects, check your internet connection and try again with `-v` to see the actual error.

!!! note "PowerShell execution policy blocks `.ps1`"
    Run once in the current session only:
    ```powershell
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
    ```
    Or run `bootstrap.ps1` with an explicit bypass:
    ```powershell
    powershell -NoProfile -ExecutionPolicy Bypass -File .\.agent-config\bootstrap.ps1
    ```
