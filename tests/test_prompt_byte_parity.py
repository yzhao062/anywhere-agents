"""Byte-parity invariant for the Auto-terminal Codex channel.

SKILL.md > Phase 1c declares the prompt sent through Terminal-relay,
Auto-terminal, and Plugin channels must be byte-identical (Plugin may
strip the surrounding Markdown fence). These tests verify:

1. SKILL.md explicitly documents the invariant (regression guard against
   future edits silently weakening the claim).
2. dispatch-codex preserves the prompt body through the dispatch pipeline,
   including LF and CRLF line endings and unicode content.

The dispatch contract tests in test_dispatch_codex.py exercise the same
pipeline with simpler ASCII; this file focuses on the byte-fidelity edge
cases that justify the invariant.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = ROOT / "skills" / "implement-review" / "SKILL.md"
SCRIPTS_DIR = ROOT / "skills" / "implement-review" / "scripts"
DISPATCH_SH = SCRIPTS_DIR / "dispatch-codex.sh"
DISPATCH_PS1 = SCRIPTS_DIR / "dispatch-codex.ps1"


def _temp_dir():
    """TemporaryDirectory with ignore_cleanup_errors on Py3.10+ (Py3.9 fallback).

    See tests/test_dispatch_codex.py:_temp_dir for the rationale.
    """
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()

BASH = shutil.which("bash")
PS_SHELL = shutil.which("pwsh") or shutil.which("powershell")


# Mock codex that writes received stdin verbatim to MOCK_CODEX_LOG/stdin.
MOCK_CODEX_BYTES_PY = r'''import os, sys
log_dir = os.environ.get("MOCK_CODEX_LOG", os.getcwd())
os.makedirs(log_dir, exist_ok=True)
# Binary read to preserve every byte (including CRLF).
data = sys.stdin.buffer.read()
with open(os.path.join(log_dir, "stdin.bin"), "wb") as f:
    f.write(data)
sys.exit(0)
'''


def _write_mock(tmpdir: Path, want_powershell: bool) -> Path:
    py = tmpdir / "mock_codex_bytes.py"
    py.write_text(MOCK_CODEX_BYTES_PY, encoding="utf-8")
    if want_powershell:
        shim = tmpdir / "codex-mock.cmd"
        shim.write_text(
            "@echo off\r\n" f'"{sys.executable}" "{py}" %*\r\n',
            encoding="utf-8",
        )
    else:
        import stat
        shim = tmpdir / "codex-mock.sh"
        shim.write_text(
            "#!/usr/bin/env bash\n" f'exec "{sys.executable}" "{py}" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _dispatch_with(
    shell_kind: str,
    tmpdir: Path,
    prompt_file: Path,
    codex_bin: Path,
    log_dir: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CODEX_BIN"] = str(codex_bin)
    env["MOCK_CODEX_LOG"] = str(log_dir)
    env["TMPDIR"] = str(tmpdir)
    env["TEMP"] = str(tmpdir)
    env["TMP"] = str(tmpdir)
    # Quick stall-watch shutdown so file handles release before tempdir cleanup.
    env.setdefault("STALL_POLL_INTERVAL_SECONDS", "1")
    env.setdefault("STALL_THRESHOLD_SECONDS", "999999")
    if shell_kind == "bash":
        cmd = [
            BASH, str(DISPATCH_SH),
            "--prompt-file", str(prompt_file),
            "--round", "1",
            "--expected-review-file", "Review-Codex.md",
        ]
    else:
        cmd = [
            PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(DISPATCH_PS1),
            "--prompt-file", str(prompt_file),
            "--round", "1",
            "--expected-review-file", "Review-Codex.md",
        ]
    return subprocess.run(
        cmd, cwd=str(tmpdir), env=env,
        capture_output=True, text=True, check=False, timeout=60,
    )


class ByteParityContract(unittest.TestCase):
    """Document-level invariant: SKILL.md must state byte-identical claim."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.skill_text = SKILL_MD.read_text(encoding="utf-8")

    def test_skill_md_states_byte_identical_for_auto_terminal(self) -> None:
        self.assertIn(
            "byte-identical", self.skill_text,
            "SKILL.md must state Auto-terminal prompt is byte-identical to "
            "Terminal-relay (Phase 1c invariant)",
        )

    def test_skill_md_states_byte_for_byte_for_plugin(self) -> None:
        self.assertIn(
            "byte-for-byte", self.skill_text,
            "SKILL.md must state Plugin prompt matches Terminal-relay "
            "byte-for-byte (Phase 1c invariant, allowing optional fence-strip)",
        )

    def test_skill_md_warns_against_arg_max_substitution(self) -> None:
        """The stdin invariant exists to dodge ARG_MAX on Windows."""
        self.assertIn(
            "ARG_MAX", self.skill_text,
            "SKILL.md must explain why the prompt goes via stdin (ARG_MAX risk)",
        )
        # Must explicitly forbid the broken positional / command-substitution patterns
        self.assertIn(
            "command substitution", self.skill_text.lower(),
            "SKILL.md must forbid command-substitution form",
        )

    def test_skill_md_forbids_truncation_fallback(self) -> None:
        """A truncated positional-arg fallback was rejected during Phase A review."""
        self.assertIn(
            "truncated positional-argument fallback",
            self.skill_text,
            "SKILL.md must explicitly forbid the truncation fallback",
        )

    def test_skill_md_documents_embedded_diff_retry(self) -> None:
        """Auto-terminal needs a tool-less retry when Codex shell spawn fails."""
        self.assertIn("embedded-diff retry", self.skill_text)
        self.assertIn("CreateProcessAsUserW failed: 1312", self.skill_text)
        self.assertIn("Do NOT run `git diff` or any shell command", self.skill_text)

    def test_skill_md_documents_sandbox_flag_as_primary(self) -> None:
        """Auto-terminal's primary defense is `--sandbox danger-full-access`,
        not the embedded-diff retry. The retry is a fallback for sandbox-
        strict environments (CI). SKILL.md must document the sandbox flag
        as primary AND state that it aligns the channel's trust model
        with Terminal-relay AND mention the CODEX_DISPATCH_SANDBOX env
        var as the override hook.
        """
        self.assertIn("--sandbox danger-full-access", self.skill_text,
                      "SKILL.md must name the primary sandbox flag")
        self.assertIn("CODEX_DISPATCH_SANDBOX", self.skill_text,
                      "SKILL.md must document the CODEX_DISPATCH_SANDBOX override")
        self.assertIn("trust model", self.skill_text.lower(),
                      "SKILL.md must explain the Terminal-relay trust-model alignment")

    def test_skill_md_documents_retry_medium_phase_2_5(self) -> None:
        """Retry-channel Medium findings have an elevated false-positive
        rate (diff-only visibility). SKILL.md must document that they
        flow through Phase 2.5 verification, not just High findings.
        """
        self.assertIn("Medium-from-retry-channel", self.skill_text,
                      "SKILL.md must name the retry-channel Medium-verify rule")
        self.assertIn("diff-scoped", self.skill_text,
                      "SKILL.md must label retry findings as diff-scoped")

    def test_skill_md_documents_full_health_check_arg_set(self) -> None:
        """SKILL.md must show health-check's full required arg shape.

        Regression: consumer-side Claude historically invoked
        `health-check --state-dir <path>` alone (omitting `--round`),
        causing argparse error before any check ran. The Phase 1c step
        that names the call must inline all three required-or-recommended
        flags so a fresh reader has an unambiguous, copy-pasteable
        invocation.
        """
        # All three flags must appear together within a short window so
        # they read as one command, not scattered across the doc.
        window_size = 200
        idx_state = self.skill_text.find("--state-dir <abs-path>")
        self.assertGreater(
            idx_state, -1,
            "SKILL.md must show the health-check --state-dir invocation",
        )
        window = self.skill_text[idx_state : idx_state + window_size]
        self.assertIn(
            "--round", window,
            "SKILL.md must show --round flag adjacent to --state-dir; "
            "argparse rejects calls without --round",
        )
        self.assertIn(
            "--review-file", window,
            "SKILL.md must show --review-file flag adjacent to --state-dir "
            "to make the invocation unambiguous for fresh readers",
        )


class _PreservationMixin:
    SHELL_KIND: str = ""

    def _round_trip(self, body_bytes: bytes) -> bytes:
        """Send `body_bytes` through dispatch via the mock codex; return what it received."""
        with _temp_dir() as td:
            tmpdir = Path(td)
            log_dir = tmpdir / "log"
            log_dir.mkdir()
            codex = _write_mock(
                tmpdir, want_powershell=(self.SHELL_KIND == "powershell")
            )
            prompt = tmpdir / "prompt.txt"
            prompt.write_bytes(body_bytes)
            result = _dispatch_with(
                self.SHELL_KIND, tmpdir, prompt, codex, log_dir
            )
            self.assertEqual(
                result.returncode, 0,
                f"dispatch failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
            )
            received = (log_dir / "stdin.bin").read_bytes()
            return received

    def test_lf_body_bytes_preserved(self) -> None:
        """LF-terminated prompt body bytes survive dispatch exactly."""
        body = b"Review prompt body.\nLine 2.\nLine 3 with backticks `foo`.\n"
        received = self._round_trip(body)
        self.assertEqual(received, body)

    def test_crlf_body_bytes_preserved(self) -> None:
        """CRLF-terminated prompt body bytes survive dispatch exactly."""
        body = b"CRLF test.\r\nSecond line.\r\nThird line with anchor :42.\r\n"
        received = self._round_trip(body)
        self.assertEqual(received, body)

    def test_long_prompt_body_bytes_preserved(self) -> None:
        """4KB+ prompt body survives dispatch without byte drift.

        Regression guard against ARG_MAX-style truncation AND silent
        encoding drift (BOM injection, CRLF normalization).
        """
        chunk = b"Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
        body = chunk * 100  # ~5.7KB
        received = self._round_trip(body)
        self.assertEqual(received, body)

    def test_unicode_bytes_preserved(self) -> None:
        """Unicode UTF-8 bytes survive dispatch exactly.

        Snowman (\\xe2\\x98\\x83), euro (\\xe2\\x82\\xac), CJK ideographs.
        """
        body = (
            b"Review notes: snowman \xe2\x98\x83 euro \xe2\x82\xac "
            b"and CJK \xe4\xb8\xad\xe6\x96\x87 test.\n"
        )
        received = self._round_trip(body)
        self.assertEqual(received, body)

    def test_no_final_newline_bytes_preserved(self) -> None:
        """No BOM injection; no trailing newline added when source has none."""
        body = b"no final newline"
        received = self._round_trip(body)
        self.assertEqual(received, body)


@unittest.skipIf(
    sys.platform.startswith("win"),
    "bash skipped on Windows; CI Linux covers .sh round-trip",
)
@unittest.skipUnless(BASH, "bash not on PATH")
class BashByteParityTests(_PreservationMixin, unittest.TestCase):
    SHELL_KIND = "bash"


@unittest.skipUnless(
    PS_SHELL and sys.platform.startswith("win"),
    "PowerShell byte-parity tests are Windows-only: dispatch-codex.ps1 is "
    "Windows-targeted (Start-Process -WindowStyle, powershell.exe, .cmd "
    "shim). Linux/macOS users exercise dispatch-codex.sh.",
)
class PowerShellByteParityTests(_PreservationMixin, unittest.TestCase):
    SHELL_KIND = "powershell"


if __name__ == "__main__":
    unittest.main()
