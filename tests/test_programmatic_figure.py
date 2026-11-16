"""A missing result must stay visible without contributing to a comparison."""
import csv
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


SKILLS = Path(__file__).resolve().parents[1] / "skills"
EXAMPLE = SKILLS / "programmatic-figure/examples/qwen"


@unittest.skipUnless(importlib.util.find_spec("matplotlib") and importlib.util.find_spec("fontTools"),
                     "requires Matplotlib and fontTools")
class MissingMeasurementsTest(unittest.TestCase):
    def test_partial_benchmark_and_unpaired_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy(EXAMPLE / "plot.py", root)
            shutil.copytree(EXAMPLE / "logos", root / "logos")
            with (EXAMPLE / "benchmarks.csv").open(newline="") as src:
                rows = list(csv.DictReader(src))
            for row in rows:
                row["Qwen3.8-Max-0902"] = ""
            with (root / "benchmarks.csv").open("w", newline="") as dest:
                writer = csv.DictWriter(dest, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            with (EXAMPLE / "efficiency.csv").open(newline="") as src:
                runs = list(csv.DictReader(src))
            with (root / "efficiency.csv").open("w", newline="") as dest:
                writer = csv.DictWriter(dest, fieldnames=runs[0].keys())
                writer.writeheader()
                writer.writerows(r for r in reversed(runs) if not (
                    r["benchmark"] == "Work360" and r["model"] == "Qwen4-Max" and r["effort"] == "Xhigh"))
            subprocess.run([sys.executable, root / "plot.py", "--out", root, "--palette",
                            SKILLS / "editable-figure/references/default-palette.json"], check=True)
            def labels(name):
                return [t.text for t in ET.parse(root / name).iter("{http://www.w3.org/2000/svg}text")]
            bars = labels("benchmarks.svg")
            self.assertEqual(bars.count("n/a"), len(rows) + 1)
            self.assertFalse(any(s and s.startswith("Largest ") for s in bars))
            gaps = [s for s in labels("efficiency.svg") if s and s.startswith("Xhigh gap")]
            self.assertEqual(gaps, ["Xhigh gap ≈137k tokens"])
