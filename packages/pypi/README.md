# anywhere-agents

Install the [**anywhere-agents**](https://github.com/yzhao062/anywhere-agents) AI agent config into any project, in one command.

```bash
pipx install anywhere-agents
anywhere-agents                          # bootstrap shared config + hooks + settings
anywhere-agents pack add <pack-repo-url>  # add a pack (one-shot: fetch, install, deploy)
```

Or zero-install for one-off bootstrap:

```bash
pipx run anywhere-agents
```

> **Why `pipx`, not `pip`?** `anywhere-agents` is a CLI tool with its own dependencies. Plain `pip install` either lands in the active venv (per-project, not per-machine) or hits PEP 668 / `externally-managed-environment` errors on modern Ubuntu / Debian / Homebrew Python. `pipx` gives each CLI tool its own isolated venv and exposes the binary on PATH globally; it is the [PyPA-recommended approach](https://packaging.python.org/en/latest/guides/installing-stand-alone-command-line-tools/) for Python CLI applications. `uv tool install anywhere-agents` works equivalently.

## What it does

Runs the shell bootstrap from the upstream repo in the current directory:

- Fetches `AGENTS.md` and replaces the local copy
- Sparse-clones the upstream repo into `.agent-config/`
- Syncs the shipped skills (`implement-review`, `my-router`) and their Claude Code command pointers
- Deep-merges project-level `.claude/settings.json`
- Deploys the safety guard hook to `~/.claude/hooks/guard.py` and merges user-level permissions
- Adds `.agent-config/` to `.gitignore`

Bootstrap logic lives in the shell bootstrap scripts at [`yzhao062/anywhere-agents/bootstrap/`](https://github.com/yzhao062/anywhere-agents/tree/main/bootstrap); the Python CLI invokes them so that agents and users in a Python-first workflow can run the same mechanism without reaching for `curl`. Pack management (`pack add | remove | verify | list | update`) and `uninstall` are implemented directly in the Python CLI.

## Options

```bash
anywhere-agents             # run bootstrap in cwd (default)
anywhere-agents --dry-run   # print what would happen without fetching or executing
anywhere-agents --version
```

## Requirements

- Python 3.9+
- `bash` on macOS/Linux, PowerShell (`pwsh` or `powershell`) on Windows
- `git` available on PATH (used by the bootstrap scripts to sparse-clone the upstream repo)

## Documentation and source

The real content lives in the GitHub repo: https://github.com/yzhao062/anywhere-agents

- [README](https://github.com/yzhao062/anywhere-agents#readme) — quickstart and benefits
- [CHANGELOG](https://github.com/yzhao062/anywhere-agents/blob/main/CHANGELOG.md)
- [Issues](https://github.com/yzhao062/anywhere-agents/issues)

## License

Apache 2.0.
