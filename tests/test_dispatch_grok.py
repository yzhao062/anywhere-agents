"""Unit and contract tests for skills/implement-review/scripts/dispatch-grok.py.

Covers:
  - Normal round over streaming-json (both snapshot-written Review-Grok.md and
    streamed response fallback, state-dir contract, child env isolation,
    grok-version, cost-usd, and review-model verification)
  - Active run whose simulated wall time exceeds 11 minutes (660s) with regular
    events via injectable clock step without real waiting
  - Silent stall warning via stall-watch.sh (warning-only, does not kill)
  - Missing end event (exits 70, no review published)
  - Stream error event (exits 70, no review published even if snapshot draft exists)
  - Each non-end_turn stopReason: max_tokens, max_turn_requests, refusal,
    cancelled (all exit 70)
  - Missing or partial total_cost_usd recorded as "unknown", never zero
  - Process-tree cleanup on absolute timeout (GROK_DISPATCH_TIMEOUT_SECONDS)
  - Narration before tool_call events excluded from response text, fallback to
    full-stream text when nothing follows the last tool_call, and unknown event
    types updating liveness only
  - Supplied-input selection for tracked AGENTS.local.md, untracked
    AGENTS.local.md, and consumer repo with gitignored/untracked AGENTS.md
  - Caller worktree and Git index unchanged after dispatch
  - Rolling 7-day budget guard refusal (GROK_WEEKLY_BUDGET_USD, exit 75)
  - Missing families.grok registry row (exit 70) and env overrides
  - Orchestrator self-review guard (IMPLEMENT_REVIEW_ORCHESTRATOR=grok, exit 2)
  - Preflight binary (exit 127) and login (exit 70, grok login) checks
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

import tests._quiet_spawn  # noqa: F401


REPO_ROOT = Path(__file__).resolve().parents[1]
DISPATCH_PY = (
    REPO_ROOT / "skills" / "implement-review" / "scripts" / "dispatch-grok.py"
)


def load_dispatch_module():
    spec = importlib.util.spec_from_file_location("dispatch_grok", DISPATCH_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DISPATCH = load_dispatch_module()


def valid_review_body(round_number: int = 1, extra: str = "") -> str:
    return (
        f"<!-- Round {round_number} -->\n"
        "# Review (Grok)\n\n"
        "Verification notes: ran pytest on target module (12 passed).\n\n"
        "Verification status: VERIFIED\n\n"
        "Commit verdict: PASS\n\n"
        "## Scope\n"
        "- Files changed: `tracked.txt`\n"
        "- Lens: code\n\n"
        "## Findings\n\n"
        "### New\n"
        "- [Low] `tracked.txt:1` — confirm edge handling on empty input.\n"
        "  Suggested check: verify behaviour with an empty fixture.\n\n"
        "### Previously raised\n"
        "None.\n"
        f"{extra}\n"
        + ("<!-- padding to exceed 500 bytes -->\n" * 6)
    )


FAKE_GROK_PY = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time

    argv = sys.argv[1:]
    if argv == ["--version"]:
        print(os.environ.get("FAKE_GROK_VERSION", "grok-cli 1.4.2-fake"))
        sys.exit(0)

    # Record invocation details if requested
    record_dir = os.environ.get("FAKE_GROK_RECORD_DIR", "")
    if record_dir:
        rec = Path(record_dir)
        rec.mkdir(parents=True, exist_ok=True)
        (rec / "argv.json").write_text(json.dumps(argv), encoding="utf-8")
        env_subset = {
            k: os.environ.get(k)
            for k in (
                "GROK_FOLDER_TRUST",
                "GROK_CLAUDE_HOOKS_ENABLED",
                "GROK_CLAUDE_MCPS_ENABLED",
                "GROK_CLAUDE_SKILLS_ENABLED",
                "GROK_CURSOR_HOOKS_ENABLED",
                "GROK_CURSOR_MCPS_ENABLED",
                "GROK_DISABLE_AUTOUPDATER",
                "GIT_PAGER",
            )
        }
        (rec / "env.json").write_text(json.dumps(env_subset), encoding="utf-8")
        (rec / "cwd.txt").write_text(os.getcwd(), encoding="utf-8")
        if "--prompt-file" in argv:
            idx = argv.index("--prompt-file")
            if idx + 1 < len(argv):
                pf = Path(argv[idx + 1])
                if pf.is_file():
                    (rec / "relay_prompt.txt").write_text(
                        pf.read_text(encoding="utf-8", errors="replace"),
                        encoding="utf-8",
                    )

    mode = os.environ.get("FAKE_GROK_MODE", "normal")
    session_id = os.environ.get("FAKE_GROK_SESSION_ID", "sess-grok-001")
    reported_model = os.environ.get("FAKE_GROK_REPORTED_MODEL", "grok-4.7-build")
    reported_effort = os.environ.get("FAKE_GROK_REPORTED_EFFORT", "high")
    grok_home = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok")))

    # Write session summary.json unless suppressed
    if os.environ.get("FAKE_GROK_SKIP_SUMMARY") != "1":
        sess_dir = grok_home / "sessions" / "proj" / session_id
        sess_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "reasoning_effort": reported_effort,
            "current_model_id": reported_model,
            "info": {"id": session_id},
        }
        (sess_dir / "summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    review_text = os.environ.get("FAKE_GROK_REVIEW_TEXT", "")

    if mode == "spawn_child_and_hang":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if record_dir:
            (Path(record_dir) / "child.pid").write_text(
                f"{child.pid}\\n", encoding="utf-8"
            )
            (Path(record_dir) / "parent.pid").write_text(
                f"{os.getpid()}\\n", encoding="utf-8"
            )
        emit({"type": "text", "data": "starting long operation...\\n"})
        time.sleep(120)
        sys.exit(0)

    if mode == "mutate_cwd":
        cwd_path = Path(os.getcwd())
        tracked = cwd_path / "tracked.txt"
        if tracked.is_file():
            tracked.write_text("MUTATED BY GROK INSIDE SNAPSHOT\\n", encoding="utf-8")
        (cwd_path / "grok-scratch.tmp").write_text("scratch\\n", encoding="utf-8")
        (cwd_path / "Review-Grok.md").write_text(review_text, encoding="utf-8")
        emit({"type": "text", "data": "wrote Review-Grok.md in snapshot\\n"})
        emit({
            "type": "end",
            "stopReason": "end_turn",
            "sessionId": session_id,
            "modelUsage": {reported_model: {"inputTokens": 10, "outputTokens": 20}},
            "total_cost_usd": 0.015,
        })
        sys.exit(0)

    if mode == "write_snapshot_file":
        cwd_path = Path(os.getcwd())
        (cwd_path / "Review-Grok.md").write_text(review_text, encoding="utf-8")
        emit({"type": "text", "data": "Done.\\n"})
        emit({
            "type": "end",
            "stopReason": "end_turn",
            "sessionId": session_id,
            "modelUsage": {reported_model: {"inputTokens": 100, "outputTokens": 200}},
            "total_cost_usd": 0.0125,
        })
        sys.exit(0)

    if mode == "narration_and_tools":
        emit({"type": "text", "data": "NARRATION_BEFORE_TOOL_1: inspecting files\\n"})
        emit({"type": "tool_call", "toolCallId": "tc-1", "toolName": "Read"})
        emit({"type": "tool_call_update", "toolCallId": "tc-1", "status": "done"})
        emit({"type": "thought", "data": "INTERNAL_THOUGHT_DO_NOT_INCLUDE"})
        emit({"type": "text", "data": "NARRATION_BEFORE_TOOL_2: running grep\\n"})
        emit({"type": "tool_call", "toolCallId": "tc-2", "toolName": "Grep"})
        emit({"type": "unknown_future_event", "data": "UNKNOWN_EVENT_PAYLOAD"})
        emit({"type": "text", "data": review_text})
        emit({
            "type": "end",
            "stopReason": "end_turn",
            "sessionId": session_id,
            "modelUsage": {reported_model: {"inputTokens": 50, "outputTokens": 150}},
            "total_cost_usd": 0.02,
        })
        sys.exit(0)

    if mode == "nothing_after_last_tool_call":
        emit({"type": "text", "data": review_text})
        emit({"type": "tool_call", "toolCallId": "tc-final", "toolName": "LS"})
        emit({"type": "tool_call_update", "toolCallId": "tc-final", "status": "done"})
        emit({
            "type": "end",
            "stopReason": "end_turn",
            "sessionId": session_id,
            "modelUsage": {reported_model: {"inputTokens": 50, "outputTokens": 150}},
            "total_cost_usd": 0.01,
        })
        sys.exit(0)

    if mode == "multi_events_long_run":
        for i in range(6):
            emit({"type": "thought", "data": f"step {i}"})
        emit({"type": "text", "data": review_text})
        emit({
            "type": "end",
            "stopReason": "end_turn",
            "sessionId": session_id,
            "modelUsage": {reported_model: {"inputTokens": 50, "outputTokens": 150}},
            "total_cost_usd": 0.03,
        })
        sys.exit(0)

    if mode == "silent_stall_then_finish":
        sleep_sec = float(os.environ.get("FAKE_GROK_STALL_SLEEP", "2.2"))
        time.sleep(sleep_sec)
        emit({"type": "text", "data": review_text})
        emit({
            "type": "end",
            "stopReason": "end_turn",
            "sessionId": session_id,
            "modelUsage": {reported_model: {"inputTokens": 50, "outputTokens": 150}},
            "total_cost_usd": 0.01,
        })
        sys.exit(0)

    if mode == "missing_end":
        (Path(os.getcwd()) / "Review-Grok.md").write_text(review_text, encoding="utf-8")
        emit({"type": "text", "data": review_text})
        sys.exit(0)

    if mode == "error_event":
        (Path(os.getcwd()) / "Review-Grok.md").write_text(review_text, encoding="utf-8")
        emit({"type": "text", "data": review_text})
        emit({"type": "error", "message": "authentication or quota error"})
        emit({
            "type": "end",
            "stopReason": "end_turn",
            "sessionId": session_id,
            "modelUsage": {reported_model: {"inputTokens": 10, "outputTokens": 10}},
        })
        sys.exit(0)

    if mode == "custom_stop_reason":
        stop_reason = os.environ.get("FAKE_GROK_STOP_REASON", "max_tokens")
        (Path(os.getcwd()) / "Review-Grok.md").write_text(review_text, encoding="utf-8")
        emit({"type": "text", "data": review_text})
        emit({
            "type": "end",
            "stopReason": stop_reason,
            "sessionId": session_id,
            "modelUsage": {reported_model: {"inputTokens": 10, "outputTokens": 10}},
            "total_cost_usd": 0.01,
        })
        sys.exit(0)

    if mode == "partial_cost":
        emit({"type": "text", "data": review_text})
        end_payload = {
            "type": "end",
            "stopReason": "end_turn",
            "sessionId": session_id,
            "modelUsage": {reported_model: {"inputTokens": 10, "outputTokens": 10}},
        }
        if os.environ.get("FAKE_GROK_COST_PARTIAL_FLAG") == "1":
            end_payload["total_cost_usd"] = 0.005
            end_payload["cost_is_partial"] = True
        emit(end_payload)
        sys.exit(0)

    # Default "normal" mode: emit review via text stream
    emit({"type": "text", "data": review_text})
    emit({
        "type": "end",
        "stopReason": "end_turn",
        "sessionId": session_id,
        "modelUsage": {reported_model: {"inputTokens": 100, "outputTokens": 250}},
        "total_cost_usd": 0.0125,
    })
    sys.exit(0)
    """
)


class DispatchGrokHarness(unittest.TestCase):
    """Base test fixture with an isolated git repo, fake GROK_HOME, and fake grok CLI."""

    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="test-dispatch-grok-"))
        self.repo_dir = self.temp_root / "repo"
        self.repo_dir.mkdir(parents=True)
        self.grok_home = self.temp_root / "grok-home"
        self.grok_home.mkdir(parents=True)
        (self.grok_home / "auth.json").write_text(
            '{"access_token": "fake-token"}\n', encoding="utf-8"
        )

        self.bin_dir = self.temp_root / "bin"
        self.bin_dir.mkdir(parents=True)
        self.fake_grok = self._install_fake_grok(self.bin_dir)
        self.record_dir = self.temp_root / "record"
        self.record_dir.mkdir(parents=True)

        self._init_git_repo(self.repo_dir)
        self.prompt_file = self.temp_root / "prompt.txt"
        self.prompt_file.write_text(
            "IMPORTANT: Save your complete review to Review-<YourAgentName>.md\n"
            "Review staged changes in repo. Round 1.\n",
            encoding="utf-8",
        )
        self.created_state_dirs: list[Path] = []

    def tearDown(self) -> None:
        for sd in self.created_state_dirs:
            shutil.rmtree(sd, ignore_errors=True)
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _install_fake_grok(self, target_dir: Path) -> Path:
        py_script = target_dir / "fake_grok.py"
        py_script.write_text(FAKE_GROK_PY, encoding="utf-8")
        py_script.chmod(py_script.stat().st_mode | stat.S_IXUSR)

        if os.name == "nt":
            wrapper = target_dir / "grok.cmd"
            wrapper.write_text(
                f'@echo off\r\n"{sys.executable}" "{py_script}" %*\r\n',
                encoding="utf-8",
            )
            return wrapper
        wrapper = target_dir / "grok"
        wrapper.write_text(
            f'#!/usr/bin/env bash\nexec "{sys.executable}" "{py_script}" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        return wrapper

    def _init_git_repo(self, repo: Path) -> None:
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "config", "user.name", "Test User"],
        ):
            subprocess.run(cmd, cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        (repo / "AGENTS.md").write_text("# Shared AGENTS rules\n", encoding="utf-8")
        (repo / "tracked.txt").write_text("line 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "AGENTS.md", "tracked.txt"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Stage a change for review
        (repo / "tracked.txt").write_text("line 1\nline 2 staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "tracked.txt"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _base_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        for key in (
            "IMPLEMENT_REVIEW_ORCHESTRATOR",
            "IMPLEMENT_REVIEW_ALLOW_STAGED_DRIFT",
            "GROK_DISPATCH_MODEL",
            "GROK_DISPATCH_EFFORT",
            "GROK_DISPATCH_TIMEOUT_SECONDS",
            "GROK_DISPATCH_KILL_GRACE_SECONDS",
            "GROK_DISPATCH_CLOCK_STEP_SECONDS",
            "GROK_DISPATCH_NOW_EPOCH",
            "GROK_WEEKLY_BUDGET_USD",
            "GROK_AUTH_PATH",
            "XAI_API_KEY",
            "AGENT_MODELS_REGISTRY",
        ):
            env.pop(key, None)
        env["GROK_HOME"] = str(self.grok_home)
        env["GROK_BIN"] = str(self.fake_grok)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_GROK_RECORD_DIR"] = str(self.record_dir)
        env["FAKE_GROK_REVIEW_TEXT"] = valid_review_body(1)
        if extra:
            env.update(extra)
        return env

    def _run_dispatch(
        self,
        round_number: int = 1,
        expected_file: str = "Review-Grok.md",
        extra_env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path | None]:
        env = self._base_env(extra_env)
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH_PY),
                "--prompt-file",
                str(self.prompt_file),
                "--round",
                str(round_number),
                "--expected-review-file",
                expected_file,
            ],
            cwd=str(cwd or self.repo_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        state_dir: Path | None = None
        stdout_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        if stdout_lines and stdout_lines[0].startswith("STATE-DIR "):
            state_dir = Path(stdout_lines[0].split(" ", 1)[1].strip())
            self.created_state_dirs.append(state_dir)
        return proc, state_dir


class TestDispatchGrokNormalAndStreaming(DispatchGrokHarness):
    def test_normal_round_publishes_review_and_records_state(self) -> None:
        proc, state_dir = self._run_dispatch(
            extra_env={"FAKE_GROK_MODE": "write_snapshot_file"}
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIsNotNone(state_dir)
        assert state_dir is not None

        # Only one stdout line: STATE-DIR <abs-path>
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        self.assertTrue(state_dir.name.startswith("implement-review-grok-"))
        self.assertIn("-round1-", state_dir.name)

        # State-dir contract files
        ts_text = (state_dir / "timestamp").read_text(encoding="utf-8").strip()
        pre_text = (state_dir / "pre-mtime").read_text(encoding="utf-8").strip()
        self.assertTrue(ts_text.isdigit(), f"non-integer timestamp: {ts_text!r}")
        self.assertEqual(pre_text, "0")

        self.assertEqual(
            (state_dir / "grok-version").read_text(encoding="utf-8").strip(),
            "grok-cli 1.4.2-fake",
        )
        self.assertEqual(
            (state_dir / "cost-usd").read_text(encoding="utf-8").strip(),
            "0.0125",
        )
        review_model_lines = (
            (state_dir / "review-model").read_text(encoding="utf-8").strip().splitlines()
        )
        self.assertEqual(
            review_model_lines,
            [
                "model=grok-4.7 source=registry effort=high effort_source=registry",
                "actual=grok-4.7-build high status=ok",
            ],
        )

        # Verify child env isolation and CLI flags
        recorded_env = json.loads(
            (self.record_dir / "env.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            recorded_env,
            {
                "GROK_FOLDER_TRUST": "0",
                "GROK_CLAUDE_HOOKS_ENABLED": "false",
                "GROK_CLAUDE_MCPS_ENABLED": "false",
                "GROK_CLAUDE_SKILLS_ENABLED": "false",
                "GROK_CURSOR_HOOKS_ENABLED": "false",
                "GROK_CURSOR_MCPS_ENABLED": "false",
                "GROK_DISABLE_AUTOUPDATER": "1",
                "GIT_PAGER": "cat",
            },
        )
        recorded_argv = json.loads(
            (self.record_dir / "argv.json").read_text(encoding="utf-8")
        )
        self.assertIn("--always-approve", recorded_argv)
        self.assertIn("--no-auto-update", recorded_argv)
        self.assertIn("streaming-json", recorded_argv)
        self.assertIn("-m", recorded_argv)
        self.assertEqual(recorded_argv[recorded_argv.index("-m") + 1], "grok-4.7")
        self.assertIn("--effort", recorded_argv)
        self.assertEqual(recorded_argv[recorded_argv.index("--effort") + 1], "high")

        # Verify published Review-Grok.md in repo root
        published = self.repo_dir / "Review-Grok.md"
        self.assertTrue(published.is_file())
        content = published.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("<!-- Round 1 -->\n"))
        self.assertGreaterEqual(len(content.encode("utf-8")), 500)

    def test_narration_before_tool_calls_is_excluded_and_unknown_events_ignored(self) -> None:
        proc, state_dir = self._run_dispatch(
            extra_env={"FAKE_GROK_MODE": "narration_and_tools"}
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        assert state_dir is not None

        published = (self.repo_dir / "Review-Grok.md").read_text(encoding="utf-8")
        self.assertTrue(published.startswith("<!-- Round 1 -->\n"))
        self.assertNotIn("NARRATION_BEFORE_TOOL_1", published)
        self.assertNotIn("NARRATION_BEFORE_TOOL_2", published)
        self.assertNotIn("INTERNAL_THOUGHT_DO_NOT_INCLUDE", published)
        self.assertNotIn("UNKNOWN_EVENT_PAYLOAD", published)

        # All raw events are still flushed to tail for liveness/diagnostics
        tail_text = (state_dir / "tail").read_text(encoding="utf-8")
        self.assertIn("NARRATION_BEFORE_TOOL_1", tail_text)
        self.assertIn("unknown_future_event", tail_text)

    def test_fallback_to_full_stream_text_when_nothing_follows_last_tool_call(self) -> None:
        proc, _ = self._run_dispatch(
            extra_env={"FAKE_GROK_MODE": "nothing_after_last_tool_call"}
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        published = (self.repo_dir / "Review-Grok.md").read_text(encoding="utf-8")
        self.assertTrue(published.startswith("<!-- Round 1 -->\n"))
        self.assertGreaterEqual(len(published.encode("utf-8")), 500)

    def test_run_exceeding_11_minutes_with_regular_events_succeeds(self) -> None:
        """6 events * 150s virtual clock step = 900s (15 min > 11 min), under 2700s limit."""
        proc, state_dir = self._run_dispatch(
            extra_env={
                "FAKE_GROK_MODE": "multi_events_long_run",
                "GROK_DISPATCH_CLOCK_STEP_SECONDS": "150",
            }
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIsNotNone(state_dir)
        self.assertTrue((self.repo_dir / "Review-Grok.md").is_file())

    def test_silent_stall_writes_warning_only_and_completes(self) -> None:
        proc, state_dir = self._run_dispatch(
            extra_env={
                "FAKE_GROK_MODE": "silent_stall_then_finish",
                "FAKE_GROK_STALL_SLEEP": "3.2",
                "IMPLEMENT_REVIEW_STALL_SECONDS": "1",
                "IMPLEMENT_REVIEW_STALL_CHECK_INTERVAL": "1",
            }
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        assert state_dir is not None
        stall_file = state_dir / "stall-warning"
        self.assertTrue(stall_file.is_file(), "stall-watch should write stall-warning")
        self.assertIn("STALL ", stall_file.read_text(encoding="utf-8"))
        self.assertTrue((self.repo_dir / "Review-Grok.md").is_file())


class TestDispatchGrokTerminalFailuresAndCost(DispatchGrokHarness):
    def test_missing_end_event_fails_and_does_not_publish(self) -> None:
        proc, _ = self._run_dispatch(extra_env={"FAKE_GROK_MODE": "missing_end"})
        self.assertEqual(proc.returncode, 70)
        self.assertIn("without an 'end' event", proc.stderr)
        self.assertFalse((self.repo_dir / "Review-Grok.md").exists())

    def test_error_event_fails_even_when_snapshot_review_exists(self) -> None:
        proc, _ = self._run_dispatch(extra_env={"FAKE_GROK_MODE": "error_event"})
        self.assertEqual(proc.returncode, 70)
        self.assertIn("error event", proc.stderr)
        self.assertFalse((self.repo_dir / "Review-Grok.md").exists())

    def test_each_non_end_turn_stop_reason_fails(self) -> None:
        for reason in ("max_tokens", "max_turn_requests", "refusal", "cancelled"):
            with self.subTest(stopReason=reason):
                review_path = self.repo_dir / "Review-Grok.md"
                if review_path.exists():
                    review_path.unlink()
                proc, _ = self._run_dispatch(
                    extra_env={
                        "FAKE_GROK_MODE": "custom_stop_reason",
                        "FAKE_GROK_STOP_REASON": reason,
                    }
                )
                self.assertEqual(proc.returncode, 70)
                self.assertIn(f"stopReason={reason!r}", proc.stderr)
                self.assertFalse(review_path.exists())

    def test_missing_or_partial_cost_recorded_as_unknown_not_zero(self) -> None:
        for flag in ("0", "1"):
            with self.subTest(cost_is_partial=flag):
                proc, state_dir = self._run_dispatch(
                    extra_env={
                        "FAKE_GROK_MODE": "partial_cost",
                        "FAKE_GROK_COST_PARTIAL_FLAG": flag,
                    }
                )
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                assert state_dir is not None
                cost = (state_dir / "cost-usd").read_text(encoding="utf-8").strip()
                self.assertEqual(cost, "unknown")
                self.assertNotIn(cost, {"0", "0.0"})

    @unittest.skipIf(os.name == "nt", "POSIX process-group tree check")
    def test_process_tree_cleanup_on_absolute_timeout(self) -> None:
        proc, _ = self._run_dispatch(
            extra_env={
                "FAKE_GROK_MODE": "spawn_child_and_hang",
                "GROK_DISPATCH_TIMEOUT_SECONDS": "0.6",
                "GROK_DISPATCH_KILL_GRACE_SECONDS": "0.2",
            }
        )
        self.assertEqual(proc.returncode, 70)
        self.assertIn("timed out", proc.stderr)

        child_pid_file = self.record_dir / "child.pid"
        self.assertTrue(child_pid_file.is_file())
        child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())

        # Both parent and spawned descendant must be reaped
        time.sleep(0.1)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)


class TestDispatchGrokSnapshotAndSuppliedInputs(DispatchGrokHarness):
    def test_worktree_and_index_remain_unchanged(self) -> None:
        before_diff = subprocess.check_output(
            ["git", "diff"], cwd=self.repo_dir, text=True
        )
        before_cached = subprocess.check_output(
            ["git", "diff", "--cached"], cwd=self.repo_dir, text=True
        )
        before_tracked = (self.repo_dir / "tracked.txt").read_text(encoding="utf-8")

        proc, _ = self._run_dispatch(extra_env={"FAKE_GROK_MODE": "mutate_cwd"})
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

        after_diff = subprocess.check_output(
            ["git", "diff"], cwd=self.repo_dir, text=True
        )
        after_cached = subprocess.check_output(
            ["git", "diff", "--cached"], cwd=self.repo_dir, text=True
        )
        after_tracked = (self.repo_dir / "tracked.txt").read_text(encoding="utf-8")

        self.assertEqual(before_diff, after_diff)
        self.assertEqual(before_cached, after_cached)
        self.assertEqual(before_tracked, after_tracked)
        self.assertFalse((self.repo_dir / "grok-scratch.tmp").exists())
        self.assertTrue((self.repo_dir / "Review-Grok.md").is_file())

    def test_supplied_input_tracked_and_untracked_agents_local(self) -> None:
        # 1. Untracked AGENTS.local.md in worktree; AGENTS.md is tracked
        (self.repo_dir / "AGENTS.local.md").write_text(
            "LOCAL_UNTRACKED_RULE=1\n", encoding="utf-8"
        )
        proc, _ = self._run_dispatch()
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        relay_untracked = (self.record_dir / "relay_prompt.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("--- SUPPLIED INPUT: AGENTS.local.md ---", relay_untracked)
        self.assertIn("LOCAL_UNTRACKED_RULE=1", relay_untracked)
        # Tracked AGENTS.md is already in snapshot and must not be duplicated
        self.assertNotIn("--- SUPPLIED INPUT: AGENTS.md ---", relay_untracked)
        self.assertIn("local overrides in `AGENTS.local.md` win over shared", relay_untracked)
        self.assertIn("do not read supplied files again from disk", relay_untracked)

        # 2. Tracked AGENTS.local.md in index
        (self.repo_dir / "AGENTS.local.md").write_text(
            "LOCAL_TRACKED_RULE=2\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "add", "AGENTS.local.md"],
            cwd=self.repo_dir,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc2, _ = self._run_dispatch()
        self.assertEqual(proc2.returncode, 0, msg=proc2.stderr)
        relay_tracked = (self.record_dir / "relay_prompt.txt").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            relay_tracked.count("--- SUPPLIED INPUT: AGENTS.local.md ---"), 1
        )
        self.assertIn("LOCAL_TRACKED_RULE=2", relay_tracked)
        self.assertNotIn("--- SUPPLIED INPUT: AGENTS.md ---", relay_tracked)

    def test_supplied_input_consumer_with_gitignored_agents_md(self) -> None:
        # Model a consumer: AGENTS.md is gitignored and untracked in HEAD, not
        # a pending staged deletion (which the drift check rightly refuses).
        with (self.repo_dir / ".gitignore").open("a", encoding="utf-8") as fh:
            fh.write("/AGENTS.md\n")
        for args in (
            ["git", "rm", "--cached", "-q", "AGENTS.md"],
            ["git", "add", ".gitignore"],
            ["git", "commit", "-q", "-m", "untrack AGENTS.md"],
        ):
            subprocess.run(
                args,
                cwd=self.repo_dir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        # The commit took the staged review change with it; stage a new one.
        (self.repo_dir / "tracked.txt").write_text("line 1\nline 2 restaged\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "tracked.txt"],
            cwd=self.repo_dir,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "ls-files", "AGENTS.md"], cwd=self.repo_dir,
                stdout=subprocess.PIPE, text=True, check=True,
            ).stdout.strip(),
            "",
        )
        (self.repo_dir / "AGENTS.md").write_text(
            "# Consumer gitignored AGENTS.md\n", encoding="utf-8"
        )
        # Ensure AGENTS.local.md does not exist
        local_md = self.repo_dir / "AGENTS.local.md"
        if local_md.exists():
            local_md.unlink()

        proc, _ = self._run_dispatch()
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        relay = (self.record_dir / "relay_prompt.txt").read_text(encoding="utf-8")
        self.assertEqual(relay.count("--- SUPPLIED INPUT: AGENTS.md ---"), 1)
        self.assertIn("# Consumer gitignored AGENTS.md", relay)
        self.assertNotIn("--- SUPPLIED INPUT: AGENTS.local.md ---", relay)


class TestDispatchGrokBudgetRegistryAndGuards(DispatchGrokHarness):
    def test_weekly_budget_guard_refuses_when_7d_spend_reaches_cap(self) -> None:
        now = 1_760_000_000.0
        recent_iso = (
            datetime.datetime.fromtimestamp(now - 2 * 86400, tz=datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        old_iso = (
            datetime.datetime.fromtimestamp(now - 10 * 86400, tz=datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

        # Session 1: touched recently; has one recent turn ($1.25) and one 10-day-old turn ($9.00)
        s1 = self.grok_home / "sessions" / "p1" / "s1"
        s1.mkdir(parents=True)
        u1 = s1 / "usage.json"
        u1.write_text(
            json.dumps(
                {
                    "turns": [
                        {"turnNumber": 1, "endedAt": old_iso, "costUsdTicks": 90_000_000_000},
                        {"turnNumber": 2, "endedAt": recent_iso, "costUsdTicks": 12_500_000_000},
                    ]
                }
            ),
            encoding="utf-8",
        )
        os.utime(u1, (now - 100, now - 100))

        # Session 2: file mtime is 10 days old (skipped by mtime filter)
        s2 = self.grok_home / "sessions" / "p1" / "s2"
        s2.mkdir(parents=True)
        u2 = s2 / "usage.json"
        u2.write_text(
            json.dumps(
                {
                    "turns": [
                        {"turnNumber": 1, "endedAt": recent_iso, "costUsdTicks": 50_000_000_000}
                    ]
                }
            ),
            encoding="utf-8",
        )
        os.utime(u2, (now - 10 * 86400, now - 10 * 86400))

        # 7-day spend is $1.25. A $1.25 budget refuses with exit 75.
        proc_refuse, state_dir = self._run_dispatch(
            extra_env={
                "GROK_WEEKLY_BUDGET_USD": "1.25",
                "GROK_DISPATCH_NOW_EPOCH": str(now),
            }
        )
        self.assertEqual(proc_refuse.returncode, 75)
        self.assertIsNone(state_dir)
        self.assertIn("1.2500", proc_refuse.stderr)
        self.assertIn("GROK_WEEKLY_BUDGET_USD", proc_refuse.stderr)
        self.assertFalse((self.record_dir / "argv.json").exists())

        # A $2.00 budget allows the dispatch.
        proc_ok, _ = self._run_dispatch(
            extra_env={
                "GROK_WEEKLY_BUDGET_USD": "2.00",
                "GROK_DISPATCH_NOW_EPOCH": str(now),
            }
        )
        self.assertEqual(proc_ok.returncode, 0, msg=proc_ok.stderr)

        # A cap that was set but cannot be read refuses rather than lifting
        # the cap, and never launches grok.
        for bad in ("not-a-number", "10usd", "-1", "nan", "inf"):
            (self.record_dir / "argv.json").unlink(missing_ok=True)
            proc_bad, _ = self._run_dispatch(
                extra_env={
                    "GROK_WEEKLY_BUDGET_USD": bad,
                    "GROK_DISPATCH_NOW_EPOCH": str(now),
                }
            )
            self.assertEqual(proc_bad.returncode, 75, msg=f"{bad}: {proc_bad.stderr}")
            self.assertIn("GROK_WEEKLY_BUDGET_USD", proc_bad.stderr)
            self.assertFalse((self.record_dir / "argv.json").exists(), msg=bad)

        # Zero is a valid budget that refuses every dispatch.
        proc_zero, _ = self._run_dispatch(
            extra_env={
                "GROK_WEEKLY_BUDGET_USD": "0",
                "GROK_DISPATCH_NOW_EPOCH": str(now),
            }
        )
        self.assertEqual(proc_zero.returncode, 75, msg=proc_zero.stderr)

    def test_missing_registry_row_exits_70_and_env_overrides_work(self) -> None:
        reg_without_grok = self.temp_root / "no-grok-models.json"
        reg_without_grok.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "families": {
                        "claude": {"model": "claude-opus-4-7", "effort": "xhigh"},
                        "codex": {"model": "gpt-5.4", "effort": "xhigh"},
                        "agy": {"model": "gemini-3.1-pro-high", "effort": "high"},
                    },
                }
            ),
            encoding="utf-8",
        )
        proc_missing, _ = self._run_dispatch(
            extra_env={"AGENT_MODELS_FILE": str(reg_without_grok)}
        )
        self.assertEqual(proc_missing.returncode, 70, msg=proc_missing.stderr)
        self.assertIn("families.grok", proc_missing.stderr)

        # Explicit valid env overrides bypass the missing registry row
        proc_override, state_dir = self._run_dispatch(
            extra_env={
                "AGENT_MODELS_FILE": str(reg_without_grok),
                "GROK_DISPATCH_MODEL": "grok-4.7",
                "GROK_DISPATCH_EFFORT": "medium",
                "FAKE_GROK_REPORTED_EFFORT": "medium",
            }
        )
        self.assertEqual(proc_override.returncode, 0, msg=proc_override.stderr)
        assert state_dir is not None
        rm_text = (state_dir / "review-model").read_text(encoding="utf-8")
        self.assertIn(
            "model=grok-4.7 source=env effort=medium effort_source=env", rm_text
        )

    def test_orchestrator_self_review_guard_exits_2(self) -> None:
        proc, state_dir = self._run_dispatch(
            extra_env={"IMPLEMENT_REVIEW_ORCHESTRATOR": "grok"}
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIsNone(state_dir)
        self.assertIn("IMPLEMENT_REVIEW_ORCHESTRATOR=grok", proc.stderr)

    @unittest.skipIf(
        os.name == "nt",
        "Grok is not exercised on Windows yet: a missing GROK_BIN there "
        "surfaces as exit 1, not the POSIX 127 this test pins",
    )
    def test_preflight_missing_binary_and_missing_login(self) -> None:
        proc_bin, _ = self._run_dispatch(
            extra_env={
                "GROK_BIN": str(self.temp_root / "nonexistent-grok"),
                "PATH": "/usr/bin:/bin",
            }
        )
        self.assertEqual(proc_bin.returncode, 127)

        (self.grok_home / "auth.json").unlink()
        proc_login, _ = self._run_dispatch()
        self.assertEqual(proc_login.returncode, 70)
        self.assertIn("grok login", proc_login.stderr)


class TestDispatchGrokStagedDrift(DispatchGrokHarness):
    """A recreated path the index deletes or renames away is drift too."""

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.repo_dir, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def _assert_refused(self, proc: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("refusing to dispatch", proc.stderr)
        self.assertIn("tracked.txt", proc.stderr)
        self.assertFalse((self.record_dir / "argv.json").exists())

    def test_staged_deletion_with_the_file_recreated_is_refused(self) -> None:
        self._git("rm", "-q", "-f", "tracked.txt")
        (self.repo_dir / "tracked.txt").write_text("back again\n", encoding="utf-8")
        proc, _ = self._run_dispatch()
        self._assert_refused(proc)

    def test_staged_rename_with_the_old_path_recreated_is_refused(self) -> None:
        self._git("commit", "-q", "-m", "stage baseline")
        self._git("mv", "tracked.txt", "moved.txt")
        (self.repo_dir / "tracked.txt").write_text("back again\n", encoding="utf-8")
        proc, _ = self._run_dispatch()
        self._assert_refused(proc)


class ConsumerLayoutRegistryTests(unittest.TestCase):
    """A consumer deploys the skill and the resolver in sibling trees."""

    def test_dispatcher_under_claude_skills_finds_the_bootstrapped_resolver(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "consumer"
            skill_scripts = project / ".claude" / "skills" / "implement-review" / "scripts"
            resolver_root = project / ".agent-config" / "repo"
            skill_scripts.mkdir(parents=True)
            (resolver_root / "scripts").mkdir(parents=True)
            (resolver_root / "user").mkdir(parents=True)
            shutil.copy2(
                repo / "skills" / "implement-review" / "scripts" / "dispatch-grok.py",
                skill_scripts / "dispatch-grok.py",
            )
            shutil.copy2(repo / "scripts" / "agent_models.py", resolver_root / "scripts" / "agent_models.py")
            shutil.copy2(repo / "user" / "agent-models.json", resolver_root / "user" / "agent-models.json")

            env_keys = ("GROK_DISPATCH_MODEL", "GROK_DISPATCH_EFFORT", "AGENT_MODELS_FILE")
            saved = {k: os.environ.pop(k) for k in env_keys if k in os.environ}
            try:
                spec = importlib.util.spec_from_file_location(
                    "dispatch_grok_consumer", skill_scripts / "dispatch-grok.py"
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                found = module.find_agent_models()
                self.assertIsNotNone(found)
                self.assertEqual(
                    Path(found).resolve(),
                    (resolver_root / "scripts" / "agent_models.py").resolve(),
                )
                self.assertIsNotNone(module.AGENT_MODELS)
                row = module.AGENT_MODELS.resolve("grok", "reviewer")
                self.assertTrue(row["model"])
                self.assertTrue(row["effort"])
            finally:
                os.environ.update(saved)


class WeeklySpendScanTests(unittest.TestCase):
    def test_a_fifo_named_usage_json_is_skipped_not_opened(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            now = datetime.datetime.now(datetime.timezone.utc)
            regular = home / "sessions" / "proj" / "s-file"
            regular.mkdir(parents=True)
            (regular / "usage.json").write_text(
                json.dumps({"turns": [{
                    "endedAt": (now - datetime.timedelta(hours=1)).isoformat(),
                    "costUsdTicks": int(1.25 * 1e10),
                }]}),
                encoding="utf-8",
            )
            fifo_dir = home / "sessions" / "proj" / "s-fifo"
            fifo_dir.mkdir(parents=True)
            os.mkfifo(fifo_dir / "usage.json")
            # In a child process, so a regression blocks there, not the suite.
            driver = (
                "import importlib.util, sys\n"
                "from pathlib import Path\n"
                "spec = importlib.util.spec_from_file_location('dg', sys.argv[1])\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "print(mod.compute_weekly_spend_usd(grok_home=Path(sys.argv[2])))\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", driver, str(DISPATCH_PY), str(home)],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertAlmostEqual(float(proc.stdout.strip()), 1.25, places=6)


if __name__ == "__main__":
    unittest.main()
