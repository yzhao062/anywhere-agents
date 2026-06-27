"""Contract tests for prun's monitor.{sh,ps1}.

Validates the active stall/fail/done monitor in skills/prun/SKILL.md (issue #5).
The monitor takes dispatch state-dirs and COMPLETES on the first actionable event:
all units done (exit 0), any stall or fail (exit 3), or hard timeout (exit 2). A
unit is "failed" when its result carries the FALLBACK header (worker wrote no
result) or its dispatch PID is dead with no result; "stalled" when the tail shows
no growth for the threshold while the dispatch is still alive.

bash runs on Linux (Spark/CI); powershell on Windows. bash is skipped on Windows
because Git Bash does not resolve backslash Windows temp paths passed as args.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "prun" / "scripts"
MONITOR_SH = SCRIPTS_DIR / "monitor.sh"
MONITOR_PS1 = SCRIPTS_DIR / "monitor.ps1"

BASH = shutil.which("bash")
PS_SHELL = shutil.which("pwsh") or shutil.which("powershell")


def _temp_dir():
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()


def _dead_pid() -> int:
    """A PID that is guaranteed dead: spawn a trivial process, wait, reap it."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class _MonitorContractMixin:
    SHELL_KIND: str = ""

    def _make_state_dir(self, base, name, *, result=None, tail="x\n",
                        dispatch_pid=None, backdate=True) -> Path:
        sd = Path(base) / name
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "tail").write_text(tail, encoding="utf-8")
        rpath = Path(base) / f"{name}.result.md"
        (sd / "result-file").write_text(str(rpath) + "\n", encoding="utf-8")
        if result is not None:
            rpath.write_text(result, encoding="utf-8")
            if backdate:
                # Backdate so (now - mtime) >= stable window even at window=0.
                past = time.time() - 5
                os.utime(rpath, (past, past))
        if dispatch_pid is not None:
            (sd / "dispatch-pid").write_text(str(dispatch_pid) + "\n",
                                             encoding="utf-8")
        return sd

    def _run_monitor(self, state_dirs, threshold="1", poll="1", timeout_s="8",
                     window="0", proc_timeout=40.0):
        if self.SHELL_KIND == "bash":
            cmd = [BASH, str(MONITOR_SH)] + [str(s) for s in state_dirs]
        else:
            cmd = [PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(MONITOR_PS1)] + [str(s) for s in state_dirs]
        env = os.environ.copy()
        env["PRUN_STALL_THRESHOLD"] = threshold
        env["PRUN_MONITOR_POLL"] = poll
        env["PRUN_MONITOR_TIMEOUT"] = timeout_s
        env["PRUN_MONITOR_STABLE_WINDOW"] = window
        return subprocess.run(cmd, capture_output=True, text=True, env=env,
                              check=False, timeout=proc_timeout)

    # --- contract ---------------------------------------------------------

    def test_all_done_exits_zero(self) -> None:
        with _temp_dir() as td:
            a = self._make_state_dir(td, "u1", result="# u1 result\nok\n")
            b = self._make_state_dir(td, "u2", result="# u2 result\nok\n")
            r = self._run_monitor([a, b])
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            self.assertIn("MONITOR-EVENT all-done", r.stdout)
            self.assertEqual(r.stdout.count(" done"), 2, r.stdout)

    def test_fallback_result_is_failure(self) -> None:
        with _temp_dir() as td:
            a = self._make_state_dir(
                td, "u1",
                result="# u1 result (FALLBACK, worker wrote no result file)\n...\n")
            r = self._run_monitor([a])
            self.assertEqual(r.returncode, 3, f"{r.stdout}\n{r.stderr}")
            self.assertIn("MONITOR-EVENT fail", r.stdout)
            self.assertIn("failed(fallback)", r.stdout)

    def test_stall_detected(self) -> None:
        # Tail present but never grows, no result, no dispatch-pid -> stalled.
        with _temp_dir() as td:
            a = self._make_state_dir(td, "u1", tail="frozen output\n")
            r = self._run_monitor([a], threshold="1", poll="1")
            self.assertEqual(r.returncode, 3, f"{r.stdout}\n{r.stderr}")
            self.assertIn("MONITOR-EVENT stall", r.stdout)
            self.assertIn("stalled(", r.stdout)

    def test_dead_dispatch_is_failure(self) -> None:
        # No growth, no result, dispatch PID dead -> failed(dispatch-dead).
        with _temp_dir() as td:
            a = self._make_state_dir(td, "u1", tail="frozen\n",
                                     dispatch_pid=_dead_pid())
            r = self._run_monitor([a], threshold="1", poll="1")
            self.assertEqual(r.returncode, 3, f"{r.stdout}\n{r.stderr}")
            self.assertIn("MONITOR-EVENT fail", r.stdout)
            self.assertIn("failed(dispatch-dead)", r.stdout)

    def test_mixed_done_and_stall_reports_both(self) -> None:
        with _temp_dir() as td:
            a = self._make_state_dir(td, "u1", result="# u1 result\nok\n")
            b = self._make_state_dir(td, "u2", tail="frozen\n")
            r = self._run_monitor([a, b], threshold="1", poll="1")
            self.assertEqual(r.returncode, 3, f"{r.stdout}\n{r.stderr}")
            self.assertIn("MONITOR-EVENT stall", r.stdout)
            self.assertIn("u1 done", r.stdout)
            self.assertIn("stalled(", r.stdout)

    def test_no_args_exits_two(self) -> None:
        r = self._run_monitor([])
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_start_line_schema(self) -> None:
        with _temp_dir() as td:
            a = self._make_state_dir(td, "u1", result="ok\n")
            r = self._run_monitor([a])
            self.assertTrue(
                r.stdout.startswith("MONITOR-START units=1 stall-threshold="),
                f"first line schema:\n{r.stdout}")

    def test_fallback_only_matches_header_not_body(self) -> None:
        # Regression (review M2): a real result that merely mentions FALLBACK in its
        # body must be "done", not "failed(fallback)" -- only the line-1 header counts.
        with _temp_dir() as td:
            a = self._make_state_dir(
                td, "u1",
                result="# u1 result\nConclusion: I weighed the FALLBACK path and rejected it.\n")
            r = self._run_monitor([a])
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            self.assertIn("u1 done", r.stdout)
            self.assertNotIn("failed(fallback)", r.stdout)

    def test_fresh_result_with_dead_dispatch_becomes_done(self) -> None:
        # Regression (review M1): a present non-empty result plus a dead dispatcher
        # must stabilize to "done", never "failed(dispatch-dead)", even with a short
        # stall threshold below the stable window.
        with _temp_dir() as td:
            a = self._make_state_dir(td, "u1", result="# u1 result\nok\n",
                                     dispatch_pid=_dead_pid(), backdate=False)
            r = self._run_monitor([a], threshold="1", poll="1", window="3",
                                   timeout_s="20")
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            self.assertIn("u1 done", r.stdout)
            self.assertNotIn("failed(dispatch-dead)", r.stdout)

    def test_timeout_path(self) -> None:
        # Review Low: a unit that never stalls (high threshold) and never finishes
        # (no result) exits via the timeout path with code 2.
        with _temp_dir() as td:
            a = self._make_state_dir(td, "u1", tail="frozen\n")  # no result
            r = self._run_monitor([a], threshold="999", poll="1", timeout_s="3")
            self.assertEqual(r.returncode, 2, f"{r.stdout}\n{r.stderr}")
            self.assertIn("MONITOR-EVENT timeout", r.stdout)


@unittest.skipIf(
    sys.platform.startswith("win"),
    "bash skipped on Windows: Git Bash does not resolve backslash Windows temp "
    "paths passed as args. CI Linux + Spark cover this lane.",
)
@unittest.skipUnless(BASH, "bash not on PATH")
class MonitorBashTests(_MonitorContractMixin, unittest.TestCase):
    SHELL_KIND = "bash"


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win"),
    "PowerShell monitor tests are Windows-only.",
)
class MonitorPowerShellTests(_MonitorContractMixin, unittest.TestCase):
    SHELL_KIND = "powershell"


class MonitorScriptsTracked(unittest.TestCase):
    def test_sh_exists(self) -> None:
        self.assertTrue(MONITOR_SH.exists(), f"missing: {MONITOR_SH}")

    def test_ps1_exists(self) -> None:
        self.assertTrue(MONITOR_PS1.exists(), f"missing: {MONITOR_PS1}")


if __name__ == "__main__":
    unittest.main()
