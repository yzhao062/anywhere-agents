---
name: prun
description: Parallel delegation fan-out. The Opus session coordinates while many task units run in parallel on Codex and Sonnet workers (never on Opus), to spend the Codex and Sonnet quotas instead of the constrained Opus one. Units may read or write code; workers never commit or push, and Opus plus the user are the final integration gate.
---

# prun (parallel run)

## Overview

`prun` fans a task out into independent units that run in parallel on cheap or separate-quota
workers, while the Opus session only coordinates. Workers are **Codex** (`codex exec`, a
separate account from Claude, abundant quota, strong on hard reasoning and code) and **Sonnet**
subagents (cheaper, inside the Claude session). Opus decomposes the task, dispatches the units,
gathers their results, reviews their diffs, and integrates. It never runs a unit itself, so the
constrained Claude "All models" weekly bucket pays only for coordination.

Mix freely. A combination of Codex and Sonnet often beats all-Codex, by efficiency or by playing
to each one's strengths (gpt-5.5 on Codex is stronger; Sonnet is cheaper and shares the Claude
session).

## When to use

Use `prun` when the task splits into **independent units that can run at once** (different
modules, separate research questions, parallel analyses). Units may be heterogeneous, and there
can be **many of them**: a dozen or twenty in parallel is normal when the task warrants it.

Do not use `prun` when the task is one sequential unit, or units depend on each other's output,
or a unit's result cannot be checked without redoing it.

## Executors

| Executor | Quota | Notes |
|---|---|---|
| Codex (`codex exec`) | Codex/OpenAI account, separate from Claude; abundant | Primary. Strong on hard reasoning and code. Run many in parallel. |
| Sonnet subagent | Claude side, own weekly bucket; cheaper than Opus | Secondary, mixable. Runs in the Claude session (its tools / MCP / context). |
| Opus (this session) | Claude "All models" weekly bucket (constrained) | Coordinator and integrator only. Never a unit. |

Rule: **units never run on Opus.** Pick Codex or Sonnet per unit by fit.

## Concurrency

Launch **as many units as the task needs**: a dozen-plus in parallel is normal. The only real
bounds are local CPU/RAM and Codex quota headroom. Queue beyond what the machine handles
comfortably; do not cap artificially.

## What a unit may do, and the one rule

A unit may **read or write code**, run commands, and fetch the web, with full access. The single
hard rule: a worker **never commits, pushes, or runs destructive git** (`commit`, `push`,
branch/tag mutation, `reset --hard`, `clean`). Everything else is allowed. The final gate is
**Opus integrating the results and the user deciding**; workers never touch the real repo history.

This is enforced structurally, not by trust:

- **Read-only / research units** run from a per-unit scratch cwd, so accidental writes stay out of
  the repo. `dispatch-task` does this by default.
- **Code-writing units** run inside a **throwaway local clone** of the repo with its remote removed:
  ```
  git clone --local -c core.longpaths=true <repo> <clone-dir>   # longpaths: Windows MAX_PATH safety
  git -C <clone-dir> remote remove origin
  ```
  The worker edits freely in the clone. An accidental `git push` has no remote to reach (GitHub /
  Overleaf stay untouched); an accidental `git commit` only lands in the throwaway clone. Opus reads
  `git -C <clone-dir> diff`, integrates the wanted changes into the real tree, and **the user
  approves the actual commit**. That is the only gate.

No credential scrubbing or sandbox wall: the user writes the prompts, the clone has no path to the
real remotes, and Opus plus the user are the integration gate. That is the whole safety model.

## Flow

1. **Gate**: confirm the task splits into independent, checkable units. Else use a single worker.
2. **Decompose**: write one prompt per unit. State the task; for a code-writing unit, that the
   working dir is a throwaway clone to edit freely but **not** commit or push; that the unit writes
   a result summary to its result file (a fresh path, in one write).
3. **Assign**: pick Codex or Sonnet per unit, and read-only (scratch) or code-writing (clone) mode.
4. **Dispatch in parallel**:
   - Codex unit: run `scripts/dispatch-task.{sh,ps1}` in the background (Bash tool,
     `run_in_background=true`). For a code-writing unit, pass the clone dir via `PRUN_SCRATCH_CWD`.
   - Sonnet unit: spawn a background Agent subagent with `model: sonnet` and web-capable tools
     (Read/Write/Edit/Bash/WebSearch/WebFetch). For code-writing it works in a clone too, and is
     additionally under Claude's `guard.py`, which already gates commit/push.
5. **Gather**: `scripts/gather.{sh,ps1} <result-file> ...` until all land, or use the per-dispatch
   background-completion signals.
6. **Integrate**: Opus reads each result plus each clone's `git diff`, merges the wanted changes into
   the real tree, runs verification, and **asks the user before any commit**.

Resolve scripts via this order, first hit wins: `skills/prun/scripts/`, then
`.claude/skills/prun/scripts/`, then `.agent-config/repo/skills/prun/scripts/`.

## dispatch-task usage (Codex)

```
scripts/dispatch-task.sh --prompt-file <prompt> --result-file <abs result> --unit-id <id>
```

- Emits exactly one stdout line `STATE-DIR <abs-path>`; codex stdout+stderr land in `<state-dir>/tail`.
- Runs codex from a per-unit working dir: a scratch dir by default (read-only units), or the path in
  `PRUN_SCRATCH_CWD` (point this at a throwaway clone for code-writing units).
- Env: `CODEX_DISPATCH_SANDBOX` (default `danger-full-access`), `CODEX_DISPATCH_REASONING` (default
  `xhigh`), `CODEX_DISPATCH_ISOLATE_MCP=off` to drop MCP isolation, `PRUN_SCRATCH_CWD` to set the cwd.

## Sonnet usage

Spawn an Agent-tool subagent with `model: sonnet` and the tools the unit needs. Give it the same
return contract and result-file path. For a code-writing unit, point it at a clone dir; commit and
push are also gated by `guard.py` on the Claude side.

## gather usage

```
scripts/gather.sh <result-file-1> <result-file-2> ...
```

- Prints `GATHER-START count=N timeout=Ss`, then `DONE <abs-path>` per file as it lands; exits 0 when
  all land, exits 2 with `TIMEOUT remaining=<k>`.
- A file is "landed" when it exists, is non-empty, and has been quiet for the stable window
  (default 10s); no startup-snapshot race.
- **Use a fresh result path per unit per run** (delete any stale file before dispatch). Have each unit
  write its result in one operation.

## Return contract (every unit writes this)

```
# <unit-id> result
Conclusion: <one line>
Files: <files created/modified in the clone, or "none (read-only)">
Open items: <blockers or follow-ups, or "none">
Verification: <what was run/checked/searched, or "none">

<body: the findings, survey, analysis, or change summary>
```

## Ledger

Keep a simple run ledger (a file in a scratch area) recording each unit: id, executor, mode, prompt
file, state-dir / clone-dir, result file, status (dispatched / done / failed), start/end. Use it to
report progress and to relaunch only units whose result is missing or fails validation.

## Web access

Codex units get web via `--sandbox danger-full-access` (built-in browser path, confirmed under MCP
isolation). Sonnet units need an `agentType` granting built-in `WebSearch` + `WebFetch`.
