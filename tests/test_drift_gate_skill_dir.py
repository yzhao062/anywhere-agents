"""v0.5.8 regression tests: drift-gate skill-dir fix.

Covers:

1. Pack-lock with directory ``output_paths`` → all on-disk files under that
   directory walk into ``prior_pack_outputs``.
2. Adopt-on-match with sha matching the historical ring entry → classified
   as ``PRESTATE_PACK_OUTPUT``, not ``PRESTATE_UNMANAGED``.
3. Newer-than-current sha (not in current and not in ring) → still classified
   as ``PRESTATE_UNMANAGED`` (Round 1 decision: do not adopt unknown shas).
4. Historical ring is FIFO-capped at 5 entries (push 6, oldest evicted).
5. Old lock without ``historical_input_sha256`` field loads cleanly.
6. End-to-end: simulate the usc-admin reproduction — pack with skill dir,
   prior commit's content on disk, upstream advances → composer succeeds
   without ``DriftAbort``.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import state  # noqa: E402
from packs import transaction as txn_mod  # noqa: E402
import compose_packs  # noqa: E402


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, content: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    return path


def _write_text(path: Path, text: str) -> Path:
    return _write(path, text.encode("utf-8"))


def _minimal_skill_file_entry(output_dir: str, input_sha: str, *, host: str = "claude-code") -> dict:
    return {
        "role": "active-skill",
        "host": host,
        "source_path": "skills/implement-review",
        "input_sha256": input_sha,
        "output_paths": [output_dir],
        "output_scope": "project-local",
        "effective_update_policy": "locked",
    }


def _pack_lock_with_file(file_entry: dict, pack_name: str = "test-pack") -> dict:
    return {
        "version": state.SCHEMA_VERSION,
        "packs": {
            pack_name: {
                "source_url": "https://example.com/test-pack",
                "requested_ref": "v1.0.0",
                "resolved_commit": "abc" * 14,
                "pack_update_policy": "locked",
                "files": [file_entry],
            }
        },
    }


# =====================================================================
# Test 1: Schema extension — historical_input_sha256 round-trips cleanly.
# =====================================================================


class HistoricalRingSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_historical_field_absent_loads_without_error(self) -> None:
        """Old locks without historical_input_sha256 load cleanly (backward compat)."""
        path = self.root / "pack-lock.json"
        entry = _minimal_skill_file_entry(".claude/skills/ir/", "dir-sha256:old")
        payload = _pack_lock_with_file(entry)
        state.save_pack_lock(path, payload)
        loaded = state.load_pack_lock(path)
        # field should be absent (not defaulted to empty list)
        file0 = loaded["packs"]["test-pack"]["files"][0]
        self.assertNotIn("historical_input_sha256", file0)

    def test_historical_field_present_round_trips(self) -> None:
        """historical_input_sha256 round-trips when written."""
        path = self.root / "pack-lock.json"
        entry = _minimal_skill_file_entry(".claude/skills/ir/", "dir-sha256:new")
        entry["historical_input_sha256"] = ["dir-sha256:v1", "dir-sha256:v2"]
        payload = _pack_lock_with_file(entry)
        state.save_pack_lock(path, payload)
        loaded = state.load_pack_lock(path)
        ring = loaded["packs"]["test-pack"]["files"][0]["historical_input_sha256"]
        self.assertEqual(ring, ["dir-sha256:v1", "dir-sha256:v2"])

    def test_historical_field_empty_list_accepted(self) -> None:
        path = self.root / "pack-lock.json"
        entry = _minimal_skill_file_entry(".claude/skills/ir/", "dir-sha256:cur")
        entry["historical_input_sha256"] = []
        payload = _pack_lock_with_file(entry)
        state.save_pack_lock(path, payload)
        loaded = state.load_pack_lock(path)
        ring = loaded["packs"]["test-pack"]["files"][0]["historical_input_sha256"]
        self.assertEqual(ring, [])

    def test_historical_field_non_list_rejects(self) -> None:
        path = self.root / "pack-lock.json"
        entry = _minimal_skill_file_entry(".claude/skills/ir/", "dir-sha256:cur")
        entry["historical_input_sha256"] = "not-a-list"
        payload = _pack_lock_with_file(entry)
        # Write raw JSON to bypass save-side validation, then test load validation
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps({"version": state.SCHEMA_VERSION, "packs": {"test-pack": {
            "source_url": "https://x",
            "requested_ref": "r",
            "resolved_commit": "c" * 40,
            "pack_update_policy": "locked",
            "files": [entry],
        }}})
        path.write_text(raw, encoding="utf-8")
        with self.assertRaisesRegex(state.StateError, r"historical_input_sha256.*list"):
            state.load_pack_lock(path)

    def test_historical_field_non_string_element_rejects(self) -> None:
        path = self.root / "pack-lock.json"
        entry = _minimal_skill_file_entry(".claude/skills/ir/", "dir-sha256:cur")
        entry["historical_input_sha256"] = [123, "ok"]  # 123 is not a string
        raw = json.dumps({"version": state.SCHEMA_VERSION, "packs": {"test-pack": {
            "source_url": "https://x",
            "requested_ref": "r",
            "resolved_commit": "c" * 40,
            "pack_update_policy": "locked",
            "files": [entry],
        }}})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        with self.assertRaisesRegex(state.StateError, r"historical_input_sha256.*strings"):
            state.load_pack_lock(path)


# =====================================================================
# Test 2: Ring FIFO cap at 5 entries.
# =====================================================================


class HistoricalRingCapTests(unittest.TestCase):
    """Tests for _update_historical_ring in compose_packs (or wherever implemented)."""

    def test_ring_cap_at_5_evicts_oldest(self) -> None:
        """Pushing 6 elements into a ring capped at 5 evicts the oldest."""
        ring: list[str] = []
        for i in range(6):
            ring = compose_packs._push_historical_sha(ring, f"sha_{i}")
        self.assertEqual(len(ring), 5)
        self.assertNotIn("sha_0", ring)
        self.assertEqual(ring[0], "sha_1")
        self.assertEqual(ring[-1], "sha_5")

    def test_ring_under_cap_no_eviction(self) -> None:
        ring: list[str] = []
        for i in range(4):
            ring = compose_packs._push_historical_sha(ring, f"sha_{i}")
        self.assertEqual(len(ring), 4)
        self.assertIn("sha_0", ring)
        self.assertIn("sha_3", ring)

    def test_ring_exactly_at_cap(self) -> None:
        ring: list[str] = []
        for i in range(5):
            ring = compose_packs._push_historical_sha(ring, f"sha_{i}")
        self.assertEqual(len(ring), 5)

    def test_ring_push_empty_string_ignored(self) -> None:
        """Empty strings (e.g., None-coerced values) are not pushed."""
        ring: list[str] = []
        ring = compose_packs._push_historical_sha(ring, "")
        ring = compose_packs._push_historical_sha(ring, None)  # type: ignore[arg-type]
        self.assertEqual(ring, [])

    def test_ring_push_returns_new_list(self) -> None:
        """_push_historical_sha does not mutate the input list."""
        original: list[str] = ["old"]
        new = compose_packs._push_historical_sha(original, "new")
        self.assertIsNot(original, new)
        self.assertEqual(original, ["old"])


# =====================================================================
# Test 3: prior_pack_outputs construction from pack-lock directory entries.
# =====================================================================


class PriorPackOutputsDirWalkTests(unittest.TestCase):
    """Verify that _build_prior_pack_outputs (or inline expansion) enumerates
    per-file paths when the pack-lock records a directory output_paths."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def _make_skill_dir(self, dir_rel: str, files: dict[str, bytes]) -> dict[str, str]:
        """Write files into a skill directory; return {rel_path: sha256} map."""
        shas: dict[str, str] = {}
        for rel, content in files.items():
            path = self.root / dir_rel / rel
            _write(path, content)
            shas[str((self.root / dir_rel / rel).resolve())] = _sha256(content)
        return shas

    def test_dir_output_paths_enumerate_files_in_prior_pack_outputs(self) -> None:
        """Files inside a recorded skill dir appear in prior_pack_outputs
        with their on-disk sha256, when the lock records the matching dir-sha."""
        skill_dir = ".claude/skills/implement-review"
        file_shas = self._make_skill_dir(skill_dir, {
            "SKILL.md": b"# Old SKILL.md content\n",
            "references/ref.md": b"reference\n",
        })

        # Compute the real dir-sha so the gate matches.
        real_merkle = compose_packs._dir_sha256(self.root / skill_dir)
        lock_entry = _minimal_skill_file_entry(skill_dir + "/", real_merkle)
        previous_lock = _pack_lock_with_file(lock_entry)

        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=previous_lock,
        )

        for abs_path, sha in file_shas.items():
            self.assertIn(abs_path, prior_outputs, f"expected {abs_path} in prior_pack_outputs")
            self.assertEqual(prior_outputs[abs_path], sha)

    def test_dir_without_trailing_slash_still_walks(self) -> None:
        """Directory output_paths without trailing slash is detected by on-disk type
        and files are included when the lock records the matching dir-sha."""
        skill_dir = ".claude/skills/my-skill"
        file_shas = self._make_skill_dir(skill_dir, {"SKILL.md": b"content\n"})

        # output_paths WITHOUT trailing slash; real dir-sha so gate matches.
        real_merkle = compose_packs._dir_sha256(self.root / skill_dir)
        lock_entry = _minimal_skill_file_entry(skill_dir, real_merkle)
        previous_lock = _pack_lock_with_file(lock_entry)

        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=previous_lock,
        )

        for abs_path, sha in file_shas.items():
            self.assertIn(abs_path, prior_outputs)
            self.assertEqual(prior_outputs[abs_path], sha)

    def test_dir_not_on_disk_does_not_error(self) -> None:
        """A directory in pack-lock that doesn't exist on disk is silently skipped."""
        lock_entry = _minimal_skill_file_entry(".claude/skills/missing/", "dir-sha256:x")
        previous_lock = _pack_lock_with_file(lock_entry)

        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=previous_lock,
        )
        # Should not raise; result may be empty or partial
        self.assertIsInstance(prior_outputs, dict)

    def test_single_file_output_in_prior_pack_outputs(self) -> None:
        """Single-file output_paths still appear with their on-disk sha."""
        file_content = b"some command file\n"
        file_path = self.root / ".claude" / "commands" / "test.md"
        _write(file_path, file_content)

        lock_entry = {
            "role": "active-skill",
            "host": "claude-code",
            "source_path": "some/source.md",
            "input_sha256": _sha256(file_content),
            "output_paths": [".claude/commands/test.md"],
            "output_scope": "project-local",
            "effective_update_policy": "locked",
        }
        previous_lock = _pack_lock_with_file(lock_entry)

        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=previous_lock,
        )

        abs_path = str(file_path.resolve())
        self.assertIn(abs_path, prior_outputs)
        self.assertEqual(prior_outputs[abs_path], _sha256(file_content))

    def test_historical_ring_match_adds_file_to_prior_pack_outputs(self) -> None:
        """A single-file whose on-disk sha matches the historical ring (not
        the current input_sha256) is included in prior_pack_outputs so the
        drift gate classifies it as PRESTATE_PACK_OUTPUT."""
        old_sha = "aabbcc" + "0" * 58
        new_sha = "ddeeff" + "0" * 58
        file_content = bytes.fromhex("aa" * 32)  # produces on-disk sha = sha256(file_content)
        on_disk_sha = _sha256(file_content)

        file_path = self.root / ".claude" / "commands" / "ir.md"
        _write(file_path, file_content)

        lock_entry = {
            "role": "active-skill",
            "host": "claude-code",
            "source_path": "ir.md",
            "input_sha256": new_sha,
            "historical_input_sha256": [old_sha, on_disk_sha],
            "output_paths": [".claude/commands/ir.md"],
            "output_scope": "project-local",
            "effective_update_policy": "locked",
        }
        previous_lock = _pack_lock_with_file(lock_entry)

        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=previous_lock,
        )

        abs_path = str(file_path.resolve())
        self.assertIn(abs_path, prior_outputs)

    def test_newer_than_current_sha_not_in_prior_pack_outputs(self) -> None:
        """A single-file whose on-disk sha does NOT match input_sha256 or the
        ring (unknown sha = user edit or future-version file) is NOT added to
        prior_pack_outputs (falls to PRESTATE_UNMANAGED per Round 1 decision)."""
        known_sha = "aabbcc" + "0" * 58
        file_content = b"user edited content that aa never wrote\n"
        on_disk_sha = _sha256(file_content)
        self.assertNotEqual(on_disk_sha, known_sha)

        file_path = self.root / ".claude" / "commands" / "user-file.md"
        _write(file_path, file_content)

        lock_entry = {
            "role": "active-skill",
            "host": "claude-code",
            "source_path": "x.md",
            "input_sha256": known_sha,
            "historical_input_sha256": [known_sha],
            "output_paths": [".claude/commands/user-file.md"],
            "output_scope": "project-local",
            "effective_update_policy": "locked",
        }
        previous_lock = _pack_lock_with_file(lock_entry)

        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=previous_lock,
        )

        abs_path = str(file_path.resolve())
        self.assertNotIn(abs_path, prior_outputs)

    def test_empty_pack_lock_returns_empty_dict(self) -> None:
        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=state.empty_pack_lock(),
        )
        self.assertEqual(prior_outputs, {})


# =====================================================================
# Test 4: historical ring update in pack-lock on successful compose.
# =====================================================================


class HistoricalRingUpdateTests(unittest.TestCase):
    """Test that _update_pack_lock_historical_rings correctly moves old
    input_sha256 values into the historical ring when pack-lock is finalized."""

    def test_first_install_no_ring_update(self) -> None:
        """No previous lock entry → new entry has empty or no ring."""
        new_lock = {
            "version": state.SCHEMA_VERSION,
            "packs": {
                "p": {
                    "source_url": "s",
                    "requested_ref": "r",
                    "resolved_commit": "c",
                    "pack_update_policy": "locked",
                    "files": [{
                        "role": "active-skill",
                        "host": "claude-code",
                        "source_path": "s",
                        "input_sha256": "sha_A",
                        "output_paths": [".claude/skills/p/"],
                        "output_scope": "project-local",
                        "effective_update_policy": "locked",
                    }],
                }
            },
        }
        prev_lock = state.empty_pack_lock()
        compose_packs._update_pack_lock_historical_rings(new_lock, prev_lock)
        ring = new_lock["packs"]["p"]["files"][0].get("historical_input_sha256", [])
        self.assertEqual(ring, [])

    def test_same_sha_no_ring_growth(self) -> None:
        """When new input_sha256 == previous input_sha256, don't push the sha
        into the ring again (no-op update)."""
        shared_sha = "sha_X"
        prev_lock = {
            "version": state.SCHEMA_VERSION,
            "packs": {
                "p": {
                    "source_url": "s",
                    "requested_ref": "r",
                    "resolved_commit": "c",
                    "pack_update_policy": "locked",
                    "files": [{
                        "role": "active-skill",
                        "host": "claude-code",
                        "source_path": "s",
                        "input_sha256": shared_sha,
                        "output_paths": [".claude/skills/p/"],
                        "output_scope": "project-local",
                        "effective_update_policy": "locked",
                    }],
                }
            },
        }
        new_lock = {
            "version": state.SCHEMA_VERSION,
            "packs": {
                "p": {
                    "source_url": "s",
                    "requested_ref": "r",
                    "resolved_commit": "c2",
                    "pack_update_policy": "locked",
                    "files": [{
                        "role": "active-skill",
                        "host": "claude-code",
                        "source_path": "s",
                        "input_sha256": shared_sha,
                        "output_paths": [".claude/skills/p/"],
                        "output_scope": "project-local",
                        "effective_update_policy": "locked",
                    }],
                }
            },
        }
        compose_packs._update_pack_lock_historical_rings(new_lock, prev_lock)
        ring = new_lock["packs"]["p"]["files"][0].get("historical_input_sha256", [])
        self.assertEqual(ring, [])

    def test_changed_sha_pushes_old_into_ring(self) -> None:
        """When input_sha256 changes, the previous value is pushed into the ring."""
        old_sha = "sha_old"
        new_sha = "sha_new"
        prev_lock = {
            "version": state.SCHEMA_VERSION,
            "packs": {
                "p": {
                    "source_url": "s",
                    "requested_ref": "r",
                    "resolved_commit": "c1",
                    "pack_update_policy": "locked",
                    "files": [{
                        "role": "active-skill",
                        "host": "claude-code",
                        "source_path": "s",
                        "input_sha256": old_sha,
                        "output_paths": [".claude/skills/p/"],
                        "output_scope": "project-local",
                        "effective_update_policy": "locked",
                    }],
                }
            },
        }
        new_lock = {
            "version": state.SCHEMA_VERSION,
            "packs": {
                "p": {
                    "source_url": "s",
                    "requested_ref": "r",
                    "resolved_commit": "c2",
                    "pack_update_policy": "locked",
                    "files": [{
                        "role": "active-skill",
                        "host": "claude-code",
                        "source_path": "s",
                        "input_sha256": new_sha,
                        "output_paths": [".claude/skills/p/"],
                        "output_scope": "project-local",
                        "effective_update_policy": "locked",
                    }],
                }
            },
        }
        compose_packs._update_pack_lock_historical_rings(new_lock, prev_lock)
        ring = new_lock["packs"]["p"]["files"][0].get("historical_input_sha256", [])
        self.assertIn(old_sha, ring)

    def test_ring_grows_across_multiple_updates(self) -> None:
        """Simulate 6 successive compose runs → ring stays capped at 5."""
        shas = [f"sha_{i}" for i in range(7)]
        prev_ring: list[str] = []
        prev_sha = shas[0]

        for i in range(1, 7):
            new_sha = shas[i]
            prev_lock = {
                "version": state.SCHEMA_VERSION,
                "packs": {
                    "p": {
                        "source_url": "s",
                        "requested_ref": "r",
                        "resolved_commit": "c",
                        "pack_update_policy": "locked",
                        "files": [{
                            "role": "active-skill",
                            "host": "claude-code",
                            "source_path": "s",
                            "input_sha256": prev_sha,
                            "historical_input_sha256": prev_ring,
                            "output_paths": [".claude/skills/p/"],
                            "output_scope": "project-local",
                            "effective_update_policy": "locked",
                        }],
                    }
                },
            }
            new_lock = {
                "version": state.SCHEMA_VERSION,
                "packs": {
                    "p": {
                        "source_url": "s",
                        "requested_ref": "r",
                        "resolved_commit": "c2",
                        "pack_update_policy": "locked",
                        "files": [{
                            "role": "active-skill",
                            "host": "claude-code",
                            "source_path": "s",
                            "input_sha256": new_sha,
                            "output_paths": [".claude/skills/p/"],
                            "output_scope": "project-local",
                            "effective_update_policy": "locked",
                        }],
                    }
                },
            }
            compose_packs._update_pack_lock_historical_rings(new_lock, prev_lock)
            prev_ring = new_lock["packs"]["p"]["files"][0].get("historical_input_sha256", [])
            prev_sha = new_sha

        self.assertLessEqual(len(prev_ring), 5)
        # sha_0 should have been evicted
        self.assertNotIn("sha_0", prev_ring)


# =====================================================================
# Test 5: End-to-end simulation of the usc-admin reproduction.
#
# A pack with a skill directory is installed. Upstream advances
# (new pack commit). Composer runs again — should succeed without DriftAbort.
# =====================================================================


def _make_v2_manifest(root: Path, pack_name: str = "test-skill-pack") -> None:
    bootstrap_dir = root / ".agent-config" / "repo" / "bootstrap"
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    (root / ".agent-config" / "AGENTS.md").parent.mkdir(parents=True, exist_ok=True)
    (root / ".agent-config" / "AGENTS.md").write_text("# upstream AGENTS.md\n", encoding="utf-8")
    (bootstrap_dir / "packs.yaml").write_text(
        f"version: 2\npacks:\n  - name: {pack_name}\n    source: bundled\n",
        encoding="utf-8",
    )


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = compose_packs.main(argv)
    return rc, out_buf.getvalue(), err_buf.getvalue()


class EndToEndSkillDirReproTest(unittest.TestCase):
    """Simulate the usc-admin scenario end-to-end using mocks.

    The test does not actually run the full composer (too many deps),
    but exercises the drift-gate classification logic directly by:
    1. Creating a "previous pack-lock" with a skill dir entry
    2. Writing "old version" files to that dir
    3. Calling _build_prior_pack_outputs
    4. Simulating what the drift gate would do with the result
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_skill_dir_files_classified_as_pack_output_not_unmanaged(self) -> None:
        """Core gate behavior: files inside a skill dir should be PRESTATE_PACK_OUTPUT
        when the dir is recorded in pack-lock with matching dir-sha, not PRESTATE_UNMANAGED."""
        skill_dir_rel = ".claude/skills/implement-review"
        skill_dir_abs = self.root / skill_dir_rel

        # Write "old version" content (simulates what previous aa install wrote)
        old_content = b"# implement-review skill (old version)\n"
        _write(skill_dir_abs / "SKILL.md", old_content)

        # Compute the real dir-sha so the gate matches (v0.5.8 fix).
        real_merkle = compose_packs._dir_sha256(skill_dir_abs)
        lock_entry = _minimal_skill_file_entry(skill_dir_rel + "/", real_merkle)
        previous_lock = _pack_lock_with_file(lock_entry)

        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=previous_lock,
        )

        skill_md_abs = str((skill_dir_abs / "SKILL.md").resolve())
        self.assertIn(skill_md_abs, prior_outputs, "SKILL.md must be in prior_pack_outputs")

        # Simulate the gate classification: file is in prior_pack_outputs
        # → PRESTATE_PACK_OUTPUT, NOT PRESTATE_UNMANAGED.
        self.assertEqual(prior_outputs[skill_md_abs], _sha256(old_content))

    def test_upstream_skill_update_does_not_cause_drift_abort_for_dir_files(self) -> None:
        """Simulates: aa advances skill dir content. User runs compose.
        Gate must not abort for the skill dir files.

        This test exercises the transaction drift-gate directly, setting up
        the expected_prestate as the composer would after the fix."""
        import uuid

        skill_dir_rel = ".claude/skills/implement-review"
        skill_dir_abs = self.root / skill_dir_rel

        # Old content on disk (previous aa install)
        old_content = b"# Old SKILL.md\n"
        _write(skill_dir_abs / "SKILL.md", old_content)
        old_sha = _sha256(old_content)  # noqa: F841

        # Build prior_pack_outputs as the fixed composer would: use real dir-sha.
        real_merkle = compose_packs._dir_sha256(skill_dir_abs)
        lock_entry = _minimal_skill_file_entry(skill_dir_rel + "/", real_merkle)
        previous_lock = _pack_lock_with_file(lock_entry)

        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=previous_lock,
        )

        # New content being written (upstream advance)
        new_content = b"# New SKILL.md (upstream advance)\n"

        # Set up a transaction + expected_prestate as the fixed composer would
        staging_dir = self.root / f"test-staging-{uuid.uuid4().hex[:8]}"
        lock_path = self.root / ".test-lock"

        with txn_mod.Transaction(staging_dir, lock_path) as txn:
            skill_md_path = skill_dir_abs / "SKILL.md"
            txn.stage_write(skill_md_path, new_content)

            # Build expected_prestate using prior_outputs (as compose_packs.py does)
            expected_prestate: dict[str, tuple[str, str | None]] = {}
            for op in txn.ops:
                target_str = op["target_path"]
                try:
                    resolved_target = str(Path(target_str).resolve())
                except OSError:
                    resolved_target = target_str
                if resolved_target in prior_outputs:
                    expected_prestate[target_str] = (
                        txn_mod.PRESTATE_PACK_OUTPUT,
                        prior_outputs[resolved_target],
                    )
                else:
                    expected_prestate[target_str] = (
                        txn_mod.PRESTATE_UNMANAGED,
                        None,
                    )

            txn.set_expected_prestate(expected_prestate)
            # If commit raises DriftAbort, the test fails

        # If we get here without DriftAbort, the test passes
        # Verify new content was written
        self.assertEqual(skill_md_path.read_bytes(), new_content)

    def test_user_edited_skill_file_still_triggers_drift_detection(self) -> None:
        """A file edited by the user (sha not in prior_pack_outputs for its dir
        entry) is detected as unmanaged, and DOES trigger abort.

        This test verifies we haven't accidentally disabled drift detection.
        NOTE: For dir entries, the disk-walk approach means ALL on-disk files
        in the dir are classified as PRESTATE_PACK_OUTPUT (conservative).
        This sub-test covers the single-file case where the file is NOT in a
        recorded dir and NOT in pack-lock at all.
        """
        import uuid

        # Create a file that is NOT in any pack-lock record
        user_file = self.root / "some" / "user-file.md"
        _write(user_file, b"user content\n")

        # Empty pack-lock (no records)
        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=state.empty_pack_lock(),
        )

        # The file should NOT be in prior_outputs (it's truly unmanaged)
        abs_path = str(user_file.resolve())
        self.assertNotIn(abs_path, prior_outputs)

        # Attempting to stage a write to this path and commit will DriftAbort
        staging_dir = self.root / f"test-staging-{uuid.uuid4().hex[:8]}"
        lock_path = self.root / ".test-lock2"

        with self.assertRaises(txn_mod.DriftAbort):
            with txn_mod.Transaction(staging_dir, lock_path) as txn:
                txn.stage_write(user_file, b"overwrite\n")
                expected: dict[str, tuple[str, str | None]] = {
                    str(user_file): (txn_mod.PRESTATE_UNMANAGED, None),
                }
                txn.set_expected_prestate(expected)
                # commit() will abort because file exists and sha differs


# =====================================================================
# Test 7: Dir-sha gating regression — v0.5.8 Codex Round 1 High finding.
# =====================================================================


class DirShaGatingRegressionTests(unittest.TestCase):
    """Regression tests for the dir-sha gating fix in _build_prior_pack_outputs.

    Verifies that the drift gate correctly rejects user-edited skill directories
    (files inside are treated as UNMANAGED when the dir-sha does not match) and
    correctly accepts directories whose on-disk dir-sha matches the lock record.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_user_edited_skill_dir_triggers_drift_abort(self) -> None:
        """Regression: lock records known dir-sha + historical ring; on-disk
        skill dir has a DIFFERENT Merkle hash (user edited a file inside).
        A staged write to an existing file under that dir must raise DriftAbort.

        This is the primary guard against the pre-fix bug where any on-disk
        dir content was unconditionally adopted as PRESTATE_PACK_OUTPUT.
        """
        import uuid

        skill_dir_rel = ".claude/skills/my-pack"
        skill_dir_abs = self.root / skill_dir_rel

        # Write original aa-managed content.
        original_content = b"# Original SKILL.md (aa-managed)\n"
        _write(skill_dir_abs / "SKILL.md", original_content)

        # Lock records the dir-sha of the original content.
        original_dir_sha = compose_packs._dir_sha256(skill_dir_abs)

        # Simulate user edits a file inside the skill dir.
        _write(skill_dir_abs / "SKILL.md", b"# User-edited content!\n")
        # Add a file the user added (not in original).
        _write(skill_dir_abs / "notes.md", b"My private notes.\n")

        # On-disk dir-sha now differs from the recorded one.
        on_disk_dir_sha = compose_packs._dir_sha256(skill_dir_abs)
        self.assertNotEqual(
            on_disk_dir_sha, original_dir_sha,
            "Pre-condition: user edits must change the dir-sha."
        )

        # Lock entry records the original dir-sha plus a historical ring entry.
        lock_entry = _minimal_skill_file_entry(skill_dir_rel + "/", original_dir_sha)
        lock_entry["historical_input_sha256"] = ["dir-sha256:even-older-version"]
        previous_lock = _pack_lock_with_file(lock_entry)

        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=previous_lock,
        )

        # After the fix: the user-edited dir is NOT adopted — the files inside
        # must NOT appear in prior_pack_outputs (they are UNMANAGED now).
        skill_md_abs = str((skill_dir_abs / "SKILL.md").resolve())
        notes_abs = str((skill_dir_abs / "notes.md").resolve())
        self.assertNotIn(
            skill_md_abs, prior_outputs,
            "User-edited SKILL.md must NOT be in prior_pack_outputs after dir-sha mismatch."
        )
        self.assertNotIn(
            notes_abs, prior_outputs,
            "User-added notes.md must NOT be in prior_pack_outputs after dir-sha mismatch."
        )

        # Confirm: attempting to write to the edited file via the composer's
        # path (PRESTATE_UNMANAGED because file NOT in prior_pack_outputs)
        # raises DriftAbort.
        staging_dir = self.root / f"staging-{uuid.uuid4().hex[:8]}"
        lock_path = self.root / ".test-lock-dir-gate"

        with self.assertRaises(
            txn_mod.DriftAbort,
            msg="DriftAbort must fire when overwriting a user-edited file in a dir whose sha mismatches.",
        ):
            with txn_mod.Transaction(staging_dir, lock_path) as txn:
                txn.stage_write(skill_dir_abs / "SKILL.md", b"# New upstream content\n")
                # Build expected_prestate as the composer would: file not in
                # prior_pack_outputs → PRESTATE_UNMANAGED.
                expected: dict[str, tuple[str, str | None]] = {}
                for op in txn.ops:
                    target_str = op["target_path"]
                    try:
                        resolved = str(Path(target_str).resolve())
                    except OSError:
                        resolved = target_str
                    if resolved in prior_outputs:
                        expected[target_str] = (
                            txn_mod.PRESTATE_PACK_OUTPUT,
                            prior_outputs[resolved],
                        )
                    else:
                        expected[target_str] = (txn_mod.PRESTATE_UNMANAGED, None)
                txn.set_expected_prestate(expected)

    def test_matching_dir_sha_allows_pack_output_write(self) -> None:
        """Positive case: lock records the real on-disk dir-sha; a staged
        write to an existing file under that directory succeeds (no DriftAbort).

        This covers the normal upstream-advance path: aa computes the real
        dir-sha at install time and records it, so the next compose recognises
        the directory as managed and overwrites its files without raising.
        """
        import uuid

        skill_dir_rel = ".claude/skills/my-pack-clean"
        skill_dir_abs = self.root / skill_dir_rel

        # Write aa-managed content (previous install).
        managed_content = b"# Managed SKILL.md (aa-written)\n"
        _write(skill_dir_abs / "SKILL.md", managed_content)

        # Lock records the REAL dir-sha — as aa would after writing it.
        real_dir_sha = compose_packs._dir_sha256(skill_dir_abs)
        lock_entry = _minimal_skill_file_entry(skill_dir_rel + "/", real_dir_sha)
        lock_entry["historical_input_sha256"] = ["dir-sha256:older-version"]
        previous_lock = _pack_lock_with_file(lock_entry)

        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=previous_lock,
        )

        skill_md_abs = str((skill_dir_abs / "SKILL.md").resolve())
        self.assertIn(
            skill_md_abs, prior_outputs,
            "SKILL.md must be in prior_pack_outputs when dir-sha matches lock."
        )
        self.assertEqual(prior_outputs[skill_md_abs], _sha256(managed_content))

        # Confirm: writing new upstream content succeeds (PRESTATE_PACK_OUTPUT).
        staging_dir = self.root / f"staging-{uuid.uuid4().hex[:8]}"
        lock_path = self.root / ".test-lock-dir-gate-pos"

        new_content = b"# New upstream SKILL.md content\n"
        # Should NOT raise DriftAbort.
        with txn_mod.Transaction(staging_dir, lock_path) as txn:
            txn.stage_write(skill_dir_abs / "SKILL.md", new_content)
            expected: dict[str, tuple[str, str | None]] = {}
            for op in txn.ops:
                target_str = op["target_path"]
                try:
                    resolved = str(Path(target_str).resolve())
                except OSError:
                    resolved = target_str
                if resolved in prior_outputs:
                    expected[target_str] = (
                        txn_mod.PRESTATE_PACK_OUTPUT,
                        prior_outputs[resolved],
                    )
                else:
                    expected[target_str] = (txn_mod.PRESTATE_UNMANAGED, None)
            txn.set_expected_prestate(expected)

        # Verify the write landed.
        self.assertEqual((skill_dir_abs / "SKILL.md").read_bytes(), new_content)

    def test_dir_sha_in_historical_ring_allows_walk(self) -> None:
        """A dir whose on-disk sha matches a HISTORICAL ring entry (not the
        current input_sha256) is still walked and its files adopted.

        True historical-ring-only positive path:
          - On disk: sha_v1 content (older version that was last deployed)
          - Lock input_sha256: sha_v2 (a newer upstream version not yet on disk)
          - Lock historical_input_sha256: [sha_v1]

        The on-disk dir-sha matches sha_v1 from the historical ring, not
        input_sha256 (sha_v2). _build_prior_pack_outputs must still walk the
        directory and record its files as PRESTATE_PACK_OUTPUT.
        """
        skill_dir_rel = ".claude/skills/historical-test"
        skill_dir_abs = self.root / skill_dir_rel

        # Write version-1 content to disk (the older, currently deployed version).
        v1_content = b"# Version 1 content\n"
        _write(skill_dir_abs / "SKILL.md", v1_content)

        # sha_v1: the on-disk dir-sha, computed from the files actually present.
        sha_v1 = compose_packs._dir_sha256(skill_dir_abs)
        # sha_v2: a synthetic sha representing a newer upstream version that has
        # NOT yet been written to disk.
        sha_v2 = "dir-sha256:" + "b" * 64

        # Lock: input_sha256 is sha_v2 (current upstream), sha_v1 in ring.
        lock_entry = _minimal_skill_file_entry(skill_dir_rel + "/", sha_v2)
        lock_entry["historical_input_sha256"] = [sha_v1]
        previous_lock = _pack_lock_with_file(lock_entry)

        prior_outputs = compose_packs._build_prior_pack_outputs(
            root=self.root,
            previous_pack_lock=previous_lock,
        )

        skill_md_abs = str((skill_dir_abs / "SKILL.md").resolve())
        self.assertIn(
            skill_md_abs,
            prior_outputs,
            "SKILL.md must be in prior_pack_outputs when on-disk sha matches "
            "a historical ring entry (sha_v1), even though input_sha256 is sha_v2.",
        )
        # Confirm the recorded hash is from the on-disk v1 content.
        self.assertEqual(
            prior_outputs[skill_md_abs],
            _sha256(v1_content),
            "prior_pack_outputs must record the on-disk file hash (v1_content).",
        )


if __name__ == "__main__":
    unittest.main()
