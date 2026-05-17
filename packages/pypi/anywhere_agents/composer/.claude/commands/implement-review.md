---
description: Run the implement-review staged-change review loop
argument-hint: "[auto|cli|auto-terminal|manual|plugin] [focus...]"
---

Read and follow the skill definition. Look for it at `skills/implement-review/SKILL.md` first, then `.agent-config/repo/skills/implement-review/SKILL.md`.

Command arguments from the slash invocation: `$ARGUMENTS`

Treat the command arguments as part of the user's current task.
`auto`, `cli`, and `auto-terminal` opt into the Auto-terminal channel.
`manual`, `back to manual`, and `use terminal-relay` force Terminal-relay.

Apply it to the user's current task. Also read the supporting files under the skill's references/ directory as needed.
