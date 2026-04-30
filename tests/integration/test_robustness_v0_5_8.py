"""Integration tests for v0.5.8 Basic Command Robustness.

End-to-end reproduction of the usc-admin scenario: a project with a compact
AGENTS.md but oversized/stale CLAUDE.md and agents/codex.md (left over from a
previous compose run that did not regenerate them).

The test uses the REAL generate_agent_configs.py from the source tree under
.agent-config/repo/scripts/ and a stub compose_packs.py (returns 0) to verify
that _invoke_composer_with_gen_fallback regenerates the derived files from the
current AGENTS.md content.

Assertion: rc == 0, and CLAUDE.md / agents/codex.md are regenerated (smaller
than the stale versions AND contain the GENERATED FILE marker).
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "pypi"))
sys.path.insert(0, str(ROOT / "scripts"))

from anywhere_agents import cli  # noqa: E402


REAL_GENERATOR = ROOT / "scripts" / "generate_agent_configs.py"

# Minimal AGENTS.md for the test project (~compact, << 135 KB).
_COMPACT_AGENTS_MD = textwrap.dedent("""\
    # AGENTS.md

    Minimal agent configuration for integration test.

    ## Writing Defaults

    - Use clear, concise language.
    - Prefer active voice.

    ## Session Start Check

    No special session checks needed.
""")

# Simulate a stale CLAUDE.md left over from a previous compose run.
# Must contain GENERATED FILE marker so the generator is willing to overwrite it.
_STALE_CLAUDE_MD = textwrap.dedent("""\
    <!-- GENERATED FILE: do not edit by hand.
    Override with CLAUDE.local.md. -->

    # Stale CLAUDE.md (135 KB-equivalent placeholder)

    This file simulates an oversized stale CLAUDE.md that was written by a
    previous compose run but not regenerated after AGENTS.md was compacted.
    The generator must overwrite it on the next successful compose.
""") + "\n" + ("# padding line\n" * 200)

_STALE_CODEX_MD = textwrap.dedent("""\
    <!-- GENERATED FILE: do not edit by hand.
    Override with agents/codex.local.md. -->

    # Stale agents/codex.md (oversized placeholder)

    Same scenario as CLAUDE.md: stale, needs regeneration.
""") + "\n" + ("# padding line\n" * 150)


class UscAdminReproductionTest(unittest.TestCase):
    """End-to-end test reproducing the usc-admin regression.

    The test invokes _invoke_composer_with_gen_fallback against a real project
    directory to verify that:
    1. rc == 0 (stub composer succeeds, generator runs without error).
    2. CLAUDE.md is regenerated from AGENTS.md (content changes, smaller).
    3. agents/codex.md is similarly regenerated.
    4. The regenerated files contain the GENERATED FILE marker.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not REAL_GENERATOR.exists():
            raise unittest.SkipTest(
                f"Real generate_agent_configs.py not found at {REAL_GENERATOR}; "
                "cannot run usc-admin integration test."
            )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)

    def _setup_project(self) -> None:
        """Populate the test project directory with the usc-admin broken state."""
        # AGENTS.md: compact, authoritative content.
        (self.project / "AGENTS.md").write_text(
            _COMPACT_AGENTS_MD, encoding="utf-8"
        )

        # Stale CLAUDE.md (oversized, but has GENERATED marker so regeneration
        # is allowed).
        (self.project / "CLAUDE.md").write_text(
            _STALE_CLAUDE_MD, encoding="utf-8"
        )

        # Stale agents/codex.md.
        agents_dir = self.project / "agents"
        agents_dir.mkdir(exist_ok=True)
        (agents_dir / "codex.md").write_text(
            _STALE_CODEX_MD, encoding="utf-8"
        )

        # Set up .agent-config/repo/scripts/ with stub composer + real generator.
        repo_scripts = self.project / ".agent-config" / "repo" / "scripts"
        repo_scripts.mkdir(parents=True)

        # Stub compose_packs.py: exits 0 (simulates successful composition).
        (repo_scripts / "compose_packs.py").write_text(
            "import sys\nsys.exit(0)\n", encoding="utf-8"
        )

        # Real generate_agent_configs.py from source tree.
        shutil.copy(str(REAL_GENERATOR), str(repo_scripts / "generate_agent_configs.py"))

    def test_usc_admin_stale_generated_files_healed(self) -> None:
        """Core regression: after compose+generate, CLAUDE.md and agents/codex.md
        are regenerated from the compact AGENTS.md, replacing the stale versions.

        Before the v0.5.8 fix, _invoke_composer_with_gen_fallback only ran the
        composer but not the generator; stale CLAUDE.md / agents/codex.md remained.
        With the fix, the generator always runs after the composer (success or fail).
        """
        self._setup_project()

        stale_claude_size = len(_STALE_CLAUDE_MD.encode("utf-8"))
        stale_codex_size = len(_STALE_CODEX_MD.encode("utf-8"))

        # Patch _bundled_composer_path to None so _invoke_composer uses
        # the project-local stub rather than the installed wheel's real composer.
        with patch.object(cli, "_bundled_composer_path", return_value=None):
            rc = cli._invoke_composer_with_gen_fallback(self.project)

        self.assertEqual(rc, 0, "Stub composer must return 0.")

        claude_path = self.project / "CLAUDE.md"
        codex_path = self.project / "agents" / "codex.md"

        self.assertTrue(
            claude_path.exists(),
            "CLAUDE.md must exist after _invoke_composer_with_gen_fallback.",
        )
        self.assertTrue(
            codex_path.exists(),
            "agents/codex.md must exist after _invoke_composer_with_gen_fallback.",
        )

        new_claude = claude_path.read_text(encoding="utf-8")
        new_codex = codex_path.read_text(encoding="utf-8")

        # The regenerated files must contain the GENERATED FILE marker.
        self.assertIn(
            "GENERATED FILE",
            new_claude,
            "Regenerated CLAUDE.md must contain GENERATED FILE marker.",
        )
        self.assertIn(
            "GENERATED FILE",
            new_codex,
            "Regenerated agents/codex.md must contain GENERATED FILE marker.",
        )

        # Regenerated files must be smaller than the stale padding-bloated versions
        # because they were re-derived from the compact AGENTS.md.
        new_claude_size = len(new_claude.encode("utf-8"))
        new_codex_size = len(new_codex.encode("utf-8"))

        self.assertLess(
            new_claude_size, stale_claude_size,
            f"Regenerated CLAUDE.md ({new_claude_size}B) should be smaller than "
            f"the stale version ({stale_claude_size}B), indicating it was regenerated "
            "from the compact AGENTS.md rather than left as-is.",
        )
        self.assertLess(
            new_codex_size, stale_codex_size,
            f"Regenerated agents/codex.md ({new_codex_size}B) should be smaller than "
            f"the stale version ({stale_codex_size}B).",
        )

    def test_usc_admin_generator_runs_even_when_composer_fails(self) -> None:
        """Generator must run (and heal CLAUDE.md) even when composer exits non-zero.

        This verifies the v0.5.8 fallback behavior: if the composer aborts
        (DriftAbort, rc=10, etc.), the generated files must still be refreshed
        from the current AGENTS.md so the user's agent config remains consistent.
        """
        self._setup_project()

        # Replace stub with one that exits 1 (simulated DriftAbort-style failure).
        repo_scripts = self.project / ".agent-config" / "repo" / "scripts"
        (repo_scripts / "compose_packs.py").write_text(
            "import sys\nsys.exit(1)\n", encoding="utf-8"
        )

        stale_claude_content = _STALE_CLAUDE_MD

        # Patch _bundled_composer_path to None so project-local stub is used.
        with patch.object(cli, "_bundled_composer_path", return_value=None):
            rc = cli._invoke_composer_with_gen_fallback(self.project)

        self.assertEqual(rc, 1, "Composer rc must be preserved as 1.")

        claude_path = self.project / "CLAUDE.md"
        self.assertTrue(claude_path.exists())

        new_claude = claude_path.read_text(encoding="utf-8")

        # Generator still ran: content was regenerated (no longer the padding-bloated stale).
        self.assertNotEqual(
            new_claude, stale_claude_content,
            "CLAUDE.md must be regenerated by the generator even when composer fails.",
        )
        self.assertIn(
            "GENERATED FILE",
            new_claude,
            "Regenerated CLAUDE.md must contain GENERATED FILE marker.",
        )


class UpgraderHealTest(unittest.TestCase):
    """v0.5.8: Gap B integration — ``pack verify --fix --yes`` heals stale
    CLAUDE.md / agents/codex.md via the real generator on an upgrader's
    "deployed but stale" project.

    The canonical upgrade scenario: v0.5.7 user with a healthy pack-lock
    (all packs DEPLOYED) but a stale CLAUDE.md left over from a previous
    compose run.  After ``pipx install --force anywhere-agents==0.5.8``, the
    user runs ``pack verify --fix`` and expects the stale files to be healed.

    This test uses the real generate_agent_configs.py (no over-mocking of the
    generator) and a stub pack-lock that puts all packs in DEPLOYED state.
    The composer is stubbed to return 0 but does not actually write files;
    only the generator is real.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not REAL_GENERATOR.exists():
            raise unittest.SkipTest(
                f"Real generate_agent_configs.py not found at {REAL_GENERATOR}; "
                "cannot run UpgraderHealTest integration test."
            )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)

    def _setup_stuck_state(self) -> None:
        """Populate a project in 'deployed but stale' state.

        Both bundled defaults (aa-core-skills, agent-style) are placed in the
        pack-lock as DEPLOYED with matching output files on disk.  This means
        _pack_verify_fix reaches the 'nothing to repair' branch — pack-level
        health passes but CLAUDE.md / agents/codex.md are stale.  The Gap B
        fix must then call _run_generator_only to heal the stale files.
        """
        # Compact AGENTS.md.
        (self.project / "AGENTS.md").write_text(
            _COMPACT_AGENTS_MD, encoding="utf-8"
        )

        # Stale CLAUDE.md (has GENERATED marker so generator may overwrite).
        (self.project / "CLAUDE.md").write_text(
            _STALE_CLAUDE_MD, encoding="utf-8"
        )

        # Stale agents/codex.md.
        agents_dir = self.project / "agents"
        agents_dir.mkdir(exist_ok=True)
        (agents_dir / "codex.md").write_text(
            _STALE_CODEX_MD, encoding="utf-8"
        )

        # pack-lock.json — both bundled defaults DEPLOYED (aa-core-skills + agent-style).
        # This suppresses the MISSING-state path that would trigger the composer
        # via the existing deployable-state branch.
        output_core = ".claude/commands/aa-core-skills.md"
        output_style = ".claude/commands/agent-style.md"
        for rel in (output_core, output_style):
            out_abs = self.project / rel
            out_abs.parent.mkdir(parents=True, exist_ok=True)
            out_abs.write_text("# stub\n", encoding="utf-8")

        resolved = "a" * 40
        lock_data = {
            "version": 2,
            "packs": {
                "aa-core-skills": {
                    "source_url": "bundled:aa",
                    "requested_ref": "bundled",
                    "resolved_commit": resolved,
                    "latest_known_head": resolved,
                    "pack_update_policy": "locked",
                    "files": [{"output_paths": [output_core]}],
                },
                "agent-style": {
                    "source_url": "bundled:aa",
                    "requested_ref": "bundled",
                    "resolved_commit": resolved,
                    "latest_known_head": resolved,
                    "pack_update_policy": "locked",
                    "files": [{"output_paths": [output_style]}],
                },
            },
        }
        agent_dir = self.project / ".agent-config"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "pack-lock.json").write_text(
            __import__("json").dumps(lock_data), encoding="utf-8"
        )

        # agent-config.yaml: explicitly list both bundled defaults so the
        # project observations match the lock entries (no user-only / project-only gap).
        # Using rule_packs: [] would clear defaults causing DECLARED state.
        (self.project / "agent-config.yaml").write_text(
            "rule_packs:\n"
            "- {name: aa-core-skills, source: {url: bundled:aa, ref: bundled}}\n"
            "- {name: agent-style, source: {url: bundled:aa, ref: bundled}}\n",
            encoding="utf-8",
        )

        # Set up .agent-config/repo/scripts/ with stub composer + real generator.
        repo_scripts = self.project / ".agent-config" / "repo" / "scripts"
        repo_scripts.mkdir(parents=True, exist_ok=True)
        (repo_scripts / "compose_packs.py").write_text(
            "import sys\nsys.exit(0)\n", encoding="utf-8"
        )
        shutil.copy(
            str(REAL_GENERATOR),
            str(repo_scripts / "generate_agent_configs.py"),
        )

    def test_upgrader_heal_stale_claude_md_via_verify_fix(self) -> None:
        """End-to-end: _pack_verify_fix heals stale CLAUDE.md even when
        pack-lock reports DEPLOYED (nothing to repair at the pack level).

        Simulates: upgrader with healthy pack-lock but stale generated files.
        After the Gap B fix, _run_generator_only runs unconditionally after
        --fix logic completes (regardless of 'nothing to repair' verdict).
        """
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch
        from anywhere_agents.cli import _pack_verify_fix

        self._setup_stuck_state()

        stale_claude_size = len(_STALE_CLAUDE_MD.encode("utf-8"))
        stale_codex_size = len(_STALE_CODEX_MD.encode("utf-8"))

        class _Args:
            yes = True
            no_deploy = False

        # Patch _bundled_composer_path to None so the project-local stub is used,
        # but let _run_generator_only use the real generator via the project path.
        import os
        cwd_before = os.getcwd()
        try:
            os.chdir(self.project)
            with patch.object(cli, "_bundled_composer_path", return_value=None), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = _pack_verify_fix(None, self.project, _Args())
        finally:
            os.chdir(cwd_before)

        self.assertEqual(rc, 0, "_pack_verify_fix must return 0.")

        claude_path = self.project / "CLAUDE.md"
        codex_path = self.project / "agents" / "codex.md"

        new_claude = claude_path.read_text(encoding="utf-8")
        new_codex = codex_path.read_text(encoding="utf-8")

        self.assertIn(
            "GENERATED FILE", new_claude,
            "CLAUDE.md must be regenerated (must contain GENERATED FILE marker).",
        )
        self.assertIn(
            "GENERATED FILE", new_codex,
            "agents/codex.md must be regenerated.",
        )

        new_claude_size = len(new_claude.encode("utf-8"))
        new_codex_size = len(new_codex.encode("utf-8"))

        self.assertLess(
            new_claude_size, stale_claude_size,
            f"CLAUDE.md ({new_claude_size}B) must be smaller than stale ({stale_claude_size}B) "
            "— regenerated from compact AGENTS.md.",
        )
        self.assertLess(
            new_codex_size, stale_codex_size,
            f"agents/codex.md ({new_codex_size}B) must be smaller than stale ({stale_codex_size}B).",
        )


if __name__ == "__main__":
    unittest.main()
