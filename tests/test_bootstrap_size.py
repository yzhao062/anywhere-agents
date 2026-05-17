"""Phase 0 size-gate: guards against silent baseline growth in aa/AGENTS.md
and in any per-agent file generated from it.

Per docs/followups/2026-05-16-aa-gh-1-context-bloat-remaining.md § Phase 0,
revised 2026-05-17 to be agent-fungible: gate measures every per-agent
derived file the generator produces, not just CLAUDE.md, so that a
regression bloating any agent's file surfaces without depending on a
single agent's tooling being present.

Source bloat in AGENTS.md cascades to all derived files (CLAUDE.md,
agents/codex.md, future agents) because generate_agent_configs.py strips
per-agent tagged blocks but preserves shared content. This test guards both:

1. The canonical AGENTS.md byte count (agent-agnostic root cause).
2. Each per-agent derived file (catches per-agent generator regressions).

Scope: tests the aa upstream baseline AGENTS.md in isolation (no passive
packs composed in). Passive-pack composition is exercised by
test_compose_packs_v0_6; Phase 0 isolates the baseline so Lever 1
compaction has a clean target. The fresh-install end-to-end gate
(baseline + agent-style passive block + bytes ceiling) can layer on top
of this once Lever 1 lands.

Override env vars (regression test simulation):
- ANYWHERE_AGENTS_SIZE_HARD_CEILING_KB (default 75; applied per file)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_agent_configs  # noqa: E402

HARD_CEILING_KB = int(os.environ.get("ANYWHERE_AGENTS_SIZE_HARD_CEILING_KB", "75"))

# Per-agent file ceilings (KB). The subset assertion below fails when
# generate_agent_configs.py's AGENTS table grows new entries; add the new
# file path here.
AGENT_FILE_CEILINGS: dict[str, int] = {
    "AGENTS.md": HARD_CEILING_KB,        # canonical source (drives everything)
    "CLAUDE.md": HARD_CEILING_KB,        # Claude Code derivation
    "agents/codex.md": HARD_CEILING_KB,  # Codex derivation
}

# Soft-warning tiers (KB). Informational only; never fail. The two tiers
# match the plan's Pragmatic / Aggressive target tiers per
# docs/followups/2026-05-16-aa-gh-1-context-bloat-remaining.md and apply
# uniformly to every measured file. The Aggressive tier (40 KB) also
# aligns with CC v2.1.143's "large CLAUDE.md" warning for the Claude
# derivation specifically.
PRAGMATIC_WARN_KB = int(os.environ.get("ANYWHERE_AGENTS_SIZE_PRAGMATIC_WARN_KB", "50"))
AGGRESSIVE_WARN_KB = int(os.environ.get("ANYWHERE_AGENTS_SIZE_AGGRESSIVE_WARN_KB", "40"))


class TestBootstrapSize(unittest.TestCase):
    """Phase 0 agent-fungible size-gate."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # Seed tmp consumer with the upstream baseline only (no passive packs).
        # Use binary copy so line endings are preserved exactly across platforms:
        # write_text() on Windows converts LF -> CRLF, which inflates the byte
        # count by ~1 byte per line and produces different measurements on
        # ubuntu vs windows runners. The size-gate must be deterministic.
        (self.root / "AGENTS.md").write_bytes((ROOT / "AGENTS.md").read_bytes())

        # Run the generator to produce per-agent files (CLAUDE.md, agents/codex.md, ...).
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

    def test_each_agent_file_under_hard_ceiling(self) -> None:
        """Every measured agent file must stay under its hard ceiling.

        Agent-fungible: the assertion loops over every per-agent file in
        AGENT_FILE_CEILINGS. Adding a new agent (extending AGENTS in
        scripts/generate_agent_configs.py) requires adding the file's
        ceiling here; the companion coverage test fails until that entry exists.

        On failure, all violations are reported together so a single test
        run surfaces every agent file that regressed.
        """
        violations: list[str] = []
        for rel, ceiling_kb in AGENT_FILE_CEILINGS.items():
            path = self.root / rel
            if not path.exists():
                # Missing expected output is a regression, not a skip. If
                # generate_agent_configs.py drops or renames a target, the
                # size-gate must fail loudly rather than silently pass.
                violations.append(f"{rel}: expected generated file is missing")
                print(
                    f"bootstrap-size: {rel} MISSING (expected generated file)",
                    file=sys.stderr,
                )
                continue
            size = path.stat().st_size
            size_kb = size / 1024

            # Always emit the measurement so Phase 1+2 work can read the
            # baseline trajectory from test output. Show the tighter tier
            # crossed (Pragmatic 50 KB takes precedence over Aggressive
            # 40 KB when both fire); both are informational, not failures.
            note = ""
            if size > PRAGMATIC_WARN_KB * 1024:
                note = f" [SOFT WARN: > {PRAGMATIC_WARN_KB} KB Pragmatic]"
            elif size > AGGRESSIVE_WARN_KB * 1024:
                note = f" [SOFT WARN: > {AGGRESSIVE_WARN_KB} KB Aggressive]"
            print(
                f"bootstrap-size: {rel} = {size} B ({size_kb:.1f} KB){note}",
                file=sys.stderr,
            )

            if size >= ceiling_kb * 1024:
                violations.append(
                    f"{rel}: {size_kb:.1f} KB exceeds {ceiling_kb} KB hard ceiling"
                )

        if violations:
            self.fail(
                "Phase 0 size-gate failed for one or more agent files:\n  "
                + "\n  ".join(violations)
                + "\n\nInvestigate aa/AGENTS.md or per-agent tag block growth. "
                "Re-run with ANYWHERE_AGENTS_SIZE_HARD_CEILING_KB=<higher> "
                "to confirm the gate is what failed (vs. genuine regression)."
            )

    def test_ceiling_table_covers_all_generator_targets(self) -> None:
        """Enforce the agent-fungibility contract at test discovery time.

        AGENT_FILE_CEILINGS must include AGENTS.md plus every per-agent
        file the generator produces (read from generate_agent_configs.AGENTS).
        If a new agent is added to the generator (e.g., a future Gemini
        target) without a matching ceiling entry here, this test fails
        until the ceiling is added — agent-fungibility enforced by the
        gate itself, not by maintainer memory.
        """
        expected = {"AGENTS.md"} | {
            agent["output_rel"] for agent in generate_agent_configs.AGENTS
        }
        missing = expected - set(AGENT_FILE_CEILINGS)
        self.assertFalse(
            missing,
            f"AGENT_FILE_CEILINGS is missing entries for generator outputs: "
            f"{sorted(missing)}. Add ceilings for these files so the "
            f"size-gate covers all agents the generator produces.",
        )


if __name__ == "__main__":
    unittest.main()
