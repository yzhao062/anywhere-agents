"""Startup orphan reconciliation for pack lifecycle transactions (Phase 2).

On every bootstrap, the composer scans for orphan transaction directories
left behind by a previous run that crashed mid-lifecycle. Each orphan is
classified by comparing on-disk content against the journal's pre-state
and new-content hashes, then one of:

- ``LIVE`` — another process still holds the transaction's lock file.
  Skipped without touching anything; the peer composer's commit or
  rollback will clean this up on its own exit. Windows busy-lock where
  the holder PID cannot be confirmed is treated the same as a proven
  live holder (``locks.is_held`` returns ``True`` for both).
- ``ROLLBACK_OK`` — all op targets still match their pre-state hashes.
  The previous run staged content but never committed. Safe to delete
  the staging directory; no on-disk targets need reverting.
- ``ROLLFORWARD_OK`` — all op targets match the new-content hashes.
  The previous run committed successfully but crashed before cleaning
  the staging directory. Safe to delete the staging directory; on-disk
  targets are already in the intended final state.
- ``PARTIAL`` — some ops at pre-state, others at new-content, every op
  matches one or the other (no drift). The previous run committed
  partway. Caller may roll forward by reapplying the un-applied ops.
- ``DRIFT`` — at least one op's target matches neither pre-state nor
  new-content. On-disk state is unknown; leave in place and surface as
  a drift report per pack-architecture.md § "Atomicity contract".
- ``MALFORMED`` — the journal cannot be read as JSON. Leave the staging
  directory alone; surface to the user.

This module classifies; it does not mutate on-disk targets and does not
delete staging directories. Phase 3 wires ``classify_orphan`` +
``cleanup_staging`` into the composer startup flow.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import locks
from . import transaction as txn_mod

# ----- classification labels -----

LIVE = "live"
ROLLBACK_OK = "rollback_ok"
ROLLFORWARD_OK = "rollforward_ok"
PARTIAL = "partial"
DRIFT = "drift"
MALFORMED = "malformed"


@dataclass
class OpClassification:
    """Per-op reconciliation result within a transaction."""

    op_index: int
    op_kind: str
    target_path: str
    on_disk_state: str  # one of: "pre_state", "new_state", "drift", "absent"


@dataclass
class OrphanClassification:
    """Overall reconciliation result for one orphan staging directory."""

    staging_dir: Path
    label: str
    journal: dict[str, Any] | None = None
    ops: list[OpClassification] = field(default_factory=list)
    detail: str = ""


def _sha256_of_path(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        # Permission errors etc.: return None so the caller reports drift.
        raise exc


def _classify_write_op(op: dict[str, Any]) -> OpClassification:
    target = Path(op["target_path"])
    pre = op.get("pre_state_sha256")
    new = op.get("new_content_sha256")
    actual = _sha256_of_path(target)
    if actual is None:
        state = "pre_state" if pre is None else "drift"
    elif actual == new:
        state = "new_state"
    elif actual == pre:
        state = "pre_state"
    else:
        state = "drift"
    return OpClassification(
        op_index=-1, op_kind=txn_mod.OP_WRITE, target_path=str(target),
        on_disk_state=state,
    )


def _classify_delete_op(op: dict[str, Any]) -> OpClassification:
    target = Path(op["target_path"])
    pre = op.get("pre_state_sha256")
    actual = _sha256_of_path(target)
    if actual is None:
        # File absent now. If pre-state was ALSO absent, this is a
        # delete-of-absent (a no-op) — indistinguishable between pre and
        # new state. Classify as pre_state so a mix of pre_state writes
        # + delete-of-absent stays ROLLBACK_OK rather than falsely
        # reporting PARTIAL (per pack-architecture.md reconciliation
        # semantics: PARTIAL implies at least one op actually committed).
        state = "pre_state" if pre is None else "new_state"
    elif pre is not None and actual == pre:
        # File still present at pre-state = delete hasn't happened = pre_state
        state = "pre_state"
    else:
        state = "drift"
    return OpClassification(
        op_index=-1, op_kind=txn_mod.OP_DELETE, target_path=str(target),
        on_disk_state=state,
    )


def _classify_restamp_op(op: dict[str, Any]) -> OpClassification:
    old_path = Path(op["old_path"])
    new_path = Path(op["new_path"])
    pre_old = op.get("pre_state_old_sha256")
    pre_new = op.get("pre_state_new_sha256")
    new_content = op.get("new_content_sha256")
    actual_old = _sha256_of_path(old_path)
    actual_new = _sha256_of_path(new_path)

    # New state: new_path has new_content, old_path is gone.
    if actual_new == new_content and actual_old is None:
        state = "new_state"
    # Pre-state: old_path matches its pre-hash; new_path at its pre-hash
    # (usually absent).
    elif (
        actual_old == pre_old
        and (actual_new == pre_new or (pre_new is None and actual_new is None))
    ):
        state = "pre_state"
    else:
        state = "drift"
    return OpClassification(
        op_index=-1, op_kind=txn_mod.OP_RESTAMP,
        target_path=f"{old_path} -> {new_path}",
        on_disk_state=state,
    )


def classify_orphan(staging_dir: Path) -> OrphanClassification:
    """Inspect one orphan staging directory and classify it.

    Reads the transaction journal, checks lock contention first (a live
    transaction always short-circuits), then compares on-disk content
    against each op's pre-state / new-content hashes.
    """
    journal_path = staging_dir / txn_mod.JOURNAL_NAME
    if not journal_path.exists():
        return OrphanClassification(
            staging_dir=staging_dir,
            label=MALFORMED,
            detail=f"journal {journal_path} not found",
        )
    try:
        journal = txn_mod.load_journal(journal_path)
    except txn_mod.TransactionError as exc:
        return OrphanClassification(
            staging_dir=staging_dir,
            label=MALFORMED,
            detail=str(exc),
        )

    lock_path_str = journal.get("lock_path")
    if isinstance(lock_path_str, str) and lock_path_str:
        lock_path = Path(lock_path_str)
        # Contention on the recorded lock path is authoritative: even
        # on Windows where holder PID cannot always be confirmed, a
        # busy lock means "live transaction, skip and retry".
        if locks.is_held(lock_path):
            return OrphanClassification(
                staging_dir=staging_dir,
                label=LIVE,
                journal=journal,
                detail=f"lock {lock_path} is held; skip this pass",
            )

    ops_in_journal = journal.get("ops")
    if not isinstance(ops_in_journal, list):
        return OrphanClassification(
            staging_dir=staging_dir,
            label=MALFORMED,
            journal=journal,
            detail="journal 'ops' is not a list",
        )

    classifications: list[OpClassification] = []
    for idx, op in enumerate(ops_in_journal):
        if not isinstance(op, dict) or "op" not in op:
            return OrphanClassification(
                staging_dir=staging_dir,
                label=MALFORMED,
                journal=journal,
                detail=f"op[{idx}] is malformed",
            )
        kind = op["op"]
        try:
            if kind == txn_mod.OP_WRITE:
                cls = _classify_write_op(op)
            elif kind == txn_mod.OP_DELETE:
                cls = _classify_delete_op(op)
            elif kind == txn_mod.OP_RESTAMP:
                cls = _classify_restamp_op(op)
            else:
                return OrphanClassification(
                    staging_dir=staging_dir,
                    label=MALFORMED,
                    journal=journal,
                    detail=f"op[{idx}] unknown kind {kind!r}",
                )
        except (KeyError, TypeError, ValueError) as exc:
            # Structural field errors (missing target_path, wrong types,
            # non-parseable values) must surface as MALFORMED so a bad
            # orphan journal can never crash startup reconciliation and
            # block bootstrap (pack-architecture.md § "Atomicity contract"
            # — orphans with unreadable state are surfaced, not raised).
            return OrphanClassification(
                staging_dir=staging_dir,
                label=MALFORMED,
                journal=journal,
                detail=f"op[{idx}] missing or invalid field: {exc}",
            )
        except OSError as exc:
            return OrphanClassification(
                staging_dir=staging_dir,
                label=DRIFT,
                journal=journal,
                detail=f"cannot read op[{idx}] target: {exc}",
            )
        cls.op_index = idx
        classifications.append(cls)

    states = {c.on_disk_state for c in classifications}
    if "drift" in states:
        label = DRIFT
    elif states == {"pre_state"}:
        label = ROLLBACK_OK
    elif states == {"new_state"}:
        label = ROLLFORWARD_OK
    elif states <= {"pre_state", "new_state"}:
        label = PARTIAL
    else:
        # Defensive: unknown state set. Treat as drift.
        label = DRIFT

    return OrphanClassification(
        staging_dir=staging_dir,
        label=label,
        journal=journal,
        ops=classifications,
    )


def scan_orphans(search_dirs: list[Path]) -> list[OrphanClassification]:
    """Find and classify every ``*.staging-*`` dir containing a journal.

    ``search_dirs`` typically includes ``~/.claude/hooks/`` (user-level
    staging parent) and ``<project>/.agent-config/`` (project-local
    staging parent). Non-existent search dirs are silently skipped.
    """
    results: list[OrphanClassification] = []
    for base in search_dirs:
        if not base.exists():
            continue
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            if ".staging-" not in entry.name:
                continue
            journal_path = entry / txn_mod.JOURNAL_NAME
            if not journal_path.exists():
                continue
            results.append(classify_orphan(entry))
    return results


def cleanup_staging(staging_dir: Path) -> None:
    """Remove an orphan staging directory after reconciliation.

    Safe to call on a directory that has already been partially cleaned;
    walks the tree and removes what it finds. Does NOT touch any target
    paths listed in the journal — those are caller-managed.
    """
    if not staging_dir.exists():
        return
    for entry in staging_dir.iterdir():
        try:
            if entry.is_dir():
                _rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            pass
    try:
        staging_dir.rmdir()
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
