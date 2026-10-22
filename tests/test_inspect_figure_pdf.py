"""Exercise physical-size inspection with real temporary PDF fixtures."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

try:
    import pymupdf
except ImportError:
    pymupdf = None

SCRIPT = Path(__file__).resolve().parents[1] / "skills/editable-figure/scripts/inspect_figure_pdf.py"
SPEC = importlib.util.spec_from_file_location("inspect_figure_pdf", SCRIPT)
INSPECTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSPECTOR)


@unittest.skipUnless(pymupdf, "optional PDF helper requires PyMuPDF")
class FigurePDFTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "figure.pdf"

    def make_pdf(self, pages=1, text=True, rotate=False):
        with pymupdf.open() as document:
            for _ in range(pages):
                page = document.new_page(width=720, height=360)
                if text:
                    page.insert_text((40, 50), "Baseline", fontsize=20)
                    page.insert_text((40, 90), "Quality missing", fontsize=12)
                if rotate:
                    page.set_rotation(90)
            document.save(self.path)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), str(self.path), *args],
                              capture_output=True, text=True)

    def test_half_width_scales_type_and_preserves_bytes(self):
        self.make_pdf()
        before = self.path.read_bytes()
        report = INSPECTOR.inspect_pdf(self.path, 127, min_font_pt=7)
        self.assertAlmostEqual(report["minimum_placed_font_pt"], 6)
        self.assertAlmostEqual(report["placed_height_mm"], 63.5)
        self.assertEqual(report["below_minimum"][0]["text"], "Quality missing")
        self.assertEqual(report["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(self.path.read_bytes(), before)

    def test_threshold_exit_status_and_json(self):
        self.make_pdf()
        for minimum, expected in (("6", 0), ("7", 1)):
            with self.subTest(minimum=minimum):
                result = self.run_cli("--width-mm", "127", "--min-font-pt", minimum)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(json.loads(result.stdout)["text_span_count"], 2)

    def test_no_text_is_unverifiable_when_threshold_requested(self):
        self.make_pdf(text=False)
        result = self.run_cli("--width-mm", "77", "--min-font-pt", "7")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertIsNone(report["minimum_placed_font_pt"])
        self.assertEqual(report["text_threshold_status"], "unverifiable")

    def test_multipage_requires_selection_and_checks_range(self):
        self.make_pdf(pages=2)
        for page_args in ((), ("--page", "0"), ("--page", "3")):
            with self.subTest(page_args=page_args):
                self.assertEqual(self.run_cli("--width-mm", "77", *page_args).returncode, 2)
        report = INSPECTOR.inspect_pdf(self.path, 127, page_number=2)
        self.assertEqual(report["page"], 2)

    def test_rotated_page_uses_displayed_width(self):
        self.make_pdf(rotate=True)
        report = INSPECTOR.inspect_pdf(self.path, 127)
        self.assertAlmostEqual(report["minimum_placed_font_pt"], 12)
        self.assertAlmostEqual(report["placed_height_mm"], 254)

    def test_json_output_survives_legacy_stdout_encoding(self):
        with pymupdf.open() as document:
            page = document.new_page(width=720, height=360)
            page.insert_htmlbox(pymupdf.Rect(40, 40, 600, 120),
                                '<p style="font-size:12pt">\u03b1 \u2265 0.5, \u22121.2</p>')
            document.save(self.path)
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.path), "--width-mm", "127"],
                                capture_output=True, text=True,
                                env=dict(os.environ, PYTHONIOENCODING="cp1252"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\u03b1", json.loads(result.stdout)["smallest_text_spans"][0]["text"])

    def test_invalid_dimensions_and_input_fail_cleanly(self):
        self.path.write_bytes(b"This is not a PDF")
        self.assertEqual(self.run_cli("--width-mm", "77").returncode, 2)
        for width in ("0", "-1", "nan", "inf"):
            with self.subTest(width=width):
                result = self.run_cli("--width-mm", width)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)

    def test_png_input_is_rejected_even_without_threshold(self):
        self.make_pdf()
        with pymupdf.open(self.path) as document:
            png = document[0].get_pixmap().tobytes("png")
        self.path.write_bytes(png)
        result = self.run_cli("--width-mm", "77")
        self.assertEqual(result.returncode, 2)
        self.assertIn("input is not a PDF", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_pdf_recovery_messages_do_not_corrupt_json(self):
        with pymupdf.open() as document:
            page = document.new_page(width=720, height=360)
            page.insert_text((40, 50), "Baseline", fontsize=12)
            document.update_stream(page.get_contents()[0],
                                   b"BT /helv 12 Tf 40 50 Td (Label) Tj ET ((( <<")
            document.save(self.path)
        result = self.run_cli("--width-mm", "127")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["page_count"], 1)
        # PyMuPDF 2.0 stopped printing MuPDF messages by default, so there is
        # nothing to redirect; 1.x prints them and they must land on stderr.
        if pymupdf.TOOLS.mupdf_display_errors():
            self.assertIn("MuPDF error", result.stderr)


if __name__ == "__main__":
    unittest.main()
