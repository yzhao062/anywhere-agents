"""Manifest schema parser for the unified pack format (v0.4.0).

Accepts two shapes:

- **Legacy (version: 1)**: passive-only rule-pack manifest as shipped in
  v0.3.x. Each pack declares a flat ``source`` URL with a ``{ref}``
  placeholder and a ``default-ref``. This shape continues to parse so that
  v0.3.x consumers upgrading to v0.4.0 see no behavior change.
- **New (version: 2)**: unified manifest. Each pack may declare ``passive:``
  and / or ``active:`` slots. Active entries carry an explicit ``kind:``
  (one of ``skill``, ``hook``, ``permission``, ``command``) and opt into
  ``hosts:`` + ``required:`` semantics.

This module validates structure and fails closed at parse time with a
``ParseError`` for v0.4.0-out-of-scope features:

- Private ``source:`` URLs (SSH, ``git@``, explicit ``auth:`` field) — per
  pack-architecture.md, private sources are a v0.5.0 feature.
- ``update_policy: auto`` on active entries — active code must be explicitly
  reviewed; the manifest author cannot opt into silent refresh.
- Unknown ``kind:`` values.

No network or filesystem side effects happen here; parsing is pure.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover — bootstrap installs pyyaml before import.
    raise

KNOWN_ACTIVE_KINDS = {"skill", "hook", "permission", "command"}
KNOWN_UPDATE_POLICIES = {"locked", "auto"}

# URL prefixes that indicate a private source (require auth). Rejected at
# parse time in v0.4.0 with a clear "v0.5.0 feature" message.
_PRIVATE_URL_PREFIXES = ("git@", "ssh://", "git+ssh://")


class ParseError(ValueError):
    """Manifest schema rejection.

    Raised at parse time, before any network or filesystem activity, so a
    malformed or out-of-scope manifest never reaches the composer's write
    path.
    """


def parse_manifest(path: Path) -> dict[str, Any]:
    """Load and validate a packs.yaml / rule-packs.yaml manifest.

    Returns a dict with this shape:

        {
            "version": 1 | 2,
            "packs": [ <pack_entry>, ... ],
        }

    Each pack entry is the source dict as written in YAML, with one added
    key ``"_legacy": bool`` that is True for version-1 entries. Callers can
    introspect ``_legacy`` to dispatch between the v0.3.x composer and the
    v0.4.0 active-kind dispatch.

    Raises
    ------
    ParseError
        If the file is missing, YAML is malformed, the top-level shape is
        wrong, a pack entry is missing required fields, or the manifest
        contains v0.4.0-out-of-scope features (private sources, unknown
        kinds, ``update_policy: auto`` on active entries).
    """
    if not path.exists():
        raise ParseError(f"manifest not found: {path}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ParseError(f"malformed YAML in manifest {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ParseError(
            f"manifest {path} must be a mapping at top level (got "
            f"{type(data).__name__})"
        )

    version = data.get("version")
    if version not in (1, 2):
        raise ParseError(
            f"manifest {path}: 'version' must be 1 or 2 (got {version!r}). "
            "v0.3.x manifests use version 1; v0.4.0+ manifests use version 2."
        )

    packs_raw = data.get("packs")
    if not isinstance(packs_raw, list):
        raise ParseError(
            f"manifest {path}: 'packs' must be a list (got "
            f"{type(packs_raw).__name__})"
        )

    seen_names: set[str] = set()
    packs: list[dict[str, Any]] = []
    for idx, entry in enumerate(packs_raw):
        if not isinstance(entry, dict):
            raise ParseError(
                f"manifest {path}: packs[{idx}] must be a mapping (got "
                f"{type(entry).__name__})"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ParseError(
                f"manifest {path}: packs[{idx}] missing or empty 'name'"
            )
        if name in seen_names:
            raise ParseError(
                f"manifest {path}: duplicate pack name {name!r}"
            )
        seen_names.add(name)

        if version == 1:
            _validate_v1_pack(path, idx, entry)
            entry["_legacy"] = True
        else:
            _validate_v2_pack(path, idx, entry)
            entry["_legacy"] = False

        packs.append(entry)

    return {"version": version, "packs": packs}


def _validate_v1_pack(path: Path, idx: int, entry: dict[str, Any]) -> None:
    """Validate a v0.3.x passive-only pack entry.

    Required: ``source`` (str URL with ``{ref}``), ``default-ref`` (str).
    Forbidden: ``active:`` key (version-1 manifests predate the active slot).
    """
    source = entry.get("source")
    if not isinstance(source, str) or not source:
        raise ParseError(
            f"manifest {path}: packs[{idx}] ({entry['name']}) 'source' must "
            "be a non-empty string in a version-1 manifest"
        )
    _reject_private_url(path, idx, entry["name"], source)

    default_ref = entry.get("default-ref")
    if not isinstance(default_ref, str) or not default_ref:
        raise ParseError(
            f"manifest {path}: packs[{idx}] ({entry['name']}) missing or "
            "empty 'default-ref' in a version-1 manifest"
        )

    if "active" in entry:
        raise ParseError(
            f"manifest {path}: packs[{idx}] ({entry['name']}) declares "
            "'active:' entries in a version-1 manifest; bump the top-level "
            "'version' to 2 to use active slots."
        )


def _validate_v2_pack(path: Path, idx: int, entry: dict[str, Any]) -> None:
    """Validate a v0.4.0+ unified pack entry.

    Checks source shape, update policy, pack-level ``hosts:`` default,
    passive list, active list (with per-entry ``kind``, ``hosts``,
    ``required``, ``files`` validation; entry-level ``hosts`` overrides the
    pack-level default per pack-architecture.md:199).
    """
    source = entry.get("source")
    if isinstance(source, str):
        _reject_private_url(path, idx, entry["name"], source)
    elif isinstance(source, dict):
        repo = source.get("repo") or source.get("url")
        if not isinstance(repo, str) or not repo:
            raise ParseError(
                f"manifest {path}: packs[{idx}] ({entry['name']}) 'source' "
                "must include a non-empty 'repo' (or legacy 'url') field"
            )
        _reject_private_url(path, idx, entry["name"], repo)
        ref = source.get("ref")
        if not isinstance(ref, str) or not ref:
            raise ParseError(
                f"manifest {path}: packs[{idx}] ({entry['name']}) 'source.ref' "
                "must be a non-empty string (every source resolves to an "
                "immutable commit id before composition; a missing ref leaves "
                "resolution ambiguous)."
            )
        if "auth" in source:
            raise ParseError(
                f"manifest {path}: packs[{idx}] ({entry['name']}) 'source.auth' "
                "requires aa v0.5.0+; remove the field or wait for the auth "
                "chain release."
            )
    elif source is None:
        # A pack may be bundled with aa (no external source); that's valid.
        pass
    else:
        raise ParseError(
            f"manifest {path}: packs[{idx}] ({entry['name']}) 'source' must "
            f"be a string URL or a mapping (got {type(source).__name__})"
        )

    update_policy = entry.get("update_policy", "locked")
    if update_policy not in KNOWN_UPDATE_POLICIES:
        raise ParseError(
            f"manifest {path}: packs[{idx}] ({entry['name']}) unknown "
            f"'update_policy' {update_policy!r}; expected one of "
            f"{sorted(KNOWN_UPDATE_POLICIES)}"
        )

    # Pack-level hosts: optional default that each active entry may override.
    # Per pack-architecture.md:199 — entry-level value wins on conflict; a
    # pack-level value supplies the default when the entry omits its own.
    pack_hosts = entry.get("hosts")
    if pack_hosts is not None:
        if not (
            isinstance(pack_hosts, list)
            and pack_hosts
            and all(isinstance(h, str) and h for h in pack_hosts)
        ):
            raise ParseError(
                f"manifest {path}: packs[{idx}] ({entry['name']}) pack-level "
                "'hosts' must be a non-empty list of non-empty strings"
            )

    passive = entry.get("passive")
    if passive is not None:
        _validate_passive(path, idx, entry["name"], passive)

    active = entry.get("active")
    if active is not None:
        _validate_active(path, idx, entry["name"], active, pack_hosts)


def _validate_passive(
    path: Path, idx: int, pack_name: str, passive: Any
) -> None:
    if not isinstance(passive, list):
        raise ParseError(
            f"manifest {path}: packs[{idx}] ({pack_name}) 'passive' must be "
            f"a list (got {type(passive).__name__})"
        )
    for j, entry in enumerate(passive):
        if not isinstance(entry, dict):
            raise ParseError(
                f"manifest {path}: packs[{idx}].passive[{j}] "
                f"({pack_name}) must be a mapping"
            )
        _validate_files_list(
            path, idx, pack_name, f"passive[{j}]", entry, required=True
        )


def _validate_active(
    path: Path,
    idx: int,
    pack_name: str,
    active: Any,
    pack_hosts: list[str] | None,
) -> None:
    if not isinstance(active, list):
        raise ParseError(
            f"manifest {path}: packs[{idx}] ({pack_name}) 'active' must be "
            f"a list (got {type(active).__name__})"
        )
    for j, entry in enumerate(active):
        if not isinstance(entry, dict):
            raise ParseError(
                f"manifest {path}: packs[{idx}].active[{j}] "
                f"({pack_name}) must be a mapping"
            )
        kind = entry.get("kind")
        if kind not in KNOWN_ACTIVE_KINDS:
            raise ParseError(
                f"manifest {path}: packs[{idx}].active[{j}] "
                f"({pack_name}) unknown 'kind' {kind!r}; expected one of "
                f"{sorted(KNOWN_ACTIVE_KINDS)}"
            )

        # hosts: required, but the requirement is satisfied by either an
        # entry-level value OR the pack-level default (pack-architecture.md:199).
        # Entry-level value wins on conflict.
        if "hosts" in entry:
            hosts = entry["hosts"]
            if not (
                isinstance(hosts, list)
                and hosts
                and all(isinstance(h, str) and h for h in hosts)
            ):
                raise ParseError(
                    f"manifest {path}: packs[{idx}].active[{j}] "
                    f"({pack_name}) 'hosts' must be a non-empty list of "
                    "non-empty strings"
                )
        elif pack_hosts is None:
            raise ParseError(
                f"manifest {path}: packs[{idx}].active[{j}] "
                f"({pack_name}) missing required 'hosts' list; every active "
                "entry must declare its target host(s), either at the entry "
                "level or via a pack-level 'hosts:' default."
            )

        required = entry.get("required", True)
        if not isinstance(required, bool):
            raise ParseError(
                f"manifest {path}: packs[{idx}].active[{j}] "
                f"({pack_name}) 'required' must be a boolean (got "
                f"{type(required).__name__})"
            )

        active_update_policy = entry.get("update_policy")
        if active_update_policy == "auto":
            raise ParseError(
                f"manifest {path}: packs[{idx}].active[{j}] "
                f"({pack_name}) 'update_policy: auto' is not allowed on "
                "active entries; active code must be explicitly reviewed "
                "(pack-architecture.md § 'Source resolution and active-code "
                "trust')."
            )
        if (
            active_update_policy is not None
            and active_update_policy not in KNOWN_UPDATE_POLICIES
        ):
            raise ParseError(
                f"manifest {path}: packs[{idx}].active[{j}] "
                f"({pack_name}) unknown 'update_policy' "
                f"{active_update_policy!r}"
            )

        _validate_files_list(
            path, idx, pack_name, f"active[{j}]", entry, required=True
        )


def _validate_files_list(
    path: Path,
    idx: int,
    pack_name: str,
    location: str,
    entry: dict[str, Any],
    *,
    required: bool,
) -> None:
    """Validate the ``files: [{from, to}, ...]`` sub-list on a slot entry.

    Active entries require ``files:`` (per pack-architecture.md:483-484) so
    the dispatch layer always has a concrete source→target mapping. Passive
    entries similarly carry concrete paths. ``required=True`` rejects a
    missing / empty ``files`` list; ``required=False`` allows absence for
    schema evolution cases.
    """
    files = entry.get("files")
    if files is None:
        if required:
            raise ParseError(
                f"manifest {path}: packs[{idx}].{location} ({pack_name}) "
                "missing required 'files' list; every active entry must "
                "declare at least one {from, to} mapping."
            )
        return
    if not isinstance(files, list):
        raise ParseError(
            f"manifest {path}: packs[{idx}].{location} ({pack_name}) "
            f"'files' must be a list (got {type(files).__name__})"
        )
    if required and not files:
        raise ParseError(
            f"manifest {path}: packs[{idx}].{location} ({pack_name}) "
            "'files' must contain at least one {from, to} entry."
        )
    for k, file_entry in enumerate(files):
        if not isinstance(file_entry, dict):
            raise ParseError(
                f"manifest {path}: packs[{idx}].{location}.files[{k}] "
                f"({pack_name}) must be a mapping"
            )
        src = file_entry.get("from")
        dst = file_entry.get("to")
        if not isinstance(src, str) or not src:
            raise ParseError(
                f"manifest {path}: packs[{idx}].{location}.files[{k}] "
                f"({pack_name}) missing or empty 'from'"
            )
        if not isinstance(dst, str) or not dst:
            raise ParseError(
                f"manifest {path}: packs[{idx}].{location}.files[{k}] "
                f"({pack_name}) missing or empty 'to'"
            )


def _reject_private_url(
    path: Path, idx: int, pack_name: str, url: str
) -> None:
    """Reject private-source URL at parse time.

    v0.4.0 ships only public anonymous fetches. SSH / ``git@`` / explicit
    auth URLs are a v0.5.0 feature; rejecting them early prevents a
    manifest that could not be fetched from reaching the composer.
    """
    for prefix in _PRIVATE_URL_PREFIXES:
        if url.startswith(prefix):
            raise ParseError(
                f"manifest {path}: packs[{idx}] ({pack_name}) 'source' uses "
                f"private URL scheme {prefix!r}; private source URLs require "
                "aa v0.5.0+. Use an anonymous HTTPS URL in v0.4.0 or wait for "
                "the auth chain release."
            )

    # HTTPS with userinfo ("https://user:pass@host/...") also counts as
    # credential-bearing and is rejected. Full credential-URL coverage lives
    # in scripts/packs/auth.py (Phase 4); this parse-time check catches the
    # obvious cases so they never reach the composer.
    if re.match(r"^https?://[^/@]+@", url):
        raise ParseError(
            f"manifest {path}: packs[{idx}] ({pack_name}) 'source' URL "
            "contains credentials in userinfo; credentials in a URL are "
            "unsafe. Use an anonymous URL, `git@` SSH, `gh auth login`, or "
            "`GITHUB_TOKEN` env (v0.5.0+)."
        )
