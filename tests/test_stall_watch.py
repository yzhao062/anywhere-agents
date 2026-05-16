"""Tests for stall-watch.{sh,ps1} -- background stall observer.

Validates the contract in skills/implement-review/SKILL.md > Script contract
invariants and Phase 2.0 Health check 9. Uses STALL_THRESHOLD_SECONDS=2
and STALL_POLL_INTERVAL_SECONDS=1 so tests complete in seconds.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "implement-review" / "scripts"
STALL_SH = SCRIPTS_DIR / "stall-watch.sh"
STALL_PS1 = SCRIPTS_DIR / "stall-watch.ps1"

BASH = shutil.which("bash")
PS_SHELL = shutil.which("pwsh") or shutil.which("powershell")


def _spawn_long_running() -> subprocess.Popen:
    """Spawn a Python process that sleeps for 120s (test cleans it up)."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _safe_kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


class _StallContractMixin:
    SHELL_KIND: str = ""

    def _build_cmd(self, state_dir: Path, parent_pid: int) -> list[str]:
        if self.SHELL_KIND == "bash":
            return [
                BASH, str(STALL_SH),
                "--state-dir", str(state_dir),
                "--parent-pid", str(parent_pid),
            ]
        if self.SHELL_KIND == "powershell":
            return [
                PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(STALL_PS1),
                "--state-dir", str(state_dir),
                "--parent-pid", str(parent_pid),
            ]
        raise AssertionError(f"unknown SHELL_KIND: {self.SHELL_KIND!r}")

    def _spawn_watch(
        self,
        state_dir: Path,
        parent_pid: int,
        threshold: int = 2,
        interval: int = 1,
    ) -> subprocess.Popen:
        import os
        env = os.environ.copy()
        env["STALL_THRESHOLD_SECONDS"] = str(threshold)
        env["STALL_POLL_INTERVAL_SECONDS"] = str(interval)
        return subprocess.Popen(
            self._build_cmd(state_dir, parent_pid),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    # --- contract assertions ---------------------------------------------

    def test_stall_logged_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            (state_dir / "tail").write_text("initial\n", encoding="utf-8")

            parent = _spawn_long_running()
            watch = None
            try:
                watch = self._spawn_watch(state_dir, parent.pid,
                                          threshold=2, interval=1)
                time.sleep(5)
                warn_file = state_dir / "stall-warning"
                self.assertTrue(
                    warn_file.exists(),
                    "stall-warning file should be created after 2s threshold elapses",
                )
                content = warn_file.read_text(encoding="utf-8")
                self.assertRegex(
                    content,
                    r"STALL \S+ tail-no-growth-for-\d+s",
                    f"unexpected stall-warning content: {content!r}",
                )
            finally:
                _safe_kill(watch)
                _safe_kill(parent)

    def test_exits_when_parent_dies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            (state_dir / "tail").write_text("x\n", encoding="utf-8")

            parent = _spawn_long_running()
            watch = None
            try:
                watch = self._spawn_watch(state_dir, parent.pid,
                                          threshold=999, interval=1)
                # Make sure stall-watch is up and polling
                time.sleep(2)
                self.assertIsNone(watch.poll(),
                                  "stall-watch should still be running")

                # Kill parent; stall-watch must notice and exit
                parent.kill()
                parent.wait(timeout=5)

                try:
                    watch.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.fail(
                        "stall-watch did not exit within 10s after parent died"
                    )
                self.assertEqual(
                    watch.returncode, 0,
                    "stall-watch must exit 0 silently when parent dies",
                )
            finally:
                _safe_kill(watch)
                _safe_kill(parent)

    def test_never_kills_other_processes(self) -> None:
        """stall-watch must never kill any process under any circumstance."""
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            (state_dir / "tail").write_text("x\n", encoding="utf-8")

            parent = _spawn_long_running()
            codex_mimic = _spawn_long_running()
            watch = None
            try:
                watch = self._spawn_watch(state_dir, parent.pid,
                                          threshold=2, interval=1)
                # Run through several stall windows so it has many opportunities
                time.sleep(6)
                self.assertIsNone(
                    parent.poll(),
                    "parent must still be alive -- stall-watch must NEVER kill its parent",
                )
                self.assertIsNone(
                    codex_mimic.poll(),
                    "codex-mimic must still be alive -- stall-watch must NEVER kill any process",
                )
            finally:
                _safe_kill(watch)
                _safe_kill(parent)
                _safe_kill(codex_mimic)

    def test_growth_resets_stall_period_and_relogs(self) -> None:
        """A second stall period after a growth burst is logged separately."""
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            tail = state_dir / "tail"
            tail.write_text("initial\n", encoding="utf-8")

            parent = _spawn_long_running()
            watch = None
            try:
                watch = self._spawn_watch(state_dir, parent.pid,
                                          threshold=2, interval=1)
                # First stall window
                time.sleep(4)
                warn_file = state_dir / "stall-warning"
                self.assertTrue(warn_file.exists(),
                                "first stall should be logged after 2s threshold")
                first_count = warn_file.read_text(encoding="utf-8").count("STALL")
                self.assertGreaterEqual(first_count, 1)

                # Grow the tail; stall-watch must observe and reset
                tail.write_text("initial\nmore content here\n", encoding="utf-8")
                # Give stall-watch time to observe growth then re-stall
                time.sleep(6)
                second_count = warn_file.read_text(encoding="utf-8").count("STALL")
                self.assertGreater(
                    second_count, first_count,
                    "second stall period after growth burst should add a new STALL line",
                )
            finally:
                _safe_kill(watch)
                _safe_kill(parent)

    def test_missing_args_exit_silently(self) -> None:
        if self.SHELL_KIND == "bash":
            cmd = [BASH, str(STALL_SH)]
        else:
            cmd = [PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(STALL_PS1)]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=10
        )
        self.assertEqual(
            result.returncode, 0,
            f"missing args must exit 0 silently\nSTDOUT:{result.stdout}\n"
            f"STDERR:{result.stderr}",
        )

    def test_nonexistent_state_dir_exits_silently(self) -> None:
        parent = _spawn_long_running()
        try:
            cmd = self._build_cmd(Path("nonexistent-dir-xyz-12345"), parent.pid)
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=10
            )
            self.assertEqual(
                result.returncode, 0,
                "nonexistent state-dir must exit 0 silently",
            )
        finally:
            _safe_kill(parent)


@unittest.skipIf(
    sys.platform.startswith("win"),
    "bash skipped on Windows: Git Bash POSIX-translates paths. "
    "CI Linux covers this lane.",
)
@unittest.skipUnless(BASH, "bash not on PATH")
class StallWatchBashTests(_StallContractMixin, unittest.TestCase):
    SHELL_KIND = "bash"


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win"),
    "PowerShell stall-watch tests are Windows-only: stall-watch.ps1 is "
    "invoked by dispatch-codex.ps1 which uses Windows-only Start-Process "
    "options. Linux/macOS users run stall-watch.sh instead.",
)
class StallWatchPowerShellTests(_StallContractMixin, unittest.TestCase):
    SHELL_KIND = "powershell"


class StallScriptsTracked(unittest.TestCase):
    def test_sh_exists(self) -> None:
        self.assertTrue(STALL_SH.exists(),
                        f"stall-watch.sh missing: {STALL_SH}")

    def test_ps1_exists(self) -> None:
        self.assertTrue(STALL_PS1.exists(),
                        f"stall-watch.ps1 missing: {STALL_PS1}")


class StallSafetyInvariants(unittest.TestCase):
    """Source-level checks that stall-watch never kills any process.

    The SKILL.md contract is absolute: 'stall-watch must never kill codex
    exec under any circumstance.' Verify this at the source level so a
    later edit can't silently break the invariant.
    """

    def test_sh_kill_invocations_are_signal_zero_only(self) -> None:
        body = STALL_SH.read_text(encoding="utf-8")
        for line_num, line in enumerate(body.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for match in re.finditer(r"\bkill\b\s+(\S+)", line):
                first_arg = match.group(1).strip("\"'")
                if first_arg != "-0":
                    self.fail(
                        f"stall-watch.sh line {line_num}: "
                        f"kill invocation must use -0 (liveness only), "
                        f"got: {line.strip()!r}"
                    )
        for forbidden in ("pkill", "killall"):
            self.assertNotIn(
                forbidden, body,
                f"stall-watch.sh must not contain {forbidden!r}",
            )

    def test_ps1_has_no_process_killing_calls(self) -> None:
        body = STALL_PS1.read_text(encoding="utf-8")
        for forbidden in (
            "Stop-Process",
            ".Kill(",
            "TerminateProcess",
            "taskkill",
        ):
            self.assertNotIn(
                forbidden, body,
                f"stall-watch.ps1 must not contain {forbidden!r} "
                f"(never kills processes)",
            )


if __name__ == "__main__":
    unittest.main()
