"""User-level config + 4-layer selection resolver (v0.4.0 Phase 4).

Per pack-architecture.md § "User-level config layer" and § "Consumer
opt-in syntax", the composer resolves pack selections from four layers
in precedence order (base → most specific):

1. User-level ($XDG_CONFIG_HOME/anywhere-agents/config.yaml on POSIX,
   %APPDATA%\\anywhere-agents\\config.yaml on Windows) — base list.
2. Project-tracked (<project>/agent-config.yaml).
3. Project-local (<project>/agent-config.local.yaml).
4. AGENT_CONFIG_PACKS env var (transient additive overlay with `-name`
   subtract grammar; legacy AGENT_CONFIG_RULE_PACKS accepted through
   v0.6.x with deprecation warning).

Merge semantics:
- Different pack names across layers: union.
- Same pack name at different layers: more-specific layer overrides for
  all fields (ref, skills-path, etc.).
- Explicit `packs: []` (or legacy `rule_packs: []`) at any layer clears
  all earlier-in-precedence layers for that list. Later layers (env var)
  MAY still add.
"""
from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


# Config file name under the per-user config home directory.
USER_CONFIG_FILENAME = "config.yaml"
USER_CONFIG_APP_DIR = "anywhere-agents"

# Env var names. New canonical name from v0.4.0; legacy name accepted
# with deprecation warning through v0.6.x per plan.
ENV_VAR_CANONICAL = "AGENT_CONFIG_PACKS"
ENV_VAR_LEGACY = "AGENT_CONFIG_RULE_PACKS"


class ConfigError(ValueError):
    """Raised when a user-level config file fails structural validation
    or a path cannot be resolved. Callers that write (pack add/remove)
    must surface this as an actionable exit-nonzero error; callers that
    only read (composer) may downgrade to a stderr warning."""


# ======================================================================
# XDG path resolution
# ======================================================================


def user_config_home(environ: dict[str, str] | None = None) -> Path | None:
    """Return the canonical directory for the user-level config file.

    POSIX:
      - ``$XDG_CONFIG_HOME`` when set and non-empty.
      - Else ``$HOME/.config``.
    Windows:
      - ``%APPDATA%``.
    Returns ``None`` when neither source resolves (e.g., running in an
    environment without ``$HOME`` and without ``%APPDATA%``). Callers
    that need to WRITE (``pack add`` / ``pack remove``) must raise an
    actionable error on ``None``; callers that only READ (composer) may
    fall back to project-level-only with a stderr note.
    """
    env = environ if environ is not None else os.environ
    if sys.platform == "win32":
        appdata = env.get("APPDATA")
        if appdata:
            return Path(appdata) / USER_CONFIG_APP_DIR
        return None
    # POSIX
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / USER_CONFIG_APP_DIR
    home = env.get("HOME")
    if home:
        return Path(home) / ".config" / USER_CONFIG_APP_DIR
    return None


def user_config_path(environ: dict[str, str] | None = None) -> Path | None:
    """Return the canonical path to the user-level config file, or
    ``None`` if the user-config home cannot be resolved. The file
    itself may or may not exist."""
    home = user_config_home(environ)
    if home is None:
        return None
    return home / USER_CONFIG_FILENAME


# ======================================================================
# YAML read/write helpers (atomic write + malformed-YAML safety)
# ======================================================================


def load_config_file(path: Path) -> dict[str, Any] | None:
    """Load a user-level or project-level config YAML file.

    Returns ``None`` when the file doesn't exist (normal case for packs
    not yet installed). Raises ``ConfigError`` when the file exists but
    cannot be parsed — callers that want to overwrite MUST treat this
    as a hard stop (we refuse to clobber a malformed file).
    """
    if not path.exists():
        return None
    if yaml is None:
        raise ConfigError(
            f"cannot read {path}: PyYAML is not installed"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"malformed YAML in {path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"config {path} must be a mapping at top level "
            f"(got {type(data).__name__})"
        )
    return data


def save_config_file(path: Path, data: dict[str, Any]) -> None:
    """Atomically write ``data`` as YAML to ``path``.

    Uses temp file + ``os.replace`` in the same directory so an
    interrupted write leaves either the old file or the new file on
    disk, never a partial one. Refuses to write when PyYAML is missing
    (unusable without YAML serialization).
    """
    if yaml is None:
        raise ConfigError(
            f"cannot write {path}: PyYAML is not installed"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ConfigError(f"cannot write {path}: {exc}") from exc


# ======================================================================
# Pack-list extraction from a single config layer
# ======================================================================


def _extract_pack_list(
    data: dict[str, Any] | None, source: str
) -> tuple[list[dict[str, Any]] | None, bool]:
    """Extract the pack list from one layer's parsed config.

    Returns ``(packs, explicit_empty)`` where ``packs`` is the list of
    pack dicts (normalized — short-form names converted to ``{"name": x}``)
    or ``None`` if the layer provides no signal. ``explicit_empty`` is
    ``True`` when the layer explicitly wrote ``packs: []`` or
    ``rule_packs: []`` — a signal that clears earlier layers.

    Accepts both the new ``packs:`` key and the legacy ``rule_packs:``
    alias. If both are present in the same file, ``packs:`` wins.
    """
    if data is None:
        return None, False

    # Prefer new key; fall back to legacy.
    key: str | None = None
    if "packs" in data:
        key = "packs"
    elif "rule_packs" in data:
        key = "rule_packs"
        # Deprecation warning per plan — through v0.6.x.
        warnings.warn(
            f"{source}: 'rule_packs:' is deprecated; use 'packs:' instead. "
            "The legacy key is accepted through v0.6.x.",
            DeprecationWarning,
            stacklevel=3,
        )
    if key is None:
        return None, False

    raw = data[key]
    if raw is None:
        # `packs:` present but null — treat as empty list (explicit clear).
        return [], True
    if not isinstance(raw, list):
        raise ConfigError(
            f"{source}: {key!r} must be a list (got {type(raw).__name__})"
        )
    if not raw:
        return [], True

    normalized: list[dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if isinstance(entry, str):
            normalized.append({"name": entry})
        elif isinstance(entry, dict):
            if "name" not in entry or not isinstance(entry["name"], str):
                raise ConfigError(
                    f"{source}: {key}[{i}] must have a string 'name' field"
                )
            normalized.append(dict(entry))
        else:
            raise ConfigError(
                f"{source}: {key}[{i}] must be a string or mapping "
                f"(got {type(entry).__name__})"
            )
    return normalized, False


# ======================================================================
# Env var grammar parsing
# ======================================================================


def parse_env_var(
    environ: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Parse the AGENT_CONFIG_PACKS env var (with legacy alias fallback).

    Returns ``(add, subtract)`` — two lists of pack names. Direct-source
    URLs are rejected at parse time per pack-architecture.md:386
    (env-var grammar is names-only because shell quoting of URLs is
    fragile). Legacy AGENT_CONFIG_RULE_PACKS is accepted with a
    deprecation warning when AGENT_CONFIG_PACKS is unset.
    """
    env = environ if environ is not None else os.environ
    raw = env.get(ENV_VAR_CANONICAL, "")
    if not raw:
        legacy = env.get(ENV_VAR_LEGACY, "")
        if legacy:
            warnings.warn(
                f"{ENV_VAR_LEGACY} is deprecated; use {ENV_VAR_CANONICAL} "
                "instead. The legacy name is accepted through v0.6.x.",
                DeprecationWarning,
                stacklevel=2,
            )
            raw = legacy
    if not raw:
        return [], []

    add: list[str] = []
    subtract: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        # Reject anything that looks like a URL or a source spec — only
        # pack names are valid env-var entries.
        if "/" in token or "@" in token or ":" in token:
            raise ConfigError(
                f"{ENV_VAR_CANONICAL}: entry {token!r} looks like a direct "
                "source; the env var is names-only. Use agent-config.yaml "
                "for direct-source entries."
            )
        if token.startswith("-"):
            name = token[1:]
            if not name:
                raise ConfigError(
                    f"{ENV_VAR_CANONICAL}: bare '-' is not a valid entry"
                )
            subtract.append(name)
        else:
            add.append(token)
    return add, subtract


# ======================================================================
# 4-layer resolver
# ======================================================================


def resolve_selections(
    *,
    user_level: dict[str, Any] | None = None,
    project_tracked: dict[str, Any] | None = None,
    project_local: dict[str, Any] | None = None,
    env_add: list[str] | None = None,
    env_subtract: list[str] | None = None,
    default_selections: list[dict[str, Any]] | None = None,
    force_defaults: bool = False,
    validate_url_fn: Callable[..., None] | None = None,
) -> list[dict[str, Any]]:
    """Resolve pack selections across the four layers.

    Layer precedence (base → most specific):
    1. ``user_level``
    2. ``project_tracked``
    3. ``project_local``
    4. ``env_add`` / ``env_subtract``

    - Different pack names across layers: union.
    - Same pack name at a more-specific layer: later layer overrides
      for all fields (ref, skills-path, etc.).
    - Explicit ``packs: []`` at any layer clears ALL earlier layers'
      contributions; later layers may still add.
    - Env-var ``add`` entries append (or override by name) at the end;
      ``subtract`` entries remove matching names from the final list.
    - By default, ``default_selections`` apply only when no signal
      exists in any of the four layers (no config files, no env var,
      and no explicit empty-clear in any layer). When
      ``force_defaults`` is true, defaults seed the base layer before
      user/project/env entries are merged. Explicit empty lists still
      clear them, and env subtract entries can still remove them.

    When ``validate_url_fn`` is provided, every entry's source URL
    (across user-level, project-tracked, and project-local layers) is
    passed through it as ``validate_url_fn(url, source_layer="<layer>")``
    before any merge or network call. Pass
    :func:`scripts.packs.auth.reject_credential_url` to enforce
    parse-time credential-URL rejection across all layers (v0.5.0
    Deferral 3 + Codex R1 M8). The env-var layer accepts pack names
    only — its URL safety is enforced by :func:`parse_env_var` which
    rejects anything that looks like a URL outright.

    Returns a list of pack dicts with ``name`` and any per-entry
    overrides (``ref``, ``skills-path``, etc.).
    """
    if env_add is None:
        env_add = []
    if env_subtract is None:
        env_subtract = []

    # Preserve order of addition via a dict keyed by pack name. Each
    # layer's entries either add-if-new or override-by-name.
    accumulated: dict[str, dict[str, Any]] = {}
    any_layer_spoke = False

    if force_defaults and default_selections:
        for entry in default_selections:
            accumulated[entry["name"]] = dict(entry)

    for data, source in [
        (user_level, "user-level"),
        (project_tracked, "project-tracked"),
        (project_local, "project-local"),
    ]:
        packs, explicit_empty = _extract_pack_list(data, source)
        if packs is None and not explicit_empty:
            continue
        any_layer_spoke = True
        if explicit_empty:
            accumulated.clear()
            continue
        assert packs is not None  # explicit_empty False + non-None means list
        # v0.5.0 R1 M8: validate every URL before the merge logic
        # touches the entry. Per-layer dispatch ensures the error
        # message identifies which config file (user-level /
        # project-tracked / project-local) holds the offending URL.
        if validate_url_fn is not None:
            for entry in packs:
                src = entry.get("source")
                url: str | None = None
                if isinstance(src, dict):
                    # Match schema.py:180 precedence: ``repo`` is the
                    # canonical key, ``url`` is the legacy alias. Reading
                    # them in opposite orders across modules would let a
                    # malformed pack with both keys present pass one
                    # validation layer and fail another with a different
                    # URL surfaced — confusing for users to debug.
                    url = src.get("repo") or src.get("url")
                elif isinstance(src, str):
                    url = src
                if url:
                    validate_url_fn(url, source_layer=source)
        for entry in packs:
            accumulated[entry["name"]] = entry

    # Env var layer
    if env_add or env_subtract:
        any_layer_spoke = True
        for name in env_add:
            accumulated.setdefault(name, {"name": name})
        for name in env_subtract:
            accumulated.pop(name, None)

    # Apply defaults only when no layer spoke at all.
    if not force_defaults and not any_layer_spoke and default_selections:
        return [dict(entry) for entry in default_selections]

    return list(accumulated.values())


# ======================================================================
# High-level: compose-time "get final selection for this project"
# ======================================================================


def resolved_for_project(
    project_root: Path,
    *,
    environ: dict[str, str] | None = None,
    default_selections: list[dict[str, Any]] | None = None,
    force_defaults: bool = False,
    validate_url_fn: Callable[..., None] | None = None,
) -> list[dict[str, Any]]:
    """Convenience: read all four layers for ``project_root`` + current
    env and return the resolved selection list.

    ``validate_url_fn`` is forwarded to :func:`resolve_selections`. Pass
    :func:`scripts.packs.auth.reject_credential_url` to enforce
    parse-time credential-URL rejection at every layer (v0.5.0
    Deferral 3 + Codex R1 M8).
    """
    user_level_path = user_config_path(environ)
    user_level = (
        load_config_file(user_level_path) if user_level_path is not None else None
    )
    project_tracked = load_config_file(project_root / "agent-config.yaml")
    project_local = load_config_file(project_root / "agent-config.local.yaml")
    env_add, env_subtract = parse_env_var(environ)
    return resolve_selections(
        user_level=user_level,
        project_tracked=project_tracked,
        project_local=project_local,
        env_add=env_add,
        env_subtract=env_subtract,
        default_selections=default_selections,
        force_defaults=force_defaults,
        validate_url_fn=validate_url_fn,
    )
