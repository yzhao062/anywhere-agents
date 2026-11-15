# Plan Review
Read this when the user asks for a plan review, or a plan-first signal holds.

## Process

1. Write the plan to a scratch file `PLAN-<identifier>.md` in the most natural location for the task (repo root for code, paper-repo root for Overleaf-style docs, a local scratch directory beside the deliverable for tasks that do not live in git). If the plan lands inside a git worktree, add it to `.git/info/exclude` so `git add -A` does not accidentally stage it; outside git, keep it as a clearly named scratch file outside the final deliverable and delete it after review.
2. Content varies by task but at minimum include: purpose, non-goals, structure, regression or failure analysis, validation plan, open questions. Keep it terse -- 1 to 3 pages.
3. Send the plan through a plan-review prompt (not the staged-change template). Make clear this is a pre-execution design review and that the plan file path or pasted contents are what the reviewer should read; instruct the reviewer to critique the design rather than to run `git diff --cached`. A reviewer with direct repository access reads the named files once. A snapshot backend (Antigravity) sees only the exported index, so a gitignored plan, an untracked evidence note, or an untracked local override is pasted into the request once, each under a `--- SUPPLIED INPUT: <path> ---` heading that names its source path; the reviewer reads the supplied copy and never the original worktree. Use the normal "Save your complete review to Review-<AgentName>.md" save-contract from section 1c (Send to reviewer).
4. **Reviewer must answer the scope-challenge questions** (see "Adversarial scope challenge" below) before any in-scope correctness review.
5. Iterate until the review has no High findings and no new design blockers.
6. Then execute (code, draft, revise, deploy).
7. Run the normal review cycle on the staged output. It is typically smaller because the architecture was already validated.
8. After the work ships or is submitted, delete the PLAN file.

## Adversarial scope challenge (mandatory in plan-review)

Plan-review **must** be adversarial about plan purpose and shape, **not** about low-probability edge cases. The single biggest plan-review failure mode in this maintainer's history was not "the plan had a bug" — it was "the plan's scope was over-conservative, deferring user value across an extra release cycle worth of process tax." Examples:

- Conservative scope (`v0.5.7 = ref bump only; defer compact to v0.6.0`) → the user's real projects would have stayed on large `AGENTS.md` files for another release cycle. The apparent product ask was a one-line bundled manifest flip, but existing-consumer delivery also required bundled-default drift detection in aa plus real-project upgrade tests. The scope challenge should force reviewers to price both parts: user value now, and the smallest code path that actually delivers it.
- Conservative deferrals also defer the validation that proves the next-step works in real consumers, so the next release inherits the same uncertainty plus a longer review chain.

The plan-review prompt must instruct the reviewer to take an explicit position on:

1. **Why this exact scope?** What user pain (or user opportunity) does THIS scope close? Could a strictly smaller scope close most of it? Could a marginally larger scope close all of it for low marginal cost?
2. **Are deferrals justified?** Every "out of scope / deferred to vNext" carries process tax: another full plan-review + implement + execution-review + CI + publish cycle. Quantify the deferral. If the deferred axis is 1 line of code and the user pain is real, the deferral is probably wrong.
3. **Is this plan the simplest path?** Are there simpler approaches the plan didn't consider — including doing nothing if the user pain is hypothetical?

Plan-review must **not** default to "no blocker, ready to implement" on the first round when scope is multi-axis or when the plan defers a user-facing axis. The reviewer must either raise a scope challenge OR explicitly state why the scope is calibrated. Skipping the explicit position on these three questions counts as an incomplete review.

Keep the challenge proportionate to the plan's real risks:

- **Edge cases**: raise one only when its realistic probability × impact exceeds the cost of reviewing it; "what if the user has an unusual config Y?" usually does not.
- **Scope additions**: ground each "should this also do Z?" in a user pain or opportunity.
- **Calibrated scope**: when the scope is calibrated, say so and accept it; propose an expansion only when its user benefit is proportional to its cost.

The `Scope-challenge focus:` line of the prompt template in section 1c (Send to reviewer) carries this contract into every plan-review send.

## Illustrative examples

- System / code: hook or infra design, cross-cutting refactor, state-file schema, cross-platform behavior, release runbook revisions.
- Research output: paper outline with specific aims, contribution claims before methods is written, figure-placement vs argument flow, reviewer response strategy, experiment design across multiple methods, ablation plan.
- Proposal: full outline (aims alignment with merit-review criteria), budget-narrative coupling, broader-impacts framing.
- Operational: migration plan, incident-response playbook, data-pipeline redesign.
- Administrative / teaching: course syllabus structure, lab policy document, committee process design.

The point is not which category -- it is whether the shape of the work precedes and constrains the execution.
