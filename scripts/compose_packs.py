#!/usr/bin/env python3
"""Unified pack composer for anywhere-agents (v0.4.0+).

Entry point invoked by bootstrap.sh / bootstrap.ps1 once Phase 1e wires it in.
Accepts the same CLI surface as ``scripts/compose_rule_packs.py`` (``--root``,
``--manifest``, ``--no-cache``, ``--print-yaml``) so the bootstrap scripts can
point at it without plumbing changes.

Phase 1 behavior (this file):

1. Resolve the manifest path (preferring ``packs.yaml`` over legacy
   ``rule-packs.yaml`` alias, per Phase 1a).
2. Parse with ``scripts.packs.schema`` to validate structure + reject
   v0.4.0-out-of-scope features (private source URLs, ``update_policy: auto``
   on active entries, unknown kinds).
3. If any pack declares ``active:`` entries, refuse with a clear
   "v0.4.0 Phase 3" error. Phase 3 replaces this branch with real dispatch.
4. Otherwise, delegate the passive composition to the v0.3.x composer in
   ``scripts/compose_rule_packs.py`` so consumer-visible output stays
   byte-identical during the BC window.

No network or filesystem side effects beyond what the delegated composer
performs. All schema errors are raised at parse time, before fetch.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Sibling imports: scripts/ is on sys.path when this file is invoked as a
# script; compose_rule_packs.py lives next to us, and the packs/ package
# is a child directory.
import compose_rule_packs as legacy  # noqa: E402
from packs import schema  # noqa: E402


def _resolve_manifest_path(root: Path, explicit: Path | None) -> Path:
    """Mirror the legacy composer's default-path logic for consistency.

    Callers that pass an explicit ``--manifest`` get that path verbatim.
    Otherwise we look in the bootstrap-sparse-clone location for
    ``packs.yaml`` first, then fall back to the v0.3.x ``rule-packs.yaml``.
    """
    if explicit is not None:
        return explicit
    bootstrap_dir = root / ".agent-config" / "repo" / "bootstrap"
    candidate = bootstrap_dir / "packs.yaml"
    if candidate.exists():
        return candidate
    return bootstrap_dir / "rule-packs.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="unified pack composer for anywhere-agents (v0.4.0+)"
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

    # --print-yaml is a pure helper; defer to legacy composer unchanged.
    if args.print_yaml:
        return legacy.main(argv)

    root = args.root.resolve()
    manifest_path = _resolve_manifest_path(root, args.manifest)

    # Parse with the new schema when a manifest is present, so v0.4.0-
    # out-of-scope features (private source URLs, update_policy: auto on
    # active, unknown kinds, missing required fields) are rejected at parse
    # time with a clear message, and active-slot / v2 entries get the
    # Phase-3 "not yet" error instead of a confusing downstream failure.
    if manifest_path.exists():
        try:
            parsed = schema.parse_manifest(manifest_path)
        except schema.ParseError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return 1

        for pack in parsed["packs"]:
            if pack.get("active"):
                sys.stderr.write(
                    f"error: pack {pack['name']!r} declares 'active:' "
                    "entries; active-kind dispatch lands in v0.4.0 Phase 3. "
                    "Use a passive-only manifest in this build.\n"
                )
                return 2

        # v0.4.0 Phase 1 accepts version-2 manifests at the schema layer
        # (so pack authors can validate future-shape manifests early) but
        # composition through the v0.3.x passive composer requires version-1
        # input. Reject v2 explicitly here so callers see a Phase-1-specific
        # message instead of the legacy composer's "version unsupported"
        # error bubbling up from a deeper layer. Phase 3 replaces this
        # branch with the unified passive + active composer.
        if parsed["version"] == 2:
            sys.stderr.write(
                "error: passive-only version-2 manifests are accepted by "
                "the schema but are not yet composed by this Phase 1 build. "
                "Keep the manifest on version 1 for now, or wait for the "
                "Phase 3 unified composer to land.\n"
            )
            return 2

    # Passive-only version-1 (or no manifest at all): hand off to the v0.3.x
    # composer, which performs the actual fetch + AGENTS.md composition.
    # Keeps v0.3.x consumer-visible output byte-identical during the BC window.
    return legacy.main(argv)


if __name__ == "__main__":
    sys.exit(main())
