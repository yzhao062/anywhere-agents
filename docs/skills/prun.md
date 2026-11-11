# prun

Fan out independent task units to Agy workers while the coordinating Claude session integrates. Every unit runs on the Google AI plan by default, on the model and effort of the registry's Agy `prun-worker` row (`user/agent-models.json`, see the `agent-models` skill), unattended in a scratch directory or throwaway clone. No Claude subagent or Workflow agent runs a `prun` unit, because those bill the same Claude account the coordinating session runs on. A unit runs on a Codex worker only when the user names Codex for it, through `dispatch-task.sh` or `dispatch-task.ps1`, and the coordinator never picks Codex on its own, not even when Agy is short. Each dispatcher writes `<state-dir>/model-actual`, the requested model and effort next to what the CLI reported, and prints `MODEL-MISMATCH` or `MODEL-UNVERIFIED` on stderr. Inputs a unit needs from session tools are gathered before dispatch; a task that needs those tools throughout belongs outside `prun`. Unit count follows the dependency graph instead of a small fixed cap, code-writing workers use throwaway clones, and the session plus the user remain the final integration gate.

{%
   include-markdown "../../skills/prun/SKILL.md"
   start="## Overview"
%}
