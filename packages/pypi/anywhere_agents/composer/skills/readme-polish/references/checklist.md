# README Audit Checklist

Run this grid against a candidate README before editing. Mark each row `✓` (present and correct), `✗` (absent or broken), or `N/A` (does not apply to this project).

## First-paint (above the fold)

| Item | Notes |
|------|-------|
| Centered project name in `<div align="center">` | Modern OSS convention; left-align is the old style. |
| One-line tagline directly under the name | Under 15 words. Survives without context. |
| Badge row (4-6 badges) | PyPI, npm, license, CI status, stars. Not more. |
| Dot-separated nav links | Optional but helpful on READMEs over 100 lines. |
| Hero image, diagram, or screenshot | At least one visual above the elevator pitch. |
| Maintainer credibility as `> [!NOTE]` callout, not paragraph | Keep to 2-3 sentences, verifiable claims only. |
| Elevator pitch paragraph (1-3 sentences) | What the project is, who it is for. |
| Organizing concept visible (category table or a one-line map of the parts) | A feature list without structure reads as unfocused. Library READMEs use a category table. |

## Install section

| Item | Notes |
|------|-------|
| Primary install command visible without scrolling | The single most important line. |
| Platform-specific variants collapsed in `<details>` | Do not show all three OSes above the fold. |
| Prerequisites listed (or explicitly none) | Reader should know if they need Python, Node, Docker, etc. |
| Example invocation right after install | "Run this to verify it works." |
| Direct-from-agent phrasing (if applicable) | `> [!TIP]` callout: "tell your agent to install it." |

## What you get / Features (or Why You'd Use This)

| Item | Notes |
|------|-------|
| Emoji-prefixed bullets *or* bold-lead paragraphs (matched to content shape) | 5-8 emoji bullets when items are feature claims; 3-5 `**Bold lead.** Setup. Without X, problem. With X, fix.` paragraphs when items are narrative scenarios. Pick one pattern per section. |
| No multi-sentence prose bullets | If a feature needs more than one sentence, switch to bold-lead paragraphs for that section, or link to a follow-up section / collapsible. |
| No overlap between feature bullets and hero image content | If the hero image already shows the 6 features, either cut the bullets or make them much shorter. |
| "Coming next" boundary paragraph (if applicable) | One short paragraph at the end of the section naming what is queued for the next release. Keeps current vs roadmap separated. |

## "What This Looks Like" examples block (optional)

| Item | Notes |
|------|-------|
| 3-5 examples between How It Works and Install | Concrete proof of what the project looks like in operation. |
| Each example uses a different visual format | Screenshot / file tree / themed Mermaid / HTML 2-col before-after / terminal mock. Variety is the point — three monospace blocks in a row read as a text wall. |
| Captions are 1-2 sentences | Long captions defeat the "skim in 10 seconds" goal of the section. |
| HTML 2-col `<table>` for rich before/after | Markdown tables cannot carry blockquotes, italic, or `<mark>` tags inside cells; HTML tables can. One per README is enough. |
| Themed Mermaid (`%%{init: ...}%%`) when used | Default red / orange / blue clashes with most brand colors. Match the project palette. |

## Reference material (should be collapsed)

| Item | Should be in `<details>`? |
|------|----------------------------|
| Platform-specific install variants | Yes |
| Repo layout / file tree | Yes |
| Related projects / alternatives | Yes |
| Limitations and caveats | Yes |
| Maintenance policy | Yes |
| "What this is not" | Yes |
| FAQ | Yes |
| Detailed configuration options | Depends — if only a power-user concern, collapse. |
| Quickstart | No, never collapse. |
| What you get / features | No, never collapse. |

## Callout discipline

| Item | Notes |
|------|-------|
| `> [!NOTE]` used for maintainer callout only | One per major section max. |
| `> [!TIP]` used only for "agent-install" or "pro tip" moments | Optional. |
| `> [!WARNING]` / `[!CAUTION]` reserved for genuine hazards | Breaking changes, destructive operations. Not decorative. |
| First source line is exactly `> [!TYPE]` (one `>`, one tag, alone) | Any extra prefix or missing `>` breaks the callout. |

## Anchor and link hygiene

| Item | Notes |
|------|-------|
| Every dot-nav link resolves to a real heading | GitHub autogenerates anchors from headings. Verify manually. |
| No markdown links to files that do not exist | `[CONTRIBUTING](CONTRIBUTING.md)` requires the file. |
| External links use HTTPS | Mixed-content issues on HTTPS pages otherwise. |
| Image alt text is descriptive | Not just "hero" or "logo". |
| Badge URLs use correct registry / package names | Shield.io is case-sensitive for package names. |

## Render verification

| Item | Notes |
|------|-------|
| Pushed to a branch and viewed on GitHub | PyCharm / VS Code previews miss `> [!NOTE]` and Mermaid. |
| `> [!NOTE]` renders as a colored box (not a plain blockquote) | If it looks like a quote, the syntax is wrong. |
| Mermaid diagrams render (not as raw text) | If raw text, the fence language is wrong or renderer disabled. |
| Hero image loads (not a broken-image icon) | A relative path must be correct from the repo root; use an absolute URL when the README is also the PyPI long description. |
| Badges show live data (not "image not found") | Shield.io URL must be well-formed. |
| Collapsibles expand cleanly | No content leaks outside the `<details>` tag. |
| PyPI long description renders (when the README is one) | Absolute URLs for images and repository files, heading links (`#section-slug`) left as they are, no links to hand-written `id` or `name` anchors, no relative paths in the built `METADATA`, a `readme_renderer[md]` preview. |

## Hygiene

| Item | Notes |
|------|-------|
| `git diff --cached --check` passes | No trailing whitespace. |
| Tables are GitHub-flavored (pipes, not grid format) | Grid format does not render. |
| Code blocks have language hints (` ```bash `, ` ```python `) | Syntax highlighting only fires with hints. |
| Line lengths reasonable (under ~120 chars for prose) | Long lines are fine in code blocks. |
| Title Case across all H2 and H3 (agent-style RULE-G) | Sentence case mixed with Title Case reads as machine-generated. |
| Generated blocks match the code | The drift test passes after the last edit (method counts, result tables, numbers in hero alt text). |
| No casual em-dash (agent-style RULE-B) | Appositives use commas, parens, or colons. En-dash in numeric ranges is fine. |

## Visual identity (when a brand color is in play)

| Item | Notes |
|------|-------|
| Mermaid blocks include `%%{init: ...}%%` themed to brand palette | Otherwise they render as cookie-cutter and clash with the rest of the page. |
| README badges use the brand color via `?color=<hex>` | One color across all badges; do not mix Shield.io defaults with custom brand colors. |
| MkDocs CSS overrides match brand color (light + slate) | If the project ships a Read the Docs site, both schemes need the brand color. |
| Custom-colored elements pass WCAG AA contrast (≥ 4.5:1) | Run a contrast check (Codex / Copilot / a contrast tool) for both light and slate themes. The dark-theme link contrast is the most common failure mode. |

## Reproducibility (when the README references rendered assets)

| Item | Notes |
|------|-------|
| Source file (HTML, vhs tape, PPTX) committed alongside the rendered output | Without the source, the asset becomes a static artifact no one can update. |
| Render helper script committed, when one is used (`docs/_render_*.py` / `_render_*.sh`, a figure builder) | One-shot reproducibility for future maintainers. A PPTX edited by hand needs recorded export steps instead. |
| Docker images pinned by content digest, not `:latest` | `vhs@sha256:...` not `vhs:latest`; otherwise the next render pulls a different image. |
| Render command or export steps work on a clean clone | Confirm by re-running the helper, or repeating the export, after the rewrite. |

## Cross-surface parity (multi-surface projects)

| Item | Notes |
|------|-------|
| Tagline matches across README, RTD landing, and bilingual variants | Drift between surfaces is a real maintenance cost. |
| How It Works claims agree across surfaces | Especially version boundaries (current vs roadmap). |
| Pack-CLI / install / What's Next paragraphs match | Codex / Copilot review handles this via "Cross-variant drift check" lens. |
| Bilingual: technical terms preserved in source language | English `pack`, `composer`, `bootstrap` stay English in zh-CN; only natural-equivalent concepts translate. |
| Bilingual: example bodies in source language, commentary translated | Don't translate banned-word examples that demonstrate language-specific patterns; translate the prose around them. |
| Bilingual: informal pronoun (`你`, `tu`) for warmth | Formal pronoun reads as machine-translated. |

## Content accuracy (cannot be linted)

| Item | Notes |
|------|-------|
| Every version number mentioned matches the current release | Package version, minimum versions, release-date claims, a `New in X.Y` line under the header. |
| Every feature claim matches what the code actually does | A README promising X must correspond to shipped X. |
| Citations match their records | Author lists checked against arXiv or DOI. README BibTeX and `CITATION.cff` agree when they describe the same work. |
| Every install command has been tested on a clean machine | Fresh-machine test before shipping. |
| No personal identifiers leaked in public README | Usernames, paths, institutional affiliations that should not be public. |

## When to stop

A modern README does not need to check every box above. Focus on:

1. First-paint (above-the-fold) correctness — non-negotiable.
2. Install path clarity — non-negotiable.
3. Feature scannability — at least emoji-prefixed bullets or a table.
4. At least one collapsible for reference material.
5. Verified rendering on GitHub.

Everything else is polish. Stop polishing when the reader's first-minute experience is strong.
