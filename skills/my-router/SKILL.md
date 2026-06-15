---
name: my-router
description: Context-aware router that detects work type and dispatches to the right skill. Ships with a minimal default routing table; extend it in a fork, or register project-local skills in a consuming repo via routing-table.local.md or an AGENTS.local.md Routing section.
---

# My Router

## Overview

A routing layer that sits between the outer workflow (e.g., `superpowers` brainstorm/plan/execute/verify) and domain skills. The router reads the working directory, file types, and user prompt to decide which skill to invoke, so the user does not need to remember skill names.

In this repo's shipped form, the routing table has concrete entries for the four shipped skills (`implement-review`, `ci-mockup-figure`, `readme-polish`, plus `my-router` itself). It is also designed as a **pattern you extend**. A fork of this repo edits `references/routing-table.md` directly. A consuming project, where that file is overwritten on every bootstrap, instead registers its own skills in a bootstrap-proof local file (`routing-table.local.md` at the repo root, or a `## Routing` section in `AGENTS.local.md`); the router reads those rows and dispatches to them. See [Extending the Router](#extending-the-router).

## When to Use Superpowers vs. Direct Dispatch

The router decides this. Not all tasks need superpowers' full ceremony.

| Task shape | Route | Why |
|---|---|---|
| Clear, scoped task with an obvious skill match (e.g., "review staged changes") | **Direct dispatch** — router picks the domain skill and runs it immediately | Brainstorming and planning add no value when the task is already well-defined |
| Open-ended, multi-step, or ambiguous task (e.g., "build a new feature", "restructure the paper") | **Superpowers first** — brainstorm → plan → execute (router dispatches during execute) → verify | These benefit from thinking before doing |
| Quick edit or fix (e.g., "fix the typo on line 42", "rename this variable") | **Neither** — just do it directly | No routing or workflow needed |
| Effort signal words in prompt (e.g., "extensively", "deep", "thorough", "in-depth", "carefully", "comprehensive") | **Superpowers + extended thinking** — enable extended thinking (`Alt+T`) and route through superpowers regardless of task shape | The user is explicitly asking for more deliberation |

The rule: **if the domain skill is obvious and the scope is clear, skip superpowers and dispatch directly. If the task needs exploration or planning, let superpowers run the outer loop and the router dispatches during execution. If the user signals they want deep effort, always use superpowers with extended thinking enabled.**

## Integration with Superpowers

When superpowers is active, it handles workflow phases: brainstorm → plan → execute → verify. The router activates during the **execute** phase and dispatches to the right domain skill.

When superpowers is not active (direct dispatch or quick task), the router works standalone.

## How Routing Works

At dispatch time, the router checks three signals in order (keywords, file types, project structure). In a consuming project, before applying the shipped table below, it first merges any **consumer-local routing extensions**: a `routing-table.local.md` at the repo root, or a `## Routing` section in `AGENTS.local.md`. These two files survive bootstrap (the shipped table does not), so they are where a consuming repo registers its own skills; on a keyword or file-type conflict, the local row wins. See [Extending the Router](#extending-the-router).

### 1. Prompt keywords (highest priority)

The user's prompt often contains the clearest signal. The shipped routing table includes keyword entries for `implement-review`, `ci-mockup-figure`, and `readme-polish`. Add entries for your own skills in your fork's `references/routing-table.md`, or, in a consuming project, in a bootstrap-proof local file (see [Extending the Router](#extending-the-router)).

See [`references/routing-table.md`](references/routing-table.md) for the current table and the extension template.

### 2. File types in working directory

If prompt keywords are ambiguous, inspect the files being worked on. The shipped router recognizes staged git changes → `implement-review`, HTML mockup files for dashboards/timelines → `ci-mockup-figure`, and a top-level `README.md` flagged for polish → `readme-polish`. Add your own file-type rules when you add new skills.

### 3. Project structure hints

Some projects declare their type in `AGENTS.local.md` or via directory naming conventions (e.g., a `proposals/` or `papers/` directory, or a submodule pointing at a shared editorial repo). Use these hints to pick content-aware behavior when relevant to your skills.

## Dispatch Rules

1. **Local-first override**: before dispatching, scan `skills/` (project-local) for any skill that is a more specific variant of the matched skill. If a local variant exists, use it instead of any pack-deployed or bootstrapped copy.
2. **If a prompt keyword matches** → invoke that skill.
3. **If file context matches but prompt is vague** (e.g., "help me with this") → state the detected context and proposed skill, ask the user to confirm before proceeding.
4. **If multiple skills could apply** → state the candidates and ask the user to choose.
5. **If nothing matches** → fall through to superpowers or general agent behavior. Do not force a skill where none fits.

## Skill Lookup Order

In consuming project repos, skills can be project-local, pack-deployed, or bootstrapped from shared config. When dispatching, look for each skill in this order:

1. `skills/<name>/SKILL.md`: project-local (highest priority). Projects can add their own skills here.
2. `.claude/skills/<name>/SKILL.md`: pack-deployed by `anywhere-agents pack install`. The `.claude/` prefix is a historical Claude Code convention; the SKILL.md contents are agent-agnostic.
3. `.agent-config/repo/skills/<name>/SKILL.md`: bootstrapped from the shared config repo.
4. **Installed plugins**: agent-specific plugin skills (e.g., Claude Code plugins), check `/skills` output.

If a project-local skill matches the task better than a pack-deployed or bootstrapped skill, prefer the project-local one. The router itself follows this same lookup order.

## Extending the Router

Where you register a new skill depends on whether you own this repo or only consume it. The mechanism is the same: a routing row keyed on prompt keywords, file types, and directory hints. Only the location differs, and it must survive your config-refresh path.

**In a fork of this repo** (you own the shipped table):

1. Add the skill directory under `skills/<your-skill>/`.
2. Add a row to `references/routing-table.md`.
3. Add a matching `.claude/commands/<your-skill>.md` pointer so Claude Code can invoke it directly.

**In a consuming project** (this skill arrives via bootstrap or `anywhere-agents pack install`):

Do **not** edit `references/routing-table.md` here. Every on-disk copy (`.agent-config/repo/skills/my-router/`, `.claude/skills/my-router/`) is overwritten on the next bootstrap or `pack verify --fix`. Register your skill where bootstrap never reaches:

1. Add the skill directory under `skills/<your-skill>/` (project-local, bootstrap-proof by construction).
2. Register its routing in **either** of these bootstrap-proof files:
   - `routing-table.local.md` at the repo root, using the same table format as `references/routing-table.md`; or
   - a `## Routing` section in `AGENTS.local.md`, convenient when you already keep project overrides there.
3. Optionally add a project-local `.claude/commands/<your-skill>.md` pointer. The bootstrap copy step is non-destructive and never deletes command files absent from upstream, so a consumer-only pointer survives.

At dispatch time the router merges these local rows on top of the shipped table; on a keyword or file-type conflict, the local entry wins. Both `routing-table.local.md` and `AGENTS.local.md` sit outside the set of files bootstrap rewrites, so the registration persists across refreshes.

## Combining with Implement-Review

When review is needed after a domain skill finishes:

1. Domain skill runs (e.g., code changes, paper edits)
2. Changes are staged
3. Router dispatches to `implement-review` with the appropriate lens (code, paper, proposal, general)
4. Reviewer applies the lens-specific criteria

See `implement-review/SKILL.md` for the review loop protocol.

## Examples

**User says:** "Review this"
→ Router detects: staged changes exist → dispatches to `implement-review`; content-type lens (code, paper, proposal, general) selected based on staged files.

**User says:** "Build the feature and review it"
→ Router detects: code context → superpowers handles the build, then router dispatches to `implement-review` with code lens.

**User says:** "Make an HTML mockup for the method figure"
→ Router detects: keyword "mockup" → dispatches to `ci-mockup-figure`.

**User says:** "Polish the README with modern patterns"
→ Router detects: keyword "polish README" → dispatches to `readme-polish`.

**User says:** (anything else, shipped router has no rule)
→ Router falls through to superpowers or general agent behavior. Add more rules in `references/routing-table.md` (in a fork) or in `routing-table.local.md` / an `AGENTS.local.md` `## Routing` section (in a consuming project).
