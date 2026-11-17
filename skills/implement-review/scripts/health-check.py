#!/usr/bin/env python3
"""health-check.py -- 10 structural Health checks + 3 Substance heuristics.

Cross-platform implementation invoked by health-check.sh / health-check.ps1
wrappers. See skills/implement-review/references/health-check.md > Phase 2.0
prologue for the checks, and SKILL.md > Phase 2.0 for the outcomes.

Usage:
  health-check.py --state-dir <abs-path> --round <N>
                  [--review-file <path>] [--prompt-file <path>]
                  [--lens <skill|code|doc|plan-review>]

Stdout schema (one machine-parseable line per check/heuristic):
  PASS|FAIL|WARN <code> [detail [detail...]]

Exit code:
  0 if no FAIL (WARN-only is exit 0)
  1 if any Check 1-6/10 FAIL or required dispatch state missing/stale
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
    r"(?:unable|not able) to (?:execute|run)",
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

# Check 8 failure vocabulary, split into two tiers (Fix B; see the Check 8 row
# of references/health-check.md and the W3 plan). INTRINSIC patterns are failure FORMS a runtime
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
    # Windows NTSTATUS 0xC000xxxx values printed as signed decimal exit codes.
    # The observed Store-alias pwsh shape is `-1073741502` (STATUS_DLL_INIT_FAILED);
    # this family also covers DLL-not-found and access-violation exits.
    # The number stays inside a code span so strip_code_spans removes it before
    # Check 8 scans; an unquoted literal here matches the pattern on the line
    # below and makes this file trip its own scanner.
    r"-107374\d{4}",
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
    r"-107374\d{4}",
]

# Documentation source sentences (W1b). These are exact prose fragments from
# this skill's own SKILL.md and references, and from the shared AGENTS.md that
# bootstrap installs at the root of every consuming project. A reviewer that reads those files
# carries the sentences into its stdout tail, where the check 8 patterns above
# then match benign documentation rather than a real failure.
#
# Removal is fragment-scoped, not line-scoped: only the matched sentence is cut
# and the rest of the line survives, so a real failure that TRAILS a quoted
# sentence still counts. Fragment scope alone is not sufficient, though. When a
# complete fragment sits inside a diagnostic line, that line's failure evidence
# lives INSIDE the fragment, so cutting it silences a real error. Removal is
# therefore also gated on the occurrence not sitting in diagnostic context; see
# DOC_DIAGNOSTIC_CONTEXT_RE and strip_doc_fragments below.
#
# Case-sensitive on purpose: these are canonical source strings, and matching
# case-insensitively would widen suppression past the three known shapes.
#
# Deliberately NOT added to REGEX_SOURCE_MARKERS: is_echo_line() treats a marker
# hit as grounds to skip the whole line, which would hide a real failure that
# happens to share the line with quoted documentation.
#
# Drift warning: these are exact strings, so one character of edit in SKILL.md,
# its references, or AGENTS.md silently disables the matching entry. The
# source-coupled tests in tests/test_health_check.py scan the live SKILL.md, the
# references, and AGENTS.md and exist to
# catch that; the inline fixtures alone would stay green straight through it.
DOC_ECHO_FRAGMENTS = [
    # Legacy pre-W1a shape from the SKILL.md Phase 2.0 check 8 row. W1a
    # backticked both occurrences, so strip_code_spans now removes them before
    # this list is consulted and the entry no longer fires against a current
    # SKILL.md. Retained for preserved pre-W1a tails, deleted-side diff text, and
    # consumers whose SKILL.md has not been refreshed. Back when it did fire, two
    # patterns matched nested spans of it, so each occurrence counted twice.
    "HTTP/status 429/5xx",
    # Legacy pre-W1a shape from the SKILL.md Substance heuristics paragraph. W1a
    # backticked the `rate limit` inside it, so the same reasoning applies.
    "tools silently failed mid-run; rate limit; context overflow; "
    "or the model did not engage",
    # AGENTS.md Tool-Use Reliability bullet. Unlike the two above, this one is
    # still live: AGENTS.md is deliberately not edited, and bootstrap installs it
    # at the root of every consuming project. It matches the intrinsic
    # ``tool .* failed`` pattern because "tool failures" carries the required
    # space, whereas references/health-check.md's "tools silently failed" does not.
    "transient-looking tool failures: a single failed attempt is weak evidence",
]

# A fragment sitting in diagnostic context is never stripped. Without this gate,
# ``ERROR: request failed: <complete fragment>`` loses every Check 8 marker it
# had, because the evidence lives inside the fragment rather than after it.
#
# The test is applied to the text BEFORE each fragment occurrence, not to the
# whole line from column zero. Anchoring at column zero was the first attempt and
# it failed in both directions: real diagnostics carry prefixes (an ISO
# timestamp, a JSON envelope, a ``codex_core::`` logger path) that pushed the
# severity token off column zero and silenced them, while a blanket
# "any label ending in failed:" branch matched benign prose such as
# ``Example failure:`` and restored the documentation false positive.
#
# Two context families, deliberately asymmetric:
#
# 1. Severity context may appear ANYWHERE in the prefix, so timestamps, log
#    levels, and logger paths do not hide it. Branch 1a accepts bare uppercase
#    tokens, branch 1b accepts mixed case before ``:`` or ``]``, and branch 2
#    accepts structured level fields.
# 2. Operational context (branch 3) accepts a bare ``<subject> failed:`` label
#    only at the start of the line and only for a maintained subject. The
#    maintained list is the point: it is narrower and far more predictable than
#    matching any word before a colon, which is what let "Example failure:"
#    through.
#
# The operational family takes only the VERB form ``failed:``, never the noun
# ``failure:``. That single distinction carries most of the precision here.
# Runtimes emit ``API call failed:``; documentation and headings write
# ``Tool failure:``. Accepting the noun form made ``Tool failure:`` and
# ``Build failure:`` read as diagnostics purely because their first word was on
# the subject list, which restored the AGENTS.md false positive. A genuine
# noun-form diagnostic is not lost: it almost always carries severity context,
# which branches 1a, 1b, and 2 still catch.
#
# The subject list is a maintained list and will need occasional additions.
# ``openai``, ``api``, ``exec``, and ``spawn`` were added after real dispatch-tail
# labels (``OpenAI API request failed:``, ``spawn failed:``) were found missing.
# The gap allowance is 3 words so multi-word subjects like
# ``OpenAI API request failed:`` still reach the verb.
DOC_DIAGNOSTIC_CONTEXT_RE = re.compile(
    r"(?:"
    # Branch 1a: a bare UPPERCASE severity token, anywhere in the prefix. This
    # is the observed Codex logger convention: a scan of preserved dispatch
    # tails found 18 ``codex_core::`` failures, all with uppercase severity and
    # none without. Uppercase alone is strong enough evidence to need no
    # punctuation.
    r"\b(?:ERROR|WARN(?:ING)?|FATAL|EXCEPTION|TRACEBACK)\b"
    # Branch 1b: mixed-case raw severity, but only when diagnostic punctuation
    # follows immediately. The punctuation is what separates a severity LABEL
    # from the same word used in prose: ``Error:``, ``error:``, ``Warning:`` and
    # ``[Error]`` are labels, while ``Error example:`` is a heading. An
    # uppercase-only branch was tried first and silenced every mixed-case
    # diagnostic; a case-insensitive branch without the punctuation guard put
    # the ``Error example:`` false positive back. This form is not hypothetical:
    # a preserved Copilot dispatch tail opens with
    # ``Error: Authentication token found but could not be validated.``
    r"|(?i:\b(?:error|warn(?:ing)?|fatal|exception|traceback)\b"
    r"(?=\s*[:\]]))"
    # Branch 2: a structured level field. Case-insensitive: JSON and logfmt
    # emitters write level=error, Level=ERROR, "severity":"Warning" alike.
    r"|(?i:[\"']?(?:level|severity)[\"']?\s*[:=]\s*[\"']?"
    r"(?:error|warn(?:ing)?|fatal|exception)\b)"
    # Branch 3: an operational subject followed by the VERB form. Case-
    # insensitive, since runtimes are inconsistent about capitalising subjects.
    r"|(?i:^\s*(?:\d+:\s*)?"
    r"(?:openai|codex|mcp|api|request|tool|command|dispatch|process|"
    r"subprocess|operation|connection|authentication|execution|exec|"
    r"run|worker|runner|backend|client|server|job|task|build|test|call|"
    r"spawn|shell)"
    r"(?:\s+[\w./-]+){0,3}\s+"
    r"(?:failed|denied|timed\s+out|timeout|refused|reset)\s*:)"
    r")",
)

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

VERIFICATION_STATUS_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|[-*+]\s+)?(?:\*\*|__)?verification\s+status"
    r"(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(VERIFIED|UNVERIFIED)\b",
    re.IGNORECASE | re.MULTILINE,
)
VERDICT_LABEL_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|[-*+]\s+)?(?:\*\*|__)?"
    r"(?P<kind>(?:commit|plan)\s+)?verdict\b(?P<rest>.*)$",
    re.IGNORECASE,
)
VERDICT_VALUE_RE = re.compile(
    r"\b(BLOCK(?:ED)?|PASS(?:ED)?|UNVERIFIED)\b",
    re.IGNORECASE,
)
# A bare "Verdict" label often opens a per-section judgment written as prose,
# such as "**Verdict**: deleting the whole block is right", so after a bare
# label the value must be the first word past the label's punctuation.
LEADING_VERDICT_VALUE_RE = re.compile(
    r"^[\s*_:=\u2013\u2014-]*(BLOCK(?:ED)?|PASS(?:ED)?|UNVERIFIED)\b",
    re.IGNORECASE,
)
VERDICT_DECORATION = " \t*_:=\u2013\u2014-"
VERIFICATION_NOTES_LABEL_RE = re.compile(
    r"^(?:verification|validation)\s+notes\b[.:]?\s*(.*)$",
    re.IGNORECASE,
)


BACKTICK_RUN_RE = re.compile(r"`+")


def code_span_bounds(text: str) -> list[tuple[int, int]]:
    """Return the start and end offsets of each paired backtick span in text.

    Backticks are read as whole runs: a span that opens with N backticks
    closes at the next run of exactly N. A span of one or two backticks stays
    on one line; three or more may cross lines, which covers fenced blocks and
    a fence inside a one-line JSON tail. A run with no partner is left as
    text. This is a stripping heuristic, not a Markdown parser: a fence closed
    by a longer run than it opened with stays visible. One reverse pass finds
    each run's next run of the same width, so the scan is linear in the text.
    health-check.py and dispatch-gemini.py carry the same copy, and a test
    keeps the two identical.
    """
    runs = list(BACKTICK_RUN_RE.finditer(text))
    next_same = [-1] * len(runs)
    last_by_width: dict[int, int] = {}
    for index in range(len(runs) - 1, -1, -1):
        width = runs[index].end() - runs[index].start()
        next_same[index] = last_by_width.get(width, -1)
        last_by_width[width] = index
    bounds: list[tuple[int, int]] = []
    index = 0
    while index < len(runs):
        opener = runs[index]
        partner = next_same[index]
        if partner != -1 and opener.end() - opener.start() < 3:
            if text.find("\n", opener.end(), runs[partner].start()) != -1:
                partner = -1
        if partner == -1:
            index += 1
            continue
        bounds.append((opener.start(), runs[partner].end()))
        index = partner + 1
    return bounds


def strip_code_spans(text: str) -> str:
    """Remove fenced ``` ... ``` blocks and inline `code` spans.

    Health checks 7 and 8 both apply this before pattern scanning so that
    Codex meta-discussing the pattern list (e.g., quoting
    `CreateProcessAsUserW failed: 1312` in its reasoning text or echoing a
    SKILL.md snippet that names the patterns) does not count as real
    failure narration. The same FP-tuning principle (references/health-check.md) that
    motivated this for the review body (Check 7) applies to the dispatch
    tail (Check 8) -- whenever /implement-review runs on the
    implement-review skill itself (or any review prompt that names the
    pattern strings), Codex's stdout carries backticked references that
    look like real tool errors without it. Check 10 reads the verification
    status and verdict from the same stripped text.

    The spans come from :func:`code_span_bounds`. Matching runs whole fixed
    the old failure: an inline four-backtick span quoting a fence was read as
    a three-backtick fence that ran to the next fenced block and removed the
    status line between them.
    """
    pieces: list[str] = []
    keep_from = 0
    for start, end in code_span_bounds(text):
        pieces.append(text[keep_from:start])
        keep_from = end
    pieces.append(text[keep_from:])
    return "".join(pieces)


def strip_doc_fragments(text: str) -> str:
    """Remove known documentation sentences before Check 8 pattern scanning.

    Applied *after* :func:`strip_code_spans` and *before* echo flags, frame
    flags, and pattern counting. The ordering is load-bearing: the fragments
    only take their final shape once neighbouring backticked spans have been
    removed, so they cannot be found by grepping the source files.

    Two guards keep this from eating real failures:

    1. **Context gate.** Each occurrence is kept when the text preceding it on
       the same line matches :data:`DOC_DIAGNOSTIC_CONTEXT_RE`. A complete
       fragment inside ``ERROR: request failed: ...`` holds that line's only
       failure evidence, so stripping it would turn a real WARN into a silent
       PASS. Judging the preceding text per occurrence, rather than the whole
       line, is what lets a timestamped or JSON-wrapped diagnostic keep its
       marker while benign prose on the same shape still gets stripped.
    2. **Fragment scope.** Only the matched sentence is cut, never the whole
       line, so evidence trailing the quoted documentation survives.

    Deliberately preserved from the previous implementation: a real error that
    appears only AFTER a benign fragment does not protect that fragment. The
    prefix is what decides, so ``<benign fragment> ERROR: ECONNRESET`` still
    strips the fragment and still counts the trailing error.
    """
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        for fragment in DOC_ECHO_FRAGMENTS:
            search_from = 0
            while True:
                index = line.find(fragment, search_from)
                if index < 0:
                    break
                if DOC_DIAGNOSTIC_CONTEXT_RE.search(line[:index]):
                    # Diagnostic context: keep this occurrence, look past it.
                    search_from = index + len(fragment)
                    continue
                line = line[:index] + line[index + len(fragment):]
        out.append(line)
    return "".join(out)


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


def extract_commit_verdicts(text: str) -> set[str]:
    """Return normalized verdict values from labeled verdict lines.

    Review files in the field use ``Commit verdict: BLOCKED``, ``Plan verdict:
    PASS`` in plan reviews, and a ``# Commit verdict`` heading followed by
    ``BLOCKED`` on the next nonempty line. Restricting detection to a line-start
    label avoids treating a finding that merely discusses the word "block" as
    the review's verdict. A bare ``Verdict`` label also opens per-section
    judgments written as prose, so after it only a value that leads the text
    counts, and the next line is read only when the label stands alone.
    """
    verdicts: set[str] = set()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = VERDICT_LABEL_RE.match(line)
        if not match:
            continue
        rest = match.group("rest")
        formal = match.group("kind") is not None
        value_re = VERDICT_VALUE_RE if formal else LEADING_VERDICT_VALUE_RE
        candidates = [rest]
        if formal or not rest.strip(VERDICT_DECORATION):
            for following in lines[index + 1:index + 5]:
                if following.strip():
                    candidates.append(following)
                    break
        for candidate in candidates:
            value_match = value_re.search(candidate)
            if not value_match:
                continue
            value = value_match.group(1).upper()
            if value.startswith("BLOCK"):
                verdicts.add("BLOCK")
            elif value.startswith("PASS"):
                verdicts.add("PASS")
            else:
                verdicts.add("UNVERIFIED")
            break
    return verdicts


def verification_notes_explicitly_empty(text: str) -> bool:
    """True when the Verification notes section says no command was executed."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        plain = re.sub(r"^\s*(?:#{1,6}\s+|[-*+]\s+)", "", line)
        plain = plain.replace("**", "").replace("__", "").strip()
        label = VERIFICATION_NOTES_LABEL_RE.match(plain)
        if not label:
            continue

        inline_notes = label.group(1).strip().replace("**", "").replace("__", "")
        if re.match(r"^(?:none|n/?a)\s*[.!]?\s*$", inline_notes, re.IGNORECASE):
            return True
        block_lines = [inline_notes]
        started = bool(block_lines[0])
        for following in lines[index + 1:index + 21]:
            stripped = following.strip()
            if re.match(r"^#{1,6}\s+", stripped):
                break
            if not stripped:
                if started:
                    break
                continue
            started = True
            block_lines.append(stripped)

        block = " ".join(part for part in block_lines if part).strip()
        block = block.replace("**", "").replace("__", "")
        if re.match(r"^(?:none|n/?a)\s*[.!]?\s*$", block, re.IGNORECASE):
            return True
        if re.search(
            r"\b(?:no (?:shell )?commands? or (?:project )?tests? were run|"
            r"no commands? (?:were )?(?:run|executed)|"
            r"nothing was (?:run|executed|verified))\b",
            block,
            re.IGNORECASE,
        ):
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


def emit_check_8(tail_file: Path, tail_stderr_file: Path) -> None:
    # ----- Check 8: dispatch-tail tool failures (line classifier + two-tier) -----
    # Fix A (echo classifier) + Fix B (intrinsic/generic) per references/health-check.md
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
        tail_no_code = strip_doc_fragments(strip_code_spans(tail_text))

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

        # dispatch-gemini.py writes backend-failure just before it publishes a
        # review from a result that ended in a backend error. Its structure check
        # does not show the findings are finished, so the recovery must not
        # advance silently: count it as a marker and let the WARN checkpoint
        # surface it.
        if (tail_file.parent / "backend-failure").exists():
            total_hits += 1
            per_pattern_counts["recovered-backend-error"] = 1

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


def emit_check_9(stall_warning_file: Path) -> None:
    if stall_warning_file.exists():
        stall_text = stall_warning_file.read_text(encoding="utf-8")
        stall_count = stall_text.count("STALL ")
        emit("WARN", "check-9", str(stall_count), "stall-periods")
    else:
        emit("PASS", "check-9", "no-stall-warning")


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
        # A missing review is the failure mode Check 8 exists to diagnose: the
        # tail can fill with repeated spawn failures while no review is written.
        emit_check_8(tail_file, tail_stderr_file)
        emit_check_9(stall_warning_file)
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
        r"^[ \t]*Verification notes[ \t]*$",
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
    emit_check_8(tail_file, tail_stderr_file)

    # ----- Check 9: stall-warning file absence -----
    emit_check_9(stall_warning_file)

    # ----- Check 10: a verdict must carry machine-readable verification -----
    # The dispatcher requires one standalone Verification status marker. This
    # turns the previously prose-only "I could not run anything" degradation
    # into a hard health-check failure while leaving non-verdict review formats
    # backward compatible.
    verification_statuses = {
        match.upper() for match in VERIFICATION_STATUS_RE.findall(review_no_code)
    }
    verdicts = extract_commit_verdicts(review_no_code)
    check10_detail = None
    if len(verification_statuses) > 1:
        check10_detail = "conflicting-verification-statuses=" + ",".join(
            sorted(verification_statuses)
        )
    elif len(verdicts) > 1:
        check10_detail = "conflicting-verdicts=" + ",".join(sorted(verdicts))
    else:
        status = next(iter(verification_statuses), None)
        verdict = next(iter(verdicts), None)
        if status == "UNVERIFIED":
            check10_detail = f"verdict={verdict or 'NONE'} verification=UNVERIFIED"
        elif verdict == "UNVERIFIED":
            check10_detail = f"verdict=UNVERIFIED verification={status or 'MISSING'}"
        elif verdict in {"PASS", "BLOCK"} and status != "VERIFIED":
            check10_detail = f"verdict={verdict} verification=MISSING"
        elif verification_notes_explicitly_empty(review_no_code):
            check10_detail = (
                f"verdict={verdict or 'NONE'} "
                f"verification={status or 'MISSING'} "
                "but-notes-report-none"
            )

    if check10_detail is not None:
        emit("FAIL", "check-10", check10_detail)
        any_fail = True
    else:
        status = next(iter(verification_statuses), None)
        verdict = next(iter(verdicts), None)
        if status == "VERIFIED":
            emit(
                "PASS", "check-10",
                f"verdict={verdict or 'NONE'}", "verification=VERIFIED",
            )
        else:
            emit("PASS", "check-10", "no-commit-verdict")

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
