---
description: "Run prun: parallel delegation fan-out (Codex-primary, Opus coordinates)"
argument-hint: "[task description or context]"
---

Read and follow the skill definition. Look for it at `skills/prun/SKILL.md` first, then `.claude/skills/prun/SKILL.md`, then `.agent-config/repo/skills/prun/SKILL.md`.

Command arguments from the slash invocation: `$ARGUMENTS`

Treat the command arguments as the task to fan out. prun decomposes the task into independent units and runs many of them in parallel on Codex and Sonnet workers (never on Opus), to spend the Codex/Sonnet quotas instead of the constrained Opus one. Units may read or write code; code-writing units run in a throwaway local clone, workers never commit or push, and Opus plus the user are the final integration gate. Opus gathers the results, reviews each diff, and integrates.