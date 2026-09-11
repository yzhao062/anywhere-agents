"""Static contract tests for skills/implement-review/SKILL.md.

One review loop on a small documentation patch produced the same class of defect
repeatedly, and every instance was found by someone reading carefully rather than
by anything mechanical: an inserted Phase 1b item renumbered the list and left a
duplicate behind; a cross-reference stayed at the pre-insertion number and
survived a hand search because it is written "Phase 1b, item 7" with a comma; and
the prompt template's hard-coded diff command was replaced in one place while
four siblings kept the old unscoped form. All three are decidable by reading the
file, so they belong here rather than in a reviewer's context.

Four earlier drafts of this file were defeated on the round after they were
written. Requiring bold titles hid an unbolded duplicate. Bounding sections to H2
through H4 let a deeper heading swallow the list. Searching the whole document
let the template be rewritten while a phrase elsewhere kept the assertion green.
Walking fences separately in each helper let a decoy fence, an unclosed fence,
and a duplicated section marker each produce a false green. Every one of those
was a symptom of reading Markdown as flat text, or of reading it more than once.

So the document is scanned exactly once, into an offset-preserving prose mask
plus explicit fence spans, and all three consumers resolve their anchors against
that single view. The mutation table at the bottom is part of the suite rather
than a separate harness, because a later edit to the scanner must not be able to
restart this loop.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = ROOT / "skills" / "implement-review" / "SKILL.md"

LIST_INTRO = "Prepare a review request with:"
TERMINAL_PATH_MARKER = "**Terminal path**:"
TERMINAL_PATH_END_MARKER = "**Auto-terminal path (opt-in)**:"

ITEM_RE = re.compile(r"^(\d+)\.\s+(.+)$")
HEADING_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+", re.M)
# Both spellings are in use, and searching only the first is how a stale
# reference survived a round.
REFERENCE_RE = re.compile(r"Phase 1b,? item (\d+)")
# CommonMark: up to three leading spaces, backticks or tildes. An opener may
# carry an info string; a closer may be followed only by whitespace. Treating
# them as one pattern let an opening ```python line close an outer fence.
OPEN_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
CLOSE_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")

# Every cross-reference into the Phase 1b list, keyed by a distinctive phrase
# from its own sentence, mapped to the item title it is talking about.
#
# Deliberately a registry rather than a resolver. A resolver can only check that
# a referenced number exists, and the defect this replaces was a reference to
# item 7 at a time when item 7 existed and meant something else. Intent is not
# derivable from the text.
EXPECTED_REFERENCES = {
    "Findings flagged Refuted or Inconclusive": "Round history",
    "This history carries forward": "Round history",
    "you may parallelize across scopes": "Splitting the round",
}


class _Fence(NamedTuple):
    start: int
    content_start: int
    content_end: int
    end: int
    complete: bool


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8", errors="replace")


def _line_body(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    return line[:-1] if line.endswith(("\r", "\n")) else line


def _opening_fence(body: str) -> str | None:
    match = OPEN_FENCE_RE.fullmatch(body)
    if not match:
        return None
    run, tail = match.groups()
    # A backtick info string may not itself contain a backtick.
    return None if run[0] == "`" and "`" in tail else run


def _closes(body: str, opener: str) -> bool:
    match = CLOSE_FENCE_RE.fullmatch(body)
    return (bool(match) and match.group(1)[0] == opener[0]
            and len(match.group(1)) >= len(opener))


def _blank_fence_line(line: str) -> str:
    return "".join(c if c in "\r\n" else " " for c in line)


def _markdown_regions(text: str) -> tuple[str, list[_Fence]]:
    """One scan: a length-preserving prose mask, and every fence span.

    Offsets in the mask line up with the original, so an index found in one is
    valid in the other. An unclosed fence is recorded as incomplete and runs to
    the end of the document, which is what Markdown renderers do with it.
    """
    out: list[str] = []
    fences: list[_Fence] = []
    opener: str | None = None
    fence_start = content_start = offset = 0
    for line in text.splitlines(keepends=True):
        body = _line_body(line)
        if opener is None:
            run = _opening_fence(body)
            if run is None:
                out.append(line)
            else:
                opener, fence_start = run, offset
                content_start = offset + len(line)
                out.append(_blank_fence_line(line))
        else:
            out.append(_blank_fence_line(line))
            if _closes(body, opener):
                fences.append(_Fence(fence_start, content_start, offset,
                                     offset + len(line), True))
                opener = None
        offset += len(line)
    if opener is not None:
        fences.append(_Fence(fence_start, content_start, len(text), len(text), False))
    masked = "".join(out)
    if len(masked) != len(text):
        raise AssertionError("Markdown mask changed document offsets")
    return masked, fences


def _unique_unfenced_index(masked: str, marker: str) -> int:
    positions = [m.start() for m in re.finditer(re.escape(marker), masked)]
    if len(positions) != 1:
        raise AssertionError(f"expected one unfenced {marker!r}, found {len(positions)}")
    return positions[0]


def _terminal_prompt_fence(masked: str, fences: list[_Fence]) -> _Fence:
    start = _unique_unfenced_index(masked, TERMINAL_PATH_MARKER)
    end = _unique_unfenced_index(masked, TERMINAL_PATH_END_MARKER)
    if end <= start:
        raise AssertionError("Terminal path end marker precedes its start marker")
    blocks = [f for f in fences if f.complete and start < f.start and f.end <= end]
    if len(blocks) != 1:
        raise AssertionError("expected exactly one complete fenced block in the "
                             f"Terminal path subsection, found {len(blocks)}")
    return blocks[0]


def _phase_1b_items(text: str) -> list[tuple[int, str]]:
    masked, _ = _markdown_regions(text)
    start = _unique_unfenced_index(masked, LIST_INTRO) + len(LIST_INTRO)
    end = HEADING_RE.search(masked, start)
    section = masked[start:(end.start() if end else len(masked))]
    items: list[tuple[int, str]] = []
    for line in section.splitlines():
        match = ITEM_RE.match(line)
        if not match:
            continue
        body = match.group(2)
        # Number first, bold title second. Requiring bold in the pattern is how
        # an unbolded duplicate number passed unseen.
        title = re.match(r"\*\*(.+?)\*\*", body)
        items.append((int(match.group(1)), title.group(1) if title else body))
    return items


def _terminal_prompt_template(text: str) -> str:
    masked, fences = _markdown_regions(text)
    block = _terminal_prompt_fence(masked, fences)
    return text[block.content_start:block.content_end]


def _reference_view(text: str) -> str:
    """Prose, plus the canonical prompt body, and nothing else fenced.

    A blanket mask would be wrong: the `you may parallelize across scopes`
    reference deliberately lives inside the canonical prompt fence. Exposing
    only that one block keeps it while still refusing a decoy reference planted
    in an arbitrary fence elsewhere.
    """
    masked, fences = _markdown_regions(text)
    block = _terminal_prompt_fence(masked, fences)
    body = text[block.content_start:block.content_end]
    inner, _ = _markdown_regions(body)
    return masked[:block.content_start] + inner + masked[block.content_end:]


def _sentence_around(text: str, index: int) -> str:
    start = max(text.rfind(". ", 0, index), text.rfind("\n", 0, index)) + 1
    end = text.find(". ", index)
    return text[start:(end if end != -1 else len(text))]


# --------------------------------------------------------------- the checks
# Written against arbitrary text so the live document and every mutation below
# exercise the same code. A check raises AssertionError when the contract is
# violated, which is what makes the mutation table expressible as tests.

def check_numbering(text: str) -> None:
    items = _phase_1b_items(text)
    if len(items) < 5:
        raise AssertionError(f"Phase 1b list not found or too short: {items}")
    numbers = [n for n, _ in items]
    if numbers != list(range(1, len(numbers) + 1)):
        raise AssertionError(f"Phase 1b numbering is not 1..N: {items}")
    if len(set(numbers)) != len(numbers):
        raise AssertionError(f"duplicate Phase 1b item number: {items}")


def check_references(text: str) -> None:
    titles = dict(_phase_1b_items(text))
    seen: list[str] = []
    for match in REFERENCE_RE.finditer(_reference_view(text)):
        sentence = _sentence_around(text, match.start())
        matched = [(p, t) for p, t in EXPECTED_REFERENCES.items() if p in sentence]
        if len(matched) != 1:
            raise AssertionError("a Phase 1b cross-reference must match exactly one "
                                 f"registry entry: {sentence.strip()[:160]!r}")
        phrase, expected = matched[0]
        seen.append(phrase)
        actual = titles.get(int(match.group(1)))
        if actual != expected:
            raise AssertionError(
                f"reference points at item {match.group(1)} ({actual!r}) but the "
                f"sentence is about {expected!r}: {sentence.strip()[:160]!r}")
    if sorted(seen) != sorted(EXPECTED_REFERENCES):
        raise AssertionError(f"every registered reference must occur exactly once: {seen}")


def check_template(text: str) -> None:
    template = _terminal_prompt_template(text)
    # Phase 1b chooses the command, so the template carries the choice rather
    # than a literal. A bare `git diff --cached` still appears legitimately
    # elsewhere in the file: as one of the two options Phase 1b picks between,
    # inside a negation, and in a symptom description.
    if re.search(r"(?m)^Run\s+`?git\s+diff\b", template):
        raise AssertionError("the prompt template hard-codes a diff command")
    expected = "Run `<diff command chosen in Phase 1b>` to see the diff."
    if template.count(expected) != 1:
        raise AssertionError("the prompt template does not name the Phase 1b choice")


ALL_CHECKS = (check_numbering, check_references, check_template)


def run_all_checks(text: str) -> None:
    for check in ALL_CHECKS:
        check(text)


class LiveDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(SKILL_MD.is_file(), f"missing {SKILL_MD}")
        self.text = _skill_text()

    def test_phase_1b_is_numbered_consecutively_from_one(self):
        check_numbering(self.text)

    def test_every_cross_reference_points_at_its_intended_item(self):
        check_references(self.text)

    def test_the_prompt_template_names_the_phase_1b_choice(self):
        check_template(self.text)

    def test_the_mask_preserves_offsets(self):
        masked, _ = _markdown_regions(self.text)
        self.assertEqual(len(masked), len(self.text))
        self.assertEqual([i for i, c in enumerate(masked) if c == "\n"],
                         [i for i, c in enumerate(self.text) if c == "\n"])

    def test_an_unclosed_fence_runs_to_the_end_of_the_document(self):
        # No mutation-table row reaches this on its own. Each one observes an
        # unclosed fence only through a check that fails, and those checks fail
        # the same way whether or not the incomplete span is recorded, because
        # masking happens during the scan. Without this the record is dead code
        # and the `complete` filter in _terminal_prompt_fence guards nothing.
        text = self.text + "\n```\nunclosed\n"
        masked, fences = _markdown_regions(text)
        self.assertFalse(fences[-1].complete)
        self.assertEqual(fences[-1].end, len(text))
        self.assertEqual(masked[fences[-1].content_start:].strip(), "")
        self.assertTrue(all(f.complete for f in fences[:-1]))


# ------------------------------------------------------------ mutation table
# Each row reproduces a defect a previous version of this file slept through, or
# a legitimate structure a previous version wrongly rejected. They are tests
# rather than a scratch harness so that editing the scanner cannot quietly
# reopen any of them.
#
# Every rejected row also names the check that must do the rejecting and a
# pattern its message must match. Asserting only that run_all_checks raised
# proved that something caught the mutation, never that the intended check did:
# several template mutations were in fact caught first by check_references, so a
# regression in check_template would not have shown up here at all.

TEMPLATE_LINE = "Run `<diff command chosen in Phase 1b>` to see the diff."
HISTORY_PHRASE = "This history carries forward"
ROUND_HISTORY_ITEM = "8. **Round history** (rounds 2+ only)"
NEXT_HEADING = "### 1c. Send to reviewer"
PROMPT_FENCE_HEAD = "````\nIMPORTANT: Save your complete review"
PROMPT_FENCE_TAIL = "````\n\nThen wait for the user"
PARALLELIZE_PHRASE = "you may parallelize across scopes that satisfy Phase 1b item 3"
PRIOR_FINDINGS_ANCHOR = "<For rounds 2+:>\nPrior findings:\n"


def _replace_unique(text: str, current: str, replacement: str) -> str:
    if text.count(current) != 1:
        raise AssertionError(f"mutation anchor is not unique: {current[:60]!r}")
    return text.replace(current, replacement, 1)


def _unique_line_containing(text: str, phrase: str) -> str:
    lines = [line for line in text.splitlines() if phrase in line]
    if len(lines) != 1:
        raise AssertionError(
            f"expected one line containing {phrase!r}, found {len(lines)}")
    return lines[0]


def _sub(current: str, replacement: str):
    return lambda text: _replace_unique(text, current, replacement)


def _duplicate_item_number(text: str) -> str:
    line = _unique_line_containing(text, ROUND_HISTORY_ITEM)
    return _replace_unique(text, line, line.replace("8. ", "7. ", 1))


def _stale_cross_reference(text: str) -> str:
    line = _unique_line_containing(text, HISTORY_PHRASE)
    return _replace_unique(text, line, line.replace("item 8", "item 7"))


def _drop_one_reference_duplicate_another(text: str) -> str:
    line = _unique_line_containing(text, HISTORY_PHRASE)
    return _replace_unique(
        text, line,
        "- Findings flagged Refuted or Inconclusive also carry forward "
        "(Phase 1b, item 8).")


def _fence_the_history_line(text: str) -> str:
    line = _unique_line_containing(text, HISTORY_PHRASE)
    return _replace_unique(text, line, "```\n" + line + "\n```")


def _fence_the_round_history_item(text: str) -> str:
    # Whole line. The constant is only a prefix, and leaving the trailing text
    # attached to the closing backticks turned this into an unclosed-fence case
    # with a different signature than the one it is meant to exercise.
    line = _unique_line_containing(text, ROUND_HISTORY_ITEM)
    return _replace_unique(text, line, "```\n" + line + "\n```")


def _put_info_string_before_round_history(text: str) -> str:
    line = _unique_line_containing(text, ROUND_HISTORY_ITEM)
    return _replace_unique(text, line, "```\n```python\n" + line + "\n```")


def _second_section_end_marker(text: str) -> str:
    return _replace_unique(
        text, "**Plugin path**:",
        TERMINAL_PATH_END_MARKER + " decoy\n\n**Plugin path**:")


def _decoy_prompt_fence_above_the_real_one(text: str) -> str:
    return _replace_unique(
        text, PROMPT_FENCE_HEAD,
        "````\n" + TEMPLATE_LINE + "\n````\n\n" + PROMPT_FENCE_HEAD)


def _leave_real_prompt_unclosed_behind_decoy(text: str) -> str:
    text = _decoy_prompt_fence_above_the_real_one(text)
    # Remove the real closing fence, so the canonical prompt is genuinely
    # incomplete rather than a second complete block.
    return _replace_unique(text, PROMPT_FENCE_TAIL, "Then wait for the user")


def _move_prompt_reference_into_nested_fence(text: str) -> str:
    # The only case that exercises the second _markdown_regions call inside
    # _reference_view. Removing that call left every other row green.
    text = _replace_unique(
        text, PARALLELIZE_PHRASE,
        "you may divide work across scopes under the splitting rule")
    return _replace_unique(
        text, PRIOR_FINDINGS_ANCHOR,
        PRIOR_FINDINGS_ANCHOR
        + "```\n- you may parallelize across scopes (Phase 1b item 3)\n```\n")


def _parity_flip_before_phase_1b(text: str) -> str:
    """A stray marker: the next fence marker below closes it, and every
    fence after that swaps opener and closer roles."""
    return _replace_unique(text, LIST_INTRO, "```\nstray\n" + LIST_INTRO)


def _unclosed_fence_before_phase_1b(text: str) -> str:
    # Tildes, and more of them than any fence in this document, so nothing
    # below can close it. Three backticks here would be closed by the next
    # fence marker and would test parity instead.
    return _replace_unique(text, LIST_INTRO, "~~~~~~~~~~\nunclosed\n" + LIST_INTRO)


def _unclosed_fence_before_next_section(text: str) -> str:
    return _replace_unique(text, NEXT_HEADING,
                           "~~~~~~~~~~\nunclosed\n\n" + NEXT_HEADING)


def _to_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


MUST_BE_REJECTED = [
    ("renumbering leaves a duplicate item 7", _duplicate_item_number),
    ("cross-reference left at the pre-insertion number", _stale_cross_reference),
    ("template hard-codes the diff command again",
     _sub(TEMPLATE_LINE, "Run `git diff --cached` to see the diff.")),
    ("unbolded duplicate item number",
     _sub(NEXT_HEADING, "8. Round history, duplicated without bold\n\n" + NEXT_HEADING)),
    ("one reference deleted, another duplicated", _drop_one_reference_duplicate_another),
    ("template swapped for a different bare git diff",
     _sub(TEMPLATE_LINE, "Run `git diff --cached -- <in-scope paths>` to see the diff.")),
    ("real item removed, decoy hidden inside a fence", _fence_the_round_history_item),
    ("decoy template fence above the real one", _decoy_prompt_fence_above_the_real_one),
    ("info-string line mistaken for a closing fence", _put_info_string_before_round_history),
    ("unclosed fence before the next section", _unclosed_fence_before_next_section),
    ("a real reference moved into an arbitrary fence", _fence_the_history_line),
    ("a second unfenced section end marker", _second_section_end_marker),
    ("real template left unclosed behind a complete decoy",
     _leave_real_prompt_unclosed_behind_decoy),
    ("reference moved into a nested canonical-prompt fence",
     _move_prompt_reference_into_nested_fence),
    ("unclosed fence before the Phase 1b anchor", _unclosed_fence_before_phase_1b),
    ("a stray fence marker flips the parity of every fence below",
     _parity_flip_before_phase_1b),
]

# label -> (the check that must reject it, a pattern its message must match)
#
# The pattern pins one raise site. "found 0" alone matched both
# _unique_unfenced_index and _terminal_prompt_fence, so a parser change that
# swapped one failure for the other left the row green.
EXPECTED_REJECTORS = {
    "renumbering leaves a duplicate item 7":
        (check_numbering, r"numbering is not 1\.\.N"),
    "cross-reference left at the pre-insertion number":
        (check_references, r"reference points at item 7 \("),
    "template hard-codes the diff command again":
        (check_template, r"hard-codes a diff command"),
    "unbolded duplicate item number":
        (check_numbering, r"numbering is not 1\.\.N"),
    "one reference deleted, another duplicated":
        (check_references, r"must occur exactly once"),
    "template swapped for a different bare git diff":
        (check_template, r"hard-codes a diff command"),
    "real item removed, decoy hidden inside a fence":
        (check_references, r"reference points at item 8 \(None\)"),
    "decoy template fence above the real one":
        (check_template, r"complete fenced block in the Terminal path subsection, found 2"),
    "info-string line mistaken for a closing fence":
        (check_references, r"reference points at item 8 \(None\)"),
    "unclosed fence before the next section":
        (check_template, r"expected one unfenced '\*\*Terminal path\*\*:', found 0"),
    "a real reference moved into an arbitrary fence":
        (check_references, r"must occur exactly once"),
    "a second unfenced section end marker":
        (check_template,
         r"expected one unfenced '\*\*Auto-terminal path \(opt-in\)\*\*:', found 2"),
    "real template left unclosed behind a complete decoy":
        (check_template,
         r"expected one unfenced '\*\*Auto-terminal path \(opt-in\)\*\*:', found 0"),
    "reference moved into a nested canonical-prompt fence":
        (check_references, r"must occur exactly once"),
    "unclosed fence before the Phase 1b anchor":
        (check_numbering, r"expected one unfenced 'Prepare a review request with:', found 0"),
    "a stray fence marker flips the parity of every fence below":
        (check_numbering, r"expected one unfenced 'Prepare a review request with:', found 0"),
}

MUST_BE_ACCEPTED = [
    ("a numbered example inside a fence in Phase 1b",
     _sub(NEXT_HEADING,
          "```\n1. A numbered example inside a fence\n2. Another one\n```\n\n" + NEXT_HEADING)),
    ("tilde and backtick fences mixed",
     _sub(NEXT_HEADING, "~~~\n```\nnot a close\n```\n~~~\n\n" + NEXT_HEADING)),
    ("four leading spaces is an indented code block, not a fence",
     _sub(NEXT_HEADING, "    ```\n    9. not a list item\n\n" + NEXT_HEADING)),
    ("a CRLF working copy", _to_crlf),
]


class MutationTableTests(unittest.TestCase):
    """Each case is applied to the live document in memory, never on disk."""

    def setUp(self) -> None:
        self.text = _skill_text()

    def test_the_document_passes_unmutated(self):
        run_all_checks(self.text)

    def test_every_row_names_its_rejecting_check(self):
        self.assertCountEqual([label for label, _ in MUST_BE_REJECTED],
                              list(EXPECTED_REJECTORS))

    def test_every_known_defect_is_rejected_by_the_intended_check(self):
        for label, mutate in MUST_BE_REJECTED:
            with self.subTest(label):
                mutated = mutate(self.text)
                check, message = EXPECTED_REJECTORS[label]
                with self.assertRaisesRegex(AssertionError, message):
                    check(mutated)

    def test_legitimate_markdown_is_accepted(self):
        for label, mutate in MUST_BE_ACCEPTED:
            with self.subTest(label):
                run_all_checks(mutate(self.text))


class StoppedTaskRuleTests(unittest.TestCase):
    """The rule that a stopped background task is not a failed review.

    A dispatched reviewer runs detached and outlives its harness wrapper, so a
    `killed` task notification carries no information about it. Six measured
    notifications on 2026-08-21 all arrived while the reviewer was still
    writing, and one session turned two of them into a sticky downgrade while
    the review it wanted was still being written. These assertions keep the
    replacement judgement, and the script it depends on, from being edited away
    one clause at a time.
    """

    def setUp(self) -> None:
        self.text = _skill_text()
        self.masked, _ = _markdown_regions(self.text)

    def test_the_script_the_rule_names_is_shipped(self):
        self.assertIn("await-review.py", self.text)
        script = SKILL_MD.parent / "scripts" / "await-review.py"
        self.assertTrue(script.is_file(), f"SKILL.md names a missing {script}")

    def test_the_rule_denies_the_notification_any_authority(self):
        self.assertIn("was stopped", self.text)
        self.assertIn("not evidence about the reviewer", self.text)

    def test_all_three_verdicts_are_documented(self):
        for verdict in ("REVIEW-READY", "ALIVE", "DEAD"):
            with self.subTest(verdict):
                self.assertIn(verdict, self.text)

    def test_only_dead_may_set_sticky_downgrade(self):
        index = _unique_unfenced_index(
            self.masked, "A harness notification that the background task")
        sentence = self.text[index:index + 400]
        self.assertIn("not one of these triggers", sentence)
        self.assertIn("`DEAD`", sentence)

    def test_the_required_script_probe_names_the_resolver(self):
        """Auto-terminal may not be selected without the recovery script.

        The probe gates on dispatch, health-check and stall-watch. If the
        resolver is absent from that list, a round can be dispatched into a
        channel that cannot tell a live reviewer from a dead one.
        """
        line = _unique_line_containing(self.text, "Required scripts are")
        self.assertIn("await-review.py", line)

    def test_the_invocation_goes_through_an_interpreter(self):
        """The file is not executable and has no .sh or .ps1 wrapper.

        Telling a reader to run it by filename fails with permission denied on
        POSIX and is not a reliable invocation on Windows.
        """
        index = _unique_unfenced_index(self.masked, "Resolve the round from state-dir evidence")
        para = self.text[index:index + 700]
        self.assertIn("<python>", para)
        self.assertNotIn("Run `await-review.py", para)

    def test_the_procedure_carries_the_backend_review_filename(self):
        """The rule claims to be reviewer-agnostic; the command has to be too.

        A hard-coded Review-Codex.md sends a stopped Copilot or Claude round to
        watch a file its reviewer never writes.
        """
        index = _unique_unfenced_index(self.masked, "Resolve the round from state-dir evidence")
        para = self.text[index:index + 700]
        self.assertIn("Review-<Reviewer>.md", para)
        for name in ("Review-Codex.md", "Review-GitHub-Copilot.md",
                     "Review-Claude-Code.md", "Review-Antigravity.md"):
            with self.subTest(name):
                self.assertIn(name, para)

    def test_the_alive_loop_has_an_owner_and_an_exit(self):
        """Without an absolute deadline the documented wait never terminates.

        The stop takes the wrapper that held the dispatch timeout and usually
        the watcher's timer with it, and each await-review call starts a fresh
        budget, so `run it again` on its own is an unbounded loop. The bound
        lives in the state directory because the agent holding it can be
        compacted, handed over, or stopped in turn.
        """
        index = _unique_unfenced_index(
            self.masked, "The round's deadline outlives the session")
        para = self.text[index:index + 1200]
        self.assertIn("60 minutes", para)
        self.assertIn("--timeout", para)
        self.assertIn("round-deadline", para)
        self.assertIn("never rewritten", para)
        # The dispatcher archives attempt 1 and rewrites the root timestamp, so
        # a first call that lands after a retry would otherwise stamp a later
        # origin and hand the round the time attempt 1 already spent.
        self.assertIn("attempt-N/timestamp", para)

    def test_the_two_checkpoints_are_documented_and_never_sticky(self):
        """Exit 2 is the way out of the loop, and it is not a failure.

        Reading either line as a runtime failure reintroduces the defect this
        change removed, with a downgrade that sticks for the session.
        """
        for line in ("- `TIMEOUT <state-dir> round-deadline=<epoch>` (exit 2)",
                     "- `REAP-UNKNOWN <state-dir> tail-idle=<s>` (exit 2)"):
            with self.subTest(line):
                self.assertIn(line, self.text)
        index = _unique_unfenced_index(self.masked, "Neither exit-2 line may set")
        self.assertIn("sticky downgrade", self.text[index:index + 200])

    def test_the_watcher_reports_a_reap_it_cannot_confirm(self):
        """Waiting for a marker that is never coming is its own failure.

        Both stall watchers swallow a failed completion write, so the gate that
        stops a premature STREAM-DEAD has to have an exit of its own.
        """
        line = _unique_line_containing(self.text, "When the watcher emits `REAP-UNKNOWN")
        self.assertIn("stream-reap-complete", line)
        self.assertIn("checkpoint", line)
        self.assertIn("not set sticky downgrade", line)

    def test_the_watcher_is_handed_the_state_directory(self):
        """Discovery is a 30-second window against a contended process launch.

        The dispatcher already printed the path, so the launch instruction has
        to pass it rather than leave the watcher to find it again.
        """
        line = _unique_line_containing(self.text, "IMPLEMENT_REVIEW_STATE_DIR")
        self.assertIn("STATE-DIR", line)

    def test_the_probe_requires_an_interpreter_for_the_resolver(self):
        """A Python-only resolver needs Python before the channel is chosen."""
        line = _unique_line_containing(self.text, "Required scripts are")
        self.assertIn("python-interpreter", line)
        self.assertIn("without** setting sticky downgrade", line)

    def test_stream_dead_certifies_the_completed_reap(self):
        line = _unique_line_containing(self.text, "When the watcher emits `STREAM-DEAD")
        self.assertIn("stream-reap-complete", line)

    def test_silence_is_documented_as_non_terminal(self):
        index = _unique_unfenced_index(self.masked, "Silence is never terminal here")
        para = self.text[index:index + 900]
        self.assertIn("stream-reap-complete", para)
        self.assertIn("round's own timeout", para)

    def test_silent_advance_accepts_a_resolved_stop(self):
        """Item 2 gates on the dispatch exit code, which a stop never yields.

        Without the second clause the rule above admits the round and this list
        still refuses it, which is the same stall in a different place.
        """
        item = _unique_line_containing(self.text, "The dispatch subprocess exited 0")
        self.assertIn("await-review", item)
        self.assertIn("REVIEW-READY", item)


if __name__ == "__main__":
    unittest.main()
