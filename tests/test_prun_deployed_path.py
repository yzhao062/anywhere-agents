"""Deployed-path release for prun's long-lived Bash scripts.

monitor.sh and gather.sh each run for up to an hour. Windows refuses a rename
over a file another process holds open without FILE_SHARE_DELETE, and a shell
holds a script open for as long as it is executing it, while the composer
deploys skill files by rename. Each script carries a private-copy guard so a
live run does not abort a compose transaction (anywhere-agents#43).

These two tests were the part of tests/test_dispatch_task.py that did not cover
the Codex worker scripts. Those scripts moved to legacy/prun-codex-worker/ and
their contract tests retired with them; the scripts and a slim contract set
came back on 2026-11-10. These two cover other scripts, so they stay here. dispatch-task.sh used to sort ahead of both and hide them, which is
why they were written in that file to begin with.
"""
from __future__ import annotations

import os
import shutil
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
SCRIPTS_DIR = ROOT / "skills" / "prun" / "scripts"


BASH = shutil.which("bash")

def _git_bash() -> str | None:
    """Return a Git Bash executable on Windows, or None if there is none.

    ``shutil.which("bash")`` answers with the WSL launcher in System32 first,
    which fails with "execvpe(/bin/bash)" when no distro is installed. The file
    handle DispatchTaskReleasesDeployedPath is about belongs to Git Bash, so
    that is the binary to find. Off Windows the plain PATH answer is right.
    """
    if not sys.platform.startswith("win"):
        return BASH
    roots = [
        os.environ.get("ProgramFiles", "C:/Program Files"),
        os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for root in roots:
        if not root:
            continue
        for parts in (("Git", "bin", "bash.exe"),
                      ("Programs", "Git", "bin", "bash.exe")):
            candidate = Path(root).joinpath(*parts)
            if candidate.is_file():
                return str(candidate)
    return None


GIT_BASH = _git_bash()


def _temp_dir():
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()



@unittest.skipUnless(
    sys.platform.startswith("win") and GIT_BASH,
    "the deployed-path release is a Windows file-sharing property; needs Git Bash",
)
class PrunLongRunningScriptsReleaseDeployedPath(unittest.TestCase):
    """monitor.sh and gather.sh are the other two long-lived Bash holders.

    Each can run for an hour, and skill files are staged in sorted path order
    while the transaction stops at its first failure, so dispatch-task.sh sat in
    front of both and hid them. They carry the same private-copy guard, and this
    is the same live-replacement proof without a codex mock: both scripts poll on
    their own and announce themselves on the first stdout line.
    """

    RUN_SECONDS = "6"
    REPLACEMENT_BODY = "#!/usr/bin/env bash\nexit 0\n"

    def _spawn(self, tmpdir, script_name, args, env_extra):
        deployed_dir = tmpdir / "deployed"
        deployed_dir.mkdir(exist_ok=True)
        deployed = deployed_dir / script_name
        shutil.copyfile(SCRIPTS_DIR / script_name, deployed)
        env = os.environ.copy()
        # Git Bash reads these as shell strings, where a Windows backslash
        # is an escape character. Hand it the forward-slash spelling so the
        # state directory it creates is a path Python can find again.
        posix_tmp = str(tmpdir).replace(chr(92), "/")
        env["TMPDIR"] = posix_tmp
        env["TEMP"] = posix_tmp
        env["TMP"] = posix_tmp
        env.update(env_extra)
        proc = subprocess.Popen(
            [GIT_BASH, str(deployed)] + args,
            cwd=str(tmpdir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        return deployed, proc

    def _replace_while_running(self, tmpdir, deployed, proc, expected_first_token):
        """Wait for the script to announce itself, perform the rename the
        composer would perform, then let it finish on its own."""
        first = proc.stdout.readline()
        self.assertTrue(
            first.startswith(expected_first_token),
            "first stdout line was %r, expected it to start with %r"
            % (first, expected_first_token),
        )
        replacement = tmpdir / "replacement.sh"
        replacement.write_text(self.REPLACEMENT_BODY, encoding="utf-8")
        os.replace(replacement, deployed)
        self.assertEqual(deployed.read_text(encoding="utf-8"), self.REPLACEMENT_BODY)
        proc.communicate(timeout=60)
        # The private-copy hand-off puts a command-string parent above the real
        # script, so the caller's status travels one extra process. Both scripts
        # end this fixture by timing out, which is exit 2. Without this
        # assertion a wrapper that swallowed the status and returned 0 passed.
        self.assertEqual(proc.returncode, 2)

    def test_monitor_releases_its_deployed_path(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            state_dir = tmpdir / "unit-state"
            state_dir.mkdir()
            (state_dir / "tail").write_text("", encoding="utf-8")
            (state_dir / "result-file").write_text(
                str(tmpdir / "never.md"), encoding="utf-8")
            deployed, proc = self._spawn(
                tmpdir, "monitor.sh", [str(state_dir)],
                {"PRUN_MONITOR_TIMEOUT": self.RUN_SECONDS, "PRUN_MONITOR_POLL": "1"},
            )
            self._replace_while_running(tmpdir, deployed, proc, "MONITOR-START")
            self.assertEqual(list(tmpdir.glob("prun-monitor-reexec-*")), [])

    def test_gather_releases_its_deployed_path(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            deployed, proc = self._spawn(
                tmpdir, "gather.sh", [str(tmpdir / "never.md")],
                {"AGENT_CONFIG_GATHER_TIMEOUT": self.RUN_SECONDS,
                 "PRUN_GATHER_POLL": "1"},
            )
            self._replace_while_running(tmpdir, deployed, proc, "GATHER-START")
            self.assertEqual(list(tmpdir.glob("prun-gather-reexec-*")), [])

    def test_monitor_reads_a_live_dispatch_pid_as_stalled(self) -> None:
        """A quiet unit whose dispatcher is alive is stalled, not dead.

        monitor.sh consults dispatch-pid only after a unit has been quiet for
        the stall threshold, so the threshold drops to one second to enter that
        branch at all. The Codex dispatcher's contract test used to cover this
        with a real worker, and retired with it; a sleeping shell supplies the
        same live PID. The shell writes its own pid, because Git Bash and
        Windows do not agree on process ids and `kill -0` reads the Bash one.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            state_dir = tmpdir / "unit-state"
            state_dir.mkdir()
            (state_dir / "tail").write_text("", encoding="utf-8")
            (state_dir / "result-file").write_text(
                str(tmpdir / "never.md"), encoding="utf-8")
            pid_file = state_dir / "dispatch-pid"
            posix_pid_file = str(pid_file).replace(chr(92), "/")
            sleeper = subprocess.Popen(
                [GIT_BASH, "-c", "echo $$ > '%s'; sleep 30" % posix_pid_file],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.time() + 30
                while time.time() < deadline and not pid_file.exists():
                    time.sleep(0.1)
                self.assertTrue(pid_file.exists(), "the sleeper never wrote its pid")
                env = os.environ.copy()
                posix_tmp = str(tmpdir).replace(chr(92), "/")
                env["TMPDIR"] = posix_tmp
                env["TEMP"] = posix_tmp
                env["TMP"] = posix_tmp
                env["PRUN_MONITOR_TIMEOUT"] = "3"
                env["PRUN_MONITOR_POLL"] = "1"
                env["PRUN_STALL_THRESHOLD"] = "1"
                monitor = subprocess.run(
                    [GIT_BASH, str(SCRIPTS_DIR / "monitor.sh"), str(state_dir)],
                    cwd=str(tmpdir), env=env,
                    capture_output=True, text=True, check=False, timeout=60,
                )
                self.assertEqual(monitor.returncode, 3,
                                 monitor.stdout + monitor.stderr)
                self.assertIn(
                    "stalled(", monitor.stdout,
                    "monitor never reached its liveness branch: " + monitor.stdout)
                self.assertNotIn(
                    "failed(dispatch-dead)", monitor.stdout,
                    "monitor read a live unit as dead: " + monitor.stdout)
            finally:
                sleeper.terminate()
                sleeper.communicate(timeout=30)


if __name__ == "__main__":
    unittest.main()
