#!/usr/bin/env python3
"""Unified pack composer for anywhere-agents (v0.4.0+).

Entry point invoked by bootstrap.sh / bootstrap.ps1. Handles both v1
(legacy passive-only) and v2 (unified passive + active) manifests:

- v1 manifests: delegate to ``scripts/compose_rule_packs.py`` so v0.3.x
  consumer-visible output stays byte-identical during the BC window.
- v2 manifests: parse with ``scripts.packs.schema``, resolve selections,
  route passive entries through the v2 passive adapter, dispatch active
  entries via the kind-handler registry, and stage all writes through
  ``scripts.packs.transaction`` for per-file atomic commits. State files
  (``pack-lock.json``, ``pack-state.json``) are written at end on success.

The v2 composition flow acquires the per-user and per-repo lock pair
at the top of ``main`` (v0.5.0 Phase 6) before any reconciliation or
state-mutating work runs; ``locks.LockTimeout`` from either lock is
surfaced as exit 10. Startup reconciliation is wired in by a later
v0.5.0 phase.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# When ``compose_packs.py`` is invoked as a script
# (``python scripts/compose_packs.py``) Python only puts the script's
# directory (``scripts/``) on sys.path, so ``from packs import X`` works
# but ``from scripts.packs import X`` does not. The Phase 3
# ``source_fetch.py`` uses the latter form; insert the repo root onto
# sys.path before any ``from packs import ...`` so both invocation
# modes (subprocess + unittest-discover) see a consistent import graph.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# NOTE (v0.5.0): the imports below use ``from packs import X`` while
# ``scripts/packs/source_fetch.py`` and Phase 4 newer test code use
# ``from scripts.packs import X``. The two import paths produce two
# separate module objects loading the same .py file (verified:
# ``packs.auth is scripts.packs.auth`` is False). Benign today because
# no cross-module isinstance checks span the boundary; a future v0.6.x
# refactor should consolidate to ``from scripts.packs import X``
# everywhere. Do not mix the two forms in the same module without a
# regression test that pins the chosen form.
import compose_rule_packs as legacy  # noqa: E402
from packs import auth  # noqa: E402
from packs import config as config_mod  # noqa: E402
from packs import dispatch  # noqa: E402
from packs import handlers  # noqa: E402 — side-effect: registers handlers
from packs import locks  # noqa: E402
from packs import passive as passive_mod  # noqa: E402
from packs import reconciliation  # noqa: E402
from packs import schema  # noqa: E402
from packs import source_fetch  # noqa: E402
from packs import state as state_mod  # noqa: E402
from packs import transaction as txn_mod  # noqa: E402


# v0.5.0 Phase 4 (Codex Round 1 H3 + Round 2 M3):
# detect_host() returns one of these strings; ``DispatchContext.current_host``
# accepts only these values. Adding a new host is a controlled point —
# update this tuple, the schema's host validator, and any host-aware
# dispatch logic in lockstep.
KNOWN_HOSTS = ("claude-code", "codex")


class ComposeError(RuntimeError):
    """Raised when a per-selection compose step fails in a way that
    should abort the run with a clear consumer-facing message.

    Currently raised by ``_process_selection`` for:

    - Bundled-name lookups that don't match any pack in the manifest.
    - Inline-source fetches whose remote ``pack.yaml`` does not declare
      a pack with the consumer-requested name.

    The composer's outer ``try`` in ``_do_compose_v2`` catches this so
    the exit-code 1 path emits a single ``error:`` line on stderr (no
    traceback)."""


class PackLockDriftAborted(RuntimeError):
    """Raised by :func:`prompt_user_for_updates` when the consumer set
    ``ANYWHERE_AGENTS_UPDATE=fail`` and at least one pack has upstream
    drift relative to ``pack-lock.json``.

    Carries the pending-updates list so ``compose_packs.main`` can write
    it to ``pending-updates.json`` before surfacing exit non-zero — the
    user gets a clear summary on the next session-start banner pass.
    """

    def __init__(self, pending_updates: list) -> None:
        self.pending_updates = list(pending_updates)
        super().__init__(
            f"{len(self.pending_updates)} pack updates pending and "
            "ANYWHERE_AGENTS_UPDATE=fail; aborting compose."
        )


# ----------------------------------------------------------------------
# v0.5.0 Phase 8: pending-updates helpers, prompt UX, compose summary.
# ----------------------------------------------------------------------


def _selection_ref(selection: dict) -> str:
    """Pull the ref string out of a resolved 4-layer selection dict.

    Selections produced by ``packs.config.resolve_selections`` shape the
    ref under ``source.ref`` (inline-source) or as a top-level ``ref``
    fallback (bundled). Falls back to ``"main"`` when neither is set so
    the prompt UX always has something to display.
    """
    source = selection.get("source") or {}
    if isinstance(source, dict):
        return source.get("ref") or selection.get("ref") or "main"
    return selection.get("ref") or "main"


def _current_commit(selection: dict) -> str:
    """Best-effort recovery of the commit recorded in pack-lock for ``selection``.

    The selection dict may carry a ``resolved_commit`` field if compose
    threaded the prior pack-lock entry into it; otherwise the requested
    ref is the closest analogue. Used only for the human-facing
    ``current`` field of ``pending-updates.json`` — exactness is not
    required, only enough context to recognise the pack.
    """
    commit = selection.get("resolved_commit") or selection.get("recorded_commit")
    if isinstance(commit, str) and commit:
        return commit
    return _selection_ref(selection)


def prompt_user_for_updates(pending_updates):
    """Resolve the apply / skip / fail decision for ``pending_updates``.

    Decision tree (Phase 8 Task 8.1, Round 1 + Round 3):

    - Both stdin and stdout are TTYs → interactive prompt (Y default).
    - Otherwise consult ``ANYWHERE_AGENTS_UPDATE`` env var:

      - ``apply`` → ``"apply"``.
      - ``skip`` (default when unset) → ``"skip"``.
      - ``fail`` → raises :class:`PackLockDriftAborted` so a non-TTY CI
        path can fail closed when drift is unacceptable.
      - any other value → :class:`ValueError` (fail-loud on typos).
    """
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _interactive_prompt(pending_updates)
    mode = os.environ.get("ANYWHERE_AGENTS_UPDATE", "skip")
    if mode == "apply":
        return "apply"
    if mode == "skip":
        return "skip"
    if mode == "fail":
        raise PackLockDriftAborted(pending_updates)
    raise ValueError(
        f"unknown ANYWHERE_AGENTS_UPDATE value: {mode!r} "
        "(expected one of: apply, skip, fail)"
    )


def _interactive_prompt(pending_updates):
    """Interactive Y/n prompt over the pending-updates list.

    Presents a per-pack line (name, ref, abbreviated commit, kind), then
    a single batch-apply prompt. Empty input or ``y`` / ``yes`` is
    apply; anything else is skip — defer the updates and rerun later.

    On EOF (e.g., Ctrl-D at the prompt, or stdin closing mid-pipe),
    treat as ``skip`` so the caller defers the updates rather than
    bubbling EOFError out of the composer. A bare newline is printed
    so the prompt line ends cleanly when run inside a TTY.
    """
    print(f"\n{len(pending_updates)} packs have upstream updates:\n")
    for selection, archive, pack_def in pending_updates:
        name = selection["name"]
        kind = "active" if pack_def.get("active") else "passive"
        commit_short = (archive.resolved_commit or "")[:7]
        print(
            f"  {name:20s} {_selection_ref(selection)} -> "
            f"{commit_short} ({kind})"
        )
    print("\nApply all? [Y/n]: ", end="", flush=True)
    try:
        response = input().strip().lower()
    except EOFError:
        # Newline so the prompt line terminates cleanly on Ctrl-D.
        print()
        return "skip"
    return "apply" if response in ("", "y", "yes") else "skip"


def _pending_updates_path(project_root: Path) -> Path:
    return project_root / ".agent-config" / "pending-updates.json"


def write_pending_updates_json(project_root: Path, host: str, pending_updates) -> None:
    """Write ``pending-updates.json`` summarising every deferred pack.

    Payload shape (Phase 8 Task 8.2):

    ::

        {
          "ts":   "<ISO-8601 UTC>",
          "host": "<claude-code|codex>",
          "packs": [
            {"name": "<pack>", "current": "<commit>", "available": "<commit>",
             "kind": "active|passive"}, ...
          ]
        }

    Atomic via temp file + ``Path.replace``. The session-start banner
    reads this file via ``session_bootstrap._maybe_print_pending_updates``.
    """
    path = _pending_updates_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "host": host,
        "packs": [
            {
                "name": selection["name"],
                "current": _current_commit(selection),
                "available": archive.resolved_commit,
                "kind": "active" if pack_def.get("active") else "passive",
            }
            for selection, archive, pack_def in pending_updates
        ],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def clear_pending_updates_json(project_root: Path) -> None:
    """Remove ``pending-updates.json`` if it exists.

    Idempotent. Round 3 M4 invariant: the composer must call this on
    every apply path (interactive Y, non-TTY apply, ``pack update``,
    subsequent run with no drift) so a stale file from an earlier run
    cannot mislead the next session-start banner.
    """
    path = _pending_updates_path(project_root)
    try:
        path.unlink()
    except FileNotFoundError:
        # Already absent; idempotent clear.
        pass


def print_compose_summary(selections, outcomes, pending_updates, host: str) -> None:
    """Print a concise per-pack outcome line, host, + pending-update hint.

    ``selections`` is the resolved selection list. ``outcomes`` is a
    name → label map (e.g., ``"fetched (ssh)"``, ``"no change"``,
    ``"deferred (drift)"``). ``pending_updates`` is the list passed
    to :func:`write_pending_updates_json`; non-empty triggers the
    apply-instructions hint.
    """
    print("\nv0.5.0 compose summary:")
    for selection in selections:
        name = selection["name"]
        outcome = outcomes.get(name, "no change")
        print(f"  {name:20s} {outcome}")
    print(f"\nHost: {host}")
    if pending_updates:
        count = len(pending_updates)
        plural = "" if count == 1 else "s"
        print(
            f"{count} pack update{plural} pending. "
            f"Run `bash .agent-config/bootstrap.sh` (Linux/macOS) or "
            f"`pwsh -File .agent-config/bootstrap.ps1` (Windows) "
            f"interactively to apply, or set "
            f"ANYWHERE_AGENTS_UPDATE=apply for non-interactive auto-apply."
        )


def print_adoption_summary(
    adopted_paths: list[str], stream: Any = None
) -> None:
    """v0.5.3: surface drift-gate adopt-on-match results.

    Empty ``adopted_paths`` is a clean install with no adoptions; emit
    nothing so the normal-run case stays quiet. Non-empty triggers a
    header line with the count plus one indented absolute path per
    adoption, matching the format of the drift error path so users see
    the symmetric audit trail.

    ``stream`` defaults to ``sys.stdout``. Tests inject ``io.StringIO()``
    to capture output without relying on ``redirect_stdout``.
    """
    if not adopted_paths:
        return
    out = stream if stream is not None else sys.stdout
    out.write(
        f"ℹ composer adopted {len(adopted_paths)} pre-existing "
        f"file(s) into pack-lock.json (content matched pack output):\n"
    )
    for path in adopted_paths:
        out.write(f"  {path}\n")


def _validated_state_bytes(
    write_fn: Callable[[Path, dict[str, Any]], None], payload: dict[str, Any]
) -> bytes:
    """Run the state-file ``write_fn`` against a temp path so schema
    validation errors surface before we stage the content. Returns the
    bytes written — caller stages them through the composer transaction
    so all writes (outputs + state files) share one commit boundary.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "state.json"
        write_fn(tmp, payload)
        return tmp.read_bytes()


# Default v2 selections applied when the consumer provides no signal.
# Includes the v0.3.x-default agent-style rule pack plus the bundled
# aa-core-skills pack so shipped-skill pointers are emitted under the
# same default-on behavior as v0.3.x.
DEFAULT_V2_SELECTIONS: list[dict[str, str]] = [
    {"name": "agent-style"},
    {"name": "aa-core-skills"},
]

# v0.5.4: name set used by ``_process_selection`` to gate the bundled
# fallback for inline-source packs whose upstream archive lacks a usable
# ``pack.yaml``. Keep in sync with DEFAULT_V2_SELECTIONS — a third
# bundled pack must be added to both lists if the bundled fallback
# should also cover it.
DEFAULT_V2_SELECTION_NAMES: frozenset[str] = frozenset(
    sel["name"] for sel in DEFAULT_V2_SELECTIONS
)


def _resolve_manifest_path(root: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    bootstrap_dir = root / ".agent-config" / "repo" / "bootstrap"
    candidate = bootstrap_dir / "packs.yaml"
    if candidate.exists():
        return candidate
    return bootstrap_dir / "rule-packs.yaml"


def detect_host(args_host: str | None = None) -> str:
    """Resolve the active agent host from ``--host`` flag, env var, or default.

    Order (Codex Round 1 H3 + Round 2 M3):

      1. ``args_host`` — explicit ``--host {claude-code,codex}`` flag.
      2. ``AGENT_CONFIG_HOST`` env var. Bootstrap.{sh,ps1} sets this to
         ``claude-code`` so the v0.4.0 default behavior is unchanged;
         a Codex-driven CI matrix sets it to ``codex``.
      3. ``"claude-code"`` default (v0.4.0 backward compat).

    Returns one of ``KNOWN_HOSTS`` or raises ``ValueError`` so a typo
    surfaces at parse time rather than slipping through to the
    host-mismatch check on every active entry.

    An empty string in ``AGENT_CONFIG_HOST`` (e.g., from an unset shell
    init line) is treated as "not provided" and falls through to the
    default; only an explicit non-empty value is consulted.
    """
    if args_host:
        candidate = args_host
    else:
        env_value = os.environ.get("AGENT_CONFIG_HOST", "").strip()
        candidate = env_value or "claude-code"
    if candidate not in KNOWN_HOSTS:
        raise ValueError(
            f"unknown host {candidate!r}; expected one of {KNOWN_HOSTS}"
        )
    return candidate


def _process_selection(
    selection: dict[str, Any],
    *,
    bundled_manifest: dict[str, Any],
    cache_root: Path,
    host: str,
    pack_lock: dict[str, Any] | None = None,
    return_archive: bool = False,
) -> tuple[dict[str, Any], Any]:
    """Process one resolved selection — inline-source branch or bundled lookup.

    ``selection`` is the consumer's resolved ``agent-config.yaml`` entry
    (a dict). It may shape as either:

      - ``{"name": "<pack-name>"}`` — looked up in ``bundled_manifest``
        (the v2 manifest shipped under ``bootstrap/packs.yaml``); existing
        v0.4.0 path.
      - ``{"name": "<pack-name>", "source": {"url": "<URL>", "ref": "<ref>",
        "auth": "<method>"}, "update_policy": "<policy>"}`` — direct URL
        fetch via ``source_fetch.fetch_pack``. Bypasses the bundled
        manifest. ``source.repo`` is also accepted as a synonym for
        ``source.url`` to match the v0.4.0 manifest field name.

    Returns ``(pack_def, archive_or_dir_or_None)``. ``pack_def`` is the
    pack definition dict from the bundled or remote manifest. The second
    member depends on ``return_archive``:

      - ``return_archive=False`` (default, v0.4.0/Phase 4 callers + tests):
        the fetched ``PackArchive.archive_dir`` for inline-source entries,
        ``None`` for bundled. The composer threads this into
        ``DispatchContext.pack_source_dir``.
      - ``return_archive=True`` (Phase 8 ``_do_compose_v2``): the full
        :class:`source_fetch.PackArchive` so the caller can inspect
        ``resolved_commit`` for drift detection vs ``pack_lock``.

    ``host`` is reserved for future host-keyed cache slots / pack-yaml
    host filtering (v0.6+); accepted now to keep the call surface stable.

    Raises ``ComposeError`` when a bundled name is unknown or a remote
    ``pack.yaml`` does not declare the requested pack name.
    """
    name = selection["name"]
    source = selection.get("source") or {}
    if isinstance(source, str):
        # Bundled string source ("bundled" sentinel) — treated as
        # bundled lookup, no inline fetch. The bundled manifest may
        # carry source: bundled / source: bundled:aa for the v0.4.0
        # default packs.
        source = {}
    source_url = source.get("url") or source.get("repo")
    source_ref = source.get("ref") or "main"
    explicit_auth = source.get("auth")
    update_policy = selection.get("update_policy", "prompt")
    pack_lock = pack_lock or {}

    if source_url:
        # v0.5.0 inline-source path.
        recorded = (
            pack_lock.get("packs", {}).get(name, {}).get("resolved_commit")
        )
        archive = source_fetch.fetch_pack(
            source_url,
            source_ref,
            policy=update_policy,
            explicit_auth=explicit_auth,
            pack_lock_recorded_commit=recorded,
            cache_root=cache_root,
        )
        # Read the third-party manifest from the fetched archive; build
        # a name → pack lookup so the consumer-supplied ``name`` resolves
        # to one of the packs declared by the upstream repo's pack.yaml.
        # v0.5.4: when upstream pack.yaml is absent OR doesn't declare
        # the requested name, fall back to the bundled pack-def for
        # ``DEFAULT_V2_SELECTIONS`` names (e.g., agent-style v0.3.x
        # predates the v0.4.0 manifest format — the bundled
        # ``bootstrap/packs.yaml`` is the source of truth for what files
        # to copy). The fetched archive_dir still provides the byte
        # source for passive handlers via ``ctx.pack_source_dir``.
        manifest_path = archive.archive_dir / "pack.yaml"
        try:
            remote_manifest = schema.parse_manifest(manifest_path)
        except schema.ParseError:
            remote_manifest = None
        pack_def = None
        if remote_manifest is not None:
            packs_by_name = {p["name"]: p for p in remote_manifest.get("packs", [])}
            pack_def = packs_by_name.get(name)
        if pack_def is None and name in DEFAULT_V2_SELECTION_NAMES:
            # v0.5.4 fallback gated on ``DEFAULT_V2_SELECTION_NAMES`` so a
            # future non-default bundled pack does not silently inherit
            # this migration-only behavior (Codex Round 1 Low #1).
            pack_def = _bundled_pack_def(bundled_manifest, name)
        if pack_def is None:
            if remote_manifest is not None:
                declared = sorted(
                    p.get("name", "")
                    for p in remote_manifest.get("packs", [])
                )
                raise ComposeError(
                    f"remote pack.yaml at {source_url}@{source_ref} does not "
                    f"declare a pack named {name!r}; declared names: "
                    f"{declared}"
                )
            raise ComposeError(
                f"remote {source_url}@{source_ref} has no usable pack.yaml "
                f"at the archive root and {name!r} is not a bundled-default "
                f"pack; cannot determine pack definition"
            )
        if return_archive:
            return pack_def, archive
        return pack_def, archive.archive_dir

    # Bundled-manifest lookup; existing v0.4.0 path.
    pack_def = _bundled_pack_def(bundled_manifest, name)
    if pack_def is not None:
        return pack_def, None
    raise ComposeError(f"unknown bundled pack: {name!r}")


def _bundled_pack_def(
    bundled_manifest: dict[str, Any], name: str
) -> dict[str, Any] | None:
    """Look up a pack by name in the bundled v2 manifest.

    Returns the pack-def dict or ``None`` if not declared. Shared by the
    bundled-lookup path and the v0.5.4 inline-source fallback (which
    uses the bundled definition when the upstream archive has no
    ``pack.yaml`` — e.g., agent-style v0.3.x).
    """
    for pack in bundled_manifest.get("packs", []):
        if isinstance(pack, dict) and pack.get("name") == name:
            return pack
    return None


def main(argv: list[str] | None = None) -> int:
    # v0.5.2: ``compose_packs.py uninstall <name>`` is a sibling mode used
    # by ``anywhere-agents pack remove`` to drive the new single-pack
    # uninstall path. Keep the dispatch out of argparse so the existing
    # compose flag set is unchanged for v0.4.0/v0.5.0 callers.
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "uninstall":
        return _uninstall_main(raw[1:])

    parser = argparse.ArgumentParser(
        description="unified pack composer for anywhere-agents (v0.4.0+)"
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--print-yaml", metavar="PACK", default=None)
    parser.add_argument(
        "--host",
        choices=KNOWN_HOSTS,
        default=None,
        help=(
            "Active agent host for this compose run. Defaults to the "
            "AGENT_CONFIG_HOST env var, then to 'claude-code'. Threads "
            "through to DispatchContext.current_host so per-host active "
            "entries skip cleanly on the wrong host."
        ),
    )
    args = parser.parse_args(argv)

    if args.print_yaml:
        return legacy.main(argv)

    root = args.root.resolve()
    manifest_path = _resolve_manifest_path(root, args.manifest)

    if not manifest_path.exists():
        # Nothing to compose; legacy.main emits the appropriate error.
        return legacy.main(argv)

    try:
        parsed = schema.parse_manifest(manifest_path)
    except schema.ParseError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    if parsed["version"] == 1:
        # Legacy passive-only manifest: delegate.
        return legacy.main(argv)

    # Resolve the active host before lock acquisition; a typo in
    # ``--host`` should fail with a clear message before we serialize on
    # the user lock for 30 seconds.
    try:
        host = detect_host(args_host=args.host)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    # v0.5.0 Phase 8 carry-forward A: parse-time credential-URL rejection
    # across every config layer (user-level, project-tracked,
    # project-local). Per plan + Codex R1 M8 / Deferral 3, this check
    # runs BEFORE lock acquisition so a typo or pasted-token URL fails
    # immediately without serializing on the user lock for 30 seconds.
    # ``resolved_for_project`` reads all three YAML layers + the env var;
    # we only care about the URL validation side effect here, not the
    # returned selection list (the production composition still uses
    # the legacy resolver in ``_do_compose_v2`` for selection assembly).
    try:
        config_mod.resolved_for_project(
            root, validate_url_fn=auth.reject_credential_url,
        )
    except auth.CredentialURLError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except config_mod.ConfigError as exc:
        # Other config-shape errors (malformed YAML, wrong key types) —
        # the legacy resolver below will re-surface most of these via
        # RulePackError, but credential-URL rejection has to win first
        # so the redacted-URL path is hit before the legacy path raises
        # on the same file.
        sys.stderr.write(f"error: {exc}\n")
        return 1

    # v2 manifest: full composition here. Acquire the outer lock pair
    # before any state-mutating work runs (Phase 6 / Deferral 1):
    #
    #   * per-user lock — serializes ``~/.claude/pack-state.json`` and
    #     ``~/.claude/hooks/`` writes across two consumer repos
    #     bootstrapping at once.
    #   * per-repo lock — serializes the project's
    #     ``.agent-config/pack-state.json`` and inner transaction
    #     staging dir against any other composer in the same checkout.
    #
    # Both default to a 30-second wait; a ``locks.LockTimeout`` from
    # either is surfaced as exit 10 (matching the v0.4.0 uninstall
    # contract for "could not acquire lock" so consumers see one
    # consistent error code across pack-lifecycle entry points).
    user_lock = locks.user_lock_path(Path.home())
    repo_lock = locks.repo_lock_path(root)
    try:
        with locks.acquire(user_lock, timeout=30), locks.acquire(
            repo_lock, timeout=30
        ):
            # v0.5.0 Phase 8 carry-forward B: reconcile orphan staging
            # dirs left behind by an earlier crashed compose. Runs
            # under the same lock pair so a peer composer that started
            # mid-reconciliation can't observe a half-cleaned state.
            # Blocking orphans (DRIFT, MALFORMED, unreapplyable PARTIAL)
            # gate the run — surfaced via stderr and exit non-zero so
            # the consumer's bootstrap fails fast instead of layering
            # new mutations on top of an unresolved prior crash.
            report = reconciliation.reconcile_orphans(
                root, Path.home(), locks_held=True,
            )
            # Only print the summary when reconciliation actually did
            # something. Live-only counts are silent — a clean startup
            # with no orphans should produce no chatter on stderr.
            if (
                report.rolled_back
                or report.rolled_forward
                or report.partial_reapplied
                or report.blocking
            ):
                sys.stderr.write(
                    f"reconciliation: live={len(report.live)} "
                    f"rolled_back={len(report.rolled_back)} "
                    f"rolled_forward={len(report.rolled_forward)} "
                    f"reapplied={len(report.partial_reapplied)} "
                    f"blocking={len(report.blocking)}\n"
                )
            if report.blocking:
                paths = ", ".join(str(p) for p in report.blocking)
                sys.stderr.write(
                    f"error: reconciliation surfaced blocking orphan "
                    f"staging dir(s); resolve manually before "
                    f"rerunning compose: {paths}\n"
                )
                return 1
            return _do_compose_v2(root, parsed, args.no_cache, host=host)
    except locks.LockTimeout as exc:
        sys.stderr.write(f"compose aborted: {exc}\n")
        return 10
    except PackLockDriftAborted as exc:
        # ANYWHERE_AGENTS_UPDATE=fail in non-TTY context — pending updates
        # were detected and the operator wants the run to fail rather than
        # silently apply or defer. Distinct exit code from generic compose
        # errors (1) and lock-acquire timeout (10) so CI can branch on it.
        sys.stderr.write(
            f"compose aborted by ANYWHERE_AGENTS_UPDATE=fail: {exc}\n"
        )
        return 11


# v0.5.2: ``compose_packs.py uninstall <name>`` mode. Self-locks via the
# uninstall engine's own per-user + per-repo acquire path; the CLI does
# NOT hold outer locks across the subprocess invocation.
_UNINSTALL_EXIT_CODES = {
    "clean": 0,
    "no-op": 0,
    "lock-timeout": 10,
    "drift": 20,
    "malformed-state": 30,
    "partial-cleanup": 40,
}


def _uninstall_main(argv: list[str]) -> int:
    """``compose_packs.py uninstall <name>`` — single-pack uninstall mode.

    Drives the new :func:`scripts.packs.uninstall.run_uninstall_pack`
    helper. Used by ``anywhere-agents pack remove`` (v0.5.2). The CLI
    invokes this as a subprocess so the composer self-locks and runs
    drift checks in the same process that would otherwise install.
    """
    parser = argparse.ArgumentParser(
        prog="compose_packs.py uninstall",
        description="single-pack uninstall mode (v0.5.2)",
    )
    parser.add_argument("name", help="Pack name to remove")
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)

    root = args.root.resolve()
    from packs import uninstall as uninstall_mod
    outcome = uninstall_mod.run_uninstall_pack(root, args.name)

    sys.stderr.write(f"uninstall status: {outcome.status}\n")
    if outcome.packs_removed:
        sys.stderr.write(
            f"packs removed: {', '.join(outcome.packs_removed)}\n"
        )
    if outcome.files_deleted:
        sys.stderr.write(f"files deleted: {len(outcome.files_deleted)}\n")
    if outcome.owners_decremented:
        sys.stderr.write(
            f"owners decremented: {len(outcome.owners_decremented)}\n"
        )
    if outcome.drift_paths:
        sys.stderr.write(
            f"drift: {len(outcome.drift_paths)} path(s) left in place\n"
        )
        for p in outcome.drift_paths:
            sys.stderr.write(f"  - {p}\n")
    if outcome.lock_holder_pid is not None:
        sys.stderr.write(f"lock holder PID: {outcome.lock_holder_pid}\n")
    for detail in outcome.details:
        sys.stderr.write(f"{detail}\n")

    return _UNINSTALL_EXIT_CODES.get(outcome.status, 40)


def _do_compose_v2(
    root: Path, parsed: dict, no_cache: bool, *, host: str = "claude-code"
) -> int:
    # ----- resolve selections (reuse legacy's config parsing) -----
    try:
        tracked = legacy.parse_user_config(root / "agent-config.yaml")
        local = legacy.parse_user_config(root / "agent-config.local.yaml")
    except legacy.RulePackError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    env_val = os.environ.get("AGENT_CONFIG_RULE_PACKS", "") or os.environ.get(
        "AGENT_CONFIG_PACKS", ""
    )
    env_list = legacy.parse_env_packs(env_val) if env_val else []

    try:
        selections = legacy.resolve_selections(
            tracked, local, env_list, default=DEFAULT_V2_SELECTIONS
        )
    except legacy.RulePackError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    # ----- upstream AGENTS.md -----
    upstream_path = root / ".agent-config" / "AGENTS.md"
    if not upstream_path.exists():
        sys.stderr.write(
            f"error: upstream AGENTS.md not found at {upstream_path}; "
            "bootstrap should fetch it before invoking this helper\n"
        )
        return 1
    upstream = upstream_path.read_text(encoding="utf-8")

    # Opt-out: selections empty → write verbatim upstream and exit.
    if not selections:
        try:
            legacy.atomic_write(root / "AGENTS.md", upstream)
        except OSError as exc:
            sys.stderr.write(
                f"error: failed to write verbatim AGENTS.md: {exc}\n"
            )
            return 1
        return 0

    # ----- state setup -----
    project_lock_path = root / ".agent-config" / "pack-lock.json"
    project_state_path = root / ".agent-config" / "pack-state.json"
    user_state_path = Path.home() / ".claude" / "pack-state.json"

    pack_lock = state_mod.empty_pack_lock()
    # Load the previous pack-lock (if any) so per-selection drift detection
    # can compare archive.resolved_commit against the recorded commit.
    # Read-only — the new pack_lock dict above is what gets written at
    # the end. A missing or unreadable previous lock is treated as "no
    # recorded commits"; every fetch then runs as a first-time fetch.
    if project_lock_path.exists():
        try:
            previous_pack_lock = state_mod.load_pack_lock(project_lock_path)
        except state_mod.StateError as exc:
            sys.stderr.write(
                f"warning: previous pack-lock at {project_lock_path} "
                f"unreadable ({exc}); skipping drift detection\n"
            )
            previous_pack_lock = state_mod.empty_pack_lock()
    else:
        previous_pack_lock = state_mod.empty_pack_lock()
    project_state = state_mod.empty_project_state()
    try:
        user_state = state_mod.load_user_state(user_state_path)
    except state_mod.StateError as exc:
        sys.stderr.write(
            f"warning: user state at {user_state_path} unreadable "
            f"({exc}); starting fresh\n"
        )
        user_state = state_mod.empty_user_state()

    # ----- compose -----
    composed_agents = upstream
    cache_dir = root / ".agent-config" / "rule-packs"
    # Cache root for fetched archives (v0.5.0 inline-source path).
    # Distinct from cache_dir above (which is the v0.4.0 raw-URL cache).
    archive_cache_root = root / ".agent-config" / "cache"

    # Staging dir name matches Phase 2's reconciliation scan pattern
    # `*.staging-*` so a crashed composer is recoverable on next startup
    # once Phase 4 wires reconciliation into bootstrap.
    staging_dir = root / ".agent-config" / f"pack-compose.staging-{os.getpid()}"
    lock_path = root / ".agent-config" / ".pack-lock.lock"

    # ----- Phase 8 pre-fetch: resolve archives before any staging -----
    # Building the archive list outside the staging transaction lets us
    # detect drift (archive.resolved_commit != previous_pack_lock recorded
    # commit) and prompt the user for apply / skip BEFORE any handler
    # writes content into the staging dir. The skip path then re-fetches
    # the locked-version archive and the main compose loop runs against
    # that. ``resolved`` carries the per-selection
    # ``(pack_def, archive_or_none, recorded_commit_or_none)`` triple
    # used by both drift detection and the main loop below.
    resolved: list[tuple[dict, dict, Any, str | None]] = []
    pending_updates: list[tuple[dict, Any, dict]] = []
    try:
        for selection in selections:
            pack, archive = _process_selection(
                selection,
                bundled_manifest=parsed,
                cache_root=archive_cache_root,
                host=host,
                pack_lock=previous_pack_lock,
                return_archive=True,
            )
            recorded = (
                previous_pack_lock.get("packs", {})
                .get(selection["name"], {})
                .get("resolved_commit")
            )
            resolved.append((selection, pack, archive, recorded))
            # Drift gate: only inline-source entries (archive is a
            # PackArchive) can drift. Bundled lookups have archive=None.
            if (
                archive is not None
                and recorded is not None
                and archive.resolved_commit != recorded
            ):
                pending_updates.append((selection, archive, pack))
    except ComposeError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except source_fetch.PackLockDriftError as exc:
        # update_policy=locked drift surfaces here; pre-fetch reraises so
        # the operator gets a clean error before any staging happens.
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except (FileNotFoundError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"error: composition failed: {exc}\n")
        return 1

    # ----- Phase 8 drift decision -----
    # PATH 1 / 2: pending non-empty + apply (interactive Y or non-TTY
    # apply) → continue; clear pending-updates.json after commit.
    # PATH 3: pending non-empty + skip → re-fetch locked archives,
    # continue with locked content; write pending-updates.json so the
    # next session-start banner surfaces the deferred updates.
    # PATH 4: pending empty → unconditional clear so a stale file from a
    # previous skip cannot mislead the next session-start banner.
    decision = "apply"
    if pending_updates:
        try:
            decision = prompt_user_for_updates(pending_updates)
        except PackLockDriftAborted:
            # ANYWHERE_AGENTS_UPDATE=fail in non-TTY context. Persist the
            # pending list so the next session-start banner can surface
            # the still-deferred work, then re-raise so ``main`` exits 11.
            try:
                write_pending_updates_json(root, host, pending_updates)
            except OSError as exc:
                sys.stderr.write(
                    f"warning: failed to write pending-updates.json: {exc}\n"
                )
            raise
        if decision == "skip":
            # Revert each drifted archive to its locked-version commit by
            # loading from the local archive cache. We CANNOT re-call
            # source_fetch.fetch_pack with the recorded SHA as ref:
            # fetch_pack always runs resolve_ref_with_auth_chain first,
            # and ls-remote matches by refname only, so a 40-char SHA
            # returns empty and the auth chain raises
            # AuthChainExhaustedError. load_cached_archive is a pure
            # cache lookup that sidesteps the network entirely.
            reverted: dict[str, Any] = {}
            for selection, archive, pack_def in pending_updates:
                source = selection.get("source") or {}
                if not isinstance(source, dict):
                    source = {}
                source_url = source.get("url") or source.get("repo")
                explicit_auth = source.get("auth")
                recorded = (
                    previous_pack_lock.get("packs", {})
                    .get(selection["name"], {})
                    .get("resolved_commit")
                )
                # Drift detection guarantees ``recorded`` is a 40-char
                # SHA: an entry only joins ``pending_updates`` when
                # ``recorded is not None`` and differs from the new
                # commit. No fallback to source_ref needed here.
                locked_archive = source_fetch.load_cached_archive(
                    source_url,
                    recorded,
                    cache_root=archive_cache_root,
                )
                if locked_archive is None:
                    # Cold cache for the recorded commit (e.g., user
                    # cleaned the cache, or upstream history was rewritten
                    # so the recorded commit is no longer reachable in
                    # the cache slot we have). Surface as a warning and
                    # keep the newly-fetched archive; there is nothing
                    # to revert to without a network round-trip we are
                    # not authorized to make from the skip path.
                    sys.stderr.write(
                        f"warning: cannot revert {selection['name']!r} to "
                        f"recorded commit {recorded[:7]}: not in local "
                        f"cache; keeping newly-fetched archive\n"
                    )
                    continue
                reverted[selection["name"]] = locked_archive
            # Splice the locked archives back into ``resolved`` so the
            # main compose loop below sees the locked content.
            resolved = [
                (sel, pack, reverted.get(sel["name"], arc), rec)
                for (sel, pack, arc, rec) in resolved
            ]
            try:
                write_pending_updates_json(root, host, pending_updates)
            except OSError as exc:
                sys.stderr.write(
                    f"warning: failed to write pending-updates.json: {exc}\n"
                )

    # ----- Phase 8 main compose loop (uses resolved + maybe-reverted archives) -----
    outcomes: dict[str, str] = {}
    pending_names = {sel["name"] for (sel, _arc, _pack) in pending_updates}
    # v0.5.2: pre-compute the set of paths previously recorded in
    # ``pack-state.json`` so the drift gate can mark them as
    # ``PRESTATE_PACK_OUTPUT``. The previous project-state may be empty
    # (first install) — in that case nothing tracked, and unmanaged
    # collisions still get caught.
    prior_pack_outputs: dict[str, str] = {}
    if project_state_path.exists():
        try:
            prior_project_state = state_mod.load_project_state(project_state_path)
        except state_mod.StateError:
            prior_project_state = state_mod.empty_project_state()
        for entry in prior_project_state.get("entries", []) or []:
            output_path = entry.get("output_path")
            sha = entry.get("sha256")
            if isinstance(output_path, str) and isinstance(sha, str):
                prior_pack_outputs[str((root / output_path).resolve())] = sha
    try:
        with txn_mod.Transaction(staging_dir, lock_path) as txn:
            for selection, pack, archive, recorded in resolved:
                ctx = _build_ctx(
                    root=root,
                    pack=pack,
                    selection=selection,
                    txn=txn,
                    pack_lock=pack_lock,
                    project_state=project_state,
                    user_state=user_state,
                    host=host,
                    archive=archive,
                )

                # Passive entries first (concatenate into AGENTS.md).
                for passive_entry in pack.get("passive", []) or []:
                    composed_agents = passive_mod.handle_passive_entry(
                        passive_entry,
                        pack,
                        ctx,
                        upstream_agents_md=composed_agents,
                        cache_dir=cache_dir,
                        no_cache=no_cache,
                    )

                # Then active entries (dispatch by kind).
                for active_entry in pack.get("active", []) or []:
                    dispatch.dispatch_active(active_entry, ctx)

                ctx.finalize_pack_lock()

                # Build a per-selection outcome label for the summary.
                name = selection["name"]
                if archive is None:
                    outcomes[name] = "bundled"
                elif name in pending_names and decision == "skip":
                    locked_short = (recorded or "")[:7] or "locked"
                    outcomes[name] = f"locked at {locked_short}"
                elif name in pending_names and decision == "apply":
                    new_short = (archive.resolved_commit or "")[:7]
                    outcomes[name] = f"updated -> {new_short}"
                else:
                    outcomes[name] = "unchanged"

            # Stage all writes — state files + AGENTS.md — through the
            # same transaction so a state-validation error surfaces
            # before any output commits and partial state cannot leak.
            txn.stage_write(
                project_lock_path,
                _validated_state_bytes(state_mod.save_pack_lock, pack_lock),
            )
            txn.stage_write(
                project_state_path,
                _validated_state_bytes(state_mod.save_project_state, project_state),
            )
            if user_state.get("entries"):
                txn.stage_write(
                    user_state_path,
                    _validated_state_bytes(state_mod.save_user_state, user_state),
                )
            txn.stage_write(
                root / "AGENTS.md", composed_agents.encode("utf-8")
            )

            # v0.5.2: build the drift-gate ``expected_prestate`` map and
            # install it on the transaction before commit. The five
            # categories (per PLAN-aa-v0.5.2.md § "Write-path drift gate"):
            #
            # 1. PRESTATE_PACK_OUTPUT — paths recorded in the prior
            #    project-state's ``entries[*].output_path``. The recorded
            #    sha256 must match current on-disk content.
            # 2. PRESTATE_INTERNAL_STATE — pack-lock.json, project + user
            #    pack-state.json. Optimistic-concurrency check against the
            #    stage-time hash.
            # 3. PRESTATE_CORE_OUTPUT — composer-owned files like
            #    AGENTS.md. Same optimistic-concurrency rule.
            # 4. PRESTATE_JSON_MERGE — declared JSON merge targets from
            #    active-permission entries; user-level settings.json is
            #    the canonical case (the permission handler stages a
            #    full-file rewrite). Without this category a normal
            #    install would be rejected as an unmanaged collision.
            # 5. PRESTATE_UNMANAGED — anything else; if the file already
            #    exists, the gate refuses to clobber.
            internal_state_paths = {
                str(project_lock_path.resolve()),
                str(project_state_path.resolve()),
                str(user_state_path.resolve()),
            }
            core_output_paths = {
                str((root / "AGENTS.md").resolve()),
            }
            # Active-permission entries declare their JSON merge target
            # via the user-state entries the permission handler creates.
            # The handler's full-file rewrite of settings.json is keyed
            # by the leading path component before the first ``#``.
            json_merge_paths: set[str] = set()
            for ent in user_state.get("entries", []) or []:
                if ent.get("kind") != "active-permission":
                    continue
                target_path = ent.get("target_path", "")
                if "#" in target_path:
                    json_path = target_path.split("#", 1)[0]
                else:
                    json_path = target_path
                if json_path:
                    try:
                        json_merge_paths.add(str(Path(json_path).resolve()))
                    except OSError:
                        json_merge_paths.add(json_path)

            expected_prestate: dict[str, tuple[str, str | None]] = {}
            for op in txn.ops:
                kind = op["op"]
                if kind == txn_mod.OP_WRITE:
                    target_str = op["target_path"]
                elif kind == txn_mod.OP_RESTAMP:
                    target_str = op["new_path"]
                else:
                    continue
                # Resolve once so the keys match how the validator looks
                # them up. Both sides must agree on case + symlink form.
                try:
                    resolved_target = str(Path(target_str).resolve())
                except OSError:
                    resolved_target = target_str
                if resolved_target in internal_state_paths:
                    expected_prestate[target_str] = (
                        txn_mod.PRESTATE_INTERNAL_STATE,
                        None,
                    )
                elif resolved_target in core_output_paths:
                    expected_prestate[target_str] = (
                        txn_mod.PRESTATE_CORE_OUTPUT,
                        None,
                    )
                elif resolved_target in json_merge_paths:
                    expected_prestate[target_str] = (
                        txn_mod.PRESTATE_JSON_MERGE,
                        None,
                    )
                elif resolved_target in prior_pack_outputs:
                    expected_prestate[target_str] = (
                        txn_mod.PRESTATE_PACK_OUTPUT,
                        prior_pack_outputs[resolved_target],
                    )
                else:
                    expected_prestate[target_str] = (
                        txn_mod.PRESTATE_UNMANAGED,
                        None,
                    )
            txn.set_expected_prestate(expected_prestate)
    except txn_mod.DriftAbort as exc:
        sys.stderr.write(f"error: composer drift gate aborted commit:\n")
        for path, category, reason in exc.drift_paths:
            sys.stderr.write(f"  {path} ({category}): {reason}\n")
        sys.stderr.write(
            "Recovery: back up local edits, then rerun the original "
            "command (pack add, pack verify --fix, or anywhere-agents).\n"
        )
        return 1
    except ComposeError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except (
        state_mod.StateError,
        dispatch.DispatchError,
        legacy.RulePackError,
        source_fetch.PackLockDriftError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"error: composition failed: {exc}\n")
        return 1

    # v0.5.3: surface adopt-on-match results so the audit trail stays
    # visible alongside the per-pack summary. The drift gate adopts a
    # pre-existing unmanaged file into pack-lock when its on-disk sha256
    # matches what the pack would write (AC->AA migration, interrupted
    # prior pack add, team-clone first run, manual deploy).
    print_adoption_summary(txn.adopted_paths)

    # ----- Phase 8 post-commit: pending-updates.json invariant -----
    # Round 3 M4: clear pending-updates.json on every apply path AND on
    # the no-drift path so a stale file cannot mislead the next session.
    # Skip path already wrote the file above and must NOT clear here.
    if decision == "apply":
        try:
            clear_pending_updates_json(root)
        except OSError as exc:
            sys.stderr.write(
                f"warning: failed to clear pending-updates.json: {exc}\n"
            )

    # Concise per-pack summary (host + pending hint when drift deferred).
    print_compose_summary(
        selections,
        outcomes,
        pending_updates if decision == "skip" else [],
        host=host,
    )

    return 0


def _build_ctx(
    *,
    root: Path,
    pack: dict,
    selection: dict,
    txn: txn_mod.Transaction,
    pack_lock: dict,
    project_state: dict,
    user_state: dict,
    host: str = "claude-code",
    archive: Any = None,
    archive_dir: Path | None = None,
) -> dispatch.DispatchContext:
    """Assemble a DispatchContext for one pack's composition.

    Phase 4 wires:

    - ``host`` → ``DispatchContext.current_host`` (preserves v0.4.0 ABI
      per Codex Round 2 M3 — the field already exists with default
      ``"claude-code"``).
    - ``archive`` (full :class:`source_fetch.PackArchive`) → inline-
      source packs use ``archive.resolved_commit`` for the lock entry,
      ``archive.url`` / ``archive.ref`` for source identity, and
      ``archive.archive_dir`` for ``DispatchContext.pack_source_dir``.
      ``archive_dir`` is accepted as a back-compat shim for callers that
      only have the directory (Phase 4 tests construct contexts directly
      this way); when ``archive`` is also passed it wins.
    - Bundled packs (no ``archive``) fall back to ``<consumer>/.agent-
      config/repo/`` for ``pack_source_dir`` and use
      ``selection.ref`` / ``pack["default-ref"]`` as the resolved-commit
      placeholder, preserving v0.4.0 bundled-pack semantics.

    Codex Round 2 H1 fix: when ``archive is not None`` (inline-source
    pack), ``pack_resolved_commit`` carries the 40-char SHA from
    ``archive.resolved_commit`` so ``pack-lock.json`` records the true
    upstream commit. The previous code wrote the requested ref (e.g.,
    ``"v0.1.0"``) into the lock, causing every subsequent run to report
    drift and defeating the peeled-annotated-tag acceptance test.
    """
    if archive is not None:
        # Inline-source pack: archive carries the authoritative URL,
        # ref, resolved commit, and archive directory.
        source_url = archive.url
        pack_ref = archive.ref
        pack_resolved_commit = archive.resolved_commit
        pack_source_dir = archive.archive_dir
        # v0.5.2 banner item 7: at fetch time the resolved_commit IS the
        # current head. Record both so subsequent ``pack verify`` (network
        # ls-remote) or banner detection knows whether upstream has moved.
        pack_latest_known_head = archive.resolved_commit
        pack_fetched_at = datetime.now(timezone.utc).isoformat()
    else:
        source = pack.get("source")
        if isinstance(source, dict):
            source_url = source.get("repo") or source.get("url") or "bundled:aa"
            pack_ref = selection.get("ref") or source.get("ref") or "bundled"
        else:
            # Source absent or string: treat as bundled.
            source_url = source if isinstance(source, str) and source else "bundled:aa"
            pack_ref = selection.get("ref") or pack.get("default-ref") or "bundled"
        # Bundled-pack path: no real upstream commit available, so reuse
        # pack_ref as the placeholder (preserves v0.4.0 lock semantics
        # for bundled entries).
        pack_resolved_commit = pack_ref
        # Inline-source packs supply a PackArchive.archive_dir; bundled
        # packs use the consumer's `.agent-config/repo/` cache for
        # active handlers.
        pack_source_dir = (
            archive_dir if archive_dir is not None
            else root / ".agent-config" / "repo"
        )
        # Bundled packs have no remote head to track for v0.5.2 update
        # detection; leave both fields ``None``.
        pack_latest_known_head = None
        pack_fetched_at = None

    # Pack-level hosts default (pack-architecture.md:199) is explicitly
    # threaded into the context so dispatch._effective_hosts() can
    # inherit it when an active entry omits its own hosts:.
    pack_hosts_default = pack.get("hosts")

    return dispatch.DispatchContext(
        pack_name=pack["name"],
        pack_source_url=source_url,
        pack_requested_ref=pack_ref,
        pack_resolved_commit=pack_resolved_commit,
        pack_update_policy=pack.get("update_policy", "prompt"),
        pack_source_dir=pack_source_dir,
        project_root=root,
        user_home=Path.home(),
        repo_id=str(root),
        txn=txn,
        pack_lock=pack_lock,
        project_state=project_state,
        user_state=user_state,
        pack_hosts=pack_hosts_default,
        current_host=host,
        pack_latest_known_head=pack_latest_known_head,
        pack_fetched_at=pack_fetched_at,
    )


if __name__ == "__main__":
    sys.exit(main())
