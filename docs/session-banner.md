# Session Banner

Every session opens with a seven-line banner that says which agent versions are running, which skills and hooks are active, and whether the last refresh left anything to fix. Since the 2026-09 rewrite of `AGENTS.md`, a script computes the banner and the agent prints it; the rule in `AGENTS.md` § "Session Start Check" says when. This page covers the format, how each field is derived, the report file, the acceptance rule, and the fallback.

## Format

```text
📦 anywhere-agents active
   ├── OS: win32
   ├── Claude Code: 2.1.275 → 2.1.280 (auto-update: on) · <model> · effort=xhigh
   ├── Codex: 0.155.0 · gpt-6-astra · xhigh · standard · fast_mode=false
   ├── Skills: 1 local (paper-tools) + 2 pack (bibref-filler, dual-pass-workflow) + 6 shared (ci-mockup-figure, editable-figure, implement-review, my-router, prun, readme-polish)
   ├── Hooks: PreToolUse guard.py (agent-style 0.7.0), SessionStart session_bootstrap.py
   └── Session check: all clear
```

The agent replaces `<model>` with its own model id when it prints the banner, because no hook input carries the model reliably. The ` → <latest>` arrow appears only when a newer version is known. The check line reads `all clear` or a semicolon-separated list of issues, each actionable in one clause.

## Where the Fields Come From

`scripts/render_banner.py` computes every field from disk, with one helper beside it, `scripts/pack_identity.py`, for the pack counts. Each collector fails on its own: an unreadable file or a probe that times out degrades one field and leaves the rest intact.

| Row | Source |
|---|---|
| OS | `sys.platform` (`win32`, `darwin`, `linux`). |
| Claude Code | `claude --version` with a three-second timeout (`unknown` on timeout, `not installed` when the binary is absent). Latest from `~/.claude/hooks/version-cache.json`, which the SessionStart hook refreshes from the npm registry at most once a day. Auto-update is `on` unless `DISABLE_AUTOUPDATER` is set in the environment or the `env` block of `~/.claude/settings.json`, or `~/.claude.json` carries `"autoUpdates": false`; a missing key means `on`, because a native install updates by default. Effort is `CLAUDE_CODE_EFFORT_LEVEL` from the environment or that `env` block, then the persisted `effortLevel` (`.claude/settings.local.json`, `.claude/settings.json`, `~/.claude/settings.json`), then `default`. |
| Codex | `codex --version` with the same timeout; latest from the same cache. The four config keys come from `~/.codex/config.toml` (`model`, `model_reasoning_effort`, `service_tier`, `[features] fast_mode`), read with `tomllib` on Python 3.11 and later and with a minimal line parser before that. A missing binary renders `not installed`, a missing file `not configured`, and a file `tomllib` rejects `config.toml unreadable`, so a broken configuration is never repaired into a clean row. |
| Skills | Directory names under `skills/` (local), `.claude/skills/` (pack-deployed), and `.agent-config/repo/skills/` (shared), with the lookup order applied: a pack name shadowed by a local skill is dropped, and a shared name shadowed by either is dropped. Empty buckets are omitted. |
| Hooks | Whether `~/.claude/hooks/guard.py` and `~/.claude/hooks/session_bootstrap.py` exist, and which `agent_style` release the rendering interpreter imports. In a Claude Code consumer session, the SessionStart hook renders the banner with the interpreter that the guard also resolves through `~/.claude/hooks/_python`. There the release shown is the one the guard runs. A render from a source repository, a standalone bootstrap, or a Codex session reports the interpreter that rendered it. `no agent-style` means that interpreter has none; `agent-style advisory off` means `AGENT_STYLE_HOOK` or the legacy `AGENT_CONFIG_GATES` disables the advisory. |
| Session check | The issues below, or `all clear`. |

The check line combines, in this order:

1. The bootstrap ledger, `.agent-config/last-run.json`, in a consumer. A nonzero bootstrap exit reads `bootstrap exited N at <phase>`; a ledger with `completed: false` reads `bootstrap incomplete: stopped at <phase>`; each failed or skipped step is named with its reason and return code (`compose skipped (no PyYAML)`). A missing ledger reads `checks unavailable (no .agent-config/last-run.json)`. A zero exit alone never yields `all clear`.
2. A missing hook, and a Claude effort other than `xhigh` or `max`. An `agent_style` package that is missing from the guard's interpreter, or older than the release the loaded rules come from, is named with that interpreter and the `pip install` command that fixes it. The expected release is the version recorded in the composed `AGENTS.md` block of a consumer, or the bundled default in `bootstrap/packs.yaml` of a source repository. A consumer without the agent-style block, or a ref that is not a whole stable release (`main`, `v0.8.0rc1`, `v0.7.0-dev`), expects nothing. On Windows, a path with a space carries PowerShell's call operator (`& "C:\...\python.exe"`), so the printed command runs. The guard gates its rule set on the installed release, so an old package narrows the advisory without any error; this check makes that visible after an agent-style release.
3. Codex drift that is actionable: a model older than the GPT-5.6 family, or a CLI below the floor of the configured model generation. The floors are 0.144.0 for GPT-5.6 and 0.150.0 for GPT-6. A `config.toml` that is not valid TOML is named as such. A `project_doc_max_bytes` below 262144 is flagged with the value to set (see [Codex](codex.md)). The model, tier, and `fast_mode` values themselves are reported, never flagged.
4. GitHub Actions pins in `.github/workflows/*.yml` below the minimums in `AGENTS.md` § "Environment" (`actions/checkout@v5`, `actions/setup-python@v6`, `actions/setup-node@v5`, `actions/upload-artifact@v6`, `actions/download-artifact@v7`), each with its file, line, and the version to bump to. A pin by commit SHA is listed for manual review rather than compared.
5. Pack counts: `⚠ N user-level pack(s) not deployed` and `ℹ N pack update(s) available`, both with the `anywhere-agents pack verify --fix` reroute, or `pack checks unavailable (<reason>)`.

## Pack Counts

`pack_identity.py` is a read-only calculation that never fetches. It builds two sets and compares them.

The user-level set is the `packs:` list of the user config (`%APPDATA%\anywhere-agents\config.yaml` on Windows, `$XDG_CONFIG_HOME/anywhere-agents/config.yaml` or `~/.config/anywhere-agents/config.yaml` elsewhere); an absent file is an empty list, and `AGENT_CONFIG_PACKS` is ignored. The project-level set starts from the host seeds (`agent-style`, plus `aa-core-skills` under Claude Code; `AGENT_CONFIG_HOST` selects the host, and an unset or unknown value means `claude-code`), then applies `agent-config.yaml` and `agent-config.local.yaml` in that order. Each layer replaces earlier entries of the same name, seeds included, and an explicit empty or null `packs:` clears everything accumulated so far. A seeded default, or a project entry that names a bundled default without a `source`, takes the identity recorded for it in `.agent-config/pack-lock.json` (`source_url`, `requested_ref`). With no lock entry, the identity comes from `.agent-config/repo/bootstrap/packs.yaml`, and a manifest entry without a `source` is `bundled:aa`. That is the same seeding `anywhere-agents pack verify` performs, which is why a user-level `agent-style` row does not count as a gap in every project that relies on the bundled default.

The gap count is the number of user-level entries with no project entry of the same name, or whose normalized `(name, url, ref)` tuple differs. The update count is the number of lock entries whose `latest_known_head` and `resolved_commit` are both present and differ. Missing evidence is reported rather than counted as zero. A missing PyYAML, malformed YAML, a malformed row, or an absent lock makes the corresponding check `unavailable`, with the reason in the check line.

## The Report File

In a consumer the renderer publishes one file, `<root>/.agent-config/banner.txt`, written to a temporary name and renamed into place, so a reader never sees a partial report. Its first line is a metadata comment, and the seven banner lines follow:

```text
<!-- anywhere-agents banner event_ts=1789690000.0 run_id=20260917T180203Z-4f2a completed=true rendered_at=2026-09-17T18:02:05Z -->
📦 anywhere-agents active
   ├── OS: linux
   ...
```

`event_ts` is the timestamp the hook passed in. A standalone render takes the timestamp in `session-event.json` when that file exists, and writes `none` otherwise. `run_id` comes from the ledger the same run finalized. `completed` is the ledger's value, except that a nonzero bootstrap return code forces it to `false`. A run the wheel's heal pass could not recover therefore never publishes a report that claims completion. The report is written after every refresh, successful, failed, or degraded:

- `bootstrap.sh` renders from an `EXIT` trap installed after the ledger is initialized, so the exit status is preserved and a failed run still publishes a report that names the failure.
- `bootstrap.ps1` renders at its exit sites. Those are the git preflight, a composer failure, a helper deployment that could not replace a file another process holds open, and the normal end of the run. A `try/finally` around the whole script was rejected. Inside a script-scope `try`, a .NET exception stops the script instead of printing and continuing, which changed the error semantics of every statement.
- The SessionStart hook (`scripts/session_bootstrap.py`) runs bootstrap with the consumer root as its working directory. It renders after the attempt with the event timestamp it wrote and the bootstrap return code. The seven lines go to stdout, so Claude Code injects them as context, and the hook returns the bootstrap code unchanged.
- The `anywhere-agents` command renders once more from the copy vendored in the wheel, after its `pack verify --fix` heal pass. The report then describes the state the command leaves behind, including a project whose clone predates the renderer.

In a source repo (`anywhere-agents` or `agent-config`) the renderer prints the seven lines to stdout and creates nothing: `python scripts/render_banner.py --root .`. In a consumer, `python .agent-config/repo/scripts/render_banner.py --root . --stdout` prints the current report without a refresh. An unrelated directory exits 2.

## The Acceptance Rule

A report is accepted only when its metadata matches the session that is reading it. In Claude Code, that means `event_ts` equals the pending event's timestamp and `run_id` equals the `run_id` in the current `last-run.json`. For Codex and other agents that run bootstrap per invocation, it means the `run_id` of the attempt that just completed. A failed attempt selects the fallback regardless of any report on disk. Freshness is decided from content, never from a file's modification time.

When the report is missing, unparseable, or mismatched, the agent prints this fixed fallback instead and acknowledges it the same way:

```text
📦 anywhere-agents active
   ├── Agent: <model>
   └── Session check: checks unavailable (run bootstrap or read .agent-config/last-run.json)
```

The rule keeps an old all-clear report from serving a failed refresh. A direct `bootstrap.sh` run that fails writes a new ledger with a new `run_id`, and a report from the earlier successful run no longer matches it. The first session after an upgrade is the same case in the other direction. It may load the new rule before any report in the new format exists, and the fallback covers it.

## The Claude Code Branch

`session_bootstrap.py` writes `.agent-config/session-event.json` when the hook fires with source `startup` or `clear`, the two events that open a fresh context with a first reply to print the banner on. On `compact` and `resume` the conversation continues, the earlier acknowledgement still stands, and the hook skips the write so the banner does not re-fire. One resume is the exception: the one that finds no usable event and no acknowledgement. A repository can become a consumer while its conversation is closed, and the first hook fire it then sees is a resume. That resume writes the event, so the gate's first arm can still require the banner once. A ten-second debounce suppresses a duplicate write when the hook fires twice for one event, and the event keeps its original timestamp so the report and the event agree. The files are per project, so two Claude Code windows in different consumers do not interfere.

Before the first content of a reply, the agent walks up from the working directory to the directory that holds `.agent-config/bootstrap.sh` or `bootstrap.ps1`. When `session-event.json` is newer than `banner-emitted.json`, or no acknowledgement exists, it reads `banner.txt`, applies the acceptance rule, prints the banner or the fallback, and copies the event `ts` into `banner-emitted.json`.

`guard.py` enforces the emission. While an event is pending with no acknowledgement file, its banner gate denies every tool call except reads and the acknowledgement write itself. The deny message carries the instruction to print the banner and write the file. The whole first arm therefore completes with a `Read` of the report and one `Write`. A stale acknowledgement passes through with a `[banner-gate]` advisory instead, so a re-fire never blocks work (anywhere-agents#7). Source repos and unrelated directories are not gated. See [Guard hook](guard-hook.md).

## Codex and Other Agents

Codex 0.153.3 and later ship hooks, including `SessionStart`, but this project has not wired them yet (anywhere-agents#50). Codex and other invocation-based agents run bootstrap per the block at the top of `AGENTS.md`, then print the banner on the first reply of the invocation. They never read or write Claude's acknowledgement file. A dispatched reviewer that was told to skip the banner keeps skipping it.

## Tests

- `tests/test_render_banner.py` covers the collectors with a private home directory, the metadata contract, and the acceptance rule. It drives both entry points for real through the preflight fixture: normal completion, a composer failure, and a helper held open by another process. It also checks the console encoding under a Windows ANSI code page.
- `tests/test_session_bootstrap.py` covers the hook lifecycle: startup, resume, clear, compact without a re-fire, and a nested working directory. It also covers the upgrade path with and without the renderer, a failed refresh, and a report for another event or run.
- `tests/test_guard.py` proves the first arm of the banner gate completes with the read and the acknowledgement write alone.
