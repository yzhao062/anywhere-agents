"""A source repo exposes its skills to Codex through one directory link.

Codex lists repository skills only from ``.agents/skills``. A consumer gets a
link per skill there from the composer. A source repo is never composed, and
its skills already sit in ``skills/``, so one tracked link covers them all,
including a skill added later.
"""
from __future__ import annotations

import os
import stat
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = ROOT / ".agents" / "skills"
TARGET = "../skills"


def _index_mode() -> str | None:
    """The index mode of ``.agents/skills``: ``""`` when untracked, ``None`` when git cannot say."""
    if not os.path.lexists(ROOT / ".git"):
        return None
    try:
        listing = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-s", "--", ".agents/skills"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listing.returncode != 0:
        return None
    return listing.stdout.split(" ", 1)[0]


class CodexSkillLinkTests(unittest.TestCase):
    def test_agents_skills_links_to_the_skills_directory(self) -> None:
        try:
            mode = os.lstat(LINK).st_mode
        except FileNotFoundError:
            self.fail(".agents/skills is missing, so Codex lists no skill of this repo")
        tracked = _index_mode()
        if tracked is not None:
            # An untracked link works on this machine and reaches nobody else.
            self.assertEqual(tracked, "120000", ".agents/skills must be tracked as a symlink")
        if stat.S_ISREG(mode):
            # Git writes a tracked link as a small text file when core.symlinks
            # is false. That checkout is supported; any other regular file is not.
            text = LINK.read_text(encoding="utf-8").strip()
            if text == TARGET and tracked == "120000":
                self.skipTest("this checkout materializes symlinks as files (core.symlinks=false)")
            self.fail(".agents/skills is a regular file, not the tracked symlink")
        self.assertTrue(stat.S_ISLNK(mode), ".agents/skills must be a symlink")
        self.assertEqual(os.readlink(LINK).replace("\\", "/"), TARGET)
        skills = sorted(p.name for p in (ROOT / "skills").iterdir() if (p / "SKILL.md").is_file())
        self.assertTrue(skills, "skills/ holds no skill")
        for name in skills:
            self.assertTrue((LINK / name / "SKILL.md").is_file(), name)


if __name__ == "__main__":
    unittest.main()
