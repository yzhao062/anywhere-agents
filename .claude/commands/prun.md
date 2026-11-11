---
description: "Run prun: parallel delegation fan-out on Agy workers, or Codex when you name it (the session coordinates)"
argument-hint: "[task description or context]"
---

Read and follow the skill definition. Look for it at `skills/prun/SKILL.md` first, then `.claude/skills/prun/SKILL.md`, then `.agent-config/repo/skills/prun/SKILL.md`.

Command arguments from the slash invocation: `$ARGUMENTS`

Treat the command arguments as the task to fan out. prun decomposes the task into independent units and runs many of them in parallel on Agy workers (Gemini through the Antigravity CLI, on the separate Google AI pool), never on this session. Do not spawn Sonnet or any other Claude subagent, or a Workflow, for a unit: those bill the same Claude account this session runs on. Every unit runs on Agy by default. A unit runs on Codex only when the user's request names Codex for that unit or a batch; this session never picks Codex on its own, not even as a fallback when Agy is short. A Codex unit goes through `scripts/dispatch-task.sh` (`dispatch-task.ps1` on Windows), and its model and effort come from the registry's `prun-worker` row for Codex. Each unit runs unattended in a scratch dir or throwaway clone and gets follow-up turns while slower units finish. Units may read or write code; code-writing units run in a throwaway local clone, workers never commit or push, and this session plus the user are the final integration gate. This session gathers the results, reviews each diff, and integrates. Choose the worker count from the actual dependency graph; do not impose an arbitrary two- or three-worker cap.
