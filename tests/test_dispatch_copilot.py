"""Contract tests for dispatch-copilot.{sh,ps1} -- Auto-terminal Copilot backend.

Validates the dispatch contract documented in
skills/implement-review/SKILL.md > Auto-terminal Copilot backend. The Copilot
backend is the cross-vendor reviewer path used when Claude Code is unavailable
and Codex (or the user) is the primary implementer.

Strategy mirrors test_dispatch_codex.py: replace the real copilot binary with a
mock Python stub (via the COPILOT_BIN env override that dispatch-copilot honors).
The mock logs args + cwd to a per-test directory and returns a configurable exit
code, so we verify the dispatch wiring end-to-end without invoking copilot.

Copilot-specific contract (vs Codex):
  * prompt is referenced as a FILE via `-p "@<prompt>"`, never on stdin and
    never as a long literal -p argument;
  * a narrow allow-list (read + write + shell(git:*)) plus --add-dir reaches the
    binary, and NO --sandbox flag is passed (that is Codex-only);
  * when standalone `copilot` is absent, the dispatcher falls back to `gh copilot`.

The bash class and the powershell class share a mixin and each skips when its
shell is not on PATH, so the same file runs on Ubuntu (bash only), on Windows
(both, since Git Bash is on PATH), and in CI on both runners.
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
DISPATCH_SH = SCRIPTS_DIR / "dispatch-copilot.sh"
DISPATCH_PS1 = SCRIPTS_DIR / "dispatch-copilot.ps1"


def _temp_dir():
    """tempfile.TemporaryDirectory with ignore_cleanup_errors on Py3.10+.

    On Windows the spawned stall-watch subprocess may still hold a handle on a
    state-dir file when the test's `with` block exits, which raises
    PermissionError during rmtree. `ignore_cleanup_errors=True` (added in Python
    3.10) silently swallows that. Older Pythons get plain TemporaryDirectory; on
    Linux the cleanup race does not exist because Linux does not lock open files.
    """
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()


# Mock copilot stub. Unlike the codex mock it does NOT read stdin: the Copilot
# backend delivers the prompt via `-p "@<file>"`, so nothing is piped on stdin
# and a stdin read could block under the test's inherited handles.
MOCK_COPILOT_PY = r'''"""Mock copilot stub for dispatch-copilot tests.

Behavior driven by env vars (all optional):
  MOCK_COPILOT_LOG     directory to write args/cwd files into (default cwd)
  MOCK_COPILOT_EXIT    integer exit code to return (default 0)
  MOCK_COPILOT_STDOUT  text written to stdout (default "mock-copilot: stdout\n")
  MOCK_COPILOT_STDERR  text written to stderr (default "mock-copilot: stderr\n")
"""
import json
import os
import sys

log_dir = os.environ.get("MOCK_COPILOT_LOG", os.getcwd())
os.makedirs(log_dir, exist_ok=True)

with open(os.path.join(log_dir, "args"), "w", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]))

with open(os.path.join(log_dir, "cwd"), "w", encoding="utf-8") as f:
    f.write(os.getcwd())

sys.stdout.write(os.environ.get("MOCK_COPILOT_STDOUT", "mock-copilot: stdout\n"))
sys.stderr.write(os.environ.get("MOCK_COPILOT_STDERR", "mock-copilot: stderr\n"))

sys.exit(int(os.environ.get("MOCK_COPILOT_EXIT", "0")))
'''


BASH = shutil.which("bash")
PS_SHELL = shutil.which("pwsh") or shutil.which("powershell")


def _write_mock(tmpdir: Path, basename: str, want_powershell_shim: bool) -> Path:
    """Drop the mock copilot Python script + a shell shim into tmpdir.

    `basename` lets a test create both a `copilot` mock and a `gh` mock that
    share the same Python stub. Returns the shim path the dispatch script should
    resolve (via COPILOT_BIN / GH_BIN).
    """
    py_path = tmpdir / "mock_copilot.py"
    if not py_path.exists():
        py_path.write_text(MOCK_COPILOT_PY, encoding="utf-8")

    if want_powershell_shim:
        # PowerShell `& $bin` invokes .cmd files via cmd.exe; the most reliable
        # cross-version shim on Windows. %* forwards args verbatim.
        shim = tmpdir / (basename + ".cmd")
        shim.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "{py_path}" %*\r\n',
            encoding="utf-8",
        )
    else:
        shim = tmpdir / (basename + ".sh")
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
        copilot_bin: Path,
        log_dir: Path,
        exit_code: int = 0,
        timeout: float = 60.0,
        extra_env: dict | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["COPILOT_BIN"] = str(copilot_bin)
        env["MOCK_COPILOT_LOG"] = str(log_dir)
        env["MOCK_COPILOT_EXIT"] = str(exit_code)
        # Force dispatch to choose `cwd` as its temp base for the state-dir so
        # unittest's TemporaryDirectory cleans everything up.
        env["TMPDIR"] = str(cwd)
        env["TEMP"] = str(cwd)
        env["TMP"] = str(cwd)
        # Make any background stall-watch spawned by dispatch die quickly (1s
        # poll) so it does not hold open handles into the test tempdir. Threshold
        # is set high so the dispatch tests never trip a spurious stall record.
        env.setdefault("STALL_POLL_INTERVAL_SECONDS", "1")
        env.setdefault("STALL_THRESHOLD_SECONDS", "999999")
        if extra_env:
            env.update(extra_env)
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
        """Returns (copilot_bin_shim, prompt_file, log_dir)."""
        log_dir = tmpdir / "mock-log"
        log_dir.mkdir()
        copilot_bin = _write_mock(
            tmpdir, "copilot-mock",
            want_powershell_shim=(self.SHELL_KIND == "powershell"),
        )
        prompt = tmpdir / "prompt.txt"
        prompt.write_text(
            "REVIEW PROMPT body\nLine 2 content\nLine 3 content\n",
            encoding="utf-8",
        )
        return copilot_bin, prompt, log_dir

    def _read_args(self, log_dir: Path) -> list[str]:
        return json.loads((log_dir / "args").read_text(encoding="utf-8"))

    # --- contract assertions ---------------------------------------------

    def test_state_dir_first_line(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            copilot, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-GitHub-Copilot.md", copilot, log_dir
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
        # Format: implement-review-copilot-<8hex>-round<N>-<pid>-<16hex>
        with _temp_dir() as td:
            tmpdir = Path(td)
            copilot, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "7", "Review-GitHub-Copilot.md", copilot, log_dir
            )
            state_dir = _parse_state_dir(result.stdout)
            self.assertRegex(
                state_dir.name,
                r"^implement-review-copilot-[0-9a-f]{8}-round7-\d+-[0-9a-f]{16}$",
                f"state-dir name pattern: {state_dir.name}",
            )

    def test_state_dir_contains_pre_mtime_and_timestamp_and_tail(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            copilot, prompt, log_dir = self._fresh_fixture(tmpdir)

            review = tmpdir / "Review-GitHub-Copilot.md"
            review.write_text("old review content\n", encoding="utf-8")
            old_mtime = int(review.stat().st_mtime)

            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-GitHub-Copilot.md", copilot, log_dir
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
            self.assertIn("mock-copilot: stdout", tail_content,
                          "tail must capture copilot stdout")
            self.assertIn("mock-copilot: stderr", tail_content,
                          "tail must capture copilot stderr (via 2>&1)")

    def test_pre_mtime_zero_when_review_file_missing(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            copilot, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Missing.md", copilot, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state_dir = _parse_state_dir(result.stdout)
            pre_mtime = (state_dir / "pre-mtime").read_text(
                encoding="utf-8"
            ).strip()
            self.assertEqual(pre_mtime, "0",
                             f"pre-mtime must be 0 when review file absent: "
                             f"{pre_mtime!r}")

    def test_prompt_referenced_via_dash_p_at_file(self) -> None:
        """Prompt must be passed as `-p "@<prompt-file>"`, never on stdin.

        The probe established that a long literal -p argument fails, so the
        dispatcher references the prompt FILE. `-p` must be immediately followed
        by an `@<path>` token whose path is the prompt file.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            copilot, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-GitHub-Copilot.md", copilot, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            self.assertIn("-p", args, f"copilot must receive -p: {args}")
            p_idx = args.index("-p")
            self.assertGreater(len(args), p_idx + 1,
                               f"-p must be followed by a value: {args}")
            at_value = args[p_idx + 1]
            self.assertTrue(
                at_value.startswith("@"),
                f"-p value must start with '@' (file reference): {at_value!r}",
            )
            self.assertTrue(
                at_value.endswith("prompt.txt") or "prompt.txt" in at_value,
                f"-p @value must reference the prompt file: {at_value!r}",
            )

    def test_prompt_body_not_passed_as_literal_arg(self) -> None:
        """No argv element may contain the prompt body text.

        Guards against a regression that passes the prompt content via -p
        instead of `@<file>` (which fails for real on long prompts).
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            copilot, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-GitHub-Copilot.md", copilot, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            for a in args:
                self.assertNotIn(
                    "REVIEW PROMPT body", a,
                    f"prompt body must not be passed as a literal arg: {a!r}",
                )

    def test_copilot_invoked_with_allow_list_and_no_sandbox(self) -> None:
        """The narrow review allow-list reaches copilot; no --sandbox leaks in."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            copilot, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-GitHub-Copilot.md", copilot, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            self.assertIn("--allow-tool=read", args,
                          f"copilot must be granted read: {args}")
            self.assertIn("--allow-tool=write", args,
                          f"copilot must be granted write (writes the review): {args}")
            self.assertIn("--allow-tool=shell(git:*)", args,
                          f"copilot must be granted git shell access: {args}")
            self.assertIn("--add-dir", args,
                          f"copilot must be granted repo dir access: {args}")
            self.assertIn("--no-ask-user", args,
                          f"copilot must run non-interactively: {args}")
            self.assertNotIn("--sandbox", args,
                             f"--sandbox is Codex-only; must not reach copilot: {args}")

    def test_copilot_invoked_with_working_dir(self) -> None:
        """`-C <repo>` must point copilot at the cwd (repo root)."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            copilot, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-GitHub-Copilot.md", copilot, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            self.assertIn("-C", args, f"copilot must receive -C: {args}")

    def test_exit_code_zero_propagation(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            copilot, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-GitHub-Copilot.md", copilot, log_dir,
                exit_code=0,
            )
            self.assertEqual(result.returncode, 0)

    def test_exit_code_nonzero_propagation(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            copilot, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-GitHub-Copilot.md", copilot, log_dir,
                exit_code=17,
            )
            self.assertEqual(
                result.returncode, 17,
                f"dispatch must propagate copilot exit 17, got {result.returncode}\n"
                f"STDERR:\n{result.stderr}",
            )

    def test_unique_state_dirs_across_consecutive_runs(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            copilot, prompt, log_dir = self._fresh_fixture(tmpdir)
            r1 = self._run_dispatch(
                tmpdir, prompt, "1", "Review-GitHub-Copilot.md", copilot, log_dir
            )
            r2 = self._run_dispatch(
                tmpdir, prompt, "1", "Review-GitHub-Copilot.md", copilot, log_dir
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
            copilot, _, log_dir = self._fresh_fixture(tmpdir)
            missing = tmpdir / "does-not-exist.txt"
            result = self._run_dispatch(
                tmpdir, missing, "1", "Review-GitHub-Copilot.md", copilot, log_dir
            )
            self.assertEqual(
                result.returncode, 2,
                f"missing prompt file must exit 2\nSTDERR:\n{result.stderr}",
            )

    def test_invalid_round_exits_two(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            copilot, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "abc", "Review-GitHub-Copilot.md", copilot, log_dir
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

    def test_gh_copilot_fallback_when_copilot_absent(self) -> None:
        """When standalone copilot is unresolvable, fall back to `gh copilot`.

        COPILOT_BIN points at an absent name so resolution fails; GH_BIN points
        at the mock. The dispatcher must then invoke `<gh> copilot ...`, so the
        mock's first argv element is the `copilot` subcommand.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            log_dir = tmpdir / "mock-log"
            log_dir.mkdir()
            gh_bin = _write_mock(
                tmpdir, "gh-mock",
                want_powershell_shim=(self.SHELL_KIND == "powershell"),
            )
            prompt = tmpdir / "prompt.txt"
            prompt.write_text("fallback prompt\n", encoding="utf-8")

            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-GitHub-Copilot.md",
                Path("copilot-absent-xyz123"), log_dir,
                extra_env={"GH_BIN": str(gh_bin)},
            )
            self.assertEqual(
                result.returncode, 0,
                f"gh fallback dispatch failed\nSTDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}",
            )
            args = self._read_args(log_dir)
            self.assertGreaterEqual(len(args), 1, f"gh mock got no args: {args}")
            self.assertEqual(
                args[0], "copilot",
                f"gh fallback must invoke the `copilot` subcommand first: {args}",
            )


@unittest.skipIf(
    sys.platform.startswith("win"),
    "bash skipped on Windows: Git Bash POSIX-translates env-var paths "
    "(C:\\...\\Temp\\tmpXX -> /tmp/tmpXX), which breaks path comparison "
    "from Python's Windows-path perspective. CI Linux covers this lane.",
)
@unittest.skipUnless(BASH, "bash not on PATH")
class DispatchCopilotBashTests(_DispatchContractMixin, unittest.TestCase):
    SHELL_KIND = "bash"


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win"),
    "PowerShell dispatch tests are Windows-only: dispatch-copilot.ps1 calls "
    "powershell.exe with -WindowStyle Hidden which is unsupported on "
    "PowerShell 7 for Linux/macOS. Production users on those platforms run "
    "dispatch-copilot.sh instead.",
)
class DispatchCopilotPowerShellTests(_DispatchContractMixin, unittest.TestCase):
    SHELL_KIND = "powershell"


class DispatchScriptsTracked(unittest.TestCase):
    """Sanity: both scripts must be present in the repo."""

    def test_sh_exists(self) -> None:
        self.assertTrue(DISPATCH_SH.exists(),
                        f"dispatch-copilot.sh missing: {DISPATCH_SH}")

    def test_ps1_exists(self) -> None:
        self.assertTrue(DISPATCH_PS1.exists(),
                        f"dispatch-copilot.ps1 missing: {DISPATCH_PS1}")


class DispatchCopilotFlagContract(unittest.TestCase):
    """Static contract: both dispatchers reference the prompt as a file via
    `-p "@..."`, pass the narrow review allow-list, set GIT_PAGER, run
    non-interactively, and never pass Codex's --sandbox flag. Freezes the
    contract so a future edit cannot silently drop a flag while leaving runtime
    behavior plausible-looking.
    """

    def _both(self) -> list[str]:
        return [
            DISPATCH_SH.read_text(encoding="utf-8"),
            DISPATCH_PS1.read_text(encoding="utf-8"),
        ]

    def test_prompt_file_reference(self) -> None:
        for text in self._both():
            self.assertIn("-p ", text, "dispatcher must pass -p")
            self.assertIn('"@', text, "dispatcher must reference the prompt as @<file>")

    def test_allow_list_present(self) -> None:
        for text in self._both():
            self.assertIn("--allow-tool=read", text)
            self.assertIn("--allow-tool=write", text)
            self.assertIn("shell(git:*)", text)
            self.assertIn("--add-dir", text)
            self.assertIn("--no-ask-user", text)

    def test_copilot_stays_offline(self) -> None:
        """The Copilot fallback backend has no web access: URL permission is
        all-or-nothing via url(), too broad for an auto-launched reviewer, so it
        is withheld. Pin the offline contract so a future edit does not silently
        grant network access to this backend."""
        for text in self._both():
            self.assertNotIn("url(", text,
                             "dispatch-copilot must not grant url() web access")
            self.assertNotIn("--allow-all-tools", text,
                             "dispatch-copilot must not grant all tools")
            self.assertNotIn("--allow-all-urls", text,
                             "dispatch-copilot must not grant all URLs")

    def test_git_pager_neutralized(self) -> None:
        for text in self._both():
            self.assertIn("GIT_PAGER", text,
                          "dispatcher must neutralize the git pager (GIT_PAGER=cat)")

    def test_no_sandbox_flag(self) -> None:
        for text in self._both():
            self.assertNotIn("--sandbox", text,
                             "--sandbox is Codex-only; must not appear in dispatch-copilot")

    def test_gh_fallback_present(self) -> None:
        for text in self._both():
            self.assertIn("GH_BIN", text,
                          "dispatcher must support the gh copilot fallback")


if __name__ == "__main__":
    unittest.main()
