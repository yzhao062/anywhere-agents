# implement-review

Structured dual-agent review loop that sends staged changes to a reviewer agent (e.g., Codex) and iterates until findings are resolved. Content-type-aware lenses apply established review criteria from the Google / Microsoft engineering playbooks (code), NeurIPS / ICLR / ICML / ACL guidelines (papers), and the NSF Merit Review / NIH Simplified Peer Review frameworks (proposals).

```mermaid
%%{init: {'theme': 'base', 'themeVariables': { 'primaryColor': '#fdf5f6', 'primaryBorderColor': '#8b2635', 'primaryTextColor': '#1a1a1a', 'lineColor': '#8b2635'}}}%%
flowchart LR
    A([you: &quot;/implement-review auto&quot;]) --> B[Claude stages<br/>the diff]
    B --> C[Codex reviews<br/>via Auto-terminal<br/>or Terminal-relay]
    C --> D[/Review-Codex.md<br/>High · Med · Low/]
    D --> H[Phase 2.0 health-check<br/>+ Phase 2.5 verify Highs]
    H --> E[Claude applies<br/>fixes, re-stages]
    E --> F{clean?}
    F -->|no, loop| C
    F -->|yes| G([merged])
```

See [Example reviews](references/example-reviews/example-code-phased.md) for complete worked examples across code, paper, and proposal lenses.

{%
   include-markdown "../../skills/implement-review/SKILL.md"
   start="## Overview"
%}
