"""Contract tests for prun's dispatch-task.{sh,ps1}.

Validates the dispatch contract documented in skills/prun/SKILL.md. Mirrors
tests/test_dispatch_codex.py: the real codex binary is replaced with a mock
Python stub (via the CODEX_BIN override that dispatch-task honors) that logs
args + stdin + cwd, so the dispatch wiring is verified without invoking codex.

prun-specific contract vs dispatch-codex:
  - args are --prompt-file / --result-file / --unit-id (no --round).
  - state-dir is named prun-task-<8hex>-<unit-id>-<pid>-<16hex>.
  - codex runs from a per-unit SCRATCH cwd (<state-dir>/work) so accidental
    relative writes stay out of the user's repo.
  - the same `exec --sandbox <mode> [--ignore-user-config -c <reasoning>] -`
    shape and stdin delivery as dispatch-codex.

The bash class and the powershell class share a mixin and each skips when its
shell is not on PATH, so the same file runs on Ubuntu (bash) and Windows (both).
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
SCRIPTS_DIR = ROOT / "skills" / "prun" / "scripts"
DISPATCH_SH = SCRIPTS_DIR / "dispatch-task.sh"
DISPATCH_PS1 = SCRIPTS_DIR / "dispatch-task.ps1"


def _temp_dir():
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()


MOCK_CODEX_PY = r'''"""Mock codex stub for dispatch-task tests."""
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

result_target = os.environ.get("MOCK_CODEX_WRITE_RESULT")
if result_target:
    with open(result_target, "w", encoding="utf-8") as f:
        f.write(os.environ.get("MOCK_CODEX_RESULT_CONTENT", "# worker result\nreal worker content\n"))

sys.stdout.write(os.environ.get("MOCK_CODEX_STDOUT", "mock-codex: stdout\n"))
sys.stderr.write(os.environ.get("MOCK_CODEX_STDERR", "mock-codex: stderr\n"))

sys.exit(int(os.environ.get("MOCK_CODEX_EXIT", "0")))
'''


BASH = shutil.which("bash")
PS_SHELL = shutil.which("pwsh") or shutil.which("powershell")


def _write_mock_codex(tmpdir: Path, want_powershell_shim: bool) -> Path:
    py_path = tmpdir / "mock_codex.py"
    py_path.write_text(MOCK_CODEX_PY, encoding="utf-8")
    if want_powershell_shim:
        shim = tmpdir / "codex-mock.cmd"
        shim.write_text(
            "@echo off\r\n" f'"{sys.executable}" "{py_path}" %*\r\n',
            encoding="utf-8",
        )
    else:
        shim = tmpdir / "codex-mock.sh"
        shim.write_text(
            "#!/usr/bin/env bash\n" f'exec "{sys.executable}" "{py_path}" "$@"\n',
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


class _DispatchTaskContractMixin:
    SHELL_KIND: str = ""  # "bash" or "powershell"

    def _build_cmd(
        self, prompt_file: Path, result_file: str, unit_id: str
    ) -> list[str]:
        if self.SHELL_KIND == "bash":
            return [
                BASH, str(DISPATCH_SH),
                "--prompt-file", str(prompt_file),
                "--result-file", result_file,
                "--unit-id", unit_id,
            ]
        if self.SHELL_KIND == "powershell":
            return [
                PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(DISPATCH_PS1),
                "--prompt-file", str(prompt_file),
                "--result-file", result_file,
                "--unit-id", unit_id,
            ]
        raise AssertionError(f"unknown SHELL_KIND: {self.SHELL_KIND!r}")

    def _run_dispatch(
        self, cwd: Path, prompt_file: Path, result_file: str, unit_id: str,
        codex_bin: Path, log_dir: Path, exit_code: int = 0, timeout: float = 60.0,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CODEX_BIN"] = str(codex_bin)
        env["MOCK_CODEX_LOG"] = str(log_dir)
        env["MOCK_CODEX_EXIT"] = str(exit_code)
        env["TMPDIR"] = str(cwd)
        env["TEMP"] = str(cwd)
        env["TMP"] = str(cwd)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            self._build_cmd(prompt_file, result_file, unit_id),
            cwd=str(cwd), env=env, capture_output=True, text=True,
            check=False, timeout=timeout,
        )

    def _fresh_fixture(self, tmpdir: Path) -> tuple[Path, Path, Path]:
        log_dir = tmpdir / "mock-log"
        log_dir.mkdir()
        codex_bin = _write_mock_codex(
            tmpdir, want_powershell_shim=(self.SHELL_KIND == "powershell")
        )
        prompt = tmpdir / "prompt.txt"
        prompt.write_text(
            "TASK PROMPT body\nLine 2 content\nLine 3 content\n", encoding="utf-8"
        )
        return codex_bin, prompt, log_dir

    # --- contract assertions ---------------------------------------------

    def test_state_dir_first_line(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, str(tmpdir / "result.md"), "unit_a", codex, log_dir
            )
            self.assertEqual(result.returncode, 0,
                             f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
            state_dir = _parse_state_dir(result.stdout)
            self.assertTrue(state_dir.is_absolute(), state_dir)
            self.assertTrue(state_dir.exists(), state_dir)

    def test_state_dir_naming(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, str(tmpdir / "r.md"), "survey_7", codex, log_dir
            )
            state_dir = _parse_state_dir(result.stdout)
            self.assertRegex(
                state_dir.name,
                r"^prun-task-[0-9a-f]{8}-survey_7-\d+-[0-9a-f]{16}$",
                f"state-dir name pattern: {state_dir.name}",
            )

    def test_state_dir_files(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result_file = tmpdir / "result.md"
            result_file.write_text("old result\n", encoding="utf-8")
            old_mtime = int(result_file.stat().st_mtime)
            result = self._run_dispatch(
                tmpdir, prompt, str(result_file), "u", codex, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state_dir = _parse_state_dir(result.stdout)

            pre_mtime = (state_dir / "pre-mtime").read_text(encoding="utf-8").strip()
            self.assertTrue(pre_mtime.isdigit(), pre_mtime)
            self.assertLess(int(pre_mtime), 10**12,
                            "pre-mtime must be Unix epoch seconds, not FILETIME")
            self.assertAlmostEqual(int(pre_mtime), old_mtime, delta=2)

            ts = (state_dir / "timestamp").read_text(encoding="utf-8").strip()
            self.assertTrue(ts.isdigit(), ts)
            self.assertLess(int(ts), 10**12)
            self.assertAlmostEqual(int(ts), int(time.time()), delta=60)

            recorded = (state_dir / "result-file").read_text(encoding="utf-8").strip()
            self.assertEqual(recorded, str(result_file),
                             "state-dir must record the result-file path")

            tail = (state_dir / "tail").read_text(encoding="utf-8")
            self.assertIn("mock-codex: stdout", tail)
            self.assertIn("mock-codex: stderr", tail)

    def test_pre_mtime_zero_when_result_missing(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, str(tmpdir / "absent.md"), "u", codex, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state_dir = _parse_state_dir(result.stdout)
            pre = (state_dir / "pre-mtime").read_text(encoding="utf-8").strip()
            self.assertEqual(pre, "0")

    def test_fallback_salvages_tail_when_result_unwritten(self) -> None:
        """If the worker exits without writing its result file, dispatch-task
        salvages the captured tail into the result file (FALLBACK header) so the
        unit is never silently missing when gather polls."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result_file = tmpdir / "result.md"  # the mock never writes this
            res = self._run_dispatch(
                tmpdir, prompt, str(result_file), "u_fb", codex, log_dir,
                extra_env={"MOCK_CODEX_STDOUT": "WORKER-OUTPUT-MARKER\n"},
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(result_file.exists(),
                            "fallback must create the result file when the worker did not")
            body = result_file.read_text(encoding="utf-8")
            self.assertIn("FALLBACK", body, "salvaged result must be marked FALLBACK")
            self.assertIn("u_fb", body, "fallback header must name the unit")
            self.assertIn("WORKER-OUTPUT-MARKER", body,
                          "fallback must salvage the worker's captured stdout from the tail")

    def test_fallback_does_not_clobber_a_written_result(self) -> None:
        """When the worker DOES write a non-empty result file, dispatch-task must
        leave it untouched (no salvage clobber)."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result_file = tmpdir / "result.md"
            real = "# u_ok result\nConclusion: genuine worker result\n"
            res = self._run_dispatch(
                tmpdir, prompt, str(result_file), "u_ok", codex, log_dir,
                extra_env={
                    "MOCK_CODEX_WRITE_RESULT": str(result_file),
                    "MOCK_CODEX_RESULT_CONTENT": real,
                },
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            body = result_file.read_text(encoding="utf-8")
            self.assertEqual(body, real, "a worker-written result must survive untouched")
            self.assertNotIn("FALLBACK", body)

    def test_codex_runs_from_scratch_cwd(self) -> None:
        """The Round-3 Medium: codex runs from a per-unit scratch dir under the
        state-dir, so accidental relative writes stay out of the user's repo."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, str(tmpdir / "r.md"), "u", codex, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state_dir = _parse_state_dir(result.stdout)
            cwd_logged = Path((log_dir / "cwd").read_text(encoding="utf-8").strip())
            self.assertEqual(cwd_logged.name, "work",
                             f"codex cwd must be the scratch 'work' dir: {cwd_logged}")
            # And it must live under this dispatch's state-dir, not the repo.
            self.assertEqual(
                cwd_logged.parent.resolve(), state_dir.resolve(),
                f"scratch cwd must be under the state-dir: {cwd_logged}",
            )

    def test_prompt_sent_via_stdin(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, str(tmpdir / "r.md"), "u", codex, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            stdin_log = (log_dir / "stdin").read_text(encoding="utf-8")
            for needle in ("TASK PROMPT body", "Line 2 content", "Line 3 content"):
                self.assertIn(needle, stdin_log)

    def test_codex_invoked_exec_dash_not_review(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, str(tmpdir / "r.md"), "u", codex, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((log_dir / "args").read_text(encoding="utf-8"))
            self.assertEqual(args[0], "exec", args)
            self.assertEqual(args[-1], "-", args)
            self.assertNotIn("review", args, args)
            # Scratch cwd is not a git repo, so codex needs this or it refuses
            # with "Not inside a trusted directory".
            self.assertIn("--skip-git-repo-check", args, args)

    def test_default_sandbox_flag(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, str(tmpdir / "r.md"), "u", codex, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((log_dir / "args").read_text(encoding="utf-8"))
            self.assertIn("--sandbox", args)
            self.assertEqual(args[args.index("--sandbox") + 1], "danger-full-access")

    def test_sandbox_override(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            old = os.environ.get("CODEX_DISPATCH_SANDBOX")
            os.environ["CODEX_DISPATCH_SANDBOX"] = "workspace-write"
            try:
                result = self._run_dispatch(
                    tmpdir, prompt, str(tmpdir / "r.md"), "u", codex, log_dir
                )
            finally:
                if old is None:
                    os.environ.pop("CODEX_DISPATCH_SANDBOX", None)
                else:
                    os.environ["CODEX_DISPATCH_SANDBOX"] = old
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((log_dir / "args").read_text(encoding="utf-8"))
            self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")

    def test_mcp_isolation_default(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            old = os.environ.pop("CODEX_DISPATCH_ISOLATE_MCP", None)
            old_r = os.environ.pop("CODEX_DISPATCH_REASONING", None)
            try:
                result = self._run_dispatch(
                    tmpdir, prompt, str(tmpdir / "r.md"), "u", codex, log_dir
                )
            finally:
                if old is not None:
                    os.environ["CODEX_DISPATCH_ISOLATE_MCP"] = old
                if old_r is not None:
                    os.environ["CODEX_DISPATCH_REASONING"] = old_r
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((log_dir / "args").read_text(encoding="utf-8"))
            self.assertIn("--ignore-user-config", args)
            self.assertIn(("-c", "model_reasoning_effort=xhigh"),
                          list(zip(args, args[1:])))
            self.assertEqual(args[-1], "-")

    def test_mcp_isolation_off(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            old = os.environ.get("CODEX_DISPATCH_ISOLATE_MCP")
            os.environ["CODEX_DISPATCH_ISOLATE_MCP"] = "off"
            try:
                result = self._run_dispatch(
                    tmpdir, prompt, str(tmpdir / "r.md"), "u", codex, log_dir
                )
            finally:
                if old is None:
                    os.environ.pop("CODEX_DISPATCH_ISOLATE_MCP", None)
                else:
                    os.environ["CODEX_DISPATCH_ISOLATE_MCP"] = old
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((log_dir / "args").read_text(encoding="utf-8"))
            self.assertNotIn("--ignore-user-config", args)
            self.assertFalse(
                any(str(a).startswith("model_reasoning_effort=") for a in args), args
            )
            self.assertIn("--sandbox", args)
            self.assertEqual(args[-1], "-")

    def test_reasoning_override(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            old_iso = os.environ.pop("CODEX_DISPATCH_ISOLATE_MCP", None)
            old_r = os.environ.get("CODEX_DISPATCH_REASONING")
            os.environ["CODEX_DISPATCH_REASONING"] = "high"
            try:
                result = self._run_dispatch(
                    tmpdir, prompt, str(tmpdir / "r.md"), "u", codex, log_dir
                )
            finally:
                if old_iso is not None:
                    os.environ["CODEX_DISPATCH_ISOLATE_MCP"] = old_iso
                if old_r is None:
                    os.environ.pop("CODEX_DISPATCH_REASONING", None)
                else:
                    os.environ["CODEX_DISPATCH_REASONING"] = old_r
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((log_dir / "args").read_text(encoding="utf-8"))
            self.assertIn("model_reasoning_effort=high", args)
            self.assertNotIn("model_reasoning_effort=xhigh", args)

    def test_exit_code_propagation(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, str(tmpdir / "r.md"), "u", codex, log_dir,
                exit_code=23,
            )
            self.assertEqual(result.returncode, 23, result.stderr)

    def test_unique_state_dirs(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            r1 = self._run_dispatch(
                tmpdir, prompt, str(tmpdir / "r.md"), "u", codex, log_dir
            )
            r2 = self._run_dispatch(
                tmpdir, prompt, str(tmpdir / "r.md"), "u", codex, log_dir
            )
            self.assertNotEqual(_parse_state_dir(r1.stdout),
                                _parse_state_dir(r2.stdout))

    def test_missing_prompt_file_exits_two(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, _, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, tmpdir / "nope.txt", str(tmpdir / "r.md"), "u",
                codex, log_dir,
            )
            self.assertEqual(result.returncode, 2, result.stderr)

    def test_bad_unit_id_exits_two(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, str(tmpdir / "r.md"), "bad id!", codex, log_dir
            )
            self.assertEqual(result.returncode, 2,
                             f"non-alnum unit-id must exit 2\nSTDERR:\n{result.stderr}")

    def test_missing_required_arg_exits_two(self) -> None:
        if self.SHELL_KIND == "bash":
            cmd = [BASH, str(DISPATCH_SH), "--prompt-file", "x.txt"]
        else:
            cmd = [
                PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(DISPATCH_PS1), "--prompt-file", "x.txt",
            ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                check=False, timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr)


@unittest.skipIf(
    sys.platform.startswith("win"),
    "bash skipped on Windows: Git Bash POSIX-translates env-var temp paths, "
    "which breaks path comparison from Python's Windows-path perspective. "
    "CI Linux + Spark cover this lane.",
)
@unittest.skipUnless(BASH, "bash not on PATH")
class DispatchTaskBashTests(_DispatchTaskContractMixin, unittest.TestCase):
    SHELL_KIND = "bash"


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win"),
    "PowerShell dispatch tests are Windows-only.",
)
class DispatchTaskPowerShellTests(_DispatchTaskContractMixin, unittest.TestCase):
    SHELL_KIND = "powershell"


class DispatchTaskScriptsTracked(unittest.TestCase):
    def test_sh_exists(self) -> None:
        self.assertTrue(DISPATCH_SH.exists(), f"missing: {DISPATCH_SH}")

    def test_ps1_exists(self) -> None:
        self.assertTrue(DISPATCH_PS1.exists(), f"missing: {DISPATCH_PS1}")


class DispatchTaskStaticContract(unittest.TestCase):
    """Freeze the safety wiring so a refactor that drops a flag fails here."""

    def _both(self):
        return [DISPATCH_SH.read_text(encoding="utf-8"),
                DISPATCH_PS1.read_text(encoding="utf-8")]

    def test_sandbox_flag_present(self) -> None:
        for text in self._both():
            self.assertIn("--sandbox", text)
            self.assertIn("CODEX_DISPATCH_SANDBOX", text)
            self.assertIn("danger-full-access", text)

    def test_mcp_isolation_present(self) -> None:
        for text in self._both():
            self.assertIn("CODEX_DISPATCH_ISOLATE_MCP", text)
            self.assertIn("--ignore-user-config", text)
            self.assertIn("model_reasoning_effort", text)

    def test_scratch_cwd_present(self) -> None:
        for text in self._both():
            self.assertIn("PRUN_SCRATCH_CWD", text)

    def test_skip_git_repo_check_present(self) -> None:
        # The scratch cwd is intentionally not a git repo; codex needs this.
        for text in self._both():
            self.assertIn("--skip-git-repo-check", text)


if __name__ == "__main__":
    unittest.main()
