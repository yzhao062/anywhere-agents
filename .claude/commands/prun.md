---
description: "Run prun: parallel delegation fan-out (Sonnet-primary, Opus coordinates)"
argument-hint: "[task description or context]"
---

Read and follow the skill definition. Look for it at `skills/prun/SKILL.md` first, then `.claude/skills/prun/SKILL.md`, then `.agent-config/repo/skills/prun/SKILL.md`.

Command arguments from the slash invocation: `$ARGUMENTS`

Treat the command arguments as the task to fan out. prun decomposes the task into independent units and runs many of them in parallel on Sonnet and Agy workers (never on Opus). Sonnet is the in-session default; Agy supplies Gemini through the separate Google AI pool. Codex is reserved for `/vet` and is not a prun executor. Units may read or write code; code-writing units run in a throwaway local clone, workers never commit or push, and Opus plus the user are the final integration gate. Opus gathers the results, reviews each diff, and integrates. Choose the worker count from the actual dependency graph; do not impose an arbitrary two- or three-worker cap.
