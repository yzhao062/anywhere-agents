# anywhere-agents

Install the [**anywhere-agents**](https://github.com/yzhao062/anywhere-agents) AI agent config into any project, in one command.

Zero-install (recommended for one-shot bootstrap):

```bash
npx anywhere-agents
```

Or install once, reuse:

```bash
npm install -g anywhere-agents
anywhere-agents
```

> **Pack management requires the Python CLI.** This npm package covers `bootstrap` only. For `anywhere-agents pack add | remove | verify | list | update` and `anywhere-agents uninstall`, install the Python CLI: `pipx install anywhere-agents` from [PyPI](https://pypi.org/project/anywhere-agents/). For full feature coverage (bootstrap + pack management) on a single install, prefer `pipx install anywhere-agents` over npm.

## What it does

Runs the shell bootstrap from the upstream repo in the current directory:

- Fetches `AGENTS.md` and replaces the local copy
- Sparse-clones the upstream repo into `.agent-config/`
- Syncs the shipped skills (`implement-review`, `my-router`) and their Claude Code command pointers
- Deep-merges project-level `.claude/settings.json`
- Deploys the safety guard hook to `~/.claude/hooks/guard.py` and merges user-level permissions
- Adds `.agent-config/` to `.gitignore`

All install logic lives in the shell bootstrap scripts at [`yzhao062/anywhere-agents/bootstrap/`](https://github.com/yzhao062/anywhere-agents/tree/main/bootstrap). This npm package is a thin CLI wrapper so that agents and users in a Node-first workflow can invoke the same mechanism without reaching for `curl`.

## Options

```bash
anywhere-agents            # run bootstrap in cwd (default)
anywhere-agents --dry-run  # print what would run without fetching or executing
anywhere-agents --version
anywhere-agents --help
```

## Requirements

- Node.js 14+
- `bash` on macOS/Linux, PowerShell (`pwsh` or `powershell`) on Windows
- `git` available on PATH (used by the bootstrap scripts to sparse-clone the upstream repo)

## Documentation and source

The real content lives in the GitHub repo: https://github.com/yzhao062/anywhere-agents

- [README](https://github.com/yzhao062/anywhere-agents#readme) — quickstart and benefits
- [CHANGELOG](https://github.com/yzhao062/anywhere-agents/blob/main/CHANGELOG.md)
- [Issues](https://github.com/yzhao062/anywhere-agents/issues)

## License

Apache 2.0.
