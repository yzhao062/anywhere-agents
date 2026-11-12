"""Contract tests for prun's Codex worker, dispatch-task.{sh,ps1}.

A prun unit runs on Agy unless the user names Codex for it. This pair of scripts
is the Codex route. The contract is in skills/prun/SKILL.md:

  - args are --prompt-file / --result-file / --unit-id, and the first and only
    stdout line is `STATE-DIR <abs-path>` (state-dir `prun-task-<8hex>-<unit>-<pid>-<16hex>`);
  - codex runs from a per-unit scratch cwd (`<state-dir>/work`), prompt on stdin;
  - under the default isolation the worker's model and effort come from the
    registry's prun-worker row for the codex family, found through
    scripts/agent_models.py, with CODEX_DISPATCH_MODEL and CODEX_DISPATCH_REASONING
    as one-run overrides, and no literal in either script;
  - after the run, `<state-dir>/model-actual` records what was requested and what
    the header codex printed says it ran on, in the format dispatch-task-agy.py
    writes, and a mismatch never changes the exit code.

The real codex binary is replaced with a mock (CODEX_BIN) that logs its arguments
and prints the same `model:` and `reasoning effort:` header lines, so the wiring is
checked without a model call. The registry is a temporary file (AGENT_MODELS_FILE),
so none of this depends on the live values.

The Bash class and the PowerShell class share a mixin. Bash is skipped on Windows
(Git Bash rewrites temp-dir paths) and PowerShell runs only on Windows, as in
tests/test_dispatch_codex.py.
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
import agent_models  # noqa: E402  the resolver both dispatchers read the registry through

SCRIPTS_DIR = ROOT / "skills" / "prun" / "scripts"
DISPATCH_SH = SCRIPTS_DIR / "dispatch-task.sh"
DISPATCH_PS1 = SCRIPTS_DIR / "dispatch-task.ps1"
REAP_WATCH_PS1 = SCRIPTS_DIR / "reap-watch.ps1"

REGISTRY_NAME = "agent-models.json"
MISSING_REGISTRY_NAME = "no-such-registry.json"
FAMILY_ROW = ("fixture-codex", "low")
WORKER_ROW = ("fixture-worker", "high")


def _temp_dir():
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()


def _write_registry(path: Path, family=FAMILY_ROW, worker=WORKER_ROW) -> Path:
    """A registry whose codex family row differs from its prun-worker row, so a
    test can tell which of the two the dispatcher read."""
    payload = {
        "schema": 1,
        "families": {
            "claude": {"model": "fixture-claude", "effort": "medium"},
            "codex": {"model": family[0], "effort": family[1]},
            "agy": {"model": "fixture-agy", "effort": "high"},
        },
        "roles": {"prun-worker": {"codex": {"model": worker[0], "effort": worker[1]}}},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


MOCK_CODEX_PY = r'''"""Mock codex for dispatch-task tests.

Logs args, stdin, and cwd under MOCK_CODEX_LOG, then prints the header real codex
prints on stderr. The header echoes the model and effort passed with -c, so a
dispatcher that passes the registry row reads back as ok. MOCK_CODEX_HEADER_MODEL
and MOCK_CODEX_HEADER_EFFORT override it, and MOCK_CODEX_NO_HEADER drops it.
"""
import json
import os
import sys
import time

log_dir = os.environ.get("MOCK_CODEX_LOG", os.getcwd())
os.makedirs(log_dir, exist_ok=True)
argv = sys.argv[1:]

with open(os.path.join(log_dir, "args"), "w", encoding="utf-8") as f:
    f.write(json.dumps(argv))
stdin_data = sys.stdin.read()
with open(os.path.join(log_dir, "stdin"), "w", encoding="utf-8", newline="") as f:
    f.write(stdin_data)
with open(os.path.join(log_dir, "cwd"), "w", encoding="utf-8") as f:
    f.write(os.getcwd())


def passed(key):
    for flag, value in zip(argv, argv[1:]):
        if flag == "-c" and value.startswith(key + "="):
            return value.split("=", 1)[1]
    return None


result_target = os.environ.get("MOCK_CODEX_WRITE_RESULT")
if result_target:
    with open(result_target, "w", encoding="utf-8") as f:
        f.write(os.environ.get("MOCK_CODEX_RESULT_CONTENT", "# worker result\n"))

if not os.environ.get("MOCK_CODEX_NO_HEADER"):
    model = os.environ.get("MOCK_CODEX_HEADER_MODEL") or passed("model") or "codex-built-in"
    effort = os.environ.get("MOCK_CODEX_HEADER_EFFORT") or passed("model_reasoning_effort") or "none"
    sys.stderr.write("model: %s\nprovider: openai\nreasoning effort: %s\n" % (model, effort))
sys.stdout.write(os.environ.get("MOCK_CODEX_STDOUT", "mock-codex: stdout\n"))
sys.stderr.write("mock-codex: stderr\n")
sys.stdout.flush()
sys.stderr.flush()

if os.environ.get("MOCK_CODEX_SLEEP"):
    time.sleep(float(os.environ["MOCK_CODEX_SLEEP"]))
sys.exit(int(os.environ.get("MOCK_CODEX_EXIT", "0")))
'''

BASH = shutil.which("bash")
PS_SHELL = shutil.which("pwsh") or shutil.which("powershell")


def _write_mock_codex(tmpdir: Path, want_powershell_shim: bool) -> Path:
    py_path = tmpdir / "mock_codex.py"
    py_path.write_text(MOCK_CODEX_PY, encoding="utf-8")
    if want_powershell_shim:
        shim = tmpdir / "codex-mock.cmd"
        shim.write_text("@echo off\r\n" f'"{sys.executable}" "{py_path}" %*\r\n', encoding="utf-8")
    else:
        shim = tmpdir / "codex-mock.sh"
        shim.write_text("#!/usr/bin/env bash\n" f'exec "{sys.executable}" "{py_path}" "$@"\n',
                        encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _parse_state_dir(stdout: str) -> Path:
    if not stdout:
        raise AssertionError("dispatch stdout is empty (expected STATE-DIR line)")
    match = re.match(r"^STATE-DIR (.+)$", stdout.splitlines()[0])
    if not match:
        raise AssertionError(f"first stdout line is not 'STATE-DIR <path>': {stdout.splitlines()[0]!r}")
    return Path(match.group(1).strip())


class _DispatchTaskContractMixin:
    SHELL_KIND = ""  # "bash" or "powershell"

    def _build_cmd(self, prompt_file, result_file, unit_id, script: Path | None = None) -> list:
        if self.SHELL_KIND == "bash":
            return [BASH, str(script or DISPATCH_SH), "--prompt-file", str(prompt_file),
                    "--result-file", str(result_file), "--unit-id", unit_id]
        return [PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(script or DISPATCH_PS1), "--prompt-file", str(prompt_file),
                "--result-file", str(result_file), "--unit-id", unit_id]

    def _fixture(self, tmpdir: Path):
        log_dir = tmpdir / "mock-log"
        log_dir.mkdir()
        codex = _write_mock_codex(tmpdir, want_powershell_shim=(self.SHELL_KIND == "powershell"))
        prompt = tmpdir / "prompt.txt"
        prompt.write_text("TASK PROMPT body\nLine 2 content\n", encoding="utf-8")
        _write_registry(tmpdir / REGISTRY_NAME)
        return codex, prompt, log_dir

    def _env(self, tmpdir: Path, codex: Path, log_dir: Path, extra_env: dict | None) -> dict:
        env = os.environ.copy()
        env.update({
            "CODEX_BIN": str(codex),
            "MOCK_CODEX_LOG": str(log_dir),
            "TMPDIR": str(tmpdir), "TEMP": str(tmpdir), "TMP": str(tmpdir),
            "AGENT_MODELS_FILE": str(tmpdir / REGISTRY_NAME),
            "ANYWHERE_AGENTS_PYTHON": sys.executable,
        })
        for name in ("CODEX_DISPATCH_MODEL", "CODEX_DISPATCH_REASONING", "CODEX_DISPATCH_ISOLATE_MCP",
                     "CODEX_DISPATCH_SANDBOX", "PRUN_SCRATCH_CWD", "PRUN_DISPATCH_REEXEC",
                     "PRUN_STALL_THRESHOLD", "CODEX_DISPATCH_TIMEOUT"):
            env.pop(name, None)
        for key, value in (extra_env or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        return env

    def _run(self, tmpdir: Path, extra_env: dict | None = None, result_file: Path | None = None,
             unit_id: str = "u", timeout: float = 60.0):
        """One dispatch in a fresh fixture. Returns (process, mock args or None when
        codex never ran, state dir or None when none was announced)."""
        codex, prompt, log_dir = self._fixture(tmpdir)
        env = self._env(tmpdir, codex, log_dir, extra_env)
        result = subprocess.run(
            self._build_cmd(prompt, result_file or tmpdir / "result.md", unit_id),
            cwd=str(tmpdir), env=env, capture_output=True, text=True, check=False, timeout=timeout,
        )
        args_path = log_dir / "args"
        args = json.loads(args_path.read_text(encoding="utf-8")) if args_path.exists() else None
        state = _parse_state_dir(result.stdout) if result.stdout.startswith("STATE-DIR") else None
        return result, args, state

    @staticmethod
    def _text(state: Path, name: str) -> str:
        return (state / name).read_text(encoding="utf-8").strip()

    # --- usage and state ----------------------------------------------------

    def test_usage_errors_exit_two_before_any_state(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fixture(tmpdir)
            env = self._env(tmpdir, codex, log_dir, None)
            good = self._build_cmd(prompt, tmpdir / "r.md", "u")
            cases = {
                "missing required argument": good[: good.index("--result-file")],
                "unknown argument": good + ["--surprise", "x"],
                "bad unit id": self._build_cmd(prompt, tmpdir / "r.md", "bad id!"),
                "missing prompt file": self._build_cmd(tmpdir / "nope.txt", tmpdir / "r.md", "u"),
            }
            for label, cmd in cases.items():
                with self.subTest(label):
                    result = subprocess.run(cmd, cwd=str(tmpdir), env=env, capture_output=True,
                                            text=True, check=False, timeout=30)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, "", "no STATE-DIR line before a usage error")
            self.assertFalse((log_dir / "args").exists(), "codex must not run on a usage error")

    def test_state_dir_line_and_contract(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            result_file = tmpdir / "result.md"
            result_file.write_text("old result\n", encoding="utf-8")
            old_mtime = int(result_file.stat().st_mtime)
            result, _args, state = self._run(tmpdir, result_file=result_file, unit_id="survey_7")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.splitlines()), 1, "STATE-DIR is the only stdout line")
            self.assertTrue(state.is_absolute() and state.is_dir(), state)
            self.assertRegex(state.name, r"^prun-task-[0-9a-f]{8}-survey_7-\d+-[0-9a-f]{16}$")
            pre = self._text(state, "pre-mtime")
            self.assertTrue(pre.isdigit() and int(pre) < 10**12, "Unix epoch seconds, not FILETIME")
            self.assertAlmostEqual(int(pre), old_mtime, delta=2)
            self.assertAlmostEqual(int(self._text(state, "timestamp")), int(time.time()), delta=60)
            # Resolved on both sides: a Windows temp directory can be spelled
            # in its 8.3 short form on one side and in full on the other.
            self.assertEqual(Path(self._text(state, "result-file")).resolve(), result_file.resolve())
            self.assertTrue(self._text(state, "dispatch-pid").isdigit())
            self.assertIn("mock-codex: stdout", self._text(state, "tail"))

    def test_a_relative_result_file_is_recorded_absolute(self) -> None:
        # report-state reads a relative <state-dir>/result-file entry against the
        # state dir, so the dispatcher records it absolute, as the Agy one does.
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, _args, state = self._run(tmpdir, result_file=Path("out") / "rel.md")
            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = Path(self._text(state, "result-file"))
            self.assertTrue(recorded.is_absolute(), recorded)
            self.assertEqual(recorded.resolve(), (tmpdir / "out" / "rel.md").resolve())

    def test_codex_command_shape_cwd_and_stdin(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, args, state = self._run(tmpdir)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(args[0], "exec", args)
            self.assertEqual(args[-1], "-", args)
            self.assertNotIn("review", args)
            self.assertIn("--skip-git-repo-check", args)
            self.assertEqual(args[args.index("--sandbox") + 1], "danger-full-access")
            log_dir = tmpdir / "mock-log"
            self.assertIn("Line 2 content", (log_dir / "stdin").read_text(encoding="utf-8"))
            cwd = Path((log_dir / "cwd").read_text(encoding="utf-8").strip())
            self.assertEqual(cwd.name, "work")
            self.assertEqual(cwd.parent.resolve(), state.resolve())

    # --- model and effort ---------------------------------------------------

    def test_isolation_passes_the_registry_prun_worker_row(self) -> None:
        """Default isolation passes both -c model and -c model_reasoning_effort, taken
        from the prun-worker row and not from the codex family row."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, args, state = self._run(tmpdir)
            self.assertEqual(result.returncode, 0, result.stderr)
            pairs = list(zip(args, args[1:]))
            self.assertIn("--ignore-user-config", args)
            self.assertIn(("-c", f"model={WORKER_ROW[0]}"), pairs, args)
            self.assertIn(("-c", f"model_reasoning_effort={WORKER_ROW[1]}"), pairs, args)
            self.assertNotIn(("-c", f"model={FAMILY_ROW[0]}"), pairs, args)
            self.assertEqual(args[-1], "-", "stdin marker stays the final positional")
            self.assertEqual(self._text(state, "model"), WORKER_ROW[0])

    def test_isolation_off_passes_neither_and_still_checks_the_run(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            result, args, state = self._run(tmpdir, {"CODEX_DISPATCH_ISOLATE_MCP": "off"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("--ignore-user-config", args)
            self.assertFalse(any(str(a).startswith(("model=", "model_reasoning_effort=")) for a in args), args)
            self.assertEqual(args[-1], "-")
            # codex read its own config (the mock reports a built-in default), and the
            # run is held to the registry row all the same.
            self.assertIn("MODEL-MISMATCH", result.stderr)
            self.assertIn("status=mismatch", self._text(state, "model-actual"))

    def test_env_overrides_outrank_the_registry(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            cases = (
                ({"CODEX_DISPATCH_MODEL": "env-model"}, "env-model", WORKER_ROW[1]),
                ({"CODEX_DISPATCH_REASONING": "medium"}, WORKER_ROW[0], "medium"),
                # Both set: the registry is not needed at all, even when it is gone.
                ({"CODEX_DISPATCH_MODEL": "env-model", "CODEX_DISPATCH_REASONING": "medium",
                  "AGENT_MODELS_FILE": str(tmpdir / MISSING_REGISTRY_NAME)}, "env-model", "medium"),
            )
            for index, (extra, model, effort) in enumerate(cases):
                with self.subTest(extra=extra):
                    sub = tmpdir / f"case{index}"
                    sub.mkdir()
                    result, args, state = self._run(sub, extra)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    pairs = list(zip(args, args[1:]))
                    self.assertIn(("-c", f"model={model}"), pairs, args)
                    self.assertIn(("-c", f"model_reasoning_effort={effort}"), pairs, args)
                    self.assertTrue(self._text(state, "model-actual").startswith(f"requested={model} {effort} "))

    def test_unreadable_registry_stops_the_unit_and_names_the_registry(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            missing = {"AGENT_MODELS_FILE": str(tmpdir / MISSING_REGISTRY_NAME)}
            for label, extra in (("no override", missing),
                                 ("effort only", dict(missing, CODEX_DISPATCH_REASONING="medium")),
                                 ("bad override", {"CODEX_DISPATCH_MODEL": "bad model!"})):
                with self.subTest(label):
                    sub = tmpdir / label.replace(" ", "-")
                    sub.mkdir()
                    result, args, state = self._run(sub, extra)
                    if label == "bad override":
                        self.assertEqual(result.returncode, 2, result.stderr)
                    else:
                        self.assertEqual(result.returncode, 70, result.stderr)
                        self.assertIn("registry", result.stderr)
                        self.assertIn("user/agent-models.json", result.stderr)
                        self.assertIn("prun-worker", result.stderr)
                    self.assertIsNone(args, "codex must not run without a model and effort")
                    self.assertIsNone(state, "no state is created for a unit that cannot start")

    def test_resolver_is_found_from_a_consumer_layout(self) -> None:
        """A consumer runs the copy under <project>/.claude/skills/prun/scripts/, and the
        resolver and registry sit under <project>/.agent-config/repo/. The dispatcher walks
        up to them, also after its private re-exec copy moved the script elsewhere."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            codex, prompt, log_dir = self._fixture(tmpdir)
            repo = tmpdir / "project" / ".agent-config" / "repo"
            (repo / "scripts").mkdir(parents=True)
            shutil.copyfile(ROOT / "scripts" / "agent_models.py", repo / "scripts" / "agent_models.py")
            _write_registry(repo / "user" / "agent-models.json", worker=("consumer-worker", "high"))
            deployed = tmpdir / "project" / ".claude" / "skills" / "prun" / "scripts"
            deployed.mkdir(parents=True)
            script = deployed / DISPATCH_SH.name if self.SHELL_KIND == "bash" else deployed / DISPATCH_PS1.name
            shutil.copyfile(DISPATCH_SH if self.SHELL_KIND == "bash" else DISPATCH_PS1, script)
            env = self._env(tmpdir, codex, log_dir, {"AGENT_MODELS_FILE": None})
            result = subprocess.run(self._build_cmd(prompt, tmpdir / "r.md", "u", script),
                                    cwd=str(tmpdir), env=env, capture_output=True, text=True,
                                    check=False, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((log_dir / "args").read_text(encoding="utf-8"))
            self.assertIn(("-c", "model=consumer-worker"), list(zip(args, args[1:])), args)

    # --- the effective model ------------------------------------------------

    def test_model_actual_ok_when_the_header_matches(self) -> None:
        with _temp_dir() as td:
            result, _args, state = self._run(Path(td))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                self._text(state, "model-actual"),
                f"requested={WORKER_ROW[0]} {WORKER_ROW[1]} actual={WORKER_ROW[0]} {WORKER_ROW[1]} status=ok",
            )
            self.assertNotIn("MODEL-MISMATCH", result.stderr)
            self.assertNotIn("MODEL-UNVERIFIED", result.stderr)

    def test_model_actual_mismatch_is_flagged_and_keeps_the_exit_code(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            for index, (header_model, header_effort) in enumerate(
                    (("other-model", WORKER_ROW[1]), (WORKER_ROW[0], "medium"))):
                with self.subTest(model=header_model, effort=header_effort):
                    sub = tmpdir / f"case{index}"
                    sub.mkdir()
                    result, _args, state = self._run(
                        sub, {"MOCK_CODEX_HEADER_MODEL": header_model,
                              "MOCK_CODEX_HEADER_EFFORT": header_effort, "MOCK_CODEX_EXIT": 23})
                    self.assertEqual(result.returncode, 23, result.stderr)
                    self.assertIn("MODEL-MISMATCH", result.stderr)
                    self.assertEqual(
                        self._text(state, "model-actual"),
                        f"requested={WORKER_ROW[0]} {WORKER_ROW[1]} "
                        f"actual={header_model} {header_effort} status=mismatch",
                    )

    def test_model_actual_unknown_when_codex_prints_no_header(self) -> None:
        with _temp_dir() as td:
            result, _args, state = self._run(Path(td), {"MOCK_CODEX_NO_HEADER": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("MODEL-UNVERIFIED", result.stderr)
            self.assertEqual(
                self._text(state, "model-actual"),
                f"requested={WORKER_ROW[0]} {WORKER_ROW[1]} actual=- status=unknown",
            )

    # --- result backstop and the watchdog -----------------------------------

    def test_result_backstop_salvages_the_tail_but_never_clobbers_a_result(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            missing = tmpdir / "missing"
            missing.mkdir()
            result_file = missing / "result.md"
            result, _args, _state = self._run(missing, {"MOCK_CODEX_STDOUT": "WORKER-OUTPUT-MARKER\n"},
                                              result_file=result_file, unit_id="u_fb")
            self.assertEqual(result.returncode, 0, result.stderr)
            body = result_file.read_text(encoding="utf-8")
            self.assertIn("FALLBACK", body)
            self.assertIn("u_fb", body)
            self.assertIn("WORKER-OUTPUT-MARKER", body)
            written = tmpdir / "written"
            written.mkdir()
            real = "# u_ok result\nConclusion: genuine worker result\n"
            result_file = written / "result.md"
            result, _args, _state = self._run(
                written, {"MOCK_CODEX_WRITE_RESULT": str(result_file), "MOCK_CODEX_RESULT_CONTENT": real},
                result_file=result_file, unit_id="u_ok")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result_file.read_text(encoding="utf-8"), real)

    def test_idle_watchdog_reaps_the_worker_with_exit_124(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            result_file = tmpdir / "result.md"
            result, _args, state = self._run(
                tmpdir, {"MOCK_CODEX_SLEEP": "30", "PRUN_STALL_THRESHOLD": "1"},
                result_file=result_file, unit_id="u_idle", timeout=25.0)
            self.assertEqual(result.returncode, 124, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
            body = result_file.read_text(encoding="utf-8")
            self.assertIn("FALLBACK", body)
            self.assertIn("idle-stall", body)
            self.assertIn("exit code 124", body)
            self.assertTrue((state / "model-actual").is_file(), "a reaped unit still leaves a record")


@unittest.skipIf(
    sys.platform.startswith("win"),
    "bash skipped on Windows: Git Bash POSIX-translates env-var temp paths, which breaks "
    "path comparison from Python's Windows-path perspective. CI Linux and macOS cover this lane.",
)
@unittest.skipUnless(BASH, "bash not on PATH")
class DispatchTaskBashTests(_DispatchTaskContractMixin, unittest.TestCase):
    SHELL_KIND = "bash"


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win"),
    "PowerShell dispatch tests are Windows-only: dispatch-task.ps1 starts powershell.exe "
    "hidden for its watchdog. Users on other platforms run dispatch-task.sh.",
)
class DispatchTaskPowerShellTests(_DispatchTaskContractMixin, unittest.TestCase):
    SHELL_KIND = "powershell"


class DispatchTaskStaticContract(unittest.TestCase):
    """Freeze the wiring that no runtime test on this machine reaches."""

    def test_no_script_holds_a_model_or_effort_literal(self) -> None:
        """The registry is the one place that names a model or an effort."""
        for path in (DISPATCH_SH, DISPATCH_PS1, REAP_WATCH_PS1):
            with self.subTest(path.name):
                hits = list(agent_models.scan_text(path.read_text(encoding="utf-8")))
                self.assertEqual(hits, [], f"{path.name} names a model or effort: {hits}")

    def test_both_scripts_read_the_codex_prun_worker_row(self) -> None:
        for path in (DISPATCH_SH, DISPATCH_PS1):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path.name):
                for token in ("--family", "prun-worker", "agent_models.py", "user/agent-models.json",
                              "CODEX_DISPATCH_MODEL", "CODEX_DISPATCH_REASONING", "--ignore-user-config",
                              "model_reasoning_effort", "--skip-git-repo-check", "model-actual",
                              "--stderr-file", "MODEL-UNVERIFIED"):
                    self.assertIn(token, text, f"{path.name} lacks {token}")


if __name__ == "__main__":
    unittest.main()
