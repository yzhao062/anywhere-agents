"""Size gate for a composed consumer: the shared baseline plus the passive
packs the maintainer's consumers load, through the real composer.

tests/test_bootstrap_size.py measures the baseline alone and is shared
byte-for-byte with agent-config. This test is anywhere-agents-only because
the composer is: it composes the committed AGENTS.md with fixed copies of the
three compact pack bodies under tests/fixtures/composed-consumer/ (agent-style
v0.5.0 docs/rule-pack-compact.md; agent-pack profile and paper-workflow
compact bodies), runs the generator, and holds the composed AGENTS.md and the
generated CLAUDE.md under 61,440 bytes. That is the consumer target
PLAN-agents-md-diet.md set (criterion 4, "consumer CLAUDE.md under 60 KB with
the current pack set"), turned from a post-release measurement into a test.

The bodies are fixed copies so the test needs no network and a pack release
cannot change the number silently; refresh them, with the sizes and hashes in
the fixture README, when a pack body changes on purpose.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import passive  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "composed-consumer"

# The consumer ceiling in bytes (60 KiB). A composed file over it means a
# pack body or the baseline grew; the fix is in the file that grew, and a
# larger budget is a recorded decision with a comment here.
CONSUMER_CEILING_BYTES = 61_440

# Name, fixture directory, and passive mapping, in the order the maintainer's
# consumers compose them (the bundled agent-style first, then the two
# agent-pack packs from agent-config.yaml).
PACKS = (
    ("agent-style", "v0.5.0", "docs/rule-pack-compact.md"),
    ("profile", "main", "docs/rule-pack-compact.md"),
    ("paper-workflow", "main", "docs/paper-workflow-compact.md"),
)


class _PassiveContext:
    """The three attributes handle_passive_entry reads from a DispatchContext.

    A full context needs a transaction and three state files; the passive
    handler only reads the archive directory and the policy and records a
    lock row, so a stand-in keeps the composition itself the real thing.
    """

    def __init__(self, source_dir: Path) -> None:
        self.pack_source_dir = source_dir
        self.pack_update_policy = "prompt"
        self.lock_rows: list[dict] = []

    def record_lock_file(self, row: dict) -> None:
        self.lock_rows.append(row)


def compose_fixture_consumer(root: Path) -> str:
    """Write the composed AGENTS.md into ``root`` and return its text."""
    composed = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for name, ref, from_path in PACKS:
        manifest = {"name": name, "source": {"repo": f"fixture:{name}", "ref": ref}}
        entry = {"files": [{"from": from_path, "to": "AGENTS.md"}]}
        ctx = _PassiveContext(FIXTURES / name)
        composed = passive.handle_passive_entry(
            entry, manifest, ctx,
            upstream_agents_md=composed, cache_dir=root, no_cache=True,
        )
        assert ctx.lock_rows and ctx.lock_rows[0]["source_path"] == from_path
    with open(root / "AGENTS.md", "w", encoding="utf-8", newline="\n") as f:
        f.write(composed)
    return composed


class TestComposedConsumerSize(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        compose_fixture_consumer(self.root)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "generate_agent_configs.py"),
                "--root", str(self.root),
                "--quiet",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode, 0,
            f"generator failed (rc={result.returncode}):\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}",
        )

    def test_fixture_bodies_are_present_and_composed_in_order(self) -> None:
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        last = -1
        for name, ref, from_path in PACKS:
            body = (FIXTURES / name / from_path).read_text(encoding="utf-8").rstrip()
            begin = f"<!-- rule-pack:{name}:begin version={ref} "
            self.assertIn(begin, text, f"{name} block missing from the composition")
            self.assertIn(body, text, f"{name} body missing from the composition")
            position = text.index(begin)
            self.assertGreater(position, last, f"{name} composed out of order")
            last = position

    def test_composed_files_stay_under_the_consumer_ceiling(self) -> None:
        violations: list[str] = []
        for rel in ("AGENTS.md", "CLAUDE.md"):
            size = (self.root / rel).stat().st_size
            print(
                f"composed-consumer-size: {rel} = {size} B "
                f"(ceiling {CONSUMER_CEILING_BYTES} B)",
                file=sys.stderr,
            )
            if size > CONSUMER_CEILING_BYTES:
                violations.append(f"{rel}: {size} B exceeds {CONSUMER_CEILING_BYTES} B")
        if violations:
            self.fail(
                "composed consumer size gate failed:\n  " + "\n  ".join(violations)
                + "\n\nThe composition is the shared AGENTS.md plus the three "
                "fixture bodies under tests/fixtures/composed-consumer/. Trim the "
                "file that grew; a larger budget is a recorded decision with a "
                "comment on CONSUMER_CEILING_BYTES."
            )


if __name__ == "__main__":
    unittest.main()
