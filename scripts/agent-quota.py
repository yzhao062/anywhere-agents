#!/usr/bin/env python3
"""Cross-agent quota: show Claude + Codex 5h / 7d remaining from disk.

Runs in any terminal, in either agent's session. This is the symmetric,
agent-independent view of usage that the Claude statusLine already shows
inside Claude Code, made readable from a Codex session (or a plain shell)
too. See docs/followups/2026-05-16-agent-fungibility-refactor-plan.md
Phase 9.

Data sources (both on disk, no live render or API call needed):
  - Claude: ~/.claude/rate-limits-cache.json, written by statusline.py on
    each Claude Code statusLine render (override via CLAUDE_RL_CACHE).
  - Codex:  most recent ~/.codex/sessions/**/rollout-*.jsonl, field
    payload.rate_limits. Each window carries window_minutes, so the label
    is derived from it (300 -> 5h, 10080 -> 7d): Codex dropped the fixed 5h
    window on 2026-07-12, so primary is now weekly and secondary may be null.

Each side is only as fresh as that agent's last activity; the age is shown
in brackets so a stale snapshot is obvious.
"""
import glob
import json
import os
import time

CLAUDE_PCT_FIELD = "used_percentage"
CODEX_PCT_FIELD = "used_percent"
CLAUDE_RL_CACHE = os.environ.get("CLAUDE_RL_CACHE") or os.path.join(
    os.path.expanduser("~"), ".claude", "rate-limits-cache.json"
)


def _age(ts):
    if not ts:
        return "?"
    secs = int(time.time() - float(ts))
    if secs < 0:
        return "just now"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60}m ago"
    return f"{secs // 86400}d ago"


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
    five = _fmt(rl.get("five_hour") or {}, CLAUDE_PCT_FIELD)
    week = _fmt(rl.get("seven_day") or {}, CLAUDE_PCT_FIELD)
    return f"Claude   {model:<12}  5h {five:<20}  7d {week:<20}  [{_age(d.get('ts'))}]"


def codex_row():
    home = os.path.expanduser("~")
    files = glob.glob(
        os.path.join(home, ".codex", "sessions", "**", "rollout-*.jsonl"),
        recursive=True,
    )
    if not files:
        return "Codex    (no session rollout found under ~/.codex/sessions/)"
    newest = max(files, key=os.path.getmtime)
    try:
        with open(newest, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return "Codex    (latest rollout unreadable)"
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
        return "Codex    (no rate_limits in latest rollout)"
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
    model = _codex_model()
    body = "   ".join(segs) if segs else "(no windows)"
    return f"Codex    {model:<12}  {body}  [{_age(os.path.getmtime(newest))}]"


def main():
    print(claude_row())
    print(codex_row())


if __name__ == "__main__":
    main()
