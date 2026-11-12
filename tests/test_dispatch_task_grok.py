"""Contract tests for prun's Grok task dispatcher (dispatch-task-grok.py)."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[1]
DISPATCH = ROOT / "skills" / "prun" / "scripts" / "dispatch-task-grok.py"
MONITOR_SH = ROOT / "skills" / "prun" / "scripts" / "monitor.sh"
GATHER_SH = ROOT / "skills" / "prun" / "scripts" / "gather.sh"
PYTHON = Path(sys.executable).resolve()


def registry_data(
    include_grok: bool = True,
    grok_model: str = "grok-9-test",
    grok_effort: str = "high",
) -> dict:
    data: dict = {
        "schema": 1,
        "families": {
            "claude": {"model": "fable", "effort": "high"},
            "codex": {"model": "gpt-6-astra", "effort": "medium"},
            "agy": {"model": "gemini-4-argon", "effort": "high"},
        },
        "roles": {
            "prun-worker": {
                "agy": {"model": "gemini-3.8-flash", "effort": "high"},
                "codex": {"effort": "high"},
            },
        },
    }
    if include_grok:
        data["families"]["grok"] = {"model": grok_model, "effort": grok_effort}
        data["roles"]["prun-worker"]["grok"] = {
            "model": grok_model,
            "effort": grok_effort,
        }
    return data


FAKE_GROK_SCRIPT = r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("0.0.10-fake-grok")
    raise SystemExit(0)

log_dir = Path(os.environ["FAKE_GROK_LOG_DIR"])
log_dir.mkdir(parents=True, exist_ok=True)
(log_dir / "args.json").write_text(json.dumps(args), encoding="utf-8")
(log_dir / "env.json").write_text(json.dumps(dict(os.environ)), encoding="utf-8")

if "--prompt-file" in args:
    p_idx = args.index("--prompt-file") + 1
    if p_idx < len(args):
        prompt_text = Path(args[p_idx]).read_text(encoding="utf-8")
        (log_dir / "prompt-relay.txt").write_text(prompt_text, encoding="utf-8")

mode = os.environ.get("FAKE_GROK_MODE", "normal")
session_id = os.environ.get("FAKE_GROK_SESSION_ID", "sess-grok-101")
model_id = os.environ.get("FAKE_GROK_ACTUAL_MODEL", "grok-9-test-build")
reported_effort = os.environ.get("FAKE_GROK_ACTUAL_EFFORT", "high")

# Write session summary.json unless suppressed
if os.environ.get("FAKE_GROK_WRITE_SUMMARY", "1") == "1":
    grok_home = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok")))
    sess_dir = grok_home / "sessions" / "hash-cwd" / session_id
    sess_dir.mkdir(parents=True, exist_ok=True)
    summary = {"current_model_id": "grok-9-test", "info": {"id": session_id}}
    if reported_effort:
        summary["reasoning_effort"] = reported_effort
    (sess_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

if os.environ.get("FAKE_GROK_SPAWN_CHILD") == "1":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (log_dir / "child.pid").write_text(str(child.pid), encoding="utf-8")

if mode == "idle_stall":
    emit({"type": "thought", "sessionId": session_id, "data": "starting"})
    time.sleep(300)
    raise SystemExit(0)

if mode == "busy_timeout":
    while True:
        emit({"type": "thought", "sessionId": session_id, "data": "still thinking"})
        time.sleep(0.15)

if mode == "missing_end":
    emit({
        "type": "text",
        "sessionId": session_id,
        "data": "# unit_a result\nConclusion: missing end\nFiles: none\nOpen items: none\nVerification: fake\n",
    })
    raise SystemExit(0)

if mode == "error_event":
    emit({
        "type": "text",
        "sessionId": session_id,
        "data": "# unit_a result\nConclusion: before error\nFiles: none\nOpen items: none\nVerification: fake\n",
    })
    emit({"type": "error", "sessionId": session_id, "message": "Authentication token expired"})
    raise SystemExit(0)

if mode == "narration_and_tools":
    emit({"type": "thought", "sessionId": session_id, "data": "Internal reasoning should be ignored"})
    emit({"type": "text", "sessionId": session_id, "data": "Narration chunk 1 before first tool call.\n"})
    emit({"type": "tool_call", "sessionId": session_id, "toolCallId": "tc1", "title": "Read file"})
    emit({"type": "tool_call_update", "sessionId": session_id, "toolCallId": "tc1", "status": "completed"})
    emit({"type": "text", "sessionId": session_id, "data": "Narration chunk 2 between tool calls.\n"})
    emit({"type": "tool_call", "sessionId": session_id, "toolCallId": "tc2", "title": "Run check"})
    emit({"type": "usage", "sessionId": session_id, "data": "tokens=100"})
    emit({
        "type": "text",
        "sessionId": session_id,
        "data": "# unit_a result\nConclusion: clean post-tool response\nFiles: none\nOpen items: none\nVerification: fake\n",
    })
    emit({
        "type": "end",
        "sessionId": session_id,
        "stopReason": "end_turn",
        "modelUsage": {model_id: {"inputTokens": 100, "outputTokens": 50}},
        "total_cost_usd": 0.0425,
    })
    raise SystemExit(0)

if mode == "only_pre_tool_text":
    emit({
        "type": "text",
        "sessionId": session_id,
        "data": "# unit_a result\nConclusion: fallback full-stream text\nFiles: none\nOpen items: none\nVerification: fake\n",
    })
    emit({"type": "tool_call", "sessionId": session_id, "toolCallId": "tc1", "title": "Read file"})
    emit({
        "type": "end",
        "sessionId": session_id,
        "stopReason": "end_turn",
        "modelUsage": {model_id: {"inputTokens": 50, "outputTokens": 20}},
    })
    raise SystemExit(0)

if mode == "worker_writes_result":
    target = Path(os.environ["FAKE_GROK_RESULT_TARGET"])
    target.write_text(
        "# unit_a result\nConclusion: worker wrote file directly\nFiles: a.py\nOpen items: none\nVerification: pytest\n",
        encoding="utf-8",
    )
    emit({
        "type": "text",
        "sessionId": session_id,
        "data": "# unit_a result\nConclusion: stream summary text\nFiles: a.py\nOpen items: none\nVerification: pytest\n",
    })
    emit({
        "type": "end",
        "sessionId": session_id,
        "stopReason": "end_turn",
        "modelUsage": {model_id: {"inputTokens": 80, "outputTokens": 40}},
        "total_cost_usd": 0.015,
    })
    raise SystemExit(0)

stop_reason = os.environ.get("FAKE_GROK_STOP_REASON", "end_turn")
emit({
    "type": "text",
    "sessionId": session_id,
    "data": "# unit_a result\nConclusion: normal unit ok\nFiles: none\nOpen items: none\nVerification: fake\n",
})
end_event = {
    "type": "end",
    "sessionId": session_id,
    "stopReason": stop_reason,
    "modelUsage": {model_id: {"inputTokens": 120, "outputTokens": 60}},
}
cost_mode = os.environ.get("FAKE_GROK_COST_MODE", "present")
if cost_mode == "present":
    end_event["total_cost_usd"] = 0.012345
elif cost_mode == "partial":
    end_event["total_cost_usd"] = 0.012345
    end_event["cost_is_partial"] = True
emit(end_event)
raise SystemExit(0)
"""


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@unittest.skipIf(
    os.name == "nt",
    "Grok is not exercised on Windows yet: the fake grok is a POSIX script "
    "that Windows cannot launch (WinError 193)",
)
class DispatchTaskGrokTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="test-grok-prun-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.fake_grok = self.bin_dir / "grok"
        self.fake_grok.write_text(FAKE_GROK_SCRIPT, encoding="utf-8")
        self.fake_grok.chmod(0o755)

        self.grok_home = self.root / "grok-home"
        self.grok_home.mkdir()
        (self.grok_home / "auth.json").write_text(
            '{"access_token": "oauth-fake"}', encoding="utf-8"
        )

        self.log_dir = self.root / "fake-log"
        self.log_dir.mkdir()

        self.registry_path = self.root / "agent-models.json"
        self.registry_path.write_text(
            json.dumps(registry_data(include_grok=True)), encoding="utf-8"
        )

        self.prompt_path = self.root / "unit_a.prompt.md"
        self.prompt_path.write_text("Investigate module X and report.\n", encoding="utf-8")
        self.result_path = self.root / "unit_a.result.md"
        self.state_dirs: list[Path] = []

    def tearDown(self) -> None:
        for sd in self.state_dirs:
            shutil.rmtree(sd, ignore_errors=True)

    def base_env(self, **overrides: str | None) -> dict[str, str]:
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("GROK_", "XAI_", "PRUN_")):
                env.pop(key, None)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["GROK_BIN"] = str(self.fake_grok)
        env["GROK_HOME"] = str(self.grok_home)
        env["AGENT_MODELS_FILE"] = str(self.registry_path)
        env["FAKE_GROK_LOG_DIR"] = str(self.log_dir)
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run_dispatch(
        self,
        extra_args: list[str] | None = None,
        env_overrides: dict[str, str | None] | None = None,
        result_path: Path | None = None,
        unit_id: str = "unit_a",
    ) -> tuple[subprocess.CompletedProcess, Path | None]:
        target_result = self.result_path if result_path is None else result_path
        cmd = [
            str(PYTHON),
            str(DISPATCH),
            "--prompt-file",
            str(self.prompt_path),
            "--result-file",
            str(target_result),
            "--unit-id",
            unit_id,
        ]
        if extra_args:
            cmd.extend(extra_args)
        env = self.base_env(**(env_overrides or {}))
        proc = subprocess.run(
            cmd,
            cwd=str(self.root),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        state_dir: Path | None = None
        for line in proc.stdout.splitlines():
            if line.startswith("STATE-DIR "):
                state_dir = Path(line.split(" ", 1)[1].strip())
                self.state_dirs.append(state_dir)
                break
        return proc, state_dir

    def test_normal_unit_contract_and_records(self) -> None:
        proc, state_dir = self.run_dispatch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNotNone(state_dir)
        assert state_dir is not None

        self.assertTrue(self.result_path.is_file())
        result_text = self.result_path.read_text(encoding="utf-8")
        self.assertIn("Conclusion: normal unit ok", result_text)

        # Verify CLI invocation and environment
        recorded_args = json.loads((self.log_dir / "args.json").read_text(encoding="utf-8"))
        self.assertIn("--always-approve", recorded_args)
        self.assertIn("--no-auto-update", recorded_args)
        self.assertEqual(
            recorded_args[recorded_args.index("--output-format") + 1],
            "streaming-json",
        )
        self.assertEqual(recorded_args[recorded_args.index("-m") + 1], "grok-9-test")
        self.assertEqual(recorded_args[recorded_args.index("--effort") + 1], "high")

        recorded_env = json.loads((self.log_dir / "env.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded_env.get("GROK_FOLDER_TRUST"), "0")
        self.assertEqual(recorded_env.get("GROK_CLAUDE_HOOKS_ENABLED"), "false")
        self.assertEqual(recorded_env.get("GROK_CLAUDE_MCPS_ENABLED"), "false")
        self.assertEqual(recorded_env.get("GROK_CLAUDE_SKILLS_ENABLED"), "false")
        self.assertEqual(recorded_env.get("GROK_CURSOR_HOOKS_ENABLED"), "false")
        self.assertEqual(recorded_env.get("GROK_CURSOR_MCPS_ENABLED"), "false")
        self.assertEqual(recorded_env.get("GROK_DISABLE_AUTOUPDATER"), "1")
        self.assertEqual(recorded_env.get("GIT_PAGER"), "cat")

        # Verify worker preamble
        relay = (self.log_dir / "prompt-relay.txt").read_text(encoding="utf-8")
        self.assertIn("Skip the bootstrap, banner, session-start, and router steps", relay)
        self.assertIn("applicable local overrides", relay)
        self.assertIn("Never commit, push", relay)
        self.assertIn("Investigate module X and report.", relay)

        # Verify state-dir files
        self.assertEqual(
            (state_dir / "grok-version").read_text(encoding="utf-8").strip(),
            "0.0.10-fake-grok",
        )
        self.assertEqual(
            (state_dir / "cost-usd").read_text(encoding="utf-8").strip(),
            "0.012345",
        )
        self.assertEqual(
            (state_dir / "model-actual").read_text(encoding="utf-8").strip(),
            "requested=grok-9-test high actual=grok-9-test-build high status=ok",
        )
        self.assertNotIn("MODEL-MISMATCH", proc.stderr)
        self.assertNotIn("MODEL-UNVERIFIED", proc.stderr)

    def test_missing_or_partial_cost_is_recorded_as_unknown(self) -> None:
        for idx, cost_mode in enumerate(("missing", "partial")):
            with self.subTest(cost_mode=cost_mode):
                res_file = self.root / f"cost_{idx}.result.md"
                proc, state_dir = self.run_dispatch(
                    env_overrides={"FAKE_GROK_COST_MODE": cost_mode},
                    result_path=res_file,
                    unit_id=f"cost_{idx}",
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                assert state_dir is not None
                self.assertEqual(
                    (state_dir / "cost-usd").read_text(encoding="utf-8").strip(),
                    "unknown",
                )

    def test_model_mismatch_and_unverified_reporting(self) -> None:
        res_mismatch = self.root / "mismatch.result.md"
        proc, state_dir = self.run_dispatch(
            env_overrides={"FAKE_GROK_ACTUAL_MODEL": "grok-9-test-build-fast"},
            result_path=res_mismatch,
            unit_id="unit_mismatch",
        )
        self.assertEqual(proc.returncode, 0)
        assert state_dir is not None
        self.assertIn("MODEL-MISMATCH", proc.stderr)
        self.assertIn(
            "status=mismatch",
            (state_dir / "model-actual").read_text(encoding="utf-8"),
        )

        res_unverified = self.root / "unverified.result.md"
        proc2, state_dir2 = self.run_dispatch(
            env_overrides={
                "FAKE_GROK_SESSION_ID": "sess-no-summary",
                "FAKE_GROK_WRITE_SUMMARY": "0",
            },
            result_path=res_unverified,
            unit_id="unit_unverified",
        )
        self.assertEqual(proc2.returncode, 0)
        assert state_dir2 is not None
        self.assertIn("MODEL-UNVERIFIED", proc2.stderr)
        self.assertIn(
            "status=unknown",
            (state_dir2 / "model-actual").read_text(encoding="utf-8"),
        )

    def test_worker_written_result_is_kept_and_response_stored_beside_it(self) -> None:
        proc, _ = self.run_dispatch(
            env_overrides={
                "FAKE_GROK_MODE": "worker_writes_result",
                "FAKE_GROK_RESULT_TARGET": str(self.result_path),
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "Conclusion: worker wrote file directly",
            self.result_path.read_text(encoding="utf-8"),
        )
        sibling = self.root / "unit_a.result.response.md"
        self.assertTrue(sibling.is_file())
        self.assertIn(
            "Conclusion: stream summary text",
            sibling.read_text(encoding="utf-8"),
        )

    def test_narration_before_tool_calls_is_excluded(self) -> None:
        proc, _ = self.run_dispatch(
            env_overrides={"FAKE_GROK_MODE": "narration_and_tools"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = self.result_path.read_text(encoding="utf-8")
        self.assertIn("Conclusion: clean post-tool response", text)
        self.assertNotIn("Narration chunk 1", text)
        self.assertNotIn("Narration chunk 2", text)
        self.assertNotIn("Internal reasoning", text)

        # Fallback to full-stream text when nothing follows the last tool_call
        res2 = self.root / "fallback_stream.result.md"
        proc2, _ = self.run_dispatch(
            env_overrides={"FAKE_GROK_MODE": "only_pre_tool_text"},
            result_path=res2,
            unit_id="unit_pre_tool",
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertIn(
            "Conclusion: fallback full-stream text",
            res2.read_text(encoding="utf-8"),
        )

    def test_idle_reaper_kills_process_tree_and_writes_fallback(self) -> None:
        proc, state_dir = self.run_dispatch(
            env_overrides={
                "FAKE_GROK_MODE": "idle_stall",
                "FAKE_GROK_SPAWN_CHILD": "1",
                "PRUN_STALL_THRESHOLD": "1",
                "GROK_KILL_GRACE_SECONDS": "0.2",
            }
        )
        self.assertEqual(proc.returncode, 124)
        self.assertIsNotNone(state_dir)
        self.assertTrue(self.result_path.is_file())
        first_line = self.result_path.read_text(encoding="utf-8").splitlines()[0]
        self.assertRegex(first_line, r"^# [A-Za-z0-9_-]+ result \(FALLBACK, ")

        child_pid = int((self.log_dir / "child.pid").read_text(encoding="utf-8").strip())
        self.assertFalse(
            _pid_exists(child_pid),
            f"spawned child process {child_pid} should have been reaped",
        )

    def test_absolute_timeout_kills_busy_process_tree_and_writes_fallback(self) -> None:
        proc, _ = self.run_dispatch(
            env_overrides={
                "FAKE_GROK_MODE": "busy_timeout",
                "FAKE_GROK_SPAWN_CHILD": "1",
                "PRUN_STALL_THRESHOLD": "60",
                "GROK_DISPATCH_TIMEOUT_SECONDS": "1",
                "GROK_KILL_GRACE_SECONDS": "0.2",
            }
        )
        self.assertEqual(proc.returncode, 124)
        first_line = self.result_path.read_text(encoding="utf-8").splitlines()[0]
        self.assertRegex(first_line, r"^# [A-Za-z0-9_-]+ result \(FALLBACK, ")
        child_pid = int((self.log_dir / "child.pid").read_text(encoding="utf-8").strip())
        self.assertFalse(_pid_exists(child_pid))

    def test_missing_end_and_error_event_and_non_end_turn_fail(self) -> None:
        # Missing end event
        res_no_end = self.root / "no_end.result.md"
        proc, _ = self.run_dispatch(
            env_overrides={"FAKE_GROK_MODE": "missing_end"},
            result_path=res_no_end,
            unit_id="no_end",
        )
        self.assertEqual(proc.returncode, 70)
        self.assertRegex(
            res_no_end.read_text(encoding="utf-8").splitlines()[0],
            r"^# no_end result \(FALLBACK, ",
        )

        # Error event
        res_err = self.root / "err.result.md"
        proc_err, _ = self.run_dispatch(
            env_overrides={"FAKE_GROK_MODE": "error_event"},
            result_path=res_err,
            unit_id="err_unit",
        )
        self.assertEqual(proc_err.returncode, 70)
        self.assertRegex(
            res_err.read_text(encoding="utf-8").splitlines()[0],
            r"^# err_unit result \(FALLBACK, ",
        )

        # Non-end_turn stopReasons
        for reason in ("max_tokens", "max_turn_requests", "refusal", "cancelled"):
            with self.subTest(stopReason=reason):
                res_stop = self.root / f"stop_{reason}.result.md"
                proc_stop, _ = self.run_dispatch(
                    env_overrides={"FAKE_GROK_STOP_REASON": reason},
                    result_path=res_stop,
                    unit_id=f"stop_{reason}",
                )
                self.assertEqual(proc_stop.returncode, 70)
                self.assertRegex(
                    res_stop.read_text(encoding="utf-8").splitlines()[0],
                    rf"^# stop_{reason} result \(FALLBACK, ",
                )

    def test_custom_grok_auth_path_with_no_default_auth_file(self) -> None:
        (self.grok_home / "auth.json").unlink()
        custom_auth = self.root / "custom-auth.json"
        custom_auth.write_text('{"token": "custom-oauth"}', encoding="utf-8")

        proc, _ = self.run_dispatch(
            env_overrides={"GROK_AUTH_PATH": str(custom_auth)}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        recorded_env = json.loads((self.log_dir / "env.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded_env.get("GROK_AUTH_PATH"), str(custom_auth))

    def test_inherited_xai_api_key_scrubbed_unless_prun_keep_env(self) -> None:
        (self.grok_home / "auth.json").unlink()

        # Without PRUN_KEEP_ENV, XAI_API_KEY is scrubbed and preflight fails with 70
        res_scrubbed = self.root / "scrubbed.result.md"
        proc, state_dir = self.run_dispatch(
            env_overrides={"XAI_API_KEY": "xai-live-secret"},
            result_path=res_scrubbed,
            unit_id="scrubbed",
        )
        self.assertEqual(proc.returncode, 70)
        self.assertIn("grok login", proc.stderr)
        assert state_dir is not None
        withheld = (state_dir / "env-withheld").read_text(encoding="utf-8").splitlines()
        self.assertIn("XAI_API_KEY", withheld)
        self.assertRegex(
            res_scrubbed.read_text(encoding="utf-8").splitlines()[0],
            r"^# scrubbed result \(FALLBACK, ",
        )

        # With PRUN_KEEP_ENV=XAI_API_KEY, XAI_API_KEY reaches the child and preflight passes
        res_kept = self.root / "kept.result.md"
        proc2, _ = self.run_dispatch(
            env_overrides={
                "XAI_API_KEY": "xai-live-secret",
                "PRUN_KEEP_ENV": "XAI_API_KEY",
            },
            result_path=res_kept,
            unit_id="kept",
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        recorded_env = json.loads((self.log_dir / "env.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded_env.get("XAI_API_KEY"), "xai-live-secret")

    def test_weekly_budget_guard_counts_recent_turns_only_and_refuses_at_cap(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        two_days_ago = (now - datetime.timedelta(days=2)).isoformat()
        ten_days_ago = (now - datetime.timedelta(days=10)).isoformat()

        sess_dir = self.grok_home / "sessions" / "proj" / "s-budget"
        sess_dir.mkdir(parents=True, exist_ok=True)
        usage_payload = {
            "turns": [
                {
                    "turnNumber": 1,
                    "endedAt": ten_days_ago,
                    "costUsdTicks": int(10.0 * 1e10),
                },
                {
                    "turnNumber": 2,
                    "endedAt": two_days_ago,
                    "costUsdTicks": int(3.50 * 1e10),
                },
            ]
        }
        (sess_dir / "usage.json").write_text(
            json.dumps(usage_payload), encoding="utf-8"
        )

        # Budget $3.00 <= recent spend $3.50 -> refused with exit 75
        res_refused = self.root / "budget_refused.result.md"
        proc, _ = self.run_dispatch(
            env_overrides={"GROK_WEEKLY_BUDGET_USD": "3.00"},
            result_path=res_refused,
            unit_id="budget_refused",
        )
        self.assertEqual(proc.returncode, 75)
        self.assertIn("$3.50", proc.stderr)
        self.assertIn("$3.00", proc.stderr)
        self.assertRegex(
            res_refused.read_text(encoding="utf-8").splitlines()[0],
            r"^# budget_refused result \(FALLBACK, ",
        )

        # Budget $4.00 > recent spend $3.50 (10-day-old $10 turn ignored) -> allowed
        res_allowed = self.root / "budget_allowed.result.md"
        proc2, _ = self.run_dispatch(
            env_overrides={"GROK_WEEKLY_BUDGET_USD": "4.00"},
            result_path=res_allowed,
            unit_id="budget_allowed",
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)

        # A cap that was set but cannot be read refuses instead of lifting it.
        for index, bad in enumerate(("10usd", "-1", "nan")):
            res_bad = self.root / f"budget_bad_{index}.result.md"
            proc_bad, _ = self.run_dispatch(
                env_overrides={"GROK_WEEKLY_BUDGET_USD": bad},
                result_path=res_bad,
                unit_id=f"budget_bad_{index}",
            )
            self.assertEqual(proc_bad.returncode, 75, msg=f"{bad}: {proc_bad.stderr}")
            self.assertIn("GROK_WEEKLY_BUDGET_USD", proc_bad.stderr)
            self.assertRegex(
                res_bad.read_text(encoding="utf-8").splitlines()[0],
                rf"^# budget_bad_{index} result \(FALLBACK, ",
            )

    def test_missing_registry_row_exits_70_and_missing_binary_exits_127(self) -> None:
        no_grok_reg = self.root / "no-grok-models.json"
        no_grok_reg.write_text(
            json.dumps(registry_data(include_grok=False)), encoding="utf-8"
        )
        proc, _ = self.run_dispatch(
            env_overrides={"AGENT_MODELS_FILE": str(no_grok_reg)},
            result_path=self.root / "no_reg.result.md",
            unit_id="no_reg",
        )
        self.assertEqual(proc.returncode, 70)
        self.assertIn("families.grok", proc.stderr)
        self.assertIn("agent-models skill", proc.stderr)

        # One override is not enough: the other value still needs the row.
        proc_one, _ = self.run_dispatch(
            env_overrides={
                "AGENT_MODELS_FILE": str(no_grok_reg),
                "GROK_DISPATCH_MODEL": "grok-test-model",
            },
            result_path=self.root / "no_reg_one.result.md",
            unit_id="no_reg_one",
        )
        self.assertEqual(proc_one.returncode, 70)

        # Both overrides carry the run without a registry row.
        proc_both, _ = self.run_dispatch(
            env_overrides={
                "AGENT_MODELS_FILE": str(no_grok_reg),
                "GROK_DISPATCH_MODEL": "grok-test-model",
                "GROK_DISPATCH_EFFORT": "high",
            },
            result_path=self.root / "no_reg_both.result.md",
            unit_id="no_reg_both",
        )
        self.assertEqual(proc_both.returncode, 0, proc_both.stderr)

        proc_bin, _ = self.run_dispatch(
            env_overrides={"GROK_BIN": str(self.root / "nonexistent-grok")},
            result_path=self.root / "no_bin.result.md",
            unit_id="no_bin",
        )
        self.assertEqual(proc_bin.returncode, 127)

    def test_scratch_cwd_requires_explicit_mode_and_continue_from_passes_resume(self) -> None:
        scratch = self.root / "caller-scratch"
        scratch.mkdir()

        # Without --mode, PRUN_SCRATCH_CWD is rejected
        proc_no_mode, _ = self.run_dispatch(
            env_overrides={"PRUN_SCRATCH_CWD": str(scratch)},
            result_path=self.root / "no_mode.result.md",
            unit_id="no_mode",
        )
        self.assertEqual(proc_no_mode.returncode, 2)

        # With --mode accept-edits, PRUN_SCRATCH_CWD succeeds
        proc_ok, state_dir_1 = self.run_dispatch(
            extra_args=["--mode", "accept-edits"],
            env_overrides={"PRUN_SCRATCH_CWD": str(scratch)},
            result_path=self.root / "mode_ok.result.md",
            unit_id="mode_ok",
        )
        self.assertEqual(proc_ok.returncode, 0, proc_ok.stderr)
        assert state_dir_1 is not None

        # Follow-up unit with --continue-from passes -r <sessionId>
        proc_cont, _ = self.run_dispatch(
            extra_args=["--continue-from", str(state_dir_1)],
            result_path=self.root / "continued.result.md",
            unit_id="continued",
        )
        self.assertEqual(proc_cont.returncode, 0, proc_cont.stderr)
        recorded_args = json.loads((self.log_dir / "args.json").read_text(encoding="utf-8"))
        self.assertIn("-r", recorded_args)
        self.assertEqual(recorded_args[recorded_args.index("-r") + 1], "sess-grok-101")

    @unittest.skipIf(os.name == "nt", "bash monitor.sh and gather.sh test runs on POSIX")
    def test_monitor_and_gather_classify_grok_unit_state_dir(self) -> None:
        ok_result = self.root / "ok_unit.result.md"
        proc_ok, state_ok = self.run_dispatch(
            result_path=ok_result,
            unit_id="ok_unit",
        )
        self.assertEqual(proc_ok.returncode, 0, proc_ok.stderr)
        assert state_ok is not None

        fast_env = os.environ.copy()
        fast_env["PRUN_MONITOR_STABLE_WINDOW"] = "0"
        fast_env["PRUN_MONITOR_POLL"] = "1"
        fast_env["PRUN_GATHER_STABLE_WINDOW"] = "0"
        fast_env["PRUN_GATHER_POLL"] = "1"

        mon_ok = subprocess.run(
            ["bash", str(MONITOR_SH), str(state_ok)],
            env=fast_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(mon_ok.returncode, 0, mon_ok.stderr)
        self.assertIn("MONITOR-EVENT all-done", mon_ok.stdout)
        self.assertIn(f"UNIT {state_ok.name} done", mon_ok.stdout)

        target_from_state = (state_ok / "result-file").read_text(encoding="utf-8").strip()
        gath_ok = subprocess.run(
            ["bash", str(GATHER_SH), target_from_state],
            env=fast_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(gath_ok.returncode, 0, gath_ok.stderr)
        self.assertIn(f"DONE {ok_result.resolve()}", gath_ok.stdout)

        # Failed unit (FALLBACK) is classified as failed(fallback) by monitor.sh
        fail_result = self.root / "fail_unit.result.md"
        proc_fail, state_fail = self.run_dispatch(
            env_overrides={"FAKE_GROK_STOP_REASON": "refusal"},
            result_path=fail_result,
            unit_id="fail_unit",
        )
        self.assertEqual(proc_fail.returncode, 70)
        assert state_fail is not None

        mon_fail = subprocess.run(
            ["bash", str(MONITOR_SH), str(state_fail)],
            env=fast_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(mon_fail.returncode, 3)
        self.assertIn("MONITOR-EVENT fail", mon_fail.stdout)
        self.assertIn(f"UNIT {state_fail.name} failed(fallback)", mon_fail.stdout)


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
                "spec = importlib.util.spec_from_file_location('dtg', sys.argv[1])\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "print(mod.compute_weekly_spend_usd(Path(sys.argv[2])))\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", driver, str(DISPATCH), str(home)],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertAlmostEqual(float(proc.stdout.strip()), 1.25, places=6)


if __name__ == "__main__":
    unittest.main()
