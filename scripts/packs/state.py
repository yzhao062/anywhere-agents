"""Pack state + lock file readers and writers (v0.4.0 Phase 2).

Three JSON files govern pack lifecycle across two ownership boundaries
(per pack-architecture.md § "Pack lifecycle operations"):

- ``.agent-config/pack-lock.json`` (project-local) — records selected packs
  for this consumer repo: declared source URL, requested ref, resolved
  commit, and sha256 of every input file used in composition. Source of
  truth for "what content is currently installed from each pack".
- ``.agent-config/pack-state.json`` (project-local) — records project-local
  outputs: AGENTS.md begin/end marker blocks, ``.claude/skills/<name>/``
  directories, ``.claude/commands/<name>.md`` pointers. Keyed by output
  path with pack attribution + sha256.
- ``~/.claude/pack-state.json`` (user-level) — records shared user-level
  outputs: hook files under ``~/.claude/hooks/<pack>/`` and permission
  entries merged into ``~/.claude/settings.json``. Each output keyed by
  ``(kind, absolute_target_path)`` with an ``owners:`` list.

This module provides load/save primitives with schema validation + atomic
writes via temp-file + ``os.replace``. It does NOT implement the Phase 3
semantics (which entries match, how to merge owners, when to delete);
those live in the kind handlers and lifecycle operations.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Windows os.replace can fail transiently with PermissionError when the
# target file is held open by another process (AV scanner, IDE indexer,
# Claude Code reading session). A brief retry lets the handle close without
# forcing the caller to re-stage the whole transaction. POSIX rename is
# atomic and doesn't exhibit this failure mode, so the retry is a no-op there.
_IS_WINDOWS = sys.platform == "win32"
_REPLACE_RETRIES = 2
_REPLACE_RETRY_DELAY_SECONDS = 0.1


def _atomic_replace(src: str, dst: str) -> None:
    """``os.replace`` with a small Windows-specific retry on sharing violations."""
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

# Schema version for every pack-lock / pack-state file this module writes.
# Bumps when the on-disk schema changes in a way that requires a migration.
SCHEMA_VERSION = 1

# ----- pack-lock.json schema -----

# Valid values for pack-lock.json's file-entry `role:` field. Matches
# pack-architecture.md § "pack-lock.json schema" exactly; Phase 2 reserves
# all five values even though Phase 1 composition only emits "passive"
# today. Phase 3 fills in the other four as dispatch lands.
VALID_LOCK_ROLES = frozenset(
    {
        "passive",
        "active-hook",
        "active-skill",
        "active-permission",
        "generated-command",
    }
)
VALID_OUTPUT_SCOPES = frozenset({"project-local", "user-level"})
VALID_UPDATE_POLICIES = frozenset({"locked", "auto", "prompt"})

# ----- user-level pack-state.json schema -----

# Valid `kind:` values for user-level state entries. These differ from the
# pack-lock roles because user-level state records the *user-facing* kind
# (hook / permission) of the shared output, not the source-file role.
VALID_USER_STATE_KINDS = frozenset({"active-hook", "active-permission"})


class StateError(ValueError):
    """Raised when a pack-lock or pack-state file fails schema validation.

    Callers should treat a ``StateError`` as "do not trust this file";
    lifecycle operations fall back to their malformed-state exit-code
    path rather than silently overwriting.
    """


# ======================================================================
# Atomic write helper
# ======================================================================


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as JSON to ``path`` via temp file + ``os.replace``.

    Ensures that a crash mid-write leaves either the pre-state or the new
    state on disk, never a partial file. Sorts keys for byte-stable output
    across re-writes with identical content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync may not be supported on every filesystem (e.g., some
                # network mounts); the os.replace atomicity contract still
                # holds without it.
                pass
        _atomic_replace(tmp, str(path))
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise StateError(f"state file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot read state file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError(
            f"state file {path} must be a JSON object at top level "
            f"(got {type(data).__name__})"
        )
    version = data.get("version")
    if version != SCHEMA_VERSION:
        raise StateError(
            f"state file {path}: unsupported version {version!r} "
            f"(expected {SCHEMA_VERSION})"
        )
    return data


# ======================================================================
# pack-lock.json (project-local)
# ======================================================================


def empty_pack_lock() -> dict[str, Any]:
    """Return a fresh, empty pack-lock.json payload."""
    return {"version": SCHEMA_VERSION, "packs": {}}


def load_pack_lock(path: Path) -> dict[str, Any]:
    """Load + validate ``.agent-config/pack-lock.json``.

    Returns the parsed payload. Raises ``StateError`` if missing, malformed,
    wrong version, or fails structural validation.
    """
    data = _load_json(path)
    packs = data.get("packs")
    if not isinstance(packs, dict):
        raise StateError(
            f"pack-lock {path}: 'packs' must be a mapping "
            f"(got {type(packs).__name__})"
        )
    for pack_name, pack_entry in packs.items():
        if not isinstance(pack_entry, dict):
            raise StateError(
                f"pack-lock {path}: packs[{pack_name!r}] must be a mapping"
            )
        _validate_lock_pack_entry(path, pack_name, pack_entry)
    return data


def _validate_lock_pack_entry(
    path: Path, pack_name: str, entry: dict[str, Any]
) -> None:
    for key in ("source_url", "requested_ref", "resolved_commit"):
        if not isinstance(entry.get(key), str):
            raise StateError(
                f"pack-lock {path}: packs[{pack_name!r}] missing or non-string {key!r}"
            )
    policy = entry.get("pack_update_policy", "prompt")
    if policy not in VALID_UPDATE_POLICIES:
        raise StateError(
            f"pack-lock {path}: packs[{pack_name!r}] unknown "
            f"'pack_update_policy' {policy!r}"
        )
    # v0.5.2: optional ``latest_known_head`` + ``fetched_at`` fields. Both
    # are populated by the composer after a successful fetch (head equals
    # ``resolved_commit`` at write time) and updated by ``pack verify``
    # when ``git ls-remote`` reveals upstream movement. Old locks predating
    # v0.5.2 omit both; banner item 7's update detector treats their
    # absence as "no update info" rather than a parse error.
    if "latest_known_head" in entry:
        head = entry["latest_known_head"]
        if not isinstance(head, str) or not head:
            raise StateError(
                f"pack-lock {path}: packs[{pack_name!r}] 'latest_known_head' "
                "must be a non-empty string when present"
            )
    if "fetched_at" in entry:
        fetched = entry["fetched_at"]
        if not isinstance(fetched, str) or not fetched:
            raise StateError(
                f"pack-lock {path}: packs[{pack_name!r}] 'fetched_at' "
                "must be a non-empty ISO-8601 string when present"
            )
    files = entry.get("files", [])
    if not isinstance(files, list):
        raise StateError(
            f"pack-lock {path}: packs[{pack_name!r}] 'files' must be a list"
        )
    for idx, file_entry in enumerate(files):
        _validate_lock_file_entry(path, pack_name, idx, file_entry)


def _validate_lock_file_entry(
    path: Path, pack_name: str, idx: int, entry: Any
) -> None:
    if not isinstance(entry, dict):
        raise StateError(
            f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
            f"must be a mapping"
        )
    role = entry.get("role")
    if role not in VALID_LOCK_ROLES:
        raise StateError(
            f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
            f"unknown 'role' {role!r}; expected one of {sorted(VALID_LOCK_ROLES)}"
        )
    scope = entry.get("output_scope")
    if scope not in VALID_OUTPUT_SCOPES:
        raise StateError(
            f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
            f"unknown 'output_scope' {scope!r}"
        )
    policy = entry.get("effective_update_policy")
    if policy not in VALID_UPDATE_POLICIES:
        raise StateError(
            f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
            f"unknown 'effective_update_policy' {policy!r}"
        )
    output_paths = entry.get("output_paths")
    if (
        not isinstance(output_paths, list)
        or not output_paths
        or not all(isinstance(p, str) and p for p in output_paths)
    ):
        raise StateError(
            f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
            "'output_paths' must be a non-empty list of non-empty strings"
        )

    # Schema per pack-architecture.md:223-304:
    #   role                |  host         | source_path   | input_sha256
    #   passive             |  null         | str           | str
    #   active-hook/skill/  |  str          | str           | str
    #   permission          |               |               |
    #   generated-command   |  str          | null          | null
    if "host" not in entry:
        raise StateError(
            f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
            "missing required 'host' field"
        )
    host = entry["host"]
    if role == "passive":
        if host is not None:
            raise StateError(
                f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
                f"passive entries must have 'host': null (got {host!r})"
            )
    else:
        if not isinstance(host, str) or not host:
            raise StateError(
                f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
                f"role {role!r} must have 'host' as a non-empty string"
            )

    if "source_path" not in entry:
        raise StateError(
            f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
            "missing required 'source_path' field"
        )
    source_path = entry["source_path"]
    if role == "generated-command":
        if source_path is not None:
            raise StateError(
                f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
                f"generated-command must have 'source_path': null (got {source_path!r})"
            )
    else:
        if not isinstance(source_path, str) or not source_path:
            raise StateError(
                f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
                f"role {role!r} must have 'source_path' as a non-empty string"
            )

    if "input_sha256" not in entry:
        raise StateError(
            f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
            "missing required 'input_sha256' field"
        )
    input_sha256 = entry["input_sha256"]
    if role == "generated-command":
        if input_sha256 is not None:
            raise StateError(
                f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
                f"generated-command must have 'input_sha256': null"
            )
    else:
        if not isinstance(input_sha256, str) or not input_sha256:
            raise StateError(
                f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
                f"role {role!r} must have 'input_sha256' as a non-empty string"
            )

    # generated-command entries carry extra attribution fields for
    # re-generation drift detection. The other four roles must NOT carry
    # them (per pack-architecture.md); enforce that to catch writer bugs.
    gc_fields = (
        "generated_from",
        "source_input_sha256",
        "template_sha256",
        "output_sha256",
    )
    if role == "generated-command":
        for key in gc_fields:
            if not isinstance(entry.get(key), str):
                raise StateError(
                    f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
                    f"generated-command entry missing {key!r}"
                )
    else:
        for key in gc_fields:
            if key in entry:
                raise StateError(
                    f"pack-lock {path}: packs[{pack_name!r}].files[{idx}] "
                    f"role {role!r} must not carry {key!r}; field is "
                    "generated-command only"
                )


def save_pack_lock(path: Path, data: dict[str, Any]) -> None:
    """Validate + atomically write ``pack-lock.json`` to ``path``."""
    # Re-validate via load-style checks to catch writer bugs early.
    if data.get("version") != SCHEMA_VERSION:
        raise StateError(
            f"refusing to write pack-lock with version "
            f"{data.get('version')!r} (expected {SCHEMA_VERSION})"
        )
    packs = data.get("packs")
    if not isinstance(packs, dict):
        raise StateError("refusing to write pack-lock: 'packs' must be a mapping")
    for pack_name, pack_entry in packs.items():
        if not isinstance(pack_entry, dict):
            raise StateError(
                f"refusing to write pack-lock: packs[{pack_name!r}] "
                "must be a mapping"
            )
        _validate_lock_pack_entry(path, pack_name, pack_entry)
    _atomic_write_json(path, data)


# ======================================================================
# project-local pack-state.json
# ======================================================================


def empty_project_state() -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "entries": []}


def load_project_state(path: Path) -> dict[str, Any]:
    """Load + validate ``.agent-config/pack-state.json``.

    Tolerates absence by returning an empty payload (a pre-install repo
    has no state yet; callers must use ``save_project_state`` to persist
    an empty file if they need one on disk).
    """
    if not path.exists():
        return empty_project_state()
    data = _load_json(path)
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise StateError(
            f"project state {path}: 'entries' must be a list "
            f"(got {type(entries).__name__})"
        )
    for idx, entry in enumerate(entries):
        _validate_project_state_entry(path, idx, entry)
    return data


def _validate_project_state_entry(
    path: Path, idx: int, entry: Any
) -> None:
    if not isinstance(entry, dict):
        raise StateError(
            f"project state {path}: entries[{idx}] must be a mapping"
        )
    for key in ("pack", "output_path", "sha256"):
        if not isinstance(entry.get(key), str):
            raise StateError(
                f"project state {path}: entries[{idx}] missing or "
                f"non-string {key!r}"
            )


def save_project_state(path: Path, data: dict[str, Any]) -> None:
    """Validate + atomically write project-local ``pack-state.json``."""
    if data.get("version") != SCHEMA_VERSION:
        raise StateError(
            f"refusing to write project state with version "
            f"{data.get('version')!r} (expected {SCHEMA_VERSION})"
        )
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise StateError(
            "refusing to write project state: 'entries' must be a list"
        )
    for idx, entry in enumerate(entries):
        _validate_project_state_entry(path, idx, entry)
    _atomic_write_json(path, data)


# ======================================================================
# user-level pack-state.json (with owners: list)
# ======================================================================


def empty_user_state() -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "entries": []}


def load_user_state(path: Path) -> dict[str, Any]:
    """Load + validate ``~/.claude/pack-state.json``.

    Tolerates absence (returns empty payload) since a fresh user without
    any active-kind pack installed has no file yet. Load-side validation
    checks *structure* only; empty-owners entries are tolerated so that
    reconciliation / uninstall code can read a degraded state file to
    clean it up, rather than failing the entire bootstrap flow on a
    malformed entry produced by an older aa version or manual editing
    (save-side validation remains strict).
    """
    if not path.exists():
        return empty_user_state()
    data = _load_json(path)
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise StateError(
            f"user state {path}: 'entries' must be a list "
            f"(got {type(entries).__name__})"
        )
    for idx, entry in enumerate(entries):
        _validate_user_state_entry(path, idx, entry, strict=False)
    return data


def _validate_user_state_entry(
    path: Path, idx: int, entry: Any, *, strict: bool
) -> None:
    """Validate a user-state entry.

    ``strict=True`` (save path) additionally rejects empty owners lists;
    an entry with no owners should be deleted, not persisted. ``strict=False``
    (load path) tolerates empty owners so cleanup code can still read the
    state file.
    """
    if not isinstance(entry, dict):
        raise StateError(
            f"user state {path}: entries[{idx}] must be a mapping"
        )
    kind = entry.get("kind")
    if kind not in VALID_USER_STATE_KINDS:
        raise StateError(
            f"user state {path}: entries[{idx}] unknown 'kind' {kind!r}; "
            f"expected one of {sorted(VALID_USER_STATE_KINDS)}"
        )
    target = entry.get("target_path")
    if not isinstance(target, str) or not target:
        raise StateError(
            f"user state {path}: entries[{idx}] missing or empty 'target_path'"
        )
    # expected_sha256_or_json is a string for hook bodies, an object for
    # permission JSON merges. Either is acceptable; reject other types.
    expected = entry.get("expected_sha256_or_json")
    if not isinstance(expected, (str, dict)):
        raise StateError(
            f"user state {path}: entries[{idx}] 'expected_sha256_or_json' "
            f"must be string (hook sha256) or object (permission JSON value)"
        )
    owners = entry.get("owners")
    if not isinstance(owners, list):
        raise StateError(
            f"user state {path}: entries[{idx}] 'owners' must be a list "
            f"(got {type(owners).__name__})"
        )
    if strict and not owners:
        raise StateError(
            f"user state {path}: entries[{idx}] 'owners' must be a non-empty "
            "list (an entry with no owners should be deleted from state, "
            "not persisted)"
        )
    for o_idx, owner in enumerate(owners):
        _validate_user_state_owner(path, idx, o_idx, owner)


def _validate_user_state_owner(
    path: Path, e_idx: int, o_idx: int, owner: Any
) -> None:
    if not isinstance(owner, dict):
        raise StateError(
            f"user state {path}: entries[{e_idx}].owners[{o_idx}] "
            "must be a mapping"
        )
    for key in ("repo_id", "pack", "requested_ref", "resolved_commit"):
        if not isinstance(owner.get(key), str):
            raise StateError(
                f"user state {path}: entries[{e_idx}].owners[{o_idx}] "
                f"missing or non-string {key!r}"
            )
    expected = owner.get("expected_sha256_or_json")
    if not isinstance(expected, (str, dict)):
        raise StateError(
            f"user state {path}: entries[{e_idx}].owners[{o_idx}] "
            "'expected_sha256_or_json' must be string or object"
        )


class UserLevelOutputConflict(StateError):
    """Raised when a second repo claims the same logical user-level output
    with different expected content than an existing owner.

    Per pack-architecture.md § "Same-path / different-content conflict",
    composition fails closed with this error; no file is overwritten, no
    ``owners:`` merge is attempted, and the installing repo receives no
    partial install for the conflicting entry. The error surfaces the
    existing owners + refs so the user can see who already owns it.
    """

    def __init__(
        self,
        target_path: str,
        existing_owners: list[dict[str, Any]],
        requested_ref: str,
        requested_content: Any,
    ) -> None:
        self.target_path = target_path
        self.existing_owners = existing_owners
        self.requested_ref = requested_ref
        self.requested_content = requested_content
        summary = ", ".join(
            f"{o['pack']}@{o.get('requested_ref', '?')} from {o.get('repo_id', '?')}"
            for o in existing_owners
        )
        super().__init__(
            f"user-level-output-conflict at {target_path}: existing owners "
            f"[{summary}] claim different expected content than the "
            f"incoming request at ref {requested_ref!r}"
        )


def upsert_user_state_entry(
    user_state: dict[str, Any],
    *,
    kind: str,
    target_path: str,
    expected_sha256_or_json: Any,
    owner: dict[str, Any],
) -> str:
    """Merge ``owner`` into the user-level state entry at ``target_path``.

    Behavior (mirrors pack-architecture.md:406-408 ownership contract):

    - If no entry exists for ``(kind, target_path)``: create one with
      ``owners = [owner]``. Returns ``"created"``.
    - If an entry exists with matching ``expected_sha256_or_json``:
      add ``owner`` to the owners list if not already present (matched
      by ``repo_id`` + ``pack``); no change to on-disk content expected.
      Returns ``"joined"`` on new owner added, ``"already-owned"`` on
      duplicate.
    - If an entry exists with different ``expected_sha256_or_json``:
      raise ``UserLevelOutputConflict``. No mutation.

    The caller is responsible for also updating pack-lock + staging the
    on-disk content (hook file or settings.json merge); this function
    only touches the in-memory user-state payload.
    """
    if kind not in VALID_USER_STATE_KINDS:
        raise StateError(
            f"upsert_user_state_entry: unknown kind {kind!r}; "
            f"expected one of {sorted(VALID_USER_STATE_KINDS)}"
        )
    entries = user_state.setdefault("entries", [])
    for entry in entries:
        if entry.get("kind") != kind or entry.get("target_path") != target_path:
            continue
        # Found an existing entry at this (kind, path). Compare content.
        existing_content = entry.get("expected_sha256_or_json")
        if existing_content != expected_sha256_or_json:
            raise UserLevelOutputConflict(
                target_path=target_path,
                existing_owners=entry.get("owners", []),
                requested_ref=owner.get("requested_ref", "?"),
                requested_content=expected_sha256_or_json,
            )
        # Matching content: join owners if not already present.
        for existing_owner in entry["owners"]:
            if (
                existing_owner.get("repo_id") == owner.get("repo_id")
                and existing_owner.get("pack") == owner.get("pack")
            ):
                return "already-owned"
        entry["owners"].append(owner)
        return "joined"
    # No existing entry: create.
    entries.append(
        {
            "kind": kind,
            "target_path": target_path,
            "expected_sha256_or_json": expected_sha256_or_json,
            "owners": [owner],
        }
    )
    return "created"


def save_user_state(path: Path, data: dict[str, Any]) -> None:
    """Validate + atomically write user-level ``pack-state.json``.

    Save-side validation is strict: rejects empty owners lists. An entry
    whose owners list has emptied should be deleted from ``entries``, not
    persisted as a zombie (load-side still tolerates such an entry so
    cleanup can read the file; the save-side invariant prevents a writer
    bug from persisting the zombie).
    """
    if data.get("version") != SCHEMA_VERSION:
        raise StateError(
            f"refusing to write user state with version "
            f"{data.get('version')!r} (expected {SCHEMA_VERSION})"
        )
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise StateError(
            "refusing to write user state: 'entries' must be a list"
        )
    for idx, entry in enumerate(entries):
        _validate_user_state_entry(path, idx, entry, strict=True)
    _atomic_write_json(path, data)
