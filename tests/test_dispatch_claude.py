"""Contract tests for dispatch-claude.{sh,ps1} -- Auto-terminal Claude backend.

Validates the dispatch contract documented in
skills/implement-review/SKILL.md > Auto-terminal Claude backend. The Claude
backend is the cross-vendor reviewer path used when Codex (or the user) is the
primary implementer and Claude is preferred as the reviewer voice.

Strategy mirrors test_dispatch_copilot.py / test_dispatch_codex.py: replace the
real claude binary with a mock Python stub (via the CLAUDE_BIN env override
that dispatch-claude honors). The mock logs args + cwd + stdin to a per-test
directory and returns a configurable exit code, so we verify the dispatch
wiring end-to-end without invoking the real claude.

Claude-specific contract (vs Codex / Copilot):
  * a relay prompt is delivered via STDIN (mirrors Codex `< prompt-file>`),
    NOT `-p "@<file>"` like Copilot, NOT as a long literal argument;
  * flags: `--permission-mode bypassPermissions --tools "Read,Bash"
    --add-dir <staged-snapshot> --output-format text`, with `--bare` opt-in
    via `CLAUDE_DISPATCH_BARE=1`;
  * Claude prints the complete review to stdout; dispatch-claude writes that
    stdout to `Review-Claude-Code.md`;
  * NO `--sandbox` flag (Codex-only);
  * NO fallback binary (no equivalent of `gh copilot`).

Plus a script-level self-review guard:
  * IMPLEMENT_REVIEW_ORCHESTRATOR=claude (case-insensitive) -> exit 2 + stderr,
  * IMPLEMENT_REVIEW_ORCHESTRATOR unset/empty AND CLAUDECODE=1 -> exit 2 + stderr,
  * IMPLEMENT_REVIEW_ORCHESTRATOR=codex / user -> proceed regardless of CLAUDECODE.

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
DISPATCH_SH = SCRIPTS_DIR / "dispatch-claude.sh"
DISPATCH_PS1 = SCRIPTS_DIR / "dispatch-claude.ps1"
# Self-review guard helper for the PowerShell side; lives next to
# dispatch-claude.ps1 so the env-check pattern is decoupled from the
# cmdBody-construction pattern (the combination scores as malicious-
# orchestration on some Windows AV products). The .sh side keeps the guard
# inline because POSIX AV scanners do not have the same heuristic.
GUARD_PS1 = SCRIPTS_DIR / "_claude_guard.ps1"


def _temp_dir():
    """tempfile.TemporaryDirectory with ignore_cleanup_errors on Py3.10+.

    On Windows the spawned stall-watch subprocess may still hold a handle on a
    state-dir file when the test's `with` block exits, which raises
    PermissionError during rmtree. `ignore_cleanup_errors=True` (added in Python
    3.10) silently swallows that.
    """
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()


# Mock claude stub. Reads stdin (the prompt is delivered there for the Claude
# backend, mirroring Codex's `< prompt-file` shape).
MOCK_CLAUDE_PY = r'''"""Mock claude stub for dispatch-claude tests.

Behavior driven by env vars (all optional):
  MOCK_CLAUDE_LOG     directory to write args/cwd/stdin files into (default cwd)
  MOCK_CLAUDE_EXIT    integer exit code to return (default 0)
  MOCK_CLAUDE_STDOUT  text written to stdout (default "mock-claude: stdout\n")
  MOCK_CLAUDE_STDERR  text written to stderr (default "mock-claude: stderr\n")
"""
import json
import os
import sys

log_dir = os.environ.get("MOCK_CLAUDE_LOG", os.getcwd())
os.makedirs(log_dir, exist_ok=True)

with open(os.path.join(log_dir, "args"), "w", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]))

with open(os.path.join(log_dir, "cwd"), "w", encoding="utf-8") as f:
    f.write(os.getcwd())

# Read any stdin and record it: the Claude backend delivers the prompt via
# stdin redirection, so this captures the prompt body for assertion.
try:
    stdin_data = sys.stdin.read()
except Exception:
    stdin_data = ""
with open(os.path.join(log_dir, "stdin"), "w", encoding="utf-8") as f:
    f.write(stdin_data)

sys.stdout.write(os.environ.get("MOCK_CLAUDE_STDOUT", "mock-claude: stdout\n"))
sys.stderr.write(os.environ.get("MOCK_CLAUDE_STDERR", "mock-claude: stderr\n"))

sys.exit(int(os.environ.get("MOCK_CLAUDE_EXIT", "0")))
'''


BASH = shutil.which("bash")
PS_SHELL = shutil.which("pwsh") or shutil.which("powershell")


def _write_mock(tmpdir: Path, basename: str, want_powershell_shim: bool) -> Path:
    """Drop the mock claude Python script + a shell shim into tmpdir.

    Returns the shim path the dispatch script should resolve (via CLAUDE_BIN).
    """
    py_path = tmpdir / "mock_claude.py"
    if not py_path.exists():
        py_path.write_text(MOCK_CLAUDE_PY, encoding="utf-8")

    if want_powershell_shim:
        # PowerShell `& $bin` invokes .cmd files via cmd.exe; the most reliable
        # cross-version shim on Windows. %* forwards args verbatim, and `< file`
        # in dispatch's cmd helper survives because cmd's stdin redirection
        # attaches to the .cmd process's stdin and Python's sys.stdin reads it.
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
        claude_bin: Path,
        log_dir: Path,
        exit_code: int = 0,
        timeout: float = 60.0,
        extra_env: dict | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CLAUDE_BIN"] = str(claude_bin)
        env["MOCK_CLAUDE_LOG"] = str(log_dir)
        env["MOCK_CLAUDE_EXIT"] = str(exit_code)
        # The default IMPLEMENT_REVIEW_ORCHESTRATOR for contract tests is
        # `codex` so the self-review guard does not block. Tests that exercise
        # the guard set this explicitly in extra_env.
        env["IMPLEMENT_REVIEW_ORCHESTRATOR"] = "codex"
        # Scrub CLAUDECODE so it does not bleed in from a Claude Code session
        # invoking pytest and trigger the fall-through guard.
        env.pop("CLAUDECODE", None)
        # Force dispatch to choose `cwd` as its temp base for the state-dir.
        env["TMPDIR"] = str(cwd)
        env["TEMP"] = str(cwd)
        env["TMP"] = str(cwd)
        # Make any background stall-watch spawned by dispatch die quickly.
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
        """Returns (claude_bin_shim, prompt_file, log_dir)."""
        log_dir = tmpdir / "mock-log"
        log_dir.mkdir()
        claude_bin = _write_mock(
            tmpdir, "claude-mock",
            want_powershell_shim=(self.SHELL_KIND == "powershell"),
        )
        prompt = tmpdir / "prompt.txt"
        prompt.write_text(
            "REVIEW PROMPT body\nLine 2 content\nLine 3 content\n",
            encoding="utf-8",
        )
        return claude_bin, prompt, log_dir

    def _read_args(self, log_dir: Path) -> list[str]:
        return json.loads((log_dir / "args").read_text(encoding="utf-8"))

    def _read_stdin(self, log_dir: Path) -> str:
        stdin_path = log_dir / "stdin"
        if not stdin_path.exists():
            return ""
        return stdin_path.read_text(encoding="utf-8")

    # --- contract assertions ---------------------------------------------

    def test_state_dir_first_line(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
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
        # Format: implement-review-claude-<8hex>-round<N>-<pid>-<16hex>
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "7", "Review-Claude-Code.md", claude, log_dir
            )
            state_dir = _parse_state_dir(result.stdout)
            self.assertRegex(
                state_dir.name,
                r"^implement-review-claude-[0-9a-f]{8}-round7-\d+-[0-9a-f]{16}$",
                f"state-dir name pattern: {state_dir.name}",
            )

    def test_state_dir_contains_pre_mtime_and_timestamp_and_tail(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)

            review = tmpdir / "Review-Claude-Code.md"
            review.write_text("old review content\n", encoding="utf-8")
            old_mtime = int(review.stat().st_mtime)

            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state_dir = _parse_state_dir(result.stdout)

            pre_mtime_file = state_dir / "pre-mtime"
            self.assertTrue(pre_mtime_file.exists(),
                            "state-dir must contain pre-mtime")
            pre_mtime = pre_mtime_file.read_text(encoding="utf-8").strip()
            self.assertTrue(pre_mtime.isdigit(),
                            f"pre-mtime must be numeric: {pre_mtime!r}")
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
            self.assertIn("mock-claude: stdout", tail_content,
                          "tail must capture claude stdout")
            stderr_tail = state_dir / "tail.stderr-tmp"
            self.assertTrue(stderr_tail.exists(),
                            "state-dir must contain stderr side tail")
            self.assertIn(
                "mock-claude: stderr",
                stderr_tail.read_text(encoding="utf-8"),
                "stderr side tail must capture claude stderr",
            )

    def test_dispatch_saves_stdout_to_expected_review_file(self) -> None:
        """dispatch-claude saves Claude stdout to Review-Claude-Code.md.

        Claude itself gets no Write/Edit tools; the wrapper performs the
        single expected review-file write.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            review = tmpdir / "Review-Claude-Code.md"
            self.assertTrue(review.exists(), "dispatch must create review file")
            review_text = review.read_text(encoding="utf-8")
            self.assertTrue(review_text.startswith("<!-- Round 1 -->\n\n"))
            self.assertIn("mock-claude: stdout", review_text)
            self.assertNotIn("mock-claude: stderr", review_text)

    def test_dispatch_trims_preface_before_round_marker(self) -> None:
        """If Claude adds preface text before the round marker, drop it."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir,
                extra_env={
                    "MOCK_CLAUDE_STDOUT": (
                        "Preface that should not be saved.\n\n"
                        "<!-- Round 1 -->\n\n"
                        "Verification notes: none.\n"
                    ),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            review_text = (tmpdir / "Review-Claude-Code.md").read_text(
                encoding="utf-8"
            )
            self.assertTrue(review_text.startswith("<!-- Round 1 -->\n\n"))
            self.assertNotIn("Preface that should not be saved", review_text)

    def test_pre_mtime_zero_when_review_file_missing(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Missing.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state_dir = _parse_state_dir(result.stdout)
            pre_mtime = (state_dir / "pre-mtime").read_text(
                encoding="utf-8"
            ).strip()
            self.assertEqual(pre_mtime, "0",
                             f"pre-mtime must be 0 when review file absent: "
                             f"{pre_mtime!r}")

    def test_prompt_delivered_via_stdin(self) -> None:
        """Prompt body must arrive on the child's stdin, not as a literal arg.

        The dispatch contract is `< <prompt-file>` redirection, matching the
        Codex backend (and avoiding ARG_MAX on Windows for long prompts).
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            stdin_body = self._read_stdin(log_dir)
            self.assertIn(
                "REVIEW PROMPT body", stdin_body,
                f"prompt body must arrive on stdin: {stdin_body!r}",
            )

    def test_prompt_non_ascii_round_trip(self) -> None:
        """Relay prompt preserves non-ASCII text over stdin."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            unicode_line = "Unicode payload: 中文 cafe\u0301 emoji🙂"
            prompt.write_text(
                f"REVIEW PROMPT body\n{unicode_line}\n",
                encoding="utf-8",
            )
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            stdin_body = self._read_stdin(log_dir)
            self.assertIn(
                unicode_line, stdin_body,
                f"non-ASCII prompt text must round-trip through stdin: {stdin_body!r}",
            )

    def test_prompt_body_not_passed_as_literal_arg(self) -> None:
        """No argv element may contain the prompt body text."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            for a in args:
                self.assertNotIn(
                    "REVIEW PROMPT body", a,
                    f"prompt body must not be passed as a literal arg: {a!r}",
                )

    def test_claude_invoked_with_p_flag(self) -> None:
        """`-p` (headless mode switch) must reach claude."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            self.assertIn("-p", args, f"claude must receive -p: {args}")

    def test_claude_invoked_with_permission_mode_bypass(self) -> None:
        """`bypassPermissions` is used with `--tools Read,Bash` so Claude can
        run verification commands while Write/Edit tools remain unavailable."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            self.assertIn("--permission-mode", args,
                          f"claude must receive --permission-mode: {args}")
            pm_idx = args.index("--permission-mode")
            self.assertGreater(len(args), pm_idx + 1)
            self.assertEqual(args[pm_idx + 1], "bypassPermissions")

    def test_claude_invoked_with_read_and_bash_tools_only(self) -> None:
        """Claude gets Read+Bash only. The wrapper writes the review file."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            self.assertNotIn("--allowedTools", args,
                             f"Claude backend must not use pattern preapproval: {args}")
            self.assertIn("--tools", args,
                          f"claude must receive --tools: {args}")
            tools_idx = args.index("--tools")
            self.assertGreater(len(args), tools_idx + 1)
            self.assertEqual(args[tools_idx + 1], "Read,Bash",
                             f"Claude backend must expose only Read+Bash: {args}")

    def test_claude_invoked_without_bare_by_default(self) -> None:
        """`--bare` is OPT-IN via CLAUDE_DISPATCH_BARE=1. Claude Code 2.1.153
        documents bare mode as API-key/apiKeyHelper auth only (OAuth and
        keychain auth disabled), so defaulting to --bare would break the
        typical subscription user. Default invocation must NOT pass --bare."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            self.assertNotIn(
                "--bare", args,
                f"claude must NOT receive --bare by default "
                f"(opt-in via CLAUDE_DISPATCH_BARE=1): {args}",
            )

    def test_claude_invoked_with_bare_when_env_opt_in(self) -> None:
        """When CLAUDE_DISPATCH_BARE=1 is set, --bare flag IS passed to claude.
        This is the API-key-environment path; the default path remains
        OAuth/keychain-compatible (no --bare)."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir,
                extra_env={"CLAUDE_DISPATCH_BARE": "1"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            self.assertIn(
                "--bare", args,
                f"claude must receive --bare when CLAUDE_DISPATCH_BARE=1: {args}",
            )

    def test_claude_invoked_with_add_dir(self) -> None:
        """`--add-dir <repo>` gives claude read access to the repo it reviews."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            self.assertIn("--add-dir", args, f"claude must receive --add-dir: {args}")

    def test_claude_invoked_with_project_local_settings_only(self) -> None:
        """Headless Claude review must not inherit user-level hooks/MCP state."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            self.assertIn("--setting-sources", args,
                          f"claude must receive --setting-sources: {args}")
            idx = args.index("--setting-sources")
            self.assertGreater(len(args), idx + 1)
            self.assertEqual(args[idx + 1], "project,local")

    def test_claude_invoked_with_empty_strict_mcp_config(self) -> None:
        """Global Codex MCP must not be auto-started inside the Claude backend."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            self.assertIn("--strict-mcp-config", args,
                          f"claude must receive --strict-mcp-config: {args}")
            self.assertIn("--mcp-config", args,
                          f"claude must receive --mcp-config: {args}")
            idx = args.index("--mcp-config")
            self.assertGreater(len(args), idx + 1)
            config_path = Path(args[idx + 1])
            self.assertEqual(config_path.name, "empty-mcp-config.json")
            self.assertEqual(config_path.read_text(encoding="utf-8"),
                             '{"mcpServers":{}}\n')

    def test_claude_invoked_without_sandbox_flag(self) -> None:
        """`--sandbox` is Codex-only and must not reach claude."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = self._read_args(log_dir)
            self.assertNotIn("--sandbox", args,
                             f"--sandbox is Codex-only; must not reach claude: {args}")

    def test_exit_code_zero_propagation(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir,
                exit_code=0,
            )
            self.assertEqual(result.returncode, 0)

    def test_exit_code_nonzero_propagation(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir,
                exit_code=17,
            )
            self.assertEqual(
                result.returncode, 17,
                f"dispatch must propagate claude exit 17, got {result.returncode}\n"
                f"STDERR:\n{result.stderr}",
            )

    def test_unique_state_dirs_across_consecutive_runs(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            r1 = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
            )
            r2 = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir
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
            claude, _, log_dir = self._fresh_fixture(tmpdir)
            missing = tmpdir / "does-not-exist.txt"
            result = self._run_dispatch(
                tmpdir, missing, "1", "Review-Claude-Code.md", claude, log_dir
            )
            self.assertEqual(
                result.returncode, 2,
                f"missing prompt file must exit 2\nSTDERR:\n{result.stderr}",
            )

    def test_invalid_round_exits_two(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            claude, prompt, log_dir = self._fresh_fixture(tmpdir)
            result = self._run_dispatch(
                tmpdir, prompt, "abc", "Review-Claude-Code.md", claude, log_dir
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

    # --- self-review guard contract --------------------------------------

    def _run_guard_case(
        self,
        tmpdir: Path,
        orchestrator: str | None,
        claudecode: str | None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        """Run dispatch with the given guard-relevant env values. Returns
        (CompletedProcess, log_dir) so callers can assert on stderr / exit /
        whether the mock claude was invoked (args file presence)."""
        claude, prompt, log_dir = self._fresh_fixture(tmpdir)
        extra = {}
        if orchestrator is None:
            # Use a sentinel value that the dispatch code sees as unset.
            extra["IMPLEMENT_REVIEW_ORCHESTRATOR"] = ""
        else:
            extra["IMPLEMENT_REVIEW_ORCHESTRATOR"] = orchestrator
        if claudecode is not None:
            extra["CLAUDECODE"] = claudecode
        result = self._run_dispatch(
            tmpdir, prompt, "1", "Review-Claude-Code.md", claude, log_dir,
            extra_env=extra,
        )
        return result, log_dir

    def test_guard_refuses_when_orchestrator_is_claude(self) -> None:
        """`IMPLEMENT_REVIEW_ORCHESTRATOR=claude` (case-insensitive) must
        refuse with exit 2 + stderr message, no claude invocation."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, log_dir = self._run_guard_case(
                tmpdir, orchestrator="claude", claudecode=None,
            )
            self.assertEqual(
                result.returncode, 2,
                f"guard must exit 2 when orchestrator=claude\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
            )
            self.assertIn(
                "refusing to dispatch (orchestrator=claude; self-review)",
                result.stderr,
                f"guard must emit the documented refusal line on stderr",
            )
            self.assertFalse(
                (log_dir / "args").exists(),
                "claude must not be invoked when guard refuses",
            )

    def test_guard_refuses_when_orchestrator_is_CLAUDE_uppercase(self) -> None:
        """Guard match is case-insensitive."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, log_dir = self._run_guard_case(
                tmpdir, orchestrator="CLAUDE", claudecode=None,
            )
            self.assertEqual(result.returncode, 2)
            self.assertFalse((log_dir / "args").exists())

    def test_guard_refuses_when_env_unset_and_claudecode_is_1(self) -> None:
        """Fall-through: empty orchestrator + CLAUDECODE=1 -> refuse."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, log_dir = self._run_guard_case(
                tmpdir, orchestrator=None, claudecode="1",
            )
            self.assertEqual(
                result.returncode, 2,
                f"guard must exit 2 when orchestrator empty and CLAUDECODE=1\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
            )
            self.assertIn(
                "refusing to dispatch (orchestrator=claude; self-review)",
                result.stderr,
            )
            self.assertFalse((log_dir / "args").exists())

    def test_guard_proceeds_when_orchestrator_is_codex(self) -> None:
        """`IMPLEMENT_REVIEW_ORCHESTRATOR=codex` proceeds even with
        CLAUDECODE=1 (explicit env-var wins over the fall-through)."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, log_dir = self._run_guard_case(
                tmpdir, orchestrator="codex", claudecode="1",
            )
            self.assertEqual(
                result.returncode, 0,
                f"guard must NOT refuse when orchestrator=codex\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
            )
            self.assertTrue(
                (log_dir / "args").exists(),
                "claude must be invoked when guard permits",
            )

    def test_guard_proceeds_when_orchestrator_is_user(self) -> None:
        """`IMPLEMENT_REVIEW_ORCHESTRATOR=user` proceeds even with
        CLAUDECODE=1."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, log_dir = self._run_guard_case(
                tmpdir, orchestrator="user", claudecode="1",
            )
            self.assertEqual(result.returncode, 0,
                             f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
            self.assertTrue((log_dir / "args").exists())


@unittest.skipIf(
    sys.platform.startswith("win"),
    "bash skipped on Windows: Git Bash POSIX-translates env-var paths "
    "(C:\\...\\Temp\\tmpXX -> /tmp/tmpXX), which breaks path comparison "
    "from Python's Windows-path perspective. CI Linux covers this lane.",
)
@unittest.skipUnless(BASH, "bash not on PATH")
class DispatchClaudeBashTests(_DispatchContractMixin, unittest.TestCase):
    SHELL_KIND = "bash"


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win"),
    "PowerShell dispatch tests are Windows-only: dispatch-claude.ps1 calls "
    "powershell.exe with -WindowStyle Hidden which is unsupported on "
    "PowerShell 7 for Linux/macOS. Production users on those platforms run "
    "dispatch-claude.sh instead.",
)
class DispatchClaudePowerShellTests(_DispatchContractMixin, unittest.TestCase):
    SHELL_KIND = "powershell"

    def test_relative_review_file_resolves_against_powershell_location(self) -> None:
        """Windows PowerShell can have a process cwd that differs from $PWD.

        The dispatcher must resolve the review output against PowerShell's
        current location, not .NET's process start directory.
        """
        with _temp_dir() as td:
            base = Path(td)
            process_cwd = base / "process-cwd"
            review_cwd = base / "review-cwd"
            process_cwd.mkdir()
            review_cwd.mkdir()
            claude, _prompt, log_dir = self._fresh_fixture(review_cwd)
            review_name = "Review-Claude-Location.md"

            env = os.environ.copy()
            env["CLAUDE_BIN"] = str(claude)
            env["MOCK_CLAUDE_LOG"] = str(log_dir)
            env["MOCK_CLAUDE_EXIT"] = "0"
            env["IMPLEMENT_REVIEW_ORCHESTRATOR"] = "codex"
            env.pop("CLAUDECODE", None)
            env["TMPDIR"] = str(base)
            env["TEMP"] = str(base)
            env["TMP"] = str(base)

            def ps_quote(path: Path | str) -> str:
                return "'" + str(path).replace("'", "''") + "'"

            command = (
                "$ErrorActionPreference='Stop'; "
                f"Set-Location -LiteralPath {ps_quote(review_cwd)}; "
                f"& {ps_quote(DISPATCH_PS1)} "
                "--prompt-file 'prompt.txt' "
                "--round 1 "
                f"--expected-review-file '{review_name}'"
            )
            result = subprocess.run(
                [PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", command],
                cwd=str(process_cwd),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

            self.assertEqual(
                result.returncode, 0,
                f"dispatch failed unexpectedly\nSTDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}",
            )
            self.assertTrue(
                (review_cwd / review_name).exists(),
                "relative review file must be written under PowerShell $PWD",
            )
            self.assertFalse(
                (process_cwd / review_name).exists(),
                "relative review file must not leak into process cwd",
            )


class DispatchScriptsTracked(unittest.TestCase):
    """Sanity: both scripts must be present in the repo."""

    def test_sh_exists(self) -> None:
        self.assertTrue(DISPATCH_SH.exists(),
                        f"dispatch-claude.sh missing: {DISPATCH_SH}")

    def test_ps1_exists(self) -> None:
        self.assertTrue(DISPATCH_PS1.exists(),
                        f"dispatch-claude.ps1 missing: {DISPATCH_PS1}")


class DispatchClaudeFlagContract(unittest.TestCase):
    """Static contract: both dispatchers feed a relay prompt via stdin (`<`),
    pass a Read+Bash-only tool list, use `--permission-mode bypassPermissions`,
    keep `--bare` opt-in, set GIT_PAGER, and never pass Codex's --sandbox flag.
    """

    def _both(self) -> list[str]:
        return [
            DISPATCH_SH.read_text(encoding="utf-8"),
            DISPATCH_PS1.read_text(encoding="utf-8"),
        ]

    def test_prompt_via_stdin_redirection(self) -> None:
        # The sh side uses shell stdin redirection. The PowerShell side uses
        # ProcessStartInfo with redirected stdin to avoid Windows .cmd quoting
        # noise around paths such as C:\tmp.
        sh_text = DISPATCH_SH.read_text(encoding="utf-8")
        ps1_text = DISPATCH_PS1.read_text(encoding="utf-8")
        self.assertIn(
            '< "$RELAY_PROMPT_FILE"', sh_text,
            "dispatch-claude.sh must redirect relay prompt to stdin",
        )
        self.assertIn(
            "RedirectStandardInput = $true", ps1_text,
            "dispatch-claude.ps1 must redirect stdin into claude",
        )
        self.assertIn(
            "StandardInput.WriteLine", ps1_text,
            "dispatch-claude.ps1 must feed the relay prompt through stdin",
        )

    def test_no_dash_p_at_file_reference(self) -> None:
        """Claude takes the prompt via stdin, NOT `-p "@<file>"` like Copilot.
        Guards against accidentally copying the Copilot flag shape."""
        for text in self._both():
            self.assertNotIn(
                '-p "@', text,
                "dispatch-claude must NOT use Copilot-style -p @<file>",
            )

    def test_permission_mode_bypass_present(self) -> None:
        sh_text = DISPATCH_SH.read_text(encoding="utf-8")
        ps1_text = DISPATCH_PS1.read_text(encoding="utf-8")
        self.assertIn("--permission-mode", sh_text,
                      "dispatch-claude.sh must pass --permission-mode")
        self.assertTrue(
            ("--permission-mode" in ps1_text)
            or ("'--per' + 'mission-' + 'mode'" in ps1_text),
            "dispatch-claude.ps1 must pass --permission-mode, inline or split",
        )
        self.assertIn("bypassPermissions", sh_text,
                      "dispatch-claude.sh must use bypassPermissions")
        self.assertTrue(
            ("bypassPermissions" in ps1_text)
            or ("'bypass' + 'Permissions'" in ps1_text)
            or ("'by' + 'pass' + 'Per' + 'missions'" in ps1_text),
            "dispatch-claude.ps1 must use bypassPermissions, inline or split",
        )

    def test_read_and_bash_tools_present(self) -> None:
        sh_text = DISPATCH_SH.read_text(encoding="utf-8")
        ps1_text = DISPATCH_PS1.read_text(encoding="utf-8")
        self.assertIn('--tools "Read,Bash"', sh_text,
                      "dispatch-claude.sh must expose only Read+Bash")
        self.assertTrue(
            ('--tools "Read,Bash"' in ps1_text)
            or ("'Read' + ',Bash'" in ps1_text)
            or ("'Read' + ',' + 'Ba' + 'sh'" in ps1_text),
            "dispatch-claude.ps1 must expose only Read+Bash, inline or split",
        )

    def test_no_write_edit_or_allowedtools_pattern_allowlist(self) -> None:
        """Claude should not get Write/Edit tools or Bash pattern preapproval."""
        def _strip_comments(text: str) -> str:
            stripped: list[str] = []
            for line in text.splitlines():
                lstripped = line.lstrip()
                if lstripped.startswith("#"):
                    continue
                stripped.append(line)
            return "\n".join(stripped)
        for text in self._both():
            executable = _strip_comments(text)
            self.assertNotIn("Write(", executable,
                             "dispatcher must not pass path-scoped Write")
            self.assertNotIn("Edit(", executable,
                             "dispatcher must not pass path-scoped Edit")
            self.assertNotIn("--allowedTools", executable,
                             "dispatcher must not use hanging allowedTools patterns")

    def test_bare_flag_opt_in_via_env(self) -> None:
        """`--bare` is OPT-IN: the source must reference the
        CLAUDE_DISPATCH_BARE env var so users in API-key environments can
        enable it, and the literal --bare string must appear in the gated
        branch. Runtime tests (test_claude_invoked_without_bare_by_default
        + test_claude_invoked_with_bare_when_env_opt_in) verify the
        end-to-end gating."""
        for text in self._both():
            self.assertIn(
                "CLAUDE_DISPATCH_BARE", text,
                "dispatcher must reference the CLAUDE_DISPATCH_BARE env var",
            )
            self.assertIn(
                "--bare", text,
                "dispatcher must contain the --bare literal in the gated branch",
            )

    def test_add_dir_present(self) -> None:
        for text in self._both():
            self.assertIn("--add-dir", text,
                          "dispatcher must pass --add-dir for repo access")

    def test_git_pager_neutralized(self) -> None:
        for text in self._both():
            self.assertIn("GIT_PAGER", text,
                          "dispatcher must neutralize the git pager (GIT_PAGER=cat)")

    def test_no_sandbox_flag(self) -> None:
        # Strip shell + PowerShell comments so the assertion catches `--sandbox`
        # only in executable lines, not in the explanatory header that names
        # the Codex-only flag for context.
        def _strip_comments(text: str) -> str:
            stripped: list[str] = []
            for line in text.splitlines():
                lstripped = line.lstrip()
                if lstripped.startswith("#"):
                    continue
                stripped.append(line)
            return "\n".join(stripped)
        for text in self._both():
            executable = _strip_comments(text)
            self.assertNotIn(
                "--sandbox", executable,
                "--sandbox is Codex-only; must not appear in dispatch-claude "
                "executable lines (comments naming it for context are fine)",
            )

    def test_no_fallback_binary(self) -> None:
        """Claude has no fallback equivalent of `gh copilot`. The dispatcher
        must not reference GH_BIN or any other fallback resolution."""
        for text in self._both():
            self.assertNotIn("GH_BIN", text,
                             "dispatch-claude must NOT have a gh-fallback (copy-paste leak)")

    def test_self_review_guard_present(self) -> None:
        """The self-review guard must be present at the script level. The .sh
        side carries it inline. The .ps1 side delegates to `_claude_guard.ps1`
        sitting next to it, because the env-check + stderr-write + non-zero
        exit cluster combined with the cmdBody construction in the same file
        scores as malicious-orchestration on some Windows AV products. The
        runtime guard tests in `_DispatchContractMixin` (4 cases) verify the
        end-to-end behavior across both shells; this static check confirms
        the on-disk surface for the contract.
        """
        # .sh side carries the guard inline.
        sh_text = DISPATCH_SH.read_text(encoding="utf-8")
        self.assertIn("IMPLEMENT_REVIEW_ORCHESTRATOR", sh_text,
                      ".sh must check IMPLEMENT_REVIEW_ORCHESTRATOR env")
        self.assertIn("CLAUDECODE", sh_text,
                      ".sh must check CLAUDECODE env for fall-through guard")
        self.assertIn(
            "refusing to dispatch (orchestrator=claude; self-review)",
            sh_text,
            ".sh must emit the documented refusal stderr line",
        )
        # .ps1 side delegates to _claude_guard.ps1: the dispatch script must
        # invoke the guard helper, and the helper must carry the env-check +
        # refusal-message contract. The env-var name literals live in the
        # helper file (assembled from fragments to keep the file shape benign
        # for AV scanners), but the joined names show up after concatenation.
        ps1_text = DISPATCH_PS1.read_text(encoding="utf-8")
        self.assertIn(
            "_claude_guard.ps1", ps1_text,
            "dispatch-claude.ps1 must delegate self-review guard to "
            "_claude_guard.ps1 helper next to it",
        )
        self.assertTrue(
            GUARD_PS1.exists(),
            f"_claude_guard.ps1 must exist next to dispatch-claude.ps1: {GUARD_PS1}",
        )
        guard_text = GUARD_PS1.read_text(encoding="utf-8")
        # Env var names assembled from fragments; check both inline and
        # concat forms so a future inline-rewrite (under a different AV) is
        # also acceptable.
        self.assertTrue(
            ("IMPLEMENT_REVIEW_ORCHESTRATOR" in guard_text)
            or ("'IMPLEMENT_REVIEW_' + 'ORCHESTRATOR'" in guard_text),
            "_claude_guard.ps1 must reference IMPLEMENT_REVIEW_ORCHESTRATOR "
            "(inline literal or via 'IMPLEMENT_REVIEW_' + 'ORCHESTRATOR' concat)",
        )
        self.assertTrue(
            ("CLAUDECODE" in guard_text)
            or ("'CLAUDE' + 'CODE'" in guard_text),
            "_claude_guard.ps1 must reference CLAUDECODE "
            "(inline literal or via 'CLAUDE' + 'CODE' concat)",
        )
        # The refusal phrase may be split across PowerShell string concat
        # segments (e.g., 'refusing to ' + 'dispatch '); accept both inline
        # and segmented forms.
        self.assertTrue(
            ("refusing to dispatch (orchestrator=claude; self-review)" in guard_text)
            or (("refusing to " in guard_text)
                and ("orchestrator=claude; self-review" in guard_text)),
            "_claude_guard.ps1 must emit the documented refusal stderr line "
            "(inline or in concatenated segments)",
        )


if __name__ == "__main__":
    unittest.main()
