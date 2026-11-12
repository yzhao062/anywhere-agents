# Grok Reviewer Backend (`dispatch-grok.py`)

This reference documents the opt-in Grok CLI backend for `/vet` (`skills/implement-review/scripts/dispatch-grok.py`).

## Role and Opt-In Scope

Grok is an explicit opt-in reviewer selected via `/vet grok` (or `/implement-review grok`, or `IMPLEMENT_REVIEW_DEFAULT_REVIEWER=grok` when a project or user sets it), or via `all` (`/vet all`, `/implement-review all`, or `IMPLEMENT_REVIEW_DEFAULT_REVIEWER=all`), which selects the three of Claude, Codex, Agy, and Grok that are not coordinating. Grok is never part of `both` and is never placed in the automatic failover queue. In a multi-reviewer round, a Grok slot that is absent, refused by the budget guard, or failed is a confirmed failure, and the round continues with the other reviewers. A failed Grok review exits with a non-zero status and does not fail over to another paid reviewer backend.

When `IMPLEMENT_REVIEW_ORCHESTRATOR=grok`, `dispatch-grok.py` refuses self-review and exits `2`.

## Invocation Flags and Model Resolution

`dispatch-grok.py` resolves the reviewer model and reasoning effort from `user/agent-models.json` through `scripts/agent_models.py resolve --family grok --role reviewer`. Environment variables `GROK_DISPATCH_MODEL` and `GROK_DISPATCH_EFFORT` override the registry values for a single run and are validated against the allowed identifier pattern and effort list. If the registry has no `families.grok` row and no override supplies the missing value, the dispatcher exits `70` and names the missing `families.grok` entry in `user/agent-models.json`.

The child command is:

```text
<grok-bin> --prompt-file <state-dir>/prompt-relay --cwd <state-dir>/staged-snapshot --always-approve --output-format streaming-json --no-auto-update -m <model> --effort <effort>
```

`<grok-bin>` is resolved in order from `GROK_BIN`, `PATH`, and `<GROK_HOME or ~/.grok>/bin/grok`. When no binary is found, the dispatcher exits `127`.

## Child Environment Isolation

Every Grok child process runs with these environment variables set so the reviewer is isolated from interactive hooks, MCP servers, skills, and updater prompts:

- `GROK_FOLDER_TRUST=0` (grants folder trust for the child process without mutating `~/.grok/trusted_folders.toml`)
- `GROK_CLAUDE_HOOKS_ENABLED=false`
- `GROK_CLAUDE_MCPS_ENABLED=false`
- `GROK_CLAUDE_SKILLS_ENABLED=false`
- `GROK_CURSOR_HOOKS_ENABLED=false`
- `GROK_CURSOR_MCPS_ENABLED=false`
- `GROK_DISABLE_AUTOUPDATER=1`
- `GIT_PAGER=cat`

## Preflight and Weekly Budget Guard

Before creating the state directory or spawning Grok, `dispatch-grok.py` runs three non-spending checks:

1. **Binary check**: verifies `<grok-bin>` exists and is executable (exit `127` when missing).
2. **Authentication check**: verifies that `XAI_API_KEY` is set and non-empty, `GROK_AUTH_PATH` points to a non-empty file, or `<GROK_HOME or ~/.grok>/auth.json` exists and is non-empty. When none is present, the dispatcher exits `70` and instructs the user to run `grok login`.
3. **Rolling 7-day budget guard (`GROK_WEEKLY_BUDGET_USD`)**: when `GROK_WEEKLY_BUDGET_USD` is set to a finite non-negative number, `dispatch-grok.py` scans `<GROK_HOME or ~/.grok>/sessions/*/*/usage.json` (using file `mtime` only to skip files untouched in the last 7 days) and sums `turns[].costUsdTicks / 1e10` for every turn whose `endedAt` timestamp falls within the last 7 days. If the 7-day total is greater than or equal to `GROK_WEEKLY_BUDGET_USD`, the dispatcher refuses before launching and exits `75` with a message naming both the budget and the current spend. When `GROK_WEEKLY_BUDGET_USD` is unset or empty, no cap applies. A value that is set but is not a non-negative number refuses the dispatch with exit `75` and a message naming the variable, so a mistyped cap never becomes no cap. Zero refuses every dispatch.

## Staged Snapshot and Supplied Inputs

Because `--always-approve` authorizes tool calls inside `--cwd`, `dispatch-grok.py` never runs Grok in the caller's working tree:

1. Unless `IMPLEMENT_REVIEW_ALLOW_STAGED_DRIFT=1`, the dispatcher verifies that no staged path also has unstaged edits in the working tree (exiting `2` on drift).
2. It exports the Git index to `<state-dir>/staged-snapshot` via `git -c core.longpaths=true checkout-index -a --prefix=<state-dir>/staged-snapshot/` and captures `git diff --cached --no-ext-diff`.
3. It runs Grok with `--cwd <state-dir>/staged-snapshot`. The original worktree and Git index remain unchanged.

The relay prompt at `<state-dir>/prompt-relay` wraps the caller's prompt with:

- The snapshot path and the target review filename (`Review-Grok.md`).
- The dispatched-reviewer exemption from `AGENTS.md` bootstrap, session-start banner, and router or coordinator-skill discovery.
- Supplied project rule files under `--- SUPPLIED INPUT: <path> ---` headings when not already present in the caller's prompt:
  - `AGENTS.md` is supplied from the working tree when it exists there as a file and is absent from the exported index (as in a consumer repository that gitignores generated rule files). When `AGENTS.md` is tracked in the index, it is already present in the snapshot and is not duplicated in the relay prompt.
  - `AGENTS.local.md` is always supplied when it exists (reading the snapshot copy when tracked in the index, or the working-tree copy when untracked), because Grok does not auto-discover `AGENTS.local.md`.
  - Nonexistent files are omitted. The relay header states that local overrides in `AGENTS.local.md` win over shared rules in `AGENTS.md` and that supplied files must not be read again from disk.
- The staged diff under `--- STAGED DIFF PROVIDED BY DISPATCHER ---`.

## Streaming Contract and State-Directory Records

On startup, `dispatch-grok.py` creates `<tmp>/implement-review-grok-<repo-hash>-round<N>-<pid>-<nonce>` and prints a single stdout line:

```text
STATE-DIR <abs-path>
```

Inside `<state-dir>`, the dispatcher maintains:

- `timestamp` and `pre-mtime`: integer epoch seconds matching the contract read by `health-check.py`, `await-review.py`, and `auto-watch`.
- `grok-version`: output of `<grok-bin> --version`.
- `tail`: live stream of NDJSON lines written and flushed as each stdout line arrives from Grok, followed by any stderr output.
- `cost-usd`: the `end` event's `total_cost_usd` value, or `unknown` when `total_cost_usd` is absent or marked partial (`cost_is_partial` or `usage_is_incomplete`). Missing cost is never recorded as zero.
- `review-model`: two-line record (`model=<M> source=<env|registry> effort=<E> effort_source=<env|registry>` followed by `actual=<model> <effort> status=<ok|mismatch|unknown>`). Actual model and effort are verified through `scripts/agent_models.py` (`grok_actual`), which reads the `end` event's `modelUsage` key from `tail` and `reasoning_effort` from `<GROK_HOME or ~/.grok>/sessions/*/<sessionId>/summary.json`, emitting `MODEL-MISMATCH` or `MODEL-UNVERIFIED` on stderr when the reported values differ or cannot be confirmed.

While reading the `streaming-json` output:

- `text` events append to a post-tool buffer and a full-stream fallback buffer.
- Every `tool_call` event clears the post-tool buffer so narration emitted before tool calls is excluded from the extracted review text. When nothing follows the last `tool_call`, the full-stream text is used as a fallback.
- Other event types (`thought`, `tool_call_update`, `usage`, and unknown future event types) update `tail` liveness only.
- A run succeeds only when Grok emits an `end` event with `stopReason == "end_turn"` and exits with code `0`. Any `error` event, missing `end` event, non-zero exit code, or non-`end_turn` stop reason (`max_tokens`, `max_turn_requests`, `refusal`, `cancelled`) fails the run with exit `70`.

After a clean `end_turn` exit, the dispatcher checks `<state-dir>/staged-snapshot/Review-Grok.md`. If Grok did not write the file directly in the snapshot, the dispatcher writes the extracted response text to `<state-dir>/staged-snapshot/Review-Grok.md`. It validates the candidate (at least 500 bytes, first line `<!-- Round <N> -->`, and `mtime` newer than dispatch start) and publishes it atomically to `<repo>/Review-Grok.md`, bumping `mtime` past `pre-mtime` when needed. Validation failure leaves `<repo>/Review-Grok.md` untouched and exits `70`.

## Limits and Stall Watch

- `GROK_DISPATCH_TIMEOUT_SECONDS` sets the absolute wall-clock limit (default `2700` seconds). Because Grok emits `streaming-json` events throughout tool calls, active runs that exceed 11 minutes continue uninterrupted up to the 2700-second ceiling. When the ceiling expires, `dispatch-grok.py` sends `SIGTERM` to the Grok process group and any descendant processes, waits a short grace period (`GROK_DISPATCH_KILL_GRACE_SECONDS`, default `2` seconds), escalates to `SIGKILL`, and exits `70`.
- `stall-watch.sh` runs in the background as a warning-only monitor (`IMPLEMENT_REVIEW_STALL_SECONDS`, default `600` seconds of `tail` silence) and writes `<state-dir>/stall-warning` without terminating the review process.

## Launch Route

Because `user/settings.json` does not pre-authorize paid third-party CLIs for all consumers, `/vet grok` and `/vet all` use the following launch route under Claude Code:

- In auto mode the classifier judges each command, and it refused a direct `grok --always-approve` launch. An `autoMode.allow` entry in the maintainer's own `~/.claude/settings.json` that describes the two Grok dispatchers lets it run them unattended. Keep `"$defaults"` in the list, because a list without it drops the built-in allow rules. Claude Code reads `autoMode` from user settings, managed settings, and inline settings passed with `--settings` or the Agent SDK, never from a project's `.claude/settings.json` or `.claude/settings.local.json`.
- A permission allow rule does not do this job. Auto mode suspends interpreter wildcards such as `Bash(python3 *dispatch-grok.py *)`, a pattern that would also match `python3 -c "<code>" dispatch-grok.py`, and an allow rule does not match past a leading assignment such as `PRUN_SCRATCH_CWD=`. Never allow the `grok` binary itself, and never put either setting in the shared `user/settings.json`.
- Without the entry, auto mode may refuse the launch; other permission modes apply the user's ordinary rules.

An entry of this shape works on the maintainer's machine. Describe only what the dispatchers enforce: the spend check runs only when `GROK_WEEKLY_BUDGET_USD` is set, and `dispatch-task-grok.py` does not check that a `PRUN_SCRATCH_CWD` path is disposable.

```json
"autoMode": {
  "allow": [
    "$defaults",
    "Running the anywhere-agents Grok dispatchers with python or python3 is allowed: dispatch-grok.py (a /vet review) and dispatch-task-grok.py (a prun unit), from a source repository's skills/ directory or any project's .claude/skills or .agent-config/repo/skills copy, including with leading PRUN_SCRATCH_CWD= or IMPLEMENT_REVIEW_ORCHESTRATOR= assignments. dispatch-grok.py keeps grok inside an exported index snapshot; dispatch-task-grok.py runs it in a temporary work directory, or in PRUN_SCRATCH_CWD when the caller sets one. Both switch off the user's hooks and MCP servers, and both refuse to launch once the last 7 days' Grok spend reaches GROK_WEEKLY_BUDGET_USD when that variable is set."
  ]
}
```
