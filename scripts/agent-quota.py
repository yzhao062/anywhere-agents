#!/usr/bin/env python3
"""Cross-agent quota: show Claude + Codex + Agy 5h / 7d remaining.

Runs in any terminal, in either agent's session. This is the symmetric,
agent-independent view of usage that the Claude statusLine already shows
inside Claude Code, made readable from a Codex session (or a plain shell)
too. See docs/followups/2026-05-16-agent-fungibility-refactor-plan.md
Phase 9.

Data sources:
  - Claude: ~/.claude/rate-limits-cache.json, written by statusline.py on
    each Claude Code statusLine render (override via CLAUDE_RL_CACHE).
  - Codex:  ~/.codex/sessions/**/rollout-*.jsonl, field
    payload.rate_limits. Each window carries window_minutes, so the label
    is derived from it (300 -> 5h, 10080 -> 7d): Codex dropped the fixed 5h
    window on 2026-07-12, so primary is now weekly and secondary may be null.
    Each snapshot also carries a limit_id. A session running a per-model
    bucket reports that bucket rather than the account plan, and such a
    bucket usually sits near 0% used, so rendering it verbatim reads as full
    quota while the plan is half spent. A recognized plan reading therefore
    always wins, and an unrecognized id never displaces one; a side meter is
    shown labeled only when the sampled rollouts hold no plan reading at
    all. Recency among those rollouts goes by the record timestamp, since
    NTFS defers the mtime update on a rollout whose session still holds it
    open. mtime still bounds which rollouts are opened, so the result is the
    freshest plan reading among those sampled rather than the newest on
    disk; the row's age bracket is what makes an old one visible.
  - Agy: ~/.claude/agy-quota-cache.json, populated from the Antigravity
    CLI's zero-turn `agy -p "/usage" --output-format json` metadata query.
    A lock and attempt timestamp permit at most one bounded background
    refresh per cache interval, including after failures.

Each side is only as fresh as that agent's last activity; the age is shown
in brackets so a stale snapshot is obvious.
"""
import json
import os
import shutil
import subprocess
import sys
import time

CLAUDE_PCT_FIELD = "used_percentage"
CODEX_PCT_FIELD = "used_percent"
# The account plan meter. Rollouts written before per-model buckets shipped
# carry no limit_id at all, so an absent id counts as the main meter.
CODEX_MAIN_LIMIT_IDS = (None, "", "codex")
# Recency bound: enough rollouts to see past a run of side-meter sessions,
# few enough that a readout stays a handful of tail reads.
MAX_ROLLOUT_SCAN = 12
MAX_RL_LINES_PER_FILE = 200
# Clock jitter tolerated when aging a record. Past it, a record stamped in
# the future is refused rather than clamped: see _codex_snapshot_age.
CODEX_FUTURE_TOLERANCE_SECONDS = 60
CLAUDE_RL_CACHE = os.environ.get("CLAUDE_RL_CACHE") or os.path.join(
    os.path.expanduser("~"), ".claude", "rate-limits-cache.json"
)
AGY_QUOTA_CACHE = os.environ.get("AGY_QUOTA_CACHE") or os.path.join(
    os.path.expanduser("~"), ".claude", "agy-quota-cache.json"
)
AGY_QUOTA_ATTEMPT = AGY_QUOTA_CACHE + ".last-attempt"
AGY_QUOTA_LOCK = AGY_QUOTA_CACHE + ".refresh.lock"


def _positive_env(name, default):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


AGY_QUOTA_TTL_SECONDS = _positive_env("AGY_QUOTA_TTL_SECONDS", 300)
AGY_QUOTA_TIMEOUT_SECONDS = _positive_env("AGY_QUOTA_TIMEOUT_SECONDS", 30)


def _path_age(path):
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def _agy_binary():
    configured = os.environ.get("ANTIGRAVITY_BIN") or "agy"
    direct = os.path.expandvars(os.path.expanduser(configured))
    if os.path.isfile(direct):
        return os.path.abspath(direct)
    found = shutil.which(configured)
    if found:
        return found
    if os.name == "nt" and configured.lower() in {"agy", "agy.exe"}:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            fallback = os.path.join(local, "agy", "bin", "agy.exe")
            if os.path.isfile(fallback):
                return fallback
    return None


def _agy_usage(payload):
    if payload.get("status") != "SUCCESS":
        return None
    command = payload.get("command") or {}
    if command.get("name") not in {"usage", "quota"}:
        return None
    data = command.get("data") or {}
    groups = data.get("groups")
    if not isinstance(groups, list):
        return None
    if not any((g or {}).get("name") == "Gemini Models" for g in groups):
        return None
    return data


def _write_json_atomic(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _refresh_agy_cache():
    """Refresh the Agy cache synchronously in a bounded helper process."""
    cache_age = _path_age(AGY_QUOTA_CACHE)
    if cache_age is not None and cache_age < AGY_QUOTA_TTL_SECONDS:
        return 0
    attempt_age = _path_age(AGY_QUOTA_ATTEMPT)
    if attempt_age is not None and attempt_age < AGY_QUOTA_TTL_SECONDS:
        return 0

    os.makedirs(os.path.dirname(AGY_QUOTA_CACHE), exist_ok=True)
    try:
        lock_fd = os.open(AGY_QUOTA_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
    except FileExistsError:
        lock_age = _path_age(AGY_QUOTA_LOCK)
        if lock_age is None or lock_age < AGY_QUOTA_TIMEOUT_SECONDS + 30:
            return 0
        try:
            os.unlink(AGY_QUOTA_LOCK)
        except OSError:
            return 0
        try:
            lock_fd = os.open(AGY_QUOTA_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(lock_fd)
        except OSError:
            return 0
    except OSError:
        return 0

    try:
        _write_json_atomic(AGY_QUOTA_ATTEMPT, {"ts": time.time()})
        executable = _agy_binary()
        if not executable:
            return 1
        try:
            result = subprocess.run(
                [
                    executable,
                    "-p",
                    "/usage",
                    "--output-format",
                    "json",
                    "--print-timeout",
                    f"{AGY_QUOTA_TIMEOUT_SECONDS}s",
                ],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=AGY_QUOTA_TIMEOUT_SECONDS + 5,
                cwd=os.path.expanduser("~"),
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
        except (OSError, subprocess.TimeoutExpired):
            return 1
        if result.returncode != 0:
            return 1
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError):
            return 1
        usage = _agy_usage(payload)
        if usage is None:
            return 1
        cached = {
            "cached_at": time.time(),
            "usage": usage,
        }
        plan_tier = payload.get("plan_tier")
        if plan_tier:
            cached["plan_tier"] = plan_tier
        _write_json_atomic(AGY_QUOTA_CACHE, cached)
        return 0
    finally:
        try:
            os.unlink(AGY_QUOTA_LOCK)
        except OSError:
            pass


def _start_agy_refresh():
    cache_age = _path_age(AGY_QUOTA_CACHE)
    if cache_age is not None and cache_age < AGY_QUOTA_TTL_SECONDS:
        return False
    attempt_age = _path_age(AGY_QUOTA_ATTEMPT)
    if attempt_age is not None and attempt_age < AGY_QUOTA_TTL_SECONDS:
        return False
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--refresh-agy"],
            **kwargs,
        )
        return True
    except OSError:
        return False


def _read_agy_cache():
    try:
        with open(AGY_QUOTA_CACHE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data.get("usage"), dict) else None


def _agy_group(data, name="Gemini Models"):
    for group in (data.get("usage") or {}).get("groups") or []:
        if (group or {}).get("name") == name:
            return group
    return None


def _agy_bucket(group, window):
    for bucket in (group or {}).get("buckets") or []:
        if (bucket or {}).get("window") == window:
            return bucket
    return {}


def _agy_window(bucket):
    try:
        remaining = min(1.0, max(0.0, float(bucket.get("remaining_fraction"))))
    except (TypeError, ValueError):
        return {}
    window = {CLAUDE_PCT_FIELD: (1.0 - remaining) * 100.0}
    reset_time = bucket.get("reset_time")
    if reset_time:
        try:
            from datetime import datetime
            window["resets_at"] = datetime.fromisoformat(
                str(reset_time).replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            pass
    return window


def _fmt_age(secs):
    """Render an age already measured in seconds."""
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60}m ago"
    return f"{secs // 86400}d ago"


def _age(ts):
    """Render the age of an absolute epoch. The Codex row measures its own
    age first, because a rollout record can be unageable in ways an epoch
    cannot; it calls _fmt_age directly."""
    if ts is None or ts == "":
        return "?"
    return _fmt_age(max(0, int(time.time() - float(ts))))


def _reset(window):
    resets_at = window.get("resets_at")
    if not resets_at:
        return ""
    secs = int(float(resets_at) - time.time())
    if secs <= 0:
        return "resetting"
    if secs >= 86400:
        return f"resets {secs // 86400}d{(secs % 86400) // 3600}h"
    if secs >= 3600:
        return f"resets {secs // 3600}h{(secs % 3600) // 60}m"
    if secs >= 60:
        return f"resets {secs // 60}m"
    return "resets <1m"


def _fmt(window, pct_field):
    # The source field is *used* percentage; the rendered number is the
    # remaining headroom (100 - used). The "left" suffix makes the row
    # self-describing so neither a human nor an agent reads it inverted:
    # "94% left" means plenty of quota remains, not "94% consumed".
    used = window.get(pct_field)
    if used is None:
        return "—"
    remaining = max(0.0, 100.0 - float(used))
    r = _reset(window)
    return f"{remaining:.0f}% left" + (f" ({r})" if r else "")


def _codex_window_label(window):
    """Label a Codex window by its actual duration (window_minutes) so a
    weekly window is not mislabeled '5h'. 300 -> '5h', 10080 -> '7d'. Codex
    dropped the fixed 5h window on 2026-07-12; primary is now weekly."""
    m = window.get("window_minutes")
    if not m:
        return ""
    if m % 1440 == 0:
        return f"{m // 1440}d"
    if m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}m"


def _codex_is_main_meter(rate_limits):
    """True when a snapshot belongs to the account plan meter.

    Codex tags each snapshot once per-model meters exist: the plan bucket
    reports limit_id "codex" with a null limit_name, a model-specific bucket
    reports its own pair (for example "codex_bengalfox" /
    "GPT-5.3-Codex-Spark"). Older rollouts carry neither field."""
    return rate_limits.get("limit_id") in CODEX_MAIN_LIMIT_IDS


def _codex_meter_label(rate_limits):
    """Short tag for a side meter, empty string for the main one.

    "GPT-5.3-Codex-Spark" renders as "Spark". The trailing segment is a
    display heuristic, not a uniqueness guarantee: two bucket names could
    share a suffix."""
    if _codex_is_main_meter(rate_limits):
        return ""
    name = rate_limits.get("limit_name") or rate_limits.get("limit_id") or ""
    return name.rsplit("-", 1)[-1] if "-" in name else name


def _epoch(ts):
    """ISO-8601 rollout timestamp to epoch seconds, or None if unparseable."""
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _codex_snapshot_age(ts, path):
    """Age of a selected snapshot in seconds, or None when it cannot be aged
    honestly.

    Three inputs, three answers. A record in the producer's format ages off
    its own timestamp. A record carrying no timestamp at all, which is what
    rollouts predating the field look like, falls back to the file's mtime.
    A record whose timestamp is present but unusable, either unparseable or
    stamped ahead of the clock, returns None and is reported as unknown
    rather than clamped or quietly handed the mtime. A future stamp is the
    case that matters: it keeps outranking valid newer readings until real
    time catches up, so any age computed for it would present the stalest
    available reading as the freshest."""
    now = time.time()
    if not ts:
        try:
            return max(0, int(now - os.path.getmtime(path)))
        except OSError:
            return None
    written = _epoch(ts)
    if written is None or written - now > CODEX_FUTURE_TOLERANCE_SECONDS:
        return None
    return max(0, int(now - written))


def _read_rollout_snapshots(path):
    """Yield (timestamp, rate_limits) for the freshest snapshot of each
    limit_id in one rollout's tail, newest first.

    Lines are chronological, so walking backwards reaches each meter's latest
    snapshot first; a repeat of an id already seen in this file is older and
    is skipped. The line budget caps the parse on a rollout whose tail is
    dense with token_count events."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return
    seen = set()
    examined = 0
    for line in reversed(tail.splitlines()):
        if "rate_limits" not in line:
            continue
        examined += 1
        if examined > MAX_RL_LINES_PER_FILE:
            return
        try:
            obj = json.loads(line)
        except Exception:
            continue
        rl = (obj.get("payload") or {}).get("rate_limits")
        if not rl:
            continue
        key = rl.get("limit_id")
        if key in seen:
            continue
        seen.add(key)
        yield obj.get("timestamp") or "", rl


def _codex_rate_limits():
    """Freshest recognized plan snapshot among the sampled rollouts.

    Returns (rate_limits, timestamp, path), or None when no rollout yields
    one. A recognized plan reading beats an unrecognized bucket even when
    the bucket is newer, because pinning the account plan is the point of
    the selection; the row's age bracket is what keeps an old plan reading
    from passing for a current one.

    mtime orders which files are opened but never which snapshot wins, for
    two reasons measured on this data. A live session keeps its rollout open,
    and NTFS holds that file's mtime at creation time until the handle
    closes, so a finished session looks newer than one still running. And the
    newest file may be reporting a side meter whose reading is minutes older
    than the plan reading in another rollout.

    MAX_ROLLOUT_SCAN is a latency budget, not a correctness threshold: a
    plan reading in a rollout that mtime ranks below it is not seen.

    Ordering is a string compare. That is exact for the producer's fixed
    `YYYY-MM-DDTHH:MM:SS.mmmZ` spelling and not for ISO-8601 at large, where
    differing fractional precision sorts wrong. A rollout predating the
    timestamp field sorts last within its own preference class and only wins
    by default."""
    files = _codex_rollouts()
    if not files:
        return None
    best_main = None
    best_any = None
    for path in files[:MAX_ROLLOUT_SCAN]:
        for ts, rl in _read_rollout_snapshots(path):
            cand = (rl, ts, path)
            if best_any is None or ts > best_any[1]:
                best_any = cand
            if _codex_is_main_meter(rl) and (best_main is None or ts > best_main[1]):
                best_main = cand
    return best_main or best_any


def _codex_rollouts():
    """Every rollout path under ~/.codex/sessions, newest mtime first.

    os.scandir rather than glob plus getmtime: the walk keeps each DirEntry
    and takes the mtime from it. On Windows that value arrives with the
    directory listing and costs nothing, where getmtime is a separate
    syscall per file; on Unix the first entry.stat() still makes one and
    only repeats come free. Measured on Windows over about 5,200 rollouts,
    discovery went from roughly 160 ms to roughly 15 ms, which a status
    line pays on every render.

    Three ways this walk refuses to fail. A directory it cannot read is
    skipped rather than raised, because one unreadable session folder is no
    reason to lose the whole readout on every prompt. A symlinked directory
    is not descended into, which bounds the walk against a symlink cycle;
    glob's `**` did follow them. A Windows junction is still descended
    into, since it keeps the directory attribute that follow_symlinks=False
    tests, but that gap predates this walk and Codex's date tree has no
    reason to hold one. And an entry whose stat raises because it was
    removed after the listing is dropped, which is ordinary here: Codex
    writes this tree while it is read. Windows may still answer from cached
    metadata in that case, and the tail reader catches the failed open."""
    root = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
    found = []
    stack = [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as listing:
                entries = list(listing)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif (entry.name.startswith("rollout-")
                        and entry.name.endswith(".jsonl")
                        and entry.is_file(follow_symlinks=False)):
                    found.append((entry.stat().st_mtime, entry.path))
            except OSError:
                continue
    found.sort(reverse=True)
    return [path for _, path in found]


def _codex_model():
    try:
        import tomllib
    except Exception:
        return "codex"
    path = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f).get("model") or "codex"
    except Exception:
        return "codex"


def claude_row():
    try:
        with open(CLAUDE_RL_CACHE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return "Claude   (no statusLine cache yet — open Claude Code once to populate)"
    rl = d.get("rate_limits") or {}
    model = d.get("model") or "?"
    if model.endswith(" context)") and " (" in model:
        model = model.rsplit(" (", 1)[0]
    five = _fmt(rl.get("five_hour") or {}, CLAUDE_PCT_FIELD)
    week = _fmt(rl.get("seven_day") or {}, CLAUDE_PCT_FIELD)
    return f"Claude   {model:<12}  5h {five:<20}  7d {week:<20}  [{_age(d.get('ts'))}]"


def codex_row():
    found = _codex_rate_limits()
    if found is None:
        if not _codex_rollouts():
            return "Codex    (no session rollout found under ~/.codex/sessions/)"
        return "Codex    (no rate_limits in recent rollouts)"
    rl, ts, path = found
    segs = []
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not w:
            continue
        label = _codex_window_label(w) or key
        segs.append(f"{label} {_fmt(w, CODEX_PCT_FIELD)}")
    credits = rl.get("credits") or {}
    bal = credits.get("balance")
    if credits.get("has_credits") or (bal not in (None, "", "0")):
        segs.append(f"credits {bal}" if bal not in (None, "") else "credits")
    # The side-meter tag replaces the configured model name: the row would
    # otherwise read "gpt-6-astra  5h 100% left" off a Spark bucket.
    meter = _codex_meter_label(rl)
    model = f"[{meter}]" if meter else _codex_model()
    body = "   ".join(segs) if segs else "(no windows)"
    age = _codex_snapshot_age(ts, path)
    shown = "?" if age is None else _fmt_age(age)
    return f"Codex    {model:<12}  {body}  [{shown}]"


def agy_row(refresh_started=False):
    data = _read_agy_cache()
    if data is None:
        state = "refresh started" if refresh_started else "quota cache unavailable"
        return f"Agy      ({state})"
    tier = data.get("plan_tier")
    specs = [
        ("Gemini Models", f"Gemini/{tier}" if tier else "Gemini"),
        ("Claude and GPT models", "Claude/GPT"),
    ]
    rows = []
    for group_name, label in specs:
        group = _agy_group(data, group_name)
        if group is None:
            continue
        five = _fmt(_agy_window(_agy_bucket(group, "5h")), CLAUDE_PCT_FIELD)
        week = _fmt(_agy_window(_agy_bucket(group, "weekly")), CLAUDE_PCT_FIELD)
        rows.append(
            f"Agy      {label:<12}  5h {five:<20}  7d {week:<20}  "
            f"[{_age(data.get('cached_at'))}]"
        )
    return "\n".join(rows) if rows else "Agy      (quota groups unavailable)"


def main():
    if sys.argv[1:] == ["--refresh-agy"]:
        return _refresh_agy_cache()
    refresh_started = _start_agy_refresh()
    print(claude_row())
    print(codex_row())
    print(agy_row(refresh_started))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
