---
name: ci-mockup-figure
description: Create space-efficient figures for papers and proposals. HTML mockups for systems, dashboards, and timelines; TikZ or skia-canvas for abstract diagrams with arrow routing. Covers tool selection, design, capture, and LaTeX insertion. The primary goal is maximizing information per page — every figure must earn its space.
---

# CI Mockup Figure

## Overview

The goal is **space-efficient, information-dense figures** that communicate
a system's design, a method's pipeline, or an architectural flowchart in
minimal page area. Three paths depending on figure type:

- **HTML mockup path** (Phases 1-4 below): for UI mockups, dashboards,
  timelines, and any figure where content is rectangular with no cross-node
  arrows. Build interactive HTML, capture screenshots, insert into LaTeX.
- **README / Markdown hero path** (README / Markdown Target section): same
  HTML build phase, but capture via headless Chrome to a PNG and embed it as a
  Markdown image referencing `assets/fig.png`. Requires larger type, higher content density, and
  one dominant focal element -- the viewer scales the image down to column width.
- **Abstract figure path** (Abstract Figure Toolchain section): for
  architecture overviews, dependency topologies, and any figure needing
  arrow routing between non-adjacent nodes. Use TikZ, skia-canvas, or
  Illustrator ExtendScript.

Every figure must pass the space test: does this figure communicate more per
square inch than the text it displaces? If a full-width figure takes half a
page but only says "A → B → C → D", it fails. A wrapfigure showing an
interactive prototype with search results, graph nodes, and real data labels
passes because it communicates system design, data model, and UX simultaneously.

This skill works for any document where figures need to communicate design
or methodology credibly: research papers (method overview, architecture
diagrams, pipeline flowcharts), proposals (NSF, NIH, DOE, etc.), technical
reports, or demo writeups. It is especially effective when the figure needs
to show multi-component structure, data flow, or step-by-step methodology
and reviewers evaluate whether the design is credible and well-conceived.

### HTML mockups vs TikZ/LaTeX diagrams

HTML and TikZ each have a clear strength. The deciding factor is **whether
the figure needs arrow routing between non-adjacent nodes**.

| | HTML mockup | TikZ |
|---|---|---|
| Visual polish | Modern CSS, shadows, gradients, rounded cards | Flat, academic-looking boxes |
| Iteration speed | Change CSS and refresh | Recompile LaTeX, debug positioning |
| Layout control | Flexbox/grid, responsive | Manual coordinate math |
| Color/font variety | Full CSS + web fonts | Limited, verbose color defs |
| Interactivity | View switching, capture mode | Static only |
| **Arrow routing** | **Breaks on cross-row/non-adjacent nodes** | **Node anchoring solves this natively** |
| Font matching | Separate from LaTeX | Perfect match with document body |

**Prefer HTML** for system mockups, dashboards, Gantt/timeline figures, and
any figure where the content is inherently rectangular and flows naturally
(no cross-node arrows needed).

**Prefer TikZ** for abstract framework diagrams, dependency topologies, and
architecture overviews where arrows must route between non-adjacent nodes
(L-shaped, curved, crossing rows). TikZ node anchoring (`node.south`,
`node.east`) handles this natively. Also prefer TikZ for small inline
diagrams that must live inside the LaTeX source or when exact font matching
is critical.

**Prefer skia-canvas** (Node.js) when you need the arrow-routing control of
TikZ but want faster iteration (edit `.mjs`, run, get PDF) and richer
visual styling than TikZ provides.

### HTML mockups vs AI image generation

HTML mockups are strictly better than AI-generated images for system and
method figures. AI image generation is acceptable only for artistic/conceptual
overview diagrams.

| | HTML mockup | AI image generation |
|---|---|---|
| Text legibility | Pixel-perfect, real fonts | Often garbled or blurry |
| Data accuracy | Every number/label controlled | Hallucinated values |
| Iteration speed | Change CSS and refresh | Re-prompt and hope |
| Consistency | Same palette across figures | Each generation varies |
| Print quality | Vector PDF via browser print | Always raster |
| Reviewer trust | Looks like a system you could build | Looks like a concept sketch |

### HTML mockups vs PowerPoint (for timelines)

HTML is also better than PPTX for Gantt/timeline figures:

| | HTML | PowerPoint |
|---|---|---|
| Alignment | CSS grid, pixel-perfect | Manual drag |
| Edits | Change one label, everything reflows | Reposition every box |
| Color consistency | CSS variables, one source of truth | Manual color matching |
| Capture | Browser Print → PDF (vector) | Export PDF (often wrong margins) |

## When to Use

- The document describes a multi-component system or multi-step methodology
  (e.g., "representation layer + discovery service + workflow engine", or
  "data collection → feature extraction → model training → evaluation")
- Abstract pipeline diagrams feel generic and do not differentiate the work
- The figure needs to show architectural structure, data flow, or method
  pipeline with real labels, not just boxes and arrows
- Collaborators need something interactive to react to and iterate on
- The document has a page limit and figures need to be space-efficient
- A timeline/Gantt figure is needed for the work plan or project overview

### When NOT to Use (either path)

- **Experimental result figures** (plots, charts, tables, ablation curves) --
  use Python (matplotlib, seaborn, plotly) or LaTeX (pgfplots, tikz) instead.
  This skill is for system/method diagrams, not data visualization.

### When to Use the Abstract Path Instead of HTML

- **Abstract framework diagrams with cross-node arrow routing** --
  dependency topologies, architecture overviews with curved arrows between
  non-adjacent nodes, box-and-arrow conceptual figures. HTML/CSS fails at
  arrow routing: JS-positioned SVG arrows drift and misalign, CSS
  pseudo-element arrows work only for simple adjacent connections, and card
  grid layouts read as a product dashboard, not a research diagram. Use the
  Abstract Figure Toolchain section below.

## Abstract Figure Toolchain

When the figure is NOT a UI mockup, dashboard, or timeline (i.e., it needs
arrows between nodes, dependency edges, or architectural flow), HTML/CSS is
the wrong tool. The core problem: **arrow routing is the bottleneck**, not
box/text rendering. Any tool with node-anchor-based arrow endpoints works;
any tool relying on CSS layout for arrow positioning will struggle.

### What fails in HTML/CSS for abstract diagrams

| Problem | Why |
|---|---|
| Curved arrows between components | Requires absolute-positioned SVG overlay that fights CSS layout |
| External screenshots as hero images | Multi-panel screenshots break `object-fit: cover` and explode containers |
| Card grid layout | Reads as a product dashboard, not a research diagram |
| Print fidelity | Browser print rescales unpredictably for non-page layouts |
| Emoji/icons for decoration | Instantly makes the figure look unprofessional |

### Recommended tools (ranked by context)

| Context | Tool | Why |
|---|---|---|
| LaTeX paper, arrow-heavy | **TikZ** | Node anchoring (`node.south`, `node.east`) handles arrow routing natively. Font/style consistency with the paper is free. Academic gold standard. |
| Programmatic iteration needed | **skia-canvas** (Node.js) | Same Canvas API as HTML but headless, with direct PDF/SVG vector export. Edit coordinates in `.mjs`, run `node script.mjs`, get PDF. No browser, no capture, no pdfcrop. |
| Final hand-polish needed | **Illustrator ExtendScript** (`.jsx`) | Generate programmatically, then hand-adjust. Best for figures that need to look "designed." Requires Illustrator. |
| Python-only environment | **drawsvg** (`pip install drawsvg`) | SVG-first imperative drawing. For PDF, convert the output SVG externally (e.g., Inkscape CLI or `cairosvg`, which needs the Cairo C library and is tricky on Windows). |

**Not recommended:** D2 (auto layout too unpredictable for precise academic
figures), Graphviz (limited custom styling), matplotlib (designed for data
plots, not diagrams).

### skia-canvas workflow

A parallel capture path to the HTML workflow, for abstract figures:

```bash
npm install skia-canvas
# edit generate-figure.mjs (Canvas API: ctx.roundRect, ctx.fillText, ctx.lineTo)
node generate-figure.mjs
# outputs figure.pdf (vector), figure.svg, figure.png
# use figure.pdf directly in \includegraphics — no pdfcrop needed
```

Script structure pattern:
```
gen_overview.mjs
├── helpers: roundRect(), text(), drawArrow(), drawImage()
├── layout constants: W, H, panel positions, gap sizes
├── draw():
│   ├── header bar (title + thumbnails)
│   ├── component panels (frame + internal diagram + footer)
│   ├── inter-component arrows with labeled handoffs
│   └── bottom strip (use cases + running example)
└── export: PDF + PNG preview
```

Adjust coordinates, rerun, get new PDF instantly. No browser, no print quirks.

### Design principles for abstract/architecture figures

1. **No external screenshots in component panels.** Draw diagrams
   programmatically (nodes, edges, flow stages). Screenshots are not made
   for your figure's aspect ratio and will break.
2. **Real images only in grounding areas** (use case strips, running example
   ribbons) where they are decorative context, not structural elements.
3. **Horizontal layout for pipeline figures** (T1 -> T2 -> T3 left-to-right).
   Vertical stacking wastes landscape width.
4. **Minimal elements per panel**: badge, title, one diagram, short
   description, one output line.
5. **Light tint fills** (`rgba(..., 0.06-0.08)`), thick top-border accent
   per component. No saturated card backgrounds, no shadows, no pills/chips.
6. **Inter-component arrows with labeled handoffs** (e.g., "Asset Graph",
   "Discovery Trace") as explicit connectors, not just whitespace.
7. **Professional typography**: Helvetica or Arial (Nature journal convention).
   Serif (Georgia) for text-heavy insets. Never Segoe UI, Inter, or geometric
   narrow variants -- those read as AI-startup product. No emoji.

**Avoid the AI-startup look.** Segoe UI or Inter combined with an
indigo-violet-and-amber palette reads as a product dashboard, not a research
figure. For scientific figures:

- **Font**: Helvetica or Arial
- **Palette**: NPG / ggsci convention -- coral #E64B35, slate-blue #3C5488,
  teal #00A087, salmon #F39B7F
- **Avoid**: indigo or violet as a primary color; gradient bars; pill buttons;
  heavy drop shadows

## Space Budget (decide first)

Before designing anything, decide the figure budget for the document. The
table below uses LaTeX environments as examples; adapt to the target format.

| Figure type | Space cost | When to use |
|---|---|---|
| `wrapfigure{r}{0.55\textwidth}` | ~55% column width, text wraps beside | **Best default** for system mockups — gives prose enough room |
| `wrapfigure{r}{0.46\textwidth}` 2x2 grid | ~46% column width, 4 images in compact grid | Motivation figures with matched-ratio panels |
| `figure[t]` full-width | Full column, ~3-4cm height for landscape | Timelines, overview diagrams, or mockups with small text |
| `subfloat` 1x4 row in `figure[t]` | Full column, ~3cm height | Cross-domain motivation (all same aspect ratio) |

**Lesson learned: 0.68\textwidth is too wide for most wrapfigures.** At 0.68,
the remaining text column is too narrow for comfortable reading and the prose
wraps through multiple paragraphs. Default to **0.55–0.56\textwidth** for
system mockups. Use 0.68 only for simple figures with large text.

**Rule of thumb for page-limited documents (e.g., 15-page proposal):**
- 3 main figures as wrapfigures = ~1.5 pages of figure space
- 1 overview figure + 1 motivation figure = ~1 page
- Total figure budget: ~2.5 pages out of 15 (17%) — leave 83% for text

**Horizontal layout is mandatory.** Vertical/portrait screenshots waste 50%+
of their space on a landscape-format page. Design the mockup for wide capture
from the start.

### README-hero figures

README and documentation hero figures differ from paper figures. Markdown
viewers scale the PNG to the content column (roughly 700-900 px on GitHub),
so design for the scaled-down view:

- **Type larger than in a paper figure.** Labels legible at `0.55\textwidth`
  will be too small in a 700 px GitHub column. Increase all font sizes by at
  least 1.5x relative to a paper figure.
- **Fill the frame.** Target >80% content fill. Large empty margins waste
  limited column area.
- **One dominant focal element.** A large keystone number, score, or pipeline
  diagram -- something readable in under two seconds at thumbnail size.
- Capture with headless Chrome (see README / Markdown Target section); no
  `pdfcrop`, no LaTeX environment.

## Pre-flight Checklist

Before the first render, verify all of the following:

- [ ] Flat fills only. No gradients on bars, panels, or backgrounds.
- [ ] No drop shadows.
- [ ] No pill-shaped buttons; border-radius <= 4 px on any container.
- [ ] Hairline borders (`1px solid`) are fine. Decorative glows and gradient
  borders are not.
- [ ] Font is Helvetica or Arial. Not Segoe UI, Inter, or geometric narrow
  variants.
- [ ] Color is reserved for data differentiation. Cut decorative or structural
  color.
- [ ] No indigo or violet primary (reads as AI-startup palette).

## Phase 1: Design the Mockup

### 1a. Split complex interfaces into separate figures

Do not cram multiple interfaces into one figure. If a system has a
coordinator-facing view and a citizen-facing view, build both in one HTML file
but capture them as **separate screenshots**, each inserted at its own
relevant subtask in the LaTeX.

Add **view-switching buttons** (e.g., COORDINATOR / CITIZEN / BOTH) to the
toolbar so the user can toggle views and capture each at full width. Each
view mode should have its own CSS that expands sidebars, scales up fonts,
and adjusts proportions for the full-width layout.

### 1b. Identify the views

Each major thrust or component gets its own view. Typically 3-4 views:
- A dashboard/overview showing the full pipeline
- One view per thrust showing that thrust's specific interface

### 1c. Choose per-thrust visual identity

Each thrust needs a distinct color palette with **three tones** (solid, mid,
light) so bars, cards, and legends are consistent:

```css
--t1: #0d9488; --t1-mid: #5eead4; --t1-light: #ccfbf1;
--t2: #2563eb; --t2-mid: #93c5fd; --t2-light: #dbeafe;
```

Use the light tone for activity bars with a solid-tone border, the solid tone
for milestone/completion bars, and the mid tone sparingly for hover states.
**Legend swatches must match the actual bar appearance** — if bars are light
with colored borders, show that in the legend, not a solid fill.

### 1d. Embed real public-domain imagery

Use real, public-domain scientific imagery as **low-opacity backgrounds** to
add geographic or domain credibility without overwhelming the schematic:

- NASA Earth Observatory for satellite/geoscience imagery
- NOAA for climate/ocean data products
- USGS for terrain/hazard data

Embed as CSS background with `opacity: 0.15–0.20` and `filter: saturate(0.6)`.
Add a small attribution credit (e.g., "Imagery: NASA/USGS Landsat 9, Jan 14
2025") in the corner. Download locally to `figure-src/assets/` for offline
reliability.

### 1e. Choose icons

Use Lucide (ISC license) or similar SVG icon library. Define icons as an SVG
sprite block at the top of the HTML so they are referenced once and used
everywhere via `<svg><use href="#icon-name"/></svg>`. Inline SVG icons in
panel headers add polish with zero external dependencies.

## Phase 2: Build the Mockup

### Technical requirements

- Single self-contained HTML file per figure type (system mockup and timeline
  should be separate files, not crammed into one)
- Embedded CSS and JavaScript
- System fonts preferred for offline reliability; Google Fonts acceptable only
  with explicit fallback stacks (e.g., Georgia, system-ui, Consolas)
- Dark sidebar + light main area, or similar professional layout
- Responsive to container size changes (for print/screenshot)

### Key features to include

- **View switching** via toolbar buttons (not sidebar — saves horizontal space)
- **Screenshot mode** (`CAPTURE` button): hides toolbar and any titles that
  duplicate the LaTeX caption, tightens spacing, scales up fonts for legibility
- **Print CSS** (`@media print`): hides chrome, preserves background colors,
  forces landscape orientation, allows text wrapping in bars
- **Dynamic rendering** (graphs, DAGs): use percentage-based CSS positioning,
  not absolute pixels, so nodes survive container resize during print

### Visual polish checklist

Raw mockups that look like "developer prototypes" undermine credibility. These
details make the difference:

- **Depth**: subtle box-shadows on cards, layered backgrounds
  (`linear-gradient` on surfaces)
- **Cards**: rounded corners, colored left-border accent for action/alert cards
- **Typography hierarchy**: bold subtask numbers in bar labels, monospace for
  metrics and audit trails, proper weight differentiation
- **SVG glow filters** on key nodes (assembly points, critical indicators)
- **Progress bars** alongside metric values (not just numbers)
- **Status indicators**: colored dots, pulse animations for live status

### Per-thrust layout patterns

Vary the layout between thrusts for visual interest:
- **Thrust A**: Info panels left | Interactive canvas right
- **Thrust B**: Search+results left | Explanation cards right (both info-dense)
- **Thrust C**: Canvas left | Timeline+cards right (flipped from A)

### Common pitfalls

- Dynamic graph nodes positioned with `px` values break during print — use `%`
- `wrapfigure` in LaTeX needs text below it to wrap — place before a long
  paragraph, never at section end
- Large hero images waste space in wide mode — integrate as panel backgrounds
  at low opacity instead of giving them a full column
- Screenshots are vertical by default — must design horizontal layout explicitly
- **Wrap-environment collisions**: never place a `wrapfigure` immediately before
  a `wraptable` or another `wrapfigure` — LaTeX emits collision warnings and
  forces floats out of position. Separate them with at least one full paragraph
  of unwrapped text, or convert one to a `figure[t]`/`table[t]`
- **`white-space: nowrap` truncates text in print** — bar labels and tags must
  allow wrapping. Always test the print preview (`Ctrl+P`) before capturing.
  Remove `nowrap`, add `line-height: 1.2`, and increase row height to
  accommodate wrapped text.
- **Capture mode should hide the HTML title/header** — the LaTeX caption
  provides the title. Duplicating it in the screenshot wastes vertical space.

## Phase 2b: Timeline / Gantt Figures

For work plan timeline figures, use a standalone HTML file with CSS grid.

### Design principles

- **CSS grid** with quarter-columns: `grid-template-columns: <label-width>
  repeat(16, 1fr)` for a 4-year / 16-quarter layout
- **Phase bars spanning multiple quarters** — not one bar per quarter. Use
  `grid-column: N / M` to span consecutive phases as single continuous blocks.
- **Stagger start times** to show dependencies. Not all thrusts start Q1.
  If Thrust 2 depends on Thrust 1 outputs, start Thrust 2's bar in Q2 or Q3.
- **Subtask numbers in bar labels** (e.g., "**1.1** WRF-SFIRE calibration")
  so reviewers can cross-reference to the thrust text.
- **Milestone diamonds** at delivery points; solid-fill bars for milestone
  quarters, bordered-light bars for activity phases.
- **Integration row** at the bottom with dark-pill milestone markers spanning
  across all thrusts (M1: Component demos, M2: End-to-end pipeline, etc.).
- **Legend** must match actual bar appearance (light + border for activity,
  solid for milestone, diamond for marker, dark pill for integration).

### Timeline capture

- Click CAPTURE → `Ctrl+P` → Destination: Save as PDF → Layout: **Landscape**
  → Margins: None → Save.
- Insert as `figure[t]` full-width (timelines need the detail).

## Phase 3: Review with Codex

Use the `implement-review` skill to send the staged figure to Codex for review.
Key review points (apply to both HTML and abstract paths):

1. **Scientific accuracy** — are dataset names, diagnostics, and workflow steps
   plausible for the target domain?
2. **Visual differentiation** — do the component views look distinct?
3. **Legibility at print size** — will fonts survive at `0.55\textwidth`?
4. **Branch coverage** — if the system has multiple modes (e.g., zero-vehicle
   vs HV-owning), does the caption explicitly label which branch is shown?
5. **Figure cross-references** — every figure must have a `Figure~\ref{}`
   callout in the prose. Figures without cross-references feel decorative.

Additional review points for the **abstract path**:
6. **Arrow routing** — do all arrows connect at correct node anchors? No
   floating or misaligned endpoints.
7. **No dashboard aesthetic** — the figure should read as a research diagram,
   not a product UI.
8. **Vector output** — PDF/SVG output is vector, not rasterized.

Iterate based on feedback. Typical: 2-3 rounds.

## Phase 4: HTML Capture and Insert

### Capture workflow

1. Open the mockup in Chrome
2. Click the view button for the target thrust (e.g., COORDINATOR)
3. Click `CAPTURE` to hide toolbar and titles
4. `Ctrl+P` → Save as PDF (landscape, no margins) for **vector output**, or
   take a screenshot for PNG
5. Save as `figure/thrustN.pdf` (or `.png`)

**Prefer PDF** — text and SVG elements stay vector (sharp at any zoom). Only
the embedded satellite imagery stays raster. Drop the file extension in
`\includegraphics` so LaTeX auto-selects the best available format:
```latex
\includegraphics[width=0.55\textwidth]{figure/thrust4-coord}
```

### Capture gotchas

- **SVG `height: 100%` inside a flex item overflows.** An inline SVG with
  `height: 100%` inside `display: flex` expands beyond its parent and overlays
  adjacent content. Use deterministic fixed heights: give each SVG an explicit
  `height` in px, a `viewBox` whose aspect ratio matches that box, and
  `overflow: hidden` as a backstop. Avoid `flex: 1` + `height: 100%` SVG for
  figure panels.
- **`--window-size` pairs with `--force-device-scale-factor`.** The
  `--window-size=W,H` flag sets logical pixels. At `--force-device-scale-factor=2`
  the output PNG is 2W x 2H physical pixels. Set `W,H` to the CSS viewport
  dimensions, not the intended output resolution.

### Trim white margins with `pdfcrop`

Browser-exported PDFs have full-page white margins that waste space in LaTeX.
Use `pdfcrop` (bundled with TeX Live) to trim to the content bounding box.

```bash
pdfcrop figure/timeline.pdf figure/timeline-trimmed.pdf
pdfcrop --margins 4 figure/thrust1.pdf figure/thrust1-trimmed.pdf
```

**Always keep the original and write to a separate `-trimmed` file.** This
preserves the full-page source for re-trimming with different margins later.
Use the trimmed file in `\includegraphics`.

- `pdfcrop` uses Ghostscript for bounding box detection, which is reliable
  for both vector and raster content.
- Default padding is 0 bp. Use `--margins N` to add N bp on all sides (2-4
  is typical for proposal figures).
- Do not attempt pixel-based or metadata-based trimming with PyMuPDF or
  similar — these methods frequently misdetect content bounds on
  browser-exported PDFs.

### LaTeX insertion

Default to `wrapfigure` at **0.55–0.56\textwidth**:

```latex
\begin{wrapfigure}{r}{0.56\textwidth}
  \vspace{-1.2em}
  \centering
  \includegraphics[width=0.55\textwidth]{figure/thrust1}
  \caption{\textbf{Thrust 1 title.} Brief description of what the
  screenshot shows, referencing left and right panels. If the system
  has multiple modes, state which mode is shown (e.g., ``zero-vehicle
  branch; the HV-owning branch provides departure/route guidance'').}
  \label{fig:thrust1}
  \vspace{-1.4em}
\end{wrapfigure}
```

Place the `wrapfigure` before a paragraph with 10+ lines of text below it.
Never place at the end of a section.

For timelines, use full-width `figure[t]`:
```latex
\begin{figure}[t]
  \centering
  \includegraphics[width=\textwidth]{figure/timeline}
  \caption{\textbf{Four-year work plan and integration timeline.}
  Colored bars show per-thrust activities by quarter; diamonds mark
  milestones; bottom row shows integration checkpoints (M1--M4).}
  \label{fig:timeline}
  \vspace{-1em}
\end{figure}
```

### Motivation figure

For a cross-domain motivation figure (e.g., "spatio-temporal data spans
multiple national-priority domains"):

1. Source 3-4 images from federal agencies (NASA, NOAA, USGS, USDA)
2. Ensure all images have the same aspect ratio (e.g., all 3:2 from NASA
   Earth Observatory)
3. Use `\subfloat` with `height=` for equal-height tiling
4. Cite data sources with `@misc` bib entries using institutional authors
5. Reference each panel in the opening paragraph prose

## README / Markdown Target

A sibling capture path for repository README and documentation hero figures.
The HTML build phase is identical to Phases 1-3; only capture and insertion
differ.

### Headless capture (agent-runnable, no browser interaction)

Resolve the browser binary first: use whichever Chrome-family executable is on the host
(`chrome`, `google-chrome`, `chromium`, or `msedge`), or an absolute path to it; create
`assets/` if it does not exist. Then run:

```bash
chrome --headless --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 \
  --window-size=W,H \
  --screenshot=assets/fig.png \
  file:///abs/path/to/fig.html
```

Set `W` and `H` to the CSS viewport dimensions of the mockup (e.g.,
`--window-size=1400,600` for a wide dashboard). The `--force-device-scale-factor=2`
flag yields a retina-density PNG: physical output is 2W x 2H pixels, so the
image stays crisp when scaled down by the Markdown viewer.

Keep the HTML source and all assets under `assets/figure-src/`. The captured
PNG goes to `assets/fig.png`.

See Capture gotchas (Phase 4) for SVG overflow and window-size pairing notes.

### Embedding

Reference the captured PNG from the README using Markdown image syntax with a
leading exclamation point: alt text in square brackets, then the relative path
`assets/fig.png` in parentheses.

No LaTeX environments. No `pdfcrop`. The PNG is the deliverable.

## Output Checklist

### README / Markdown hero path
- [ ] HTML source and all assets under `assets/figure-src/`
- [ ] Headless Chrome capture command tested; PNG in `assets/`
- [ ] Type at least 1.5x larger than equivalent paper figure
- [ ] Content fills >80% of frame; one dominant focal element visible at
  thumbnail size
- [ ] Pre-flight checklist passed (no gradients, no shadows, Helvetica/Arial,
  no indigo/violet primary)

### HTML mockup path
- [ ] HTML mockup(s) in `figure-src/` with all assets in `figure-src/assets/`
- [ ] Separate HTML files for system mockup vs timeline (not combined)
- [ ] View-switching buttons for multi-interface systems
- [ ] Print preview tested — text wraps, no truncation, landscape orientation
- [ ] Screenshot PNG/PDF files in `figure/` (originals kept, trimmed copies via `pdfcrop`)

### Abstract figure path
- [ ] Generation script (`.mjs` for skia-canvas, `.tex` for TikZ, `.jsx` for Illustrator) in `figure-src/`
- [ ] Vector output (PDF/SVG) in `figure/` — no browser capture needed
- [ ] Arrow endpoints anchored to correct node edges, no drift
- [ ] No dashboard aesthetic (no card shadows, no pills/chips, no emoji)
- [ ] Professional typography (system sans-serif or serif, no playful fonts)

### Both paths
- [ ] `wrapfigure` (0.55–0.56) or `figure[t]` environments in tex files
- [ ] `Figure~\ref{}` callouts in the prose for every figure
- [ ] Captions describing visible content with explicit branch/mode labeling
- [ ] Legend swatches match actual bar/card colors in the figure
- [ ] Bib entries for any cited data products (NASA, USGS, etc.)
- [ ] Codex review passed (scientific accuracy, legibility, differentiation)
