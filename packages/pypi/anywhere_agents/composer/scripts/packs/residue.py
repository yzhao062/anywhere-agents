"""Advance aa-written default-pack ref residue to the bundled default ref.

aa's own auto-reconciliation (``anywhere-agents pack verify --fix --yes``)
copies the then-current bundled default into a minimal row of the shape
``{name, source: {url, ref}}`` in the user-level config
(``packs.config.user_config_path()``) and in the project
``agent-config.yaml``. When a later aa release bumps the bundled default
(``bootstrap/packs.yaml``), those rows keep the old ref. Selections resolve
by name with the most specific layer winning for every field, so a stale
row silently holds the pack at the old ref: the composed ``AGENTS.md`` block
header reports the new default while the body and ``pack-lock.json`` stay on
the old one.

This module is the single home of the residue ledger and of the rewrite that
advances such rows. It is shared by two callers that must agree:

* the composer (``scripts/compose_packs.py``), which every bootstrap runs
  before it resolves selections, and
* the ``anywhere-agents`` CLI, through the vendored copy under
  ``anywhere_agents/packs/`` (``scripts/vendor-packs.py``).

Only aa-written residue moves. A row is residue when it has exactly the
shape reconciliation writes, names the bundled URL, and carries a ref the
ledger lists. A deliberate pin (a ``passive`` / ``active`` / ``update_policy``
key or any other extra key, a ref outside the ledger, a different URL) is
left exactly as written. See pack-architecture.md "Reconciliation-aware BC
guard refinement".
"""
from __future__ import annotations

import copy
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection, Iterable, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


# The ledger of bundled-default refs that aa itself auto-assigned to a minimal
# default row, keyed by pack name. An entry advances to the bundled ref only
# when its ref is listed here; any other minimal default-name ref is a
# deliberate user pin and must survive byte-for-byte. The ledger includes the
# ref that is currently bundled, not only past ones, so that the entry is
# already present when a later bump moves past it.
#
# Every ref that has ever been the bundled default can appear as aa-written
# residue, because reconciliation assigns the then-current bundled ref into the
# minimal row. Each listed ref was the default for a release line (`v0.3.5`
# for aa v0.6.0, `v0.3.6` for aa v0.7.5, and so on). Omitting one silently
# strands the consumers healed during its release line: the aa-written row
# fails the known-residue check, `_has_explicit_default_override` reads the ref
# difference as a deliberate pin, and those projects never reach the fetch,
# marker rewrite, or lock refresh.
#
# The invariant that keeps this list complete is tested rather than
# remembered: the current bundled ref must itself be listed, so each ref is
# added while it is the default and is already here by the time a later bump
# moves away from it. This list was incomplete twice before that test existed.
# `v0.9.0` is listed while it *is* the default. Advancing a row to the ref it
# already holds is a no-op: the rewrite is guarded by `entry_ref != bundled_ref`,
# which short-circuits first.
AUTO_RECONCILED_DEFAULT_REF_REWRITES: dict[str, set[str]] = {
    "agent-style": {"v0.3.2", "v0.3.5", "v0.3.6", "v0.4.1", "v0.5.0", "v0.6.0", "v0.7.0", "v0.8.0", "v0.9.0"},
}

# Project-tracked config file name. ``agent-config.local.yaml`` is hand
# authored and aa never writes it, so it never carries residue.
PROJECT_CONFIG_FILENAME = "agent-config.yaml"

# Upper bound on waiting for the user-config lock. Bootstrap runs this at every
# session start; a ``pack add`` holds the lock for milliseconds, so a longer
# wait means a stuck holder and the rewrite is better skipped than waited on.
LOCK_TIMEOUT_SECONDS = 10.0

# Keys reconciliation writes. A row with any other key carries user intent.
_RESIDUE_ENTRY_KEYS = frozenset({"name", "source", "ref"})
_RESIDUE_SOURCE_KEYS = frozenset({"url", "repo", "ref"})

# (pack name, ref before, ref after)
Advance = tuple[str, str, str]


@dataclass
class FileRewrite:
    """One config file whose residue rows were advanced."""

    path: Path
    advanced: list[Advance] = field(default_factory=list)


@dataclass
class _Plan:
    """What one pass over a config file would change."""

    text: str
    key: str
    new_data: dict[str, Any]
    # (index into the pack list, name, old ref, new ref)
    edits: list[tuple[int, str, str, str]]
    # True when every edit only changes an existing ``source.ref`` scalar, so
    # the file can be patched in place and keep its comments and layout.
    in_place: bool


def user_config_lock_path(user_config_path: Path) -> Path:
    """Sibling lock file for user-config rewrites.

    Serializes ``pack add`` / ``pack remove`` and the residue rewrite against
    each other on one machine. The composer's own user lock
    (``~/.claude/.pack-lock.lock``) guards ``~/.claude`` state and is a
    different file, so a config-only rewrite does not block a composer
    working in another project.
    """
    return user_config_path.with_name(user_config_path.name + ".lock")


def bundled_default_sources(
    bundled_packs: Iterable[Any],
    default_names: Collection[str],
) -> dict[str, tuple[str, str]]:
    """Return ``{name: (url, ref)}`` for the default packs the ledger covers.

    ``bundled_packs`` is the pack list of the bundled manifest as written
    (``source: {repo|url, ref}``, or the legacy top-level ``ref``). Packs
    without a ledger entry, a source URL, or a ref are left out, so a bundled
    pack with no inline source (``aa-core-skills``) is never touched.
    """
    out: dict[str, tuple[str, str]] = {}
    for pack in bundled_packs:
        if not isinstance(pack, dict):
            continue
        name = pack.get("name")
        if (
            not isinstance(name, str)
            or name not in default_names
            or name not in AUTO_RECONCILED_DEFAULT_REF_REWRITES
        ):
            continue
        source = pack.get("source")
        if not isinstance(source, dict):
            continue
        url = _source_url(source)
        ref = source.get("ref") or pack.get("ref")
        if isinstance(url, str) and url and isinstance(ref, str) and ref:
            out[name] = (url, ref)
    return out


def _source_url(source: dict[str, Any]) -> Any:
    """The URL the fetch path would use for ``source``.

    ``compose_packs._process_selection`` reads ``source.url`` first and
    falls back to the legacy ``source.repo`` key. Every residue lookup uses
    the same order: a row that names a fork under ``url`` and the upstream
    under ``repo`` fetches the fork, so it must not pass as upstream residue.
    """
    return source.get("url") or source.get("repo")


def _normalize_url(url: str) -> str:
    # Lazy: the module stays importable where only the ledger is wanted, and
    # the vendoring rewrites this spelling for the installed CLI.
    from scripts.packs import source_fetch

    return source_fetch.normalize_pack_source_url(url)


def _reconciled_shape(
    entry: Any,
    bundled: dict[str, tuple[str, str]],
) -> Optional[tuple[str, str, str]]:
    """Return ``(name, url, ref)`` when ``entry`` has exactly the shape
    reconciliation writes for a covered default pack, else ``None``.

    Shape only: neither the URL nor the ref is compared with the bundled
    default here.
    """
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or name not in bundled:
        return None
    # Anything beyond what reconciliation writes (passive / active /
    # update_policy, hosts, a skills path, ...) is the user's intent.
    if not set(entry) <= _RESIDUE_ENTRY_KEYS:
        return None
    source = entry.get("source")
    if not isinstance(source, dict) or not set(source) <= _RESIDUE_SOURCE_KEYS:
        return None
    entry_url = _source_url(source)
    entry_ref = source.get("ref") or entry.get("ref")
    if not isinstance(entry_url, str) or not isinstance(entry_ref, str):
        return None
    return name, entry_url, entry_ref


def _match(
    entry: Any,
    bundled: dict[str, tuple[str, str]],
) -> Optional[tuple[str, str, str]]:
    """Return ``(name, old_ref, new_ref)`` when ``entry`` is advanceable residue."""
    shape = _reconciled_shape(entry, bundled)
    if shape is None:
        return None
    name, entry_url, entry_ref = shape
    bundled_url, bundled_ref = bundled[name]
    if entry_ref == bundled_ref:
        return None
    if entry_ref not in AUTO_RECONCILED_DEFAULT_REF_REWRITES.get(name, ()):
        return None
    if _normalize_url(entry_url) != _normalize_url(bundled_url):
        return None
    return name, entry_ref, bundled_ref


def is_bundled_default(
    entry: Any,
    bundled: dict[str, tuple[str, str]],
) -> bool:
    """True when ``entry`` is exactly the bundled default row.

    That is the row reconciliation writes (no key beyond ``name`` and
    ``source``, no ``update_policy``, ``passive`` or ``active``), naming the
    bundled URL at the bundled ref. Such a row restates the default instead of
    overriding it, which is what an advanced residue row is: the rewrite above
    leaves precisely this row behind. A different URL, a different ref
    (listed in the ledger or not) or any extra key is the user's own choice,
    so the answer is ``False``.

    ``bundled`` is the ``bundled_default_sources`` mapping.
    """
    shape = _reconciled_shape(entry, bundled)
    if shape is None:
        return False
    name, entry_url, entry_ref = shape
    bundled_url, bundled_ref = bundled[name]
    return entry_ref == bundled_ref and (
        _normalize_url(entry_url) == _normalize_url(bundled_url)
    )


def _read_text(path: Path) -> Optional[str]:
    # newline="" keeps CRLF files CRLF through the in-place patch.
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


def _plan(path: Path, bundled: dict[str, tuple[str, str]]) -> Optional[_Plan]:
    """Read ``path`` and return what would change, or ``None`` when nothing."""
    if yaml is None or not path.is_file():
        return None
    text = _read_text(path)
    if text is None or not text.strip():
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    key = "packs" if isinstance(data.get("packs"), list) else "rule_packs"
    entries = data.get(key)
    if not isinstance(entries, list):
        return None

    # ``deepcopy`` keeps YAML aliases shared inside the copy: a row whose
    # ``source`` is an anchor shared with another row (or a pack list that
    # another key aliases) would move every holder of the anchor when one
    # row is edited, and the reparse check in ``_render`` would then compare
    # against data that already carries the unintended change. Detach the
    # selected list and each edited row so an edit reaches only its own row;
    # a scalar patch that would also move an alias then fails the reparse
    # check and the full dump writes the intended data instead.
    new_data = copy.deepcopy(data)
    new_entries = list(new_data[key])
    new_data[key] = new_entries
    edits: list[tuple[int, str, str, str]] = []
    in_place = True
    for index, entry in enumerate(entries):
        hit = _match(entry, bundled)
        if hit is None:
            continue
        name, old_ref, new_ref = hit
        new_entry = copy.deepcopy(entry)
        new_entries[index] = new_entry
        if "ref" not in new_entry["source"] or "ref" in new_entry:
            # Legacy top-level ref, or a ref on both levels: the rewrite moves
            # it into ``source`` and drops the top-level key.
            in_place = False
        new_entry["source"]["ref"] = new_ref
        new_entry.pop("ref", None)
        edits.append((index, name, old_ref, new_ref))
    if not edits:
        return None
    return _Plan(text, key, new_data, edits, in_place)


def _mapping_get(node: Any, key: str) -> Any:
    """Value node for ``key`` in a YAML mapping node; the last duplicate wins,
    as it does in ``safe_load``."""
    found = None
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
                found = value_node
    return found


def _patch_in_place(plan: _Plan) -> Optional[str]:
    """Replace each stale ``source.ref`` scalar in the original text.

    Everything else, comments and layout included, stays byte for byte.
    Returns ``None`` when the layout is not the simple one this handles.
    """
    try:
        root = yaml.compose(plan.text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None
    sequence = _mapping_get(root, plan.key)
    if not isinstance(sequence, yaml.SequenceNode):
        return None
    spans: list[tuple[int, int, str]] = []
    for index, _name, _old_ref, new_ref in plan.edits:
        if index >= len(sequence.value):
            return None
        source = _mapping_get(sequence.value[index], "source")
        ref_node = _mapping_get(source, "ref")
        if not isinstance(ref_node, yaml.ScalarNode):
            return None
        if ref_node.style is None:
            replacement = new_ref
        elif ref_node.style in ("'", '"'):
            replacement = f"{ref_node.style}{new_ref}{ref_node.style}"
        else:
            return None
        spans.append(
            (ref_node.start_mark.index, ref_node.end_mark.index, replacement)
        )
    patched = plan.text
    for start, end, replacement in sorted(spans, reverse=True):
        patched = patched[:start] + replacement + patched[end:]
    return patched


def _render(plan: _Plan) -> str:
    """Return the new file text.

    The in-place patch is used only when it parses back to exactly the
    intended data; anything else (aliases, an unusual scalar style, a ref that
    needs quoting) falls back to a full dump, which keeps every key and entry
    but not comments.
    """
    if plan.in_place:
        patched = _patch_in_place(plan)
        if patched is not None:
            try:
                reparsed = yaml.safe_load(patched)
            except yaml.YAMLError:
                reparsed = None
            if reparsed == plan.new_data:
                return patched
    return yaml.safe_dump(plan.new_data, sort_keys=False, default_flow_style=False)


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` through a temp file in its directory.

    ``os.replace`` makes an interrupted write leave the old file or the new
    one, never a partial one. The temp name is unique so two bootstraps
    rewriting one project file do not interleave.
    """
    target = Path(os.path.realpath(str(path)))
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        try:
            shutil.copymode(str(target), tmp_name)
        except OSError:
            pass
        os.replace(tmp_name, str(target))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _commit(path: Path, plan: _Plan, say: Callable[[str], None]) -> FileRewrite:
    _write_atomic(path, _render(plan))
    rewrite = FileRewrite(
        path, [(name, old, new) for _i, name, old, new in plan.edits]
    )
    say(
        f"advanced stale default pack ref in {path}: "
        + ", ".join(f"{name} {old} -> {new}" for name, old, new in rewrite.advanced)
    )
    return rewrite


def _advance_file(
    path: Path,
    bundled: dict[str, tuple[str, str]],
    lock_path: Optional[Path],
    say: Callable[[str], None],
) -> Optional[FileRewrite]:
    try:
        # A lock-free read first: almost every run finds nothing to do, and
        # taking the lock would create the lock file and its directory for no
        # reason.
        plan = _plan(path, bundled)
        if plan is None:
            return None
        if lock_path is None:
            return _commit(path, plan, say)
        from scripts.packs import locks

        with locks.acquire(lock_path, timeout=LOCK_TIMEOUT_SECONDS):
            # Plan again under the lock: a concurrent ``pack add`` may have
            # rewritten the file since the first read.
            fresh = _plan(path, bundled)
            if fresh is None:
                return None
            return _commit(path, fresh, say)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # LockTimeout is a TimeoutError, so a busy lock lands here as well.
        say(
            f"warning: could not advance stale default pack ref in {path}: "
            f"{exc}; left unchanged"
        )
        return None


def advance_residue(
    *,
    user_config_path: Optional[Path],
    project_root: Optional[Path],
    bundled_packs: Iterable[Any],
    default_names: Collection[str],
    emit: Optional[Callable[[str], None]] = None,
) -> list[FileRewrite]:
    """Advance aa-written residue rows in the user-level and project configs.

    ``user_config_path`` and ``project_root`` may each be ``None`` to skip
    that layer; a missing, unreadable, or unparsable file is skipped quietly
    (the resolver reports a malformed file on its own). The user-level file is
    rewritten under the lock ``pack add`` / ``pack remove`` take. ``emit``
    receives one line per rewritten file and one per failed attempt.

    Idempotent: a second call finds every row at the bundled ref and writes
    nothing. Never raises for a file problem.
    """
    if yaml is None:
        return []
    say = emit if emit is not None else (lambda _message: None)
    bundled = bundled_default_sources(bundled_packs, default_names)
    if not bundled:
        return []

    targets: list[tuple[Path, Optional[Path]]] = []
    if user_config_path is not None:
        user_path = Path(user_config_path)
        targets.append((user_path, user_config_lock_path(user_path)))
    if project_root is not None:
        targets.append((Path(project_root) / PROJECT_CONFIG_FILENAME, None))

    results: list[FileRewrite] = []
    for path, lock_path in targets:
        rewrite = _advance_file(path, bundled, lock_path, say)
        if rewrite is not None:
            results.append(rewrite)
    return results
