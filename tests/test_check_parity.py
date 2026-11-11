"""The shared AGENTS.md is one file in two repos; this checks the mirror.

Since the 2026-09 rewrite the shared baseline is authored in agent-config and
mirrored byte for byte into anywhere-agents (scripts/check-parity.sh lists it
under STRICT). The old section-level test here compared one banner bullet
across the two copies because the whole file was allowed to differ; full-file
identity now replaces it.

The cross-repo assertion runs only when the ac sibling clone is on the
maintainer's filesystem; CI has one repo on disk and skips it. The
single-repo checks (no maintainer-only lines in the shared file, the
generated files derived from it, the wheel mirror in step with the source)
always run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

# tests/ is on sys.path under `unittest discover -s tests` but not under
# `python -m unittest tests.<module>`, which validate.yml uses for the
# Sentinel redaction smoke. Put it there before the sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows


REPO_ROOT = Path(__file__).resolve().parents[1]
AA_AGENTS = REPO_ROOT / "AGENTS.md"

# Tokens that belong to the maintainer's own setup and reach consumers through
# the agent-pack packs or stay in agent-config/AGENTS.local.md. The shared
# file keeps personal content out by construction rather than by a strip
# step, and this pins that.
MAINTAINER_ONLY_TOKENS = ("py312", "PyCharm", "Overleaf", "yuezh", "USC")


def _candidate_ac_roots() -> list[Path]:
    """Sibling lookup paths for an ac clone (maintainer-local, not CI)."""
    candidates: list[Path] = []
    env = os.environ.get("AGENT_CONFIG_REPO")
    if env:
        candidates.append(Path(env))
    candidates.append(REPO_ROOT.parent / "agent-config")
    return candidates


def _find_ac_root() -> Path | None:
    for c in _candidate_ac_roots():
        if (c / "AGENTS.md").is_file() and (c / "scripts" / "check-parity.sh").is_file():
            return c
    return None


class SharedBaselineTests(unittest.TestCase):
    def test_shared_file_carries_no_maintainer_only_lines(self) -> None:
        text = AA_AGENTS.read_text(encoding="utf-8")
        for token in MAINTAINER_ONLY_TOKENS:
            self.assertNotIn(
                token, text,
                f"{token!r} in the shared AGENTS.md; maintainer lines go to "
                "agent-config/AGENTS.local.md or an agent-pack pack",
            )

    def test_shared_file_names_the_public_upstream(self) -> None:
        text = AA_AGENTS.read_text(encoding="utf-8")
        self.assertIn("raw.githubusercontent.com/yzhao062/anywhere-agents/main/bootstrap/", text)
        self.assertNotIn("raw.githubusercontent.com/yzhao062/agent-config/", text)


class SharedBaselineMirrorTests(unittest.TestCase):
    """Cross-repo: the shared file and its generated copies are byte-identical
    between anywhere-agents and the agent-config sibling clone.

    Only runs when an ac sibling clone is available locally. CI has only one
    repo on disk and skips this class; the maintainer's local runs and
    scripts/check-parity.sh catch drift before a release.
    """

    def setUp(self) -> None:
        ac_root = _find_ac_root()
        if ac_root is None:
            self.skipTest(
                "ac sibling clone not found; set AGENT_CONFIG_REPO env or "
                "place the agent-config clone next to anywhere-agents"
            )
        self.ac_root = ac_root

    def test_agents_md_is_byte_identical_with_ac(self) -> None:
        self.assertEqual(
            AA_AGENTS.read_bytes(),
            (self.ac_root / "AGENTS.md").read_bytes(),
            "AGENTS.md drifted between anywhere-agents and agent-config; "
            "copy the agent-config file over this one and regenerate "
            "(`python scripts/generate_agent_configs.py`).",
        )

    def test_generated_files_are_byte_identical_with_ac(self) -> None:
        for rel in ("CLAUDE.md", "agents/codex.md"):
            with self.subTest(file=rel):
                self.assertEqual(
                    (REPO_ROOT / rel).read_bytes(),
                    (self.ac_root / rel).read_bytes(),
                    f"{rel} drifted between the repos; regenerate in both",
                )


class AaInternalStrictBlockTests(unittest.TestCase):
    """Phase 6 of v0.6.0: ``scripts/check-parity.sh`` carries an aa-internal
    STRICT block that compares aa source files against their wheel-bundled
    mirror at ``packages/pypi/anywhere_agents/composer/``. Drift in any
    mirrored file must cause the script to exit nonzero and print the
    offending source-side path.

    The script is bash-only (the cross-repo logic predates Python tooling)
    and is exercised here via subprocess. Skips when ``bash`` is not on
    PATH (rare on Windows-without-Git-for-Windows or stripped-down CI).
    """

    SCRIPT = REPO_ROOT / "scripts" / "check-parity.sh"
    MIRROR_FILE = (
        REPO_ROOT
        / "packages"
        / "pypi"
        / "anywhere_agents"
        / "composer"
        / "bootstrap"
        / "packs.yaml"
    )

    @staticmethod
    def _resolve_bash() -> str | None:
        """Find a bash that can actually execute scripts.

        On Windows, ``shutil.which("bash")`` may return a WSL launcher
        (``C:\\Windows\\System32\\bash.exe``) or a Microsoft Store stub
        before it returns Git for Windows' real bash. The WSL launcher
        cannot execute a Windows-path script and crashes with
        ``execvpe(/bin/bash) failed`` even before reading argv. Prefer
        a known Git-for-Windows install path; fall back to PATH lookup
        only when no Git Bash is installed (POSIX hosts, where the PATH
        result is correct).
        """
        candidates = [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ]
        for c in candidates:
            if Path(c).is_file():
                return c
        return shutil.which("bash")

    @classmethod
    def setUpClass(cls) -> None:
        cls.bash = cls._resolve_bash()
        if cls.bash is None:
            raise unittest.SkipTest("bash not on PATH; cannot exercise check-parity.sh")
        if not cls.SCRIPT.is_file():
            raise unittest.SkipTest(f"check-parity.sh not found at {cls.SCRIPT}")
        if not cls.MIRROR_FILE.is_file():
            raise unittest.SkipTest(
                f"wheel-bundled mirror not found at {cls.MIRROR_FILE}; "
                "expected after v0.5.6 mirror layout"
            )

    def _run_script(self) -> subprocess.CompletedProcess:
        # --aa-internal-only, because this block is the only one a single
        # checkout can answer. Naming the aa root without the flag points both
        # roots at one tree, and the script refuses that as the vacuous
        # self-comparison it is; CI has no sibling agent-config to offer.
        return subprocess.run(
            [self.bash, str(self.SCRIPT), "--aa-internal-only", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

    def test_aa_internal_strict_detects_drift(self) -> None:
        """Synthesize a one-byte drift in the wheel-bundled mirror copy of
        ``bootstrap/packs.yaml`` and assert ``check-parity.sh`` exits
        nonzero with the source-side path printed.

        Uses save/restore so the test cleans up after itself even if the
        assertion fails (try/finally).
        """
        original = self.MIRROR_FILE.read_bytes()
        try:
            # Append a single byte. Plain whitespace keeps the YAML
            # syntactically valid (so other tests / tools that read the
            # mirror copy during the test window do not crash) while still
            # producing real byte-level drift that diff -q catches.
            self.MIRROR_FILE.write_bytes(original + b" ")

            result = self._run_script()

            self.assertNotEqual(
                result.returncode,
                0,
                "check-parity.sh should exit nonzero when the wheel mirror "
                "drifts from the aa source; got exit 0.\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            combined = result.stdout + result.stderr
            self.assertIn(
                "bootstrap/packs.yaml",
                combined,
                "expected source-side path 'bootstrap/packs.yaml' in "
                "check-parity.sh output when the mirror drifts.\n"
                f"output:\n{combined}",
            )
        finally:
            self.MIRROR_FILE.write_bytes(original)

    def test_aa_internal_strict_covers_the_vendored_renderer(self) -> None:
        """The wheel re-renders the session banner after its heal pass from
        composer/scripts/render_banner.py, so that copy and pack_identity.py
        beside it are release-gated like the composer itself."""
        mirror_scripts = self.MIRROR_FILE.parent.parent / "scripts"
        for name in ("render_banner.py", "pack_identity.py"):
            with self.subTest(file=name):
                mirrored = mirror_scripts / name
                self.assertTrue(mirrored.is_file(), f"{name} missing from the wheel mirror")
                original = mirrored.read_bytes()
                try:
                    mirrored.write_bytes(original + b"\n# drift\n")
                    result = self._run_script()
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(f"scripts/{name}", result.stdout + result.stderr)
                finally:
                    mirrored.write_bytes(original)

    def test_aa_internal_strict_covers_the_agent_models_skill(self) -> None:
        """agent-models ships through the pack like the other shared skills, so
        its tree and its pointer in the wheel mirror are release-gated too."""
        composer = self.MIRROR_FILE.parent.parent
        for rel in (
            "skills/agent-models/SKILL.md",
            ".claude/commands/agent-models.md",
        ):
            with self.subTest(file=rel):
                mirrored = composer / rel
                self.assertTrue(mirrored.is_file(), f"{rel} missing from the wheel mirror")
                original = mirrored.read_bytes()
                try:
                    mirrored.write_bytes(original + b"\n<!-- drift -->\n")
                    result = self._run_script()
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("agent-models", result.stdout + result.stderr)
                finally:
                    mirrored.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
