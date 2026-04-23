"""Recoverable staged transaction for pack lifecycle writes (v0.4.0 Phase 2).

Every install / update / uninstall / re-stamp operation that mutates
either project-local or user-level state runs inside a transaction. The
contract is "recoverable staged transaction" (per pack-architecture.md §
"Atomicity contract"), not atomic directory swap:

- Cross-platform atomic directory replacement is not a reliable primitive.
  POSIX ``rename(2)`` atomicity on directories requires the target to be
  empty; Windows ``MoveFileEx(MOVEFILE_REPLACE_EXISTING)`` does not work
  on non-empty directories; antivirus and open file handles can both
  block rename.
- The only primitive relied on is per-file atomic rename (``os.replace``).
- Every op (write / delete / restamp) is journaled in ``transaction.json``
  with pre-state hashes for both sides + liveness metadata (PID + lock
  path) before any filesystem change is made.
- On normal exit, ``commit()`` runs the ops in order with per-file atomic
  rename, then deletes the journal + staging directory.
- On exception inside the ``with`` block, ``rollback()`` cleans the
  staging directory; no on-disk targets are changed because no commit
  ran yet.
- On a hard crash mid-commit, the journal stays on disk and
  ``reconciliation.py`` on the next startup either rolls forward (all
  new content already in place), rolls back (pre-state still matches),
  or surfaces drift (neither matches).

Usage::

    with transaction.Transaction(staging_dir, lock_path) as txn:
        txn.stage_write(target_path, new_content_bytes)
        txn.stage_delete(old_path)
        txn.stage_restamp(old_hook_path, new_hook_path, hook_bytes)
    # commit happens on __exit__ success; rollback on exception.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Windows-specific os.replace retry for transient sharing violations from
# AV scanners, IDE indexers, or Claude Code reading handles. POSIX rename
# is atomic and does not exhibit this failure mode, so the retry is a no-op
# on non-Windows platforms. Duplicated from state.py to avoid inter-module
# dependencies at the atomicity primitive layer.
_IS_WINDOWS = sys.platform == "win32"
_REPLACE_RETRIES = 2
_REPLACE_RETRY_DELAY_SECONDS = 0.1


def _atomic_replace(src: str, dst: str) -> None:
    if not _IS_WINDOWS:
        os.replace(src, dst)
        return
    for attempt in range(_REPLACE_RETRIES + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_SECONDS)


# Op types recorded in transaction.json. All three are kept explicit rather
# than decomposing restamp into write+delete so reconciliation can detect
# a mid-restamp crash and distinguish it from an unrelated write crash.
OP_WRITE = "write"
OP_DELETE = "delete"
OP_RESTAMP = "restamp"

JOURNAL_NAME = "transaction.json"
JOURNAL_VERSION = 1


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_of_path(path: Path) -> str | None:
    """Return the sha256 of the file at ``path``, or ``None`` if the file
    does not exist. Used to record pre-state hashes for later reconciliation."""
    try:
        return _sha256_bytes(path.read_bytes())
    except FileNotFoundError:
        return None


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via temp file + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        _atomic_replace(tmp, str(path))
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class TransactionError(RuntimeError):
    """Raised when a transaction cannot commit or when rollback cannot
    clean staging state. Distinct from ``FileNotFoundError`` etc. so
    callers can distinguish transaction-layer failures from the IO
    errors that caused them."""


class Transaction:
    """Staged recoverable transaction for a batch of pack lifecycle writes.

    Parameters
    ----------
    staging_dir
        Directory (typically ``<parent>/<pack>.staging-<txn_id>``) that
        holds the journal + staged content files. Created on ``__enter__``.
    lock_path
        Absolute path to the lock file whose contention state indicates a
        live transaction. Recorded in the journal so ``reconciliation.py``
        can check ``flock``/``msvcrt.locking`` contention to distinguish
        a live transaction from an orphan.
    """

    def __init__(self, staging_dir: Path, lock_path: Path) -> None:
        self.staging_dir = staging_dir
        self.lock_path = lock_path
        self.txn_id = (
            f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        )
        self.journal_path = staging_dir / JOURNAL_NAME
        self.ops: list[dict[str, Any]] = []
        self._committed = False

    # ------------------------------------------------------------------
    # Context management
    # ------------------------------------------------------------------

    def __enter__(self) -> Transaction:
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._write_journal()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    # ------------------------------------------------------------------
    # Op staging
    # ------------------------------------------------------------------

    def stage_write(self, target_path: Path, content: bytes) -> None:
        """Queue a write op: copy ``content`` into the staging dir and
        record it for later atomic rename into ``target_path``.

        The pre-state hash at ``target_path`` is captured now so a later
        reconciliation can tell whether the on-disk file still matches
        what we expected before committing.
        """
        staged = self._stage_filename_for(target_path, suffix=".new")
        _atomic_write_bytes(staged, content)
        self.ops.append(
            {
                "op": OP_WRITE,
                "target_path": str(target_path),
                "staged_path": str(staged),
                "pre_state_sha256": _sha256_of_path(target_path),
                "new_content_sha256": _sha256_bytes(content),
            }
        )
        self._write_journal()

    def stage_delete(self, target_path: Path) -> None:
        """Queue a delete op on ``target_path``.

        No staging file is required. The pre-state hash is captured so
        reconciliation knows what content was meant to disappear.
        """
        self.ops.append(
            {
                "op": OP_DELETE,
                "target_path": str(target_path),
                "pre_state_sha256": _sha256_of_path(target_path),
            }
        )
        self._write_journal()

    def stage_restamp(
        self, old_path: Path, new_path: Path, content: bytes
    ) -> None:
        """Queue a restamp op: write ``content`` to ``new_path`` (via
        staging), then delete ``old_path`` at commit time.

        Used by hook-order re-stamping where ``01-foo.py`` becomes
        ``02-foo.py`` (content unchanged; prefix changed). Recorded as a
        single op so reconciliation can tell a mid-restamp crash from an
        unrelated write-crash.
        """
        staged = self._stage_filename_for(new_path, suffix=".restamp")
        _atomic_write_bytes(staged, content)
        self.ops.append(
            {
                "op": OP_RESTAMP,
                "old_path": str(old_path),
                "new_path": str(new_path),
                "staged_path": str(staged),
                "pre_state_old_sha256": _sha256_of_path(old_path),
                "pre_state_new_sha256": _sha256_of_path(new_path),
                "new_content_sha256": _sha256_bytes(content),
            }
        )
        self._write_journal()

    # ------------------------------------------------------------------
    # Commit / rollback
    # ------------------------------------------------------------------

    def commit(self) -> None:
        """Apply all queued ops in order with per-file atomic rename.

        On success, deletes the journal and staging directory. If a
        rename fails partway, raises ``TransactionError``; the partially
        applied state (some targets updated, others not) is left on disk
        for ``reconciliation.py`` to resolve on the next startup.
        """
        if self._committed:
            raise TransactionError(
                f"transaction {self.txn_id} already committed"
            )
        for idx, op in enumerate(self.ops):
            try:
                self._apply_op(op)
            except Exception as exc:
                raise TransactionError(
                    f"transaction {self.txn_id} failed at op[{idx}] "
                    f"({op['op']}): {exc}. Partial state left on disk for "
                    "reconciliation on next startup."
                ) from exc
        self._committed = True
        self._cleanup()

    def rollback(self) -> None:
        """Discard the staged ops without touching on-disk targets.

        Called automatically on exception inside the ``with`` block,
        before ``commit()`` has started. No targets have been renamed yet,
        so cleaning the staging directory is sufficient; the journal
        disappears with it.
        """
        self._cleanup()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_op(self, op: dict[str, Any]) -> None:
        kind = op["op"]
        if kind == OP_WRITE:
            target = Path(op["target_path"])
            staged = Path(op["staged_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_replace(str(staged), str(target))
        elif kind == OP_DELETE:
            target = Path(op["target_path"])
            try:
                target.unlink()
            except FileNotFoundError:
                # Treat "already gone" as success; pre-state hash being
                # None is normal (a previous op may have deleted it).
                pass
        elif kind == OP_RESTAMP:
            old_path = Path(op["old_path"])
            new_path = Path(op["new_path"])
            staged = Path(op["staged_path"])
            new_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_replace(str(staged), str(new_path))
            try:
                old_path.unlink()
            except FileNotFoundError:
                pass
        else:
            raise TransactionError(f"unknown op type {kind!r}")

    def _stage_filename_for(self, target_path: Path, *, suffix: str) -> Path:
        """Build a collision-resistant staged filename inside ``staging_dir``.

        Encodes the target path's basename + a short uuid suffix so two
        ops targeting the same directory do not clobber each other in
        staging.
        """
        stem = target_path.name
        token = uuid.uuid4().hex[:8]
        return self.staging_dir / f"{stem}.{token}{suffix}"

    def _write_journal(self) -> None:
        payload = {
            "version": JOURNAL_VERSION,
            "txn_id": self.txn_id,
            "created_at": time.time(),
            "pid": os.getpid(),
            "lock_path": str(self.lock_path),
            "staging_dir": str(self.staging_dir),
            "ops": self.ops,
        }
        fd, tmp = tempfile.mkstemp(
            dir=str(self.staging_dir),
            prefix=JOURNAL_NAME + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            # Use _atomic_replace so the journal rewrite gets the same
            # Windows AV-retry as staged target writes. The journal is in
            # the staging dir (low external-handle risk) but AV scanners
            # and indexers do open newly-written files, and a failed
            # journal rewrite aborts the whole transaction.
            _atomic_replace(tmp, str(self.journal_path))
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _cleanup(self) -> None:
        """Remove the staging directory and its contents best-effort."""
        if not self.staging_dir.exists():
            return
        for entry in self.staging_dir.iterdir():
            try:
                if entry.is_dir():
                    # Nested dirs inside a staging dir are unusual but
                    # possible if a caller staged deeply. Walk and remove.
                    _rmtree(entry)
                else:
                    entry.unlink()
            except OSError:
                pass
        try:
            self.staging_dir.rmdir()
        except OSError:
            pass


def _rmtree(root: Path) -> None:
    for child in root.iterdir():
        if child.is_dir():
            _rmtree(child)
        else:
            try:
                child.unlink()
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass


def load_journal(journal_path: Path) -> dict[str, Any]:
    """Load a ``transaction.json`` journal for reconciliation.

    Does NOT validate structural correctness here; reconciliation has
    richer error handling (drift report + abort). We just parse JSON and
    let the caller decide.
    """
    try:
        text = journal_path.read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise TransactionError(
            f"cannot load journal {journal_path}: {exc}"
        ) from exc


@contextmanager
def scratch_transaction(
    base_dir: Path, lock_path: Path, tag: str
) -> Iterator[Transaction]:
    """Convenience helper: build a uniquely-named staging dir under
    ``base_dir`` and wrap a ``Transaction`` around it.

    The staging dir is ``<base_dir>/<tag>.staging-<short-token>``. Caller
    owns the lock at ``lock_path``; this helper only writes to the staging
    directory.
    """
    token = uuid.uuid4().hex[:8]
    staging = base_dir / f"{tag}.staging-{token}"
    with Transaction(staging, lock_path) as txn:
        yield txn
