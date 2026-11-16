"""Rebuild the Qwen examples with Python and Matplotlib. See sources.md."""
import argparse
import csv
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from fontTools.pens.recordingPen import RecordingPen
from fontTools.svgLib.path import parse_path
from matplotlib.colors import LinearSegmentedColormap, to_rgb
from matplotlib.offsetbox import AnnotationBbox, DrawingArea
from matplotlib.patches import FancyBboxPatch, Patch, PathPatch
from matplotlib.path import Path as MplPath
from matplotlib.ticker import FuncFormatter, MultipleLocator
from matplotlib.transforms import Affine2D

HERE = Path(__file__).resolve().parent
PALETTE = HERE.parents[2] / "editable-figure/references/default-palette.json"
FAMILY = {"Qwen4-Max": "qwen", "Qwen3.8-Max-0902": "qwen", "Opus5.5": "claude",
          "GPT6.1-Sol": "openai", "GPT6-Astra": "openai", "Kimi K3.1": "kimi"}


def logo_path(family):
    pen = RecordingPen()
    for shape in ET.parse(HERE / "logos" / f"{family}.svg").getroot().findall("{*}path"):
        parse_path(shape.attrib["d"], pen)
    vertices, codes = [], []
    commands = {"moveTo": MplPath.MOVETO, "lineTo": MplPath.LINETO, "curveTo": MplPath.CURVE4}
    for operation, points in pen.value:
        if operation == "closePath":
            vertices.append((0, 0))
            codes.append(MplPath.CLOSEPOLY)
        elif operation != "endPath":
            vertices.extend(points)
            codes.extend([commands[operation]] * len(points))
    return MplPath(vertices, codes)


def add_bar_logo(ax, x, score, mark, ink):
    bar_height_pt = (ax.transData.transform((x, score))[1] - ax.transData.transform((x, 0))[1]) * 72 / ax.figure.dpi
    size = min(15, bar_height_pt - 4)
    if size < 7:
        return
    badge = DrawingArea(size, size)
    badge.add_artist(FancyBboxPatch((0, 0), size, size, boxstyle="round,pad=0,rounding_size=2.5",
                                    facecolor="white", edgecolor="none"))
    scale = .80 * size / 24  # The four source SVGs use a 24-unit viewBox.
    path = Affine2D().scale(scale, -scale).translate(.10 * size, .90 * size).transform_path(mark)
    badge.add_artist(PathPatch(path, facecolor=ink, edgecolor="none"))
    ax.add_artist(AnnotationBbox(badge, (x, score), xybox=(0, -2.5), boxcoords="offset points",
                                 box_alignment=(.5, 1), frameon=False, pad=0))


def read_rows(name):
    with (HERE / name).open(newline="") as stream:
        return list(csv.DictReader(stream))


def save(fig, folder, name):
    for suffix in ("pdf", "svg", "png"):
        fig.savefig(folder / f"{name}.{suffix}", dpi=165, facecolor="white")
    plt.close(fig)


def benchmarks(colors):
    rows = read_rows("benchmarks.csv")
    models = list(FAMILY)
    marks = {family: logo_path(family) for family in set(FAMILY.values())}
    fills = [colors[k] for k in ("coral", "mint", "teal", "gold", "gray", "mutedText")]
    fig, axes = plt.subplots(2, 3, figsize=(11, 6.5))
    fig.subplots_adjust(left=.055, right=.985, top=.727, bottom=.094, wspace=.19, hspace=.56)
    fig.text(.043, .927, "Qwen4-Max across six benchmarks", size=24, weight="bold")
    gains = [(float(r[models[0]]) - float(r[models[1]]), r["benchmark"]) for r in rows
             if r[models[0]] and r[models[1]]]
    if gains:
        gain, task = max(gains)
        fig.text(.045, .874, f"Largest Qwen4-Max minus Qwen3.8-Max difference: {gain:+.1f} points on {task}.",
                 size=11, color=colors["mutedText"])
    fig.legend([Patch(facecolor=c) for c in fills], models, loc="center left",
               bbox_to_anchor=(.039, .808), frameon=False, ncols=6, fontsize=9,
               handlelength=1.05, columnspacing=2.0)
    for ax, row in zip(axes.flat, rows):
        ax.set_title(row["benchmark"], loc="left", fontsize=13, fontweight="bold", pad=15)
        ax.text(0, 1.015, row["domain"], transform=ax.transAxes, fontsize=8.5, color=colors["mutedText"])
        ax.set(xlim=(-.65, 5.65), ylim=(0, 100), xticks=[], yticks=[0, 50, 100])
        ax.tick_params(axis="y", length=0, labelsize=7, colors=colors["mutedText"], pad=2)
        ax.grid(axis="y", color=colors["rule"], linewidth=.6)
        ax.set_axisbelow(True)
        for x, (model, color) in enumerate(zip(models, fills)):
            value = row[model]
            if not value:
                ax.text(x, 4, "n/a", ha="center", size=8, color=colors["mutedText"])
                continue
            score = float(value)
            base = tuple(.76 + .24 * channel for channel in to_rgb(color))
            ramp = LinearSegmentedColormap.from_list("bar", [base, color])
            ax.pcolormesh([x-.38, x+.38], [0, score], [[0, 0], [1, 1]],
                          cmap=ramp, shading="gouraud", zorder=2)
            add_bar_logo(ax, x, score, marks[FAMILY[model]], colors["ink"])
            ax.text(x, score+2.6, value, ha="center", fontsize=9,
                    weight="bold" if x == 0 else "normal")
    fig.text(.044, .027, "Source: Qwen4-Max release evaluation  ·  Scores as reported  ·  n/a = score not shown in the source",
             size=8.2, color=colors["mutedText"])
    return fig


def efficiency(colors):
    rows = read_rows("efficiency.csv")
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.95))
    fig.subplots_adjust(left=.075, right=.978, bottom=.205, top=.74, wspace=.22)
    fig.text(.048, .915, "Score versus tokens per task", size=24, weight="bold")
    fig.text(.05, .85, "Each point is one reasoning setting. Brackets compare token use at Xhigh.",
             size=11, color=colors["mutedText"])
    styles = [
        ("Qwen4-Max", colors["coralStroke"], colors["coral"], "o", "-"),
        ("Qwen3.8-Max-0902", colors["mintStroke"], colors["mint"], "D", "--"),
    ]
    for ax, task in zip(axes, dict.fromkeys(row["benchmark"] for row in rows)):
        ax.set_title(task, loc="left", fontsize=14, weight="bold", pad=10)
        for model, edge, fill, marker, style in styles:
            by_effort = {r["effort"]: r for r in rows if r["benchmark"] == task and r["model"] == model}
            points = [by_effort[effort] for effort in ("Low", "High", "Xhigh") if effort in by_effort]
            xs = [int(p["tokens_per_task_k"]) for p in points]
            ys = [float(p["score"]) for p in points]
            ax.plot(xs, ys, color=edge, linestyle=style, lw=2.1, marker=marker, ms=7,
                    markerfacecolor=fill, markeredgewidth=1.3, label=model)
            for x, y, p in zip(xs, ys, points):
                low = p["effort"] == "Low"
                offset = (-5, 6) if p["effort"] == "High" else (0, -9 if low else 8)
                ax.annotate(p["effort"], (x, y), xytext=offset,
                            textcoords="offset points", ha="right" if offset[0] else "center", va="top" if low else "bottom",
                            fontsize=9, color=edge)
        ax.margins(x=.12, y=.24)
        costs = {r["model"]: int(r["tokens_per_task_k"]) for r in rows
                 if r["benchmark"] == task and r["effort"] == "Xhigh"}
        if all(model in costs for model, *_ in styles):
            xhigh = [costs["Qwen4-Max"], costs["Qwen3.8-Max-0902"]]
            lower, upper = ax.get_ylim()
            span = upper - lower
            bracket = upper + .035 * span
            ax.set_ylim(lower, upper + .19 * span)
            ax.plot(xhigh, [bracket, bracket], color=colors["mutedText"], marker="|", ms=6, lw=.7)
            ax.annotate(f"Xhigh gap ≈{abs(xhigh[1] - xhigh[0])}k tokens", (sum(xhigh)/2, bracket),
                        xytext=(0, 5), textcoords="offset points", ha="center", size=8, color=colors["ink"])
        ax.yaxis.set_major_locator(MultipleLocator(5 if task == "Work360" else 10))
        ax.xaxis.set_major_locator(MultipleLocator(20 if task == "Work360" else 50))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:g}k"))
        ax.grid(axis="y", color=colors["rule"], lw=.8)
        ax.set_xlabel("Tokens per task", labelpad=7)
        ax.tick_params(length=0, pad=5, colors=colors["mutedText"])
    axes[0].set_ylabel("Score", labelpad=6)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower right", bbox_to_anchor=(.983, .022), frameon=False,
               ncols=2, fontsize=9, handlelength=2.4)
    fig.text(.05, .047, "Source: Qwen4-Max release evaluation  ·  Coordinates approximated from the published curves",
             fontsize=8, color=colors["mutedText"])
    return fig


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--palette", type=Path, default=PALETTE)
    args = parser.parse_args()
    colors = json.loads(args.palette.read_text())["tokens"]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "text.color": colors["ink"],
                         "axes.labelcolor": colors["ink"], "axes.titlecolor": colors["ink"],
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.spines.left": False, "axes.spines.bottom": False,
                         "svg.fonttype": "none", "pdf.fonttype": 42})
    args.out.mkdir(parents=True, exist_ok=True)
    save(benchmarks(colors), args.out, "benchmarks")
    save(efficiency(colors), args.out, "efficiency")
