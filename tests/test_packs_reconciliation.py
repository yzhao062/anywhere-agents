"""Tests for scripts/packs/reconciliation.py.

Classification logic for orphan staging directories: pre-state (rollback),
new-state (rollforward), mixed (partial), drift, malformed, live.
"""
from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import locks  # noqa: E402
from packs import reconciliation  # noqa: E402
from packs import transaction as txn_mod  # noqa: E402


def _hold_lock_for(lock_path_str: str, hold_seconds: float, started_flag_path: str) -> None:
    """Subprocess target for 'live transaction' test."""
    flag = Path(started_flag_path)
    with locks.acquire(Path(lock_path_str), timeout=5.0):
        flag.write_text("ready", encoding="utf-8")
        time.sleep(hold_seconds)


class _TmpDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lock_path = self.root / "peer.lock"
        self.lock_path.write_text("0\n", encoding="utf-8")

    def _orphan_for(self, target: Path, new_content: bytes) -> Path:
        """Build an orphan staging dir with a single stage_write op,
        then manually roll back to leave on-disk targets at pre-state."""
        staging = self.root / "stage.staging-aaaa"
        txn = txn_mod.Transaction(staging, self.lock_path)
        txn.__enter__()
        txn.stage_write(target, new_content)
        # Don't commit or clean up — leave the staging dir as an orphan
        # with the journal on disk.
        return staging


class RollbackOkTests(_TmpDirCase):
    def test_target_still_at_pre_state_is_rollback(self) -> None:
        target = self.root / "out.txt"
        target.write_bytes(b"pre")
        staging = self._orphan_for(target, b"new-content")
        # Target still at pre-state since we never committed.
        result = reconciliation.classify_orphan(staging)
        self.assertEqual(result.label, reconciliation.ROLLBACK_OK)

    def test_target_absent_and_pre_state_null_is_rollback(self) -> None:
        """Write op where pre-state was absent; target still absent = pre-state."""
        target = self.root / "out.txt"
        staging = self._orphan_for(target, b"new-content")
        # Target doesn't exist; pre_state_sha256 in journal is None; this
        # reads as pre-state too.
        result = reconciliation.classify_orphan(staging)
        self.assertEqual(result.label, reconciliation.ROLLBACK_OK)

    def test_pre_state_write_plus_delete_of_absent_is_rollback(self) -> None:
        """A mixed transaction of pre-state writes and delete-of-absent ops
        must classify as ROLLBACK_OK, not PARTIAL: delete-of-absent is a
        no-op so it cannot tip a clean rollback into a mixed state."""
        a = self.root / "a.txt"
        a.write_bytes(b"a-pre")
        b = self.root / "b.txt"  # never existed; pre-state absent
        staging = self.root / "stage.staging-mixed-rollback"
        txn = txn_mod.Transaction(staging, self.lock_path)
        txn.__enter__()
        txn.stage_write(a, b"a-new")  # pre-state hash = sha(a-pre)
        txn.stage_delete(b)  # pre-state hash = None (absent)
        # No commit: a still at pre-state, b still absent.
        result = reconciliation.classify_orphan(staging)
        self.assertEqual(result.label, reconciliation.ROLLBACK_OK)


class RollforwardOkTests(_TmpDirCase):
    def test_target_at_new_content_is_rollforward(self) -> None:
        target = self.root / "out.txt"
        target.write_bytes(b"pre")
        staging = self._orphan_for(target, b"new-content")
        # Simulate that commit ran: write new content to target manually.
        target.write_bytes(b"new-content")
        result = reconciliation.classify_orphan(staging)
        self.assertEqual(result.label, reconciliation.ROLLFORWARD_OK)


class DriftTests(_TmpDirCase):
    def test_unexpected_content_is_drift(self) -> None:
        target = self.root / "out.txt"
        target.write_bytes(b"pre")
        staging = self._orphan_for(target, b"new-content")
        # Someone wrote something else entirely.
        target.write_bytes(b"unexpected")
        result = reconciliation.classify_orphan(staging)
        self.assertEqual(result.label, reconciliation.DRIFT)


class PartialTests(_TmpDirCase):
    def test_mixed_pre_and_new_states_is_partial(self) -> None:
        a = self.root / "a.txt"
        b = self.root / "b.txt"
        a.write_bytes(b"a-pre")
        b.write_bytes(b"b-pre")
        staging = self.root / "stage.staging-mixed"
        txn = txn_mod.Transaction(staging, self.lock_path)
        txn.__enter__()
        txn.stage_write(a, b"a-new")
        txn.stage_write(b, b"b-new")
        # Simulate partial commit: a applied, b not applied.
        a.write_bytes(b"a-new")
        result = reconciliation.classify_orphan(staging)
        self.assertEqual(result.label, reconciliation.PARTIAL)


class MalformedTests(_TmpDirCase):
    def test_missing_journal_is_malformed(self) -> None:
        staging = self.root / "stage.staging-empty"
        staging.mkdir()
        result = reconciliation.classify_orphan(staging)
        self.assertEqual(result.label, reconciliation.MALFORMED)

    def test_malformed_journal_is_malformed(self) -> None:
        staging = self.root / "stage.staging-bad"
        staging.mkdir()
        (staging / "transaction.json").write_text("not json", encoding="utf-8")
        result = reconciliation.classify_orphan(staging)
        self.assertEqual(result.label, reconciliation.MALFORMED)

    def test_unknown_op_kind_is_malformed(self) -> None:
        staging = self.root / "stage.staging-weird"
        staging.mkdir()
        (staging / "transaction.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "txn_id": "x",
                    "pid": 0,
                    "lock_path": str(self.lock_path),
                    "ops": [{"op": "teleport", "target_path": "/nope"}],
                }
            ),
            encoding="utf-8",
        )
        result = reconciliation.classify_orphan(staging)
        self.assertEqual(result.label, reconciliation.MALFORMED)

    def test_missing_required_op_field_is_malformed_not_crash(self) -> None:
        """A journal op missing a required field must classify as MALFORMED
        rather than crash reconciliation (safety path: any orphan, no
        matter how corrupt, must not block startup)."""
        staging = self.root / "stage.staging-bad-field"
        staging.mkdir()
        (staging / "transaction.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "txn_id": "x",
                    "pid": 0,
                    "lock_path": str(self.lock_path),
                    "ops": [{"op": "write", "new_content_sha256": "abc"}],
                    # missing target_path + staged_path + pre_state_sha256
                }
            ),
            encoding="utf-8",
        )
        result = reconciliation.classify_orphan(staging)
        self.assertEqual(result.label, reconciliation.MALFORMED)
        self.assertIn("missing or invalid field", result.detail)


class LiveTransactionTests(_TmpDirCase):
    def test_held_lock_short_circuits_to_live(self) -> None:
        """Contention on the journal's lock_path means a peer is still live;
        reconciliation must skip without inspecting ops."""
        target = self.root / "out.txt"
        target.write_bytes(b"pre")
        staging = self._orphan_for(target, b"new-content")

        # Have a peer process acquire the lock and hold it briefly.
        started = self.root / "started.flag"
        proc = multiprocessing.Process(
            target=_hold_lock_for,
            args=(str(self.lock_path), 2.0, str(started)),
        )
        proc.start()
        self.addCleanup(proc.join)
        # 15s tolerates slow CI process startup; see test_packs_locks.py note.
        deadline = time.monotonic() + 15.0
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(started.exists())

        result = reconciliation.classify_orphan(staging)
        self.assertEqual(result.label, reconciliation.LIVE)

        proc.join(timeout=10.0)


# ---------- scan + cleanup ----------


class ScanOrphansTests(_TmpDirCase):
    def test_scan_finds_matching_dirs_with_journals(self) -> None:
        target = self.root / "out.txt"
        self._orphan_for(target, b"new-content")
        # Non-staging dir should be ignored.
        (self.root / "not-a-staging-dir").mkdir()
        # Staging-named dir without journal should be ignored.
        (self.root / "fake.staging-bogus").mkdir()

        results = reconciliation.scan_orphans([self.root])
        self.assertEqual(len(results), 1)

    def test_scan_skips_nonexistent_search_dir(self) -> None:
        results = reconciliation.scan_orphans([self.root / "nope"])
        self.assertEqual(results, [])


class CleanupStagingTests(_TmpDirCase):
    def test_cleanup_removes_staging_dir(self) -> None:
        target = self.root / "out.txt"
        staging = self._orphan_for(target, b"new-content")
        reconciliation.cleanup_staging(staging)
        self.assertFalse(staging.exists())

    def test_cleanup_is_idempotent(self) -> None:
        staging = self.root / "gone.staging-x"
        reconciliation.cleanup_staging(staging)  # nothing to clean
        staging.mkdir()
        reconciliation.cleanup_staging(staging)
        self.assertFalse(staging.exists())


if __name__ == "__main__":
    unittest.main()
