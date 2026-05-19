"""Tests for the git --version >= 2.25 preflight in bootstrap.sh / bootstrap.ps1.

The preflight runs before any sparse-clone command in either bootstrap path
(fresh-clone OR existing-repo refresh) and fails closed only on confirmed
too-old git. Parse failures default-pass with a stderr warning so unexpected
`git --version` strings (alpha builds, distro suffixes like `2.30.1.windows.1`
or `(Apple Git-141)`) do not block already-modern systems.

These tests stub git via a per-test temp dir prepended to PATH, run the
bootstrap script with AGENT_CONFIG_PREFLIGHT_TEST=1 so the script exits
right after preflight, and assert exit code + stderr shape per case.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SH = ROOT / "bootstrap" / "bootstrap.sh"
BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"


def _resolve_bash() -> str | None:
    """Find a real bash binary, avoiding the Windows WSL launcher stub.

    On Windows, plain `bash` on PATH often resolves to the WSL launcher
    (`C:\\Windows\\System32\\bash.exe` or the WindowsApps shim), which
    requires a WSL distro to be installed and works through a syscall
    broker that breaks PATH stubbing. Prefer the Git for Windows bash.
    """
    # Honor an explicit override first (CI may need this).
    override = os.environ.get("AGENT_PREFLIGHT_BASH")
    if override and os.path.isfile(override):
        return override
    if os.name == "nt":
        candidates = [
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        # shutil.which may surface the WSL stub on Windows; only trust it
        # if the resolved path is NOT under System32 / WindowsApps.
        found = shutil.which("bash")
        if found:
            low = found.lower()
            if "system32" not in low and "windowsapps" not in low:
                return found
        return None
    return shutil.which("bash")


BASH = _resolve_bash()


def _stripped_env(stub_dir: Path | None) -> dict[str, str]:
    """Build an env where PATH includes ONLY directories needed for bash
    builtins (sed, printf, sh) and the test stub_dir (if any).

    On Windows the Git Bash bash needs `Git\\usr\\bin` for sed/printf; we
    intentionally exclude `Git\\bin`, `Git\\cmd`, and `Git\\mingw64\\bin`
    because those contain the real `git` binary and would defeat the
    missing-git / stubbed-version scenarios.
    """
    env = os.environ.copy()
    keep_path: list[str] = []
    if os.name == "nt":
        # Always include the bash binary's sibling `usr/bin` so sed/printf
        # are reachable even on hosts where Git for Windows did not put it
        # on PATH directly (e.g., only `Git\cmd` is on PATH). Without this,
        # preflight parser silently default-passes because `sed` is missing
        # and "git 2.24" is treated as unparseable.
        if BASH:
            bash_dir = Path(BASH).resolve().parent
            if bash_dir.name.lower() == "bin" and bash_dir.parent.name.lower() == "usr":
                git_usr_bin = bash_dir
            else:
                git_usr_bin = bash_dir.parent / "usr" / "bin"
            if (git_usr_bin / "sed.exe").is_file():
                keep_path.append(str(git_usr_bin))
        for src in (os.environ.get("PATH") or "").split(os.pathsep):
            src_low = src.lower().rstrip("\\/")
            # Reject every dir that ships a real `git` binary.
            if any(bad in src_low for bad in (
                "system32",
                "windowsapps",
                "git\\cmd",
                "git\\bin",
                "git\\mingw64\\bin",
                "scoop\\apps\\git",
                "github\\bin",
            )):
                continue
            # Keep dirs that ship bash builtins (sed, printf, etc.).
            if any(needle in src_low for needle in (
                "git\\usr\\bin",
                "msys2",
                "msys64",
            )):
                keep_path.append(src)
    else:
        keep_path = ["/usr/bin", "/bin", "/usr/local/bin"]
    if stub_dir is not None:
        keep_path.insert(0, str(stub_dir))
    env["PATH"] = os.pathsep.join(keep_path)
    # Avoid leaking the developer's persisted upstream / cached repo state.
    env.pop("AGENT_CONFIG_UPSTREAM", None)
    env.pop("AGENT_CONFIG_SKIP_GIT_PREFLIGHT", None)
    env.pop("AGENT_CONFIG_PREFLIGHT_TEST", None)
    return env


def _make_stub_git(stub_dir: Path, version_line: str | None) -> None:
    """Create a stub `git` on PATH that prints `version_line` for `git --version`.

    When `version_line` is None, no stub is created (PATH has no git).
    """
    if version_line is None:
        return
    stub_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Windows: ship both a .cmd shim (for cmd-style lookups) and a
        # bash-targeted shell script (for `command -v git` inside Git Bash).
        # Git Bash's command -v walks PATH for executables by name without
        # extension preference, so a bare-name file with #!/bin/sh suffices.
        cmd_path = stub_dir / "git.cmd"
        cmd_path.write_text(
            f"@echo off\r\nif \"%1\"==\"--version\" (\r\n  echo {version_line}\r\n  exit /b 0\r\n)\r\nexit /b 0\r\n",
            encoding="ascii",
        )
        # Bash-style stub for the bootstrap.sh path on Windows (Git Bash).
        sh_path = stub_dir / "git"
        sh_path.write_text(
            f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then\n  printf '%s\\n' '{version_line}'\n  exit 0\nfi\nexit 0\n",
            encoding="ascii",
        )
        sh_path.chmod(sh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    else:
        sh_path = stub_dir / "git"
        sh_path.write_text(
            f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then\n  printf '%s\\n' '{version_line}'\n  exit 0\nfi\nexit 0\n",
            encoding="ascii",
        )
        sh_path.chmod(sh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_bootstrap_sh(version_line: str | None, *, scenario: str = "fresh") -> subprocess.CompletedProcess:
    """Run bootstrap.sh with a stubbed git on PATH; capture exit + stderr.

    scenario:
      'fresh': no existing .agent-config/repo/.git
      'existing': pre-create .agent-config/repo/.git so the existing-repo
                  refresh branch would fire first if preflight did NOT gate.
    """
    if not BASH:
        raise unittest.SkipTest("bash not available (Git Bash on Windows or system bash on POSIX)")
    tmp = Path(tempfile.mkdtemp(prefix="aa-preflight-"))
    try:
        stub_dir = tmp / "stub_path"
        _make_stub_git(stub_dir, version_line)
        work = tmp / "work"
        work.mkdir()
        if scenario == "existing":
            (work / ".agent-config" / "repo" / ".git").mkdir(parents=True)
            (work / ".agent-config" / "repo" / ".git" / "config").write_text(
                "[remote \"origin\"]\n  url = https://github.com/example/example.git\n"
            )
        env = _stripped_env(stub_dir if version_line is not None else None)
        env["AGENT_CONFIG_PREFLIGHT_TEST"] = "1"
        # On Windows the env passed to bash needs forward-slash-friendly PATH;
        # subprocess.Popen handles the translation when the program is bash.exe.
        result = subprocess.run(
            [BASH, str(BOOTSTRAP_SH)],
            cwd=str(work),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class GitPreflightBashTests(unittest.TestCase):

    def test_missing_git_binary_fails(self):
        result = _run_bootstrap_sh(None)
        self.assertNotEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("git is not installed or not on PATH", result.stderr)
        self.assertIn("install:", result.stderr)

    def test_too_old_2_24_fails(self):
        result = _run_bootstrap_sh("git version 2.24.0")
        self.assertNotEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("git 2.24 is too old", result.stderr)
        self.assertIn("install:", result.stderr)

    def test_too_old_1_9_fails(self):
        # Major < 2 must also fail (covers ancient git installs).
        result = _run_bootstrap_sh("git version 1.9.5")
        self.assertNotEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("git 1.9 is too old", result.stderr)

    def test_boundary_2_25_0_passes(self):
        result = _run_bootstrap_sh("git version 2.25.0")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn("too old", result.stderr)
        self.assertNotIn("not installed", result.stderr)

    def test_modern_2_50_passes(self):
        result = _run_bootstrap_sh("git version 2.50.0")
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_windows_suffix_passes(self):
        # Real format from Git for Windows: `git version 2.30.1.windows.1`.
        result = _run_bootstrap_sh("git version 2.30.1.windows.1")
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_apple_suffix_passes(self):
        # Real format from macOS Xcode-bundled git: `git version 2.34.1 (Apple Git-141)`.
        result = _run_bootstrap_sh("git version 2.34.1 (Apple Git-141)")
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_release_candidate_passes(self):
        result = _run_bootstrap_sh("git version 2.50.0.rc1")
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_unparseable_version_default_passes(self):
        # Default-pass with stderr warning so unexpected formats don't break modern systems.
        result = _run_bootstrap_sh("git version foo.bar")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("could not parse git version", result.stderr)
        self.assertIn("assuming OK", result.stderr)

    def test_empty_version_default_passes(self):
        # Stub prints nothing for --version; preflight should default-pass.
        if not BASH:
            raise unittest.SkipTest("bash not available")
        tmp = Path(tempfile.mkdtemp(prefix="aa-preflight-"))
        try:
            stub_dir = tmp / "stub_path"
            stub_dir.mkdir(parents=True)
            sh_path = stub_dir / "git"
            sh_path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            sh_path.chmod(sh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            if os.name == "nt":
                (stub_dir / "git.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")
            work = tmp / "work"
            work.mkdir()
            env = _stripped_env(stub_dir)
            env["AGENT_CONFIG_PREFLIGHT_TEST"] = "1"
            result = subprocess.run(
                [BASH, str(BOOTSTRAP_SH)],
                cwd=str(work),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("could not parse git version", result.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_existing_repo_path_still_gated(self):
        # If preflight only fired on the fresh-clone branch, an existing
        # .agent-config/repo/.git would slip past with old git. Verify the
        # gate runs first regardless.
        result = _run_bootstrap_sh("git version 2.24.0", scenario="existing")
        self.assertNotEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("git 2.24 is too old", result.stderr)

    def test_skip_env_bypasses_preflight(self):
        # AGENT_CONFIG_SKIP_GIT_PREFLIGHT=1 lets the check pass even on
        # too-old git (an escape hatch for unusual installs).
        if not BASH:
            raise unittest.SkipTest("bash not available")
        tmp = Path(tempfile.mkdtemp(prefix="aa-preflight-"))
        try:
            stub_dir = tmp / "stub_path"
            _make_stub_git(stub_dir, "git version 2.10.0")
            work = tmp / "work"
            work.mkdir()
            env = _stripped_env(stub_dir)
            env["AGENT_CONFIG_PREFLIGHT_TEST"] = "1"
            env["AGENT_CONFIG_SKIP_GIT_PREFLIGHT"] = "1"
            result = subprocess.run(
                [BASH, str(BOOTSTRAP_SH)],
                cwd=str(work),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertNotIn("too old", result.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_platform_install_hint_matches_uname(self):
        # The platform-specific install line should match `uname -s` shape.
        result = _run_bootstrap_sh("git version 2.20.0")
        self.assertNotEqual(result.returncode, 0, msg=result.stderr)
        # Cross-platform: at least one of the known platform lines must appear.
        platform_hints = (
            "brew install git",
            "apt update && sudo apt install -y git",
            "https://git-scm.com/download/win",
            "https://git-scm.com/downloads",
        )
        self.assertTrue(
            any(h in result.stderr for h in platform_hints),
            msg=f"no platform install hint in stderr: {result.stderr!r}",
        )


if __name__ == "__main__":
    unittest.main()
