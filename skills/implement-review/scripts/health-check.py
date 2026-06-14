#!/usr/bin/env python3
"""health-check.py -- 9 structural Health checks + 3 Substance heuristics.

Cross-platform implementation invoked by health-check.sh / health-check.ps1
wrappers. See skills/implement-review/SKILL.md > Phase 2.0 prologue for the
contract.

Usage:
  health-check.py --state-dir <abs-path> --round <N>
                  [--review-file <path>] [--prompt-file <path>]
                  [--lens <skill|code|doc|plan-review>]

Stdout schema (one machine-parseable line per check/heuristic):
  PASS|FAIL|WARN <code> [detail [detail...]]

Exit code:
  0 if no FAIL (WARN-only is exit 0)
  1 if any Check 1-6 FAIL or required dispatch state missing/stale
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


SUSPICIOUS_PHRASES = [
    r"could not",
    r"i cannot",
    r"failed to",
    r"permission denied",
    r"rate limit",
    r"unable to access",
    r"do not have access",
    r"not authenticated",
    r"authentication failed",
    r"unauthorized",
    r"timed out",
    r"timeout",
    r"quota",
    r"command not found",
    r"no such file",
    r"sandbox.*fail",
]

# Check 8 failure vocabulary, split into two tiers (Fix B; see SKILL.md
# Phase 2.0 and the W3 plan). INTRINSIC patterns are failure FORMS a runtime
# emits; they count on their own. GENERIC patterns are ambiguous words that also
# appear in benign prose; they count only when an error-frame token sits on the
# same or an adjacent (non-echo) line. The split closes the false-negative class
# of terse real failures (e.g. a bare ``Rate limit exceeded`` line) that an
# earlier blanket "needs an error frame" rule would have dropped.
INTRINSIC_FAILURE_PATTERNS = [
    r"tool .* failed",
    r"mcp tool failed",
    r"HTTP/\S* (?:429|5\d\d)",
    r"status (?:429|5\d\d)",
    r"too many requests",
    r"rate limit exceeded",
    r"rate limited",
    r"service unavailable",
    r"quota exceeded",
    r"insufficient_quota",
    r"connection refused",
    r"connection reset",
    r"connection timed out",
    r"context_length_exceeded",
    r"maximum context length",
    r"CreateProcessAsUserW failed: 1312",
    r"windows sandbox: runner error",
    r"sandbox.*runner error",
    r"\bENOSPC\b",
    r"\bEACCES\b",
    r"\bETIMEDOUT\b",
    r"\bECONNRESET\b",
    r"\bECONNREFUSED\b",
]

# Generic words: count only with an error-frame token nearby. Bare "rate limit"
# appears in benign prose ("the rate limit logic"); the failure FORM "rate limit
# exceeded" is intrinsic above.
GENERIC_FAILURE_PATTERNS = [
    # No \b anchors: they would pollute the auto-generated breakdown label
    # ("brate") for negligible precision. The spaces around "rate limit" already
    # bound it in practice, and a stray "moderate limit" would only count next
    # to an error frame, a benign WARN.
    r"rate limit",
]

# Error-frame tokens that license a GENERIC pattern to count on the same or an
# adjacent non-echo line.
ERROR_FRAME_PATTERNS = [
    r"\berror\b", r"\bfailed\b", r"\bfailure\b", r"\bfatal\b", r"\bexception\b",
    r"\btraceback\b", r"\bdenied\b", r"\bunauthorized\b", r"\bforbidden\b",
    r"\btimeout\b", r"\btimed out\b", r"\brefused\b", r"\breset\b",
    r"\bunavailable\b", r"\bunreachable\b", r"\baborted\b", r"\bcancell?ed\b",
    r"\bnon-zero\b", r"\bexit code\b", r"\bstatus code\b", r"\berrno\b",
    r"\bAPI\b", r"\bRPC\b", r"\bHTTP\b", r"\bTLS\b", r"\bSSL\b", r"\bDNS\b",
    r"\bgetaddrinfo\b",
]

# Diagnostic tokens that, at the start of a line-numbered line, mark it as a real
# runner/log line rather than a source citation (Fix A). Such a line is never
# classified as an echo. ``E[A-Z]{3,}`` matches errno symbols (EACCES, ENOSPC).
DIAGNOSTIC_LINE_TOKENS = [
    r"ERROR", r"WARN", r"FATAL", r"Traceback", r"HTTP/", r"status", r"errno",
    r"E[A-Z]{3,}",
]

# Literal regex-source fragments (Fix A). A tail line containing one of these is
# the health-check pattern list being quoted (codex reading this file or the
# tests), not a real failure. These are EXACT pattern sources with regex
# metacharacters; a real failure line or a Windows path never contains them. We
# deliberately do NOT use a bare-backslash or bare-line-number predicate: a
# Windows path (``C:\bin``) and a line-numbered real diagnostic
# (``12: ERROR: HTTP/1.1 429``) must still count.
REGEX_SOURCE_MARKERS = [
    r"HTTP/\S* (?:429|5\d\d)",
    r"status (?:429|5\d\d)",
    r"sandbox.*runner error",
    r"\bENOSPC\b",
    r"\bEACCES\b",
    r"\bETIMEDOUT\b",
    r"\bECONNRESET\b",
    r"\bECONNREFUSED\b",
    r"tool .* failed",
]

# Back-compat: callers/tests that import the flat union still work.
TOOL_FAILURE_PATTERNS = INTRINSIC_FAILURE_PATTERNS + GENERIC_FAILURE_PATTERNS

ANCHOR_PATTERNS = [
    r":\d+\b",
    r"\bline \d+\b",
    r"\blines? \d+\s*[-–]\s*\d+\b",
]

AXIS_2_KEYWORDS = ("deferral", "process tax", "release cycle")
AXIS_3_KEYWORDS = (
    "simplest", "do nothing", "doing nothing", "smaller path",
    "shrink", "docs only", "document only", "script only", "no-op",
)


def strip_code_spans(text: str) -> str:
    """Remove fenced ``` ... ``` blocks and inline `code` spans.

    Health checks 7 and 8 both apply this before pattern scanning so that
    Codex meta-discussing the pattern list (e.g., quoting
    `CreateProcessAsUserW failed: 1312` in its reasoning text or echoing a
    SKILL.md snippet that names the patterns) does not count as real
    failure narration. The same SKILL.md FP-tuning principle that
    motivated this for the review body (Check 7) applies to the dispatch
    tail (Check 8) -- whenever /implement-review runs on the
    implement-review skill itself (or any review prompt that names the
    pattern strings), Codex's stdout carries backticked references that
    look like real tool errors without it.
    """
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", "", text)
    return text


def is_echo_line(line: str, intrinsic_res: list, diagnostic_re) -> bool:
    r"""True when a tail line is a modeled pattern-echo that must NOT count as a
    Check 8 marker (Fix A). Conservative by design: a real failure line is never
    classified as an echo (real-failure-wins). Two modeled echo shapes:

    1. A literal regex-source line: the line quotes a health-check pattern
       definition (regex metacharacters, e.g. ``HTTP/\S* (?:429|5\d\d)`` or
       ``\bENOSPC\b``). These exact fragments never appear in a real failure
       line or a Windows path.
    2. A line-numbered source citation (``^\s*\d+: ...``) whose content is
       neither a runner/log diagnostic (ERROR, HTTP/, status, errno, ...) nor a
       match of any intrinsic failure pattern. A line-numbered real diagnostic
       such as ``12: ERROR: HTTP/1.1 429`` is therefore kept, not suppressed.

    A bare line-number prefix or a backslash is never sufficient to suppress a
    line; that would drop real Windows-path and line-numbered failures.
    """
    for marker in REGEX_SOURCE_MARKERS:
        if marker in line:
            return True
    m = re.match(r"^\s*\d+:\s?(.*)$", line)
    if m:
        rest = m.group(1)
        if diagnostic_re.match(rest):
            return False
        for pat_re in intrinsic_res:
            if pat_re.search(rest):
                return False
        # Intentional intrinsic/generic asymmetry (kept by design): an INTRINSIC
        # match above keeps the line (real-failure-wins, strong evidence). A
        # GENERIC match (bare "rate limit") is deliberately NOT checked here, so
        # a line-numbered generic line stays classified as an echo. Generic
        # patterns are weak evidence and a `\d+:` prefix is a citation signal
        # (grep / file-read output); a real generic failure carries no such
        # prefix. Treating it as an echo avoids re-counting source citations of
        # rate-limit / error-handling code, which is the W3 FP-reduction goal.
        return True
    return False


def emit(kind: str, code: str, *parts: str) -> None:
    rest = (" " + " ".join(parts)) if parts else ""
    print(f"{kind} {code}{rest}", flush=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--state-dir", required=True, type=Path)
    p.add_argument("--round", required=True, type=int, dest="round_num")
    p.add_argument("--review-file", default=Path("Review-Codex.md"), type=Path)
    p.add_argument("--prompt-file", default=None, type=Path)
    p.add_argument(
        "--lens", default=None,
        choices=["skill", "code", "doc", "plan-review", "default"],
    )
    return p.parse_args(argv)


def read_int_file(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return int(text)
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    state_dir: Path = args.state_dir
    review_path: Path = args.review_file

    if not state_dir.is_dir():
        emit("FAIL", "state-contract", f"state-dir-missing:{state_dir}")
        return 1

    pre_mtime_file = state_dir / "pre-mtime"
    timestamp_file = state_dir / "timestamp"
    tail_file = state_dir / "tail"
    tail_stderr_file = state_dir / "tail.stderr-tmp"
    stall_warning_file = state_dir / "stall-warning"

    pre_mtime = read_int_file(pre_mtime_file)
    dispatch_time = read_int_file(timestamp_file)
    if pre_mtime is None or dispatch_time is None:
        emit("FAIL", "state-contract",
             f"missing-or-unreadable pre-mtime={pre_mtime_file.exists()} "
             f"timestamp={timestamp_file.exists()}")
        return 1

    any_fail = False

    # ----- Check 1: review file exists -----
    if not review_path.exists():
        emit("FAIL", "check-1", f"review-file-missing:{review_path}")
        # Cannot do file-content checks; still surface state of Check 8, 9
        if not tail_file.exists():
            emit("WARN", "check-8", "1", "missing-dispatch-tail")
        else:
            emit("PASS", "check-8", "tail-present-not-scanned")
        emit("PASS" if not stall_warning_file.exists() else "WARN",
             "check-9",
             "no-stall-warning" if not stall_warning_file.exists() else "stall-warning-present")
        return 1

    emit("PASS", "check-1", "review-file-exists")

    review_text = review_path.read_text(encoding="utf-8", errors="replace")
    review_mtime = int(review_path.stat().st_mtime)

    # ----- Check 2: freshness -----
    if review_mtime > dispatch_time and review_mtime > pre_mtime:
        emit("PASS", "check-2", f"fresh review-mtime={review_mtime}")
    else:
        emit("FAIL", "check-2",
             f"mtime-not-fresh review-mtime={review_mtime} "
             f"dispatch={dispatch_time} pre={pre_mtime}")
        any_fail = True

    # ----- Check 3: round marker -----
    lines = review_text.splitlines()
    first = lines[0].rstrip() if lines else ""
    expected_marker = f"<!-- Round {args.round_num} -->"
    if first == expected_marker:
        emit("PASS", "check-3", "round-marker")
    else:
        emit("FAIL", "check-3", f"first-line={first!r} expected={expected_marker!r}")
        any_fail = True

    # ----- Check 4: size -----
    size = len(review_text)
    if size >= 500:
        emit("PASS", "check-4", f"size={size}")
    else:
        emit("FAIL", "check-4", f"size-too-small={size}")
        any_fail = True

    # ----- Check 5: Verification notes -----
    vn_patterns = [
        r"^#{1,6}\s+Verification notes\b",
        r"^\*\*Verification notes[.:]?\*\*",
        r"Verification notes[.:]",
    ]
    if any(re.search(p, review_text, re.MULTILINE | re.IGNORECASE) for p in vn_patterns):
        emit("PASS", "check-5", "verification-notes-present")
    else:
        emit("FAIL", "check-5", "verification-notes-missing")
        any_fail = True

    # ----- Check 6: scope correspondence (optional, depends on --prompt-file) -----
    prompt_text = ""
    if args.prompt_file is not None and args.prompt_file.exists():
        prompt_text = args.prompt_file.read_text(encoding="utf-8", errors="replace")

    if prompt_text:
        plan_refs = re.findall(r"\bPLAN-[\w\-]+\.md\b", prompt_text)
        # Backticked filenames in prompt; conservative pattern -- avoid matching whole sentences.
        file_refs = re.findall(r"`([^`\s]+\.(?:md|py|sh|ps1|tex|rst|toml|yaml|yml|json))`", prompt_text)
        all_refs = sorted(set(plan_refs) | set(file_refs))
        if all_refs:
            matched = [r for r in all_refs if r in review_text]
            if matched:
                emit("PASS", "check-6", f"scope-mentions={len(matched)}/{len(all_refs)}")
            else:
                emit("FAIL", "check-6",
                     f"no-scope-files-mentioned prompt-refs={','.join(all_refs)}")
                any_fail = True
        else:
            emit("PASS", "check-6", "no-explicit-files-in-prompt")
    else:
        emit("PASS", "check-6", "no-prompt-file-provided")

    # ----- Check 7: review-text suspicious phrases (exclude code spans) -----
    review_no_code = strip_code_spans(review_text)
    pattern_7 = re.compile("|".join(SUSPICIOUS_PHRASES), re.IGNORECASE)
    check7_hits: list[int] = []
    for line_num, line in enumerate(review_no_code.splitlines(), 1):
        if pattern_7.search(line):
            check7_hits.append(line_num)
    if check7_hits:
        emit("WARN", "check-7", str(len(check7_hits)),
             "lines=" + ",".join(str(n) for n in check7_hits))
    else:
        emit("PASS", "check-7", "0-suspicious-phrases")

    # ----- Check 8: dispatch-tail tool failures (line classifier + two-tier) -----
    # Fix A (echo classifier) + Fix B (intrinsic/generic) per SKILL.md Phase 2.0
    # and the W3 plan. Scan line by line: skip modeled pattern-echo lines, count
    # intrinsic failure patterns on their own, and count generic words only when
    # an error-frame token sits on the same or an adjacent non-echo line.
    if not tail_file.exists():
        emit("WARN", "check-8", "1", "missing-dispatch-tail")
    else:
        tail_text = tail_file.read_text(encoding="utf-8", errors="replace")
        if tail_stderr_file.exists():
            tail_text += "\n" + tail_stderr_file.read_text(
                encoding="utf-8", errors="replace"
            )
        tail_no_code = strip_code_spans(tail_text)

        intrinsic_compiled = [
            (p, re.compile(p, re.IGNORECASE)) for p in INTRINSIC_FAILURE_PATTERNS
        ]
        generic_compiled = [
            (p, re.compile(p, re.IGNORECASE)) for p in GENERIC_FAILURE_PATTERNS
        ]
        frame_re = re.compile("|".join(ERROR_FRAME_PATTERNS), re.IGNORECASE)
        diagnostic_re = re.compile(
            r"^\s*(?:" + "|".join(DIAGNOSTIC_LINE_TOKENS) + r")", re.IGNORECASE
        )
        intrinsic_res = [r for _, r in intrinsic_compiled]

        lines = tail_no_code.splitlines()
        echo_flags = [
            is_echo_line(ln, intrinsic_res, diagnostic_re) for ln in lines
        ]
        # Only a real (non-echo) line may license a generic match via adjacency.
        frame_flags = [
            (not echo_flags[i]) and bool(frame_re.search(ln))
            for i, ln in enumerate(lines)
        ]

        per_pattern_counts: dict[str, int] = {}
        total_hits = 0
        for idx, line in enumerate(lines):
            if echo_flags[idx]:
                continue
            for pat_src, pat_re in intrinsic_compiled:
                n = len(pat_re.findall(line))
                if n:
                    per_pattern_counts[pat_src] = (
                        per_pattern_counts.get(pat_src, 0) + n
                    )
                    total_hits += n
            near_frame = (
                frame_flags[idx]
                or (idx > 0 and frame_flags[idx - 1])
                or (idx + 1 < len(lines) and frame_flags[idx + 1])
            )
            if near_frame:
                for pat_src, pat_re in generic_compiled:
                    n = len(pat_re.findall(line))
                    if n:
                        per_pattern_counts[pat_src] = (
                            per_pattern_counts.get(pat_src, 0) + n
                        )
                        total_hits += n

        if total_hits:
            # Compact per-pattern breakdown so downstream Claude can recognize
            # known-noise shapes (e.g. WSL-stub-bash 1312 burst when Substance
            # heuristics pass) without re-grepping. Example breakdown shape:
            #   `1312:30 windows-sandbox:24 rate-limit:13 429:11`
            top = sorted(per_pattern_counts.items(), key=lambda kv: -kv[1])
            breakdown_parts = []
            for pat_src, n in top:
                # Short label: longest run of word chars in the pattern,
                # lowercased, max 20 chars. Falls back to the raw pattern.
                words = re.findall(r"[A-Za-z0-9_]+", pat_src)
                label = max(words, key=len).lower()[:20] if words else pat_src
                breakdown_parts.append(f"{label}:{n}")
            breakdown = " ".join(breakdown_parts)
            emit(
                "WARN", "check-8", str(total_hits),
                "tool-failure-markers", f"breakdown={breakdown}",
            )
        else:
            emit("PASS", "check-8", "0-tool-failure-markers")

    # ----- Check 9: stall-warning file absence -----
    if stall_warning_file.exists():
        stall_text = stall_warning_file.read_text(encoding="utf-8")
        stall_count = stall_text.count("STALL ")
        emit("WARN", "check-9", str(stall_count), "stall-periods")
    else:
        emit("PASS", "check-9", "no-stall-warning")

    # ----- Substance 1: time-to-completion floor -----
    prompt_chars = len(prompt_text)
    if prompt_chars >= 2000:
        elapsed = review_mtime - dispatch_time
        if elapsed < 30:
            emit("WARN", "substance-1",
                 f"elapsed={elapsed}s", f"prompt={prompt_chars}chars")
        else:
            emit("PASS", "substance-1", f"elapsed={elapsed}s")
    else:
        emit("PASS", "substance-1",
             f"prompt-below-threshold={prompt_chars}chars")

    # ----- Substance 2: anchor density -----
    if size > 1000:
        anchor_re = re.compile("|".join(ANCHOR_PATTERNS))
        anchor_count = len(anchor_re.findall(review_text))
        if anchor_count == 0:
            emit("WARN", "substance-2", f"chars={size}", "0-anchors")
        else:
            emit("PASS", "substance-2", f"anchors={anchor_count}")
    else:
        emit("PASS", "substance-2", f"review-below-threshold={size}chars")

    # ----- Substance 3: scope-challenge engagement (plan-review only) -----
    if args.lens == "plan-review":
        rl = review_text.lower()
        axis_1 = "scope" in rl and ("smaller" in rl or "larger" in rl)
        axis_2 = any(kw in rl for kw in AXIS_2_KEYWORDS)
        axis_3 = any(kw in rl for kw in AXIS_3_KEYWORDS)
        missing = [str(n) for n, ok in [(1, axis_1), (2, axis_2), (3, axis_3)] if not ok]
        if missing:
            emit("WARN", "substance-3", "missing-axes=" + ",".join(missing))
        else:
            emit("PASS", "substance-3", "all-axes-engaged")
    else:
        lens_label = args.lens or "unspecified"
        emit("PASS", "substance-3", f"non-plan-review-lens-skipped lens={lens_label}")

    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
