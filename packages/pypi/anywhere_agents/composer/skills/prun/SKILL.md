---
name: prun
description: Parallel delegation fan-out on Agy. The coordinating session decomposes and integrates while task units run in parallel as Agy processes (Gemini through the Antigravity CLI), never on the coordinator and never on Claude-side workers such as Sonnet subagents. Each unit runs unattended in a scratch dir or throwaway clone and gets follow-up turns while slower units finish. A unit runs on Codex only when the user names Codex for it, or on Grok only when the user names Grok for it. Unit count follows the dependency graph rather than a small fixed cap. Units may read or write code; workers never commit or push, and the session plus the user are the final integration gate.
---

# prun (parallel run)

## Overview

`prun` fans a task out into independent units that run in parallel while the current session only
coordinates. **Every unit runs on Agy by default**: an Agy process running Gemini through the
Antigravity CLI, on the Google AI plan authenticated in `agy`. A unit runs on Codex only when the
user names Codex for it, or on Grok only when the user names Grok for it (see Executors). The coordinator decomposes the task, dispatches the units,
gathers their results, reviews their diffs, and integrates. It never runs a unit itself.

**No Claude-side workers.** A Sonnet subagent, a Workflow agent, or any other Agent-tool worker draws
on the same Claude account as the coordinating session, so a fan-out of them spends the pool the
session itself needs. Inspect current Agy quota before a large batch.

`prun` never starts a Workflow or a Claude subagent, not as a unit, a fallback, or a second panel. A
Claude panel the user explicitly asks for is a Workflow run outside `prun`. A cross-vendor read on
staged work is what `/vet` is for.

## When to use

Use `prun` when the task splits into **independent units that can run at once** (different
modules, separate research questions, parallel analyses). Units may be heterogeneous, and there
can be **many of them**: a dozen or twenty in parallel is normal when the task warrants it.

Do not use `prun` when the task is one sequential unit, or units depend on each other's output,
or a unit's result cannot be checked without redoing it.

## Reference Index

Read a reference only when its condition holds; reading one never authorizes an action this file
withholds.

| File | Read when |
|---|---|
| [references/grok-executor.md](references/grok-executor.md) | the user names Grok for a unit or a batch |
| [references/agy-quota-and-models.md](references/agy-quota-and-models.md) | before overriding `ANTIGRAVITY_DISPATCH_MODEL`, `ANTIGRAVITY_DISPATCH_EFFORT`, or `ANTIGRAVITY_PREFLIGHT`; when a `MODEL-*` line appears or a dispatch exits 75; when the sandbox needs changing; or when auto mode refuses a dispatch |
| [references/codex-executor.md](references/codex-executor.md) | the user names Codex for a unit or a batch |
| [references/state-recovery.md](references/state-recovery.md) | a run was interrupted, or `gather` replaces `monitor` |
| [references/web-units.md](references/web-units.md) | a unit needs the web |

## Executors

| Executor | Quota | Notes |
|---|---|---|
| Agy (`agy`) | Google AI plan authenticated in Antigravity | **The default worker.** Runs the registry's `prun-worker` model and effort for Agy; fast, separately funded, and dispatched with full unattended tool permission inside a scratch dir or throwaway clone. |
| Codex (`codex`) | Codex plan, the pool `/vet` reviewers use | **Only when the user names Codex** for a unit or a batch. Runs the registry's `prun-worker` model and effort for Codex through `dispatch-task.sh`. |
| Grok (`grok`) | Grok plan authenticated in the Grok CLI | **Only when the user names Grok** for a unit or a batch. Runs the registry's `prun-worker` model and effort for Grok through `dispatch-task-grok.py`. |
| Claude session (this session) | Current Claude account; check Settings > Usage for the applicable limits or credits | Coordinator and integrator only, on whatever model is selected. Never a unit. |

Rules:

- **Every unit runs on Agy by default.** Research, verification, extraction, cross-checks, and
  code-writing units in a throwaway clone all go through `dispatch-task-agy`. The dispatcher gives a unit the
  same unattended capability as the `/vet` Agy reviewer, so it can verify numbers, run experiments,
  and fetch the web. The registry (`user/agent-models.json`, see the `agent-models` skill) decides
  the worker model and effort: its `prun-worker` row, Agy column. A unit that needs another
  model names it in `ANTIGRAVITY_DISPATCH_MODEL`. An unreadable registry exits 2 and names the
  registry; the dispatcher never guesses a model.
- **The metered second group is never an unprompted choice.** Agy Ultra exposes a second quota
  group for Claude and GPT-OSS models, metered apart from the Gemini group. An agent does not
  reach for that group on its own outside the dispatcher's freer-meter routing, which sends a unit
  that names no model to the group with more headroom (see `references/agy-quota-and-models.md`).
- **Never a Claude-side worker.** Do not spawn an Agent-tool subagent (Sonnet or any other model)
  or a Workflow agent for a unit, including as a fallback when the Agy pool is short. Those workers
  spend the coordinating session's own Claude account. When Agy cannot take a batch, queue it or
  tell the user.
- **Codex only when the user names it.** A unit goes to Codex only when the user's request names
  Codex for that unit or for a batch ("a small /prun with codex for the hard problems"). The
  coordinator never picks Codex on its own, and never as a fallback when Agy is short: queue the
  units or tell the user. A Codex unit runs through `dispatch-task.sh`, never a hand-typed
  `codex exec`, and its model and effort come from the registry's `prun-worker` row for Codex, not
  from the command line or the interactive Codex config.
- **Grok only when the user names it.** A unit goes to Grok only when the user's request names
  Grok for that unit or for a batch. The coordinator never picks Grok on its own, and never moves
  units to Grok as a fallback when Agy is short: queue the units or tell the user. When Agy fails
  on a network or login error (`FALLBACK` with `preflight timed out after 60s` or
  `Antigravity preflight failed`, exit 124 or 70), report the failure and offer Grok to the user
  rather than switching automatically. A Grok unit runs through `dispatch-task-grok.py` with its
  model and effort from the registry's `prun-worker` row for Grok; see
  `skills/prun/references/grok-executor.md` for flags, child environment, and the automatic
  launch route.
- **Session-internal tools stay outside the fan-out.** An Agy process cannot use the coordinator's
  MCP, email connectors, or Artifact tool. Gather what a unit needs from those tools in the
  coordinating session before dispatch, and put it in the unit prompt; leave a small action that
  needs them to the coordinator as one inline step. A task whose substantive work needs those tools
  throughout is not a `prun` task.
- **Keep the pool busy with follow-up turns.** When a unit returns while others still run, dispatch
  a follow-up rather than idling, provided it discharges real work: an acceptance criterion the
  result left open, a claim made without evidence, a source cited but not fetched, a check proposed
  but not run, or the next independent unit in the queue. A slower sibling alone is no reason to
  invent work. `--continue-from <state-dir>` resumes the same conversation so the follow-up keeps
  its context; a fresh prompt with a fresh result path is the alternative. Record each follow-up in
  the ledger like any other unit.
- **The Claude session stays the coordinator, never a unit.** Independent substantive work belongs
  in workers.

The coordinator reviews every result and every diff. Do not convert changing meter readings into an
arbitrary low worker cap.

## Concurrency

The orchestrator decides the unit count autonomously. Partition the task by **dependency
structure** (split only along genuinely independent boundaries) and **balanced workload**
(roughly equal-sized units, each worth a full worker run). Do not target a fixed number or cap
artificially: a dozen-plus in parallel is fine when the task genuinely decomposes that way.

Two soft bounds, not hard rules: local CPU/RAM (enough concurrent workers eventually contend and
the excess queues) and the headroom of the Agy pool, which `agent-quota` reads for both groups,
with snapshot age and reset times. The usual real ceiling is **integration bandwidth**, since the
orchestrator must read and reconcile every result, so prefer fewer well-scoped units over many tiny
ones: over-splitting wastes worker startup and tends to produce thin results. Dispatch in batches
that fit the runtime's concurrent-worker limit and the available quota, and leave the rest queued;
that in-flight limit is separate from how many units a run may have in total.

## What a unit may do, and the one rule

A unit may **read or write code**, run commands, and fetch the web, with full access. The single
hard rule: a worker **never commits, pushes, or runs destructive git** (`commit`, `push`,
branch/tag mutation, `reset --hard`, `clean`). Everything else is allowed. The final gate is
**the Claude session integrating the results and the user deciding**; workers never touch the real repo history.

This is enforced structurally, not by trust:

- **Read-only / research units** run from a per-unit scratch cwd, so accidental writes stay out of
  the repo. `dispatch-task-agy.py` does this by default.
- **Code-writing units** run inside a **throwaway local clone** of the repo with its remote removed:
  ```
  git clone --local -c core.longpaths=true <repo> <clone-dir>
  git -C <clone-dir> remote remove origin
  ```
  The worker edits freely in the clone. An accidental `git push` has no remote to reach (GitHub /
  Overleaf stay untouched); an accidental `git commit` only lands in the throwaway clone. The coordinator reads
  `git -C <clone-dir> diff`, integrates and commits the wanted changes, and **the user
  approves the push**. That is the only gate.

**The worker's environment is scrubbed.** `dispatch-task-agy.py` builds the child environment
with `worker_env()`, which drops every name containing `KEY`, `TOKEN`, `SECRET`, `PASSWORD`,
`PASSWD`, `CREDENTIAL`, `APIKEY`, or `AUTH`, and every name starting `AWS_`. `ANTIGRAVITY_*` is
kept, because the CLI's own session plumbing lives there. The names withheld from a run are
listed in `<state-dir>/env-withheld`; values are never written anywhere. A unit that genuinely
needs one variable through gets it with `PRUN_KEEP_ENV=NAME1,NAME2`.

The dispatcher once passed `os.environ.copy()` straight through, and a worker need only list its
environment once, in an `env` command or a traceback, for a live key to enter a third-party model's
context: on 2026-09-20 a scan found two live credentials in 18 Agy conversations. The Codex
dispatcher does not scrub, because Codex may authenticate from its environment.

Beyond that: the user writes the prompts, the clone has no path to the real remotes, and the
session plus the user are the integration gate. Scrubbing removes the credential class of
accident; it is not a sandbox, and a worker can still read any file the user can.

## Flow

1. **Gate**: confirm the task splits into independent, checkable units. Else use a single worker.
2. **Decompose**: write one prompt per unit. State the task; for a code-writing unit, that the
   working dir is a throwaway clone to edit freely but **not** commit or push; that the unit writes
   a result summary to its result file (a fresh path, in one write).
3. **Assign**: every unit goes to Agy, except a unit the user named for Codex or Grok. Gather anything a unit
   needs from session-internal tools first and write it into that unit's prompt. Pick read-only
   (scratch) or code-writing (clone) mode, and record the mode in the ledger.
4. **Dispatch in parallel**: run `<python> scripts/dispatch-task-agy.py` in the background for each
   unit (`scripts/dispatch-task.sh` for a Codex unit, `<python> scripts/dispatch-task-grok.py` for a Grok unit). With no `--mode` it runs `accept-edits` with `--dangerously-skip-permissions` in a scratch
   directory it creates. A caller-supplied workspace, meaning `PRUN_SCRATCH_CWD` (a throwaway clone
   for a code-writing unit) or `--add-dir` (a clone or snapshot the unit should see), requires an
   explicit `--mode accept-edits` or `--mode plan`, so the write-capable mode is a named choice for
   any directory the dispatcher did not create. `--continue-from <state-dir>` resumes an earlier
   unit's conversation for a follow-up turn.
5. **Monitor (do not go idle)**: launch `scripts/monitor.sh <state-dir> ...` in the background
   (`run_in_background=true`) and wait on its completion. It wakes you on the first actionable event:
   all done, any unit **stalled** (tail no-growth for `PRUN_STALL_THRESHOLD`, default 10 min), or any
   unit **failed** (`FALLBACK` result or dead dispatch), printing a per-unit digest. On a stall,
   surface it to the user with a likely cause (capacity or concurrency pressure; suggest lowering the
   worker count or re-dispatching) rather than waiting silently; act, then re-launch the monitor on
   the still-running units until all are done. `monitor` only observes; each dispatcher reaps its
   own idle worker (exit 124, `FALLBACK`). (`gather.sh` remains for the plain wait-for-all case; see
   `references/state-recovery.md`.)
6. **Reconcile, then integrate.** Reconcile every dispatched unit before integrating the set. A
   nonzero dispatcher exit fails the unit even when its worker wrote a result. For Agy, an explicit
   nonblank final status other than `SUCCESS` also fails the unit; a missing or blank final status
   counts as success, so an older Agy keeps working. A worker-written non-empty result is kept, and
   a closing response goes beside it. Only when no such worker result exists does a missing, empty,
   or sub-20-byte final response fail the unit. A monitor or gather `DONE` is not enough: check
   dispatcher completion and the backend's result evidence before accepting the result, and never
   let a `SUCCESS` event override an observed nonzero exit. Re-dispatch or ask when no usable
   result remains. If any dispatched unit still has no usable result, do not integrate the partial
   set. Then read each accepted result and clone diff, integrate the wanted changes, run
   verification, commit, and ask before pushing.

Resolve scripts via this order, first hit wins: `skills/prun/scripts/`, then
`.claude/skills/prun/scripts/`, then `.agent-config/repo/skills/prun/scripts/`.

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
file, clone-dir, result file, status (dispatched / done / failed), start/end, and the unit's
state-dir. Take the executor column from `<state-dir>/model` (the model sent to the worker, after
any Agy quota fallback) and the `status=` word from `<state-dir>/model-actual`; report a `mismatch` or
`unknown` unit as unconfirmed. Use the ledger to report progress and to relaunch
only units whose result is missing or fails validation.

**Where a unit's own files go**: the per-unit prompt, the result file, the shared-context file every worker reads, and the run ledger belong under an `agent-io` directory inside the scratch area. The name tells the writing-style hook to skip them, because none of that text is the coordinator's prose to rewrite. Anything the fan-out produces for a human reader stays outside `agent-io`.

**Interrupted runs**: `references/state-recovery.md` holds `report-state` and `snapshot-tail`.
Inspecting `report-state` output or snapshotting a tail never establishes that deleting,
overwriting, or promoting a unit is safe, or that the unit is finished.

## dispatch-task usage (Agy)

```
<python> scripts/dispatch-task-agy.py --prompt-file <prompt> --result-file <fresh abs result> --unit-id <id>
```

- Emits exactly one stdout line `STATE-DIR <abs-path>`; Agy stream events and stderr land in the
  state directory, the conversation id from Agy's `init` event is recorded to
  `<state-dir>/conversation-id`, and the final response is published atomically to the result path.
- `--mode` defaults to `accept-edits` with `--dangerously-skip-permissions`, the same unattended
  capability the implement-review Gemini reviewer already runs with, so a unit can verify numbers,
  run experiments, and fetch the web without a permission prompt. The default applies only to the
  scratch directory the dispatcher creates. When the caller supplies a workspace through
  `PRUN_SCRATCH_CWD` or `--add-dir`, the dispatcher refuses to launch until `--mode` is given,
  because the write-capable mode could otherwise reach a directory it did not create. `--mode plan` is the strictly read-only opt-in; it keeps request-review
  permissions and never gets the skip flag. In headless use a tool request that needs an approval
  nobody can give (`run_command`, `read_url`, browser tools) is denied, and the process can still
  exit 0 with a normal-looking result that reports it could not verify. A normal result file
  therefore does not prove those checks ran; read its `Verification` and `Open items` fields. A
  standing `permissions.allow` rule in Agy's own `settings.json`
  (`~/.gemini/antigravity-cli/settings.json`, entries such as `read_url(*)` or `command(*)`) is the
  alternative for a plan-mode unit.
- `--add-dir PATH` (repeatable) adds a directory outside the unit's working directory to its
  workspace without copying a repository into the scratch area, in either mode; it requires an
  explicit `--mode`. Point it at a clone or a read-only snapshot, never the real tree, since
  `accept-edits` can write there. Each path is resolved to absolute and must be an existing directory.
- `--continue-from STATE_DIR` resumes the conversation recorded at `<STATE_DIR>/conversation-id`,
  so a follow-up keeps the earlier turn's context instead of re-embedding the prior result. It
  still needs its own fresh `--result-file`; an empty `STATE_DIR`, or one whose conversation id file
  is missing or empty, is a pre-launch error.
- Requires a fresh result path and refuses to overwrite an existing result. The final response is
  published to that path, unless the worker already wrote a non-empty result file there itself: then
  the worker's file is kept and the final response lands beside it as `<result>.response.<ext>`, so
  a one-line closing reply never replaces a full result. If no non-empty worker result exists, a
  failed preflight, launch, worker run, or timeout, or an unusable final response (missing, empty,
  or under 20 bytes), produces an atomic `FALLBACK` result with captured tails.
- Both signals decide the outcome. A non-zero process exit fails the unit, and after an exit of 0
  the final `result` event's `status` is consulted, because Agy exits 0 when it stops on a quota
  limit and that `ERROR` event still carries the opening narration in `response`; publishing it
  would hand the coordinator work that never happened. Any status other than `SUCCESS` fails the
  unit and carries the event's `error` text into the `FALLBACK` result. A missing or blank status
  counts as success, so an older Agy keeps working.
- A failed run whose worker had already written its own result keeps that file as the record of
  how far the unit got, puts the partial response beside it as `<result>.response.<ext>`, and exits
  non-zero with the backend error on stderr; the unit still fails (Flow step 6). `monitor` and
  `gather` call a stable worker-written result `done` without reading the backend status, so before
  accepting such a unit, wait for the dispatcher to finish, then check both its exit code and the
  `result` event's `status` and `error` in `<state-dir>/tail`.
- `ANTIGRAVITY_DISPATCH_TIMEOUT_SECONDS` defaults to 2700 and is passed to Agy's bounded
  `--print-timeout`.
- The dispatcher omits Agy's `--sandbox` flag unless `PRUN_AGY_SANDBOX` asks for it (accepted
  values and the scratch-directory limits are in `references/agy-quota-and-models.md`).

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
