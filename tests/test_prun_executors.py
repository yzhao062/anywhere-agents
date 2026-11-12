"""prun runs its units on Agy, and on Codex only when the user names it.

Routing units to Sonnet subagents billed the coordinating session's own Claude
account, and a wide fan-out consumed that quota quickly, so the skill dropped
every Claude-side worker. Codex is back as a worker on one condition: the user
names it for a unit or a batch. The coordinator never picks it on its own, and
not as a fallback when Agy is short. These checks keep a later edit from quietly
putting a Claude worker back, or making Codex a default, through the executor
table or a model line in the skill, its pointer, or its wrapper.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "prun" / "SKILL.md"
POINTER = ROOT / ".claude" / "commands" / "prun.md"
OPENAI = ROOT / "skills" / "prun" / "agents" / "openai.yaml"


def executors_table(text: str) -> list[list[str]]:
    """Rows of the table under the Executors heading, header and rule excluded."""
    section = text.split("\n## Executors\n", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("|") or set(line) <= {"|", "-", " "}:
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows[1:]


class PrunExecutorContractTests(unittest.TestCase):
    def test_agy_is_the_default_worker_and_codex_needs_the_user_to_name_it(self) -> None:
        rows = executors_table(SKILL.read_text(encoding="utf-8"))
        names = [row[0] for row in rows]
        self.assertEqual(
            names,
            [
                "Agy (`agy`)",
                "Codex (`codex`)",
                "Grok (`grok`)",
                "Claude session (this session)",
            ],
        )
        self.assertIn("default worker", rows[0][2])
        self.assertIn("Only when the user names Codex", rows[1][2])
        self.assertIn("prun-worker", rows[1][2])
        self.assertIn("Only when the user names Grok", rows[2][2])
        self.assertIn("prun-worker", rows[2][2])
        self.assertIn("dispatch-task-grok.py", rows[2][2])
        # The coordinator's row stays, and it is never a unit.
        self.assertIn("Never a unit", rows[3][2])

    def test_no_surface_spawns_a_claude_subagent(self) -> None:
        for path in (SKILL, POINTER, OPENAI):
            with self.subTest(path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(re.search(r"model:\s*[\"']?sonnet", text, re.I))


if __name__ == "__main__":
    unittest.main()
