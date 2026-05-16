"""Tests for health-check.py + health-check.{sh,ps1} wrappers.

Covers the 9 structural Health checks + 3 Substance heuristics defined in
skills/implement-review/SKILL.md > Phase 2.0 prologue. The Python helper
contains the real logic; the shell wrappers are exercised by smoke tests to
confirm they delegate correctly.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "implement-review" / "scripts"
HEALTH_PY = SCRIPTS_DIR / "health-check.py"
HEALTH_SH = SCRIPTS_DIR / "health-check.sh"
HEALTH_PS1 = SCRIPTS_DIR / "health-check.ps1"

BASH = shutil.which("bash")
PS_SHELL = shutil.which("pwsh") or shutil.which("powershell")


def parse_output(stdout: str) -> dict[str, tuple[str, str]]:
    """Parse health-check output into {code: (kind, rest_of_line)}.

    Each line has shape: KIND code [details...]. Returns dict keyed by code.
    """
    out: dict[str, tuple[str, str]] = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(maxsplit=2)
        if len(parts) < 2:
            continue
        kind, code = parts[0], parts[1]
        rest = parts[2] if len(parts) > 2 else ""
        out[code] = (kind, rest)
    return out


def make_review(
    path: Path,
    round_num: int = 1,
    extra_body: str = "",
    include_verification_notes: bool = True,
    pad_to: int = 600,
) -> str:
    """Build a minimally-valid review file at `path`."""
    parts = [f"<!-- Round {round_num} -->", "", "# Review", ""]
    if include_verification_notes:
        parts.extend(["Verification notes: spot-checked source code.", ""])
    if extra_body:
        parts.append(extra_body)
    body = "\n".join(parts) + "\n"
    if len(body) < pad_to:
        body += "Filler content. " * ((pad_to - len(body)) // 16 + 1)
    path.write_text(body, encoding="utf-8")
    return body


def make_state_dir(
    parent: Path,
    *,
    with_tail: bool = True,
    tail_content: str = "mock codex stdout\nmock codex stderr\n",
    with_stall: bool = False,
    stall_content: str = "STALL 2026-05-15T12:00:00Z tail-no-growth-for-300s\n",
    pre_mtime: int = 0,
    dispatch_offset: int = 60,
    skip_pre_mtime: bool = False,
    skip_timestamp: bool = False,
) -> Path:
    """Create a state-dir under `parent/state` with the requested fixture state.

    dispatch_offset = seconds *before now* for the dispatch timestamp.
    Negative offset puts the dispatch timestamp in the future (for Check 2 FAIL).
    """
    state_dir = parent / "state"
    state_dir.mkdir()
    now = int(time.time())
    dispatch_time = now - dispatch_offset
    if not skip_pre_mtime:
        (state_dir / "pre-mtime").write_text(f"{pre_mtime}\n", encoding="utf-8")
    if not skip_timestamp:
        (state_dir / "timestamp").write_text(f"{dispatch_time}\n", encoding="utf-8")
    if with_tail:
        (state_dir / "tail").write_text(tail_content, encoding="utf-8")
    if with_stall:
        (state_dir / "stall-warning").write_text(stall_content, encoding="utf-8")
    return state_dir


def run_health_py(
    state_dir: Path,
    review_file: Path,
    round_num: int = 1,
    *,
    prompt_file: Path | None = None,
    lens: str | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable, str(HEALTH_PY),
        "--state-dir", str(state_dir),
        "--review-file", str(review_file),
        "--round", str(round_num),
    ]
    if prompt_file is not None:
        cmd += ["--prompt-file", str(prompt_file)]
    if lens is not None:
        cmd += ["--lens", lens]
    return subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=30
    )


class HealthCheckPython(unittest.TestCase):
    """Direct tests against health-check.py."""

    # ----- happy path -----
    def test_all_pass_for_well_formed_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            state = make_state_dir(td_path, dispatch_offset=60)
            result = run_health_py(state, review, round_num=1)
            self.assertEqual(
                result.returncode, 0,
                f"happy path must exit 0; stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}",
            )
            parsed = parse_output(result.stdout)
            for code in ("check-1", "check-2", "check-3", "check-4", "check-5"):
                self.assertEqual(
                    parsed[code][0], "PASS",
                    f"{code} should PASS for well-formed review; "
                    f"got {parsed[code]}",
                )

    # ----- Check 1: review file missing -----
    def test_check1_missing_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            state = make_state_dir(td_path)
            result = run_health_py(
                state, td_path / "Review-Nonexistent.md", round_num=1
            )
            self.assertEqual(result.returncode, 1)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-1"][0], "FAIL")

    # ----- Check 2: freshness -----
    def test_check2_stale_review_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            # Dispatch happens in the future -> review mtime is BEFORE dispatch_time
            state = make_state_dir(td_path, dispatch_offset=-3600)
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 1)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-2"][0], "FAIL")

    # ----- Check 3: wrong round marker -----
    def test_check3_wrong_round_marker_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review, round_num=2)  # marker says Round 2
            state = make_state_dir(td_path)
            result = run_health_py(state, review, round_num=1)  # but we asked Round 1
            self.assertEqual(result.returncode, 1)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-3"][0], "FAIL")

    # ----- Check 4: tiny review -----
    def test_check4_tiny_review_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            # Force a tiny file (round marker + verification notes, no padding)
            review.write_text(
                "<!-- Round 1 -->\n# Review\nVerification notes: ok.\n",
                encoding="utf-8",
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 1)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-4"][0], "FAIL")

    # ----- Check 5: verification notes missing -----
    def test_check5_missing_verification_notes_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review, include_verification_notes=False)
            state = make_state_dir(td_path)
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 1)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-5"][0], "FAIL")

    # ----- Check 6: scope correspondence -----
    def test_check6_prompt_files_not_mentioned_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review, extra_body="The review covered some files.")
            prompt = td_path / "prompt.txt"
            prompt.write_text(
                "Review the staged file `skills/implement-review/SKILL.md` "
                "for clarity.",
                encoding="utf-8",
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review, prompt_file=prompt)
            self.assertEqual(result.returncode, 1)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-6"][0], "FAIL")

    def test_check6_prompt_files_mentioned_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(
                review,
                extra_body="I checked skills/implement-review/SKILL.md carefully.",
            )
            prompt = td_path / "prompt.txt"
            prompt.write_text(
                "Review the staged file `skills/implement-review/SKILL.md`.",
                encoding="utf-8",
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review, prompt_file=prompt)
            self.assertEqual(result.returncode, 0)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-6"][0], "PASS")

    # ----- Check 7: suspicious phrases -----
    def test_check7_warns_on_suspicious_phrases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(
                review,
                extra_body=(
                    "I could not read the source file.\n"
                    "Rate limit hit during inspection.\n"
                ),
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 0,
                             "WARN-only must still exit 0")
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-7"][0], "WARN")
            self.assertIn("lines=", parsed["check-7"][1])

    def test_check7_ignores_phrases_in_backticks(self) -> None:
        """FP-tune: Codex meta-discussing the pattern list must not fire."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(
                review,
                extra_body=(
                    "The pattern list includes `could not`, `failed to`, "
                    "`rate limit` -- this is discussion, not failure.\n"
                ),
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 0)
            parsed = parse_output(result.stdout)
            self.assertEqual(
                parsed["check-7"][0], "PASS",
                f"backtick code spans must be excluded: {parsed['check-7']}",
            )

    def test_check7_ignores_phrases_in_fenced_code_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(
                review,
                extra_body=(
                    "Example failure log inside a fence:\n"
                    "```\n"
                    "ERROR: could not connect\n"
                    "ERROR: rate limit\n"
                    "```\n"
                    "End of example.\n"
                ),
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-7"][0], "PASS")

    # ----- Check 8: tool failures in dispatch tail -----
    def test_check8_warns_on_tail_tool_failures(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            state = make_state_dir(
                td_path,
                tail_content=(
                    "running tool foo\n"
                    "ERROR: HTTP/1.1 429 too many requests\n"
                    "ERROR: tool github_api failed\n"
                ),
            )
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 0)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-8"][0], "WARN")

    def test_check8_warns_when_tail_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            state = make_state_dir(td_path, with_tail=False)
            result = run_health_py(state, review)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-8"][0], "WARN")
            self.assertIn("missing-dispatch-tail", parsed["check-8"][1])

    # ----- Check 9: stall warning -----
    def test_check9_warns_when_stall_warning_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            state = make_state_dir(td_path, with_stall=True)
            result = run_health_py(state, review)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-9"][0], "WARN")
            self.assertIn("stall-periods", parsed["check-9"][1])

    # ----- State contract -----
    def test_state_contract_missing_pre_mtime_is_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            state = make_state_dir(td_path, skip_pre_mtime=True)
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 1)
            self.assertIn("FAIL state-contract", result.stdout)

    def test_state_contract_missing_timestamp_is_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            state = make_state_dir(td_path, skip_timestamp=True)
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 1)
            self.assertIn("FAIL state-contract", result.stdout)

    def test_state_contract_missing_state_dir_is_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            result = run_health_py(
                td_path / "nonexistent-state-dir", review
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("FAIL state-contract", result.stdout)

    # ----- Substance 1: time floor -----
    def test_substance1_warns_on_fast_completion_with_long_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            prompt = td_path / "prompt.txt"
            prompt.write_text("X" * 2500, encoding="utf-8")
            # dispatch was 5 seconds ago, review just written -> elapsed ~5s
            state = make_state_dir(td_path, dispatch_offset=5)
            result = run_health_py(state, review, prompt_file=prompt)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["substance-1"][0], "WARN",
                             f"got {parsed['substance-1']}")

    def test_substance1_passes_when_prompt_short(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            prompt = td_path / "prompt.txt"
            prompt.write_text("short prompt only.", encoding="utf-8")
            state = make_state_dir(td_path, dispatch_offset=5)
            result = run_health_py(state, review, prompt_file=prompt)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["substance-1"][0], "PASS")

    def test_substance1_passes_when_elapsed_above_floor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            prompt = td_path / "prompt.txt"
            prompt.write_text("X" * 2500, encoding="utf-8")
            state = make_state_dir(td_path, dispatch_offset=60)
            result = run_health_py(state, review, prompt_file=prompt)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["substance-1"][0], "PASS")

    # ----- Substance 2: anchor density -----
    def test_substance2_warns_on_long_review_with_no_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            # Long generic prose, no file:line anchors
            make_review(
                review,
                extra_body="Generic discussion. " * 200,
                pad_to=2000,
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["substance-2"][0], "WARN")
            self.assertIn("0-anchors", parsed["substance-2"][1])

    def test_substance2_passes_when_anchors_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(
                review,
                extra_body=(
                    "I checked `skills/implement-review/SKILL.md:223` and "
                    "line 234 of dispatch-codex.sh.\n"
                    + ("Filler. " * 100)
                ),
                pad_to=1500,
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["substance-2"][0], "PASS")

    # ----- Substance 3: scope-challenge axes (plan-review lens only) -----
    def test_substance3_warns_when_axes_missing_under_plan_review_lens(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(
                review,
                extra_body="Plain prose without scope-challenge keywords.",
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review, lens="plan-review")
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["substance-3"][0], "WARN")
            self.assertIn("missing-axes=", parsed["substance-3"][1])

    def test_substance3_passes_when_all_axes_engaged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(
                review,
                extra_body=(
                    "Scope position: this is the smallest path forward. "
                    "Considered a larger scope but rejected. "
                    "Deferral of further work is appropriate to avoid "
                    "process tax overhead. The simplest path is to ship now."
                ),
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review, lens="plan-review")
            parsed = parse_output(result.stdout)
            self.assertEqual(
                parsed["substance-3"][0], "PASS",
                f"all axes should be engaged; got {parsed['substance-3']}",
            )

    def test_substance3_skipped_for_non_plan_lens(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review, extra_body="Plain prose without scope keywords.")
            state = make_state_dir(td_path)
            result = run_health_py(state, review, lens="code")
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["substance-3"][0], "PASS")
            self.assertIn("non-plan-review-lens-skipped", parsed["substance-3"][1])


class HealthCheckWrappers(unittest.TestCase):
    """Smoke tests: shell wrappers delegate to Python helper correctly."""

    def _build_fixture(self, td_path: Path) -> tuple[Path, Path]:
        review = td_path / "Review-Codex.md"
        make_review(review)
        state = make_state_dir(td_path)
        return state, review

    @unittest.skipIf(
        sys.platform.startswith("win"),
        "bash skipped on Windows; CI Linux covers .sh wrapper",
    )
    @unittest.skipUnless(BASH, "bash not on PATH")
    def test_sh_wrapper_delegates_to_python(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state, review = self._build_fixture(Path(td))
            cmd = [
                BASH, str(HEALTH_SH),
                "--state-dir", str(state),
                "--review-file", str(review),
                "--round", "1",
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=30
            )
            self.assertEqual(result.returncode, 0,
                             f"sh wrapper failed: {result.stderr}")
            self.assertIn("PASS check-1", result.stdout)

    @unittest.skipUnless(PS_SHELL, "pwsh/powershell not available")
    def test_ps1_wrapper_delegates_to_python(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state, review = self._build_fixture(Path(td))
            cmd = [
                PS_SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(HEALTH_PS1),
                "--state-dir", str(state),
                "--review-file", str(review),
                "--round", "1",
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=30
            )
            self.assertEqual(result.returncode, 0,
                             f"ps1 wrapper failed: {result.stderr}")
            self.assertIn("PASS check-1", result.stdout)


class HealthCheckScriptsTracked(unittest.TestCase):
    def test_py_exists(self) -> None:
        self.assertTrue(HEALTH_PY.exists())

    def test_sh_exists(self) -> None:
        self.assertTrue(HEALTH_SH.exists())

    def test_ps1_exists(self) -> None:
        self.assertTrue(HEALTH_PS1.exists())


if __name__ == "__main__":
    unittest.main()
