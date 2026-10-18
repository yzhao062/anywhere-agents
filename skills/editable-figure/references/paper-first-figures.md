# Paper First Figures

Design for an adjacent-field graduate student who has not read the methods. A first figure should establish why the problem matters and what the paper contributes. The following choices come from a compact, two-column paper workflow; they are options for this role, not a template for every overview.

## Give Panels Different Jobs

Before polishing, write one sentence per panel describing what the reader learns there. A useful division is **A: the observed problem; B: what this work does to investigate or address it**. If both panels restate the same headline, replace the second with a concrete intervention, audit example, or supported outcome. Panel letters alone do not establish a progression.

For an audit, show the operation that produces the conclusion: locate a reported comparison, hold the relevant setting fixed, change the comparator, then check the metric and quality constraint. A generic pipeline adds little without an example of what changes. State whether the operation recomputes published values or reruns an experiment.

Several source-derived cases can show distinct outcomes, such as a smaller gain, a reversed ranking, and a comparison excluded by a quality constraint. Choose cases for those differences. Do not pad the panel with invented values or redundant copies of the same lesson. Shared row structure and aligned axes let additional cases add evidence with little extra text.

When a paper studies text or agent behavior, consider showing one real instance among the first figures. Mark the decisive span, and place the figure no later than just after the first headline aggregate. A prompt, a trajectory excerpt, or a model reply all work, and readers then see the phenomenon early. Chen et al. (2021, the Codex paper) show real HumanEval prompts with a shaded successful completion as Figure 2, right after a single pass-rate plot. Keep the instance verbatim apart from the marking, and say whether it is typical.

## Make Comparisons Interpretable

Keep the metric, direction, comparator, and necessary setting near the values. Prefer a familiar word such as "baseline" when a term such as "arm" adds no useful distinction. "Another reported baseline" needs a visible explanation of what changed and what stayed fixed.

For a latency speedup, define the ratio as baseline latency divided by method latency. A value above one means faster, one means parity, and a value below one means slower. A separate row containing only the normalized baseline value of one often adds no evidence; a labeled parity mark can do that job. Check the mark's contrast and position at print size so it remains visible without crossing numeric labels.

Raw ratios and gains above parity are different quantities. If an audit criterion uses the excess over one, recompute that quantity before applying the criterion; a ratio bar alone does not establish it. Keep percentages, percentage points, and multiplicative ratios distinct. Use a common scale for comparable cases; label a different metric or scale explicitly. Bars that encode magnitude normally start at zero.

Missing quality reporting does not establish equal quality. Identify which method or comparator each quality score belongs to, and distinguish a numerical comparison from one that satisfies the paper's inclusion rule. Use approximation marks consistently; note when a value was read from a plot rather than merely rounded.

If the author requests neutral case labels instead of naming papers in the artwork, retain an exact source mapping in the caption or appendix. Keep a copy in the author notes. This is a presentation choice, not permission to remove attribution or make an illustrative case appear empirical.

## Preserve Accepted Artwork While Reflowing

A rich illustration can establish the domain while native plots and labels carry evidence. Preserve the accepted illustration when feedback concerns alignment, axes, wording, or panel balance. Those revisions usually need native editing, not another image-generation round. "More information" should mean another supported relationship or outcome, not more prose or decoration.

In a narrow column, test stacked A/B panels before shrinking a wide composition. Align their visible edges, including headings and callouts; a single long headline should not determine the width of the whole canvas. Remove redundant labels, then reduce unused canvas area. Keep enough whitespace to distinguish the reading levels.

## Verify the Inserted Figure

Inspect three separate artifacts: the builder preview, the native PowerPoint export, and the compiled manuscript page. Name them distinctly and record which one was reviewed. A preview cannot establish native rendering, and a native export cannot establish the manuscript's placement.

Measure type at the final column width. The PDF inspection helper, `scripts/inspect_figure_pdf.py`, reports extractable text sizes after scaling; it cannot read raster labels or certify legibility. Inspect the actual page, including caption, neighboring paragraphs, and section transitions. Equal page counts before and after insertion can hide substantial added space within the last page.

Check the caption against both the drawing and the cited appendix. Do not promise calculations if the appendix only lists recomputed values. Use document references for section or appendix identifiers instead of manually maintaining letters or numbers. Keep score ownership, exclusions, and source mapping unambiguous across figure and caption.

When independent review is requested, follow step 5 of `SKILL.md` ("Inspect semantics, appearance, and editability") for the review packet and first-read sequence. Include the compiled page when insertion is in scope. Distinguish blockers from optional polish; a PASS can still carry suggestions. Record artifact hashes, then verify the included and published bytes match the reviewed version. Commit and push only within explicit user authorization, preserving unrelated work.
