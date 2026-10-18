"""Tests for ``scripts/packs/residue.py`` and its two callers.

aa's reconciliation writes a minimal ``{name, source: {url, ref}}`` row for a
default pack into the user-level config and the project ``agent-config.yaml``.
After a bundled pin bump that row keeps the old ref and, because the most
specific layer wins, holds every consumer on it. These tests cover the shared
rewrite (``packs.residue``), the composer that bootstrap runs
(``compose_packs._advance_default_ref_residue``), and the ``anywhere-agents``
CLI (``cli._rewrite_auto_reconciled_default_refs``).

Everything runs against synthetic configs in temp directories. ``HOME``,
``XDG_CONFIG_HOME`` and ``APPDATA`` are pointed at the temp directory wherever
a composer or CLI entry point resolves a user-level path, so no test can read
or write the real user config.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "pypi"))
sys.path.insert(0, str(ROOT / "scripts"))
# ``from scripts.packs import X`` is the spelling the shared module uses for
# its siblings; it resolves from the repo root.
sys.path.insert(0, str(ROOT))
# tests/ is on sys.path under ``unittest discover -s tests`` but not under
# ``python -m unittest tests.<module>``; put it there before the sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import compose_packs  # noqa: E402
from bundled_manifest_ref import agent_style_ref  # noqa: E402
from packs import config as config_mod  # noqa: E402
from packs import locks  # noqa: E402
from packs import residue  # noqa: E402
from packs import source_fetch  # noqa: E402
from packs import state as state_mod  # noqa: E402
import scripts.packs.locks as scripts_locks  # noqa: E402

BUNDLED_AGENT_STYLE_REF = agent_style_ref()

URL = "https://github.com/yzhao062/agent-style"
# A ref that is in the ledger and is not the one the synthetic manifest below
# bundles. The synthetic bundled ref is fixed (not read from the real manifest)
# so the unit tests keep their meaning across later pin bumps.
STALE_REF = "v0.5.0"
BUNDLED_REF = "v0.6.0"
BUNDLED_PACKS = [
    {
        "name": "agent-style",
        "source": {"repo": URL, "ref": BUNDLED_REF},
        "update_policy": "auto",
    },
    {"name": "aa-core-skills", "update_policy": "prompt"},
]
DEFAULT_NAMES = frozenset({"agent-style", "aa-core-skills"})


def _residue_config(key: str = "packs", ref: str = STALE_REF) -> str:
    return (
        f"{key}:\n"
        "  - name: agent-style\n"
        "    source:\n"
        f"      url: {URL}\n"
        f"      ref: {ref}\n"
    )


class _TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.user = self.base / "xdg" / "anywhere-agents" / "config.yaml"
        self.project_cfg = self.project / "agent-config.yaml"

    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def advance(self, *, user: bool = True, project: bool = True):
        messages: list[str] = []
        results = residue.advance_residue(
            user_config_path=self.user if user else None,
            project_root=self.project if project else None,
            bundled_packs=BUNDLED_PACKS,
            default_names=DEFAULT_NAMES,
            emit=messages.append,
        )
        return results, messages


# =====================================================================
# The shared rewrite.
# =====================================================================


class TestAdvanceResidue(_TempCase):
    def test_user_level_residue_is_advanced(self) -> None:
        self.write(self.user, _residue_config())
        results, messages = self.advance()
        self.assertEqual(
            [(r.path, r.advanced) for r in results],
            [(self.user, [("agent-style", STALE_REF, BUNDLED_REF)])],
        )
        self.assertEqual(
            self.user.read_text(encoding="utf-8"),
            _residue_config(ref=BUNDLED_REF),
        )
        self.assertEqual(len(messages), 1)
        self.assertIn(str(self.user), messages[0])
        self.assertIn(f"agent-style {STALE_REF} -> {BUNDLED_REF}", messages[0])

    def test_project_residue_is_advanced(self) -> None:
        self.write(self.project_cfg, _residue_config())
        results, messages = self.advance()
        self.assertEqual([r.path for r in results], [self.project_cfg])
        self.assertEqual(
            self.project_cfg.read_text(encoding="utf-8"),
            _residue_config(ref=BUNDLED_REF),
        )
        self.assertEqual(len(messages), 1)

    def test_both_files_advance_with_one_line_each(self) -> None:
        self.write(self.user, _residue_config())
        self.write(self.project_cfg, _residue_config())
        results, messages = self.advance()
        self.assertEqual({r.path for r in results}, {self.user, self.project_cfg})
        self.assertEqual(len(messages), 2)

    def test_layers_can_be_skipped(self) -> None:
        self.write(self.user, _residue_config())
        self.write(self.project_cfg, _residue_config())
        self.advance(user=False)
        self.assertIn(STALE_REF, self.user.read_text(encoding="utf-8"))
        self.assertIn(BUNDLED_REF, self.project_cfg.read_text(encoding="utf-8"))

    def test_rest_of_the_file_is_preserved_comments_included(self) -> None:
        text = (
            "# written by hand, keep me\n"
            "extra_key: {nested: [1, 2, 3]}\n"
            "packs:\n"
            "  - name: other-pack   # trailing comment\n"
            "    source: {url: https://example.com/o, ref: main}\n"
            "  - name: agent-style  # the residue row\n"
            "    source:\n"
            f"      url: {URL}\n"
            f"      ref: {STALE_REF}  # stale\n"
            "  - aa-core-skills\n"
        )
        self.write(self.user, text)
        self.advance()
        self.assertEqual(
            self.user.read_text(encoding="utf-8"),
            text.replace(f"ref: {STALE_REF}", f"ref: {BUNDLED_REF}"),
        )

    def test_quoted_ref_keeps_its_quote_style(self) -> None:
        for quote in ("'", '"'):
            with self.subTest(quote=quote):
                text = (
                    "packs:\n"
                    "  - name: agent-style\n"
                    f"    source: {{url: {URL}, ref: {quote}{STALE_REF}{quote}}}\n"
                )
                self.write(self.user, text)
                self.advance(project=False)
                self.assertEqual(
                    self.user.read_text(encoding="utf-8"),
                    text.replace(
                        f"{quote}{STALE_REF}{quote}",
                        f"{quote}{BUNDLED_REF}{quote}",
                    ),
                )

    def test_crlf_line_endings_survive(self) -> None:
        text = _residue_config().replace("\n", "\r\n")
        self.write(self.user, text)
        self.advance(project=False)
        self.assertEqual(
            self.user.read_bytes(),
            text.replace(STALE_REF, BUNDLED_REF).encode("utf-8"),
        )

    def test_every_listed_ref_advances(self) -> None:
        for ref in sorted(residue.AUTO_RECONCILED_DEFAULT_REF_REWRITES["agent-style"]):
            if ref == BUNDLED_REF:
                continue
            with self.subTest(ref=ref):
                self.write(self.user, _residue_config(ref=ref))
                self.advance(project=False)
                self.assertEqual(
                    self.user.read_text(encoding="utf-8"),
                    _residue_config(ref=BUNDLED_REF),
                )

    def test_legacy_rule_packs_key_is_advanced_and_kept(self) -> None:
        for path, kwargs in (
            (self.user, {"project": False}),
            (self.project_cfg, {"user": False}),
        ):
            with self.subTest(path=path.name):
                self.write(path, _residue_config(key="rule_packs"))
                self.advance(**kwargs)
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertEqual(list(data), ["rule_packs"])
                self.assertEqual(data["rule_packs"][0]["source"]["ref"], BUNDLED_REF)

    def test_packs_key_wins_over_legacy_when_both_present(self) -> None:
        text = _residue_config(key="rule_packs") + _residue_config(key="packs")
        self.write(self.user, text)
        self.advance(project=False)
        data = yaml.safe_load(self.user.read_text(encoding="utf-8"))
        self.assertEqual(data["packs"][0]["source"]["ref"], BUNDLED_REF)
        # The resolver ignores ``rule_packs`` when ``packs`` is present, so
        # that list is not touched.
        self.assertEqual(data["rule_packs"][0]["source"]["ref"], STALE_REF)

    def test_legacy_top_level_ref_moves_into_source(self) -> None:
        text = (
            "rule_packs:\n"
            "  - name: agent-style\n"
            f"    ref: {STALE_REF}\n"
            "    source:\n"
            f"      url: {URL}\n"
            "  - name: other\n"
            "    ref: main\n"
        )
        self.write(self.project_cfg, text)
        self.advance(user=False)
        data = yaml.safe_load(self.project_cfg.read_text(encoding="utf-8"))
        self.assertEqual(
            data,
            {
                "rule_packs": [
                    {"name": "agent-style", "source": {"url": URL, "ref": BUNDLED_REF}},
                    {"name": "other", "ref": "main"},
                ]
            },
        )

    def test_repo_alias_and_url_spellings_match_the_bundled_url(self) -> None:
        spellings = (
            "repo: " + URL,
            "url: " + URL + ".git",
            "url: git@github.com:yzhao062/agent-style.git",
            "url: https://github.com/YZhao062/Agent-Style",
        )
        for spelling in spellings:
            with self.subTest(spelling=spelling):
                text = (
                    "packs:\n"
                    "  - name: agent-style\n"
                    f"    source: {{{spelling}, ref: {STALE_REF}}}\n"
                )
                self.write(self.user, text)
                self.advance(project=False)
                self.assertEqual(
                    self.user.read_text(encoding="utf-8"),
                    text.replace(STALE_REF, BUNDLED_REF),
                )

    def test_second_run_writes_nothing(self) -> None:
        self.write(self.user, _residue_config())
        self.write(self.project_cfg, _residue_config())
        self.advance()
        user_after = self.user.read_bytes()
        project_after = self.project_cfg.read_bytes()
        results, messages = self.advance()
        self.assertEqual(results, [])
        self.assertEqual(messages, [])
        self.assertEqual(self.user.read_bytes(), user_after)
        self.assertEqual(self.project_cfg.read_bytes(), project_after)

    def test_no_temp_files_are_left_behind(self) -> None:
        self.write(self.user, _residue_config())
        self.write(self.project_cfg, _residue_config())
        self.advance()
        leftovers = [
            p.name
            for d in (self.user.parent, self.project)
            for p in d.iterdir()
            if p.name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_file_mode_is_preserved(self) -> None:
        self.write(self.project_cfg, _residue_config())
        os.chmod(self.project_cfg, 0o640)
        self.advance(user=False)
        self.assertEqual(self.project_cfg.stat().st_mode & 0o777, 0o640)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_symlinked_config_is_rewritten_through_the_link(self) -> None:
        real = self.base / "dotfiles" / "agent-config.yaml"
        self.write(real, _residue_config())
        self.project_cfg.symlink_to(real)
        self.advance(user=False)
        self.assertTrue(self.project_cfg.is_symlink())
        self.assertEqual(
            real.read_text(encoding="utf-8"), _residue_config(ref=BUNDLED_REF)
        )


class TestDeliberatePinsAreUntouched(_TempCase):
    """A row that is not exactly what reconciliation writes is the user's."""

    def entry(self, **overrides) -> str:
        source = {"url": URL, "ref": STALE_REF}
        source.update(overrides.pop("source", {}))
        body = {"name": "agent-style", "source": source}
        body.update(overrides)
        return yaml.safe_dump({"packs": [body]}, sort_keys=False)

    def assert_untouched(self, text: str) -> None:
        for path, kwargs in (
            (self.user, {"project": False}),
            (self.project_cfg, {"user": False}),
        ):
            self.write(path, text)
            before = path.read_bytes()
            results, messages = self.advance(**kwargs)
            self.assertEqual(results, [])
            self.assertEqual(messages, [])
            self.assertEqual(path.read_bytes(), before)

    def test_shape_and_policy_keys(self) -> None:
        cases = {
            "passive": {
                "passive": [
                    {"files": [{"from": "docs/rule-pack.md", "to": "AGENTS.md"}]}
                ]
            },
            "active": {"active": [{"kind": "skill", "files": []}]},
            "update_policy": {"update_policy": "locked"},
            "hosts": {"hosts": ["codex"]},
            "source extra key": {"source": {"subdir": "docs"}},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                self.assert_untouched(self.entry(**overrides))

    def test_ref_outside_the_ledger(self) -> None:
        for ref in ("v0.2.0", "v9.9.9", "main", "a" * 40):
            with self.subTest(ref=ref):
                self.assert_untouched(self.entry(source={"ref": ref}))

    def test_ref_already_at_the_bundled_default(self) -> None:
        self.assert_untouched(self.entry(source={"ref": BUNDLED_REF}))

    def test_ref_that_is_not_a_string(self) -> None:
        self.assert_untouched(self.entry(source={"ref": 0.5}))

    def test_different_url(self) -> None:
        for url in (
            "https://github.com/someone-else/agent-style",
            "https://github.com/yzhao062/agent-style-fork",
            "https://example.com/yzhao062/agent-style",
        ):
            with self.subTest(url=url):
                self.assert_untouched(self.entry(source={"url": url}))

    def test_fork_url_beside_the_upstream_repo_key(self) -> None:
        """The fetch path reads ``source.url`` before ``source.repo``, so a row
        naming a fork under ``url`` and the upstream under ``repo`` selects
        the fork. It is not upstream residue, whichever key matches."""
        fork = "https://github.com/someone-else/agent-style"
        self.assert_untouched(self.entry(source={"url": fork, "repo": URL}))

    def test_a_non_default_pack_with_the_same_source(self) -> None:
        text = self.entry().replace("name: agent-style", "name: my-own-style")
        self.assert_untouched(text)

    def test_rows_without_a_source_mapping(self) -> None:
        self.assert_untouched("packs:\n  - agent-style\n")
        self.assert_untouched("packs:\n  - name: agent-style\n")
        self.assert_untouched(
            f"packs:\n  - name: agent-style\n    ref: {STALE_REF}\n"
        )
        self.assert_untouched(
            f"packs:\n  - name: agent-style\n    source: {URL}\n"
        )

    def test_a_pin_beside_residue_does_not_block_the_residue_row(self) -> None:
        text = (
            "packs:\n"
            "  - name: my-own-style\n"
            "    source:\n"
            f"      url: {URL}\n"
            f"      ref: {STALE_REF}\n"
            "  - name: agent-style\n"
            "    source:\n"
            f"      url: {URL}\n"
            f"      ref: {STALE_REF}\n"
        )
        self.write(self.user, text)
        self.advance(project=False)
        data = yaml.safe_load(self.user.read_text(encoding="utf-8"))
        self.assertEqual(data["packs"][0]["source"]["ref"], STALE_REF)
        self.assertEqual(data["packs"][1]["source"]["ref"], BUNDLED_REF)


class TestUrlPrecedenceMatchesTheFetchPath(_TempCase):
    def test_url_wins_over_repo_when_both_name_the_upstream_side(self) -> None:
        """Upstream under ``url`` and a fork under ``repo``: composition
        fetches ``url``, so the row is upstream residue and advances."""
        fork = "https://github.com/someone-else/agent-style"
        text = (
            "packs:\n"
            "  - name: agent-style\n"
            "    source:\n"
            f"      url: {URL}\n"
            f"      repo: {fork}\n"
            f"      ref: {STALE_REF}\n"
        )
        self.write(self.user, text)
        self.advance(project=False)
        self.assertEqual(
            self.user.read_text(encoding="utf-8"),
            text.replace(STALE_REF, BUNDLED_REF),
        )

    def test_empty_url_falls_back_to_repo_like_the_fetch_path(self) -> None:
        text = (
            "packs:\n"
            "  - name: agent-style\n"
            "    source:\n"
            "      url: ''\n"
            f"      repo: {URL}\n"
            f"      ref: {STALE_REF}\n"
        )
        self.write(self.user, text)
        self.advance(project=False)
        self.assertEqual(
            self.user.read_text(encoding="utf-8"),
            text.replace(STALE_REF, BUNDLED_REF),
        )


class TestSharedYamlNodes(_TempCase):
    """An anchor shared with another row or key must not drag it along."""

    def load(self, path: Path) -> dict:
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_a_shared_source_does_not_advance_a_locked_row(self) -> None:
        shared = f"url: {URL}\n      ref: {STALE_REF}\n"
        cases = {
            "anchor in the residue row": (
                "packs:\n"
                "  - name: agent-style\n"
                f"    source: &shared\n      {shared}"
                "  - name: deliberate-other\n"
                "    source: *shared\n"
                "    update_policy: locked\n"
            ),
            "anchor in the locked row": (
                "packs:\n"
                "  - name: deliberate-other\n"
                f"    source: &shared\n      {shared}"
                "    update_policy: locked\n"
                "  - name: agent-style\n"
                "    source: *shared\n"
            ),
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.write(self.user, text)
                results, _messages = self.advance(project=False)
                self.assertEqual(
                    [r.advanced for r in results],
                    [[("agent-style", STALE_REF, BUNDLED_REF)]],
                )
                rows = {row["name"]: row for row in self.load(self.user)["packs"]}
                self.assertEqual(rows["agent-style"]["source"]["ref"], BUNDLED_REF)
                self.assertEqual(rows["deliberate-other"]["source"]["ref"], STALE_REF)
                self.assertEqual(rows["deliberate-other"]["update_policy"], "locked")
                self.assertEqual(rows["deliberate-other"]["source"]["url"], URL)

    def test_a_locked_row_of_the_same_name_keeps_its_ref(self) -> None:
        text = (
            "packs:\n"
            "  - name: agent-style\n"
            "    source: &shared\n"
            f"      url: {URL}\n"
            f"      ref: {STALE_REF}\n"
            "  - name: agent-style\n"
            "    source: *shared\n"
            "    update_policy: locked\n"
        )
        self.write(self.user, text)
        self.advance(project=False)
        first, second = self.load(self.user)["packs"]
        self.assertEqual(first["source"]["ref"], BUNDLED_REF)
        self.assertEqual(second["source"]["ref"], STALE_REF)
        self.assertEqual(second["update_policy"], "locked")

    def test_the_shared_source_stays_untouched_for_a_pinned_pair(self) -> None:
        """Neither row is residue: the file is not rewritten at all."""
        text = (
            "packs:\n"
            "  - name: agent-style\n"
            "    source: &shared\n"
            f"      url: {URL}\n"
            f"      ref: {STALE_REF}\n"
            "    update_policy: locked\n"
            "  - name: deliberate-other\n"
            "    source: *shared\n"
            "    update_policy: locked\n"
        )
        self.write(self.user, text)
        before = self.user.read_bytes()
        results, messages = self.advance(project=False)
        self.assertEqual((results, messages), ([], []))
        self.assertEqual(self.user.read_bytes(), before)

    def test_two_residue_rows_sharing_one_source_both_advance(self) -> None:
        # A 7-character ref makes a double patch of the shared scalar visible.
        long_ref = "v0.10.0"
        text = (
            "packs:\n"
            "  - name: agent-style\n"
            "    source: &shared\n"
            f"      url: {URL}\n"
            f"      ref: {STALE_REF}\n"
            "  - name: agent-style\n"
            "    source: *shared\n"
        )
        self.write(self.user, text)
        results = residue.advance_residue(
            user_config_path=self.user,
            project_root=None,
            bundled_packs=[
                {"name": "agent-style", "source": {"repo": URL, "ref": long_ref}}
            ],
            default_names=DEFAULT_NAMES,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0].advanced), 2)
        refs = [row["source"]["ref"] for row in self.load(self.user)["packs"]]
        self.assertEqual(refs, [long_ref, long_ref])

    def test_a_pack_list_that_another_key_aliases_is_edited_alone(self) -> None:
        rows = (
            "  - name: agent-style\n"
            "    source:\n"
            f"      url: {URL}\n"
            f"      ref: {STALE_REF}\n"
        )
        cases = {
            "anchor on another key": f"mirror: &rows\n{rows}packs: *rows\n",
            "anchor on packs": f"packs: &rows\n{rows}mirror: *rows\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.write(self.user, text)
                results, _messages = self.advance(project=False)
                self.assertEqual(
                    [r.advanced for r in results],
                    [[("agent-style", STALE_REF, BUNDLED_REF)]],
                )
                data = self.load(self.user)
                self.assertEqual(data["packs"][0]["source"]["ref"], BUNDLED_REF)
                self.assertEqual(data["mirror"][0]["source"]["ref"], STALE_REF)

    def test_unshared_anchors_still_take_the_comment_preserving_patch(self) -> None:
        """An anchor that no residue row touches must not force the full dump."""
        text = (
            "# keep this comment\n"
            "defaults: &defaults\n"
            "  hosts: [claude-code]\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source:\n"
            f"      url: {URL}\n"
            f"      ref: {STALE_REF}  # trailing note\n"
            "  - name: other\n"
            "    <<: *defaults\n"
        )
        self.write(self.user, text)
        self.advance(project=False)
        self.assertEqual(
            self.user.read_text(encoding="utf-8"),
            text.replace(STALE_REF, BUNDLED_REF),
        )


class TestUnreadableInput(_TempCase):
    def test_absent_files_are_left_absent(self) -> None:
        results, messages = self.advance()
        self.assertEqual((results, messages), ([], []))
        self.assertFalse(self.user.parent.exists())
        self.assertFalse(self.project_cfg.exists())

    def test_nothing_to_do_creates_no_lock_file(self) -> None:
        self.write(self.user, _residue_config(ref=BUNDLED_REF))
        self.advance()
        self.assertEqual(
            sorted(p.name for p in self.user.parent.iterdir()), ["config.yaml"]
        )

    def test_unparsable_and_unusable_files_are_left_alone(self) -> None:
        cases = {
            "malformed yaml": b"packs: [unclosed\n  - {\n",
            "top-level list": b"- agent-style\n",
            "top-level scalar": b"agent-style\n",
            "empty": b"",
            "whitespace": b"\n\n  \n",
            "not utf-8": b"\xff\xfe\x00packs\n",
            "packs not a list": b"packs: agent-style\n",
            "packs null": b"packs:\n",
        }
        for label, payload in cases.items():
            with self.subTest(label):
                for path, kwargs in (
                    (self.user, {"project": False}),
                    (self.project_cfg, {"user": False}),
                ):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
                    results, messages = self.advance(**kwargs)
                    self.assertEqual((results, messages), ([], []))
                    self.assertEqual(path.read_bytes(), payload)

    def test_a_directory_in_place_of_the_file_is_ignored(self) -> None:
        self.project_cfg.mkdir()
        self.assertEqual(self.advance(user=False), ([], []))

    def test_yaml_unavailable(self) -> None:
        self.write(self.user, _residue_config())
        self.write(self.project_cfg, _residue_config())
        before = (self.user.read_bytes(), self.project_cfg.read_bytes())
        with patch.object(residue, "yaml", None):
            results, messages = self.advance()
        self.assertEqual((results, messages), ([], []))
        self.assertEqual((self.user.read_bytes(), self.project_cfg.read_bytes()), before)

    def test_bundled_manifest_without_a_usable_default(self) -> None:
        self.write(self.user, _residue_config())
        before = self.user.read_bytes()
        for bundled in (
            [],
            [{"name": "aa-core-skills"}],
            [{"name": "agent-style"}],
            [{"name": "agent-style", "source": {"repo": URL}}],
            [{"name": "agent-style", "source": "bundled"}],
            ["agent-style", None],
        ):
            with self.subTest(bundled=bundled):
                results = residue.advance_residue(
                    user_config_path=self.user,
                    project_root=None,
                    bundled_packs=bundled,
                    default_names=DEFAULT_NAMES,
                )
                self.assertEqual(results, [])
                self.assertEqual(self.user.read_bytes(), before)

    def test_only_default_names_are_considered(self) -> None:
        self.write(self.user, _residue_config())
        before = self.user.read_bytes()
        results = residue.advance_residue(
            user_config_path=self.user,
            project_root=None,
            bundled_packs=BUNDLED_PACKS,
            default_names=frozenset({"aa-core-skills"}),
        )
        self.assertEqual(results, [])
        self.assertEqual(self.user.read_bytes(), before)


class TestUserConfigLock(_TempCase):
    def spy(self, seen, *, hook=None):
        real = scripts_locks.acquire

        @contextmanager
        def acquire(path, timeout=scripts_locks.DEFAULT_TIMEOUT_SECONDS):
            seen.append(Path(path))
            with real(path, timeout=timeout) as handle:
                if hook is not None:
                    hook()
                yield handle

        return acquire

    def test_user_level_rewrite_takes_the_lock_pack_add_takes(self) -> None:
        from anywhere_agents import cli

        self.write(self.user, _residue_config())
        self.write(self.project_cfg, _residue_config())
        seen: list[Path] = []
        with patch.object(scripts_locks, "acquire", self.spy(seen)):
            self.advance()
        expected = self.user.with_name("config.yaml.lock")
        self.assertEqual(seen, [expected])
        self.assertEqual(cli._user_config_lock_path(self.user), expected)
        self.assertEqual(residue.user_config_lock_path(self.user), expected)

    def test_lock_is_not_held_after_the_rewrite(self) -> None:
        self.write(self.user, _residue_config())
        self.advance(project=False)
        self.assertFalse(scripts_locks.is_held(self.user.with_name("config.yaml.lock")))

    def test_file_is_planned_again_under_the_lock(self) -> None:
        """A concurrent ``pack add`` can rewrite the file between the
        lock-free read and the lock; the rewrite must follow what is on disk
        once the lock is held."""
        self.write(self.user, _residue_config())
        pinned = (
            "packs:\n"
            "  - name: agent-style\n"
            "    source:\n"
            f"      url: {URL}\n"
            f"      ref: {STALE_REF}\n"
            "    update_policy: locked\n"
        )
        seen: list[Path] = []
        spy = self.spy(seen, hook=lambda: self.write(self.user, pinned))
        with patch.object(scripts_locks, "acquire", spy):
            results, messages = self.advance(project=False)
        self.assertEqual((results, messages), ([], []))
        self.assertEqual(self.user.read_text(encoding="utf-8"), pinned)

    def test_busy_lock_warns_and_leaves_the_file(self) -> None:
        self.write(self.user, _residue_config())
        self.write(self.project_cfg, _residue_config())

        @contextmanager
        def busy(path, timeout=0.0):
            raise scripts_locks.LockTimeout(Path(path), timeout, 4242)
            yield  # pragma: no cover

        with patch.object(scripts_locks, "acquire", busy):
            results, messages = self.advance()
        self.assertEqual(self.user.read_text(encoding="utf-8"), _residue_config())
        self.assertEqual([r.path for r in results], [self.project_cfg])
        warnings = [m for m in messages if m.startswith("warning:")]
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(self.user), warnings[0])
        self.assertIn("4242", warnings[0])

    @unittest.skipIf(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        "needs POSIX permissions and a non-root user",
    )
    def test_unwritable_directory_warns_and_leaves_the_file(self) -> None:
        self.write(self.project_cfg, _residue_config())
        before = self.project_cfg.read_bytes()
        os.chmod(self.project, 0o555)
        self.addCleanup(os.chmod, self.project, 0o755)
        results, messages = self.advance(user=False)
        self.assertEqual(results, [])
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("warning:"))
        self.assertEqual(self.project_cfg.read_bytes(), before)


class TestBundledDefaultSources(unittest.TestCase):
    def test_reads_repo_url_and_ref_for_covered_default_packs(self) -> None:
        self.assertEqual(
            residue.bundled_default_sources(BUNDLED_PACKS, DEFAULT_NAMES),
            {"agent-style": (URL, BUNDLED_REF)},
        )

    def test_legacy_url_and_top_level_ref_are_read(self) -> None:
        packs = [{"name": "agent-style", "source": {"url": URL}, "ref": "v1.2.3"}]
        self.assertEqual(
            residue.bundled_default_sources(packs, DEFAULT_NAMES),
            {"agent-style": (URL, "v1.2.3")},
        )

    def test_url_wins_over_repo_like_the_fetch_path(self) -> None:
        other = "https://github.com/someone-else/agent-style"
        packs = [
            {
                "name": "agent-style",
                "source": {"url": URL, "repo": other, "ref": "v1.2.3"},
            }
        ]
        self.assertEqual(
            residue.bundled_default_sources(packs, DEFAULT_NAMES),
            {"agent-style": (URL, "v1.2.3")},
        )

    def test_real_bundled_manifest_yields_the_current_default(self) -> None:
        from packs import schema

        parsed = schema.parse_manifest(ROOT / "bootstrap" / "packs.yaml")
        sources = residue.bundled_default_sources(
            parsed["packs"], compose_packs.DEFAULT_V2_SELECTION_NAMES
        )
        self.assertEqual(
            sources,
            {"agent-style": (URL, BUNDLED_AGENT_STYLE_REF)},
        )


class TestIsBundledDefault(unittest.TestCase):
    """``residue.is_bundled_default``: the row the rewrite leaves behind.

    After the rewrite advances a stale row, what remains restates the bundled
    default. The verify classifier asks this predicate whether such a row is
    an override, so it has to agree with ``_match`` on what a reconciled row
    looks like and still refuse every row that carries the user's own choice.
    """

    BUNDLED = {"agent-style": (URL, BUNDLED_REF)}

    def row(self, **overrides) -> dict:
        source = {"url": URL, "ref": BUNDLED_REF}
        source.update(overrides.pop("source", {}))
        body = {"name": "agent-style", "source": source}
        body.update(overrides)
        return body

    def test_the_exact_bundled_row(self) -> None:
        self.assertTrue(residue.is_bundled_default(self.row(), self.BUNDLED))

    def test_url_spellings_that_normalize_to_the_bundled_url(self) -> None:
        for url in (
            URL + ".git",
            URL + "/",
            "https://GitHub.com/YZhao062/Agent-Style",
        ):
            with self.subTest(url=url):
                self.assertTrue(
                    residue.is_bundled_default(
                        self.row(source={"url": url}), self.BUNDLED
                    )
                )

    def test_repo_alias_and_legacy_top_level_ref(self) -> None:
        self.assertTrue(
            residue.is_bundled_default(
                {"name": "agent-style", "source": {"repo": URL, "ref": BUNDLED_REF}},
                self.BUNDLED,
            )
        )
        self.assertTrue(
            residue.is_bundled_default(
                {"name": "agent-style", "source": {"url": URL}, "ref": BUNDLED_REF},
                self.BUNDLED,
            )
        )

    def test_a_row_that_differs_in_one_respect_is_not_the_default(self) -> None:
        cases = {
            "ref in the ledger, not the bundled one": self.row(
                source={"ref": STALE_REF}
            ),
            "ref outside the ledger": self.row(source={"ref": "v0.2.0"}),
            "moving ref": self.row(source={"ref": "main"}),
            "ref that is not a string": self.row(source={"ref": 0.5}),
            "different owner": self.row(
                source={"url": "https://github.com/someone-else/agent-style"}
            ),
            "different host": self.row(
                source={"url": "https://example.com/yzhao062/agent-style"}
            ),
            "fork url beside the upstream repo key": self.row(
                source={
                    "url": "https://github.com/someone-else/agent-style",
                    "repo": URL,
                }
            ),
            "update_policy": self.row(update_policy="locked"),
            "update_policy equal to the default": self.row(update_policy="auto"),
            "passive": self.row(
                passive=[{"files": [{"from": "docs/x.md", "to": "AGENTS.md"}]}]
            ),
            "active": self.row(active=[{"kind": "skill", "files": []}]),
            "hosts": self.row(hosts=["codex"]),
            "source extra key": self.row(source={"subdir": "docs"}),
            "no source": {"name": "agent-style"},
            "string source": {"name": "agent-style", "source": URL},
            "not a mapping": "agent-style",
            "another pack name": self.row(name="my-own-style"),
        }
        for label, entry in cases.items():
            with self.subTest(label):
                self.assertFalse(residue.is_bundled_default(entry, self.BUNDLED))

    def test_a_pack_the_ledger_does_not_cover(self) -> None:
        self.assertFalse(residue.is_bundled_default(self.row(), {}))


# =====================================================================
# One ledger, four views of it.
# =====================================================================


class TestLedgerHasOneSource(unittest.TestCase):
    EXPECTED = {
        "agent-style": {"v0.3.2", "v0.3.5", "v0.3.6", "v0.4.1", "v0.5.0", "v0.6.0", "v0.7.0", "v0.8.0", "v0.9.0"}
    }

    def test_ledger_contents(self) -> None:
        self.assertEqual(residue.AUTO_RECONCILED_DEFAULT_REF_REWRITES, self.EXPECTED)

    def test_current_bundled_ref_is_listed(self) -> None:
        self.assertIn(
            BUNDLED_AGENT_STYLE_REF,
            residue.AUTO_RECONCILED_DEFAULT_REF_REWRITES["agent-style"],
        )

    def test_cli_and_vendored_copy_read_the_same_ledger(self) -> None:
        from anywhere_agents import cli
        from anywhere_agents.packs import residue as vendored

        self.assertEqual(vendored.AUTO_RECONCILED_DEFAULT_REF_REWRITES, self.EXPECTED)
        self.assertIs(
            cli._AUTO_RECONCILED_DEFAULT_REF_REWRITES,
            vendored.AUTO_RECONCILED_DEFAULT_REF_REWRITES,
        )
        self.assertIs(cli._residue, vendored)

    def test_cli_no_longer_defines_its_own_ledger(self) -> None:
        source = (
            ROOT / "packages" / "pypi" / "anywhere_agents" / "cli.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn('"v0.3.6"', source)

    def test_packaged_composer_carries_the_module_byte_for_byte(self) -> None:
        mirror = (
            ROOT
            / "packages"
            / "pypi"
            / "anywhere_agents"
            / "composer"
            / "scripts"
            / "packs"
            / "residue.py"
        )
        self.assertEqual(
            mirror.read_bytes(),
            (ROOT / "scripts" / "packs" / "residue.py").read_bytes(),
        )


# =====================================================================
# Composer path (what bootstrap.sh and bootstrap.ps1 run).
# =====================================================================


class _ComposerEnv(_TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.base / "home"
        self.home.mkdir()
        self.xdg = self.base / "xdg"
        self.appdata = self.base / "appdata"
        patcher = patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "USERPROFILE": str(self.home),
                "XDG_CONFIG_HOME": str(self.xdg),
                "APPDATA": str(self.appdata),
                "AGENT_CONFIG_PACKS": "",
                "AGENT_CONFIG_RULE_PACKS": "",
                "AGENT_CONFIG_HOST": "",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ANYWHERE_AGENTS_UPDATE", None)
        # Where the composer reads the user-level config on this platform:
        # under XDG_CONFIG_HOME on POSIX, under APPDATA on Windows.
        self.user = config_mod.user_config_path()

    def run_helper(self) -> str:
        err = io.StringIO()
        with redirect_stderr(err):
            compose_packs._advance_default_ref_residue(
                self.project, {"version": 2, "packs": BUNDLED_PACKS}
            )
        return err.getvalue()


class TestComposerHelper(_ComposerEnv):
    def test_user_level_row_is_advanced_through_the_resolved_path(self) -> None:
        self.write(self.user, _residue_config())
        stderr = self.run_helper()
        self.assertEqual(
            self.user.read_text(encoding="utf-8"), _residue_config(ref=BUNDLED_REF)
        )
        self.assertEqual(stderr.count("advanced stale default pack ref"), 1)
        self.assertIn(f"agent-style {STALE_REF} -> {BUNDLED_REF}", stderr)

    @unittest.skipIf(sys.platform == "win32", "POSIX config home fallback")
    def test_user_level_row_is_advanced_through_the_home_fallback(self) -> None:
        del os.environ["XDG_CONFIG_HOME"]
        home_cfg = self.home / ".config" / "anywhere-agents" / "config.yaml"
        self.write(home_cfg, _residue_config())
        self.run_helper()
        self.assertEqual(
            home_cfg.read_text(encoding="utf-8"), _residue_config(ref=BUNDLED_REF)
        )

    def test_windows_style_appdata_path(self) -> None:
        win_cfg = self.appdata / "anywhere-agents" / "config.yaml"
        decoy = self.xdg / "anywhere-agents" / "config.yaml"
        self.write(win_cfg, _residue_config())
        self.write(decoy, _residue_config())
        with patch.object(sys, "platform", "win32"):
            stderr = self.run_helper()
        self.assertEqual(
            win_cfg.read_text(encoding="utf-8"), _residue_config(ref=BUNDLED_REF)
        )
        # Windows resolves %APPDATA% only; the XDG file is not the user config.
        self.assertEqual(decoy.read_text(encoding="utf-8"), _residue_config())
        self.assertIn(str(win_cfg), stderr)

    def test_project_row_is_advanced(self) -> None:
        self.write(self.project_cfg, _residue_config())
        self.run_helper()
        self.assertEqual(
            self.project_cfg.read_text(encoding="utf-8"),
            _residue_config(ref=BUNDLED_REF),
        )

    def test_user_and_project_rows_are_both_advanced(self) -> None:
        self.write(self.user, _residue_config())
        self.write(self.project_cfg, _residue_config())
        stderr = self.run_helper()
        self.assertEqual(stderr.count("advanced stale default pack ref"), 2)
        self.assertNotIn(STALE_REF, self.user.read_text(encoding="utf-8"))
        self.assertNotIn(STALE_REF, self.project_cfg.read_text(encoding="utf-8"))

    def test_deliberate_pin_is_untouched(self) -> None:
        pin = _residue_config(ref="v0.2.0")
        self.write(self.user, pin)
        pin_with_policy = _residue_config() + "    update_policy: locked\n"
        self.write(self.project_cfg, pin_with_policy)
        stderr = self.run_helper()
        self.assertEqual(stderr, "")
        self.assertEqual(self.user.read_text(encoding="utf-8"), pin)
        self.assertEqual(
            self.project_cfg.read_text(encoding="utf-8"), pin_with_policy
        )

    def test_no_files_no_output_and_nothing_created(self) -> None:
        self.assertEqual(self.run_helper(), "")
        self.assertFalse(self.xdg.exists())
        self.assertFalse(self.appdata.exists())

    def test_no_resolvable_user_config_home_still_does_the_project(self) -> None:
        self.write(self.project_cfg, _residue_config())
        with patch.object(config_mod, "user_config_path", return_value=None):
            self.run_helper()
        self.assertEqual(
            self.project_cfg.read_text(encoding="utf-8"),
            _residue_config(ref=BUNDLED_REF),
        )

    def test_helper_never_fails_composition(self) -> None:
        with patch.object(
            compose_packs.residue_mod, "advance_residue",
            side_effect=RuntimeError("boom"),
        ):
            stderr = self.run_helper()
        self.assertIn("warning:", stderr)
        self.assertIn("boom", stderr)

    def test_helper_passes_the_composer_default_names(self) -> None:
        with patch.object(
            compose_packs.residue_mod, "advance_residue", return_value=[]
        ) as advance:
            self.run_helper()
        kwargs = advance.call_args.kwargs
        self.assertEqual(kwargs["default_names"], compose_packs.DEFAULT_V2_SELECTION_NAMES)
        self.assertEqual(kwargs["project_root"], self.project)
        self.assertEqual(kwargs["user_config_path"], self.user)


class TestComposerAdvancesBeforeResolving(_ComposerEnv):
    """``main`` must advance the rows before either resolver pass reads them."""

    def test_both_resolver_passes_see_the_advanced_row(self) -> None:
        boot = self.project / ".agent-config" / "repo" / "bootstrap"
        manifest = (
            "version: 2\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source:\n"
            f"      repo: {URL}\n"
            f"      ref: {BUNDLED_REF}\n"
            "    update_policy: auto\n"
            "    passive:\n"
            "      - files:\n"
            "          - from: docs/rule-pack-compact.md\n"
            "            to: AGENTS.md\n"
            "  - name: aa-core-skills\n"
            "    update_policy: prompt\n"
        )
        self.write(boot / "packs.yaml", manifest)
        self.write(self.user, _residue_config())

        seen_refs: list[str] = []
        real = config_mod.resolved_for_project

        def recording(*args, **kwargs):
            selections = real(*args, **kwargs)
            for selection in selections:
                if selection["name"] == "agent-style":
                    seen_refs.append(selection["source"]["ref"])
            raise config_mod.ConfigError("stop after the early pass")

        with patch.object(config_mod, "resolved_for_project", recording):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = compose_packs.main(["--root", str(self.project)])
        self.assertEqual(rc, 1)
        self.assertEqual(seen_refs, [BUNDLED_REF])


class _ComposeFixture(_ComposerEnv):
    """A project whose composer fetch is a fake archive, so ``main`` runs end
    to end. The lock and the project are seeded at the stale ref, the shape a
    consumer has after aa reconciled it before the bundled pin moved."""

    def setUp(self) -> None:
        super().setUp()
        self.write(
            self.project / ".agent-config" / "repo" / "bootstrap" / "packs.yaml",
            "version: 2\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source:\n"
            f"      repo: {URL}\n"
            f"      ref: {BUNDLED_REF}\n"
            "    update_policy: auto\n"
            "    passive:\n"
            "      - files:\n"
            "          - from: docs/rule-pack-compact.md\n"
            "            to: AGENTS.md\n"
            "  - name: aa-core-skills\n"
            "    update_policy: prompt\n",
        )
        self.write(self.project / ".agent-config" / "AGENTS.md", "# upstream\n")

        lock = state_mod.empty_pack_lock()
        lock["packs"]["agent-style"] = {
            "source_url": URL,
            "requested_ref": STALE_REF,
            "resolved_commit": "c" * 40,
            "pack_update_policy": "auto",
            "files": [
                {
                    "role": "passive",
                    "host": None,
                    "source_path": "docs/rule-pack-compact.md",
                    "input_sha256": "0" * 64,
                    "output_paths": ["AGENTS.md"],
                    "output_scope": "project-local",
                    "effective_update_policy": "auto",
                },
            ],
        }
        self.lock_path = self.project / ".agent-config" / "pack-lock.json"
        state_mod.save_pack_lock(self.lock_path, lock)

        archive_dir = self.base / "fake-archive"
        (archive_dir / "docs").mkdir(parents=True)
        (archive_dir / "pack.yaml").write_text(
            "version: 2\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source:\n"
            f"      repo: {URL}\n"
            f"      ref: {BUNDLED_REF}\n"
            "    passive:\n"
            "      - files:\n"
            "          - from: docs/rule-pack-compact.md\n"
            "            to: AGENTS.md\n",
            encoding="utf-8",
        )
        (archive_dir / "docs" / "rule-pack-compact.md").write_text(
            "# compact rule body\n", encoding="utf-8"
        )
        self.archive_dir = archive_dir
        self.archive = self.archive_at(BUNDLED_REF)

    def archive_at(self, ref: str) -> MagicMock:
        """The fetch result for ``ref``. The lock records ``archive.ref``, so
        it has to echo the ref that was asked for, as a real fetch does."""
        archive = MagicMock()
        archive.archive_dir = self.archive_dir
        archive.resolved_commit = "d" * 40
        archive.method = "ssh"
        archive.url = URL
        archive.ref = ref
        return archive

    @contextmanager
    def _no_composer_lock(self):
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None

        with patch.object(locks, "acquire", side_effect=_ctx):
            yield

    def compose(self, *extra_args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with self._no_composer_lock(), patch.object(
            source_fetch, "fetch_pack",
            side_effect=lambda url, ref, **_kw: self.archive_at(ref),
        ) as fetch, patch.object(
            source_fetch, "load_cached_archive", return_value=self.archive
        ), redirect_stdout(out), redirect_stderr(err):
            rc = compose_packs.main(["--root", str(self.project), *extra_args])
        self.fetched_refs = [call.args[1] for call in fetch.call_args_list]
        return rc, err.getvalue()

    def lock_ref(self) -> str:
        return state_mod.load_pack_lock(self.lock_path)["packs"]["agent-style"][
            "requested_ref"
        ]


class TestComposeEndToEnd(_ComposeFixture):
    def test_user_level_residue_no_longer_holds_the_pack_at_the_old_ref(self) -> None:
        """The reported failure: the user-level config carried the old ref, no
        project row existed, and every consumer's bootstrap kept composing it."""
        self.write(self.user, "# managed by anywhere-agents\n" + _residue_config())
        rc, stderr = self.compose()
        self.assertEqual(rc, 0, stderr)

        self.assertEqual(self.fetched_refs, [BUNDLED_REF])
        self.assertEqual(self.lock_ref(), BUNDLED_REF)
        agents_md = (self.project / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("compact rule body", agents_md)
        self.assertIn(f"version={BUNDLED_REF}", agents_md)
        self.assertNotIn(f"version={STALE_REF}", agents_md)
        self.assertEqual(
            self.user.read_text(encoding="utf-8"),
            "# managed by anywhere-agents\n" + _residue_config(ref=BUNDLED_REF),
        )
        self.assertEqual(stderr.count("advanced stale default pack ref"), 1)

    def test_control_without_the_advance_the_old_ref_wins(self) -> None:
        """Documents the bug the advance fixes: with it disabled, the same
        fixture composes the stale ref."""
        self.write(self.user, _residue_config())
        with patch.object(compose_packs, "_advance_default_ref_residue"):
            rc, stderr = self.compose()
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(self.fetched_refs, [STALE_REF])
        self.assertEqual(self.lock_ref(), STALE_REF)

    def test_project_level_residue_converges(self) -> None:
        self.write(self.project_cfg, _residue_config(key="rule_packs"))
        rc, stderr = self.compose()
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(self.fetched_refs, [BUNDLED_REF])
        self.assertEqual(self.lock_ref(), BUNDLED_REF)
        self.assertEqual(
            self.project_cfg.read_text(encoding="utf-8"),
            _residue_config(key="rule_packs", ref=BUNDLED_REF),
        )

    def test_custom_manifest_leaves_both_persistent_selections_alone(self) -> None:
        """``--manifest`` is a one-off input. Its ref is not the bundled
        default, so composing with it must not rewrite the user-level or the
        project config to that ref."""
        custom = self.base / "custom-packs.yaml"
        self.write(
            custom,
            "version: 2\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source:\n"
            f"      repo: {URL}\n"
            "      ref: experiment\n"
            "    update_policy: auto\n"
            "    passive:\n"
            "      - files:\n"
            "          - from: docs/rule-pack-compact.md\n"
            "            to: AGENTS.md\n"
            "  - name: aa-core-skills\n"
            "    update_policy: prompt\n",
        )
        self.write(self.user, _residue_config())
        self.write(self.project_cfg, _residue_config())
        before = (self.user.read_bytes(), self.project_cfg.read_bytes())
        with patch.object(
            compose_packs, "_advance_default_ref_residue"
        ) as advance:
            self.compose("--manifest", str(custom))
        advance.assert_not_called()
        _rc, stderr = self.compose("--manifest", str(custom))
        self.assertNotIn("advanced stale default pack ref", stderr)
        self.assertNotIn("experiment", self.user.read_text(encoding="utf-8"))
        self.assertNotIn("experiment", self.project_cfg.read_text(encoding="utf-8"))
        self.assertEqual(
            (self.user.read_bytes(), self.project_cfg.read_bytes()), before
        )

    def test_user_level_pin_is_composed_as_pinned_and_left_alone(self) -> None:
        pin = _residue_config(ref="v0.2.0")
        self.write(self.user, pin)
        rc, stderr = self.compose()
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(self.fetched_refs, ["v0.2.0"])
        self.assertEqual(self.user.read_text(encoding="utf-8"), pin)
        self.assertNotIn("advanced stale default pack ref", stderr)

    def test_second_compose_is_quiet_and_changes_no_config(self) -> None:
        self.write(self.user, _residue_config())
        self.write(self.project_cfg, _residue_config())
        self.assertEqual(self.compose()[0], 0)
        after = (self.user.read_bytes(), self.project_cfg.read_bytes())
        rc, stderr = self.compose()
        self.assertEqual(rc, 0, stderr)
        self.assertNotIn("advanced stale default pack ref", stderr)
        self.assertEqual((self.user.read_bytes(), self.project_cfg.read_bytes()), after)

    def test_malformed_user_config_still_fails_the_way_it_did(self) -> None:
        """The advance leaves the file alone and the resolver reports it, so
        this change does not turn a config error into silence."""
        self.write(self.user, "packs: [unclosed\n")
        rc, stderr = self.compose()
        self.assertEqual(rc, 1)
        self.assertIn("error:", stderr)
        self.assertEqual(self.user.read_text(encoding="utf-8"), "packs: [unclosed\n")


# =====================================================================
# CLI path (bare ``anywhere-agents`` and ``pack verify --fix``).
# =====================================================================


class TestCliAdvancesBothLayers(_ComposerEnv):
    def rewrite(self, *args):
        from anywhere_agents import cli

        err = io.StringIO()
        with redirect_stderr(err):
            names = cli._rewrite_auto_reconciled_default_refs(*args)
        return names, err.getvalue()

    def test_user_and_project_rows_are_advanced(self) -> None:
        self.write(self.user, _residue_config())
        self.write(self.project_cfg, _residue_config(key="rule_packs"))
        names, stderr = self.rewrite(self.project, self.user)
        self.assertEqual(names, {"agent-style"})
        self.assertEqual(
            self.user.read_text(encoding="utf-8"),
            _residue_config(ref=BUNDLED_AGENT_STYLE_REF),
        )
        self.assertEqual(
            self.project_cfg.read_text(encoding="utf-8"),
            _residue_config(key="rule_packs", ref=BUNDLED_AGENT_STYLE_REF),
        )
        self.assertEqual(stderr.count("advanced stale default pack ref"), 2)
        self.assertTrue(stderr.startswith("[anywhere-agents] "))

    def test_user_level_row_alone(self) -> None:
        self.write(self.user, _residue_config())
        names, _ = self.rewrite(self.project, self.user)
        self.assertEqual(names, {"agent-style"})
        self.assertIn(BUNDLED_AGENT_STYLE_REF, self.user.read_text(encoding="utf-8"))

    def test_project_only_call_leaves_the_user_level_row(self) -> None:
        self.write(self.user, _residue_config())
        self.write(self.project_cfg, _residue_config())
        names, _ = self.rewrite(self.project)
        self.assertEqual(names, {"agent-style"})
        self.assertEqual(self.user.read_text(encoding="utf-8"), _residue_config())
        self.assertIn(
            BUNDLED_AGENT_STYLE_REF, self.project_cfg.read_text(encoding="utf-8")
        )

    def test_no_user_config_path_is_accepted(self) -> None:
        self.write(self.project_cfg, _residue_config())
        names, _ = self.rewrite(self.project, None)
        self.assertEqual(names, {"agent-style"})

    def test_deliberate_pins_are_untouched(self) -> None:
        pins = (
            _residue_config(ref="v0.2.0"),
            _residue_config() + "    update_policy: locked\n",
            _residue_config().replace(URL, "https://github.com/someone/agent-style"),
        )
        for pin in pins:
            with self.subTest(pin=pin):
                self.write(self.user, pin)
                self.write(self.project_cfg, pin)
                names, stderr = self.rewrite(self.project, self.user)
                self.assertEqual((names, stderr), (set(), ""))
                self.assertEqual(self.user.read_text(encoding="utf-8"), pin)
                self.assertEqual(self.project_cfg.read_text(encoding="utf-8"), pin)

    def test_second_call_is_a_no_op(self) -> None:
        self.write(self.user, _residue_config())
        self.rewrite(self.project, self.user)
        names, stderr = self.rewrite(self.project, self.user)
        self.assertEqual((names, stderr), (set(), ""))

    def test_pack_verify_fix_passes_the_user_config_path(self) -> None:
        from anywhere_agents import cli

        self.write(self.project_cfg, "")
        self.write(self.user, _residue_config())
        with patch.object(
            cli, "_rewrite_auto_reconciled_default_refs", return_value=set()
        ) as rewrite, patch.object(
            cli, "_verify_gather", side_effect=cli._VerifyParseError("stop here")
        ), redirect_stderr(io.StringIO()):
            rc = cli._pack_verify_fix(self.user, self.project, MagicMock())
        self.assertEqual(rc, 2)
        rewrite.assert_called_once_with(self.project, self.user)


class TestUserLevelOverrideCheck(_ComposerEnv):
    """``cli._has_explicit_default_override`` for a user-level row.

    The bare identity check used to call every user-level row that names a
    source an override, including the row the residue rewrite had just
    advanced to the bundled default. With the user config path the check
    reads the rows themselves, and only a row that is exactly the bundled
    default stops counting. Everything else keeps counting, as before.
    """

    def setUp(self) -> None:
        super().setUp()
        from anywhere_agents import cli

        self.cli = cli

    def row_for(self, entry: dict) -> dict:
        return {"u": self.cli._identity_for_user_entry(entry), "p": None, "l": None}

    def override(self, text: str, *, path: bool = True) -> bool:
        self.write(self.user, text)
        entry = yaml.safe_load(text)["packs"][0]
        return self.cli._has_explicit_default_override(
            self.project,
            self.row_for(entry),
            "agent-style",
            self.user if path else None,
        )

    def test_the_bundled_default_row_is_not_an_override(self) -> None:
        self.assertFalse(
            self.override(_residue_config(ref=BUNDLED_AGENT_STYLE_REF))
        )

    def test_the_same_row_is_an_override_without_the_user_config_path(self) -> None:
        self.assertTrue(
            self.override(_residue_config(ref=BUNDLED_AGENT_STYLE_REF), path=False)
        )

    def test_deliberate_pins_are_overrides(self) -> None:
        at_bundled = _residue_config(ref=BUNDLED_AGENT_STYLE_REF)
        pins = {
            "stale ref in the ledger": _residue_config(ref=STALE_REF),
            "ref outside the ledger": _residue_config(ref="v0.2.0"),
            "update_policy": at_bundled + "    update_policy: locked\n",
            "passive": at_bundled
            + "    passive:\n"
            + "      - files:\n"
            + "          - from: docs/rule-pack.md\n"
            + "            to: AGENTS.md\n",
            "active": at_bundled
            + "    active:\n"
            + "      - kind: skill\n"
            + "        files: []\n",
            "fork url": at_bundled.replace(
                URL, "https://github.com/someone-else/agent-style"
            ),
        }
        for label, text in pins.items():
            with self.subTest(label):
                self.assertTrue(self.override(text))

    def test_one_pinned_row_among_default_rows_still_counts(self) -> None:
        text = (
            _residue_config(ref=BUNDLED_AGENT_STYLE_REF)
            + "  - name: agent-style\n"
            + "    source:\n"
            + f"      url: {URL}\n"
            + "      ref: v0.2.0\n"
        )
        self.write(self.user, text)
        entry = yaml.safe_load(text)["packs"][0]
        self.assertTrue(
            self.cli._has_explicit_default_override(
                self.project, self.row_for(entry), "agent-style", self.user
            )
        )

    def test_an_unreadable_user_config_keeps_the_row_an_override(self) -> None:
        entry = yaml.safe_load(_residue_config(ref=BUNDLED_AGENT_STYLE_REF))[
            "packs"
        ][0]
        for text in ("packs: [unclosed\n", "- just\n- a list\n", ""):
            with self.subTest(text=text):
                self.write(self.user, text)
                self.assertTrue(
                    self.cli._has_explicit_default_override(
                        self.project, self.row_for(entry), "agent-style", self.user
                    )
                )
        self.assertTrue(
            self.cli._has_explicit_default_override(
                self.project,
                self.row_for(entry),
                "agent-style",
                self.base / "absent.yaml",
            )
        )

    def test_a_pack_the_ledger_does_not_cover_keeps_the_identity_rule(self) -> None:
        text = (
            "packs:\n"
            "  - name: aa-core-skills\n"
            "    source:\n"
            f"      url: {URL}\n"
            f"      ref: {BUNDLED_AGENT_STYLE_REF}\n"
        )
        self.write(self.user, text)
        entry = yaml.safe_load(text)["packs"][0]
        self.assertTrue(
            self.cli._has_explicit_default_override(
                self.project, self.row_for(entry), "aa-core-skills", self.user
            )
        )


if __name__ == "__main__":
    unittest.main()
