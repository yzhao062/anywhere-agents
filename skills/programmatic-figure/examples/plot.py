"""Illustrative score histogram and operating points. Requires Matplotlib."""
import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--data", type=Path, default=HERE / "score-counts.csv")
parser.add_argument("--palette", type=Path, default=HERE.parents[1] / "editable-figure/references/default-palette.json")
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
with args.data.open() as f:
    rows = sorted((float(r["score"]), int(r["negative"]), int(r["positive"])) for r in csv.DictReader(f))
colors = json.loads(args.palette.read_text())["tokens"]
plt.rcParams.update({"font.size": 8, "pdf.fonttype": 42, "svg.fonttype": "none",
                     "text.color": colors["ink"], "axes.labelcolor": colors["ink"]})
fig, (hist, counts) = plt.subplots(1, 2, figsize=(7, 2.8), gridspec_kw={"width_ratios": [1.4, 1]})
fig.subplots_adjust(left=.08, right=.97, bottom=.22, top=.71, wspace=.36)
fig.text(.017, .94, "Which cases meet each score cutoff?", fontsize=13, weight="bold")
fig.text(.018, .85, f"Illustrative score data  /  {sum(n+p for _, n, p in rows)} labeled cases", color=colors["mutedText"])
scores, neg, pos = zip(*rows)
series = [("Negative", "mint", "mintStroke", neg), ("Positive", "coral", "coralStroke", pos)]
for i, (label, fill, stroke, values) in enumerate(series):
    hist.bar(scores, values, width=.075, bottom=neg if i else None,
             color=colors[fill], edgecolor=colors[stroke], linewidth=.65, label=label)
    for j, cutoff in enumerate((.5, .8)):
        n = sum(row[i+1] for row in rows if row[0] >= cutoff)
        y = j + (i-.5)*.33
        counts.barh(y, n, height=.29, color=colors[fill], edgecolor=colors[stroke], linewidth=.65)
        counts.annotate(str(n), (n, y), xytext=(3, 0), textcoords="offset points", va="center")
hist.set(xlabel="Model score", ylabel="Cases", xticks=scores[::2], ylim=(0, max(n+p for _, n, p in rows)*1.4))
hist.legend(frameon=False, ncols=2, loc="upper right", fontsize=7, handlelength=1)
counts.set(yticks=[0, 1], yticklabels=["≥ 0.5", "≥ 0.8"], xlabel="Flagged cases", ylabel="Cutoff")
counts.invert_yaxis()
counts.margins(x=.22)
for ax, title in [(hist, "a   All cases"), (counts, "b   Cases at or above cutoff")]:
    ax.set_title(title, loc="left", fontsize=9, weight="bold")
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(length=2.5)
args.out.mkdir(parents=True, exist_ok=True)
for ext in ["pdf", "svg", "png"]:
    fig.savefig(args.out / f"score-cutoffs.{ext}", dpi=180, facecolor="white")
plt.close(fig)
