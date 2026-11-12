"""Tests for stall-watch.{sh,ps1} -- background stall observer.

Validates the contract in skills/implement-review/SKILL.md > Script contract
invariants and Phase 2.0 Health check 9. Uses STALL_THRESHOLD_SECONDS=2
and STALL_POLL_INTERVAL_SECONDS=1 so tests complete in seconds.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
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
SCRIPTS_DIR = ROOT / "skills" / "implement-review" / "scripts"
STALL_SH = Path(os.environ.get(
    "TEST_STALL_WATCH_SH", SCRIPTS_DIR / "stall-watch.sh"
))
STALL_PS1 = Path(os.environ.get(
    "TEST_STALL_WATCH_PS1", SCRIPTS_DIR / "stall-watch.ps1"
))

BASH = shutil.which("bash")
PS_SHELL = shutil.which("pwsh") or shutil.which("powershell")


def _spawn_long_running() -> subprocess.Popen:
    """Spawn a Python process that sleeps for 120s (test cleans it up)."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _safe_kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


_GRANDCHILD_CODE = "import time; time.sleep(120)"
_WORKER_CODE = r"""
import pathlib
import subprocess
import sys
import time

state_dir = pathlib.Path(sys.argv[1])
grandchild = subprocess.Popen([sys.executable, "-c", sys.argv[2]])
(state_dir / "grandchild-pid").write_text(str(grandchild.pid), encoding="utf-8")
while True:
    grandchild.poll()
    time.sleep(0.1)
"""
_PARENT_CODE = r"""
import pathlib
import subprocess
import sys
import time

state_dir = pathlib.Path(sys.argv[1])
worker = subprocess.Popen([
    sys.executable, "-c", sys.argv[2], str(state_dir), sys.argv[3]
])
(state_dir / "worker-pid").write_text(str(worker.pid), encoding="utf-8")
while True:
    worker.poll()
    time.sleep(0.1)
"""


# A wait sized on an idle machine has failed under load three times in two
# releases, most recently on this file in both repositories on one push,
# green again on a rerun with no change. A deadline is only spent when the
# event never arrives, so the budget is generous and a passing run does not
# pay for it. Raise it on a slow or heavily loaded runner.
STALL_WAIT_TIMEOUT_SECONDS = float(
    os.environ.get("STALL_WAIT_TIMEOUT_SECONDS", "30")
)


def _wait_for(predicate, message: str,
              timeout: float = STALL_WAIT_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(message)


def _spawn_worker_tree(state_dir: Path) -> tuple[subprocess.Popen, int, int]:
    parent = subprocess.Popen(
        [
            sys.executable, "-c", _PARENT_CODE, str(state_dir),
            _WORKER_CODE, _GRANDCHILD_CODE,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(
            lambda: (state_dir / "worker-pid").exists()
            and (state_dir / "grandchild-pid").exists(),
            "worker tree did not publish its process IDs",
        )
        worker_pid = int((state_dir / "worker-pid").read_text(encoding="utf-8"))
        grandchild_pid = int(
            (state_dir / "grandchild-pid").read_text(encoding="utf-8")
        )
        return parent, worker_pid, grandchild_pid
    except Exception:
        _safe_kill(parent)
        raise


def _pid_alive(pid: int) -> bool:
    if sys.platform.startswith("win"):
        import ctypes

        synchronize = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            return proc_stat.read_text(encoding="utf-8").split()[2] != "Z"
        except (OSError, IndexError):
            pass
    return True


def _safe_kill_pid(pid: int | None) -> None:
    if not pid or not _pid_alive(pid):
        return
    if sys.platform.startswith("win"):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class _StallContractMixin:
    SHELL_KIND: str = ""

    def _build_cmd(self, state_dir: Path, parent_pid: int) -> list[str]:
        if self.SHELL_KIND == "bash":
            return [
                BASH, str(STALL_SH),
                "--state-dir", str(state_dir),
                "--parent-pid", str(parent_pid),
            ]
        if self.SHELL_KIND == "powershell":
            return [
                PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(STALL_PS1),
                "--state-dir", str(state_dir),
                "--parent-pid", str(parent_pid),
            ]
        raise AssertionError(f"unknown SHELL_KIND: {self.SHELL_KIND!r}")

    def _spawn_watch(
        self,
        state_dir: Path,
        parent_pid: int,
        threshold: int = 2,
        interval: int = 1,
    ) -> subprocess.Popen:
        import os
        env = os.environ.copy()
        env["STALL_THRESHOLD_SECONDS"] = str(threshold)
        env["STALL_POLL_INTERVAL_SECONDS"] = str(interval)
        return subprocess.Popen(
            self._build_cmd(state_dir, parent_pid),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    # --- contract assertions ---------------------------------------------

    def test_stall_logged_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            (state_dir / "tail").write_text("initial\n", encoding="utf-8")

            parent = _spawn_long_running()
            watch = None
            try:
                watch = self._spawn_watch(state_dir, parent.pid,
                                          threshold=2, interval=1)
                warn_file = state_dir / "stall-warning"
                _wait_for(
                    warn_file.exists,
                    "stall-warning file should be created after 2s threshold elapses",
                )
                content = warn_file.read_text(encoding="utf-8")
                self.assertRegex(
                    content,
                    r"STALL \S+ tail-no-growth-for-\d+s",
                    f"unexpected stall-warning content: {content!r}",
                )
            finally:
                _safe_kill(watch)
                _safe_kill(parent)

    def test_exits_when_parent_dies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            (state_dir / "tail").write_text("x\n", encoding="utf-8")

            parent = _spawn_long_running()
            watch = None
            try:
                watch = self._spawn_watch(state_dir, parent.pid,
                                          threshold=999, interval=1)
                # Make sure stall-watch is up and polling
                time.sleep(2)
                self.assertIsNone(watch.poll(),
                                  "stall-watch should still be running")

                # Kill parent; stall-watch must notice and exit
                parent.kill()
                parent.wait(timeout=5)

                try:
                    watch.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.fail(
                        "stall-watch did not exit within 10s after parent died"
                    )
                self.assertEqual(
                    watch.returncode, 0,
                    "stall-watch must exit 0 silently when parent dies",
                )
            finally:
                _safe_kill(watch)
                _safe_kill(parent)

    def test_silence_and_midstream_error_preserve_processes_then_terminal_error_reaps(self) -> None:
        """Only a terminal stream-death suffix reaps the captured worker tree."""
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            tail = state_dir / "tail"
            tail.write_text("initial progress\n", encoding="utf-8")

            parent = None
            worker_pid = None
            grandchild_pid = None
            unrelated = _spawn_long_running()
            watch = None
            try:
                parent, worker_pid, grandchild_pid = _spawn_worker_tree(state_dir)
                watch = self._spawn_watch(state_dir, parent.pid,
                                          threshold=1, interval=1)
                worker_roots = state_dir / "worker-roots"
                _wait_for(
                    lambda: worker_roots.exists()
                    and str(worker_pid) in worker_roots.read_text(encoding="utf-8"),
                    "stall-watch did not capture the worker root",
                )
                _wait_for(
                    lambda: (state_dir / "stall-warning").exists(),
                    "silence threshold did not produce stall-warning",
                )

                for pid, label in (
                    (parent.pid, "dispatcher"),
                    (worker_pid, "captured worker"),
                    (grandchild_pid, "captured grandchild"),
                    (unrelated.pid, "unrelated process"),
                ):
                    self.assertTrue(_pid_alive(pid), f"silence killed {label}")
                self.assertFalse(
                    (state_dir / "reap-reason").exists(),
                    "silence must never invoke worker reaping",
                )

                error_line = (
                    "ERROR: stream disconnected before completion: "
                    "https://chatgpt.com/backend-api/codex/responses/test-response"
                )
                middle = [error_line, "tokens used", "123"]
                middle.extend(f'{{"type":"progress","step":{i}}}' for i in range(6))
                middle.extend(["tokens used", "456"])
                warn_file = state_dir / "stall-warning"
                stalls_before = warn_file.read_text(encoding="utf-8").count("STALL")
                with tail.open("a", encoding="utf-8") as stream:
                    stream.write("\n".join(middle) + "\n")
                # A second STALL line proves the watcher observed growth and
                # that the suffix check returned no match. Both implementations
                # test that suffix at the top of the growth branch and exit
                # there on a match, so the latch reset that allows a second
                # STALL is reachable only past the check. It does not prove a
                # successful read: both classifiers report no match when their
                # own read fails. A sleep proves less again, letting a watcher
                # that never polled the mid-transcript state satisfy the
                # negative below without the classifier being asked at all.
                _wait_for(
                    lambda: warn_file.read_text(encoding="utf-8").count("STALL")
                    > stalls_before
                    or (state_dir / "stream-death").exists(),
                    "stall-watch never polled the mid-transcript tail, so the "
                    "assertions below would not have exercised the classifier",
                )

                self.assertFalse(
                    (state_dir / "stream-death").exists(),
                    "the same disconnect text mid-transcript must not match",
                )
                self.assertTrue(_pid_alive(worker_pid),
                                "mid-transcript error reaped the worker")
                self.assertTrue(_pid_alive(unrelated.pid),
                                "mid-transcript error killed an unrelated process")

                with tail.open("a", encoding="utf-8") as stream:
                    stream.write(f"{error_line}\ntokens used\n1,234\n")
                _wait_for(
                    lambda: (state_dir / "stream-death").exists(),
                    "terminal stream-death suffix was not detected",
                )
                _wait_for(
                    lambda: not _pid_alive(worker_pid)
                    and not _pid_alive(grandchild_pid),
                    "stream-death did not reap the captured worker tree",
                )

                marker = (state_dir / "stream-death").read_text(encoding="utf-8")
                self.assertRegex(
                    marker,
                    r"^STREAM-DEATH \S+ codex-response-stream-disconnected$",
                )
                # The completion marker is the only proof a consumer gets that
                # the reap ran. auto-watch refuses to emit STREAM-DEAD without
                # it and await-review refuses to return DEAD, so deleting this
                # write would make a real terminal failure unreachable on this
                # platform while every synthetic fixture stayed green.
                _wait_for(
                    lambda: (state_dir / "stream-reap-complete").exists(),
                    "stream-reap-complete was not written after the reap",
                )
                self.assertRegex(
                    (state_dir / "stream-reap-complete").read_text(encoding="utf-8"),
                    r"^STREAM-REAP-COMPLETE \S+",
                )
                self.assertEqual(
                    (state_dir / "reap-reason").read_text(encoding="utf-8").strip(),
                    "stream-death",
                )
                self.assertIsNone(parent.poll(),
                                  "stream-death must not kill the dispatcher")
                self.assertIsNone(unrelated.poll(),
                                  "stream-death killed an unrelated process")
            finally:
                _safe_kill(watch)
                _safe_kill(parent)
                _safe_kill(unrelated)
                _safe_kill_pid(worker_pid)
                _safe_kill_pid(grandchild_pid)

    def test_stream_death_suffix_allows_two_filler_lines(self) -> None:
        """Both implementations inspect three slots before the trailer."""
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            error_line = (
                "ERROR: stream disconnected before completion: "
                "https://chatgpt.com/backend-api/codex/responses/test-response"
            )
            (state_dir / "tail").write_text(
                "initial progress\n"
                f"{error_line}\n"
                '{"type":"progress","step":1}\n'
                '{"type":"progress","step":2}\n'
                "tokens used\n"
                "1,234\n",
                encoding="utf-8",
            )

            parent = _spawn_long_running()
            watch = None
            try:
                watch = self._spawn_watch(
                    state_dir, parent.pid, threshold=999, interval=1
                )
                _wait_for(
                    lambda: (state_dir / "stream-death").exists(),
                    "two-filler terminal stream-death suffix was not detected",
                )
                marker = (state_dir / "stream-death").read_text(
                    encoding="utf-8"
                )
                self.assertRegex(
                    marker,
                    r"^STREAM-DEATH \S+ codex-response-stream-disconnected$",
                )
            finally:
                _safe_kill(watch)
                _safe_kill(parent)

    def test_parent_exit_reaps_only_the_captured_worker_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            (state_dir / "tail").write_text("active\n", encoding="utf-8")

            parent = None
            worker_pid = None
            grandchild_pid = None
            unrelated = _spawn_long_running()
            watch = None
            try:
                parent, worker_pid, grandchild_pid = _spawn_worker_tree(state_dir)
                watch = self._spawn_watch(state_dir, parent.pid,
                                          threshold=999, interval=1)
                worker_roots = state_dir / "worker-roots"
                _wait_for(
                    lambda: worker_roots.exists()
                    and str(worker_pid) in worker_roots.read_text(encoding="utf-8"),
                    "stall-watch did not capture the worker before parent exit",
                )

                parent.kill()
                parent.wait(timeout=5)
                watch.wait(timeout=12)
                self.assertEqual(watch.returncode, 0)
                _wait_for(
                    lambda: not _pid_alive(worker_pid)
                    and not _pid_alive(grandchild_pid),
                    "parent-exited did not reap the captured worker tree",
                )
                self.assertEqual(
                    (state_dir / "reap-reason").read_text(encoding="utf-8").strip(),
                    "parent-exited",
                )
                self.assertIsNone(
                    unrelated.poll(),
                    "parent-exited reaping killed an unrelated process",
                )
            finally:
                _safe_kill(watch)
                _safe_kill(parent)
                _safe_kill(unrelated)
                _safe_kill_pid(worker_pid)
                _safe_kill_pid(grandchild_pid)

    def test_growth_resets_stall_period_and_relogs(self) -> None:
        """A second stall period after a growth burst is logged separately."""
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            tail = state_dir / "tail"
            tail.write_text("initial\n", encoding="utf-8")

            parent = _spawn_long_running()
            watch = None
            try:
                watch = self._spawn_watch(state_dir, parent.pid,
                                          threshold=2, interval=1)
                # First stall window
                warn_file = state_dir / "stall-warning"
                _wait_for(warn_file.exists,
                          "first stall should be logged after 2s threshold")
                first_count = warn_file.read_text(encoding="utf-8").count("STALL")
                self.assertGreaterEqual(first_count, 1)

                # Grow the tail; stall-watch must observe and reset
                tail.write_text("initial\nmore content here\n", encoding="utf-8")
                # Wait for stall-watch to observe the growth and re-stall.
                _wait_for(
                    lambda: warn_file.read_text(encoding="utf-8").count("STALL")
                    > first_count,
                    "second stall period after growth burst should add a new "
                    f"STALL line; first_count={first_count}",
                )
            finally:
                _safe_kill(watch)
                _safe_kill(parent)

    def test_missing_args_exit_silently(self) -> None:
        if self.SHELL_KIND == "bash":
            cmd = [BASH, str(STALL_SH)]
        else:
            cmd = [PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(STALL_PS1)]
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=10
        )
        self.assertEqual(
            result.returncode, 0,
            f"missing args must exit 0 silently\nSTDOUT:{result.stdout}\n"
            f"STDERR:{result.stderr}",
        )

    def test_nonexistent_state_dir_exits_silently(self) -> None:
        parent = _spawn_long_running()
        try:
            cmd = self._build_cmd(Path("nonexistent-dir-xyz-12345"), parent.pid)
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=10
            )
            self.assertEqual(
                result.returncode, 0,
                "nonexistent state-dir must exit 0 silently",
            )
        finally:
            _safe_kill(parent)


@unittest.skipIf(
    sys.platform.startswith("win"),
    "bash skipped on Windows: Git Bash POSIX-translates paths. "
    "CI Linux covers this lane.",
)
@unittest.skipUnless(BASH, "bash not on PATH")
class StallWatchBashTests(_StallContractMixin, unittest.TestCase):
    SHELL_KIND = "bash"


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win"),
    "PowerShell stall-watch tests are Windows-only: stall-watch.ps1 is "
    "invoked by dispatch-codex.ps1 which uses Windows-only Start-Process "
    "options. Linux/macOS users run stall-watch.sh instead.",
)
class StallWatchPowerShellTests(_StallContractMixin, unittest.TestCase):
    SHELL_KIND = "powershell"


class StallSafetyInvariants(unittest.TestCase):
    """Source checks for reason-bounded, identity-checked worker reaping."""

    @staticmethod
    def _shell_function(body: str, name: str) -> str:
        match = re.search(
            rf"(?ms)^{re.escape(name)}\(\) \{{\n(.*?)^\}}",
            body,
        )
        if not match:
            raise AssertionError(f"missing shell function: {name}")
        return match.group(1)

    @staticmethod
    def _powershell_function(body: str, name: str) -> str:
        match = re.search(
            rf"(?ms)^function {re.escape(name)} \{{\n(.*?)^\}}",
            body,
        )
        if not match:
            raise AssertionError(f"missing PowerShell function: {name}")
        return match.group(1)

    def test_sh_reaping_is_reason_bounded_tree_scoped_and_identity_checked(self) -> None:
        body = STALL_SH.read_text(encoding="utf-8")
        reap_body = self._shell_function(body, "_reap_worker_roots")

        call_reasons = re.findall(
            r"(?m)^\s*_reap_worker_roots\s+([^\s#]+)", body
        )
        self.assertCountEqual(
            call_reasons,
            ["stream-death", "parent-exited"],
            "only stream-death and parent-exited may invoke worker reaping",
        )
        self.assertIn(
            'if _same_process "$root_pid" "$root_start"; then', reap_body,
            "captured worker roots must be checked against recorded start identity",
        )
        self.assertIn('_collect_tree "$root_pid"', reap_body)
        self.assertIn(
            '_same_process "$target_pid" "$target_start" || continue',
            reap_body,
            "each tree member must be identity-checked immediately before a signal",
        )
        self.assertIn('tail -n 16 "$TAIL_FILE"', body)

        destructive = []
        for line_num, line in enumerate(body.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for match in re.finditer(r"\bkill\b\s+(\S+)", line):
                first_arg = match.group(1).strip("\"'")
                if first_arg != "-0":
                    destructive.append((line_num, line.strip()))
        self.assertEqual(
            [line for _, line in destructive],
            [
                'kill -TERM "$target_pid" 2>/dev/null || true',
                'kill -KILL "$target_pid" 2>/dev/null || true',
            ],
            "destructive signals must target only identity-checked tree members",
        )
        for forbidden in ("pkill", "killall"):
            self.assertNotIn(
                forbidden, body,
                f"stall-watch.sh must not use unbounded {forbidden!r}",
            )

    def test_ps1_reaping_is_reason_bounded_tree_scoped_and_identity_checked(self) -> None:
        body = STALL_PS1.read_text(encoding="utf-8")
        reap_body = self._powershell_function(body, "Invoke-WorkerReap")

        call_reasons = re.findall(
            r"(?m)^\s*Invoke-WorkerReap\s+'([^']+)'", body
        )
        self.assertCountEqual(
            call_reasons,
            ["stream-death", "parent-exited"],
            "only stream-death and parent-exited may invoke worker reaping",
        )
        self.assertIn(
            "Test-SameWorkerProcess ([int]$_.Key) ([int64]$_.Value)",
            reap_body,
            "worker PID must match its captured creation identity",
        )
        self.assertIn("taskkill /PID %1 /T /F", reap_body)
        self.assertIn("$reapCmd ([int]$victim.Key)", reap_body)
        self.assertIn("Get-Content -LiteralPath $tailPath -Tail 16", body)
        self.assertEqual(
            body.count("taskkill /PID"), 1,
            "taskkill must appear only in the bounded worker-reap helper",
        )
        for forbidden in (
            "Stop-Process",
            ".Kill(",
            "TerminateProcess",
        ):
            self.assertNotIn(
                forbidden, body,
                f"stall-watch.ps1 must not contain unbounded {forbidden!r}",
            )


if __name__ == "__main__":
    unittest.main()
