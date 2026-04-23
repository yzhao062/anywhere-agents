#!/usr/bin/env python3
"""Rule-pack composer for anywhere-agents bootstrap.

Invoked by bootstrap.sh and bootstrap.ps1 by default whenever Python 3
and PyYAML are both available: under the default-on policy, the composer
applies an internal default selection (currently [agent-style]) when the
consumer has provided no explicit signal. Consumers can override the
selection via:
  - agent-config.yaml at the consumer repo root (tracked, durable)
  - agent-config.local.yaml at the consumer repo root (gitignored)
  - AGENT_CONFIG_RULE_PACKS environment variable (transient one-run)
  - rule_packs: [] in agent-config.yaml — explicit opt-out

Fetches each rule pack's docs/rule-pack.md at its pinned git ref, validates
against the per-agent routing-marker grammar, and composes the upstream
AGENTS.md (already fetched by bootstrap at .agent-config/AGENTS.md) with
delimited rule-pack blocks. Writes the composed result atomically to the
consumer repo's AGENTS.md so the on-disk file is never partial.

When Python or PyYAML is unavailable, bootstrap does NOT call this helper
and falls back to writing the verbatim upstream AGENTS.md plus a tip.
Explicit opt-out via `rule_packs: []` is honored by this helper (it
writes the verbatim upstream to AGENTS.md under that signal).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "error: PyYAML is required for rule-pack composition. "
        "Install with `pip install pyyaml`, or remove rule_packs from "
        "agent-config.yaml to stay on the shell-only bootstrap path.\n"
    )
    sys.exit(2)

# Reject any per-agent routing marker in rule-pack content. Covers
# <!-- agent:<tag> -->, <!-- /agent:<tag> -->, and whitespace variants.
# Conservative superset of scripts/generate_agent_configs.py routing grammar.
ROUTING_MARKER_RE = re.compile(r"<!--\s*/?agent:[\w-]+\s*-->")

# Allowed characters in a rule-pack ref (semver tags, branches, commit SHAs).
REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

BEGIN_FMT = "<!-- rule-pack:{name}:begin version={ref} sha256={sha} -->"
END_FMT = "<!-- rule-pack:{name}:end -->"

# Default rule-pack selections applied when the consumer provides no
# explicit signal (no agent-config.yaml, no agent-config.local.yaml, no
# AGENT_CONFIG_RULE_PACKS env var). Consumers opt out with an explicit
# empty `rule_packs: []` in agent-config.yaml.
DEFAULT_SELECTIONS: list[dict[str, Any]] = [{"name": "agent-style"}]


class RulePackError(RuntimeError):
    """Named error for rule-pack composition failures."""


def parse_manifest(path: Path) -> dict[str, dict[str, Any]]:
    """Parse bootstrap/packs.yaml (or legacy rule-packs.yaml). Returns {pack_name: entry_dict}."""
    if not path.exists():
        raise RulePackError(f"manifest not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise RulePackError(f"malformed manifest {path}: {e}") from e
    if not isinstance(data, dict):
        raise RulePackError(f"manifest {path} must be a mapping at top level")
    if "version" not in data or "packs" not in data:
        raise RulePackError(
            f"manifest {path} missing required keys 'version' and 'packs'"
        )
    if data["version"] != 1:
        raise RulePackError(
            f"manifest version {data['version']!r} unsupported (expected 1)"
        )
    packs: dict[str, dict[str, Any]] = {}
    for entry in data.get("packs") or []:
        if not isinstance(entry, dict):
            raise RulePackError(
                f"manifest pack entry is not a mapping: {entry!r}"
            )
        for req in ("name", "source", "default-ref"):
            if req not in entry:
                raise RulePackError(
                    f"manifest pack entry missing '{req}': {entry}"
                )
        name = entry["name"]
        if name in packs:
            raise RulePackError(f"manifest has duplicate pack '{name}'")
        packs[name] = entry
    return packs


def parse_user_config(path: Path) -> list[dict[str, Any]] | None:
    """Parse agent-config.yaml or agent-config.local.yaml.

    Return value distinguishes three cases:
      - None: no signal. The file is absent, empty, or present without a
        top-level `rule_packs` key. The composer applies DEFAULT_SELECTIONS
        unless another source signals.
      - []: explicit opt-out. The file has `rule_packs: []` or
        `rule_packs:` with a null value. The composer respects this and
        applies no rule packs.
      - [dict, ...]: explicit selections from the user.
    """
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise RulePackError(f"malformed user config {path}: {e}") from e
    if data is None:
        return None
    if not isinstance(data, dict):
        raise RulePackError(
            f"user config {path} must be a mapping at top level"
        )
    if "rule_packs" not in data:
        return None
    raw = data["rule_packs"]
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RulePackError(
            f"user config {path}: 'rule_packs' must be a list"
        )
    result: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, str):
            result.append({"name": entry})
        elif isinstance(entry, dict):
            if "name" not in entry:
                raise RulePackError(
                    f"user config {path}: pack entry missing 'name': {entry}"
                )
            result.append(dict(entry))
        else:
            raise RulePackError(
                f"user config {path}: pack entry must be a string or mapping, "
                f"got {entry!r}"
            )
    return result


def parse_env_packs(env_val: str) -> list[dict[str, Any]]:
    """Parse AGENT_CONFIG_RULE_PACKS. Comma or whitespace separated names."""
    names = [n.strip() for n in env_val.replace(",", " ").split() if n.strip()]
    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return [{"name": n} for n in ordered]


def resolve_selections(
    tracked: list[dict[str, Any]] | None,
    local: list[dict[str, Any]] | None,
    env_list: list[dict[str, Any]],
    default: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve effective pack selections from the three opt-in sources.

    Signal semantics (from parse_user_config):
      None = no signal (file absent, or no `rule_packs` key).
      []   = explicit opt-out.
      [..] = explicit selections.

    Precedence (durable to transient):
      - tracked base: agent-config.yaml at repo root
      - local override: agent-config.local.yaml (overrides tracked by
        name for ref or other fields)
      - env additive: AGENT_CONFIG_RULE_PACKS env var (adds packs that
        are not already selected)

    If NO source signals (all None / empty-env), return a copy of
    `default` (typically DEFAULT_SELECTIONS for opinionated installs).
    An explicit opt-out via tracked=[] or local=[] counts as a signal
    and suppresses the default.

    Duplicate names within a single source: last wins, warning emitted.
    """
    has_signal = (
        tracked is not None or local is not None or bool(env_list)
    )
    if not has_signal:
        return [dict(entry) for entry in (default or [])]

    base: dict[str, dict[str, Any]] = {}
    for entry in (tracked or []):
        name = entry["name"]
        if name in base:
            sys.stderr.write(
                f"warning: duplicate pack '{name}' in agent-config.yaml; "
                f"last entry wins\n"
            )
        base[name] = dict(entry)
    for entry in (local or []):
        name = entry["name"]
        if name in base:
            merged = dict(base[name])
            merged.update(entry)
            base[name] = merged
        else:
            base[name] = dict(entry)
    for entry in env_list:
        name = entry["name"]
        if name not in base:
            base[name] = dict(entry)
    return list(base.values())


def validate_ref(pack_name: str, ref: str) -> None:
    """Reject refs that could escape the URL or cache path."""
    if not REF_RE.match(ref):
        raise RulePackError(
            f"rule pack '{pack_name}': ref {ref!r} contains characters "
            f"outside [A-Za-z0-9._/-]; expected a git tag, branch, or SHA"
        )


def fetch_rule_pack(
    source_template: str,
    ref: str,
    cache_md: Path,
    no_cache: bool,
) -> tuple[str, str]:
    """Fetch a rule pack's Markdown. Returns (content, sha256_hex).

    Semantics (PLAN "Cache and offline behavior"):
      - Always attempt the network fetch first. On success, overwrite the
        cache and return the fresh content.
      - On fetch failure with no_cache=False: fall back to the cache if
        present (warn on stderr). If no cache is available, raise.
      - On fetch failure with no_cache=True: raise regardless of cache.
        --no-cache forces refetch semantics; a stale cache must not be
        silently served under --no-cache.

    Cache sidecar (cache_md + '.sha256') records the SHA-256 of the
    content actually used (fresh or cached); sidecar failure is not fatal.
    """
    url = source_template.replace("{ref}", ref)
    cache_sha = cache_md.with_name(cache_md.name + ".sha256")
    content: str | None = None
    fetch_error: Exception | None = None

    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            body: bytes = resp.read()
        fetched = body.decode("utf-8")
        cache_md.parent.mkdir(parents=True, exist_ok=True)
        cache_md.write_text(fetched, encoding="utf-8")
        content = fetched
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError) as e:
        fetch_error = e

    if content is None and not no_cache and cache_md.exists():
        try:
            content = cache_md.read_text(encoding="utf-8")
            sys.stderr.write(
                f"warning: fetch failed for {url} ({fetch_error}); "
                f"using cached {cache_md}\n"
            )
        except OSError:
            pass

    if content is None:
        cause = "--no-cache set" if no_cache else "no cache available"
        raise RulePackError(
            f"failed to fetch rule pack from {url} (ref {ref!r}); {cause}: "
            f"{fetch_error}. Next step: check network connectivity or "
            f"remove the pack from agent-config.yaml."
        )

    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        cache_sha.write_text(sha + "\n", encoding="utf-8")
    except OSError:
        # Cache sidecar is informational; not fatal if we cannot write it.
        pass
    return content, sha


def validate_rule_pack(name: str, content: str) -> None:
    """Reject any per-agent routing marker in the rule-pack content."""
    m = ROUTING_MARKER_RE.search(content)
    if m:
        raise RulePackError(
            f"rule pack '{name}' contains a per-agent routing marker "
            f"{m.group(0)!r}; these are reserved for anywhere-agents' "
            f"per-agent generator and are not permitted in shared rule-pack "
            f"Markdown. Remove the marker from docs/rule-pack.md at the "
            f"pack source repo."
        )


def compose_agents_md(
    upstream: str,
    selections: list[dict[str, Any]],
    manifest: dict[str, dict[str, Any]],
    cache_dir: Path,
    no_cache: bool,
) -> str:
    """Build the composed AGENTS.md content in memory.

    Empty selections returns upstream byte-identical (no-op composition).
    """
    if not selections:
        return upstream

    parts: list[str] = [upstream.rstrip()]
    for sel in selections:
        name = sel["name"]
        if name not in manifest:
            known = ", ".join(sorted(manifest.keys())) or "(none)"
            raise RulePackError(
                f"unknown rule pack '{name}'; known packs in manifest: "
                f"{known}. Check spelling in agent-config.yaml or add the "
                f"pack to bootstrap/packs.yaml in anywhere-agents."
            )
        entry = manifest[name]
        ref = sel.get("ref") or entry["default-ref"]
        validate_ref(name, ref)
        # Percent-encode the filename components so a ref containing '/'
        # (branch names) or '..' (path traversal) cannot escape cache_dir
        # even if validate_ref is loosened in future.
        cache_name = f"{quote(name, safe='')}-{quote(ref, safe='')}.md"
        cache_md = cache_dir / cache_name
        content, sha = fetch_rule_pack(entry["source"], ref, cache_md, no_cache)
        validate_rule_pack(name, content)

        parts.append("")
        parts.append(BEGIN_FMT.format(name=name, ref=ref, sha=sha))
        parts.append(content.rstrip())
        parts.append(END_FMT.format(name=name))

    return "\n".join(parts) + "\n"


def atomic_write(target: Path, content: str) -> None:
    """Write content to target via temp file + os.replace in same directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.replace(tmp, str(target))
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def do_compose(root: Path, manifest_path: Path, no_cache: bool) -> int:
    """Run the composition flow. Returns process exit code."""
    try:
        manifest = parse_manifest(manifest_path)
        tracked = parse_user_config(root / "agent-config.yaml")
        local = parse_user_config(root / "agent-config.local.yaml")
        env_val = os.environ.get("AGENT_CONFIG_RULE_PACKS", "")
        env_list = parse_env_packs(env_val) if env_val else []
        selections = resolve_selections(
            tracked, local, env_list, default=DEFAULT_SELECTIONS
        )
    except RulePackError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    upstream_path = root / ".agent-config" / "AGENTS.md"
    if not upstream_path.exists():
        sys.stderr.write(
            f"error: upstream AGENTS.md not found at {upstream_path}; "
            f"bootstrap should fetch it before invoking this helper\n"
        )
        return 1
    upstream = upstream_path.read_text(encoding="utf-8")

    cache_dir = root / ".agent-config" / "rule-packs"
    try:
        composed = compose_agents_md(
            upstream, selections, manifest, cache_dir, no_cache=no_cache
        )
    except RulePackError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    try:
        atomic_write(root / "AGENTS.md", composed)
    except OSError as e:
        sys.stderr.write(f"error: failed to write composed AGENTS.md: {e}\n")
        return 1
    return 0


def do_print_yaml(pack_name: str) -> int:
    """Print agent-config.yaml snippet for pack_name. Dry helper mode."""
    sys.stdout.write(
        "Add the following to agent-config.yaml at your project root, "
        "then run bootstrap again to apply:\n"
        "\n"
        "  rule_packs:\n"
        f"    - name: {pack_name}\n"
        "      # Optional: pin to a specific ref "
        "(defaults to manifest's default-ref)\n"
        "      # ref: v0.3.2\n"
        "\n"
        "After committing agent-config.yaml, run:\n"
        "\n"
        "  bash bootstrap.sh\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="rule-pack composer for anywhere-agents bootstrap"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="consumer repo root (default: cwd)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help=(
            "path to packs.yaml manifest (legacy rule-packs.yaml also accepted; "
            "default: <root>/.agent-config/repo/bootstrap/packs.yaml, falling back "
            "to rule-packs.yaml if packs.yaml is absent)"
        ),
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="force refetch of rule-pack content, ignoring cache",
    )
    parser.add_argument(
        "--print-yaml",
        metavar="PACK",
        default=None,
        help="dry helper: print agent-config.yaml snippet for PACK and exit",
    )
    args = parser.parse_args(argv)

    if args.print_yaml:
        return do_print_yaml(args.print_yaml)

    root = args.root.resolve()
    if args.manifest is not None:
        manifest_path = args.manifest
    else:
        # Loader alias: prefer new packs.yaml; fall back to legacy rule-packs.yaml
        # so pre-v0.4.0 bootstrap caches continue to compose correctly.
        bootstrap_dir = root / ".agent-config" / "repo" / "bootstrap"
        candidate = bootstrap_dir / "packs.yaml"
        if candidate.exists():
            manifest_path = candidate
        else:
            manifest_path = bootstrap_dir / "rule-packs.yaml"
    return do_compose(root, manifest_path, args.no_cache)


if __name__ == "__main__":
    sys.exit(main())
