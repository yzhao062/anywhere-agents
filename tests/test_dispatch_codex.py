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

# tests/ is on sys.path under `unittest discover -s tests` but not under
# `python -m unittest tests.<module>`, which validate.yml uses for the
# Sentinel redaction smoke. Put it there before the sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import agent_models  # noqa: E402  the resolver the dispatchers read the registry through

SCRIPTS_DIR = ROOT / "skills" / "implement-review" / "scripts"
DISPATCH_SH = Path(os.environ.get(
    "TEST_DISPATCH_CODEX_SH", SCRIPTS_DIR / "dispatch-codex.sh"
))
DISPATCH_PS1 = Path(os.environ.get(
    "TEST_DISPATCH_CODEX_PS1", SCRIPTS_DIR / "dispatch-codex.ps1"
))
GUARD_PS1 = SCRIPTS_DIR / "_codex_guard.ps1"

# A tmp registry keeps these tests independent of the live values: they check
# that the dispatcher follows whatever the registry says, not what it says today.
REGISTRY_NAME = "agent-models.json"
MISSING_REGISTRY_NAME = "no-such-registry.json"


def _write_registry(directory: Path, codex=("fixture-codex", "low"), reviewer=None) -> Path:
    payload = {
        "schema": 1,
        "families": {
            "claude": {"model": "fixture-claude", "effort": "medium"},
            "codex": {"model": codex[0], "effort": codex[1]},
            "agy": {"model": "fixture-agy", "effort": "high"},
        },
        "roles": {},
    }
    if reviewer:
        payload["roles"]["reviewer"] = {"codex": {"model": reviewer[0], "effort": reviewer[1]}}
    path = directory / REGISTRY_NAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _registry_reviewer(directory: Path) -> tuple[str, str]:
    """The reviewer model and effort the tmp registry resolves to."""
    row = agent_models.resolve("codex", "reviewer", agent_models.load(directory / REGISTRY_NAME))
    return row["model"], row["effort"]


def _codex_header(model: str, effort: str) -> str:
    """The lines `codex exec` prints on stderr before it starts working."""
    return f"model: {model}\nprovider: openai\nreasoning effort: {effort}\n"

# A wait sized on an idle machine has failed under load three times in two
# releases, most recently on this file in both repositories on one push,
# green again on a rerun with no change. A deadline is only spent when the
# event never arrives, so the budget is generous and a passing run does not
# pay for it. Raise it on a slow or heavily loaded runner.
STALL_WAIT_TIMEOUT_SECONDS = float(
    os.environ.get("STALL_WAIT_TIMEOUT_SECONDS", "30")
)


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
import time

log_dir = os.environ.get("MOCK_CODEX_LOG", os.getcwd())
os.makedirs(log_dir, exist_ok=True)

with open(os.path.join(log_dir, "args"), "w", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]))

stdin_data = sys.stdin.read()
with open(os.path.join(log_dir, "stdin"), "w", encoding="utf-8", newline="") as f:
    f.write(stdin_data)

with open(os.path.join(log_dir, "cwd"), "w", encoding="utf-8") as f:
    f.write(os.getcwd())

with open(os.path.join(log_dir, "dispatch-env"), "w", encoding="utf-8") as f:
    json.dump({
        "reexec": os.environ.get("IMPLEMENT_REVIEW_DISPATCH_REEXEC"),
        "source_dir": os.environ.get("IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR"),
    }, f)

started = os.environ.get("MOCK_CODEX_STARTED")
if started:
    with open(started, "w", encoding="utf-8") as f:
        f.write("started\n")

time.sleep(float(os.environ.get("MOCK_CODEX_SLEEP", "0")))

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
        script_path: Path | None = None,
    ) -> list[str]:
        if self.SHELL_KIND == "bash":
            return [
                BASH,
                str(script_path or DISPATCH_SH),
                "--prompt-file", str(prompt_file),
                "--round", round_arg,
                "--expected-review-file", expected_review_file,
            ]
        if self.SHELL_KIND == "powershell":
            return [
                PS_SHELL,
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", str(script_path or DISPATCH_PS1),
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
        extra_env: dict | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["CODEX_BIN"] = str(codex_bin)
        env["MOCK_CODEX_LOG"] = str(log_dir)
        env["MOCK_CODEX_EXIT"] = str(exit_code)
        # The default IMPLEMENT_REVIEW_ORCHESTRATOR for contract tests is
        # `claude` so the self-review guard does not block. Tests that exercise
        # the guard set this explicitly in extra_env.
        env["IMPLEMENT_REVIEW_ORCHESTRATOR"] = "claude"
        # Scrub CODEX_THREAD_ID and CODEX_SESSION_ID so they do not bleed in
        # from a Codex session invoking pytest and trigger the fall-through guard.
        env.pop("CODEX_THREAD_ID", None)
        env.pop("CODEX_SESSION_ID", None)
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
        # Point CODEX_HOME at an empty directory inside cwd so tests never
        # read the user's config.toml, even when CODEX_HOME is exported.
        empty_codex_home = cwd / ".empty-codex-home"
        empty_codex_home.mkdir(exist_ok=True)
        env["CODEX_HOME"] = str(empty_codex_home)
        # The tmp registry (see _fresh_fixture), read by a known interpreter,
        # and no per-run override unless the test sets one in extra_env.
        env["AGENT_MODELS_FILE"] = str(cwd / REGISTRY_NAME)
        env["ANYWHERE_AGENTS_PYTHON"] = sys.executable
        for name in ("CODEX_DISPATCH_MODEL", "CODEX_DISPATCH_REASONING",
                     "CODEX_DISPATCH_ISOLATE_MCP"):
            env.pop(name, None)
        if extra_env:
            for k, v in extra_env.items():
                if v is None:
                    env.pop(k, None)
                else:
                    env[k] = str(v)
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
        _write_registry(tmpdir)
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

    def _isolated_run(
        self,
        extra_env: dict | None = None,
        registry: dict | None = None,
        registry_down: bool = False,
        config_text: str | None = None,
        no_config_dir: bool = False,
    ):
        """One dispatch in a fresh fixture, with default isolation unless
        extra_env turns it off. Returns (result, codex args or None when codex
        never ran, review-model lines or None when no record was written).
        registry_down points AGENT_MODELS_FILE at a file that does not exist;
        config_text becomes $CODEX_HOME/config.toml."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            if registry is not None:
                _write_registry(tmpdir, **registry)
            env: dict = {}
            if registry_down:
                env["AGENT_MODELS_FILE"] = str(tmpdir / MISSING_REGISTRY_NAME)
            if config_text is not None:
                codex_home = tmpdir / "custom-codex-home"
                codex_home.mkdir()
                (codex_home / "config.toml").write_bytes(config_text.encode("utf-8"))
                env["CODEX_HOME"] = str(codex_home)
            elif no_config_dir:
                env["CODEX_HOME"] = str(tmpdir / "nonexistent-codex-home")
            env.update(extra_env or {})
            result = self._run_dispatch(
                tmpdir, prompt, "1", "Review-Codex.md", codex, log_dir, extra_env=env
            )
            args_path = log_dir / "args"
            args = json.loads(args_path.read_text(encoding="utf-8")) if args_path.exists() else None
            record = None
            if result.stdout.startswith("STATE-DIR"):
                record_path = _parse_state_dir(result.stdout) / "review-model"
                if record_path.exists():
                    record = record_path.read_text(encoding="utf-8").splitlines()
            return result, args, record

    def _assert_registry_failure(self, result, args) -> None:
        """No source named the reviewer: the dispatcher says so, names the
        registry, and spends no round."""
        self.assertEqual(result.returncode, 70, result.stderr)
        self.assertIn("registry", result.stderr)
        self.assertIn("agent-models", result.stderr)
        self.assertIsNone(args, "codex must not run without a model and effort")

    def test_codex_invoked_with_mcp_isolation_default(self) -> None:
        """Default-on isolation passes --ignore-user-config and re-passes the
        registry's model and effort.

        Regression guard for agent-config#1: a user-level Codex MCP server
        auto-starting under `codex exec` recursed into a nested codex and hung
        the headless reviewer with an empty review. --ignore-user-config is the
        only reliable stop (a narrower `-c mcp_servers={}` is deep-merged by
        codex and leaves the servers running). That flag also drops the
        configured model and sets reasoning effort to "none", so the dispatcher
        re-passes `-c model` and `-c model_reasoning_effort` from the registry,
        and all of them reach codex's CLI before the trailing stdin marker.
        """
        result, args, record = self._isolated_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--ignore-user-config", args,
                      f"--ignore-user-config must isolate the reviewer: {args}")
        pairs = list(zip(args, args[1:]))
        # Bind each value to its flag: a future arg shuffle could leave both
        # tokens present but parsed wrong.
        self.assertIn(("-c", "model=fixture-codex"), pairs, args)
        self.assertIn(("-c", "model_reasoning_effort=low"), pairs, args)
        self.assertEqual(
            record[0],
            "model=fixture-codex source=registry effort=low effort_source=registry",
        )
        self.assertEqual(args[-1], "-",
                         f"stdin marker must stay the final positional: {args}")

    def test_codex_follows_the_registry_reviewer_row(self) -> None:
        """The registry decides: a reviewer override changes the command line
        and the record, with no edit to the dispatcher."""
        result, args, record = self._isolated_run(
            registry={"reviewer": ("fixture-reviewer", "high")}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pairs = list(zip(args, args[1:]))
        self.assertIn(("-c", "model=fixture-reviewer"), pairs, args)
        self.assertIn(("-c", "model_reasoning_effort=high"), pairs, args)
        self.assertNotIn(("-c", "model=fixture-codex"), pairs, args)
        self.assertEqual(
            record[0],
            "model=fixture-reviewer source=registry effort=high effort_source=registry",
        )

    def test_codex_mcp_isolation_can_be_disabled(self) -> None:
        """CODEX_DISPATCH_ISOLATE_MCP=off drops the isolation flags.

        Escape hatch for the rare review that genuinely needs the user's full
        config (MCP servers, plugins, hooks). When off, codex args must carry
        neither --ignore-user-config nor the model and effort re-pass, falling
        back to the plain `exec --sandbox <mode> -` shape. The record still
        exists: it holds no request, and the actual run is checked against the
        registry.
        """
        result, args, record = self._isolated_run(
            extra_env={"CODEX_DISPATCH_ISOLATE_MCP": "off"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(
            "--ignore-user-config", args,
            f"isolation=off must drop --ignore-user-config: {args}",
        )
        # Reject ANY re-pass in opt-out mode: off is the full-config escape
        # hatch, so no model or effort override should be injected.
        self.assertFalse(
            any(str(arg).startswith("model_reasoning_effort=") for arg in args),
            f"isolation=off must drop any reasoning re-pass: {args}",
        )
        self.assertFalse(
            any(str(arg).startswith("model=") for arg in args),
            f"isolation=off must drop the pinned model: {args}",
        )
        # Sandbox flag and stdin marker are unaffected by the opt-out.
        self.assertIn("--sandbox", args, f"--sandbox must remain: {args}")
        self.assertEqual(args[-1], "-", f"last arg must be '-': {args}")
        self.assertEqual(
            record[0], "model=- source=user-config effort=- effort_source=user-config"
        )

    def test_codex_isolation_off_still_checks_the_run_against_the_registry(self) -> None:
        """With isolation off codex reads its own config. The header it
        prints is compared with the registry, so a config that drifted from
        the registry is flagged instead of silently reviewing."""
        result, _args, record = self._isolated_run(
            extra_env={
                "CODEX_DISPATCH_ISOLATE_MCP": "off",
                "MOCK_CODEX_STDERR": _codex_header("config-drifted-model", "low"),
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MODEL-MISMATCH", result.stderr)
        self.assertEqual(record[1], "actual=config-drifted-model low status=mismatch")

    def test_codex_isolation_reasoning_override(self) -> None:
        """CODEX_DISPATCH_REASONING outranks the registry effort for one run.

        --ignore-user-config drops the user's reasoning effort, so the
        dispatcher re-passes it. This env var lets a consumer whose
        model/account does not support the registry's level pick another one,
        or a round raise its depth. The model still follows the registry.
        """
        result, args, record = self._isolated_run(
            extra_env={"CODEX_DISPATCH_REASONING": "high"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pairs = list(zip(args, args[1:]))
        self.assertIn(("-c", "model_reasoning_effort=high"), pairs, args)
        self.assertNotIn(("-c", "model_reasoning_effort=low"), pairs, args)
        self.assertIn(("-c", "model=fixture-codex"), pairs, args)
        self.assertEqual(
            record[0],
            "model=fixture-codex source=registry effort=high effort_source=env",
        )

    def test_codex_isolation_model_override(self) -> None:
        """CODEX_DISPATCH_MODEL outranks the registry model, for an account
        without that model or a round meant for a different one."""
        result, args, record = self._isolated_run(
            extra_env={"CODEX_DISPATCH_MODEL": "gpt-test-override"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pairs = list(zip(args, args[1:]))
        self.assertIn(("-c", "model=gpt-test-override"), pairs, args)
        self.assertNotIn(("-c", "model=fixture-codex"), pairs, args)
        self.assertEqual(
            record[0],
            "model=gpt-test-override source=env effort=low effort_source=registry",
        )

    def test_codex_model_config_toml_top_level(self) -> None:
        """With the registry silent, the top-level model and effort in
        config.toml are passed."""
        result, args, record = self._isolated_run(
            registry_down=True,
            config_text='model = "gpt-test-config"\nmodel_reasoning_effort = "high"\n',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pairs = list(zip(args, args[1:]))
        self.assertIn(("-c", "model=gpt-test-config"), pairs, args)
        self.assertIn(("-c", "model_reasoning_effort=high"), pairs, args)
        self.assertEqual(
            record[0],
            "model=gpt-test-config source=config effort=high effort_source=config",
        )

    def test_codex_registry_outranks_config_toml(self) -> None:
        """config.toml is a fallback, not a competitor: bootstrap writes the
        registry into it, so when the two differ the registry is the truth."""
        result, args, record = self._isolated_run(
            config_text='model = "gpt-test-config"\nmodel_reasoning_effort = "high"\n',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pairs = list(zip(args, args[1:]))
        self.assertIn(("-c", "model=fixture-codex"), pairs, args)
        self.assertIn(("-c", "model_reasoning_effort=low"), pairs, args)
        self.assertNotIn(("-c", "model=gpt-test-config"), pairs, args)
        self.assertEqual(
            record[0],
            "model=fixture-codex source=registry effort=low effort_source=registry",
        )

    def test_codex_model_env_wins_over_config(self) -> None:
        """CODEX_DISPATCH_MODEL overrides the config.toml model and the registry."""
        result, args, record = self._isolated_run(
            registry_down=True,
            config_text='model = "gpt-test-config"\nmodel_reasoning_effort = "high"\n',
            extra_env={"CODEX_DISPATCH_MODEL": "gpt-env-wins"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pairs = list(zip(args, args[1:]))
        self.assertIn(("-c", "model=gpt-env-wins"), pairs, args)
        self.assertNotIn("model=gpt-test-config", args)
        self.assertEqual(
            record[0],
            "model=gpt-env-wins source=env effort=high effort_source=config",
        )

    def test_codex_two_overrides_need_no_registry_and_no_config(self) -> None:
        result, args, record = self._isolated_run(
            registry_down=True,
            no_config_dir=True,
            extra_env={"CODEX_DISPATCH_MODEL": "gpt-env-model",
                       "CODEX_DISPATCH_REASONING": "medium"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pairs = list(zip(args, args[1:]))
        self.assertIn(("-c", "model=gpt-env-model"), pairs, args)
        self.assertIn(("-c", "model_reasoning_effort=medium"), pairs, args)
        self.assertEqual(
            record[0],
            "model=gpt-env-model source=env effort=medium effort_source=env",
        )

    def test_codex_model_table_header_ignored(self) -> None:
        """A model key inside a TOML table is ignored. With the registry
        silent nothing else names a model, so the dispatch stops."""
        result, args, _record = self._isolated_run(
            registry_down=True,
            config_text=(
                'model_reasoning_effort = "low"\n'
                '[profiles.x]\n'
                'model = "gpt-in-table"\n'
            ),
        )
        self._assert_registry_failure(result, args)

    def test_codex_model_single_quoted_with_trailing_comment(self) -> None:
        """Single-quoted value with trailing comment is parsed correctly."""
        result, args, record = self._isolated_run(
            registry_down=True,
            config_text=(
                "  model = 'gpt-single-model' # comment with 'quote'\n"
                "model_reasoning_effort = 'medium'\n"
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pairs = list(zip(args, args[1:]))
        self.assertIn(("-c", "model=gpt-single-model"), pairs, args)
        self.assertIn(("-c", "model_reasoning_effort=medium"), pairs, args)
        self.assertEqual(
            record[0],
            "model=gpt-single-model source=config effort=medium effort_source=config",
        )

    def test_codex_config_values_outside_the_safe_set_are_refused(self) -> None:
        """A stray config line must not reach the -c argument or the .ps1's
        generated cmd helper: an invalid value (with space, unquoted, empty,
        shell-looking) is not used, and with the registry silent the
        dispatch stops."""
        for invalid_line in ('model = "bad model"', "model = gpt-unquoted", 'model = ""',
                             'model = "x;y"'):
            with self.subTest(invalid=invalid_line):
                result, args, _record = self._isolated_run(
                    registry_down=True,
                    config_text=f'{invalid_line}\nmodel_reasoning_effort = "low"\n',
                )
                self._assert_registry_failure(result, args)
        with self.subTest(invalid="effort"):
            result, args, _record = self._isolated_run(
                registry_down=True,
                config_text='model = "gpt-test-config"\nmodel_reasoning_effort = "hi gh"\n',
            )
            self._assert_registry_failure(result, args)

    def test_codex_model_follows_provider_and_bom(self) -> None:
        """A non-OpenAI model_provider skips the config model, since isolation
        drops the provider; an OpenAI one keeps it; a UTF-8 BOM is stripped."""
        effort = 'model_reasoning_effort = "low"\n'
        cases = [
            ('model_provider = "ollama"\nmodel = "gpt-oss:20b"\n' + effort, None),
            ('model = "gpt-oss:20b"\nmodel_provider = "azure"\n' + effort, None),
            ('model_provider = "openai"\nmodel = "gpt-test-config"\n' + effort, "gpt-test-config"),
            (effort + 'model = "gpt-test-config"\n[profiles.x]\nmodel_provider = "ollama"\n',
             "gpt-test-config"),
            ('﻿model = "gpt-test-bom"\n' + effort, "gpt-test-bom"),
        ]
        for config_text, want_model in cases:
            with self.subTest(config=config_text):
                result, args, record = self._isolated_run(
                    registry_down=True, config_text=config_text
                )
                if want_model is None:
                    self._assert_registry_failure(result, args)
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(("-c", f"model={want_model}"), list(zip(args, args[1:])), args)
                self.assertEqual(
                    record[0],
                    f"model={want_model} source=config effort=low effort_source=config",
                )

    def test_codex_no_registry_and_no_config_file_stops(self) -> None:
        """No literal to fall back on: the dispatcher names the registry and
        refuses to spend a round."""
        result, args, record = self._isolated_run(registry_down=True, no_config_dir=True)
        self._assert_registry_failure(result, args)
        self.assertIsNone(record, "no request was made, so no record is written")

    def test_codex_effort_missing_everywhere_stops(self) -> None:
        """A config model alone is not enough: the effort has no source either."""
        result, args, _record = self._isolated_run(
            registry_down=True, config_text='model = "gpt-test-config"\n'
        )
        self._assert_registry_failure(result, args)

    def test_codex_record_holds_the_actual_run_when_it_matches(self) -> None:
        result, _args, record = self._isolated_run(
            extra_env={"MOCK_CODEX_STDERR": _codex_header("fixture-codex", "low")}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record[1], "actual=fixture-codex low status=ok")
        self.assertNotIn("MODEL-MISMATCH", result.stderr)
        self.assertNotIn("MODEL-UNVERIFIED", result.stderr)

    def test_codex_a_run_on_another_model_or_effort_is_flagged(self) -> None:
        """Configured is not effective: when the header codex prints names
        another model or effort, the record and stderr say so, and the
        dispatch still returns codex's own exit code."""
        for header_model, header_effort in (("other-model", "low"), ("fixture-codex", "xhigh")):
            with self.subTest(model=header_model, effort=header_effort):
                result, _args, record = self._isolated_run(
                    extra_env={"MOCK_CODEX_STDERR": _codex_header(header_model, header_effort)}
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("MODEL-MISMATCH", result.stderr)
                self.assertEqual(
                    record[1], f"actual={header_model} {header_effort} status=mismatch"
                )

    def test_codex_a_run_that_cannot_be_read_back_is_unverified(self) -> None:
        result, _args, record = self._isolated_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record[1], "actual=unknown status=unknown")
        self.assertIn("MODEL-UNVERIFIED", result.stderr)

    def _assert_project_doc_budget(self, args: list) -> None:
        self.assertIn(
            ("-c", "project_doc_max_bytes=262144"), list(zip(args, args[1:])),
            f"project_doc_max_bytes=262144 must immediately follow a -c: {args}",
        )
        self.assertEqual(args[-1], "-", f"stdin marker must stay the final positional: {args}")

    def test_codex_project_doc_budget_with_isolation_on(self) -> None:
        """The dispatcher raises Codex's instruction-file byte budget.

        Codex injects at most project_doc_max_bytes (default 32 KiB) of the
        discovered AGENTS.md and truncates the rest with a tracing-only
        warning. A consumer's composed AGENTS.md (baseline plus passive packs)
        exceeds that, so a /vet round used to run without the Git Safety and
        writing rules its author assumed were injected. The dispatcher passes
        the budget itself because --ignore-user-config would drop a value set
        in ~/.codex/config.toml.
        """
        result, args, _record = self._isolated_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--ignore-user-config", args, f"isolation must be on here: {args}")
        self._assert_project_doc_budget(args)

    def test_codex_project_doc_budget_with_isolation_off(self) -> None:
        """The budget is passed outside the isolation branch, so opting out of
        isolation (CODEX_DISPATCH_ISOLATE_MCP=off) keeps it."""
        result, args, _record = self._isolated_run(
            extra_env={"CODEX_DISPATCH_ISOLATE_MCP": "off"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--ignore-user-config", args, f"isolation must be off here: {args}")
        self._assert_project_doc_budget(args)

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

    def test_reexec_uses_temp_copy_scrubs_sentinels_and_cleans_up(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fresh_fixture(tmpdir)
            deployed_dir = tmpdir / "deployed"
            deployed_dir.mkdir()
            source_script = DISPATCH_SH if self.SHELL_KIND == "bash" else DISPATCH_PS1
            deployed_script = deployed_dir / source_script.name
            shutil.copy2(source_script, deployed_script)
            if self.SHELL_KIND == "bash":
                deployed_script.chmod(
                    deployed_script.stat().st_mode
                    | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                )
            else:
                shutil.copy2(GUARD_PS1, deployed_dir / GUARD_PS1.name)

            started = tmpdir / "mock-started"
            env = os.environ.copy()
            env.pop("IMPLEMENT_REVIEW_DISPATCH_REEXEC", None)
            env.pop("IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR", None)
            env["IMPLEMENT_REVIEW_ORCHESTRATOR"] = "claude"
            env.pop("CODEX_THREAD_ID", None)
            env.pop("CODEX_SESSION_ID", None)
            env.update({
                "CODEX_BIN": str(codex),
                # The copied script sits outside the repository layout, so it
                # cannot reach the registry; the per-run overrides stand in.
                "CODEX_DISPATCH_MODEL": "fixture-codex",
                "CODEX_DISPATCH_REASONING": "low",
                "MOCK_CODEX_LOG": str(log_dir),
                "MOCK_CODEX_STARTED": str(started),
                "MOCK_CODEX_SLEEP": "2",
                "TMPDIR": str(tmpdir),
                "TEMP": str(tmpdir),
                "TMP": str(tmpdir),
                "STALL_POLL_INTERVAL_SECONDS": "1",
                "STALL_THRESHOLD_SECONDS": "999999",
            })
            proc = subprocess.Popen(
                self._build_cmd(
                    prompt, "1", "Review-Codex.md",
                    script_path=deployed_script,
                ),
                cwd=str(tmpdir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not started.exists():
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertTrue(started.exists(), "mock reviewer did not start")

                reexec_dirs = list(tmpdir.glob(
                    "implement-review-dispatch-codex-reexec-*"
                ))
                self.assertEqual(
                    len(reexec_dirs), 1,
                    f"expected one live re-exec directory, found: {reexec_dirs}",
                )
                self.assertTrue(
                    (reexec_dirs[0] / source_script.name).is_file(),
                    "re-exec directory must contain the copied dispatcher",
                )

                deployed_script.unlink()
                self.assertFalse(
                    deployed_script.exists(),
                    "the deployed script path remained held during review",
                )

                stdout, stderr = proc.communicate(timeout=20)
                self.assertEqual(
                    proc.returncode, 0,
                    f"re-exec dispatch failed\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}",
                )
                self.assertEqual(
                    list(tmpdir.glob("implement-review-dispatch-codex-reexec-*")),
                    [],
                    "the temporary dispatcher copy was not cleaned up",
                )
                dispatch_env = json.loads(
                    (log_dir / "dispatch-env").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    dispatch_env,
                    {"reexec": None, "source_dir": None},
                    "re-exec sentinels must not leak into the reviewer process",
                )
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()

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

    # --- self-review guard contract --------------------------------------

    def _run_guard_case(
        self,
        tmpdir: Path,
        orchestrator: str | None = None,
        thread_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        """Run dispatch with the given guard-relevant env values. Returns
        (CompletedProcess, log_dir) so callers can assert on stderr / exit /
        whether the mock codex was invoked (args file presence)."""
        codex, prompt, log_dir = self._fresh_fixture(tmpdir)
        extra: dict[str, str | None] = {}
        if orchestrator is None:
            extra["IMPLEMENT_REVIEW_ORCHESTRATOR"] = None
        else:
            extra["IMPLEMENT_REVIEW_ORCHESTRATOR"] = orchestrator
        if thread_id is not None:
            extra["CODEX_THREAD_ID"] = thread_id
        if session_id is not None:
            extra["CODEX_SESSION_ID"] = session_id
        result = self._run_dispatch(
            tmpdir, prompt, "1", "Review-Codex.md", codex, log_dir,
            extra_env=extra,
        )
        return result, log_dir

    def _assert_guard_refusal(
        self,
        result: subprocess.CompletedProcess[str],
        log_dir: Path,
        tmpdir: Path,
    ) -> None:
        self.assertEqual(
            result.returncode, 2,
            f"guard must exit 2 on refusal\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn(
            "dispatch-codex: refusing to dispatch (orchestrator=codex; self-review)",
            result.stderr,
            "guard must emit the documented refusal line on stderr",
        )
        self.assertNotIn(
            "STATE-DIR",
            result.stdout,
            "stdout must not contain a STATE-DIR line on refusal",
        )
        self.assertFalse(
            (log_dir / "args").exists(),
            "mock codex must not be invoked when guard refuses",
        )
        self.assertEqual(
            list(log_dir.iterdir()),
            [],
            "mock codex log directory must stay empty when guard refuses",
        )
        state_dirs = list(tmpdir.glob("implement-review-codex-*"))
        self.assertEqual(
            state_dirs,
            [],
            f"no state directory should be created under temp base, found: {state_dirs}",
        )

    def test_guard_refuses_when_orchestrator_is_codex(self) -> None:
        """`IMPLEMENT_REVIEW_ORCHESTRATOR=codex` (case-insensitive) must
        refuse with exit 2 + stderr message, no codex invocation."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, log_dir = self._run_guard_case(
                tmpdir, orchestrator="codex",
            )
            self._assert_guard_refusal(result, log_dir, tmpdir)

    def test_guard_refuses_when_orchestrator_is_CODEX_uppercase(self) -> None:
        """Guard match is case-insensitive."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, log_dir = self._run_guard_case(
                tmpdir, orchestrator="CODEX",
            )
            self._assert_guard_refusal(result, log_dir, tmpdir)

    def test_guard_refuses_when_orchestrator_unset_and_thread_id_set(self) -> None:
        """Fall-through: unset orchestrator + CODEX_THREAD_ID -> refuse."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, log_dir = self._run_guard_case(
                tmpdir, orchestrator=None, thread_id="thread-probe",
            )
            self._assert_guard_refusal(result, log_dir, tmpdir)

    def test_guard_refuses_when_orchestrator_unset_and_session_id_set(self) -> None:
        """Fall-through: unset orchestrator + CODEX_SESSION_ID -> refuse."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, log_dir = self._run_guard_case(
                tmpdir, orchestrator=None, session_id="session-probe",
            )
            self._assert_guard_refusal(result, log_dir, tmpdir)

    def test_guard_refuses_when_orchestrator_empty_and_thread_id_set(self) -> None:
        """Fall-through: empty orchestrator + CODEX_THREAD_ID -> refuse."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, log_dir = self._run_guard_case(
                tmpdir, orchestrator="", thread_id="thread-probe",
            )
            self._assert_guard_refusal(result, log_dir, tmpdir)

    def test_guard_proceeds_when_orchestrator_is_claude(self) -> None:
        """`IMPLEMENT_REVIEW_ORCHESTRATOR=claude` proceeds even with both
        CODEX_THREAD_ID and CODEX_SESSION_ID set (explicit orchestrator wins
        over the fall-through)."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, log_dir = self._run_guard_case(
                tmpdir,
                orchestrator="claude",
                thread_id="thread-probe",
                session_id="session-probe",
            )
            self.assertEqual(
                result.returncode, 0,
                f"guard must NOT refuse when orchestrator=claude\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
            )
            self.assertIn("STATE-DIR", result.stdout)
            self.assertTrue(
                (log_dir / "args").exists(),
                "mock codex must be invoked when guard permits",
            )

    def test_guard_proceeds_when_orchestrator_is_user(self) -> None:
        """`IMPLEMENT_REVIEW_ORCHESTRATOR=user` proceeds even with
        CODEX_THREAD_ID set."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, log_dir = self._run_guard_case(
                tmpdir,
                orchestrator="user",
                thread_id="thread-probe",
            )
            self.assertEqual(
                result.returncode, 0,
                f"guard must NOT refuse when orchestrator=user\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
            )
            self.assertIn("STATE-DIR", result.stdout)
            self.assertTrue(
                (log_dir / "args").exists(),
                "mock codex must be invoked when guard permits",
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
    """Sanity: both scripts and guard helper must be present in the repo."""

    def test_sh_exists(self) -> None:
        self.assertTrue(DISPATCH_SH.exists(),
                        f"dispatch-codex.sh missing: {DISPATCH_SH}")

    def test_ps1_exists(self) -> None:
        self.assertTrue(DISPATCH_PS1.exists(),
                        f"dispatch-codex.ps1 missing: {DISPATCH_PS1}")

    def test_guard_ps1_exists(self) -> None:
        self.assertTrue(GUARD_PS1.exists(),
                        f"_codex_guard.ps1 missing: {GUARD_PS1}")

    def test_self_review_guard_present(self) -> None:
        """The self-review guard must be present at the script level. The .sh
        side carries it inline. The .ps1 side delegates to `_codex_guard.ps1`
        sitting next to it, because the env-check + stderr-write + non-zero
        exit cluster combined with the cmdBody construction in the same file
        scores as malicious-orchestration on some Windows AV products. The
        runtime guard tests in `_DispatchContractMixin` (7 cases) verify the
        end-to-end behavior across both shells; this static check confirms
        the on-disk surface for the contract.
        """
        # .sh side carries the guard inline.
        sh_text = DISPATCH_SH.read_text(encoding="utf-8")
        self.assertIn("IMPLEMENT_REVIEW_ORCHESTRATOR", sh_text,
                      ".sh must check IMPLEMENT_REVIEW_ORCHESTRATOR env")
        self.assertIn("CODEX_THREAD_ID", sh_text,
                      ".sh must check CODEX_THREAD_ID env for fall-through guard")
        self.assertIn("CODEX_SESSION_ID", sh_text,
                      ".sh must check CODEX_SESSION_ID env for fall-through guard")
        self.assertIn(
            "refusing to dispatch (orchestrator=codex; self-review)",
            sh_text,
            ".sh must emit the documented refusal stderr line",
        )
        # .ps1 side delegates to _codex_guard.ps1: the dispatch script must
        # invoke the guard helper, and the helper must carry the env-check +
        # refusal-message contract.
        ps1_text = DISPATCH_PS1.read_text(encoding="utf-8")
        self.assertIn(
            "_codex_guard.ps1", ps1_text,
            "dispatch-codex.ps1 must delegate self-review guard to "
            "_codex_guard.ps1 helper next to it",
        )
        self.assertTrue(
            GUARD_PS1.exists(),
            f"_codex_guard.ps1 must exist next to dispatch-codex.ps1: {GUARD_PS1}",
        )
        guard_text = GUARD_PS1.read_text(encoding="utf-8")
        # Env var names assembled from fragments; check both inline and
        # concat forms so a future inline-rewrite (under a different AV) is
        # also acceptable.
        self.assertTrue(
            ("IMPLEMENT_REVIEW_ORCHESTRATOR" in guard_text)
            or ("'IMPLEMENT_REVIEW_' + 'ORCHESTRATOR'" in guard_text),
            "_codex_guard.ps1 must reference IMPLEMENT_REVIEW_ORCHESTRATOR "
            "(inline literal or via 'IMPLEMENT_REVIEW_' + 'ORCHESTRATOR' concat)",
        )
        self.assertTrue(
            ("CODEX_THREAD_ID" in guard_text)
            or ("'CODEX_' + 'THREAD_ID'" in guard_text),
            "_codex_guard.ps1 must reference CODEX_THREAD_ID "
            "(inline literal or via 'CODEX_' + 'THREAD_ID' concat)",
        )
        self.assertTrue(
            ("CODEX_SESSION_ID" in guard_text)
            or ("'CODEX_' + 'SESSION_ID'" in guard_text),
            "_codex_guard.ps1 must reference CODEX_SESSION_ID "
            "(inline literal or via 'CODEX_' + 'SESSION_ID' concat)",
        )
        # The refusal phrase may be split across PowerShell string concat
        # segments (e.g., 'refusing to ' + 'dispatch '); accept both inline
        # and segmented forms.
        self.assertTrue(
            ("refusing to dispatch (orchestrator=codex; self-review)" in guard_text)
            or (("refusing to " in guard_text)
                and ("orchestrator=codex; self-review" in guard_text)),
            "_codex_guard.ps1 must emit the documented refusal stderr line "
            "(inline or in concatenated segments)",
        )


class DispatchInterpreterResolutionContract(unittest.TestCase):
    """The execution probe must preserve environment-specific launch paths."""

    def test_probe_preserves_sys_executable_and_alias_safeguards(self) -> None:
        sh = DISPATCH_SH.read_text(encoding="utf-8")
        ps1 = DISPATCH_PS1.read_text(encoding="utf-8")

        for script_name, body in (("dispatch-codex.sh", sh),
                                  ("dispatch-codex.ps1", ps1)):
            self.assertEqual(
                body.count("realpath(sys.executable)"), 1,
                f"{script_name} may canonicalize only for alias rejection",
            )
            self.assertIn(
                "WindowsApps", body,
                f"{script_name} must retain Windows Store alias rejection",
            )

        self.assertIn(
            'print("IMPLEMENT_REVIEW_PYTHON=" + sys.executable)', sh
        )
        self.assertIn(
            'case "$resolved_real" in\n'
            '        *WindowsApps*|*windowsapps*) return 1 ;;',
            sh,
        )
        self.assertIn('[ -s "$resolved" ] || return 1', sh)
        self.assertIn(
            '"$resolved" -I -c \'import sys; sys.exit(0)\'', sh
        )

        # Single quotes inside, not double. Windows PowerShell 5.1 rebuilds the
        # native command line without escaping a double quote inside the -c
        # argument, so the shape below is the only one that reaches Python
        # intact on both editions. Same defect as anywhere-agents#34.
        self.assertIn(
            "print('IMPLEMENT_REVIEW_PYTHON='+sys.executable)", ps1
        )
        probe_line = next(
            line for line in ps1.splitlines()
            if "IMPLEMENT_REVIEW_PYTHON=" in line and "print(" in line
        ).strip()
        # Exactly two double quotes, the delimiters. Any third one is inside the
        # argument, which is the failure mode.
        self.assertEqual(
            probe_line.count('"'), 2,
            "the PowerShell probe argument must carry no double quote of its "
            f"own; Windows PowerShell 5.1 drops the escaping: {probe_line}",
        )
        self.assertIn(
            "$resolvedRealPath -match '(?i)[\\\\/]WindowsApps[\\\\/]'",
            ps1,
        )
        self.assertIn("$resolvedItem.Length -le 0", ps1)
        self.assertIn(
            "& $resolvedPath -I -c 'import sys; sys.exit(0)'", ps1
        )

    def test_unresolved_interpreter_degrades_instead_of_aborting(self) -> None:
        """Finding no interpreter must not abort dispatch.

        The probe keeps a reviewer from claiming verification it never ran,
        but check 10 enforces that independently by refusing PASS or BLOCK
        without a VERIFIED status. Reviews also run on repositories whose
        verification is not Python (LaTeX, Node, docs). Aborting there made
        those repositories unreviewable on any machine without a discoverable
        interpreter, which is what agent-config CI reproduced on
        windows-latest: nine dispatch fixtures failed with exit 2.
        """
        unresolved = "no working Python interpreter found"
        degraded = "dispatching without a pre-resolved interpreter"

        for script_name, body in (
            ("dispatch-codex.sh", DISPATCH_SH.read_text(encoding="utf-8")),
            ("dispatch-codex.ps1", DISPATCH_PS1.read_text(encoding="utf-8")),
        ):
            with self.subTest(script=script_name):
                self.assertIn(unresolved, body)
                self.assertIn(
                    degraded, body,
                    f"{script_name} must say it is proceeding degraded",
                )
                # The branch that reports the miss must not terminate. Scan
                # from the message to the end of its enclosing block.
                start = body.index(unresolved)
                tail = body[start:start + 600]
                self.assertNotIn(
                    "exit 2", tail,
                    f"{script_name} must not abort when the probe resolves "
                    f"nothing",
                )

                # A configured but broken override stays a hard failure: the
                # user named an interpreter and it does not work.
                override_msg = (
                    "ANYWHERE_AGENTS_PYTHON is not an executable Python "
                    "interpreter"
                )
                self.assertIn(override_msg, body)
                override_tail = body[body.index(override_msg):][:400]
                self.assertIn(
                    "exit 2", override_tail,
                    f"{script_name} must still abort on a broken explicit "
                    f"ANYWHERE_AGENTS_PYTHON",
                )

    def test_unresolved_interpreter_omits_the_state_dir_marker(self) -> None:
        """Absence of the marker is the unambiguous signal.

        Writing a sentinel string would let a reader mistake it for a path.
        """
        sh = DISPATCH_SH.read_text(encoding="utf-8")
        ps1 = DISPATCH_PS1.read_text(encoding="utf-8")

        self.assertIn(
            'if [ -n "$PYTHON_BIN" ]; then\n'
            '    printf \'%s\\n\' "$PYTHON_BIN" > "$STATE_DIR/python-interpreter"\n'
            'fi',
            sh,
        )
        self.assertIn(
            "if ($pythonBin) {\n"
            "    [System.IO.File]::WriteAllText((Join-Path $stateDir "
            "'python-interpreter'), \"$pythonBin`n\")\n"
            "}",
            ps1,
        )

    def test_child_instructions_branch_on_interpreter_availability(self) -> None:
        """The reviewer is told which situation it is in, either way."""
        sh = DISPATCH_SH.read_text(encoding="utf-8")
        ps1 = DISPATCH_PS1.read_text(encoding="utf-8")

        resolved = "A working Python interpreter was execution-probed"
        unresolved = "No Python interpreter could be pre-resolved on this machine"

        for script_name, body in (("dispatch-codex.sh", sh),
                                  ("dispatch-codex.ps1", ps1)):
            with self.subTest(script=script_name):
                self.assertIn(resolved, body)
                self.assertIn(unresolved, body)
                # The verification contract itself is unconditional.
                self.assertEqual(
                    body.count("Verification status: VERIFIED"), 1,
                    f"{script_name} must state the contract exactly once",
                )


class DispatchPwshResolutionContract(unittest.TestCase):
    """The pwsh probe rejects Store aliases without executing candidates."""

    def test_pwsh_probe_is_structural_not_execution(self) -> None:
        sh = DISPATCH_SH.read_text(encoding="utf-8")
        ps1 = DISPATCH_PS1.read_text(encoding="utf-8")

        self.assertIn("type -a -p pwsh", sh)
        self.assertIn('[ ! -s "$candidate" ]', sh)
        sh_probe = sh[sh.index('PWSH_BIN=""'):sh.index("# Build unique state-dir")]
        self.assertNotRegex(sh_probe, r'(?m)^\s*"\$candidate"\s')

        self.assertIn("Get-Command -Name pwsh -All", ps1)
        self.assertIn("$candidateItem.Length -le 0", ps1)
        ps1_probe = ps1[
            ps1.index("$pwshBin = $null"):ps1.index("# Resolve temp base")
        ]
        self.assertNotIn("& $candidatePath", ps1_probe)

    def test_unusable_pwsh_degrades_instead_of_aborting(self) -> None:
        unresolved = "no usable pwsh found"
        for script_name, body in (
            ("dispatch-codex.sh", DISPATCH_SH.read_text(encoding="utf-8")),
            ("dispatch-codex.ps1", DISPATCH_PS1.read_text(encoding="utf-8")),
        ):
            with self.subTest(script=script_name):
                self.assertIn(unresolved, body)
                start = body.index(unresolved)
                tail = body[start:start + 600]
                self.assertNotIn(
                    "exit 2", tail,
                    f"{script_name} must not abort when pwsh is unusable",
                )

    def test_pwsh_state_dir_marker_omitted_when_unusable(self) -> None:
        sh = DISPATCH_SH.read_text(encoding="utf-8")
        ps1 = DISPATCH_PS1.read_text(encoding="utf-8")

        self.assertIn(
            'if [ -n "$PWSH_BIN" ]; then\n'
            '    printf \'%s\\n\' "$PWSH_BIN" > "$STATE_DIR/pwsh-interpreter"\n'
            'fi',
            sh,
        )
        self.assertIn(
            "if ($pwshBin) {\n"
            "    [System.IO.File]::WriteAllText((Join-Path $stateDir "
            "'pwsh-interpreter'), \"$pwshBin`n\")\n"
            "}",
            ps1,
        )

    def test_child_instructions_branch_on_pwsh_availability(self) -> None:
        resolved = "PowerShell 7 was resolved at this absolute path"
        unresolved = "Do not shell out to pwsh"
        for script_name, body in (
            ("dispatch-codex.sh", DISPATCH_SH.read_text(encoding="utf-8")),
            ("dispatch-codex.ps1", DISPATCH_PS1.read_text(encoding="utf-8")),
        ):
            with self.subTest(script=script_name):
                self.assertIn(resolved, body)
                self.assertIn(unresolved, body)
                self.assertEqual(
                    body.count("Verification status: VERIFIED"), 1,
                    f"{script_name} must state the contract exactly once",
                )

    def test_pwsh_instruction_does_not_embed_the_ntstatus_code(self) -> None:
        for script_name, body in (
            ("dispatch-codex.sh", DISPATCH_SH.read_text(encoding="utf-8")),
            ("dispatch-codex.ps1", DISPATCH_PS1.read_text(encoding="utf-8")),
        ):
            with self.subTest(script=script_name):
                self.assertNotIn("-1073741502", body)


class DispatchReexecContract(unittest.TestCase):
    def test_sh_reexec_copy_is_guarded_scrubbed_and_cleaned(self) -> None:
        text = DISPATCH_SH.read_text(encoding="utf-8")
        self.assertIn('IMPLEMENT_REVIEW_DISPATCH_REEXEC:-', text)
        self.assertIn('IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR', text)
        self.assertIn('implement-review-dispatch-codex-reexec-', text)
        self.assertIn('"${BASH:-bash}" "$reexec_copy" "$@"', text)
        self.assertIn('rm -f -- "$reexec_copy"', text)
        self.assertIn('rmdir -- "$(dirname -- "$reexec_copy")"', text)
        self.assertIn(
            'unset IMPLEMENT_REVIEW_DISPATCH_REEXEC '
            'IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR',
            text,
        )

    def test_ps1_reexec_copy_is_guarded_scrubbed_and_cleaned(self) -> None:
        text = DISPATCH_PS1.read_text(encoding="utf-8")
        self.assertIn('$env:IMPLEMENT_REVIEW_DISPATCH_REEXEC', text)
        self.assertIn('$env:IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR', text)
        self.assertIn('implement-review-dispatch-codex-reexec-', text)
        self.assertIn('-File $reexecCopy @args', text)
        self.assertIn('Remove-Item -LiteralPath $reexecCopy', text)
        self.assertIn('Remove-Item -LiteralPath $reexecDir', text)
        self.assertIn('Remove-Item Env:IMPLEMENT_REVIEW_DISPATCH_REEXEC', text)
        self.assertIn('Remove-Item Env:IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR', text)


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


class DispatchReviewerContract(unittest.TestCase):
    """Both dispatchers hand Codex the same reviewer contract in
    `developer_instructions`: the reviewer skips coordinator-workflow
    discovery but keeps targeted verification reads, applies supplied policy
    including local overrides, and treats a staged-change review and a plan
    review as different inputs. Milestone A of the 2026-09 skills diet;
    plan-review round 3 and execution-review round 1 both asked for the
    copies to agree word for word."""

    SENTENCES = (
        "This session is an assigned reviewer.",
        "Skip router dispatch and coordinator-workflow discovery at all skill lookup locations",
        "Do not load coordinator skill files or example reviews to discover the workflow or response format.",
        "Files under review remain readable.",
        "A targeted read outside the review scope is permitted when it supplies missing context, resolves truncation, or answers a concrete verification question.",
        "including applicable local overrides",
        "Read an applicable instruction file once only when its rules have not been supplied.",
        "For a staged-change review, obtain the selected diff once with the command in the request;",
        "For a plan review, the named plan and evidence files are the review input:",
    )

    @staticmethod
    def _instruction(text: str, marker: str) -> str:
        start = text.index(marker) + len(marker)
        end = text.index('"', start + 1)
        return text[start + 1:end]

    def test_both_dispatchers_carry_every_sentence(self) -> None:
        sh = self._instruction(DISPATCH_SH.read_text(encoding="utf-8"), "CHILD_SESSION_INSTRUCTIONS=")
        ps = self._instruction(DISPATCH_PS1.read_text(encoding="utf-8"), "$childSessionInstructions = ")
        for sentence in self.SENTENCES:
            with self.subTest(sentence[:50]):
                self.assertIn(sentence, sh)
                self.assertIn(sentence, ps)
        self.assertNotIn("Follow all other project instructions", sh + ps)

    def test_the_two_copies_agree_after_variable_normalization(self) -> None:
        sh = self._instruction(DISPATCH_SH.read_text(encoding="utf-8"), "CHILD_SESSION_INSTRUCTIONS=")
        ps = self._instruction(DISPATCH_PS1.read_text(encoding="utf-8"), "$childSessionInstructions = ")
        sh = sh.replace("$PYTHON_INSTRUCTION", "@py").replace("$PWSH_INSTRUCTION", "@pwsh").replace("$EXPECTED_REVIEW_FILE", "@review")
        ps = ps.replace("$pythonInstruction", "@py").replace("$pwshInstruction", "@pwsh").replace("$ExpectedReviewFile", "@review")
        self.assertEqual(sh, ps)

    def test_skip_list_includes_agents_skills_directory(self) -> None:
        sh = self._instruction(DISPATCH_SH.read_text(encoding="utf-8"), "CHILD_SESSION_INSTRUCTIONS=")
        ps = self._instruction(DISPATCH_PS1.read_text(encoding="utf-8"), "$childSessionInstructions = ")
        self.assertIn(".agents/skills/implement-review/", sh)
        self.assertIn(".agents/skills/implement-review/", ps)


class DispatchMcpIsolationContract(unittest.TestCase):
    """Both dispatchers must isolate user MCP servers by default.

    Background: agent-config#1. A user-level Codex MCP server (e.g.
    node_repl) auto-started under `codex exec`, spawned a nested codex,
    looped on "ERROR: Reconnecting... N/5", and hung the headless reviewer
    15+ minutes with an empty review. The fix passes --ignore-user-config
    (the only reliable stop; a narrower `-c mcp_servers={}` is deep-merged
    by codex and leaves servers running). That flag also drops the reasoning
    effort to "none" and drops the configured model. The dispatcher therefore
    re-passes `-c model_reasoning_effort` and `-c model`, both named by the
    registry unless CODEX_DISPATCH_REASONING or CODEX_DISPATCH_MODEL overrides
    them (config.toml is the last source, and no literal backs it up), so the
    reviewer is not silently changed. Default on; opt out with
    CODEX_DISPATCH_ISOLATE_MCP=off.

    These static checks freeze the wiring so a future refactor that drops
    the override (while the runtime mock tests happen to still pass) fails
    here, mirroring DispatchSandboxFlagContract's rationale. The .ps1 keeps
    the isolation line beside the cmdBody construction a plain ternary over
    variables and resolves the model far earlier, beside the other state
    markers, so the Windows-AV heuristic that blocks richer logic next to
    the cmdBody construction stays clear.
    """

    def test_sh_isolates_mcp(self) -> None:
        text = DISPATCH_SH.read_text(encoding="utf-8")
        self.assertIn("CODEX_DISPATCH_ISOLATE_MCP", text,
                      "dispatch-codex.sh must read CODEX_DISPATCH_ISOLATE_MCP")
        self.assertIn("--ignore-user-config", text,
                      "dispatch-codex.sh must isolate via --ignore-user-config")
        self.assertIn("model_reasoning_effort", text,
                      "dispatch-codex.sh must re-pass reasoning effort")

    def test_ps1_isolates_mcp(self) -> None:
        text = DISPATCH_PS1.read_text(encoding="utf-8")
        self.assertIn("CODEX_DISPATCH_ISOLATE_MCP", text,
                      "dispatch-codex.ps1 must read $env:CODEX_DISPATCH_ISOLATE_MCP")
        self.assertIn("--ignore-user-config", text,
                      "dispatch-codex.ps1 must isolate via --ignore-user-config")
        self.assertIn("model_reasoning_effort", text,
                      "dispatch-codex.ps1 must re-pass reasoning effort")

    def test_no_model_or_effort_literal_and_no_stale_fallback(self) -> None:
        """Neither dispatcher names a model or an effort: the drift scan finds
        no literal, and the old fallback constant and effort default are gone."""
        for path in (DISPATCH_SH, DISPATCH_PS1):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                hits = [line for _, _, line in agent_models.scan_text(text)]
                self.assertEqual(hits, [], f"{path.name} names a model or effort literal")
                self.assertNotIn("CODEX_FALLBACK_MODEL", text)
                self.assertNotIn("codexFallbackModel", text)
                self.assertNotIn("'fallback'", text)
                self.assertNotIn('"fallback"', text)

    def test_both_shells_read_the_registry_and_record_the_actual_run(self) -> None:
        """Both dispatchers ask the resolver for the codex reviewer row and
        for what the run reported, in the same order of sources."""
        for path in (DISPATCH_SH, DISPATCH_PS1):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("agent_models.py", text)
                self.assertIn("--family", text)
                self.assertIn("reviewer", text)
                self.assertIn("--stderr-file", text)
                self.assertIn("MODEL-UNVERIFIED", text)
                self.assertIn("effort_source", text)

    def test_ps1_model_resolution_matches_contract(self) -> None:
        """Static text contract for dispatch-codex.ps1 model resolution.

        pwsh is not installed in every test environment, so this freezes the
        ps1 implementation: it must read CODEX_HOME, config.toml, enforce the
        ^[A-Za-z0-9._:/-]+$ character set, record the same sources as .sh
        (env, registry, config), and write review-model.
        """
        ps1_text = DISPATCH_PS1.read_text(encoding="utf-8")

        self.assertIn("CODEX_HOME", ps1_text)
        self.assertIn("config.toml", ps1_text)
        self.assertIn("^[A-Za-z0-9._:/-]+$", ps1_text)
        self.assertIn("'env'", ps1_text)
        self.assertIn("'registry'", ps1_text)
        self.assertIn("'config'", ps1_text)
        self.assertIn("review-model", ps1_text)
        self.assertIn("model_provider", ps1_text)
        self.assertIn("model_reasoning_effort", ps1_text)

    def test_both_shells_raise_the_project_doc_budget(self) -> None:
        """Both dispatchers pass -c project_doc_max_bytes=262144 outside the
        isolation branch (the runtime tests above prove it reaches codex with
        isolation on and off; this freezes the literal so the two shells
        cannot drift to different budgets)."""
        for path in (DISPATCH_SH, DISPATCH_PS1):
            text = path.read_text(encoding="utf-8")
            self.assertEqual(
                text.count("project_doc_max_bytes=262144"), 1,
                f"{path.name} must pass project_doc_max_bytes=262144 exactly once",
            )


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
            env["IMPLEMENT_REVIEW_ORCHESTRATOR"] = "claude"
            env.pop("CODEX_THREAD_ID", None)
            env.pop("CODEX_SESSION_ID", None)
            env["TMPDIR"] = str(tmpdir)
            env["TEMP"] = str(tmpdir)
            env["TMP"] = str(tmpdir)
            # The per-run overrides stand in for the registry, so these
            # stall tests need no resolver or interpreter lookup.
            env["CODEX_DISPATCH_MODEL"] = "fixture-codex"
            env["CODEX_DISPATCH_REASONING"] = "low"
            # Tiny window so the test runs fast: codex stays silent for 5s,
            # polled every 1s. The threshold is 1 rather than 2 so the
            # warning needs two polls instead of three. A loaded CI runner
            # flaked this test three times at 2 (anywhere-agents#46). Do
            # not raise it back without widening MOCK_CODEX_SLEEP first.
            env["MOCK_CODEX_SLEEP"] = "5"
            env["STALL_THRESHOLD_SECONDS"] = "1"
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

            # stall-watch may still be doing its final-on-parent-dead poll,
            # so wait for the flush rather than assuming it already happened.
            stall_warning = state_dir / "stall-warning"
            deadline = time.monotonic() + STALL_WAIT_TIMEOUT_SECONDS
            while time.monotonic() < deadline and not stall_warning.exists():
                time.sleep(0.25)
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
            env["IMPLEMENT_REVIEW_ORCHESTRATOR"] = "claude"
            env.pop("CODEX_THREAD_ID", None)
            env.pop("CODEX_SESSION_ID", None)
            env["TMPDIR"] = str(tmpdir)
            env["TEMP"] = str(tmpdir)
            env["TMP"] = str(tmpdir)
            # The per-run overrides stand in for the registry, so these
            # stall tests need no resolver or interpreter lookup.
            env["CODEX_DISPATCH_MODEL"] = "fixture-codex"
            env["CODEX_DISPATCH_REASONING"] = "low"
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


class ReexecFailureCleanupReachable(unittest.TestCase):
    """A failed exec has to reach the cleanup each guard already writes.

    Bash exits a noninteractive shell when exec fails, with 126 or 127, so the
    diagnostic, the removal of the private copy, and the exit 2 that every guard
    places after its exec could not run. Measured on the pre-fix script: rc=127,
    no diagnostic, and the private directory left behind. `set +e` and
    `shopt -s execfail` make that block reachable, and both are needed:
    `execfail` stops the immediate exit, `set +e` stops the returned status from
    tripping errexit before the cleanup runs (anywhere-agents#48; prun took the
    same fix first in ac d067c0c / aa 9f873d5).

    Checked by position rather than by presence, because the options only do
    their job immediately before the exec they guard. One test covers all four
    scripts because the guard is one shared shape, copied four times.
    """

    GUARDED = (
        "auto-watch.sh",
        "dispatch-claude.sh",
        "dispatch-codex.sh",
        "stall-watch.sh",
    )

    def test_every_guard_can_reach_its_cleanup(self) -> None:
        for name in self.GUARDED:
            with self.subTest(script=name):
                text = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
                exec_at = text.find('exec "${BASH:-bash}"')
                self.assertNotEqual(exec_at, -1, name + " has no re-exec hand-off")

                before = text[:exec_at].rstrip()
                tail = [line.strip() for line in before.splitlines()[-2:]]
                self.assertEqual(
                    tail, ["set +e", "shopt -s execfail"],
                    name + " must set both options directly before its exec, "
                    "otherwise a failed exec still leaves without cleaning up. "
                    "Found: " + repr(tail),
                )

                self.assertIn(
                    "failed to launch re-exec copy", text[exec_at:],
                    name + " has no post-exec cleanup for those options to reach",
                )


if __name__ == "__main__":
    unittest.main()
