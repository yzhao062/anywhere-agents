"""Contract tests for prun's gather.{sh,ps1}.

Validates the result-collector contract in skills/prun/SKILL.md. Key regression:
gather must fire on a result file that already exists + is non-empty + stable
when it starts (a unit that finished BEFORE gather began) -- the prun fix for
the auto-watch startup-snapshot race. Also: empty files are ignored, missing
files time out (exit 2), and the schema is GATHER-START / DONE / TIMEOUT.

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
GATHER_SH = SCRIPTS_DIR / "gather.sh"
GATHER_PS1 = SCRIPTS_DIR / "gather.ps1"

BASH = shutil.which("bash")
PS_SHELL = shutil.which("pwsh") or shutil.which("powershell")


def _temp_dir():
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()


class _GatherContractMixin:
    SHELL_KIND: str = ""

    def _run_gather(self, files, timeout_s="6", poll="1", window="0",
                    proc_timeout=30.0):
        if self.SHELL_KIND == "bash":
            cmd = [BASH, str(GATHER_SH)] + [str(f) for f in files]
        else:
            cmd = [PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(GATHER_PS1)] + [str(f) for f in files]
        env = os.environ.copy()
        env["AGENT_CONFIG_GATHER_TIMEOUT"] = timeout_s
        env["PRUN_GATHER_POLL"] = poll
        env["PRUN_GATHER_STABLE_WINDOW"] = window
        return subprocess.run(cmd, capture_output=True, text=True, env=env,
                              check=False, timeout=proc_timeout)

    def test_fires_on_preexisting_complete_file(self) -> None:
        # B1 regression: file exists with content before gather starts.
        with _temp_dir() as td:
            f = Path(td) / "r.md"
            f.write_text("# result\nConclusion: done\n", encoding="utf-8")
            r = self._run_gather([f])
            self.assertEqual(r.returncode, 0,
                             f"must collect a pre-existing complete file\n{r.stdout}\n{r.stderr}")
            self.assertIn("DONE", r.stdout)

    def test_multiple_files_all_fire(self) -> None:
        with _temp_dir() as td:
            f1 = Path(td) / "a.md"; f1.write_text("a\n", encoding="utf-8")
            f2 = Path(td) / "b.md"; f2.write_text("b\n", encoding="utf-8")
            r = self._run_gather([f1, f2])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.count("DONE"), 2,
                             f"both files must report DONE:\n{r.stdout}")

    def test_empty_file_is_not_landed(self) -> None:
        # Non-empty requirement: an empty (touched) file must not fire -> timeout.
        with _temp_dir() as td:
            f = Path(td) / "empty.md"
            f.write_text("", encoding="utf-8")
            r = self._run_gather([f], timeout_s="3")
            self.assertEqual(r.returncode, 2, f"empty file must not land:\n{r.stdout}")
            self.assertIn("TIMEOUT", r.stdout)

    def test_missing_file_times_out(self) -> None:
        with _temp_dir() as td:
            f = Path(td) / "never.md"
            r = self._run_gather([f], timeout_s="3")
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn("TIMEOUT remaining=1", r.stdout)

    def test_no_args_exits_two(self) -> None:
        r = self._run_gather([])
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_quiet_window_is_honored(self) -> None:
        # With a nonzero stable window, gather must WAIT the window before firing
        # rather than fire instantly (as window=0 would). This exercises the real
        # quiet-window code path that the other tests (window=0) skip, so a
        # regression that ignored the window is caught. Deterministic: the file is
        # written once (fixed mtime), no mid-window-write race.
        with _temp_dir() as td:
            f = Path(td) / "r.md"
            f.write_text("body\n", encoding="utf-8")
            start = time.monotonic()
            r = self._run_gather([f], timeout_s="30", poll="1", window="4",
                                 proc_timeout=40)
            elapsed = time.monotonic() - start
            self.assertEqual(r.returncode, 0, f"{r.stdout}\n{r.stderr}")
            self.assertIn("DONE", r.stdout)
            # window=4 -> DONE only after the file has been quiet ~4s; window=0
            # would have fired in well under 1s.
            self.assertGreaterEqual(
                elapsed, 3.0,
                f"gather fired in {elapsed:.1f}s; the quiet window was not honored",
            )

    def test_start_line_schema(self) -> None:
        with _temp_dir() as td:
            f = Path(td) / "r.md"; f.write_text("x\n", encoding="utf-8")
            r = self._run_gather([f])
            self.assertTrue(r.stdout.startswith("GATHER-START count=1 timeout="),
                            f"first line schema:\n{r.stdout}")


@unittest.skipIf(
    sys.platform.startswith("win"),
    "bash skipped on Windows: Git Bash does not resolve backslash Windows temp "
    "paths passed as args. CI Linux + Spark cover this lane.",
)
@unittest.skipUnless(BASH, "bash not on PATH")
class GatherBashTests(_GatherContractMixin, unittest.TestCase):
    SHELL_KIND = "bash"


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win"),
    "PowerShell gather tests are Windows-only.",
)
class GatherPowerShellTests(_GatherContractMixin, unittest.TestCase):
    SHELL_KIND = "powershell"


class GatherScriptsTracked(unittest.TestCase):
    def test_sh_exists(self) -> None:
        self.assertTrue(GATHER_SH.exists(), f"missing: {GATHER_SH}")

    def test_ps1_exists(self) -> None:
        self.assertTrue(GATHER_PS1.exists(), f"missing: {GATHER_PS1}")


if __name__ == "__main__":
    unittest.main()
