---
description: "Run prun: parallel delegation fan-out on Agy workers, or Codex or Grok when you name it (the session coordinates)"
argument-hint: "[task description or context]"
---

Read and follow the skill definition. Look for it at `skills/prun/SKILL.md` first, then `.claude/skills/prun/SKILL.md`, then `.agent-config/repo/skills/prun/SKILL.md`.

Command arguments from the slash invocation: `$ARGUMENTS`

Treat the command arguments as the task to fan out, with Agy (Gemini through the Antigravity CLI) as the default worker for every unit. Codex or Grok runs a unit only when the user's request names it for that unit or a batch; this session never picks either one, not even as a fallback when Agy is short. Never run a unit as a Claude-side subagent or a Workflow, since those bill the account this session runs on. The full rules, the dispatch commands, and the reference index (open a reference only when its read condition holds) are in `skills/prun/SKILL.md`.
