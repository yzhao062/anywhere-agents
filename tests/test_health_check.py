"""Tests for health-check.py + health-check.{sh,ps1} wrappers.

Covers the 9 structural Health checks + 3 Substance heuristics defined in
skills/implement-review/references/health-check.md > Phase 2.0 prologue. The Python helper
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

# tests/ is on sys.path under `unittest discover -s tests` but not under
# `python -m unittest tests.<module>`, which validate.yml uses for the
# Sentinel redaction smoke. Put it there before the sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows


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
    tail_stderr_content: str | None = None,
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
    if tail_stderr_content is not None:
        (state_dir / "tail.stderr-tmp").write_text(
            tail_stderr_content, encoding="utf-8"
        )
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

    def test_check8_scans_tail_when_review_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            incident = (
                '"C:\\Users\\me\\AppData\\Local\\Microsoft\\WindowsApps\\pwsh.exe" '
                "-Command 'Get-Date -Format o'\n"
                "ERROR codex_core::tools::router: error=Exit code: -1073741502\n"
                "exited -1073741502 in 33ms\n"
            ) * 3
            state = make_state_dir(td_path, tail_content=incident)
            result = run_health_py(state, td_path / "Review-Nonexistent.md")
            self.assertEqual(result.returncode, 1)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-1"][0], "FAIL")
            self.assertEqual(parsed["check-8"][0], "WARN")
            self.assertIn("tool-failure-markers", parsed["check-8"][1])
            self.assertNotIn("tail-present-not-scanned", result.stdout)

    def test_check8_passes_on_clean_tail_when_review_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            state = make_state_dir(td_path)
            result = run_health_py(state, td_path / "Review-Nonexistent.md")
            self.assertEqual(result.returncode, 1)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-8"][0], "PASS")
            self.assertIn("0-tool-failure-markers", parsed["check-8"][1])

    def test_check9_reports_stall_periods_when_review_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            state = make_state_dir(
                td_path,
                with_stall=True,
                stall_content=(
                    "STALL 2026-05-15T12:00:00Z tail-no-growth-for-300s\n"
                    "STALL 2026-05-15T12:05:00Z tail-no-growth-for-300s\n"
                ),
            )
            result = run_health_py(state, td_path / "Review-Nonexistent.md")
            self.assertEqual(result.returncode, 1)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-9"][0], "WARN")
            self.assertIn("stall-periods", parsed["check-9"][1])

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

    def test_check5_accepts_bold_sentence_verification_notes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(
                review,
                include_verification_notes=False,
                extra_body="**Verification notes.** none.\n",
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-5"][0], "PASS")

    def test_check5_accepts_heading_levels(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(
                review,
                include_verification_notes=False,
                extra_body="### Verification notes\nnone.\n",
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-5"][0], "PASS")

    def test_check5_accepts_bare_label_line(self) -> None:
        # The request template says "Begin with Verification notes listing
        # exact commands and outcomes", and a Grok review wrote it that way.
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Grok.md"
            make_review(
                review,
                include_verification_notes=False,
                extra_body="Verification notes\n\n- `python -m unittest`: OK\n",
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-5"][0], "PASS")

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

    def test_check8_warns_on_stderr_side_tail_tool_failures(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            state = make_state_dir(
                td_path,
                tail_content="clean stdout\n",
                tail_stderr_content="ERROR: tool file_write failed\n",
            )
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 0)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-8"][0], "WARN")

    def _assert_check8_warn(self, tail_content: str) -> None:
        """Run health-check.py with the given tail and assert Check 8 WARN."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            state = make_state_dir(td_path, tail_content=tail_content)
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 0)
            parsed = parse_output(result.stdout)
            self.assertEqual(
                parsed["check-8"][0], "WARN",
                f"expected WARN for tail {tail_content!r}, got {parsed['check-8']}",
            )
            self.assertIn("tool-failure-markers", parsed["check-8"][1])

    def _assert_check8_pass(
        self, tail_content: str, source_label: str | None = None
    ) -> None:
        """Run health-check.py with the given tail and assert Check 8 PASS.

        ``source_label`` replaces the tail text in the failure message. Pass it
        when the tail is a whole file: repr-ing a 90 KB SKILL.md produces a
        six-figure assertion message that buries the pattern breakdown, which is
        the part that actually says what went wrong. Inline fixtures leave it
        unset and keep showing their own text.
        """
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            state = make_state_dir(td_path, tail_content=tail_content)
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 0)
            parsed = parse_output(result.stdout)
            described = source_label or repr(tail_content)
            self.assertEqual(
                parsed["check-8"][0], "PASS",
                f"expected PASS for tail {described}, got {parsed['check-8']}",
            )

    def test_check8_warns_on_createprocessasuserw_1312_alone(self) -> None:
        """Isolated fixture: only the exact CreateProcessAsUserW 1312 marker.

        Tail has no `sandbox`, no `runner error`, no other Check 8 trigger.
        If `r"CreateProcessAsUserW failed: 1312"` were removed from
        TOOL_FAILURE_PATTERNS, this fixture would no longer match anything
        in the pattern set and the test would fail. Freezes that pattern's
        necessity independently of the other two new sandbox patterns.
        """
        self._assert_check8_warn(
            "ERROR codex_core::process: launch failed; "
            "CreateProcessAsUserW failed: 1312\n"
        )

    def test_check8_warns_on_windows_sandbox_runner_error_alone(self) -> None:
        """Isolated fixture: `windows sandbox: runner error` without 1312.

        Tail does NOT contain `CreateProcessAsUserW failed: 1312` and does
        not match HTTP/connection/quota patterns. If both
        `r"windows sandbox: runner error"` AND the broader
        `r"sandbox.*runner error"` were removed, this fixture would no
        longer match. Practically: pattern #2 is fully subsumed by
        pattern #3 (`sandbox.*runner error` matches the same string via
        the `.*`), so this test pins the joint coverage rather than #2
        in isolation. Keeping #2 explicit is defensive: it surfaces in
        SKILL.md Check 8 description as the canonical Windows shape.
        """
        self._assert_check8_warn(
            "ERROR codex_core::exec: exec error: "
            "windows sandbox: runner error\n"
        )

    def test_check8_warns_on_generic_sandbox_runner_error(self) -> None:
        """Catch-all fixture: `sandbox` + `runner error` without `windows`.

        Targets `r"sandbox.*runner error"` specifically. The literal
        `r"windows sandbox: runner error"` pattern would NOT match this
        line (no `windows` prefix); only the broader regex covers it.
        Freezes the broader pattern's value: cross-version / cross-
        platform variants Codex might emit.
        """
        self._assert_check8_warn(
            "ERROR codex_core::exec: macos sandbox: command runner error: "
            "spawn failed\n"
        )

    def test_check8_warns_on_ntstatus_spawn_exit_code(self) -> None:
        observed_shapes = (
            "ERROR codex_core::tools::router: error=Exit code: -1073741502\n",
            "exited -1073741502 in 33ms\n",
        )
        for tail_content in observed_shapes:
            with self.subTest(tail_content=tail_content):
                self._assert_check8_warn(tail_content)

    def test_check8_passes_on_benign_small_negative_exit(self) -> None:
        self._assert_check8_pass("process exited with exit code -1\n")

    def test_check8_passes_on_sandbox_word_without_runner_error(self) -> None:
        """Negative fixture: `sandbox` mention without `runner error` or 1312.

        Ensures the broader `r"sandbox.*runner error"` catch-all does not
        drift into matching benign `sandbox` mentions. Without this
        negative, a future regex weakening (e.g. dropping `runner error`
        from the pattern) would silently pass the positive tests above
        AND start firing on every codex log line that mentioned sandbox
        at all.
        """
        self._assert_check8_pass(
            "INFO codex_core::config: using sandbox policy: workspace-write\n"
        )

    def test_check8_passes_on_backtick_quoted_pattern_strings(self) -> None:
        """Inline backtick-quoted pattern strings in Codex reasoning text
        must NOT trigger Check 8.

        Regression: when /implement-review reviews the implement-review
        skill itself (or any prompt that names the patterns), Codex's
        stdout reasoning quotes the pattern strings with backticks for
        technical clarity. Pre-fix this produced 12-152 FP markers across
        4 confirmed runs (ac self-review r2/r3, random, NSF, Letter-).
        Mitigation mirrors Check 7's `strip_code_spans` pass.
        """
        self._assert_check8_pass(
            "Codex reasoning: I need to scan for `CreateProcessAsUserW failed: 1312`\n"
            "and `windows sandbox: runner error` in the dispatch tail.\n"
            "The pattern `rate limit` is also relevant for 429 surfaces.\n"
        )

    def test_check8_passes_on_backticked_ntstatus_code(self) -> None:
        self._assert_check8_pass(
            "Codex reasoning names the observed code as `-1073741502`.\n"
        )

    def test_check8_passes_on_fenced_block_with_pattern_strings(self) -> None:
        """Triple-backtick fenced blocks (e.g., Codex echoing a SKILL.md
        snippet that lists pattern strings) must be stripped before
        scanning. Mirror of Check 7's fenced-block handling.
        """
        self._assert_check8_pass(
            "Codex reasoning: I read the skill, which states:\n"
            "```\n"
            "Windows sandbox launch failures such as CreateProcessAsUserW failed: 1312\n"
            "or windows sandbox: runner error, plus rate limit / quota exceeded.\n"
            "```\n"
            "End of skill quote.\n"
        )

    def test_check8_code_stripping_hides_quoted_markers_and_keeps_real_ones(
        self,
    ) -> None:
        """Raw tails meet the same whole-run stripping as reviews.

        A marker inside a paired span is hidden, including a fence inside one
        JSON line with escaped newlines; a marker outside every paired span
        still warns, whatever unpaired runs or quoted fences come before it.
        """
        marker = "connection refused"
        hidden = {
            "single-backtick span": f"Reasoning names `{marker}` as a pattern.\n",
            "fence inside one JSON line": (
                '{"text":"```text\\n' + marker + '\\n```"}\n'
            ),
            "double-backtick span holding a backtick": (
                f"Quoted ``a ` {marker}`` here.\n"
            ),
        }
        shown = {
            "after three single backticks": f"`example` then ` {marker}\n",
            "after an unpaired four-backtick run": f"```` example\n{marker}\n",
            "between a quoted fence and a fenced example": (
                f"Quoted ```` ``` ```` fence.\n{marker}\n```\nexample\n```\n"
            ),
        }
        for name, tail in hidden.items():
            with self.subTest(case=name):
                self._assert_check8_pass(tail)
        for name, tail in shown.items():
            with self.subTest(case=name):
                self._assert_check8_warn(tail)

    def test_check8_warn_emits_pattern_breakdown(self) -> None:
        """When Check 8 WARNs, the line includes a `breakdown=` segment
        with per-pattern counts sorted by frequency. This is what lets
        downstream Claude recognize WSL-stub-bash 1312 burst (and other
        known-noise shapes in references/health-check.md, False-positive tuning) without
        re-grepping the tail.
        """
        # Mix of patterns with distinct counts so order is deterministic.
        # 3 windows sandbox + 2 rate limit + 1 http 429.
        tail = (
            "windows sandbox: runner error\n"
            "windows sandbox: runner error\n"
            "windows sandbox: runner error\n"
            "rate limit\n"
            "rate limit\n"
            "HTTP/1.1 429 Too Many Requests\n"
        )
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            state = make_state_dir(td_path, tail_content=tail)
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 0)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-8"][0], "WARN")
            self.assertIn("tool-failure-markers", parsed["check-8"][1])
            self.assertIn("breakdown=", parsed["check-8"][1])
            breakdown_segment = parsed["check-8"][1].split("breakdown=", 1)[1]
            # Pull labels in order: each `label:N` token.
            labels = [seg.split(":")[0] for seg in breakdown_segment.split()]
            # Labels must be sorted by count descending. With this tail
            # `windows` (3 hits) and `sandbox` (3 hits via two distinct
            # patterns) precede `limit` (2 hits) which precedes `429`
            # (1 hit).
            for top_label in ("windows", "sandbox"):
                self.assertLess(
                    labels.index(top_label),
                    labels.index("limit"),
                    f"breakdown not sorted by count desc: {breakdown_segment!r}",
                )
            # Pattern `HTTP/\S* (?:429|5\d\d)` yields label `http`
            # (longest word run in the pattern source) with 1 hit; must
            # come after `limit` (2 hits).
            self.assertLess(
                labels.index("limit"),
                labels.index("http"),
                f"breakdown not sorted by count desc: {breakdown_segment!r}",
            )

    def test_check8_breakdown_labels_ntstatus_family(self) -> None:
        tail = (
            "exited -1073741502 in 31ms\n"
            "exited -1073741502 in 32ms\n"
            "exited -1073741502 in 33ms\n"
            "ERROR: ECONNRESET\n"
        )
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(review)
            state = make_state_dir(td_path, tail_content=tail)
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 0)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-8"][0], "WARN")
            self.assertIn("107374:", parsed["check-8"][1])

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

    def test_check8_warns_on_a_backend_failure_record(self) -> None:
        # dispatch-gemini.py writes backend-failure just before it publishes a
        # review from a result that ended in a backend error. Its structure check
        # does not show the findings are finished, so the record alone must block
        # a silent advance, with a clean tail and an otherwise valid review.
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Antigravity.md"
            make_review(review)
            state = make_state_dir(td_path)
            parsed = parse_output(run_health_py(state, review).stdout)
            self.assertEqual(parsed["check-8"][0], "PASS")
            (state / "backend-failure").write_text(
                "Antigravity reported status ERROR: UNAVAILABLE (code 503)\n",
                encoding="utf-8",
            )
            result = run_health_py(state, review)
            self.assertEqual(result.returncode, 0)
            parsed = parse_output(result.stdout)
            self.assertEqual(parsed["check-8"][0], "WARN")
            self.assertIn("1 tool-failure-markers", parsed["check-8"][1])
            self.assertIn("breakdown=recovered:1", parsed["check-8"][1])

    # ----- Check 8 Fix A (line-level echo classifier) + Fix B (two-tier) -----
    # Real-failure-wins: a real failure must keep WARNing even when it looks
    # line-numbered or carries a backslash path. Validated live against the
    # preserved R1 dispatch tail; these freeze the shapes permanently.
    def test_check8_warns_on_line_numbered_real_diagnostic(self) -> None:
        """A line-numbered line that begins (after the number prefix) with a
        runner/log diagnostic token is a REAL failure, not a source echo, and
        must still WARN. Echo suppression must never drop it."""
        self._assert_check8_warn("12: ERROR: HTTP/1.1 429 too many requests\n")

    def test_check8_warns_on_windows_path_eacces(self) -> None:
        """A real EACCES failure on a Windows path (backslashes) must WARN. A
        bare backslash must NEVER classify a line as a regex-source echo, or
        real Windows-path failures would be silently dropped."""
        self._assert_check8_warn(
            "ERROR: EACCES opening C:\\Users\\me\\Review-Codex.md\n"
        )

    def test_check8_warns_on_rate_limit_exceeded_intrinsic(self) -> None:
        """The failure FORM `rate limit exceeded` is intrinsic and counts on its
        own, with no error-frame token required. Closes the R3 false-negative
        where a bare `Rate limit exceeded` line was dropped as generic."""
        self._assert_check8_warn("Rate limit exceeded after 3 retries\n")

    def test_check8_warns_on_too_many_requests_intrinsic(self) -> None:
        """`Too Many Requests` (the 429 text form) is intrinsic; counts alone."""
        self._assert_check8_warn("Too Many Requests\n")

    def test_check8_passes_on_line_numbered_source_citation(self) -> None:
        """A line-numbered source citation (codex quoting a file with line
        numbers) whose content is neither a diagnostic nor an intrinsic pattern
        is a benign echo and must NOT count."""
        self._assert_check8_pass(
            "61: token co-occurs with an error-frame token on the same line\n"
        )

    def test_check8_passes_on_literal_regex_source_line(self) -> None:
        r"""A line quoting a health-check pattern definition (regex
        metacharacters like ``\S`` / ``(?:`` / ``\bENOSPC\b``) is a benign echo
        and must NOT count. This is the dominant self-review FP source."""
        self._assert_check8_pass(
            '50: r"HTTP/\\S* (?:429|5\\d\\d)",\n'
            '51: r"\\bENOSPC\\b",\n'
        )

    def test_check8_passes_on_literal_ntstatus_regex_source(self) -> None:
        self._assert_check8_pass(r'52: r"-107374\d{4}",' + "\n")

    def test_check8_passes_on_bare_rate_limit_without_frame(self) -> None:
        """Bare `rate limit` in benign prose, with no error-frame token nearby,
        must NOT count (Fix B generic rule)."""
        self._assert_check8_pass(
            "the rate limit is 100 requests per minute by design\n"
        )

    def test_check8_warns_on_bare_rate_limit_with_frame(self) -> None:
        """Bare `rate limit` WITH an error-frame token on the same line counts
        (Fix B generic rule)."""
        self._assert_check8_warn("ERROR: rate limit was hit during the run\n")

    # ----- Check 8 W1b (documentation-echo fragment removal) -----
    # Negative corpus. Built from the three real documentation source lines that
    # blocked 11 of 11 preserved dispatch state dirs, with zero real tool
    # failures among them. Determined pre-fix marker counts: the SKILL.md
    # check-8 row yields 4, the SKILL.md Substance paragraph 1, the AGENTS.md
    # Tool-Use Reliability bullet 1. All three must now scan to zero.
    #
    # Inline on purpose: check-parity.sh has no tests/fixtures/ rule, so an
    # external fixture file would let this mirrored test reference a file
    # present in only one repo while parity still reported green.

    def test_check8_passes_on_legacy_pre_w1a_check8_row_shape(self) -> None:
        """Legacy pre-W1a shape of the Phase 2.0 check-8 row (now in references/health-check.md).

        This fixture is NOT how the current SKILL.md reads: W1a backticked both
        occurrences, so strip_code_spans now removes them before the fragment
        list is consulted. It is retained to protect preserved pre-W1a tails,
        deleted-side diff text, and consumers whose SKILL.md has not been
        refreshed. The live-file test below is what covers the current shape.

        Back when it did fire, ``HTTP/status 429/5xx`` was the only unbackticked
        pattern in its table cell, so strip_code_spans deleted its neighbours and
        left it exposed. Two regexes then matched nested spans of the same
        substring -- ``HTTP/\\S* (?:429|5\\d\\d)`` and ``status (?:429|5\\d\\d)``
        -- so every occurrence counted twice and the row's two yielded 4.
        """
        self._assert_check8_pass(
            "for `tool ... failed`, `mcp tool failed`, HTTP/status 429/5xx, "
            "`rate limit`, `quota exceeded`, then count intrinsic failure "
            "forms (HTTP/status 429/5xx, `Too Many Requests`, quota) alone.\n"
        )

    def test_check8_passes_on_legacy_pre_w1a_substance_paragraph_shape(
        self,
    ) -> None:
        """Legacy pre-W1a shape of the SKILL.md Substance heuristics paragraph.

        As above, W1a backticked the ``rate limit`` inside this sentence, so the
        fixture no longer matches a current SKILL.md and is kept for preserved
        tails, deleted-side diff text, and unrefreshed consumers.

        When it did fire it contributed exactly one generic ``rate limit``
        marker, licensed by the ``failed`` error-frame token on the same line.
        Note it does NOT also match intrinsic ``tool .* failed``: that pattern
        needs a literal ``"tool "`` with a trailing space, and this sentence
        reads "tools silently failed".
        """
        self._assert_check8_pass(
            "They do NOT catch a review that is structurally clean but "
            "substantively shallow (Codex's tools silently failed mid-run; "
            "rate limit; context overflow; or the model did not engage).\n"
        )

    def test_check8_passes_on_agents_md_tool_reliability_shape(self) -> None:
        """AGENTS.md Tool-Use Reliability bullet.

        Reaches consumers because bootstrap installs AGENTS.md at the root of
        every consuming project. Matches intrinsic ``tool .* failed`` here
        because "tool failures: a single failed" does carry the required space.
        """
        self._assert_check8_pass(
            "The same caution applies to other transient-looking tool "
            "failures: a single failed attempt is weak evidence, unless the "
            "failure is clearly deterministic.\n"
        )

    def test_check8_passes_on_all_three_doc_shapes_together(self) -> None:
        """The three shapes co-occur in a real tail; together they must still
        scan to zero rather than summing into a block."""
        self._assert_check8_pass(
            "for `mcp tool failed`, HTTP/status 429/5xx, `quota exceeded`\n"
            "(Codex's tools silently failed mid-run; rate limit; context "
            "overflow; or the model did not engage)\n"
            "other transient-looking tool failures: a single failed attempt "
            "is weak evidence\n"
        )

    # Positive preservation cases, kept isolated from the negative corpus above.
    # These are the specific guard against over-suppression: W1b must not buy
    # its zeros by eating real failure evidence.

    def test_check8_w1b_preserves_isolated_http_429(self) -> None:
        """A real HTTP 429 status line still counts."""
        self._assert_check8_warn("HTTP/1.1 429 Too Many Requests\n")

    def test_check8_w1b_preserves_bare_econnreset(self) -> None:
        """A bare errno symbol still counts."""
        self._assert_check8_warn("ECONNRESET\n")

    def test_check8_w1b_preserves_line_numbered_diagnostic(self) -> None:
        """A line-numbered REAL diagnostic is not an echo and still counts."""
        self._assert_check8_warn("12: ERROR: HTTP/1.1 429\n")

    def test_check8_w1b_preserves_real_error_sharing_a_doc_line(self) -> None:
        """Removal is fragment-scoped, not line-scoped.

        A documentation sentence followed on the SAME line by a real error must
        still count. Adding these fragments to REGEX_SOURCE_MARKERS instead
        would skip the whole line via is_echo_line() and lose this failure.
        """
        self._assert_check8_warn(
            "other transient-looking tool failures: a single failed attempt "
            "is weak evidence ERROR: ECONNRESET\n"
        )

    def test_check8_w1b_preserves_framed_skill_md_prefix_collision(self) -> None:
        """Truncated prefix of the SKILL.md fragment under an error frame.

        This is why the fragments are long sentences rather than short keys:
        suppressing on a bare ``rate limit`` key would consume the only failure
        evidence on this line and drop it from 1 marker to 0.
        """
        self._assert_check8_warn(
            "ERROR: tools silently failed mid-run; rate limit\n"
        )

    def test_check8_w1b_preserves_framed_agents_md_prefix_collision(self) -> None:
        """Truncated prefix of the AGENTS.md fragment under an error frame.

        Same guard as above for the ``tool .* failed`` shape: a short
        ``tool ... failed`` suppression key would have eaten this line.
        """
        self._assert_check8_warn(
            "ERROR: tool failures: a single failed attempt\n"
        )

    def test_check8_w1b_preserves_complete_fragment_under_diagnostic_prefix(
        self,
    ) -> None:
        """A COMPLETE fragment inside a diagnostic line must still count.

        Fragment scope alone does not cover this: the truncated-prefix cases
        above survive because the fragment never matches, but here it matches
        in full and the line's only failure evidence is *inside* it. Stripping
        would leave a bare ``ERROR: request failed: `` scoring zero, turning a
        real WARN into a silent PASS. DOC_DIAGNOSTIC_CONTEXT_RE is what stops
        that, so this test pins the line gate rather than the fragment list.
        """
        skill_fragment = (
            "tools silently failed mid-run; rate limit; context overflow; "
            "or the model did not engage"
        )
        agents_fragment = (
            "transient-looking tool failures: a single failed attempt "
            "is weak evidence"
        )
        for prefix, fragment in (
            ("ERROR: request failed: ", skill_fragment),
            ("request failed: ", skill_fragment),
            ("ERROR: ", skill_fragment),
            ("FATAL: ", agents_fragment),
        ):
            with self.subTest(prefix=prefix):
                self._assert_check8_warn(prefix + fragment + "\n")

    def test_check8_w1b_preserves_fragment_under_prefixed_diagnostics(
        self,
    ) -> None:
        """Real diagnostics whose severity token is NOT at column zero.

        The first line gate anchored its match at the start of the line, so an
        ISO timestamp, a JSON envelope, or a ``codex_core::`` logger path pushed
        the severity token out of reach and silenced the whole line. These four
        shapes each returned PASS 0 under that version. The context gate now
        accepts a severity or structured level token anywhere in the text
        preceding the fragment, which is what keeps them counting.
        """
        fragment = (
            "tools silently failed mid-run; rate limit; context overflow; "
            "or the model did not engage"
        )
        for label, line in (
            ("iso timestamp",
             f"2026-08-07T12:34:56Z ERROR request failed: {fragment}"),
            ("space timestamp",
             f"2026-08-07 12:34:56 WARN request failed: {fragment}"),
            ("json envelope",
             '{"level":"error","message":"request failed: ' + fragment + '"}'),
            ("logger path",
             f"codex_core::ERROR request failed: {fragment}"),
        ):
            with self.subTest(shape=label):
                self._assert_check8_warn(line + "\n")

    def test_check8_w1b_strips_fragment_under_benign_failure_labels(
        self,
    ) -> None:
        """Benign prose that merely labels an example is not a diagnostic.

        The first line gate treated any word followed by ``failed:`` within 80
        characters as diagnostic context, so ``Example failure:`` and friends
        restored the documentation false positive. Restricting the bare-label
        branch to an operational subject list at line start is what separates
        these from a real ``request failed:``.
        """
        fragment = (
            "tools silently failed mid-run; rate limit; context overflow; "
            "or the model did not engage"
        )
        for label in ("Example failure:", "Expected failure:",
                      "This example failed:"):
            with self.subTest(label=label):
                self._assert_check8_pass(f"{label} {fragment}\n")

    def test_check8_w1b_preserves_fragment_under_operational_failure_labels(
        self,
    ) -> None:
        """Realistic dispatch-tail labels must reach the verb in the subject list.

        Each of these was silenced by the first subject list: ``API`` and
        ``OpenAI`` were absent, ``exec`` was present only as ``execution``,
        ``spawn`` was missing outright, and a two-word gap allowance could not
        span ``OpenAI API request``. The only Check 8 evidence on these lines
        lives inside the fragment, so a miss here is a silent PASS on a real
        failure.
        """
        fragment = (
            "tools silently failed mid-run; rate limit; context overflow; "
            "or the model did not engage"
        )
        for label in ("API call failed:", "OpenAI API request failed:",
                      "spawn failed:", "exec failed:"):
            with self.subTest(label=label):
                self._assert_check8_warn(f"{label} {fragment}\n")

    def test_check8_w1b_strips_fragment_under_noun_form_failure_headings(
        self,
    ) -> None:
        """Noun-form ``failure:`` headings are documentation, not diagnostics.

        This is the other half of the pair above and the reason the operational
        branch takes only the verb ``failed:``. ``Tool failure:`` and ``Build
        failure:`` open with words that ARE on the subject list, so accepting
        the noun form classified them as diagnostics and restored the AGENTS.md
        false positive. A real noun-form failure still counts through the
        severity branch, which is unaffected.
        """
        fragment = (
            "transient-looking tool failures: a single failed attempt "
            "is weak evidence"
        )
        for heading in ("Tool failure:", "Build failure:"):
            with self.subTest(heading=heading):
                self._assert_check8_pass(f"{heading} {fragment}\n")

    def test_check8_w1b_strips_fragment_under_prose_error_headings(
        self,
    ) -> None:
        """Lowercase prose containing "Error" is not a log severity token.

        Under a blanket IGNORECASE the severity branch matched the English word,
        so ``Error example:`` counted as diagnostic context, kept its fragment,
        and produced a Check 8 warning. That is a false positive of exactly the
        kind W1b exists to remove. Branch 1a is uppercase-only, branch 1b
        accepts mixed case only before ``:`` or ``]``, and structured level
        fields remain case-insensitive. The word here is followed by another
        word rather than punctuation, which is what keeps it out.
        """
        fragment = (
            "tools silently failed mid-run; rate limit; context overflow; "
            "or the model did not engage"
        )
        for heading in ("Error example:", "Error case:", "Warning example:"):
            with self.subTest(heading=heading):
                self._assert_check8_pass(f"{heading} {fragment}\n")

    def test_check8_w1b_preserves_fragment_under_mixed_case_severity(
        self,
    ) -> None:
        """Mixed-case severity LABELS are diagnostics and must keep counting.

        An uppercase-only severity branch was tried first and silenced every
        one of these, which is a silent false negative on real output rather
        than a cosmetic miss: a preserved Copilot dispatch tail opens with
        ``Error: Authentication token found but could not be validated.``
        The punctuation lookahead is what admits these without also admitting
        the prose headings pinned in the test above.
        """
        fragment = (
            "tools silently failed mid-run; rate limit; context overflow; "
            "or the model did not engage"
        )
        for prefix in ("Error:", "error:", "Warning:", "[Error]",
                       "2026-08-07T12:34:56Z Error:"):
            with self.subTest(prefix=prefix):
                self._assert_check8_warn(f"{prefix} {fragment}\n")

    def test_check8_w1b_structured_level_field_stays_case_insensitive(
        self,
    ) -> None:
        """Splitting raw severity into uppercase and punctuation-gated forms
        must not break structured levels.

        JSON and logfmt emitters disagree on capitalisation, so the
        ``level=`` / ``severity=`` branch keeps matching any case. This is the
        guard on the tightening above: it would be easy to make the whole
        pattern case-sensitive and silently lose lowercase structured logs.
        Note these records carry no ``:`` or ``]`` directly after the severity
        word, so branch 1b would not rescue them.
        """
        fragment = (
            "tools silently failed mid-run; rate limit; context overflow; "
            "or the model did not engage"
        )
        for envelope in (
            '{"level":"error","msg":"%s"}',
            '{"Level":"ERROR","msg":"%s"}',
            'severity=warning msg="%s"',
        ):
            with self.subTest(envelope=envelope[:24]):
                self._assert_check8_warn((envelope % fragment) + "\n")

    def test_check8_w1b_severity_token_still_wins_over_noun_form(self) -> None:
        """Dropping noun-form ``failure:`` must not lose real noun diagnostics.

        Guards the escape hatch claimed by the test above: a genuine noun-form
        failure carries a severity token, and the severity branch catches it
        regardless of the operational-label branch.
        """
        fragment = (
            "transient-looking tool failures: a single failed attempt "
            "is weak evidence"
        )
        self._assert_check8_warn(f"ERROR Tool failure: {fragment}\n")

    def test_check8_w1b_trailing_error_does_not_protect_earlier_fragment(
        self,
    ) -> None:
        """An error AFTER a benign fragment does not shield the fragment.

        Only the preceding text decides, so the fragment is still stripped and
        the trailing error is still counted. This pins the asymmetry: without
        it, appending any error token to a documentation line would restore the
        false positive.
        """
        self._assert_check8_warn(
            "other transient-looking tool failures: a single failed attempt "
            "is weak evidence ERROR: ECONNRESET\n"
        )

    # Source-coupled drift alarm. Every other fixture in this section is an
    # inline copy, so a one-character edit to the live SKILL.md or AGENTS.md
    # could restore a false positive while the whole suite stayed green. These
    # two feed the real files through the real classifier, which is the cheapest
    # thing that actually fails when the documentation and the fragment list
    # drift apart.

    def test_check8_live_skill_md_scans_clean(self) -> None:
        """The current SKILL.md, read whole, produces zero Check 8 markers."""
        skill_md = ROOT / "skills" / "implement-review" / "SKILL.md"
        self.assertTrue(skill_md.is_file(), f"missing {skill_md}")
        self._assert_check8_pass(
            skill_md.read_text(encoding="utf-8", errors="replace"),
            source_label=str(skill_md),
        )

    def test_check8_live_implement_review_references_scan_clean(self) -> None:
        """Every implement-review reference, read whole, produces zero Check 8 markers.

        The core moved prose into references/, and a coordinator that opens one
        reads it into the same context a Check 8 scan covers, so each file is
        held to the standard SKILL.md meets. references/example-reviews/ holds
        sample reviews and is not read as instruction text.
        """
        refs_dir = ROOT / "skills" / "implement-review" / "references"
        refs = sorted(p for p in refs_dir.glob("*.md") if p.is_file())
        self.assertTrue(refs, f"no reference files under {refs_dir}")
        for ref in refs:
            with self.subTest(ref.name):
                self._assert_check8_pass(
                    ref.read_text(encoding="utf-8", errors="replace"),
                    source_label=str(ref),
                )

    def test_check8_live_agents_md_scans_clean(self) -> None:
        """The current AGENTS.md, read whole, produces zero Check 8 markers.

        AGENTS.md is deliberately not edited by W1a, so its shape is handled
        only at the classifier. That makes this the load-bearing half of the
        drift alarm: bootstrap installs this file at the root of every
        consuming project, so a regression here reaches all of them.
        """
        agents_md = ROOT / "AGENTS.md"
        self.assertTrue(agents_md.is_file(), f"missing {agents_md}")
        self._assert_check8_pass(
            agents_md.read_text(encoding="utf-8", errors="replace"),
            source_label=str(agents_md),
        )

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

    # ----- Check 10: verdict-verification contract -----
    def test_check10_accepts_all_supported_status_label_shapes(self) -> None:
        label_shapes = {
            "plain": "Verification status: {status}",
            "bulleted": "- **Verification status:** {status}",
            "bold-label": "**Verification status**: {status}",
            "bold-label-and-colon": "**Verification status:** {status}",
            "underscored-label-and-colon": "__Verification status:__ {status}",
            "heading": "## Verification status: {status}",
            "bold-value": "Verification status: **{status}**",
            "no-space": "**Verification status:**{status}",
        }
        expected = {
            "VERIFIED": (0, "PASS", "verification=VERIFIED"),
            "UNVERIFIED": (1, "FAIL", "verification=UNVERIFIED"),
        }

        for shape_name, template in label_shapes.items():
            for status, (returncode, outcome, detail) in expected.items():
                with self.subTest(shape=shape_name, status=status):
                    with tempfile.TemporaryDirectory() as td:
                        td_path = Path(td)
                        review = td_path / "Review-Codex.md"
                        make_review(
                            review,
                            extra_body=(
                                f"{template.format(status=status)}\n\n"
                                "**Commit verdict:** PASS"
                            ),
                        )
                        state = make_state_dir(td_path)
                        result = run_health_py(state, review)
                        parsed = parse_output(result.stdout)
                        self.assertEqual(result.returncode, returncode)
                        self.assertEqual(parsed["check-10"][0], outcome)
                        self.assertIn(detail, parsed["check-10"][1])

    def test_check10_reads_a_bare_verdict_label_only_when_the_value_leads(self) -> None:
        """Plan labels count, and prose after a bare Verdict label does not.

        A plan review on 2026-09-23 closed with "Plan verdict: PASS" and had
        per-section lines such as the one below. The old parser skipped the
        Plan label, searched the whole section line, and reported BLOCK from
        the prose word "block"; had it read both, the review would have failed
        Check 10 with conflicting verdicts. Commit and Plan verdict lines keep
        the old reading, so a conditional verdict still counts there.
        """
        section = (
            "- **Verdict**: Deleting the entire `autoMode` block is the correct fix."
        )
        cases = {
            "plan-label": ("Plan verdict: PASS", "verdict=PASS"),
            "section-prose-then-plan-label": (
                f"{section}\n\nPlan verdict: PASS",
                "verdict=PASS",
            ),
            "section-prose-only": (section, "verdict=NONE"),
            "bare-label-value-first": ("- **Verdict**: BLOCK", "verdict=BLOCK"),
            "bare-label-alone-then-value": ("Verdict:\n\nBLOCK", "verdict=BLOCK"),
            "bare-label-prose-then-value": ("Verdict: explanatory prose\nBLOCK", "verdict=NONE"),
            "bare-heading-then-value": ("## Verdict\n\nPASS", "verdict=PASS"),
            "bare-heading-then-prose": (
                "## Verdict\n\nDeleting the entire block is right.",
                "verdict=NONE",
            ),
            "commit-label-conditional": (
                "Commit verdict: will be PASS if the remaining checks succeed.",
                "verdict=PASS",
            ),
            "heading-then-value": ("## Commit verdict\n\nBLOCKED", "verdict=BLOCK"),
            "dash-separated": ("Commit verdict \u2014 PASS, one Low open", "verdict=PASS"),
        }
        for name, (body, detail) in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as td:
                    td_path = Path(td)
                    review = td_path / "Review-Codex.md"
                    make_review(
                        review,
                        extra_body=f"Verification status: VERIFIED\n\n{body}",
                    )
                    state = make_state_dir(td_path)
                    result = run_health_py(state, review)
                    parsed = parse_output(result.stdout)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(parsed["check-10"][0], "PASS")
                    self.assertIn(detail, parsed["check-10"][1])

    def test_check7_warns_when_review_could_not_execute_commands(self) -> None:
        blocked_phrases = (
            "I was unable to execute any commands here",
            "I was unable to run the tests here",
            "I was not able to execute any commands here",
            "I was not able to run the tests here",
        )
        for phrase in blocked_phrases:
            with self.subTest(phrase=phrase):
                with tempfile.TemporaryDirectory() as td:
                    td_path = Path(td)
                    review = td_path / "Review-Codex.md"
                    make_review(
                        review,
                        extra_body=(
                            f"I inspected the diff. {phrase}.\n\n"
                            "Recommendation: BLOCK"
                        ),
                    )
                    state = make_state_dir(td_path)
                    result = run_health_py(state, review)
                    parsed = parse_output(result.stdout)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(parsed["check-7"][0], "WARN")
                    self.assertEqual(parsed["check-10"][0], "PASS")
                    self.assertIn("no-commit-verdict", parsed["check-10"][1])

    def test_check10_rejects_explicitly_empty_notes_without_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Codex.md"
            make_review(
                review,
                include_verification_notes=False,
                extra_body="Verification notes: none.\n\nRecommendation: BLOCK",
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review)
            parsed = parse_output(result.stdout)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(parsed["check-10"][0], "FAIL")
            self.assertIn("but-notes-report-none", parsed["check-10"][1])

    def test_check10_reads_the_status_after_an_inline_span_quoting_a_fence(
        self,
    ) -> None:
        """A four-backtick inline span that quotes a fence is one span.

        Code stripping once split the four-backtick run into a three-backtick
        fence, which then ran to the next fenced block and took the status
        line with it, so a VERIFIED review failed Check 10.
        """
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            review = td_path / "Review-Antigravity.md"
            make_review(
                review,
                extra_body=(
                    "- Rendered the README; the ```` ```python ```` fences "
                    "display.\n\n"
                    "Verification status: VERIFIED\n\n"
                    "Example:\n\n```python\nprint(1)\n```\n\n"
                    "Commit verdict: BLOCK"
                ),
            )
            state = make_state_dir(td_path)
            result = run_health_py(state, review)
            parsed = parse_output(result.stdout)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(parsed["check-10"][0], "PASS")
            self.assertIn("verdict=BLOCK verification=VERIFIED", parsed["check-10"][1])

    def test_code_stripping_matches_backtick_runs_by_length(self) -> None:
        """A longer fence holds a shorter one, and an unpaired run stays text.

        Each case puts a Check 7 phrase where code stripping decides whether
        it counts: inside code it must not warn, and after an unpaired run it
        must still be seen.
        """
        phrase = "I was unable to run the tests here"
        cases = {
            "four-backtick fence holding a three-backtick fence": (
                f"````md\n```\n{phrase}.\n```\n````\n\nRecommendation: BLOCK",
                "PASS",
            ),
            "double-backtick span holding a single backtick": (
                f"Quoted ``{phrase}` text`` once.\n\nRecommendation: BLOCK",
                "PASS",
            ),
            "unpaired four-backtick run before the phrase and a fence": (
                f"A stray ```` run. {phrase}.\n\n```\ncode\n```\n\n"
                "Recommendation: BLOCK",
                "WARN",
            ),
        }
        for name, (body, outcome) in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as td:
                    td_path = Path(td)
                    review = td_path / "Review-Codex.md"
                    make_review(review, extra_body=body)
                    state = make_state_dir(td_path)
                    result = run_health_py(state, review)
                    parsed = parse_output(result.stdout)
                    self.assertEqual(parsed["check-7"][0], outcome, result.stdout)

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


class HealthCheckWheelMirror(unittest.TestCase):
    """Non-mutating byte comparison of the source pair against its wheel copy.

    check-parity.sh already detects this drift, but it is a maintainer script
    that has to be run by hand. The existing aa-internal drift test synthesises
    a `bootstrap/packs.yaml` difference and only asserts that the script reports
    *that* file, so it can pass while implement-review is already out of sync.
    These assertions run in the default suite and read only.

    Skipped in repositories with no wheel-bundled composer tree (agent-config),
    which is what keeps this file byte-identical across the parity mirror.
    """

    COMPOSER = (
        ROOT / "packages" / "pypi" / "anywhere_agents" / "composer"
        / "skills" / "implement-review"
    )
    SOURCE = ROOT / "skills" / "implement-review"

    def setUp(self) -> None:
        if not self.COMPOSER.is_dir():
            self.skipTest("no wheel-bundled composer tree in this repository")

    def _assert_mirrored(self, relative: str) -> None:
        src = self.SOURCE / relative
        dst = self.COMPOSER / relative
        self.assertTrue(src.is_file(), f"missing source copy {src}")
        self.assertTrue(dst.is_file(), f"missing wheel copy {dst}")
        self.assertEqual(
            src.read_bytes(), dst.read_bytes(),
            f"{relative} has drifted between the source skill and its "
            f"wheel-bundled copy; re-run the mirror step before committing",
        )

    def test_health_check_py_matches_wheel_copy(self) -> None:
        self._assert_mirrored("scripts/health-check.py")

    def test_skill_md_matches_wheel_copy(self) -> None:
        self._assert_mirrored("SKILL.md")


if __name__ == "__main__":
    unittest.main()
