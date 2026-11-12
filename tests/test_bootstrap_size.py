"""Size gate for the shared AGENTS.md and every per-agent file generated from it.

The 2026-09 rewrite (docs/followups/2026-09-17-agents-md-diet.md) brought the
shared baseline from 74.6 KB (ac) and 66.0 KB (aa) down to one file of about
23 KB that is byte-identical in both repos. Two ceilings guard the result:

1. A rewrite gate of 24,576 bytes, the number the rewrite was sized to. It is
   what the acceptance criterion names and is kept here as the record.
2. A routine ceiling per file of the measured size plus ten percent, rounded
   up to the next 512 bytes. A change that needs more is a budget decision
   and records the new number in CEILINGS below with its reason.

Ceilings are bytes. The old version of this test in anywhere-agents used KB
constants multiplied by 1024 under a decimal name and carried 50 KB and
40 KB soft tiers that never failed; both are gone. Codex injects only the
first 32 KiB of a discovered AGENTS.md by default (project_doc_max_bytes), so
the routine ceilings also keep the baseline well inside that budget with
room for the passive packs a consumer composes in.

The file is measured through the generator on a fresh copy, so the gate
covers every agent the generator knows about, and a new generator target
without a ceiling entry fails the coverage test rather than passing unseen.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# tests/ is on sys.path under `unittest discover -s tests` but not under
# `python -m unittest tests.<module>`, which validate.yml uses for the
# Sentinel redaction smoke. Put it there before the sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_agent_configs  # noqa: E402

# The size the 2026-09 rewrite was held to. The ten-percent test below reads
# this constant, so editing it moves that bound.
REWRITE_GATE_BYTES = 24_576

# Routine ceilings in bytes: measured size at the rewrite (2026-09-17) plus
# ten percent, rounded up to the next 512. Measured: AGENTS.md 23,812;
# CLAUDE.md 24,540; agents/codex.md 24,552. The generated files carry a
# header comment the baseline does not, which is why their ceiling is higher.
CEILINGS: dict[str, int] = {
    "AGENTS.md": 26_624,
    "CLAUDE.md": 27_136,
    "agents/codex.md": 27_136,
}


class TestBootstrapSize(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Binary copy so line endings survive: write_text() on Windows turns
        # LF into CRLF, which adds a byte per line and makes the measurement
        # differ between the ubuntu and windows runners.
        (self.root / "AGENTS.md").write_bytes((ROOT / "AGENTS.md").read_bytes())
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "generate_agent_configs.py"),
                "--root", str(self.root),
                "--quiet",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode, 0,
            f"generator failed (rc={result.returncode}):\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )

    def test_each_file_under_its_ceiling(self) -> None:
        """Every measured file stays under its routine ceiling.

        All violations are reported together so one run shows every file
        that regressed. A missing generator output is a failure, not a skip:
        if the generator drops or renames a target, the gate says so.
        """
        violations: list[str] = []
        for rel, ceiling in CEILINGS.items():
            path = self.root / rel
            if not path.exists():
                violations.append(f"{rel}: expected generated file is missing")
                continue
            size = path.stat().st_size
            # Always print the measurement so the trajectory is readable from
            # test output without a separate script.
            print(f"bootstrap-size: {rel} = {size} B (ceiling {ceiling} B)", file=sys.stderr)
            if size > ceiling:
                violations.append(f"{rel}: {size} B exceeds the {ceiling} B ceiling")
        if violations:
            self.fail(
                "size gate failed:\n  " + "\n  ".join(violations)
                + "\n\nAGENTS.md holds rules; rationale and how-to material go to "
                "docs/ and are linked from its Reference section. A larger "
                "budget is a recorded decision: raise the entry in CEILINGS "
                "with a comment saying why."
            )

    def test_ceilings_stay_within_ten_percent_of_the_rewrite_gate(self) -> None:
        """Routine ceilings stay within the rewrite gate plus ten percent,
        rounded up to 512 bytes. A later increase requires a separately
        documented budget and an updated bound in this test.
        """
        allowed = -(-(REWRITE_GATE_BYTES + REWRITE_GATE_BYTES // 10) // 512) * 512
        for rel, ceiling in CEILINGS.items():
            self.assertLessEqual(
                ceiling, allowed,
                f"{rel} ceiling {ceiling} B is above the rewrite gate plus ten "
                f"percent ({allowed} B); record the budget decision",
            )

    def test_ceiling_table_covers_all_generator_targets(self) -> None:
        """CEILINGS names AGENTS.md plus every file the generator produces.

        A new generator target (a future Gemini file, say) without a ceiling
        entry fails here until the entry exists, so coverage does not depend
        on anyone remembering this file.
        """
        expected = {"AGENTS.md"} | {
            agent["output_rel"] for agent in generate_agent_configs.AGENTS
        }
        missing = expected - set(CEILINGS)
        self.assertFalse(
            missing,
            f"CEILINGS is missing entries for generator outputs: {sorted(missing)}",
        )


if __name__ == "__main__":
    unittest.main()
