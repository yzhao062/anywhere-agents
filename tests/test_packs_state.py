"""Tests for scripts/packs/state.py.

Covers load/save + schema validation for all three state files:
project-local pack-lock.json, project-local pack-state.json, and
user-level pack-state.json with owners list.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import state  # noqa: E402


class _TmpDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


# ---------- pack-lock.json ----------


class PackLockTests(_TmpDirCase):
    def test_empty_round_trip(self) -> None:
        path = self.root / "pack-lock.json"
        payload = state.empty_pack_lock()
        state.save_pack_lock(path, payload)
        loaded = state.load_pack_lock(path)
        self.assertEqual(loaded, payload)

    def test_full_pack_entry_round_trip(self) -> None:
        path = self.root / "pack-lock.json"
        payload = {
            "version": state.SCHEMA_VERSION,
            "packs": {
                "agent-style": {
                    "source_url": "https://github.com/yzhao062/agent-style",
                    "requested_ref": "v0.3.2",
                    "resolved_commit": "39cdc67a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e",
                    "pack_update_policy": "locked",
                    "files": [
                        {
                            "role": "passive",
                            "host": None,
                            "source_path": "docs/rule-pack.md",
                            "input_sha256": "f175e39b...",
                            "output_paths": ["AGENTS.md"],
                            "output_scope": "project-local",
                            "effective_update_policy": "locked",
                        }
                    ],
                }
            },
        }
        state.save_pack_lock(path, payload)
        loaded = state.load_pack_lock(path)
        self.assertEqual(loaded, payload)

    def test_generated_command_fields_round_trip(self) -> None:
        path = self.root / "pack-lock.json"
        payload = {
            "version": state.SCHEMA_VERSION,
            "packs": {
                "implement-review": {
                    "source_url": "bundled:aa",
                    "requested_ref": "bundled",
                    "resolved_commit": "bundled",
                    "pack_update_policy": "locked",
                    "files": [
                        {
                            "role": "generated-command",
                            "host": "claude-code",
                            "source_path": None,
                            "input_sha256": None,
                            "output_paths": [".claude/commands/implement-review.md"],
                            "output_scope": "project-local",
                            "effective_update_policy": "locked",
                            "generated_from": "active-skill:implement-review",
                            "source_input_sha256": "dir-sha256:1122aa",
                            "template_sha256": "aa-composer-command-v1:7f8e9d",
                            "output_sha256": "b4c3d2e1",
                        }
                    ],
                }
            },
        }
        state.save_pack_lock(path, payload)
        loaded = state.load_pack_lock(path)
        self.assertEqual(loaded, payload)

    def _minimal_passive_file_entry(self) -> dict:
        """A structurally valid passive file entry for tests that want to
        perturb exactly one field at a time."""
        return {
            "role": "passive",
            "host": None,
            "source_path": "docs/rule-pack.md",
            "input_sha256": "abc123",
            "output_paths": ["AGENTS.md"],
            "output_scope": "project-local",
            "effective_update_policy": "locked",
        }

    def _pack_lock_with_file(self, file_entry: dict) -> dict:
        return {
            "version": state.SCHEMA_VERSION,
            "packs": {
                "p": {
                    "source_url": "s",
                    "requested_ref": "r",
                    "resolved_commit": "c",
                    "pack_update_policy": "locked",
                    "files": [file_entry],
                }
            },
        }

    def test_unknown_role_rejects(self) -> None:
        path = self.root / "pack-lock.json"
        bad = self._minimal_passive_file_entry()
        bad["role"] = "mystery"
        path.write_text(
            json.dumps(self._pack_lock_with_file(bad)), encoding="utf-8"
        )
        with self.assertRaisesRegex(state.StateError, r"unknown 'role'"):
            state.load_pack_lock(path)

    def test_passive_with_non_null_host_rejects(self) -> None:
        path = self.root / "pack-lock.json"
        bad = self._minimal_passive_file_entry()
        bad["host"] = "claude-code"  # passive must have host=null
        path.write_text(
            json.dumps(self._pack_lock_with_file(bad)), encoding="utf-8"
        )
        with self.assertRaisesRegex(state.StateError, r"passive entries must have 'host': null"):
            state.load_pack_lock(path)

    def test_active_hook_missing_host_rejects(self) -> None:
        path = self.root / "pack-lock.json"
        bad = {
            "role": "active-hook",
            "source_path": "scripts/h.py",
            "input_sha256": "abc",
            "output_paths": ["~/.claude/hooks/x/01-h.py"],
            "output_scope": "user-level",
            "effective_update_policy": "locked",
        }
        path.write_text(
            json.dumps(self._pack_lock_with_file(bad)), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            state.StateError, r"missing required 'host'"
        ):
            state.load_pack_lock(path)

    def test_active_hook_empty_source_path_rejects(self) -> None:
        path = self.root / "pack-lock.json"
        bad = {
            "role": "active-hook",
            "host": "claude-code",
            "source_path": "",
            "input_sha256": "abc",
            "output_paths": ["~/.claude/hooks/x/01-h.py"],
            "output_scope": "user-level",
            "effective_update_policy": "locked",
        }
        path.write_text(
            json.dumps(self._pack_lock_with_file(bad)), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            state.StateError, r"'source_path' as a non-empty string"
        ):
            state.load_pack_lock(path)

    def test_generated_command_with_non_null_source_path_rejects(self) -> None:
        path = self.root / "pack-lock.json"
        bad = {
            "role": "generated-command",
            "host": "claude-code",
            "source_path": "not-null",  # must be null
            "input_sha256": None,
            "output_paths": [".claude/commands/x.md"],
            "output_scope": "project-local",
            "effective_update_policy": "locked",
            "generated_from": "active-skill:x",
            "source_input_sha256": "dir-sha256:abc",
            "template_sha256": "tpl-v1:xyz",
            "output_sha256": "out-hash",
        }
        path.write_text(
            json.dumps(self._pack_lock_with_file(bad)), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            state.StateError, r"'source_path': null"
        ):
            state.load_pack_lock(path)

    def test_empty_output_paths_rejects(self) -> None:
        path = self.root / "pack-lock.json"
        bad = self._minimal_passive_file_entry()
        bad["output_paths"] = []
        path.write_text(
            json.dumps(self._pack_lock_with_file(bad)), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            state.StateError, r"non-empty list of non-empty strings"
        ):
            state.load_pack_lock(path)

    def test_generated_command_missing_triple_hash_rejects(self) -> None:
        path = self.root / "pack-lock.json"
        bad = {
            "role": "generated-command",
            "host": "claude-code",
            "source_path": None,
            "input_sha256": None,
            "output_paths": [".claude/commands/x.md"],
            "output_scope": "project-local",
            "effective_update_policy": "locked",
            # Missing generated_from / source_input_sha256 / template_sha256 / output_sha256.
        }
        path.write_text(
            json.dumps(self._pack_lock_with_file(bad)), encoding="utf-8"
        )
        with self.assertRaisesRegex(state.StateError, r"generated-command entry missing"):
            state.load_pack_lock(path)

    def test_non_generated_command_with_gc_field_rejects(self) -> None:
        path = self.root / "pack-lock.json"
        bad = self._minimal_passive_file_entry()
        bad["generated_from"] = "leaked"  # forbidden on non-GC roles
        path.write_text(
            json.dumps(self._pack_lock_with_file(bad)), encoding="utf-8"
        )
        with self.assertRaisesRegex(state.StateError, r"generated-command only"):
            state.load_pack_lock(path)

    def test_bad_version_rejects(self) -> None:
        path = self.root / "pack-lock.json"
        path.write_text(
            json.dumps({"version": 99, "packs": {}}), encoding="utf-8"
        )
        with self.assertRaisesRegex(state.StateError, r"unsupported version"):
            state.load_pack_lock(path)

    def test_malformed_json_rejects(self) -> None:
        path = self.root / "pack-lock.json"
        path.write_text("not json {[", encoding="utf-8")
        with self.assertRaisesRegex(state.StateError, r"cannot read"):
            state.load_pack_lock(path)

    def test_missing_file_rejects(self) -> None:
        with self.assertRaisesRegex(state.StateError, r"not found"):
            state.load_pack_lock(self.root / "missing.json")


# ---------- project-local pack-state.json ----------


class ProjectStateTests(_TmpDirCase):
    def test_missing_file_returns_empty(self) -> None:
        path = self.root / ".agent-config" / "pack-state.json"
        data = state.load_project_state(path)
        self.assertEqual(data, state.empty_project_state())

    def test_round_trip_with_entries(self) -> None:
        path = self.root / ".agent-config" / "pack-state.json"
        payload = {
            "version": state.SCHEMA_VERSION,
            "entries": [
                {
                    "pack": "implement-review",
                    "output_path": ".claude/commands/implement-review.md",
                    "sha256": "abcd1234",
                }
            ],
        }
        state.save_project_state(path, payload)
        loaded = state.load_project_state(path)
        self.assertEqual(loaded, payload)

    def test_missing_required_field_rejects(self) -> None:
        path = self.root / "pack-state.json"
        path.write_text(
            json.dumps(
                {
                    "version": state.SCHEMA_VERSION,
                    "entries": [{"pack": "x", "output_path": "y"}],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(state.StateError, r"missing or non-string 'sha256'"):
            state.load_project_state(path)


# ---------- user-level pack-state.json ----------


class UserStateTests(_TmpDirCase):
    def test_missing_file_returns_empty(self) -> None:
        path = self.root / ".claude" / "pack-state.json"
        data = state.load_user_state(path)
        self.assertEqual(data, state.empty_user_state())

    def test_round_trip_hook_entry(self) -> None:
        path = self.root / ".claude" / "pack-state.json"
        payload = {
            "version": state.SCHEMA_VERSION,
            "entries": [
                {
                    "kind": "active-hook",
                    "target_path": "/home/u/.claude/hooks/agent-behave/01-git-guard.py",
                    "expected_sha256_or_json": "fedcba9876",
                    "owners": [
                        {
                            "repo_id": "repo-A-id",
                            "pack": "agent-behave",
                            "requested_ref": "v0.1.0",
                            "resolved_commit": "ab12cd34",
                            "expected_sha256_or_json": "fedcba9876",
                        },
                        {
                            "repo_id": "repo-B-id",
                            "pack": "agent-behave",
                            "requested_ref": "v0.1.0",
                            "resolved_commit": "ab12cd34",
                            "expected_sha256_or_json": "fedcba9876",
                        },
                    ],
                }
            ],
        }
        state.save_user_state(path, payload)
        loaded = state.load_user_state(path)
        self.assertEqual(loaded, payload)

    def test_round_trip_permission_entry(self) -> None:
        path = self.root / ".claude" / "pack-state.json"
        permission_value = {"pattern": "Bash(git push)", "decision": "ask"}
        payload = {
            "version": state.SCHEMA_VERSION,
            "entries": [
                {
                    "kind": "active-permission",
                    "target_path": "/home/u/.claude/settings.json",
                    "expected_sha256_or_json": permission_value,
                    "owners": [
                        {
                            "repo_id": "repo-A-id",
                            "pack": "agent-behave",
                            "requested_ref": "v0.1.0",
                            "resolved_commit": "ab12cd34",
                            "expected_sha256_or_json": permission_value,
                        }
                    ],
                }
            ],
        }
        state.save_user_state(path, payload)
        loaded = state.load_user_state(path)
        self.assertEqual(loaded, payload)

    def test_empty_owners_tolerated_on_load(self) -> None:
        """Load tolerates empty owners so cleanup code can read the file;
        save remains strict so writer bugs can't persist the zombie."""
        path = self.root / "pack-state.json"
        path.write_text(
            json.dumps(
                {
                    "version": state.SCHEMA_VERSION,
                    "entries": [
                        {
                            "kind": "active-hook",
                            "target_path": "/home/u/.claude/hooks/x.py",
                            "expected_sha256_or_json": "abc",
                            "owners": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        # Load succeeds (for cleanup).
        loaded = state.load_user_state(path)
        self.assertEqual(loaded["entries"][0]["owners"], [])

    def test_empty_owners_rejected_on_save(self) -> None:
        """Save rejects empty owners to prevent persisting a zombie entry."""
        path = self.root / "pack-state.json"
        payload = {
            "version": state.SCHEMA_VERSION,
            "entries": [
                {
                    "kind": "active-hook",
                    "target_path": "/home/u/.claude/hooks/x.py",
                    "expected_sha256_or_json": "abc",
                    "owners": [],
                }
            ],
        }
        with self.assertRaisesRegex(state.StateError, r"non-empty list"):
            state.save_user_state(path, payload)

    def test_unknown_kind_rejects(self) -> None:
        path = self.root / "pack-state.json"
        path.write_text(
            json.dumps(
                {
                    "version": state.SCHEMA_VERSION,
                    "entries": [
                        {
                            "kind": "active-skill",  # skill is project-local
                            "target_path": "/x",
                            "expected_sha256_or_json": "a",
                            "owners": [
                                {
                                    "repo_id": "r",
                                    "pack": "p",
                                    "requested_ref": "v",
                                    "resolved_commit": "c",
                                    "expected_sha256_or_json": "a",
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(state.StateError, r"unknown 'kind'"):
            state.load_user_state(path)

    def test_owner_missing_field_rejects(self) -> None:
        path = self.root / "pack-state.json"
        path.write_text(
            json.dumps(
                {
                    "version": state.SCHEMA_VERSION,
                    "entries": [
                        {
                            "kind": "active-hook",
                            "target_path": "/x",
                            "expected_sha256_or_json": "a",
                            "owners": [
                                {
                                    "repo_id": "r",
                                    "pack": "p",
                                    # missing requested_ref + resolved_commit
                                    "expected_sha256_or_json": "a",
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            state.StateError, r"missing or non-string 'requested_ref'"
        ):
            state.load_user_state(path)


# ---------- atomic write ----------


class AtomicWriteTests(_TmpDirCase):
    def test_save_is_byte_stable_under_reserialize(self) -> None:
        """Identical payloads saved twice produce byte-identical files."""
        path = self.root / "pack-lock.json"
        payload = state.empty_pack_lock()
        state.save_pack_lock(path, payload)
        first = path.read_bytes()
        state.save_pack_lock(path, payload)
        second = path.read_bytes()
        self.assertEqual(first, second)

    def test_save_rejects_wrong_version(self) -> None:
        path = self.root / "pack-lock.json"
        with self.assertRaisesRegex(state.StateError, r"refusing to write"):
            state.save_pack_lock(
                path, {"version": 42, "packs": {}}
            )


if __name__ == "__main__":
    unittest.main()
