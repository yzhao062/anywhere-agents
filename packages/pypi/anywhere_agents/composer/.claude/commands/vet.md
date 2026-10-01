---
description: "Vet the staged change: run the implement-review review loop (short alias)"
argument-hint: "[both|agy|claude|copilot|codex] [auto|cli|auto-terminal|manual|plugin] [focus...]"
alias-of: implement-review
---

Read and follow the skill definition. Look for it at `skills/implement-review/SKILL.md` first, then `.claude/skills/implement-review/SKILL.md`, then `.agent-config/repo/skills/implement-review/SKILL.md`.

Command arguments from the slash invocation: `$ARGUMENTS`

Treat the command arguments as part of the user's current task.
`auto`, `cli`, and `auto-terminal` opt into the Auto-terminal channel.
`manual`, `back to manual`, and `use terminal-relay` force Terminal-relay.
With Auto-terminal as the user default, `/vet agy` selects Antigravity directly;
`gemini` and `antigravity` remain aliases. No reviewer token selects Codex.
`/vet both` runs the two of Claude, Codex, and Agy that are not coordinating this session, for the same round.
That is Codex and Agy under Claude Code, Claude and Agy under Codex, and Codex and Claude under Agy.
The two run in parallel when memory allows and one after the other otherwise.
When the pair includes Claude, every staged path must be in scope, because the Claude backend reviews the whole staged diff.

Apply it to the user's current task. Also read the supporting files under the skill's references/ directory as needed.
