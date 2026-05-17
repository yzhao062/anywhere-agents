"""Contract tests for dispatch-codex.{sh,ps1} -- Auto-terminal channel.

Validates the dispatch contract documented in
skills/implement-review/SKILL.md Phase 1c "Auto-terminal mechanics".

Strategy: replace the real codex binary with a mock Python stub (via the
CODEX_BIN env override that dispatch-codex honors). The mock logs args +
stdin + cwd to a per-test directory and returns a configurable exit code,
so we can verify the dispatch wiring end-to-end without invoking codex.

The bash class and the powershell class share a mixin and each skips when
its shell is not on PATH, so the same file runs on Ubuntu (bash only),
on Windows (both, since Git Bash is on PATH), and in CI on both runners.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "implement-review" / "scripts"
DISPATCH_SH = SCRIPTS_DIR / "dispatch-codex.sh"
DISPATCH_PS1 = SCRIPTS_DIR / "dispatch-codex.ps1"


def _temp_dir():
    """tempfile.TemporaryDirectory with ignore_cleanup_errors on Py3.10+.

    On Windows the spawned stall-watch subprocess may still hold a handle
    on a state-dir file when the test's `with` block exits, which raises
    PermissionError during rmtree. `ignore_cleanup_errors=True` (added in
    Python 3.10) silently swallows that. Older Pythons (the CI matrix
    includes Ubuntu py3.9) get plain TemporaryDirectory; on Linux the
    cleanup race does not exist because Linux does not lock open files.
    """
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()


MOCK_CODEX_PY = r'''"""Mock codex stub for dispatch-codex tests.

Behavior driven by env vars (all optional):
  MOCK_CODEX_LOG     directory to write args/stdin/cwd files into (default cwd)
  MOCK_CODEX_EXIT    integer exit code to return (default 0)
  MOCK_CODEX_STDOUT  text written to stdout (default "mock-codex: stdout\n")
  MOCK_CODEX_STDERR  text written to stderr (default "mock-codex: stderr\n")
"""
import json
import os
import sys

log_dir = os.environ.get("MOCK_CODEX_LOG", os.getcwd())
os.makedirs(log_dir, exist_ok=True)

with open(os.path.join(log_dir, "args"), "w", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]))

stdin_data = sys.stdin.read()
with open(os.path.join(log_dir, "stdin"), "w", encoding="utf-8", newline="") as f:
    f.write(stdin_data)

with open(os.path.join(log_dir, "cwd"), "w", encoding="utf-8") as f:
    f.write(os.getcwd())

sys.stdout.write(os.environ.get("MOCK_CODEX_STDOUT", "mock-codex: stdout\n"))
sys.stderr.write(os.environ.get("MOCK_CODEX_STDERR", "mock-codex: stderr\n"))

sys.exit(int(os.environ.get("MOCK_CODEX_EXIT", "0")))
'''


BASH = shutil.which("bash")
PS_SHELL = shutil.which("pwsh") or shutil.which("powershell")


def _write_mock_codex(tmpdir: Path, want_powershell_shim: bool) -> Path:
    """Drop the mock codex Python script + a shell shim into tmpdir.

    Returns the path the dispatch script should set as CODEX_BIN.
    """
    py_path = tmpdir / "mock_codex.py"
    py_path.write_text(MOCK_CODEX_PY, encoding="utf-8")

    if want_powershell_shim:
        # PowerShell `& $codexBin` invokes .cmd files via cmd.exe; this is
        # the most reliable cross-version shim on Windows.
        shim = tmpdir / "codex-mock.cmd"
        shim.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "{py_path}" %*\r\n',
            encoding="utf-8",
        )
    else:
        shim = tmpdir / "codex-mock.sh"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            f'exec "{sys.executable}" "{py_path}" "$@"\n',
            encoding="utf-8",
        )
        mode = shim.stat().st_mode
        shim.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _parse_state_dir(stdout: str) -> Path:
    if not stdout:
        raise AssertionError("dispatch stdout is empty (expected STATE-DIR line)")
    first_line = stdout.splitlines()[0]
    match = re.match(r"^STATE-DIR (.+)$", first_line)
    if not match:
        raise AssertionError(
            f"first stdout line is not 'STATE-DIR <path>': {first_line!r}"
        )
    return Path(match.group(1).strip())


class _DispatchContractMixin:
    """Shared assertions; concrete subclasses pick the shell."""

    SHELL_KIND: str = ""  # "bash" or "powershell" -- subclass overrides

    def _build_cmd(
        self,
        prompt_file: Path,
        round_arg: str,
        expected_review_file: str,
    ) -> list[str]:
        if self.SHELL_KIND == "bash":
            return [
                BASH,
                str(DISPATCH_SH),
                "--prompt-file", str(prompt_file),
                "--round", round_arg,
                "--expected-review-file", expected_review_file,
            ]
        if self.SHELL_KIND == "powershell":
            return [
                PS_SHELL,
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", str(DISPATCH_PS1),
                "--prompt-file", str(prompt_file),
                "--round", round_arg,
                "--expected-review-file", expected_review_file,
            ]
        raise AssertionError(f"unknown SHELL_KIND: {self.SHELL_KIND!r}")

    def _run_dispatch(
        self,
        cwd: Path,
        prompt_file: Path,
        round_arg: str,
        expected_review_file: str,
        codex_bin: Path,
        log_dir: Path,
        exit_code: int = 0,
        timeout: float = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CODEX_BIN"] = str(codex_bin)
        env["MOCK_CODEX_LOG"] = str(log_dir)
        env["MOCK_CODEX_EXIT"] = str(exit_code)
        # Force dispatch to choose `cwd` as its temp base for the state-dir
        # so unittest's TemporaryDirectory cleans everything up.
        env["TMPDIR"] = str(cwd)
        env["TEMP"] = str(cwd)
        env["TMP"] = str(cwd)
        # Make any background stall-watch spawned by dispatch die quickly
        # (1s poll) so it does not hold open handles into the test tempdir.
        # Threshold is set high so the dispatch tests never trip a spurious
        # stall record; dedicated stall behavior lives in test_stall_watch.py.
        env.setdefault("STALL_POLL_INTERVAL_SECONDS", "1")
        env.setdefault("STALL_THRESHOLD_SECONDS", "999999")
        return subprocess.run(
            self._build_cmd(prompt_file, round_arg, expected_review_file),
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def _fresh_fixture(self, tmpdir: Path) -> tuple[Path, Path, Path]:
        """Returns (codex_bin_shim, prompt_file, log_dir)."""
        log_dir = tmpdir / "mock-log"
        log_dir.mkdir()
        codex_bin = _write_mock_codex(
            tmpdir, want_powershell_shim=(self.SHELL_KIND == "powershell")
        )
        prompt = tmpdir / "prompt.txt"
        prompt.write_text(
            "REVIEW PROMPT body\nLine 2 content\nLine 3 content\n",
            encoding="utf-8",
        )
        return codex_bin, prompt, log_dir

    # --- contract assertions ---------------------------------------------

    def test_state_dir_first_line(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Codex.md", codex, log_dir
            )
            self.assertEqual(
                result.returncode, 0,
                f"dispatch failed unexpectedly\nSTDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}",
            )
            state_dir = _parse_state_dir(result.stdout)
            self.assertTrue(state_dir.is_absolute(),
                            f"STATE-DIR must be absolute: {state_dir}")
            self.assertTrue(state_dir.exists(),
                            f"STATE-DIR must exist: {state_dir}")

    def test_state_dir_naming(self) -> None:
        # Format: implement-review-codex-<8hex>-round<N>-<pid>-<16hex>
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "7", "Review-Codex.md", codex, log_dir
            )
            state_dir = _parse_state_dir(result.stdout)
            self.assertRegex(
                state_dir.name,
                r"^implement-review-codex-[0-9a-f]{8}-round7-\d+-[0-9a-f]{16}$",
                f"state-dir name pattern: {state_dir.name}",
            )

    def test_state_dir_contains_pre_mtime_and_timestamp_and_tail(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)

            review = tmpdir / "Review-Codex.md"
            review.write_text("old review content\n", encoding="utf-8")
            old_mtime = int(review.stat().st_mtime)

            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Codex.md", codex, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state_dir = _parse_state_dir(result.stdout)

            pre_mtime_file = state_dir / "pre-mtime"
            self.assertTrue(pre_mtime_file.exists(),
                            "state-dir must contain pre-mtime")
            pre_mtime = pre_mtime_file.read_text(encoding="utf-8").strip()
            self.assertTrue(pre_mtime.isdigit(),
                            f"pre-mtime must be numeric: {pre_mtime!r}")
            # Pre-mtime must be Unix epoch seconds, not FILETIME (Windows).
            # FILETIME values for any date after ~1970 are far above 1e16;
            # Unix epoch seconds are around 1.7e9 in 2026.
            self.assertLess(
                int(pre_mtime), 10**12,
                "pre-mtime looks like FILETIME (100ns since 1601), "
                "not Unix epoch seconds. SKILL.md Phase 1c bans this.",
            )
            self.assertAlmostEqual(int(pre_mtime), old_mtime, delta=2,
                                   msg="pre-mtime must match file's mtime")

            ts_file = state_dir / "timestamp"
            self.assertTrue(ts_file.exists(),
                            "state-dir must contain timestamp")
            ts = ts_file.read_text(encoding="utf-8").strip()
            self.assertTrue(ts.isdigit(), f"timestamp must be numeric: {ts!r}")
            self.assertLess(int(ts), 10**12,
                            "timestamp must be Unix epoch seconds")
            self.assertAlmostEqual(int(ts), int(time.time()), delta=60)

            tail_file = state_dir / "tail"
            self.assertTrue(tail_file.exists(),
                            "state-dir must contain tail")
            tail_content = tail_file.read_text(encoding="utf-8")
            self.assertIn("mock-codex: stdout", tail_content,
                          "tail must capture codex stdout")
            self.assertIn("mock-codex: stderr", tail_content,
                          "tail must capture codex stderr (via 2>&1)")

    def test_pre_mtime_zero_when_review_file_missing(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            # No Review-Whatever.md created
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Missing.md", codex, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state_dir = _parse_state_dir(result.stdout)
            pre_mtime = (state_dir / "pre-mtime").read_text(
                encoding="utf-8"
            ).strip()
            self.assertEqual(pre_mtime, "0",
                             f"pre-mtime must be 0 when review file absent: "
                             f"{pre_mtime!r}")

    def test_prompt_sent_via_stdin(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Codex.md", codex, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            stdin_log = (log_dir / "stdin").read_text(encoding="utf-8")
            for needle in ("REVIEW PROMPT body", "Line 2 content",
                           "Line 3 content"):
                self.assertIn(needle, stdin_log,
                              f"stdin log must contain: {needle!r}")

    def test_codex_invoked_exec_dash_not_review(self) -> None:
        """codex must be called as `exec [flags] -`, never `exec review ...`.

        The dispatcher's positional shape is `codex exec --sandbox <mode> -`
        (the --sandbox flag was added to align Auto-terminal's trust model
        with Terminal-relay; see test_codex_invoked_with_default_sandbox).
        Stdin marker `-` must always be the FINAL positional, and the
        `review` subcommand must never appear at any position because
        that would trigger Codex's built-in review prompt template.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Codex.md", codex, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((log_dir / "args").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(args), 2,
                                    f"codex must receive at least 2 args: {args}")
            self.assertEqual(args[0], "exec",
                             f"first arg must be 'exec', got: {args}")
            self.assertEqual(args[-1], "-",
                             f"last arg must be '-' (stdin), got: {args}")
            self.assertNotIn("review", args,
                             f"'review' positional must not appear: {args}")

    def test_codex_invoked_with_default_sandbox_flag(self) -> None:
        """Default sandbox mode flows through to the actual codex args.

        Static substring tests in DispatchSandboxFlagContract are not
        enough: a future edit could leave `--sandbox danger-full-access`
        in comments or doc strings while dropping the actual invocation,
        and the substring tests would still pass. This test asserts the
        runtime-logged args from the mock codex include the flag.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Codex.md", codex, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((log_dir / "args").read_text(encoding="utf-8"))
            self.assertIn("--sandbox", args,
                          f"--sandbox flag must be present in codex args: {args}")
            sandbox_idx = args.index("--sandbox")
            self.assertGreater(
                len(args), sandbox_idx + 1,
                f"--sandbox must be followed by a mode value: {args}",
            )
            self.assertEqual(
                args[sandbox_idx + 1], "danger-full-access",
                f"default sandbox mode must be danger-full-access: {args}",
            )

    def test_codex_invoked_with_overridden_sandbox_flag(self) -> None:
        """CODEX_DISPATCH_SANDBOX env var overrides the default mode.

        CI / sandbox-strict environments need to narrow the trust posture
        below danger-full-access. The dispatcher honors the env var; this
        test asserts the override actually reaches codex's command line.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            # _run_dispatch builds env from os.environ.copy(); pre-set the
            # override there so it propagates into the subprocess.
            old = os.environ.get("CODEX_DISPATCH_SANDBOX")
            os.environ["CODEX_DISPATCH_SANDBOX"] = "workspace-write"
            try:
                result = self._run_dispatch(
                    tmpdir, prompt, "1", "Review-Codex.md", codex, log_dir
                )
            finally:
                if old is None:
                    os.environ.pop("CODEX_DISPATCH_SANDBOX", None)
                else:
                    os.environ["CODEX_DISPATCH_SANDBOX"] = old

            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((log_dir / "args").read_text(encoding="utf-8"))
            self.assertIn("--sandbox", args,
                          f"--sandbox flag must be present: {args}")
            sandbox_idx = args.index("--sandbox")
            self.assertEqual(
                args[sandbox_idx + 1], "workspace-write",
                f"CODEX_DISPATCH_SANDBOX override must reach codex: {args}",
            )

    def test_exit_code_zero_propagation(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Codex.md", codex, log_dir,
                exit_code=0,
            )
            self.assertEqual(result.returncode, 0)

    def test_exit_code_nonzero_propagation(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Codex.md", codex, log_dir,
                exit_code=17,
            )
            self.assertEqual(
                result.returncode, 17,
                f"dispatch must propagate codex exit 17, got {result.returncode}\n"
                f"STDERR:\n{result.stderr}",
            )

    def test_unique_state_dirs_across_consecutive_runs(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            r1 = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Codex.md", codex, log_dir
            )
            r2 = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Codex.md", codex, log_dir
            )
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            d1 = _parse_state_dir(r1.stdout)
            d2 = _parse_state_dir(r2.stdout)
            self.assertNotEqual(d1, d2,
                                "consecutive dispatches must produce unique state-dirs")

    def test_missing_prompt_file_exits_two(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, _, log_dir = self._fresh_fixture(tmpdir)
            missing = tmpdir / "does-not-exist.txt"
            result = self._run_dispatch(
                tmpdir, missing, "1", "Review-Codex.md", codex, log_dir
            )
            self.assertEqual(
                result.returncode, 2,
                f"missing prompt file must exit 2\nSTDERR:\n{result.stderr}",
            )

    def test_invalid_round_exits_two(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "abc", "Review-Codex.md", codex, log_dir
            )
            self.assertEqual(
                result.returncode, 2,
                f"non-numeric round must exit 2\nSTDERR:\n{result.stderr}",
            )

    def test_missing_required_arg_exits_two(self) -> None:
        if self.SHELL_KIND == "bash":
            cmd = [BASH, str(DISPATCH_SH), "--prompt-file", "x.txt"]
        else:
            cmd = [
                PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(DISPATCH_PS1),
                "--prompt-file", "x.txt",
            ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=30
        )
        self.assertEqual(
            result.returncode, 2,
            f"missing required args must exit 2\nSTDERR:\n{result.stderr}",
        )


@unittest.skipIf(
    sys.platform.startswith("win"),
    "bash skipped on Windows: Git Bash POSIX-translates env-var paths "
    "(C:\\...\\Temp\\tmpXX -> /tmp/tmpXX), which breaks path comparison "
    "from Python's Windows-path perspective. CI Linux covers this lane.",
)
@unittest.skipUnless(BASH, "bash not on PATH")
class DispatchCodexBashTests(_DispatchContractMixin, unittest.TestCase):
    SHELL_KIND = "bash"


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win"),
    "PowerShell dispatch tests are Windows-only: dispatch-codex.ps1 calls "
    "powershell.exe with -WindowStyle Hidden which is unsupported on "
    "PowerShell 7 for Linux/macOS. Production users on those platforms run "
    "dispatch-codex.sh instead.",
)
class DispatchCodexPowerShellTests(_DispatchContractMixin, unittest.TestCase):
    SHELL_KIND = "powershell"


class DispatchScriptsTracked(unittest.TestCase):
    """Sanity: both scripts must be present in the repo."""

    def test_sh_exists(self) -> None:
        self.assertTrue(DISPATCH_SH.exists(),
                        f"dispatch-codex.sh missing: {DISPATCH_SH}")

    def test_ps1_exists(self) -> None:
        self.assertTrue(DISPATCH_PS1.exists(),
                        f"dispatch-codex.ps1 missing: {DISPATCH_PS1}")


class DispatchSandboxFlagContract(unittest.TestCase):
    """Both dispatchers must invoke `codex exec --sandbox <mode>` so the
    Auto-terminal trust model aligns with Terminal-relay. Default mode is
    `danger-full-access`; `CODEX_DISPATCH_SANDBOX` env var overrides.

    Background: Codex 0.130.0's default `workspace-write` sandbox runner
    on Windows hits `CreateProcessAsUserW failed: 1312` when Codex spawns
    its own git / grep / pwsh subprocess, so the review came back as
    "could not access files". Passing `--sandbox danger-full-access` to
    `codex exec` bypasses the broken sandbox runner and gives Codex the
    same access it has in Terminal-relay. See SKILL.md "Trust model and
    the sandbox flag" paragraph for the full rationale.

    These tests freeze the dispatcher contract: a future refactor that
    silently drops the flag would re-introduce the 1312 failure mode.
    """

    def test_sh_passes_sandbox_flag(self) -> None:
        text = DISPATCH_SH.read_text(encoding="utf-8")
        self.assertIn("--sandbox", text,
                      "dispatch-codex.sh must pass --sandbox to codex exec")
        self.assertIn("CODEX_DISPATCH_SANDBOX", text,
                      "dispatch-codex.sh must read CODEX_DISPATCH_SANDBOX env var")
        self.assertIn("danger-full-access", text,
                      "dispatch-codex.sh must default sandbox to danger-full-access")

    def test_ps1_passes_sandbox_flag(self) -> None:
        text = DISPATCH_PS1.read_text(encoding="utf-8")
        self.assertIn("--sandbox", text,
                      "dispatch-codex.ps1 must pass --sandbox to codex exec")
        self.assertIn("CODEX_DISPATCH_SANDBOX", text,
                      "dispatch-codex.ps1 must read $env:CODEX_DISPATCH_SANDBOX")
        self.assertIn("danger-full-access", text,
                      "dispatch-codex.ps1 must default sandbox to danger-full-access")


# Mock codex that consumes stdin then sleeps (writes nothing to stdout).
# Used by the stall-integration test below to simulate a Codex run that
# stalls past the threshold without producing output.
MOCK_CODEX_SLEEPING_PY = r'''import os, sys, time
sys.stdin.buffer.read()
time.sleep(float(os.environ.get("MOCK_CODEX_SLEEP", "5")))
'''

# Mock codex that consumes stdin and writes periodic stderr progress while
# never writing stdout. On PowerShell dispatch, stdout is what feeds the
# live <state-dir>/tail growth; stall-watch must also observe the stderr
# side file so this counts as "alive" and we do NOT log a stall.
MOCK_CODEX_STDERR_PROGRESS_PY = r'''import os, sys, time
sys.stdin.buffer.read()
duration = float(os.environ.get("MOCK_CODEX_STDERR_SLEEP", "4"))
deadline = time.monotonic() + duration
while time.monotonic() < deadline:
    sys.stderr.write("stderr progress tick\n")
    sys.stderr.flush()
    time.sleep(0.5)
'''


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win"),
    "Stall integration tests use the PowerShell dispatch (Windows-only)",
)
class DispatchStallIntegrationTests(unittest.TestCase):
    """End-to-end check that closes Codex Round 1 Medium #3.

    When a Codex run crosses the stall threshold mid-execution, stall-watch
    must record the STALL even if Codex exits shortly after; dispatch must
    not erase that signal by terminating stall-watch on its hot path.
    """

    def test_stall_warning_survives_dispatch_completion(self) -> None:
        import stat
        with _temp_dir() as td:
            tmpdir = Path(td)
            log_dir = tmpdir / "mock-log"
            log_dir.mkdir()

            mock_py = tmpdir / "mock_codex_sleep.py"
            mock_py.write_text(MOCK_CODEX_SLEEPING_PY, encoding="utf-8")
            shim = tmpdir / "codex-mock.cmd"
            shim.write_text(
                "@echo off\r\n" f'"{sys.executable}" "{mock_py}" %*\r\n',
                encoding="utf-8",
            )

            prompt = tmpdir / "prompt.txt"
            prompt.write_text("stall integration test\n", encoding="utf-8")

            env = os.environ.copy()
            env["CODEX_BIN"] = str(shim)
            env["TMPDIR"] = str(tmpdir)
            env["TEMP"] = str(tmpdir)
            env["TMP"] = str(tmpdir)
            # Tiny window so the test runs fast: codex stays silent for 5s,
            # threshold trips at 2s, polled every 1s.
            env["MOCK_CODEX_SLEEP"] = "5"
            env["STALL_THRESHOLD_SECONDS"] = "2"
            env["STALL_POLL_INTERVAL_SECONDS"] = "1"

            cmd = [
                PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(DISPATCH_PS1),
                "--prompt-file", str(prompt),
                "--round", "1",
                "--expected-review-file", "Review-Codex.md",
            ]
            result = subprocess.run(
                cmd, cwd=str(tmpdir), env=env,
                capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(
                result.returncode, 0,
                f"dispatch failed\nSTDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}",
            )
            state_dir = _parse_state_dir(result.stdout)

            # stall-watch may still be doing its final-on-parent-dead poll;
            # give it a short window to flush stall-warning.
            stall_warning = state_dir / "stall-warning"
            for _ in range(10):
                if stall_warning.exists():
                    break
                time.sleep(0.5)
            self.assertTrue(
                stall_warning.exists(),
                f"stall-warning must persist after dispatch when threshold "
                f"crossed during codex run. STDERR:\n{result.stderr}",
            )
            content = stall_warning.read_text(encoding="utf-8")
            self.assertRegex(
                content,
                r"STALL \S+ tail-no-growth-for-\d+s",
                f"stall-warning content: {content!r}",
            )

    def test_stderr_only_progress_does_not_trigger_stall(self) -> None:
        """Codex Round 2 Medium: stderr-only output must count as growth.

        PowerShell dispatch redirects stdout to <state-dir>/tail and stderr
        to <state-dir>/tail.stderr-tmp during the run. stall-watch must
        observe BOTH so a Codex run that emits progress only on stderr is
        not falsely flagged as stalled.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)

            mock_py = tmpdir / "mock_codex_stderr.py"
            mock_py.write_text(MOCK_CODEX_STDERR_PROGRESS_PY, encoding="utf-8")
            shim = tmpdir / "codex-mock.cmd"
            shim.write_text(
                "@echo off\r\n" f'"{sys.executable}" "{mock_py}" %*\r\n',
                encoding="utf-8",
            )

            prompt = tmpdir / "prompt.txt"
            prompt.write_text("stderr-only progress test\n", encoding="utf-8")

            env = os.environ.copy()
            env["CODEX_BIN"] = str(shim)
            env["TMPDIR"] = str(tmpdir)
            env["TEMP"] = str(tmpdir)
            env["TMP"] = str(tmpdir)
            # Codex writes stderr every 0.5s for 4s; threshold 2s, poll 1s.
            # A correct stall-watch sees stderr growth and never logs a stall.
            env["MOCK_CODEX_STDERR_SLEEP"] = "4"
            env["STALL_THRESHOLD_SECONDS"] = "2"
            env["STALL_POLL_INTERVAL_SECONDS"] = "1"

            cmd = [
                PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(DISPATCH_PS1),
                "--prompt-file", str(prompt),
                "--round", "1",
                "--expected-review-file", "Review-Codex.md",
            ]
            result = subprocess.run(
                cmd, cwd=str(tmpdir), env=env,
                capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(
                result.returncode, 0,
                f"dispatch failed\nSTDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}",
            )
            state_dir = _parse_state_dir(result.stdout)

            # Give stall-watch a moment for any post-dispatch poll.
            time.sleep(1.0)
            stall_warning = state_dir / "stall-warning"
            if stall_warning.exists():
                content = stall_warning.read_text(encoding="utf-8")
                self.fail(
                    "stall-warning was written despite continuous stderr "
                    "progress -- stall-watch must observe stderr stream too. "
                    f"content: {content!r}"
                )
            tail_text = (state_dir / "tail").read_text(encoding="utf-8")
            self.assertIn(
                "stderr progress tick", tail_text,
                "tail must contain the appended stderr content "
                "(diagnostic completeness check)",
            )


if __name__ == "__main__":
    unittest.main()
