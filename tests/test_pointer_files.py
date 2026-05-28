"""Regression test: every committed .claude/commands/*.md pointer carries
.claude/skills/<name>/ in its lookup-order line. Guards against a future
pack shipping stale 2-path text (issue anywhere-agents#6)."""

import pathlib
import re
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_COMMANDS_DIR = _REPO_ROOT / ".claude" / "commands"


class PointerLookupOrderTests(unittest.TestCase):

    def test_every_pointer_names_three_paths_in_order(self):
        pointer_files = sorted(_COMMANDS_DIR.glob("*.md"))
        self.assertGreater(len(pointer_files), 0, "no .claude/commands/*.md files found")
        for pf in pointer_files:
            text = pf.read_text(encoding="utf-8")
            name = pf.stem  # e.g., "implement-review"
            # Find the lookup line (starts with "Read and follow the skill definition.")
            lookup_match = re.search(
                r"Read and follow the skill definition\.[^\n]*?\n",
                text,
            )
            self.assertIsNotNone(lookup_match,
                                 f"{pf.name}: lookup line not found")
            lookup_line = lookup_match.group(0)
            self.assertIn(f"skills/{name}/SKILL.md", lookup_line,
                          f"{pf.name}: missing project-local path")
            self.assertIn(f".claude/skills/{name}/SKILL.md", lookup_line,
                          f"{pf.name}: missing .claude/skills/ path")
            self.assertIn(f".agent-config/repo/skills/{name}/SKILL.md", lookup_line,
                          f"{pf.name}: missing .agent-config/repo/skills/ path")
            # Order: skills/ → .claude/skills/ → .agent-config/repo/skills/
            local_idx = lookup_line.index(f"skills/{name}/SKILL.md")
            claude_idx = lookup_line.index(f".claude/skills/{name}/SKILL.md")
            bootstrap_idx = lookup_line.index(f".agent-config/repo/skills/{name}/SKILL.md")
            self.assertLess(local_idx, claude_idx,
                            f"{pf.name}: skills/ must precede .claude/skills/")
            self.assertLess(claude_idx, bootstrap_idx,
                            f"{pf.name}: .claude/skills/ must precede .agent-config/repo/skills/")


if __name__ == "__main__":
    unittest.main()
