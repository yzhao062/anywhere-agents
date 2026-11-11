"""PATH-resolution fixture tests for dispatch-codex.

Real-world codex install variants across platforms:

  Windows
  -------
  1. npm / pnpm / yarn / volta:
       <bin>\\codex         (extensionless bash shim; not a Win32 PE)
       <bin>\\codex.cmd     (the actually runnable wrapper)
     The dispatcher MUST pick the .cmd, otherwise CreateProcess fails
     with "%1 is not a valid Win32 application". This is the regression
     guard for the bug observed on 2026-05-15 in PycharmProjects/random.

  2. winget / scoop / manual extraction:
       <bin>\\codex.cmd     (or codex.exe; only one file)
     Trivially correct, included as smoke coverage for non-npm installs.

  3. Microsoft Store App Execution Alias:
       %LOCALAPPDATA%\\Microsoft\\WindowsApps\\codex.exe
     A 0-byte stub that pops the Store rather than running codex. The
     resolver must filter \\WindowsApps\\ paths so a real install wins.

  4. CODEX_BIN absolute-path override:
     Bypasses PATH lookup entirely; honored verbatim.

  POSIX
  -----
  5. Extensionless shebang script (npm / pnpm / yarn / brew / manual):
     dispatch-codex.sh uses bash's bare exec lookup. No PathExt layer to
     test; one sanity case confirms the basic shape still works.

The test layouts use small wrapper scripts that each tag themselves with
a unique identifier before invoking a shared Python mock; the test then
asserts the tag matches the install variant that should have won.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from functools import wraps
from pathlib import Path

# tests/ is on sys.path under `unittest discover -s tests` but not under
# `python -m unittest tests.<module>`, which validate.yml uses for the
# Sentinel redaction smoke. Put it there before the sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "implement-review" / "scripts"
DISPATCH_PS1 = SCRIPTS_DIR / "dispatch-codex.ps1"
DISPATCH_SH = SCRIPTS_DIR / "dispatch-codex.sh"


BASH = shutil.which("bash")
PS_SHELLS = [p for p in (shutil.which("powershell"), shutil.which("pwsh")) if p]
PS_SHELL = PS_SHELLS[0] if PS_SHELLS else None


def _for_each_powershell(test_method):
    @wraps(test_method)
    def wrapper(self):
        for ps_shell in PS_SHELLS:
            with self.subTest(edition=Path(ps_shell).stem):
                self._ps_shell = ps_shell
                test_method(self)

    return wrapper


def _temp_dir():
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()


# Mock codex body: writes stdin verbatim and exits 0. The wrapper that
# invoked it (codex.cmd / codex.exe-shim / extensionless shim) is
# responsible for tagging itself in <log>/variant before exec'ing this.
MOCK_CODEX_PY = r'''import os, sys
log_dir = os.environ["MOCK_CODEX_LOG"]
os.makedirs(log_dir, exist_ok=True)
with open(os.path.join(log_dir, "stdin.bin"), "wb") as f:
    f.write(sys.stdin.buffer.read())
sys.exit(0)
'''


def _drop_python_mock(tmpdir: Path) -> Path:
    p = tmpdir / "mock_codex.py"
    p.write_text(MOCK_CODEX_PY, encoding="utf-8")
    return p


def _drop_cmd_wrapper(
    tmpdir: Path, name: str, mock_py: Path, tag: str
) -> Path:
    """Write a .cmd file that records `tag` then runs the Python mock.

    The .cmd uses %* to forward dispatch args (e.g., `exec -`); stdin is
    inherited via cmd's `< prompt` redirection in the dispatcher helper.
    """
    path = tmpdir / name
    path.write_text(
        "@echo off\r\n"
        f'> "%MOCK_CODEX_LOG%\\variant" echo {tag}\r\n'
        f'"{sys.executable}" "{mock_py}" %*\r\n',
        encoding="utf-8",
    )
    return path


def _drop_extensionless_shim(
    tmpdir: Path, name: str, mock_py: Path, tag: str
) -> Path:
    """Write a bash-shebang extensionless file at tmpdir/<name>.

    On Windows this file is NOT a valid Win32 executable -- CreateProcess
    would reject it ("%1 is not a valid Win32 application"). The point of
    the test is to confirm the resolver does not pick it.
    """
    path = tmpdir / name
    path.write_text(
        "#!/usr/bin/env bash\n"
        'echo "' + tag + '" > "$MOCK_CODEX_LOG/variant"\n'
        f'exec "{sys.executable}" "{mock_py}" "$@"\n',
        encoding="utf-8",
    )
    if not sys.platform.startswith("win"):
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _read_variant(log_dir: Path) -> str:
    f = log_dir / "variant"
    if not f.is_file():
        return ""
    return f.read_text(encoding="utf-8").strip()


def _system32() -> str:
    """Return path to Windows System32 (or its 64-bit alias on WOW64).

    The dispatch.ps1 cmd-helper invocation needs cmd.exe to be findable;
    keeping System32 in PATH guarantees it. SystemRoot is set on every
    Windows session.
    """
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(sysroot, "System32")


class _DispatchInvokerMixin:
    """Helpers to invoke dispatch with a controlled PATH and no CODEX_BIN."""

    SHELL_KIND: str = ""

    def _build_cmd(self, prompt_file: Path) -> list[str]:
        if self.SHELL_KIND == "powershell":
            ps_shell = getattr(self, "_ps_shell", PS_SHELL)
            return [
                ps_shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(DISPATCH_PS1),
                "--prompt-file", str(prompt_file),
                "--round", "1",
                "--expected-review-file", "Review-Codex.md",
            ]
        if self.SHELL_KIND == "bash":
            return [
                BASH, str(DISPATCH_SH),
                "--prompt-file", str(prompt_file),
                "--round", "1",
                "--expected-review-file", "Review-Codex.md",
            ]
        raise AssertionError(f"unknown SHELL_KIND: {self.SHELL_KIND!r}")

    def _run_dispatch(
        self,
        tmpdir: Path,
        path_prefix_dir: Path,
        prompt_file: Path,
        log_dir: Path,
        codex_bin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        # The controlled PATH below holds no Python, and the dispatcher needs
        # one to read the model registry.
        env["ANYWHERE_AGENTS_PYTHON"] = sys.executable
        env.pop("CODEX_BIN", None)
        if codex_bin is not None:
            env["CODEX_BIN"] = codex_bin

        if sys.platform.startswith("win"):
            path = f"{path_prefix_dir};{_system32()}"
            # Add git's directory so descendant processes that probe for git
            # (the spawn-child regression test does this) can find it. Cheap
            # and harmless for fixture tests that do not invoke git: git is
            # already a documented expectation in the dispatch contract --
            # codex itself spawns git/grep subprocesses on real reviews.
            git = shutil.which("git")
            if git:
                path += ";" + str(Path(git).parent)
            env["Path"] = path
            env["PATH"] = path
        else:
            # Keep /usr/bin so bash can find stat, head, tail, etc.
            env["PATH"] = f"{path_prefix_dir}:/usr/bin:/bin"

        env["MOCK_CODEX_LOG"] = str(log_dir)
        env["IMPLEMENT_REVIEW_ORCHESTRATOR"] = "claude"
        env.pop("CODEX_THREAD_ID", None)
        env.pop("CODEX_SESSION_ID", None)
        env["TMPDIR"] = str(tmpdir)
        env["TEMP"] = str(tmpdir)
        env["TMP"] = str(tmpdir)
        env.setdefault("STALL_POLL_INTERVAL_SECONDS", "1")
        env.setdefault("STALL_THRESHOLD_SECONDS", "999999")

        return subprocess.run(
            self._build_cmd(prompt_file),
            cwd=str(tmpdir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


@unittest.skipUnless(
    sys.platform.startswith("win"),
    "Windows PowerShell availability is a Windows-only contract.",
)
class WindowsPowerShellAvailabilityTests(unittest.TestCase):
    def test_installed_editions_include_windows_powershell_5(self) -> None:
        self.assertIsNotNone(
            shutil.which("powershell"),
            "powershell.exe is missing from PATH on Windows; every supported "
            "Windows environment provides it through System32.",
        )
        majors = set()
        for ps_shell in PS_SHELLS:
            with self.subTest(edition=Path(ps_shell).stem):
                result = subprocess.run(
                    [ps_shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-Command",
                     "Write-Output $PSVersionTable.PSVersion.Major"],
                    capture_output=True, text=True, check=True, timeout=30,
                )
                majors.add(int(result.stdout.strip()))
        self.assertIn(
            5, majors,
            f"Windows PowerShell 5.1 was not exercised; saw majors {majors}",
        )


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win"),
    "Windows PATH-resolution tests target dispatch-codex.ps1 (Windows-only "
    "behavior: PathExt resolution + WindowsApps filtering + CreateProcess "
    "vs cmd-shell semantics).",
)
class WindowsPathResolutionTests(_DispatchInvokerMixin, unittest.TestCase):
    SHELL_KIND = "powershell"

    def _setup_layout(self, tmpdir: Path):
        bin_dir = tmpdir / "bin"
        bin_dir.mkdir()
        log_dir = tmpdir / "log"
        log_dir.mkdir()
        mock_py = _drop_python_mock(tmpdir)
        prompt = tmpdir / "prompt.txt"
        prompt.write_text("PATH resolution test prompt body.\n", encoding="utf-8")
        return bin_dir, log_dir, mock_py, prompt

    # --- npm/pnpm/yarn dual-file trap regression -----------------------------

    @_for_each_powershell
    def test_npm_trap_dual_file_picks_cmd(self) -> None:
        """Regression: extensionless shim + codex.cmd in same dir; .cmd wins."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            bin_dir, log_dir, mock_py, prompt = self._setup_layout(tmpdir)
            _drop_cmd_wrapper(bin_dir, "codex.cmd", mock_py, "cmd-wrapper")
            _drop_extensionless_shim(bin_dir, "codex", mock_py, "extensionless-shim")

            result = self._run_dispatch(tmpdir, bin_dir, prompt, log_dir)

            self.assertEqual(
                result.returncode, 0,
                f"dispatch failed under npm-trap layout\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
            )
            self.assertEqual(
                _read_variant(log_dir), "cmd-wrapper",
                "Resolver must prefer codex.cmd over the extensionless "
                "bash shim under npm/pnpm/yarn dual-file installs",
            )

    # --- single-file installs (winget / scoop / manual) ----------------------

    @_for_each_powershell
    def test_single_cmd_install(self) -> None:
        """winget/scoop/manual install with only codex.cmd."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            bin_dir, log_dir, mock_py, prompt = self._setup_layout(tmpdir)
            _drop_cmd_wrapper(bin_dir, "codex.cmd", mock_py, "single-cmd")

            result = self._run_dispatch(tmpdir, bin_dir, prompt, log_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(_read_variant(log_dir), "single-cmd")

    @_for_each_powershell
    def test_single_exe_like_install(self) -> None:
        """winget/scoop produce codex.exe as a real PE; we approximate it with .cmd.

        We cannot fabricate a real .exe in a portable way (a renamed .cmd
        is rejected by CreateProcess for lacking a PE header). The
        resolver path for .cmd vs .exe is identical -- both are PathExt
        entries with a non-empty .Extension -- so .cmd serves as a stand-in.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            bin_dir, log_dir, mock_py, prompt = self._setup_layout(tmpdir)
            # Drop only one entry; resolver returns it; cmd /c invokes it.
            _drop_cmd_wrapper(bin_dir, "codex.cmd", mock_py, "winget-style")

            result = self._run_dispatch(tmpdir, bin_dir, prompt, log_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(_read_variant(log_dir), "winget-style")

    # --- multi-install PATH order (Round 1 Codex regression) ----------------

    @_for_each_powershell
    def test_multiple_extension_installs_first_path_entry_wins(self) -> None:
        """Two codex.cmd entries in PATH; the FIRST PATH dir must win.

        Regression catch: Windows PowerShell 5.1's Sort-Object with a
        custom Expression scriptblock is NOT stable -- it reordered
        Get-Command output, so the dispatcher could pick the user's
        SECOND codex install over their first. The explicit two-pass
        filter in dispatch-codex.ps1 (Where-Object { $_.Extension }
        | Select-First) preserves PATH order on every PS version.

        Verified live on 2026-05-15: with PATH=A;B and codex.cmd in
        both, the old Sort-Object pipeline returned B on PS 5.1.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            log_dir = tmpdir / "log"
            log_dir.mkdir()
            mock_py = _drop_python_mock(tmpdir)
            prompt = tmpdir / "prompt.txt"
            prompt.write_text("multi-install test.\n", encoding="utf-8")

            dir_a = tmpdir / "first-install"
            dir_b = tmpdir / "second-install"
            dir_a.mkdir()
            dir_b.mkdir()
            _drop_cmd_wrapper(dir_a, "codex.cmd", mock_py, "first-install")
            _drop_cmd_wrapper(dir_b, "codex.cmd", mock_py, "second-install")

            path_value = f"{dir_a};{dir_b};{_system32()}"
            git = shutil.which("git")
            if git:
                path_value += ";" + str(Path(git).parent)
            env = os.environ.copy()
            env["ANYWHERE_AGENTS_PYTHON"] = sys.executable
            env.pop("CODEX_BIN", None)
            env["Path"] = path_value
            env["PATH"] = path_value
            env["MOCK_CODEX_LOG"] = str(log_dir)
            env["IMPLEMENT_REVIEW_ORCHESTRATOR"] = "claude"
            env.pop("CODEX_THREAD_ID", None)
            env.pop("CODEX_SESSION_ID", None)
            env["TMPDIR"] = str(tmpdir)
            env["TEMP"] = str(tmpdir)
            env["TMP"] = str(tmpdir)
            env.setdefault("STALL_POLL_INTERVAL_SECONDS", "1")
            env.setdefault("STALL_THRESHOLD_SECONDS", "999999")

            result = subprocess.run(
                self._build_cmd(prompt),
                cwd=str(tmpdir),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                _read_variant(log_dir), "first-install",
                "Resolver must pick the FIRST PATH entry's codex.cmd, not "
                "the second. Old Sort-Object pipeline failed this on PS 5.1.",
            )

    # --- WindowsApps filtering ----------------------------------------------

    @_for_each_powershell
    def test_windowsapps_entry_filtered_when_real_install_present(self) -> None:
        """A `\\WindowsApps\\` codex.cmd is skipped in favor of a real bin dir.

        Simulates: user has both the Microsoft Store App Execution Alias
        AND a real install. The Store alias normally appears FIRST on PATH
        and would otherwise win.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            log_dir = tmpdir / "log"
            log_dir.mkdir()
            mock_py = _drop_python_mock(tmpdir)
            prompt = tmpdir / "prompt.txt"
            prompt.write_text("WindowsApps filter test.\n", encoding="utf-8")

            fake_store = tmpdir / "WindowsApps"
            fake_store.mkdir()
            _drop_cmd_wrapper(fake_store, "codex.cmd", mock_py, "store-alias")

            real_bin = tmpdir / "real-bin"
            real_bin.mkdir()
            _drop_cmd_wrapper(real_bin, "codex.cmd", mock_py, "real-install")

            # Store alias listed FIRST on PATH; real install second.
            path_value = f"{fake_store};{real_bin};{_system32()}"
            env = os.environ.copy()
            env["ANYWHERE_AGENTS_PYTHON"] = sys.executable
            env.pop("CODEX_BIN", None)
            env["Path"] = path_value
            env["PATH"] = path_value
            env["MOCK_CODEX_LOG"] = str(log_dir)
            env["IMPLEMENT_REVIEW_ORCHESTRATOR"] = "claude"
            env.pop("CODEX_THREAD_ID", None)
            env.pop("CODEX_SESSION_ID", None)
            env["TMPDIR"] = str(tmpdir)
            env["TEMP"] = str(tmpdir)
            env["TMP"] = str(tmpdir)
            env.setdefault("STALL_POLL_INTERVAL_SECONDS", "1")
            env.setdefault("STALL_THRESHOLD_SECONDS", "999999")

            result = subprocess.run(
                self._build_cmd(prompt),
                cwd=str(tmpdir),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                _read_variant(log_dir), "real-install",
                "WindowsApps entry must be filtered so the real install wins, "
                "even when WindowsApps is FIRST on PATH",
            )

    # --- CODEX_BIN absolute-path override -----------------------------------

    @_for_each_powershell
    def test_codex_bin_absolute_path_bypasses_path_lookup(self) -> None:
        """Explicit absolute path in CODEX_BIN runs verbatim, ignoring PATH.

        This is the lane the existing dispatch contract tests already use
        (they set CODEX_BIN to the mock shim's full path). Re-asserted here
        so a future resolver rewrite cannot silently break that contract.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            log_dir = tmpdir / "log"
            log_dir.mkdir()
            mock_py = _drop_python_mock(tmpdir)
            prompt = tmpdir / "prompt.txt"
            prompt.write_text("Explicit CODEX_BIN test.\n", encoding="utf-8")

            override_bin = tmpdir / "explicit"
            override_bin.mkdir()
            cmd = _drop_cmd_wrapper(
                override_bin, "codex.cmd", mock_py, "explicit-override"
            )

            # PATH does NOT contain the override dir; CODEX_BIN points to it.
            other_bin = tmpdir / "other"
            other_bin.mkdir()
            _drop_cmd_wrapper(other_bin, "codex.cmd", mock_py, "should-not-run")

            result = self._run_dispatch(
                tmpdir, other_bin, prompt, log_dir, codex_bin=str(cmd)
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                _read_variant(log_dir), "explicit-override",
                "CODEX_BIN absolute path must run the named file, not whatever "
                "PATH would resolve to",
            )

    # --- cmd-helper percent-expansion safety (Round 1 Codex regression) ----

    @_for_each_powershell
    def test_state_dir_with_percent_in_path_dispatches_cleanly(self) -> None:
        """A state-dir path containing `%BAD%` must not cmd-env-expand.

        Regression catch: the cmd helper interpolated paths into a
        batch-script body. Without escaping, cmd treats `%BAD%` as an
        env-var reference and rewrites the redirection target. The
        dispatcher escapes `%` to `%%` for every interpolated path so
        cmd reads the literal percent.

        Note: we cannot put `%` into a Windows tmpdir name reliably
        (some Windows APIs object), so this test forces the state-dir
        layout by routing the prompt file through a path that contains
        a literal `%` segment. The dispatcher escapes all three
        interpolated paths uniformly, so a single percent-bearing path
        exercises the escape logic.
        """
        with _temp_dir() as td:
            tmpdir = Path(td)
            bin_dir, log_dir, mock_py, _ = self._setup_layout(tmpdir)
            _drop_cmd_wrapper(bin_dir, "codex.cmd", mock_py, "percent-test")

            # Place the prompt file in a subdir whose name contains `%`.
            # Codex's repro confirmed cmd expands `%BAD%` to empty in
            # batch context, so the redirection target gets corrupted.
            weird = tmpdir / "has%percent%dirs"
            weird.mkdir()
            prompt = weird / "prompt.txt"
            prompt.write_text(
                "percent-in-path test prompt.\n", encoding="utf-8"
            )

            result = self._run_dispatch(tmpdir, bin_dir, prompt, log_dir)

            self.assertEqual(
                result.returncode, 0,
                f"dispatch failed on percent-bearing path\nSTDOUT:\n"
                f"{result.stdout}\nSTDERR:\n{result.stderr}",
            )
            self.assertEqual(
                _read_variant(log_dir), "percent-test",
                "Mock did not run -- cmd-helper percent expansion likely "
                "corrupted the redirection",
            )
            stdin_bytes = (log_dir / "stdin.bin").read_bytes()
            self.assertIn(
                b"percent-in-path test prompt.",
                stdin_bytes,
                "Prompt bytes were lost or corrupted through the "
                "percent-bearing path",
            )

    # --- byte-fidelity through the resolved variant -------------------------

    @_for_each_powershell
    def test_npm_trap_preserves_prompt_bytes(self) -> None:
        """Byte-parity invariant survives the resolver + cmd-helper path."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            bin_dir, log_dir, mock_py, _ = self._setup_layout(tmpdir)
            _drop_cmd_wrapper(bin_dir, "codex.cmd", mock_py, "cmd-wrapper")
            _drop_extensionless_shim(bin_dir, "codex", mock_py, "extensionless-shim")

            body = (
                b"resolver byte test: snowman \xe2\x98\x83 "
                b"crlf line\r\nlf line\nend\n"
            )
            prompt = tmpdir / "prompt.bin"
            prompt.write_bytes(body)

            result = self._run_dispatch(tmpdir, bin_dir, prompt, log_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(_read_variant(log_dir), "cmd-wrapper")
            self.assertEqual((log_dir / "stdin.bin").read_bytes(), body)


# =============================================================================
# Live-PATH coverage: same resolver block, run against the maintainer's
# actual codex install. Confirms the user's real machine satisfies the
# contract, not just mocked fixtures. Skipped when codex is not installed.
# =============================================================================


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win") and shutil.which("codex"),
    "Live-PATH resolver test: requires codex on PATH (Windows real-repo "
    "coverage). Skipped on machines without codex installed.",
)
class LiveWindowsCodexResolverTest(unittest.TestCase):
    """Real-repo regression coverage: maintainer's live codex install.

    The fixture tests in WindowsPathResolutionTests construct mocked install
    layouts. This class runs the same resolver block dispatch-codex.ps1 uses
    against the live $env:Path, confirming the resolved path is something
    CreateProcess + cmd /c can actually run. Catches: future regressions
    where the resolver stops being PathExt-aware; WindowsApps Store stubs
    being picked over real installs on this specific machine. Cheap (no
    codex invocation), and orthogonal to the fixture tests.
    """

    @_for_each_powershell
    def test_live_resolver_returns_runnable_codex_path(self) -> None:
        # Replicates the two-pass resolver in dispatch-codex.ps1 (the
        # `$candidates | Where-Object { $_.Extension } | Select-First`
        # selection with extensionless-shim fallback). If the dispatcher
        # resolver changes, this snippet must be updated in lock-step --
        # an earlier revision of this test still embedded the old
        # Sort-Object pipeline, which Round 2 review (2026-05-16) caught
        # because Windows PowerShell 5.1's Sort-Object reordered results.
        resolver_ps = (
            "$candidates = @(Get-Command -Name 'codex' "
            "-CommandType Application -ErrorAction SilentlyContinue | "
            "Where-Object { "
            "$src = [string]$_.Source; "
            "$src -and ($src -notlike '*\\WindowsApps\\*') }); "
            "$resolved = $candidates | Where-Object { $_.Extension } | "
            "Select-Object -First 1; "
            "if (-not $resolved) { "
            "$resolved = $candidates | Select-Object -First 1 }; "
            "if ($resolved) { Write-Output $resolved.Source } "
            "else { Write-Output '' }"
        )
        result = subprocess.run(
            [self._ps_shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", resolver_ps],
            capture_output=True, text=True, check=True, timeout=30,
        )
        path = result.stdout.strip()
        self.assertTrue(
            path,
            "Resolver returned no path on live $env:Path; dispatch would "
            "fall back to the literal string 'codex' and let cmd re-resolve. "
            "Expected: the user's actual codex install resolves cleanly.",
        )
        path_obj = Path(path)
        self.assertTrue(
            path_obj.is_file(),
            f"Resolver returned nonexistent path: {path!r}",
        )
        ext = path_obj.suffix.lower()
        self.assertIn(
            ext, [".cmd", ".exe", ".bat", ".com", ".ps1"],
            f"Resolved codex {path!r} has extension {ext!r}, which CreateProcess "
            "and cmd /c cannot launch. This is the npm-dual-file-trap class: "
            "the resolver picked the extensionless bash shim instead of the "
            ".cmd wrapper. Regression of the 2026-05-15 fix.",
        )
        self.assertNotIn(
            "\\WindowsApps\\", path,
            f"Resolved codex {path!r} is under \\WindowsApps\\ (Microsoft Store "
            "App Execution Alias). These are 0-byte stubs that pop the Store; "
            "the resolver was supposed to filter them out.",
        )


# =============================================================================
# Child-spawn coverage: codex spawns subprocesses (git, grep, etc.) on
# every real review. Confirms the dispatch pipeline preserves the user's
# logon-session token so descendants can call CreateProcess.
# =============================================================================


# Mock codex that tries to spawn a git subprocess and reports the result.
# Regression target: CreateProcessAsUserW (Start-Process -RedirectStandardInput)
# strips the logon-session token, so the descendant subprocess.run() fails
# with Windows error 1312 "no logon session". cmd /c uses CreateProcess
# directly, inheriting the token cleanly so this test passes.
SPAWN_CHILD_MOCK_PY = r'''import json, os, subprocess, sys
log_dir = os.environ["MOCK_CODEX_LOG"]
os.makedirs(log_dir, exist_ok=True)
# Drain stdin so dispatch sees a closed pipe like real codex.
sys.stdin.buffer.read()
try:
    cp = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, timeout=15,
    )
    record = {
        "spawn_ok": True,
        "returncode": cp.returncode,
        "stdout_starts_with_git_version": cp.stdout.startswith("git version"),
        "stderr": cp.stderr[:200],
    }
except OSError as exc:
    record = {
        "spawn_ok": False,
        "winerror": getattr(exc, "winerror", None),
        "errno": exc.errno,
        "strerror": str(exc),
    }
with open(os.path.join(log_dir, "spawn-result.json"), "w", encoding="utf-8") as f:
    json.dump(record, f)
sys.exit(0)
'''


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win") and shutil.which("git"),
    "Child-spawn regression test requires Windows + git on PATH (codex's "
    "real subprocesses are git/grep; git is the cheapest universal probe).",
)
class WindowsChildSpawnRegressionTest(_DispatchInvokerMixin, unittest.TestCase):
    """Regression guard against CreateProcessAsUserW 1312.

    Background (2026-05-15): dispatch-codex.ps1 originally used
        Start-Process -FilePath codex -RedirectStandardInput <file>
    which routes through CreateProcessAsUserW. That call strips the logon-
    session token, so any descendant of codex (its git/grep subprocess,
    typically) fails to spawn with Windows error 1312 "no logon session".
    Manifested as opaque codex internal errors during real /implement-review
    runs in PycharmProjects/random.

    The fix landed in 2026-05-15: spawn via a transient .cmd helper and
    cmd /c, which uses plain CreateProcess and inherits the token cleanly.

    This test installs a mock codex that tries to spawn `git --version` and
    asserts the subprocess returned 0 with the expected stdout. If a future
    refactor regresses back to Start-Process, this test fails.
    """
    SHELL_KIND = "powershell"

    @_for_each_powershell
    def test_mock_codex_can_spawn_git_subprocess(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            bin_dir = tmpdir / "bin"
            bin_dir.mkdir()
            log_dir = tmpdir / "log"
            log_dir.mkdir()

            mock_py = tmpdir / "spawn_mock.py"
            mock_py.write_text(SPAWN_CHILD_MOCK_PY, encoding="utf-8")

            cmd_path = bin_dir / "codex.cmd"
            cmd_path.write_text(
                "@echo off\r\n"
                f'"{sys.executable}" "{mock_py}" %*\r\n',
                encoding="utf-8",
            )

            prompt = tmpdir / "prompt.txt"
            prompt.write_text("spawn-child test prompt.\n", encoding="utf-8")

            result = self._run_dispatch(tmpdir, bin_dir, prompt, log_dir)

            self.assertEqual(
                result.returncode, 0,
                f"dispatch returned nonzero\nSTDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}",
            )
            spawn_file = log_dir / "spawn-result.json"
            self.assertTrue(
                spawn_file.is_file(),
                "Mock codex did not write spawn-result.json -- either it was "
                "not reached, or it crashed before writing. Dispatch tail "
                f"may have details:\n{result.stderr}",
            )
            import json as _json
            record = _json.loads(spawn_file.read_text(encoding="utf-8"))
            self.assertTrue(
                record.get("spawn_ok"),
                f"codex descendant failed to spawn git subprocess: {record}. "
                "This is the CreateProcessAsUserW 1312 class -- dispatch must "
                "use cmd /c (not Start-Process -RedirectStandardInput) so the "
                "logon-session token survives down to codex's children.",
            )
            self.assertEqual(record["returncode"], 0)
            self.assertTrue(
                record["stdout_starts_with_git_version"],
                f"git --version stdout did not start with 'git version': {record}",
            )


@unittest.skipIf(
    sys.platform.startswith("win"),
    "POSIX shebang resolution test targets dispatch-codex.sh; the bash "
    "path-translation issue documented in test_dispatch_codex.py applies "
    "to running .sh from Python on Windows, so this test is POSIX-only.",
)
@unittest.skipUnless(BASH, "bash not on PATH")
class PosixShebangResolutionTests(_DispatchInvokerMixin, unittest.TestCase):
    """POSIX: dispatch-codex.sh resolves an extensionless shebang `codex`.

    The .sh dispatcher does not enumerate PATH itself -- it relies on
    bash's exec lookup. This test confirms the basic POSIX install shape
    (extensionless `codex` shebang script with execute bit) still works
    once the resolver layer landed in the .ps1 sibling. Coverage that
    the .sh side did not silently regress during the .ps1 rewrite.
    """
    SHELL_KIND = "bash"

    def test_extensionless_shebang_codex_runs(self) -> None:
        with _temp_dir() as td:
            tmpdir = Path(td)
            bin_dir = tmpdir / "bin"
            bin_dir.mkdir()
            log_dir = tmpdir / "log"
            log_dir.mkdir()
            mock_py = _drop_python_mock(tmpdir)
            _drop_extensionless_shim(bin_dir, "codex", mock_py, "posix-shebang")

            prompt = tmpdir / "prompt.txt"
            prompt.write_text("POSIX shebang resolution test.\n", encoding="utf-8")

            result = self._run_dispatch(tmpdir, bin_dir, prompt, log_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(_read_variant(log_dir), "posix-shebang")


if __name__ == "__main__":
    unittest.main()
