# prun

Fan out independent task units to Sonnet and Agy workers while the coordinating Claude session integrates. Sonnet is the in-session default; Agy supplies Gemini 3.8 Flash High at `high` effort through a separate Google AI quota pool. Codex is reserved for `/vet`, not `prun`. Unit count follows the dependency graph instead of a small fixed cap, code-writing workers use throwaway clones, and the session plus the user remain the final integration gate.

{%
   include-markdown "../../skills/prun/SKILL.md"
   start="## Overview"
%}
