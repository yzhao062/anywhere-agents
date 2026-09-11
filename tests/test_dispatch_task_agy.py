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
print(json.dumps({"event": "init", "model": "gemini-3.8-flash-high"}))
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
        mode: str = "plan",
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        target = result_path or (self.root / "unit-result.md")
        env = os.environ.copy()
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
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            [
                str(PYTHON),
                str(DISPATCH),
                "--prompt-file",
                str(self.prompt),
                "--result-file",
                str(target),
                "--unit-id",
                "unit_a",
                "--mode",
                mode,
            ],
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
        self.assertIn("--sandbox", args)
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

    def test_accept_edits_is_explicit(self) -> None:
        result, _ = self._run(mode="accept-edits")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--mode") + 1], "accept-edits")
        self.assertIn("--dangerously-skip-permissions", args)

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

    def test_existing_result_is_never_overwritten(self) -> None:
        target = self.root / "existing.md"
        target.write_text("keep me\n", encoding="utf-8")
        result, _ = self._run(result_path=target)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
