# prun

Fan out independent task units to Agy workers while the coordinating Claude session integrates. Every unit runs on the Google AI plan, by default on the newest Gemini Flash High at `high` effort, unattended in a scratch directory or throwaway clone. No Claude subagent or Workflow agent runs a `prun` unit, because those bill the same Claude account the coordinating session runs on, and Codex is reserved for `/vet`. Inputs a unit needs from session tools are gathered before dispatch; a task that needs those tools throughout belongs outside `prun`. Unit count follows the dependency graph instead of a small fixed cap, code-writing workers use throwaway clones, and the session plus the user remain the final integration gate.

{%
   include-markdown "../../skills/prun/SKILL.md"
   start="## Overview"
%}
