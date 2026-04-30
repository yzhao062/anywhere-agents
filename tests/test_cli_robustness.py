"""Tests for v0.5.8 Item 2: generator fallback on compose abort.

Covers:
- composer success → behavior unchanged (generator still runs; no recovery msg)
- composer failure (non-zero rc) → generator still runs; rc preserved; recovery
  message printed to stderr
- generator error does not suppress the composer's original rc
- bootstrap.ps1 rc preservation (BootstrapScriptRcPreservationTests)
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch, call, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "pypi"))
sys.path.insert(0, str(ROOT / "scripts"))

from anywhere_agents import cli  # noqa: E402


def _make_project(tmp: str) -> Path:
    """Return a minimal bootstrapped project directory."""
    project = Path(tmp)
    # Minimal .agent-config/repo/scripts/compose_packs.py presence
    repo_scripts = project / ".agent-config" / "repo" / "scripts"
    repo_scripts.mkdir(parents=True)
    (repo_scripts / "compose_packs.py").write_text("# stub")
    (repo_scripts / "generate_agent_configs.py").write_text("# stub")
    return project


class TestInvokeComposerWithGenFallback(unittest.TestCase):
    """Unit tests for _invoke_composer_with_gen_fallback."""

    def test_composer_success_generator_runs(self) -> None:
        """rc=0 path: generator still runs; returns 0."""
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(tmp)
            gen_calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                m = MagicMock()
                if "compose_packs" in " ".join(cmd):
                    m.returncode = 0
                elif "generate_agent_configs" in " ".join(cmd):
                    gen_calls.append(cmd)
                    m.returncode = 0
                else:
                    m.returncode = 0
                return m

            with patch("subprocess.run", side_effect=fake_run):
                rc = cli._invoke_composer_with_gen_fallback(project)

        self.assertEqual(rc, 0)
        self.assertEqual(len(gen_calls), 1, "generator should run exactly once on success")

    def test_composer_failure_generator_still_runs(self) -> None:
        """rc!=0 path: generator still runs; original rc preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(tmp)
            gen_calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                m = MagicMock()
                if "compose_packs" in " ".join(cmd):
                    m.returncode = 1
                elif "generate_agent_configs" in " ".join(cmd):
                    gen_calls.append(cmd)
                    m.returncode = 0
                else:
                    m.returncode = 0
                return m

            err_buf = io.StringIO()
            with patch("subprocess.run", side_effect=fake_run):
                with redirect_stderr(err_buf):
                    rc = cli._invoke_composer_with_gen_fallback(project)

        self.assertEqual(rc, 1, "composer rc must be preserved")
        self.assertEqual(len(gen_calls), 1, "generator must run even after composer failure")
        err_out = err_buf.getvalue()
        self.assertIn("pack composition did not complete", err_out,
                      "recovery message should appear on stderr")

    def test_composer_failure_rc_preserved_over_generator_success(self) -> None:
        """Generator success must not convert composer rc=2 into rc=0."""
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(tmp)

            def fake_run(cmd, **kwargs):
                m = MagicMock()
                if "compose_packs" in " ".join(cmd):
                    m.returncode = 2
                else:
                    m.returncode = 0
                return m

            with patch("subprocess.run", side_effect=fake_run):
                rc = cli._invoke_composer_with_gen_fallback(project)

        self.assertEqual(rc, 2)

    def test_generator_failure_does_not_mask_composer_success(self) -> None:
        """If generator fails but composer succeeded, return 0 (composer rc)."""
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(tmp)

            def fake_run(cmd, **kwargs):
                m = MagicMock()
                if "compose_packs" in " ".join(cmd):
                    m.returncode = 0
                else:
                    # generator fails
                    m.returncode = 3
                return m

            with patch("subprocess.run", side_effect=fake_run):
                rc = cli._invoke_composer_with_gen_fallback(project)

        self.assertEqual(rc, 0)

    def test_no_generator_script_does_not_crash(self) -> None:
        """If generate_agent_configs.py is absent, fallback returns composer rc silently."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            # Only compose_packs.py present, no generator
            repo_scripts = project / ".agent-config" / "repo" / "scripts"
            repo_scripts.mkdir(parents=True)
            (repo_scripts / "compose_packs.py").write_text("# stub")

            def fake_run(cmd, **kwargs):
                m = MagicMock()
                m.returncode = 0
                return m

            with patch("subprocess.run", side_effect=fake_run):
                rc = cli._invoke_composer_with_gen_fallback(project)

        self.assertEqual(rc, 0)

    def test_pack_add_callsite_uses_gen_fallback(self) -> None:
        """_pack_add_v0_5 in-project path calls _invoke_composer_with_gen_fallback, not _invoke_composer."""
        import inspect
        src = inspect.getsource(cli._pack_add_v0_5)
        # The step-4c composer call must now go through the fallback wrapper.
        self.assertIn("_invoke_composer_with_gen_fallback", src,
                      "_pack_add_v0_5 must use _invoke_composer_with_gen_fallback")

    def test_pack_update_callsite_uses_gen_fallback(self) -> None:
        """_pack_update_v0_5 calls _invoke_composer_with_gen_fallback."""
        import inspect
        src = inspect.getsource(cli._pack_update)
        self.assertIn("_invoke_composer_with_gen_fallback", src,
                      "_pack_update must use _invoke_composer_with_gen_fallback")


class BootstrapScriptRcPreservationTests(unittest.TestCase):
    """Script-level tests for bootstrap.ps1 exit-code preservation.

    Verifies that when the composer exits with a non-1 exit code (e.g. 7),
    bootstrap.ps1 preserves that exact code as the script's own exit code,
    AND still runs the generator script before exiting.

    The tests build a minimal project scaffold:
    - .agent-config/repo/ initialised as a local git repo so bootstrap.ps1
      skips the remote clone path.
    - Stub compose_packs.py (exits with a configurable code).
    - Stub generate_agent_configs.py (writes a sentinel file when run).
    - AGENTS.md pre-populated under .agent-config/ so the Invoke-WebRequest
      path is bypassed via a test-local HTTP-free stub mechanism.

    Each test is skipped gracefully when pwsh/powershell is not on PATH.
    """

    @classmethod
    def _find_powershell(cls) -> str | None:
        """Return the first usable PowerShell executable name, or None."""
        for exe in ("pwsh", "powershell"):
            if shutil.which(exe):
                return exe
        return None

    def _make_bootstrap_project(
        self,
        tmp: str,
        composer_rc: int,
    ) -> tuple[Path, Path]:
        """Set up a minimal project directory for a bootstrap.ps1 test.

        Returns (project_root, generator_sentinel_path) where
        generator_sentinel_path is the file the stub generator creates when run.
        """
        root = Path(tmp)

        # Pre-create .agent-config/AGENTS.md so bootstrap.ps1's
        # Invoke-WebRequest path does NOT fail with a network call.
        # We override by pre-writing the file and patching the web request via
        # a stub AGENTS.md — bootstrap.ps1 overwrites .agent-config/AGENTS.md
        # with the downloaded copy, but only if it can reach the network.
        # Instead, we use $env:AGENT_CONFIG_UPSTREAM set to a non-existent
        # host to make Invoke-WebRequest fail silently.  The script continues
        # because it does not `-ErrorAction Stop` on the web request, and the
        # file we pre-write survives as-is (the failed request leaves old content).
        # Pre-populate the AGENTS.md file before the web request could run:
        (root / ".agent-config").mkdir(parents=True, exist_ok=True)
        (root / ".agent-config" / "AGENTS.md").write_text(
            "# Minimal test AGENTS.md\n", encoding="utf-8"
        )

        # Initialise .agent-config/repo/ as a local git repo so the
        # `git -C .agent-config/repo pull --ff-only` path is taken instead
        # of the network clone path.
        repo_dir = root / ".agent-config" / "repo"
        repo_scripts = repo_dir / "scripts"
        repo_scripts.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            ["git", "-C", str(repo_dir), "init", "--quiet"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "commit", "--allow-empty",
             "--quiet", "-m", "init"],
            check=True, capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "sparse-checkout", "init", "--cone"],
            check=True, capture_output=True,
        )

        # Stub composer: exits with composer_rc.
        (repo_scripts / "compose_packs.py").write_text(
            textwrap.dedent(f"""\
                import sys
                sys.exit({composer_rc})
            """),
            encoding="utf-8",
        )

        # Stub generator: writes a sentinel file so we can verify it ran.
        sentinel = root / ".generator_ran"
        (repo_scripts / "generate_agent_configs.py").write_text(
            textwrap.dedent(f"""\
                from pathlib import Path
                Path({str(sentinel)!r}).write_text("ran", encoding="utf-8")
            """),
            encoding="utf-8",
        )

        return root, sentinel

    def test_ps1_preserves_non1_composer_exit_code(self) -> None:
        """bootstrap.ps1 exits with the composer's exact rc (7), not 1."""
        ps = self._find_powershell()
        if ps is None:
            self.skipTest("PowerShell not available on PATH")

        bootstrap_ps1 = ROOT / "bootstrap" / "bootstrap.ps1"
        if not bootstrap_ps1.exists():
            self.skipTest(f"bootstrap.ps1 not found at {bootstrap_ps1}")

        with tempfile.TemporaryDirectory() as tmp:
            root, sentinel = self._make_bootstrap_project(tmp, composer_rc=7)

            result = subprocess.run(
                [
                    ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(bootstrap_ps1),
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    # Point to a non-existent upstream so Invoke-WebRequest fails
                    # fast (and leaves our pre-written AGENTS.md intact).
                    "AGENT_CONFIG_UPSTREAM": "localhost-invalid/nonexistent",
                    # Use the current Python so Test-PythonRuns passes.
                    "ANYWHERE_AGENTS_PYTHON": sys.executable,
                    # Suppress pip noise.
                    "PIP_QUIET": "1",
                },
                timeout=60,
            )

            self.assertEqual(
                result.returncode, 7,
                f"bootstrap.ps1 must preserve composer rc=7, got {result.returncode}.\n"
                f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}",
            )

    def test_ps1_generator_runs_after_composer_failure(self) -> None:
        """bootstrap.ps1 runs the generator even when composer exits non-zero."""
        ps = self._find_powershell()
        if ps is None:
            self.skipTest("PowerShell not available on PATH")

        bootstrap_ps1 = ROOT / "bootstrap" / "bootstrap.ps1"
        if not bootstrap_ps1.exists():
            self.skipTest(f"bootstrap.ps1 not found at {bootstrap_ps1}")

        with tempfile.TemporaryDirectory() as tmp:
            root, sentinel = self._make_bootstrap_project(tmp, composer_rc=7)

            subprocess.run(
                [
                    ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(bootstrap_ps1),
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "AGENT_CONFIG_UPSTREAM": "localhost-invalid/nonexistent",
                    "ANYWHERE_AGENTS_PYTHON": sys.executable,
                    "PIP_QUIET": "1",
                },
                timeout=60,
            )

            self.assertTrue(
                sentinel.exists(),
                f"Generator sentinel file must exist; generator must have run.\n"
                f"Sentinel path: {sentinel}",
            )

    def test_ps1_composer_success_exits_0(self) -> None:
        """bootstrap.ps1 exits 0 when composer succeeds (rc=0)."""
        ps = self._find_powershell()
        if ps is None:
            self.skipTest("PowerShell not available on PATH")

        bootstrap_ps1 = ROOT / "bootstrap" / "bootstrap.ps1"
        if not bootstrap_ps1.exists():
            self.skipTest(f"bootstrap.ps1 not found at {bootstrap_ps1}")

        with tempfile.TemporaryDirectory() as tmp:
            root, _sentinel = self._make_bootstrap_project(tmp, composer_rc=0)

            result = subprocess.run(
                [
                    ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(bootstrap_ps1),
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "AGENT_CONFIG_UPSTREAM": "localhost-invalid/nonexistent",
                    "ANYWHERE_AGENTS_PYTHON": sys.executable,
                    "PIP_QUIET": "1",
                },
                timeout=60,
            )

            self.assertEqual(
                result.returncode, 0,
                f"bootstrap.ps1 must exit 0 when composer succeeds.\n"
                f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}",
            )


class BootstrapMainFallThroughTests(unittest.TestCase):
    """v0.5.8: Gap A — _bootstrap_main falls through to wheel-side recovery
    when bootstrap.sh exits non-zero.

    The pre-fix behavior was: rc != 0 → early return, never reaching the
    post-bootstrap reconcile (pack verify --fix --yes).  The fix makes the
    reconcile always run.
    """

    # Minimal project scaffold used across tests.
    def _make_bootstrap_project(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / ".agent-config").mkdir(parents=True, exist_ok=True)
        return root

    def test_bootstrap_failure_falls_through_to_wheel_recovery(self) -> None:
        """Gap A: bootstrap.sh rc=1 → reconcile runs → rc=0 (recovery succeeded).

        Mock: choose_script returns a fake script name; urllib retrieves OK;
        subprocess returns rc=1 for bootstrap.sh; main(pack verify --fix --yes)
        returns 0 (recovery succeeded).  _bootstrap_main must return 0 and log
        a recovery message.

        The evidence check requires .agent-config/repo/scripts/compose_packs.py
        to exist before crediting recovery, so we create it here to simulate a
        successful clone.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_bootstrap_project(tmp)
            # Create the clone sentinel so the evidence check credits recovery.
            sentinel = root / ".agent-config" / "repo" / "scripts" / "compose_packs.py"
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("# stub\n")
            log_lines: list[str] = []

            with patch("anywhere_agents.cli.choose_script", return_value=("bootstrap.sh", ["bash"])), \
                 patch("anywhere_agents.cli.bootstrap_url", return_value="http://fake/bootstrap.sh"), \
                 patch("urllib.request.urlretrieve"), \
                 patch("subprocess.run", return_value=MagicMock(returncode=1)), \
                 patch("anywhere_agents.cli.main", return_value=0) as mock_main, \
                 patch("anywhere_agents.cli.log", side_effect=lambda msg: log_lines.append(msg)), \
                 patch("pathlib.Path.cwd", return_value=root):
                rc = cli._bootstrap_main([])

            self.assertEqual(rc, 0, "_bootstrap_main must return 0 when wheel-side recovery succeeds.")
            mock_main.assert_called_once()
            call_args = mock_main.call_args[0][0]
            self.assertIn("pack", call_args)
            self.assertIn("verify", call_args)
            self.assertIn("--fix", call_args)
            # Recovery message must be logged.
            recovery_logged = any(
                "recovery" in line.lower() or "wheel" in line.lower()
                for line in log_lines
            )
            self.assertTrue(
                recovery_logged,
                f"Expected a wheel-recovery log line; got: {log_lines}",
            )

    def test_bootstrap_failure_with_recovery_failure_preserves_bootstrap_rc(self) -> None:
        """Gap A: bootstrap.sh rc=1, reconcile rc=2 → _bootstrap_main returns 1.

        The original bootstrap failure category (rc=1) is preserved, not the
        reconcile's rc (2).
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_bootstrap_project(tmp)
            log_lines: list[str] = []

            with patch("anywhere_agents.cli.choose_script", return_value=("bootstrap.sh", ["bash"])), \
                 patch("anywhere_agents.cli.bootstrap_url", return_value="http://fake/bootstrap.sh"), \
                 patch("urllib.request.urlretrieve"), \
                 patch("subprocess.run", return_value=MagicMock(returncode=1)), \
                 patch("anywhere_agents.cli.main", return_value=2), \
                 patch("anywhere_agents.cli.log", side_effect=lambda msg: log_lines.append(msg)), \
                 patch("pathlib.Path.cwd", return_value=root):
                rc = cli._bootstrap_main([])

            self.assertEqual(
                rc, 1,
                "_bootstrap_main must return the bootstrap rc (1) when both bootstrap and "
                f"recovery fail, not the recovery rc (2). Got rc={rc}.",
            )
            # Both failures must be logged.
            all_log = "\n".join(log_lines)
            self.assertIn("1", all_log, "Bootstrap rc=1 must appear in log output.")

    def test_bootstrap_success_runs_reconcile_as_today(self) -> None:
        """Gap A: bootstrap.sh rc=0 → reconcile still runs (existing behavior preserved)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_bootstrap_project(tmp)

            with patch("anywhere_agents.cli.choose_script", return_value=("bootstrap.sh", ["bash"])), \
                 patch("anywhere_agents.cli.bootstrap_url", return_value="http://fake/bootstrap.sh"), \
                 patch("urllib.request.urlretrieve"), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0)), \
                 patch("anywhere_agents.cli.main", return_value=0) as mock_main, \
                 patch("anywhere_agents.cli.log"), \
                 patch("pathlib.Path.cwd", return_value=root):
                rc = cli._bootstrap_main([])

            self.assertEqual(rc, 0)
            mock_main.assert_called_once()

    def test_bootstrap_success_with_reconcile_failure_preserves_reconcile_rc(self) -> None:
        """Gap A rc-matrix: bootstrap.sh rc=0, reconcile rc=2 → returns 2.

        When bootstrap succeeds but the post-bootstrap reconcile fails, the
        reconcile rc is returned with a warning.  This is the existing semantics
        (bootstrap_rc == 0 path), and this test pins the one previously untested
        quadrant of the rc matrix.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_bootstrap_project(tmp)
            log_lines: list[str] = []

            with patch("anywhere_agents.cli.choose_script", return_value=("bootstrap.sh", ["bash"])), \
                 patch("anywhere_agents.cli.bootstrap_url", return_value="http://fake/bootstrap.sh"), \
                 patch("urllib.request.urlretrieve"), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0)), \
                 patch("anywhere_agents.cli.main", return_value=2), \
                 patch("anywhere_agents.cli.log", side_effect=lambda msg: log_lines.append(msg)), \
                 patch("pathlib.Path.cwd", return_value=root):
                rc = cli._bootstrap_main([])

            self.assertEqual(
                rc, 2,
                f"Must return reconcile rc=2 when bootstrap succeeds but reconcile fails. Got rc={rc}.",
            )
            # A warning about the reconcile failure must be logged.
            warning_logged = any(
                "reconcile" in line.lower() or "verify" in line.lower()
                for line in log_lines
            )
            self.assertTrue(
                warning_logged,
                f"Expected a reconcile-failure warning in logs; got: {log_lines}",
            )

    def test_bootstrap_failure_with_no_clone_preserves_bootstrap_rc(self) -> None:
        """Regression: bootstrap rc=7, rule_packs=[], no user config, no clone.

        When the bootstrap subprocess exits non-zero and the project has no
        .agent-config/repo/ (reconcile returns 0 from the "nothing to repair"
        branch), _bootstrap_main must return the original bootstrap rc (7),
        not 0.  Crediting "recovery" without a project clone is a false success.

        Scenario: valid opt-out project (rule_packs: []), no user config, no
        .agent-config/repo/.  Mock bootstrap subprocess rc=7; reconcile returns 0
        (nothing to repair).  Expected: _bootstrap_main returns 7.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_bootstrap_project(tmp)
            # No .agent-config/repo/ created — simulates a failed clone.
            log_lines: list[str] = []

            with patch("anywhere_agents.cli.choose_script", return_value=("bootstrap.sh", ["bash"])), \
                 patch("anywhere_agents.cli.bootstrap_url", return_value="http://fake/bootstrap.sh"), \
                 patch("urllib.request.urlretrieve"), \
                 patch("subprocess.run", return_value=MagicMock(returncode=7)), \
                 patch("anywhere_agents.cli.main", return_value=0), \
                 patch("anywhere_agents.cli.log", side_effect=lambda msg: log_lines.append(msg)), \
                 patch("pathlib.Path.cwd", return_value=root):
                rc = cli._bootstrap_main([])

            self.assertEqual(
                rc, 7,
                f"_bootstrap_main must preserve bootstrap rc=7 when no project clone exists. Got rc={rc}.\n"
                f"Logs: {log_lines}",
            )
            # The log must explain that no clone was found.
            clone_absent_logged = any(
                "clone" in line.lower() or "absent" in line.lower() or "preserving" in line.lower()
                for line in log_lines
            )
            self.assertTrue(
                clone_absent_logged,
                f"Expected a 'no clone / preserving rc' log line; got: {log_lines}",
            )


if __name__ == "__main__":
    unittest.main()
