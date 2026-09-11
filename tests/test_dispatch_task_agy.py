"""Contract tests for prun's Antigravity task dispatcher."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISPATCH = ROOT / "skills" / "prun" / "scripts" / "dispatch-task-agy.py"
PYTHON = Path(sys.executable).resolve()


def load_dispatch_module():
    spec = importlib.util.spec_from_file_location("dispatch_task_agy", DISPATCH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOCK_AGY = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

log = Path(os.environ["MOCK_AGY_LOG"])
log.mkdir(parents=True, exist_ok=True)
if sys.argv[1:] == ["--version"]:
    print("Antigravity CLI 1.2.0")
    raise SystemExit(0)
if sys.argv[1:] == ["models"]:
    print(os.environ.get("MOCK_AGY_MODELS", "gemini-3.8-flash-high"))
    raise SystemExit(0)

(log / "args.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
(log / "cwd.txt").write_text(os.getcwd(), encoding="utf-8")
event = json.loads(sys.stdin.readline())
(log / "prompt.txt").write_text(event["message"]["content"], encoding="utf-8")
init_event = {"event": "init", "model": "gemini-3.8-flash-high"}
if os.environ.get("MOCK_AGY_NO_INIT_CONVERSATION_ID") != "1":
    init_event["conversation_id"] = os.environ.get(
        "MOCK_AGY_CONVERSATION_ID", "conv-fixed-0001"
    )
print(json.dumps(init_event))
worker_result = os.environ.get("MOCK_AGY_WRITE_RESULT")
if worker_result:
    Path(worker_result).write_text(
        "# unit_a result\nConclusion: worker-written\nFiles: none\n"
        "Open items: none\nVerification: mock\n\n| row | value |\n|---|---|\n| a | 1 |\n",
        encoding="utf-8",
    )
if os.environ.get("MOCK_AGY_NO_RESULT") != "1":
    response = os.environ.get(
        "MOCK_AGY_RESPONSE",
        "# unit_a result\nConclusion: complete\nFiles: none\nOpen items: none\nVerification: mock\n",
    )
    print(json.dumps({"event": "result", "result": {"response": response}}))
raise SystemExit(int(os.environ.get("MOCK_AGY_EXIT", "0")))
'''


class DispatchTaskAgyUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_dispatch_module()

    def test_script_exists(self) -> None:
        self.assertTrue(DISPATCH.is_file())

    def test_defaults_pin_flash_high(self) -> None:
        self.assertEqual(self.module.DEFAULT_MODEL, "gemini-3.8-flash-high")
        self.assertEqual(self.module.DEFAULT_EFFORT, "high")

    def test_unit_id_is_narrow(self) -> None:
        self.assertIsNotNone(self.module.UNIT_RE.fullmatch("paper_review-12"))
        self.assertIsNone(self.module.UNIT_RE.fullmatch("../escape"))


class DispatchTaskAgyIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("Audit section 3 and report evidence.\n", encoding="utf-8")
        self.log = self.root / "log"
        self.mock = self._write_mock()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_mock(self) -> Path:
        script = self.root / "mock_agy.py"
        script.write_text(MOCK_AGY, encoding="utf-8")
        if os.name == "nt":
            wrapper = self.root / "agy.cmd"
            wrapper.write_text(f'@"{PYTHON}" "{script}" %*\r\n', encoding="utf-8")
            return wrapper
        wrapper = self.root / "agy"
        wrapper.write_text(
            f"#!{PYTHON}\n" + "\n".join(MOCK_AGY.splitlines()[1:]) + "\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return wrapper

    def _run(
        self,
        result_path: Path | None = None,
        mode: str | None = "plan",
        extra_env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
        own_cwd: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        target = result_path or (self.root / "unit-result.md")
        env = os.environ.copy()
        # The default-argv assertions must not inherit an operator's opt-in.
        env.pop("PRUN_AGY_SANDBOX", None)
        env.update(
            {
                "ANTIGRAVITY_BIN": str(self.mock),
                "ANTIGRAVITY_PREFLIGHT_TIMEOUT_SECONDS": "10",
                "MOCK_AGY_LOG": str(self.log),
                "PRUN_SCRATCH_CWD": str(self.work),
                "TEMP": str(self.root),
                "TMP": str(self.root),
                "TMPDIR": str(self.root),
            }
        )
        if own_cwd:
            # Let the dispatcher create its own scratch directory, which is
            # the only case where an omitted --mode is allowed.
            env.pop("PRUN_SCRATCH_CWD", None)
        if extra_env:
            env.update(extra_env)
        argv = [
            str(PYTHON),
            str(DISPATCH),
            "--prompt-file",
            str(self.prompt),
            "--result-file",
            str(target),
            "--unit-id",
            "unit_a",
        ]
        if mode is not None:
            argv += ["--mode", mode]
        if extra_args:
            argv += extra_args
        result = subprocess.run(
            argv,
            cwd=self.root,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=30,
        )
        return result, target

    def test_plan_dispatch_streams_and_atomically_publishes(self) -> None:
        result, target = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^STATE-DIR .+\n$")
        self.assertIn("Conclusion: complete", target.read_text(encoding="utf-8"))

        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("gemini-3.8-flash-high", args)
        self.assertIn("high", args)
        self.assertEqual(args[args.index("--mode") + 1], "plan")
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertNotIn("--disable-slash-commands", args)
        self.assertNotIn("--sandbox", args)
        actual_cwd = Path((self.log / "cwd.txt").read_text(encoding="utf-8"))
        self.assertTrue(
            actual_cwd.samefile(self.work), f"{actual_cwd} != {self.work}"
        )
        relay = (self.log / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("Audit section 3", relay)
        self.assertIn("Never commit, push", relay)

        state_dir = Path(result.stdout.strip().split(" ", 1)[1])
        for name in (
            "timestamp",
            "pre-mtime",
            "result-file",
            "dispatch-pid",
            "python-interpreter",
            "worker-pid-unverified",
            "tail",
            "tail.stderr-tmp",
        ):
            self.assertTrue((state_dir / name).is_file(), name)

    def test_default_mode_is_accept_edits_with_skip_permissions(self) -> None:
        result, _ = self._run(mode=None, own_cwd=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--mode") + 1], "accept-edits")
        self.assertIn("--dangerously-skip-permissions", args)
        state_dir = Path(result.stdout.strip().split(" ", 1)[1])
        actual_cwd = Path((self.log / "cwd.txt").read_text(encoding="utf-8"))
        self.assertTrue(actual_cwd.samefile(state_dir / "work"))

    def test_omitted_mode_with_caller_workspace_fails_before_launch(self) -> None:
        # Review finding: the unattended default must not reach a directory
        # the caller supplied, which could be the real checkout.
        for label, kwargs in (
            ("PRUN_SCRATCH_CWD", {}),
            ("--add-dir", {"own_cwd": True, "extra_args": ["--add-dir", str(self.work)]}),
        ):
            with self.subTest(label):
                if (self.log / "args.json").exists():
                    (self.log / "args.json").unlink()
                result, target = self._run(mode=None, **kwargs)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("explicit", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(target.exists())
                self.assertFalse((self.log / "args.json").exists())

    def test_explicit_accept_edits_in_clone_keeps_unattended_permissions(self) -> None:
        result, _ = self._run(mode="accept-edits")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("--dangerously-skip-permissions", args)
        actual_cwd = Path((self.log / "cwd.txt").read_text(encoding="utf-8"))
        self.assertTrue(actual_cwd.samefile(self.work))

    def test_empty_add_dir_and_continue_from_fail_closed(self) -> None:
        # Review finding: an unset shell variable must not resolve to the
        # invocation directory or silently start a fresh conversation.
        for label, extra in (
            ("--add-dir", ["--add-dir", ""]),
            ("--add-dir-blank", ["--add-dir", "   "]),
            ("--continue-from", ["--continue-from", ""]),
            ("--continue-from-blank", ["--continue-from", " "]),
        ):
            with self.subTest(label):
                if (self.log / "args.json").exists():
                    (self.log / "args.json").unlink()
                result, target = self._run(extra_args=extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("non-empty", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(target.exists())
                self.assertFalse((self.log / "args.json").exists())

    def test_sandbox_is_off_by_default_and_opt_in_via_env(self) -> None:
        # On Windows Agy's sandbox spawns an elevated admin broker
        # (agy --exebox-admin-broker), one UAC prompt per unit that runs a
        # command, so the dispatcher must not ask for it unless told to.
        result, _ = self._run(mode="accept-edits")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertNotIn("--sandbox", args)

        (self.log / "args.json").unlink()
        result, _ = self._run(
            result_path=self.root / "unit-result-sandbox.md",
            mode="accept-edits",
            extra_env={"PRUN_AGY_SANDBOX": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("--sandbox", args)
        self.assertIn("--dangerously-skip-permissions", args)

    def test_sandbox_env_spellings_are_case_and_whitespace_insensitive(self) -> None:
        for index, (value, expected) in enumerate(
            (("true", True), (" YES ", True), ("On", True), ("0", False), ("False", False), (" off", False))
        ):
            with self.subTest(value=value):
                if (self.log / "args.json").exists():
                    (self.log / "args.json").unlink()
                result, _ = self._run(
                    result_path=self.root / f"unit-result-spelling-{index}.md",
                    mode="plan",
                    extra_env={"PRUN_AGY_SANDBOX": value},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
                self.assertEqual("--sandbox" in args, expected)
                self.assertEqual(args[args.index("--mode") + 1], "plan")
                self.assertNotIn("--dangerously-skip-permissions", args)

    def test_unrecognized_sandbox_env_fails_before_launch(self) -> None:
        for value in ("maybe", "sandbox"):
            with self.subTest(value):
                if (self.log / "args.json").exists():
                    (self.log / "args.json").unlink()
                result, target = self._run(extra_env={"PRUN_AGY_SANDBOX": value})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("PRUN_AGY_SANDBOX", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(target.exists())
                self.assertFalse((self.log / "args.json").exists())

    def test_plan_mode_carries_neither_accept_edits_nor_skip(self) -> None:
        result, _ = self._run(mode="plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--mode") + 1], "plan")
        self.assertNotIn("--dangerously-skip-permissions", args)

    def test_conversation_id_is_recorded_from_init_event(self) -> None:
        result, _ = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        state_dir = Path(result.stdout.strip().split(" ", 1)[1])
        recorded = (state_dir / "conversation-id").read_text(encoding="utf-8")
        self.assertEqual(recorded, "conv-fixed-0001\n")

    def test_no_init_event_conversation_id_writes_no_file(self) -> None:
        result, _ = self._run(extra_env={"MOCK_AGY_NO_INIT_CONVERSATION_ID": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        state_dir = Path(result.stdout.strip().split(" ", 1)[1])
        self.assertFalse((state_dir / "conversation-id").exists())

    def test_add_dir_is_forwarded_for_each_directory(self) -> None:
        dir_a = self.root / "extra-a"
        dir_b = self.root / "extra-b"
        dir_a.mkdir()
        dir_b.mkdir()
        result, _ = self._run(
            extra_args=["--add-dir", str(dir_a), "--add-dir", str(dir_b)]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        positions = [i for i, token in enumerate(args) if token == "--add-dir"]
        self.assertEqual(len(positions), 2)
        forwarded = {args[i + 1] for i in positions}
        self.assertEqual(forwarded, {str(dir_a.resolve()), str(dir_b.resolve())})

    def test_add_dir_rejects_missing_directory(self) -> None:
        missing = self.root / "does-not-exist"
        result, target = self._run(extra_args=["--add-dir", str(missing)])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertFalse(target.exists())
        self.assertFalse((self.log / "args.json").exists())

    def test_continue_from_forwards_conversation_flag(self) -> None:
        first_result, _ = self._run()
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        first_state_dir = Path(first_result.stdout.strip().split(" ", 1)[1])
        recorded_id = (first_state_dir / "conversation-id").read_text(
            encoding="utf-8"
        ).strip()
        self.assertTrue(recorded_id)

        second_result, _ = self._run(
            result_path=self.root / "unit-result-continued.md",
            extra_args=["--continue-from", str(first_state_dir)],
        )
        self.assertEqual(second_result.returncode, 0, second_result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--conversation") + 1], recorded_id)

    def test_continue_from_missing_conversation_id_file_exits_before_launch(
        self,
    ) -> None:
        empty_dir = self.root / "state-without-id"
        empty_dir.mkdir()
        result, target = self._run(
            extra_args=["--continue-from", str(empty_dir)]
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertFalse(target.exists())
        self.assertFalse((self.log / "args.json").exists())

        blank_id_dir = self.root / "state-with-blank-id"
        blank_id_dir.mkdir()
        (blank_id_dir / "conversation-id").write_text("\n", encoding="utf-8")
        result2, target2 = self._run(
            result_path=self.root / "unit-result-2.md",
            extra_args=["--continue-from", str(blank_id_dir)],
        )
        self.assertEqual(result2.returncode, 2)
        self.assertEqual(result2.stdout, "")
        self.assertFalse(target2.exists())

    def test_nonzero_exit_publishes_fallback(self) -> None:
        result, target = self._run(extra_env={"MOCK_AGY_EXIT": "9"})
        self.assertEqual(result.returncode, 9)
        body = target.read_text(encoding="utf-8")
        self.assertIn("FALLBACK", body)
        self.assertIn("Agy exited with code 9", body)

    def test_missing_result_event_publishes_fallback(self) -> None:
        result, target = self._run(extra_env={"MOCK_AGY_NO_RESULT": "1"})
        self.assertEqual(result.returncode, 70)
        self.assertIn("without a final result", target.read_text(encoding="utf-8"))

    def test_unavailable_model_fails_before_task_and_publishes_fallback(self) -> None:
        result, target = self._run(
            extra_env={"MOCK_AGY_MODELS": "gemini-3.1-pro-high"}
        )
        self.assertEqual(result.returncode, 70)
        self.assertFalse((self.log / "prompt.txt").exists())
        self.assertIn("unavailable", target.read_text(encoding="utf-8"))

    def test_worker_written_result_is_kept_and_response_stored_beside(self) -> None:
        # A live unit wrote its trace table to the result file and then replied
        # with a one-line summary; the dispatcher replaced the table with the
        # summary. The worker's file must win and the response must land beside it.
        target = self.root / "unit-result.md"
        result, target = self._run(
            result_path=target,
            extra_env={
                "MOCK_AGY_WRITE_RESULT": str(target),
                "MOCK_AGY_RESPONSE": "done",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        body = target.read_text(encoding="utf-8")
        self.assertIn("Conclusion: worker-written", body)
        self.assertIn("| a | 1 |", body)
        beside = self.root / "unit-result.response.md"
        self.assertEqual(beside.read_text(encoding="utf-8"), "done\n")
        self.assertNotIn("FALLBACK", body)

    def test_existing_result_is_never_overwritten(self) -> None:
        target = self.root / "existing.md"
        target.write_text("keep me\n", encoding="utf-8")
        result, _ = self._run(result_path=target)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
