#!/usr/bin/env python3
"""Claude Code statusLine: show Claude Max + Codex 5h/weekly quota.

Claude data: v2.1.80+ injects `rate_limits` into statusLine stdin JSON for
Pro/Max subscribers. Field is absent for API-key sessions and before first
API response of a session.

Codex data: read from the most recent rollout JSONL under ~/.codex/sessions/.
Codex writes `payload.rate_limits` on every `token_count` event. Each window
carries `window_minutes`, so the label is derived from it (300 -> 5h,
10080 -> 7d) instead of hard-coded: Codex dropped the fixed 5h window on
2026-07-12, so `primary` is now the weekly meter and `secondary` may be
null. Snapshot is as fresh as the last Codex prompt; older windows are
flagged `(stale)`.

Side effect: each render also persists the Claude `rate_limits` to
~/.claude/rate-limits-cache.json (best-effort, never fatal) so a Codex
session or the standalone `agent-quota` command can read Claude's quota
off disk without a live Claude statusLine render. Override the path with
the CLAUDE_RL_CACHE env var.
"""
import glob
import json
import os
import sys
import time

CLAUDE_PCT_FIELD = "used_percentage"
CODEX_PCT_FIELD = "used_percent"
CLAUDE_RL_CACHE = os.environ.get("CLAUDE_RL_CACHE") or os.path.join(
    os.path.expanduser("~"), ".claude", "rate-limits-cache.json"
)


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


def claude_segment(data):
    model = (data.get("model") or {}).get("display_name") or "?"
    rl = data.get("rate_limits") or {}
    five = fmt_window(rl.get("five_hour") or {}, CLAUDE_PCT_FIELD)
    week = fmt_window(rl.get("seven_day") or {}, CLAUDE_PCT_FIELD)
    return f"🤖 {model} · 5h {five} · 7d {week}"


def codex_segment():
    home = os.path.expanduser("~")
    files = glob.glob(
        os.path.join(home, ".codex", "sessions", "**", "rollout-*.jsonl"),
        recursive=True,
    )
    if not files:
        return None
    newest = max(files, key=os.path.getmtime)
    try:
        with open(newest, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    rl = None
    for line in reversed(tail.splitlines()):
        if "rate_limits" not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        rl = (obj.get("payload") or {}).get("rate_limits")
        if rl:
            break
    if not rl:
        return None
    segs = []
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not w:
            continue
        label = codex_window_label(w) or key
        segs.append(f"{label} {fmt_window(w, CODEX_PCT_FIELD)}")
    credits = rl.get("credits") or {}
    bal = credits.get("balance")
    if credits.get("has_credits") or (bal not in (None, "", "0")):
        segs.append(f"cr {bal}" if bal not in (None, "") else "cr")
    if not segs:
        return None
    return "Codex " + " · ".join(segs)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.stdout.write("statusline: bad stdin\n")
        return
    persist_claude(data)
    line = claude_segment(data)
    cx = codex_segment()
    if cx:
        line += "  |  " + cx
    sys.stdout.write(line + "\n")


if __name__ == "__main__":
    main()
