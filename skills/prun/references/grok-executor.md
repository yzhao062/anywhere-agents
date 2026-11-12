# Grok Executor Reference (`dispatch-task-grok.py`)

This reference documents the Grok executor for `prun` (`skills/prun/scripts/dispatch-task-grok.py`). Like Codex, a unit runs on Grok only when the user names Grok for that unit or batch. The coordinator never moves units to Grok on its own, including when Antigravity (`agy`) is out of quota or unreachable.

## Command Contract and Flags

Invoke the dispatcher in the background with the same arguments `dispatch-task-agy.py` takes:

```bash
python3 "$PRUN_GROK" \
  --prompt-file "$SCRATCH/<unit>.prompt.md" \
  --result-file "$SCRATCH/<unit>.result.md" \
  --unit-id <unit>
```

Supported arguments:

- `--prompt-file <path>`: path to the unit prompt file. The dispatcher prepends a worker preamble instructing the Grok worker to skip the bootstrap, banner, session-start, and router steps of any rule file it finds, apply the substantive policy and local overrides carried in the unit prompt, and never commit, push, or run destructive git commands.
- `--result-file <path>`: fresh output path; the dispatcher refuses to overwrite an existing file.
- `--unit-id <id>`: identifier matching `^[A-Za-z0-9_-]+$`.
- `--mode <plan|accept-edits>`: defaults to `accept-edits` (`--always-approve`) when the dispatcher creates its own scratch directory (`<state-dir>/work`). When `PRUN_SCRATCH_CWD` or `--add-dir` is supplied, `--mode` must be passed explicitly (`--mode accept-edits` for a disposable throwaway clone, or `--mode plan` for read-only work using `--permission-mode plan`).
- `--add-dir <dir>`: repeatable option that adds an extra directory to Grok's workspace. Cannot be combined with `--continue-from`.
- `--continue-from <prev-state-dir>`: resumes the session recorded in `<prev-state-dir>/session-id` (or `conversation-id`) by passing `-r <sessionId>` to `grok`.

Under the hood, the dispatcher resolves `<model>` and `<effort>` from `user/agent-models.json` via `scripts/agent_models.py resolve --family grok --role prun-worker` and runs:

```text
<grok-bin> --prompt-file <state-dir>/prompt-relay --cwd <scratch> \
  --always-approve --output-format streaming-json --no-auto-update \
  -m <model> --effort <effort>
```

## State Directory and Streaming Contract

The dispatcher prints one line to stdout (`STATE-DIR <abs-path>`) and populates `<state-dir>` with the files `monitor.sh`, `gather.sh`, `report-state.sh`, and `prun_state.py` inspect:

- `tail`: raw `streaming-json` NDJSON events written and flushed line by line as they arrive.
- `result-file`, `dispatch-pid`, `timestamp`, `pre-mtime`, `python-interpreter`, `worker-pid-unverified`.
- `grok-version`: output of `grok --version`.
- `session-id` and `conversation-id`: session identifier from the stream.
- `cost-usd`: list-price USD reported by the `end` event's `total_cost_usd`, or `unknown` when omitted or marked partial.
- `model` and `model-actual`: requested model and `requested=<model> <effort> actual=<model> <effort> status=<ok|mismatch|unknown>` verified via `agent_models.py`'s `grok_actual` (reading `modelUsage` from the `end` event and `reasoning_effort` from `<GROK_HOME or ~/.grok>/sessions/*/<sessionId>/summary.json`). `MODEL-MISMATCH` or `MODEL-UNVERIFIED` is printed on stderr when the report differs or omits a field.

Text accumulation buffers `text` events and resets the buffer at every `tool_call` event so narration between tool calls is excluded from the final response. If nothing follows the last `tool_call`, full-stream text is used as the fallback. Unknown event types update `tail` liveness only.

A unit succeeds only when the stream contains an `end` event with `stopReason == "end_turn"` and the process exits `0`. Any `error` event, missing `end` event, or other `stopReason` (`max_tokens`, `max_turn_requests`, `refusal`, `cancelled`) fails the unit (exit `70`) and writes a `FALLBACK` result file whose first line matches `^# [A-Za-z0-9_-]+ result \(FALLBACK, `. If the worker wrote a non-empty `--result-file` itself during a successful run, the dispatcher keeps that file and writes the final response beside it as `<stem>.response<suffix>`.

## Child Environment and Authentication Scrubbing

Every Grok worker runs with these isolation variables set in its environment:

- `GROK_FOLDER_TRUST=0` (avoids mutating `~/.grok/trusted_folders.toml`)
- `GROK_CLAUDE_HOOKS_ENABLED=false`
- `GROK_CLAUDE_MCPS_ENABLED=false`
- `GROK_CLAUDE_SKILLS_ENABLED=false`
- `GROK_CURSOR_HOOKS_ENABLED=false`
- `GROK_CURSOR_MCPS_ENABLED=false`
- `GROK_DISABLE_AUTOUPDATER=1`
- `GIT_PAGER=cat`

`worker_env()` strips secret-shaped environment variables (`*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*PASSWD*`, `*CREDENTIAL*`, `*APIKEY*`, `*AUTH*`, `AWS_*`) while preserving `ANTIGRAVITY_*`, `GROK_AUTH_PATH`, and `GROK_HOME` (which are filesystem paths rather than secrets). Withheld variable names are listed in `<state-dir>/env-withheld`.

Because `XAI_API_KEY` matches `*KEY*`, it is scrubbed by default so `prun` units use the local OAuth login (`<GROK_HOME or ~/.grok>/auth.json` or `GROK_AUTH_PATH`). To pass an API key intentionally to a worker, set `PRUN_KEEP_ENV=XAI_API_KEY`. Preflight checks the final child environment before launching `grok`: a missing binary exits `127`, a missing registry row or missing login exits `70` (instructing the user to run `grok login` or pass `PRUN_KEEP_ENV=XAI_API_KEY`), and an authentication or quota error during the run fails without retry or fallback to another backend.

Additional environment variables:

- `GROK_BIN`: path or name of the `grok` executable.
- `GROK_DISPATCH_MODEL`, `GROK_DISPATCH_EFFORT`: validated overrides for `-m` and `--effort`.
- `PRUN_STALL_THRESHOLD`: seconds without `tail` growth before the idle reaper terminates the process tree (default `600`, exit `124`).
- `GROK_DISPATCH_TIMEOUT_SECONDS`: absolute wall-clock timeout before terminating the process tree (default `2700`, exit `124`).

## Weekly Budget Guard

When `GROK_WEEKLY_BUDGET_USD` is set to a non-negative number, the dispatcher sums `turns[].costUsdTicks / 1e10` for all turns whose `endedAt` timestamp falls within the last 7 days across `<GROK_HOME or ~/.grok>/sessions/*/*/usage.json` (using file `mtime` only to skip files older than 7 days). When the 7-day local list-price spend has reached or exceeded `GROK_WEEKLY_BUDGET_USD`, the dispatcher refuses to launch, writes a `FALLBACK` result, and exits `75` naming the budget and the spend. When `GROK_WEEKLY_BUDGET_USD` is unset or empty, no cap is applied. A value that is set but is not a non-negative number refuses the launch the same way, so a mistyped cap never becomes no cap. Zero refuses every launch.

## Automatic Launch Route

Claude Code's auto mode refused a direct `grok --always-approve` launch, and its classifier judges each dispatcher command too. To run Grok units unattended on a maintainer machine, add an `autoMode.allow` entry describing `dispatch-task-grok.py` (and `dispatch-grok.py` for `/vet`) to the machine-local `~/.claude/settings.json`, after `"$defaults"`; [the implement-review Grok reference](../../implement-review/references/grok-backend.md) shows one. A permission allow rule does not do this job: it does not match past a leading `PRUN_SCRATCH_CWD=` assignment, and auto mode suspends interpreter wildcards such as `Bash(python3 *dispatch-task-grok.py *)`. Never allow `grok` itself, and never put the entry in the repository's shared `user/settings.json`. Without the entry, auto mode may refuse the unit.
