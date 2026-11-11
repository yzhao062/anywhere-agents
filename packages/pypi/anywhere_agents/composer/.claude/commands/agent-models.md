---
description: "Show, check, or change the model and effort each agent role runs on (one registry)"
argument-hint: "[show | doctor | verify | set <family> [role] <model> <effort>]"
---

Read and follow the skill definition. Look for it at `skills/agent-models/SKILL.md` first, then `.claude/skills/agent-models/SKILL.md`, then `.agent-config/repo/skills/agent-models/SKILL.md`.

Command arguments from the slash invocation: `$ARGUMENTS`

With no arguments, run `show` and `doctor` and report the result. `show`, `doctor`, and `verify` map to the resolver commands of the same name. `set` changes the registry and is done only in a source repository, following the skill's "Change a model or an effort" steps; it ends at the user's approval of the commit.
