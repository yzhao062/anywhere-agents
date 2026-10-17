#!/usr/bin/env python3
"""Render the session-start banner from files on disk.

The banner used to be a set of instructions for the agent: read this file,
run that command, compare these versions, and print seven lines before the
first reply. The instructions cost about 11 KB of every loaded AGENTS.md and
several shell spawns per session, and each agent derived the fields a little
differently. This script derives them once and publishes the result.

Two modes, chosen from the explicit ``--root``:

- A consumer repository (``<root>/.agent-config/bootstrap.sh`` or
  ``.ps1`` exists) gets one atomically published report at
  ``<root>/.agent-config/banner.txt``. Its first line is an HTML comment
  carrying the metadata an agent needs to decide freshness without a shell:
  the session-event timestamp when one is present, the bootstrap ``run_id``,
  and the completion status from the ledger. The seven banner lines follow.
  The hook and both bootstrap entry points call this after every refresh
  attempt, successful or not, once the ledger is final.
- A source repository (``bootstrap/``, ``scripts/generate_agent_configs.py``
  and ``skills/`` at the root) gets the seven lines on stdout and no
  ``.agent-config/`` directory.

The ``<model>`` field stays a placeholder because no hook input carries the
model reliably; the agent substitutes its own id when it prints the lines.

A nonzero bootstrap result, or a ledger with ``completed: false``, produces
an incomplete-checks banner that names the failed phase or the skipped step.
A zero exit alone never yields ``all clear``: every other check still runs.
When an input is missing or unreadable, its check reports ``unavailable``
rather than a zero, because a zero reads as a clean check.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Optional

REPORT_NAME = "banner.txt"
METADATA_PREFIX = "<!-- anywhere-agents banner"
TITLE = "📦 anywhere-agents active"
FALLBACK_LINES = (
    TITLE,
    "   ├── Agent: <model>",
    "   └── Session check: checks unavailable (run bootstrap or read .agent-config/last-run.json)",
)
VERSION_TIMEOUT_SECONDS = 3.0
PROJECT_DOC_MIN_BYTES = 262144

# Minimum majors for the GitHub-maintained actions (Node.js 24 runners).
ACTION_MINIMUMS = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
    "actions/setup-node": 5,
    "actions/upload-artifact": 6,
    "actions/download-artifact": 7,
}

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+[0-9A-Za-z.\-+]*")
_USES_RE = re.compile(r"^\s*-?\s*uses:\s*['\"]?(?P<action>[^@'\"\s]+)@(?P<ref>[^'\"\s#]+)")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


# --------------------------------------------------------------------------
# Small readers. Every one of them answers None on any failure, so a broken
# input degrades one field rather than the whole banner.
# --------------------------------------------------------------------------

def _read_json(path: str) -> Optional[Any]:
    # utf-8-sig: Windows PowerShell 5.1 writes a byte-order mark through
    # Set-Content -Encoding utf8, and a ledger with one is still a ledger.
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def home_dir() -> str:
    return os.path.expanduser("~")


def _run_version(executable: str) -> Optional[str]:
    """The first version-shaped token of ``<executable> --version``."""
    path = shutil.which(executable)
    if not path:
        return None
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    output = (result.stdout or "") + " " + (result.stderr or "")
    match = _VERSION_RE.search(output)
    return match.group(0) if match else "unknown"


def _parse_version(text: Optional[str]) -> Optional[tuple]:
    if not text:
        return None
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


# --------------------------------------------------------------------------
# Field collectors.
# --------------------------------------------------------------------------

def os_name() -> str:
    return sys.platform


def version_cache() -> dict:
    data = _read_json(os.path.join(home_dir(), ".claude", "hooks", "version-cache.json"))
    return data if isinstance(data, dict) else {}


def _settings_env(path: str) -> dict:
    data = _read_json(path)
    env = data.get("env") if isinstance(data, dict) else None
    return env if isinstance(env, dict) else {}


def _is_off(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def claude_fields(root: str, environ: Optional[dict] = None) -> dict:
    """Version, latest, auto-update state, and effort for the Claude row."""
    env = os.environ if environ is None else environ
    user_settings = os.path.join(home_dir(), ".claude", "settings.json")
    settings_env = _settings_env(user_settings)
    version = _run_version("claude")
    latest = version_cache().get("claude_latest") or ""

    disable = env.get("DISABLE_AUTOUPDATER", settings_env.get("DISABLE_AUTOUPDATER", ""))
    auto_update = not _is_off(disable)
    claude_json = _read_json(os.path.join(home_dir(), ".claude.json"))
    if isinstance(claude_json, dict) and claude_json.get("autoUpdates") is False:
        auto_update = False

    effort = env.get("CLAUDE_CODE_EFFORT_LEVEL") or settings_env.get("CLAUDE_CODE_EFFORT_LEVEL") or ""
    effort_source = "env" if effort else ""
    if not effort:
        for candidate in (
            os.path.join(root, ".claude", "settings.local.json"),
            os.path.join(root, ".claude", "settings.json"),
            user_settings,
        ):
            data = _read_json(candidate)
            if isinstance(data, dict) and data.get("effortLevel"):
                effort = str(data["effortLevel"])
                effort_source = "settings"
                break
    if not effort:
        effort = "default"
    return {
        "version": version,
        "latest": str(latest),
        "auto_update": auto_update,
        "effort": effort,
        "effort_source": effort_source,
    }


def _parse_toml_minimal(text: str) -> dict:
    """Enough TOML for ``config.toml``: top-level and ``[table]`` scalars.

    Python 3.11 ships ``tomllib``; consumers on 3.9 and 3.10 do not, and the
    banner reads five scalar keys, so a line parser covers them. Strings,
    booleans and integers are decoded; anything else keeps its raw text.
    """
    data: dict = {}
    table = data
    for raw_line in text.splitlines():
        line = _strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line.strip("[]").strip()
            table = {}
            data[name] = table
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().strip('"')
        value = value.strip()
        if value.startswith('"') or value.startswith("'"):
            quote = value[0]
            end = value.find(quote, 1)
            table[key] = value[1:end] if end > 0 else value.strip(quote)
        elif value in ("true", "false"):
            table[key] = value == "true"
        else:
            token = value.replace("_", "")
            try:
                table[key] = int(token)
            except ValueError:
                table[key] = value
    return data


def _strip_toml_comment(line: str) -> str:
    """The line without a ``#`` comment that sits outside quotes."""
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == "#":
            return line[:index]
    return line


class TomlError(Exception):
    """``config.toml`` exists but is not valid TOML."""


def read_toml(path: str) -> Optional[dict]:
    """The parsed file, ``None`` when absent, ``TomlError`` when invalid.

    The minimal parser stands in only when ``tomllib`` cannot be imported
    (Python 3.9 and 3.10). A file that ``tomllib`` rejects is invalid, and
    the line parser would repair it silently (a duplicate key reads as the
    last assignment), which is a clean banner for a broken configuration.
    """
    text = _read_text(path)
    if text is None:
        return None
    try:
        import tomllib  # type: ignore
    except ImportError:
        return _parse_toml_minimal(text)
    try:
        return tomllib.loads(text)
    except Exception as exc:
        raise TomlError(_one_line(exc))


def codex_fields() -> dict:
    """Version, latest, and the config keys the Codex row and its checks use."""
    version = _run_version("codex")
    latest = version_cache().get("codex_latest") or ""
    config_error = ""
    try:
        config = read_toml(os.path.join(home_dir(), ".codex", "config.toml"))
    except TomlError as exc:
        config = None
        config_error = str(exc)
    fields = {
        "installed": version is not None,
        "version": version,
        "latest": str(latest),
        "configured": isinstance(config, dict),
        "config_error": config_error,
        "model": "",
        "reasoning": "",
        "tier": "",
        "fast_mode": None,
        "project_doc_max_bytes": None,
    }
    if isinstance(config, dict):
        fields["model"] = str(config.get("model") or "")
        fields["reasoning"] = str(config.get("model_reasoning_effort") or "")
        fields["tier"] = str(config.get("service_tier") or "")
        features = config.get("features")
        if isinstance(features, dict) and "fast_mode" in features:
            fields["fast_mode"] = bool(features.get("fast_mode"))
        budget = config.get("project_doc_max_bytes")
        fields["project_doc_max_bytes"] = budget if isinstance(budget, int) and not isinstance(budget, bool) else None
    return fields


def _skill_names(directory: str) -> list:
    names = []
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return names
    for entry in entries:
        if os.path.isfile(os.path.join(directory, entry, "SKILL.md")):
            names.append(entry)
    return names


def skill_buckets(root: str) -> dict:
    """The three buckets with the lookup-order shadowing applied."""
    local = _skill_names(os.path.join(root, "skills"))
    pack = [n for n in _skill_names(os.path.join(root, ".claude", "skills")) if n not in local]
    shadowed = set(local) | set(pack)
    shared = [n for n in _skill_names(os.path.join(root, ".agent-config", "repo", "skills")) if n not in shadowed]
    return {"local": local, "pack": pack, "shared": shared}


def hook_state() -> dict:
    hooks = os.path.join(home_dir(), ".claude", "hooks")
    return {
        "guard": os.path.isfile(os.path.join(hooks, "guard.py")),
        "session_bootstrap": os.path.isfile(os.path.join(hooks, "session_bootstrap.py")),
    }


def workflow_findings(root: str) -> list:
    """Action pins below the Node.js 24 minimums, and SHA pins for review."""
    findings = []
    pattern = os.path.join(root, ".github", "workflows", "*")
    for path in sorted(glob.glob(pattern)):
        if not path.endswith((".yml", ".yaml")):
            continue
        text = _read_text(path)
        if text is None:
            continue
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        for number, line in enumerate(text.splitlines(), start=1):
            match = _USES_RE.match(line)
            if not match:
                continue
            action = match.group("action")
            ref = match.group("ref")
            minimum = ACTION_MINIMUMS.get(action)
            if minimum is None:
                continue
            if _SHA_RE.match(ref):
                findings.append("%s@%s in %s:%d pinned by SHA, review manually" % (action, ref[:7], rel, number))
                continue
            major_match = re.match(r"v?(\d+)", ref)
            if not major_match:
                continue
            if int(major_match.group(1)) < minimum:
                findings.append("%s@%s in %s:%d, bump to v%d" % (action, ref, rel, number, minimum))
    return findings


def read_ledger(root: str) -> Optional[dict]:
    data = _read_json(os.path.join(root, ".agent-config", "last-run.json"))
    return data if isinstance(data, dict) else None


def read_event_ts(root: str) -> Optional[float]:
    data = _read_json(os.path.join(root, ".agent-config", "session-event.json"))
    if isinstance(data, dict) and isinstance(data.get("ts"), (int, float)):
        try:
            return float(data["ts"])
        except OverflowError:
            # A JSON integer has no width limit, a float does. Treat one that
            # cannot convert as no timestamp, which is what every other
            # unusable value here reads as, rather than ending the render and
            # leaving no report at all.
            return None
    return None


_AGENT_STYLE_BLOCK_RE = re.compile(r"<!--\s*rule-pack:agent-style:begin\s+version=(?P<ref>[^\s>]+)")
# One pack entry: its indented lines up to the next "- name:" item.
_PACK_ENTRY_RE = re.compile(
    r"-[ \t]*name:[ \t]*['\"]?agent-style['\"]?[ \t]*\n"
    r"(?P<body>(?:(?![ \t]*-[ \t]*name:)[ \t]+.*\n|[ \t]*\n)*)"
)
_PACK_REF_RE = re.compile(r"^\s*ref:\s*['\"]?(?P<ref>[^'\"\s#]+)", re.MULTILINE)
# A stable release ref, whole. A branch, a prerelease such as v0.8.0rc1, or a
# suffixed ref such as v0.7.0-dev has no package version to pin.
_RELEASE_REF_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_HOOK_OFF_VALUES = ("off", "0", "disabled", "false", "no")


def agent_style_installed() -> Optional[str]:
    """The agent_style release this interpreter imports, or None when it has none.

    In a Claude Code consumer session the SessionStart hook renders the banner
    with the interpreter that the guard also resolves through
    ~/.claude/hooks/_python, so there this is the package the guard's advisory
    imports. A render from a source repository, a standalone bootstrap, or a
    Codex session reads the interpreter that rendered it. A package without a
    version reads as "unknown", which the check treats as older, as the guard
    does.
    """
    try:
        import importlib
        module = importlib.import_module("agent_style")
    except Exception:
        return None
    version = getattr(module, "__version__", None)
    return str(version) if version else "unknown"


def _bundled_agent_style_ref(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    entry = _PACK_ENTRY_RE.search(text)
    if not entry:
        return None
    ref = _PACK_REF_RE.search(entry.group("body"))
    return ref.group("ref") if ref else None


def agent_style_expected(root: str, consumer: bool) -> Optional[str]:
    """The agent-style release whose rules this repository loads.

    A consumer reads the version its composed AGENTS.md block records, so a
    repository without the pack expects nothing. A source repository has no
    composed block and reads the bundled default from bootstrap/packs.yaml.
    """
    if consumer:
        match = _AGENT_STYLE_BLOCK_RE.search(_read_text(os.path.join(root, "AGENTS.md")) or "")
        return match.group("ref") if match else None
    return _bundled_agent_style_ref(_read_text(os.path.join(root, "bootstrap", "packs.yaml")))


def _switch_off(name: str, env: Any, settings_env: dict) -> bool:
    value = env.get(name)
    if value is None:
        value = settings_env.get(name, "")
    return str(value).strip().lower() in _HOOK_OFF_VALUES


def python_command(python: str, platform: Optional[str] = None) -> str:
    """The interpreter as a command a shell runs. A path with a space is
    quoted, and on Windows it also takes PowerShell's call operator, without
    which a quoted path is printed as a string rather than run."""
    if " " not in python:
        return python
    quoted = '"%s"' % python
    return "& " + quoted if (platform or sys.platform) == "win32" else quoted


def agent_style_fields(root: str, consumer: bool, environ: Optional[dict] = None) -> dict:
    env = os.environ if environ is None else environ
    settings_env = _settings_env(os.path.join(home_dir(), ".claude", "settings.json"))
    python = sys.executable or "python"
    return {
        "installed": agent_style_installed(),
        "expected": agent_style_expected(root, consumer),
        "python": python,
        "command": python_command(python),
        # The guard turns its advisory off under either switch: the per-gate
        # AGENT_STYLE_HOOK or the legacy AGENT_CONFIG_GATES blanket.
        "disabled": _switch_off("AGENT_STYLE_HOOK", env, settings_env)
        or _switch_off("AGENT_CONFIG_GATES", env, settings_env),
    }


def pack_fields(root: str) -> dict:
    """Gap and update counts through the vendored helper beside this file."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import pack_identity  # type: ignore
    except Exception as exc:  # the helper is vendored beside this script
        return {"gap_count": None, "update_count": None, "unavailable": "pack_identity.py missing: %s" % exc}
    try:
        return pack_identity.pack_checks(root)
    except Exception as exc:  # a defect in the helper must not blank the banner
        return {"gap_count": None, "update_count": None, "unavailable": "pack check failed: %s" % exc}


# --------------------------------------------------------------------------
# Checks and rendering.
# --------------------------------------------------------------------------

def _codex_model_generation(model: str) -> Optional[tuple]:
    match = re.match(r"gpt-(\d+)(?:\.(\d+))?", model or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2) or 0))


def codex_issues(codex: dict) -> list:
    """Actionable Codex drift: an old CLI under a newer model generation, a
    model older than the GPT-5.6 family, or a byte budget that still drops
    most of a composed AGENTS.md."""
    issues = []
    if codex.get("config_error"):
        issues.append("Codex config.toml is not valid TOML (%s)" % codex["config_error"])
        return issues
    if not codex.get("installed") or not codex.get("configured"):
        return issues
    generation = _codex_model_generation(codex.get("model", ""))
    version = _parse_version(codex.get("version"))
    if codex.get("model"):
        if generation is None or generation < (5, 6):
            issues.append("Codex model %s is older than the GPT-5.6 family" % codex["model"])
        elif version is not None:
            if generation >= (6, 0) and version < (0, 150, 0):
                issues.append("Codex %s is below the GPT-6 CLI floor; update the CLI" % codex["version"])
            elif generation < (6, 0) and version < (0, 144, 0):
                issues.append("Codex %s is below the GPT-5.6 CLI floor (0.144.0); update the CLI" % codex["version"])
    budget = codex.get("project_doc_max_bytes")
    if budget is None or budget < PROJECT_DOC_MIN_BYTES:
        issues.append(
            "Codex config.toml: set project_doc_max_bytes = %d (the default injects 32 KiB of AGENTS.md)"
            % PROJECT_DOC_MIN_BYTES
        )
    return issues


def agent_style_issues(state: Optional[dict]) -> list:
    """A guard advisory that cannot run the rules this repository loads.

    The guard imports agent_style from the hook interpreter and gates its
    rule set on that release, so a missing or older package narrows the
    advisory without any error. A ref that is not a stable release, such as
    main or v0.8.0rc1, has no version to pin and is not reported.
    """
    if not state or state.get("disabled"):
        return []
    expected = state.get("expected")
    match = _RELEASE_REF_RE.match(str(expected).strip()) if expected else None
    if match is None:
        return []
    want = tuple(int(part) for part in match.groups())
    python = state.get("python") or "python"
    command = state.get("command") or python_command(python)
    pin = "%d.%d.%d" % want
    installed = state.get("installed")
    if installed is None:
        return ["agent-style advisory off: %s has no agent_style (run `%s -m pip install agent-style==%s`)"
                % (python, command, pin)]
    have = _parse_version(installed)
    if have is None or have < want:
        return ["agent-style %s in %s is older than the %s rules (run `%s -m pip install -U agent-style==%s`)"
                % (installed, python, expected, command, pin)]
    return []


def bootstrap_issues(ledger: Optional[dict], bootstrap_rc: Optional[int]) -> list:
    """What an incomplete refresh looks like in the check line."""
    issues = []
    if ledger is None:
        issues.append("checks unavailable (no .agent-config/last-run.json)")
        if bootstrap_rc not in (None, 0):
            issues.append("bootstrap exited %d" % bootstrap_rc)
        return issues
    phase = str(ledger.get("last_phase") or "unknown")
    if bootstrap_rc not in (None, 0):
        issues.append("bootstrap exited %d at %s" % (bootstrap_rc, phase))
    elif ledger.get("completed") is not True:
        issues.append("bootstrap incomplete: stopped at %s" % phase)
    steps = ledger.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            status = str(step.get("status") or "")
            if status in ("failed", "skipped"):
                note = "%s %s" % (step.get("phase", "?"), status)
                if step.get("reason"):
                    note += " (%s)" % step["reason"]
                if status == "failed" and step.get("rc") not in (None, 0):
                    note += " rc=%s" % step["rc"]
                issues.append(note)
    return issues


def collect(root: str, event_ts: Optional[float] = None, bootstrap_rc: Optional[int] = None,
            consumer: bool = True, environ: Optional[dict] = None) -> dict:
    """Every field the banner needs, computed once."""
    fields: dict[str, Any] = {
        "os": os_name(),
        "claude": claude_fields(root, environ),
        "codex": codex_fields(),
        "skills": skill_buckets(root),
        "hooks": hook_state(),
        "agent_style": agent_style_fields(root, consumer, environ),
        "workflows": workflow_findings(root),
        "ledger": read_ledger(root) if consumer else None,
        "event_ts": event_ts if event_ts is not None else (read_event_ts(root) if consumer else None),
        "bootstrap_rc": bootstrap_rc,
        "packs": pack_fields(root) if consumer else {"gap_count": 0, "update_count": 0, "unavailable": None},
        "consumer": consumer,
    }
    return fields


def session_check(fields: dict) -> str:
    issues = []
    if fields.get("consumer"):
        issues.extend(bootstrap_issues(fields.get("ledger"), fields.get("bootstrap_rc")))
    hooks = fields["hooks"]
    if not hooks["guard"]:
        issues.append("guard.py missing from ~/.claude/hooks (run bootstrap)")
    if not hooks["session_bootstrap"]:
        issues.append("session_bootstrap.py missing from ~/.claude/hooks (run bootstrap)")
    if hooks["guard"]:
        issues.extend(agent_style_issues(fields.get("agent_style")))
    claude = fields["claude"]
    if claude["effort"] not in ("xhigh", "max"):
        issues.append("Claude effort is %s; set CLAUDE_CODE_EFFORT_LEVEL=xhigh in ~/.claude/settings.json env" % claude["effort"])
    issues.extend(codex_issues(fields["codex"]))
    issues.extend(fields["workflows"])
    packs = fields["packs"]
    if packs.get("unavailable"):
        issues.append("pack checks unavailable (%s)" % _one_line(packs["unavailable"]))
    if packs.get("gap_count"):
        issues.append("⚠ %d user-level pack(s) not deployed (run `anywhere-agents pack verify --fix`)" % packs["gap_count"])
    if packs.get("update_count"):
        issues.append("ℹ %d pack update(s) available (run `anywhere-agents pack verify --fix`)" % packs["update_count"])
    return "all clear" if not issues else "; ".join(issues)


def _one_line(text: Any) -> str:
    """Whitespace-normalized diagnostic text. A YAML parser's message spans
    several lines, and a banner field is one line by contract."""
    return " ".join(str(text).split())


def _with_latest(version: Optional[str], latest: str) -> str:
    """``current → latest`` only when the cache knows a newer release. A
    native install can run ahead of the npm registry the cache reads, and an
    arrow pointing backwards would read as a downgrade request."""
    if not version:
        return "unknown"
    current = _parse_version(version)
    newest = _parse_version(latest)
    if current and newest and newest > current:
        return "%s → %s" % (version, latest)
    return version


def render_lines(fields: dict) -> list:
    claude = fields["claude"]
    if claude["version"] is None:
        claude_row = "Claude Code: not installed"
    else:
        claude_row = "Claude Code: %s (auto-update: %s) · <model> · effort=%s" % (
            _with_latest(claude["version"], claude["latest"]),
            "on" if claude["auto_update"] else "off",
            claude["effort"],
        )
    codex = fields["codex"]
    if not codex["installed"]:
        codex_row = "Codex: not installed"
    elif codex.get("config_error"):
        codex_row = "Codex: %s · config.toml unreadable" % _with_latest(codex["version"], codex["latest"])
    elif not codex["configured"]:
        codex_row = "Codex: %s · not configured" % _with_latest(codex["version"], codex["latest"])
    else:
        fast = codex["fast_mode"]
        codex_row = "Codex: %s · %s · %s · %s · fast_mode=%s" % (
            _with_latest(codex["version"], codex["latest"]),
            codex["model"] or "no model key",
            codex["reasoning"] or "default effort",
            codex["tier"] or "default tier",
            "false" if fast is None else str(fast).lower(),
        )
    skills = fields["skills"]
    parts = []
    for label in ("local", "pack", "shared"):
        names = skills[label]
        if names:
            parts.append("%d %s (%s)" % (len(names), label, ", ".join(names)))
    skills_row = "Skills: " + (" + ".join(parts) if parts else "none")
    hooks = fields["hooks"]
    hook_parts = []
    style = fields.get("agent_style")
    if not hooks["guard"]:
        hook_parts.append("PreToolUse guard.py missing")
    elif not style:
        hook_parts.append("PreToolUse guard.py")
    elif style.get("disabled"):
        hook_parts.append("PreToolUse guard.py (agent-style advisory off)")
    elif style.get("installed") is None:
        hook_parts.append("PreToolUse guard.py (no agent-style)")
    else:
        hook_parts.append("PreToolUse guard.py (agent-style %s)" % style["installed"])
    hook_parts.append(
        "SessionStart session_bootstrap.py" if hooks["session_bootstrap"] else "SessionStart session_bootstrap.py missing"
    )
    return [
        TITLE,
        "   ├── OS: %s" % fields["os"],
        "   ├── %s" % claude_row,
        "   ├── %s" % codex_row,
        "   ├── %s" % skills_row,
        "   ├── Hooks: %s" % ", ".join(hook_parts),
        "   └── Session check: %s" % session_check(fields),
    ]


# --------------------------------------------------------------------------
# Report metadata: the one line an agent reads to decide freshness.
# --------------------------------------------------------------------------

def metadata_line(event_ts: Optional[float], run_id: Optional[str], completed: Optional[bool]) -> str:
    rendered_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "%s event_ts=%s run_id=%s completed=%s rendered_at=%s -->" % (
        METADATA_PREFIX,
        "none" if event_ts is None else repr(float(event_ts)),
        run_id if run_id else "none",
        "unknown" if completed is None else str(bool(completed)).lower(),
        rendered_at,
    )


def parse_metadata(text: str) -> Optional[dict]:
    """The metadata of a report, or ``None`` when the first line is not one."""
    first = (text or "").lstrip("﻿").split("\n", 1)[0].strip()
    if not first.startswith(METADATA_PREFIX) or not first.endswith("-->"):
        return None
    body = first[len(METADATA_PREFIX):-3].strip()
    fields: dict[str, Any] = {}
    for token in body.split():
        key, sep, value = token.partition("=")
        if sep:
            fields[key] = value
    if "run_id" not in fields or "event_ts" not in fields:
        return None
    event_ts: Optional[float]
    if fields["event_ts"] == "none":
        event_ts = None
    else:
        try:
            event_ts = float(fields["event_ts"])
        except ValueError:
            return None
    completed = {"true": True, "false": False}.get(fields.get("completed", ""), None)
    return {
        "event_ts": event_ts,
        "run_id": None if fields["run_id"] == "none" else fields["run_id"],
        "completed": completed,
        "rendered_at": fields.get("rendered_at"),
    }


def report_is_current(text: str, event_ts: Optional[float], run_id: Optional[str]) -> bool:
    """The acceptance rule from AGENTS.md: the report's metadata records the
    pending event's timestamp (when there is one) and the current ledger's
    ``run_id``. Anything else selects the fallback."""
    meta = parse_metadata(text)
    if meta is None:
        return False
    if not run_id or meta["run_id"] != run_id:
        return False
    if event_ts is not None:
        if meta["event_ts"] is None or abs(meta["event_ts"] - float(event_ts)) > 1e-6:
            return False
    return True


def banner_body(text: str) -> list:
    """The banner lines of a report, without the metadata comment."""
    lines = (text or "").splitlines()
    if lines and lines[0].startswith(METADATA_PREFIX):
        lines = lines[1:]
    return [line for line in lines if line.strip()]


def write_report(root: str, lines: list) -> str:
    """Publish ``.agent-config/banner.txt`` atomically; returns the path."""
    directory = os.path.join(root, ".agent-config")
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, REPORT_NAME)
    tmp = "%s.tmp-%d" % (target, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(tmp, target)
    return target


# --------------------------------------------------------------------------
# Modes and entry point.
# --------------------------------------------------------------------------

def is_consumer_root(root: str) -> bool:
    """A root that bootstrap serves: it carries an entry point, or the ledger
    a run has written. The ledger counts because both entry points render
    before the self-update step that installs their own copy, and a wheel
    or fixture run may never install one."""
    return any(
        os.path.isfile(os.path.join(root, ".agent-config", marker))
        for marker in ("bootstrap.sh", "bootstrap.ps1", "last-run.json")
    )


def is_source_root(root: str) -> bool:
    return (
        os.path.isfile(os.path.join(root, "bootstrap", "bootstrap.sh"))
        and os.path.isfile(os.path.join(root, "bootstrap", "bootstrap.ps1"))
        and os.path.isfile(os.path.join(root, "scripts", "generate_agent_configs.py"))
        and os.path.isdir(os.path.join(root, "skills"))
    )


def render_consumer(root: str, event_ts: Optional[float], bootstrap_rc: Optional[int]) -> tuple:
    """``(report_lines, banner_lines)`` for a consumer root."""
    fields = collect(root, event_ts=event_ts, bootstrap_rc=bootstrap_rc, consumer=True)
    lines = render_lines(fields)
    ledger = fields["ledger"] or {}
    completed: Optional[bool]
    if bootstrap_rc not in (None, 0):
        completed = False
    elif fields["ledger"] is None:
        completed = None
    else:
        completed = ledger.get("completed") is True
    run_id = ledger.get("run_id") if isinstance(ledger.get("run_id"), str) else None
    meta = metadata_line(fields["event_ts"], run_id, completed)
    return [meta] + lines, lines


def render_source(root: str) -> list:
    fields = collect(root, consumer=False)
    return render_lines(fields)


def _utf8_stdout() -> None:
    """Print the banner as UTF-8 regardless of the console code page.

    The tree glyphs and the package emoji are outside cp1252, and a hook
    launched from a Windows console without PYTHONUTF8 would otherwise fail
    on the first character. Consumers of the output (the hook, Claude Code)
    read UTF-8; a console that cannot show a glyph loses only that glyph.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


def main(argv: Optional[list] = None) -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(description="Render the anywhere-agents session banner.")
    parser.add_argument("--root", required=True, help="consumer or source repository root")
    parser.add_argument("--event-ts", type=float, default=None,
                        help="session-event timestamp to record; read from session-event.json when omitted")
    parser.add_argument("--bootstrap-rc", type=int, default=None,
                        help="exit status of the bootstrap attempt that preceded this render")
    parser.add_argument("--stdout", action="store_true",
                        help="in a consumer, also print the banner lines (never the metadata) to stdout")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    if is_consumer_root(root):
        report, lines = render_consumer(root, args.event_ts, args.bootstrap_rc)
        try:
            write_report(root, report)
        except OSError as exc:
            print("render_banner: could not publish %s: %s" % (REPORT_NAME, exc), file=sys.stderr)
            return 1
        if args.stdout:
            print("\n".join(lines))
        return 0
    if is_source_root(root):
        print("\n".join(render_source(root)))
        return 0
    print("render_banner: %s is neither a consumer root (.agent-config/bootstrap.*) nor a source root" % root,
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
