---
name: programmatic-figure
description: Draw publication figures directly from data and runnable code. Use for benchmark comparisons, ablations, scaling and efficiency curves, distributions, heatmaps, and quantitative panels in papers or technical posts. Uses the author's lab colors and Qwen4-Max visual references. Flowcharts and architecture overviews belong to editable-figure.
---

# Programmatic Figure

Deliver the plotting source, its inputs or a loader for them, and a verified export. Work directly in code, normally Python with Matplotlib and a headless backend. Keep an existing R, PGFPlots, or other plotting pipeline when it already reads the project data and meets the requested format. No PowerPoint or generated concept image is needed. In a mixed figure, supply the measured panel at its allotted width; the overview workflow owns the schematic.

## Choose the graphic from the comparison

Read the table, logs, or analysis code first. Identify the question, observation unit, metric direction, and intended display width. Check splits, units, missing values, and aggregation before comparing series. A missing run is missing, never zero. Label a mock dataset visibly as illustrative; do not turn reference figures or plausible numbers into experimental results.

| The reader wants to see | Prefer |
|---|---|
| Which methods lead across benchmarks? | Small multiples with consistent method order and one key. Share scales when units and ranges are comparable; otherwise label each scale. Use dots for close differences and start magnitude bars at zero. |
| How does performance change with cost or compute? | Put measured cost on the horizontal axis and quality on the vertical. Mark evaluated settings; connect ordered settings within a method. Curves across different budgets do not establish a matched-budget win. |
| Which component matters? | Align ablation rows across metrics. Show the full configuration or named baseline. For deltas, identify the reference and keep zero visible. |
| How variable are the outcomes? | Show observations, an ECDF, or a distribution with sample counts. Identify what an interval summarizes (runs, examples, or subjects) and how it was computed; do not manufacture intervals from means. |
| Where is the pattern in a matrix? | Order rows and columns by a stated rule. Use a sequential lightness scale for magnitude, or a diverging one centered on a meaningful reference for signed effects. Keep missing cells distinguishable. |

## Turn the chosen comparison into a figure

Use the two [source collections](references/visual-sources.md) as a design library. Qwen shows how readers move between a model, a number, and a claim. Chen Liu's scripts show how to arrange observations, methods, and metrics so that comparison survives a change of dataset. Apply the following decisions in the builder.

1. **Map the inputs.** Name the column or expression that determines each panel, series, position, and annotation. Read the placement width from the manuscript template and build at that width. A two-column paper might allow about 3.35 inches for one column and 6.75 for both. Put the headline in the caption when page space is tight. For example, a model-by-task table supports within-task rankings. A run log with effort and token counts supports score at a measured cost. A table of mean scores alone cannot show run-to-run spread.
2. **Choose the layout and key together.** For model-by-task scores, use one panel per task and one stable model order. In a Qwen-style grid, draw zero-based gradient bars, direct values above their tips, and recognizable family marks just inside when space allows. Use the full color at the value edge and a pale tint at zero. Keep exact variants in a shared key. For component-by-metric data, align named configurations down the rows and metrics across panels as in ImmunoStruct. Give a panel group its own key when its configurations change. For close differences that bars hide, switch to labeled dots on a visible scale.
3. **Define the comparison in data coordinates.** For a budget or parameter sweep, put the measured budget or parameter on the horizontal axis and group observations by method before joining ordered settings. Use markers at sampled points, as in RNAGenScape. Label effort when it identifies the point, as in Qwen's efficiency curves. For a savings annotation, look up the same task and effort for both methods and subtract their measured costs. When settings have different meanings, compare at a measured common score or show the separate operating points. Identify interpolation if used. Use a labeled log axis for positive budgets spanning orders of magnitude. For a dense training trace, use a fine line and preserve fluctuations relevant to the claim.
4. **Assign identity once.** Map model or configuration names to styles explicitly, then reuse them across panels. Start from the manuscript colors or the lab's [palette](../editable-figure/references/default-palette.json). Use coral for the focal series, mint for its paired comparison, and teal, gold, or gray where more categories need distinction. Use the matching stroke tokens for coral and mint curves. For a thin gold or teal trace, darken the hue toward ink until it reads at print size. Pair hue with a label, marker, dash, or stable position. Keep quantitative color scales ordered.
5. **Read the export as a reader.** Trace one series from the key to its mark, recover one value and reproduce the stated comparison from the inputs. When transcribing another chart, check the series identity against its original key even where a family logo repeats or an entry is missing. Inspect the shortest bar or closest pair of points at the intended width. Move a crowded label, move a logo to the key, or split a dense grid before reducing type. Re-render after fixing the first failed reading.

## Build, verify, deliver

Keep labels, transforms, ordering, and styling in the builder. Read the project data once and derive annotations from the same records as the marks. Make the script runnable from a clean process without notebook state or a desktop app. Record the command, dependencies, and any stochastic seed. If the builder moves to another project, put its palette and other required inputs alongside it.

Verify any remaining headline claims against the inputs. For paper placement, use the final column width; if insertion was requested, inspect the compiled page. After a revision, regenerate the exports from the current source.

Deliver a vector PDF for print or an SVG for web, with a PNG preview. Dense point clouds or image layers may be rasterized while axes and labels remain vector. Keep the code, data locator, and exact rebuild command with the result.

## Runnable examples

For benchmark comparisons, inspect the [six-benchmark grid](examples/qwen/benchmarks.png). For compute tradeoffs, inspect the [score versus tokens curves](examples/qwen/efficiency.png). Both use Qwen's published figures with the lab palette. The [builder](examples/qwen/plot.py) and [data notes](examples/qwen/sources.md) show how to preserve missing scores and label estimates from a published curve. Run `python examples/qwen/plot.py --out /path/to/figures` from the skill directory with Matplotlib and fontTools installed.

For an ablation, the [per-seed example](examples/ablation/plot.py) aligns configurations across metrics with opposite directions, shows each observed seed, and computes each mean from the available measurements. Its [preview](examples/ablation/ablation.png) uses illustrative data. Run `python examples/ablation/plot.py --out /path/to/figures`.

For a distribution and two operating points, [plot.py](examples/plot.py) reads [illustrative score counts](examples/score-counts.csv). Run `python examples/plot.py --out build` from the skill directory. [Preview](examples/score-cutoffs.png).
