---
name: prun
description: Parallel delegation fan-out. The Claude session coordinates (on whatever Claude model is currently selected, e.g. Opus or Fable) while task units run in parallel on workers (never on the coordinator). Codex (`codex exec`, a separate abundant account) is the prioritized default; Sonnet is reserved for units needing Claude-session-internal capabilities (MCP/email tools, Artifacts, cross-vendor web verification), with the orchestrator deciding per unit. Units may read or write code; workers never commit or push, and the session plus the user are the final integration gate.
---

# prun (parallel run)

## Overview

`prun` fans a task out into independent units that run in parallel on separate-quota or in-session
workers, while the Claude session only coordinates. Workers are **Codex** (`codex exec`, a separate abundant account, frontier model) and **Sonnet**
subagents (inside the Claude session). **Codex is the prioritized default**: its quota is separate
from the Claude plan and its current model (gpt-5.6 tier) is strong on hard reasoning and code, so
most units go to Codex. **Sonnet is reserved** for units that need something the Claude session
uniquely provides (see the Executors rule). The coordinator decomposes the task, dispatches the units, gathers
their results, reviews their diffs, and integrates. It never runs a unit itself.

The orchestrator picks the executor per unit; when in doubt, Codex. A Codex unit runs through the
separately authenticated Codex/OpenAI account, so the worker run does not draw on the Claude plan at
all. A Sonnet unit and the Claude coordinator both consume the current Claude account's quota; the
exact split across models and weekly buckets depends on the plan and on active promotions and shifts
over time, so check Settings > Usage before relying on any model-specific split. Codex is the default
because its worker run is outside the Claude plan; keep Sonnet units targeted because they draw
Claude-side quota.

## Relationship to the native Workflow tool

The native Workflow tool fans a task out across **Claude** subagents under a deterministic script,
with structured output, judge panels, and resume. A Workflow run counts against the Anthropic plan's
usage and rate limits, and its agents use the session model unless the script routes a stage to a
different Claude model.

`prun` has a different quota shape. A **Codex** unit is dispatched by a shell call to `codex exec`,
so the worker run uses the separate Codex/OpenAI account. A **Sonnet** unit and the coordinating
session both draw the current Claude account's quota, so reserve Sonnet for units that need the
Claude session's own tools. The coordinating session also spends a small Anthropic amount while it
decomposes, dispatches, reads results, and integrates.

The two relate in two ways, both with the current session as the orchestrator:

- **Substitute (quota).** When the Anthropic pool is too constrained to run a Workflow, use prun
  with Codex-only units, or keep any Sonnet units small and targeted. This shifts the heavy fan-out
  to Codex while leaving only the coordinator and any chosen Sonnet work in the Anthropic pool.
- **Complement (diversity).** When a Workflow is affordable and you want cross-vendor perspectives,
  run a Claude panel through the Workflow and a Codex panel through prun. Use the same structured
  contract and the same question on both sides, then cross-check. Agreement across vendors is usually
  a stronger signal than agreement inside one model family, because shared model lineage and tools
  can share blind spots. Invoke them together in one natural-language request; no special mode is
  needed. Reserve this for high-stakes work (a review, an audit, a hard design call), since it spends
  both pools and the coordinator must merge two result sets.

## When to use

Use `prun` when the task splits into **independent units that can run at once** (different
modules, separate research questions, parallel analyses). Units may be heterogeneous, and there
can be **many of them**: a dozen or twenty in parallel is normal when the task warrants it.

Do not use `prun` when the task is one sequential unit, or units depend on each other's output,
or a unit's result cannot be checked without redoing it.

## Executors

| Executor | Quota | Notes |
|---|---|---|
| Codex (`codex exec`) | Separately authenticated Codex/OpenAI account; abundant | **Prioritized default.** Frontier model (gpt-5.6 tier), strong on hard reasoning and code, and the worker run spends no Claude-plan quota. Run many in parallel. |
| Sonnet subagent | Current Claude account; check Settings > Usage for the applicable limits or credits | Reserved, not a default. Runs in the Claude session, so it alone can reach session-internal tools (MCP / email / Artifacts) that Codex cannot. |
| Claude session (this session) | Current Claude account; check Settings > Usage for the applicable limits or credits | Coordinator and integrator only, on whatever model is selected. Never a unit. |

Rule: **units never run on the coordinator (the Claude session itself).** The orchestrator picks the
executor per unit, with a strong default toward Codex:

- **Codex is the default for almost every unit** (code, research, analysis, web fetch). Its quota is
  separate and abundant and its frontier model (gpt-5.6 tier) is capability-competitive with the top Claude models,
  so there is rarely a reason to prefer another worker. Start here.
- **Sonnet is the reserved exception, chosen only when a unit needs a tool the Claude session has but
  the isolated Codex worker does not.** Codex is an external process, so route to Sonnet when a unit
  needs a session-internal MCP / email connector (Gmail, Calendar, Drive, Slack), the Artifact tool,
  or a **cross-vendor web-search verification** where you want a Claude-side `WebSearch` result to
  cross-check the Codex one. A normal Sonnet subagent inherits the session's available tools but
  **starts with fresh, isolated context** (it does not see the conversation history), so put any
  needed state in its unit prompt; if a task truly needs the full live conversation, keep it in the
  coordinator (an explicit fork inherits that context but also the coordinator's model, so it is not
  a Sonnet worker). The orchestrator decides per unit; when in doubt, use Codex. Sonnet draws
  Claude-side quota, so keep these units targeted.
- **The Claude session stays the coordinator, never a unit.** A single small session-tool task the coordinator can
  do inline; reach for Sonnet when you need to run *many* such units in parallel.

## Concurrency

The orchestrator decides the unit count autonomously. Partition the task by **dependency
structure** (split only along genuinely independent boundaries) and **balanced workload**
(roughly equal-sized units, each worth a full worker run). High autonomy is the intent: do not
target a fixed number, and do not cap artificially. A dozen-plus in parallel is fine when the
task genuinely decomposes that way.

Two soft bounds, not hard rules: local CPU/RAM (heavy Codex workers contend past roughly a
handful at once, and the excess just queues) and Codex quota headroom. The usual real ceiling is
**integration bandwidth**, since the orchestrator must read and reconcile every result, so
prefer fewer well-scoped units over many tiny ones. Over-splitting into trivial units wastes
worker startup and tends to produce thin results.

## What a unit may do, and the one rule

A unit may **read or write code**, run commands, and fetch the web, with full access. The single
hard rule: a worker **never commits, pushes, or runs destructive git** (`commit`, `push`,
branch/tag mutation, `reset --hard`, `clean`). Everything else is allowed. The final gate is
**the Claude session integrating the results and the user deciding**; workers never touch the real repo history.

This is enforced structurally, not by trust:

- **Read-only / research units** run from a per-unit scratch cwd, so accidental writes stay out of
  the repo. `dispatch-task` does this by default.
- **Code-writing units** run inside a **throwaway local clone** of the repo with its remote removed:
  ```
  git clone --local -c core.longpaths=true <repo> <clone-dir>   # longpaths: Windows MAX_PATH safety
  git -C <clone-dir> remote remove origin
  ```
  The worker edits freely in the clone. An accidental `git push` has no remote to reach (GitHub /
  Overleaf stay untouched); an accidental `git commit` only lands in the throwaway clone. The coordinator reads
  `git -C <clone-dir> diff`, integrates the wanted changes into the real tree, and **the user
  approves the actual commit**. That is the only gate.

No credential scrubbing or sandbox wall: the user writes the prompts, the clone has no path to the
real remotes, and the Claude session plus the user are the integration gate. That is the whole safety model.

## Flow

1. **Gate**: confirm the task splits into independent, checkable units. Else use a single worker.
2. **Decompose**: write one prompt per unit. State the task; for a code-writing unit, that the
   working dir is a throwaway clone to edit freely but **not** commit or push; that the unit writes
   a result summary to its result file (a fresh path, in one write).
3. **Assign**: default the unit to Codex; pick Sonnet only for the reserved cases (session-internal
   MCP / email / Artifacts, or cross-vendor web verification). Also pick read-only (scratch) or code-writing (clone) mode.
   For a web-heavy unit, "Web access" below covers which executor fits.
4. **Dispatch in parallel**:
   - Codex unit: run `scripts/dispatch-task.{sh,ps1}` in the background (Bash tool,
     `run_in_background=true`). For a code-writing unit, pass the clone dir via `PRUN_SCRATCH_CWD`.
   - Sonnet unit: spawn a background Agent subagent with `model: sonnet`. It inherits the session's
     available tools, including MCP and connector tools; if you set a `tools` allowlist, include every
     connector, Artifact, file, shell, and web tool the unit needs. The subagent starts with fresh
     context, so put any needed state in its prompt. For code-writing it works in a clone too, under
     Claude's `guard.py`, which already gates commit/push.
5. **Monitor (do not go idle)**: launch `scripts/monitor.{sh,ps1} <state-dir> ...` in the background
   (`run_in_background=true`) and wait on its completion. It wakes you on the first actionable event:
   all done, any unit **stalled** (tail no-growth for `PRUN_STALL_THRESHOLD`, default 10 min), or any
   unit **failed** (`FALLBACK` result or dead dispatch), printing a per-unit digest. On a stall,
   surface it to the user with a likely cause (capacity or concurrency pressure; suggest lowering the
   worker count or re-dispatching) rather than waiting silently; act, then re-launch the monitor on the
   still-running units until all are done. `monitor` only observes; the unit's own `dispatch-task`
   reaps a worker idle past `PRUN_STALL_THRESHOLD` at the same threshold, so a persistent stall
   surfaces as a `FALLBACK` to re-dispatch rather than a leaked zombie. (`gather.{sh,ps1}` remains for
   the plain wait-for-all case.)
6. **Reconcile, then integrate**: before integrating, **reconcile the ledger**: every dispatched unit
   must have a non-empty result. If any is missing or empty, do **not** integrate the partial set;
   recover the worker's output from its `<state-dir>/tail` (dispatch-task also salvages the tail into
   the result file automatically under a `FALLBACK` header), then re-dispatch or flag the user if it is
   unusable. Then the coordinator reads each result plus each clone's `git diff`, merges the wanted changes into
   the real tree, runs verification, and **asks the user before any commit**.

Resolve scripts via this order, first hit wins: `skills/prun/scripts/`, then
`.claude/skills/prun/scripts/`, then `.agent-config/repo/skills/prun/scripts/`.

## dispatch-task usage (Codex)

```
scripts/dispatch-task.sh --prompt-file <prompt> --result-file <abs result> --unit-id <id>
```

- Emits exactly one stdout line `STATE-DIR <abs-path>`; codex stdout+stderr land in `<state-dir>/tail`.
- If the worker exits without writing a non-empty result file, dispatch-task salvages its captured
  `<state-dir>/tail` into the result file under a `FALLBACK` header, so a failed result-write never
  makes the unit silently vanish at gather. Treat a `FALLBACK` result as "review or re-dispatch."
- Self-heals a hung worker: if the tail stops growing for `PRUN_STALL_THRESHOLD` seconds (default
  `600`; the same idle signal `monitor` reports) or the run exceeds `CODEX_DISPATCH_TIMEOUT` seconds
  (default `0` = hard cap off, so the idle signal stays primary and an actively streaming long run is
  not killed), dispatch-task kills the worker's whole process tree, exits `124`, and writes the
  `FALLBACK` above naming `idle-stall` or `hard-timeout`. A non-empty result the worker already wrote
  is preserved, never clobbered. On Windows the watch+kill runs in the sibling `reap-watch.ps1` (an
  AMSI-safe split of launch from watch+kill; the `.sh` does it inline).
- Runs codex from a per-unit working dir: a scratch dir by default (read-only units), or the path in
  `PRUN_SCRATCH_CWD` (point this at a throwaway clone for code-writing units).
- Env: `CODEX_DISPATCH_SANDBOX` (default `danger-full-access`), `CODEX_DISPATCH_REASONING` (default
  `xhigh`), `CODEX_DISPATCH_ISOLATE_MCP=off` to drop MCP isolation, `PRUN_SCRATCH_CWD` to set the cwd,
  `PRUN_STALL_THRESHOLD` (default `600`) for the idle-reap threshold, `CODEX_DISPATCH_TIMEOUT`
  (default `0` = disabled) for an optional hard wall-clock cap.

## Sonnet usage

Sonnet is the reserved executor (see Executors), for units needing session-internal tools (MCP /
email connectors, the Artifact tool) or a cross-vendor web verification. Spawn an Agent-tool subagent
with `model: sonnet`. It inherits the session's available tools but starts with **fresh context** (it
does not see the conversation), so put any needed state in the unit prompt. Give it the same return
contract and result-file path. For a code-writing unit, point it at a clone dir; commit and push are
also gated by `guard.py` on the Claude side.

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

## monitor usage

```
scripts/monitor.sh <state-dir-1> <state-dir-2> ...
```

- Takes the `STATE-DIR` paths from each dispatch (not result files); reads each unit's `tail` (growth),
  `result-file` (done/fail), and `dispatch-pid` (liveness).
- Prints `MONITOR-START units=N stall-threshold=Ts timeout=Ss`, then on the first actionable event
  `MONITOR-EVENT <all-done|stall|fail|timeout>` and one `UNIT <name> <status>` line per unit (`done` /
  `failed(fallback)` / `failed(dispatch-dead)` / `stalled(Ns)` / `growing`).
- Exit: `0` all done, `3` attention needed (a stall or fail), `2` hard timeout.
- Env: `PRUN_STALL_THRESHOLD` (default 600, ten minutes; raise it for long code-writing units),
  `PRUN_MONITOR_POLL` (default 15), `PRUN_MONITOR_TIMEOUT` (default 3600), `PRUN_MONITOR_STABLE_WINDOW`
  (default 10).
- Run it in the background; after handling a stall or fail, re-launch on the still-running units so a
  resolved unit is not re-flagged.

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

Both executors reach the web by different paths, each with its own strengths, so assign per unit.

**Codex** runs on the user's local machine, so its requests leave from the user's local network
rather than the cloud fetcher's egress IP, often a residential IP. That can reach some pages a cloud
fetcher gets `403` on, though a hardened site can still block on bot score, fingerprint, or rate. It
also surfaces pages a cloud fetch would miss. Web access comes from `--sandbox danger-full-access`
(built-in browser path, confirmed under MCP isolation). Codex quota is abundant, so the extra unit is
cheap.

**Sonnet** units get web from an `agentType` granting built-in `WebSearch` and `WebFetch`. Claude's
`WebSearch` is strong at broad discovery (finding the right page when the URL is unknown), but
discovery alone is not a session-internal capability, so treat Sonnet here as a reserved path for an
explicitly wanted Claude-side cross-check or for recovery after Codex discovery falls short, not as
the default for discovery.

Routing heuristic (apply the Executors rule; when in doubt, Codex):

- **Fetch or discover on one path**: use a **Codex** unit first, whether or not the URL is known. Its
  local-network path also reaches some pages a cloud fetch gets `403` on.
- **Codex discovery fell short, or acceptance needs a Claude-side result**: add a targeted **Sonnet**
  unit and its `WebSearch`.
- **A high-stakes fact that might be stale or blocked**: run Codex first, then add a Sonnet
  cross-check when the value of a second vendor's view justifies the Claude-side quota.

A Codex web-fetch unit can use curl. Report the HTTP status per URL so a cloud-vs-local block shows
up in the result. In Windows PowerShell, name the binary `curl.exe`, since a bare `curl` can resolve
to the `Invoke-WebRequest` alias instead:

```bash
curl -sSL -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36" -o <body-file> -w "%{http_code} %{url_effective}\n" <URL>
```

```powershell
curl.exe -sSL -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36" -o <body-file> -w "%{http_code} %{url_effective}\n" <URL>
```
