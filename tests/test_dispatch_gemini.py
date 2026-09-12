"""Contract tests for the Antigravity-backed Gemini reviewer dispatcher."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DISPATCH = ROOT / "skills" / "implement-review" / "scripts" / "dispatch-gemini.py"
PYTHON = Path(sys.executable).resolve()
GIT = shutil.which("git")


def load_dispatch_module():
    spec = importlib.util.spec_from_file_location("dispatch_gemini", DISPATCH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOCK_AGY = r'''#!/usr/bin/env python3
import json
import os
import re
import sys
from pathlib import Path

log = Path(os.environ["MOCK_AGY_LOG"])
log.mkdir(parents=True, exist_ok=True)
(log / "args.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")

if sys.argv[1:] == ["--version"]:
    print("Antigravity CLI 1.2.0")
    raise SystemExit(0)
if sys.argv[1:] == ["models"]:
    print(os.environ.get("MOCK_AGY_MODELS", "gemini-3.8-flash-high"))
    raise SystemExit(0)

event = json.loads(sys.stdin.readline())
prompt = event["message"]["content"]
(log / "prompt.txt").write_text(prompt, encoding="utf-8")
round_match = re.search(r"<!-- Round (\d+) -->", prompt)
round_num = round_match.group(1) if round_match else "1"
padding = "Independent review evidence. " * 24
response = (
    f"<!-- Round {round_num} -->\n\n"
    "## Verification notes\n\n"
    "- `git diff --cached --no-ext-diff` completed successfully.\n\n"
    "Verification status: VERIFIED\n\n"
    "## New\n\nNo findings. " + padding + "\n\n"
    "## Previously raised\n\nNone.\n\n"
    "Commit verdict: PASS\n"
)
print(json.dumps({"event": "init", "model": "gemini-3.8-flash-high"}))
payload = {"response": response}
# Older Antigravity builds omit status, so the fixture carries the field only
# when a test asks for it.
for key in ("status", "error"):
    value = os.environ.get("MOCK_AGY_" + key.upper())
    if value:
        payload[key] = value
print(json.dumps({"event": "result", "result": payload}))
'''


class DispatchGeminiUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_dispatch_module()

    def test_script_exists(self) -> None:
        self.assertTrue(DISPATCH.is_file())

    def test_defaults_pin_independent_gemini_model(self) -> None:
        self.assertEqual(self.module.DEFAULT_MODEL, "gemini-3.8-flash-high")
        self.assertEqual(self.module.DEFAULT_EFFORT, "high")

    def test_normalize_trims_preface_before_round_marker(self) -> None:
        value = self.module.normalize_review(
            "preface\n<!-- Round 4 -->\nbody\n", 4
        )
        self.assertEqual(value, "<!-- Round 4 -->\nbody\n")

    def test_normalize_inserts_missing_round_marker(self) -> None:
        value = self.module.normalize_review("body\n", 3)
        self.assertEqual(value, "<!-- Round 3 -->\n\nbody\n")

    def test_failure_reason_rejects_a_non_success_status(self) -> None:
        # Antigravity exits 0 when it stops on a quota limit, and that ERROR
        # event still carries the model's opening narration. Publishing it
        # would make the narration the round's review.
        self.assertEqual(self.module.failure_reason(None, None), "")
        self.assertEqual(self.module.failure_reason("SUCCESS", None), "")
        self.assertEqual(self.module.failure_reason("", None), "")
        reason = self.module.failure_reason("ERROR", "Individual quota reached.")
        self.assertIn("ERROR", reason)
        self.assertIn("Individual quota reached.", reason)

    def test_extract_result_keeps_a_status_a_later_event_omits(self) -> None:
        # A trailing event without the field must not erase the verdict, and a
        # bare JSON scalar in the tail must not raise.
        events = [
            {
                "event": "result",
                "result": {
                    "status": "ERROR",
                    "error": "Individual quota reached.",
                    "response": "I will begin by reading the diff.",
                },
            },
            {"event": "result", "result": {"response": "still reading"}},
            42,
        ]
        with tempfile.TemporaryDirectory() as temp:
            tail = Path(temp) / "tail"
            tail.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            status, response, error = self.module.extract_result(tail)
        self.assertEqual(status, "ERROR")
        self.assertEqual(error, "Individual quota reached.")
        self.assertEqual(response, "still reading")

    def test_snapshot_export_raises_the_windows_path_limit(self) -> None:
        # The state-dir prefix is 114 characters, so an index path of 186 puts
        # the target past 260 and the export failed with "Filename too long",
        # which blocked the reviewer. The override is per command, so the
        # user's git config is untouched.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            state_dir.mkdir()
            results = [
                subprocess.CompletedProcess([], 0, "diff body", ""),
                subprocess.CompletedProcess([], 0, str(root), ""),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            calls: list[tuple] = []

            def record(cwd, *args):
                calls.append(args)
                return results[len(calls) - 1]

            with mock.patch.object(self.module, "git_output", side_effect=record):
                snapshot, _, _ = self.module.prepare_snapshot(root, state_dir)

        self.assertIsNotNone(snapshot)
        export = calls[-1]
        self.assertEqual(export[:2], ("-c", "core.longpaths=true"))
        self.assertIn("checkout-index", export)
        # The snapshot stays a runnable working copy: the reviewer changes
        # directory into it and runs the repository's own verification, so the
        # export keeps every indexed file rather than only the changed ones.
        self.assertIn("-a", export)

    def test_snapshot_export_failure_never_falls_back_to_original_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            state_dir.mkdir()
            results = [
                subprocess.CompletedProcess([], 0, "diff body", ""),
                subprocess.CompletedProcess([], 0, str(root), ""),
                subprocess.CompletedProcess([], 1, "", "export failed"),
            ]
            with mock.patch.object(
                self.module, "git_output", side_effect=results
            ):
                validation_dir, _, diagnostics = self.module.prepare_snapshot(
                    root, state_dir
                )

            self.assertIsNone(validation_dir)
            self.assertIn("refusing to run Gemini in the original repo", diagnostics)

    def test_copy_stream_writes_each_chunk_before_eof(self) -> None:
        # This dispatcher carries its own copy of the pump, so the 64 KiB
        # blocking read had to be fixed twice and can regress independently.
        # A review's tail is what the operator watches while the round runs.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        tail_path = Path(temp.name) / "tail"
        read_fd, write_fd = os.pipe()
        # bufsize -1 is what subprocess.Popen hands the dispatcher, so this
        # read end buffers exactly the way process.stdout does.
        source = open(read_fd, "rb")
        self.addCleanup(source.close)
        writer = open(write_fd, "wb", buffering=0)
        line = b'{"event": "init", "model": "gemini-3.8-flash-high"}\n'
        observed = b""
        with tail_path.open("wb") as tail:
            pump = threading.Thread(
                target=self.module.copy_stream, args=(source, tail), daemon=True
            )
            pump.start()
            try:
                writer.write(line)
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    observed = tail_path.read_bytes()
                    if observed == line:
                        break
                    time.sleep(0.02)
            finally:
                # Closing before the join keeps a failed assertion from
                # leaving the pump blocked on a pipe nobody will write to.
                writer.close()
                pump.join(timeout=10.0)
        self.assertEqual(observed, line, "tail did not grow while the pipe stayed open")
        self.assertFalse(pump.is_alive(), "the pump did not return at EOF")


@unittest.skipUnless(GIT, "git is required for dispatcher integration tests")
class DispatchGeminiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_dispatch_module()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run([GIT, "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "sample.txt").write_text("staged Gemini fixture\n", encoding="utf-8")
        subprocess.run([GIT, "add", "sample.txt"], cwd=self.repo, check=True)
        (self.repo / "prompt.txt").write_text(
            "Review the staged change. Start with <!-- Round 2 -->.",
            encoding="utf-8",
        )
        self.log = self.root / "log"
        self.mock = self._write_mock()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_mock(self) -> Path:
        script = self.root / "mock_agy.py"
        script.write_text(MOCK_AGY, encoding="utf-8")
        if os.name == "nt":
            wrapper = self.root / "agy.cmd"
            wrapper.write_text(
                f'@"{PYTHON}" "{script}" %*\r\n', encoding="utf-8"
            )
            return wrapper
        wrapper = self.root / "agy"
        wrapper.write_text(
            f"#!{PYTHON}\n" + "\n".join(MOCK_AGY.splitlines()[1:]) + "\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return wrapper

    def _run(self, extra_env: dict[str, str] | None = None):
        env = os.environ.copy()
        env["ANTIGRAVITY_BIN"] = str(self.mock)
        env["MOCK_AGY_LOG"] = str(self.log)
        env["ANTIGRAVITY_PREFLIGHT_TIMEOUT_SECONDS"] = "10"
        env.pop("IMPLEMENT_REVIEW_ORCHESTRATOR", None)
        # An inherited status would silently rewrite what the status-free
        # cases below are covering, so only a test that asks for one gets it.
        env.pop("MOCK_AGY_STATUS", None)
        env.pop("MOCK_AGY_ERROR", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [
                str(PYTHON),
                str(DISPATCH),
                "--prompt-file",
                "prompt.txt",
                "--round",
                "2",
                "--expected-review-file",
                "Review-Antigravity.md",
            ],
            cwd=self.repo,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=30,
        )

    def test_dispatch_grants_unattended_execution_and_publishes_review(self) -> None:
        result = self._run()
        self.assertEqual(
            result.returncode,
            0,
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertRegex(result.stdout, r"^STATE-DIR .+\n$")
        review = self.repo / "Review-Antigravity.md"
        self.assertTrue(review.is_file())
        self.assertEqual(
            review.read_text(encoding="utf-8").splitlines()[0],
            "<!-- Round 2 -->",
        )
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("--input-format", args)
        self.assertIn("stream-json", args)
        self.assertIn("--mode", args)
        self.assertEqual(args[args.index("--mode") + 1], "accept-edits")
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertIn("--add-dir", args)
        self.assertIn("gemini-3.8-flash-high", args)
        prompt = (self.log / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("staged Gemini fixture", prompt)
        self.assertIn("Run relevant tests, experiments, benchmarks", prompt)
        self.assertIn("disposable staged snapshot", prompt)
        self.assertIn("change directory there before shell commands", prompt)
        snapshot_arg = Path(args[args.index("--add-dir") + 1])
        self.assertEqual(snapshot_arg.name, "staged-snapshot")
        self.assertIn(str(snapshot_arg), prompt)
        self.assertIn("Do not commit, push, publish", prompt)

        state_dir = Path(result.stdout.strip().split(" ", 1)[1])
        self.assertTrue((state_dir / "timestamp").is_file())
        self.assertTrue((state_dir / "pre-mtime").is_file())
        self.assertEqual(
            Path((state_dir / "python-interpreter").read_text(encoding="utf-8").strip()),
            PYTHON,
        )
        self.assertTrue((state_dir / "tail").is_file())
        self.assertTrue((state_dir / "tail.stderr-tmp").is_file())

    def test_model_override_is_forwarded(self) -> None:
        result = self._run(
            {
                "ANTIGRAVITY_DISPATCH_MODEL": "gemini-3.1-pro-high",
                "MOCK_AGY_MODELS": "gemini-3.1-pro-high",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("gemini-3.1-pro-high", args)

    def test_unavailable_model_fails_before_review_request(self) -> None:
        result = self._run({"MOCK_AGY_MODELS": "gemini-3.1-pro-high"})
        self.assertEqual(result.returncode, 70)
        self.assertIn("is not available", result.stderr)
        self.assertFalse((self.log / "prompt.txt").exists())

    def test_error_status_rejects_a_review_that_clears_the_size_floor(self) -> None:
        review = self.repo / "Review-Antigravity.md"
        review.write_text("<!-- Round 1 -->\n\nEarlier round.\n", encoding="utf-8")
        before = review.read_bytes()
        result = self._run(
            {
                "MOCK_AGY_STATUS": "ERROR",
                "MOCK_AGY_ERROR": "Quota exceeded for gemini-3.8-flash-high",
            }
        )
        self.assertEqual(
            result.returncode,
            70,
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn("Quota exceeded for gemini-3.8-flash-high", result.stderr)
        self.assertIn("review rejected", result.stderr)
        self.assertEqual(review.read_bytes(), before)

        # The rejected response clears the publication floor, so the size check
        # cannot be what stopped it.
        state_dir = Path(result.stdout.strip().split(" ", 1)[1])
        status, response, error = self.module.extract_result(state_dir / "tail")
        self.assertEqual(status, "ERROR")
        self.assertIsNotNone(response)
        self.assertGreaterEqual(
            len(response.encode("utf-8")), self.module.MIN_REVIEW_BYTES
        )
        self.assertEqual(error, "Quota exceeded for gemini-3.8-flash-high")

    def test_success_status_still_publishes_the_review(self) -> None:
        result = self._run({"MOCK_AGY_STATUS": "SUCCESS"})
        self.assertEqual(
            result.returncode,
            0,
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        review = self.repo / "Review-Antigravity.md"
        self.assertEqual(
            review.read_text(encoding="utf-8").splitlines()[0],
            "<!-- Round 2 -->",
        )

    def test_self_review_guard_refuses_gemini_orchestrator(self) -> None:
        result = self._run({"IMPLEMENT_REVIEW_ORCHESTRATOR": "gemini"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("self-review", result.stderr)
        self.assertFalse((self.log / "args.json").exists())

    def test_self_review_guard_refuses_agy_orchestrator(self) -> None:
        result = self._run({"IMPLEMENT_REVIEW_ORCHESTRATOR": "agy"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("self-review", result.stderr)
        self.assertFalse((self.log / "args.json").exists())


if __name__ == "__main__":
    unittest.main()
