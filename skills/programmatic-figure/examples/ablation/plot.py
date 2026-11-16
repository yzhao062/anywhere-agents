"""Plot paired ablation panels from illustrative per-seed measurements."""
import argparse
import csv
import json
from pathlib import Path
from statistics import mean

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--data", type=Path, default=HERE / "seeds.csv")
parser.add_argument("--palette", type=Path, default=HERE.parents[2] / "editable-figure/references/default-palette.json")
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
with args.data.open(newline="") as stream:
    records = list(csv.DictReader(stream))
colors = json.loads(args.palette.read_text())["tokens"]
configs = list(dict.fromkeys(r["configuration"] for r in records))
fig, axes = plt.subplots(1, 2, figsize=(6.75, 2.3), sharey=True)
fig.subplots_adjust(left=.24, right=.95, bottom=.28, top=.80, wspace=.30)
fig.text(.045, .90, "Illustrative ablation   /   Circles: individual seeds   /   Vertical marks: means",
         size=8, color=colors["mutedText"])
for ax, (field, label) in zip(axes, [("auroc", "AUROC (higher is better)"),
                                    ("latency_ms", "Latency, ms (lower is better)")]):
    for y, config in enumerate(configs):
        values = [float(r[field]) for r in records if r["configuration"] == config and r[field]]
        if not values:
            ax.text(.5, y, "n/a", transform=ax.get_yaxis_transform(), ha="center")
            continue
        fill, edge = ("coral", "coralStroke") if config == "Full" else ("mint", "mintStroke")
        ax.scatter(values, [y]*len(values), s=26, facecolor=colors[fill], edgecolor=colors[edge], zorder=2)
        ax.scatter(mean(values), y, marker="|", s=170, linewidth=2, color=colors["ink"], zorder=3)
        ax.text(1.01, y, f"n={len(values)}", transform=ax.get_yaxis_transform(), va="center",
                size=7, color=colors["mutedText"])
    ax.set_xlabel(label, fontsize=8.5)
    ax.grid(axis="y", color=colors["rule"], linewidth=.7)
    ax.spines[["top", "left", "right"]].set_visible(False)
    ax.tick_params(length=0, labelsize=8, pad=5)
    ax.margins(x=.12)
axes[0].set(yticks=range(len(configs)), yticklabels=configs, ylim=(len(configs)-.55, -.45))
args.out.mkdir(parents=True, exist_ok=True)
with plt.rc_context({"pdf.fonttype": 42, "svg.fonttype": "none"}):
    for ext in ("png", "pdf", "svg"):
        fig.savefig(args.out / f"ablation.{ext}", dpi=180)
plt.close(fig)
