"""prun runs its units on Agy, and on Codex only when the user names it.

Routing units to Sonnet subagents billed the coordinating session's own Claude
account, and a wide fan-out consumed that quota quickly, so the skill dropped
every Claude-side worker. Codex is back as a worker on one condition: the user
names it for a unit or a batch. The coordinator never picks it on its own, and
not as a fallback when Agy is short. These checks keep a later edit from quietly
putting a Claude worker back, or making Codex a default, through the table, the
dispatch step, or a surface that advertises the skill.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "prun" / "SKILL.md"
POINTER = ROOT / ".claude" / "commands" / "prun.md"
OPENAI = ROOT / "skills" / "prun" / "agents" / "openai.yaml"
ROUTING_TABLE = ROOT / "skills" / "my-router" / "references" / "routing-table.md"


RELEASE_NOTE = re.compile(r"`v\d+\.\d+")


def current_prun_lines(path: Path) -> list[str]:
    """Lines naming prun that describe it now rather than narrate a past release."""
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if "prun" in line and not RELEASE_NOTE.search(line)
    ]


def prun_card(index: str) -> str:
    """The prun card of the skills index, from its title to the next card."""
    rest = index[index.index("__`prun`__"):]
    end = rest.find("\n-   ")
    return rest if end == -1 else rest[:end]


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

    def test_every_surface_states_the_codex_policy(self) -> None:
        # Same policy in the skill, the command pointer, and the OpenAI wrapper:
        # Agy by default, Codex only when the user names it, never the
        # coordinator's own pick or a fallback, run by dispatch-task, and its
        # model and effort from the registry row.
        for path in (SKILL, POINTER, OPENAI):
            with self.subTest(path.name):
                text = " ".join(path.read_text(encoding="utf-8").split())
                self.assertIn("by default", text)
                self.assertRegex(text, r"only when the (?:user|user's request) names Codex")
                self.assertRegex(text, r"never picks Codex on its own")
                self.assertRegex(text, r"fallback when Agy is short|as a fallback")
                self.assertIn("dispatch-task.sh", text)
                self.assertIn("prun-worker", text)
                self.assertNotRegex(text, r"Codex (?:is|are) (?:not|excluded)|not a prun executor")
                self.assertNotIn("reserved for `/vet`", text)

    def test_every_surface_states_the_grok_policy(self) -> None:
        for path in (SKILL, POINTER, OPENAI):
            with self.subTest(path.name):
                text = " ".join(path.read_text(encoding="utf-8").split())
                self.assertRegex(text, r"only when the (?:user|user's request) names Grok")
                self.assertRegex(text, r"never picks Grok on its own")
                self.assertIn("dispatch-task-grok.py", text)
                self.assertIn("prun-worker", text)
                self.assertIn("preflight timed out after 60s", text)
                self.assertIn("Antigravity preflight failed", text)
                self.assertIn("grok-executor.md", text)

    def test_the_router_row_follows_the_executor_policy(self) -> None:
        # The router's one-line summary is read before the skill is, so a row
        # that still reserves Codex for review sends a user who names it away.
        rows = [
            line
            for line in ROUTING_TABLE.read_text(encoding="utf-8").splitlines()
            if line.startswith("| `prun` |")
        ]
        self.assertEqual(len(rows), 1)
        self.assertIn("Agy workers by default", rows[0])
        self.assertIn("when the user names Codex", rows[0])
        self.assertIn("Grok", rows[0])
        self.assertNotIn("reserved for", rows[0])

    def test_no_surface_spawns_a_claude_subagent(self) -> None:
        for path in (SKILL, POINTER, OPENAI):
            with self.subTest(path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(re.search(r"model:\s*[\"']?sonnet", text, re.I))

    def test_the_advertised_descriptions_name_agy_workers_only(self) -> None:
        # The pointer description is what the skill list shows every session,
        # and the OpenAI wrapper's short description is Codex's equivalent.
        pointer = POINTER.read_text(encoding="utf-8")
        description = re.search(r'^description:\s*"(.+)"$', pointer, re.M)
        self.assertIsNotNone(description)
        self.assertNotIn("Sonnet", description.group(1))
        self.assertIn("Agy", description.group(1))
        short = re.search(r'short_description:\s*"(.+)"', OPENAI.read_text(encoding="utf-8"))
        self.assertIsNotNone(short)
        self.assertNotIn("Sonnet", short.group(1))

    def test_the_prohibition_is_stated_where_units_are_assigned(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("**Never a Claude-side worker.**", text)
        self.assertIn("3. **Assign**: every unit goes to Agy, except a unit the user named for Codex or Grok.", text)

    def test_the_public_docs_do_not_route_units_to_sonnet(self) -> None:
        # anywhere-agents publishes a docs site whose prun page puts its own
        # summary above the included skill, so a stale summary contradicts the
        # skill on the same page. agent-config has no such site.
        page = ROOT / "docs" / "skills" / "prun.md"
        index = ROOT / "docs" / "skills" / "index.md"
        landing = ROOT / "docs" / "index.md"
        if not any(path.is_file() for path in (page, index, landing)):
            self.skipTest("this repository publishes no docs site")
        if page.is_file():
            self.assertNotIn("Sonnet", page.read_text(encoding="utf-8"))
        if index.is_file():
            # Every skill has a card on the index, so only the prun card is held to this.
            self.assertNotIn("Sonnet", prun_card(index.read_text(encoding="utf-8")))
        if landing.is_file():
            for line in current_prun_lines(landing):
                self.assertNotIn("Sonnet", line)

    def test_the_readmes_do_not_route_units_to_sonnet(self) -> None:
        # A README and its translation describe prun in a paragraph, a table row
        # or a tree comment. Their release-history paragraphs still name Sonnet
        # for the versions that used it, which remains true of those versions.
        readmes = [path for path in (ROOT / "README.md", ROOT / "README.zh-CN.md") if path.is_file()]
        self.assertTrue(readmes)
        for readme in readmes:
            lines = current_prun_lines(readme)
            self.assertTrue(lines, readme.name)
            for line in lines:
                with self.subTest(readme.name, line=line[:60]):
                    self.assertNotIn("Sonnet", line)


if __name__ == "__main__":
    unittest.main()
