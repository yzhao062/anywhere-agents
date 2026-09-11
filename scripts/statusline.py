#!/usr/bin/env python3
"""Claude Code statusLine: show Claude, Codex, and Agy quota.

Claude data: v2.1.80+ injects `rate_limits` into statusLine stdin JSON for
Pro/Max subscribers. Field is absent for API-key sessions and before first
API response of a session.

Codex data: read from the rollout JSONLs under ~/.codex/sessions/. Codex
writes `payload.rate_limits` on every `token_count` event. Each window
carries `window_minutes`, so the label is derived from it (300 -> 5h,
10080 -> 7d) instead of hard-coded: Codex dropped the fixed 5h window on
2026-07-12, so `primary` is now the weekly meter and `secondary` may be
null. Snapshots also carry a `limit_id`: a session running a per-model
bucket reports that bucket rather than the account plan, and such a bucket
usually sits near 0% used, so rendering it verbatim shows full quota while
the plan is half spent. A recognized plan reading therefore always wins,
and an unrecognized id never displaces one; a side meter renders, labeled,
only when the sampled rollouts hold no plan reading at all. Recency among
those rollouts goes by the record timestamp, since NTFS defers the mtime
update on a rollout whose session still holds it open.

What that yields is an approximation rather than the newest reading on
disk. mtime bounds which rollouts are opened, so a plan reading in one
ranked below the bound is not seen, and preferring the plan meter can keep
an older plan reading in front of a newer unrecognized bucket. Neither is
silent: the segment always carries the selected snapshot's age, and says
`age ?` when the record cannot be aged honestly. The age is unconditional
because nothing here can establish that a quiet-looking interval was
quiet. The very reading the mtime bound excluded is proof that prompts ran
and the percentage moved, so a threshold below which the age is hidden
would only shorten the window in which the number misleads. Window resets
are flagged `(reset)` separately, off `resets_at`.

Side effect: each render also persists the Claude `rate_limits` to
~/.claude/rate-limits-cache.json (best-effort, never fatal) so a Codex
session or the standalone `agent-quota` command can read Claude's quota
off disk without a live Claude statusLine render. Override the path with
the CLAUDE_RL_CACHE env var.

Agy data comes from ~/.claude/agy-quota-cache.json. When it is stale, the
status line starts the sibling agent-quota.py in a hidden helper process.
That helper performs one lock-protected, bounded, zero-turn `/usage` query;
the current render never waits on the network.
"""
import json
import os
import subprocess
import sys
import time

CLAUDE_PCT_FIELD = "used_percentage"
CODEX_PCT_FIELD = "used_percent"
# The account plan meter. Rollouts written before per-model buckets shipped
# carry no limit_id at all, so an absent id counts as the main meter.
CODEX_MAIN_LIMIT_IDS = (None, "", "codex")
# Recency bound: enough rollouts to see past a run of side-meter sessions,
# few enough that a render stays a handful of tail reads.
MAX_ROLLOUT_SCAN = 12
MAX_RL_LINES_PER_FILE = 200
# Clock jitter tolerated when aging a record. Past it, a record stamped in
# the future is refused rather than clamped: see codex_snapshot_age.
CODEX_FUTURE_TOLERANCE_SECONDS = 60
CLAUDE_RL_CACHE = os.environ.get("CLAUDE_RL_CACHE") or os.path.join(
    os.path.expanduser("~"), ".claude", "rate-limits-cache.json"
)
AGY_QUOTA_CACHE = os.environ.get("AGY_QUOTA_CACHE") or os.path.join(
    os.path.expanduser("~"), ".claude", "agy-quota-cache.json"
)
AGY_QUOTA_ATTEMPT = AGY_QUOTA_CACHE + ".last-attempt"


def _positive_env(name, default):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


AGY_QUOTA_TTL_SECONDS = _positive_env("AGY_QUOTA_TTL_SECONDS", 300)
AGY_QUOTA_TIMEOUT_SECONDS = _positive_env("AGY_QUOTA_TIMEOUT_SECONDS", 30)


def fmt_window(window, pct_field):
    used = window.get(pct_field)
    if used is None:
        return "—"
    remaining = max(0.0, 100.0 - float(used))
    out = f"{remaining:.0f}%"
    resets_at = window.get("resets_at")
    if not resets_at:
        return out
    secs = int(float(resets_at) - time.time())
    if secs <= 0:
        return out + " (stale)"
    if secs >= 86400:
        return out + f" ({secs // 86400}d{(secs % 86400) // 3600}h)"
    if secs >= 3600:
        return out + f" ({secs // 3600}h{(secs % 3600) // 60}m)"
    if secs >= 60:
        return out + f" ({secs // 60}m)"
    return out + " (<1m)"


def fmt_window_compact(window, pct_field):
    used = window.get(pct_field)
    if used is None:
        return "—"
    out = f"{max(0.0, 100.0 - float(used)):.0f}%"
    resets_at = window.get("resets_at")
    if not resets_at:
        return out
    secs = int(float(resets_at) - time.time())
    if secs <= 0:
        return out + " (reset)"
    if secs >= 86400:
        reset = f"{secs // 86400}d{(secs % 86400) // 3600}h"
    elif secs >= 3600:
        reset = f"{secs // 3600}h{(secs % 3600) // 60}m"
    elif secs >= 60:
        reset = f"{secs // 60}m"
    else:
        reset = "<1m"
    return f"{out} ({reset})"


def fmt_age(secs):
    """Compact snapshot age: just now, 35m ago, 4h ago, 2d ago.

    Worded to match agent-quota's row, so the two readouts of the same
    snapshot do not appear to disagree; the status line drops the minutes
    on an hours-old reading where the row keeps them."""
    if secs < 60:
        return "just now"
    if secs >= 86400:
        return "%dd ago" % (secs // 86400)
    if secs >= 3600:
        return "%dh ago" % (secs // 3600)
    return "%dm ago" % (secs // 60)


def fmt_age_compact(secs):
    if secs < 60:
        return "now"
    if secs >= 86400:
        return "%dd" % (secs // 86400)
    if secs >= 3600:
        return "%dh" % (secs // 3600)
    return "%dm" % (secs // 60)


def codex_snapshot_age(ts):
    """Seconds since a rollout record was written, or None when it cannot be
    aged honestly.

    A record stamped ahead of the clock is refused rather than clamped to
    zero. It arises after a clock correction, or from a rollout written
    under a different clock, and it is the worst case to round down: the
    future stamp keeps outranking valid newer records until real time
    catches up, so clamping would present the stalest available reading as
    the freshest one. Jitter inside CODEX_FUTURE_TOLERANCE_SECONDS is
    absorbed; a real offset returns None and renders as `age ?`."""
    if not ts:
        return None
    try:
        from datetime import datetime
        written = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None
    now = time.time()
    if written - now > CODEX_FUTURE_TOLERANCE_SECONDS:
        return None
    return max(0, int(now - written))


def codex_window_label(window):
    """Label a Codex rate-limit window by its actual duration (from
    window_minutes) so a weekly window is not mislabeled '5h'. Codex dropped
    the fixed 5h window on 2026-07-12; primary is now weekly (10080). Deriving
    the label keeps this correct whether Codex reports weekly-only or restores
    the 5h. 300 -> '5h', 10080 -> '7d'."""
    m = window.get("window_minutes")
    if not m:
        return ""
    if m % 1440 == 0:
        return f"{m // 1440}d"
    if m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}m"


def persist_claude(data):
    """Write the latest Claude rate_limits to disk so off-session readers
    (a Codex session, the agent-quota command) can show Claude's quota.

    Best-effort: any failure is swallowed. The statusLine output must never
    depend on this succeeding. Written atomically via a temp file + replace
    so a concurrent reader never sees a half-written file.
    """
    rl = data.get("rate_limits")
    if not rl:
        return
    try:
        model = (data.get("model") or {}).get("display_name")
        payload = {"model": model, "rate_limits": rl, "ts": time.time()}
        os.makedirs(os.path.dirname(CLAUDE_RL_CACHE), exist_ok=True)
        tmp = CLAUDE_RL_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, CLAUDE_RL_CACHE)
    except Exception:
        pass


def claude_segment(data, compact=False):
    model = (data.get("model") or {}).get("display_name") or "?"
    rl = data.get("rate_limits") or {}
    if compact:
        five = fmt_window_compact(rl.get("five_hour") or {}, CLAUDE_PCT_FIELD)
        week = fmt_window_compact(rl.get("seven_day") or {}, CLAUDE_PCT_FIELD)
        return f"cc 5h{five} 7d{week}"
    five = fmt_window(rl.get("five_hour") or {}, CLAUDE_PCT_FIELD)
    week = fmt_window(rl.get("seven_day") or {}, CLAUDE_PCT_FIELD)
    return f"🤖 {model} · 5h {five} · 7d {week}"


def _path_age(path):
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def start_agy_refresh():
    """Start a bounded refresh without adding network latency to this render."""
    cache_age = _path_age(AGY_QUOTA_CACHE)
    if cache_age is not None and cache_age < AGY_QUOTA_TTL_SECONDS:
        return False
    attempt_age = _path_age(AGY_QUOTA_ATTEMPT)
    if attempt_age is not None and attempt_age < AGY_QUOTA_TTL_SECONDS:
        return False
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-quota.py")
    if not os.path.isfile(helper):
        return False
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": os.path.expanduser("~"),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, helper, "--refresh-agy"], **kwargs)
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


def agy_segment(compact=False):
    data = _read_agy_cache()
    if data is None:
        return None
    group = _agy_group(data)
    five_window = _agy_window(_agy_bucket(group, "5h"))
    week_window = _agy_window(_agy_bucket(group, "weekly"))
    formatter = fmt_window_compact if compact else fmt_window
    five = formatter(five_window, CLAUDE_PCT_FIELD)
    week = formatter(week_window, CLAUDE_PCT_FIELD)
    age = data.get("cached_at")
    shown_age = "@?" if compact else "age ?"
    if age not in (None, ""):
        try:
            age_seconds = max(0, int(time.time() - float(age)))
            shown_age = (
                "@" + fmt_age_compact(age_seconds)
                if compact
                else fmt_age(age_seconds)
            )
        except (TypeError, ValueError):
            pass
    tier = data.get("plan_tier")
    head = "agy" if compact else (f"Agy Gemini [{tier}]" if tier else "Agy Gemini")
    if compact:
        return f"{head} 5h{five} 7d{week} {shown_age}"
    return f"{head} · 5h {five} · 7d {week} · {shown_age}"


def codex_is_main_meter(rate_limits):
    """True when a snapshot belongs to the account plan meter.

    Codex tags each snapshot once per-model meters exist: the plan bucket
    reports limit_id "codex" with a null limit_name, a model-specific bucket
    reports its own pair (for example "codex_bengalfox" /
    "GPT-5.3-Codex-Spark"). Older rollouts carry neither field."""
    return rate_limits.get("limit_id") in CODEX_MAIN_LIMIT_IDS


def codex_meter_label(rate_limits):
    """Short tag for a side meter, empty string for the main one.

    "GPT-5.3-Codex-Spark" renders as "Spark". The trailing segment is a
    display heuristic picked to fit a status line, not a uniqueness
    guarantee: two bucket names could share a suffix."""
    if codex_is_main_meter(rate_limits):
        return ""
    name = rate_limits.get("limit_name") or rate_limits.get("limit_id") or ""
    return name.rsplit("-", 1)[-1] if "-" in name else name


def read_rollout_snapshots(path):
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


def codex_rollouts():
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


def codex_rate_limits():
    """Freshest recognized plan snapshot among the sampled rollouts.

    Returns (rate_limits, timestamp, path), or None when no rollout yields
    one. A recognized plan reading beats an unrecognized bucket even when
    the bucket is newer, because pinning the account plan is the point of
    the selection. Which reading won is therefore not enough on its own to
    say whether it is current, so the caller renders the chosen snapshot's
    age beside the percentage.

    mtime orders which files are opened but never which snapshot wins, for
    two reasons measured on this data. A live session keeps its rollout open,
    and NTFS holds that file's mtime at creation time until the handle
    closes, so a finished session looks newer than one still running. And the
    newest file may be reporting a side meter whose reading is minutes older
    than the plan reading in another rollout.

    MAX_ROLLOUT_SCAN is a latency budget, not a correctness threshold: a
    plan reading in a rollout that mtime ranks below it is not seen. Reading
    every rollout tail in this corpus took about 7 seconds, which no status
    line can spend.

    Ordering is a string compare. That is exact for the producer's fixed
    `YYYY-MM-DDTHH:MM:SS.mmmZ` spelling and not for ISO-8601 at large, where
    differing fractional precision sorts wrong. A rollout predating the
    timestamp field sorts last within its own preference class and only wins
    by default."""
    files = codex_rollouts()
    if not files:
        return None
    best_main = None
    best_any = None
    for path in files[:MAX_ROLLOUT_SCAN]:
        for ts, rl in read_rollout_snapshots(path):
            cand = (rl, ts, path)
            if best_any is None or ts > best_any[1]:
                best_any = cand
            if codex_is_main_meter(rl) and (best_main is None or ts > best_main[1]):
                best_main = cand
    return best_main or best_any


def codex_segment(compact=False):
    found = codex_rate_limits()
    if not found:
        return None
    rl = found[0]
    segs = []
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not w:
            continue
        label = codex_window_label(w) or key
        shown = (
            fmt_window_compact(w, CODEX_PCT_FIELD)
            if compact
            else fmt_window(w, CODEX_PCT_FIELD)
        )
        segs.append(f"{label}{shown}" if compact else f"{label} {shown}")
    credits = rl.get("credits") or {}
    bal = credits.get("balance")
    if credits.get("has_credits") or (bal not in (None, "", "0")):
        if compact:
            segs.append(f"cr{bal}" if bal not in (None, "") else "cr")
        else:
            segs.append(f"cr {bal}" if bal not in (None, "") else "cr")
    if not segs:
        return None
    age = codex_snapshot_age(found[1])
    if compact:
        segs.append("@?" if age is None else "@" + fmt_age_compact(age))
    else:
        segs.append("age ?" if age is None else fmt_age(age))
    meter = codex_meter_label(rl)
    label = "gpt" if compact else "Codex"
    head = f"{label} [{meter}]" if meter else label
    return head + " " + (" ".join(segs) if compact else " · ".join(segs))


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.stdout.write("statusline: bad stdin\n")
        return
    persist_claude(data)
    start_agy_refresh()
    line = claude_segment(data, compact=True)
    cx = codex_segment(compact=True)
    if cx:
        line += " | " + cx
    agy = agy_segment(compact=True)
    if agy:
        line += " | " + agy
    sys.stdout.write(line + "\n")


if __name__ == "__main__":
    main()
