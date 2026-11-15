# Codex Executor Reference (`dispatch-task.sh`)

Read this when the user names Codex for a unit or a batch.

## dispatch-task usage (Codex, only when the user names it)

```
scripts/dispatch-task.sh --prompt-file <prompt> --result-file <fresh abs result> --unit-id <id>
```

It takes only these three arguments, so a Codex unit gets no follow-up turn. Result publishing,
exit codes, and `STATE-DIR` match Agy, so `monitor` and `gather` work; a `FALLBACK` holds the final
message and the last 200 tail lines. Ahead of the unit prompt, the dispatcher sends a worker
preamble: skip the bootstrap, banner, session-start, and router steps of any rule file, never commit
or push, and return the `# <unit-id> result` block of the return contract. There is no quota
routing; a Codex unit spends the Codex plan.

- **Model and effort come from the registry.** The `prun-worker` row for the Codex family names both
  (`agent_models.py resolve --family codex --role prun-worker`), and the dispatcher passes both with
  `-c`. `CODEX_DISPATCH_MODEL` and `CODEX_DISPATCH_REASONING` override one run. With neither an
  override nor a registry answer it exits `70` and names the registry; it never guesses.
- **Which model really ran.** `<state-dir>/model-actual` has the Agy record's format
  (`requested=<slug> <effort> actual=<slug> <tier> status=ok|mismatch|unknown`, described in
  `agy-quota-and-models.md`), read from the header `codex exec` prints (`actual=-` when there is none). `MODEL-MISMATCH` and `MODEL-UNVERIFIED`
  go to stderr; the exit code stays the worker's.
- `codex exec` runs from the scratch dir (`PRUN_SCRATCH_CWD` for a clone) with `--sandbox
  danger-full-access` (`CODEX_DISPATCH_SANDBOX`), the preamble and prompt on stdin, and
  `--ignore-user-config` so a user MCP server cannot start inside the worker
  (`CODEX_DISPATCH_ISOLATE_MCP=off` drops it and passes no model or effort).
- No output growth for `PRUN_STALL_THRESHOLD` seconds (default 600), or a run past
  `CODEX_DISPATCH_TIMEOUT` (default off), reaps the worker: exit `124` and a `FALLBACK` result.
