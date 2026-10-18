---
name: editable-figure
description: Design editable PowerPoint overview figures, especially Figure 1 for papers and proposals, using source material and the author's visual preferences. Also supports mechanism, workflow, and README figures, simplifying dense drafts, and combining schematics with results. Yue Zhao's new paper/proposal overviews and standalone mechanism figures start from a generated whole-figure visual concept; two independent concepts are recommended when authorized. Object art can remain pictures while diagram text and relationships stay native. Standalone data-analysis plots belong to a plotting workflow such as Matplotlib. Does not replace screenshot capture, prompt-only work, or full slide-deck authoring. Completing a PPTX requires desktop PowerPoint on Windows or macOS for native rendering and editing checks; the bundled export helper is Windows-only. Assessment and prompt-only requests need neither.
---

# Editable Figure

## Overview

The author's primary use is the overview or Figure 1 in paper and proposal writing. Prioritize visual narrative, composition, concrete domain objects, and information hierarchy. Standalone data-analysis charts belong to a plotting workflow such as Matplotlib; a result inset may support an overview when it helps its argument.

Turn a document's central idea into a figure that a reader can understand quickly, then deliver a PowerPoint source that the author can actually edit. For a new paper/proposal overview, standalone mechanism figure, or substantial redesign, first analyze the source, study references, and design the takeaway. Then **generate a whole-figure visual concept, select the composition, build editable layers, and inspect in context**. For Yue Zhao, this is the default creation route, with the scoped exceptions below. Recommend two independent concepts when delegation is authorized; a second generator must not become a reason to skip the first. Small revisions can reuse the accepted composition. Paper, proposal, and README figures share these principles but serve different reader decisions.

## Top design priority: understand and imitate the author's preferences

For Yue Zhao's paper and proposal figures, follow the [required preference-first design protocol](references/gallery/index.md#required-preference-first-design) before choosing a composition or generating a draft. Read the latest feedback, inspect the relevant reference images, and state which confirmed qualities will be imitated and how. Apply that mapping in every generator brief and in the implementation, then compare the output against the selected images before delivery. Merely mentioning the gallery is insufficient. This priority outranks generic palette, minimalism, icon, and text-density defaults; current task instructions, scientific correctness, and required deliverable constraints remain binding. Other users must confirm their own preferences.

## Choose the scope

Respect the requested deliverable. An assessment or prompt-only request does not require generating a deck. Once figure creation is authorized, use subsequent feedback to revise the artifact without repeatedly asking permission. Choose a reasonable composition and produce a reviewable draft when the source is sufficient.

Use this skill for the figure's editorial and design decisions. If an installed presentation skill applies, read it for the current authoring runtime and validation requirements. Without one, use an available PPTX library or native PowerPoint automation and the principles in [native-powerpoint.md](references/native-powerpoint.md). This workflow works with Codex, Claude Code, or another capable agent; it does not depend on one model or desktop plugin.

**Platform requirement.** Before starting a figure build, confirm that the session can use desktop PowerPoint on Windows or macOS for the rendering and editing checks in step 5. A PPTX library such as `python-pptx` or PptxGenJS can create native objects headlessly, and that is a real capability. It does not complete these checks: nothing on a headless machine can confirm the result opens, renders, and edits as intended, and an unverified figure is the failure this skill exists to prevent. The bundled `scripts/render_powerpoint.ps1` uses Windows COM automation; macOS requires an available native PowerPoint workflow instead.

Without that capability, explain the limitation before building. Assessment and prompt-only work can continue unaffected. Offer `ci-mockup-figure` when its output meets the user's needs. Preserve an explicit PPTX requirement unless the user agrees to another deliverable; do not silently substitute a flattened image and call it an editable figure.

Nearby workflows have distinct outputs:

- `figure-prompt-builder`: prompts and reference-guided concepts, when that is the requested endpoint.
- `ci-mockup-figure`: HTML mockups, screenshots, TikZ, or other code-native figure formats.
- A plotting workflow: scientific data plots with reproducible data and axes.
- A presentation workflow: complete decks. Here a slide is a canvas for a document figure, not a presentation with a cover.

Do not replace an explicitly requested SVG, Illustrator document, or screenshot with PPTX merely because this skill is available.

## 1. Analyze the source and the reader's decision

Read the relevant section and its neighboring prose, the document's purpose, existing figures, and primary artifacts behind any claims. Identify the intended placement and display width before allocating space. Read only enough of a large repository to establish the contribution, terminology, evidence, and constraints.

Write a short working brief, in scratch space or beside the figure source when it needs to persist:

| Decision | Record |
|---|---|
| Audience and placement | Who sees the figure, where, and at what size? |
| Reader takeaway | One sentence the reader should remember after a quick look. |
| Usefulness | What can the reader understand, evaluate, build, or decide because of this work? |
| Necessary visual evidence | The example, relationship, mechanism, or measured result that supports that takeaway. |
| Content allocation | What belongs in the drawing, caption, neighboring prose, or a table? |
| Claim boundaries | Facts versus illustrative examples, proposed work, predictions, and measured results. |

Apply the relevant mode in [document-contexts.md](references/document-contexts.md). Do not require every figure to summarize the whole project.

For proposal figures, also read [proposal-figures.md](references/proposal-figures.md) for distinct figure roles, visible aim-name consistency, shared-graph semantics, and reading at manuscript width. It links the author's preferred examples. Keep lessons from compact integration figures separate from untested large-overview designs.

For a benchmark, distinguish the system producing the record from the method being evaluated. For a proposal, distinguish planned capabilities from completed results. For a README, show the actual user workflow and supported behavior.

## 2. Find and study references before designing

For a new figure or substantial redesign, first read the [personal visual gallery](references/gallery/index.md) and inspect relevant available images before choosing the composition. This applies to paper, proposal, and README work. Use it to distinguish author-confirmed preferences from candidates and color-only references. Record which visual qualities transfer into the working brief. Reuse suitable gallery examples; search externally when the gallery leaves a concrete gap. Read [reference-search.md](references/reference-search.md) for the route that matches the document:

- **Paper:** related top-venue papers, typically NeurIPS, ICML, and ICLR for ML/AI. Inspect the actual figure, caption, and nearby prose.
- **GitHub README:** current relevant trending projects and related active repositories. Inspect the rendered README and its actual visual assets.
- **Proposal:** start with the author's [preferred examples](references/proposal-style-exemplars.md) when they fit the figure's job. Search the local awarded/funded collection further when needed, using figure purpose and agency/program as selection criteria. Check indexes and known collection paths first.

Grow the gallery during real figure work using its [durable update procedure](references/gallery/index.md#grow-the-record-through-use). Read feedback in `figure-preferences/index.md` at the consumer repository root and any gallery location named in the project's instructions alongside the bundled record. Save consumer feedback outside bootstrap-managed skill copies so it survives refreshes and can be merged into the canonical gallery. Adding an image alone makes it a candidate, and accepting a deliverable does not establish approval of every stylistic choice. Do not turn inferred preferences into confirmed ones or interrupt every task with a preference questionnaire.

Read the gallery's [confirmed feedback](references/gallery/preferences.md) when choosing a visual direction. These records describe Yue Zhao's taste; for another user they are candidates until confirmed. Yue Zhao values recognizable real-world content and logos, visual variety, rich color, and clear organization, with professional technical detail, meaningful equations, and consistent color/form encodings. Reading-load rules below target redundant text while preserving this substance. MemoHarness (14) is strongly liked for its colors, detail, equations, 3D graphics, polish, and low AI feel. TDC (12) is a preferred large, readable, varied overview with a real distribution plot. FedGNN (15) supplies professional detail and clarity, but its heavy colors are disliked. CatchBench is a color reference with an average overall aesthetic assessment; Cat-DPO and No Attacker Needed have moderate approval. Choose relevant qualities for the current scientific story rather than imposing one minimal or cartoon style on every overview.

Select a small number of references and state what transfers: information hierarchy, a concrete example, a mechanism, or a useful visual structure. Keep exact source locators in the working brief. Popularity, acceptance, and funding are discovery signals, not proof of figure quality.

Use the user's supplied references when appropriate. Reuse already inspected examples for small revisions rather than restarting research. If a source is inaccessible, report that boundary and continue with available material; do not invent search results or funding status. Once a direction is supported, proceed to design without adding a reference-approval checkpoint.

## 3. Design for the takeaway

Make usefulness visible through an understandable problem, a consequential distinction, a concrete mechanism, or a supported result. Claims such as "powerful" and lists of components do not substitute for that evidence.

Choose the visual structure from the message: an example with an intervention point, evidence becoming available over time, a bottleneck and proposed mechanism, input-to-output transformation, comparison, or another justified structure. Do not default every task to three columns, a lifecycle, or a grid of cards.

For a position, framework, or concept paper, make the overview a compressed argument rather than only a taxonomy or a polished slogan. Retain the minimal claim graph and a concrete domain object needed to show why the position follows, even while removing prose. Read [position-and-concept-figures.md](references/position-and-concept-figures.md) for semantic compression, staged refinement, and links back to the manuscript.

Use graphics to carry relationships. Text should identify objects and explain only what the graphic cannot. A useful starting point for a compact overview is a few focal elements, each with an object and a short question or label. Adjust to the source; this is not a fixed panel or word quota.

For overview figures, retain a concrete domain object that participates in the mechanism, such as a circuit feeding measurements, rather than relying on a domain name in the title. Check that a reader can identify both the subject and the connection between panels. Shared variables, a carried example, or explicit input/output edges can establish that connection; adjacency and panel letters alone cannot. A section pointer printed in the figure, such as `Section 3.2`, helps navigation but does not explain the mechanism. Recheck it in the compiled document after structural edits, because section numbers move.

Concise explanation can use visually rich content. Maps, scene illustrations, screenshots, scientific plots, and structured panels can make a proposal easier to understand when they carry its substance. Evaluate their role and reading hierarchy rather than treating flat minimalism, a particular font, or the absence of rounded panels as a quality test.

### Make occupied space earn its place

Space efficiency is a design requirement for every figure, especially in papers and proposals where page area is scarce. Judge useful information per occupied area at the final document size. Inspect both the figure's internal composition and the space reserved around it in the document, including captions, gutters, and wrapped prose. A figure can be legible and editable yet still waste substantial page space.

Distinguish whitespace that supports hierarchy and separation from broad unused bands or regions with no reading function. Check each panel's actual content bounds; a long footer, wide background, or oversized text box can hide wasted space when inspecting only the overall bounding box. Reflow or shorten the element that sets an unnecessary width, then reduce the canvas or panel dimensions while preserving meaningful content and readable type. Do not add labels, decoration, or repeated claims just to fill a hole, and do not compress necessary gutters to maximize a pixel-fill ratio.

Return reclaimed space to the destination. For a wrapfigure, reduce the placed width along with the canvas when the goal is to give room back to prose. Cropping the source while retaining its old placed width enlarges the content and can increase its height instead of saving page area. Recheck physical font size, caption wrapping, adjacent paragraphs, section transitions, and total pagination. Read [space-efficiency.md](references/space-efficiency.md) when reflowing a sparse figure or adapting one to a compact document slot.

### Choose reference colors, then the lab fallback

For new figures, first apply the selected reference's confirmed color preference and any current document identity. Use the shared CatchBench, Cat-DPO, and No Attacker Needed color family as a fallback for paper, proposal, and GitHub README figures without a selected palette. Do not recolor a MemoHarness-inspired overview into this fallback merely because it is called the lab default. When using that fallback, start with a white background, mint/coral contrast, teal and warm gold for additional categories or regions, and dark text with quiet gray context. Overviews may use the full family alongside naturally colored domain scenes. The latter two papers are Tiankai Yang's reference examples. Read [default-palette.md](references/default-palette.md) for color roles, source evidence, and the canonical reusable tokens in `references/default-palette.json`.

An explicit user palette or an established document palette takes priority. Preserve accepted figures' colors when editing them. Carry the same color-to-meaning mapping across the schematic, results, caption keys, and other figures in a document. Reference searches may supply composition ideas without changing an established visual identity; a selected reference with confirmed color feedback can set the palette for a new figure. Do not repeat a paper search just to recover stored colors.

### Reduce reading load before reducing font size

For each proposed label, identify the information it adds or the misunderstanding it prevents. Omit text whose removal leaves the correct reading intact, including sentences that repeat a visible relationship, panel title, or caption. Do not add explanations, formal terms, or caveat blocks merely to make the figure appear rigorous. Keep verification details in the working notes and methodological detail in the surrounding prose unless the reader needs it at that point in the graphic.

Keep a number when it carries the claim, such as a verified performance gap or scale comparison. Do not add counts merely to make the work appear substantial. Do not transfer every deleted sentence into an oversized caption.

Simplification must preserve the inference. Check whether a removed qualifier makes a diagnostic look like general evidence, an illustrative story look like a paired experiment, a forecast look like a result, or a highlighted answer look like an input available to the evaluated method.

Prefer short, concrete action labels; keep a formal term underneath only when it helps connect the drawing to the prose. Judge understanding rather than word count alone. A record edit should be described as a record edit: wording that implies the agent actually behaved differently requires evidence of that behavior.

Require secondary strips and extra panels to explain a distinct relationship or supply useful evidence. A generic sequence such as edit, rebuild, record may add little even when accurate. If its only contribution fits in one caption sentence, remove it and reclaim its space. When that secondary mechanism is itself central, show a concrete before/after example instead. After deleting a footer, reduce canvas height while keeping its width, retained object coordinates, and font sizes fixed. If the width or object scale changes, recalculate printed text size.

The reader should recognize the main contrast at a glance and explain the figure after reading its short caption. Inspect at the intended manuscript or README width. A legible full-screen slide can still fail as a paper figure.

When breadth of resources, research outcomes, or community is itself the argument, retain the supported inventory and give it sufficient page area and reading levels. For a compact destination, select the relevant part rather than shrinking the entire inventory below readable size. A proposal ecosystem overview can expose its organization at a glance while leaving individual publications or examples for a later read. The reduction criteria target detail that does not support the figure's claim; see [proposal-figures.md](references/proposal-figures.md) and the [preferred examples](references/proposal-style-exemplars.md).

For mechanism diagrams, read [mechanism-figures.md](references/mechanism-figures.md): it covers minimal examples, source checks, essential qualifiers, secondary content, and a concrete revision case.

When revising a dense first figure, read [iteration-example.md](references/iteration-example.md). Its layout and counts are an example, not defaults.

### Combine explanation with evidence when useful

A dominant schematic can explain what the work makes possible, while a smaller result panel gives the reader a numerical reason to care. Repeating a selected result from Results can serve this introductory purpose. Judge its contribution to the reading path rather than rejecting all repetition or optimizing for the fewest words.

When the author wants a large (a) and narrow (b), reserve the gutter first, then divide the remaining width according to their reading roles. Judge separation between the actual labels and artwork, including axes and callouts. A divider does not compensate for crowded panels. After changing the canvas, recalculate text size at the final display width.

Read [panels-and-results.md](references/panels-and-results.md) when combining an explanatory panel with data, separating regions within a wide schematic, or revising panel spacing. It covers panel hierarchy, region separation, cross-panel edges, source-derived numbers, descriptive versus inferential claims, and focused revisions. Use the plotting workflow for scientific correctness and the native PowerPoint guidance for editable chart behavior.

For a paper first figure, read [paper-first-figures.md](references/paper-first-figures.md) for problem/contribution panel roles, compact comparison cases, baseline semantics, and review of the inserted manuscript page. For external plotting repositories such as figures4papers, use the concrete reuse procedure in [reference-search.md](references/reference-search.md#external-plotting-repositories).

### Choose how to draft

For Yue Zhao's new paper/proposal overviews, new standalone mechanism figures, and substantial redesigns, produce at least one actual whole-figure image-generation draft before committing to native layout geometry. Use ImageGen or an available image-generating route such as Agy, respecting the user's backend choice and existing authorization. A prompt, object asset sheet, code-rendered box diagram, or text-only review does not complete this composition step. Read [hybrid-figures.md](references/hybrid-figures.md) for the brief, comparison, and picture boundaries, and [recent workflow feedback](references/recent-workflow-feedback.md) for the confirmed scope and failure cases. This is an author-specific workflow preference, not a claim that generated images are always better.

Recommend two independent generated concepts when authorized. Both routes must produce actual images to fulfill a two-concept request. One authorized generator is sufficient for the default concept stage; do not launch another agent or paid backend without the required authorization.

Select one primary composition and borrow specific strengths from another candidate when one exists. Keep diagram labels, symbols, arrows, and relationship lines native. Detailed object illustrations can remain replaceable pictures. Respect a stricter all-native requirement when supplied; do not flatten a diagram into one picture and call it editable.

Native-first remains suitable for small revisions to an accepted composition, exact reproduction of a supplied layout, a small inset or local edit within an accepted figure, or an explicit request for a native-only draft. For Yue Zhao, choosing a three-column outline or noticing that components can be drawn as boxes does not exempt a new overview or a new standalone mechanism figure from concept generation. Other authors may choose native-first for a settled composition. Standalone plots and prompt-only tasks keep their own workflows. If generation is unavailable, report the limitation and use an already authorized fallback; otherwise ask the user to choose a fallback before proceeding, without silently delivering a native approximation as a completed concept stage. Do not relabel a duplicate or a reconstruction as an independent generated concept.

Show the generated original or originals, followed by one recommended direction with a concrete reason. Generate further alternatives only when a real design tradeoff remains or the user requests them. Save revisions under meaningful new names so the author can compare them.

## 4. Build the editable figure

Read [native-powerpoint.md](references/native-powerpoint.md) before authoring or converting a diagram. Discover current tools and fonts rather than copying cache paths or runtime versions from a previous session.

Use text boxes for text, native shapes for semantic objects, and attached connectors for relationships that should follow moved nodes. Keep imported photos or screenshots as such when they are part of the requested figure. Name objects and group them by meaningful region so both whole-block and individual editing remain practical.

Use a figure-sized canvas rather than inheriting a slide aspect ratio without reason. Preserve an existing palette or typography when supplied. Work out the final physical text size after scaling into the document. Do not shrink labels to rescue a composition that needs editing.

Retain the editable source and a reproducible builder when one was used. After manual PowerPoint edits, identify the current source of truth; a stale builder must not silently overwrite an author's changes.

## 5. Inspect semantics, appearance, and editability

For Yue Zhao, first check preference fidelity against the chosen gallery images and the reference-to-design mapping. Revise an output that drops the promised visual richness, technical substance, or color/form consistency, or repeats a disliked treatment. Then complete these separate checks:

1. **Meaning:** Trace claims to the source, distinguish illustrative from empirical content, and verify the figure plus caption conveys the intended usefulness without overstating scope.
2. **Rendering and space:** Export the final PPTX and inspect the actual result for clipping, wrapping, arrow direction, visibility, spacing, and unused regions. Evaluate each panel and the complete occupied document area, not only a tightly cropped preview. Recheck at the intended document width, and in the actual document when insertion is part of the task.
3. **Editability:** Apply the [picture boundary](references/hybrid-figures.md): diagram labels, symbols, arrows, and relationship lines stay native; detailed object illustrations may remain independently replaceable pictures. On a disposable copy, edit representative text and symbols throughout the figure, and move a connected node to check behavior. When used, move an imported object independently and change and restore a static curve's control point. For a native result chart, check its data source and edit/restore a series value; verify the embedded workbook when portable data editing is required. Counts of shapes or a PNG preview alone do not prove this. Disclose picture boundaries and curves that need manual rerouting.

Use the installed presentation workflow's validators when available. The optional Windows helper `scripts/render_powerpoint.ps1` exports a single-slide figure through local PowerPoint and reports native object counts. It is a renderer and inventory check, not a substitute for visual or editing inspection.

The portable `scripts/inspect_figure_pdf.py` reports extractable PDF text sizes at a supplied placement width. See [native-powerpoint.md](references/native-powerpoint.md#inspect-physical-text-size) for its usage and limits.

Fix issues that affect the requested result and stop when the checks pass. Do not expand a one-figure revision into unrelated document edits or a large review process. If native PowerPoint becomes unavailable, report which rendering and editing checks remain incomplete and follow the platform requirement above. A fallback preview does not complete this workflow.

Treat review suggestions as claims to reconcile with primary sources and the user's requirements. An optional reviewer does not decide the authoring format or override later user feedback. Record which artifact version a review covers. A small spacing revision calls for geometry, rendering, and relevant data-integrity checks; it need not restart reference research or a full review cycle.

For a visual-only pass, freeze wording, font sizes, semantic groups, and connector endpoints, and compare them with the previous version so polish cannot hide a redesign.

When another model is requested, prepare a frozen image, caption, relevant section, and only the source excerpts needed to verify the mechanism. Include the actual publication width and column layout so advice addresses the real destination. For a first-read comprehension check, initially send only the image and placement context, without revealing the desired takeaway. Obtain that reading before sending the caption and source for the semantic check; putting the answer later in the same prompt still primes the reader. Other review tasks can use the combined packet. Use existing authorization for that material and destination; resolve any missing permission only when required, and do not expand a limited review into sending the full manuscript or repository. Separate the reviewer's direct checks from locally reported PowerPoint and document checks.

## 6. Deliver for the destination

Save the editable `.pptx` and the appropriate viewing or publication export together, using the project's existing figure/assets folder or the user's named destination. Do not leave the only usable deliverable in temporary storage.

- Papers and proposals: normally a vector `.pdf`, plus a PNG preview when useful.
- GitHub README: a `.png` or compatible `.svg` for display, plus the `.pptx` source. Provide meaningful alt text when embedding it.

Check that the exports correspond to the final editable source. When inserting the figure, trace the actual document inclusion path to that export, compile, and compare the rendered page with the PowerPoint export. Resolve wrong-source selection before investigating preview caching. See [hybrid-figures.md](references/hybrid-figures.md) for the handoff when several candidates and reconstructions coexist.

Supply a concise caption or nearby sentence when needed to make the figure interpretable. Do not insert into or rewrite the document unless authorized by the task. Link the editable file, show a preview, and briefly identify any material limitation.

Keep caption or adjacent explanatory text in one canonical place; for LaTeX insertion, use one canonical float fragment. After a revision, synchronize the PPTX, viewing exports, builder dimensions and group definitions, caption, and current verification notes. Before replacing files after a long build, check whether those targets changed concurrently. Preserve unrelated edits and render or compile the current document when insertion is authorized; a private snapshot is not a replacement for newer document work. Keep historical reviews labeled by version rather than implying they cover later edits.
