#!/usr/bin/env python3
"""SessionStart hook: run .agent-config bootstrap for the enclosing consumer repo.

Deployed to ~/.claude/hooks/session_bootstrap.py by bootstrap.sh / .ps1,
and wired into ~/.claude/settings.json under hooks.SessionStart.

When Claude Code opens a session, this hook runs before the agent sees any
user prompt. It walks up from the current working directory to find a
consumer repo (a directory containing .agent-config/bootstrap.sh or
.agent-config/bootstrap.ps1) — so a launch from a nested subdirectory still
resolves to the project root. If found, it writes a per-project SessionStart
event marker and runs the platform-specific bootstrap script from that root.
Otherwise (unrelated directory) it exits silently.

Also handles the source repos (agent-config / anywhere-agents) launched from
their own root: detected via a cwd-only check for bootstrap/ + skills/.
Source repos get legacy-flag cleanup but no per-project event (no
.agent-config/ to write to).

Claude Code's SessionStart hook behavior: stdout from the hook is added as
context to the session. To avoid flooding Claude with git-pull noise or
generator messages on every session start/resume/clear, this script captures
the subprocess output and emits a single concise summary line on success.
Errors go to stderr with the last ~2KB of child output for debugging.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time


VERSION_CACHE_TTL_SECONDS = 86400  # 24 hours


def _find_consumer_root(start=None):
    """Walk up from `start` (default os.getcwd()) looking for a directory that
    contains .agent-config/bootstrap.sh or .agent-config/bootstrap.ps1.
    Returns the absolute consumer-repo root, or None if not inside one.

    Mirrors the helper in guard.py. Duplicated because both scripts deploy to
    ~/.claude/hooks/ as standalone files (no shared module at that location).
    """
    cwd = os.path.abspath(start or os.getcwd())
    prev = None
    while cwd and cwd != prev:
        for marker in ("bootstrap.sh", "bootstrap.ps1"):
            if os.path.isfile(os.path.join(cwd, ".agent-config", marker)):
                return cwd
        prev = cwd
        cwd = os.path.dirname(cwd)
    return None


def _cleanup_legacy_flag_files() -> None:
    """One-time cleanup of 0.1.8 global flag files under ~/.claude/hooks/.
    Harmless after migration (files are gone and FileNotFoundError is swallowed).
    Runs every SessionStart; cost is a couple of os.remove attempts.
    """
    home = os.path.expanduser("~")
    for name in ("session-event.json", "banner-emitted.json"):
        legacy = os.path.join(home, ".claude", "hooks", name)
        try:
            os.remove(legacy)
        except FileNotFoundError:
            pass
        except Exception:
            pass


def write_session_event(consumer_root: str) -> None:
    """Write <consumer_root>/.agent-config/session-event.json on every
    SessionStart fire so the agent can detect resume / clear / compact events
    (not just fresh startup) and re-emit the session banner when appropriate.
    The banner rule in AGENTS.md compares this timestamp to
    <consumer_root>/.agent-config/banner-emitted.json and re-emits when the
    event is newer than the last emission. Per-project scope prevents
    cross-session interference when multiple Claude Code windows run at once.
    """
    path = os.path.join(consumer_root, ".agent-config", "session-event.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time()}, f)
    except Exception:
        pass


def update_version_cache() -> None:
    """Refresh ~/.claude/hooks/version-cache.json with the latest Claude Code and
    Codex versions from the npm registry. Used by the session-start banner to
    show current vs latest. 24-hour TTL keeps the common path to a file read.
    Silent on any failure — the banner tolerates a missing cache by omitting
    the "→ latest" half instead of blocking.
    """
    cache_path = os.path.join(
        os.path.expanduser("~"), ".claude", "hooks", "version-cache.json"
    )
    cache: dict = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    now = time.time()
    if cache.get("checked_at", 0) + VERSION_CACHE_TTL_SECONDS > now:
        return  # still fresh

    new_cache: dict = {
        "checked_at": now,
        "claude_latest": cache.get("claude_latest", ""),
        "codex_latest": cache.get("codex_latest", ""),
    }

    import urllib.request

    for key, url in (
        (
            "claude_latest",
            "https://registry.npmjs.org/@anthropic-ai%2Fclaude-code/latest",
        ),
        ("codex_latest", "https://registry.npmjs.org/@openai%2Fcodex/latest"),
    ):
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
                v = data.get("version", "")
                if v:
                    new_cache[key] = v
        except Exception:
            pass  # preserve previous value

    # Only persist the cache (advancing checked_at) if at least one version is
    # known. First-ever run where both fetches fail leaves the cache absent so
    # the next session retries instead of waiting out the 24h TTL with empty
    # values.
    if new_cache.get("claude_latest") or new_cache.get("codex_latest"):
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(new_cache, f)
        except Exception:
            pass


def main() -> int:
    cwd = os.getcwd()

    # Walk up from cwd to find the consumer-repo root. A launch from a deep
    # subdirectory (e.g. <project>/src/nested) should still resolve to the
    # project root that owns .agent-config/ — guard.py does the same walk,
    # so the two hooks agree on where the per-project flag files live.
    consumer_root = _find_consumer_root(cwd)

    # Source-repo detection is intentionally cwd-only: maintainers launch the
    # source repos (agent-config / anywhere-agents) from their root. A source
    # repo subdir launch is a known narrow gap (only affects maintainer
    # workflow, not user consumers).
    has_source_skills = (
        os.path.isdir(os.path.join(cwd, "reference-skills"))
        or os.path.isdir(os.path.join(cwd, "skills"))
    )
    is_source_repo = (
        os.path.isfile(os.path.join(cwd, "bootstrap", "bootstrap.sh"))
        and os.path.isfile(os.path.join(cwd, "bootstrap", "bootstrap.ps1"))
        and has_source_skills
    )

    is_consumer_repo = consumer_root is not None

    # Skip any state mutation in unrelated Claude Code sessions. Writing the
    # per-project session-event file unconditionally would be harmless (there
    # is no .agent-config/ to write to), but running legacy cleanup, version
    # cache refresh, or the bootstrap subprocess in unrelated sessions would
    # waste time and violate the "only participating sessions are affected"
    # contract.
    if not (is_source_repo or is_consumer_repo):
        return 0

    _cleanup_legacy_flag_files()

    if is_consumer_repo:
        # Mark the SessionStart event so the agent (and the banner gate in
        # guard.py) can tell a fresh event needs a new banner. Source repos
        # have no .agent-config/ to write to; they fall back to prompt-level
        # banner compliance per AGENTS.md Session Start Check.
        write_session_event(consumer_root)

        # Resolve the bootstrap subprocess command from the consumer root so
        # a nested-cwd launch still runs the correct .agent-config/bootstrap.*.
        if platform.system() == "Windows":
            script = os.path.join(consumer_root, ".agent-config", "bootstrap.ps1")
            if os.path.isfile(script):
                cmd: list[str] | None = [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    script,
                ]
            else:
                cmd = None
        else:
            script = os.path.join(consumer_root, ".agent-config", "bootstrap.sh")
            if os.path.isfile(script):
                cmd = ["bash", script]
            else:
                cmd = None
    else:
        cmd = None  # source repo: no bootstrap subprocess to run

    if cmd is None:
        return 0

    # Refresh the version cache only when this is a participating repo, so the
    # hook stays silent (and network-free) in unrelated Claude Code sessions.
    update_version_cache()

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print("anywhere-agents: bootstrap refreshed")
        return 0

    print(
        f"anywhere-agents: bootstrap failed (exit {result.returncode})",
        file=sys.stderr,
    )
    if result.stdout:
        print(result.stdout[-2000:], file=sys.stderr)
    if result.stderr:
        print(result.stderr[-2000:], file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
