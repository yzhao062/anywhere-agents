# Skills

`anywhere-agents` ships five skills. Each is a self-contained capability with its own `SKILL.md` definition. The router (`my-router`) dispatches automatically for routing-table matches; `prun` is invoked by explicit parallel fan-out intent.

<div class="grid cards" markdown>

-   :material-magnify-scan:{ .lg .middle } &nbsp; __`implement-review`__

    ---

    Structured dual-agent review loop with content-type lenses. Codex reviews, Claude applies fixes, iterates until clean.

    [:octicons-arrow-right-24: Deep docs](implement-review.md)

-   :material-routes:{ .lg .middle } &nbsp; __`my-router`__

    ---

    Context-aware dispatcher. Picks the right skill from prompt keywords, file types, and directory hints.

    [:octicons-arrow-right-24: Deep docs](my-router.md)

-   :material-image-frame:{ .lg .middle } &nbsp; __`ci-mockup-figure`__

    ---

    HTML mockups of systems, dashboards, timelines → space-efficient PNG / PDF figures via headless Chrome.

    [:octicons-arrow-right-24: Deep docs](ci-mockup-figure.md)

-   :material-book-open-outline:{ .lg .middle } &nbsp; __`readme-polish`__

    ---

    Audit and rewrite GitHub READMEs using modern 2025-2026 patterns (badges, hero, callouts, collapsibles).

    [:octicons-arrow-right-24: Deep docs](readme-polish.md)

-   :material-call-split:{ .lg .middle } &nbsp; __`prun`__

    ---

    Parallel delegation fan-out where Opus coordinates Codex and Sonnet workers that spend their separate quotas.

    [:octicons-arrow-right-24: Deep docs](prun.md)

</div>

## Skill lookup order

When a skill is invoked, the agent looks for its `SKILL.md` in this order:

1. **Project-local**: `skills/<name>/SKILL.md` in the consuming project. Use this to override a shared skill or add project-specific skills.
2. **Pack-deployed**: `.claude/skills/<name>/SKILL.md` installed by `anywhere-agents pack install`. The `.claude/` prefix is a historical Claude Code convention; the SKILL.md contents are agent-agnostic.
3. **Bootstrapped shared**: `.agent-config/repo/skills/<name>/SKILL.md` in the consuming project (synced by bootstrap).
4. **Plugin-provided**: agent-platform plugins may provide skills too (e.g., Claude Code plugins).

The router follows the same lookup order.

## Adding your own skill

In a fork of `anywhere-agents`:

1. Create `skills/<your-skill>/SKILL.md` with the skill definition.
2. Add `agents/openai.yaml` for Codex invocation and `.claude/commands/<your-skill>.md` for Claude Code invocation.
3. Register the skill in `skills/my-router/references/routing-table.md` with triggering keywords, file types, and directory hints.
4. Add regression tests where appropriate under `tests/`.

See the five shipped skills for reference structure.
