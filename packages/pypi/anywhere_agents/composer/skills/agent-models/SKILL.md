---
name: agent-models
description: Show, check, and change the model and effort every agent role runs on (interactive sessions, /vet reviewers, review panels, audits, Claude subagents and Workflow agents, prun workers, the Claude advisor) through the one registry at user/agent-models.json. Use when the user asks which model something runs on, wants to switch a model or effort, suspects a setting is not taking effect, or adds a script that launches a model.
---

# agent-models

## Overview

One file names the model and effort of every agent: `user/agent-models.json`, the registry. One script reads it: `scripts/agent_models.py`, the resolver. Nothing else in a source repository or a consumer names a concrete model or effort. A dispatcher asks the resolver at run time, and bootstrap writes the interactive values into each tool's own config.

This skill does not repeat the values. Run `show` to see them.

Find the resolver at `scripts/agent_models.py` in a source repository (`agent-config`, `anywhere-agents`) and at `.agent-config/repo/scripts/agent_models.py` in a consumer. Run it with the project's Python (`.agent-config/repo/scripts/_python` or `~/.claude/hooks/_python` finds one). The commands are the same under Claude Code, Codex, and any other agent, on macOS, Linux, and Windows.

## The rules

1. **One row per family.** The registry has a row for `claude`, `codex`, and `agy`, and an optional row for `grok`. Every role of a family runs on that row unless the registry lists an override for the role. The advisor is the one role that never inherits it.
2. **Roles.** `interactive` is the session a person types into. `reviewer` covers every review: a `/vet` reviewer, a venue or proposal review panel, an internal-review audit. `worker` is a Claude subagent or Workflow agent that does fan-out work. `prun-worker` is a unit `prun` runs. `premium` is optional: a model whose plan allowance is small, for a writing task or another task the user decides is worth it. Nothing dispatches to it automatically, and every task works without it. `advisor` is the model Claude Code's advisor tool consults. It is optional, lists `claude` only, and carries a model with no effort. See "Use the advisor row". A role other than the advisor with no override equals the family row, so a review runs on the same model and effort as the interactive session by default.
3. **Exceptions are listed, never implied.** An override exists only as an entry under `roles` in the registry. If a role should differ from its family, add the entry there; do not put a value in a script, a skill, or a rule file.
4. **No literal outside the registry.** Code resolves at run time. Prose states the rule and points here. `tests/test_agent_models.py` scans the tracked files and fails on a concrete model name or effort value outside the registry, the changelog, archived records, and test fixtures. A line that must quote one (a past measurement, a parser example) carries the marker `agent-models: allow`.
5. **Per-run overrides stay possible.** `CODEX_DISPATCH_MODEL`, `CODEX_DISPATCH_REASONING`, `ANTIGRAVITY_DISPATCH_MODEL`, `ANTIGRAVITY_DISPATCH_EFFORT`, `GROK_DISPATCH_MODEL`, and `GROK_DISPATCH_EFFORT` change one run and outrank the registry. They are for a single run, not a place to keep a default. The Antigravity and Grok variables are read by both `/vet` and `prun`.
6. **Effective, not configured.** A value counts only when the CLI reports it ran on that value. Every dispatcher records the requested and the actual model and effort, and prints `MODEL-MISMATCH` when they differ. See "Check that it took effect".

## How each tool receives its value

| Role | Path |
|---|---|
| Claude interactive | `user/settings.json` carries `model`, `effortLevel`, and the env entry `CLAUDE_CODE_EFFORT_LEVEL`; `agent_models.py render` keeps them equal to the registry, and bootstrap merges the file into `~/.claude/settings.json`. |
| Claude advisor | `user/settings.json` carries `advisorModel` while the registry lists an advisor; `render` keeps it equal to the registry, and bootstrap merges the file into `~/.claude/settings.json`. |
| Claude reviewer | `dispatch-claude` passes `--model` and `--effort` and sets `CLAUDE_CODE_EFFORT_LEVEL` in the child environment. |
| Claude worker | The env entry `CLAUDE_CODE_SUBAGENT_MODEL` in `user/settings.json`. A subagent or Workflow agent that acts as a reviewer passes the reviewer model in the call, which outranks that entry. |
| Codex interactive | `agent_models.py sync`, run by bootstrap, sets `model` and `model_reasoning_effort` at the top of `~/.codex/config.toml`. |
| Codex reviewer | `dispatch-codex` passes `-c model=` and `-c model_reasoning_effort=`. |
| Agy interactive | `sync` sets `model` in `~/.gemini/antigravity-cli/settings.json` to the display name `agy models` lists. |
| Agy reviewer | `dispatch-gemini.py` takes the family stem and effort and floats to the newest version Agy lists. |
| Grok reviewer / prun worker | No interactive config sync (`~/.grok/config.toml` is untouched). `dispatch-grok.py` and `dispatch-task-grok.py` pass `-m` and `--effort` from the `reviewer` and `prun-worker` rows; `verify --family grok` checks this dispatcher route. |
| prun worker | `dispatch-task-agy.py` takes the Agy `prun-worker` row. `dispatch-task.sh` and `.ps1`, used only when the user names Codex for a unit, take the Codex one. `dispatch-task-grok.py`, used only when the user names Grok, takes the Grok one. |

Facts that explain most surprises:

- In Claude Code, `CLAUDE_CODE_EFFORT_LEVEL` outranks `/effort`, `--effort`, and the per-model `modelSettings` entry that `/effort` saves. A session reads it once at start, so a running session keeps its old effort until it is restarted.
- A child `claude -p` inherits that variable from a Claude parent. `--setting-sources project,local` skips user settings but does not clear it.
- `codex exec --ignore-user-config` drops the configured model and effort, so the dispatcher passes both again.
- An Agy slug is a stem plus an effort tier. The registry holds the stem; the version floats.
- Claude settings cannot persist the top effort level; the env entry carries it.
- `sync` leaves the Codex model alone when `config.toml` sets a `model_provider` other than `openai`. It leaves the whole file alone, and says so, when it cannot check its own edit: a layout its line editor does not handle, or a Python before 3.11, which has no TOML parser. `doctor` still reports the drift. `AGENT_MODELS_SYNC=off` in the environment skips `sync`.

## Commands

```bash
agent_models.py show                      # every family and role, resolved
agent_models.py resolve --family codex --role reviewer        # "<model> <effort>"
agent_models.py resolve --family agy --role reviewer --field slug
agent_models.py doctor                    # live configs against the registry; exit 1 on drift
agent_models.py doctor --scan ~/PycharmProjects/<repo>        # also list literals in a tree
agent_models.py verify                    # launch each CLI once and read back what it ran on
agent_models.py sync [--dry-run]          # write the Codex and Agy interactive values
agent_models.py render [--check]          # keep user/settings.json equal to the registry
agent_models.py set --family <family> [--role <role>] [--model <name>] [--effort <level>]
agent_models.py set --family agy --role <role> --role-only --fallback-model <stem> [--fallback-effort <tier>]
agent_models.py set --family claude --role advisor (--model <name> | --off)    # set or remove the advisor row
agent_models.py map <dir>...              # which skills and scripts read which row; a file that resolves two or more reviewer rows gets its missing families listed
agent_models.py actual --family <family> (--session-id ID | --stderr-file F | --log-file F | --json-file F)
```

## Use the premium row

The premium row exists so that an occasional task can run on the scarcer model at a known effort, without making that model the default and without watching its allowance. The user decides when; a skill never requires it.

- New session: read the row with `agent_models.py resolve --family claude --role premium`, then start `CLAUDE_CODE_EFFORT_LEVEL=<effort> claude --model <model>`.
- Open session: `/model <model>` switches the model, but the effort stays at the session's level, because `CLAUDE_CODE_EFFORT_LEVEL` applies to every model.
- When the allowance for that model is spent, Claude Code stops the request and says so. Switch back with `/model`; the default row is unaffected.

## Use the advisor row

The advisor row names the model that Claude Code's advisor tool consults. Claude decides when to call it, and no setting forces or caps the calls, so the shared rule in `AGENTS.md` is the only lever on how often it runs. It must rank at or above the main model, a check Claude Code also applies to subagents; the registry does not check the pairing. The tool has no effort setting, so the row carries a model and nothing else. A registry without a `roles.advisor.claude` row has no advisor, because this role never inherits the family row.

- **Where it applies.** `render` writes `advisorModel` into `user/settings.json` while the registry lists an advisor row and removes it otherwise, and bootstrap merges the file into `~/.claude/settings.json`. The registry is byte-identical between `agent-config` and its public mirror, so every consumer of either gets the advisor. `doctor` compares the live `advisorModel` with the registry, reports a key left behind after the row is removed, and skips the check where `CLAUDE_CODE_DISABLE_ADVISOR_TOOL` is set.
- **Billing and consent.** On a subscription plan the advisor's usage counts toward the plan's limits; on API billing it is charged at the advisor model's rates. Each call rereads the whole conversation, with no cache reuse between calls. Where the advisor's model bills to usage credits, the advisor bills there too, and Claude Code applies it only after the one-time consent given through `/model <advisor model>`: until then an interactive session shows a notification that points there, and `claude --advisor <model>` exits at launch. Accepting the consent also makes the advisor model the main model, so run `/model <interactive model>` right after; `advisorModel` stays the advisor setting.
- **When the advisor is missing.** Except on Claude Platform on AWS and Claude Models on Google Cloud (Anthropic managed) with Claude Code v2.1.300 or later, the tool needs a feature flag fetched from Anthropic. A session skips that fetch when `CLAUDE_CODE_DISABLE_FEATURE_FLAGS` is `1`, when `DO_NOT_TRACK` is on (`0` leaves the fetch on), or when `DISABLE_TELEMETRY` or `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` holds any non-empty value, `0` and `false` included. It also skips the fetch on a third-party provider such as Amazon Bedrock, and a session can lack the flag after an install or upgrade, in a clean environment on every run, or with a gateway token and no API key. An advisor that ranks below the main model is not attached to the main model's requests, though a subagent whose own model it outranks still uses it, and a pairing the API refuses is resent without the advisor and without an error. In these cases a run can exit 0 with no advisor, so check that it fires rather than trusting the exit code.
- **Check that it fires.** Run `claude -p "<a small design question; consult the advisor first>" --output-format json` and look for the advisor's model among the `modelUsage` keys, or for a `server_tool_use` named `advisor` in the session transcript. `usage.iterations` covers only the last API response, so a multi-turn run can miss the call there. In an interactive session the transcript shows an `Advising` line, and Ctrl+O shows the guidance when the advisor returned readable text. In headless output the advice of current advisor models arrives as the API's encrypted `advisor_redacted_result`, which proves which model ran but not what it said.
- **Change it.** `set --family claude --role advisor --model <name>` writes the row and renders. In a session, `/advisor <model>` saves the key to the user settings and `claude --advisor <model>` applies to that session only; bootstrap writes the registry value back at the next session start.
- **Turn it off.** `/advisor off` lasts until the next bootstrap, which merges `advisorModel` from the registry again. For a durable stop, set `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` in the `env` block of `~/.claude/settings.json` (it disables the tool on that machine), or remove the row with `set --family claude --role advisor --off`. The second renders the key out of `user/settings.json`, but bootstrap's merge never deletes a key, so on each machine that already has it, run `/advisor off` once after the row is gone; `doctor` reports the leftover key until then.

## Check that it took effect

Use all three levels after a change. A config file that holds the right value proves nothing when another setting outranks it.

1. `doctor` compares `~/.claude/settings.json`, `~/.codex/config.toml`, the Antigravity settings, and the process environment with the registry.
2. `verify` starts each CLI with no model argument, as a new terminal session would, and reads the model and effort from the CLI's own report: the Claude session transcript, the header `codex exec` prints, and the backend model label in the Agy log. It spends one short request per family.
3. A review round writes `<state-dir>/review-model` with the requested values, their source, and the actual values. After a change, run one `/vet both` round and read those files. `prun` records the same for each unit.

For a Claude session already open, read its transcript: `agent_models.py actual --family claude --session-id <id>`.

## Change a model or an effort

Do this in the source repository your projects bootstrap from (your fork of `anywhere-agents`), never in a consumer project: bootstrap overwrites the copy there. The user approves every commit and push.

1. `agent_models.py set --family <family> --model <name> --effort <level>` changes the family row and renders `user/settings.json`. Add `--role <role>` to write an override for `premium`, `worker`, or `prun-worker`. Add `--role reviewer --role-only` only when a review must differ from the interactive session, and say why in the commit.
2. `agent_models.py show`, then run `tests/test_agent_models.py` and the dispatcher tests (`test_dispatch_claude.py`, `test_dispatch_codex.py`, `test_dispatch_gemini.py`, `test_dispatch_grok.py`, `test_dispatch_task_agy.py`, `test_dispatch_task_grok.py`, `test_dispatch_task.py`, `test_render_banner.py`).
3. Run `/vet`, wait for approval, then commit and push.
4. Bring the change to each consumer project: run bootstrap there, or start a new session in it, since bootstrap runs at session start. Bootstrap merges the Claude settings and runs `sync`.
5. Run `doctor`, then `verify`. Start a new Claude session, since an open one keeps its startup effort. Run one `/vet both` round and check the `review-model` records.
6. On every other machine, pull and run bootstrap in one consumer.

A model that does not exist for the account fails at step 5, not before, so do not skip it.

A maintainer who keeps a second repository byte-identical for the shared files (as `agent-config` and `anywhere-agents` are kept) adds this before step 3: copy the registry to the mirror, run `agent_models.py render` there (a sanitized `user/settings.json` is rendered, never copied), and run `scripts/check-parity.sh`. Then review and push both.

## Add a script that launches a model

1. Pick the role. If none fits, add the role to `ROLES` in `agent_models.py` and to the registry in the same change.
2. Resolve at run time. Find the resolver by walking up from the script's own directory: at each ancestor take `scripts/agent_models.py` when `user/agent-models.json` sits beside it, else `.agent-config/repo/scripts/agent_models.py`. Do not count directories: a consumer runs the copy bootstrap deploys under `.claude/skills/`, which is not inside `.agent-config/repo`. `dispatch-gemini.py` has the Python form (`find_agent_models`) and `dispatch-codex.sh` the shell form.
3. Pass the values explicitly to the CLI, and for Claude also set `CLAUDE_CODE_EFFORT_LEVEL` in the child environment.
4. Record what the CLI reports with `agent_models.py actual` and surface a mismatch.
5. Fail with a message that names the registry when the resolver cannot answer. Do not fall back to a value written in the script.

## Review a consumer for drift

`agent_models.py doctor --scan <repo>` lists every model and effort literal in a tree. A hit in a skill or a script that launches an agent is a defect, including a script written for one run: change it to resolve. Two kinds of hit stay. The first is a data record of a finished run (a score file, a saved review), since editing it would falsify the record. The second is an experiment whose subject is the model itself, where the name is the thing under study and not an agent setting.
