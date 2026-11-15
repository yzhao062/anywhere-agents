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
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
SKILL_MD = SKILLS_DIR / "implement-review" / "SKILL.md"

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

    A blanket mask would be wrong: a reviewer-facing reference may live inside
    the canonical prompt fence (the parallelize sentence did until Milestone A
    of the 2026-09 skills diet replaced its pointer with the criterion).
    Exposing only that one block keeps such a reference checkable while still
    refusing a decoy reference planted in an arbitrary fence elsewhere.
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

    def test_the_prompt_template_skip_list_includes_agents_skills(self):
        template = _terminal_prompt_template(self.text)
        self.assertIn(".agents/skills/implement-review/", template)

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
        text, "### 1d. Auto-watch",
        TERMINAL_PATH_END_MARKER + " decoy\n\n### 1d. Auto-watch")


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
    # _reference_view. Removing that call left every other row green. The
    # Round-history reference is moved out of prose into a fence nested in the
    # canonical prompt body; a masked nested fence hides it, so the registry
    # entry is no longer seen and the check must reject the document.
    line = _unique_line_containing(text, HISTORY_PHRASE)
    text = _replace_unique(
        text, line,
        "- Findings from earlier rounds carry forward without a cross-reference.")
    return _replace_unique(
        text, PRIOR_FINDINGS_ANCHOR,
        PRIOR_FINDINGS_ANCHOR + "```\n" + line + "\n```\n")


def _move_history_reference_into_prompt_body(text: str) -> str:
    # The positive twin of the nested-fence rejection: the canonical prompt
    # body is exposed to the reference check, so a registered reference that
    # moves there unfenced is still seen exactly once. A _reference_view that
    # returned only the prose mask would fail this row.
    line = _unique_line_containing(text, HISTORY_PHRASE)
    text = _replace_unique(
        text, line,
        "- Findings from earlier rounds carry forward without a cross-reference.")
    return _replace_unique(text, PRIOR_FINDINGS_ANCHOR, PRIOR_FINDINGS_ANCHOR + line + "\n")


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
    ("a registered reference moved into the unfenced canonical prompt body",
     _move_history_reference_into_prompt_body),
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
    """The stopped-task rule resolves a round through `await-review.py`.

    The rule's wording is reviewed with the skill. What a test can hold is that
    the script the rule sends the coordinator to is shipped.
    """

    def setUp(self) -> None:
        self.text = _skill_text()

    def test_the_script_the_rule_names_is_shipped(self):
        self.assertIn("await-review.py", self.text)
        script = SKILL_MD.parent / "scripts" / "await-review.py"
        self.assertTrue(script.is_file(), f"SKILL.md names a missing {script}")


# ------------------------------------------------------- reference index
# A skill core that moves prose into references/ routes the reader with a
# "## Reference Index" table: "| File | Read when |", one row per reference. The
# table is what an agent follows to decide which file to open, so the contract
# is structural. Every row links a file that exists, every row says when to read
# it, and no reference file sits on disk without a row. The wording of a read
# condition is reviewed with the skill and is not pinned here.

INDEX_HEADING = "## Reference Index"
INDEX_HEADER_CELLS = ["File", "Read when"]
INDEX_LINK_RE = re.compile(r"\[(references/[\w.-]+\.md)\]\((references/[\w.-]+\.md)\)")
INDEX_SEPARATOR_CELL_RE = re.compile(r":?-{3,}:?")

# Skills whose SKILL.md carries the index: the two coordinator skills that
# the skills diet slimmed into a core plus references/.
REFERENCE_INDEX_SKILLS = ("prun", "implement-review")


def _table_cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


def _reference_index_rows(text: str) -> list[tuple[str, str]]:
    """The (link target, read condition) pairs of the Reference Index table.

    Read through the prose mask, so a table inside a fence is not an index.
    """
    masked, _ = _markdown_regions(text)
    lines = masked.splitlines()
    heads = [i for i, line in enumerate(lines) if line.strip() == INDEX_HEADING]
    if len(heads) != 1:
        raise AssertionError(
            f"expected one unfenced {INDEX_HEADING!r}, found {len(heads)}")
    table: list[str] = []
    for line in lines[heads[0] + 1:]:
        if HEADING_RE.match(line):
            break
        if line.lstrip().startswith("|"):
            table.append(line)
    if len(table) < 3:
        raise AssertionError("the Reference Index needs a header row, a separator "
                             f"row, and at least one reference row: {table}")
    if _table_cells(table[0]) != INDEX_HEADER_CELLS:
        raise AssertionError(f"the Reference Index header row must be "
                             f"{INDEX_HEADER_CELLS}: {_table_cells(table[0])}")
    separator = _table_cells(table[1])
    if len(separator) != 2 or not all(INDEX_SEPARATOR_CELL_RE.fullmatch(c) for c in separator):
        raise AssertionError(f"the Reference Index has no separator row: {table[1]!r}")
    rows: list[tuple[str, str]] = []
    for line in table[2:]:
        cells = _table_cells(line)
        if len(cells) != 2:
            raise AssertionError(f"a Reference Index row needs two cells: {line!r}")
        link = INDEX_LINK_RE.fullmatch(cells[0])
        if link is None or link.group(1) != link.group(2):
            raise AssertionError("a Reference Index File cell must be a link of the "
                                 f"form [references/<name>.md](references/<name>.md): {cells[0]!r}")
        rows.append((link.group(1), cells[1]))
    return rows


def check_reference_index(text: str, skill_dir: Path) -> None:
    rows = _reference_index_rows(text)
    targets = [target for target, _ in rows]
    for target, read_when in rows:
        if not (skill_dir / target).is_file():
            raise AssertionError(f"the Reference Index names a missing file: {target}")
        if not read_when:
            raise AssertionError(f"the Reference Index row for {target} has an empty read condition")
    duplicates = sorted({t for t in targets if targets.count(t) > 1})
    if duplicates:
        raise AssertionError(f"the Reference Index lists a file twice: {duplicates}")
    # glob() is not recursive, which leaves out references/example-reviews/.
    on_disk = sorted(f"references/{p.name}"
                     for p in (skill_dir / "references").glob("*.md") if p.is_file())
    unlisted = [path for path in on_disk if path not in targets]
    if unlisted:
        raise AssertionError(f"a reference file has no Reference Index row: {unlisted}")


class ReferenceIndexTests(unittest.TestCase):
    def test_each_listed_skill_routes_to_every_reference_it_ships(self):
        for name in REFERENCE_INDEX_SKILLS:
            with self.subTest(name):
                skill_md = SKILLS_DIR / name / "SKILL.md"
                self.assertTrue(skill_md.is_file(), f"missing {skill_md}")
                text = skill_md.read_text(encoding="utf-8", errors="replace")
                check_reference_index(text, skill_md.parent)


class ReferenceIndexCheckTests(unittest.TestCase):
    """The check itself, run against small skill directories built in a temp dir."""

    GOOD_INDEX = (
        "## Reference Index\n\n"
        "| File | Read when |\n|---|---|\n"
        "| [references/a.md](references/a.md) | the user names A |\n"
        "| [references/b.md](references/b.md) | a run was interrupted |\n"
    )

    def _check(self, text: str, files=("a.md", "b.md"), extra_dirs=()) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            (skill_dir / "references").mkdir()
            for name in files:
                (skill_dir / "references" / name).write_text("x\n", encoding="utf-8")
            for sub in extra_dirs:
                (skill_dir / "references" / sub).mkdir()
                (skill_dir / "references" / sub / "r.md").write_text("x\n", encoding="utf-8")
            check_reference_index(text, skill_dir)

    def test_a_complete_index_passes(self):
        self._check("# Core\n\n" + self.GOOD_INDEX + "\n## Next\n")

    def test_a_crlf_copy_passes(self):
        self._check(_to_crlf(self.GOOD_INDEX))

    def test_example_reviews_need_no_row(self):
        self._check(self.GOOD_INDEX, extra_dirs=("example-reviews",))

    def test_a_row_naming_a_missing_file_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, r"missing file: references/b\.md"):
            self._check(self.GOOD_INDEX, files=("a.md",))

    def test_an_empty_read_condition_is_rejected(self):
        text = self.GOOD_INDEX.replace("a run was interrupted", "  ")
        with self.assertRaisesRegex(AssertionError, r"empty read condition"):
            self._check(text)

    def test_a_reference_without_a_row_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, r"no Reference Index row: \['references/c\.md'\]"):
            self._check(self.GOOD_INDEX, files=("a.md", "b.md", "c.md"))

    def test_a_file_listed_twice_is_rejected(self):
        text = self.GOOD_INDEX + "| [references/a.md](references/a.md) | again |\n"
        with self.assertRaisesRegex(AssertionError, r"lists a file twice"):
            self._check(text)

    def test_a_wrong_header_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, r"header row must be"):
            self._check(self.GOOD_INDEX.replace("Read when", "Notes"))

    def test_a_file_cell_that_is_not_a_matching_link_is_rejected(self):
        for cell in ("`references/a.md`", "[references/a.md](references/b.md)",
                     "[a](references/a.md)"):
            with self.subTest(cell):
                text = self.GOOD_INDEX.replace("[references/a.md](references/a.md)", cell)
                with self.assertRaisesRegex(AssertionError, r"File cell must be a link"):
                    self._check(text)

    def test_a_missing_index_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, r"expected one unfenced .* found 0"):
            self._check("# Core\n\nNo index here.\n")

    def test_an_index_inside_a_fence_is_not_an_index(self):
        with self.assertRaisesRegex(AssertionError, r"expected one unfenced .* found 0"):
            self._check("```\n" + self.GOOD_INDEX + "```\n")

    def test_a_second_index_heading_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, r"expected one unfenced .* found 2"):
            self._check(self.GOOD_INDEX + "\n" + self.GOOD_INDEX)


# ------------------------------------------------------- round checklist
# implement-review opens with a "## Round checklist": two lettered standing
# rules, then one numbered step per action of a round. A Codex head reads the
# first 115 lines and a whole-file cat is cut by a token cap, so the checklist
# must sit in that head and the standing rules (the Claude slot and the stop
# rule) in its first 9,500 bytes. Each step names the heading that holds its
# procedure as an in-page link; a link that matches no heading, or two, would
# send the reader nowhere. What a step says is reviewed with the skill and is
# not pinned here.

CHECKLIST_HEADING = "## Round checklist"
CHECKLIST_LAST_LINE = 115
STANDING_RULES_LAST_BYTE = 9_500
CHECKLIST_STEP_RE = re.compile(r"^(\d+)\.\s+\S")
STANDING_RULE_RE = re.compile(r"^[A-Z]\.\s+\S")
INPAGE_LINK_RE = re.compile(r"\[[^\]\n]*\]\(#([^)\s]*)\)")
HEADING_TEXT_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")

# Skills whose SKILL.md carries the checklist.
ROUND_CHECKLIST_SKILLS = ("implement-review",)


def github_slug(heading: str) -> str:
    """The anchor GitHub gives a heading: lowercase, keep letters, digits,
    spaces, hyphens, and underscores, then turn spaces into hyphens."""
    return re.sub(r"[^\w \-]", "", heading.strip().lower()).replace(" ", "-")


def _heading_slugs(masked: str) -> list[str]:
    slugs: list[str] = []
    for line in masked.splitlines():
        match = HEADING_TEXT_RE.match(line)
        if match:
            slugs.append(github_slug(match.group(1)))
    return slugs


def _reject_broken_links(where: str, targets: list[str], slugs: list[str]) -> None:
    broken = [t for t in targets if slugs.count(t) != 1]
    if broken:
        raise AssertionError(f"{where} has an in-page link that matches no heading, "
                             f"or more than one: {broken}")


def check_round_checklist(text: str) -> None:
    masked, _ = _markdown_regions(text)
    lines = masked.splitlines(keepends=True)
    heads = [i for i, line in enumerate(lines)
             if _line_body(line).strip() == CHECKLIST_HEADING]
    if len(heads) != 1:
        raise AssertionError(
            f"expected one unfenced {CHECKLIST_HEADING!r}, found {len(heads)}")
    start = heads[0]
    end = next((i for i in range(start + 1, len(lines)) if HEADING_RE.match(lines[i])),
               len(lines))
    last = end
    while last > start + 1 and not lines[last - 1].strip():
        last -= 1
    if last > CHECKLIST_LAST_LINE:
        raise AssertionError(f"the round checklist ends at line {last}, past line "
                             f"{CHECKLIST_LAST_LINE}")
    slugs = _heading_slugs(masked)
    step_numbers: list[int] = []
    rule_end = None
    offset = sum(len(line) for line in lines[:start + 1])
    for i in range(start + 1, end):
        body = _line_body(lines[i])
        step = CHECKLIST_STEP_RE.match(body)
        if step:
            number = int(step.group(1))
            step_numbers.append(number)
            targets = INPAGE_LINK_RE.findall(body)
            if not any(slugs.count(t) == 1 for t in targets):
                raise AssertionError(
                    f"checklist step {number} links no heading of this file by a "
                    f"slug that matches exactly one heading: {targets}")
            _reject_broken_links(f"checklist step {number}", targets, slugs)
        elif STANDING_RULE_RE.match(body):
            rule_end = len(text[:offset + len(body)].encode("utf-8"))
            _reject_broken_links("a standing rule", INPAGE_LINK_RE.findall(body), slugs)
        offset += len(lines[i])
    if not step_numbers:
        raise AssertionError("the round checklist has no numbered step")
    if step_numbers != list(range(1, len(step_numbers) + 1)):
        raise AssertionError(f"checklist steps are not numbered 1..N: {step_numbers}")
    if rule_end is None:
        raise AssertionError("the round checklist has no lettered standing rule")
    if rule_end > STANDING_RULES_LAST_BYTE:
        raise AssertionError(f"the standing rules end at byte {rule_end}, past byte "
                             f"{STANDING_RULES_LAST_BYTE}")


class RoundChecklistTests(unittest.TestCase):
    def test_each_listed_skill_has_a_checklist_that_links_its_procedures(self):
        for name in ROUND_CHECKLIST_SKILLS:
            with self.subTest(name):
                skill_md = SKILLS_DIR / name / "SKILL.md"
                self.assertTrue(skill_md.is_file(), f"missing {skill_md}")
                check_round_checklist(skill_md.read_text(encoding="utf-8", errors="replace"))


class RoundChecklistCheckTests(unittest.TestCase):
    """The check itself, run against small documents."""

    GOOD = (
        "# Core\n\n"
        "## Round checklist\n\n"
        "A. **Rule.** Keep the slot. See [Phase 2.0: Health check (Auto-terminal only)]"
        "(#phase-20-health-check-auto-terminal-only).\n\n"
        "1. **First.** Follow [Prerequisites](#prerequisites).\n\n"
        "2. **Second.** Send by [1c. Send to reviewer](#1c-send-to-reviewer).\n\n"
        "## Prerequisites\n\nBody.\n\n"
        "### 1c. Send to reviewer\n\nBody.\n\n"
        "### Phase 2.0: Health check (Auto-terminal only)\n\nBody.\n"
    )

    def test_a_complete_checklist_passes(self):
        check_round_checklist(self.GOOD)

    def test_a_crlf_copy_passes(self):
        check_round_checklist(_to_crlf(self.GOOD))

    def test_the_slug_drops_punctuation_and_keeps_hyphens_and_underscores(self):
        self.assertEqual(github_slug("Phase 2.0: Health check (Auto-terminal only)"),
                         "phase-20-health-check-auto-terminal-only")
        self.assertEqual(github_slug("1d. Auto-watch (Terminal-relay and Auto-terminal channels)"),
                         "1d-auto-watch-terminal-relay-and-auto-terminal-channels")
        self.assertEqual(github_slug("Round_history"), "round_history")

    def test_a_missing_checklist_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, r"expected one unfenced .* found 0"):
            check_round_checklist("# Core\n\nNo checklist here.\n")

    def test_a_checklist_inside_a_fence_is_not_a_checklist(self):
        with self.assertRaisesRegex(AssertionError, r"expected one unfenced .* found 0"):
            check_round_checklist("```\n" + self.GOOD + "```\n")

    def test_a_second_checklist_heading_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, r"expected one unfenced .* found 2"):
            check_round_checklist(self.GOOD + "\n## Round checklist\n\nAgain.\n")

    def test_a_step_without_an_in_page_link_is_rejected(self):
        text = self.GOOD.replace("Follow [Prerequisites](#prerequisites).", "Follow Prerequisites.")
        with self.assertRaisesRegex(AssertionError, r"step 1 links no heading"):
            check_round_checklist(text)

    def test_a_link_to_a_file_is_not_an_in_page_link(self):
        text = self.GOOD.replace("[Prerequisites](#prerequisites)",
                                 "[Prerequisites](references/a.md)")
        with self.assertRaisesRegex(AssertionError, r"step 1 links no heading"):
            check_round_checklist(text)

    def test_a_link_to_a_missing_heading_is_rejected(self):
        text = self.GOOD.replace("(#prerequisites)", "(#prerequisite)")
        with self.assertRaisesRegex(AssertionError, r"step 1 links no heading"):
            check_round_checklist(text)

    def test_a_link_that_keeps_the_punctuation_is_rejected(self):
        text = self.GOOD.replace("(#1c-send-to-reviewer)", "(#1c.-send-to-reviewer)")
        with self.assertRaisesRegex(AssertionError, r"step 2 links no heading"):
            check_round_checklist(text)

    def test_a_link_that_matches_two_headings_is_rejected(self):
        text = self.GOOD + "\n## Prerequisites\n\nAgain.\n"
        with self.assertRaisesRegex(AssertionError, r"step 1 links no heading"):
            check_round_checklist(text)

    def test_a_broken_link_beside_a_good_one_is_rejected(self):
        text = self.GOOD.replace("Follow [Prerequisites](#prerequisites).",
                                 "See [gone](#gone) and [Prerequisites](#prerequisites).")
        with self.assertRaisesRegex(AssertionError, r"step 1 has an in-page link"):
            check_round_checklist(text)

    def test_a_broken_link_in_a_standing_rule_is_rejected(self):
        text = self.GOOD.replace("A. **Rule.** Keep the slot.",
                                 "A. **Rule.** Keep the slot ([gone](#gone)).")
        with self.assertRaisesRegex(AssertionError, r"a standing rule has an in-page link"):
            check_round_checklist(text)

    def test_a_step_numbering_gap_is_rejected(self):
        text = self.GOOD.replace("2. **Second.**", "3. **Second.**")
        with self.assertRaisesRegex(AssertionError, r"not numbered 1\.\.N"):
            check_round_checklist(text)

    def test_a_checklist_without_steps_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, r"no numbered step"):
            check_round_checklist("## Round checklist\n\nA. **Rule.** Text.\n")

    def test_a_checklist_without_a_standing_rule_is_rejected(self):
        text = self.GOOD.replace("A. **Rule.**", "**Rule.**")
        with self.assertRaisesRegex(AssertionError, r"no lettered standing rule"):
            check_round_checklist(text)

    def test_a_checklist_that_ends_past_the_head_is_rejected(self):
        filler = "".join(f"Filler line {n}.\n\n" for n in range(60))
        text = self.GOOD.replace("## Prerequisites", filler + "## Prerequisites", 1)
        # The filler sits after the steps but before the next heading, so it
        # belongs to the checklist section.
        with self.assertRaisesRegex(AssertionError, r"ends at line \d+, past line 115"):
            check_round_checklist(text)

    def test_standing_rules_past_the_byte_budget_are_rejected(self):
        text = self.GOOD.replace("Keep the slot.", "Keep the slot. " + "x" * STANDING_RULES_LAST_BYTE)
        with self.assertRaisesRegex(AssertionError, r"standing rules end at byte \d+, past byte 9500"):
            check_round_checklist(text)

    def test_the_budget_counts_bytes_not_characters(self):
        # 6,000 two-byte characters are 6,000 characters and 12,000 bytes.
        text = self.GOOD.replace("Keep the slot.", "Keep the slot. " + "é" * 6000)
        with self.assertRaisesRegex(AssertionError, r"standing rules end at byte \d+"):
            check_round_checklist(text)

    def test_a_standing_rule_inside_the_budget_passes(self):
        text = self.GOOD.replace("Keep the slot.", "Keep the slot. " + "x" * 5000)
        check_round_checklist(text)


if __name__ == "__main__":
    unittest.main()
