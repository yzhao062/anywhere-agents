# Agy Dispatcher Reference: Models, Quota, and Sandbox

Read this when you are about to override `ANTIGRAVITY_DISPATCH_MODEL`, `ANTIGRAVITY_DISPATCH_EFFORT`, or `ANTIGRAVITY_PREFLIGHT`, when a `MODEL-*` line appears, a dispatch exits 75, or a unit exits 124, when the sandbox (Agy's `--sandbox` flag) needs changing, or when auto mode refuses a dispatch.

- The Agy dispatcher (`dispatch-task-agy.py`) defaults to the registry's `prun-worker` row for Agy: stem plus effort tier gives the slug, and
  `second_model` gives the second group's slug. `DEFAULT_MODEL`, `DEFAULT_EFFORT`, and
  `SECOND_MODEL` come from `scripts/agent_models.py` at start, so a registry change needs no script
  edit. They are family templates: preflight takes the newest version in each family from the
  `agy models` listing, the tier first (the template's own, then `high`) and the version second, so
  an older high beats a newer medium. An unset `ANTIGRAVITY_DISPATCH_EFFORT` follows the chosen
  slug's tier, because Agy rejects a conflicting `--effort`. The switch shows as a `MODEL-RESOLVE
  from=... to=...` line on stderr and in `<state-dir>/quota-note`, and `<state-dir>/model` names the
  model sent to Agy. Quota routing picks the group first, and floating never moves a unit to the
  other group, with one exception: when Agy lists nothing in the second model's family, a unit
  routed there runs on the worker default, recorded as `MODEL-FALLBACK ...
  reason=template-unlisted`. The Gemini group's quota is checked again first, so a fallback into an
  empty group exits `75` without launching. A model named in `ANTIGRAVITY_DISPATCH_MODEL` runs
  verbatim, and `ANTIGRAVITY_PREFLIGHT=off` skips the listing, so the templates run as written.
  `ANTIGRAVITY_DISPATCH_EFFORT` moves the registry's slugs for both groups to that tier (the
  Claude and GPT group still gets no `--effort` flag and takes the tier from its slug). A tier
  the family does not list floats to `high`, and the Gemini `--effort` follows the floated
  slug. The dispatcher passes `--effort` for the Gemini models only: Agy rejected the flag for
  the older Claude models, and since 1.2.16 it rejects any effort that conflicts with the tier
  in a Claude slug, so the second group runs without it.
- **The dispatcher checks which model Agy really ran.** Every unit passes
  `--log-file <state-dir>/agy.log`, and after the run the resolver's Agy reader takes the model
  label Agy propagated to its backend from it (the earlier `not in local config` line is noise).
  `<state-dir>/model-actual` records
  `requested=<slug> <effort> actual=<slug> <tier> status=ok|mismatch|unknown`. Family and tier must
  agree; a newer version is fine, and a second-group slug is held to the tier it names, since it
  takes no `--effort`. A mismatch prints `MODEL-MISMATCH` on stderr, and a missing label prints
  `MODEL-UNVERIFIED`. Neither changes the exit code or the result, and no model call is spent.
- Agy Ultra exposes a second quota group for Claude and GPT-OSS models, metered apart from the
  Gemini group. **A unit that names no model goes to whichever group has the freer meter**, with
  the registry's `second_model` (floated to its newest version) as the second group's model; a row
  without one leaves the group out of the routing. The worker is the Agy CLI either way, so a
  Claude model here spends Agy quota and never the coordinator's Claude account.
  `ANTIGRAVITY_DISPATCH_MODEL` disables headroom balancing for the run; the exhaustion rules below
  still apply to the model it names, including the fallback from an exhausted Claude and GPT group
  to Gemini.
- **The dispatcher checks group quota before launching.** The two groups are
  metered separately, and one dispatch names one model, so a batch aimed at an
  empty group fails once per unit. Before launching, the dispatcher reads the snapshot `agent-quota`
  maintains and decides:
  - No model was named: each unit starts from the worker default. When both
    groups are reported, it moves to the second group's model if the second group's
    lowest remaining fraction is at least 15 points higher, or if Gemini is
    empty and the second group has quota left. A move on headroom also needs
    both metered windows of the destination present in the snapshot, since a
    group entry is its emptiest bucket and an unreported window may be the
    empty one. An empty own group moves the unit without that evidence, because
    the alternative is not running at all. Units decide independently, so
    successive readings can switch the group a batch is using. The line
    `MODEL-BALANCE from=... to=... reason=freer-meter own=... other=...` goes to
    stderr and to `<state-dir>/quota-note`.
  - A named model in the Claude and GPT group, with that group empty and Gemini
    not: dispatch the worker default instead, and record the swap. The line
    `MODEL-FALLBACK from=... to=... reason=claude-and-gpt-quota-exhausted
    resets=...` goes to stderr and to `<state-dir>/quota-note`.
    `<state-dir>/model` names the model sent to Agy, not the one the caller asked for.
  - A named Gemini model whose group is empty: exit `75` without launching, and
    say so. A model someone chose is not escalated into the metered group on its
    own; the message names `ANTIGRAVITY_DISPATCH_MODEL` for the operator who
    wants that.
  - Both groups are empty: exit `75` with both reset times.
  - A group the snapshot does not report is unknown rather than empty, and an
    unreadable snapshot skips the check entirely. The gate stops a dispatch
    only into a group it read as empty. `PRUN_AGY_QUOTA_GATE=off` disables it.
  A run that fails at the backend forces a snapshot refresh before exiting,
  past the readout's own five-minute TTL, because the meter it just hit is
  newer evidence than the snapshot. Later units then route on what it recorded.
  A refresh that cannot run, an unavailable meter, and units already in flight can still
  produce repeated quota errors.
- `PRUN_AGY_SANDBOX` controls whether Agy's `--sandbox` flag is added (the dispatcher omits it by
  default); it does not disable a sandbox enabled in Agy's own settings (`enableTerminalSandbox`).
  Accepted values are
  `1`/`true`/`yes`/`on` to add the flag and `0`/`false`/`no`/`off`, empty, or unset to omit it.
  Values ignore case and surrounding whitespace; anything else exits 2 before state creation or
  launch. Scratch directories and throwaway clones reduce accidental changes to the working
  repository. They do not enforce filesystem or network isolation; the worker must follow the
  prompt's ban on commit, push, and destructive git.
- **Idle reaper (`PRUN_STALL_THRESHOLD`).** When `PRUN_STALL_THRESHOLD` seconds (default 600) pass without stdout or stderr output, the dispatcher sends SIGTERM and then SIGKILL (after 2 s) to the Agy process and its descendants. The unit exits 124, writes a `FALLBACK` (a result the worker already wrote is kept), and appends a `[reaper]` line to the tail. The conversation id is kept, so `--continue-from` can resume the unit. Set `PRUN_STALL_THRESHOLD` on the dispatch command, not only on `monitor.sh`, for units that run a single command longer than ten minutes. `ANTIGRAVITY_DISPATCH_TIMEOUT_SECONDS` / `--print-timeout` stays the wall-clock cap.

## Automatic Launch Route

Claude Code's auto-mode classifier judges each dispatcher command, and in two sessions it refused `dispatch-task-agy.py` and `dispatch-task.sh` with `[Unauthorized Access]` while it let the Grok dispatcher through, because the machine's `autoMode.allow` entry named only the Grok dispatchers. To run Agy units, and Codex units the user names, unattended under auto mode, describe `dispatch-task-agy.py` and `dispatch-task.sh` in the same machine-local `~/.claude/settings.json` entry, after `"$defaults"`. `references/grok-executor.md` gives the limits that apply to any such entry, and `skills/implement-review/references/grok-backend.md` shows its shape. Without the entry, auto mode may refuse the unit; then tell the user, who can run the dispatch command with a `!` prefix or add the entry.
