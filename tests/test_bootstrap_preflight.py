"""Tests for bootstrap preflight and run-ledger behavior.

The preflight runs before any sparse-clone command in either bootstrap path
(fresh-clone OR existing-repo refresh) and fails closed only on confirmed
too-old git. Parse failures default-pass with a stderr warning so unexpected
`git --version` strings (alpha builds, distro suffixes like `2.30.1.windows.1`
or `(Apple Git-141)`) do not block already-modern systems.

These tests stub git via a per-test temp dir prepended to PATH, run the
bootstrap script with AGENT_CONFIG_PREFLIGHT_TEST=1 so the script exits
right after preflight, and assert exit code + stderr shape per case. The ledger
contract is tested at the same boundary because every bootstrap run emits it
before checking git.
"""
from __future__ import annotations

import errno
import importlib.util
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
from unittest import mock

# tests/ is on sys.path under `unittest discover -s tests` but not under
# `python -m unittest tests.<module>`, which validate.yml uses for the
# Sentinel redaction smoke. Put it there before the sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_SH = ROOT / "bootstrap" / "bootstrap.sh"
BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"

# The composed-artifact predicate requires a full 64-digit digest, so fixtures
# need a real one. Earlier fixtures wrote `sha256=abc123`, which the loose
# predicate accepted; they would have gone on passing while the product stopped
# recognizing anything the composer actually writes. This is the SHA-256 of the
# empty input, chosen because it is verifiable rather than invented.
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# One bootstrap run costs about 17 seconds on this Windows machine when it is
# idle, because every fixture starts Git Bash or a PowerShell edition and the
# script itself spawns more. The cap was 30 seconds, under 2x that, so any
# competing load turned a healthy run into `TimeoutExpired` and the failure read
# like a regression in whatever was being changed at the time. It did that three
# times across two releases: a CI job, a local full run, and a reviewer's own
# verification pass, which cost more to diagnose each time than the cap ever
# saved. Linux spawns these in about a second and never approaches either value.
#
# 90 seconds keeps roughly 5x headroom while still bounding a genuine hang.
# AGENT_CONFIG_TEST_TIMEOUT overrides it for a machine that needs more, or for a
# CI job that would rather fail fast.
SUBPROCESS_TIMEOUT = int(os.environ.get("AGENT_CONFIG_TEST_TIMEOUT", "90"))


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

# Every installed edition, stricter one first. Choosing a single edition is how
# anywhere-agents#34 reached six green CI legs: windows-latest ships both, the
# constant preferred pwsh, and the defect only reproduced under Windows
# PowerShell 5.1. Restoring the defective quoting in a scratch copy leaves the
# ledger suite 19/19 green under pwsh and failing five tests under 5.1, so the
# contract was already written down and only the edition choice hid it.
POWERSHELL_EDITIONS = [p for p in (shutil.which("powershell"), shutil.which("pwsh")) if p]
POWERSHELL = POWERSHELL_EDITIONS[0] if POWERSHELL_EDITIONS else None


def powershell_editions_or_fail(case: unittest.TestCase) -> list[str]:
    """Return the editions to exercise, refusing to narrow silently.

    `powershell` ships in System32 on every supported Windows, so its absence
    there is a broken environment rather than a reason to pass. `pwsh` is a
    separate install and may legitimately be missing.
    """
    if sys.platform.startswith("win"):
        if not any(Path(p).stem.lower() == "powershell" for p in POWERSHELL_EDITIONS):
            case.fail(
                "Windows PowerShell is not on PATH. It ships in System32 on every "
                "supported Windows, so this is a broken environment rather than a "
                "skip, and skipping here is what let anywhere-agents#34 ship."
            )
    elif not POWERSHELL_EDITIONS:
        raise unittest.SkipTest("no PowerShell edition available")
    return POWERSHELL_EDITIONS


_POSIX_SANDBOX_TOOLS = (
    "sed", "uname", "printf", "tr", "grep", "cat", "mkdir", "echo", "rm",
    "dirname", "basename", "test", "ls", "head", "tail", "sh", "mv", "cp",
    "chmod", "mktemp", "sleep", "ln", "date", "nohup", "stat", "kill", "pkill",
)


def _build_posix_sandbox(parent: Path) -> Path:
    """Create a sandbox bin dir with symlinks to shell utilities, no `git`.

    The default POSIX PATH (`/usr/bin`, `/bin`, `/usr/local/bin`,
    `/opt/homebrew/bin`) ships real git on macOS / Linux CI runners, so
    "missing-git" tests that only strip a stub_dir fail-open. The sandbox
    contains symlinks to a curated set of shell utilities (sed, uname, ...)
    but deliberately does NOT include `git`. Tests prepend the stub_dir
    (if any) and use the sandbox as the rest of PATH.
    """
    sandbox = parent / "sandbox_bin"
    sandbox.mkdir(parents=True, exist_ok=True)
    for tool in _POSIX_SANDBOX_TOOLS:
        src = shutil.which(tool)
        if src:
            link = sandbox / tool
            if not link.exists():
                try:
                    os.symlink(src, link)
                except (OSError, NotImplementedError):
                    # Filesystem cannot symlink; copy instead.
                    shutil.copy2(src, link)
    return sandbox


def _stripped_env(stub_dir: Path | None) -> dict[str, str]:
    """Build an env where PATH includes ONLY directories needed for bash
    builtins (sed, printf, sh) and the test stub_dir (if any).

    On Windows the Git Bash bash needs `Git\\usr\\bin` for sed/printf; we
    intentionally exclude `Git\\bin`, `Git\\cmd`, and `Git\\mingw64\\bin`
    because those contain the real `git` binary and would defeat the
    missing-git / stubbed-version scenarios.

    On POSIX we build a per-call sandbox bin dir containing symlinks to a
    curated set of shell utilities, and use ONLY that dir (plus the
    optional stub_dir) as PATH. This excludes real git from `/usr/bin`,
    `/usr/local/bin`, `/opt/homebrew/bin`, and any other default location.
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
        # POSIX: build a sandbox bin dir with no git. Anchor it under the
        # stub_dir's parent so the same temp lifecycle cleans it up; fall
        # back to a fresh tempdir if no stub_dir is provided.
        if stub_dir is not None:
            sandbox = _build_posix_sandbox(stub_dir.parent)
        else:
            sandbox = _build_posix_sandbox(Path(tempfile.mkdtemp(prefix="aa-preflight-sandbox-")))
        keep_path = [str(sandbox)]
    if stub_dir is not None:
        keep_path.insert(0, str(stub_dir))
    env["PATH"] = os.pathsep.join(keep_path)
    # Avoid leaking the developer's persisted upstream / cached repo state.
    env.pop("AGENT_CONFIG_UPSTREAM", None)
    env.pop("AGENT_CONFIG_SKIP_GIT_PREFLIGHT", None)
    env.pop("AGENT_CONFIG_PREFLIGHT_TEST", None)
    env.pop("GROK_BIN", None)
    env.pop("GROK_HOME", None)
    env.pop("ANYWHERE_AGENTS_GROK_AUTO_UPDATE", None)
    env.pop("ANYWHERE_AGENTS_GROK_UPDATE_DIR", None)
    env.pop("ANYWHERE_AGENTS_GROK_UPDATE_STAMP", None)
    env.pop("ANYWHERE_AGENTS_GROK_UPDATE_LOCK", None)
    env.pop("ANYWHERE_AGENTS_GROK_UPDATE_LOG", None)
    env.pop("ANYWHERE_AGENTS_GROK_UPDATE_TTL_SECONDS", None)
    env.pop("ANYWHERE_AGENTS_GROK_CHECK_TIMEOUT_SECONDS", None)
    env.pop("ANYWHERE_AGENTS_GROK_UPDATE_TIMEOUT_SECONDS", None)
    return env


def powershell_stub_dir(stub_dir: Path) -> Path:
    """The PATH entry a PowerShell entrypoint should use.

    On Windows each entrypoint gets its own directory because the two cannot
    share one. Git Bash needs an extensionless file, and PowerShell prefers an
    extensionless file over a `.ps1` of the same name when both are present.
    It then cannot run the shell script, falls back to ShellExecute, and the
    user gets a "How do you want to open this file?" dialog. Keeping the
    PowerShell stubs in a subdirectory of their own leaves each side with only
    the form it can run.

    Everywhere else the split is wrong, and returning it cost a whole CI run.
    `pwsh` on Linux and macOS runs the extensionless `#!/bin/sh` stub the way
    it runs any other program, and it never finds a `.ps1` on PATH because
    PATHEXT is a Windows variable. Every `.ps1` writer here sits behind
    `os.name == "nt"`, so off Windows this subdirectory is never created.
    Pointing PATH at it hands pwsh a directory that does not exist: the git
    preflight then fails, and each test in the family either asserts against a
    bootstrap that refused to start or reads an AGENTS.md it never wrote.
    """
    if os.name != "nt":
        return stub_dir
    return stub_dir / "ps"


def _make_stub_git(stub_dir: Path, version_line: str | None) -> None:
    """Create a stub `git` on PATH that prints `version_line` for `git --version`.

    When `version_line` is None, no stub is created (PATH has no git).

    Two forms are written: an extensionless shell script in `stub_dir` for Git
    Bash, and a `.ps1` in `powershell_stub_dir(stub_dir)` for PowerShell.

    `ls-files --error-unmatch -- <path>` answers from
    `ANYWHERE_AGENTS_STUB_TRACKED`, a space-separated list of tracked paths.
    An earlier stub exited 0 for every subcommand, so the .gitignore block read
    every generated file as already tracked and skipped all three entries. The
    tests around it would then have passed against a bootstrap that wrote
    nothing.
    """
    if version_line is None:
        return
    stub_dir.mkdir(parents=True, exist_ok=True)
    ls_files_sh = (
        'if [ "$1" = "ls-files" ]; then\n'
        '  for _t in ${ANYWHERE_AGENTS_STUB_TRACKED:-}; do\n'
        '    if [ "$_t" = "$4" ]; then exit 0; fi\n'
        '  done\n'
        '  exit 1\n'
        'fi\n'
    )
    # The path is read as the last argument rather than by index. `git ls-files
    # --error-unmatch -- <path>` puts it at $4 for the shell stub, because sh
    # passes `--` through, and at $args[2] for the PowerShell stub, because
    # PowerShell consumes `--` as its own end-of-parameters marker. Taking the
    # last argument is correct either way.
    ls_files_ps1 = (
        "if ($args[0] -eq 'ls-files') {\n"
        "  $target = $args[$args.Count - 1]\n"
        "  foreach ($t in (($env:ANYWHERE_AGENTS_STUB_TRACKED -split '\\s+') | Where-Object { $_ })) {\n"
        "    if ($t -eq $target) { exit 0 }\n"
        "  }\n"
        "  exit 1\n"
        "}\n"
    )
    if os.name == "nt":
        # The PowerShell shim is a .ps1 and not a .cmd on purpose. PowerShell
        # runs a .ps1 in-process, so no process is created and nothing can ask
        # Windows for a console. A .cmd is run through cmd.exe, and a cmd.exe
        # spawned by a shell whose own parent had no console gets a new console
        # that is shown: the suite flashed a window per git call, and no
        # creation flag on the Python side reached it. Measured against the real
        # fixture across CREATE_NO_WINDOW, SW_HIDE, both together,
        # CREATE_NEW_CONSOLE with SW_HIDE, and a runner holding its own hidden
        # console; every one of them left cmd.exe owning a visible window.
        # See anywhere-agents#38.
        ps_dir = powershell_stub_dir(stub_dir)
        ps_dir.mkdir(parents=True, exist_ok=True)
        (ps_dir / "git.ps1").write_text(
            f"if ($args[0] -eq '--version') {{ Write-Output '{version_line}'; exit 0 }}\n"
            + ls_files_ps1
            + "exit 0\n",
            encoding="ascii",
        )
        # Bash-style stub for the bootstrap.sh path on Windows (Git Bash).
        sh_path = stub_dir / "git"
        sh_path.write_text(
            f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then\n  printf '%s\\n' '{version_line}'\n  exit 0\nfi\n"
            + ls_files_sh
            + "exit 0\n",
            encoding="ascii",
        )
        sh_path.chmod(sh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    else:
        sh_path = stub_dir / "git"
        sh_path.write_text(
            f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then\n  printf '%s\\n' '{version_line}'\n  exit 0\nfi\n"
            + ls_files_sh
            + "exit 0\n",
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
            timeout=SUBPROCESS_TIMEOUT,
        )
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_bootstrap_sh_with_ledger(
    version_line: str | None,
    *,
    scenario: str = "fresh",
    upstream: str | None = None,
) -> tuple[subprocess.CompletedProcess, dict | None]:
    """Run bootstrap.sh through preflight and return its parsed ledger."""
    if not BASH:
        raise unittest.SkipTest("bash not available (Git Bash on Windows or system bash on POSIX)")
    tmp = Path(tempfile.mkdtemp(prefix="aa-preflight-ledger-"))
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
        elif scenario == "unwritable":
            (work / ".agent-config").write_text("regular file\n", encoding="utf-8")
        elif scenario != "fresh":
            raise ValueError(f"unknown scenario: {scenario}")
        env = _stripped_env(stub_dir if version_line is not None else None)
        env["AGENT_CONFIG_PREFLIGHT_TEST"] = "1"
        command = [BASH, str(BOOTSTRAP_SH)]
        if upstream is not None:
            if os.name == "nt":
                # Git Bash rewrites quotes in Windows argv. The environment
                # path reaches the same upstream cascade without that rewrite.
                env["AGENT_CONFIG_UPSTREAM"] = upstream
            else:
                command.append(upstream)
        result = subprocess.run(
            command,
            cwd=str(work),
            env=env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        ledger_path = work / ".agent-config" / "last-run.json"
        ledger = (
            json.loads(ledger_path.read_text(encoding="utf-8"))
            if ledger_path.is_file()
            else None
        )
        return result, ledger
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_bootstrap_ps1_with_ledger(
    version_line: str | None,
    *,
    scenario: str = "fresh",
    upstream: str | None = None,
) -> tuple[subprocess.CompletedProcess, dict | None]:
    """Run bootstrap.ps1 through preflight and return its parsed ledger."""
    if not POWERSHELL:
        raise unittest.SkipTest("pwsh/powershell not available")
    tmp = Path(tempfile.mkdtemp(prefix="aa-preflight-ledger-ps1-"))
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
        elif scenario == "unwritable":
            (work / ".agent-config").write_text("regular file\n", encoding="utf-8")
        elif scenario != "fresh":
            raise ValueError(f"unknown scenario: {scenario}")
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join(
            (str(powershell_stub_dir(stub_dir)), env.get("PATH", "")))
        env.pop("AGENT_CONFIG_UPSTREAM", None)
        env.pop("AGENT_CONFIG_SKIP_GIT_PREFLIGHT", None)
        env["AGENT_CONFIG_PREFLIGHT_TEST"] = "1"
        if upstream is not None:
            env["AGENT_CONFIG_UPSTREAM"] = upstream
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(BOOTSTRAP_PS1)],
            cwd=str(work),
            env=env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        ledger_path = work / ".agent-config" / "last-run.json"
        ledger = (
            json.loads(ledger_path.read_text(encoding="utf-8-sig"))
            if ledger_path.is_file()
            else None
        )
        return result, ledger
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _temp_dir() -> tempfile.TemporaryDirectory:
    """TemporaryDirectory with ignore_cleanup_errors on Py3.10+ (Py3.9 fallback).

    Git for Windows' bash can still hold a directory when it exits, and the
    keyword that tolerates that is 3.10+; the validate matrix runs 3.9.
    """
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()


def _write_text_lf(path: Path, content: str) -> None:
    # open(newline=) rather than Path.write_text(newline=), for the reason
    # _write_executable gives just below. The keyword arrived in 3.10 and this
    # suite still runs on 3.9, where it raises TypeError rather than writing
    # the wrong ending. Seven callers wrote shell input the other way and took
    # every 3.9 job in CI down with them.
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _write_executable(path: Path, content: str) -> None:
    # open(newline=) rather than Path.write_text(newline=): the latter kwarg is
    # Python 3.10+, and the CI matrix still covers 3.9. These files are shell
    # stubs handed to a POSIX shell, so the LF ending is load-bearing on
    # Windows checkouts and cannot be left to the platform default.
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


TODO_README_BODY = "# The `todo/` Folder\n\nupstream drop-box copy\n"


def _prepare_full_bootstrap_fixture(
    tmp: Path,
    *,
    composer_rc: int | None,
    generator_rc: int | None,
    yaml_available: bool = True,
    todo_readme: bool = True,
) -> tuple[Path, Path, Path]:
    work = tmp / "work"
    scripts = work / ".agent-config" / "repo" / "scripts"
    (work / ".agent-config" / "repo" / ".git").mkdir(parents=True)
    scripts.mkdir(parents=True)
    if todo_readme:
        # The seeding step copies from the sparse clone rather than carrying the
        # text in both entry points, so the fixture has to supply it the same
        # way the real clone does.
        upstream_bootstrap = work / ".agent-config" / "repo" / "bootstrap"
        upstream_bootstrap.mkdir(parents=True, exist_ok=True)
        _write_text_lf(upstream_bootstrap / "todo-readme.md", TODO_README_BODY)
    (work / ".agent-config" / "AGENTS.md").write_text("fetched rules\n", encoding="utf-8")
    if composer_rc is not None:
        (scripts / "compose_packs.py").write_text(
            "from pathlib import Path\n"
            "Path('AGENTS.md').write_text('composed rules\\n', encoding='utf-8')\n"
            f"raise SystemExit({composer_rc})\n",
            encoding="utf-8",
        )
    if generator_rc is not None:
        (scripts / "generate_agent_configs.py").write_text(
            "from pathlib import Path\n"
            f"rc = {generator_rc}\n"
            "if rc == 0:\n"
            "    agents = Path('AGENTS.md').read_text(encoding='utf-8')\n"
            "    Path('CLAUDE.md').write_text('generated claude\\n' + agents, encoding='utf-8')\n"
            "    Path('agents').mkdir(exist_ok=True)\n"
            "    Path('agents/codex.md').write_text('generated codex\\n' + agents, encoding='utf-8')\n"
            "raise SystemExit(rc)\n",
            encoding="utf-8",
        )
    python_path = tmp / "python_path"
    python_path.mkdir()
    yaml_stub = "# test PyYAML stand-in\n" if yaml_available else "raise ImportError('PyYAML blocked by test')\n"
    (python_path / "yaml.py").write_text(yaml_stub, encoding="utf-8")
    if not yaml_available:
        (python_path / "pip.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    home = tmp / "home"
    home.mkdir()
    return work, python_path, home


def _run_full_bootstrap_with_ledger(
    entrypoint: str,
    *,
    shell: str | None = None,
    composer_rc: int | None = 0,
    generator_rc: int | None = 0,
    yaml_available: bool = True,
    prepare=None,
    env_extra: dict | None = None,
    capture=None,
) -> tuple[subprocess.CompletedProcess, dict, dict[str, bytes]]:
    """Run a full bootstrap against the stub fixture and return its ledger.

    `prepare(work, home)` runs before the bootstrap and `capture(work, home)`
    after it, while the fixture still exists, for a test that plants files in
    the sparse clone or the home directory and reads back what the run wrote.
    """
    if entrypoint == "bash" and not BASH:
        raise unittest.SkipTest("bash not available (Git Bash on Windows or system bash on POSIX)")
    if entrypoint == "powershell" and not shell and not POWERSHELL:
        raise unittest.SkipTest("pwsh/powershell not available")
    tmp = Path(tempfile.mkdtemp(prefix=f"aa-generate-ledger-{entrypoint}-"))
    try:
        work, python_path, home = _prepare_full_bootstrap_fixture(
            tmp,
            composer_rc=composer_rc,
            generator_rc=generator_rc,
            yaml_available=yaml_available,
        )
        stub_dir = tmp / "stub_path"
        _make_stub_git(stub_dir, "git version 2.50.0")
        _write_executable(stub_dir / "curl", "#!/bin/sh\nexit 0\n")
        if entrypoint == "bash":
            env = _stripped_env(stub_dir)
            python_command = str(Path(sys.executable)).replace("\\", "/")
            command = [BASH, str(BOOTSTRAP_SH)]
        else:
            env = os.environ.copy()
            env["PATH"] = os.pathsep.join(
                (str(powershell_stub_dir(stub_dir)), env.get("PATH", "")))
            python_command = sys.executable
            wrapper = tmp / "invoke-bootstrap.ps1"
            bootstrap_literal = str(BOOTSTRAP_PS1).replace("'", "''")
            wrapper.write_text(
                "function Invoke-WebRequest { param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile) }\n"
                f"& '{bootstrap_literal}'\n"
                "if (-not $?) { exit $LASTEXITCODE }\n"
                "exit 0\n",
                encoding="utf-8",
            )
            command = [shell or POWERSHELL, "-NoProfile", "-NonInteractive",
                       "-ExecutionPolicy", "Bypass", "-File", str(wrapper)]
        env.pop("AGENT_CONFIG_PREFLIGHT_TEST", None)
        env["AGENT_CONFIG_UPSTREAM"] = "example/repo"
        env["ANYWHERE_AGENTS_PYTHON"] = python_command
        env["ANYWHERE_AGENTS_CODEX_AUTO_UPDATE"] = "off"
        env["ANYWHERE_AGENTS_GROK_AUTO_UPDATE"] = "off"
        env["PYTHONPATH"] = str(python_path)
        if not yaml_available:
            env["PIP_NO_INDEX"] = "1"
            env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        # Point the user-level config layer at the fixture home. Without
        # this the four-layer resolver reads the developer's real
        # %APPDATA%\anywhere-agents\config.yaml and the test stops being
        # hermetic: the answer changes with who runs it.
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env.update(env_extra or {})
        if prepare is not None:
            prepare(work, home)
        result = subprocess.run(
            command,
            cwd=str(work),
            env=env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if capture is not None:
            capture(work, home)
        ledger_path = work / ".agent-config" / "last-run.json"
        if not ledger_path.is_file():
            raise AssertionError(
                f"{entrypoint} did not emit a ledger; rc={result.returncode}; stderr={result.stderr!r}"
            )
        ledger = json.loads(ledger_path.read_text(encoding="utf-8-sig"))
        artifacts = {
            relative: (work / relative).read_bytes()
            for relative in ("AGENTS.md", "CLAUDE.md", "agents/codex.md")
            if (work / relative).is_file()
        }
        return result, ledger, artifacts
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _link_directory(link: Path, target: Path) -> bool:
    """Link `link` to the directory `target`, or report that neither form works.

    A POSIX symlink is refused on an unelevated Windows token (WinError 1314),
    but `mklink /J` builds a directory junction there without elevation, and
    that is the form a Windows consumer would end up with. Both are reparse
    points and both are what the bootstrap containment check has to reject.
    """
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name != "nt":
        return False
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
    )
    return completed.returncode == 0 and link.exists()


def _run_no_python_bootstrap_with_ledger(
    entrypoint: str,
    *,
    shell: str | None = None,
    config_text: str | None = None,
    local_config_text: str | None = None,
    user_config_text: str | None = None,
    existing_agents_text: str | None = None,
    existing_gitignore: str | None = None,
    tracked: tuple[str, ...] = (),
    env_extra: dict | None = None,
    capture_into: dict | None = None,
    capture_exists_into: dict | None = None,
    existing_todo_readme: str | None = None,
    existing_todo_file: str | None = None,
    existing_todo_readme_dir: bool = False,
    linked_todo: bool = False,
    linked_todo_readme: str | None = None,
    todo_readme: bool = True,
) -> tuple[subprocess.CompletedProcess, dict, str, bytes, bytes]:
    """Run a full bootstrap where every discoverable Python wrapper is broken.

    `tracked` names the paths the stub git reports as already in the index.
    `capture_into` is filled in place, keyed by repo-relative path, with each
    file's bytes or None when it does not exist. The fixture directory is
    removed on the way out, so anything a caller wants to assert on has to be
    read before then; the return tuple carries the files every caller needs and
    this covers the rest without changing its shape.
    """
    if entrypoint == "bash" and not BASH:
        raise unittest.SkipTest("bash not available (Git Bash on Windows or system bash on POSIX)")
    if entrypoint == "powershell" and not shell and not POWERSHELL:
        raise unittest.SkipTest("pwsh/powershell not available")
    tmp = Path(tempfile.mkdtemp(prefix=f"aa-no-python-ledger-{entrypoint}-"))
    try:
        work, _, home = _prepare_full_bootstrap_fixture(
            tmp,
            composer_rc=0,
            generator_rc=0,
            todo_readme=todo_readme,
        )
        if existing_todo_readme is not None:
            (work / "todo").mkdir(parents=True, exist_ok=True)
            (work / "todo" / "README.md").write_bytes(
                existing_todo_readme.encode("utf-8"))
        if existing_todo_file is not None:
            # A plain file where the drop box would go. `Test-Path 'todo'` is
            # true for it while `[ -d todo ]` is false, which is how the two
            # entry points came to disagree.
            (work / "todo").write_bytes(existing_todo_file.encode("utf-8"))
        if existing_todo_readme_dir:
            (work / "todo" / "README.md").mkdir(parents=True, exist_ok=True)
        if linked_todo:
            # The target sits under tmp beside work, so it is outside the
            # consumer repo but still inside what the finally block removes.
            link_target = tmp / "outside-the-repo"
            if linked_todo_readme is not None:
                link_target.mkdir(parents=True, exist_ok=True)
                (link_target / "README.md").write_bytes(
                    linked_todo_readme.encode("utf-8"))
            if not _link_directory(work / "todo", link_target):
                raise unittest.SkipTest(
                    "no directory symlink or junction could be created here")
        if config_text is not None:
            (work / "agent-config.yaml").write_text(config_text, encoding="utf-8")
        if local_config_text is not None:
            (work / "agent-config.local.yaml").write_text(local_config_text, encoding="utf-8")
        if user_config_text is not None:
            # Under the fixture home, matching the APPDATA / XDG_CONFIG_HOME
            # values the env block below sets.
            user_config = home / "AppData" / "Roaming" / "anywhere-agents" / "config.yaml"
            user_config.parent.mkdir(parents=True, exist_ok=True)
            user_config.write_text(user_config_text, encoding="utf-8")
            posix_config = home / ".config" / "anywhere-agents" / "config.yaml"
            posix_config.parent.mkdir(parents=True, exist_ok=True)
            posix_config.write_text(user_config_text, encoding="utf-8")
        if existing_agents_text is not None:
            # write_bytes rather than write_text: the caller compares the file
            # byte for byte afterwards, and text mode would rewrite the line
            # endings on Windows and make that comparison meaningless.
            (work / "AGENTS.md").write_bytes(existing_agents_text.encode("utf-8"))
        if existing_gitignore is not None:
            (work / ".gitignore").write_bytes(existing_gitignore.encode("utf-8"))
        broken_wrapper = "#!/bin/sh\nexit 127\n"
        hooks = home / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        _write_executable(hooks / "_python", broken_wrapper)
        _write_executable(work / ".agent-config" / "repo" / "scripts" / "_python", broken_wrapper)

        stub_dir = tmp / "stub_path"
        _make_stub_git(stub_dir, "git version 2.50.0")
        path_wrapper = stub_dir / "python"
        _write_executable(path_wrapper, broken_wrapper)
        if os.name == "nt":
            # .ps1, not .cmd: PowerShell runs it in-process, so the interpreter
            # probe creates no process and cannot flash a console. It goes in
            # the PowerShell-only directory so PowerShell never sees the
            # extensionless shell script beside it. See the note in
            # _make_stub_git.
            ps_dir = powershell_stub_dir(stub_dir)
            ps_dir.mkdir(parents=True, exist_ok=True)
            (ps_dir / "python.ps1").write_text("exit 127\n", encoding="ascii")
        _write_executable(stub_dir / "curl", "#!/bin/sh\nexit 0\n")

        if entrypoint == "bash":
            env = _stripped_env(stub_dir)
            override = str(path_wrapper).replace("\\", "/")
            command = [BASH, str(BOOTSTRAP_SH)]
        else:
            env = os.environ.copy()
            env["PATH"] = str(powershell_stub_dir(stub_dir))
            # Point the override at the .ps1 too. Handing PowerShell the
            # extensionless shell script makes CreateProcess fail on a file
            # that is not a PE image, and the fallback is ShellExecute, which
            # opens the "How do you want to open this file?" dialog and blocks
            # the run until it is dismissed. Only Windows has that problem and
            # only Windows has the .ps1; elsewhere the shell script is the one
            # pwsh can run.
            if os.name == "nt":
                override = str(powershell_stub_dir(stub_dir) / "python.ps1")
            else:
                override = str(path_wrapper)
            wrapper = tmp / "invoke-bootstrap.ps1"
            bootstrap_literal = str(BOOTSTRAP_PS1).replace("'", "''")
            wrapper.write_text(
                "function Invoke-WebRequest { param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile) }\n"
                f"& '{bootstrap_literal}'\n"
                "if (-not $?) { exit $LASTEXITCODE }\n"
                "exit 0\n",
                encoding="utf-8",
            )
            command = [shell or POWERSHELL, "-NoProfile", "-NonInteractive",
                       "-ExecutionPolicy", "Bypass", "-File", str(wrapper)]

        env.pop("AGENT_CONFIG_PREFLIGHT_TEST", None)
        env.pop("CONDA_PREFIX", None)
        env.pop("CONDA_ROOT", None)
        env["AGENT_CONFIG_UPSTREAM"] = "example/repo"
        env["ANYWHERE_AGENTS_PYTHON"] = override
        env["ANYWHERE_AGENTS_CODEX_AUTO_UPDATE"] = "off"
        env["ANYWHERE_AGENTS_GROK_AUTO_UPDATE"] = "off"
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        # Point the user-level config layer at the fixture home. Without
        # this the four-layer resolver reads the developer's real
        # %APPDATA%\anywhere-agents\config.yaml and the test stops being
        # hermetic: the answer changes with who runs it.
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["ANYWHERE_AGENTS_STUB_TRACKED"] = " ".join(tracked)
        if env_extra:
            env.update(env_extra)
        result = subprocess.run(
            command,
            cwd=str(work),
            env=env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if capture_into is not None:
            for relative in list(capture_into):
                captured = work / relative
                capture_into[relative] = captured.read_bytes() if captured.is_file() else None
        if capture_exists_into is not None:
            # Separate from capture_into on purpose. That one reports bytes and
            # maps both an absent path and a directory to None, so a test that
            # asserts "no README" cannot tell a clean skip from an empty todo/
            # left behind. Existence is the question those tests actually ask.
            for relative in list(capture_exists_into):
                capture_exists_into[relative] = (work / relative).exists()
        ledger_path = work / ".agent-config" / "last-run.json"
        if not ledger_path.is_file():
            raise AssertionError(
                f"{entrypoint} did not emit a ledger; rc={result.returncode}; stderr={result.stderr!r}"
            )
        raw_ledger = ledger_path.read_text(encoding="utf-8-sig")
        agents_bytes = (work / "AGENTS.md").read_bytes()
        upstream_bytes = (work / ".agent-config" / "AGENTS.md").read_bytes()
        return result, json.loads(raw_ledger), raw_ledger, agents_bytes, upstream_bytes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_atomic_helper_deployment(
    entrypoint: str,
    shell: str | None = None,
) -> tuple[subprocess.CompletedProcess, bytes, bytes, bytes, bool]:
    """Pause a real helper copy mid-write and read the live destination."""
    if entrypoint == "bash" and not BASH:
        raise unittest.SkipTest("bash not available (Git Bash on Windows or system bash on POSIX)")
    if entrypoint == "powershell" and not shell and not POWERSHELL:
        raise unittest.SkipTest("pwsh/powershell not available")
    tmp = Path(tempfile.mkdtemp(prefix=f"aa-atomic-helper-{entrypoint}-"))
    try:
        work, python_path, home = _prepare_full_bootstrap_fixture(
            tmp,
            composer_rc=0,
            generator_rc=None,
        )
        source = work / ".agent-config" / "repo" / "scripts" / "_python"
        old_content = b"#!/usr/bin/env bash\n# old helper\nexit 0\n"
        new_content = b"#!/usr/bin/env bash\n# new helper\nexit 0\n"
        source.write_bytes(new_content)
        hooks = home / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        target = hooks / "_python"
        _write_executable(target, old_content.decode("ascii"))
        signal = tmp / "copy-started"

        stub_dir = tmp / "stub_path"
        _make_stub_git(stub_dir, "git version 2.50.0")
        _write_executable(stub_dir / "curl", "#!/bin/sh\nexit 0\n")
        if entrypoint == "bash":
            real_cp = shutil.which("cp")
            if not real_cp and os.name == "nt" and BASH:
                bash_path = Path(BASH).resolve()
                candidates = (
                    bash_path.parent / "cp.exe",
                    bash_path.parent.parent / "usr" / "bin" / "cp.exe",
                )
                real_cp = next((str(path) for path in candidates if path.is_file()), None)
            if not real_cp:
                raise unittest.SkipTest("cp not available for atomic-deployment probe")
            _write_executable(
                stub_dir / "cp",
                "#!/bin/sh\n"
                "source_path=''\n"
                "destination=''\n"
                "for argument in \"$@\"; do\n"
                "  case \"$argument\" in\n"
                "    -*) ;;\n"
                "    *) if [ -z \"$source_path\" ]; then source_path=$argument; else destination=$argument; fi ;;\n"
                "  esac\n"
                "done\n"
                "case \"$source_path\" in\n"
                "  */_python)\n"
                "    printf '%s' partial > \"$destination\"\n"
                "    printf '%s' ready > \"$ATOMIC_COPY_SIGNAL\"\n"
                "    sleep 1\n"
                "    \"$REAL_CP\" -f \"$source_path\" \"$destination\"\n"
                "    ;;\n"
                "  *) exec \"$REAL_CP\" \"$@\" ;;\n"
                "esac\n",
            )
            env = _stripped_env(stub_dir)
            env["REAL_CP"] = str(Path(real_cp).resolve()).replace("\\", "/")
            env["ATOMIC_COPY_SIGNAL"] = str(signal).replace("\\", "/")
            command = [BASH, str(BOOTSTRAP_SH)]
        else:
            env = os.environ.copy()
            env["PATH"] = os.pathsep.join(
                (str(powershell_stub_dir(stub_dir)), env.get("PATH", "")))
            env["ATOMIC_COPY_SIGNAL"] = str(signal)
            bootstrap_literal = str(BOOTSTRAP_PS1).replace("'", "''")
            wrapper = tmp / "invoke-bootstrap.ps1"
            wrapper.write_text(
                "function Invoke-WebRequest { param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile) }\n"
                "function Copy-Item {\n"
                "  [CmdletBinding()] param(\n"
                "    [Parameter(Position=0)][string[]]$Path,\n"
                "    [string[]]$LiteralPath,\n"
                "    [Parameter(Position=1)][string]$Destination,\n"
                "    [switch]$Force\n"
                "  )\n"
                "  $sourcePath = if ($LiteralPath) { [string]$LiteralPath[0] } else { [string]$Path[0] }\n"
                "  if ($sourcePath -like '*_python') {\n"
                "    [System.IO.File]::WriteAllText($Destination, 'partial')\n"
                "    [System.IO.File]::WriteAllText($env:ATOMIC_COPY_SIGNAL, 'ready')\n"
                "    Start-Sleep -Seconds 1\n"
                "  }\n"
                "  if ($LiteralPath) {\n"
                "    Microsoft.PowerShell.Management\\Copy-Item -LiteralPath $LiteralPath -Destination $Destination -Force\n"
                "  } else {\n"
                "    Microsoft.PowerShell.Management\\Copy-Item -Path $Path -Destination $Destination -Force\n"
                "  }\n"
                "}\n"
                f"& '{bootstrap_literal}'\n"
                "if (-not $?) { exit $LASTEXITCODE }\n"
                "exit 0\n",
                encoding="utf-8",
            )
            command = [shell or POWERSHELL, "-NoProfile", "-NonInteractive",
                       "-ExecutionPolicy", "Bypass", "-File", str(wrapper)]

        env.pop("AGENT_CONFIG_PREFLIGHT_TEST", None)
        env["AGENT_CONFIG_UPSTREAM"] = "example/repo"
        env["ANYWHERE_AGENTS_PYTHON"] = str(Path(sys.executable)).replace("\\", "/") if entrypoint == "bash" else sys.executable
        env["ANYWHERE_AGENTS_CODEX_AUTO_UPDATE"] = "off"
        env["ANYWHERE_AGENTS_GROK_AUTO_UPDATE"] = "off"
        env["PYTHONPATH"] = str(python_path)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        # Point the user-level config layer at the fixture home. Without
        # this the four-layer resolver reads the developer's real
        # %APPDATA%\anywhere-agents\config.yaml and the test stops being
        # hermetic: the answer changes with who runs it.
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        process = subprocess.Popen(
            command,
            cwd=str(work),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 20
        while not signal.is_file() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.005)
        running_when_observed = signal.is_file() and process.poll() is None
        observed = target.read_bytes()
        stdout, stderr = process.communicate(timeout=SUBPROCESS_TIMEOUT)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        return result, observed, target.read_bytes(), new_content, running_when_observed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_bash_self_update() -> tuple[subprocess.CompletedProcess, bytes, bytes, bytes, dict]:
    """Run bootstrap through its deployed path while it installs a shorter successor."""
    if not BASH:
        raise unittest.SkipTest("bash not available (Git Bash on Windows or system bash on POSIX)")
    tmp = Path(tempfile.mkdtemp(prefix="aa-self-update-bash-"))
    try:
        work, python_path, home = _prepare_full_bootstrap_fixture(
            tmp,
            composer_rc=0,
            generator_rc=None,
        )
        deployed = work / ".agent-config" / "bootstrap.sh"
        original = BOOTSTRAP_SH.read_bytes()
        deployed.write_bytes(original)
        deployed.chmod(deployed.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        replacement = b"#!/bin/sh\n# shorter self-update fixture\nexit 0\n"
        upstream = work / ".agent-config" / "repo" / "bootstrap" / "bootstrap.sh"
        # exist_ok: the shared fixture already puts todo-readme.md in this
        # directory, so this is no longer the first writer into it.
        upstream.parent.mkdir(parents=True, exist_ok=True)
        upstream.write_bytes(replacement)
        upstream.chmod(upstream.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        stub_dir = tmp / "stub_path"
        _make_stub_git(stub_dir, "git version 2.50.0")
        _write_executable(stub_dir / "curl", "#!/bin/sh\nexit 0\n")
        env = _stripped_env(stub_dir)
        env["AGENT_CONFIG_UPSTREAM"] = "example/repo"
        env["ANYWHERE_AGENTS_PYTHON"] = str(Path(sys.executable)).replace("\\", "/")
        env["ANYWHERE_AGENTS_CODEX_AUTO_UPDATE"] = "off"
        env["ANYWHERE_AGENTS_GROK_AUTO_UPDATE"] = "off"
        env["PYTHONPATH"] = str(python_path)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        # Point the user-level config layer at the fixture home. Without
        # this the four-layer resolver reads the developer's real
        # %APPDATA%\anywhere-agents\config.yaml and the test stops being
        # hermetic: the answer changes with who runs it.
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        result = subprocess.run(
            [BASH, str(deployed)],
            cwd=str(work),
            env=env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        ledger = json.loads(
            (work / ".agent-config" / "last-run.json").read_text(encoding="utf-8-sig")
        )
        return result, deployed.read_bytes(), replacement, original, ledger
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
                # .ps1 for the same reason as the other stubs: a .cmd would be
                # run through cmd.exe and show a console window.
                ps_dir = powershell_stub_dir(stub_dir)
                ps_dir.mkdir(parents=True, exist_ok=True)
                (ps_dir / "git.ps1").write_text("exit 0\n", encoding="ascii")
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
                timeout=SUBPROCESS_TIMEOUT,
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
                timeout=SUBPROCESS_TIMEOUT,
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


class _BootstrapLedgerContract:
    entrypoint = ""
    emitted_by = ""
    # Which PowerShell to drive. None means the default pick; the generated
    # subclasses below set one edition each, so a defect that reproduces
    # under only one of them cannot hide behind the other.
    shell: str | None = None

    def _run_preflight_ledger(
        self,
        version_line: str | None,
        *,
        scenario: str = "fresh",
        upstream: str | None = None,
    ) -> tuple[subprocess.CompletedProcess, dict | None]:
        if self.entrypoint == "bash":
            return _run_bootstrap_sh_with_ledger(
                version_line,
                scenario=scenario,
                upstream=upstream,
            )
        return _run_bootstrap_ps1_with_ledger(
            version_line,
            scenario=scenario,
            upstream=upstream,
        )

    @staticmethod
    def _phase(ledger: dict, phase: str) -> dict:
        matches = [step for step in ledger["steps"] if step["phase"] == phase]
        if len(matches) != 1:
            raise AssertionError(f"expected one {phase!r} step, got {matches!r}")
        return matches[0]

    def test_ledger_written_at_preflight_boundary(self):
        result, ledger = self._run_preflight_ledger("git version 2.50.0")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger["schema"], 1)
        self.assertEqual(ledger["emitted_by"], self.emitted_by)
        self.assertIs(ledger["completed"], False)
        self.assertEqual(ledger["last_phase"], "start")
        self.assertEqual(ledger["steps"], [])
        self.assertIsInstance(ledger["run_id"], str)
        self.assertTrue(ledger["run_id"])
        self.assertIsInstance(ledger["started_at"], str)
        self.assertTrue(ledger["started_at"])

    def test_ledger_records_incomplete_run_on_preflight_failure(self):
        result, ledger = self._run_preflight_ledger("git version 2.24.0")
        self.assertNotEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNotNone(ledger)
        self.assertIs(ledger["completed"], False)
        self.assertEqual(ledger["last_phase"], "start")

    def test_ledger_round_trips_hostile_upstream(self):
        upstreams = (
            'evil"x/re\\po',
            "evil\nrepo",
            "evil\rrepo",
            "evil\trepo",
            "evil\brepo",
            "evil\frepo",
            "evil\x01repo",
        )
        for upstream in upstreams:
            with self.subTest(upstream=repr(upstream)):
                result, ledger = self._run_preflight_ledger(
                    "git version 2.50.0",
                    upstream=upstream,
                )
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertIsNotNone(ledger)
                self.assertEqual(ledger["upstream"], upstream)

    def test_ledger_never_aborts_run_when_unwritable(self):
        result, ledger = self._run_preflight_ledger(
            "git version 2.50.0",
            scenario="unwritable",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNone(ledger)

    def test_generate_ok(self):
        result, ledger, _ = _run_full_bootstrap_with_ledger(
            self.entrypoint,
            generator_rc=0,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        step = self._phase(ledger, "generate")
        self.assertEqual(step["status"], "ok")
        self.assertIsNone(step["rc"])
        self.assertEqual(step["targets"], ["CLAUDE.md", "agents/codex.md"])

    # The model sync: bootstrap runs agent_models.py sync after the guard is
    # deployed, so the interactive Codex and Agy values the registry names are
    # in the tools' own config before the first session reads them. The
    # registry here is invented; nothing depends on the live values.
    SYNC_REGISTRY = {
        "schema": 1,
        "families": {
            "claude": {"model": "claude-test-1", "effort": "high"},
            "codex": {"model": "codex-test-model", "effort": "medium"},
            "agy": {"model": "testy-2-stem", "effort": "high"},
        },
        "roles": {},
    }

    def _sync_run(self, *, resolver_text: str | None = None, env_extra: dict | None = None):
        """Run a bootstrap with the real resolver and an invented registry.

        Returns the run result, its ledger, and the Codex config bytes read
        back after the run (None when the file is absent).
        """
        seen: dict = {}

        def prepare(work: Path, home: Path) -> None:
            repo = work / ".agent-config" / "repo"
            (repo / "user").mkdir(parents=True, exist_ok=True)
            (repo / "user" / "agent-models.json").write_text(
                json.dumps(self.SYNC_REGISTRY), encoding="utf-8")
            resolver = repo / "scripts" / "agent_models.py"
            if resolver_text is None:
                shutil.copy2(ROOT / "scripts" / "agent_models.py", resolver)
            else:
                resolver.write_text(resolver_text, encoding="utf-8")
            (repo / "scripts" / "guard.py").write_text("# guard stub\n", encoding="utf-8")
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_bytes(
                b'model = "old-model"\nmodel_reasoning_effort = "low"  # kept comment\n'
                b'service_tier = "standard"\n\n[features]\nfast_mode = false\n')
            (home / ".gemini" / "antigravity-cli").mkdir(parents=True)

        def capture(work: Path, home: Path) -> None:
            config = home / ".codex" / "config.toml"
            seen["codex"] = config.read_bytes() if config.is_file() else None

        # No real Antigravity CLI: the sync would otherwise ask the developer's
        # own `agy models` for a listing.
        env = {"ANTIGRAVITY_BIN": "agy-is-not-installed-here", **(env_extra or {})}
        result, ledger, _ = _run_full_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, prepare=prepare, capture=capture,
            env_extra=env)
        return result, ledger, seen["codex"]

    @unittest.skipUnless(sys.version_info >= (3, 11),
                         "the Codex config edit needs the standard library TOML parser")
    def test_models_sync_writes_the_registry_into_the_codex_config(self):
        result, ledger, codex = self._sync_run()
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        text = codex.decode("utf-8")
        self.assertIn('model = "codex-test-model"', text)
        self.assertIn('model_reasoning_effort = "medium"  # kept comment', text)
        self.assertIn('service_tier = "standard"', text)
        self.assertIn("[features]\nfast_mode = false", text.replace("\r\n", "\n"))
        targets = self._phase(ledger, "user_files")["targets"]
        self.assertIn("~/.codex/config.toml", targets)
        self.assertIn("~/.gemini/antigravity-cli/settings.json", targets)
        self.assertIs(ledger["completed"], True)

    def test_models_sync_can_be_switched_off(self):
        before = b'model = "old-model"\nmodel_reasoning_effort = "low"  # kept comment\nservice_tier = "standard"\n\n[features]\nfast_mode = false\n'
        for value in ("off", "0", "False", "NO", "disabled"):
            with self.subTest(AGENT_MODELS_SYNC=value):
                result, ledger, codex = self._sync_run(env_extra={"AGENT_MODELS_SYNC": value})
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertEqual(codex.replace(b"\r\n", b"\n"), before)
                targets = self._phase(ledger, "user_files")["targets"]
                self.assertNotIn("~/.codex/config.toml", targets)

    def test_a_failing_resolver_never_fails_the_run(self):
        result, ledger, codex = self._sync_run(
            resolver_text="import sys\nprint('resolver broke', file=sys.stderr)\nsys.exit(7)\n")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIs(ledger["completed"], True)
        self.assertEqual(self._phase(ledger, "user_files")["status"], "ok")
        self.assertIn(b'model = "old-model"', codex)

    def test_models_sync_is_skipped_without_a_resolver_in_the_clone(self):
        """A sparse clone that predates the resolver runs exactly as before."""
        result, ledger, _ = _run_full_bootstrap_with_ledger(self.entrypoint, shell=self.shell)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn("~/.codex/config.toml", self._phase(ledger, "user_files")["targets"])

    def test_generate_failed(self):
        result, ledger, _ = _run_full_bootstrap_with_ledger(
            self.entrypoint,
            generator_rc=7,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        step = self._phase(ledger, "generate")
        self.assertEqual(step["status"], "failed")
        self.assertEqual(step["rc"], 7)
        self.assertEqual(step["targets"], [])

    def test_generate_skipped(self):
        result, ledger, _ = _run_full_bootstrap_with_ledger(
            self.entrypoint,
            generator_rc=None,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        step = self._phase(ledger, "generate")
        self.assertEqual(step["status"], "skipped")
        self.assertIsNone(step["rc"])
        self.assertEqual(step["targets"], [])

    def test_generate_recorded_before_composer_failure_exit(self):
        result, ledger, _ = _run_full_bootstrap_with_ledger(
            self.entrypoint,
            composer_rc=23,
            generator_rc=9,
        )
        self.assertEqual(result.returncode, 23, msg=result.stderr)
        compose = self._phase(ledger, "compose")
        self.assertEqual(compose["status"], "failed")
        self.assertEqual(compose["rc"], 23)
        generate = self._phase(ledger, "generate")
        self.assertEqual(generate["status"], "failed")
        self.assertEqual(generate["rc"], 9)

    def test_no_python_fallback_is_marked_and_incomplete_for_passive_pack(self):
        result, ledger, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs:\n  - name: agent-style\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        reason = "no Python 3 interpreter found"
        marker = f"<!-- rule-pack composition skipped: {reason}; run anywhere-agents to compose -->\n".encode()
        self.assertNotEqual(agents_bytes, upstream_bytes)
        self.assertTrue(agents_bytes.startswith(marker), msg=agents_bytes[:200])
        self.assertEqual(agents_bytes[len(marker):], upstream_bytes)
        compose = self._phase(ledger, "compose")
        self.assertEqual(compose["status"], "skipped")
        self.assertEqual(compose["reason"], reason)
        self.assertIs(ledger["completed"], False)

    def test_missing_composer_upstream_is_marked_but_not_incomplete(self):
        # agent-config deliberately ships the generator and no composer, so
        # bootstrapping from an ac-shaped remote always takes this branch. The
        # artifact still carries the marker, because it is not a composed file
        # and must not be mistaken for one. The run is not incomplete, because
        # nothing about that upstream is broken and there is nothing to fix.
        # test_repo's end-to-end bash smoke test bootstraps from exactly such a
        # snapshot, and flagging it turned every platform in CI red.
        result, ledger, artifacts = _run_full_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            composer_rc=None,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        compose = self._phase(ledger, "compose")
        self.assertEqual(compose["status"], "skipped")
        self.assertEqual(compose["reason"], "no composer script in sparse clone")
        self.assertIn(b"rule-pack composition skipped", artifacts["AGENTS.md"])
        self.assertIs(ledger["completed"], True)

    def test_local_empty_opt_out_overrides_tracked_selection(self):
        # The layers are ordered, not merged. Probing packs.config directly
        # returns [] for tracked [agent-style] plus local [], so this is a
        # deliberate opt-out and must not be marked as an incomplete run.
        # The predicate previously treated either layer's non-emptiness as
        # sufficient and reported completed:false here.
        result, ledger, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs:\n  - name: agent-style\n",
            local_config_text="rule_packs: []\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, upstream_bytes)
        self.assertNotIn(b"rule-pack composition skipped", agents_bytes)
        self.assertIs(ledger["completed"], True)

    def test_local_nonempty_selection_still_counts_as_configured(self):
        # The mirror of the case above: a non-empty local layer is a real
        # selection, so an omitted composition is still an incomplete run.
        # Without this the fix could pass by always returning false.
        result, ledger, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs: []\n",
            local_config_text="rule_packs:\n  - name: agent-style\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotEqual(agents_bytes, upstream_bytes)
        self.assertIn(b"rule-pack composition skipped", agents_bytes)
        self.assertIs(ledger["completed"], False)

    def test_zero_indent_selection_counts_as_configured(self):
        # A YAML block sequence may sit at the same indentation as its key, and
        # that is what PyYAML's safe_dump emits. anywhere-agents writes
        # agent-config.yaml with safe_dump, so this is the shape real consumer
        # repos carry; every other test here uses an indented list, which is
        # the shape none of them have. The no-Python pre-parser required the
        # indentation and so read a configured consumer as an explicit
        # `rule_packs: []` opt-out. A degraded run then wrote the bare upstream
        # file with no marker and reported completed:true, which is the one
        # combination that leaves nothing for anyone to notice.
        result, ledger, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs:\n- name: agent-style\n  source:\n    url: https://example.invalid\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn(b"rule-pack composition skipped", agents_bytes)
        self.assertNotEqual(agents_bytes, upstream_bytes)
        self.assertIs(ledger["completed"], False)

    def test_zero_indent_empty_opt_out_is_still_an_opt_out(self):
        # The mirror: fixing the indentation assumption must not turn the
        # explicit opt-out into a configured selection.
        result, ledger, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs: []\nother: 1\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, upstream_bytes)
        self.assertNotIn(b"rule-pack composition skipped", agents_bytes)
        self.assertIs(ledger["completed"], True)

    def test_canonical_packs_wins_over_the_legacy_alias(self):
        # The earlier version of the test above used `rule_packs: []` beside a
        # nonempty `packs:` and expected the alias to win. The resolver prefers
        # the canonical key within one file, so that fixture is a configured
        # consumer and a run that cannot compose must preserve its artifact
        # rather than replace it.
        result, ledger, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs: []\npacks:\n- name: agent-style\n",
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, self.COMPOSED_AGENTS.encode("utf-8"))
        self.assertNotEqual(agents_bytes, upstream_bytes)
        self.assertIs(ledger["completed"], False)

    def test_canonical_empty_list_clears_a_legacy_selection(self):
        # The inverse. A canonical opt-out beside a legacy selection is an
        # opt-out, and the composed leftover must go.
        result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs:\n- name: agent-style\npacks: []\n",
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, upstream_bytes)

    def test_user_level_opt_out_clears_the_default_selection(self):
        # The layer that had no representation at all before. A user-level
        # `packs: []` clears the seeded default, so a composed leftover is not
        # preserved.
        result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            user_config_text="packs: []\n",
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, upstream_bytes)

    def test_env_overlay_restores_a_cleared_selection(self):
        # The overlay is additive and applies after the file layers, so it can
        # turn a cleared selection back on. Reading only the legacy env name
        # meant a canonical overlay deleted the pack block it had just asked
        # for.
        result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs: []\n",
            env_extra={"AGENT_CONFIG_PACKS": "agent-style"},
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, self.COMPOSED_AGENTS.encode("utf-8"))
        self.assertNotEqual(agents_bytes, upstream_bytes)

    def test_env_overlay_of_only_subtractions_adds_nothing(self):
        # A value made only of `-name` entries removes; it must not read as a
        # selection that revives a cleared one.
        result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="packs: []\n",
            env_extra={"AGENT_CONFIG_PACKS": "-agent-style"},
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, upstream_bytes)

    def test_a_multiline_null_still_clears(self):
        # The conservative indented-node rule read `packs:\n  null` as a
        # selection, so the opt-out stopped working the moment it was written
        # on its own line. Preserving is the safe direction, but an opt-out
        # that silently does nothing is still a defect.
        result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="packs:\n  null\n",
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, upstream_bytes)

    # A later layer that selects, in a spelling the scanner now reads. Each has
    # to override the tracked clear on its own, without the uncertainty valve.
    READABLE_KEY_SPELLINGS = (
        ("quoted key", '"packs": [agent-style]\n'),
        ("space before the colon", "packs : [agent-style]\n"),
        ("single-quoted key", "'packs': [agent-style]\n"),
    )

    def test_a_later_layer_in_another_key_spelling_selects(self):
        # Valid YAML the key match did not cover. All three answered `none`
        # here and select agent-style in the resolver, and after a tracked
        # clear that answer deleted the block the later layer had just asked
        # for. Reading the spelling is what makes this a selection rather than
        # a guess: the first repair used the later file's length as a stand-in
        # for "unreadable", which preserved these but also overrode opt-outs.
        for label, local_text in self.READABLE_KEY_SPELLINGS:
            with self.subTest(spelling=label):
                result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
                    self.entrypoint,
                    shell=self.shell,
                    config_text="packs: []\n",
                    local_config_text=local_text,
                    existing_agents_text=self.COMPOSED_AGENTS,
                )
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertEqual(agents_bytes, self.COMPOSED_AGENTS.encode("utf-8"))
                self.assertNotEqual(agents_bytes, upstream_bytes)

    # A later layer that says nothing about packs. The clear has to stand.
    LAYERS_THAT_SAY_NOTHING = (
        ("only comments", "# machine-local overrides go here\n\n  # nothing yet\n"),
        ("only unrelated keys", "verbose: true\nother:\n- name: x\n"),
        ("an explicit clear in another spelling", '"packs": []\n'),
        ("a spaced clear", "packs : []\n"),
        ("whitespace only", "   \n\t\n"),
    )

    def test_a_later_layer_that_says_nothing_leaves_the_clear_standing(self):
        # The half the first repair got wrong. Treating any later file with
        # bytes in it as uncertainty meant an opt-out stopped taking effect as
        # soon as the project had a local file at all, which is a state the
        # resolver never produces.
        for label, local_text in self.LAYERS_THAT_SAY_NOTHING:
            with self.subTest(layer=label):
                result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
                    self.entrypoint,
                    shell=self.shell,
                    config_text="packs: []\n",
                    local_config_text=local_text,
                    existing_agents_text=self.COMPOSED_AGENTS,
                )
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertEqual(agents_bytes, upstream_bytes)

    # Shapes the scanner reads as no key while YAML reads a selection. Each has
    # to reach the valve and preserve.
    LAYERS_THE_SCANNER_CANNOT_READ = (
        # A root-level flow mapping: a selection to YAML, and a line whose head
        # is not a name to this scanner.
        ("a flow mapping at the root", "{packs: [agent-style]}\n"),
        # A whole document written one indent in. YAML reads a mapping; the
        # scanner reads a nested key. Treating the indentation as a
        # continuation of the line above assumed there was a line above.
        ("an indented document", "  packs: [agent-style]\n"),
    )

    def test_a_later_layer_this_scanner_cannot_read_preserves(self):
        # What the valve is actually for.
        for label, local_text in self.LAYERS_THE_SCANNER_CANNOT_READ:
            with self.subTest(layer=label):
                result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
                    self.entrypoint,
                    shell=self.shell,
                    config_text="packs: []\n",
                    local_config_text=local_text,
                    existing_agents_text=self.COMPOSED_AGENTS,
                )
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertEqual(agents_bytes, self.COMPOSED_AGENTS.encode("utf-8"))
                self.assertNotEqual(agents_bytes, upstream_bytes)

    def test_an_absent_later_layer_leaves_the_clear_standing(self):
        # The other half of the rule above. Uncertainty needs a file to be
        # uncertain about; with none, the opt-out still takes effect.
        result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="packs: []\n",
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, upstream_bytes)

    INVALID_OVERLAY_TOKENS = ("-", "-bad/name", "-bad:name", "-bad@ref")

    def test_an_invalid_overlay_preserves_rather_than_deletes(self):
        # config.parse_env_var rejects each of these, so with Python present
        # the run fails and the artifact is untouched. Read as plain
        # subtractions, they cleared the selection and deleted it instead.
        for token in self.INVALID_OVERLAY_TOKENS:
            with self.subTest(token=token):
                result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
                    self.entrypoint,
                    shell=self.shell,
                    config_text="packs: []\n",
                    env_extra={"AGENT_CONFIG_PACKS": token},
                    existing_agents_text=self.COMPOSED_AGENTS,
                )
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertEqual(agents_bytes, self.COMPOSED_AGENTS.encode("utf-8"))
                self.assertNotEqual(agents_bytes, upstream_bytes)

    def test_a_cancelling_overlay_preserves_rather_than_deletes(self):
        # Recorded gap 2. The gate answers "is anything selected", not "which
        # packs", so an overlay whose additions and subtractions cancel reads as
        # one addition here and as no selection in the resolver. This pins the
        # direction: the wrong answer keeps a file the resolver would replace,
        # and the reverse is what this release exists to stop.
        result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="packs: []\n",
            env_extra={"AGENT_CONFIG_PACKS": "agent-style,-agent-style"},
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, self.COMPOSED_AGENTS.encode("utf-8"))

    def test_a_selection_of_another_pack_preserves_the_old_marker(self):
        # Recorded gap 3. Marker names are never compared with selected names,
        # so a cleared base plus a project-local selection of some other pack
        # keeps a composed file carrying only the old pack's block. Stale
        # content rather than none, and the next run with Python recomposes it.
        result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="packs: []\n",
            local_config_text="packs:\n- name: aa-core-skills\n",
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, self.COMPOSED_AGENTS.encode("utf-8"))
        self.assertNotEqual(agents_bytes, upstream_bytes)

    def test_an_indented_flow_sequence_is_a_selection(self):
        # The end-to-end half of the table case. Both scanners skipped the
        # indented node and answered `empty`, the explicit opt-out, so the run
        # replaced a composed AGENTS.md with the bare upstream copy and
        # recorded `completed: true`. Measured under all three entry points.
        result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="packs:\n  [agent-style]\n",
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, self.COMPOSED_AGENTS.encode("utf-8"))
        self.assertNotEqual(agents_bytes, upstream_bytes)

    TIP = "ships with agent-style writing rules enabled by default"

    def test_the_skipped_packs_tip_fires_when_nothing_mentions_packs(self):
        # The control for the two cases below. Without it they pass for a
        # consumer who never sees the tip at all.
        result, _, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn(self.TIP, result.stderr)

    def test_a_user_level_mention_silences_the_tip(self):
        # The tip tells the operator packs were skipped and offers `rule_packs:
        # []` to silence it. Told that to a consumer who has a user-level
        # selection, it is wrong twice over.
        result, _, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            user_config_text="packs:\n- name: agent-style\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn(self.TIP, result.stderr)

    def test_the_canonical_env_name_silences_the_tip(self):
        # The awareness check read only the deprecated env name, so a consumer
        # on the documented one was told packs were never asked for.
        result, _, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            env_extra={"AGENT_CONFIG_PACKS": "agent-style"},
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn(self.TIP, result.stderr)

    COMPOSED_AGENTS = (
        "# Composed AGENTS.md\n"
        "<!-- rule-pack:agent-style:begin version=v0.4.2 sha256=" + EMPTY_SHA256 + " -->\n"
        "RULE-01 through RULE-I live here.\n"
        "<!-- rule-pack:agent-style:end -->\n"
        "trailing upstream content\n"
    )

    def test_composed_agents_md_survives_a_run_that_cannot_compose(self):
        # The damage this guards against: a run that cannot compose used to
        # replace a composed AGENTS.md with the verbatim upstream copy, which
        # deletes every pack block. Where the file is tracked, that surfaces as
        # a working-tree deletion waiting to be committed; two consumer repos
        # carried a 473-line one. The predicate reads the artifact rather than
        # the configuration, so it holds even when the configuration is misread.
        result, ledger, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs:\n- name: agent-style\n",
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, self.COMPOSED_AGENTS.encode("utf-8"))
        self.assertNotEqual(agents_bytes, upstream_bytes)
        self.assertNotIn(b"rule-pack composition skipped", agents_bytes)
        compose = self._phase(ledger, "compose")
        self.assertEqual(compose["status"], "skipped")
        self.assertIn("existing composed AGENTS.md preserved", compose["reason"])
        self.assertIs(ledger["completed"], False)
        # Preserving quietly would be its own failure: the file is now as stale
        # as the last successful composition and nothing else says so.
        self.assertIn("left it untouched", result.stderr)

    def test_opt_out_replaces_a_composed_agents_md(self):
        # Preservation keyed on the artifact alone, so a consumer who had packs,
        # composed once, then set `rule_packs: []` kept the composed file for
        # good: every later run saw the marker and preserved, and the opt-out
        # never took effect. Gate preservation on the configuration too. Here
        # the operator has said they want no packs, so the composed leftover is
        # exactly what should go.
        result, ledger, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs: []\n",
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, upstream_bytes)
        self.assertNotIn(b"rule-pack:agent-style:begin", agents_bytes)
        self.assertNotIn(b"rule-pack composition skipped", agents_bytes)
        compose = self._phase(ledger, "compose")
        self.assertNotIn("preserved", compose["reason"])
        self.assertIs(ledger["completed"], True)

    def test_local_layer_opt_out_replaces_a_composed_agents_md(self):
        # The same override reached through the layer that wins. A tracked
        # selection with a machine-local empty list resolves to no packs, so the
        # preservation gate has to read the resolved answer rather than the
        # tracked file it happens to find first.
        result, _, _, agents_bytes, upstream_bytes = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs:\n- name: agent-style\n",
            local_config_text="rule_packs: []\n",
            existing_agents_text=self.COMPOSED_AGENTS,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(agents_bytes, upstream_bytes)

    def test_uncomposed_agents_md_is_still_replaced(self):
        # The mirror. Only a composed artifact is protected; an ordinary file
        # must still be refreshed from upstream, or the fallback path would
        # never run again once any AGENTS.md existed.
        result, ledger, _, agents_bytes, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs:\n- name: agent-style\n",
            existing_agents_text="# hand-written, no pack blocks\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn(b"rule-pack composition skipped", agents_bytes)
        compose = self._phase(ledger, "compose")
        self.assertNotIn("preserved", compose["reason"])
        self.assertIs(ledger["completed"], False)

    def test_skip_marker_alone_does_not_count_as_composed(self):
        # A file carrying only the skipped-composition marker is the degraded
        # artifact, not a composed one. Protecting it would freeze a consumer
        # on the un-composed copy for good.
        marker = "<!-- rule-pack composition skipped: no Python 3 interpreter found; run anywhere-agents to compose -->\n"
        result, ledger, _, agents_bytes, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            config_text="rule_packs:\n- name: agent-style\n",
            existing_agents_text=marker + "stale upstream body\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn(b"stale upstream body", agents_bytes)
        compose = self._phase(ledger, "compose")
        self.assertNotIn("preserved", compose["reason"])

    def test_skip_reasons_distinguish_missing_yaml_and_missing_composer(self):
        yaml_result, yaml_ledger, yaml_artifacts = _run_full_bootstrap_with_ledger(
            self.entrypoint,
            yaml_available=False,
        )
        self.assertEqual(yaml_result.returncode, 0, msg=yaml_result.stderr)
        self.assertEqual(
            self._phase(yaml_ledger, "compose")["reason"],
            "Python 3 interpreter has no PyYAML after install attempt",
        )
        yaml_marker = b"<!-- rule-pack composition skipped: Python 3 interpreter has no PyYAML after install attempt; run anywhere-agents to compose -->"
        for relative in ("AGENTS.md", "CLAUDE.md", "agents/codex.md"):
            self.assertIn(yaml_marker, yaml_artifacts[relative])
        composer_result, composer_ledger, _ = _run_full_bootstrap_with_ledger(
            self.entrypoint,
            composer_rc=None,
        )
        self.assertEqual(composer_result.returncode, 0, msg=composer_result.stderr)
        self.assertEqual(
            self._phase(composer_ledger, "compose")["reason"],
            "no composer script in sparse clone",
        )

    def test_the_todo_dropbox_is_seeded_when_absent(self):
        # The convention spread by hand-copying, so it reached neither a new
        # repo nor two of the existing ones. Bootstrap seeds it instead.
        captured = {".gitignore": None, "todo/README.md": None}
        result, _, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, capture_into=captured,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNotNone(captured["todo/README.md"], "todo/README.md not created")
        self.assertEqual(
            captured["todo/README.md"].decode("utf-8"), TODO_README_BODY)
        lines = captured[".gitignore"].decode("utf-8").splitlines()
        self.assertIn("todo/*", lines)
        self.assertIn("!todo/README.md", lines)

    def test_the_negation_lands_after_the_exclusion(self):
        # Order is the whole mechanism. A negation above its exclusion is
        # overridden by it, the README stops being tracked, and the folder
        # disappears from fresh clones, which is what the directory form of the
        # pattern does on its own.
        captured = {".gitignore": None}
        result, _, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, capture_into=captured,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        lines = captured[".gitignore"].decode("utf-8").splitlines()
        self.assertLess(lines.index("todo/*"), lines.index("!todo/README.md"))

    def test_an_existing_todo_readme_is_never_rewritten(self):
        # One consumer carries a README written around its own filing rules.
        # Replacing it with the upstream copy is the failure this release
        # series exists to remove, in a smaller file.
        local = "# todo/ - the inbox\n\nrules specific to this repo\n"
        captured = {"todo/README.md": None}
        result, _, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, capture_into=captured,
            existing_todo_readme=local,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(captured["todo/README.md"].decode("utf-8"), local)

    def test_an_absent_upstream_readme_creates_nothing(self):
        # A clone without the file must not leave an empty folder behind: an
        # empty todo/ with no README is the shape that vanishes on the next
        # clone, which is worse than not having one.
        #
        # The directory is asserted through capture_exists_into rather than
        # through the absent README. An earlier version of this test checked
        # only the README bytes, which a mutation that moved the mkdir above
        # the source check survived in all three entry points: the capture
        # helper reports an absent file and a directory identically.
        captured = {".gitignore": None, "todo/README.md": None}
        exists = {"todo": True}
        result, ledger, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, capture_into=captured,
            capture_exists_into=exists, todo_readme=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNone(captured["todo/README.md"])
        self.assertFalse(exists["todo"], "bootstrap left an empty todo/ behind")
        self.assertNotIn("todo/*", captured[".gitignore"].decode("utf-8"))
        self.assertNotIn("todo/README.md",
                         self._phase(ledger, "finalize")["targets"])

    def test_a_lone_negation_is_repaired(self):
        # git applies the last matching rule, so a negation above its exclusion
        # does nothing. A .gitignore carrying only the negation lands in exactly
        # that state once the exclusion is appended below it, and the README
        # then stops being tracked and the folder leaves fresh clones.
        captured = {".gitignore": None}
        result, _, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, capture_into=captured,
            existing_gitignore="!todo/README.md\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        lines = captured[".gitignore"].decode("utf-8").splitlines()
        last_exclude = max(i for i, ln in enumerate(lines) if ln == "todo/*")
        last_negate = max(i for i, ln in enumerate(lines) if ln == "!todo/README.md")
        self.assertGreater(last_negate, last_exclude, lines)
        self.assertEqual(lines.count("todo/*"), 1, lines)
        self.assertEqual(lines.count("!todo/README.md"), 2, lines)

    def test_a_repaired_order_is_not_repaired_again(self):
        # The shape the previous test produces, fed back in. A run that keeps
        # appending a negation on every pass would grow .gitignore forever.
        captured = {".gitignore": None}
        result, _, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, capture_into=captured,
            existing_gitignore="!todo/README.md\ntodo/*\n!todo/README.md\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        lines = captured[".gitignore"].decode("utf-8").splitlines()
        self.assertEqual(lines.count("todo/*"), 1, lines)
        self.assertEqual(lines.count("!todo/README.md"), 2, lines)

    def test_a_plain_file_named_todo_is_left_alone(self):
        # Both entry points must agree here. `Test-Path 'todo'` is true for a
        # plain file while `[ -d todo ]` is false, and PowerShell used to record
        # ignore rules and a ledger target for a README that was never written.
        captured = {".gitignore": None, "todo": None}
        result, ledger, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, capture_into=captured,
            existing_todo_file="not a directory\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(captured["todo"], b"not a directory\n")
        gitignore = (captured[".gitignore"] or b"").decode("utf-8")
        self.assertNotIn("todo/*", gitignore)
        self.assertNotIn("!todo/README.md", gitignore)
        self.assertNotIn("todo/README.md",
                         self._phase(ledger, "finalize")["targets"])

    def test_a_linked_todo_is_never_written_through(self):
        # Containment. With todo linked elsewhere, todo/README.md resolves to a
        # path outside the repo, so asserting it does not exist is exactly the
        # assertion that no external file was created. A session start has no
        # business writing there, and git would not index it through the link
        # even if it did, so the ignore rules and the ledger target must stay
        # out too. Bash tests -L; PowerShell tests the ReparsePoint attribute.
        captured = {".gitignore": None}
        exists = {"todo": False, "todo/README.md": True}
        result, ledger, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, capture_into=captured,
            capture_exists_into=exists, linked_todo=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertFalse(exists["todo/README.md"],
                         "bootstrap wrote through the link, outside the repo")
        self.assertTrue(exists["todo"], "bootstrap removed the link")
        gitignore = (captured[".gitignore"] or b"").decode("utf-8")
        self.assertNotIn("todo/*", gitignore)
        self.assertNotIn("!todo/README.md", gitignore)
        self.assertNotIn("todo/README.md",
                         self._phase(ledger, "finalize")["targets"])

    def test_a_linked_todo_with_a_readme_claims_nothing(self):
        # The other link predicate. Here the README already exists behind the
        # link, so absence alone would skip the seed and the postcondition gate
        # is the only thing standing between this run and ignore rules plus a
        # ledger target naming a file that lives outside the repo.
        captured = {".gitignore": None, "todo/README.md": None}
        result, ledger, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, capture_into=captured,
            linked_todo=True, linked_todo_readme="filed elsewhere\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(captured["todo/README.md"], b"filed elsewhere\n")
        gitignore = (captured[".gitignore"] or b"").decode("utf-8")
        self.assertNotIn("todo/*", gitignore)
        self.assertNotIn("!todo/README.md", gitignore)
        self.assertNotIn("todo/README.md",
                         self._phase(ledger, "finalize")["targets"])

    def test_a_directory_named_readme_is_left_alone(self):
        # The other half of the leaf gate. -e and Test-Path are both true for a
        # directory at todo/README.md, so the seed is skipped; -f and
        # -PathType Leaf are both false, so nothing may be claimed for it.
        captured = {".gitignore": None, "todo/README.md": None}
        exists = {"todo/README.md": False}
        result, ledger, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, capture_into=captured,
            capture_exists_into=exists, existing_todo_readme_dir=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertTrue(exists["todo/README.md"])
        self.assertIsNone(captured["todo/README.md"],
                          "the directory was replaced by a file")
        gitignore = (captured[".gitignore"] or b"").decode("utf-8")
        self.assertNotIn("todo/*", gitignore)
        self.assertNotIn("!todo/README.md", gitignore)
        self.assertNotIn("todo/README.md",
                         self._phase(ledger, "finalize")["targets"])

    def test_the_dropbox_can_be_turned_off(self):
        captured = {"todo/README.md": None, ".gitignore": None}
        result, _, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, capture_into=captured,
            env_extra={"AGENT_CONFIG_NO_TODO_DROPBOX": "1"},
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNone(captured["todo/README.md"])
        self.assertNotIn("todo/*", captured[".gitignore"].decode("utf-8"))

    def test_the_dropbox_entries_are_not_duplicated(self):
        captured = {".gitignore": None}
        result, _, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint, shell=self.shell, capture_into=captured,
            existing_gitignore="todo/*\n!todo/README.md\n",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        lines = captured[".gitignore"].decode("utf-8").splitlines()
        self.assertEqual(lines.count("todo/*"), 1, lines)
        self.assertEqual(lines.count("!todo/README.md"), 1, lines)

    GENERATED = ("/AGENTS.md", "/CLAUDE.md", "/agents/codex.md")

    def _gitignore_after_run(self, **kwargs) -> str:
        captured = {".gitignore": None}
        result, _, _, _, _ = _run_no_python_bootstrap_with_ledger(
            self.entrypoint,
            shell=self.shell,
            capture_into=captured,
            **kwargs,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        raw = captured[".gitignore"]
        return "" if raw is None else raw.decode("utf-8")

    def test_untracked_generated_files_are_ignored(self):
        # These three are regenerated on every run and their bytes depend on
        # which packs the machine resolved, so two up-to-date machines each see
        # the other's copy as a diff to commit.
        text = self._gitignore_after_run()
        for entry in self.GENERATED:
            with self.subTest(entry=entry):
                self.assertEqual(text.splitlines().count(entry), 1, text)

    def test_a_tracked_generated_file_gets_no_entry(self):
        # .gitignore does not untrack anything already in the index, so an entry
        # for a tracked path is inert and reads as if it did something. Moving
        # such a file out of the index removes it from every other clone, which
        # is an operator's call.
        text = self._gitignore_after_run(tracked=("AGENTS.md",))
        self.assertNotIn("/AGENTS.md", text.splitlines())
        self.assertIn("/CLAUDE.md", text.splitlines())

    def test_track_generated_env_suppresses_every_entry(self):
        text = self._gitignore_after_run(
            env_extra={"AGENT_CONFIG_TRACK_GENERATED": "1"},
        )
        for entry in self.GENERATED:
            with self.subTest(entry=entry):
                self.assertNotIn(entry, text.splitlines())
        # The opt-out covers the generated files only.
        self.assertIn(".agent-config/", text)

    def test_an_existing_unanchored_entry_is_not_duplicated(self):
        # A consumer already ignoring `AGENTS.md` at any depth has the broader
        # rule; appending the narrower `/AGENTS.md` under it changes nothing.
        text = self._gitignore_after_run(existing_gitignore="AGENTS.md\n")
        self.assertNotIn("/AGENTS.md", text.splitlines())
        self.assertEqual(text.splitlines().count("AGENTS.md"), 1, text)

    def test_an_existing_anchored_entry_is_not_duplicated(self):
        text = self._gitignore_after_run(existing_gitignore="/CLAUDE.md\n")
        self.assertEqual(text.splitlines().count("/CLAUDE.md"), 1, text)

    def test_a_mixed_case_entry_does_not_satisfy_the_probe(self):
        # `Select-String` is case-insensitive by default and `grep -qE` is not,
        # so the two entry points disagreed here: PowerShell read `/AGENTS.MD`
        # as the rule already being present and Bash appended the lowercase one.
        # On a case-sensitive checkout `/AGENTS.MD` does not cover `AGENTS.md`,
        # so the generated file becomes visible and committable again.
        text = self._gitignore_after_run(existing_gitignore="/AGENTS.MD\n")
        self.assertIn("/AGENTS.md", text.splitlines(), text)
        self.assertIn("/AGENTS.MD", text.splitlines(), text)

    def test_a_gitignore_without_a_final_newline_is_not_corrupted(self):
        # `printf '%s\n' entry >> f` on a file whose last byte is not a newline
        # glues the entry onto the last rule, breaking that rule and leaving the
        # new one unmatchable.
        text = self._gitignore_after_run(existing_gitignore="node_modules/")
        lines = text.splitlines()
        self.assertEqual(lines[0], "node_modules/", text)
        self.assertIn(".agent-config/", lines)

    def test_helper_deployment_never_exposes_staged_partial_file(self):
        result, observed, final, expected, running = _run_atomic_helper_deployment(self.entrypoint)
        self.assertTrue(running, msg="copy pause was not observed while bootstrap was running")
        self.assertEqual(observed, b"#!/usr/bin/env bash\n# old helper\nexit 0\n")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(final, expected)


class BootstrapLedgerBashTests(_BootstrapLedgerContract, unittest.TestCase):
    entrypoint = "bash"
    emitted_by = "bootstrap.sh"

    def test_self_update_replaces_inode_without_skipping_running_tail(self):
        result, deployed, replacement, original, ledger = _run_bash_self_update()
        self.assertNotEqual(len(replacement), len(original))
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(deployed, replacement)
        self.assertEqual(ledger["last_phase"], "finalize")
        self.assertIs(ledger["completed"], True)


# One TestCase per installed PowerShell edition. Windows PowerShell 5.1 and
# PowerShell 7 differ on native command-line quoting, on ConvertTo-Json
# formatting, and on Set-Content BOM behaviour, so a suite that exercises
# one of them tests roughly half of what it appears to.
def _make_powershell_ledger_cases():
    made = {}
    for path in POWERSHELL_EDITIONS:
        stem = Path(path).stem.lower()
        name = "BootstrapLedgerPowerShellTests_" + stem
        made[name] = type(name, (_BootstrapLedgerContract, unittest.TestCase), {
            "entrypoint": "powershell",
            "emitted_by": "bootstrap.ps1",
            "shell": path,
            "__doc__": "Ledger contract driven by %s." % path,
        })
    return made


globals().update(_make_powershell_ledger_cases())


class PowerShellEditionCoverageTests(unittest.TestCase):
    def test_windows_powershell_is_exercised(self) -> None:
        editions = powershell_editions_or_fail(self)
        self.assertTrue(editions)
        if sys.platform.startswith("win"):
            majors = set()
            for path in editions:
                probe = subprocess.run(
                    [path, "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-Command", "$PSVersionTable.PSVersion.Major"],
                    capture_output=True, text=True,
                )
                self.assertEqual(probe.returncode, 0, msg=probe.stderr)
                majors.add(probe.stdout.strip())
            # Prove the edition rather than trusting the executable name.
            self.assertIn(
                "5", majors,
                "Windows PowerShell 5.1 is not among the exercised editions "
                "(saw majors %s); that is the edition anywhere-agents#34 "
                "reproduced under" % sorted(majors),
            )


class BootstrapLedgerParityTests(unittest.TestCase):

    EXPECTED_GENERATE = {
        "phase": "generate",
        "scope": "repo",
        "status": "skipped",
        "rc": None,
        "targets": [],
    }

    def test_no_python_generate_steps_match(self):
        bash_result, bash_ledger, bash_raw, _, _ = _run_no_python_bootstrap_with_ledger("bash")
        self.assertEqual(bash_result.returncode, 0, msg=bash_result.stderr)
        bash_generate = next(step for step in bash_ledger["steps"] if step["phase"] == "generate")
        self.assertEqual(bash_generate, self.EXPECTED_GENERATE)
        self.assertEqual(json.loads(bash_raw), bash_ledger)
        self.assertIn("ANYWHERE_AGENTS_PYTHON did not execute Python 3 successfully", bash_result.stderr)

        # Compare bash against every installed edition. The two editions differ
        # on native command-line quoting, so agreement with one of them says
        # nothing about the other.
        for shell in powershell_editions_or_fail(self):
            with self.subTest(edition=Path(shell).stem):
                ps_result, ps_ledger, ps_raw, _, _ = _run_no_python_bootstrap_with_ledger(
                    "powershell", shell=shell
                )
                self.assertEqual(ps_result.returncode, 0, msg=ps_result.stderr)
                ps_generate = next(
                    step for step in ps_ledger["steps"] if step["phase"] == "generate"
                )
                self.assertEqual(ps_generate, self.EXPECTED_GENERATE)
                self.assertIs(type(bash_generate["rc"]), type(ps_generate["rc"]))
                self.assertEqual(json.loads(ps_raw), ps_ledger)
                self.assertIn(
                    "ANYWHERE_AGENTS_PYTHON did not execute Python 3 successfully",
                    ps_result.stderr,
                )


class RulePacksConfigStateTests(unittest.TestCase):
    """Both no-Python pre-parsers, driven directly across all three answers.

    The end-to-end ledger tests cannot isolate this. Measured: a zero-indent
    selection, a renamed key, and a file with no `rule_packs` key at all all
    produce the same ledger, because the predicate defaults to configured when
    both layers report `none`. So a test that only reads the ledger passes for
    three different reasons and stays green on a future degradation to
    "no signal".

    The fixtures are written by `yaml.safe_dump`, the same call
    `anywhere-agents` uses to write `agent-config.yaml`. Hand-written YAML is
    what hid the defect: every existing fixture indented its list, the product
    emits a block sequence at the key's own indentation, and the parser
    required the indentation. A fixture should come from the product's writer
    rather than from someone's memory of its output.
    """

    BOOTSTRAP_SH = ROOT / "bootstrap" / "bootstrap.sh"
    BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("PyYAML is not importable")

    @staticmethod
    def _safe_dump(data) -> str:
        import yaml
        return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)

    def _cases(self) -> list[tuple[str, str, str]]:
        selection = {"rule_packs": [{"name": "agent-style",
                                     "source": {"url": "https://example.invalid"}}]}
        return [
            # label, file content, expected state
            ("safe_dump selection", self._safe_dump(selection), "nonempty"),
            ("indented selection", "rule_packs:\n  - name: agent-style\n", "nonempty"),
            ("flow selection", "rule_packs: [agent-style]\n", "nonempty"),
            ("explicit empty list", self._safe_dump({"rule_packs": []}), "empty"),
            ("bare key", "rule_packs:\n", "empty"),
            ("null value", "rule_packs: null\n", "empty"),
            ("no key at all", "other: 1\n", "none"),
            # `packs:` is canonical and `rule_packs:` the deprecated alias. A
            # parser that knew only the alias answered `none` for a consumer on
            # the canonical key, which is the answer that deletes composed pack
            # blocks and freezes opted-out ones.
            ("canonical selection",
             self._safe_dump({"packs": [{"name": "agent-style"}]}), "nonempty"),
            ("canonical empty list", self._safe_dump({"packs": []}), "empty"),
            ("canonical bare key", "packs:\n", "empty"),
            # Within one file the canonical key wins, both ways round.
            ("canonical beats alias when it selects",
             "rule_packs: []\npacks:\n- name: agent-style\n", "nonempty"),
            ("canonical beats alias when it clears",
             "rule_packs:\n- name: agent-style\npacks: []\n", "empty"),
            ("unrelated key only", self._safe_dump({"other": [{"name": "x"}]}), "none"),
            # A flow sequence indented under its own key is valid YAML that
            # resolves to a nonempty list. Both scanners skipped it, reached
            # the end of the file, and answered `empty`, which is the explicit
            # opt-out: measured end to end, all three entry points replaced a
            # composed AGENTS.md with the bare upstream copy and recorded
            # `completed: true`.
            ("indented flow sequence", "packs:\n  [agent-style]\n", "nonempty"),
            ("indented empty flow sequence", "packs:\n  []\n", "empty"),
            # The other two spellings of an empty value. The key line accepted
            # them from the start; the conservative indented-node rule read
            # them as a selection and quietly disabled the opt-out.
            ("indented null", "packs:\n  null\n", "empty"),
            ("indented tilde", "packs:\n  ~\n", "empty"),
            ("indented NULL", "packs:\n  NULL\n", "empty"),
            # Spellings YAML gives the key itself. Matching only the bare one
            # answered `none` for all of these, which after a clear in an
            # earlier layer deleted the block the later layer had asked for.
            # Using file length as a stand-in for "unreadable" then overrode
            # genuine opt-outs, so the spellings are read instead.
            ("quoted key", '"packs": [agent-style]\n', "nonempty"),
            ("quoted empty", '"packs": []\n', "empty"),
            ("single-quoted key", "'packs': [agent-style]\n", "nonempty"),
            ("space before the colon", "packs : [agent-style]\n", "nonempty"),
            ("space before the colon, empty", "packs : []\n", "empty"),
            ("quoted and spaced", '"packs" : [agent-style]\n', "nonempty"),
            ("quoted key with a block sequence",
             '"packs":\n- name: agent-style\n', "nonempty"),
            # A nested key of the same name is a different key.
            ("nested key of the same name",
             "other:\n  packs: [agent-style]\n", "none"),
            # Case still matters, in both entry points.
            ("quoted but wrong case", '"Packs": [agent-style]\n', "none"),
            # Not a list at all. The scanner cannot tell what the resolver
            # would make of it, and an uncertain answer preserves.
            ("indented mapping", "packs:\n  name: agent-style\n", "nonempty"),
            # The opt-out still has to survive a following key.
            ("empty list then another key",
             "packs: []\nother:\n  - x\n", "empty"),
        ]

    def _bash_state(self, path: Path, work: Path) -> str:
        if not BASH:
            raise unittest.SkipTest("bash not available")
        text = self.BOOTSTRAP_SH.read_text(encoding="utf-8")
        start = text.index("_rule_packs_config_state() {")
        # Through the end of the single-key scanner it delegates to, not just
        # the first closing brace: slicing one function left the driver calling
        # a helper that was not in it.
        end = text.index("\n}\n", text.index("_rule_packs_key_state() {")) + len("\n}\n")
        driver = work / "state.sh"
        _write_text_lf(
            driver,
            text[start:end] + '\n_rule_packs_config_state "$1"\nprintf "\\n"\n',
        )
        # An EMPTY PATH, deliberately. The function used to shell out to `tr`,
        # and the Git for Windows bash this resolves to does not put its own
        # tool directory on PATH when invoked from Windows: the probe then
        # reported `empty` for a configured consumer, the same wrong answer the
        # indentation defect gave. The first version of this test prepended
        # `Path(BASH).parent`, which is the directory holding `tr.exe`, so it
        # handed the parser the very utility whose absence was the bug.
        # Reverting the fix left the test green. `BASH` is invoked by absolute
        # path and the corrected parser uses only shell builtins, so an empty
        # PATH runs the intended code and fails the moment anything external
        # creeps back in.
        env = os.environ.copy()
        empty_path = work / "no-tools"
        empty_path.mkdir(exist_ok=True)
        env["PATH"] = str(empty_path)
        result = subprocess.run(
            [BASH, str(driver), str(path).replace("\\", "/")],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn(
            "command not found", result.stderr,
            "a missing shell utility makes this parser answer `empty`, which is "
            "the answer that suppresses the skip marker: " + result.stderr.strip(),
        )
        return result.stdout.strip()

    @staticmethod
    def _powershell_function(source: str, name: str) -> str:
        """Slice one function out of the script by matching its braces.

        Comments and quoted strings are skipped rather than counted. A comment
        explaining that a target holding an opening brace produced no object
        put two of them in the text, and the counter then ran off the end of
        the file and every test extracting that function errored. Writing about
        the character must not break the tool that looks for it.
        """
        return RulePacksConfigStateTests._powershell_block(source, "function " + name, name)

    @staticmethod
    def _powershell_block(source: str, marker: str, label: str) -> str:
        """Slice from a marker through the block its first brace opens.

        Used for the inline `~/.claude.json` heal as well as for functions, so
        a test can run the shipped statements rather than a paraphrase of them.
        """
        start = source.index(marker)
        depth = 0
        index = start
        while index < len(source):
            char = source[index]
            if char == "#":
                newline = source.find("\n", index)
                index = len(source) if newline < 0 else newline
            elif char == "'":
                closing = source.find("'", index + 1)
                index = len(source) if closing < 0 else closing
            elif char == '"':
                index += 1
                while index < len(source) and source[index] != '"':
                    # A backtick escapes the next character, including a quote.
                    index += 2 if source[index] == "`" else 1
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return source[start:index + 1]
            index += 1
        raise AssertionError(f"unbalanced braces reading {label}")

    def _powershell_state(self, shell: str, path: Path, work: Path) -> str:
        source = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        # Both functions: the entry point delegates to the single-key scanner.
        body = "\n".join(
            self._powershell_function(source, name)
            for name in ("Get-RulePacksConfigState", "Get-RulePacksKeyState",
                         "Get-RulePacksKeyTail")
        )
        driver = work / "state.ps1"
        driver.write_text(body + "\nGet-RulePacksConfigState $args[0]\n", encoding="utf-8")
        result = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver), str(path)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return result.stdout.strip()

    def test_both_parsers_agree_on_every_state(self) -> None:
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            for label, content, expected in self._cases():
                path = work / "agent-config.yaml"
                _write_text_lf(path, content)
                with self.subTest(case=label, entrypoint="bash"):
                    self.assertEqual(self._bash_state(path, work), expected, content)
                for shell in shells:
                    with self.subTest(case=label, entrypoint=Path(shell).stem):
                        self.assertEqual(
                            self._powershell_state(shell, path, work), expected, content
                        )

    def test_an_empty_path_answers_none_without_complaining(self) -> None:
        # Get-UserConfigPath returns an empty string when no user-config home
        # resolves, and that value reaches this function. `Test-Path
        # -LiteralPath ''` answers $false on PowerShell 7. On Windows
        # PowerShell 5.1 it fails to bind, writes a non-terminating error, and
        # produces no result, and the surrounding `if (-not ...)` then takes the
        # branch for a path that exists. Measured with the guard removed from a
        # standalone copy: `answer=none` under 7, and under 5.1 a
        # ParameterBindingValidationException followed by `answer=exists`.
        #
        # Both scanners carry the guard, so removing one leaves the other to
        # answer `none` and the returned value alone cannot see the difference.
        # The empty stderr assertion is what makes each of them load-bearing.
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        source = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        body = "\n".join(
            self._powershell_function(source, name)
            for name in ("Get-RulePacksConfigState", "Get-RulePacksKeyState",
                         "Get-RulePacksKeyTail")
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            driver = work / "emptypath.ps1"
            driver.write_text(body + "\nGet-RulePacksConfigState ''\n", encoding="utf-8")
            for shell in shells:
                with self.subTest(entrypoint=Path(shell).stem):
                    result = subprocess.run(
                        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 0, msg=result.stderr)
                    self.assertEqual(result.stdout.strip(), "none", msg=result.stderr)
                    self.assertEqual(
                        "", result.stderr.strip(),
                        "an empty path reached a cmdlet that rejects it",
                    )
            # The bash half has no equivalent trap, `[ -f "" ]` being false, but
            # the two must still answer the same thing.
            if BASH:
                text = self.BOOTSTRAP_SH.read_text(encoding="utf-8")
                start = text.index("_rule_packs_config_state() {")
                end = text.index("\n}\n", text.index("_rule_packs_key_state() {")) + len("\n}\n")
                sh_driver = work / "emptypath.sh"
                _write_text_lf(
                    sh_driver,
                    text[start:end] + '\n_rule_packs_config_state ""\nprintf "\\n"\n',
                )
                result = subprocess.run(
                    [BASH, str(sh_driver)], capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                self.assertEqual(result.stdout.strip(), "none", msg=result.stderr)

    def test_key_match_is_case_sensitive_in_both(self) -> None:
        # PowerShell's -match is case-insensitive by default, so `Rule_Packs:`
        # read as `empty` there and `none` in bash. The PowerShell answer routes
        # to the silent bare-copy path, which is the outcome the marker exists
        # to prevent.
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            path = work / "agent-config.yaml"
            _write_text_lf(path, "Rule_Packs: []\n")
            self.assertEqual(self._bash_state(path, work), "none")
            for shell in shells:
                with self.subTest(entrypoint=Path(shell).stem):
                    self.assertEqual(self._powershell_state(shell, path, work), "none")


class ComposedArtifactPredicateTests(unittest.TestCase):
    """Both composed-artifact predicates, driven directly over marker shapes.

    The end-to-end preservation tests use one marker, so they cannot tell a
    predicate that reads the marker from one that matches any line containing
    `rule-pack:`. Both failure directions destroy something. Rejecting an
    authentic marker deletes the pack blocks the preservation exists to save;
    accepting a fake freezes a consumer on an un-composed file that no later
    run will refresh.

    The first version keyed on `<!-- rule-pack:[^ :]*:begin`. A pack name is
    free text in the manifest, so a name with a space or a colon produced an
    authentic artifact the predicate called plain. The same pattern accepted
    `:begin-fake` and a `:begin` with no digest at all.
    """

    BOOTSTRAP_SH = ROOT / "bootstrap" / "bootstrap.sh"
    BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"

    @staticmethod
    def _marker(name: str = "agent-style", version: str = "v0.4.2",
                digest: str = EMPTY_SHA256) -> str:
        return "<!-- rule-pack:%s:begin version=%s sha256=%s -->" % (name, version, digest)

    def _cases(self) -> list[tuple[str, str, bool]]:
        body = "\npack body\n<!-- rule-pack:agent-style:end -->\n"
        return [
            # label, AGENTS.md content, expected "is composed"
            ("plain marker", "# heading\n" + self._marker() + body, True),
            ("marker on the first line", self._marker() + body, True),
            ("pack name with a space", self._marker(name="my pack") + body, True),
            ("pack name with a colon", self._marker(name="org:pack") + body, True),
            ("uppercase digest", self._marker(digest=EMPTY_SHA256.upper()) + body, True),
            ("version with a slash", self._marker(version="refs/tags/v1.2") + body, True),
            # The v2 schema accepts any nonempty source.ref and the composer
            # formats it into the marker unchanged. A narrow character class
            # copied from the legacy raw-URL validator called this authentic
            # marker plain, which deletes the pack block it protects.
            ("version with SemVer build metadata",
             self._marker(version="v1.2.3+build.7") + body, True),
            ("version with a tilde", self._marker(version="v1.2~rc1") + body, True),
            ("CRLF line endings", ("# heading\n" + self._marker() + body).replace("\n", "\r\n"), True),
            ("trailing blanks after the marker",
             self._marker() + "  \t" + body, True),
            # Rejections.
            ("begin-fake suffix",
             self._marker().replace(":begin ", ":begin-fake ") + body, False),
            ("begin with no digest", "<!-- rule-pack:agent-style:begin -->" + body, False),
            ("short digest", self._marker(digest="abc123") + body, False),
            ("63-digit digest", self._marker(digest=EMPTY_SHA256[:-1]) + body, False),
            ("65-digit digest", self._marker(digest=EMPTY_SHA256 + "a") + body, False),
            ("non-hex digest", self._marker(digest="z" * 64) + body, False),
            ("marker indented", "  " + self._marker() + body, False),
            ("marker mid-line", "text " + self._marker() + body, False),
            ("end marker only", "# heading\n<!-- rule-pack:agent-style:end -->\n", False),
            ("skipped-composition marker",
             "<!-- rule-pack composition skipped: no Python 3 interpreter found; "
             "run anywhere-agents to compose -->\nbody\n", False),
            ("no marker at all", "# hand-written AGENTS.md\n", False),
            ("empty file", "", False),
        ]

    def _bash_verdict(self, work: Path) -> bool:
        if not BASH:
            raise unittest.SkipTest("bash not available")
        text = self.BOOTSTRAP_SH.read_text(encoding="utf-8")
        start = text.index("_agents_md_is_composed() {")
        end = text.index("\n}\n", start) + len("\n}\n")
        driver = work / "composed.sh"
        _write_text_lf(
            driver,
            text[start:end]
            + '\nif _agents_md_is_composed; then printf composed; else printf plain; fi\n',
        )
        env = os.environ.copy()
        env["PATH"] = str(Path(BASH).parent) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(
            [BASH, "composed.sh"],
            capture_output=True, text=True, cwd=str(work), env=env,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn("command not found", result.stderr, result.stderr.strip())
        return result.stdout.strip() == "composed"

    def _powershell_verdict(self, shell: str, work: Path) -> bool:
        source = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        start = source.index("function Test-AgentsMdIsComposed")
        depth = 0
        for index in range(start, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    body = source[start:index + 1]
                    break
        driver = work / "composed.ps1"
        driver.write_text(
            body + "\nif (Test-AgentsMdIsComposed) { 'composed' } else { 'plain' }\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "composed.ps1"],
            capture_output=True, text=True, cwd=str(work),
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return result.stdout.strip() == "composed"

    def test_both_predicates_agree_on_every_marker_shape(self) -> None:
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            agents = work / "AGENTS.md"
            for label, content, expected in self._cases():
                agents.write_bytes(content.encode("utf-8"))
                with self.subTest(case=label, entrypoint="bash"):
                    self.assertEqual(self._bash_verdict(work), expected, content)
                for shell in shells:
                    with self.subTest(case=label, entrypoint=Path(shell).stem):
                        self.assertEqual(
                            self._powershell_verdict(shell, work), expected, content
                        )

    def test_a_missing_agents_md_is_not_composed(self) -> None:
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            self.assertFalse(self._bash_verdict(work))
            for shell in shells:
                with self.subTest(entrypoint=Path(shell).stem):
                    self.assertFalse(self._powershell_verdict(shell, work))


class QuietSpawnContractTests(unittest.TestCase):
    """Both halves of the anywhere-agents#38 fix, asserted rather than assumed.

    The suite imported `_quiet_spawn` and used `.ps1` stubs and passed, but
    nothing failed if either half were undone. Returning kwargs unchanged from
    `_with_flag`, making `install()` a no-op, or restoring the `.cmd` stubs all
    left every functional assertion green while restoring the visual failure.

    These are structural rather than behavioural on purpose. Window creation is
    not observable from inside the child, and three earlier attempts to measure
    it from outside each ended up measuring a process shape the real failure
    never takes. Asserting the two decisions the fix actually makes is honest
    about what a test can see here.
    """

    WINDOWS_ONLY = "the flag and the stub split are Windows-only"

    def setUp(self) -> None:
        if not sys.platform.startswith("win"):
            self.skipTest(self.WINDOWS_ONLY)
        sys.path.insert(0, str(ROOT / "tests"))
        self.addCleanup(lambda: sys.path.remove(str(ROOT / "tests")))

    def test_with_flag_adds_create_no_window(self) -> None:
        import _quiet_spawn

        self.assertEqual(
            _quiet_spawn._with_flag({})["creationflags"],
            _quiet_spawn.CREATE_NO_WINDOW,
        )

    def test_with_flag_preserves_a_caller_flag(self) -> None:
        # Or-ing rather than assigning. A caller that already asked for
        # something must not lose it to this default.
        import _quiet_spawn

        detached = 0x00000008
        merged = _quiet_spawn._with_flag({"creationflags": detached})["creationflags"]
        self.assertEqual(merged, detached | _quiet_spawn.CREATE_NO_WINDOW)

    def test_install_patches_and_is_idempotent(self) -> None:
        import _quiet_spawn

        self.assertTrue(getattr(subprocess, "_agent_config_quiet_spawn_installed", False))
        self.assertIs(subprocess.Popen.__module__ == "_quiet_spawn", True)
        self.assertFalse(_quiet_spawn.install(), "a second install must be a no-op")

    def test_powershell_stub_dir_holds_ps1_and_nothing_cmd_can_run(self) -> None:
        # PowerShell runs a .ps1 in-process, so no process is created and no
        # console can be allocated. A .cmd goes through cmd.exe, which is what
        # owned the visible windows. The extensionless shell stub must stay out
        # of this directory: PowerShell prefers it over the .ps1, cannot run it,
        # and falls back to ShellExecute, which raises a file-association dialog
        # that blocks the run.
        with tempfile.TemporaryDirectory() as tmpdir:
            stub_dir = Path(tmpdir) / "stub_path"
            _make_stub_git(stub_dir, "git version 2.50.0")
            ps_dir = powershell_stub_dir(stub_dir)
            self.assertTrue((ps_dir / "git.ps1").is_file())
            for forbidden in ("git.cmd", "git.bat", "git"):
                with self.subTest(name=forbidden):
                    self.assertFalse(
                        (ps_dir / forbidden).exists(),
                        f"{forbidden} in the PowerShell stub directory reintroduces "
                        f"either a cmd.exe spawn or the ShellExecute dialog",
                    )
            # The bash side keeps its extensionless script, one level up.
            self.assertTrue((stub_dir / "git").is_file())

    def test_no_fixture_writes_a_cmd_stub(self) -> None:
        # A static scan, because the stub writers are spread across three
        # fixtures and a fourth added later would not be covered by the
        # directory assertion above.
        # Matches the path-join form the stub writers use rather than any
        # quoted string. Two earlier versions of this scan flagged themselves:
        # first the forbidden-name list in the test above, then an example in
        # this comment. Nothing here may spell the shape it looks for.
        source = Path(__file__).read_text(encoding="utf-8")
        offenders = re.findall(r'/\s*"[A-Za-z0-9_]+\.(?:cmd|bat)"', source)
        self.assertEqual(
            [], offenders,
            "a .cmd or .bat stub is run through cmd.exe, which allocates a "
            "console window the Python-side creation flags cannot reach",
        )


class UserConfigLayerPathTests(unittest.TestCase):
    """The user-level layer must land on the file the resolver actually reads.

    `config.user_config_home` branches on the platform rather than cascading:
    Windows reads `%APPDATA%` and stops, POSIX reads `$XDG_CONFIG_HOME` then
    `$HOME/.config` and never looks at `%APPDATA%`. The first version of these
    pre-parsers walked all three in one order for every platform. That agreed
    whenever `%APPDATA%` was set on Windows, which is the ordinary case, and
    disagreed in six of fourteen environment shapes otherwise. In one of them it
    pointed the layer at a POSIX-shaped file on Windows, where an explicit
    `packs: []` would clear a selection the resolver never sees and take a
    composed `AGENTS.md` with it.

    `$OSTYPE` is set by bash itself and cannot be overridden from the parent
    environment, and `$IsWindows` is fixed at build time, so each platform can
    exercise only its own branch. The table is keyed on the running platform,
    and the differential against the resolver runs wherever
    `scripts/packs/config.py` is present, which is `anywhere-agents` and not
    here.
    """

    BOOTSTRAP_SH = ROOT / "bootstrap" / "bootstrap.sh"
    BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"

    # label -> the three variables, empty meaning unset. Both entry points read
    # an empty value as absent, so one shape covers both.
    ENVIRONMENTS = [
        ("all three set", {"APPDATA": "/A", "XDG_CONFIG_HOME": "/X", "HOME": "/H"}),
        ("appdata only", {"APPDATA": "/A", "XDG_CONFIG_HOME": "", "HOME": ""}),
        ("xdg only", {"APPDATA": "", "XDG_CONFIG_HOME": "/X", "HOME": ""}),
        ("home only", {"APPDATA": "", "XDG_CONFIG_HOME": "", "HOME": "/H"}),
        ("xdg and home", {"APPDATA": "", "XDG_CONFIG_HOME": "/X", "HOME": "/H"}),
        ("appdata and home", {"APPDATA": "/A", "XDG_CONFIG_HOME": "", "HOME": "/H"}),
        ("none set", {"APPDATA": "", "XDG_CONFIG_HOME": "", "HOME": ""}),
    ]

    WINDOWS_EXPECTED = {
        "all three set": "/A/anywhere-agents/config.yaml",
        "appdata only": "/A/anywhere-agents/config.yaml",
        "xdg only": "",
        "home only": "",
        "xdg and home": "",
        "appdata and home": "/A/anywhere-agents/config.yaml",
        "none set": "",
    }

    POSIX_EXPECTED = {
        "all three set": "/X/anywhere-agents/config.yaml",
        "appdata only": "",
        "xdg only": "/X/anywhere-agents/config.yaml",
        "home only": "/H/.config/anywhere-agents/config.yaml",
        "xdg and home": "/X/anywhere-agents/config.yaml",
        "appdata and home": "/H/.config/anywhere-agents/config.yaml",
        "none set": "",
    }

    @property
    def expected(self) -> dict[str, str]:
        return (
            self.WINDOWS_EXPECTED
            if sys.platform.startswith("win")
            else self.POSIX_EXPECTED
        )

    @staticmethod
    def _normalize(path: str) -> str:
        return path.strip().replace("\\", "/")

    def _bash_path(self, environment: dict, work: Path) -> str:
        if not BASH:
            raise unittest.SkipTest("bash not available")
        text = self.BOOTSTRAP_SH.read_text(encoding="utf-8")
        start = text.index("_user_config_path() {")
        end = text.index("\n}\n", start) + len("\n}\n")
        assignments = "".join(
            f'export {name}="{value}"\n' for name, value in sorted(environment.items())
        )
        driver = work / "userpath.sh"
        _write_text_lf(
            driver,
            text[start:end] + "\n" + assignments + '_user_config_path\nprintf "\\n"\n',
        )
        # An empty PATH, as in RulePacksConfigStateTests: the branch reads
        # $OSTYPE rather than shelling out, and this fails if that regresses.
        env = os.environ.copy()
        empty_path = work / "no-tools"
        empty_path.mkdir(exist_ok=True)
        env["PATH"] = str(empty_path)
        result = subprocess.run(
            [BASH, str(driver)], capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertNotIn("command not found", result.stderr, result.stderr)
        return self._normalize(result.stdout)

    def _powershell_path(self, shell: str, environment: dict, work: Path) -> str:
        source = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        body = RulePacksConfigStateTests._powershell_function(source, "Get-UserConfigPath")
        assignments = "".join(
            f"$env:{name} = '{value}'\n" for name, value in sorted(environment.items())
        )
        driver = work / "userpath.ps1"
        driver.write_text(body + "\n" + assignments + "Get-UserConfigPath\n", encoding="utf-8")
        result = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return self._normalize(result.stdout)

    def test_both_entry_points_pick_the_platform_branch(self) -> None:
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            for label, environment in self.ENVIRONMENTS:
                expected = self.expected[label]
                with self.subTest(case=label, entrypoint="bash"):
                    self.assertEqual(self._bash_path(environment, work), expected)
                for shell in shells:
                    with self.subTest(case=label, entrypoint=Path(shell).stem):
                        self.assertEqual(
                            self._powershell_path(shell, environment, work), expected
                        )

    def test_the_table_matches_the_resolver(self) -> None:
        # The differential the table stands in for. Only anywhere-agents ships
        # the resolver, so this skips here and carries the contract there.
        if not (ROOT / "scripts" / "packs" / "config.py").is_file():
            self.skipTest("scripts/packs/config.py is not in this repo")
        sys.path.insert(0, str(ROOT))
        self.addCleanup(lambda: sys.path.remove(str(ROOT)))
        from scripts.packs import config as resolver  # noqa: PLC0415

        for label, environment in self.ENVIRONMENTS:
            with self.subTest(case=label):
                live = {k: v for k, v in environment.items() if v}
                answer = resolver.user_config_path(live)
                self.assertEqual(
                    self._normalize(str(answer)) if answer is not None else "",
                    self.expected[label],
                )


class EnvOverlayGrammarTests(unittest.TestCase):
    """Where the overlay pre-parsers disagree with the resolver, and which way.

    The overlay is additive with a `-name` subtract form, so only a positive
    entry counts as a selection. `config.parse_env_var` splits on commas alone
    and strips each token; both pre-parsers split on whitespace as well, because
    trimming a token in shell without an external utility is what the `tr`
    defect was made of. The two therefore read `-a b` differently.

    The contract is not equality, it is direction. A pre-parser that reads an
    overlay as adding when the resolver would not preserves a composed
    `AGENTS.md` the operator may no longer want. The reverse loses one, which is
    the failure this release exists to stop, so it must never happen.
    """

    BOOTSTRAP_SH = ROOT / "bootstrap" / "bootstrap.sh"
    BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"

    VALUES = [
        "",
        "agent-style",
        "-agent-style",
        "a,b",
        "-a,-b",
        "a,-b",
        "-a,b",
        " , ",
        "   ",
        "a b",
        "-a b",
        "-a -b",
        # Values config.parse_env_var rejects outright. With Python present the
        # run fails and the artifact is untouched, so the pre-parser has to
        # answer "adds" for them or it deletes what the resolver refuses to
        # touch. The earlier twelve were all valid, which is why the direction
        # test could not see this.
        "-",
        "-bad/name",
        "-bad:name",
        "-bad@ref",
        "bad/name",
    ]

    # The shapes where whitespace splitting makes the pre-parsers see an
    # addition the resolver does not. Listed rather than derived, so a new
    # disagreement shows up as a failure instead of being absorbed.
    KNOWN_WHITESPACE_DISAGREEMENTS = {"-a b"}

    def _bash_adds(self, value: str, work: Path) -> bool:
        if not BASH:
            raise unittest.SkipTest("bash not available")
        text = self.BOOTSTRAP_SH.read_text(encoding="utf-8")
        start = text.index("_env_pack_selection_adds() {")
        end = text.index("\n}\n", start) + len("\n}\n")
        driver = work / "overlay.sh"
        _write_text_lf(
            driver,
            text[start:end]
            + '\nif _env_pack_selection_adds; then printf "yes\\n"; else printf "no\\n"; fi\n',
        )
        env = os.environ.copy()
        empty_path = work / "no-tools"
        empty_path.mkdir(exist_ok=True)
        env["PATH"] = str(empty_path)
        env["AGENT_CONFIG_PACKS"] = value
        env.pop("AGENT_CONFIG_RULE_PACKS", None)
        result = subprocess.run(
            [BASH, str(driver)], capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return result.stdout.strip() == "yes"

    def _powershell_adds(self, shell: str, value: str, work: Path) -> bool:
        source = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        body = RulePacksConfigStateTests._powershell_function(source, "Test-EnvPackSelectionAdds")
        driver = work / "overlay.ps1"
        driver.write_text(
            body + "\nif (Test-EnvPackSelectionAdds) { 'yes' } else { 'no' }\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["AGENT_CONFIG_PACKS"] = value
        env.pop("AGENT_CONFIG_RULE_PACKS", None)
        result = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return result.stdout.strip() == "yes"

    def _resolver_adds(self, value: str) -> bool:
        """Whether the resolver would leave a composed artifact in place.

        A rejected value is not "no selection". The composer stops, nothing is
        rewritten, and the artifact survives, so the pre-parser has to answer
        the same way. Letting ConfigError escape here was what let an invalid
        overlay delete an artifact while the test reported agreement.
        """
        from scripts.packs import config as resolver  # noqa: PLC0415

        try:
            add, _subtract = resolver.parse_env_var({"AGENT_CONFIG_PACKS": value})
        except resolver.ConfigError:
            return True
        return bool(add)

    def test_both_entry_points_agree_with_each_other(self) -> None:
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            for value in self.VALUES:
                answer = self._bash_adds(value, work)
                for shell in shells:
                    with self.subTest(value=value, entrypoint=Path(shell).stem):
                        self.assertEqual(self._powershell_adds(shell, value, work), answer)

    def test_every_character_python_strips_lands_on_the_preserving_side(self) -> None:
        """The generated half of the case above.

        `config.parse_env_var` strips each token with Python's `str.strip()`
        before validating, so `-\\r` arrives there as a bare `-` and is
        rejected while reaching the pre-parsers intact. A fixed list of
        separators loses that race every time one is missed: bash does not
        split on `\\r` at all, and the two PowerShell editions disagree about
        whether `\\s` covers U+001C. This walks the whole set rather than the
        four names that were found by hand.
        """
        if not (ROOT / "scripts" / "packs" / "config.py").is_file():
            self.skipTest("scripts/packs/config.py is not in this repo")
        sys.path.insert(0, str(ROOT))
        self.addCleanup(lambda: sys.path.remove(str(ROOT)))
        stripped = [chr(code) for code in range(0x100) if chr(code).isspace()]
        self.assertIn("\r", stripped, "the sweep did not find the character that started this")
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            for char in stripped:
                value = "-" + char
                with self.subTest(codepoint=f"U+{ord(char):04X}"):
                    self.assertTrue(
                        self._resolver_adds(value),
                        "this sweep assumes the resolver rejects the value",
                    )
                    self.assertTrue(
                        self._bash_adds(value, work),
                        "the pre-parser reads a subtraction where the resolver "
                        "refuses the whole overlay, which deletes the artifact",
                    )
                    for shell in shells:
                        self.assertTrue(
                            self._powershell_adds(shell, value, work),
                            f"{Path(shell).stem} reads a subtraction where the "
                            f"resolver refuses the whole overlay",
                        )

    def test_no_disagreement_with_the_resolver_can_delete(self) -> None:
        if not (ROOT / "scripts" / "packs" / "config.py").is_file():
            self.skipTest("scripts/packs/config.py is not in this repo")
        sys.path.insert(0, str(ROOT))
        self.addCleanup(lambda: sys.path.remove(str(ROOT)))
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            for value in self.VALUES:
                shell_adds = self._bash_adds(value, work)
                resolver_adds = self._resolver_adds(value)
                with self.subTest(value=value):
                    if shell_adds == resolver_adds:
                        self.assertNotIn(value, self.KNOWN_WHITESPACE_DISAGREEMENTS)
                        continue
                    self.assertTrue(
                        shell_adds,
                        f"{value!r}: the pre-parser reads no addition where the "
                        f"resolver reads one, which clears a selection and "
                        f"deletes the composed AGENTS.md it asked for",
                    )
                    self.assertIn(
                        value, self.KNOWN_WHITESPACE_DISAGREEMENTS,
                        f"{value!r} is a new disagreement; it preserves rather "
                        f"than deletes, but it belongs in the recorded list",
                    )


class QuietSpawnImportPathTests(unittest.TestCase):
    """The sibling import must survive every way the suite is started.

    `unittest discover -s tests` puts tests/ on sys.path, so a bare
    `import _quiet_spawn` resolved and both full suites passed. The dotted form,
    `python -m unittest tests.<module>`, does not, and validate.yml runs the
    Sentinel redaction smoke that way against a module carrying the import. The
    green suites said nothing about it because they never used that form.

    Not Windows-gated. The import line is unconditional, so it fails on any
    platform; only what the module then does is Windows-specific.
    """

    GUARD = "sys.path.insert(0, str("

    def _modules_importing_quiet_spawn(self) -> list[Path]:
        return [
            path
            for path in sorted((ROOT / "tests").glob("test_*.py"))
            if re.search(r"^import _quiet_spawn\b", path.read_text(encoding="utf-8"), re.M)
        ]

    def test_every_import_site_carries_a_sys_path_guard(self) -> None:
        offenders = []
        for path in self._modules_importing_quiet_spawn():
            text = path.read_text(encoding="utf-8")
            before = text.split("import _quiet_spawn", 1)[0]
            if self.GUARD not in before:
                offenders.append(path.name)
        self.assertEqual(
            [], offenders,
            "these modules import the sibling helper without first putting "
            "tests/ on sys.path, so they raise ModuleNotFoundError under "
            "`python -m unittest tests.<module>`",
        )

    def test_every_module_imports_under_a_dotted_unittest_run(self) -> None:
        # The static check above cannot tell whether the guard actually works.
        # Run the invocation that broke, with a filter that matches no test, so
        # each module is imported and nothing else costs anything. Every module
        # rather than one: the first version checked only the first name in the
        # list, so removing the guard from any later one left it green while
        # the release note claimed otherwise.
        modules = self._modules_importing_quiet_spawn()
        self.assertTrue(modules, "no module imports the helper; this test is vacuous")
        targets = ["tests." + path.stem for path in modules]
        result = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "-k", "zzz_matches_no_test", *targets],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        combined = result.stdout + result.stderr
        self.assertNotIn(
            "ModuleNotFoundError", combined,
            f"one of {len(targets)} modules cannot be imported by a dotted "
            f"unittest run:\n{combined}",
        )
        self.assertNotIn("Traceback", combined, combined)


class SettingsMergeEncodingTests(unittest.TestCase):
    """Regression for anywhere-agents#36: the settings.json merge round trip.

    Neither entry point pinned the text encoding. `Get-Content` / `Set-Content`
    without `-Encoding` use the machine's ANSI codepage on Windows PowerShell
    5.1, and Python's text mode does the same, so a UTF-8 settings.json was
    decoded as cp1252 and written back with every character outside that
    codepage destroyed. The file stayed valid JSON, so nothing failed and
    nothing was reported. Five U+FFFD were observed in a live
    ~/.claude/settings.json, all inside the block that configures the
    permission classifier.

    The behavioural assertions cannot detect that on a host whose ANSI
    codepage is already UTF-8, which is this machine and may be the CI runner,
    so the static check carries the encoding contract and runs everywhere. The
    behavioural checks carry the properties that hold on any host: no BOM, LF
    line endings, a stable second run, and the same bytes from both entry
    points.
    """

    BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"
    BOOTSTRAP_SH = ROOT / "bootstrap" / "bootstrap.sh"

    # A settings document shaped like the real ones, plus the cases where two
    # JSON serializers usually part company.
    SAMPLE = {
        "permissions": {"allow": ["Bash(ls:*)"], "deny": [], "ask": ["Bash(git push:*)"]},
        "env": {"CLAUDE_CODE_EFFORT_LEVEL": "max"},
        "note": "alpha — beta é 中文",
        "path": "https://example.invalid/a/b",
        "angle": "<tag> & 'quote' \"dquote\"",
        "empty_obj": {},
        "empty_arr": [],
        "flags": {"on": True, "off": False, "none": None, "n": 12345},
        "hooks": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x --quiet"}]}],
    }

    def test_powershell_pins_encoding_at_every_json_site(self) -> None:
        text = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        for pattern, why in (
            (r"Get-Content[^\n]*-Raw[^\n]*ConvertFrom-Json",
             "reads JSON through the ANSI codepage"),
            (r"ConvertTo-Json[^\n]*\|\s*Set-Content",
             "writes JSON through the ANSI codepage, with a BOM on 5.1"),
        ):
            hits = re.findall(pattern, text)
            self.assertEqual(
                hits, [],
                f"bootstrap.ps1 still {why}: {hits}. Use Read-JsonFileUtf8 / "
                f"Write-JsonFileUtf8 with ConvertTo-CanonicalJson instead.",
            )
        for helper in ("Read-JsonFileUtf8", "ConvertTo-CanonicalJson", "Write-JsonFileUtf8"):
            self.assertIn(f"function {helper}", text)

    def test_bash_pins_encoding_and_newlines_at_every_json_site(self) -> None:
        text = self.BOOTSTRAP_SH.read_text(encoding="utf-8")
        for pattern, why in (
            (r"\.read_text\(\)", "reads JSON in text mode, which picks the locale codepage"),
            (r"write_text\(json\.dumps", "writes JSON in text mode, which also rewrites newlines"),
            (r"os\.fdopen\(fd, 'w'", "writes JSON in text mode, which also rewrites newlines"),
        ):
            hits = re.findall(pattern, text)
            self.assertEqual(hits, [], f"bootstrap.sh still {why}: {hits}")

    def _merge_via_powershell(self, shell: str, work: Path, target: Path, shared: Path) -> None:
        source = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        # The shared slicer, not a second copy of it. This method used to carry
        # its own, which appended nothing when the braces did not balance
        # instead of raising, so a function simply went missing from the driver
        # and the merge wrote `null` over the target. The test then reported a
        # lost em dash, which is a true statement about a file that no longer
        # had anything to do with the behaviour under test.
        bodies = [
            RulePacksConfigStateTests._powershell_function(source, name)
            for name in ("Merge-Json", "Test-PythonScalarEqual", "Get-PythonNumericKey",
                         "Read-JsonFileUtf8", "ConvertTo-CanonicalJson", "Write-JsonFileUtf8")
        ]
        script = work / "merge.ps1"
        script.write_text(
            "$script:JsonDepth = 64\n"
            + "\n".join(bodies)
            + "\n$t = $args[0]\n$s = $args[1]\n"
            "$o = Read-JsonFileUtf8 $t\n"
            "$sh = Read-JsonFileUtf8 $s\n"
            "Merge-Json $o $sh\n"
            "Write-JsonFileUtf8 $t (ConvertTo-CanonicalJson $o)\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
             str(target), str(shared)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def _merge_via_helper(self, target: Path, shared: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "merge_settings.py"),
             str(target), str(shared)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    @staticmethod
    def _assert_canonical(case: unittest.TestCase, data: bytes, label: str) -> None:
        case.assertFalse(data.startswith(b"\xef\xbb\xbf"), f"{label} carries a BOM")
        case.assertNotIn(b"\r\n", data, f"{label} carries CRLF")
        case.assertIn("—".encode("utf-8"), data, f"{label} lost the em dash")
        case.assertIn("中文".encode("utf-8"), data, f"{label} lost the CJK text")

    SHARED = {"permissions": {"allow": ["Bash(git:*)"]}, "attribution": {"co_authored_by": False}}

    def test_both_entry_points_call_the_shared_helper(self) -> None:
        # The formatting divergence cannot be closed by making two serializers
        # agree: ConvertTo-Json indents with four spaces and aligns nested
        # objects on Windows PowerShell 5.1, and with two spaces on
        # PowerShell 7, so neither matches json.dumps. One implementation is
        # the only thing that makes the bytes agree, so pin that both entry
        # points reach for it.
        self.assertTrue((ROOT / "scripts" / "merge_settings.py").is_file())
        for script in (self.BOOTSTRAP_SH, self.BOOTSTRAP_PS1):
            text = script.read_text(encoding="utf-8")
            self.assertIn(
                "scripts/merge_settings.py", text,
                f"{script.name} does not call the shared settings merge helper",
            )

    # Scalar-array cases where .NET and Python disagree about what one key is.
    # Python's dict.fromkeys treats 1, 1.0 and True as the same key and keeps
    # "1" apart; a hash set over boxed .NET values does the opposite on both
    # counts. The PowerShell fallback runs only on a machine with no Python,
    # but it is supposed to reach the same answer as the shared helper.
    SCALAR_DEDUP_CASES = (
        ("int and its float", [1], [1.0]),
        ("int and true", [1], [True]),
        ("int and its string", [1], ["1"]),
        ("case-distinct strings", ["a"], ["A"]),
        ("plain duplicate", ["x", "y"], ["y", "z"]),
        ("zero and false", [0], [False]),
        ("null twice", [None], [None]),
        # Python compares a bool as the integer 1 or 0, so True is 1 and not 2.
        # PowerShell converts toward the left operand's type, so the answer
        # depends on which side the bool is on: `2` then `True` compares as
        # numbers and keeps both, while `True` then `2` converts 2 to a bool and
        # merges them. Both orders are listed, because a settings file decides
        # the order and the earlier cases all happened to use the safe one.
        ("two then true", [2], [True]),
        ("true then two", [True], [2]),
        ("true then minus one", [True], [-1]),
        ("false then two", [False], [2]),
        # Above 2**53 a double can no longer name every integer, so widening
        # the integer to a double before comparing merges two distinct values.
        # Measured: PowerShell 7 collapsed both orderings of the first pair,
        # and both editions collapsed the second. The two editions do not even
        # agree on the parsed type; 5.1 reads a number written with a decimal
        # point as a Decimal and 7 reads it as a Double.
        ("int and the float below it", [2 ** 53 + 1], [float(2 ** 53)]),
        ("float below it and the int", [float(2 ** 53)], [2 ** 53 + 1]),
        ("max int and its rounded float", [2 ** 63 - 1], [float(2 ** 63 - 1)]),
        ("rounded float and the max int", [float(2 ** 63 - 1)], [2 ** 63 - 1]),
        ("big int and its exact float", [10 ** 20], [1e20]),
        ("negative zero and zero", [-0.0], [0]),
        ("int and a fraction", [1], [1.5]),
        ("fraction twice", [1.5], [1.5]),
    )

    def test_powershell_fallback_dedups_scalars_like_the_helper(self) -> None:
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        for label, base_list, over_list in self.SCALAR_DEDUP_CASES:
            base = {"env": {"values": base_list}}
            over = {"env": {"values": over_list}}
            with tempfile.TemporaryDirectory() as tmpdir:
                work = Path(tmpdir)
                shared_path = work / "shared.json"
                shared_path.write_bytes(
                    (json.dumps(over, indent=2) + "\n").encode("utf-8"))
                reference = work / "reference.json"
                reference.write_bytes(
                    (json.dumps(base, indent=2) + "\n").encode("utf-8"))
                self._merge_via_helper(reference, shared_path)
                expected = json.loads(reference.read_text(encoding="utf-8"))
                for shell in shells:
                    target = work / f"target-{Path(shell).stem}.json"
                    target.write_bytes(
                        (json.dumps(base, indent=2) + "\n").encode("utf-8"))
                    self._merge_via_powershell(shell, work, target, shared_path)
                    actual = json.loads(target.read_text(encoding="utf-8"))
                    with self.subTest(case=label, entrypoint=Path(shell).stem):
                        self.assertEqual(
                            expected["env"]["values"], actual["env"]["values"],
                            f"{label}: fallback disagrees with merge_settings.py",
                        )

    # Inputs the shared helper reads and then refuses. Each is a state where
    # the target must survive untouched.
    HELPER_REFUSALS = (
        ("malformed json", b"{", b'{"values": [2]}\n'),
        ("invalid utf-8", b'{"note":"\xe9","values":[1]}\n', b'{"values": [2]}\n'),
        ("empty result", b"{}", b"{}\n"),
        ("mixed array", b'{"values": [0]}', b'{"values": [1, {"a": 1}]}\n'),
    )

    def test_the_json_reader_rejects_invalid_utf8(self) -> None:
        # `[System.Text.Encoding]::UTF8` substitutes U+FFFD for an invalid byte
        # and reports nothing. The Bash half decodes with `utf-8-sig`, which
        # raises, so the two disagreed on the input this release is about: the
        # `~/.claude.json` heal read a file already damaged by the cp1252 round
        # trip, substituted its own damage, and wrote the whole file back. That
        # path has no Python helper in front of it.
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        body = RulePacksConfigStateTests._powershell_function(
            self.BOOTSTRAP_PS1.read_text(encoding="utf-8"), "Read-JsonFileUtf8")
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            driver = work / "read.ps1"
            driver.write_text(
                body + "\ntry { $null = Read-JsonFileUtf8 $args[0]; 'accepted' } "
                "catch { 'rejected' }\n",
                encoding="utf-8",
            )
            bad = work / "bad.json"
            bad.write_bytes(b'{"note":"\xe9","values":[1]}\n')
            good = work / "good.json"
            good.write_bytes('{"note":"é","values":[1]}\n'.encode("utf-8"))
            for shell in shells:
                for label, path, expected in (("invalid", bad, "rejected"),
                                              ("valid", good, "accepted")):
                    with self.subTest(case=label, entrypoint=Path(shell).stem):
                        result = subprocess.run(
                            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                             "Bypass", "-File", str(driver), str(path)],
                            capture_output=True, text=True,
                        )
                        self.assertEqual(result.returncode, 0, msg=result.stderr)
                        self.assertEqual(expected, result.stdout.strip(), msg=result.stderr)

    # Inputs the in-shell fallback must refuse when no helper is available.
    # `{` is valid UTF-8, so the strict decoder does not see it; a bare array
    # root parses cleanly and is not an object; `{}` merged with `{}` is the
    # empty result the helper already refuses.
    FALLBACK_REFUSALS = (
        ("malformed json", b"{", b'{"values": [2]}\n'),
        ("invalid utf-8", b'{"note":"\xe9","values":[1]}\n', b'{"values": [2]}\n'),
        ("array root", b'[1, 2]', b'{"values": [2]}\n'),
        ("string root", b'"hello"', b'{"values": [2]}\n'),
        ("empty result", b"{}", b"{}\n"),
    )

    def test_the_no_helper_fallback_leaves_unreadable_targets_alone(self) -> None:
        # The path the previous test does not reach. With no Python at all,
        # `Invoke-SettingsMerge` returns false and the in-shell merge runs, and
        # that is where a target holding `{` came back as one newline under
        # Windows PowerShell 5.1 and as `null` under PowerShell 7. A strict
        # decoder is not enough on its own, because `ConvertFrom-Json` fails
        # without throwing; it needs -ErrorAction Stop. This runs the shipped
        # call-site statements with $pyCmd forced to null.
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        source = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        names = ("Invoke-SettingsMerge", "Merge-Json", "Test-PythonScalarEqual",
                 "Get-PythonNumericKey", "Read-JsonFileUtf8", "ConvertTo-CanonicalJson",
                 "Write-JsonFileUtf8")
        bodies = "\n".join(
            RulePacksConfigStateTests._powershell_function(source, name) for name in names
        )
        call_site = RulePacksConfigStateTests._powershell_block(
            source,
            "if (-not (Invoke-SettingsMerge '.claude/settings.json'",
            "the project settings call site",
        )
        driver_text = (
            "$script:JsonDepth = 64\n$pyCmd = $null\n"
            + bodies
            + "\nSet-Location $args[0]\n"
            + call_site
            + "\n"
        )
        for label, target_bytes, shared_bytes in self.FALLBACK_REFUSALS:
            for shell in shells:
                with self.subTest(case=label, entrypoint=Path(shell).stem):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        work = Path(tmpdir)
                        (work / ".claude").mkdir()
                        (work / ".agent-config" / "repo" / ".claude").mkdir(parents=True)
                        target = work / ".claude" / "settings.json"
                        target.write_bytes(target_bytes)
                        (work / ".agent-config" / "repo" / ".claude" / "settings.json").write_bytes(
                            shared_bytes)
                        driver = work / "driver.ps1"
                        driver.write_text(driver_text, encoding="utf-8")
                        result = subprocess.run(
                            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                             "Bypass", "-File", str(driver), str(work)],
                            capture_output=True, text=True,
                        )
                        self.assertEqual(result.returncode, 0, msg=result.stderr)
                        self.assertEqual(
                            target_bytes, target.read_bytes(),
                            f"{label}: the no-helper fallback rewrote an unreadable target",
                        )

    def test_the_claude_json_heal_leaves_an_unreadable_file_alone(self) -> None:
        # The third call site, and the only one with no helper in front of it
        # at all. Its catch was already there; what made it ineffective was a
        # reader that substituted U+FFFD instead of failing. A file damaged by
        # the cp1252 round trip is exactly the input it would meet.
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        source = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        bodies = "\n".join(
            RulePacksConfigStateTests._powershell_function(source, name)
            for name in ("Read-JsonFileUtf8", "ConvertTo-CanonicalJson", "Write-JsonFileUtf8")
        )
        heal = RulePacksConfigStateTests._powershell_block(
            source, "if (Test-Path $claudeJson) {", "the claude.json heal")
        driver_text = (
            "$script:JsonDepth = 64\n"
            + bodies
            + "\n$claudeJson = $args[0]\n"
            + heal
            + "\n"
        )
        # autoUpdates is false, so a reader that does not fail rewrites the file.
        damaged = b'{"autoUpdates": false, "note": "\xe9"}\n'
        healthy = '{"autoUpdates": false, "note": "é"}\n'.encode("utf-8")
        for shell in shells:
            for label, original, expect_changed in (("invalid utf-8", damaged, False),
                                                    ("valid utf-8", healthy, True)):
                with self.subTest(case=label, entrypoint=Path(shell).stem):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        work = Path(tmpdir)
                        state = work / ".claude.json"
                        state.write_bytes(original)
                        driver = work / "heal.ps1"
                        driver.write_text(driver_text, encoding="utf-8")
                        result = subprocess.run(
                            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                             "Bypass", "-File", str(driver), str(state)],
                            capture_output=True, text=True,
                        )
                        self.assertEqual(result.returncode, 0, msg=result.stderr)
                        changed = state.read_bytes() != original
                        self.assertEqual(
                            expect_changed, changed,
                            f"{label}: heal {'skipped' if expect_changed else 'rewrote'} "
                            f"the file; bytes are {state.read_bytes()!r}",
                        )
                        if not expect_changed:
                            self.assertNotIn(
                                "�".encode("utf-8"), state.read_bytes(),
                                "the heal wrote a replacement character into user state",
                            )

    def test_a_refused_merge_does_not_fall_back_onto_the_same_input(self) -> None:
        # `Invoke-SettingsMerge` returned $false for every nonzero exit, and the
        # call site reads $false as "the helper was unavailable" and runs the
        # in-shell merge over the same bytes. Those are different states: a
        # helper that read the file and refused has already decided it must not
        # change. Measured before the fix: `{` came back as one newline under
        # 5.1 and as `null` under 7, and an invalid UTF-8 byte came back as
        # U+FFFD, both with the run reporting success. Bash leaves all of them
        # alone, so this is also what makes the two entry points agree.
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        source = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        names = ("Invoke-SettingsMerge", "Merge-Json", "Test-PythonScalarEqual",
                 "Get-PythonNumericKey", "Read-JsonFileUtf8", "ConvertTo-CanonicalJson",
                 "Write-JsonFileUtf8")
        bodies = "\n".join(
            RulePacksConfigStateTests._powershell_function(source, name) for name in names
        )
        helper = str(ROOT / "scripts" / "merge_settings.py").replace("'", "''")
        bodies = bodies.replace(
            "$helper = '.agent-config/repo/scripts/merge_settings.py'",
            f"$helper = '{helper}'",
        )
        driver_text = (
            "$script:JsonDepth = 64\n"
            f"$pyCmd = [pscustomobject]@{{ Path = '{sys.executable}' }}\n"
            + bodies
            + "\n$target = $args[0]\n$sharedPath = $args[1]\n"
            "if (-not (Invoke-SettingsMerge $target $sharedPath)) {\n"
            "  $shared = Read-JsonFileUtf8 $sharedPath\n"
            "  $project = Read-JsonFileUtf8 $target\n"
            "  Merge-Json $project $shared\n"
            "  Write-JsonFileUtf8 $target (ConvertTo-CanonicalJson $project)\n"
            "}\n"
        )
        for label, target_bytes, shared_bytes in self.HELPER_REFUSALS:
            for shell in shells:
                with self.subTest(case=label, entrypoint=Path(shell).stem):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        work = Path(tmpdir)
                        driver = work / "driver.ps1"
                        driver.write_text(driver_text, encoding="utf-8")
                        target = work / "target.json"
                        target.write_bytes(target_bytes)
                        shared_path = work / "shared.json"
                        shared_path.write_bytes(shared_bytes)
                        result = subprocess.run(
                            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                             "Bypass", "-File", str(driver), str(target), str(shared_path)],
                            capture_output=True, text=True,
                        )
                        self.assertEqual(result.returncode, 0, msg=result.stderr)
                        self.assertEqual(
                            target_bytes, target.read_bytes(),
                            f"{label}: the fallback rewrote a target the helper refused",
                        )

    def test_the_fallback_publish_skips_a_file_it_would_not_change(self) -> None:
        # The in-shell merge runs where the helper cannot, so it needs the same
        # rule: identical bytes are not republished and not copied aside
        # (anywhere-agents#58). `Publish-SettingsIfChanged` is the call site, so
        # the driver runs that rather than a copy of it.
        shells = powershell_editions_or_fail(self) if sys.platform.startswith("win") else []
        source = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        names = ("Publish-SettingsIfChanged", "Test-SettingsUnchanged",
                 "Backup-SettingsFile", "Write-JsonFileAtomic", "Write-JsonFileUtf8")
        bodies = "\n".join(
            RulePacksConfigStateTests._powershell_function(source, name) for name in names
        )
        driver_text = (
            bodies
            + "\n$target = $args[0]\n$text = [System.IO.File]::ReadAllText($args[1])\n"
            "$wrote = Publish-SettingsIfChanged $target $text -KeepBackup\n"
            "Write-Output ([string]$wrote)\n"
        )
        for shell in shells:
            with self.subTest(entrypoint=Path(shell).stem):
                with tempfile.TemporaryDirectory() as tmpdir:
                    work = Path(tmpdir)
                    driver = work / "driver.ps1"
                    driver.write_text(driver_text, encoding="utf-8")
                    target = work / "settings.json"
                    payload = work / "payload.json"
                    text = '{\n  "env": {\n    "KEEP": "1"\n  }\n}\n'
                    payload.write_bytes(text.encode("utf-8"))
                    target.write_bytes(text.encode("utf-8"))
                    stamp = target.stat().st_mtime_ns

                    def run() -> subprocess.CompletedProcess:
                        return subprocess.run(
                            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                             "Bypass", "-File", str(driver), str(target), str(payload)],
                            capture_output=True, text=True, timeout=120,
                        )

                    result = run()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("False", result.stdout)
                    self.assertEqual(target.stat().st_mtime_ns, stamp, "the target was rewritten")
                    copies = [p.name for p in work.iterdir()
                              if p.name.startswith("settings.json.bak-")]
                    self.assertEqual(copies, [], f"a no-op run left a backup: {copies}")

                    # A real change still publishes and still keeps a copy.
                    payload.write_bytes('{\n  "env": {\n    "KEEP": "2"\n  }\n}\n'.encode("utf-8"))
                    result = run()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("True", result.stdout)
                    self.assertEqual(target.read_bytes(), payload.read_bytes())
                    copies = [p for p in work.iterdir()
                              if p.name.startswith("settings.json.bak-")]
                    self.assertEqual(len(copies), 1, "a changed file kept no copy")
                    self.assertEqual(copies[0].read_bytes(), text.encode("utf-8"))

    def test_a_mixed_array_is_reported_rather_than_traced(self) -> None:
        # The branch that chooses dedup over replace reads the incoming list's
        # first element alone, which is deliberate parity with the inline
        # program the helper replaced. A scalar first and an object later
        # therefore reaches dict.fromkeys with an unhashable element. The Bash
        # entry point does not read the helper's exit code, so an uncaught
        # TypeError printed a traceback and the run continued past it.
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            shared_path = work / "shared.json"
            shared_path.write_bytes(
                (json.dumps({"env": {"values": [1, {"a": 1}]}}, indent=2) + "\n").encode("utf-8"))
            target = work / "target.json"
            original = (json.dumps({"env": {"values": [0]}}, indent=2) + "\n").encode("utf-8")
            target.write_bytes(original)
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "merge_settings.py"),
                 str(target), str(shared_path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
            self.assertIn("cannot merge", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(
                original, target.read_bytes(),
                "a merge that cannot complete must leave the file alone",
            )

    def test_helper_writes_canonical_bytes_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            shared_path = work / "shared.json"
            shared_path.write_bytes(
                (json.dumps(self.SHARED, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            )
            target = work / "target.json"
            target.write_bytes(
                (json.dumps(self.SAMPLE, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            )
            self._merge_via_helper(target, shared_path)
            first = target.read_bytes()
            self._assert_canonical(self, first, "shared helper")
            merged = json.loads(first.decode("utf-8"))
            self.assertIn("Bash(git:*)", merged["permissions"]["allow"])
            self.assertIn("Bash(ls:*)", merged["permissions"]["allow"])
            self.assertIs(merged["attribution"]["co_authored_by"], False)
            self._merge_via_helper(target, shared_path)
            self.assertEqual(
                first, target.read_bytes(),
                "the helper is not idempotent, so every bootstrap rewrites the file",
            )

    def test_helper_reads_a_bom_prefixed_file(self) -> None:
        # An earlier `Set-Content -Encoding UTF8` on Windows PowerShell 5.1
        # left a BOM on some machines. Reading that back must work, and the
        # BOM must not survive the write.
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            shared_path = work / "shared.json"
            shared_path.write_bytes((json.dumps(self.SHARED, indent=2) + "\n").encode("utf-8"))
            target = work / "target.json"
            target.write_bytes(
                b"\xef\xbb\xbf" + (json.dumps(self.SAMPLE, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            )
            self._merge_via_helper(target, shared_path)
            data = target.read_bytes()
            self._assert_canonical(self, data, "shared helper on a BOM-prefixed file")

    def test_helper_refuses_to_write_an_empty_object(self) -> None:
        # The user-level settings.json was truncated to zero bytes once. A
        # merge that produces nothing is a defect rather than a state to
        # persist, so the old content stays readable.
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            shared_path = work / "shared.json"
            shared_path.write_bytes(b"{}\n")
            target = work / "target.json"
            # No trailing newline, so a write would be visible. With `{}\n` on
            # both sides the canonical form of the empty result is the original
            # bytes, and the byte check could not tell a refusal from a write.
            original = b"{}"
            target.write_bytes(original)
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "merge_settings.py"),
                 str(target), str(shared_path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("refusing to write an empty object", result.stderr)
            # The exit code and the message alone stayed green if the helper
            # wrote `{}` and then complained about it. What matters is the file.
            self.assertEqual(original, target.read_bytes())

    def test_powershell_fallback_is_canonical_where_it_can_be(self) -> None:
        # The fallback runs only when no Python is available. It cannot match
        # the helper's bytes, for the ConvertTo-Json reason above, but it must
        # still write UTF-8 without a BOM, use LF, keep non-ASCII, and be
        # stable across runs so it does not rewrite the file every session.
        shells = powershell_editions_or_fail(self)
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            shared_path = work / "shared.json"
            shared_path.write_bytes(
                (json.dumps(self.SHARED, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            )
            original = (json.dumps(self.SAMPLE, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            for shell in shells:
                with self.subTest(shell=Path(shell).stem):
                    target = work / f"ps-{Path(shell).stem}.json"
                    target.write_bytes(original)
                    self._merge_via_powershell(shell, work, target, shared_path)
                    first = target.read_bytes()
                    self._assert_canonical(self, first, f"{Path(shell).stem} fallback")
                    merged = json.loads(first.decode("utf-8"))
                    self.assertIn("Bash(git:*)", merged["permissions"]["allow"])
                    self.assertIn("Bash(ls:*)", merged["permissions"]["allow"])
                    self._merge_via_powershell(shell, work, target, shared_path)
                    self.assertEqual(
                        first, target.read_bytes(),
                        "the fallback is not idempotent, so every bootstrap rewrites the file",
                    )


class SettingsPublicationTests(unittest.TestCase):
    """~/.claude/settings.json survives a concurrent or interrupted writer.

    The file registers the PreToolUse guard for every consumer on the machine.
    A session-start burst runs one bootstrap per consumer repo, all writing this
    one path, and an in-place write truncates before it fills. One such run left
    it at zero bytes: the guard stopped gating destructive commands, the quota
    status line went blank, and six user-only keys were gone with the newest
    backup on disk 78 days old (anywhere-agents#32).
    """

    HELPER = ROOT / "scripts" / "merge_settings.py"
    BOOTSTRAP_SH = ROOT / "bootstrap" / "bootstrap.sh"
    BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("merge_settings", cls.HELPER)
        assert spec and spec.loader
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.target = self.home / "settings.json"
        self.shared = self.home / "shared.json"
        self.shared.write_text(
            json.dumps({"permissions": {"allow": ["Bash(git:*)"]}}), encoding="utf-8"
        )

    def _write_target(self, data: dict) -> bytes:
        payload = (json.dumps(data, indent=2) + "\n").encode("utf-8")
        self.target.write_bytes(payload)
        return payload

    def _run_helper(self, target=None, shared=None, env=None):
        return subprocess.run(
            [sys.executable, str(self.HELPER),
             str(target or self.target), str(shared or self.shared)],
            capture_output=True, text=True, timeout=120, env=env,
        )

    def test_an_interrupted_publish_leaves_the_previous_content(self) -> None:
        # The truncation is what made the incident expensive, so the target must
        # never be the file being filled in. Failing the rename stands in for
        # every way a write can stop early.
        before = self._write_target({"env": {"KEEP": "1"}})
        with mock.patch.object(
            self.module.os, "replace", side_effect=OSError("interrupted")
        ):
            with self.assertRaises(OSError):
                self.module.write_json(self.target, {"env": {"NEW": "2"}})
        self.assertEqual(self.target.read_bytes(), before)
        leftovers = [p.name for p in self.home.iterdir()
                     if p.name.startswith(".") and p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], f"temp file left behind: {leftovers}")

    def test_concurrent_merges_keep_every_writers_key(self) -> None:
        # The failing shape is a lost update, not only a truncation: two runs
        # read the same bytes, each merges its own shared file, and the second
        # rename discards what the first added. The lock is what stops it.
        self._write_target({"env": {"BASE": "1"}})
        shared_files = []
        for index in range(6):
            path = self.home / f"shared-{index}.json"
            path.write_text(json.dumps({"env": {f"K{index}": str(index)}}), encoding="utf-8")
            shared_files.append(path)
        procs = [
            subprocess.Popen(
                [sys.executable, str(self.HELPER), str(self.target), str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for path in shared_files
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=180)
            self.assertEqual(proc.returncode, 0, err)
        merged = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(merged["env"]["BASE"], "1")
        for index in range(6):
            self.assertIn(f"K{index}", merged["env"],
                          f"a concurrent writer lost K{index}: {merged['env']}")

    def test_a_held_lock_refuses_rather_than_racing(self) -> None:
        # A peer mid-merge is the case the lock exists for, so the answer is a
        # distinct exit code and an untouched target, not a best-effort write.
        before = self._write_target({"env": {"KEEP": "1"}})
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\n"
             "sys.path.insert(0, %r)\n" % str(self.HELPER.parent) +
             "import merge_settings, pathlib\n"
             "with merge_settings.settings_lock(pathlib.Path(%r)) as held:\n" % str(self.target) +
             "    print('held' if held else 'unsupported', flush=True)\n"
             "    time.sleep(120)\n"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # Cleanup is LIFO, so wait registers first and runs last: kill, then
        # reap. Windows refuses to remove a directory whose file is still open,
        # and kill alone does not guarantee the handle is gone.
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        state = holder.stdout.readline().strip()
        if state != "held":
            self.skipTest("this filesystem does not support locking")
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys, pathlib\n"
             "sys.path.insert(0, %r)\n" % str(self.HELPER.parent) +
             "import merge_settings\n"
             "merge_settings.LOCK_TIMEOUT_SECONDS = 1.0\n"
             "sys.exit(merge_settings.main(['merge_settings', %r, %r]))"
             % (str(self.target), str(self.shared))],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.target.read_bytes(), before)

    def test_the_previous_content_is_kept_under_a_capped_history(self) -> None:
        keep = self.module.BACKUP_KEEP
        self._write_target({"env": {"ROUND": "0"}})
        for round_number in range(1, keep + 4):
            self.shared.write_text(
                json.dumps({"env": {"ROUND": str(round_number)}}), encoding="utf-8"
            )
            result = self._run_helper()
            self.assertEqual(result.returncode, 0, result.stderr)
        backups = sorted(p for p in self.home.iterdir()
                         if p.name.startswith("settings.json.bak-"))
        self.assertEqual(len(backups), keep,
                         f"history is not capped at {keep}: {[p.name for p in backups]}")
        newest = json.loads(backups[-1].read_text(encoding="utf-8"))
        self.assertEqual(newest["env"]["ROUND"], str(keep + 2),
                         "the newest backup is not the content the last run replaced")

    def test_a_run_that_changes_nothing_writes_nothing(self) -> None:
        # Bootstrap runs on every session, and a consumer loop runs it 27 times
        # in a row. Republishing identical bytes each time left 198 backups
        # across those repos, every one of them a copy of the live file, which
        # pushed the recovery history out of the capped window
        # (anywhere-agents#58).
        merged = {"permissions": {"allow": ["Bash(git:*)"]}, "env": {"KEEP": "1"}}
        self.target.write_bytes(self.module.canonical_bytes(merged))
        before = self.target.read_bytes()
        stamp = self.target.stat().st_mtime_ns
        result = self._run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.target.stat().st_mtime_ns, stamp, "the target was rewritten")
        copies = [p.name for p in self.home.iterdir()
                  if p.name.startswith("settings.json.bak-")]
        self.assertEqual(copies, [], f"a no-op run left a backup: {copies}")

    def test_a_run_that_changes_something_still_keeps_a_copy(self) -> None:
        before = self._write_target({"env": {"KEEP": "1"}})
        result = self._run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        copies = [p for p in self.home.iterdir()
                  if p.name.startswith("settings.json.bak-")]
        self.assertEqual(len(copies), 1, "a merge that changed the file kept no copy")
        self.assertEqual(copies[0].read_bytes(), before)
        merged = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(merged["permissions"]["allow"], ["Bash(git:*)"])
        self.assertEqual(merged["env"]["KEEP"], "1")

    def test_a_target_in_another_shape_is_rewritten_once(self) -> None:
        # Canonicalizing is a real change, so the first run publishes and keeps
        # a copy. The second sees its own bytes and stops.
        merged = {"permissions": {"allow": ["Bash(git:*)"]}, "env": {"KEEP": "1"}}
        self.target.write_bytes(
            (json.dumps(merged, indent=4) + "\n").encode("utf-8")
        )
        self.assertEqual(self._run_helper().returncode, 0)
        after_first = self.target.read_bytes()
        self.assertEqual(after_first, self.module.canonical_bytes(merged))
        self.assertEqual(
            len([p for p in self.home.iterdir() if p.name.startswith("settings.json.bak-")]), 1
        )
        stamp = self.target.stat().st_mtime_ns
        self.assertEqual(self._run_helper().returncode, 0)
        self.assertEqual(self.target.read_bytes(), after_first)
        self.assertEqual(self.target.stat().st_mtime_ns, stamp,
                         "the second run rewrote bytes it did not change")
        self.assertEqual(
            len([p for p in self.home.iterdir() if p.name.startswith("settings.json.bak-")]), 1,
            "the second run kept another copy of bytes it did not change",
        )

    def test_a_target_the_serializer_cannot_encode_is_reported(self) -> None:
        # `{"BAD": "\ud800"}` parses back and then fails to encode, so the
        # comparison's own call to canonical_bytes raises. That failure used to
        # land in the publish handler and print a diagnostic; a guard catching
        # only OSError would turn the same input into a traceback.
        self.target.write_bytes(b'{\n  "env": {\n    "BAD": "\\ud800"\n  }\n}\n')
        before = self.target.read_bytes()
        result = self._run_helper()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("merge_settings: cannot publish", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.target.read_bytes(), before,
                         "a target the serializer refused was modified anyway")

    def test_the_user_settings_target_takes_the_lock_the_composer_takes(self) -> None:
        # The composer's permission handler stages a merge into this same file
        # and publishes it under the per-user lock. A sibling lock would not
        # exclude that writer, and a probe showed the interleaving dropping a
        # committed permission with exit 0.
        user_settings = Path.home() / ".claude" / "settings.json"
        chosen = self.module.lock_path_for(user_settings)
        self.assertEqual(chosen.name, ".pack-lock.lock")
        self.assertEqual(chosen.parent, user_settings.parent)

    def test_every_other_target_keeps_a_sibling_lock(self) -> None:
        # The per-user lock has nothing to do with a file inside one repository,
        # and this helper is called with the project settings path too.
        project = self.home / ".claude" / "settings.json"
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text("{}", encoding="utf-8")
        chosen = self.module.lock_path_for(project)
        self.assertEqual(chosen, project.with_name("settings.json.lock"))

    def test_a_sharing_violation_is_contention_not_an_unsupported_filesystem(self) -> None:
        # Treating every open failure as "this filesystem cannot lock" is how a
        # Windows peer holding the lock file with exclusive sharing turned into
        # an unlocked merge that reported success.
        before = self._write_target({"env": {"KEEP": "1"}})
        denied = PermissionError(errno.EACCES, "sharing violation")
        with mock.patch.object(self.module.os, "open", side_effect=denied):
            self.module.LOCK_TIMEOUT_SECONDS = 0.3
            try:
                rc = self.module.main(
                    ["merge_settings", str(self.target), str(self.shared)]
                )
            finally:
                self.module.LOCK_TIMEOUT_SECONDS = 60.0
        self.assertEqual(rc, 3, "a sharing violation must be reported as contention")
        self.assertEqual(self.target.read_bytes(), before)

    def test_only_a_documented_unsupported_error_degrades(self) -> None:
        self._write_target({"env": {"BASE": "1"}})
        unsupported = OSError(errno.ENOSYS, "locking not supported")
        with mock.patch.object(self.module, "_try_lock", side_effect=unsupported):
            rc = self.module.main(["merge_settings", str(self.target), str(self.shared)])
        self.assertEqual(rc, 0)
        merged = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(merged["env"]["BASE"], "1")
        self.assertIn("Bash(git:*)", merged["permissions"]["allow"])

    def test_an_absent_target_is_created_inside_the_lock(self) -> None:
        # Moving the existence check inside the lock meant the helper had to be
        # able to create, and it refused an absent target with exit 1, so a clean
        # machine ended up with no settings file at all, and therefore no guard.
        self.assertFalse(self.target.exists())
        result = self._run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        created = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertIn("Bash(git:*)", created["permissions"]["allow"])

    def test_creating_an_absent_target_still_waits_for_the_lock(self) -> None:
        # Creation must not be the path that skips serialization: a composer
        # committing at that moment is exactly the writer being protected.
        self.assertFalse(self.target.exists())
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\n"
             "sys.path.insert(0, %r)\n" % str(self.HELPER.parent) +
             "import merge_settings, pathlib\n"
             "with merge_settings.settings_lock(pathlib.Path(%r)) as held:\n" % str(self.target) +
             "    print('held' if held else 'unsupported', flush=True)\n"
             "    time.sleep(120)\n"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        if holder.stdout.readline().strip() != "held":
            self.skipTest("this filesystem does not support locking")
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys\n"
             "sys.path.insert(0, %r)\n" % str(self.HELPER.parent) +
             "import merge_settings\n"
             "merge_settings.LOCK_TIMEOUT_SECONDS = 1.0\n"
             "sys.exit(merge_settings.main(['merge_settings', %r, %r]))"
             % (str(self.target), str(self.shared))],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(self.target.exists(),
                         "creation went ahead while a peer held the lock")

    def test_an_unresolvable_target_refuses_rather_than_guessing(self) -> None:
        # Choosing the sibling lock after a resolution failure published over a
        # composer commit in a fault injection, so not knowing is a refusal.
        self._write_target({"env": {"KEEP": "1"}})
        with mock.patch.object(
            Path, "resolve", side_effect=OSError(errno.EIO, "io error")
        ):
            with self.assertRaises(self.module.LockBusy):
                self.module.lock_path_for(self.target)

    @staticmethod
    def _composer_locks_module() -> Path | None:
        """Locate the composer's own lock module, wherever the wheel tree sits.

        agent-config carries no composer, so this returns None there and the
        cases below skip. anywhere-agents carries the bundled copy, which is
        where the exclusion actually has to hold.
        """
        for base in (ROOT, ROOT.parent / "anywhere-agents"):
            candidate = (base / "packages" / "pypi" / "anywhere_agents" / "composer"
                         / "scripts" / "packs" / "locks.py")
            if candidate.is_file():
                return candidate
        return None

    def _composer_child(self, locks: Path, lock_path: Path) -> str:
        """Hold the composer's user lock in a peer process until this test ends."""
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import importlib.util, pathlib, sys, time\n"
             "spec = importlib.util.spec_from_file_location('composer_locks', %r)\n" % str(locks) +
             "mod = importlib.util.module_from_spec(spec)\n"
             "spec.loader.exec_module(mod)\n"
             "with mod.acquire(pathlib.Path(%r), timeout=5.0):\n" % str(lock_path) +
             "    print('held', flush=True)\n"
             "    time.sleep(120)\n"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child.stdout.readline().strip()

    def test_the_composer_lock_actually_excludes_the_helper(self) -> None:
        # Naming the same path is not the contract; excluding the other writer
        # is. The Round 1 design named a sibling lock and a probe drove the two
        # writers straight through each other, dropping a committed permission.
        locks = self._composer_locks_module()
        if locks is None:
            self.skipTest("no bundled composer in this repo")
        home = Path(self.temp.name) / "fakehome"
        (home / ".claude").mkdir(parents=True)
        target = home / ".claude" / "settings.json"
        target.write_text('{"env": {"KEEP": "1"}}', encoding="utf-8")
        if self._composer_child(locks, home / ".claude" / ".pack-lock.lock") != "held":
            self.skipTest("this filesystem does not support locking")
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys\n"
             "sys.path.insert(0, %r)\n" % str(self.HELPER.parent) +
             "import merge_settings\n"
             "merge_settings.LOCK_TIMEOUT_SECONDS = 1.0\n"
             "sys.exit(merge_settings.main(['merge_settings', %r, %r]))"
             % (str(target), str(self.shared))],
            capture_output=True, text=True, timeout=120, env=env,
        )
        self.assertEqual(result.returncode, 3,
                         f"the helper wrote while the composer held the lock\n{result.stderr}")
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"env": {"KEEP": "1"}})

    def test_the_helper_lock_actually_excludes_the_composer(self) -> None:
        # The reverse direction matters just as much: a composer that could
        # walk past this lock would overwrite a merge in progress.
        locks = self._composer_locks_module()
        if locks is None:
            self.skipTest("no bundled composer in this repo")
        home = Path(self.temp.name) / "fakehome2"
        (home / ".claude").mkdir(parents=True)
        target = home / ".claude" / "settings.json"
        target.write_text('{"env": {"KEEP": "1"}}', encoding="utf-8")
        lock_path = home / ".claude" / ".pack-lock.lock"
        # settings_lock takes the settings file and derives the lock from the
        # home directory, so the holder has to see the fixture as its home.
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\n"
             "sys.path.insert(0, %r)\n" % str(self.HELPER.parent) +
             "import merge_settings, pathlib\n"
             "with merge_settings.settings_lock(pathlib.Path(%r)) as held:\n" % str(target) +
             "    print('held' if held else 'unsupported', flush=True)\n"
             "    time.sleep(120)\n"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, HOME=str(home), USERPROFILE=str(home)),
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        if holder.stdout.readline().strip() != "held":
            self.skipTest("this filesystem does not support locking")
        self.assertTrue(lock_path.is_file(),
                        "the holder did not take the lock beside the fixture home")
        result = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util, pathlib, sys\n"
             "spec = importlib.util.spec_from_file_location('composer_locks', %r)\n" % str(locks) +
             "mod = importlib.util.module_from_spec(spec)\n"
             "spec.loader.exec_module(mod)\n"
             "try:\n"
             "    with mod.acquire(pathlib.Path(%r), timeout=1.0):\n" % str(lock_path) +
             "        print('ACQUIRED')\n"
             "except mod.LockTimeout:\n"
             "    print('REFUSED')\n"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REFUSED", result.stdout,
                      f"the composer walked past the helper's lock: {result.stdout}")

    def test_both_entry_points_read_the_merge_result(self) -> None:
        # The Bash entry point used to discard the exit code, so a refused merge
        # and an applied one produced the same ledger line.
        sh = self.BOOTSTRAP_SH.read_text(encoding="utf-8")
        self.assertIn("_settings_rc=$?", sh,
                      "bootstrap.sh does not read the merge helper's exit code")
        self.assertIn("_ledger_step user_files user failed", sh,
                      "bootstrap.sh cannot record a failed settings merge")
        ps1 = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        self.assertIn("SettingsMergeReason", ps1,
                      "bootstrap.ps1 does not carry a merge failure to the ledger")
        self.assertIn("'user_files' 'user' 'failed'", ps1,
                      "bootstrap.ps1 cannot record a failed settings merge")

    def test_no_entry_point_installs_settings_by_truncating_copy(self) -> None:
        # First install is the same hazard: cp -f and Copy-Item -Force both
        # truncate the destination before writing. Shape is all this can check;
        # SettingsPublicationBashTests runs the Bash branch, and the PowerShell
        # writer is exercised in SettingsPublicationPowerShellTests.
        sh = self.BOOTSTRAP_SH.read_text(encoding="utf-8")
        self.assertNotIn(
            'cp -f .agent-config/repo/user/settings.json "$HOME/.claude/settings.json"', sh,
            "bootstrap.sh still installs user settings with a truncating copy",
        )
        self.assertIn(
            'ln "$_settings_temp" "$HOME/.claude/settings.json"', sh,
            "bootstrap.sh does not publish user settings from a sibling temp file",
        )
        ps1 = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        self.assertNotIn(
            "Copy-Item .agent-config/repo/user/settings.json $userSettings -Force", ps1,
            "bootstrap.ps1 still installs user settings with a truncating copy",
        )
        self.assertIn(
            "Copy-HelperAtomic .agent-config/repo/user/settings.json $userSettings", ps1,
            "bootstrap.ps1 does not install user settings atomically",
        )
        self.assertIn("function Write-JsonFileAtomic", ps1,
                      "bootstrap.ps1 has no atomic JSON writer")
        # The user phase publishes through the one call site that also decides
        # whether there is anything to write, and that helper is the only thing
        # between the merge and the atomic writer.
        self.assertIn("Publish-SettingsIfChanged $userSettings", ps1,
                      "bootstrap.ps1 still publishes user settings in place")
        self.assertIn("  Write-JsonFileAtomic $Path $Text\n", ps1,
                      "Publish-SettingsIfChanged does not reach the atomic writer")

    def test_both_settings_call_sites_decide_before_they_publish(self) -> None:
        # The project phase rewrites .claude/settings.json in every consumer on
        # every session, and the user phase copies the old content aside first.
        # Neither may reach the writer on its own, or a run that changed nothing
        # republishes and keeps a copy of what it did not change
        # (anywhere-agents#58).
        ps1 = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        self.assertIn(
            "Publish-SettingsIfChanged (Join-Path (Get-Location).Path '.claude/settings.json')", ps1,
            "the project phase publishes without deciding whether anything changed",
        )
        calls = [line.strip() for line in ps1.splitlines()
                 if "Write-JsonFileAtomic" in line and not line.startswith("function")]
        self.assertEqual(calls, ["Write-JsonFileAtomic $Path $Text"],
                         f"a settings write bypasses Publish-SettingsIfChanged: {calls}")
        copies = [line.strip() for line in ps1.splitlines()
                  if "Backup-SettingsFile" in line and not line.startswith("function")]
        self.assertEqual(copies, ["if ($KeepBackup) { Backup-SettingsFile $Path }"],
                         f"a copy is kept outside Publish-SettingsIfChanged: {copies}")

    SMOKE_MARK = (
        'USER_SETTINGS="$HOME/.claude/settings.json"\n'
        "SETTINGS_PROBE=false\n"
        'if [ -f "$USER_SETTINGS" ]; then\n'
        '  printf \'\\n\\n\' >> "$USER_SETTINGS"'
        ' || fail "could not mark $USER_SETTINGS;'
        ' the user-level merge cannot be verified"\n'
        "  SETTINGS_PROBE=true\n"
        "fi\n"
    )
    SMOKE_CHECK = (
        'SETTINGS_TAIL=$(tail -c 2 "$USER_SETTINGS" | od -An -tx1 | tr -d \' \\n\')\n'
        'if $SETTINGS_PROBE && [ "$SETTINGS_TAIL" = "0a0a" ]; then\n'
    )

    def _run_smoke_probe(self, settings: bytes | None, preamble: str = "", *, install: str):
        """Run the smoke's own mark and check lines around a stand-in install.

        Returns the completed process and the bytes the settings file was left
        holding, read before the temporary home goes away.
        """
        if not BASH:
            self.skipTest("bash not available")
        smoke = (ROOT / "scripts" / "remote-smoke.sh").read_text(encoding="utf-8")
        for fragment in (self.SMOKE_MARK, self.SMOKE_CHECK):
            self.assertIn(fragment, smoke,
                          "remote-smoke.sh no longer carries the fragment this test runs")
        # Git for Windows' bash can still hold the directory when it exits, and
        # the probe's own result is what this reads, not the tree it ran in.
        with _temp_dir() as tmpdir:
            work = Path(tmpdir)
            home = work / "home"
            (home / ".claude").mkdir(parents=True)
            target = home / ".claude" / "settings.json"
            if settings is not None:
                target.write_bytes(settings)
            script = work / "probe.sh"
            _write_text_lf(script, (
                "fail() { echo \"FAIL $*\"; exit 1; }\n"
                + preamble
                + self.SMOKE_MARK
                + install
                + self.SMOKE_CHECK
                + '  fail "stale"\nfi\necho OK\n'
            ))
            # HOME in forward slashes, because Git for Windows' bash does not
            # treat a backslash in it as a separator and every path built from
            # it would then miss. bash's own directory joins PATH because
            # earlier tests in this class leave a fixture PATH behind, and the
            # fragment needs the coreutils that ship beside bash.
            env = {**os.environ, "HOME": home.as_posix()}
            env["PATH"] = os.pathsep.join(
                [str(Path(BASH).resolve().parent), env.get("PATH", "")])
            result = subprocess.run(
                [BASH, str(script)], capture_output=True, text=True, timeout=120,
                env=env,
            )
            return result, (target.read_bytes() if target.is_file() else None)

    def test_the_release_smoke_catches_an_install_that_did_not_merge(self) -> None:
        # The smoke proved the install applied the user-level merge by watching
        # the file change. A healthy merge now writes nothing when it changes
        # nothing, so on a release-gate machine that already ran the smoke once,
        # watching alone stopped telling a healthy merge from a regressed one.
        # The script marks the file first; an install that never rewrote it
        # gives the mark back.
        canonical = b'{\n  "env": {\n    "KEEP": "1"\n  }\n}\n'
        stale, _ = self._run_smoke_probe(
            canonical, install=": # an install that writes nothing\n")
        self.assertEqual(stale.returncode, 1, stale.stdout)
        self.assertIn("FAIL stale", stale.stdout)

        # A merge that republishes the canonical form takes the mark away, and
        # it passes even though both runs land in the same whole second, which
        # is the resolution `stat` reports.
        healthy, _ = self._run_smoke_probe(
            canonical,
            install='printf \'{\\n  "env": {\\n    "KEEP": "1"\\n  }\\n}\\n\' > "$USER_SETTINGS"\n',
        )
        self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
        self.assertIn("OK", healthy.stdout)

        # First install: no file to mark, so the check has nothing to say and
        # the statusLine assertions after it carry the step on their own.
        first, _ = self._run_smoke_probe(
            None, install='printf \'{}\\n\' > "$USER_SETTINGS"\n')
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

    def test_the_release_smoke_stops_when_it_cannot_mark_the_file(self) -> None:
        # A file that exists and refuses the append is a failure to prepare, and
        # a read-only settings file is how it happens. Carrying on would leave
        # the check disabled and the step reporting that the merge ran, which is
        # the false pass the mark exists to close. `printf` is overridden rather
        # than the file's permissions, so the branch is reached the same way on
        # every platform and under an elevated account.
        result, marked = self._run_smoke_probe(
            b'{\n  "env": {\n    "KEEP": "1"\n  }\n}\n',
            "printf() { return 1; }\n",
            install="echo INSTALLED\n",
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FAIL could not mark", result.stdout)
        self.assertEqual(marked, b'{\n  "env": {\n    "KEEP": "1"\n  }\n}\n',
                         "a probe that could not mark the file changed it anyway")
        self.assertNotIn("INSTALLED", result.stdout,
                         "the install ran although the probe could not be prepared")

    def test_the_release_smoke_mark_leaves_the_settings_readable(self) -> None:
        # It is appended rather than composed and renamed, because this is the
        # operator's own file. A prepended byte would push a byte-order mark off
        # byte zero, where `utf-8-sig` stops stripping it, and the helper would
        # then refuse the file on every later run.
        for label, settings in (
            ("plain", b'{\n  "env": {\n    "KEEP": "1"\n  }\n}\n'),
            ("bom", '\ufeff{\n  "env": {\n    "KEEP": "1"\n  }\n}\n'.encode("utf-8")),
            # A file ending without a newline is why the mark is two of them:
            # one would land on the canonical ending and read as repaired.
            ("no trailing newline", b'{"env": {"KEEP": "1"}}'),
        ):
            with self.subTest(settings=label):
                result, marked = self._run_smoke_probe(
                    settings, install=": # install fails before the merge\n")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(
                    json.loads(marked.decode("utf-8-sig")), {"env": {"KEEP": "1"}},
                    f"{label}: a failed install left settings the helper cannot read",
                )


@unittest.skipUnless(POWERSHELL, "pwsh/powershell not available")
class SettingsPublicationPowerShellTests(unittest.TestCase):
    """The PowerShell half of the settings write path, exercised rather than read.

    Two of the Round 1 findings were defects the source-shape assertions could
    not see: a whole-second backup stamp that collapsed two copies into one, and
    a cleanup that deleted the recovery material when the publish failed.
    """

    BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"

    @classmethod
    def setUpClass(cls) -> None:
        text = cls.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        wanted = ("Write-JsonFileUtf8", "Write-JsonFileAtomic", "Backup-SettingsFile")
        blocks = []
        for name in wanted:
            match = re.search(
                r"^function %s\b.*?^\}" % re.escape(name), text, re.S | re.M
            )
            if not match:
                raise unittest.SkipTest(f"{name} not found in bootstrap.ps1")
            blocks.append(match.group(0))
        cls.functions = "\n\n".join(blocks)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def _pwsh(
        self,
        body: str,
        substitutions: dict[str, str] | None = None,
        prelude: str = "",
    ) -> subprocess.CompletedProcess:
        functions = self.functions
        for old, new in (substitutions or {}).items():
            assert functions.count(old) == 1, f"substitution anchor not unique: {old}"
            functions = functions.replace(old, new, 1)
        script = self.home / "probe.ps1"
        script.write_text(prelude + functions + "\n\n" + body, encoding="utf-8")
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True, text=True, timeout=120,
        )

    def test_two_backups_in_one_second_keep_both_and_their_order(self) -> None:
        # The first draft stamped whole seconds, so the second copy overwrote the
        # first and a burst of consumer bootstraps kept one snapshot. Different
        # content per round is what makes the ordering claim checkable: comparing
        # a sorted list against itself sorted again cannot fail.
        target = self.home / "settings.json"
        target.write_text('{"round":1}', encoding="utf-8")
        result = self._pwsh(
            f"$p = '{target}'\n"
            "Backup-SettingsFile $p\n"
            "Set-Content -LiteralPath $p -Value '{\"round\":2}' -NoNewline\n"
            "Backup-SettingsFile $p\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        copies = sorted((p for p in self.home.iterdir()
                         if p.name.startswith("settings.json.bak-")),
                        key=lambda p: p.name)
        self.assertEqual(len(copies), 2,
                         f"backups collapsed into one: {[p.name for p in copies]}")
        rounds = [json.loads(p.read_text(encoding="utf-8"))["round"] for p in copies]
        self.assertEqual(rounds, [1, 2],
                         f"lexical order is not time order: {[p.name for p in copies]}")
        # No same-second assertion here: two live clock reads can straddle a
        # boundary with nothing wrong. The supplied-instant case below owns
        # that claim, and this one keeps the count and the content order.

    FIXED_CLOCK = (
        "$script:instants = @(\n"
        "  [datetime]::new(2026, 5, 4, 12, 0, 0, 123, [System.DateTimeKind]::Utc),\n"
        "  [datetime]::new(2026, 5, 4, 12, 0, 0, 876, [System.DateTimeKind]::Utc))\n"
        "$script:tick = 0\n"
        "function Get-Date { $v = $script:instants[$script:tick]; "
        "$script:tick++; return $v }\n"
    )

    def test_two_backups_at_supplied_instants_keep_both_and_their_order(self) -> None:
        # The case above uses the real clock, so its same-second claim can fail
        # on a boundary with nothing wrong. This one supplies both instants, so
        # the naming contract is checked rather than the scheduler.
        target = self.home / "settings.json"
        target.write_text('{"round":1}', encoding="utf-8")
        result = self._pwsh(
            f"$p = '{target}'\n"
            "Backup-SettingsFile $p\n"
            "Set-Content -LiteralPath $p -Value '{\"round\":2}' -NoNewline\n"
            "Backup-SettingsFile $p\n",
            prelude=self.FIXED_CLOCK,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        copies = sorted((p for p in self.home.iterdir()
                         if p.name.startswith("settings.json.bak-")),
                        key=lambda p: p.name)
        self.assertEqual(len(copies), 2,
                         f"backups collapsed into one: {[p.name for p in copies]}")
        rounds = [json.loads(p.read_text(encoding="utf-8"))["round"] for p in copies]
        self.assertEqual(rounds, [1, 2],
                         f"lexical order is not time order: {[p.name for p in copies]}")
        self.assertIn("settings.json.bak-20260504-120000-123000-", copies[0].name)
        self.assertIn("settings.json.bak-20260504-120000-876000-", copies[1].name)

    PARTIAL_REPLACE = (
        "function Invoke-PartialReplace($src, $dst, $bak) {\n"
        "  [System.IO.File]::Move($dst, $bak)\n"
        "  throw 'simulated ERROR_UNABLE_TO_MOVE_REPLACEMENT_2 (1177)'\n"
        "}\n"
    )

    def _ledger_block(self, phase: str) -> str:
        """Lift the shipped ledger decision for one phase out of bootstrap.ps1."""
        text = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        match = re.search(
            r"try \{\n  if \(\$script:SettingsMergeReason\) \{\n"
            r"    Add-LedgerStep '%s'.*?\n\} catch \{\}" % re.escape(phase),
            text, re.S,
        )
        if not match:
            self.skipTest(f"the {phase} ledger decision is not in its expected shape")
        return match.group(0)

    def _run_ledger(self, phase: str, reason: str | None) -> str:
        stub = (
            "$script:rows = @()\n"
            "function Add-LedgerStep { param([string]$Phase, [string]$Scope, "
            "[string]$Status, $Rc = $null, [string]$Reason = '')\n"
            "  $script:rows += \"$Phase|$Scope|$Status|$Reason\" }\n"
        )
        assignment = (
            "$script:SettingsMergeReason = $null\n" if reason is None
            else f"$script:SettingsMergeReason = '{reason}'\n"
        )
        script = self.home / f"ledger-{phase}.ps1"
        script.write_text(
            stub + assignment + self._ledger_block(phase) + "\n$script:rows -join \"`n\"\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_a_refused_settings_merge_reaches_its_own_ledger_row(self) -> None:
        # The project reason used to be saved after the project row had already
        # been written as ok, so a refused project merge vanished, and an earlier
        # revision reported it against the user phase instead. Searching the
        # source for the word "failed" cannot tell either case apart.
        for phase, scope in (("project_files", "repo"), ("user_files", "user")):
            with self.subTest(phase=phase):
                self.assertEqual(
                    self._run_ledger(phase, None), f"{phase}|{scope}|ok|",
                    f"{phase} did not record success",
                )
                self.assertEqual(
                    self._run_ledger(phase, "refused for the test"),
                    f"{phase}|{scope}|failed|refused for the test",
                    f"{phase} did not record its own failure",
                )

    def test_a_partially_replaced_target_is_restored(self) -> None:
        # ReplaceFile documents error 1177 with the original already moved to the
        # backup name and the replacement still under its temp name. Holding the
        # destination open fails earlier than that, so it never reached the state
        # the recovery exists for; this substitutes the one .NET call instead.
        target = self.home / "settings.json"
        target.write_text('{"keep":1}', encoding="utf-8")
        result = self._pwsh(
            f"$p = '{target}'\n"
            "$failed = $false\n"
            "try { Write-JsonFileAtomic $p '{\"new\":2}' } catch { $failed = $true }\n"
            "if (-not $failed) { Write-Error 'the publish was expected to fail'; exit 1 }\n",
            substitutions={
                "[System.IO.File]::Replace($temp, $Path, $backup)":
                    "Invoke-PartialReplace $temp $Path $backup",
            },
            prelude=self.PARTIAL_REPLACE,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertTrue(target.is_file(),
                        "the target was not restored after a partial replacement")
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"keep": 1})

    def test_a_failed_restore_keeps_the_backup_and_names_it(self) -> None:
        # When the restore itself cannot run, the previous content is still on
        # disk and the operator needs its exact path to recover by hand.
        target = self.home / "settings.json"
        target.write_text('{"keep":1}', encoding="utf-8")
        result = self._pwsh(
            f"$p = '{target}'\n"
            "try { Write-JsonFileAtomic $p '{\"new\":2}' } catch { }\n",
            substitutions={
                "[System.IO.File]::Replace($temp, $Path, $backup)":
                    "Invoke-PartialReplace $temp $Path $backup",
                "[System.IO.File]::Move($backup, $Path)":
                    "throw 'injected restore failure'",
            },
            prelude=self.PARTIAL_REPLACE,
        )
        survivors = sorted(p.name for p in self.home.iterdir()
                           if p.name.startswith(".settings.json.")
                           and p.name.endswith(".bak"))
        self.assertTrue(survivors, "the previous content was deleted after a failed restore")
        kept = self.home / survivors[0]
        self.assertEqual(json.loads(kept.read_text(encoding="utf-8")), {"keep": 1})
        self.assertIn(survivors[0], result.stderr,
                      f"stderr does not name the surviving copy: {result.stderr}")


class SettingsPublicationBashTests(unittest.TestCase):
    """The Bash settings block run rather than read.

    Codex raised the no-helper install twice: it tested for absence outside any
    critical section and then published with a rename, and a probe drove that
    interleaving into replacing a permission the composer had already committed.
    Source-string checks passed the whole time, which is why these run the block.
    """

    BOOTSTRAP_SH = ROOT / "bootstrap" / "bootstrap.sh"
    SHARED = '{"permissions": {"allow": ["Bash(git:*)"]}}'

    @classmethod
    def setUpClass(cls) -> None:
        text = cls.BOOTSTRAP_SH.read_text(encoding="utf-8")
        match = re.search(
            r"^if \[ -f \.agent-config/repo/user/settings\.json \]; then\n"
            r".*?^  _ledger_target '~/\.claude/settings\.json'\n^fi$",
            text, re.S | re.M,
        )
        if not match:
            raise unittest.SkipTest("the settings block is not in its expected shape")
        cls.block = match.group(0)

    def setUp(self) -> None:
        if not BASH:
            self.skipTest("bash not available (Git Bash on Windows or system bash on POSIX)")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.work = self.root / "work"
        shared = self.work / ".agent-config" / "repo" / "user"
        shared.mkdir(parents=True)
        (shared / "settings.json").write_text(self.SHARED, encoding="utf-8")
        self.target = self.home / ".claude" / "settings.json"

    def _run(self, substitutions: dict[str, str] | None = None,
             env_extra: dict[str, str] | None = None,
             py: str = "") -> subprocess.CompletedProcess:
        """Run the extracted block with stubbed surroundings.

        `py` empty is the no-helper path; naming an interpreter routes through
        whatever `merge_settings.py` the fixture placed in the fetched tree.
        """
        block = self.block
        for old, new in (substitutions or {}).items():
            self.assertEqual(block.count(old), 1, f"substitution anchor not unique: {old}")
            block = block.replace(old, new, 1)
        script = self.root / "probe.sh"
        _write_text_lf(
            script,
            f"_py='{py}'\n"
            "_atomic_deploy_helper() { return 1; }\n"
            "_ledger_target() { :; }\n"
            + block
            + "\nprintf 'REASON=%s\\n' \"${_SETTINGS_MERGE_REASON:-}\"\n",
        )
        env = _stripped_env(None)
        env["HOME"] = str(self.home)
        env.update(env_extra or {})
        return subprocess.run(
            [BASH, str(script)], cwd=str(self.work), env=env,
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT,
        )

    def test_an_absent_target_is_routed_through_the_helper_not_the_shell(self) -> None:
        # Whenever Python is present the helper owns create-or-merge, because it
        # is the only writer here holding the lock. An existence test out in the
        # shell would send a first install down the unlocked path instead, and
        # that is not visible in the resulting file unless the two writers
        # produce different bytes.
        helper = self.work / ".agent-config" / "repo" / "scripts"
        helper.mkdir(parents=True)
        _write_text_lf(
            helper / "merge_settings.py",
            "import pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text('{\"by\": \"helper\"}')\n",
        )
        self.assertFalse(self.target.exists())
        result = self._run(py=sys.executable.replace("\\", "/"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.target.read_text(encoding="utf-8")), {"by": "helper"},
            "a first install bypassed the helper and its lock",
        )

    def test_a_first_install_without_python_still_lands_the_file(self) -> None:
        # Refusing to install rather than installing unlocked would cost every
        # such machine the PreToolUse guard this file registers.
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.SHARED)
        self.assertIn("REASON=\n", result.stdout,
                      f"a successful install recorded a failure: {result.stdout}")

    def test_an_unavailable_atomic_create_refuses_rather_than_renaming(self) -> None:
        # A rename here was tried and withdrawn. A negative existence check does
        # not reserve the pathname, so the rename replaced a permission the
        # composer committed inside that window and reported success. Refusing
        # is loud, and it cannot destroy a finished write.
        result = self._run(substitutions={
            'ln "$_settings_temp" "$HOME/.claude/settings.json" 2>/dev/null':
                'false',
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.target.exists(),
                         "an unlocked rename published after the atomic create failed")
        self.assertIn("REASON=could not create", result.stdout,
                      f"the refusal was not recorded: {result.stdout}")
        leftovers = [p.name for p in (self.home / ".claude").iterdir()
                     if p.name.startswith(".settings.json.")]
        self.assertEqual(leftovers, [], f"temp files were left behind: {leftovers}")

    def test_a_target_that_appears_mid_install_is_not_replaced(self) -> None:
        # The interleaving Codex drove: another writer commits the file between
        # the absence test and the publish. ln(1) refuses an existing
        # destination, which is what makes create-if-absent a single step.
        other = '{"permissions": {"allow": ["Bash(ls:*)"]}}'
        # Braces matter: the anchor sits under `!` in an `||` list, so an
        # unbraced `&&` would rebind the condition and the race would not run.
        result = self._run(substitutions={
            'cp -f .agent-config/repo/user/settings.json "$_settings_temp"':
                '{ cp -f .agent-config/repo/user/settings.json "$_settings_temp" '
                '&& printf %s "$_RACE" > "$HOME/.claude/settings.json"; }',
        }, env_extra={"_RACE": other})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.target.read_text(encoding="utf-8"), other,
            "the install overwrote a file another writer had already committed",
        )
        self.assertIn("another writer created", result.stdout + result.stderr)
        leftovers = [p.name for p in (self.home / ".claude").iterdir()
                     if p.name.startswith(".settings.json.")]
        self.assertEqual(leftovers, [], f"temp files were left behind: {leftovers}")

    def test_no_python_over_an_existing_target_reports_rather_than_passes(self) -> None:
        # Merging into an existing file is what needs the helper. The previous
        # revision skipped silently, so the ledger row read the same as a run
        # that had applied the shared keys.
        existing = '{"env": {"KEEP": "1"}}'
        self.target.write_text(existing, encoding="utf-8")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_text(encoding="utf-8"), existing)
        self.assertIn("REASON=no Python for the settings merge helper",
                      result.stdout, f"the skipped merge was not recorded: {result.stdout}")


@unittest.skipUnless(POWERSHELL, "pwsh/powershell not available")
class SettingsEntryBlockPowerShellTests(unittest.TestCase):
    """The PowerShell user-settings block run rather than read.

    Round 2 found the shipped block failing a first install outright: the helper
    ran before the existence check, refused an absent target, and the shell never
    reached its own install branch. Nothing in the source shape said so.
    """

    BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"
    SHARED = {"permissions": {"allow": ["Bash(git:*)"]}}

    @classmethod
    def setUpClass(cls) -> None:
        text = cls.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        # Every top-level function, so the block's dependencies do not have to be
        # enumerated and kept in step as it grows. Definitions alone run nothing.
        cls.functions = re.findall(r"^function [\w-]+.*?^\}", text, re.S | re.M)
        if len(cls.functions) < 10:
            raise unittest.SkipTest("bootstrap.ps1 functions are not in their expected shape")
        block = re.search(
            r"^if \(Test-Path \.agent-config/repo/user/settings\.json\) \{\n"
            r".*?^  try \{ Add-LedgerTarget '~/\.claude/settings\.json' \} catch \{\}\n^\}$",
            text, re.S | re.M,
        )
        if not block:
            raise unittest.SkipTest("the user settings block is not in its expected shape")
        cls.block = block.group(0)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.claude = self.home / ".claude"
        self.claude.mkdir(parents=True)
        self.target = self.claude / "settings.json"
        self.work = self.root / "work"
        self.repo = self.work / ".agent-config" / "repo"
        (self.repo / "user").mkdir(parents=True)
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "user" / "settings.json").write_text(
            json.dumps(self.SHARED), encoding="utf-8")

    def _stub_helper(self, body: str) -> None:
        _write_text_lf(self.repo / "scripts" / "merge_settings.py", body)

    def _run(self, py: str | None, substitutions: dict[str, str] | None = None):
        """Run the block with `py` as the interpreter, or None for no Python."""
        functions = "\n\n".join(self.functions)
        for old, new in (substitutions or {}).items():
            self.assertEqual(functions.count(old), 1,
                             f"substitution anchor not unique: {old}")
            functions = functions.replace(old, new, 1)
        py_line = (f"$pyCmd = Get-Command '{py}'\n" if py else "$pyCmd = $null\n")
        script = self.root / "entry.ps1"
        # The stub follows the real definitions, which include one of this name.
        script.write_text(
            f"$userClaude = '{self.claude.as_posix()}'\n"
            + py_line
            + functions + "\n\n"
            + "function Add-LedgerTarget { param($Target) }\n"
            + self.block + "\n"
            "Write-Output \"REASON=$script:SettingsMergeReason\"\n",
            encoding="utf-8",
        )
        # $HOME comes from USERPROFILE at session start, which is how the lock
        # path gets pointed at the fixture without editing the function.
        env = dict(os.environ, USERPROFILE=str(self.home), HOME=str(self.home))
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True, text=True, timeout=180, cwd=str(self.work), env=env,
        )

    @classmethod
    def _project_block(cls) -> str | None:
        text = cls.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        match = re.search(
            r"^if \(Test-Path \.agent-config/repo/\.claude/settings\.json\) \{\n"
            r".*?^\} catch \{\}$",
            text, re.S | re.M,
        )
        return match.group(0) if match else None

    def test_a_real_project_fallback_failure_reaches_the_ledger(self) -> None:
        # The decision that emits the row was correct and had nothing to read:
        # the fallback's catch printed a warning without setting the reason, so
        # a malformed project file was preserved, warned about, and recorded ok.
        block = self._project_block()
        if block is None:
            self.skipTest("the project settings block is not in its expected shape")
        (self.repo / ".claude").mkdir(parents=True)
        (self.repo / ".claude" / "settings.json").write_text(
            json.dumps(self.SHARED), encoding="utf-8")
        project = self.work / ".claude"
        project.mkdir()
        malformed = '{"permissions": '
        (project / "settings.json").write_text(malformed, encoding="utf-8")
        script = self.root / "project.ps1"
        # The stubs come after the real definitions on purpose: bootstrap.ps1
        # defines Add-LedgerStep itself, so a stub placed first is replaced by
        # the real one and the rows silently never arrive.
        script.write_text(
            "$script:rows = @()\n"
            "$pyCmd = $null\n"
            + "\n\n".join(self.functions) + "\n\n"
            "function Add-LedgerTarget { param($Target) }\n"
            "function Add-LedgerStep { param([string]$Phase, [string]$Scope, "
            "[string]$Status, $Rc = $null, [string]$Reason = '')\n"
            "  $script:rows += \"$Phase|$Scope|$Status|$Reason\" }\n"
            + block + "\n"
            "Write-Output ($script:rows -join \"`n\")\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True, text=True, timeout=180, cwd=str(self.work),
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertEqual((project / "settings.json").read_text(encoding="utf-8"),
                         malformed, "the malformed project file was not preserved")
        self.assertIn("project_files|repo|failed", result.stdout,
                      f"a real fallback failure recorded success: {result.stdout}")

    def test_a_first_install_with_python_goes_through_the_helper(self) -> None:
        # The regression Round 2 caught. Distinct bytes per writer are what make
        # "which one published" answerable from the resulting file.
        self._stub_helper(
            "import pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text('{\"by\": \"helper\"}')\n"
        )
        result = self._run(sys.executable)
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertTrue(self.target.is_file(),
                        f"a clean machine ended up with no settings file: {result.stdout}")
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")),
                         {"by": "helper"},
                         "the install bypassed the helper that holds the lock")
        self.assertIn("REASON=\n", result.stdout, result.stdout)

    def test_a_refused_helper_does_not_fall_through_to_an_unlocked_install(self) -> None:
        # Exit 3 means a peer holds the lock. Installing anyway would be the
        # unlocked write the lock exists to prevent.
        self._stub_helper("import sys\nsys.exit(3)\n")
        result = self._run(sys.executable)
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertFalse(self.target.exists(),
                         "the shell installed over a peer that held the lock")
        self.assertIn("another writer held the lock", result.stdout,
                      f"the refusal was not recorded: {result.stdout}")

    def test_the_no_python_fallback_waits_for_the_lock_and_reports(self) -> None:
        # Without Python this shell does the merge itself, so it has to take the
        # same lock. A peer holding it must stop the write, not slow it down.
        existing = {"env": {"KEEP": "1"}}
        self.target.write_text(json.dumps(existing), encoding="utf-8")
        helper = ROOT / "scripts" / "merge_settings.py"
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\n"
             "sys.path.insert(0, %r)\n" % str(helper.parent) +
             "import merge_settings, pathlib\n"
             "with merge_settings.settings_lock(pathlib.Path(%r)) as held:\n" % str(self.target) +
             "    print('held' if held else 'unsupported', flush=True)\n"
             "    time.sleep(120)\n"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, USERPROFILE=str(self.home), HOME=str(self.home)),
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        if holder.stdout.readline().strip() != "held":
            self.skipTest("this filesystem does not support locking")
        result = self._run(None, substitutions={
            "function Open-SettingsLock([string]$TargetPath, [int]$TimeoutSeconds = 60) {":
                "function Open-SettingsLock([string]$TargetPath, [int]$TimeoutSeconds = 1) {",
        })
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), existing,
                         "the fallback merged while a peer held the lock")
        self.assertIn("could not take the settings lock", result.stdout,
                      f"the refusal was not recorded: {result.stdout}")


class PowerShellPythonProbeQuotingTests(unittest.TestCase):
    """Regression: bootstrap.ps1's Test-PythonRuns probe under both PowerShell
    editions.

    Windows PowerShell 5.1 rebuilds a single command-line string when calling a
    native executable and does not escape a double quote inside an argument. A
    probe written as `-c '...write("SENTINEL")...'` therefore reaches Python as
    a truncated program, which fails to parse. The probe returns false for every
    interpreter, Find-RealPython yields null, and composition plus config
    generation both skip while the run blames a missing Python. PowerShell 7
    quotes the argument correctly, so the defect is invisible to anyone testing
    only with pwsh.

    Two layers, because each covers what the other cannot:

    - The static check runs everywhere, including the Linux and macOS CI legs
      that have no PowerShell at all.
    - The live check proves actual behavior, and runs against every installed
      edition rather than the first one found. Picking one edition is how the
      defect reached six green CI legs: this file's POWERSHELL constant prefers
      pwsh, and windows-latest ships both.
    """

    BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"
    SENTINEL = "__ANYWHERE_AGENTS_PY3__"

    def _probe_argument(self) -> str:
        """Return the -c argument text from Test-PythonRuns, quotes stripped."""
        text = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        match = re.search(
            r"-c\s+(['\"])(.*?import sys.*?sys\.stdout\.write.*?)\1",
            text,
        )
        self.assertIsNotNone(
            match, "bootstrap.ps1 must contain a Test-PythonRuns probe command"
        )
        return match.group(2)

    def _function_text(self) -> str:
        """Return the source of the Test-PythonRuns function, braces balanced."""
        text = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        start = text.index("function Test-PythonRuns")
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        self.fail("Test-PythonRuns function is not brace-balanced")

    def test_probe_argument_carries_no_double_quote(self) -> None:
        argument = self._probe_argument()
        self.assertNotIn(
            '"',
            argument,
            "the -c argument must contain no double quote: Windows PowerShell "
            "5.1 does not escape one when it rebuilds the native command line, "
            "so Python receives a truncated program. Quote the argument with "
            "double quotes and use single quotes inside it.",
        )
        self.assertIn(self.SENTINEL, argument)

    def test_no_powershell_script_nests_double_quotes_in_a_c_argument(self) -> None:
        """The same defect class, across every .ps1 in the repo.

        Scoping this check to bootstrap.ps1 is what let a second live instance
        survive in skills/implement-review/scripts/dispatch-codex.ps1, where
        the interpreter probe rejected a working Python under Windows
        PowerShell 5.1 and the reviewer dispatched without one. A defect that
        recurs across files needs a check that reads every file.
        """
        offenders = []
        for script in sorted(ROOT.rglob("*.ps1")):
            if ".agent-config" in script.parts or "build" in script.parts:
                continue
            for number, line in enumerate(
                script.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                stripped = line.strip()
                # A Python program passed as a single-quoted PowerShell string:
                # any double quote inside it reaches the native command line
                # unescaped on 5.1.
                if stripped.startswith("'") and stripped.endswith("'") and '"' in stripped:
                    if "import " in stripped or "print(" in stripped:
                        offenders.append(f"{script.relative_to(ROOT)}:{number}")
                elif re.search(r"-c\s+'[^']*\"", stripped):
                    offenders.append(f"{script.relative_to(ROOT)}:{number}")
        self.assertEqual(
            offenders, [],
            "these lines pass a double quote inside a single-quoted PowerShell "
            "string to a native executable; Windows PowerShell 5.1 drops the "
            "escaping and the program arrives truncated: " + ", ".join(offenders),
        )

    def test_probe_argument_is_python_that_emits_the_sentinel(self) -> None:
        argument = self._probe_argument()
        result = subprocess.run(
            [sys.executable, "-c", argument],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), self.SENTINEL)

    @unittest.skipUnless(sys.platform.startswith("win"), "PowerShell editions are Windows-only here")
    def test_probe_succeeds_under_every_installed_powershell(self) -> None:
        available = [(Path(p).stem.lower(), p) for p in powershell_editions_or_fail(self)]

        # Run the real function from a script file rather than through
        # -Command, so the test does not add a quoting layer the shipped code
        # does not have.
        body = self._function_text()
        interpreter = sys.executable.replace("'", "''")
        script = (
            f"{body}\n"
            f"if (Test-PythonRuns '{interpreter}') {{ 'PROBE_OK' }} else {{ 'PROBE_FAILED' }}\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "probe.ps1"
            script_path.write_text(script, encoding="utf-8")
            for name, path in available:
                with self.subTest(edition=name):
                    result = subprocess.run(
                        [path, "-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-File", str(script_path)],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        result.stdout.strip(),
                        "PROBE_OK",
                        f"Test-PythonRuns rejected a working Python 3 under "
                        f"{name}; stdout={result.stdout!r} stderr={result.stderr!r}",
                    )


def _deploy_probe_bash() -> str | None:
    """Return a bash that can run a snippet, including on Windows.

    ``_resolve_bash`` deliberately rejects the System32 and WindowsApps entries,
    and ``shutil.which`` answers with the System32 one first on a machine where
    both exist, so it can come back empty while Git for Windows is installed.
    The probe below only needs to run a self-contained function, so it may look
    in the standard install locations that PATH order hid.
    """
    if BASH:
        return BASH
    if not sys.platform.startswith("win"):
        return None
    roots = [
        os.environ.get("ProgramFiles", "C:/Program Files"),
        os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for root in roots:
        if not root:
            continue
        for parts in (("Git", "bin", "bash.exe"), ("Programs", "Git", "bin", "bash.exe")):
            candidate = Path(root).joinpath(*parts)
            if candidate.is_file():
                return str(candidate)
    return None


DEPLOY_PROBE_BASH = _deploy_probe_bash()


@unittest.skipUnless(DEPLOY_PROBE_BASH, "no usable bash for the helper-deploy probe")
class AtomicHelperIdenticalDeploySkipTests(unittest.TestCase):
    """_atomic_deploy_helper must not rename when the target already matches.

    On Windows the rename is refused while a live session holds the helper open,
    and bootstrap exits on that failure, so a deploy that would change nothing
    fails the phase and records the whole run incomplete (anywhere-agents#44).

    The skip has to respect the executable argument as well. Returning success
    while the target is not executable would leave a helper the caller asked to
    be runnable sitting there unrunnable, which is a quieter bug than the one
    being fixed.

    These run the real function text lifted out of bootstrap.sh, with mv shadowed
    so a rename records itself and then fails the way Windows fails one onto a
    held target. A skip is therefore visible as the absence of the marker, and
    not merely as a success that might have renamed successfully.
    """

    def _helper_source(self) -> str:
        text = BOOTSTRAP_SH.read_text(encoding="utf-8")
        start = text.index("_atomic_deploy_helper() {")
        end = text.index(chr(10) + "}" + chr(10), start) + 3
        return text[start:end]

    def _reports_executable(self, path: Path) -> bool:
        probe = subprocess.run(
            [DEPLOY_PROBE_BASH, "-c", 'test -x "$1"', "probe", str(path)],
            capture_output=True, text=True, check=False, timeout=60,
        )
        return probe.returncode == 0

    def _deploy(self, tmp: Path, source_bytes: bytes, target_bytes: bytes | None,
                executable: str = "false", target_executable: bool = False):
        source = tmp / "source-helper"
        source.write_bytes(source_bytes)
        target = tmp / "deployed-helper"
        if target_bytes is not None:
            target.write_bytes(target_bytes)
            if target_executable:
                os.chmod(str(target), 0o755)
        marker = tmp / "mv-was-called"
        driver = tmp / "driver.sh"
        driver.write_text(
            "set -u" + chr(10)
            + self._helper_source()
            + chr(10)
            + 'mv() { : > "$MV_MARKER"; return 1; }' + chr(10)
            + '_atomic_deploy_helper "$1" "$2" "$3"' + chr(10)
            + "exit $?" + chr(10),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["MV_MARKER"] = str(marker)
        # bash.exe invoked directly inherits the Windows PATH, which carries no
        # dirname, basename, or mkdir. Git for Windows ships two bash entry
        # points, <git>/bin/bash.exe and <git>/usr/bin/bash.exe, and the tools
        # live beside the second one either way. Take whichever candidate
        # actually holds them rather than assuming which entry point was found.
        bash_dir = Path(DEPLOY_PROBE_BASH).resolve().parent
        for candidate in (bash_dir, bash_dir.parent / "usr" / "bin"):
            if (candidate / "dirname.exe").is_file() or (candidate / "dirname").is_file():
                env["PATH"] = str(candidate) + os.pathsep + env.get("PATH", "")
                break
        result = subprocess.run(
            [DEPLOY_PROBE_BASH, str(driver), str(source), str(target), executable],
            capture_output=True, text=True, check=False, timeout=60, env=env,
        )
        return result, target, marker

    def test_identical_bytes_skip_the_rename(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            body = b"#!/usr/bin/env bash" + bytes([10]) + b"exit 0" + bytes([10])
            result, target, marker = self._deploy(tmp, body, body)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists(),
                             "an identical deploy still attempted the rename")
            self.assertEqual(target.read_bytes(), body)

    def test_changed_bytes_still_rename(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            result, target, marker = self._deploy(tmp, b"new body", b"old body")
            self.assertTrue(marker.exists(),
                            "a changed deploy skipped the rename")
            self.assertNotEqual(result.returncode, 0,
                                "a failed rename must still fail the helper")

    def test_absent_target_still_renames(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            result, target, marker = self._deploy(tmp, b"body", None)
            self.assertTrue(marker.exists(), "a first install skipped the rename")

    def test_identical_bytes_and_executable_target_skip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            body = b"#!/bin/sh" + bytes([10]) + b"exit 0" + bytes([10])
            result, target, marker = self._deploy(
                tmp, body, body, executable="true", target_executable=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists(),
                             "an identical executable deploy attempted the rename")

    def test_identical_bytes_but_non_executable_target_still_renames(self) -> None:
        """The half of the condition that a plain byte compare would miss."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            body = b"#!/bin/sh" + bytes([10]) + b"exit 0" + bytes([10])
            probe = tmp / "mode-probe"
            probe.write_bytes(body)
            os.chmod(str(probe), 0o644)
            if self._reports_executable(probe):
                self.skipTest(
                    "this filesystem reports a plain file as executable, so the "
                    "mode half of the condition cannot be observed here")
            result, target, marker = self._deploy(
                tmp, body, body, executable="true", target_executable=False)
            self.assertTrue(
                marker.exists(),
                "a deploy that had to add the executable bit skipped the rename")


class GitignoreWriterEmptyArrayTests(unittest.TestCase):
    """Regression: the gitignore writers on a repository with no .gitignore.

    Both writers bound their existing-bytes variable from an `if` used as an
    expression, whose else branch was `[byte[]]@()`. PowerShell sends the result
    of that `if` through the pipeline, an empty array enumerates to nothing, and
    the variable received $null rather than a zero-length Byte[]. The next line
    passed that null to Array.Copy as `sourceArray` and threw. Every repository
    without a .gitignore therefore aborted the run at the first ignore rule,
    with the files written so far already on disk and `completed: false` in the
    ledger. That is the documented first run for a new consumer, which is why no
    existing consumer ever reached it. See anywhere-agents#62.

    Two layers, following PowerShellPythonProbeQuotingTests above, though the
    static half here is deliberately narrower than the one in that class:

    - The source check guards recurrence of the assignment shape in the two
      writers. It does not classify arbitrary PowerShell, and the method below
      records why trying to do so was abandoned. It runs on the Linux and macOS
      legs, which have no PowerShell.
    - The live check proves behavior, against every installed edition. The bug
      is a language semantic rather than an edition quirk, but the empty-array
      unroll is exactly the kind of rule an edition could differ on, and the
      single-edition choice is what let anywhere-agents#34 ship.

    The absent file is the case that failed in production. The empty and
    no-trailing-newline files are the neighboring states of the same branch,
    where a fix that traded one boundary for another would show up.
    """

    BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"
    WRITERS = ("Add-GitignoreLine", "Add-GitignoreEntry")

    def _function_text(self, name: str) -> str:
        """Return the source of one writer, braces balanced."""
        text = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        start = text.index(f"function {name}")
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        self.fail(f"{name} is not brace-balanced")

    def test_writers_do_not_restore_the_original_if_assignment(self) -> None:
        """Guard the two writers against the exact shape that was fixed.

        This replaced a repository-wide scanner for the whole defect class. The
        scanner was removed rather than extended because it was unsound in both
        directions and could not become sound without a real PowerShell parser.
        It reported `[string]@()` and `[bool]@()`, which bind an empty string and
        False rather than null, and `$(if (...) { 1 } else { @() }; 42)`, which
        binds 42; a single such line in any .ps1 turned the whole suite red on
        correct code. It also missed `${x} =`, `$script:x =`, a backtick line
        continuation, `(@())`, `[byte []]@()`, `@();;` and `@(;)`, every one of
        which parses and binds null in both editions. Reaching soundness means
        classifying arbitrary PowerShell expressions and their destination
        conversions, which is a parser, not a test helper.

        What is left is narrow and provable: the two functions that carried the
        defect must not assign a variable from an `if` expression at all. They
        have no reason to, and the live check below covers behavior.
        """
        for writer in self.WRITERS:
            with self.subTest(writer=writer):
                self.assertNotRegex(
                    self._function_text(writer),
                    r"(?im)^\s*\$\w+\s*=\s*if\b",
                    f"{writer} assigns a variable from an `if` expression. That "
                    "is how anywhere-agents#62 happened: an empty-array branch "
                    "enumerates to nothing and binds $null. Declare the typed "
                    "variable first and assign inside a plain `if`.",
                )

    @unittest.skipUnless(sys.platform.startswith("win"),
                         "PowerShell editions are Windows-only here")
    def test_writers_append_under_every_installed_powershell(self) -> None:
        available = [(Path(p).stem.lower(), p) for p in powershell_editions_or_fail(self)]
        body = "\n".join(self._function_text(name) for name in self.WRITERS)

        # (label, pre-existing bytes or None for an absent file, expected result)
        scenarios = (
            ("absent", None, b".agent-config/\n"),
            ("empty", b"", b".agent-config/\n"),
            ("no-trailing-newline", b"*.log", b"*.log\n.agent-config/\n"),
            ("trailing-newline", b"*.log\n", b"*.log\n.agent-config/\n"),
        )

        for name, path in available:
            for label, seed, expected in scenarios:
                for writer in self.WRITERS:
                    with self.subTest(edition=name, state=label, writer=writer):
                        with tempfile.TemporaryDirectory() as tmpdir:
                            work = Path(tmpdir)
                            target = work / ".gitignore"
                            if seed is not None:
                                target.write_bytes(seed)
                            call = (
                                "Add-GitignoreEntry '^/?\\.agent-config/' '.agent-config/'"
                                if writer == "Add-GitignoreEntry"
                                else "Add-GitignoreLine '.agent-config/'"
                            )
                            # Stop turns the non-terminating method-invocation
                            # error into the abort the run actually suffered,
                            # so the regression shows up as a non-zero exit
                            # rather than a silently wrong file.
                            script = (
                                "$ErrorActionPreference = 'Stop'\n"
                                f"{body}\n"
                                f"Set-Location -LiteralPath '{str(work).replace(chr(39), chr(39) * 2)}'\n"
                                f"{call}\n"
                            )
                            script_path = work / "writer-probe.ps1"
                            script_path.write_text(script, encoding="utf-8")
                            result = subprocess.run(
                                [path, "-NoProfile", "-ExecutionPolicy", "Bypass",
                                 "-File", str(script_path)],
                                capture_output=True,
                                text=True,
                            )
                            self.assertEqual(
                                result.returncode, 0,
                                f"{writer} failed under {name} with a "
                                f"{label} .gitignore; stderr={result.stderr!r}",
                            )
                            self.assertTrue(
                                target.exists(),
                                f"{writer} wrote no .gitignore under {name} "
                                f"from the {label} state",
                            )
                            self.assertEqual(
                                target.read_bytes(), expected,
                                f"{writer} produced the wrong bytes under {name} "
                                f"from the {label} state",
                            )


class GrokAutoUpdateTests(unittest.TestCase):
    """Detached Grok CLI auto-updater (`maybe_update_grok_cli` / `Invoke-GrokAutoUpdate`)."""

    def _setup_workspace(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        work, python_path, home = _prepare_full_bootstrap_fixture(
            tmp,
            composer_rc=0,
            generator_rc=None,
        )
        stub_dir = tmp / "stub_path"
        _make_stub_git(stub_dir, "git version 2.50.0")
        _write_executable(stub_dir / "curl", "#!/bin/sh\nexit 0\n")
        return work, python_path, home, stub_dir

    def _run_bootstrap(
        self,
        work: Path,
        python_path: Path,
        home: Path,
        stub_dir: Path,
        *,
        grok_bin: Path | str | None,
        env_extra: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> tuple[subprocess.CompletedProcess, dict, float]:
        if not BASH:
            self.skipTest("bash not available")
        env = _stripped_env(stub_dir)
        env["AGENT_CONFIG_UPSTREAM"] = "example/repo"
        env["ANYWHERE_AGENTS_PYTHON"] = str(Path(sys.executable)).replace("\\", "/")
        env["ANYWHERE_AGENTS_CODEX_AUTO_UPDATE"] = "off"
        env["ANYWHERE_AGENTS_GROK_AUTO_UPDATE"] = "on"
        env["PYTHONPATH"] = str(python_path)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        state_dir = home / ".claude"
        env["ANYWHERE_AGENTS_GROK_UPDATE_DIR"] = str(state_dir)
        if grok_bin is not None:
            env["GROK_BIN"] = str(grok_bin)
        else:
            env.pop("GROK_BIN", None)
        if env_extra:
            env.update(env_extra)

        t0 = time.monotonic()
        proc = subprocess.Popen(
            [BASH, str(BOOTSTRAP_SH)],
            cwd=str(work),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        elapsed = time.monotonic() - t0
        result = subprocess.CompletedProcess(
            [BASH, str(BOOTSTRAP_SH)], proc.returncode, stdout, stderr
        )
        ledger_path = work / ".agent-config" / "last-run.json"
        ledger = (
            json.loads(ledger_path.read_text(encoding="utf-8"))
            if ledger_path.is_file()
            else {}
        )
        return result, ledger, elapsed

    @staticmethod
    def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return bool(predicate())

    @staticmethod
    def _read_lines(path: Path) -> list[str]:
        if not path.is_file():
            return []
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _write_fake_grok(path: Path, body: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_executable(path, body)
        return path

    def test_missing_grok_is_silent_noop_and_writes_no_stamp(self) -> None:
        for grok_bin_override in (None, "nonexistent-grok-binary"):
            with self.subTest(grok_bin=grok_bin_override):
                with tempfile.TemporaryDirectory(prefix="aa-grok-missing-") as td:
                    tmp = Path(td)
                    work, python_path, home, stub_dir = self._setup_workspace(tmp)
                    result, ledger, _ = self._run_bootstrap(
                        work,
                        python_path,
                        home,
                        stub_dir,
                        grok_bin=grok_bin_override,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIs(ledger.get("completed"), True, ledger)
                    state_dir = home / ".claude"
                    self.assertFalse((state_dir / "grok-update-stamp").exists())
                    self.assertFalse((state_dir / "grok-update.lock").exists())
                    self.assertFalse((state_dir / "grok-update.log").exists())
                    self.assertNotIn("grok", result.stderr.lower())

    def test_disabled_by_env_skips_update_and_writes_no_stamp(self) -> None:
        for spelling in ("off", "0", "disabled", "false", "no", " OFF ", "False"):
            with self.subTest(spelling=spelling):
                with tempfile.TemporaryDirectory(prefix="aa-grok-disabled-") as td:
                    tmp = Path(td)
                    work, python_path, home, stub_dir = self._setup_workspace(tmp)
                    invocations = tmp / "grok-invocations.log"
                    fake_grok = self._write_fake_grok(
                        stub_dir / "grok",
                        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{invocations.as_posix()}"\nexit 0\n',
                    )
                    result, ledger, _ = self._run_bootstrap(
                        work,
                        python_path,
                        home,
                        stub_dir,
                        grok_bin=fake_grok,
                        env_extra={"ANYWHERE_AGENTS_GROK_AUTO_UPDATE": spelling},
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIs(ledger.get("completed"), True, ledger)
                    state_dir = home / ".claude"
                    self.assertFalse((state_dir / "grok-update-stamp").exists())
                    self.assertFalse(invocations.exists())

    def test_failed_update_check_throttles_until_next_stamp_and_keeps_bootstrap_green(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aa-grok-fail-") as td:
            tmp = Path(td)
            work, python_path, home, stub_dir = self._setup_workspace(tmp)
            invocations = tmp / "grok-invocations.log"
            fake_grok = self._write_fake_grok(
                stub_dir / "grok",
                "#!/bin/sh\n"
                f'printf "%s\\n" "$*" >> "{invocations.as_posix()}"\n'
                'printf \'{"error":"network down"}\\n\' >&2\n'
                "exit 1\n",
            )
            state_dir = home / ".claude"
            stamp_path = state_dir / "grok-update-stamp"
            lock_dir = state_dir / "grok-update.lock"

            result1, ledger1, _ = self._run_bootstrap(
                work,
                python_path,
                home,
                stub_dir,
                grok_bin=fake_grok,
            )
            self.assertEqual(result1.returncode, 0, result1.stderr)
            self.assertIs(ledger1.get("completed"), True, ledger1)
            self.assertTrue(stamp_path.is_file())
            self.assertTrue(
                self._wait_until(
                    lambda: len(self._read_lines(invocations)) >= 1 and not lock_dir.exists()
                ),
                f"invocations={self._read_lines(invocations)} lock={lock_dir.exists()}",
            )
            self.assertEqual(self._read_lines(invocations), ["update --check --json"])

            # Immediate second run is throttled by the 24h stamp despite the check failure.
            result2, ledger2, _ = self._run_bootstrap(
                work,
                python_path,
                home,
                stub_dir,
                grok_bin=fake_grok,
            )
            self.assertEqual(result2.returncode, 0, result2.stderr)
            self.assertIs(ledger2.get("completed"), True, ledger2)
            time.sleep(0.15)
            self.assertEqual(self._read_lines(invocations), ["update --check --json"])

            # Expiring the 24h stamp allows the next bootstrap run to retry.
            expired = int(time.time()) - 90000
            stamp_path.write_text(f"{expired}\n", encoding="utf-8")
            os.utime(stamp_path, (expired, expired))
            result3, ledger3, _ = self._run_bootstrap(
                work,
                python_path,
                home,
                stub_dir,
                grok_bin=fake_grok,
            )
            self.assertEqual(result3.returncode, 0, result3.stderr)
            self.assertIs(ledger3.get("completed"), True, ledger3)
            self.assertTrue(
                self._wait_until(
                    lambda: len(self._read_lines(invocations)) >= 2 and not lock_dir.exists()
                )
            )
            self.assertEqual(
                self._read_lines(invocations),
                ["update --check --json", "update --check --json"],
            )

    @unittest.skipIf(
        os.name == "nt",
        "Grok is not exercised on Windows yet: under Git Bash the detached "
        "updater's lock timing differs and a second check can start",
    )
    def test_hanging_grok_detaches_closes_pipes_promptly_and_enforces_single_flight(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aa-grok-hang-") as td:
            tmp = Path(td)
            work, python_path, home, stub_dir = self._setup_workspace(tmp)
            invocations = tmp / "grok-invocations.log"
            started_marker = tmp / "grok-started"
            fake_grok = self._write_fake_grok(
                stub_dir / "grok",
                "#!/bin/sh\n"
                f'printf "%s\\n" "$*" >> "{invocations.as_posix()}"\n'
                f'printf "1\\n" > "{started_marker.as_posix()}"\n'
                # exec, so the timeout kills the sleeping process itself and no
                # shell of the stub's own reports the death into the check output.
                "exec /bin/sleep 30\n",
            )
            state_dir = home / ".claude"
            stamp_path = state_dir / "grok-update-stamp"
            lock_dir = state_dir / "grok-update.lock"

            # Check timeout is 2s, while fake grok sleeps for 30s.
            # _run_bootstrap uses Popen(... stdout=PIPE, stderr=PIPE).communicate(),
            # which blocks until every process holding the stdout/stderr pipe closes it.
            result1, ledger1, elapsed = self._run_bootstrap(
                work,
                python_path,
                home,
                stub_dir,
                grok_bin=fake_grok,
                env_extra={
                    "ANYWHERE_AGENTS_GROK_CHECK_TIMEOUT_SECONDS": "2",
                    "ANYWHERE_AGENTS_GROK_UPDATE_TIMEOUT_SECONDS": "2",
                },
                timeout=10.0,
            )
            self.assertEqual(result1.returncode, 0, result1.stderr)
            self.assertIs(ledger1.get("completed"), True, ledger1)
            self.assertLess(
                elapsed,
                4.5,
                f"bootstrap blocked on detached grok updater (elapsed={elapsed:.2f}s)",
            )
            self.assertTrue(
                self._wait_until(lambda: started_marker.is_file(), timeout=2.0),
                "detached grok check did not start",
            )
            self.assertTrue(lock_dir.is_dir(), "single-flight lock directory should be held")

            # While the first job holds the single-flight lock, even removing the stamp
            # must not start a second concurrent grok job.
            stamp_path.unlink(missing_ok=True)
            result2, ledger2, _ = self._run_bootstrap(
                work,
                python_path,
                home,
                stub_dir,
                grok_bin=fake_grok,
                env_extra={
                    "ANYWHERE_AGENTS_GROK_CHECK_TIMEOUT_SECONDS": "2",
                    "ANYWHERE_AGENTS_GROK_UPDATE_TIMEOUT_SECONDS": "2",
                },
                timeout=10.0,
            )
            self.assertEqual(result2.returncode, 0, result2.stderr)
            self.assertIs(ledger2.get("completed"), True, ledger2)
            self.assertEqual(self._read_lines(invocations), ["update --check --json"])

            # Bounded check timeout terminates the hung check and releases the lock.
            self.assertTrue(
                self._wait_until(lambda: not lock_dir.exists(), timeout=8.0),
                "bounded check timeout did not release single-flight lock",
            )
            self.assertEqual(self._read_lines(invocations), ["update --check --json"])
            # The watcher killed during its grace sleep is reaped quietly too.
            log_path = state_dir / "grok-update.log"
            log_text = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
            self.assertNotIn("Terminated", log_text)

    def test_update_available_invokes_update_once_and_false_skips_update(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aa-grok-avail-true-") as td:
            tmp = Path(td)
            work, python_path, home, stub_dir = self._setup_workspace(tmp)
            invocations = tmp / "grok-invocations.log"
            fake_grok = self._write_fake_grok(
                stub_dir / "grok",
                "#!/bin/sh\n"
                f'printf "%s\\n" "$*" >> "{invocations.as_posix()}"\n'
                'if [ "$1" = "update" ] && [ "$2" = "--check" ]; then\n'
                '  printf \'{"currentVersion":"1.0.0","latestVersion":"1.1.0","updateAvailable":true}\\n\'\n'
                "  exit 0\n"
                "fi\n"
                'if [ "$1" = "update" ] && [ $# -eq 1 ]; then\n'
                '  printf "Updated grok to 1.1.0\\n"\n'
                "  exit 0\n"
                "fi\n"
                "exit 2\n",
            )
            lock_dir = home / ".claude" / "grok-update.lock"
            log_file = home / ".claude" / "grok-update.log"

            result1, ledger1, _ = self._run_bootstrap(
                work,
                python_path,
                home,
                stub_dir,
                grok_bin=fake_grok,
            )
            self.assertEqual(result1.returncode, 0, result1.stderr)
            self.assertIs(ledger1.get("completed"), True, ledger1)
            self.assertTrue(
                self._wait_until(
                    lambda: len(self._read_lines(invocations)) >= 2 and not lock_dir.exists()
                ),
                f"invocations={self._read_lines(invocations)} lock={lock_dir.exists()}",
            )
            self.assertEqual(
                self._read_lines(invocations),
                ["update --check --json", "update"],
            )
            self.assertTrue(log_file.is_file())
            log_text = log_file.read_text(encoding="utf-8")
            self.assertIn("Updated grok to 1.1.0", log_text)
            # The timeout watcher is reaped quietly, not reported as a killed job.
            self.assertNotIn("Terminated", log_text)

            # Subsequent bootstrap within 24h does not invoke grok again.
            result2, _, _ = self._run_bootstrap(
                work,
                python_path,
                home,
                stub_dir,
                grok_bin=fake_grok,
            )
            self.assertEqual(result2.returncode, 0, result2.stderr)
            time.sleep(0.15)
            self.assertEqual(
                self._read_lines(invocations),
                ["update --check --json", "update"],
            )

        with tempfile.TemporaryDirectory(prefix="aa-grok-avail-false-") as td:
            tmp = Path(td)
            work, python_path, home, stub_dir = self._setup_workspace(tmp)
            invocations = tmp / "grok-invocations.log"
            fake_grok = self._write_fake_grok(
                stub_dir / "grok",
                "#!/bin/sh\n"
                f'printf "%s\\n" "$*" >> "{invocations.as_posix()}"\n'
                'printf \'{"currentVersion":"1.1.0","latestVersion":"1.1.0","updateAvailable":false}\\n\'\n'
                "exit 0\n",
            )
            lock_dir = home / ".claude" / "grok-update.lock"
            result, ledger, _ = self._run_bootstrap(
                work,
                python_path,
                home,
                stub_dir,
                grok_bin=fake_grok,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIs(ledger.get("completed"), True, ledger)
            self.assertTrue(
                self._wait_until(
                    lambda: len(self._read_lines(invocations)) >= 1 and not lock_dir.exists()
                )
            )
            self.assertEqual(self._read_lines(invocations), ["update --check --json"])
            log_text = (home / ".claude" / "grok-update.log").read_text(encoding="utf-8")
            self.assertIn('"updateAvailable":false', log_text)
            self.assertNotIn("Terminated", log_text)

    def test_discovers_grok_under_home_dot_grok_bin_when_not_on_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aa-grok-homebin-") as td:
            tmp = Path(td)
            work, python_path, home, stub_dir = self._setup_workspace(tmp)
            invocations = tmp / "grok-invocations.log"
            home_grok = self._write_fake_grok(
                home / ".grok" / "bin" / "grok",
                "#!/bin/sh\n"
                f'printf "%s\\n" "$*" >> "{invocations.as_posix()}"\n'
                'printf \'{"updateAvailable":false}\\n\'\n'
                "exit 0\n",
            )
            self.assertTrue(home_grok.is_file())
            lock_dir = home / ".claude" / "grok-update.lock"
            result, ledger, _ = self._run_bootstrap(
                work,
                python_path,
                home,
                stub_dir,
                grok_bin=None,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIs(ledger.get("completed"), True, ledger)
            self.assertTrue(
                self._wait_until(
                    lambda: len(self._read_lines(invocations)) >= 1 and not lock_dir.exists()
                )
            )
            self.assertEqual(self._read_lines(invocations), ["update --check --json"])

    def test_powershell_mechanical_twin_contract(self) -> None:
        ps1 = BOOTSTRAP_PS1.read_text(encoding="utf-8")
        sh = BOOTSTRAP_SH.read_text(encoding="utf-8")

        for token in (
            "function Invoke-GrokAutoUpdate",
            "ANYWHERE_AGENTS_GROK_AUTO_UPDATE",
            "grok-update-stamp",
            "grok-update.lock",
            "grok-update.log",
            "update --check --json",
            "updateAvailable",
            "Start-Process",
            "-WindowStyle Hidden",
        ):
            self.assertIn(token, ps1)

        # Placed outside the bootstrap ledger finalize path and before banner rendering.
        ledger_pos = ps1.rfind("Write-Ledger 'finalize'")
        grok_call_pos = ps1.rfind("Invoke-GrokAutoUpdate")
        banner_pos = ps1.rfind("Invoke-BannerRender 0")
        self.assertGreater(ledger_pos, 0)
        self.assertGreater(grok_call_pos, ledger_pos)
        self.assertGreater(banner_pos, grok_call_pos)

        sh_ledger_pos = sh.rfind("_ledger_write finalize true")
        sh_grok_pos = sh.rfind("maybe_update_grok_cli")
        self.assertGreater(sh_ledger_pos, 0)
        self.assertGreater(sh_grok_pos, sh_ledger_pos)


if __name__ == "__main__":
    unittest.main()
