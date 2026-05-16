---
name: implement-review
description: Review loop for staged changes. Detects content type, prepares a review request for Codex (Terminal-relay manual default, opt-in Auto-terminal codex-exec subprocess, or IDE plugin), categorizes feedback, revises, and iterates. Works for code, papers, proposals, or any text-based output.
---

# Implement-Review

## Overview

A review loop for staged changes. Claude Code detects the content type, sends the changes to one or more reviewers, categorizes the feedback, revises, and iterates. Codex is the primary reviewer via three channels: terminal relay (manual copy-paste, default on every platform), Auto-terminal (opt-in `codex exec` subprocess dispatch), or IDE plugin. Other reviewers (Copilot, Gemini, Claude Code, etc.) are driven ad-hoc by the user through their own UI and only need to honor the `Review-<AgentName>.md` save contract defined in Phase 1c.

## When to plan-review first

**Any complex task benefits from a plan review BEFORE execution**, not only writing or code. Plan-first catches architectural holes while they are still cheap to fix. The scope includes: system design, refactors, paper outlines, proposal structure, data-pipeline redesigns, multi-stage debugging strategies, teaching / curriculum planning, release-process changes, migration plans, and anything else where the shape of the work precedes and constrains the execution.

**Plan-review is a Phase 0 before the staged-change loop below.** If the user asks for a plan review, or if the task clearly meets the signals below, do not apply the staged-change prerequisite in Phase 1 yet. Tell the reviewer to read the plan file directly (or paste the plan contents via the terminal path when the reviewer cannot access the file) and critique the design, not `git diff --cached`. After the plan has no High findings and no new design blockers, execute the work and resume the normal staged-output review flow at Prerequisites / Phase 1.

### When to plan-first

Signals that the round-trip pays off:

- **Blast radius is large** -- multiple files, cross-cutting concerns, shared state, multiple stakeholders, or the organizing structure of a deliverable.
- **Irreversible once executed** -- publishes, submissions, deployments, immutable packages, paper submissions, external commitments.
- **History shows a pattern** -- "got the structure wrong, redo next cycle" has happened on this track before.
- **Uncertainty in the approach** -- the user is weighing alternatives and wants the design validated, not the execution reviewed.
- **Context is unfamiliar** -- new codebase, domain, audience, agency, collaborator workflow, or external constraint set, where a wrong assumption can shape the rest of the work.

### When to skip plan-first

- Change is small, local, reversible.
- The design is already worked out and only execution feedback is wanted.
- Plan and execution would be the same artifact (three-line bug fix, one-sentence footnote).

### Process

1. Write the plan to a scratch file `PLAN-<identifier>.md` in the most natural location for the task (repo root for code, paper-repo root for Overleaf-style docs, a local scratch directory beside the deliverable for tasks that do not live in git). If the plan lands inside a git worktree, add it to `.git/info/exclude` so `git add -A` does not accidentally stage it; outside git, keep it as a clearly named scratch file outside the final deliverable and delete it after review.
2. Content varies by task but at minimum include: purpose, non-goals, structure, regression or failure analysis, validation plan, open questions. Keep it terse -- 1 to 3 pages.
3. Send the plan through a plan-review prompt (not the staged-change template). Make clear this is a pre-execution design review and that the plan file path or pasted contents are what the reviewer should read; instruct the reviewer to critique the design rather than to run `git diff --cached`. Use the normal "Save your complete review to Review-<AgentName>.md" save-contract from Phase 1c.
4. **Reviewer must answer the scope-challenge questions** (see "Adversarial scope challenge" below) before any in-scope correctness review.
5. Iterate until the review has no High findings and no new design blockers.
6. Then execute (code, draft, revise, deploy).
7. Run the normal review cycle on the staged output. It is typically smaller because the architecture was already validated.
8. After the work ships or is submitted, delete the PLAN file.

### Adversarial scope challenge (mandatory in plan-review)

Plan-review **must** be adversarial about plan purpose and shape, **not** about low-probability edge cases. The single biggest plan-review failure mode in this maintainer's history was not "the plan had a bug" — it was "the plan's scope was over-conservative, deferring user value across an extra release cycle worth of process tax." Examples:

- Conservative scope (`v0.5.7 = ref bump only; defer compact to v0.6.0`) → the user's real projects would have stayed on large `AGENTS.md` files for another release cycle. The apparent product ask was a one-line bundled manifest flip, but existing-consumer delivery also required bundled-default drift detection in aa plus real-project upgrade tests. The scope challenge should force reviewers to price both parts: user value now, and the smallest code path that actually delivers it.
- Conservative deferrals also defer the validation that proves the next-step works in real consumers, so the next release inherits the same uncertainty plus a longer review chain.

The plan-review prompt must instruct the reviewer to take an explicit position on:

1. **Why this exact scope?** What user pain (or user opportunity) does THIS scope close? Could a strictly smaller scope close most of it? Could a marginally larger scope close all of it for low marginal cost?
2. **Are deferrals justified?** Every "out of scope / deferred to vNext" carries process tax: another full plan-review + implement + execution-review + CI + publish cycle. Quantify the deferral. If the deferred axis is 1 line of code and the user pain is real, the deferral is probably wrong.
3. **Is this plan the simplest path?** Are there simpler approaches the plan didn't consider — including doing nothing if the user pain is hypothetical?

Plan-review must **not** default to "no blocker, ready to implement" on the first round when scope is multi-axis or when the plan defers a user-facing axis. The reviewer must either raise a scope challenge OR explicitly state why the scope is calibrated. Skipping the explicit position on these three questions counts as an incomplete review.

Anti-patterns the reviewer must NOT pursue:

- **Edge-case fishing**: "what if user has unusual config Y?" — only matters when realistic probability × impact > review cost.
- **Process ritual**: "should this also do Z?" without grounding in user pain or opportunity.
- **Adversarial-for-its-own-sake**: rejecting plans whose scope IS calibrated, or proposing scope expansion that adds cost without proportional user benefit.

The Phase 1c prompt template `Scope-challenge focus:` line below carries this contract into every plan-review send.

### Illustrative examples (not exhaustive; the category is less important than the pattern)

- System / code: hook or infra design, cross-cutting refactor, state-file schema, cross-platform behavior, release runbook revisions.
- Research output: paper outline with specific aims, contribution claims before methods is written, figure-placement vs argument flow, reviewer response strategy, experiment design across multiple methods, ablation plan.
- Proposal: full outline (aims alignment with merit-review criteria), budget-narrative coupling, broader-impacts framing.
- Operational: migration plan, incident-response playbook, data-pipeline redesign.
- Administrative / teaching: course syllabus structure, lab policy document, committee process design.

The point is not which category -- it is whether the shape of the work precedes and constrains the execution.

### Empirical note

In the agent-config 0.1.9 release cycle, two plan-review rounds caught a High-severity design flaw before implementation. The later execution-review rounds were limited to documentation and test polish, avoiding a likely post-ship hotfix.

## Codex Channels

Three paths to Codex are supported. Default is Terminal-relay (manual copy-paste). Auto-terminal (`codex exec` subprocess) is opt-in via the trigger rules in Path selection below. Plugin is user-initiated. The skill picks the channel based on those rules.

### Terminal path (default)

The user has a Codex interactive terminal window open alongside Claude Code. Claude Code prepares a copy-pasteable review prompt (summary, diff, lens, round number) and presents it as a fenced text block. The user copies it into the Codex terminal, then relays the feedback back to Claude Code.

### Auto-terminal path (opt-in, codex exec subprocess)

When the user opts in via the trigger rules in Path selection below, Claude Code dispatches Codex via plain `codex exec` as a Bash subprocess in background. The dispatch script writes the prompt to a per-dispatch temp file under `%TEMP%` / `$TMPDIR`, feeds it on stdin (`codex exec -`), and emits a state-dir path to stdout for Phase 2 to consume. Codex writes its review to `Review-Codex.md` per the save contract; the same Phase 1d auto-watch fires on file appearance.

This channel preserves the byte-identical prompt invariant with Terminal-relay: the assembled prompt bytes are the same regardless of channel. The dispatch script must NOT call `codex exec review`, `codex exec review --uncommitted`, or any other Codex `exec` subcommand, because those carry Codex's own built-in review prompt template which would compete with the skill's lens-aware prompts.

The Auto-terminal path requires `codex` on PATH. Probe before dispatch; if absent, warn and downgrade to Terminal-relay for the round. On non-zero `codex exec` exit, timeout, or stdin-pipe failure (rare on Windows with PowerShell quirks or Bitdefender input-stream interception), downgrade to Terminal-relay for that round; do NOT add a truncated positional-argument fallback, which would break the prompt invariant. Sticky downgrade applies session-wide once Auto-terminal fails: subsequent rounds default to Terminal-relay until the user re-opts-in explicitly.

**Script presence is also required**: if any of the `dispatch-codex` / `health-check` / `stall-watch` scripts for the current platform is missing (e.g., Phase B has not landed yet in a given checkout), this path is documented-only — Phase 1c automatically downgrades to Terminal-relay **without** setting sticky downgrade, because the absence is design state, not a runtime failure. See the "Auto-terminal path (opt-in)" subsection in Phase 1c for the script-presence probe specifics.

### Plugin path (IDE sidebar)

Codex runs as an IDE plugin with direct access to the repo. The user tells Codex to review in the plugin sidebar (e.g., "review the staged changes"). Codex can see the working tree and run `git diff` itself, so no diff needs to be copy-pasted. The user relays Codex's feedback back to Claude Code.

### Path selection

1. **Default**: Terminal-relay (manual copy-paste) on every platform. Users who do not opt into another channel get the current Terminal-relay flow without modification.
2. **Auto-terminal opt-in triggers**, evaluated in detection order at Phase 1c entry:
   - Mid-flow override toward manual: "back to manual" / "next round manual" / "use terminal-relay" in the user's most recent message forces Terminal-relay.
   - Mid-flow opt-in toward auto: "next round use cli" / "next round auto codex" forces Auto-terminal.
   - Slash-arg opt-in: `/implement-review cli`, `/implement-review auto`, and `/implement-review auto-terminal` are equivalent synonyms; `auto` is the most natural keyword and is accepted.
   - Plain-phrase opt-in in the invoking message: case-insensitive substring match on any of `cli mode`, `use cli`, `auto codex`, `use codex exec`, AND no negation word (`do not`, `don't`, `no`, `not`, `avoid`, `manual`) within 4 words before the matched phrase.
3. **Plugin path** is available on every platform when the user initiates it (e.g., "use the plugin") but is never a default. The user can override at any time (e.g., "use the plugin", "use the terminal").
4. **MCP forward-compat slot**: when an MCP-based Codex channel is later added (currently out of scope), it follows the same trigger pattern: `/implement-review mcp` slash arg + analogous plain phrases (`use mcp`, `mcp mode`) under the same negation guard. The trigger UX is a single extensible mechanism across all channels.

Emit one line at Phase 1c entry stating which channel was picked and why (e.g., `Channel: Auto-terminal (triggered by "/implement-review auto"; codex on PATH at C:\...\codex.cmd)`), so the user can immediately override if it picked wrong.

## Prerequisites

At skill start, check for staged changes (`git diff --cached`). If nothing is staged but unstaged or untracked changes exist, list them and ask the user whether to stage all (`git add -A`), stage specific files, or abort. Do not auto-stage without confirmation — untracked files may be sensitive or unrelated. If there are no changes at all, there is nothing to review -- inform the user and stop.

## Pre-Review Checks (optional)

Before sending staged changes for review, run automated checks that catch mechanical issues locally. This lets reviewers focus on content and judgment calls instead of issues a script could find. Skip this phase if the user says to proceed directly, or if the project has no relevant tooling.

| Content type | Checks |
|---|---|
| LaTeX paper or proposal | Compile. Scan the log for overfull/underfull box warnings and undefined references. Report counts. |
| Anonymized submission | Grep staged files for author names, GitHub/lab URLs, institutional names, and tool names. Source these from the project's de-anonymization checklist if one exists; otherwise use the git user name, institution domain, and any names in the paper's author metadata or `\author{}` block. |
| Code | Run the project linter and type checker if configured. |

Report any findings to the user before proceeding to Phase 1. Findings here do not go to the reviewer; fix them locally first.

## Phase 1: Prepare and Send Review

### 1a. Detect content type

Inspect the file extensions in the staged diff to classify the change:

| Extensions | Content type |
|---|---|
| `.py`, `.js`, `.ts`, `.go`, `.rs`, `.java`, `.c`, `.cpp`, `.h`, `.sh`, `.yaml`, `.json`, `.toml` | `code` |
| `.tex`, `.bib` (in a paper or manuscript directory) | `paper` |
| `.tex`, `.bib` (in a proposal or grant directory) | `proposal` |
| `.md`, `.rst`, `.txt` (in a proposal or grant directory) | `proposal` |
| Everything else or mixed | `general` |

If the diff spans multiple types, pick the dominant one. The user can override by saying, e.g., "review this as a proposal." For proposals, also ask which agency lens to apply (NSF or NIH) since they use different evaluation frameworks.

### 1b. Build the review context

Prepare a review request with:

1. **Summary** -- one to three sentences on what changed and why.
2. **Diff scope** -- list the changed files. Always tell the reviewer to run `git diff --cached` itself. Do not paste the diff inline; this keeps the prompt compact and avoids bloat across rounds.
3. **Review lens** -- the content-type-specific criteria from [references/review-lenses.md](references/review-lenses.md). If a focused sub-lens or agency-specific lens fits better than the full lens, use it (e.g., `paper/formatting` for a layout-only change, `proposal/nsf` when the agency is known). See the lens tables in that file.
4. **Additional focus** -- specific concerns beyond the generic lens. This is often the highest-value part of the prompt because it catches real bugs that generic criteria miss. **Always ask the user explicitly rather than guessing.** Recurring project concerns belong here: phased-development coupling, anonymization checks, page-limit compliance, budget-to-narrative consistency, terminology drift, benchmark-claim calibration, overclaim flagging. If there are no project-specific concerns this round, write "none" rather than padding the line. Examples: "check that all appendix URLs are anonymized", "verify Year 3 budget matches the narrative", "flag any overclaim in intro / conclusion", "watch for Phase 1 / Phase 2 coupling issues".
5. **Round number** -- which iteration this is (starting at 1).
6. **Variant targets (multi-target reviews)** -- if the staged files cover two or more variant targets that should be reviewed separately (long + short paper version, narrative + appendix tracker, internal + external report, primary + supplement), list each target by directory or file pattern. Tell the reviewer to review each target in its own top-level section and then add a cross-variant drift check at the end (tables that should match, claims that should be consistent, terminology that should align).
7. **Round history** (rounds 2+ only) -- a one-line-per-finding summary of what prior rounds raised and how each was resolved. Tag each finding as `Resolved`, `Still open`, `Deferred`, `Refuted`, or `Inconclusive`. The last two come from the Phase 2.5 verification step (see below); they let the reviewer see when a prior factual claim did not hold up under verification, with a pointer to the evidence so the reviewer can either retract or sharpen the claim. This prevents the reviewer from re-litigating closed decisions and lets them verify that fixes landed instead of re-reviewing from scratch. Example:
   ```
   Prior findings:
   - DMP listed wrong project name (Resolved — fixed in round 1)
   - Budget table exceeds page width (Still open)
   - Consider reordering Section 3 (Deferred — user decision)
   - Citation [Smith2023] does not exist (Refuted in round 2 — arxiv.org/abs/2023.XXXXX confirms paper)
   - Compile error in section 4 (Inconclusive in round 2 — could not run latexmk in this env)
   ```

### 1c. Send to reviewer

All review prompts sent to the reviewer (regardless of channel) must include a save instruction **at the very top of the prompt, before the summary or diff**, so the reviewer sees it first. This lets Claude Code read the feedback directly from the file, and lets the user read or forward it without copy-pasting from chat. The save instruction is:

> IMPORTANT: Save your complete review to `Review-<YourAgentName>.md` in the repository root. Normalize `<YourAgentName>` as follows: choose the stable agent or product name visible to the user (not a transient model/version list unless that is the only identity available); convert any run of whitespace to a single dash; delete every character except ASCII letters, digits, and dashes; collapse repeated dashes; trim leading and trailing dashes. Examples: `Codex` → `Review-Codex.md`, `GitHub Copilot` → `Review-GitHub-Copilot.md`, `Gemini 3.1 Pro` → `Review-Gemini-31-Pro.md`, `Claude Code` → `Review-Claude-Code.md`. If the normalized result is empty or you cannot identify your own name with reasonable confidence, use `Review-Unknown.md` and note the uncertainty at the top of the file. Overwrite any existing content for that filename on each new round; do not append across rounds. Use plain Markdown. Start the file with a `<!-- Round N -->` comment (matching the round number below) so the reader can verify freshness. **Begin the review with a short "Verification notes" section (paragraph or short bulleted list; "Validation notes" is also an accepted name) stating exactly what was compiled, run, or verified (e.g., `latexmk built cleanly`, `pytest pyod/test/... 5 passed`, `checked citation X against arXiv:YYYY`). If nothing was verified at runtime, write "Verification notes: none."** Separate findings into **New** (raised for the first time) and **Previously raised** (with status: Fixed, Still open, Reopened, or Deferred) sections. On Round 1, the Previously raised section may be omitted or shown as "None." Then include the file/diff scope, review lens, findings in priority order, and concrete recommended changes. **For any finding flagged High priority, include an exact suggested rewrite with file path and line range. Use a fenced code block for multi-line rewrites.** Do not skip this step. **For examples of the expected depth and format, see `skills/implement-review/references/example-reviews/`.**

**Recording the expected reviewer set**: Before presenting the prompt, record two pieces of Claude-side state that Phase 2 will use:

- **Expected reviewer set**: the reviewers the user intends to invoke this round. Infer from, in order of preference: (1) explicit user statement in this or a recent turn (e.g., "I'll run Codex and Copilot", "just Gemini", "only Codex"); (2) prior-round pattern with no change announced; (3) channel default of `{Codex}` when only Terminal-relay, Auto-terminal, or Plugin has been engaged and no other reviewer is in scope. If none of these produces a confident set, ask the user which reviewers they plan to invoke before presenting the prompt; do not guess.
- **Phase 1c emission time**: the timestamp when the prompt is shown to the user. Used as an mtime tiebreaker in Phase 2 for files that cannot be classified by expected set alone.

Phase 2 uses the expected set as a scope partition axis and the emission time as a freshness tiebreaker.

**Terminal path**: Present a compact, copy-pasteable review prompt as a fenced text block. Keep the prompt under 20 lines. Tell the reviewer to read the diff itself (`git diff --cached`) rather than pasting it inline; this prevents prompt bloat as rounds accumulate. The abbreviated save instruction below inherits the full contract stated above (statuses, Round 1 behavior, required sections).

````
IMPORTANT: Save your complete review to Review-<YourAgentName>.md in the repo root. Normalize your name: pick the stable product name, whitespace → one dash, keep only ASCII letters/digits/dashes, collapse repeated dashes, trim edge dashes. Examples: Codex → Review-Codex.md, GitHub Copilot → Review-GitHub-Copilot.md, Gemini 3.1 Pro → Review-Gemini-31-Pro.md, Claude Code → Review-Claude-Code.md. Use Review-Unknown.md if the result is empty or you cannot identify yourself, and note the uncertainty at the top of the file. Overwrite any existing content for that filename. Start with <!-- Round N -->. Begin with a "Verification notes" paragraph or short bulleted list (what was compiled, run, or verified; "none" if nothing). Include file/diff scope and review lens. Separate findings into New and Previously raised (Fixed / Still open / Reopened / Deferred) sections. For High-priority findings, include an exact rewrite with file:line. See skills/implement-review/references/example-reviews/ for expected depth.

Review staged changes in <repo path>. Round <N>.
Run `git diff --cached` to see the diff. Files changed: <file list>.

Summary: <one to three sentences>
Lens: <content type> — <abbreviated criteria, sub-lens, or agency-specific lens name>
Focus: <additional focus if any, or omit line>
Scope-challenge focus (mandatory; reviewer must take an explicit position): (a) Why exactly this scope — what user pain or opportunity does it close, could a smaller scope close most of it, could a marginally larger scope close all of it for low cost? (b) Is each "out of scope / deferred to vNext" worth its process tax (extra plan-review + implement + execution-review + CI + publish cycle) versus inclusion now? (c) Is this plan the simplest path? Be adversarial about purpose and shape; do NOT fish for low-probability edge cases.
<When the staged diff spans two or more variant targets:>
Variant targets:
- TARGET A: <path or pattern>
- TARGET B: <path or pattern>
(Review each target in its own top-level section and add a Cross-variant drift check at the end.)
<For rounds 2+:>
Prior findings:
- <finding> (Resolved | Still open | Deferred | Refuted | Inconclusive)
````

Then wait for the user to relay the reviewer's feedback or confirm that the reviewer has finished (see Phase 2 for how Claude Code picks up the review).

**Auto-terminal path (opt-in)**: When the user has opted in per the trigger rules in Codex Channels > Path selection, **first check whether the required Auto-terminal scripts exist**. Look up all three script pairs in this order: repo-local `skills/implement-review/scripts/`, then bootstrapped `.agent-config/repo/skills/implement-review/scripts/`. Required scripts are `dispatch-codex.{ps1,sh}`, `health-check.{ps1,sh}`, and `stall-watch.{ps1,sh}`. If any required script for the current platform is missing, say `Auto-terminal is documented but unavailable until the Phase B scripts are present; using Terminal-relay for this round.` Then downgrade to Terminal-relay **without** setting sticky downgrade (the absence is design state, not failure; sticky should kick in only for real Auto-terminal runtime failures).

If the scripts are present, Claude Code does NOT present a copy-paste prompt block. Instead, the skill:

1. Probes `codex` on PATH (`Get-Command codex` on Windows, `command -v codex` on POSIX). If absent: warn the user and downgrade to Terminal-relay for this round.
2. Probes session sticky-downgrade state. If active: downgrade to Terminal-relay (silent unless the user asks why).
3. Assembles the prompt: byte-identical to the fenced Terminal-relay block above (same save contract, lens, focus, scope-challenge focus, prior findings).
4. Writes the prompt to a temp file under `%TEMP%` / `$TMPDIR`.
5. Invokes `skills/implement-review/scripts/dispatch-codex.{ps1,sh}` (repo-local first, then bootstrapped under `.agent-config/repo/`) with `--prompt-file <temp-path>`, `--round <N>`, `--expected-review-file Review-Codex.md`. Run via the Bash tool with `run_in_background=True` and `timeout=1200000` (20 minutes; sized for paper-review-scale prompts under xhigh reasoning effort). The dispatch script internally launches `stall-watch.{ps1,sh}` in the background to monitor tail-file growth; stall events are recorded to `<state-dir>/stall-warning` for Health check 9 to surface post-hoc.
6. Reads the dispatch script's stdout: it emits exactly one line `STATE-DIR <abs-path>` (the only stdout line). Capture this path for Phase 2 to pass to `health-check --state-dir <abs-path>`. All other dispatch diagnostics plus the last-80 codex-exec lines go to the script's stderr.
7. Phase 1d auto-watch runs unchanged; it polls for `Review-Codex.md` with the current round marker. Phase 2 prologue (defined in Phase 2 below) adds Auto-terminal-specific gating before silent advance.

On Auto-terminal failure (non-zero exit, timeout, missing fresh review file, or Health check fail): downgrade to Terminal-relay for that round AND set sticky downgrade for the rest of the session. Do not add a truncated positional-argument fallback.

**Script contract invariants** (apply to all `dispatch-codex` / `health-check` / `stall-watch` `.sh` and `.ps1` variants):

- **State-dir lifecycle**: created per-dispatch by `dispatch-codex` under `%TEMP%` / `$TMPDIR`; preserved on any `WARN` or `FAIL` outcome so the user can inspect; deleted only after a clean Phase 2 intake completes silently. State-dir names carry `<pid>-<nonce>` and are never recycled across dispatches.
- **Timestamp units**: all values in `<state-dir>/timestamp` and `<state-dir>/pre-mtime` are **Unix epoch seconds** (integer or float). mtime comparisons happen in UTC; cross-platform stat is the implementer's responsibility. POSIX may use `stat -c %Y` (GNU) or `stat -f %m` (BSD/macOS). PowerShell must convert `LastWriteTimeUtc` to Unix epoch seconds, for example `$utc = (Get-Item -LiteralPath <path>).LastWriteTimeUtc; ([DateTimeOffset]$utc).ToUnixTimeSeconds()` (two-step form, avoids cast-precedence confusion) or an equivalent Unix-epoch conversion. **Do NOT write Windows FILETIME values from `ToFileTimeUtc()`** (FILETIME is 100-nanosecond ticks since 1601-01-01, not Unix epoch; mixing units across `.ps1` and `.sh` would silently break Health check 2 freshness comparisons).
- **stdin invocation shape**: PowerShell variant uses an explicit stream pipe (`Get-Content -Raw <prompt-file> | codex exec -`); POSIX variant uses redirection (`codex exec - < <prompt-file>`). Neither variant may use command substitution (`codex exec "$(cat <file>)"`) or positional-arg passing of the full prompt, because both hit ARG_MAX on Windows.
- **`stall-watch` parent-PID liveness**: best-effort check every poll (`kill -0 <pid>` on POSIX, `Get-Process -Id <pid>` on Windows). The check is informational only; `stall-watch` **must never kill `codex exec`** under any circumstance. If `stall-watch` itself errors, it exits silently.
- **Exit codes**: `dispatch-codex` propagates Codex's own exit code unchanged. `health-check` exits non-zero only when Phase 2 must refuse to read the review file (Check 1-6 FAIL or required dispatch state missing/stale); WARN-only outcomes (Check 7/8/9 hit, Substance heuristic flag) exit 0 with WARN lines on stdout. **Phase 2 must parse `health-check` stdout and treat any `WARN` line as a silent-advance blocker; exit code 0 alone is NOT sufficient to proceed silently.** `stall-watch` exits 0 regardless of what it detected.

**Plugin path**: Tell the user the changes are ready for review and provide **the same review prompt content used by the Terminal-relay path** (save contract first, then round, diff scope, summary, lens, focus, scope-challenge focus when applicable, variant targets when applicable, prior findings for rounds 2+). The user pastes the same content into the plugin sidebar; the surrounding Markdown fence may be omitted if the plugin UI does not need it, but the prompt content itself must match Terminal-relay byte-for-byte (after the optional fence-strip). This preserves the cross-channel prompt invariant: Codex receives the same instructions regardless of channel.

Example instruction to the user:
> Paste the same review prompt shown for Terminal-relay into the Codex plugin sidebar. The plugin can inspect the repository directly (so the diff is visible without copying), but the prompt content itself must still include the save contract, round number, files changed, summary, lens, focus, scope-challenge focus when applicable, variant targets when applicable, and prior findings when applicable. Ask the plugin reviewer to save the complete review to `Review-<YourAgentName>.md` in the repo root using the normal Phase 1c save contract.

Then wait for the user to relay the reviewer's feedback or confirm that the reviewer has finished.

### 1d. Auto-watch (Terminal-relay and Auto-terminal channels)

After Phase 1c emits the Terminal-relay prompt or dispatches Auto-terminal (and records the expected reviewer set + emission/dispatch time), the skill **automatically** launches a background watcher that detects when the reviewer writes `Review-<expected>.md` and resumes Phase 2 — eliminating the manual "done" relay. The watcher runs by default for both channels; the user does not need to confirm. To opt out, the user can say so explicitly (e.g., "stop auto-watch", "manual mode this round") and the skill terminates the background process and falls through to the wait-for-user path. Plugin path skips this subsection entirely (IDE plugins typically have the file open and gain little from auto-watch).

Launch the platform-appropriate watcher script immediately after emitting the prompt, using positional arguments `(FILE_GLOB, ROUND_NUMBER, EXPECTED_REVIEWERS)`. Look up the script in this order: `skills/implement-review/scripts/auto-watch.{sh,ps1}` (repo-local), then `.agent-config/repo/skills/implement-review/scripts/auto-watch.{sh,ps1}` (bootstrapped). Use the Bash variant on macOS / Linux and the PowerShell variant on Windows. `FILE_GLOB` is `Review-<expected>.md` for a single expected reviewer or `Review-*.md` for multiple; `EXPECTED_REVIEWERS` is the comma-separated normalized name list from Phase 1c (e.g., `Codex` or `Codex,GitHub-Copilot`). Run the watcher in the background so the skill can keep accepting user input while it polls.

The watcher polls every 5 seconds. It fires when (a) the file's mtime has advanced past the snapshot taken at watcher startup, (b) the file has been quiet for 10 seconds (mtime is at least 10 seconds in the past), AND (c) its first line equals `<!-- Round N -->` after stripping trailing `\r` and whitespace. Hard timeout is 60 minutes. Stdout schema is exactly two lines: `WATCH-START round=N reviewers=<csv> timeout=3600s` followed by either `DONE <absolute-path>` (exit 0) or `TIMEOUT` (exit 2). Total output is ~50 tokens whether successful or timed out.

When the watcher emits `DONE <path>`, resume Phase 2 immediately. The watcher's path output is informational; Phase 2 still re-lists `Review-*.md` itself and applies the freshness + scope partition described below. If the expected set has multiple reviewers and only one fired, Phase 2's reviewer-specific follow-up handles the rest.

When the watcher emits `TIMEOUT`, print `Auto-watch timed out after 60 min; reply 'done' when the reviewer finishes.` and resume the existing wait-for-user path. On explicit opt-out, user interrupt, or watcher launch failure, also fall through to the same wait-for-user path. The fallback is the unchanged Phase 1c → Phase 2 flow, so no Phase 2 logic depends on whether auto-watch was used.

## Phase 2: Intake Feedback

### Phase 2.0 prologue: Auto-terminal Health check (Auto-terminal channel only; Terminal-relay and Plugin skip)

For the Auto-terminal channel only, run 9 structural Health checks plus 3 Substance heuristics on `Review-Codex.md` before the existing freshness + scope partition begins. Required dispatch state files (`<state-dir>/pre-mtime`, `<state-dir>/timestamp`, `<state-dir>/tail`, plus optionally `<state-dir>/stall-warning` when stall-watch wrote it) come from the `dispatch-codex` and `stall-watch` scripts via dispatch-codex's stdout `STATE-DIR <abs-path>` line. Health checks 2, 7, 8, 9 and the Substance heuristics depend on these files.

| # | Check | Failure → |
|---|---|---|
| 1 | `Review-Codex.md` exists at repo root | Surface failure; offer downgrade-retry via Terminal-relay |
| 2 | File mtime is later than the Phase 1c dispatch timestamp AND later than `<state-dir>/pre-mtime` (catches stale file from prior round or task) | Same as 1 |
| 3 | First line equals `<!-- Round N -->` | Same as 1 |
| 4 | File size ≥ 500 chars | Same as 1 |
| 5 | "Verification notes" section present (`Verification notes:` paragraph form OR `## Verification notes` / `**Verification notes**` heading) | Same as 1 |
| 6 | If the dispatch prompt named a plan file or staged file list, the review's file scope mentions that current scope | Same as 1 |
| 7 | **Review-text** suspicious-phrase scan: case-insensitive match in the saved review body on any of `could not`, `i cannot`, `failed to`, `permission denied`, `rate limit`, `unable to access`, `do not have access`, `not authenticated`, `authentication failed`, `unauthorized`, `timed out`, `timeout`, `quota`, `command not found`, `no such file`, `sandbox.*fail`. Exclude content inside backtick code spans (Codex meta-discussing the pattern list is not failure narration). | Do not hard-fail; block silent advance and surface: `Auto-terminal review-text scan: N suspicious phrases (lines L1, L2, ...) -- review may be partial. Proceed?` |
| 8 | **Dispatch-tail** tool-failure scan: case-insensitive regex match in `<state-dir>/tail` (full codex-exec stdout+stderr, NOT the review body) for `tool ... failed`, `mcp tool failed`, HTTP/status 429/5xx, `rate limit`, `quota exceeded`, `insufficient_quota`, `connection refused/reset/timed out`, `context_length_exceeded`, `maximum context length`, errno forms `ENOSPC` / `EACCES` / `ETIMEDOUT` / `ECONNRESET` / `ECONNREFUSED`. See `skills/implement-review/scripts/health-check.{sh,ps1}` for the canonical pattern list. | Do not hard-fail; block silent advance and surface: `Auto-terminal dispatch-tail scan: N tool-failure markers -- Codex's CLI / OS / network layer leaked errors. Proceed?` |
| 9 | **Stall-watch** check: `<state-dir>/stall-warning` file does NOT exist. The `stall-watch` background daemon (launched by `dispatch-codex` alongside `codex exec`) appends to this file when `<state-dir>/tail` has zero growth for ≥ 5 min, which signals Codex paused (CLI deadlock, network stall, long reasoning, or corrupted-prompt loop) without self-narrating an error. File present = at least one stall period occurred. | Do not hard-fail; block silent advance and surface: `Auto-terminal stall-watch: N stall period(s) detected (first at T+<min>); Codex output paused for 5+ min during run. Review may have been produced under stress. Proceed?` |

> Implementer note: pipes inside Check 7 and Check 8 regex patterns are escaped as `\|` where Markdown-table parsing requires it. The runtime regex engine (Python `re`, PowerShell `-match`, etc.) must receive unescaped alternation (`|`). The health-check script should compile patterns from a source-of-truth list rather than from the rendered Markdown.

**Outcomes**:

- Checks 1-6 all pass AND Checks 7, 8, and 9 all clear (0 suspicious phrases, 0 tool-failure markers, no stall-warning file present) → eligible for silent proceed (still subject to Substance heuristics and Phase 1d coordination below).
- Checks 1-6 all pass AND Check 7, 8, OR 9 hit (any marker or stall-warning present) → emit the corresponding one-line note(s); block silent advance; ask user to Proceed or Downgrade.
- Any of Checks 1-6 fails → do not read the file; surface specifically which check failed; offer Terminal-relay retry.

**Required dispatch state contract**: Missing or stale `<state-dir>/pre-mtime` or `<state-dir>/timestamp` is `FAIL` (Health check 2 freshness and the time-floor heuristic cannot be trusted). Missing `<state-dir>/tail` is `WARN check-8 1 missing-dispatch-tail` and blocks silent advance; it must NOT be silently treated as Check 8 hit 0.

#### Substance heuristics (Auto-terminal only; soft signals; ANY hit blocks silent advance; never hard-fails)

The 9 structural Health checks above only verify the file looks well-formed and the dispatch didn't visibly stall. They do NOT catch a review that is structurally clean but substantively shallow (Codex's tools silently failed mid-run; rate limit; context overflow; or the model did not engage). Three lens-aware heuristics fire after the 9 Health checks and surface independently. **Any hit blocks silent advance**: the user is prompted "Proceed? / Downgrade?" before Phase 2 reads the file.

| Heuristic | Signal | Surface format |
|---|---|---|
| Time-to-completion floor | Elapsed wall time from `<state-dir>/timestamp` to `Review-Codex.md` finalization is less than 30s, when the dispatched prompt is ≥ 2000 chars | `Substance heuristic: review completed in <T>s for <P>-char prompt -- Codex may have bailed early. Spot-check before trusting.` |
| Anchor density | Review body > 1000 chars AND zero file-line anchors matching `:\d+`, `\bline \d+\b`, `\blines? \d+\s*[-–]\s*\d+\b`, or `<file>:<line>` patterns | `Substance heuristic: review has <N> chars and 0 file:line anchors -- may be generic prose without code/line grounding. Spot-check High findings.` |
| Scope-challenge engagement (plan-review lens only) | Review does NOT contain visible evidence of ALL three scope-challenge axes. Evidence per axis = `(a)` / `(b)` / `(c)` enumeration OR keyword coverage (case-insensitive): **Axis 1** (`scope` AND one of `smaller` / `larger`), **Axis 2** (one of `deferral` / `process tax` / `release cycle`), **Axis 3** (one of `simplest` / `do nothing` / `doing nothing` / `smaller path` / `shrink` / `docs only` / `document only` / `script only` / `no-op`). Axis-3 keyword set is intentionally broad to avoid false positives on substantive reviews. | `Substance heuristic: plan-review did not visibly engage scope-challenge axes <missing-axis-numbers>. May be incomplete per skill mandate.` |

Substance heuristics are skipped for Terminal-relay and Plugin path (the user has direct eyes on the run).

#### Phase 1d coordination (Auto-terminal-specific silent-intake rule)

For Terminal-relay, auto-watch `DONE` is sufficient signal to silently advance into Phase 2. For Auto-terminal, `DONE` is necessary but not sufficient. Phase 2 may silently advance only when ALL of the following hold:

1. Auto-watch emits `DONE <path>` (file appeared with current round marker).
2. The dispatch subprocess exited 0 (Bash background task notification reports `exit code 0`).
3. Health checks 1-6 all pass.
4. Health check 7 hits 0 phrases.
5. Health check 8 hits 0 markers.
6. Health check 9 (stall-warning file absent) holds.
7. Substance heuristics flag 0 signals.

If 2 fails, 3 fails, 4 hits ≥ 1 phrase, 5 hits ≥ 1 marker, 6 detects the stall-warning file, or 7 flags ≥ 1 signal, Phase 2 stops at a human checkpoint with `Proceed? / Downgrade?`; no silent advance. Auto-watch `TIMEOUT` also stops at a human checkpoint.

#### False-positive tuning principle

The Health checks (especially Check 7) and Substance heuristics are intentionally soft (surface + block silent advance, never hard-fail) because false positives are expected. Observed FP modes during dogfooding:

- Check 7 firing on Codex's own meta-discussion of the regex pattern list. Mitigated: Check 7 excludes content inside backtick code spans.
- Substance axis-3 firing on synonym phrasings (`shrink to docs only` rather than `simplest path`). Mitigated: axis-3 keyword set broadened.
- Anchor density firing on a precise convergence review with low anchor count. Accepted as rare; one-click Proceed.

Operational principle: better to under-flag a real signal than to cry wolf so often the user clicks through every flag without reading. Alarm fatigue is not recoverable; once the user trains themselves to click past every surface, the silent-failure mitigation collapses. Track FP rate per check during the first ~10 Auto-terminal runs; if any check class exceeds 30% FP, tune the trigger criteria (broaden keywords, exclude additional contexts, raise size threshold) until signal-to-noise is workable.

---

### Phase 2.1: Partition and classify (all channels)

Reviewers are instructed to write their review to a `Review-<AgentName>.md` file in the repository root, using their own self-reported name (see Phase 1c). When the user says a reviewer is done, or when multiple reviewers have been run in parallel for the same round, list the files matching `Review-*.md` at the repo root. Apply the two-axis partition described below (freshness + scope) to decide which files to read, and report any ignored files to the user.

Multi-reviewer consolidation: if two or more current-round review sources are available (current-round `Review-*.md` files and/or reviewer feedback the user relays directly), classify each new finding as:

- **Convergent** -- two or more reviewers raise substantially the same point. High confidence; treat as "will fix" unless wrong on the merits. If reviewers agree on the underlying problem but differ on severity, scope, or recommended remedy, classify as **Convergent with differences**: preserve each reviewer's severity and recommended fix in the consolidation report, and use the highest severity until the user or implementer resolves the difference.
- **Single-source** -- only one reviewer raises the point. Label the source (e.g., "from Review-Codex.md") when presenting.
- **Divergent** -- reviewers take opposite positions on the same finding. Flag the disagreement explicitly; present both sides; ask the user to decide.

Before trusting any file, verify that its `<!-- Round N -->` comment matches the current round number. Partition matching `Review-*.md` files along two axes.

**Freshness axis**: **current-round** (first line is `<!-- Round N -->`), **stale-round** (a different round marker), **empty**, or **unreadable** (cannot be read, or has a malformed or missing round marker).

**Scope axis** (if the expected reviewer set from Phase 1c is known): **expected** (the reviewer name extracted from the filename is in the expected set) or **unexpected** (the name is not in the set, possibly a leftover artifact from an earlier task that happens to share the round number).

Read and consolidate only files that are both **current-round** and **expected**. Report every ignored file by filename, grouped by reason (stale-round, empty, unreadable, unexpected), before presenting findings, so the user sees which reviewers produced usable output this round.

If a file is current-round but unexpected, flag it to the user before inclusion or exclusion: it may be a leftover from another task, or an additional reviewer the user invoked without announcing. Treat the file mtime against the recorded Phase 1c emission time as a secondary signal only, subject to clock skew, filesystem timestamp granularity, and editor-touch noise: mtime clearly older than emission weakly suggests a prior-task artifact; mtime at or after emission is consistent with a current-round file. Require at least one corroborating signal before including (e.g., file/diff scope matching current staged files, verification notes referencing current commands or files, the reviewer being named in the conversation). Do not silently include or exclude.

If the expected reviewer set is unknown, treat mtime as weak evidence only: use it to rank candidates, not to auto-classify. Ask the user to confirm which current-round files belong to this review before consolidating.

If the expected reviewer set is known and any reviewer in it is not represented in the current-round + expected bucket (absent entirely, or its file is stale, empty, unreadable, or unexpected):
1. Present a reviewer-specific follow-up prompt the user can paste back into that reviewer, identifying which reviewer is missing so the user knows where to paste it: `Save your review to Review-<YourAgentName>.md in the repo root. Normalize your name: pick the stable product name, whitespace → one dash, keep only ASCII letters/digits/dashes, collapse repeated dashes, trim edge dashes. Examples: Review-Codex.md, Review-GitHub-Copilot.md, Review-Gemini-31-Pro.md, Review-Claude-Code.md. Use Review-Unknown.md if the result is empty or you cannot identify yourself, and note the uncertainty at the top. Overwrite any existing content for that filename. Start with <!-- Round N -->. Begin with a "Verification notes" paragraph or short bulleted list. Separate findings into New and Previously raised (Fixed / Still open / Reopened / Deferred) sections. For High-priority findings, include an exact rewrite with file:line.`
2. If the file is still missing, still empty, or still carries a stale round marker after the follow-up, ask the user to paste that reviewer's feedback directly.

If only one current-round source remains after retry and direct-paste handling when multiple reviewers were expected, proceed with single-reviewer intake and label every finding as Single-source (no Convergent classification is possible without a second source). If the user did not invoke multiple reviewers this round, treat the single current-round source as the complete intake.

- When feedback arrives (from any `Review-*.md` file or relayed by the user), acknowledge each point.
- If a reviewer separated findings into "New" and "Previously raised" sections, verify the classifications. If a reviewer did not separate them (older prompts or non-compliance), do the separation yourself based on the round history.
- Categorize each **new** point as:
  - **Will fix** -- clear, actionable, and correct.
  - **Needs discussion** -- ambiguous or potentially wrong; ask the user before acting.
  - **Disagree** -- explain why and let the user decide.
- For **previously raised** points, check the status the reviewer assigned:
  - **Fixed** -- the reviewer confirms the prior finding was addressed. No action needed.
  - **Still open** -- the fix did not land or was incomplete. Treat as "will fix" unless the user overrides.
  - **Reopened** -- the reviewer re-raises a point that was marked Resolved. Flag to the user: this needs a decision, not silent re-litigation.
  - **Deferred** -- the user chose not to address this. The reviewer acknowledges it as unchanged. No action unless the user reconsiders.
- **Verify factual claims (Phase 2.5)** for High-priority findings that make checkable factual assertions (citation existence, code behavior, link reachability, count or size, compile error). Verification outcomes (`Verified` / `Refuted` / `Inconclusive`) override or augment the categorization above: a Refuted finding is not applied, regardless of its original `Will fix` / `Needs discussion` category.
- Present the categorized list (including verification outcomes from Phase 2.5) and confirm with the user before making changes.
- For follow-up questions within the same review round, prepare a short prompt the user can paste into the reviewer.

## Phase 2.5: Verify Factual Claims (when triggered)

After Phase 2 categorization but before the user is presented with the categorized list, verify any High-priority finding that makes a checkable factual claim. Reviewer assertions are not always correct; verifying first prevents revising in a wrong direction and produces an evidence trail when the claim is refuted. This phase is the inverse failure mode of "blindly accept reviewer feedback": a confidently-worded but wrong finding can otherwise propagate into the next round and waste both sides' time.

### Trigger

Verification fires for a finding when **all** of the following hold:

1. The finding is tagged High priority (or the user explicitly requests verification on a Medium / Low finding).
2. The finding makes an objectively checkable factual claim — citation existence, code behavior, link reachability, page-limit or word-count assertion, compile or type error, anonymization leak, etc.
3. The reviewer's "Verification notes" section did not already cover this specific claim with a method that resolves it. If the reviewer ran a check that covers the claim, trust it unless contradicting evidence appears during fix application.
4. The cost of verification is reasonable relative to the cost of applying the fix blindly. Skip verification when the proposed fix is trivially correct (a one-line typo) or when the cheapest verification path takes longer than the user can wait on a single finding.

Skip entirely for stylistic, opinion-based, or structural findings (term choice, paragraph order, tone, organization). Those have no factual ground truth to verify against.

### Methods

Pick the cheapest applicable method per claim type:

| Claim type | Verification method |
|---|---|
| Citation / paper / author existence | `WebSearch` for title and author; `WebFetch` arXiv / DOI URL; cross-check against the project's `.bib` file |
| Code behavior assertion | Run the targeted test (`pytest path::test_name`); read the source to trace; eval a small repro snippet |
| Link reachability | `WebFetch` of the URL; HEAD request via curl |
| Page limit / word count / file size | `wc -w`, `wc -l`, `ls -la`; compile and read the LaTeX log |
| Compile-time / type error | Run the compiler (`latexmk`, `tsc`, etc.) or type checker (`mypy`, `pyright`) on the affected file |
| Anonymization or leak claim | `Grep` for the alleged term; cross-check against the project's de-anonymization checklist |

If no listed method applies, ask the user before either accepting the claim or pushing back.

### Outcome

Record one of three outcomes for each verified claim:

- **Verified** — the reviewer's claim is correct. Proceed with the fix as a normal `Will fix` item.
- **Refuted** — the reviewer's claim is wrong, or the issue does not reproduce. **Do not apply the fix.** Push back via a follow-up reviewer prompt that cites the verification evidence (command run, output, URL fetched, file path inspected). The user may override this by asking to apply anyway, but the default is to push back.
- **Inconclusive** — verification could not run (no internet, no test runner, claim under-specified) or returned ambiguous results. Surface the ambiguity to the user and let them decide.

### Round history integration

Findings flagged Refuted or Inconclusive enter the next round's `Prior findings` block (see Phase 1b item 7) so the reviewer sees the evidence and can either retract the claim or sharpen it. Refuted findings appear as `(Refuted in round N — <one-line evidence>)`. Inconclusive findings appear as `(Inconclusive in round N — <reason>)`, prompting the reviewer to supply a more specific or pre-verified check.

### Example

Round 2 of a paper review. Reviewer flags High: "Citation [Smith2023] does not exist; could not find this paper on arXiv or Google Scholar."

1. Trigger fires (High priority + checkable factual claim).
2. Verification path: `WebSearch "Smith 2023 <topic keywords>"`, then `WebFetch arxiv.org/abs/<id>` if a candidate surfaces.
3. Outcome:
   - If a real paper is found → **Refuted**. Reply to the reviewer with the URL and arXiv ID, ask them to confirm before re-flagging. Do not edit the citation.
   - If no paper is found after a reasonable search → **Verified**. Remove the citation or replace it with a real one.
4. Record in round history: `[Smith2023] reference (Verified in round 2 — not present on arXiv or Google Scholar)` or `[Smith2023] reference (Refuted in round 2 — arxiv.org/abs/2023.XXXXX confirms paper)`.

## Root Review Sink (per reviewer)

When a review produces substantial written feedback, each reviewer saves the latest review to `Review-<AgentName>.md` in the repository root. Normalize `<AgentName>` as follows: choose the stable agent or product name visible to the user (not a transient model/version list unless that is the only identity available); convert any run of whitespace to a single dash; delete every character except ASCII letters, digits, and dashes; collapse repeated dashes; trim leading and trailing dashes. Examples: `Codex` → `Review-Codex.md`, `GitHub Copilot` → `Review-GitHub-Copilot.md`, `Gemini 3.1 Pro` → `Review-Gemini-31-Pro.md`, `Claude Code` → `Review-Claude-Code.md`. If the normalized result is empty or a reviewer cannot identify itself with reasonable confidence, it uses `Review-Unknown.md` and notes the uncertainty at the top of the file.

One file per reviewer, one round per file. Treat each file as a reusable scratch file for the current review round, not as a permanent archive. By default, overwrite the file completely on each new saved review rather than creating per-directory review files or appending multiple rounds, unless the user explicitly asks to preserve history. Running two reviewers in the same round produces two files (e.g., `Review-Codex.md` and `Review-GitHub-Copilot.md`), which Phase 2 reads together and consolidates.

Legacy `CodexReview.md` files from pre-upgrade sessions are ignored by Phase 2 intake. If one is present in the repo root or under `docs/`, treat it as stale scratch output from the old single-reviewer flow unless the user explicitly asks to inspect it.

The purpose of the `Review-*.md` files is to let the user and Claude Code read, reuse, and forward the latest review(s) without copy-pasting from chat. Keep each file in plain Markdown and make it directly useful on its own. Include:

- a `<!-- Round N -->` HTML comment on the first line (used by Phase 2 to verify freshness)
- a `Verification notes` paragraph or short bulleted list at the top of the review (immediately after `# Review`), stating what was compiled, run, or verified; write "none" if nothing at runtime
- the file or diff scope reviewed
- the review lens or context
- findings separated into **New** and **Previously raised** sections (previously raised items tagged Fixed, Still open, Reopened, or Deferred; on Round 1 the Previously raised section may be omitted or shown as "None")
- concrete recommended changes, with exact values when relevant
- for any finding flagged High priority, an exact suggested rewrite with file path and line range (use a fenced code block for multi-line rewrites)

Do not stage, commit, or move `Review-*.md` files unless the user explicitly asks. Before the first review round, check whether `Review-*.md` is excluded from git. Look in `.gitignore` and `.git/info/exclude`. If the pattern is not excluded anywhere, append `Review-*.md` to `.git/info/exclude` (a local, untracked ignore file) so that `git add -A` during the revision flow does not accidentally stage scratch files. A repo that already ships `Review-*.md` in the committed `.gitignore` (as this repo does) satisfies the exclusion requirement without a local edit.

## Phase 3: Revise

- Address all "will fix" points and any "needs discussion" points the user approved.
- Update the round history: mark addressed findings as `Resolved`, keep unaddressed ones as `Still open`, and tag user-deferred items as `Deferred`. This history carries forward into the next round's prompt (Phase 1b, item 7).
- Stage the revised changes.
- Return to Phase 1 with an incremented round number.

## Phase 4: Conclude

The loop ends when:
- The user says the review is done or approved.
- No reviewer raises actionable issues.
- The user decides to stop iterating.

At conclusion, present a short summary: total rounds, key changes made, and any unresolved points from the last review.

## When Not To Use

- Trivial changes where review adds no value (typo fixes, config tweaks).
- Changes that require running tests or builds to validate -- run those first, then review.
- When the user wants a single-shot review with no revision loop; just ask a reviewer directly.
