"""Tests for v0.5.8 Item 4: Git Bash on Windows fetches .sh instead of .ps1.

Covers:
- Linux/macOS → bootstrap.sh
- Windows + BASH_VERSION set → bootstrap.sh
- Windows + MSYSTEM=MINGW64 → bootstrap.sh
- Windows + MSYSTEM=MINGW32 → bootstrap.sh
- Windows + neither set → bootstrap.ps1 (fallback)
- Windows + both BASH_VERSION and MSYSTEM set → bootstrap.sh (env vars win)
- Windows + MSYSTEM=MSYS (not MINGW prefix) → bootstrap.ps1
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "pypi"))
sys.path.insert(0, str(ROOT / "scripts"))

from anywhere_agents import cli  # noqa: E402


class TestDetectWindowsShell(unittest.TestCase):
    """Unit tests for _detect_windows_shell()."""

    def _call(self, env_overrides: dict[str, str]) -> str:
        """Call _detect_windows_shell with a clean environment."""
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("BASH_VERSION", "MSYSTEM")}
        clean_env.update(env_overrides)
        with patch.dict(os.environ, clean_env, clear=True):
            return cli._detect_windows_shell()

    def test_bash_version_set_returns_bash(self) -> None:
        result = self._call({"BASH_VERSION": "5.1.0(1)-release"})
        self.assertEqual(result, "bash")

    def test_bash_version_empty_string_not_bash(self) -> None:
        # Empty string is falsy; should not trigger bash detection
        result = self._call({"BASH_VERSION": "", "MSYSTEM": ""})
        self.assertEqual(result, "powershell")

    def test_msystem_mingw64_returns_bash(self) -> None:
        result = self._call({"MSYSTEM": "MINGW64"})
        self.assertEqual(result, "bash")

    def test_msystem_mingw32_returns_bash(self) -> None:
        result = self._call({"MSYSTEM": "MINGW32"})
        self.assertEqual(result, "bash")

    def test_msystem_mingw_prefix_lower_returns_bash(self) -> None:
        # Git for Windows occasionally uses mixed case
        result = self._call({"MSYSTEM": "mingw64"})
        self.assertEqual(result, "bash")

    def test_msystem_msys_is_not_mingw(self) -> None:
        # MSYS without MINGW → not git-bash terminal context
        result = self._call({"MSYSTEM": "MSYS"})
        self.assertEqual(result, "powershell")

    def test_neither_set_returns_powershell(self) -> None:
        result = self._call({})
        self.assertEqual(result, "powershell")

    def test_both_set_returns_bash(self) -> None:
        result = self._call({"BASH_VERSION": "4.4.0", "MSYSTEM": "MINGW64"})
        self.assertEqual(result, "bash")


class TestChooseScriptWindowsBash(unittest.TestCase):
    """Unit tests for choose_script() with Windows + bash detection."""

    def _choose(self, *, is_windows: bool, bash_shell: bool) -> tuple[str, list[str]]:
        """Call choose_script() with mocked platform and env."""
        env_overrides: dict[str, str] = {}
        if bash_shell:
            env_overrides["BASH_VERSION"] = "5.2.0"
        else:
            # Ensure BASH_VERSION and MSYSTEM are absent
            pass

        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("BASH_VERSION", "MSYSTEM")}
        clean_env.update(env_overrides)

        import shutil
        with patch("platform.system", return_value="Windows" if is_windows else "Linux"):
            with patch.dict(os.environ, clean_env, clear=True):
                if is_windows and bash_shell:
                    # bash must be findable
                    with patch.object(shutil, "which",
                                      side_effect=lambda x: "/usr/bin/bash" if x == "bash" else None):
                        return cli.choose_script()
                elif is_windows:
                    # PowerShell must be findable
                    with patch.object(shutil, "which",
                                      side_effect=lambda x: "pwsh" if x == "pwsh" else None):
                        return cli.choose_script()
                else:
                    # Linux: bash findable
                    with patch.object(shutil, "which",
                                      side_effect=lambda x: "/usr/bin/bash" if x == "bash" else None):
                        return cli.choose_script()

    def test_linux_returns_sh(self) -> None:
        script_name, _ = self._choose(is_windows=False, bash_shell=False)
        self.assertEqual(script_name, "bootstrap.sh")

    def test_windows_no_bash_returns_ps1(self) -> None:
        script_name, _ = self._choose(is_windows=True, bash_shell=False)
        self.assertEqual(script_name, "bootstrap.ps1")

    def test_windows_bash_version_returns_sh(self) -> None:
        script_name, interp = self._choose(is_windows=True, bash_shell=True)
        self.assertEqual(script_name, "bootstrap.sh")
        self.assertIn("bash", interp[0].lower())

    def test_windows_msystem_mingw64_returns_sh(self) -> None:
        import shutil
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("BASH_VERSION", "MSYSTEM")}
        clean_env["MSYSTEM"] = "MINGW64"
        with patch("platform.system", return_value="Windows"):
            with patch.dict(os.environ, clean_env, clear=True):
                with patch.object(shutil, "which",
                                  side_effect=lambda x: "/usr/bin/bash" if x == "bash" else None):
                    script_name, interp = cli.choose_script()
        self.assertEqual(script_name, "bootstrap.sh")
        self.assertIn("bash", interp[0].lower())


if __name__ == "__main__":
    unittest.main()
