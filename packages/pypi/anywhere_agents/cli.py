"""CLI for anywhere-agents.

Subcommands:
- Default (no subcommand): download + run the anywhere-agents shell
  bootstrap in the current directory. Refreshes AGENTS.md, skills,
  command pointers, and settings from the upstream repo. Same behavior
  as v0.3.x.
- ``pack add/remove/list``: manage the user-level pack config file
  (``$XDG_CONFIG_HOME/anywhere-agents/config.yaml`` on POSIX,
  ``%APPDATA%\\anywhere-agents\\config.yaml`` on Windows). Pack management
  writes only to the user-level file; project-level config is owned by
  consumer repos.
- ``uninstall --all``: remove every aa-pack-owned output from the
  current project via the composer's uninstall engine. Requires the
  project to have been bootstrapped (needs
  ``.agent-config/repo/scripts/packs/``). Exits with one of the six
  codes defined by pack-architecture.md § "CLI contract for ``uninstall
  --all``".

Invariant: when invoked with no subcommand, behavior is identical to
v0.3.x so existing usage continues unchanged.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__

REPO = "yzhao062/anywhere-agents"
BRANCH = "main"

# ----- shared helpers -----


def log(msg: str) -> None:
    print(f"[anywhere-agents] {msg}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Dispatch on the first positional arg.

    - ``pack`` → pack management subcommand.
    - ``uninstall`` → uninstall subcommand.
    - Otherwise (or no args) → default bootstrap behavior.
    """
    raw = argv if argv is not None else sys.argv[1:]
    # Peek at the first non-option arg to decide routing. This keeps
    # ``anywhere-agents --version`` / ``anywhere-agents --dry-run`` /
    # ``anywhere-agents`` on the existing bootstrap path.
    first_pos = next((a for a in raw if not a.startswith("-")), None)
    if first_pos == "pack":
        return _pack_main(None, raw[raw.index("pack") + 1:])
    if first_pos == "uninstall":
        return _uninstall_main(raw[raw.index("uninstall") + 1:])
    return _bootstrap_main(raw)


# ======================================================================
# Default bootstrap subcommand (v0.3.x behavior, unchanged)
# ======================================================================


def bootstrap_url(script_name: str) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/bootstrap/{script_name}"


def choose_script() -> tuple[str, list[str]]:
    """Return (script_name, interpreter_argv_prefix) for the current platform."""
    if platform.system() == "Windows":
        interpreter = shutil.which("pwsh") or shutil.which("powershell")
        if interpreter is None:
            raise RuntimeError("PowerShell is required on Windows but was not found on PATH.")
        return "bootstrap.ps1", [interpreter, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    bash = shutil.which("bash")
    if bash is None:
        raise RuntimeError("bash is required on macOS/Linux but was not found on PATH.")
    return "bootstrap.sh", [bash]


def _detect_legacy_ac() -> bool:
    """Detect a legacy ``agent-config`` (AC) bootstrap state.

    Returns True when **either** signal is present:

    1. ``.agent-config/repo/.git/config`` has a ``[remote "origin"]``
       URL matching ``yzhao062/agent-config(\\.git)?$``.
    2. ``.agent-config/upstream`` content (whitespace + CR stripped)
       equals ``yzhao062/agent-config``.

    Either signal → legacy AC; ``anywhere-agents`` should auto-migrate
    rather than the no-op path that v0.5.1 took.

    Both unreadable / unrecognized → returns False (fall through to
    the existing AA bootstrap path).
    """
    cwd = Path.cwd()
    upstream_file = cwd / ".agent-config" / "upstream"
    if upstream_file.exists():
        try:
            content = upstream_file.read_text(encoding="utf-8")
            stripped = content.replace("\r", "").strip()
            if stripped == "yzhao062/agent-config":
                return True
        except OSError:
            pass
    git_config = cwd / ".agent-config" / "repo" / ".git" / "config"
    if git_config.exists():
        try:
            text = git_config.read_text(encoding="utf-8", errors="replace")
            # Scan only lines under the [remote "origin"] section so a
            # repo with origin = fork.git and a separate `upstream` remote
            # pointing at agent-config is NOT classified as legacy AC.
            import re
            in_origin = False
            origin_re = re.compile(r'^\s*\[\s*remote\s+"origin"\s*\]\s*$', flags=re.IGNORECASE)
            other_section_re = re.compile(r'^\s*\[')
            url_re = re.compile(r'^\s*url\s*=\s*(\S+)', flags=re.IGNORECASE)
            ac_re = re.compile(r'(^|[:/])yzhao062/agent-config(\.git)?/?$', flags=re.IGNORECASE)
            for line in text.splitlines():
                if origin_re.match(line):
                    in_origin = True
                    continue
                if in_origin and other_section_re.match(line):
                    in_origin = False
                    continue
                if in_origin:
                    m = url_re.match(line)
                    if m and ac_re.search(m.group(1)):
                        return True
        except OSError:
            pass
    return False


def _migrate_legacy_ac() -> None:
    """Cross-platform delete of legacy AC bootstrap artifacts.

    Removes ``.agent-config/repo``, ``.agent-config/upstream``, and both
    bootstrap scripts in ``.agent-config/`` so the subsequent AA
    bootstrap re-clones from anywhere-agents.

    v0.5.4 also moves ``.claude/commands/`` aside to a timestamped
    backup directory. AC's bootstrap copies pointer files into
    ``.claude/commands/`` (one per skill it ships); AA's aa-core-skills
    pack also writes pointer files at the same paths but with bytes
    that may differ from AC. Without the move, AA's drift gate refuses
    to clobber the AC-leftover pointers (PRESTATE_UNMANAGED) and the
    migration aborts. The backup directory preserves user-customized
    files for manual merge.
    """
    import contextlib
    import stat
    from datetime import datetime, timezone

    def _force_remove(func, path, _exc_info):
        """``shutil.rmtree`` onerror callback for Windows-friendly cleanup.

        Git sparse-clone leaves ``.git/objects/pack/*.idx`` and similar
        files marked read-only on Windows; the default ``rmtree`` aborts
        with ``PermissionError`` on the first such file. This handler
        clears the read-only attribute and retries the failed operation.
        Any second failure is swallowed so the migration continues — the
        AA bootstrap below re-clones into the same path and overwrites
        whatever survives. Without this, a v0.5.4 migration aborts mid-
        delete on Windows with a half-cleaned ``.agent-config/repo/``.
        """
        try:
            os.chmod(path, stat.S_IWRITE)
        except OSError:
            pass
        try:
            func(path)
        except OSError:
            pass

    cwd = Path.cwd()
    for rel in ("repo",):
        target = cwd / ".agent-config" / rel
        if target.exists():
            # ``onerror`` is the pre-3.12 spelling; ``onexc`` is the
            # 3.12+ replacement. Use ``onerror`` for backward-compat down
            # to Python 3.9 (the package's minimum supported version).
            shutil.rmtree(target, onerror=_force_remove)
    for rel in ("upstream", "bootstrap.sh", "bootstrap.ps1"):
        target = cwd / ".agent-config" / rel
        with contextlib.suppress(FileNotFoundError):
            try:
                os.remove(target)
            except PermissionError:
                # Same Windows read-only quirk as above; clear and retry.
                try:
                    os.chmod(target, stat.S_IWRITE)
                    os.remove(target)
                except OSError:
                    pass
    cmds = cwd / ".claude" / "commands"
    if cmds.exists() and cmds.is_dir() and any(cmds.iterdir()):
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%SZ")
        backup = cwd / ".claude" / f"commands.bak-{ts}"
        # If the backup target already exists (rare; same-second migration
        # retry) bump with a counter so we don't clobber.
        suffix = 0
        final = backup
        while final.exists():
            suffix += 1
            final = cwd / ".claude" / f"commands.bak-{ts}-{suffix}"
        cmds.rename(final)
        log(
            f"ℹ Backed up {cmds} -> {final.name} so AA's aa-core-skills "
            f"can lay down fresh pointer files"
        )


def _bootstrap_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="anywhere-agents",
        description=(
            "Download and run the anywhere-agents shell bootstrap in the "
            "current directory. Refreshes AGENTS.md, skills, command pointers, "
            "and settings from the upstream repo."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would run without fetching or executing.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"anywhere-agents {__version__}",
    )
    args = parser.parse_args(argv)

    try:
        script_name, interpreter_argv = choose_script()
    except RuntimeError as e:
        log(str(e))
        return 2

    url = bootstrap_url(script_name)
    config_dir = Path(".agent-config")
    out_path = config_dir / script_name

    if args.dry_run:
        log(f"Would fetch: {url}")
        log(f"Would write: {out_path}")
        log(f"Would run:   {' '.join(interpreter_argv + [str(out_path)])}")
        return 0

    # v0.5.2: legacy AC detection. If the project was bootstrapped from
    # ``yzhao062/agent-config`` (the old upstream), wipe the cached repo
    # and bootstrap files so the AA bootstrap below re-clones from
    # ``yzhao062/anywhere-agents``.
    if _detect_legacy_ac():
        log("ℹ Migrating from agent-config bootstrap to anywhere-agents...")
        try:
            _migrate_legacy_ac()
        except OSError as exc:
            log(f"error: legacy AC delete failed: {exc}")
            return 2
        log("✓ Migration complete; proceeding with AA bootstrap.")

    config_dir.mkdir(parents=True, exist_ok=True)

    log(f"Fetching {script_name} from {url}")
    try:
        urllib.request.urlretrieve(url, out_path)  # noqa: S310 (user-controlled URL is hard-coded)
    except Exception as exc:  # pragma: no cover — network failure path
        log(f"Download failed: {exc}")
        return 1

    log("Running bootstrap (refreshes AGENTS.md, skills, settings)")
    try:
        result = subprocess.run(interpreter_argv + [str(out_path)], check=False)
    except FileNotFoundError as exc:
        log(f"Interpreter not found: {exc}")
        return 2

    if result.returncode != 0:
        log(f"Bootstrap exited with code {result.returncode}")
        return result.returncode

    # v0.5.4: post-bootstrap reconcile. The bootstrap script just cloned
    # the AA source and ran the composer with project-side selections
    # (typically just the bundled defaults on a fresh / migrated
    # project). User-level packs registered in ``user_config_path()``
    # have not been reconciled yet. Run ``pack verify --fix --yes`` so
    # any user-level packs (e.g., agent-pack profile / paper-workflow /
    # acad-skills) also land in one shot. Skipped when the user has no
    # user-level config file at all (fast no-op for fresh installs).
    user_config = _user_config_path()
    if user_config is not None and user_config.exists():
        log("Reconciling user-level packs (pack verify --fix --yes)...")
        try:
            fix_rc = main(["pack", "verify", "--fix", "--yes"])
        except SystemExit as exc:
            fix_rc = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:  # pragma: no cover - belt and suspenders
            log(f"warning: post-bootstrap reconcile raised: {exc}")
            fix_rc = 1
        if fix_rc != 0:
            log(
                f"warning: post-bootstrap reconcile returned rc={fix_rc}; "
                f"run `anywhere-agents pack verify --fix` manually to "
                f"deploy user-level packs."
            )

    return result.returncode


# ======================================================================
# pack subcommand: user-level config management
# ======================================================================


_USER_CONFIG_APP_DIR = "anywhere-agents"
_USER_CONFIG_FILENAME = "config.yaml"


def _user_config_path() -> Path | None:
    """Resolve the user-level config path per XDG / Windows conventions.

    Returns ``None`` if neither ``$HOME``/``$XDG_CONFIG_HOME`` nor
    ``%APPDATA%`` is set — callers surface an actionable error.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / _USER_CONFIG_APP_DIR / _USER_CONFIG_FILENAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / _USER_CONFIG_APP_DIR / _USER_CONFIG_FILENAME
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".config" / _USER_CONFIG_APP_DIR / _USER_CONFIG_FILENAME
    return None


def _load_user_config(path: Path) -> dict[str, Any]:
    """Load user-level config YAML. Missing file → empty dict; malformed
    → hard error (refuse to clobber on write)."""
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        log("error: PyYAML is required for pack management; install with `pip install pyyaml`")
        raise SystemExit(2)
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) if text.strip() else {}
    except Exception as exc:
        log(f"error: {path} is not valid YAML ({exc}); refusing to overwrite")
        raise SystemExit(2)
    if not isinstance(data, dict):
        log(f"error: {path} must be a mapping at top level (got {type(data).__name__})")
        raise SystemExit(2)
    return data


def _save_user_config(path: Path, data: dict[str, Any]) -> None:
    """Atomic write via temp + os.replace in the same directory."""
    try:
        import yaml
    except ImportError:
        log("error: PyYAML is required for pack management; install with `pip install pyyaml`")
        raise SystemExit(2)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _pack_main(path: Path | None, argv: list[str]) -> int:
    """Pack-management subcommand router.

    ``path`` may be ``None``; in that case the user-level config path is
    resolved from ``$HOME``/``$XDG_CONFIG_HOME``/``%APPDATA%``. Tests pass
    an explicit path to exercise the helpers without env-var fixtures.
    """
    parser = argparse.ArgumentParser(prog="anywhere-agents pack")
    sub = parser.add_subparsers(dest="action", required=True)

    p_add = sub.add_parser("add", help="Add a pack to user-level config")
    p_add.add_argument("source", help="Pack source (GitHub repository URL)")
    p_add.add_argument("--name", help="Override derived pack name (single-pack only)")
    p_add.add_argument("--ref", help="Pin to a specific ref (default: main)")
    p_add.add_argument(
        "--pack", action="append", default=[],
        help="Remote pack name to include; repeatable. Default: include all packs in the remote manifest.",
    )
    p_add.add_argument(
        "--type", choices=("skill", "rule"), default=None,
        help="Filter remote packs by slot: 'rule' = passive-only, 'skill' = include active too (default).",
    )

    p_remove = sub.add_parser("remove", help="Remove a pack from user-level config")
    p_remove.add_argument("name", help="Pack name to remove")

    p_list = sub.add_parser("list", help="List packs from user-level + current project")
    p_list.add_argument(
        "--drift", action="store_true",
        help="Read pack-lock entries and report packs whose upstream ref has moved.",
    )

    p_update = sub.add_parser(
        "update",
        help="Refresh a pack's user-config ref pin and re-run the project composer.",
    )
    p_update.add_argument("name", help="Pack name to update")
    p_update.add_argument(
        "--ref",
        help="New ref to pin. Default: keep the existing ref recorded in user-level config.",
    )

    p_verify = sub.add_parser(
        "verify",
        help="Audit pack deployment state across user-level + project-level + pack-lock.",
    )
    p_verify.add_argument(
        "--fix",
        action="store_true",
        help="Write missing rule_packs: entries to agent-config.yaml for user-level-only packs.",
    )
    p_verify.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation when applying --fix.",
    )

    args = parser.parse_args(argv)

    if path is None:
        path = _user_config_path()
    if path is None:
        log("error: cannot resolve user-level config home ($HOME / $XDG_CONFIG_HOME / %APPDATA% all unset)")
        return 2

    if args.action == "add":
        return _pack_add_v0_5(path, args)
    if args.action == "remove":
        return _pack_remove(path, args.name)
    if args.action == "list":
        if args.drift:
            return _pack_list_drift()
        return _pack_list(path)
    if args.action == "update":
        return _pack_update(path, args)
    if args.action == "verify":
        project_root = Path.cwd()
        if args.fix:
            return _pack_verify_fix(path, project_root, args)
        return _pack_verify(path, project_root, args)
    return 2  # unreachable due to argparse required=True


def _derive_pack_name(source: str, override: str | None) -> str:
    if override:
        return override
    # Strip .git suffix, take last path segment.
    stem = source.rstrip("/")
    if stem.endswith(".git"):
        stem = stem[:-4]
    return stem.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _pack_add(path: Path, source: str, name: str | None, ref: str | None) -> int:
    # Credential-URL check — reject HTTP(S) userinfo (tokens baked into
    # URLs) AND SSH URLs with password field in userinfo.
    import re
    from urllib.parse import urlsplit
    if re.match(r"^https?://[^/@]+@", source):
        log("error: credentials in a URL are unsafe; use 'git@' SSH, 'gh auth login', or 'GITHUB_TOKEN' env")
        return 2
    if source.startswith("ssh://") or source.startswith("git+ssh://"):
        try:
            parsed = urlsplit(source)
        except ValueError as exc:
            log(f"error: source URL {source!r} is malformed ({exc})")
            return 2
        if parsed.password is not None:
            log("error: credentials in a URL are unsafe; use 'git@' SSH, 'gh auth login', or 'GITHUB_TOKEN' env")
            return 2

    data = _load_user_config(path)

    # Normalize legacy rule_packs: → packs: on first write per
    # pack-architecture.md:382. The legacy key is accepted for read but
    # any CLI write migrates to the unified name so future reads are
    # consistent. Without this migration, adding a new pack to a file
    # that contained only rule_packs would silently drop the existing
    # legacy entries from effective config resolution.
    if "packs" not in data and "rule_packs" in data:
        legacy = data.pop("rule_packs")
        if legacy is None:
            legacy = []
        if not isinstance(legacy, list):
            log(f"error: {path} has a malformed 'rule_packs' entry (not a list)")
            return 2
        data["packs"] = list(legacy)
    elif "packs" in data and "rule_packs" in data:
        # Both present — packs: wins; drop the legacy alias so it
        # doesn't confuse future readers.
        data.pop("rule_packs", None)

    pack_name = _derive_pack_name(source, name)
    entry: dict[str, Any] = {"name": pack_name, "source": source}
    if ref:
        entry["ref"] = ref

    packs = data.get("packs")
    if packs is None:
        # First-add default preservation: seed with agent-style + the user's pack.
        data["packs"] = [{"name": "agent-style"}, entry]
        log(f"Seeded new user-level config at {path} with default agent-style + {pack_name}")
    elif not isinstance(packs, list):
        log(f"error: {path} has a malformed 'packs' entry (not a list)")
        return 2
    else:
        # Replace existing entry with same name; else append.
        for i, existing in enumerate(packs):
            if isinstance(existing, dict) and existing.get("name") == pack_name:
                packs[i] = entry
                log(f"Updated {pack_name!r} in {path}")
                break
        else:
            packs.append(entry)
            log(f"Added {pack_name!r} to {path}")

    _save_user_config(path, data)
    return 0


# ----------------------------------------------------------------------
# v0.5.0 pack add: remote-manifest expansion
# ----------------------------------------------------------------------


def _dedup_user_packs(packs: list[Any]) -> tuple[list[Any], int]:
    """Return ``(deduped_list, n_dropped)`` for a user-config packs list.

    Keeps the first occurrence per name; later duplicates are dropped.
    Non-dict entries and entries without ``name`` are preserved as-is.
    Earlier ``pack add`` versions and concurrent invocations could leave
    duplicate entries in user-level config; v0.5.4 self-heals at every
    ``_load_or_create_user_config`` call so the next write persists the
    deduped list.
    """
    seen: set[str] = set()
    deduped: list[Any] = []
    n_dropped = 0
    for entry in packs:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            name = entry["name"]
            if name in seen:
                n_dropped += 1
                continue
            seen.add(name)
        deduped.append(entry)
    return deduped, n_dropped


def _load_or_create_user_config(path: Path) -> dict[str, Any]:
    """Return existing user-level config or a fresh empty dict.

    Mirrors :func:`_load_user_config` but tolerates a missing file
    (returns ``{}``), migrates legacy ``rule_packs:`` to ``packs:``, and
    silently dedups duplicate-name entries (v0.5.4).
    """
    if not path.exists():
        return {}
    data = _load_user_config(path)
    if "packs" not in data and "rule_packs" in data:
        legacy = data.pop("rule_packs")
        if isinstance(legacy, list):
            data["packs"] = list(legacy)
        else:
            data["packs"] = []
    elif "packs" in data and "rule_packs" in data:
        data.pop("rule_packs", None)
    packs = data.get("packs")
    if isinstance(packs, list):
        deduped, _ = _dedup_user_packs(packs)
        data["packs"] = deduped
    return data


def _write_user_config(path: Path, data: dict[str, Any]) -> None:
    """Atomic write helper for user-level config. Thin wrapper around
    :func:`_save_user_config` to match the helper name used in the
    Phase 9 plan."""
    _save_user_config(path, data)


def _is_in_project() -> bool:
    """Return True when the cwd looks like a bootstrapped consumer project.

    Per PLAN-aa-v0.5.2.md § 1 step 2 the project signal is the presence
    of the bootstrapped composer at
    ``.agent-config/repo/scripts/compose_packs.py``. As a fallback, the
    presence of either bootstrap script also counts (covers a stale
    sparse clone where compose_packs.py is gated on a future sparse
    pattern). ``agent-config.yaml`` is intentionally NOT a signal: a
    consumer can declare packs in YAML without ever bootstrapping, and
    we don't want to pretend they're in-project.
    """
    cwd = Path.cwd()
    if (cwd / ".agent-config" / "repo" / "scripts" / "compose_packs.py").exists():
        return True
    if (cwd / ".agent-config" / "bootstrap.sh").exists():
        return True
    if (cwd / ".agent-config" / "bootstrap.ps1").exists():
        return True
    return False


def _identity_tuple(entry: dict[str, Any]) -> tuple[str, str]:
    """Return ``(normalized_url, ref)`` for a user-config entry.

    The composite tuple is the v0.5.2 identity key. Used by the same-name
    detection pass: same name AND matching identity is idempotent;
    matching name with different identity is rc=1.
    """
    from anywhere_agents.packs import source_fetch as _src
    source = entry.get("source")
    if isinstance(source, dict):
        url = source.get("url") or source.get("repo") or ""
        # Source-backed entries default to "main" when ref is omitted, so
        # config rows authored without an explicit ref classify identical
        # to the lock entry the composer writes (which also defaults to "main").
        ref = source.get("ref") or entry.get("ref") or ("main" if url else "")
    elif isinstance(source, str):
        url = source
        ref = entry.get("ref") or "main"
    else:
        url = ""
        ref = entry.get("ref") or ""
    return (_src.normalize_pack_source_url(url), ref)


def _user_config_lock_path(user_config_path: Path) -> Path:
    """Sibling lock file for user-config rewrites.

    Used to serialize concurrent ``pack add`` / ``pack remove`` against
    each other on the same machine. The composer also takes its own
    user-level lock for state mutations (``~/.claude/pack-state.json``);
    those two locks are independent so a config-only rewrite doesn't
    block a composer in another project.
    """
    return user_config_path.with_name(user_config_path.name + ".lock")


def _append_to_user_config(
    user_config_path: Path,
    selected_packs: list[tuple[str, dict]],
    source_url: str,
    requested_ref: str,
) -> tuple[int, list[str]]:
    """Append packs to user-level config with idempotent / mismatch rules.

    Returns ``(rc, names)``. ``rc=0`` is success; ``rc=1`` is identity
    mismatch (same name, different ``(url, ref)``). ``names`` lists the
    pack names that were either appended or already present with
    matching identity.

    Caller is expected to hold the user-level config lock around this
    call so a concurrent ``pack add`` cannot interleave between read
    and write.
    """
    user_config = _load_or_create_user_config(user_config_path)
    existing = user_config.get("packs", []) or []
    existing_by_name: dict[str, dict] = {}
    for entry in existing:
        if isinstance(entry, dict) and "name" in entry:
            existing_by_name[entry["name"]] = entry
    incoming_identity = (
        _identity_tuple({"source": {"url": source_url, "ref": requested_ref}})
    )
    new_entries: list[dict] = []
    appended_or_idempotent: list[str] = []
    for name, _pack_def in selected_packs:
        new_row = {
            "name": name,
            "source": {"url": source_url, "ref": requested_ref},
        }
        if name in existing_by_name:
            existing_identity = _identity_tuple(existing_by_name[name])
            if existing_identity == incoming_identity:
                # Idempotent: same identity already registered.
                appended_or_idempotent.append(name)
                continue
            # Same name, different identity → rc=1, no writes.
            log(
                f"error: pack {name!r} already registered with different "
                f"identity ({existing_identity[0]} @ {existing_identity[1]}); "
                f"incoming ({incoming_identity[0]} @ {incoming_identity[1]}). "
                "Use `pack remove` first if you intended to replace."
            )
            return 1, []
        new_entries.append(new_row)
        appended_or_idempotent.append(name)
    if new_entries:
        user_config.setdefault("packs", []).extend(new_entries)
        _write_user_config(user_config_path, user_config)
    return 0, appended_or_idempotent


def _append_to_project_config(
    project_yaml: Path,
    selected_packs: list[tuple[str, dict]],
    source_url: str,
    requested_ref: str,
) -> int:
    """Append packs to ``agent-config.yaml`` ``rule_packs:`` list.

    Same identity rules as ``_append_to_user_config``: matching name
    with matching identity is idempotent; matching name with different
    identity is rc=1. Creates the file as ``{rule_packs: [...]}`` when
    missing.
    """
    try:
        import yaml
    except ImportError:
        log("error: PyYAML is required (install: `pip install pyyaml`)")
        return 2
    if project_yaml.exists():
        try:
            text = project_yaml.read_text(encoding="utf-8")
            data = yaml.safe_load(text) if text.strip() else {}
        except Exception as exc:
            log(f"error: {project_yaml} is not valid YAML ({exc}); refusing to overwrite")
            return 2
        if not isinstance(data, dict):
            log(
                f"error: {project_yaml}: top level must be a mapping; "
                "refusing to overwrite."
            )
            return 2
    else:
        data = {}
    existing = data.get("rule_packs") or []
    if not isinstance(existing, list):
        log(
            f"error: {project_yaml}: 'rule_packs' must be a list; "
            "refusing to overwrite."
        )
        return 2
    existing_by_name: dict[str, dict] = {}
    for entry in existing:
        if isinstance(entry, dict) and "name" in entry:
            existing_by_name[entry["name"]] = entry
        elif isinstance(entry, str):
            existing_by_name[entry] = {"name": entry}
    incoming_identity = (
        _identity_tuple({"source": {"url": source_url, "ref": requested_ref}})
    )
    new_entries = []
    for name, _pack_def in selected_packs:
        new_row = {
            "name": name,
            "source": {"url": source_url, "ref": requested_ref},
        }
        if name in existing_by_name:
            existing_identity = _identity_tuple(existing_by_name[name])
            if existing_identity == incoming_identity:
                continue  # idempotent
            log(
                f"error: pack {name!r} already in {project_yaml.name} with "
                f"different identity ({existing_identity[0]} @ "
                f"{existing_identity[1]}); incoming ({incoming_identity[0]} "
                f"@ {incoming_identity[1]}). Edit the file manually if you "
                "intended to replace."
            )
            return 1
        new_entries.append(new_row)
    if not new_entries:
        return 0
    existing.extend(new_entries)
    data["rule_packs"] = existing
    out_text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    project_yaml.parent.mkdir(parents=True, exist_ok=True)
    tmp = project_yaml.with_name(project_yaml.name + ".tmp")
    tmp.write_text(out_text, encoding="utf-8")
    os.replace(str(tmp), str(project_yaml))
    return 0


def _bundled_composer_path() -> Path | None:
    """Return the PyPI-wheel bundled composer path when available."""
    candidate = (
        Path(__file__).resolve().parent
        / "composer"
        / "scripts"
        / "compose_packs.py"
    )
    return candidate if candidate.exists() else None


def _invoke_composer(
    project_root: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
) -> int:
    """Subprocess invocation of the current package's composer.

    The composer self-locks (per-user + per-repo) so the CLI must NOT
    hold any outer lock across this call. ``args`` are passed through
    verbatim (e.g., ``"uninstall <name>"`` for the v0.5.2 single-pack
    uninstall mode).

    A bootstrapped project is still required, but the CLI prefers the
    composer bundled with the installed PyPI package over the potentially
    stale project-local copy. This lets ``pipx install --force`` repair a
    project even when ``.agent-config/repo`` is one release behind or the
    bootstrap network refresh failed.
    """
    project_composer = (
        project_root / ".agent-config" / "repo" / "scripts" / "compose_packs.py"
    )
    if not project_composer.exists():
        log(
            f"error: composer not found at {project_composer}. Run bootstrap first."
        )
        return 2
    composer = _bundled_composer_path() or project_composer
    cmd = [sys.executable, str(composer)] + list(args)
    if not args:
        cmd.extend(["--root", str(project_root)])
    env = None
    if env_extra:
        env = dict(os.environ)
        env.update(env_extra)
    result = subprocess.run(cmd, cwd=str(project_root), env=env, check=False)
    return result.returncode


def _pack_add_v0_5(user_config_path: Path, args) -> int:
    """``pack add <url>`` — one-shot install (v0.5.2).

    Behavior split:

    - **Outside a bootstrapped project**: append rows to user-level
      config only; print a hint that the user must run ``anywhere-agents``
      inside a project to deploy.
    - **In a bootstrapped project**: append to user-level config,
      append to project ``agent-config.yaml`` ``rule_packs:``, then
      invoke the composer subprocess so the install lands on disk in
      one shot.

    Identity rules apply to both layers: same ``(name, normalized_url,
    ref)`` is idempotent; same ``name`` with different identity is
    rc=1. Composer drift-abort leaves config rows committed but no
    on-disk install; recovery is to back up local edits and rerun.
    """
    # Credential-URL safety check first (no network).
    import re
    from urllib.parse import urlsplit
    source = args.source
    if re.match(r"^https?://[^/@]+@", source):
        log("error: credentials in a URL are unsafe; use 'git@' SSH, 'gh auth login', or 'GITHUB_TOKEN' env")
        return 2
    if source.startswith("ssh://") or source.startswith("git+ssh://"):
        try:
            parsed = urlsplit(source)
        except ValueError as exc:
            log(f"error: source URL {source!r} is malformed ({exc})")
            return 2
        if parsed.password is not None:
            log("error: credentials in a URL are unsafe; use 'git@' SSH, 'gh auth login', or 'GITHUB_TOKEN' env")
            return 2

    from anywhere_agents.packs import auth, source_fetch, schema

    try:
        archive = source_fetch.fetch_pack(args.source, args.ref or "main")
    except auth.AuthChainExhaustedError as exc:
        log(f"error: could not fetch {args.source}@{args.ref or 'main'}: {exc}")
        return 2
    except source_fetch.PackLockDriftError as exc:
        log(f"error: pack-lock drift: {exc}")
        return 2

    try:
        remote_manifest = schema.parse_manifest(archive.archive_dir / "pack.yaml")
    except schema.ParseError as exc:
        log(f"error: remote pack.yaml is malformed: {exc}")
        return 2

    remote_packs = remote_manifest.get("packs", [])
    packs_by_name = {p["name"]: p for p in remote_packs}

    # Resolve --pack / --name / --type filters into the list of
    # ``(output_name, pack_def)`` pairs to register.
    if args.pack:
        selected_pairs: list[tuple[str, str]] = [(name, name) for name in args.pack]
        if args.name:
            log(
                f"warning: --name {args.name!r} ignored; "
                f"applies only when remote manifest has exactly 1 pack and no --pack filter"
            )
    elif args.name and len(remote_packs) == 1:
        only_remote_name = remote_packs[0]["name"]
        selected_pairs = [(only_remote_name, args.name)]
    else:
        if args.name:
            log(
                f"warning: --name {args.name!r} ignored; "
                f"applies only when remote manifest has exactly 1 pack and no --pack filter"
            )
        selected_pairs = [(p["name"], p["name"]) for p in remote_packs]

    selected_packs: list[tuple[str, dict]] = []
    for remote_name, output_name in selected_pairs:
        pack = packs_by_name.get(remote_name)
        if pack is None:
            print(
                f"warning: pack {remote_name!r} not in remote manifest; skipping",
                file=sys.stderr,
            )
            continue
        if args.type == "rule" and pack.get("active"):
            continue
        selected_packs.append((output_name, pack))

    if not selected_packs:
        log("warning: no packs matched the filter; nothing written")
        return 0

    requested_ref = args.ref or "main"

    # Step 4a: take user-level config lock; idempotent append.
    from anywhere_agents.packs import locks as locks_mod
    user_lock = _user_config_lock_path(user_config_path)
    user_config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with locks_mod.acquire(user_lock, timeout=30):
            rc, written_names = _append_to_user_config(
                user_config_path, selected_packs, args.source, requested_ref,
            )
    except locks_mod.LockTimeout as exc:
        log(f"error: could not acquire user-config lock: {exc}")
        return 10
    if rc != 0:
        return rc

    in_project = _is_in_project()
    if not in_project:
        if written_names:
            log(
                f"Added {len(written_names)} pack(s) to {user_config_path}: "
                f"{', '.join(written_names)}"
            )
        log(
            "ℹ Registered globally. Run `anywhere-agents` in a "
            "bootstrapped project to deploy."
        )
        return 0

    # Step 4b: in-project — atomic-write project agent-config.yaml.
    project_root = Path.cwd().resolve()
    project_yaml = project_root / "agent-config.yaml"
    rc = _append_to_project_config(
        project_yaml, selected_packs, args.source, requested_ref,
    )
    if rc != 0:
        return rc

    # Step 4c: invoke the composer subprocess. Composer self-locks.
    rc = _invoke_composer(project_root)
    if rc != 0:
        log(
            "error: pack add updated configs but composer failed "
            f"(rc={rc}). Configs are persistent; back up local edits to "
            "managed files and rerun `anywhere-agents pack add` (idempotent) "
            "or `anywhere-agents pack verify --fix` to retry deploy."
        )
        return rc

    summary_parts: list[str] = []
    for name, pack_def in selected_packs:
        n_rules = len(pack_def.get("passive", []) or [])
        actives = pack_def.get("active", []) or []
        n_skills = sum(1 for a in actives if a.get("kind") == "skill")
        n_commands = sum(1 for a in actives if a.get("kind") == "command")
        summary_parts.append(
            f"{name} — {n_rules} rules, {n_skills} skills, {n_commands} commands"
        )
    log(
        f"✓ Installed {len(selected_packs)} pack(s) from {args.source}:\n  "
        + "\n  ".join(summary_parts)
    )
    return 0


# ----------------------------------------------------------------------
# v0.5.0 pack update: refresh a pinned ref + invoke project composer
# ----------------------------------------------------------------------


def _pack_update(user_config_path: Path, args) -> int:
    """Refresh a pack's user-level ref pin and trigger a project re-compose.

    ``pack update`` rewrites the ref pin and delegates the actual update
    to the current package's bundled composer with
    ``ANYWHERE_AGENTS_UPDATE=apply`` set in the environment. A
    bootstrapped project-local composer must still exist as the project
    signal, but it is not trusted to be current.
    """
    from anywhere_agents.packs import auth

    if not user_config_path.exists():
        log(
            f"error: pack {args.name!r} not in user config; use `pack add` first"
        )
        return 2
    user_config = _load_or_create_user_config(user_config_path)
    packs = user_config.get("packs", [])
    if not isinstance(packs, list):
        log(f"error: {user_config_path} has a malformed 'packs' entry (not a list)")
        return 2

    matching = [
        e for e in packs
        if isinstance(e, dict) and e.get("name") == args.name
    ]
    if not matching:
        log(
            f"error: pack {args.name!r} not in user config; use `pack add` first"
        )
        return 2
    entry = matching[0]
    source = entry.get("source")
    if isinstance(source, str):
        url = source
        existing_ref = entry.get("ref") or "main"
        # Promote string-source entries to dict-source on update so the
        # rewrite below has a place to land.
        entry["source"] = {"url": url, "ref": existing_ref}
        source = entry["source"]
    elif isinstance(source, dict):
        url = source.get("url") or source.get("repo")
        existing_ref = source.get("ref") or entry.get("ref") or "main"
        if not isinstance(url, str) or not url:
            log(
                f"error: pack {args.name!r} source has no 'url'/'repo' field"
            )
            return 2
    else:
        log(
            f"error: pack {args.name!r} source is missing or malformed"
        )
        return 2

    new_ref = args.ref or existing_ref

    # Codex Round 2 H3-B: pre-validate the URL so a credential-bearing
    # entry in user-config (legacy hand-edited file with
    # ``https://ghp_TOKEN@github.com/...``) is rejected before any
    # network call AND before the URL appears in any error message.
    try:
        auth.reject_credential_url(url, source_layer="user-config")
    except auth.CredentialURLError as exc:
        log(f"error: {exc}")
        return 2

    try:
        resolved_commit, _method = auth.resolve_ref_with_auth_chain(url, new_ref)
    except auth.CredentialURLError as exc:
        # Defense-in-depth (auth.resolve_ref_with_auth_chain also
        # validates) — keep the redacted CLI error path symmetric.
        log(f"error: {exc}")
        return 2
    except auth.AuthChainExhaustedError as exc:
        safe_url = auth.redact_url_userinfo(url)
        log(f"error: could not resolve {safe_url}@{new_ref}: {exc}")
        return 2
    log(f"resolved {auth.redact_url_userinfo(url)}@{new_ref} -> {resolved_commit[:7]}")

    source["ref"] = new_ref
    # Drop a top-level "ref" key if present so the dict-source ref is the
    # single source of truth.
    entry.pop("ref", None)
    _write_user_config(user_config_path, user_config)

    project_root = Path.cwd()
    return _invoke_composer(
        project_root,
        env_extra={"ANYWHERE_AGENTS_UPDATE": "apply"},
    )


# ----------------------------------------------------------------------
# v0.5.0 pack list --drift: read-only audit using auth-aware ls-remote
# ----------------------------------------------------------------------


def _read_all_pack_lock_entries() -> list[dict[str, Any]] | None:
    """Read every pack entry from ``.agent-config/pack-lock.json``.

    Returns a list of dicts each with at least ``name``, ``source_url``,
    ``requested_ref``, and ``resolved_commit``. Returns an empty list
    when no pack-lock exists or it has no packs. Returns ``None`` when
    the pack-lock file is present but unreadable / corrupt JSON, so the
    caller can distinguish "no data" from "error reading data".
    """
    lock_path = Path.cwd() / ".agent-config" / "pack-lock.json"
    if not lock_path.exists():
        return []
    import json
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"error: cannot read {lock_path}: {exc}")
        return None
    packs = data.get("packs") if isinstance(data, dict) else None
    if not isinstance(packs, dict):
        return []
    entries: list[dict[str, Any]] = []
    for name, body in packs.items():
        if not isinstance(body, dict):
            continue
        entries.append({
            "name": name,
            "source_url": body.get("source_url", ""),
            "requested_ref": body.get("requested_ref", ""),
            "resolved_commit": body.get("resolved_commit", ""),
        })
    return entries


def _pack_list_drift() -> int:
    """Read pack-lock + run auth-aware ls-remote per entry.

    Read-only audit: prints drifted packs (current → new commit). On
    ``auth.AuthChainExhaustedError`` for a single entry, prints a
    warning to stderr and continues with the remaining entries.
    """
    from anywhere_agents.packs import auth

    entries = _read_all_pack_lock_entries()
    if entries is None:
        # Pack-lock present but unreadable — surface as error rc=2 so
        # users do not interpret silent "no drift" as a clean state.
        return 2
    drifted: list[tuple[str, str, str]] = []
    for entry in entries:
        url = entry["source_url"]
        ref = entry["requested_ref"]
        if not url or not ref:
            continue
        # Codex Round 2 H3-B: pre-validate per entry so a
        # credential-bearing URL recorded in pack-lock (e.g., legacy
        # hand-edited lock from a pre-v0.5.0 release) is rejected for
        # this entry without leaking the token into the audit's stderr.
        try:
            auth.reject_credential_url(url, source_layer="pack-lock")
            new_commit, _ = auth.resolve_ref_with_auth_chain(url, ref)
        except auth.CredentialURLError as exc:
            print(
                f"  {entry['name']:20s} (unsafe source URL: {exc})",
                file=sys.stderr,
            )
            continue
        except auth.AuthChainExhaustedError as exc:
            print(
                f"  {entry['name']:20s} (could not resolve: {exc})",
                file=sys.stderr,
            )
            continue
        if new_commit != entry["resolved_commit"]:
            drifted.append(
                (entry["name"], entry["resolved_commit"], new_commit)
            )
    if not drifted:
        print("no drift")
        return 0
    for name, old, new in drifted:
        print(f"  {name:20s} {old[:7]} -> {new[:7]}")
    return 0


# ----------------------------------------------------------------------
# v0.5.x pack verify: deployment-state audit + opt-in --fix
# ----------------------------------------------------------------------

# State labels — kept stable for test assertions and tooling integration.
_VERIFY_STATE_DEPLOYED = "deployed"
_VERIFY_STATE_DEPLOYED_NOT_LOCKED = "deployed, not locked"
_VERIFY_STATE_BUNDLED_DRIFT = "bundled default updated"
_VERIFY_STATE_USER_ONLY = "user-level only"
_VERIFY_STATE_MISMATCH = "config mismatch"
_VERIFY_STATE_DECLARED = "declared, not bootstrapped"
_VERIFY_STATE_MISSING = "missing"
_VERIFY_STATE_BROKEN = "broken state"
_VERIFY_STATE_LOCK_STALE = "lock schema stale"
_VERIFY_STATE_ORPHAN = "orphan"

_STATE_GLYPHS = {
    _VERIFY_STATE_DEPLOYED: "✅",       # ✅
    _VERIFY_STATE_DEPLOYED_NOT_LOCKED: "⚠",
    _VERIFY_STATE_BUNDLED_DRIFT: "⚠",
    _VERIFY_STATE_USER_ONLY: "⚠",      # ⚠
    _VERIFY_STATE_MISMATCH: "\U0001f500",   # 🔀
    _VERIFY_STATE_DECLARED: "\U0001f6ab",   # 🚫
    _VERIFY_STATE_MISSING: "❌",
    _VERIFY_STATE_BROKEN: "❌",         # ❌
    _VERIFY_STATE_LOCK_STALE: "\U0001f4dc", # 📜
    _VERIFY_STATE_ORPHAN: "\U0001f47b",     # 👻
}

# Default project selections seeded when no durable config signal exists.
# Mirrors compose_packs.DEFAULT_V2_SELECTIONS so verify and bootstrap see
# the same baseline.
_DEFAULT_V2_SELECTIONS = ("agent-style", "aa-core-skills")
_BUNDLED_IDENTITY_URL = "bundled:aa"
_BUNDLED_IDENTITY_REF = "bundled"
_AGENT_STYLE_BLOCK_RE = re.compile(
    r"<!--\s*rule-pack:agent-style:begin(?:\s|>)[\s\S]*?"
    r"<!--\s*rule-pack:agent-style:end\s*-->",
    re.IGNORECASE,
)
# Legacy markerless signatures lifted from agent-style v0.3.2 upstream
# (https://github.com/yzhao062/agent-style @ v0.3.2). All three
# substrings must currently be present to count an AGENTS.md as carrying
# the agent-style content without a composer-emitted block wrapper.
# Revisit these signatures when agent-style bumps to a release that
# renames any of these headers.
_AGENT_STYLE_LEGACY_SIGNATURES = (
    "# The Elements of Agent Style",
    "#### RULE-01: Do Not Assume the Reader Shares Your Tacit Knowledge",
    "#### RULE-H: Support Factual Claims with Citation or Concrete Evidence",
)


class _VerifyParseError(Exception):
    """Raised when verify cannot parse a config or lock file at all."""


def _normalize_url(url) -> str:
    """Wrapper around the vendored ``normalize_pack_source_url`` helper."""
    if not isinstance(url, str) or not url:
        return ""
    from anywhere_agents.packs import source_fetch
    return source_fetch.normalize_pack_source_url(url)


def _project_clone_packs_yaml_path(project_root: Path) -> Path:
    """Path to the project's bootstrap-clone packs.yaml (may be stale)."""
    return project_root / ".agent-config" / "repo" / "bootstrap" / "packs.yaml"


def _manifest_pack_from_path(
    manifest: Path | None,
    name: str,
    *,
    strict: bool,
) -> dict | None:
    """Read ``packs.yaml`` and return the entry named ``name``, or ``None``.

    ``strict=True`` propagates :class:`_VerifyParseError` on malformed YAML
    (used for the project-clone fallback so a corrupt clone surfaces a
    real error rather than silent mis-classification). ``strict=False``
    swallows parse errors (used for the wheel-bundled primary lookup;
    we'd rather fall back to the project clone than crash verify on a
    malformed wheel artifact).
    """
    if manifest is None or not manifest.exists():
        return None
    try:
        data = _read_yaml_or_none(manifest) or {}
    except _VerifyParseError:
        if strict:
            raise
        return None
    packs = data.get("packs") if isinstance(data, dict) else None
    if not isinstance(packs, list):
        return None
    for pack in packs:
        if isinstance(pack, dict) and pack.get("name") == name:
            return pack
    return None


def _identity_from_manifest_pack(name: str, pack: dict | None):
    """Build a 5-tuple identity from a packs.yaml ``packs[]`` entry."""
    if isinstance(pack, dict):
        source = pack.get("source")
        if isinstance(source, dict):
            url = source.get("url") or source.get("repo") or ""
            ref = source.get("ref") or pack.get("default-ref") or ""
            if url:
                return (name, _normalize_url(url), ref, url, ref)
        if isinstance(source, str) and source:
            ref = pack.get("default-ref") or ""
            return (name, _normalize_url(source), ref, source, ref)
    return (
        name,
        _BUNDLED_IDENTITY_URL,
        _BUNDLED_IDENTITY_REF,
        _BUNDLED_IDENTITY_URL,
        _BUNDLED_IDENTITY_REF,
    )


def _identity_for_default_selection(name, project_root=None):
    """Resolve the upstream identity tuple for a bundled-default pack.

    Source-of-truth precedence: prefer the wheel-bundled
    ``composer/bootstrap/packs.yaml`` (what v0.5.6 thick-wheel composer
    actually uses) over the project's potentially-stale bootstrap clone
    at ``.agent-config/repo/bootstrap/packs.yaml``. After v0.5.7 ships a
    new bundled default, an existing consumer's clone may still hold the
    old config until the next ``anywhere-agents`` bootstrap; verify
    must align with the lock the composer just wrote, not the stale
    clone, or post-fix verify will report a spurious config mismatch.

    Fall back to the project clone only when the wheel-bundled manifest
    is unavailable (pre-bootstrap, or running outside a bootstrapped
    project). Final fallback is the synthetic bundled identity.
    """
    pack = _manifest_pack_from_path(
        _bundled_packs_yaml_path(),
        name,
        strict=False,
    )
    if pack is None and project_root is not None:
        pack = _manifest_pack_from_path(
            _project_clone_packs_yaml_path(project_root),
            name,
            strict=True,
        )
    return _identity_from_manifest_pack(name, pack)


def _identity_for_user_entry(entry):
    """Return ``(name, normalized_url, ref, raw_url, raw_ref)`` for a
    user/project pack-list entry, or ``None`` if the entry has no name.
    Bundled-default names without a remote source get the synthetic
    bundled identity ``(name, "bundled:aa", "bundled")``.
    """
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not name:
        return None
    source = entry.get("source")
    if isinstance(source, dict):
        url = source.get("url") or source.get("repo") or ""
        ref = source.get("ref") or entry.get("ref") or ("main" if url else "")
    elif isinstance(source, str):
        url = source
        ref = entry.get("ref") or "main"
    else:
        if name in _DEFAULT_V2_SELECTIONS:
            return (
                name,
                _BUNDLED_IDENTITY_URL,
                _BUNDLED_IDENTITY_REF,
                _BUNDLED_IDENTITY_URL,
                _BUNDLED_IDENTITY_REF,
            )
        url = ""
        ref = entry.get("ref") or ""
    return (name, _normalize_url(url), ref, url, ref)


def _identity_for_lock_entry(name, body):
    """Build an identity tuple from a pack-lock ``packs.<name>`` body."""
    raw_url = body.get("source_url", "") or ""
    raw_ref = body.get("requested_ref", "") or ""
    if not raw_url and not raw_ref and name in _DEFAULT_V2_SELECTIONS:
        return (
            name,
            _BUNDLED_IDENTITY_URL,
            _BUNDLED_IDENTITY_REF,
            _BUNDLED_IDENTITY_URL,
            _BUNDLED_IDENTITY_REF,
        )
    return (name, _normalize_url(raw_url), raw_ref, raw_url, raw_ref)


def _read_yaml_or_none(path: Path):
    """Return ``None`` if file absent, ``{}`` if empty, dict otherwise.
    Raises :class:`_VerifyParseError` on malformed YAML or non-mapping
    top-level values.
    """
    if not path.exists():
        return None
    try:
        import yaml
    except ImportError:
        raise _VerifyParseError("PyYAML is required (install: `pip install pyyaml`)")
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        data = yaml.safe_load(text)
    except Exception as exc:
        raise _VerifyParseError(f"{path} is not valid YAML: {exc}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise _VerifyParseError(
            f"{path}: top level must be a mapping (got {type(data).__name__})"
        )
    return data


def _load_user_observations(user_config_path):
    """Return a list of identity tuples from user-level config.

    Empty list when the file is absent or has no pack list. Raises
    :class:`_VerifyParseError` on parse failure (caller maps to exit 2).
    """
    if user_config_path is None:
        return []
    data = _read_yaml_or_none(user_config_path)
    if not data:
        return []
    packs = data.get("packs")
    if packs is None:
        packs = data.get("rule_packs")
    if packs is None:
        return []
    if not isinstance(packs, list):
        raise _VerifyParseError(
            f"{user_config_path}: 'packs' must be a list"
        )
    packs, _ = _dedup_user_packs(packs)
    out = []
    for entry in packs:
        if isinstance(entry, str):
            entry = {"name": entry}
        ident = _identity_for_user_entry(entry)
        if ident is not None:
            out.append(ident)
    return out


def _load_project_observations(project_root: Path):
    """Return a list of project identity tuples after default-seeding.

    Mirrors the project half of the composer's current resolver behavior.
    User-level rows are loaded separately by ``_load_user_observations``.

    - ``DEFAULT_V2_SELECTIONS`` seed the base project view.
    - An explicit ``packs: []`` / ``rule_packs: []`` (or null) in either
      file clears prior entries, including bundled defaults.
    - Otherwise, merge tracked + local with local-overrides-tracked.

    ``AGENT_CONFIG_PACKS`` env var is excluded; it never satisfies
    "deployed" for the verify classifier.
    """
    yaml_path = project_root / "agent-config.yaml"
    local_path = project_root / "agent-config.local.yaml"

    def _signal(path):
        data = _read_yaml_or_none(path)
        if data is None:
            return None  # file absent
        key = None
        if "packs" in data:
            key = "packs"
        elif "rule_packs" in data:
            key = "rule_packs"
        if key is None:
            return None  # no signal
        raw = data[key]
        if raw is None:
            return []  # explicit opt-out
        if not isinstance(raw, list):
            raise _VerifyParseError(
                f"{path}: '{key}' must be a list"
            )
        return raw

    tracked = _signal(yaml_path)
    local = _signal(local_path)

    # Group entries by name within each file so same-name duplicates in
    # one file (e.g., two ``profile`` rows in agent-config.yaml with
    # different refs) survive into the classifier and surface as
    # ``config mismatch``. Across files, local-overrides-tracked: any
    # name present in agent-config.local.yaml replaces the tracked
    # file's entries entirely for that name.
    def _group_by_name(entries):
        grouped: dict[str, list] = {}
        for entry in entries or []:
            if isinstance(entry, str):
                entry = {"name": entry}
            if isinstance(entry, dict) and "name" in entry:
                grouped.setdefault(entry["name"], []).append(entry)
        return grouped

    merged_lists: dict[str, list] = {
        name: [{"name": name}] for name in _DEFAULT_V2_SELECTIONS
    }
    for layer in (tracked, local):
        if layer is None:
            continue
        if layer == []:
            merged_lists.clear()
            continue
        for name, rows in _group_by_name(layer).items():
            merged_lists[name] = list(rows)

    out = []
    for name in merged_lists:
        for entry in merged_lists[name]:
            # Sourceless project entries naming a bundled default (e.g.,
            # ``rule_packs: [{name: agent-style}]``) inherit the upstream
            # identity from packs.yaml so they compare equal to the lock
            # entry the composer writes. Sourceless non-default names
            # fall through to the standard helper (returns a sentinel
            # identity that will compare distinct from any remote
            # source).
            if (
                isinstance(entry, dict)
                and entry.get("name") in _DEFAULT_V2_SELECTIONS
                and entry.get("source") is None
            ):
                ident = _identity_for_default_selection(entry["name"], project_root)
            else:
                ident = _identity_for_user_entry(entry)
            if ident is not None:
                out.append(ident)
    return out


def _load_lock_observations(project_root: Path):
    """Return ``(identities, lock_health)`` from ``pack-lock.json``.

    ``lock_health`` maps name -> one of ``"ok"``, ``"schema_stale"``, or
    ``("broken", [missing_paths])``. Empty pack-lock returns
    ``([], {})``. Raises :class:`_VerifyParseError` on JSON parse
    failure (caller maps to exit 2).
    """
    lock_path = project_root / ".agent-config" / "pack-lock.json"
    if not lock_path.exists():
        return [], {}
    import json
    try:
        text = lock_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise _VerifyParseError(f"{lock_path} is malformed: {exc}")
    if not isinstance(data, dict):
        raise _VerifyParseError(f"{lock_path}: top level must be a JSON object")
    packs = data.get("packs")
    if not isinstance(packs, dict):
        return [], {}
    identities = []
    health = {}
    for name, body in packs.items():
        if not isinstance(body, dict):
            continue
        ident = _identity_for_lock_entry(name, body)
        if ident is None:
            continue
        identities.append(ident)
        # Composer-written lock entries record outputs as
        # ``body["files"][i]["output_paths"]`` (nested per file entry,
        # see scripts/packs/dispatch.py and scripts/packs/state.py).
        # Treat the absence of ``files`` (or any malformed entry) as
        # ``schema_stale`` so a pre-v0.5 lock with a different shape
        # surfaces as repairable rather than corrupt.
        files = body.get("files")
        paths: list[str] = []
        stale = False
        if isinstance(files, list) and files:
            for file_entry in files:
                if not isinstance(file_entry, dict):
                    stale = True
                    break
                fe_paths = file_entry.get("output_paths")
                if (
                    not isinstance(fe_paths, list)
                    or not fe_paths
                    or not all(isinstance(p, str) and p for p in fe_paths)
                ):
                    stale = True
                    break
                paths.extend(fe_paths)
        elif "output_paths" in body:
            # Hand-edited or pre-composer lock that uses the flat shape.
            # Accept it but require the same per-path validation.
            fe_paths = body.get("output_paths")
            if (
                not isinstance(fe_paths, list)
                or not fe_paths
                or not all(isinstance(p, str) and p for p in fe_paths)
            ):
                stale = True
            else:
                paths = list(fe_paths)
        else:
            stale = True
        if stale or not paths:
            health[name] = "schema_stale"
            continue
        missing = []
        for p in paths:
            full = project_root / p
            if not full.exists():
                missing.append(p)
        if missing:
            health[name] = ("broken", missing)
        else:
            health[name] = "ok"
    return identities, health


def _default_pack_expected_outputs(project_root: Path, name: str) -> list[str]:
    """Return output paths declared by the installed bundled manifest.

    Same source-of-truth precedence as :func:`_identity_for_default_selection`:
    prefer wheel-bundled, fall back to project clone. This keeps the
    expected-outputs view aligned with what the composer just wrote
    (post-v0.5.7 bundled-default updates write the new ``to:`` paths
    to the lock; verify must look up outputs from the same source so
    disk-presence checks line up).
    """
    pack = _manifest_pack_from_path(
        _bundled_packs_yaml_path(),
        name,
        strict=False,
    )
    if pack is None:
        pack = _manifest_pack_from_path(
            _project_clone_packs_yaml_path(project_root),
            name,
            strict=True,
        )
    if not isinstance(pack, dict):
        return []
    outputs: list[str] = []
    for section in ("passive", "active"):
        blocks = pack.get(section) or []
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            files = block.get("files") or []
            if not isinstance(files, list):
                continue
            for item in files:
                if isinstance(item, dict) and isinstance(item.get("to"), str):
                    outputs.append(item["to"])
    return outputs


def _default_pack_disk_present(project_root: Path, name: str) -> bool | None:
    """Best-effort check for a bundled default already present on disk."""
    outputs = _default_pack_expected_outputs(project_root, name)
    if not outputs:
        return None
    present = []
    for rel in outputs:
        path = project_root / rel
        if name == "agent-style" and rel == "AGENTS.md":
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                present.append(False)
            else:
                has_composer_block = bool(_AGENT_STYLE_BLOCK_RE.search(text))
                has_legacy_content = all(
                    marker in text for marker in _AGENT_STYLE_LEGACY_SIGNATURES
                )
                present.append(has_composer_block or has_legacy_content)
            continue
        present.append(path.exists())
    return all(present)


def _bundled_packs_yaml_path() -> Path | None:
    """Return the wheel-bundled ``composer/bootstrap/packs.yaml`` if present."""
    candidate = (
        Path(__file__).resolve().parent
        / "composer"
        / "bootstrap"
        / "packs.yaml"
    )
    return candidate if candidate.exists() else None


def _detect_bundled_default_drift(project_root: Path) -> set[str]:
    """Return the names of bundled-default packs whose pack-lock entry
    differs from the wheel-bundled manifest's current configuration.

    A maintainer-declared bundled-default change (ref bump or ``from:``
    flip in ``bootstrap/packs.yaml``) does not trigger a re-compose
    through the existing flow because the consumer's pack-lock still
    reports the pack as ``deployed``. ``update_policy: locked`` gates
    upstream HEAD drift on the same ref, NOT maintainer-declared bundled
    config changes; locked bundled defaults still must migrate when the
    wheel ships a new manifest. This function compares the bundled
    manifest's ``source.ref`` and ``passive[].files[].from`` set against
    the lock entry's ``requested_ref`` and ``files[].source_path`` set
    and surfaces any mismatch as drift.

    Returns the set of drifted pack names. Tolerates missing manifest
    or lock; returns ``set()`` when either is unavailable or unreadable.
    """
    bundled_yaml = _bundled_packs_yaml_path()
    if bundled_yaml is None:
        return set()
    # Gate on the project's bootstrap clone existing. Synthetic test
    # fixtures populate a project_root + lock without a bootstrap clone;
    # they should not trigger drift against the real wheel-bundled
    # manifest. Real consumer projects always have the clone (created
    # by the first ``anywhere-agents`` bootstrap), so this gate is a
    # cheap discriminator without affecting the production semantics.
    project_clone = (
        project_root / ".agent-config" / "repo" / "bootstrap" / "packs.yaml"
    )
    if not project_clone.exists():
        return set()
    try:
        data = _read_yaml_or_none(bundled_yaml) or {}
    except _VerifyParseError:
        return set()
    packs = data.get("packs") if isinstance(data, dict) else None
    if not isinstance(packs, list):
        return set()

    lock_path = project_root / ".agent-config" / "pack-lock.json"
    if not lock_path.exists():
        return set()
    import json
    try:
        lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    lock_packs = lock_data.get("packs") if isinstance(lock_data, dict) else None
    if not isinstance(lock_packs, dict):
        return set()

    drifted: set[str] = set()
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        name = pack.get("name")
        if name not in _DEFAULT_V2_SELECTIONS:
            continue
        source = pack.get("source")
        # Truly bundled-only packs (no remote source, e.g., aa-core-skills)
        # do not have a ref to drift against. Skip.
        if not isinstance(source, dict):
            continue
        bundled_ref = source.get("ref") or ""
        if not bundled_ref:
            continue
        # Collect bundled passive source paths.
        bundled_paths: set[str] = set()
        passive = pack.get("passive") or []
        if isinstance(passive, list):
            for entry in passive:
                if not isinstance(entry, dict):
                    continue
                files = entry.get("files") or []
                if not isinstance(files, list):
                    continue
                for f in files:
                    if isinstance(f, dict):
                        src = f.get("from")
                        if isinstance(src, str) and src:
                            bundled_paths.add(src)
        if not bundled_paths:
            continue
        # Compare against lock entry.
        lock_body = lock_packs.get(name)
        if not isinstance(lock_body, dict):
            continue
        lock_ref = lock_body.get("requested_ref") or ""
        lock_files = lock_body.get("files") or []
        lock_paths: set[str] = set()
        if isinstance(lock_files, list):
            for f in lock_files:
                if isinstance(f, dict):
                    sp = f.get("source_path")
                    if isinstance(sp, str) and sp:
                        lock_paths.add(sp)
        if lock_ref != bundled_ref:
            drifted.add(name)
            continue
        # Only fire on path drift when the lock actually carries
        # source_path entries. Older lock formats and minimal fixtures
        # leave source_path absent; without lock evidence we cannot
        # claim from-flip drift. Ref drift above already covers the
        # common case (any maintainer-declared bundled-default change
        # bumps the ref alongside any from-flip).
        if lock_paths and not bundled_paths.issubset(lock_paths):
            drifted.add(name)
    return drifted


def _annotate_default_rows(rows, project_root: Path):
    """Mark bundled defaults and refine no-lock states using disk content."""
    for row in rows:
        name = row.get("name")
        if name not in _DEFAULT_V2_SELECTIONS:
            continue
        row["bundled_default"] = True
        if row.get("state") != _VERIFY_STATE_DECLARED:
            continue
        disk_present = _default_pack_disk_present(project_root, name)
        if disk_present is None:
            continue
        if disk_present:
            row["state"] = _VERIFY_STATE_DEPLOYED_NOT_LOCKED
            row["note"] = (
                "bundled default content is present, but pack-lock is "
                "missing this entry; run --fix to refresh the lock"
            )
        else:
            row["state"] = _VERIFY_STATE_MISSING
            row["note"] = (
                "bundled default content is missing; run --fix to "
                "materialize it"
            )
    # Detect maintainer-declared bundled-default updates against the lock.
    # A drifted bundled default still has its content on disk, so the
    # earlier passes leave it as DEPLOYED; surface the drift here so
    # ``--fix`` invokes the composer to migrate the lock + recompose.
    drifted = _detect_bundled_default_drift(project_root)
    for row in rows:
        name = row.get("name")
        if name not in drifted:
            continue
        # Skip drift when the consumer has an explicit override for this
        # bundled default. Resolver does entry-level replace, so consumer
        # entries fully override the bundled defaults — flagging drift
        # against the bundled would migrate the user's deliberately-pinned
        # entry against their wishes (BC violation).
        if _has_explicit_default_override(project_root, row, name):
            continue
        # Override BOTH _VERIFY_STATE_DEPLOYED (lock matches old wheel)
        # AND _VERIFY_STATE_MISMATCH. The mismatch case is the post-fix
        # dirty state: wheel-bundled identity is now v0.3.5+compact, but
        # the project's bootstrap clone may still hold v0.3.2 until the
        # next bootstrap refresh. The classifier reports this as
        # mismatch; drift detection routes it through composer instead
        # so `pack verify --fix` migrates cleanly.
        if row.get("state") not in (
            _VERIFY_STATE_DEPLOYED,
            _VERIFY_STATE_MISMATCH,
        ):
            continue
        row["state"] = _VERIFY_STATE_BUNDLED_DRIFT
        row["sole"] = row.get("l") or row.get("p")
        row["note"] = (
            "bundled default config (ref or source path) has changed "
            "since the lock was written; run --fix to refresh"
        )
    return rows


def _has_explicit_default_override(
    project_root: Path,
    row: dict,
    name: str,
) -> bool:
    """Return True when consumer config has an explicit override for ``name``.

    An "explicit" override is a user-level or project-level pack entry
    that names a real source / ref / passive / active / update_policy —
    distinct from a name-only entry that just selects the bundled default.
    Used to short-circuit bundled-default drift detection: consumer-pinned
    packs must remain stable across maintainer-declared bundled bumps.
    """
    user_ident = row.get("u")
    if user_ident is not None and len(user_ident) >= 4 and user_ident[3] != _BUNDLED_IDENTITY_URL:
        return True
    for path in (
        project_root / "agent-config.yaml",
        project_root / "agent-config.local.yaml",
    ):
        try:
            data = _read_yaml_or_none(path)
        except _VerifyParseError:
            continue
        if not isinstance(data, dict):
            continue
        entries = data.get("packs")
        if entries is None:
            entries = data.get("rule_packs")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("name") != name:
                continue
            if any(
                key in entry
                for key in ("source", "ref", "passive", "active", "update_policy")
            ):
                return True
    return False


def _classify_pack_states(user, project, lock, lock_health):
    """Apply the priority-order classifier from the plan.

    Returns a list of dicts (one per pack name) sorted by name, each
    with keys: ``name``, ``state``, ``u``, ``p``, ``l``, ``sole``,
    ``note``, ``missing_paths``.
    """
    by_name: dict[str, dict[str, Any]] = {}
    intra_layer_dupes: set[str] = set()

    def _add(name: str, layer_key: str, ident: tuple) -> None:
        slot = by_name.setdefault(
            name, {"u": None, "p": None, "l": None}
        )
        existing = slot[layer_key]
        if existing is not None and (existing[1], existing[2]) != (ident[1], ident[2]):
            # Same name appears twice in one layer with distinct
            # normalized identities — treat as a config mismatch even
            # if the other layers are absent (`pack add` can append
            # rows over time, and we want the user to see the dup).
            intra_layer_dupes.add(name)
        slot[layer_key] = ident

    for ident in user:
        _add(ident[0], "u", ident)
    for ident in project:
        _add(ident[0], "p", ident)
    for ident in lock:
        _add(ident[0], "l", ident)

    rows = []
    for name in sorted(by_name.keys()):
        layers = by_name[name]
        u = layers["u"]
        p = layers["p"]
        l = layers["l"]
        norm_set = set()
        for ident in (u, p, l):
            if ident is not None:
                norm_set.add((ident[1], ident[2]))
        is_mismatch = len(norm_set) > 1 or name in intra_layer_dupes

        lh = lock_health.get(name, "ok")
        lh_kind = lh[0] if isinstance(lh, tuple) else lh
        missing_paths = lh[1] if isinstance(lh, tuple) and len(lh) > 1 else []

        if is_mismatch:
            rows.append({
                "name": name,
                "state": _VERIFY_STATE_MISMATCH,
                "u": u, "p": p, "l": l,
                "sole": None,
                "note": (
                    f"lock {lh_kind}"
                    if lh_kind in ("schema_stale", "broken") else None
                ),
                "missing_paths": missing_paths if lh_kind == "broken" else [],
            })
            continue

        U = u is not None
        P = p is not None
        L = l is not None

        if not P:
            if U:
                state = _VERIFY_STATE_USER_ONLY
                note = None
                if L and lh_kind in ("schema_stale", "broken"):
                    note = "lock has missing/legacy output paths; run --fix, then bootstrap"
                sole = u
                rows.append({
                    "name": name, "state": state,
                    "u": u, "p": p, "l": l, "sole": sole,
                    "note": note, "missing_paths": [],
                })
            else:
                state = _VERIFY_STATE_ORPHAN
                note = None
                if lh_kind in ("schema_stale", "broken"):
                    note = f"lock {lh_kind}"
                sole = l
                rows.append({
                    "name": name, "state": state,
                    "u": u, "p": p, "l": l, "sole": sole,
                    "note": note, "missing_paths": missing_paths,
                })
            continue

        if L:
            if lh_kind == "schema_stale":
                state = _VERIFY_STATE_LOCK_STALE
            elif lh_kind == "broken":
                state = _VERIFY_STATE_BROKEN
            else:
                state = _VERIFY_STATE_DEPLOYED
        else:
            state = _VERIFY_STATE_DECLARED

        rows.append({
            "name": name, "state": state,
            "u": u, "p": p, "l": l, "sole": p,
            "note": None,
            "missing_paths": missing_paths if state == _VERIFY_STATE_BROKEN else [],
        })

    return rows


def _format_source(ident):
    """Format an identity for display. Redacts URL userinfo so a
    legacy hand-edited config containing ``https://TOKEN@host/repo``
    never leaks the token to stdout.
    """
    if ident is None:
        return ""
    _, _norm_url, ref, raw_url, raw_ref = ident
    if raw_url == _BUNDLED_IDENTITY_URL:
        return "bundled"
    if raw_url:
        try:
            from anywhere_agents.packs import auth as _pack_auth
            src = _pack_auth.redact_url_userinfo(raw_url)
        except Exception:
            src = raw_url
    else:
        src = ""
    out_ref = raw_ref or ref or ""
    if src and out_ref:
        return f"{src} @ {out_ref}"
    return src or out_ref


def _print_verify_table(rows, env_var_value, file=None):
    """Print the verify output table to stdout."""
    if file is None:
        file = sys.stdout
    if env_var_value:
        print(
            f"note: AGENT_CONFIG_PACKS={env_var_value} "
            "(transient project selection, not durable)",
            file=file,
        )
    if not rows:
        print(
            "No packs declared in user-level, project-level, or pack-lock.",
            file=file,
        )
        return

    name_w = max(4, max(len(r["name"]) for r in rows))
    def _status(row):
        state = row["state"]
        glyph = _STATE_GLYPHS.get(state, "[?]")
        status = f"{glyph} {state}"
        if row.get("bundled_default") and state == _VERIFY_STATE_DEPLOYED:
            status += " (bundled default)"
        return status

    status_w = max(27, max(len(_status(r)) for r in rows))
    print(
        f"{'PACK':<{name_w}}  {'STATUS':<{status_w}}  SOURCE",
        file=file,
    )
    for r in rows:
        state = r["state"]
        if state == _VERIFY_STATE_MISMATCH:
            parts = []
            for layer_name, key in (("user", "u"), ("project", "p"), ("lock", "l")):
                ident = r[key]
                if ident is not None:
                    parts.append(f"{layer_name}: {_format_source(ident)}")
            source = "; ".join(parts)
        else:
            source = _format_source(r.get("sole"))
        status = _status(r)
        print(f"{r['name']:<{name_w}}  {status:<{status_w}}  {source}", file=file)
        if r.get("missing_paths"):
            for path in r["missing_paths"][:3]:
                print(f"{'':<{name_w}}    missing: {path}", file=file)
            if len(r["missing_paths"]) > 3:
                print(
                    f"{'':<{name_w}}    ... and {len(r['missing_paths']) - 3} more",
                    file=file,
                )
        if r.get("note"):
            print(f"{'':<{name_w}}    note: {r['note']}", file=file)
        if state == _VERIFY_STATE_MISMATCH:
            print(
                f"{'':<{name_w}}    hint: edit agent-config.yaml, then rerun bootstrap",
                file=file,
            )
        elif state == _VERIFY_STATE_ORPHAN:
            print(
                f"{'':<{name_w}}    hint: restore a rule_packs: entry, OR run",
                file=file,
            )
            print(
                f"{'':<{name_w}}          `anywhere-agents uninstall --all` to remove",
                file=file,
            )
            print(
                f"{'':<{name_w}}          all aa-managed outputs. Do not use `pack remove`",
                file=file,
            )
            print(
                f"{'':<{name_w}}          (it edits user-level config only).",
                file=file,
            )

    not_deployed = sum(
        1 for r in rows if r["state"] != _VERIFY_STATE_DEPLOYED
    )
    if not_deployed > 0:
        print("", file=file)
        print(
            f"{not_deployed} of {len(rows)} pack(s) not deployed in this project.",
            file=file,
        )


def _verify_gather(user_config_path, project_root):
    """Collect (rows, env_var_value); raises :class:`_VerifyParseError` on parse error."""
    user = _load_user_observations(user_config_path)
    project = _load_project_observations(project_root)
    lock_idents, lock_health = _load_lock_observations(project_root)
    rows = _classify_pack_states(user, project, lock_idents, lock_health)
    rows = _annotate_default_rows(rows, project_root)
    env_var_value = os.environ.get("AGENT_CONFIG_PACKS", "")
    return rows, env_var_value


def _looks_like_sha(ref: str) -> bool:
    """Return True if ``ref`` is a 40-char hex SHA (immutable).

    v0.5.2 banner item 7 + ``pack verify`` only run ``git ls-remote``
    against mutable refs (branches, tags). A pinned 40-char SHA is
    immutable so the network call is wasted.
    """
    if not isinstance(ref, str) or len(ref) != 40:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in ref)


def _ls_remote_head(url: str, ref: str, *, timeout: float = 5.0) -> str | None:
    """Run ``git ls-remote --exit-code <url> <ref>`` with a hard timeout.

    Returns the resolved 40-char SHA on success, or ``None`` on any
    failure (network error, non-existent ref, timeout). The caller
    silently skips packs that return ``None`` so an offline ``pack
    verify`` still classifies the local state.
    """
    if _looks_like_sha(ref):
        return None
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "ls-remote", "--exit-code", url, ref],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    # Output: "<sha>\t<refname>\n..." (possibly multiple matches; the
    # first qualifying line wins).
    for line in result.stdout.splitlines():
        sha = line.split("\t", 1)[0].strip()
        if _looks_like_sha(sha):
            return sha
    return None


def _merge_latest_known_heads(
    lock_path: Path,
    snapshot_packs: dict[str, dict],
    head_results: dict[str, str],
) -> None:
    """Lock-bracketed merge of ``latest_known_head`` into ``pack-lock.json``.

    Matches each result by ``(source_url, requested_ref, resolved_commit)``
    against the locked re-read of the lock so a concurrent composer
    write between the snapshot read and the locked merge does not
    overwrite the new ``resolved_commit``. Skipped (per-entry) when the
    re-read shows the entry was modified or removed.
    """
    if not head_results:
        return
    try:
        from anywhere_agents.packs import locks as locks_mod
        from anywhere_agents.packs import state as state_mod
    except ImportError:
        return
    repo_lock = locks_mod.repo_lock_path(lock_path.parent.parent)
    try:
        with locks_mod.acquire(repo_lock, timeout=5.0):
            if not lock_path.exists():
                return
            try:
                current = state_mod.load_pack_lock(lock_path)
            except state_mod.StateError:
                return
            current_packs = current.get("packs", {}) or {}
            mutated = False
            now_iso = datetime.now(timezone.utc).isoformat()
            for name, head in head_results.items():
                snap = snapshot_packs.get(name)
                cur = current_packs.get(name)
                if snap is None or cur is None:
                    continue
                # Identity tuple from snapshot must still match the
                # locked re-read; if any field changed the entry was
                # rewritten by a concurrent composer and we skip.
                if (
                    snap.get("source_url") != cur.get("source_url")
                    or snap.get("requested_ref") != cur.get("requested_ref")
                    or snap.get("resolved_commit") != cur.get("resolved_commit")
                ):
                    continue
                if cur.get("latest_known_head") == head:
                    continue
                cur["latest_known_head"] = head
                cur["fetched_at"] = now_iso
                mutated = True
            if mutated:
                try:
                    state_mod.save_pack_lock(lock_path, current)
                except state_mod.StateError:
                    return
    except locks_mod.LockTimeout:
        # Another composer / verify is running; skip the merge so we
        # don't block the audit. Next ``pack verify`` will retry.
        return


def _pack_verify(user_config_path, project_root, args):
    """Read-only audit. Exit 0 when every identity is deployed (or
    nothing to check), 1 when any identity is in a non-deployed state,
    2 when a config or lock file is unparseable.

    v0.5.2 also performs an opportunistic ``git ls-remote`` check
    against every pack-lock entry whose ``requested_ref`` is mutable
    (not a 40-char SHA), then lock-bracket-merges the resolved
    ``latest_known_head`` into the lock so the session-start banner
    can surface available updates.
    """
    try:
        rows, env_var_value = _verify_gather(user_config_path, project_root)
    except _VerifyParseError as exc:
        log(f"error: {exc}")
        return 2

    # v0.5.2 step 3: per-lock-entry ls-remote with 5s timeout. Snapshot
    # the lock contents BEFORE issuing the network calls so the
    # locked-merge step can match by identity tuple if a concurrent
    # composer rewrites between snapshot and merge.
    lock_path = project_root / ".agent-config" / "pack-lock.json"
    snapshot_packs: dict[str, dict] = {}
    head_results: dict[str, str] = {}
    update_count = 0
    if lock_path.exists():
        try:
            import json
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            snapshot_packs = data.get("packs") or {}
        except (OSError, json.JSONDecodeError):
            snapshot_packs = {}
        for name, body in snapshot_packs.items():
            if not isinstance(body, dict):
                continue
            url = body.get("source_url", "")
            ref = body.get("requested_ref", "")
            resolved = body.get("resolved_commit", "")
            if not url or not ref or url == "bundled:aa":
                continue
            if _looks_like_sha(ref):
                continue
            head = _ls_remote_head(url, ref)
            if head is None:
                continue
            head_results[name] = head
            if resolved and head != resolved:
                update_count += 1

    _merge_latest_known_heads(lock_path, snapshot_packs, head_results)
    _print_verify_table(rows, env_var_value)

    bad = [r for r in rows if r["state"] != _VERIFY_STATE_DEPLOYED]
    if update_count > 0:
        print("", file=sys.stdout)
        print(
            f"ℹ {update_count} pack(s) have updates available "
            "(run `pack verify --fix` to apply).",
            file=sys.stdout,
        )
    if not bad:
        return 0
    if any(r["state"] == _VERIFY_STATE_USER_ONLY for r in bad):
        print("", file=sys.stdout)
        print(
            "To deploy: run `anywhere-agents pack verify --fix` "
            "(writes rule_packs: entries to agent-config.yaml and "
            "invokes the composer).",
            file=sys.stdout,
        )
    return 1


def _user_only_rule_pack_entry(row):
    """Return the ``rule_packs:`` entry to write for a user-level-only row."""
    u = row.get("u")
    if u is None:
        return None
    name, _norm_url, _ref, raw_url, raw_ref = u
    entry: dict[str, Any] = {"name": name}
    if raw_url:
        source = {"url": raw_url}
        if raw_ref:
            source["ref"] = raw_ref
        entry["source"] = source
    return entry


def _project_only_user_pack_entry(row):
    """Return the user-level config entry to write for a project-only row.

    Mirrors :func:`_user_only_rule_pack_entry` but emits the user-config
    shape (uses ``source: {url, ref}`` for inline-source packs; bundled
    packs get ``{name}`` only).
    """
    p = row.get("p")
    if p is None:
        return None
    name, _norm_url, _ref, raw_url, raw_ref = p
    entry: dict[str, Any] = {"name": name}
    if raw_url and raw_url != _BUNDLED_IDENTITY_URL:
        source = {"url": raw_url}
        if raw_ref:
            source["ref"] = raw_ref
        entry["source"] = source
    return entry


def _pack_verify_fix(user_config_path, project_root, args):
    """v0.5.2 ``pack verify --fix`` — bidirectional reconcile + materialize.

    1. Read configs + lock (snapshot).
    2. Plan reconcile:
       - User has X but project doesn't → plan a project ``rule_packs`` append.
       - Project has X but user doesn't → plan a user-level append.
       - Same name with different identity (``MISMATCH``) → rc=1, no writes.
    3. Take user-level lock; atomic-write user config if planned changes;
       release.
    4. Atomic-write project ``agent-config.yaml`` if planned changes.
    5. Invoke ``compose_packs.py`` as a subprocess. Composer self-locks
       and runs its own drift gate; on rc=1 the CLI surfaces rc=1 too.
    6. Composer's lock write populates ``latest_known_head =
       resolved_commit`` for fetched entries — no extra step here.

    Recovery for composer drift-abort: same as ``pack add``. Config
    rows are persistent; the user backs up local edits and reruns ``--fix``.
    """
    project_yaml = project_root / "agent-config.yaml"
    if project_yaml.exists():
        try:
            _read_yaml_or_none(project_yaml)
        except _VerifyParseError as exc:
            log(
                f"error: {exc} -- refusing to overwrite. "
                "Fix the YAML manually first."
            )
            return 2

    # v0.5.4 self-heal: rewrite the user-config file if it has duplicate
    # ``packs`` entries (earlier ``pack add`` versions and concurrent
    # invocations could leave duplicates that shadow dedup checks).
    # Independent of reconcile; runs even when there are no project-only
    # rows. Read + dedup + write must hold the user-config lock so a
    # concurrent ``pack add`` cannot land between the read at the top of
    # this block and the write at the bottom (Codex Round 1 Medium).
    # Errors are non-fatal — verify continues with the in-memory deduped
    # view from ``_load_user_observations``.
    if user_config_path is not None and user_config_path.exists():
        try:
            from anywhere_agents.packs import locks as _locks_mod  # type: ignore[import-not-found]
        except ImportError:
            _locks_mod = None  # type: ignore[assignment]
        if _locks_mod is not None:
            user_lock = _user_config_lock_path(user_config_path)
            try:
                with _locks_mod.acquire(user_lock, timeout=30):
                    try:
                        raw_user = _read_yaml_or_none(user_config_path)
                    except _VerifyParseError:
                        raw_user = None
                    if isinstance(raw_user, dict):
                        raw_packs = raw_user.get("packs")
                        if isinstance(raw_packs, list):
                            deduped, n_dropped = _dedup_user_packs(raw_packs)
                            if n_dropped > 0:
                                raw_user["packs"] = deduped
                                try:
                                    _write_user_config(user_config_path, raw_user)
                                    log(
                                        f"--fix: dropped {n_dropped} duplicate "
                                        f"pack entry/entries from "
                                        f"{user_config_path}"
                                    )
                                except OSError as exc:
                                    log(
                                        f"warning: dedup self-heal of "
                                        f"{user_config_path} failed: {exc}"
                                    )
            except _locks_mod.LockTimeout:
                # Another writer holds the lock; skip the durable rewrite
                # this round. ``_load_user_observations`` still returns the
                # deduped view in memory for the rest of --fix.
                log(
                    f"warning: could not acquire user-config lock for dedup "
                    f"self-heal of {user_config_path}; skipping rewrite"
                )

    try:
        rows, env_var_value = _verify_gather(user_config_path, project_root)
    except _VerifyParseError as exc:
        log(f"error: {exc}")
        return 2

    _print_verify_table(rows, env_var_value)

    # Mismatch → rc=1 with no writes (per plan § 2 step 2 last bullet).
    mismatch_rows = [r for r in rows if r["state"] == _VERIFY_STATE_MISMATCH]
    if mismatch_rows:
        print("", file=sys.stdout)
        names = ", ".join(sorted({r["name"] for r in mismatch_rows}))
        print(
            f"--fix: {len(mismatch_rows)} pack(s) have identity mismatch "
            f"({names}); refusing to auto-resolve. Edit "
            f"{project_yaml} and {user_config_path} to align identities.",
            file=sys.stdout,
        )
        return 1

    def _is_bundled_default_row(row: dict) -> bool:
        """Bundled-default packs (aa-core-skills, agent-style) are
        composer-seeded via ``DEFAULT_V2_SELECTIONS`` — the composer
        always materializes them from ``bootstrap/packs.yaml`` regardless
        of any user/project layer entry. v0.5.4: never reconcile these
        names across layers, regardless of URL form. Reconciling them
        creates churn: the lock-side URL form (which the lock writer
        emits because the bundled pack-def has an upstream source for
        passive content) propagates into user-level config, then back
        into project YAML, ad infinitum. To override a bundled-default
        with a different upstream version, edit ``agent-config.yaml``
        directly and the override is consumed at the project layer; the
        upstream must declare a v0.4.0+ ``pack.yaml`` (or be one of the
        DEFAULT_V2_SELECTIONS names; v0.5.4 falls back to the bundled
        pack-def in the no-manifest case).
        """
        return row.get("name") in _DEFAULT_V2_SELECTIONS

    user_only_rows = [
        r for r in rows
        if r["state"] == _VERIFY_STATE_USER_ONLY
        and not _is_bundled_default_row(r)
    ]
    # ``DECLARED`` = project has it, no lock entry, may or may not have
    # user-level. Filter to project-only-no-user via the layers.
    project_only_rows = [
        r for r in rows
        if r.get("p") is not None and r.get("u") is None
        and r["state"] != _VERIFY_STATE_MISMATCH
        and not _is_bundled_default_row(r)
    ]

    # Reject credential-bearing URLs before printing or writing.
    from anywhere_agents.packs import auth as _pack_auth_check
    for r in user_only_rows + project_only_rows:
        for layer_key in ("u", "p"):
            ident = r.get(layer_key)
            raw_url = (
                ident[3] if (ident is not None and len(ident) >= 4) else ""
            )
            if not raw_url:
                continue
            try:
                _pack_auth_check.reject_credential_url(
                    raw_url, source_layer="config layer"
                )
            except _pack_auth_check.CredentialURLError as exc:
                log(f"error: {exc}")
                return 2

    if not user_only_rows and not project_only_rows:
        bad = [r for r in rows if r["state"] != _VERIFY_STATE_DEPLOYED]
        # No reconcile needed; if any row can be repaired by a plain
        # composer run, invoke it. This includes bundled defaults whose
        # files are already present but whose lock row was dropped by an
        # older composer.
        deployable = [
            r for r in rows
            if r["state"] in (
                _VERIFY_STATE_DECLARED,
                _VERIFY_STATE_DEPLOYED_NOT_LOCKED,
                _VERIFY_STATE_MISSING,
                _VERIFY_STATE_BUNDLED_DRIFT,
            )
        ]
        if not bad:
            print("", file=sys.stdout)
            print("--fix: nothing to repair.", file=sys.stdout)
            return 0
        if deployable:
            print("", file=sys.stdout)
            print(
                "--fix: invoking composer to deploy or refresh "
                "pack-lock entries...",
                file=sys.stdout,
            )
            rc = _invoke_composer(project_root)
            if rc != 0:
                log(
                    f"error: composer failed (rc={rc}). Recovery: back up "
                    "local edits to managed files and rerun "
                    "`pack verify --fix`."
                )
                return rc
            print("✓ Deployed.", file=sys.stdout)
            return 0
        unresolved = ", ".join(
            f"{r['name']} ({r['state']})" for r in sorted(
                bad, key=lambda row: row["name"]
            )
        )
        print("", file=sys.stdout)
        print(
            f"--fix: no automatic repair for {len(bad)} pack(s): "
            f"{unresolved}. Resolve these rows manually, then rerun "
            "`pack verify --fix`.",
            file=sys.stdout,
        )
        return 1

    # Confirmation prompt (shared with v0.5.1 UX).
    print("", file=sys.stdout)
    print("--fix planned changes:", file=sys.stdout)
    for r in user_only_rows:
        entry = _user_only_rule_pack_entry(r)
        if entry:
            display_src = _format_source(r.get("u"))
            print(
                f"  + add to {project_yaml}: name={entry['name']}, source={display_src}",
                file=sys.stdout,
            )
    for r in project_only_rows:
        entry = _project_only_user_pack_entry(r)
        if entry:
            display_src = _format_source(r.get("p"))
            print(
                f"  + add to {user_config_path}: name={entry['name']}, source={display_src}",
                file=sys.stdout,
            )
    if not args.yes:
        if sys.stdin.isatty():
            print("", file=sys.stdout)
            try:
                resp = input("Apply these changes? [y/N]: ").strip().lower()
            except EOFError:
                resp = ""
            if resp != "y":
                print(
                    "--fix: aborted by user; nothing written.",
                    file=sys.stdout,
                )
                return 1
        else:
            print("", file=sys.stdout)
            print(
                "--fix: --yes required in non-interactive mode; nothing written.",
                file=sys.stdout,
            )
            return 0

    # Step 3: take user-level config lock; write user-config additions.
    try:
        from anywhere_agents.packs import locks as locks_mod  # type: ignore[import-not-found]
    except ImportError:
        log(
            "error: cannot import locks module; refusing to write."
        )
        return 2

    if project_only_rows:
        user_lock = _user_config_lock_path(user_config_path)
        try:
            with locks_mod.acquire(user_lock, timeout=30):
                user_data = _load_or_create_user_config(user_config_path)
                packs_list = user_data.setdefault("packs", [])
                if not isinstance(packs_list, list):
                    log(
                        f"error: {user_config_path}: 'packs' must be a "
                        "list; refusing to overwrite."
                    )
                    return 2
                existing_names = {
                    e.get("name") for e in packs_list
                    if isinstance(e, dict)
                }
                added = 0
                for r in project_only_rows:
                    entry = _project_only_user_pack_entry(r)
                    if entry and entry["name"] not in existing_names:
                        packs_list.append(entry)
                        existing_names.add(entry["name"])
                        added += 1
                if added:
                    _write_user_config(user_config_path, user_data)
                    print(
                        f"--fix: wrote {added} pack(s) to {user_config_path}",
                        file=sys.stdout,
                    )
        except locks_mod.LockTimeout as exc:
            log(f"error: could not acquire user-config lock: {exc}")
            return 10

    # Step 4: write project agent-config.yaml additions under repo lock.
    repo_lock = locks_mod.repo_lock_path(project_root)
    if user_only_rows:
        try:
            with locks_mod.acquire(repo_lock, timeout=30):
                try:
                    import yaml
                except ImportError:
                    log("error: PyYAML is required (install: `pip install pyyaml`)")
                    return 2
                if project_yaml.exists():
                    text = project_yaml.read_text(encoding="utf-8")
                    data = yaml.safe_load(text) if text.strip() else {}
                    if data is None:
                        data = {}
                    if not isinstance(data, dict):
                        log(
                            f"error: {project_yaml}: top level must be a "
                            "mapping; refusing to overwrite."
                        )
                        return 2
                else:
                    data = {}
                existing = data.get("rule_packs") or []
                if not isinstance(existing, list):
                    log(
                        f"error: {project_yaml}: 'rule_packs' must be a "
                        "list; refusing to overwrite."
                    )
                    return 2
                existing_names = {
                    e.get("name") for e in existing if isinstance(e, dict)
                }
                added = 0
                for r in user_only_rows:
                    entry = _user_only_rule_pack_entry(r)
                    if entry and entry["name"] not in existing_names:
                        existing.append(entry)
                        existing_names.add(entry["name"])
                        added += 1
                if added:
                    data["rule_packs"] = existing
                    out_text = yaml.safe_dump(
                        data, sort_keys=False, default_flow_style=False
                    )
                    project_yaml.parent.mkdir(parents=True, exist_ok=True)
                    tmp = project_yaml.with_name(project_yaml.name + ".tmp")
                    tmp.write_text(out_text, encoding="utf-8")
                    os.replace(str(tmp), str(project_yaml))
                    print(
                        f"--fix: wrote {added} rule_packs entry/entries to "
                        f"{project_yaml}",
                        file=sys.stdout,
                    )
        except locks_mod.LockTimeout as exc:
            log(f"error: could not acquire repo lock: {exc}")
            return 10

    # Step 5: invoke composer.
    print("", file=sys.stdout)
    print(
        "--fix: invoking composer to deploy...",
        file=sys.stdout,
    )
    rc = _invoke_composer(project_root)
    if rc != 0:
        log(
            f"error: composer failed (rc={rc}). Configs are persistent; "
            "back up local edits to managed files and rerun "
            "`pack verify --fix`."
        )
        return rc
    repaired = len(user_only_rows) + len(project_only_rows)
    print(
        f"✓ Repaired {repaired} mismatches; deployed.",
        file=sys.stdout,
    )
    return 0


def _pack_remove(path: Path, name: str) -> int:
    """``pack remove <name>`` — cascade delete (v0.5.2).

    1. Locate ``name`` in user config, project ``rule_packs:``, and
       prior pack-lock. Not found in any → rc=1.
    2. Take user-level config lock; remove user-level entry; release.
    3. Atomic-write project ``agent-config.yaml`` removing from
       ``rule_packs:``.
    4. Invoke ``compose_packs.py uninstall <name>`` so the new single-
       pack uninstall path runs (deletes physical outputs, prunes
       state, decrements user-level owners with composite-key filter).
    5. Print summary.
    """
    project_root = Path.cwd().resolve()
    project_yaml = project_root / "agent-config.yaml"
    lock_path = project_root / ".agent-config" / "pack-lock.json"

    found_in_user = False
    found_in_project = False
    found_in_lock = False

    # Probe user config (do not mutate yet).
    user_data = _load_user_config(path) if path.exists() else {}
    user_packs = user_data.get("packs") or user_data.get("rule_packs") or []
    if isinstance(user_packs, list):
        for entry in user_packs:
            if isinstance(entry, dict) and entry.get("name") == name:
                found_in_user = True
                break
            if isinstance(entry, str) and entry == name:
                found_in_user = True
                break

    # Probe project YAML.
    if project_yaml.exists():
        try:
            project_data = _read_yaml_or_none(project_yaml) or {}
        except _VerifyParseError:
            project_data = {}
        for entry in project_data.get("rule_packs") or []:
            if isinstance(entry, dict) and entry.get("name") == name:
                found_in_project = True
                break
            if isinstance(entry, str) and entry == name:
                found_in_project = True
                break

    # Probe pack-lock.
    if lock_path.exists():
        try:
            import json
            lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
            if (
                isinstance(lock_data, dict)
                and isinstance(lock_data.get("packs"), dict)
                and name in lock_data["packs"]
            ):
                found_in_lock = True
        except (OSError, json.JSONDecodeError):
            pass

    if not (found_in_user or found_in_project or found_in_lock):
        if name in _DEFAULT_V2_SELECTIONS:
            log(
                f"notice: {name!r} is a bundled default. To stop bundled "
                "deployment entirely, add `packs: []` to agent-config.yaml "
                "and rerun `anywhere-agents pack verify --fix`."
            )
        log(f"error: pack {name!r} not in user config, project rule_packs, or pack-lock")
        return 1

    if name in _DEFAULT_V2_SELECTIONS:
        log(
            f"notice: {name!r} is a bundled default. Removing it only drops "
            "current lock/config ownership; the next normal compose will "
            "restore it unless project config explicitly clears defaults "
            "with `packs: []`."
        )

    # Step 2: lock user-level config and remove the entry.
    if found_in_user:
        try:
            from anywhere_agents.packs import locks as locks_mod  # type: ignore[import-not-found]
        except ImportError:
            locks_mod = None
        user_lock = _user_config_lock_path(path)
        try:
            if locks_mod is not None:
                with locks_mod.acquire(user_lock, timeout=30):
                    _remove_from_user_config(path, name)
            else:
                _remove_from_user_config(path, name)
        except Exception as exc:
            log(f"error: could not acquire user-config lock: {exc}")
            return 10

    # Step 3: rewrite project YAML.
    if found_in_project:
        try:
            import yaml
        except ImportError:
            log("error: PyYAML is required (install: `pip install pyyaml`)")
            return 2
        text = project_yaml.read_text(encoding="utf-8") if project_yaml.exists() else ""
        data = yaml.safe_load(text) if text.strip() else {}
        if not isinstance(data, dict):
            data = {}
        rule_packs = data.get("rule_packs") or []
        if isinstance(rule_packs, list):
            data["rule_packs"] = [
                e for e in rule_packs
                if not (isinstance(e, dict) and e.get("name") == name)
                and not (isinstance(e, str) and e == name)
            ]
            out_text = yaml.safe_dump(
                data, sort_keys=False, default_flow_style=False
            )
            tmp = project_yaml.with_name(project_yaml.name + ".tmp")
            tmp.write_text(out_text, encoding="utf-8")
            os.replace(str(tmp), str(project_yaml))

    # Step 4: invoke composer's uninstall mode for the single pack.
    if found_in_lock:
        rc = _invoke_composer(project_root, "uninstall", name)
        if rc not in (0,):
            log(
                f"error: composer uninstall failed (rc={rc}). Configs "
                "have been removed; on-disk artifacts may remain. Resolve "
                "any drift and rerun `pack remove`."
            )
            return rc

    log(f"✓ Removed {name!r}")
    return 0


def _remove_from_user_config(path: Path, name: str) -> None:
    """Atomic in-place rewrite of the user-config to drop ``name``.

    Caller holds the user-config lock. No-op when the file is missing
    or the entry is absent (idempotent).
    """
    if not path.exists():
        return
    data = _load_user_config(path)
    if "packs" not in data and "rule_packs" in data:
        legacy = data.pop("rule_packs")
        if isinstance(legacy, list):
            data["packs"] = list(legacy)
    elif "packs" in data and "rule_packs" in data:
        data.pop("rule_packs", None)
    packs = data.get("packs", [])
    if not isinstance(packs, list):
        return
    data["packs"] = [
        p for p in packs
        if not (isinstance(p, dict) and p.get("name") == name)
        and not (isinstance(p, str) and p == name)
    ]
    _save_user_config(path, data)


def _pack_list(path: Path) -> int:
    print(f"User-level config: {path}")
    if not path.exists():
        print("  (not created yet)")
    else:
        data = _load_user_config(path)
        packs = data.get("packs", [])
        if not packs:
            print("  (empty)")
        else:
            for p in packs:
                if isinstance(p, str):
                    print(f"  - {p}")
                elif isinstance(p, dict):
                    name = p.get("name", "<no name>")
                    ref = p.get("ref")
                    source = p.get("source")
                    line = f"  - {name}"
                    if ref:
                        line += f" (ref: {ref})"
                    if source:
                        line += f" <- {source}"
                    print(line)

    cwd_tracked = Path.cwd() / "agent-config.yaml"
    cwd_local = Path.cwd() / "agent-config.local.yaml"
    for label, p in [("Project-tracked", cwd_tracked), ("Project-local", cwd_local)]:
        if p.exists():
            print(f"\n{label}: {p}")
            data = _load_user_config(p)
            packs = data.get("packs") or data.get("rule_packs") or []
            if not packs:
                print("  (empty)")
            else:
                for entry in packs:
                    if isinstance(entry, str):
                        print(f"  - {entry}")
                    elif isinstance(entry, dict):
                        print(f"  - {entry.get('name', '<no name>')}")
    return 0


# ======================================================================
# uninstall subcommand: full project uninstall via composer engine
# ======================================================================


# Map uninstall engine outcomes to CLI exit codes per pack-architecture.md
# § "CLI contract for ``uninstall --all``".
_UNINSTALL_EXIT_CODES = {
    "clean": 0,
    "no-op": 0,
    "lock-timeout": 10,
    "drift": 20,
    "malformed-state": 30,
    "partial-cleanup": 40,
}


def _uninstall_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="anywhere-agents uninstall")
    parser.add_argument(
        "--all",
        action="store_true",
        required=True,
        help="Uninstall every aa-pack-owned output from the current project",
    )
    parser.parse_args(argv)

    project_root = Path.cwd().resolve()
    # The uninstall engine lives in the bootstrap-clone at
    # .agent-config/repo/scripts/packs/. Add its parent to sys.path so
    # `from packs import uninstall` resolves.
    packs_parent = project_root / ".agent-config" / "repo" / "scripts"
    if not packs_parent.exists():
        log(
            f"error: {packs_parent} not found; "
            "uninstall --all requires a project bootstrapped with aa v0.4.0+"
        )
        return 2

    sys.path.insert(0, str(packs_parent))
    try:
        from packs import uninstall as uninstall_mod  # type: ignore[import-not-found]
    except ImportError as exc:
        log(f"error: could not import uninstall engine: {exc}")
        return 2

    outcome = uninstall_mod.run_uninstall_all(project_root)

    # Report summary.
    log(f"status: {outcome.status}")
    if outcome.packs_removed:
        log(f"packs removed: {', '.join(outcome.packs_removed)}")
    if outcome.files_deleted:
        log(f"files deleted: {len(outcome.files_deleted)}")
    if outcome.owners_decremented:
        log(f"owners decremented: {len(outcome.owners_decremented)}")
    if outcome.drift_paths:
        log(f"drift: {len(outcome.drift_paths)} path(s) left in place")
        for p in outcome.drift_paths:
            log(f"  - {p}")
    if outcome.lock_holder_pid is not None:
        log(f"lock holder PID: {outcome.lock_holder_pid}")
    for detail in outcome.details:
        log(detail)

    return _UNINSTALL_EXIT_CODES.get(outcome.status, 40)


if __name__ == "__main__":
    sys.exit(main())
