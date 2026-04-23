"""Tests for scripts/packs/schema.py.

Schema acceptance for both v1 (legacy passive-only) and v2 (unified) shapes;
rejection for v0.4.0-out-of-scope features (private source URLs,
``update_policy: auto`` on active entries, unknown kinds).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import schema  # noqa: E402


def _write_manifest(dir_path: Path, text: str) -> Path:
    """Write ``text`` to ``packs.yaml`` inside ``dir_path`` and return it."""
    p = dir_path / "packs.yaml"
    p.write_text(text, encoding="utf-8")
    return p


class _TmpDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


# ---------- v1 legacy shape ----------


class LegacyV1Tests(_TmpDirCase):
    def test_minimal_legacy_accepts(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 1\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source: https://example.com/{ref}/rule-pack.md\n"
            "    default-ref: v0.3.2\n",
        )
        parsed = schema.parse_manifest(path)
        self.assertEqual(parsed["version"], 1)
        self.assertEqual(len(parsed["packs"]), 1)
        pack = parsed["packs"][0]
        self.assertEqual(pack["name"], "agent-style")
        self.assertTrue(pack["_legacy"])

    def test_shipped_manifest_parses(self) -> None:
        """The real bootstrap/packs.yaml we ship today must parse."""
        shipped = ROOT / "bootstrap" / "packs.yaml"
        parsed = schema.parse_manifest(shipped)
        self.assertEqual(parsed["version"], 1)
        names = [p["name"] for p in parsed["packs"]]
        self.assertIn("agent-style", names)

    def test_legacy_missing_source_rejects(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 1\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    default-ref: v0.3.2\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"'source' must be"):
            schema.parse_manifest(path)

    def test_legacy_missing_default_ref_rejects(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 1\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source: https://example.com/{ref}/rule-pack.md\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"'default-ref'"):
            schema.parse_manifest(path)

    def test_legacy_v1_with_active_rejects(self) -> None:
        """``active:`` entries only belong in version-2 manifests."""
        path = _write_manifest(
            self.root,
            "version: 1\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    source: https://example.com/{ref}/rule-pack.md\n"
            "    default-ref: v0.3.2\n"
            "    active:\n"
            "      - kind: hook\n"
            "        hosts: [claude-code]\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"version-1 manifest"):
            schema.parse_manifest(path)


# ---------- v2 unified shape ----------


class UnifiedV2Tests(_TmpDirCase):
    def test_minimal_passive_only_accepts(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source:\n"
            "      repo: https://github.com/yzhao062/agent-style\n"
            "      ref: v0.3.2\n"
            "    passive:\n"
            "      - files:\n"
            "          - {from: docs/rule-pack.md, to: AGENTS.md}\n",
        )
        parsed = schema.parse_manifest(path)
        self.assertEqual(parsed["version"], 2)
        pack = parsed["packs"][0]
        self.assertFalse(pack["_legacy"])
        self.assertEqual(len(pack["passive"]), 1)

    def test_active_skill_accepts(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: implement-review\n"
            "    active:\n"
            "      - kind: skill\n"
            "        hosts: [claude-code]\n"
            "        files:\n"
            "          - {from: skills/implement-review/, to: .claude/skills/implement-review/}\n",
        )
        parsed = schema.parse_manifest(path)
        pack = parsed["packs"][0]
        self.assertEqual(pack["active"][0]["kind"], "skill")

    def test_all_four_kinds_accept(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: multi-kind\n"
            "    active:\n"
            "      - kind: skill\n"
            "        hosts: [claude-code]\n"
            "        files: [{from: skills/x/, to: .claude/skills/x/}]\n"
            "      - kind: hook\n"
            "        hosts: [claude-code]\n"
            "        files: [{from: scripts/h.py, to: ~/.claude/hooks/x/01-h.py}]\n"
            "      - kind: permission\n"
            "        hosts: [claude-code]\n"
            "        files: [{from: settings.json, to: ~/.claude/settings.json}]\n"
            "      - kind: command\n"
            "        hosts: [claude-code]\n"
            "        files: [{from: commands/c.md, to: .claude/commands/c.md}]\n",
        )
        parsed = schema.parse_manifest(path)
        kinds = [e["kind"] for e in parsed["packs"][0]["active"]]
        self.assertEqual(kinds, ["skill", "hook", "permission", "command"])

    def test_unknown_kind_rejects(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: bad-pack\n"
            "    active:\n"
            "      - kind: widget\n"
            "        hosts: [claude-code]\n"
            "        files: [{from: x, to: y}]\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"unknown 'kind' 'widget'"):
            schema.parse_manifest(path)

    def test_update_policy_auto_on_active_rejects(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    active:\n"
            "      - kind: hook\n"
            "        hosts: [claude-code]\n"
            "        files: [{from: x, to: y}]\n"
            "        update_policy: auto\n",
        )
        with self.assertRaisesRegex(
            schema.ParseError, r"'update_policy: auto' is not allowed"
        ):
            schema.parse_manifest(path)

    def test_update_policy_locked_on_active_accepts(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    active:\n"
            "      - kind: hook\n"
            "        hosts: [claude-code]\n"
            "        files: [{from: x, to: y}]\n"
            "        update_policy: locked\n",
        )
        parsed = schema.parse_manifest(path)
        self.assertEqual(
            parsed["packs"][0]["active"][0]["update_policy"], "locked"
        )

    def test_unknown_pack_level_update_policy_rejects(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    source:\n"
            "      repo: https://github.com/example/pack\n"
            "      ref: main\n"
            "    update_policy: sporadic\n"
            "    passive: [{files: [{from: a, to: b}]}]\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"unknown 'update_policy'"):
            schema.parse_manifest(path)

    def test_required_must_be_bool(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    active:\n"
            "      - kind: hook\n"
            "        hosts: [claude-code]\n"
            "        files: [{from: x, to: y}]\n"
            "        required: 'yes'\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"'required' must be a boolean"):
            schema.parse_manifest(path)

    def test_active_missing_hosts_rejects(self) -> None:
        """hosts is required on every active entry (pack-architecture.md:483-484);
        satisfied by either entry-level or pack-level 'hosts:'."""
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    active:\n"
            "      - kind: hook\n"
            "        files: [{from: x, to: y}]\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"missing required 'hosts'"):
            schema.parse_manifest(path)

    def test_pack_level_hosts_inherited_by_active(self) -> None:
        """pack-architecture.md:199 — pack-level hosts is a default for active entries."""
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    hosts: [claude-code]\n"
            "    active:\n"
            "      - kind: hook\n"
            "        files: [{from: x, to: y}]\n",
        )
        parsed = schema.parse_manifest(path)
        # Entry itself still has no 'hosts' key — inheritance is applied by
        # dispatch, not by the schema parser. The parser only verifies the
        # effective host set is resolvable.
        self.assertEqual(parsed["packs"][0]["hosts"], ["claude-code"])
        self.assertNotIn("hosts", parsed["packs"][0]["active"][0])

    def test_entry_hosts_overrides_pack_hosts(self) -> None:
        """pack-architecture.md:199 — entry-level value wins on conflict."""
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    hosts: [claude-code]\n"
            "    active:\n"
            "      - kind: hook\n"
            "        hosts: [codex]\n"
            "        files: [{from: x, to: y}]\n",
        )
        parsed = schema.parse_manifest(path)
        self.assertEqual(parsed["packs"][0]["hosts"], ["claude-code"])
        self.assertEqual(parsed["packs"][0]["active"][0]["hosts"], ["codex"])

    def test_pack_level_hosts_must_be_valid_list(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    hosts: claude-code\n"
            "    active:\n"
            "      - kind: hook\n"
            "        files: [{from: x, to: y}]\n",
        )
        with self.assertRaisesRegex(
            schema.ParseError, r"pack-level 'hosts' must be a non-empty list"
        ):
            schema.parse_manifest(path)

    def test_empty_pack_level_hosts_rejects(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    hosts: []\n"
            "    active:\n"
            "      - kind: hook\n"
            "        files: [{from: x, to: y}]\n",
        )
        with self.assertRaisesRegex(
            schema.ParseError, r"pack-level 'hosts' must be a non-empty list"
        ):
            schema.parse_manifest(path)

    def test_active_empty_hosts_rejects(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    active:\n"
            "      - kind: hook\n"
            "        hosts: []\n"
            "        files: [{from: x, to: y}]\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"non-empty list"):
            schema.parse_manifest(path)

    def test_active_missing_files_rejects(self) -> None:
        """pack-architecture.md:483-484: files is required on every active entry."""
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    active:\n"
            "      - kind: hook\n"
            "        hosts: [claude-code]\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"missing required 'files'"):
            schema.parse_manifest(path)

    def test_active_empty_files_rejects(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    active:\n"
            "      - kind: hook\n"
            "        hosts: [claude-code]\n"
            "        files: []\n",
        )
        with self.assertRaisesRegex(
            schema.ParseError, r"at least one \{from, to\}"
        ):
            schema.parse_manifest(path)

    def test_v2_source_missing_ref_rejects(self) -> None:
        """Every source must pin to an explicit ref for immutable resolution."""
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    source:\n"
            "      repo: https://github.com/example/pack\n"
            "    passive: [{files: [{from: a, to: b}]}]\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"'source.ref'"):
            schema.parse_manifest(path)

    def test_hosts_must_be_list_of_strings(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    active:\n"
            "      - kind: hook\n"
            "        hosts: claude-code\n"
            "        files: [{from: x, to: y}]\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"'hosts' must be"):
            schema.parse_manifest(path)

    def test_files_entry_missing_from_rejects(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: some-pack\n"
            "    active:\n"
            "      - kind: hook\n"
            "        hosts: [claude-code]\n"
            "        files:\n"
            "          - {to: ~/.claude/hooks/x.py}\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"missing or empty 'from'"):
            schema.parse_manifest(path)


# ---------- private / credential-bearing sources (v0.5.0 gate) ----------


class PrivateSourceRejectTests(_TmpDirCase):
    def test_ssh_source_rejects_v1(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 1\n"
            "packs:\n"
            "  - name: private\n"
            "    source: git@github.com:me/private.git\n"
            "    default-ref: main\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"v0.5.0\+"):
            schema.parse_manifest(path)

    def test_ssh_scheme_rejects_v2(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: private\n"
            "    source:\n"
            "      repo: ssh://git@github.com/me/private\n"
            "      ref: main\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"v0.5.0\+"):
            schema.parse_manifest(path)

    def test_credential_url_rejects(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 1\n"
            "packs:\n"
            "  - name: credbearer\n"
            "    source: https://user:pass@example.com/{ref}/rule-pack.md\n"
            "    default-ref: main\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"credentials"):
            schema.parse_manifest(path)

    def test_source_auth_field_rejects(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 2\n"
            "packs:\n"
            "  - name: needs-auth\n"
            "    source:\n"
            "      repo: https://github.com/example/private\n"
            "      ref: main\n"
            "      auth: ssh\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"'source.auth' requires"):
            schema.parse_manifest(path)


# ---------- structural errors ----------


class StructuralTests(_TmpDirCase):
    def test_missing_file_raises(self) -> None:
        with self.assertRaisesRegex(schema.ParseError, r"not found"):
            schema.parse_manifest(self.root / "missing.yaml")

    def test_malformed_yaml_raises(self) -> None:
        path = _write_manifest(self.root, "version: 1\npacks: [\n")
        with self.assertRaisesRegex(schema.ParseError, r"malformed YAML"):
            schema.parse_manifest(path)

    def test_non_mapping_top_level_rejects(self) -> None:
        path = _write_manifest(self.root, "- just a list\n- at top level\n")
        with self.assertRaisesRegex(
            schema.ParseError, r"must be a mapping at top level"
        ):
            schema.parse_manifest(path)

    def test_missing_version_rejects(self) -> None:
        path = _write_manifest(self.root, "packs:\n  - name: x\n")
        with self.assertRaisesRegex(schema.ParseError, r"'version' must be 1 or 2"):
            schema.parse_manifest(path)

    def test_invalid_version_rejects(self) -> None:
        path = _write_manifest(self.root, "version: 99\npacks: []\n")
        with self.assertRaisesRegex(schema.ParseError, r"'version' must be 1 or 2"):
            schema.parse_manifest(path)

    def test_packs_must_be_list(self) -> None:
        path = _write_manifest(self.root, "version: 1\npacks: not-a-list\n")
        with self.assertRaisesRegex(schema.ParseError, r"'packs' must be a list"):
            schema.parse_manifest(path)

    def test_pack_entry_must_be_mapping(self) -> None:
        path = _write_manifest(self.root, "version: 1\npacks:\n  - 'just a string'\n")
        with self.assertRaisesRegex(schema.ParseError, r"must be a mapping"):
            schema.parse_manifest(path)

    def test_pack_missing_name_rejects(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 1\npacks:\n"
            "  - source: https://example.com/{ref}/rule-pack.md\n"
            "    default-ref: v1\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"missing or empty 'name'"):
            schema.parse_manifest(path)

    def test_duplicate_pack_names_reject(self) -> None:
        path = _write_manifest(
            self.root,
            "version: 1\npacks:\n"
            "  - name: a\n"
            "    source: https://example.com/{ref}/x.md\n"
            "    default-ref: v1\n"
            "  - name: a\n"
            "    source: https://example.com/{ref}/y.md\n"
            "    default-ref: v2\n",
        )
        with self.assertRaisesRegex(schema.ParseError, r"duplicate pack name"):
            schema.parse_manifest(path)

    def test_empty_packs_list_accepts(self) -> None:
        path = _write_manifest(self.root, "version: 1\npacks: []\n")
        parsed = schema.parse_manifest(path)
        self.assertEqual(parsed["packs"], [])


if __name__ == "__main__":
    unittest.main()
