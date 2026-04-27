"""Active-entry dispatch for the unified pack composer (v0.4.0 Phase 3).

Every ``active:`` entry in a v2 manifest carries an explicit ``kind:``
field; dispatch routes the entry to the matching handler via the
``KIND_HANDLERS`` registry. Handlers mutate the shared ``DispatchContext``
(transaction, pack-lock, pack-state payloads) rather than writing to
disk directly, so commit / rollback is caller-controlled.

Handler contract::

    def handle_<kind>(entry: dict, ctx: DispatchContext) -> None:
        # Validate entry fields this kind requires beyond schema
        # (schema already validated shape).
        # Stage writes/deletes via ctx.txn.
        # Append file records to ctx.pack_lock entry for this pack.
        # Append project-state / user-state entries as appropriate.

No handler writes state files or commits transactions itself; that is
the composer's job once every pack has been dispatched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import transaction as txn_mod


@dataclass
class DispatchContext:
    """Shared mutable state passed through every active-entry handler.

    Each pack gets its own context (fresh ``pack_name`` / ``pack_source_dir``
    etc.) but the ``pack_lock``, ``project_state``, ``user_state``, and
    ``txn`` are shared across the whole composition so the final commit
    can write all three state files atomically.
    """

    # Pack identity
    pack_name: str
    pack_source_url: str  # e.g. "bundled:aa" or "https://github.com/..."
    pack_requested_ref: str
    pack_resolved_commit: str
    pack_update_policy: str  # "locked" | "auto" | "prompt"
    # In v0.4.0 dispatch read pack_update_policy as one of {locked, auto}.
    # v0.5.0 adds 'prompt' which is resolved by compose BEFORE any apply step;
    # dispatch never sees a pack with policy=prompt + pending drift (compose
    # either applies or defers via pending-updates.json).

    # Pack source tree (where the handler reads content from).
    # For bundled:aa packs this is ``<consumer_root>/.agent-config/repo``;
    # for fetched packs it is the cache directory after fetch.
    pack_source_dir: Path

    # Consumer identity
    project_root: Path  # consumer repo root
    user_home: Path  # typically Path.home(); injectable for tests
    repo_id: str  # stable id for this consumer repo (for owners list)

    # Transaction + state payloads (mutated by handlers)
    txn: txn_mod.Transaction
    pack_lock: dict[str, Any]  # full pack-lock.json payload
    project_state: dict[str, Any]  # full project-local pack-state.json payload
    user_state: dict[str, Any]  # full user-level pack-state.json payload

    # Pack-architecture.md:196 host-mismatch semantics — the current host.
    # v0.4.0 supports only claude-code; exposed here so handlers can enforce
    # the required: true/false branching without hardcoding the check.
    current_host: str = "claude-code"

    # Pack-level hosts default (pack-architecture.md:199). Populated by the
    # composer from the manifest's pack-level ``hosts:`` when present; an
    # entry that omits its own ``hosts:`` falls back to this list before
    # defaulting to the current host. Must be populated even when pack-level
    # hosts is absent (use ``None``) so dispatch can distinguish "no default"
    # from "empty default".
    pack_hosts: list[str] | None = None

    # v0.5.2: optional head + fetched timestamp recorded after a fresh
    # fetch. ``latest_known_head`` equals ``pack_resolved_commit`` at
    # write time (banner item 7's update detector compares the two);
    # ``pack verify`` updates ``latest_known_head`` later when ``git
    # ls-remote`` shows upstream movement. ``None`` for bundled packs.
    pack_latest_known_head: str | None = None
    pack_fetched_at: str | None = None

    # Handler outputs: per-pack file entries collected as handlers run,
    # then copied into pack_lock.packs[pack_name].files after all active
    # entries for this pack have been dispatched.
    _file_entries: list[dict[str, Any]] = field(default_factory=list)

    # In-memory shadow of JSON files being merged during this compose
    # transaction. Each permission-handler call reads from this shadow
    # (not from disk) so multiple values staged into the same target
    # accumulate correctly before the transaction commits.
    _pending_json_targets: dict[str, Any] = field(default_factory=dict)

    def record_lock_file(self, file_entry: dict[str, Any]) -> None:
        """Append a file entry to this pack's pack-lock.json record."""
        self._file_entries.append(file_entry)

    def finalize_pack_lock(self) -> None:
        """Flush staged file entries into pack_lock[packs][pack_name].

        Called once after every active and passive entry for this pack
        has been dispatched, so the lock file reflects the union of file
        outputs from all slots.

        If no file entries accumulated during dispatch (e.g., a pack with
        only ``kind: command`` no-op entries), do NOT create an empty
        pack-lock record — an empty entry would misrepresent what the
        install did and would fail state.py's validation on save
        (``output_paths`` must be non-empty).
        """
        if not self._file_entries:
            return
        packs = self.pack_lock.setdefault("packs", {})
        default_entry: dict[str, Any] = {
            "source_url": self.pack_source_url,
            "requested_ref": self.pack_requested_ref,
            "resolved_commit": self.pack_resolved_commit,
            "pack_update_policy": self.pack_update_policy,
            "files": [],
        }
        # v0.5.2: thread the optional head/fetched_at fields so the lock
        # records what we just fetched. Composer sets both at fetch time
        # (head equals resolved_commit because we just hit the remote).
        if self.pack_latest_known_head is not None:
            default_entry["latest_known_head"] = self.pack_latest_known_head
        if self.pack_fetched_at is not None:
            default_entry["fetched_at"] = self.pack_fetched_at
        pack_entry = packs.setdefault(self.pack_name, default_entry)
        # When the entry already existed (a previous active/passive call
        # set it up), refresh the optional fields so the latest fetch
        # reflects in the lock even if dispatch ran in two passes.
        if self.pack_latest_known_head is not None:
            pack_entry["latest_known_head"] = self.pack_latest_known_head
        if self.pack_fetched_at is not None:
            pack_entry["fetched_at"] = self.pack_fetched_at
        pack_entry["files"].extend(self._file_entries)
        self._file_entries.clear()


# ----- registry -----

HandlerFn = Callable[[dict[str, Any], DispatchContext], None]


# Populated by scripts/packs/handlers/__init__.py on import so this
# module stays import-cycle-free (handlers import DispatchContext from
# here; the registry is filled after both modules are defined).
KIND_HANDLERS: dict[str, HandlerFn] = {}


def register(kind: str, handler: HandlerFn) -> None:
    """Register a handler for ``kind:`` entries.

    Called from handlers/__init__.py at import time; not part of the
    composer's runtime surface. Re-registration replaces the previous
    handler for that kind (used only for test monkey-patching).
    """
    KIND_HANDLERS[kind] = handler


class DispatchError(RuntimeError):
    """Raised when an active entry cannot be dispatched.

    Covers host-mismatch failures with ``required: true`` entries (per
    pack-architecture.md:192-196) and any handler-level errors that
    should abort composition rather than silently skip.
    """


def dispatch_active(entry: dict[str, Any], ctx: DispatchContext) -> None:
    """Route an active entry to its registered handler.

    Host-mismatch check runs here, before the handler sees the entry,
    so ``required: false`` entries skip cleanly and ``required: true``
    entries produce a uniform error regardless of which kind they are.
    Host inheritance from pack-level default has already been resolved
    by the schema parser (entry carries the effective hosts list once
    inherited defaults apply; if entry omits ``hosts:``, a pack-level
    default must have been present).
    """
    kind = entry["kind"]
    handler = KIND_HANDLERS.get(kind)
    if handler is None:
        raise DispatchError(
            f"no handler registered for kind {kind!r}; "
            "did scripts/packs/handlers/__init__.py import complete?"
        )

    effective_hosts = _effective_hosts(entry, ctx)
    if ctx.current_host not in effective_hosts:
        required = bool(entry.get("required", True))
        if required:
            raise DispatchError(
                f"pack {ctx.pack_name!r} active entry requires host "
                f"{effective_hosts!r} but the current host is "
                f"{ctx.current_host!r}: host-mismatch"
            )
        # required=false: skip silently (the caller may log informatively).
        return

    handler(entry, ctx)


def resolve_output_path(
    to_path: str, ctx: DispatchContext
) -> tuple[Path, str]:
    """Resolve a manifest ``to:`` path to an absolute filesystem path and
    report whether it targets project-local or user-level scope.

    - ``~/...`` or ``~\\...`` → relative to ``ctx.user_home``; scope is
      ``user-level``.
    - Absolute path (rare; test fixtures only): returned as-is; scope is
      ``user-level`` by default since absolute manifest paths are a
      user-level-adjacent concern.
    - Otherwise: relative to ``ctx.project_root``; scope is
      ``project-local``.
    """
    if to_path == "~":
        return ctx.user_home, "user-level"
    if to_path.startswith("~/") or to_path.startswith("~\\"):
        return (ctx.user_home / to_path[2:]).resolve(), "user-level"
    p = Path(to_path)
    if p.is_absolute():
        return p.resolve(), "user-level"
    return (ctx.project_root / p).resolve(), "project-local"


def _effective_hosts(
    entry: dict[str, Any], ctx: DispatchContext
) -> list[str]:
    """Return the hosts list that this entry targets.

    Entry-level ``hosts`` overrides pack-level default per
    pack-architecture.md:199. The composer populates ``ctx.pack_hosts``
    from the manifest's pack-level ``hosts:`` (or ``None`` if absent).
    If both are missing the schema parser would already have rejected
    the pack; dispatch defaulting to ``[ctx.current_host]`` is a
    defensive fallback so a required-true entry still fails
    deterministically rather than silently passing.
    """
    hosts = entry.get("hosts")
    if hosts is not None:
        return list(hosts)
    if ctx.pack_hosts is not None:
        return list(ctx.pack_hosts)
    return [ctx.current_host]
