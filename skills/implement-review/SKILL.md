---
name: implement-review
description: Review loop for staged changes. Detects content type, prepares a review request for Codex (terminal or plugin), categorizes feedback, revises, and iterates. Works for code, papers, proposals, or any text-based output.
---

# Implement-Review

## Overview

A review loop for staged changes. Claude Code detects the content type, sends the changes to one or more reviewers, categorizes the feedback, revises, and iterates. Codex is the primary reviewer via two channels: terminal relay (default on all platforms) or IDE plugin. Other reviewers (Copilot, Gemini, Claude Code, etc.) are driven ad-hoc by the user through their own UI and only need to honor the `Review-<AgentName>.md` save contract defined in Phase 1c.

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
4. Iterate until the review has no High findings and no new design blockers.
5. Then execute (code, draft, revise, deploy).
6. Run the normal review cycle on the staged output. It is typically smaller because the architecture was already validated.
7. After the work ships or is submitted, delete the PLAN file.

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

Two paths to Codex are supported. The skill picks the best available path automatically.

### Terminal path (default)

The user has a Codex interactive terminal window open alongside Claude Code. Claude Code prepares a copy-pasteable review prompt (summary, diff, lens, round number) and presents it as a fenced text block. The user copies it into the Codex terminal, then relays the feedback back to Claude Code.

### Plugin path (IDE sidebar)

Codex runs as an IDE plugin with direct access to the repo. The user tells Codex to review in the plugin sidebar (e.g., "review the staged changes"). Codex can see the working tree and run `git diff` itself, so no diff needs to be copy-pasted. The user relays Codex's feedback back to Claude Code.

### Path selection

1. Default to the terminal path on all platforms.
2. The plugin path is available on all platforms when the user initiates it, but it is not a default.
3. The user can override at any time (e.g., "use the plugin", "use the terminal").

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
7. **Round history** (rounds 2+ only) -- a one-line-per-finding summary of what prior rounds raised and how each was resolved. Tag each finding as `Resolved`, `Still open`, or `Deferred`. This prevents the reviewer from re-litigating closed decisions and lets them verify that fixes landed instead of re-reviewing from scratch. Example:
   ```
   Prior findings:
   - DMP listed wrong project name (Resolved — fixed in round 1)
   - Budget table exceeds page width (Still open)
   - Consider reordering Section 3 (Deferred — user decision)
   ```

### 1c. Send to reviewer

All review prompts sent to the reviewer (regardless of channel) must include a save instruction **at the very top of the prompt, before the summary or diff**, so the reviewer sees it first. This lets Claude Code read the feedback directly from the file, and lets the user read or forward it without copy-pasting from chat. The save instruction is:

> IMPORTANT: Save your complete review to `Review-<YourAgentName>.md` in the repository root. Normalize `<YourAgentName>` as follows: choose the stable agent or product name visible to the user (not a transient model/version list unless that is the only identity available); convert any run of whitespace to a single dash; delete every character except ASCII letters, digits, and dashes; collapse repeated dashes; trim leading and trailing dashes. Examples: `Codex` → `Review-Codex.md`, `GitHub Copilot` → `Review-GitHub-Copilot.md`, `Gemini 3.1 Pro` → `Review-Gemini-31-Pro.md`, `Claude Code` → `Review-Claude-Code.md`. If the normalized result is empty or you cannot identify your own name with reasonable confidence, use `Review-Unknown.md` and note the uncertainty at the top of the file. Overwrite any existing content for that filename on each new round; do not append across rounds. Use plain Markdown. Start the file with a `<!-- Round N -->` comment (matching the round number below) so the reader can verify freshness. **Begin the review with a short "Verification notes" section (paragraph or short bulleted list; "Validation notes" is also an accepted name) stating exactly what was compiled, run, or verified (e.g., `latexmk built cleanly`, `pytest pyod/test/... 5 passed`, `checked citation X against arXiv:YYYY`). If nothing was verified at runtime, write "Verification notes: none."** Separate findings into **New** (raised for the first time) and **Previously raised** (with status: Fixed, Still open, Reopened, or Deferred) sections. On Round 1, the Previously raised section may be omitted or shown as "None." Then include the file/diff scope, review lens, findings in priority order, and concrete recommended changes. **For any finding flagged High priority, include an exact suggested rewrite with file path and line range. Use a fenced code block for multi-line rewrites.** Do not skip this step. **For examples of the expected depth and format, see `skills/implement-review/references/example-reviews/`.**

**Recording the expected reviewer set**: Before presenting the prompt, record two pieces of Claude-side state that Phase 2 will use:

- **Expected reviewer set**: the reviewers the user intends to invoke this round. Infer from, in order of preference: (1) explicit user statement in this or a recent turn (e.g., "I'll run Codex and Copilot", "just Gemini", "only Codex"); (2) prior-round pattern with no change announced; (3) channel default of `{Codex}` when only the Codex terminal or plugin has been engaged and no other reviewer is in scope. If none of these produces a confident set, ask the user which reviewers they plan to invoke before presenting the prompt; do not guess.
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
<When the staged diff spans two or more variant targets:>
Variant targets:
- TARGET A: <path or pattern>
- TARGET B: <path or pattern>
(Review each target in its own top-level section and add a Cross-variant drift check at the end.)
<For rounds 2+:>
Prior findings:
- <finding> (Resolved | Still open | Deferred)
````

Then wait for the user to relay the reviewer's feedback or confirm that the reviewer has finished (see Phase 2 for how Claude Code picks up the review).

**Plugin path**: Tell the user the changes are ready for review and suggest what to tell the reviewer in the plugin. The suggestion inherits the full save contract stated above. Example:
> "Review the staged changes (round N). Focus on [detected lens]. Save your complete review to `Review-<YourAgentName>.md` in the repo root. Normalize your name: pick the stable product name, whitespace → one dash, keep only ASCII letters/digits/dashes, collapse repeated dashes, trim edge dashes. Examples: `Review-Codex.md`, `Review-GitHub-Copilot.md`, `Review-Gemini-31-Pro.md`, `Review-Claude-Code.md`. Use `Review-Unknown.md` if the result is empty or you cannot identify yourself, and note the uncertainty at the top of the file. Overwrite any existing content for that filename. Start the file with `<!-- Round N -->`. Begin with a `Verification notes` paragraph or short bulleted list (what you compiled, ran, or verified; 'none' if nothing). Include file/diff scope and review lens. Separate findings into New and Previously raised (Fixed / Still open / Reopened / Deferred) sections. For any High-priority finding, include an exact rewrite with file:line. If the diff spans two or more variant targets (long + short, narrative + tracker, internal + external), review each target in its own top-level section and add a Cross-variant drift check at the end."

Then wait for the user to relay the reviewer's feedback or confirm that the reviewer has finished.

### 1d. Auto-watch (terminal path only)

After Phase 1c emits the prompt and records the expected reviewer set + emission time, the terminal path **automatically** launches a background watcher that detects when the reviewer writes `Review-<expected>.md` and resumes Phase 2 — eliminating the manual "done" relay. The watcher runs by default; the user does not need to confirm. To opt out, the user can say so explicitly (e.g., "stop auto-watch", "manual mode this round") and the skill terminates the background process and falls through to the wait-for-user path. Plugin paths skip this subsection entirely (IDE plugins typically have the file open and gain little from auto-watch).

Launch the platform-appropriate watcher script immediately after emitting the prompt, using positional arguments `(FILE_GLOB, ROUND_NUMBER, EXPECTED_REVIEWERS)`. Look up the script in this order: `skills/implement-review/scripts/auto-watch.{sh,ps1}` (repo-local), then `.agent-config/repo/skills/implement-review/scripts/auto-watch.{sh,ps1}` (bootstrapped). Use the Bash variant on macOS / Linux and the PowerShell variant on Windows. `FILE_GLOB` is `Review-<expected>.md` for a single expected reviewer or `Review-*.md` for multiple; `EXPECTED_REVIEWERS` is the comma-separated normalized name list from Phase 1c (e.g., `Codex` or `Codex,GitHub-Copilot`). Run the watcher in the background so the skill can keep accepting user input while it polls.

The watcher polls every 5 seconds. It fires when (a) the file's mtime has advanced past the snapshot taken at watcher startup, (b) the file has been quiet for 10 seconds (mtime is at least 10 seconds in the past), AND (c) its first line equals `<!-- Round N -->` after stripping trailing `\r` and whitespace. Hard timeout is 60 minutes. Stdout schema is exactly two lines: `WATCH-START round=N reviewers=<csv> timeout=3600s` followed by either `DONE <absolute-path>` (exit 0) or `TIMEOUT` (exit 2). Total output is ~50 tokens whether successful or timed out.

When the watcher emits `DONE <path>`, resume Phase 2 immediately. The watcher's path output is informational; Phase 2 still re-lists `Review-*.md` itself and applies the freshness + scope partition described below. If the expected set has multiple reviewers and only one fired, Phase 2's reviewer-specific follow-up handles the rest.

When the watcher emits `TIMEOUT`, print `Auto-watch timed out after 60 min; reply 'done' when the reviewer finishes.` and resume the existing wait-for-user path. On explicit opt-out, user interrupt, or watcher launch failure, also fall through to the same wait-for-user path. The fallback is the unchanged Phase 1c → Phase 2 flow, so no Phase 2 logic depends on whether auto-watch was used.

## Phase 2: Intake Feedback

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
- Present the categorized list and confirm with the user before making changes.
- For follow-up questions within the same review round, prepare a short prompt the user can paste into the reviewer.

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
