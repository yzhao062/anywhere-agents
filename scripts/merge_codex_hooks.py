"""Register guard.py as a Codex PreToolUse hook in ~/.codex/hooks.json.

bootstrap.sh calls this after it deploys ~/.claude/hooks/guard.py, so Codex
runs the same gates Claude Code does (see the Codex block in guard.py). It
adds one group and leaves every other hook in the file alone. A machine with
no Codex home is skipped, since creating one would be installing Codex config
for a tool the user never set up.

Codex runs a hook only after the user trusts it, and the trust hash covers
the hook's definition but not the script it points at. The definition here is
fixed, so trusting it once survives every later guard.py update. Bootstrap
cannot trust it on the user's behalf: until they choose Trust at Codex's
startup review, this prints a reminder on each run, because an untrusted hook
is skipped without any message from Codex.

The file is rewritten only when the group changes, and by rename, so the
concurrent bootstraps of a session-start burst either find it current or
publish identical bytes.

Usage:
    merge_codex_hooks.py [CODEX_HOME]

CODEX_HOME defaults to $CODEX_HOME, then ~/.codex. Exit 0 always: a Codex hook
that cannot be registered must not fail the bootstrap run, so a problem is a
warning on stderr.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile

COMMAND = '"$HOME/.claude/hooks/_python" "$HOME/.claude/hooks/guard.py" --agent codex'
GROUP = {
    "matcher": "Bash|apply_patch",
    "hooks": [{"type": "command", "command": COMMAND, "timeout": 30}],
}
_MARK = 'guard.py" --agent codex'


def _ours(group):
    hooks = group.get("hooks") if isinstance(group, dict) else None
    return isinstance(hooks, list) and any(
        isinstance(h, dict) and _MARK in str(h.get("command", "")) for h in hooks
    )


def merge(codex_home):
    """Return (index of our group, whether the file changed), or None."""
    path = os.path.join(codex_home, "hooks.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                data = json.loads(f.read().decode("utf-8-sig"))
        except (OSError, ValueError) as exc:
            print(f"warning: left {path} alone, it could not be read: {exc}", file=sys.stderr)
            return None
    hooks = data.setdefault("hooks", {}) if isinstance(data, dict) else None
    groups = hooks.setdefault("PreToolUse", []) if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        print(f"warning: left {path} alone, its PreToolUse is not a list", file=sys.stderr)
        return None
    index = next((i for i, g in enumerate(groups) if _ours(g)), None)
    if index is not None:
        existing = groups[index]
        if existing == GROUP:
            return index, False
        # A group the user shares with other handlers, or extends with keys
        # of their own, is theirs: replacing it would drop those handlers.
        if len(existing.get("hooks", [])) != 1 or set(existing) != set(GROUP):
            print(f"warning: left {path} alone; guard.py shares a hook group "
                  "with other settings, so give it a group of its own",
                  file=sys.stderr)
            return None
        groups[index] = GROUP
    else:
        groups.append(GROUP)
        index = len(groups) - 1
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=codex_home, prefix=".hooks.json.", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write((json.dumps(data, indent=2) + "\n").encode("utf-8"))
        os.replace(tmp, path)
    except OSError as exc:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
        print(f"warning: could not write {path}: {exc}", file=sys.stderr)
        return None
    return index, True


def trust_hash(group):
    """The hash Codex records when the user trusts `group`'s one handler.

    Codex hashes a normalized identity rather than the file's text: the event,
    the matcher, and the handler with its timeout filled in, serialized as
    compact JSON with sorted keys (hooks/src/engine/discovery.rs and
    config/src/fingerprint.rs at rust-v0.163.0). Computed this way, the hash
    matched each of the four hooks a maintainer machine had trusted. Should a
    later Codex change the recipe, the reminder below keeps printing, which
    errs toward asking.
    """
    handler = group["hooks"][0]
    identity = {
        "event_name": "pre_tool_use",
        "matcher": group["matcher"],
        "hooks": [{
            "type": "command", "command": handler["command"],
            "async": False, "timeout": handler["timeout"],
        }],
    }
    body = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def trust_state(codex_home, index):
    """Return "trusted", "disabled", or "untrusted" for our group.

    Read from the `[hooks.state."<hooks.json>:pre_tool_use:<group>:0"]` table
    Codex writes to config.toml. A record whose hash differs from the current
    definition is untrusted, as Codex treats it; a record the user disabled is
    left alone.
    """
    header = f'[hooks.state."{os.path.join(codex_home, "hooks.json")}:pre_tool_use:{index}:0"]'
    try:
        with open(os.path.join(codex_home, "config.toml"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, ValueError):
        return "untrusted"
    fields = {}
    if header in (line.strip() for line in lines):
        start = [line.strip() for line in lines].index(header) + 1
        for line in lines[start:]:
            if line.lstrip().startswith("["):
                break
            match = re.match(r'\s*(\w+)\s*=\s*"?([^"]*)"?\s*$', line)
            if match:
                fields[match.group(1)] = match.group(2)
    if fields.get("enabled") == "false":
        return "disabled"
    if fields.get("trusted_hash") == trust_hash(GROUP):
        return "trusted"
    return "untrusted"


def main(argv):
    if len(argv) > 1:
        print("usage: merge_codex_hooks.py [CODEX_HOME]", file=sys.stderr)
        return 0
    codex_home = os.path.abspath(os.path.expanduser(
        argv[0] if argv else os.environ.get("CODEX_HOME") or "~/.codex"))
    if not os.path.isdir(codex_home):
        return 0
    result = merge(codex_home)
    if result is None:
        return 0
    index, changed = result
    if changed:
        print(f"codex: registered guard.py as a PreToolUse hook in {codex_home}/hooks.json")
    if trust_state(codex_home, index) == "untrusted":
        print("codex: start codex once and choose Trust for the guard.py hook; "
              "Codex skips an untrusted hook without saying so")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
