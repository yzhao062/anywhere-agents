"""Internal uninstall engine for pack lifecycle (v0.4.0 Phase 4).

Removes every aa-pack-owned output recorded in a consumer project's
state files, with correct cross-repo owners-list semantics on
user-level outputs. Called by the ``anywhere-agents uninstall --all``
CLI (Phase 5), by rollback paths (Phase 4+), and by release-time
smoke tests.

Outcome contract (pack-architecture.md § "CLI contract for
``uninstall --all``"):

- ``clean`` — everything that was owned is gone; state files consistent.
- ``no-op`` — nothing to uninstall (state files absent or empty).
- ``lock-timeout`` — another process holds the per-user or per-repo
  lock longer than the timeout; no state change.
- ``drift`` — at least one owned file's on-disk content no longer
  matches the recorded hash; abort without overwriting.
- ``malformed-state`` — a state file failed parse / schema validation.
- ``partial-cleanup`` — some packs cleaned cleanly, others hit errors
  mid-operation; safe-to-reapply drift report generated.

The engine never overwrites or deletes a file whose on-disk content
drifted from its recorded hash. Drift is surfaced; user resolves.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import locks
from . import state as state_mod


# ----- typed outcomes -----

STATUS_CLEAN = "clean"
STATUS_NO_OP = "no-op"
STATUS_LOCK_TIMEOUT = "lock-timeout"
STATUS_DRIFT = "drift"
STATUS_MALFORMED = "malformed-state"
STATUS_PARTIAL = "partial-cleanup"


@dataclass
class UninstallOutcome:
    """Result of a ``run_uninstall_all`` invocation."""

    status: str
    packs_removed: list[str] = field(default_factory=list)
    files_deleted: list[str] = field(default_factory=list)
    owners_decremented: list[str] = field(default_factory=list)
    drift_paths: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    lock_holder_pid: int | None = None


# ----- engine -----


def run_uninstall_all(
    project_root: Path,
    *,
    user_home: Path | None = None,
    repo_id: str | None = None,
    lock_timeout: float = 30.0,
) -> UninstallOutcome:
    """Remove every aa-pack-owned output for this consumer project.

    Parameters
    ----------
    project_root
        The consumer repo root. Holds ``.agent-config/pack-lock.json``
        and ``.agent-config/pack-state.json``.
    user_home
        Base for user-level state. Defaults to ``Path.home()``.
    repo_id
        Stable identifier for this consumer repo; used to decrement
        owners lists on user-level state entries. Defaults to
        ``str(project_root.resolve())``, matching the composer.
    lock_timeout
        Seconds to wait for per-user and per-repo locks before giving
        up with ``STATUS_LOCK_TIMEOUT``.
    """
    project_root = project_root.resolve()
    if user_home is None:
        user_home = Path.home()
    if repo_id is None:
        repo_id = str(project_root)

    project_lock_path = project_root / ".agent-config" / "pack-lock.json"
    project_state_path = project_root / ".agent-config" / "pack-state.json"
    user_state_path = user_home / ".claude" / "pack-state.json"

    # No-op: nothing recorded.
    if not project_lock_path.exists() and not project_state_path.exists():
        return UninstallOutcome(status=STATUS_NO_OP)

    # Acquire locks before touching any state.
    user_lock = locks.user_lock_path(user_home)
    repo_lock = locks.repo_lock_path(project_root)

    try:
        with locks.acquire(user_lock, timeout=lock_timeout) as _user_h:
            with locks.acquire(repo_lock, timeout=lock_timeout) as _repo_h:
                return _uninstall_under_locks(
                    project_root=project_root,
                    user_home=user_home,
                    repo_id=repo_id,
                    project_lock_path=project_lock_path,
                    project_state_path=project_state_path,
                    user_state_path=user_state_path,
                )
    except locks.LockTimeout as exc:
        return UninstallOutcome(
            status=STATUS_LOCK_TIMEOUT,
            details=[str(exc)],
            lock_holder_pid=exc.holder_pid,
        )


def _uninstall_under_locks(
    *,
    project_root: Path,
    user_home: Path,
    repo_id: str,
    project_lock_path: Path,
    project_state_path: Path,
    user_state_path: Path,
) -> UninstallOutcome:
    """Body of the uninstall flow; caller holds per-user + per-repo locks."""
    # Load state files with malformed-state gating.
    try:
        pack_lock = (
            state_mod.load_pack_lock(project_lock_path)
            if project_lock_path.exists()
            else state_mod.empty_pack_lock()
        )
    except state_mod.StateError as exc:
        return UninstallOutcome(
            status=STATUS_MALFORMED,
            details=[f"pack-lock: {exc}"],
        )

    try:
        project_state = state_mod.load_project_state(project_state_path)
    except state_mod.StateError as exc:
        return UninstallOutcome(
            status=STATUS_MALFORMED,
            details=[f"project state: {exc}"],
        )

    try:
        user_state = state_mod.load_user_state(user_state_path)
    except state_mod.StateError as exc:
        # Tolerate malformed user-state on the uninstall path — we may
        # still be able to clean project-local outputs. Report as
        # partial-cleanup at the end if true cleanup is blocked.
        user_state = state_mod.empty_user_state()
        user_state_malformed = str(exc)
    else:
        user_state_malformed: str | None = None

    packs = pack_lock.get("packs", {})
    if not packs and not project_state.get("entries"):
        return UninstallOutcome(status=STATUS_NO_OP)

    outcome = UninstallOutcome(status=STATUS_CLEAN)
    # Tracks whether we touched any permission entry in this run.
    # True → outcome must surface as PARTIAL since we cannot yet
    # JSON-unmerge the permission value out of settings.json.
    permission_owner_decremented = False

    # Walk each pack's file records; delete / decrement as appropriate.
    for pack_name, pack_entry in list(packs.items()):
        files = pack_entry.get("files", [])
        for file_record in files:
            scope = file_record.get("output_scope")
            role = file_record.get("role")
            output_paths = file_record.get("output_paths", [])
            input_sha = file_record.get("input_sha256")
            output_sha = file_record.get("output_sha256")  # generated-command
            expected_sha = output_sha or input_sha
            for out_path in output_paths:
                if scope == "project-local":
                    _delete_project_local(
                        out_path, expected_sha, project_root, outcome
                    )
                elif scope == "user-level":
                    if role == "active-permission":
                        # Permission entries use composite target_path
                        # keys in user-state ("<settings.json>#<merge_key>#<value>")
                        # while pack-lock records only the file path.
                        # Decrement owners for every matching entry.
                        if _decrement_user_level_permissions(
                            out_path, file_record, user_state, repo_id, outcome
                        ):
                            permission_owner_decremented = True
                    else:
                        _decrement_user_level(
                            out_path, file_record, user_state, repo_id, outcome
                        )
                else:
                    outcome.drift_paths.append(out_path)
                    outcome.details.append(
                        f"pack {pack_name!r} file {out_path!r} has unknown "
                        f"output_scope {scope!r}"
                    )

        outcome.packs_removed.append(pack_name)

    # Drift is fail-closed per pack-architecture.md:451,467 — if any
    # drift was observed during the walk, do NOT rewrite state files.
    # State stays intact so the user can retry after manually resolving
    # the drifted file. Otherwise we'd strand the drifted output with
    # no ownership record left to drive a safe re-attempt.
    if outcome.drift_paths:
        outcome.status = STATUS_DRIFT
        return outcome

    # Write back state files reflecting removals.
    try:
        # Empty pack-lock + project-state (we removed this repo's record
        # of everything).
        if project_lock_path.exists():
            state_mod.save_pack_lock(
                project_lock_path, state_mod.empty_pack_lock()
            )
        if project_state_path.exists():
            state_mod.save_project_state(
                project_state_path, state_mod.empty_project_state()
            )
        # User state: only save if still non-empty (empty-owners entries
        # are pruned; file may end up empty and can be removed).
        if user_state.get("entries"):
            state_mod.save_user_state(user_state_path, user_state)
        elif user_state_path.exists() and user_state_malformed is None:
            # No remaining entries — remove the file.
            try:
                user_state_path.unlink()
            except OSError:
                pass
    except state_mod.StateError as exc:
        outcome.details.append(f"state write failed: {exc}")
        outcome.status = STATUS_PARTIAL
        return outcome

    # Promote status based on what we saw (drift was already handled above).
    if user_state_malformed:
        outcome.status = STATUS_PARTIAL
        outcome.details.append(
            f"user state partially usable: {user_state_malformed}"
        )
    elif permission_owner_decremented:
        # Owners are decremented but JSON unmerge of the permission
        # value from settings.json is not yet implemented. Surface as
        # PARTIAL so the caller knows the logical "remove permission X"
        # step is incomplete; a follow-up release implements the JSON
        # unmerge path.
        outcome.status = STATUS_PARTIAL
        outcome.details.append(
            "permission owners decremented; JSON unmerge from "
            "settings.json is not implemented in this release"
        )
    elif not outcome.packs_removed and not outcome.files_deleted:
        outcome.status = STATUS_NO_OP

    return outcome


def _delete_project_local(
    out_path: str,
    expected_sha: str | None,
    project_root: Path,
    outcome: UninstallOutcome,
) -> None:
    """Delete a project-local output (skill dir or file) if on-disk
    content still matches the recorded hash; else report drift."""
    target = (project_root / out_path).resolve()
    if not target.exists():
        # Already gone — nothing to do; not drift.
        return

    if target.is_dir():
        # Directory outputs: verify hash via merkle (dir-sha256:...).
        if expected_sha and expected_sha.startswith("dir-sha256:"):
            actual = _dir_sha256(target)
            if actual != expected_sha:
                outcome.drift_paths.append(out_path)
                outcome.details.append(
                    f"drift: directory {out_path!r} no longer matches "
                    f"recorded dir-sha256"
                )
                return
        shutil.rmtree(target, ignore_errors=False)
    else:
        if expected_sha:
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected_sha:
                outcome.drift_paths.append(out_path)
                outcome.details.append(
                    f"drift: file {out_path!r} content no longer matches "
                    f"recorded sha256"
                )
                return
        target.unlink()

    outcome.files_deleted.append(out_path)


def _decrement_user_level(
    out_path: str,
    file_record: dict[str, Any],
    user_state: dict[str, Any],
    repo_id: str,
    outcome: UninstallOutcome,
) -> None:
    """Remove this repo's record from the user-level entry at ``out_path``;
    physical-delete only when the owners list becomes empty AND the
    on-disk content still matches the recorded hash."""
    entries = user_state.get("entries", [])
    entry = None
    for e in entries:
        if e.get("target_path") == out_path:
            entry = e
            break
    if entry is None:
        # No user-state entry for this output — already cleaned by another
        # pass. Not drift; not counted as a decrement.
        return

    owners = entry.get("owners", [])
    before = len(owners)
    owners = [o for o in owners if o.get("repo_id") != repo_id]
    entry["owners"] = owners
    outcome.owners_decremented.append(out_path)

    if before > 0 and not owners:
        # This repo was the last owner; check drift and delete.
        target = Path(out_path)
        expected = entry.get("expected_sha256_or_json")
        if target.exists():
            if isinstance(expected, str):
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
                if actual != expected:
                    outcome.drift_paths.append(out_path)
                    outcome.details.append(
                        f"drift: user-level file {out_path!r} no longer "
                        "matches recorded sha256"
                    )
                    # Put the owner back — we're not cleaning this.
                    entry["owners"] = list(owners) + [
                        {"repo_id": repo_id, **{
                            k: file_record.get(k, "") for k in
                            ("pack", "requested_ref", "resolved_commit")
                        }, "expected_sha256_or_json": expected}
                    ]
                    return
            # dict-expected (JSON-value): skip content-delete since
            # settings.json may contain many entries; Phase 5+ will
            # do a proper JSON-merge-unmerge.
            try:
                target.unlink()
                outcome.files_deleted.append(out_path)
            except OSError as exc:
                outcome.details.append(
                    f"cannot delete user-level {out_path!r}: {exc}"
                )

    # Prune entries with no remaining owners so the file won't persist
    # a zombie record.
    user_state["entries"] = [e for e in entries if e.get("owners")]


# ======================================================================
# v0.5.2: single-pack uninstall (run_uninstall_pack)
# ======================================================================
#
# Used by ``pack remove <name>`` to remove a single pack while leaving
# every other pack's outputs and state intact. Differs from
# :func:`run_uninstall_all` in three ways:
#
# 1. Scoped to one pack name: only that pack's lock entry is removed,
#    only that pack's project-state entries are dropped, and only
#    user-state owners with composite key ``(repo_id, name)`` are pruned.
#    Other packs' state survives untouched.
# 2. Computes remaining-output-claims from the OTHER lock entries before
#    deleting any project-local output, so a path shared between two
#    packs (rare but possible — e.g., two packs both producing
#    ``.claude/skills/x/``) is retained as long as another pack still
#    claims it.
# 3. User-level deletion rule splits file-like outputs (active hooks,
#    copied skill files: empty-owners-AND-hash-match → delete; remaining
#    owner → retain on disk + save filtered owners) from active-permission
#    entries (only prune state-side owners; NEVER touch settings.json
#    itself — JSON unmerge stays out of v0.5.2 scope).


def run_uninstall_pack(
    project_root: Path,
    name: str,
    *,
    user_home: Path | None = None,
    repo_id: str | None = None,
    lock_timeout: float = 30.0,
) -> UninstallOutcome:
    """Remove a single pack's outputs from the consumer project.

    Parameters mirror :func:`run_uninstall_all` plus ``name``: the pack
    to remove. ``no-op`` is returned when ``name`` is not in the lock
    (so a duplicate ``pack remove`` after the first invocation is
    idempotent rather than an error).

    Drift handling is identical to ``run_uninstall_all``: any owned
    output whose on-disk content no longer matches the recorded hash
    leaves the pack's lock entry and ownership records intact for retry.
    """
    project_root = project_root.resolve()
    if user_home is None:
        user_home = Path.home()
    if repo_id is None:
        repo_id = str(project_root)

    project_lock_path = project_root / ".agent-config" / "pack-lock.json"
    project_state_path = project_root / ".agent-config" / "pack-state.json"
    user_state_path = user_home / ".claude" / "pack-state.json"

    # No-op: nothing recorded.
    if not project_lock_path.exists() and not project_state_path.exists():
        return UninstallOutcome(status=STATUS_NO_OP)

    # Acquire locks before touching any state.
    user_lock = locks.user_lock_path(user_home)
    repo_lock = locks.repo_lock_path(project_root)

    try:
        with locks.acquire(user_lock, timeout=lock_timeout) as _user_h:
            with locks.acquire(repo_lock, timeout=lock_timeout) as _repo_h:
                return _uninstall_pack_under_locks(
                    project_root=project_root,
                    user_home=user_home,
                    repo_id=repo_id,
                    name=name,
                    project_lock_path=project_lock_path,
                    project_state_path=project_state_path,
                    user_state_path=user_state_path,
                )
    except locks.LockTimeout as exc:
        return UninstallOutcome(
            status=STATUS_LOCK_TIMEOUT,
            details=[str(exc)],
            lock_holder_pid=exc.holder_pid,
        )


def _uninstall_pack_under_locks(
    *,
    project_root: Path,
    user_home: Path,
    repo_id: str,
    name: str,
    project_lock_path: Path,
    project_state_path: Path,
    user_state_path: Path,
) -> UninstallOutcome:
    """Body of single-pack uninstall; caller holds per-user + per-repo locks."""
    # Load state files with malformed-state gating (mirrors run_uninstall_all).
    try:
        pack_lock = (
            state_mod.load_pack_lock(project_lock_path)
            if project_lock_path.exists()
            else state_mod.empty_pack_lock()
        )
    except state_mod.StateError as exc:
        return UninstallOutcome(
            status=STATUS_MALFORMED,
            details=[f"pack-lock: {exc}"],
        )
    try:
        project_state = state_mod.load_project_state(project_state_path)
    except state_mod.StateError as exc:
        return UninstallOutcome(
            status=STATUS_MALFORMED,
            details=[f"project state: {exc}"],
        )
    try:
        user_state = state_mod.load_user_state(user_state_path)
    except state_mod.StateError as exc:
        user_state = state_mod.empty_user_state()
        user_state_malformed: str | None = str(exc)
    else:
        user_state_malformed = None

    packs = pack_lock.get("packs", {})
    pack_entry = packs.get(name)
    if pack_entry is None:
        # Not in the lock — already removed (or never installed).
        return UninstallOutcome(status=STATUS_NO_OP)

    # Compute the set of project-local output paths claimed by OTHER
    # packs in the lock so a shared output is retained when another
    # claimant remains. Active-permission user-level outputs are not
    # included here: their owners list is the canonical claim, not the
    # lock entry.
    other_claims: set[str] = set()
    for other_name, other_entry in packs.items():
        if other_name == name:
            continue
        for file_record in other_entry.get("files", []) or []:
            scope = file_record.get("output_scope")
            for out_path in file_record.get("output_paths", []) or []:
                if scope == "project-local":
                    other_claims.add(out_path)

    outcome = UninstallOutcome(status=STATUS_CLEAN)
    permission_owner_decremented = False
    retained_shared: list[str] = []

    # Walk this pack's file records.
    for file_record in pack_entry.get("files", []) or []:
        scope = file_record.get("output_scope")
        role = file_record.get("role")
        output_paths = file_record.get("output_paths", []) or []
        input_sha = file_record.get("input_sha256")
        output_sha = file_record.get("output_sha256")  # generated-command
        expected_sha = output_sha or input_sha
        for out_path in output_paths:
            if scope == "project-local":
                if out_path in other_claims:
                    retained_shared.append(out_path)
                    continue
                _delete_project_local(
                    out_path, expected_sha, project_root, outcome
                )
            elif scope == "user-level":
                if role == "active-permission":
                    if _decrement_user_level_permissions_composite(
                        out_path, user_state, repo_id, name, outcome
                    ):
                        permission_owner_decremented = True
                else:
                    _decrement_user_level_filelike(
                        out_path,
                        file_record,
                        user_state,
                        repo_id,
                        name,
                        outcome,
                    )
            else:
                outcome.drift_paths.append(out_path)
                outcome.details.append(
                    f"pack {name!r} file {out_path!r} has unknown "
                    f"output_scope {scope!r}"
                )

    # Drift on owned outputs: leave the target's lock + state ownership
    # records intact so a retry has the data needed to finish (per plan
    # § 3 step 4 last bullet).
    if outcome.drift_paths:
        outcome.status = STATUS_DRIFT
        if retained_shared:
            outcome.details.append(
                f"retained {len(retained_shared)} shared output(s) still "
                f"claimed by another pack: {', '.join(retained_shared)}"
            )
        return outcome

    outcome.packs_removed.append(name)

    # Remove only this pack's lock entry (single-key delete; preserves
    # other packs' lock entries).
    packs.pop(name, None)
    pack_lock["packs"] = packs

    # Filter project-state entries: keep entries that don't belong to
    # ``name``. Composite key here is just ``pack`` because project-state
    # entries are scoped to this repo by default (the file lives at
    # ``<root>/.agent-config/pack-state.json``).
    project_state["entries"] = [
        e for e in project_state.get("entries", []) or []
        if e.get("pack") != name
    ]

    # Save back state files.
    try:
        state_mod.save_pack_lock(project_lock_path, pack_lock)
        state_mod.save_project_state(project_state_path, project_state)
        if user_state.get("entries"):
            state_mod.save_user_state(user_state_path, user_state)
        elif user_state_path.exists() and user_state_malformed is None:
            try:
                user_state_path.unlink()
            except OSError:
                pass
    except state_mod.StateError as exc:
        outcome.details.append(f"state write failed: {exc}")
        outcome.status = STATUS_PARTIAL
        return outcome

    if retained_shared:
        outcome.details.append(
            f"retained {len(retained_shared)} shared output(s) still "
            f"claimed by another pack: {', '.join(retained_shared)}"
        )

    if user_state_malformed:
        outcome.status = STATUS_PARTIAL
        outcome.details.append(
            f"user state partially usable: {user_state_malformed}"
        )
    elif permission_owner_decremented:
        # Same caveat as run_uninstall_all: JSON unmerge from
        # settings.json stays out of v0.5.2 scope. Owners are pruned;
        # the residual permission entry in settings.json is expected
        # behavior for v0.5.2.
        outcome.status = STATUS_PARTIAL
        outcome.details.append(
            "permission owners decremented; settings.json JSON unmerge "
            "is not implemented in this release"
        )

    return outcome


def _decrement_user_level_filelike(
    out_path: str,
    file_record: dict[str, Any],
    user_state: dict[str, Any],
    repo_id: str,
    pack_name: str,
    outcome: UninstallOutcome,
) -> None:
    """File-like user-level output (active-hook, copied skill file).

    Composite-key owner filter ``(repo_id, pack_name)``; if the owners
    list becomes empty AND the recorded sha256 still matches the
    on-disk file, delete the physical file. If any owner remains,
    retain the file on disk and save the filtered owners list.

    Drift handling: a hash mismatch when the owners list goes empty
    leaves the file on disk and the (just-removed) owner reinstated so
    a retry sees the original state.
    """
    entries = user_state.get("entries", [])
    entry = None
    for e in entries:
        if e.get("target_path") == out_path:
            entry = e
            break
    if entry is None:
        return

    owners = entry.get("owners", [])
    before = len(owners)
    filtered = [
        o for o in owners
        if not (
            o.get("repo_id") == repo_id and o.get("pack") == pack_name
        )
    ]
    entry["owners"] = filtered
    if len(filtered) != before:
        outcome.owners_decremented.append(out_path)

    if before > 0 and not filtered:
        # Last owner gone; check drift and delete.
        target = Path(out_path)
        expected = entry.get("expected_sha256_or_json")
        if target.exists():
            if isinstance(expected, str):
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
                if actual != expected:
                    outcome.drift_paths.append(out_path)
                    outcome.details.append(
                        f"drift: user-level file {out_path!r} no longer "
                        "matches recorded sha256"
                    )
                    # Reinstate the owner for retry.
                    reinstated_owner = {
                        "repo_id": repo_id,
                        "pack": pack_name,
                        "requested_ref": file_record.get("requested_ref")
                        or file_record.get("source_ref")
                        or "",
                        "resolved_commit": file_record.get("resolved_commit")
                        or "",
                        "expected_sha256_or_json": expected,
                    }
                    entry["owners"] = filtered + [reinstated_owner]
                    return
            try:
                target.unlink()
                outcome.files_deleted.append(out_path)
            except OSError as exc:
                outcome.details.append(
                    f"cannot delete user-level {out_path!r}: {exc}"
                )

    # Prune entries whose owners list went empty so they don't persist
    # as zombies. Other entries are preserved.
    user_state["entries"] = [e for e in entries if e.get("owners")]


def _decrement_user_level_permissions_composite(
    out_path: str,
    user_state: dict[str, Any],
    repo_id: str,
    pack_name: str,
    outcome: UninstallOutcome,
) -> bool:
    """Active-permission user-level entries with composite-key filter.

    Mirrors :func:`_decrement_user_level_permissions` but filters owners
    by ``(repo_id, pack_name)`` instead of ``repo_id`` alone, so removing
    one pack from a repo that has another pack contributing different
    permission keys leaves the other pack's owners intact. NEVER touches
    ``~/.claude/settings.json`` itself — JSON unmerge is out of v0.5.2
    scope. Returns True when any entry was modified.
    """
    prefix = out_path + "#"
    entries = user_state.get("entries", [])
    touched = False
    for entry in entries:
        if entry.get("kind") != "active-permission":
            continue
        if not entry.get("target_path", "").startswith(prefix):
            continue
        owners = entry.get("owners", [])
        before = len(owners)
        entry["owners"] = [
            o for o in owners
            if not (
                o.get("repo_id") == repo_id and o.get("pack") == pack_name
            )
        ]
        if len(entry["owners"]) != before:
            touched = True
            outcome.owners_decremented.append(entry.get("target_path", ""))
    user_state["entries"] = [e for e in entries if e.get("owners")]
    return touched


def _decrement_user_level_permissions(
    out_path: str,
    file_record: dict[str, Any],
    user_state: dict[str, Any],
    repo_id: str,
    outcome: UninstallOutcome,
) -> bool:
    """Decrement this repo's owner record from every active-permission
    user-state entry whose composite ``target_path`` starts with
    ``out_path + "#"``.

    Permissions key user-state as ``"<abs_settings_path>#<merge_key>#<value>"``
    but pack-lock only records the settings.json path itself. Prefix-
    matching is the only way to find the actual entries this pack owns
    without re-deriving the merge_key + value.

    Returns True when at least one entry was decremented, so the outer
    flow can surface the "permission owners decremented but JSON
    unmerge not done" partial-cleanup status.
    """
    prefix = out_path + "#"
    entries = user_state.get("entries", [])
    touched = False
    for entry in entries:
        if entry.get("kind") != "active-permission":
            continue
        if not entry.get("target_path", "").startswith(prefix):
            continue
        owners = entry.get("owners", [])
        before = len(owners)
        entry["owners"] = [o for o in owners if o.get("repo_id") != repo_id]
        if len(entry["owners"]) != before:
            touched = True
            outcome.owners_decremented.append(entry.get("target_path", ""))

    # Prune entries with no remaining owners so they don't persist as
    # zombie records. JSON unmerge against settings.json itself is a
    # future-release concern (flagged in the outer status promotion).
    user_state["entries"] = [e for e in entries if e.get("owners")]
    return touched


def _dir_sha256(path: Path) -> str:
    """Compute the merkle-style dir-sha256 of ``path`` in the same
    format that scripts/packs/handlers/skill.py emits, so the two
    hashes compare directly."""
    hasher = hashlib.sha256()
    entries = sorted(
        (p for p in path.rglob("*") if p.is_file()),
        key=lambda p: str(p.relative_to(path)).replace("\\", "/"),
    )
    for src_file in entries:
        rel = str(src_file.relative_to(path)).replace("\\", "/")
        content = src_file.read_bytes()
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(content)
        hasher.update(b"\0")
    return f"dir-sha256:{hasher.hexdigest()}"
