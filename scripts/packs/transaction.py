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


# v0.5.2 drift gate categories. The composer classifies every staged
# write target into one of these before calling :meth:`Transaction.commit`
# so the pre-commit validation pass knows which validation rule applies.
# See PLAN-aa-v0.5.2.md § "Write-path drift gate" for the full contract.
PRESTATE_PACK_OUTPUT = "pack-output"
"""Tracked pack output recorded in a prior pack-state.json. The recorded
sha256 must match the current on-disk content; mismatch is hand-edit drift."""

PRESTATE_INTERNAL_STATE = "internal-state"
"""Internal state file (pack-lock.json, pack-state.json, user-level
pack-state.json). The current on-disk hash captured at stage time must
still match at commit time (optimistic concurrency for concurrent writers)."""

PRESTATE_CORE_OUTPUT = "core-output"
"""Composer-owned core output (e.g., AGENTS.md). Same optimistic-concurrency
rule as ``internal-state``: stage-time hash must equal commit-time hash."""

PRESTATE_JSON_MERGE = "json-merge"
"""Declared JSON merge target for active-permission entries (typically
~/.claude/settings.json). Same optimistic-concurrency rule as core-output.
Without this category the permission handler's full-file rewrite of an
existing settings.json would be rejected as an unmanaged collision."""

PRESTATE_UNMANAGED = "unmanaged"
"""Anything not classified above. If the path exists on disk, abort as
an unmanaged collision; if absent, allow the write to proceed."""

# Valid category strings. Used by the drift-gate validator to reject
# typos in the composer's ``expected_prestate`` map.
_VALID_PRESTATE_CATEGORIES = frozenset(
    {
        PRESTATE_PACK_OUTPUT,
        PRESTATE_INTERNAL_STATE,
        PRESTATE_CORE_OUTPUT,
        PRESTATE_JSON_MERGE,
        PRESTATE_UNMANAGED,
    }
)


class DriftAbort(TransactionError):
    """Raised by :meth:`Transaction.commit` when the pre-commit drift gate
    rejects at least one staged write.

    Carries the per-path category + reason so the composer can surface a
    consumer-facing message that distinguishes managed-file drift (recorded
    sha256 differs from current), optimistic-concurrency loss (stage-time
    sha256 differs from commit-time sha256), and unmanaged-path collision
    (a non-pack file already lives at a path the composer planned to
    write). All three are recoverable: the user fixes the issue and reruns
    the original command. No on-disk target has been modified at this
    point — the staging dir contains the would-be commits.
    """

    def __init__(self, drift_paths: list[tuple[str, str, str]]) -> None:
        self.drift_paths = list(drift_paths)
        summary = "; ".join(
            f"{path} ({category}: {reason})"
            for path, category, reason in drift_paths
        )
        super().__init__(
            f"drift gate rejected {len(drift_paths)} staged write(s): {summary}"
        )


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
        # v0.5.2 drift gate. The composer populates this map via
        # :meth:`set_expected_prestate` (or directly) before calling
        # :meth:`commit`. Keys are absolute target paths; values are
        # ``(category, recorded_sha256_or_None)`` tuples. Empty dict (the
        # default) disables the gate so callers that don't supply it
        # see v0.4.0/v0.5.0 behavior.
        self.expected_prestate: dict[str, tuple[str, str | None]] = {}
        # v0.5.3 adopt-on-match: target paths whose pre-existing on-disk
        # content matched what this transaction was about to write. The
        # drift gate adopts these instead of rejecting them; the composer
        # caller may surface the count to the user. Populated by
        # :meth:`_validate_prestate` and reset on every gate run.
        self.adopted_paths: list[str] = []

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

    def set_expected_prestate(
        self, expected_prestate: dict[str, tuple[str, str | None]]
    ) -> None:
        """Install the v0.5.2 drift-gate classification map.

        Keys are absolute target paths (matched after string-coercing
        each op's ``target_path``); values are ``(category, recorded_sha256)``
        pairs. ``recorded_sha256`` is meaningful only for
        ``PRESTATE_PACK_OUTPUT`` (the prior recorded hash from
        ``pack-state.json``); for ``PRESTATE_INTERNAL_STATE``,
        ``PRESTATE_CORE_OUTPUT`` and ``PRESTATE_JSON_MERGE`` the validator
        compares the stage-time hash captured by ``stage_write`` /
        ``stage_restamp`` against the commit-time hash, so callers may pass
        ``None``.

        Raises ``TransactionError`` on unknown categories so a typo in the
        composer's classification map fails fast rather than silently
        downgrading to ``unmanaged`` and rejecting a normal install.
        """
        for path, value in expected_prestate.items():
            if not (isinstance(value, tuple) and len(value) == 2):
                raise TransactionError(
                    f"expected_prestate[{path!r}] must be a (category, sha256) tuple"
                )
            category, _ = value
            if category not in _VALID_PRESTATE_CATEGORIES:
                raise TransactionError(
                    f"expected_prestate[{path!r}] unknown category {category!r}; "
                    f"expected one of {sorted(_VALID_PRESTATE_CATEGORIES)}"
                )
        self.expected_prestate = dict(expected_prestate)

    def _validate_prestate(self) -> list[tuple[str, str, str]]:
        """Pre-commit drift-gate pass.

        Walks every queued op and applies the validation rule for its
        classified category. Returns a list of ``(target_path, category,
        reason)`` tuples for every staged write that fails its rule. An
        empty list means commit may proceed.

        Validation rules per PLAN-aa-v0.5.2.md § "Write-path drift gate":

        - ``PRESTATE_PACK_OUTPUT``: recorded sha256 (from prior
          pack-state.json) must equal the current on-disk hash; mismatch
          is hand-edit drift on a tracked pack output.
        - ``PRESTATE_INTERNAL_STATE`` / ``PRESTATE_CORE_OUTPUT`` /
          ``PRESTATE_JSON_MERGE``: stage-time hash captured by
          ``stage_write`` (op["pre_state_sha256"]) must equal current
          on-disk hash. This is optimistic-concurrency: a concurrent
          writer that mutated the file between stage_write and commit
          loses.
        - ``PRESTATE_UNMANAGED``: present-on-disk → unmanaged collision
          UNLESS the on-disk sha256 already equals the staged
          ``new_content_sha256`` (v0.5.3 adopt-on-match). On match, the
          path is appended to ``self.adopted_paths`` and skipped — the
          subsequent ``_apply_op`` rewrites byte-identical content, so
          the file ends up owned by the lockfile entry the caller just
          recorded. On mismatch (or ``new_content_sha256`` missing for
          any reason), keep the v0.5.2 reject behavior. Absent target →
          allow.
        - No classification → behave as ``PRESTATE_UNMANAGED`` (fail-safe).

        Delete and restamp ops are validated against ``target_path`` /
        ``new_path`` respectively; restamp's ``old_path`` is treated as
        a delete (no validation needed beyond the existing journal
        pre-state hash).
        """
        drift: list[tuple[str, str, str]] = []
        adopted: list[str] = []
        for op in self.ops:
            kind = op["op"]
            if kind == OP_WRITE:
                target_str = op["target_path"]
                staged_pre = op.get("pre_state_sha256")
            elif kind == OP_RESTAMP:
                target_str = op["new_path"]
                staged_pre = op.get("pre_state_new_sha256")
            elif kind == OP_DELETE:
                # Deletes do not write new content; the existing
                # uninstall.py drift check already validates pre-state
                # before we get here. Skip the gate for deletes.
                continue
            else:
                continue
            target_path = Path(target_str)
            classification = self.expected_prestate.get(target_str)
            if classification is None:
                category = PRESTATE_UNMANAGED
                recorded = None
            else:
                category, recorded = classification
            current = _sha256_of_path(target_path)
            if category == PRESTATE_PACK_OUTPUT:
                # recorded is from pack-state.json; current must match.
                if recorded is None:
                    # Tracked pack output without a recorded hash = first
                    # install of that path; treat as absent target (allow).
                    if current is not None:
                        drift.append(
                            (
                                target_str,
                                category,
                                "tracked output marked first-install but "
                                "file already exists on disk",
                            )
                        )
                    continue
                if current is None:
                    # Tracked output but missing on disk: re-install case;
                    # allow (the transaction will recreate it).
                    continue
                if current != recorded:
                    drift.append(
                        (
                            target_str,
                            category,
                            "tracked pack output content drifted from "
                            "recorded sha256 (hand-edited)",
                        )
                    )
            elif category in (
                PRESTATE_INTERNAL_STATE,
                PRESTATE_CORE_OUTPUT,
                PRESTATE_JSON_MERGE,
            ):
                # Optimistic concurrency: stage-time hash must equal
                # commit-time hash. Both being None (file absent both at
                # stage and commit) is acceptable.
                if staged_pre != current:
                    drift.append(
                        (
                            target_str,
                            category,
                            "concurrent writer modified target between "
                            "stage and commit",
                        )
                    )
            else:
                # PRESTATE_UNMANAGED. If the file already exists, refuse
                # to clobber UNLESS its content already matches what we
                # were about to write — v0.5.3 adopt-on-match closes the
                # AC->AA migration gap, interrupted-pack-add resumption,
                # team-collaboration first-clone, and manual-deploy
                # adoption cases without weakening the gate (mismatched
                # content still rejects, preserving user edits).
                if current is not None:
                    new_hash = op.get("new_content_sha256")
                    if new_hash is not None and current == new_hash:
                        adopted.append(target_str)
                    else:
                        drift.append(
                            (
                                target_str,
                                category,
                                "unmanaged file already exists at target path",
                            )
                        )
        self.adopted_paths = adopted
        return drift

    def commit(self) -> None:
        """Apply all queued ops in order with per-file atomic rename.

        v0.5.2 adds a pre-commit drift-gate pass when
        ``expected_prestate`` has been populated by the caller. The gate
        runs before any disk mutation: if any staged write violates its
        category's validation rule, raises :class:`DriftAbort` carrying
        the offending paths, calls ``rollback()`` to clean staging, and
        leaves on-disk targets untouched. Empty ``expected_prestate``
        (the default) skips the gate so v0.4.0/v0.5.0 callers see
        unchanged behavior.

        On success, deletes the journal and staging directory. If a
        rename fails partway, raises ``TransactionError``; the partially
        applied state (some targets updated, others not) is left on disk
        for ``reconciliation.py`` to resolve on the next startup.
        """
        if self._committed:
            raise TransactionError(
                f"transaction {self.txn_id} already committed"
            )
        # v0.5.2: drift-gate pre-commit validation. Skipped when
        # ``expected_prestate`` is empty (v0.4.0/v0.5.0 callers).
        if self.expected_prestate:
            drift = self._validate_prestate()
            if drift:
                # Roll back the staging dir before raising so the caller's
                # __exit__ doesn't re-rollback an already-cleaned dir.
                self._cleanup()
                raise DriftAbort(drift)
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
