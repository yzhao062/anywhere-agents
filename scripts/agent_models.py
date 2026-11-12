#!/usr/bin/env python3
"""Resolve, sync, and check the agent models named in user/agent-models.json.

The registry is the single source for the model and effort of every agent
role: the interactive session, a /vet reviewer, a review panel, an audit, a
Claude subagent or Workflow worker, a prun worker, and the optional premium
row for a task the user chooses to run on a scarcer model. A role inherits its
family's row unless the registry lists an override for it. Nothing else in the
repository names a concrete model; a dispatcher asks this script at run time,
and bootstrap writes the interactive values into each tool's own config.

Commands:
    resolve --family F [--role R] [--field NAME] [--json]
        Print "<model> <effort>" (or one field, or JSON). For agy the model is
        a family stem such as gemini-4-argon; --field slug gives the Agy slug
        (stem plus effort tier) that dispatchers float to the newest version.
    show [--json]
        Every family and role with its resolved model and effort.
    render [--check]
        Rewrite the keys of user/settings.json that mirror the registry
        (model, effortLevel, and two env entries). --check exits 1 when stale.
    set --family F [--role R] [--model M] [--effort E]
        Change the registry in a source repository, then render.
    sync [--dry-run] [--quiet]
        Write the interactive Codex and Agy values into ~/.codex/config.toml
        and the Antigravity settings file. Claude's values reach
        ~/.claude/settings.json through bootstrap's settings merge.
    doctor [--scan DIR ...]
        Compare each tool's live config with the registry, and with --scan
        list model literals in the given trees. Exit 1 on a mismatch.
    scan PATH ...
        List model and effort literals outside the registry. Exit 1 on a hit.
    actual --family F (--session-id ID | --stderr-file F | --log-file F | --json-file F)
           [--expect-model M --expect-effort E]
        Read what a finished run actually used from the CLI's own report and
        print `actual=<model> <effort> status=ok|mismatch|unknown`. Exit 0.
    verify [--family F]
        Launch each CLI with no model flags (one short request each) and
        compare what it ran on with the registry. Exit 1 on a mismatch.

Python 3.9 compatible; standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Iterator

REQUIRED_FAMILIES = ("claude", "codex", "agy")
OPTIONAL_FAMILIES = ("grok",)
FAMILIES = REQUIRED_FAMILIES + OPTIONAL_FAMILIES
ROLES = ("interactive", "reviewer", "premium", "worker", "prun-worker")
EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
    "agy": ("low", "medium", "high"),
    "grok": ("low", "medium", "high", "xhigh"),
}
AGY_TIERS = ("low", "medium", "high", "xhigh", "max", "thinking")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY = SCRIPT_DIR.parent / "user" / "agent-models.json"
DEFAULT_SETTINGS = SCRIPT_DIR.parent / "user" / "settings.json"
ALLOW_MARKER = "agent-models: allow"


class RegistryError(Exception):
    pass


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def registry_path() -> Path:
    override = os.environ.get("AGENT_MODELS_FILE", "").strip()
    return Path(override) if override else DEFAULT_REGISTRY


def load(path: Path | None = None) -> dict:
    path = path or registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise RegistryError(f"registry not found: {path}")
    except (OSError, ValueError) as exc:
        raise RegistryError(f"cannot read registry {path}: {exc}")
    validate(data)
    return data


def validate(data: dict) -> None:
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise RegistryError("registry schema must be 1")
    families = data.get("families")
    roles = data.get("roles", {})
    if (
        not isinstance(families, dict)
        or not set(REQUIRED_FAMILIES) <= set(families)
        or not set(families) <= set(FAMILIES)
    ):
        raise RegistryError(
            f"registry families must be exactly {', '.join(REQUIRED_FAMILIES)} "
            f"(optional: {', '.join(OPTIONAL_FAMILIES)})"
        )
    if not isinstance(roles, dict) or not set(roles) <= set(ROLES):
        unknown = sorted(set(roles) - set(ROLES)) if isinstance(roles, dict) else roles
        raise RegistryError(f"unknown registry roles: {unknown}")
    for family, row in families.items():
        _check_row(family, row, family_row=True)
    present_families = tuple(f for f in FAMILIES if f in families)
    for role, rows in roles.items():
        if not isinstance(rows, dict) or not set(rows) <= set(families):
            raise RegistryError(f"role {role} may only list {', '.join(present_families)}")
        for family, row in rows.items():
            _check_row(family, row, family_row=False, where=f"role {role}")


def _check_row(family: str, row: dict, family_row: bool, where: str = "family") -> None:
    if not isinstance(row, dict):
        raise RegistryError(f"{where} {family}: row must be an object")
    allowed = {"model", "effort"}
    if family == "agy":
        allowed |= {"fallback_model", "second_model"}
    extra = set(row) - allowed
    if extra:
        raise RegistryError(f"{where} {family}: unknown keys {sorted(extra)}")
    if family_row and not (row.get("model") and row.get("effort")):
        raise RegistryError(f"family {family}: model and effort are required")
    model = row.get("model")
    if model is not None and not re.fullmatch(r"[A-Za-z0-9._:/-]+", str(model)):
        raise RegistryError(f"{where} {family}: bad model name {model!r}")
    effort = row.get("effort")
    if effort is not None and effort not in EFFORTS[family]:
        raise RegistryError(
            f"{where} {family}: effort {effort!r} is not one of {', '.join(EFFORTS[family])}"
        )
    if family == "agy" and model and re.search(r"-(%s)$" % "|".join(AGY_TIERS), str(model)):
        raise RegistryError(f"{where} agy: model {model!r} must be a stem without a tier")


def resolve(family: str, role: str = "interactive", data: dict | None = None) -> dict:
    """The effective row for one family in one role."""
    if family not in FAMILIES:
        raise RegistryError(f"unknown family {family!r}; expected one of {', '.join(FAMILIES)}")
    if role not in ROLES:
        raise RegistryError(f"unknown role {role!r}; expected one of {', '.join(ROLES)}")
    data = data if data is not None else load()
    if family not in data["families"]:
        raise RegistryError(f"registry has no families.{family} row")
    row = dict(data["families"][family])
    row.update(data.get("roles", {}).get(role, {}).get(family, {}))
    result = {"family": family, "role": role, "model": row["model"], "effort": row["effort"]}
    if family == "agy":
        result["slug"] = f"{row['model']}-{row['effort']}"
        for key in ("fallback_model", "second_model"):
            stem = row.get(key)
            result[key.replace("_model", "_slug")] = f"{stem}-{row['effort']}" if stem else None
    return result


# --------------------------------------------------------------------------
# Agy slugs
# --------------------------------------------------------------------------

def newest_in_family(template: str, available: Iterable[str]) -> str:
    """Same rule as dispatch-gemini.py: newest listed member of the template's family."""
    match = re.match(
        r"^(?P<prefix>[a-z]+(?:-[a-z]+)*-)"
        r"(?P<version>\d+(?:[.-]\d+)*)"
        r"(?P<suffix>(?:-[a-z][a-z0-9]*)*)$",
        template,
    )
    if not match:
        return template
    words = match.group("suffix").split("-")[1:]
    template_tier = words.pop() if words and words[-1] in AGY_TIERS else None
    candidate_re = re.compile(
        "^" + re.escape(match.group("prefix")) + r"(\d+(?:[.-]\d+)*)"
        + re.escape("".join("-" + word for word in words))
        + "(?:-(" + "|".join(AGY_TIERS) + "))?$"
    )
    candidates = []
    for slug in available:
        found = candidate_re.match(slug)
        if found:
            version = tuple(int(part) for part in re.split(r"[.-]", found.group(1)))
            candidates.append((slug, version, found.group(2)))
    if not candidates:
        return template
    for wanted in (template_tier or "high", "high", None):
        members = [(slug, version) for slug, version, tier in candidates if tier == wanted]
        if members:
            best = max(version for _, version in members)
            tied = [slug for slug, version in members if version == best]
            return template if template in tied else tied[0]
    return max(candidates, key=lambda item: item[1])[0]


def family_key(slug: str) -> tuple | None:
    """(name words, tier) of an Agy slug, ignoring the version."""
    match = re.match(r"^([a-z]+(?:-[a-z]+)*)-\d+(?:[.-]\d+)*((?:-[a-z][a-z0-9]*)*)$", slug)
    if not match:
        return None
    words = match.group(2).split("-")[1:]
    tier = words.pop() if words and words[-1] in AGY_TIERS else None
    return (match.group(1), tuple(words), tier)


def display_to_slug(display: str) -> str:
    """'Gemini 4 Argon (High)' -> 'gemini-4-argon-high'."""
    text = display.strip().lower().replace("(", " ").replace(")", " ")
    return "-".join(text.split())


def agy_listing(timeout: int = 20) -> dict | None:
    """slug -> display name from `agy models`, or None when Agy cannot answer."""
    raw = os.environ.get("ANTIGRAVITY_BIN", "agy")
    exe = shutil.which(raw) or (raw if Path(raw).is_file() else None)
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, "models"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    listing = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and re.fullmatch(r"[a-z0-9.\-]+", parts[0]):
            listing[parts[0]] = parts[1].strip()
    return listing or None


# --------------------------------------------------------------------------
# Tool config locations
# --------------------------------------------------------------------------

def codex_config_path() -> Path:
    home = os.environ.get("CODEX_HOME", "").strip()
    return (Path(home) if home else Path.home() / ".codex") / "config.toml"


def agy_settings_path() -> Path:
    override = os.environ.get("ANTIGRAVITY_SETTINGS_FILE", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def claude_settings_path() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(override) if override else Path.home() / ".claude") / "settings.json"


TOML_KEY_RE = (
    r"^(?P<indent>[ \t]*)(?P<quote>[\"']?){key}(?P=quote)[ \t]*=[ \t]*"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^#\s]*)(?P<comment>[ \t]*#.*)?[ \t]*$"
)


class TomlUnsupported(Exception):
    """The file uses TOML this line editor will not touch."""


def toml_top_lines(text: str) -> tuple:
    """(lines, indexes of top-level lines outside any multi-line string, end).

    `end` is the index of the first table header. A multi-line string can hold
    a line that looks like a header or a key, so the scan tracks the two
    multi-line delimiters and never reads inside one.
    """
    lines = text.splitlines(keepends=True)
    usable = []
    inside = None
    end = len(lines)
    for index, line in enumerate(lines):
        if inside is None:
            if re.match(r"^\s*\[", line):
                end = index
                break
            usable.append(index)
            first = min(
                (pos, delim) for delim in ('"""', "'''")
                for pos in [line.find(delim)] if pos >= 0
            ) if ('"""' in line or "'''" in line) else None
            if first and line.count(first[1]) % 2 == 1:
                inside = first[1]
        elif line.count(inside) % 2 == 1:
            inside = None
    if inside is not None:
        raise TomlUnsupported("an unterminated multi-line string")
    return lines, usable, end


def toml_top_level(text: str) -> dict:
    """String values of top-level keys (before the first table).

    The standard library's parser answers where it exists (Python 3.11+). The
    line reader below is the fallback; it can miss a key that follows a
    multi-line array.
    """
    try:
        import tomllib
    except ImportError:
        tomllib = None
    if tomllib is not None:
        try:
            parsed = tomllib.loads(text)
        except ValueError:  # TOMLDecodeError; the line reader does what it can
            parsed = None
        if parsed is not None:
            return {key: value for key, value in parsed.items() if isinstance(value, str)}
    values = {}
    try:
        lines, usable, _ = toml_top_lines(text)
    except TomlUnsupported:
        return values
    for index in usable:
        match = re.match(r"^\s*([\"']?)([A-Za-z0-9_-]+)\1\s*=\s*([\"'])(.*?)\3", lines[index])
        if match:
            values.setdefault(match.group(2), match.group(4))
    return values


def toml_set_top_level(text: str, updates: dict) -> str:
    """Set top-level string keys, keeping every other byte of the file.

    Raises TomlUnsupported instead of guessing. A changed result is parsed back
    with the standard library's TOML parser and must equal the original with
    only the updated keys changed; where that parser is missing (Python before
    3.11) a change is refused.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines, usable, end = toml_top_lines(text)
    pending = dict(updates)
    for index in usable:
        bare = lines[index].rstrip("\r\n")
        for key in list(pending):
            match = re.match(TOML_KEY_RE.format(key=re.escape(key)), bare)
            if match:
                ending = lines[index][len(bare):] or newline
                comment = match.group("comment") or ""
                quote = match.group("quote")
                lines[index] = (f'{match.group("indent")}{quote}{key}{quote} = '
                                f'"{pending.pop(key)}"{comment}{ending}')
            elif re.match(r"^\s*[\"']?%s[\"']?\s*=" % re.escape(key), bare):
                raise TomlUnsupported(f"a {key} value this editor cannot rewrite")
    if pending:
        insert_at = end
        while insert_at > 0 and not lines[insert_at - 1].strip():
            insert_at -= 1
        if insert_at > 0 and not lines[insert_at - 1].endswith(("\n", "\r")):
            lines[insert_at - 1] += newline
        lines[insert_at:insert_at] = [f'{key} = "{value}"{newline}' for key, value in pending.items()]
    result = "".join(lines)
    if result != text:
        _toml_check(text, result, updates)
    return result


def _toml_check(before: str, after: str, updates: dict) -> None:
    """Refuse an edit that changed anything but the updated keys."""
    try:
        import tomllib
    except ImportError:
        # Python < 3.11 has no parser to check the edit with, and the line
        # reader that made the edit cannot vouch for it (an escaped key or a
        # multi-line array defeats it), so the file is left alone.
        raise TomlUnsupported("an edit this interpreter cannot check (Python before 3.11 has no TOML parser)")
    try:
        old = tomllib.loads(before)
    except tomllib.TOMLDecodeError as exc:
        raise TomlUnsupported(f"TOML that does not parse ({exc})")
    try:
        new = tomllib.loads(after)
    except tomllib.TOMLDecodeError as exc:
        raise TomlUnsupported(f"an edit that would break the file ({exc})")
    old.update(updates)
    if new != old:
        raise TomlUnsupported("an edit that would change other settings")


def atomic_write(path: Path, payload: bytes, backup: bool = True) -> None:
    def publish(target: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    if backup and path.exists():
        publish(path.with_name(path.name + ".agent-models.bak"), path.read_bytes())
    publish(path, payload)


SYNC_LOCK_STALE_SECONDS = 120


class sync_lock:
    """Exclusive lock for one read, check, backup, and write of a config file.

    Several bootstraps start together at session start. Without the lock two
    of them read the old file, and the second copies the first one's result
    over the backup, losing the original. A holder that died leaves a lock
    that is taken over once it is older than SYNC_LOCK_STALE_SECONDS. `held`
    is False when another process has it; the caller then skips, since that
    process is writing the same values.
    """

    def __init__(self, path: Path):
        self.lock = path.with_name(path.name + ".agent-models.lock")
        self.held = False

    def __enter__(self) -> "sync_lock":
        import time
        for _ in range(2):
            try:
                os.close(os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                self.held = True
                break
            except FileExistsError:
                try:
                    # lstat, so a dangling symlink still has an age; abs, so a
                    # lock dated in the future does not hold forever.
                    age = abs(time.time() - self.lock.lstat().st_mtime)
                    if age < SYNC_LOCK_STALE_SECONDS:
                        break
                    self.lock.unlink()
                except OSError:
                    break
            except OSError:
                break
        return self

    def __exit__(self, *exc) -> None:
        if self.held:
            try:
                self.lock.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------
# user/settings.json rendering (Claude's interactive values)
# --------------------------------------------------------------------------

def rendered_settings(settings: dict, data: dict) -> dict:
    claude = resolve("claude", "interactive", data)
    worker = resolve("claude", "worker", data)
    out = json.loads(json.dumps(settings))
    out["model"] = claude["model"]
    if claude["effort"] == "max":
        out.pop("effortLevel", None)  # settings cannot persist max; the env entry carries it
    else:
        out["effortLevel"] = claude["effort"]
    env = out.setdefault("env", {})
    env["CLAUDE_CODE_EFFORT_LEVEL"] = claude["effort"]
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = worker["model"]
    return out


def settings_bytes(settings: dict) -> bytes:
    return (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def cmd_render(args: argparse.Namespace) -> int:
    data = load()
    path = Path(args.settings) if args.settings else DEFAULT_SETTINGS
    current = path.read_bytes()
    wanted = settings_bytes(rendered_settings(json.loads(current.decode("utf-8-sig")), data))
    if current == wanted:
        return 0
    if args.check:
        print(f"agent_models: {path} is stale; run agent_models.py render", file=sys.stderr)
        return 1
    atomic_write(path, wanted, backup=False)
    print(f"agent_models: rendered {path}")
    return 0


# --------------------------------------------------------------------------
# set
# --------------------------------------------------------------------------

def in_source_repo() -> bool:
    root = SCRIPT_DIR.parent
    return all(
        (root / rel).exists()
        for rel in ("bootstrap/bootstrap.sh", "bootstrap/bootstrap.ps1",
                    "scripts/generate_agent_configs.py", "skills")
    ) and root.name != "repo"


def cmd_set(args: argparse.Namespace) -> int:
    if not in_source_repo() and not os.environ.get("AGENT_MODELS_FILE"):
        print("agent_models: set edits the registry in a source repo (agent-config or "
              "anywhere-agents); a consumer copy is overwritten by the next bootstrap",
              file=sys.stderr)
        return 2
    if not (args.model or args.effort):
        print("agent_models: set needs --model and/or --effort", file=sys.stderr)
        return 2
    path = registry_path()
    data = load(path)
    if args.family not in data["families"]:
        raise RegistryError(f"registry has no families.{args.family} row")
    if args.role in (None, "interactive", "reviewer") and not args.role_only:
        row = data["families"][args.family]
    else:
        row = data.setdefault("roles", {}).setdefault(args.role, {}).setdefault(args.family, {})
    if args.model:
        row["model"] = args.model
    if args.effort:
        row["effort"] = args.effort
    validate(data)
    atomic_write(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                 backup=False)
    print(f"agent_models: {args.family} {args.role or 'family'} -> "
          f"{resolve(args.family, args.role or 'interactive', data)}")
    if path == DEFAULT_REGISTRY and DEFAULT_SETTINGS.exists():
        return cmd_render(argparse.Namespace(settings=None, check=False))
    return 0


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------

def sync_codex(data: dict, dry_run: bool) -> str:
    path = codex_config_path()
    if not path.parent.is_dir():
        return f"codex: skipped, no Codex home at {path.parent}"
    row = resolve("codex", "interactive", data)

    def plan() -> tuple:
        raw = path.read_bytes() if path.exists() else b""
        text = raw.decode("utf-8-sig")
        updates = {"model": row["model"], "model_reasoning_effort": row["effort"]}
        have = toml_top_level(text)
        provider = have.get("model_provider")
        note = ""
        if provider and provider != "openai":
            updates.pop("model")
            note = f" (model left alone: model_provider is {provider})"
        if all(have.get(key) == value for key, value in updates.items()):
            return raw, text, updates, note, text  # already right, whatever the layout
        return raw, text, updates, note, toml_set_top_level(text, updates)

    try:
        raw, text, updates, note, new_text = plan()
        if new_text == text:
            return f"codex: current ({row['model']} {row['effort']}){note}"
        if dry_run:
            return f"codex: would set {updates} in {path}{note}"
        with sync_lock(path) as lock:
            if not lock.held:
                return "codex: skipped, another sync is writing the file"
            raw, text, updates, note, new_text = plan()  # reread under the lock
            if new_text != text:
                bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
                atomic_write(path, bom + new_text.encode("utf-8"))
    except TomlUnsupported as exc:
        return f"codex: skipped, {path} has {exc}; set model and model_reasoning_effort by hand"
    except UnicodeDecodeError:
        return f"codex: skipped, {path} is not UTF-8"
    return f"codex: set {updates} in {path}{note}"


def sync_agy(data: dict, dry_run: bool) -> str:
    path = agy_settings_path()
    if not path.parent.is_dir():
        return f"agy: skipped, no Antigravity settings directory at {path.parent}"
    row = resolve("agy", "interactive", data)

    def current() -> tuple:
        settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        if not isinstance(settings, dict):
            raise ValueError("not a JSON object")
        return settings, settings.get("model")

    try:
        settings, model = current()
    except (OSError, ValueError) as exc:
        return f"agy: skipped, cannot read {path}: {exc}"
    if agy_family_state(model, row) == "primary":
        return f"agy: current ({model})"
    # On the fallback family the listing is asked again each time, so the
    # setting moves to the primary family as soon as the account lists it.
    listing = agy_listing()
    if not listing:
        return "agy: skipped, `agy models` did not answer; run agy once interactively"
    slug = newest_in_family(row["slug"], listing)
    if slug not in listing and row.get("fallback_slug"):
        slug = newest_in_family(row["fallback_slug"], listing)
    if slug not in listing:
        return f"agy: skipped, the signed-in account lists no {row['slug']}"
    if model == listing[slug]:
        return f"agy: current ({model}; the account lists no {row['slug']})"
    if dry_run:
        return f"agy: would set model {listing[slug]!r} in {path}"
    with sync_lock(path) as lock:
        if not lock.held:
            return "agy: skipped, another sync is writing the file"
        try:
            settings, model = current()  # reread under the lock
        except (OSError, ValueError) as exc:
            return f"agy: skipped, cannot read {path}: {exc}"
        if model == listing[slug]:
            return f"agy: current ({model}; the account lists no {row['slug']})"
        settings["model"] = listing[slug]
        atomic_write(path, (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return f"agy: set model {listing[slug]!r} in {path}"


def agy_family_state(display, row: dict) -> str:
    """primary, fallback, or other, for an Antigravity settings model.

    The version floats, so only the family and tier are compared.
    """
    have = family_key(display_to_slug(display)) if isinstance(display, str) else None
    if have is None:
        return "other"
    if have == family_key(row["slug"]):
        return "primary"
    if row.get("fallback_slug") and have == family_key(row["fallback_slug"]):
        return "fallback"
    return "other"


def agy_fallback_verdict(row: dict) -> tuple:
    """(ok, drift, or skip, reason) for an Antigravity setting on the fallback family.

    sync installs the fallback only for an account that lists no member of the
    primary family, so doctor and verify ask the listing the same question.
    """
    listing = agy_listing()
    if listing is None:
        return "skip", "fallback family, and `agy models` did not answer"
    if newest_in_family(row["slug"], listing) not in listing:
        return "ok", "fallback family, the account lists no primary"
    return "drift", "fallback family, though the account lists the primary"


def cmd_sync(args: argparse.Namespace) -> int:
    if os.environ.get("AGENT_MODELS_SYNC", "").strip().lower() in ("off", "0", "false", "no", "disabled"):
        if not args.quiet:
            print("agent_models: sync skipped (AGENT_MODELS_SYNC=off)")
        return 0
    try:
        data = load()
    except RegistryError as exc:
        print(f"agent_models: sync skipped: {exc}", file=sys.stderr)
        return 0
    for fn in (sync_codex, sync_agy):
        try:
            message = fn(data, args.dry_run)
        except (OSError, ValueError, KeyError) as exc:
            message = f"{fn.__name__[5:]}: failed: {exc}"
            print(f"agent_models: {message}", file=sys.stderr)
            continue
        if not args.quiet or " set " in f" {message}" or "failed" in message:
            print(f"agent_models: {message}")
    return 0


# --------------------------------------------------------------------------
# scan (drift guard)
# --------------------------------------------------------------------------

EFFORT_WORDS = r"(?:minimal|low|medium|high|xhigh|max|ultra)"
LITERAL_PATTERNS = [
    re.compile(r"\bgpt-\d[\w.\-]*"),
    re.compile(r"\bgrok-\d[\w.\-]*"),
    # A model slug has a family word after the version; `gemini-5h` is a quota
    # bucket id, not a model.
    re.compile(r"\bgemini-\d+(?:\.\d+)*-[a-z][\w.\-]*"),
    re.compile(r"\bclaude-(?:opus|sonnet|haiku|fable)-\d[\w\-]*"),
    re.compile(r"--model(?:[ =]['\"]?|['\"]\s*,\s*['\"])(?:opus|sonnet|haiku|fable)\b"),
    re.compile(r"\bANTHROPIC(?:_DEFAULT)?_MODEL['\"]?\s*[:=]\s*['\"]?(?:opus|sonnet|haiku|fable)\b"),
    re.compile(r"\bmodel['\"]?\s*[:=]\s*['\"](?:opus|sonnet|haiku|fable)['\"]"),
    re.compile(r"--effort(?:[ =]['\"]?|['\"]\s*,\s*['\"])" + EFFORT_WORDS + r"\b"),
    re.compile(r"\bmodel_reasoning_effort\s*=\s*['\"]?" + EFFORT_WORDS),
    re.compile(r"\b\w*(?:EFFORT|REASONING)\w*['\"]?\s*(?::-|[:=])\s*['\"]?" + EFFORT_WORDS + r"\b"),
    re.compile(r"\beffortLevel['\"]?\s*:\s*['\"]" + EFFORT_WORDS),
    # A default written as an assignment or a key: effort = "high", "effort": "high".
    re.compile(r"\b(?:\w*_)?effort['\"]?\s*[:=]\s*['\"]" + EFFORT_WORDS + r"['\"]"),
    re.compile(r"\bCLAUDE_CODE_SUBAGENT_MODEL['\"]?\s*[:=]\s*['\"]?(?:opus|sonnet|haiku|fable)"),
]
SCAN_SUFFIXES = {".py", ".sh", ".ps1", ".md", ".json", ".yaml", ".yml", ".toml", ".js", ".mjs",
                 ".ts", ".txt", ".cfg", ".ini", ""}


def scan_text(text: str) -> Iterator[tuple[int, str, str]]:
    for number, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        for pattern in LITERAL_PATTERNS:
            for match in pattern.finditer(line):
                yield number, match.group(0), line.strip()


def iter_files(paths: Iterable[str]) -> Iterator[Path]:
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            yield path
            continue
        for child in sorted(path.rglob("*")):
            if any(part in (".git", "node_modules", "__pycache__", ".agent-config", "site")
                   for part in child.parts):
                continue
            if child.is_file() and child.suffix in SCAN_SUFFIXES:
                yield child


def scan(paths: Iterable[str]) -> list:
    hits = []
    for path in iter_files(paths):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, literal, line in scan_text(text):
            hits.append((str(path), number, literal, line))
    return hits


def cmd_scan(args: argparse.Namespace) -> int:
    hits = scan(args.paths)
    for path, number, literal, line in hits:
        print(f"{path}:{number}: {literal}    {line[:140]}")
    return 1 if hits else 0


# --------------------------------------------------------------------------
# actual: what a CLI reports it ran on
# --------------------------------------------------------------------------

def claude_actual(session_id: str) -> tuple | None:
    """(model id, effort) from the transcript of a Claude Code session."""
    base = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    projects = (Path(base) if base else Path.home() / ".claude") / "projects"
    found = sorted(projects.glob(f"*/{session_id}.jsonl"))
    if not found:
        return None
    model = effort = None
    for line in found[-1].read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message") or {}
        if isinstance(message, dict) and message.get("model"):
            model = message["model"]
        if record.get("effort"):
            effort = record["effort"]
    return (model, effort) if model else None


def codex_actual(stderr_text: str) -> tuple | None:
    """(model, reasoning effort) from the header `codex exec` prints on stderr."""
    model = re.search(r"^model:\s*(\S+)\s*$", stderr_text, re.MULTILINE)
    effort = re.search(r"^reasoning effort:\s*(\S+)\s*$", stderr_text, re.MULTILINE)
    return (model.group(1), effort.group(1) if effort else None) if model else None


def agy_actual(log_text: str) -> tuple | None:
    """(slug, tier) from the model label an Agy run propagated to its backend."""
    labels = re.findall(r'Propagating selected model override to backend: label="([^"]+)"', log_text)
    if not labels:
        return None
    slug = display_to_slug(labels[-1])
    key = family_key(slug)
    return (slug, key[2] if key else None)


def grok_home_path() -> Path:
    home = os.environ.get("GROK_HOME", "").strip()
    return Path(home) if home else Path.home() / ".grok"


def _grok_model_from_record(record: dict) -> str | None:
    for node in (record, record.get("session")):
        if not isinstance(node, dict):
            continue
        usage = node.get("modelUsage")
        primary = node.get("primaryModelId")
        if isinstance(usage, dict) and usage:
            if isinstance(primary, str) and primary in usage:
                return primary
            first = next(iter(usage))
            if isinstance(first, str) and first:
                return first
        if isinstance(primary, str) and primary:
            return primary
    return None


def grok_actual(json_text: str | None = None, session_id: str | None = None) -> tuple | None:
    """(model id, reasoning effort) from a Grok JSON report and/or session directory."""
    model = effort = None
    sid = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
    if isinstance(json_text, str) and json_text.strip():
        raw = json_text.strip()
        records = []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                records.append(parsed)
        except ValueError:
            for line in raw.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    records.append(parsed)
        if not records and not sid and re.fullmatch(r"[A-Za-z0-9._-]+", raw):
            sid = raw
        for record in records:
            found_model = _grok_model_from_record(record)
            if found_model:
                model = found_model
            found_sid = record.get("sessionId") or record.get("session_id")
            if not found_sid and isinstance(record.get("info"), dict):
                found_sid = record["info"].get("id")
            if isinstance(found_sid, str) and found_sid.strip():
                sid = sid or found_sid.strip()
            if isinstance(record.get("reasoning_effort"), str) and record["reasoning_effort"].strip():
                effort = record["reasoning_effort"].strip()
    if sid and re.fullmatch(r"[A-Za-z0-9._-]+", sid):
        sessions = grok_home_path() / "sessions"
        for session_dir in sorted(sessions.glob(f"*/{sid}")):
            if not model:
                usage_path = session_dir / "usage.json"
                try:
                    usage_data = json.loads(usage_path.read_text(encoding="utf-8", errors="replace"))
                    if isinstance(usage_data, dict):
                        model = _grok_model_from_record(usage_data) or model
                except (OSError, ValueError):
                    pass
            if not effort:
                summary_path = session_dir / "summary.json"
                try:
                    summary_data = json.loads(summary_path.read_text(encoding="utf-8", errors="replace"))
                    if isinstance(summary_data, dict):
                        raw_effort = summary_data.get("reasoning_effort")
                        if isinstance(raw_effort, str) and raw_effort.strip():
                            effort = raw_effort.strip()
                except (OSError, ValueError):
                    pass
    return (model, effort) if model else None


def compare(family: str, expected_model: str, expected_effort: str | None,
            actual_model: str, actual_effort: str | None) -> str:
    """ok, mismatch, or unknown.

    A field the CLI reported and that differs is a mismatch. An expected
    effort the CLI did not report is unknown: absence does not show that
    another effort ran. Only complete matching evidence is ok.
    """
    tier_missing = False
    if family == "claude":
        alias = expected_model in ("fable", "opus", "sonnet", "haiku")
        model_ok = actual_model == expected_model or (
            alias and actual_model.startswith(f"claude-{expected_model}-"))
    elif family == "agy":
        # The version floats, so the family words decide. A tier written in the
        # expected slug must not be contradicted; a report that names no tier
        # leaves it unknown. A slug with no version is compared whole.
        want, have = family_key(expected_model), family_key(actual_model)
        if want is None or have is None:
            model_ok = actual_model == expected_model
        else:
            model_ok = have[:2] == want[:2] and (None in (want[2], have[2]) or have[2] == want[2])
            tier_missing = want[2] is not None and have[2] is None
    elif family == "grok":
        model_ok = actual_model == expected_model or (
            not expected_model.endswith(("-build", "-fast"))
            and actual_model == f"{expected_model}-build"
        )
    else:
        model_ok = actual_model == expected_model
    if not model_ok:
        return "mismatch"
    if expected_effort is not None and actual_effort is not None and actual_effort != expected_effort:
        return "mismatch"
    if tier_missing or (expected_effort is not None and actual_effort is None):
        return "unknown"
    return "ok"


def matches(family: str, expected_model: str, expected_effort: str | None,
            actual_model: str, actual_effort: str | None) -> bool:
    return compare(family, expected_model, expected_effort, actual_model, actual_effort) == "ok"


def cmd_actual(args: argparse.Namespace) -> int:
    """Print `actual=<model> <effort> status=<ok|mismatch|unknown>`; always exit 0."""
    result = None
    try:
        if args.family == "claude" and args.session_id:
            result = claude_actual(args.session_id)
        elif args.family == "codex" and args.stderr_file:
            result = codex_actual(Path(args.stderr_file).read_text(encoding="utf-8", errors="replace"))
        elif args.family == "agy" and args.log_file:
            result = agy_actual(Path(args.log_file).read_text(encoding="utf-8", errors="replace"))
        elif args.family == "grok":
            json_file = getattr(args, "json_file", None) or getattr(args, "log_file", None)
            json_text = (
                Path(json_file).read_text(encoding="utf-8", errors="replace")
                if json_file else None
            )
            result = grok_actual(json_text, session_id=getattr(args, "session_id", None))
    except OSError:
        result = None
    if not result:
        print("actual=unknown status=unknown")
        print(f"agent_models: MODEL-UNVERIFIED family={args.family}: the CLI report was not found",
              file=sys.stderr)
        return 0
    model, effort = result
    status = "ok"
    if args.expect_model:
        status = compare(args.family, args.expect_model, args.expect_effort, model, effort)
    requested = f"{args.expect_model} {args.expect_effort or '-'}"
    if status == "mismatch":
        print(f"agent_models: MODEL-MISMATCH family={args.family} requested={requested} "
              f"actual={model} {effort or '-'}", file=sys.stderr)
    elif status == "unknown":
        print(f"agent_models: MODEL-UNVERIFIED family={args.family} requested={requested} "
              f"actual={model} -: the CLI did not report its effort", file=sys.stderr)
    print(f"actual={model} {effort or '-'} status={status}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Launch each CLI with no model flags and read back what it actually used.

    This proves the tool's own config (what bootstrap synced) is in effect, as
    a fresh terminal session would see it. It spends one short request per
    family. Reviewer runs are checked by the dispatchers' review-model record.
    """
    import uuid

    data = load()
    families = [args.family] if args.family else list(REQUIRED_FAMILIES)
    prompt = "Reply with the single word OK."
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_EFFORT_LEVEL", "ANTHROPIC_MODEL", "CLAUDECODE",
                        "CLAUDE_CODE_ENTRYPOINT")}
    failed = False
    with tempfile.TemporaryDirectory(prefix="agent-models-verify-") as scratch:
        for family in families:
            want = resolve(family, "interactive", data)
            expect_model = want["slug"] if family == "agy" else want["model"]
            result = None
            try:
                if family == "claude" and shutil.which("claude"):
                    sid = str(uuid.uuid4())
                    subprocess.run(["claude", "-p", prompt, "--session-id", sid],
                                   cwd=scratch, env=env, capture_output=True, text=True,
                                   timeout=300, stdin=subprocess.DEVNULL)
                    result = claude_actual(sid)
                elif family == "codex" and shutil.which("codex"):
                    run = subprocess.run(["codex", "exec", "--skip-git-repo-check", "-C", scratch, prompt],
                                         env=env, capture_output=True, text=True, timeout=300,
                                         stdin=subprocess.DEVNULL)
                    result = codex_actual(run.stderr)
                elif family == "agy" and shutil.which(os.environ.get("ANTIGRAVITY_BIN", "agy")):
                    log = Path(scratch) / "agy.log"
                    subprocess.run([os.environ.get("ANTIGRAVITY_BIN", "agy"), "-p", prompt,
                                    "--log-file", str(log)], cwd=scratch, env=env,
                                   capture_output=True, text=True, timeout=300,
                                   stdin=subprocess.DEVNULL)
                    result = agy_actual(log.read_text(encoding="utf-8", errors="replace")
                                        if log.exists() else "")
                elif family == "grok" and shutil.which(os.environ.get("GROK_BIN", "grok")):
                    grok_env = dict(
                        env,
                        GROK_FOLDER_TRUST="0",
                        GROK_CLAUDE_HOOKS_ENABLED="false",
                        GROK_CLAUDE_MCPS_ENABLED="false",
                        GROK_CLAUDE_SKILLS_ENABLED="false",
                        GROK_CURSOR_HOOKS_ENABLED="false",
                        GROK_CURSOR_MCPS_ENABLED="false",
                        GROK_DISABLE_AUTOUPDATER="1",
                    )
                    run = subprocess.run(
                        [
                            os.environ.get("GROK_BIN", "grok"), "-p", "Reply OK",
                            "--output-format", "json", "--no-auto-update",
                            "-m", want["model"], "--effort", want["effort"],
                        ],
                        cwd=scratch, env=grok_env, capture_output=True, text=True,
                        timeout=300, stdin=subprocess.DEVNULL,
                    )
                    result = grok_actual(run.stdout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"[skip] {family}: {exc}")
                continue
            if not result:
                print(f"[skip] {family}: CLI missing or its report not found")
                continue
            model, effort = result
            status = compare(family, expect_model, want["effort"], model, effort)
            if family == "agy" and agy_family_state(model, want) == "fallback":
                # The same account-aware answer doctor gives, so the three
                # commands agree on an account that lacks the primary family.
                verdict, _ = agy_fallback_verdict(want)
                status = {"skip": "unknown", "drift": "mismatch"}.get(verdict) or compare(
                    family, want["fallback_slug"], want["effort"], model, effort)
            failed = failed or status == "mismatch"
            label = {"ok": "ok", "mismatch": "drift", "unknown": "unverified"}[status]
            print(f"[{label}] {family} interactive: actual {model} {effort or '-'}"
                  f" (registry {expect_model} {want['effort']})")
    return 1 if failed else 0


# --------------------------------------------------------------------------
# show / doctor
# --------------------------------------------------------------------------

def cmd_resolve(args: argparse.Namespace) -> int:
    try:
        row = resolve(args.family, args.role)
    except RegistryError as exc:
        print(f"agent_models: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(row))
    elif args.field:
        value = row.get(args.field)
        if value is None:
            print(f"agent_models: {args.family} {args.role} has no {args.field}", file=sys.stderr)
            return 2
        print(value)
    else:
        print(f"{row['model']} {row['effort']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    data = load()
    present = [f for f in FAMILIES if f in data["families"]]
    rows = [resolve(f, r, data) for f in present for r in ROLES]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"registry: {registry_path()}")
    print(f"{'family':<8} {'role':<12} {'model':<22} {'effort':<8} extra")
    for row in rows:
        extra = " ".join(f"{k}={v}" for k, v in row.items()
                         if k in ("slug", "fallback_slug", "second_slug") and v)
        print(f"{row['family']:<8} {row['role']:<12} {row['model']:<22} {row['effort']:<8} {extra}")
    return 0


def doctor_checks(data: dict) -> list:
    """(status, message) pairs; status is ok, drift, or skip."""
    checks = []
    claude = resolve("claude", "interactive", data)
    worker = resolve("claude", "worker", data)
    path = claude_settings_path()
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
        env = settings.get("env", {}) if isinstance(settings, dict) else {}
        pairs = [
            ("model", settings.get("model"), claude["model"]),
            ("env.CLAUDE_CODE_EFFORT_LEVEL", env.get("CLAUDE_CODE_EFFORT_LEVEL"), claude["effort"]),
            ("env.CLAUDE_CODE_SUBAGENT_MODEL", env.get("CLAUDE_CODE_SUBAGENT_MODEL"), worker["model"]),
        ]
        if "effortLevel" in settings:
            pairs.append(("effortLevel", settings.get("effortLevel"), claude["effort"]))
        # /effort saves a per-model level here; the env entry outranks it, so a
        # different value is a setting that looks applied and is not.
        per_model = settings.get("modelSettings")
        for model_id, entry in (per_model.items() if isinstance(per_model, dict) else ()):
            if isinstance(entry, dict) and "effortLevel" in entry and matches(
                    "claude", claude["model"], None, str(model_id), None):
                pairs.append((f"modelSettings.{model_id}.effortLevel",
                              entry["effortLevel"], claude["effort"]))
        for key, have, want in pairs:
            status = "ok" if have == want else "drift"
            checks.append((status, f"claude {path} {key}={have!r} (registry {want!r})"))
    except (OSError, ValueError) as exc:
        checks.append(("skip", f"claude: cannot read {path}: {exc}"))
    for name in ("ANTHROPIC_MODEL", "CLAUDE_CODE_EFFORT_LEVEL"):
        have = os.environ.get(name)
        want = claude["model"] if name == "ANTHROPIC_MODEL" else claude["effort"]
        if have and have != want:
            checks.append(("drift", f"claude: process env {name}={have!r} (registry {want!r})"))
    codex = resolve("codex", "interactive", data)
    cpath = codex_config_path()
    try:
        values = toml_top_level(cpath.read_text(encoding="utf-8-sig")) if cpath.exists() else None
    except (OSError, ValueError) as exc:
        values = None
        checks.append(("skip", f"codex: cannot read {cpath}: {exc}"))
    if values is not None:
        for key, want in (("model", codex["model"]), ("model_reasoning_effort", codex["effort"])):
            have = values.get(key)
            if key == "model" and values.get("model_provider", "openai") != "openai":
                checks.append(("skip", f"codex: model_provider {values['model_provider']}; model not compared"))
                continue
            checks.append(("ok" if have == want else "drift",
                           f"codex {cpath} {key}={have!r} (registry {want!r})"))
    elif not cpath.exists():
        checks.append(("skip", f"codex: no {cpath}"))
    agy = resolve("agy", "interactive", data)
    apath = agy_settings_path()
    try:
        have = json.loads(apath.read_text(encoding="utf-8-sig")).get("model")
        state = agy_family_state(have, agy)
        status, why = ("ok", "") if state == "primary" else ("drift", "")
        if state == "fallback":
            status, reason = agy_fallback_verdict(agy)
            why = "; " + reason
        checks.append((status, f"agy {apath} model={have!r} (registry {agy['slug']}{why})"))
    except (OSError, ValueError, AttributeError) as exc:
        checks.append(("skip", f"agy: cannot read {apath}: {exc}"))
    if DEFAULT_SETTINGS.exists() and registry_path() == DEFAULT_REGISTRY:
        current = DEFAULT_SETTINGS.read_bytes()
        wanted = settings_bytes(rendered_settings(json.loads(current.decode("utf-8-sig")), data))
        checks.append(("ok" if current == wanted else "drift",
                       f"repo {DEFAULT_SETTINGS} rendered keys"))
    return checks


def cmd_doctor(args: argparse.Namespace) -> int:
    data = load()
    checks = doctor_checks(data)
    for status, message in checks:
        print(f"[{status}] {message}")
    drift = any(status == "drift" for status, _ in checks)
    if args.scan:
        hits = scan(args.scan)
        for path, number, literal, line in hits:
            print(f"[literal] {path}:{number}: {literal}    {line[:120]}")
        print(f"[scan] {len(hits)} literal(s) under {', '.join(args.scan)}")
        drift = drift or bool(hits)
    return 1 if drift else 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Single source for agent models and effort")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("resolve")
    p.add_argument("--family", required=True, choices=FAMILIES)
    p.add_argument("--role", default="interactive", choices=ROLES)
    p.add_argument("--field", choices=("model", "effort", "slug", "fallback_slug", "second_slug"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_resolve)
    p = sub.add_parser("show")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("render")
    p.add_argument("--check", action="store_true")
    p.add_argument("--settings")
    p.set_defaults(func=cmd_render)
    p = sub.add_parser("set")
    p.add_argument("--family", required=True, choices=FAMILIES)
    p.add_argument("--role", choices=ROLES)
    p.add_argument("--role-only", action="store_true",
                   help="write an interactive or reviewer override instead of the family row")
    p.add_argument("--model")
    p.add_argument("--effort")
    p.set_defaults(func=cmd_set)
    p = sub.add_parser("sync")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("doctor")
    p.add_argument("--scan", nargs="*", default=[])
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("scan")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_scan)
    p = sub.add_parser("actual")
    p.add_argument("--family", required=True, choices=FAMILIES)
    p.add_argument("--session-id", help="claude or grok: the session id the run used")
    p.add_argument("--stderr-file", help="codex: captured stderr of codex exec")
    p.add_argument("--log-file", help="agy: the --log-file the run wrote")
    p.add_argument("--json-file", help="grok: JSON result or streaming-json file")
    p.add_argument("--expect-model", help="requested model (agy: slug or stem)")
    p.add_argument("--expect-effort")
    p.set_defaults(func=cmd_actual)
    p = sub.add_parser("verify")
    p.add_argument("--family", choices=FAMILIES)
    p.set_defaults(func=cmd_verify)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RegistryError as exc:
        print(f"agent_models: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
