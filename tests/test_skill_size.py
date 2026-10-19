"""No-growth gate for the two coordinator skills.

`skills/implement-review/SKILL.md` and `skills/prun/SKILL.md` load whole on
every `/vet` round and every `prun` run, and both grew by patching: 137,076 and
36,473 bytes when PLAN-skills-diet.md set Milestone B targets of 85,000 and
22,000, then 154,545 and 39,629 at the 2026-10-18 slimming audit
(docs/followups/2026-10-18-harness-slimming-audit.md). Until Milestone B runs,
the ceiling is the measured size, so the files cannot keep creeping.

A change that needs more bytes trims elsewhere in the same file, or raises
the number in CEILINGS with its reason in the same commit. When savings
exceed RATCHET_SLACK, lower the ceiling to the measured size. Smaller savings
can be reused before the next ceiling update.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Bytes, measured 2026-10-19 after dropping the "Validation notes" clause
# from the implement-review save instruction.
CEILINGS: dict[str, int] = {
    "skills/implement-review/SKILL.md": 154_500,
    "skills/prun/SKILL.md": 39_629,
}

RATCHET_SLACK = 4_096


def _size(rel: str) -> int:
    # .gitattributes pins LF, but a CRLF working copy must not read as growth.
    return len((ROOT / rel).read_bytes().replace(b"\r\n", b"\n"))


class TestSkillSize(unittest.TestCase):
    def test_no_growth(self) -> None:
        for rel, ceiling in CEILINGS.items():
            with self.subTest(rel):
                size = _size(rel)
                self.assertLessEqual(
                    size, ceiling,
                    f"{rel} is {size} bytes, over its {ceiling}-byte ceiling; "
                    "trim elsewhere in the file, or raise CEILINGS in "
                    "tests/test_skill_size.py with the reason",
                )

    def test_savings_are_kept(self) -> None:
        for rel, ceiling in CEILINGS.items():
            with self.subTest(rel):
                size = _size(rel)
                self.assertLessEqual(
                    ceiling - size, RATCHET_SLACK,
                    f"{rel} is {size} bytes, more than {RATCHET_SLACK} under "
                    f"its {ceiling}-byte ceiling; lower CEILINGS in "
                    "tests/test_skill_size.py to the new size",
                )


if __name__ == "__main__":
    unittest.main()
