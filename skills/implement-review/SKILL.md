---
name: implement-review
description: Review loop for staged changes. Detects content type, prepares a review request for Codex (terminal or plugin), categorizes feedback, revises, and iterates. Works for code, papers, proposals, or any text-based output.
---

# Implement-Review

## Overview

A review loop for staged changes. Claude Code detects the content type, sends the changes to Codex for review, categorizes the feedback, revises, and iterates. Works through two Codex channels: terminal relay (default on all platforms) or IDE plugin.

## When to plan-review first

**Any complex task benefits from a plan review BEFORE execution**, not only writing or code. Plan-first catches architectural holes while they are still cheap to fix. The scope includes: system design, refactors, paper outlines, proposal structure, data-pipeline redesigns, multi-stage debugging strategies, teaching / curriculum planning, release-process changes, migration plans, and anything else where the shape of the work precedes and constrains the execution.

**Plan-review is a Phase 0 before the staged-change loop below.** If the user asks for a plan review, or if the task clearly meets the signals below, do not apply the staged-change prerequisite in Phase 1 yet. Tell Codex to read the plan file directly (or paste the plan contents via the terminal path when Codex cannot access the file) and critique the design, not `git diff --cached`. After the plan has no High findings and no new design blockers, execute the work and resume the normal staged-output review flow at Prerequisites / Phase 1.

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
3. Send the plan through a plan-review prompt (not the staged-change template). Make clear this is a pre-execution design review and that the plan file path or pasted contents are what Codex should read; instruct Codex to critique the design rather than to run `git diff --cached`. Use the normal "Save your complete review to CodexReview.md" save-contract from Phase 1c.
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

Before sending staged changes for review, run automated checks that catch mechanical issues locally. This lets Codex focus on content and judgment calls instead of issues a script could find. Skip this phase if the user says to proceed directly, or if the project has no relevant tooling.

| Content type | Checks |
|---|---|
| LaTeX paper or proposal | Compile. Scan the log for overfull/underfull box warnings and undefined references. Report counts. |
| Anonymized submission | Grep staged files for author names, GitHub/lab URLs, institutional names, and tool names. Source these from the project's de-anonymization checklist if one exists; otherwise use the git user name, institution domain, and any names in the paper's author metadata or `\author{}` block. |
| Code | Run the project linter and type checker if configured. |

Report any findings to the user before proceeding to Phase 1. Findings here do not go to Codex; fix them locally first.

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
2. **Diff scope** -- list the changed files. Always tell Codex to run `git diff --cached` itself. Do not paste the diff inline; this keeps the prompt compact and avoids bloat across rounds.
3. **Review lens** -- the content-type-specific criteria from [references/review-lenses.md](references/review-lenses.md). If a focused sub-lens or agency-specific lens fits better than the full lens, use it (e.g., `paper/formatting` for a layout-only change, `proposal/nsf` when the agency is known). See the lens tables in that file.
4. **Additional focus** -- specific concerns beyond the generic lens. This is often the highest-value part of the prompt because it catches real bugs that generic criteria miss. **Always ask the user explicitly rather than guessing.** Recurring project concerns belong here: phased-development coupling, anonymization checks, page-limit compliance, budget-to-narrative consistency, terminology drift, benchmark-claim calibration, overclaim flagging. If there are no project-specific concerns this round, write "none" rather than padding the line. Examples: "check that all appendix URLs are anonymized", "verify Year 3 budget matches the narrative", "flag any overclaim in intro / conclusion", "watch for Phase 1 / Phase 2 coupling issues".
5. **Round number** -- which iteration this is (starting at 1).
6. **Variant targets (multi-target reviews)** -- if the staged files cover two or more variant targets that should be reviewed separately (long + short paper version, narrative + appendix tracker, internal + external report, primary + supplement), list each target by directory or file pattern. Tell Codex to review each target in its own top-level section and then add a cross-variant drift check at the end (tables that should match, claims that should be consistent, terminology that should align).
7. **Round history** (rounds 2+ only) -- a one-line-per-finding summary of what prior rounds raised and how each was resolved. Tag each finding as `Resolved`, `Still open`, or `Deferred`. This prevents Codex from re-litigating closed decisions and lets it verify that fixes landed instead of re-reviewing from scratch. Example:
   ```
   Prior findings:
   - DMP listed wrong project name (Resolved — fixed in round 1)
   - Budget table exceeds page width (Still open)
   - Consider reordering Section 3 (Deferred — user decision)
   ```

### 1c. Send to Codex

All review prompts sent to Codex (regardless of channel) must include a save instruction **at the very top of the prompt, before the summary or diff**, so Codex sees it first. This lets Claude Code read the feedback directly from the file, and lets the user read or forward it without copy-pasting from chat. The save instruction is:

> IMPORTANT: Save your complete review to `CodexReview.md` in the repository root. Overwrite any existing content. Use plain Markdown. Start the file with a `<!-- Round N -->` comment (matching the round number below) so the reader can verify freshness. **Begin the review with a short "Verification notes" section (paragraph or short bulleted list; "Validation notes" is also an accepted name) stating exactly what was compiled, run, or verified (e.g., `latexmk built cleanly`, `pytest pyod/test/... 5 passed`, `checked citation X against arXiv:YYYY`). If nothing was verified at runtime, write "Verification notes: none."** Separate findings into **New** (raised for the first time) and **Previously raised** (with status: Fixed, Still open, Reopened, or Deferred) sections. On Round 1, the Previously raised section may be omitted or shown as "None." Then include the file/diff scope, review lens, findings in priority order, and concrete recommended changes. **For any finding flagged High priority, include an exact suggested rewrite with file path and line range. Use a fenced code block for multi-line rewrites.** Do not skip this step. **For examples of the expected depth and format, see `skills/implement-review/references/example-reviews/`.**

**Terminal path**: Present a compact, copy-pasteable review prompt as a fenced text block. Keep the prompt under 20 lines. Tell Codex to read the diff itself (`git diff --cached`) rather than pasting it inline; this prevents prompt bloat as rounds accumulate. The abbreviated save instruction below inherits the full contract stated above (statuses, Round 1 behavior, required sections).

````
IMPORTANT: Save your complete review to CodexReview.md in the repository root. Overwrite any existing content. Start with <!-- Round N -->. Begin with a "Verification notes" paragraph or short bulleted list (what was compiled, run, or verified; "none" if nothing). Include file/diff scope and review lens. Separate findings into New and Previously raised (Fixed / Still open / Reopened / Deferred) sections. For High-priority findings, include an exact rewrite with file:line. See skills/implement-review/references/example-reviews/ for expected depth.

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

Then wait for the user to relay Codex's feedback or confirm that Codex has finished (see Phase 2 for how Claude Code picks up the review).

**Plugin path**: Tell the user the changes are ready for review and suggest what to tell Codex in the plugin. The suggestion inherits the full save contract stated above. Example:
> "Review the staged changes (round N). Focus on [detected lens]. Save your complete review to `CodexReview.md` in the repo root. Start the file with `<!-- Round N -->`. Begin with a `Verification notes` paragraph or short bulleted list (what you compiled, ran, or verified; 'none' if nothing). Include file/diff scope and review lens. Separate findings into New and Previously raised (Fixed / Still open / Reopened / Deferred) sections. For any High-priority finding, include an exact rewrite with file:line. If the diff spans two or more variant targets (long + short, narrative + tracker, internal + external), review each target in its own top-level section and add a Cross-variant drift check at the end."

Then wait for the user to relay Codex's feedback or confirm that Codex has finished.

## Phase 2: Intake Feedback

Codex is instructed to write its review to `CodexReview.md` in the repository root. When the user says Codex is done, read `CodexReview.md` to pick up the full feedback. Before trusting the file, verify that its `<!-- Round N -->` comment matches the current round number.

If the file is missing, empty, or carries a stale round marker:
1. Present a short follow-up prompt the user can paste into Codex: `Save your review to CodexReview.md in the repo root. Overwrite any existing content. Start with <!-- Round N -->. Begin with a "Verification notes" paragraph or short bulleted list. Separate findings into New and Previously raised (Fixed / Still open / Reopened / Deferred) sections. For High-priority findings, include an exact rewrite with file:line.`
2. If the file is still missing, still empty, or still carries a stale round marker after the follow-up, ask the user to paste the feedback directly.

- When feedback arrives (from `CodexReview.md` or relayed by the user), acknowledge each point.
- If Codex separated findings into "New" and "Previously raised" sections, verify the classifications. If Codex did not separate them (older prompts or non-compliance), do the separation yourself based on the round history.
- Categorize each **new** point as:
  - **Will fix** -- clear, actionable, and correct.
  - **Needs discussion** -- ambiguous or potentially wrong; ask the user before acting.
  - **Disagree** -- explain why and let the user decide.
- For **previously raised** points, check the status Codex assigned:
  - **Fixed** -- Codex confirms the prior finding was addressed. No action needed.
  - **Still open** -- the fix did not land or was incomplete. Treat as "will fix" unless the user overrides.
  - **Reopened** -- Codex re-raises a point that was marked Resolved. Flag to the user: this needs a decision, not silent re-litigation.
  - **Deferred** -- the user chose not to address this. Codex acknowledges it as unchanged. No action unless the user reconsiders.
- Present the categorized list and confirm with the user before making changes.
- For follow-up questions within the same review round, prepare a short prompt the user can paste into Codex.

## Root Review Sink

When a review produces substantial written feedback, save the latest review to `CodexReview.md` in the repository root in addition to replying in chat. Treat this file as a reusable scratch file for the current review round, not as a permanent archive. By default, overwrite the file completely on each new saved review rather than creating per-directory review files or appending multiple rounds, unless the user explicitly asks to preserve history.

The purpose of `CodexReview.md` is to let the user read, reuse, and forward the latest review without copy-pasting from chat. Keep the file in plain Markdown and make it directly useful on its own. Include:

- a `<!-- Round N -->` HTML comment on the first line (used by Phase 2 to verify freshness)
- a `Verification notes` paragraph or short bulleted list at the top of the review (immediately after `# Review`), stating what was compiled, run, or verified; write "none" if nothing at runtime
- the file or diff scope reviewed
- the review lens or context
- findings separated into **New** and **Previously raised** sections (previously raised items tagged Fixed, Still open, Reopened, or Deferred; on Round 1 the Previously raised section may be omitted or shown as "None")
- concrete recommended changes, with exact values when relevant
- for any finding flagged High priority, an exact suggested rewrite with file path and line range (use a fenced code block for multi-line rewrites)

Do not stage, commit, or move `CodexReview.md` unless the user explicitly asks. Before the first review round, check whether `CodexReview.md` is excluded from git. Look in `.gitignore` and `.git/info/exclude`. If it is not excluded anywhere, append `CodexReview.md` to `.git/info/exclude` (a local, untracked ignore file) so that `git add -A` during the revision flow does not accidentally stage the scratch file. Do not edit `.gitignore` for this purpose, as that would introduce a tracked side-effect inside the review loop.

## Phase 3: Revise

- Address all "will fix" points and any "needs discussion" points the user approved.
- Update the round history: mark addressed findings as `Resolved`, keep unaddressed ones as `Still open`, and tag user-deferred items as `Deferred`. This history carries forward into the next round's prompt (Phase 1b, item 7).
- Stage the revised changes.
- Return to Phase 1 with an incremented round number.

## Phase 4: Conclude

The loop ends when:
- The user says the review is done or approved.
- Codex's feedback has no actionable issues.
- The user decides to stop iterating.

At conclusion, present a short summary: total rounds, key changes made, and any unresolved points from the last review.

## When Not To Use

- Trivial changes where review adds no value (typo fixes, config tweaks).
- Changes that require running tests or builds to validate -- run those first, then review.
- When the user wants a single-shot review with no revision loop; just ask Codex directly.
