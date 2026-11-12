#!/usr/bin/env python3
"""Headless Grok CLI dispatcher for /vet (implement-review).

Runs the Grok CLI against a disposable staged snapshot so the reviewer never
executes in or mutates the caller's working tree or Git index. Enforces:

- Orchestrator self-review refusal (IMPLEMENT_REVIEW_ORCHESTRATOR=grok exits 2)
- Registry model and effort resolution via scripts/agent_models.py
- Preflight binary and login checks without spending quota
- Rolling 7-day machine spend guard (GROK_WEEKLY_BUDGET_USD, exits 75)
- Staged-snapshot isolation and supplied-input injection (AGENTS.md and
  AGENTS.local.md)
- Live NDJSON streaming to <state-dir>/tail with tool_call narration reset
- Terminal end_turn verification and atomic review publication
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Optional


EXIT_USAGE = 2
EXIT_RUNTIME = 70
EXIT_BUDGET = 75
EXIT_NOT_FOUND = 127

DEFAULT_TIMEOUT_SECONDS = 2700.0
DEFAULT_KILL_GRACE_SECONDS = 2.0
EXPECTED_REVIEW_BASENAME = "Review-Grok.md"
MIN_REVIEW_BYTES = 500
VALID_OVERRIDE_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")

CHILD_ENV_OVERRIDES = {
    "GROK_FOLDER_TRUST": "0",
    "GROK_CLAUDE_HOOKS_ENABLED": "false",
    "GROK_CLAUDE_MCPS_ENABLED": "false",
    "GROK_CLAUDE_SKILLS_ENABLED": "false",
    "GROK_CURSOR_HOOKS_ENABLED": "false",
    "GROK_CURSOR_MCPS_ENABLED": "false",
    "GROK_DISABLE_AUTOUPDATER": "1",
    "GIT_PAGER": "cat",
}


def find_agent_models() -> Optional[Path]:
    """Locate scripts/agent_models.py by walking up from this script.

    A source checkout keeps the resolver beside the skill tree; a consumer
    deploys the skill under .claude/skills/ and the resolver under
    .agent-config/repo/, a sibling no ancestor walk reaches on its own.
    """
    for base in Path(__file__).resolve().parents:
        for root in (base, base / ".agent-config" / "repo"):
            candidate = root / "scripts" / "agent_models.py"
            if candidate.is_file() and (root / "user" / "agent-models.json").is_file():
                return candidate
    return None


def load_agent_models_module() -> Optional[Any]:
    path = find_agent_models()
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location("agent_models", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AGENT_MODELS = load_agent_models_module()


def grok_home_dir() -> Path:
    override = os.environ.get("GROK_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if AGENT_MODELS is not None and hasattr(AGENT_MODELS, "grok_home_path"):
        return Path(AGENT_MODELS.grok_home_path())
    return Path.home() / ".grok"


def resolve_model_and_effort() -> tuple[str, str, str, str]:
    """Return (model, model_source, effort, effort_source) or raise RuntimeError/ValueError."""
    raw_model_env = os.environ.get("GROK_DISPATCH_MODEL")
    raw_effort_env = os.environ.get("GROK_DISPATCH_EFFORT")

    model_override: Optional[str] = None
    effort_override: Optional[str] = None

    if raw_model_env is not None:
        stripped = raw_model_env.strip()
        if not stripped or not VALID_OVERRIDE_RE.match(stripped):
            raise ValueError(
                f"invalid GROK_DISPATCH_MODEL override: {raw_model_env!r}"
            )
        model_override = stripped

    if raw_effort_env is not None:
        stripped = raw_effort_env.strip()
        if not stripped or not VALID_OVERRIDE_RE.match(stripped):
            raise ValueError(
                f"invalid GROK_DISPATCH_EFFORT override: {raw_effort_env!r}"
            )
        if AGENT_MODELS is not None:
            allowed_efforts = getattr(AGENT_MODELS, "EFFORTS", {}).get("grok", ())
            if allowed_efforts and stripped not in allowed_efforts:
                raise ValueError(
                    f"invalid GROK_DISPATCH_EFFORT override {stripped!r}; "
                    f"expected one of {', '.join(allowed_efforts)}"
                )
        effort_override = stripped

    if model_override is not None and effort_override is not None:
        return model_override, "env", effort_override, "env"

    if AGENT_MODELS is None:
        raise RuntimeError(
            "cannot locate scripts/agent_models.py to resolve families.grok from user/agent-models.json (add it as the agent-models skill describes, or set both GROK_DISPATCH_MODEL and GROK_DISPATCH_EFFORT)"
        )

    try:
        reg_env = (
            os.environ.get("AGENT_MODELS_FILE", "").strip()
            or os.environ.get("AGENT_MODELS_REGISTRY", "").strip()
        )
        reg = (
            AGENT_MODELS.load(Path(reg_env))
            if reg_env
            else AGENT_MODELS.load()
        )
        entry = AGENT_MODELS.resolve("grok", "reviewer", reg)
        reg_model = entry.get("model") if isinstance(entry, dict) else None
        reg_effort = entry.get("effort") if isinstance(entry, dict) else None
    except Exception as exc:
        raise RuntimeError(
            f"missing or invalid families.grok row in user/agent-models.json: {exc} (add it as the agent-models skill describes, or set both GROK_DISPATCH_MODEL and GROK_DISPATCH_EFFORT)"
        ) from exc

    if not reg_model or not reg_effort:
        raise RuntimeError(
            "missing families.grok model or effort in user/agent-models.json (add it as the agent-models skill describes, or set both GROK_DISPATCH_MODEL and GROK_DISPATCH_EFFORT)"
        )

    final_model = model_override if model_override is not None else str(reg_model)
    final_model_source = "env" if model_override is not None else "registry"
    final_effort = effort_override if effort_override is not None else str(reg_effort)
    final_effort_source = "env" if effort_override is not None else "registry"
    return final_model, final_model_source, final_effort, final_effort_source


def is_executable_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


def resolve_grok_executable() -> Optional[str]:
    """Locate the Grok CLI binary from GROK_BIN, PATH, or ~/.grok/bin/grok."""
    override = os.environ.get("GROK_BIN", "").strip()
    if override:
        override_path = Path(override).expanduser()
        if override_path.is_file() and is_executable_file(override_path):
            return str(override_path.resolve())
        found = shutil.which(override)
        if found:
            return str(Path(found).resolve())
        return None

    found = shutil.which("grok")
    if found:
        return str(Path(found).resolve())

    candidates: list[Path] = []
    for base in (grok_home_dir(), Path.home() / ".grok"):
        if os.name == "nt":
            candidates.extend(
                [
                    base / "bin" / "grok.exe",
                    base / "bin" / "grok.cmd",
                    base / "bin" / "grok",
                ]
            )
        else:
            candidates.append(base / "bin" / "grok")

    for candidate in candidates:
        if is_executable_file(candidate):
            return str(candidate.resolve())
    return None


def check_grok_login() -> tuple[bool, str]:
    """Verify Grok authentication without invoking the CLI or spending quota."""
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    if api_key:
        return True, "XAI_API_KEY"

    auth_override = os.environ.get("GROK_AUTH_PATH")
    if auth_override is not None:
        stripped = auth_override.strip()
        if not stripped:
            return (
                False,
                "GROK_AUTH_PATH is empty and XAI_API_KEY is unset; run `grok login` or set XAI_API_KEY.",
            )
        auth_path = Path(stripped).expanduser()
        try:
            if auth_path.is_file() and auth_path.stat().st_size > 0:
                return True, str(auth_path)
        except OSError:
            pass
        return (
            False,
            f"Grok auth file not found at GROK_AUTH_PATH ({auth_path}); run `grok login`.",
        )

    default_auth = grok_home_dir() / "auth.json"
    try:
        if default_auth.is_file() and default_auth.stat().st_size > 0:
            return True, str(default_auth)
    except OSError:
        pass
    return (
        False,
        f"Grok credentials not found at {default_auth}; run `grok login` or set XAI_API_KEY.",
    )


def parse_iso8601_epoch(value: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp string into POSIX epoch seconds (Python 3.9+)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Python 3.9 datetime.fromisoformat supports 3 or 6 fractional digits only.
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?([+-]\d{2}:?\d{2})?$",
        text,
    )
    if match:
        head, frac, tz = match.groups()
        if frac:
            frac = (frac + "000000")[:6]
            text = f"{head}.{frac}{tz or ''}"
        else:
            text = f"{head}{tz or ''}"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def compute_weekly_spend_usd(
    grok_home: Optional[Path] = None,
    now_epoch: Optional[float] = None,
) -> float:
    """Sum turns[].costUsdTicks / 1e10 over the last 7 days across usage.json files."""
    if now_epoch is None:
        env_now = os.environ.get("GROK_DISPATCH_NOW_EPOCH", "").strip()
        if env_now:
            try:
                now_epoch = float(env_now)
            except ValueError:
                now_epoch = time.time()
        else:
            now_epoch = time.time()

    cutoff = now_epoch - (7 * 86400.0)
    home = grok_home if grok_home is not None else grok_home_dir()
    sessions_root = home / "sessions"
    if not sessions_root.is_dir():
        return 0.0

    total_ticks = 0.0
    for usage_path in sessions_root.glob("*/*/usage.json"):
        try:
            st = usage_path.stat()
        except OSError:
            continue
        # A FIFO or device named usage.json would block the read; skip it.
        if not stat.S_ISREG(st.st_mode):
            continue
        # File mtime is used strictly as a skip filter for files untouched in 7 days.
        if st.st_mtime < cutoff:
            continue
        try:
            data = json.loads(usage_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        turns = data.get("turns")
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            ended_epoch = parse_iso8601_epoch(turn.get("endedAt"))
            if ended_epoch is None or ended_epoch < cutoff:
                continue
            ticks = turn.get("costUsdTicks")
            if (
                isinstance(ticks, (int, float))
                and not isinstance(ticks, bool)
                and math.isfinite(ticks)
                and ticks > 0
            ):
                total_ticks += float(ticks)
    return total_ticks / 1e10


def check_weekly_budget(
    grok_home: Optional[Path] = None,
    now_epoch: Optional[float] = None,
) -> Optional[str]:
    """Return a refusal message when GROK_WEEKLY_BUDGET_USD is reached, else None."""
    raw_budget = os.environ.get("GROK_WEEKLY_BUDGET_USD", "").strip()
    if not raw_budget:
        return None
    # A cap someone set but mistyped must not turn into no cap at all.
    try:
        budget = float(raw_budget)
    except ValueError:
        budget = float("nan")
    if not math.isfinite(budget) or budget < 0:
        return (
            f"GROK_WEEKLY_BUDGET_USD={raw_budget!r} is not a non-negative number; "
            "refusing to dispatch until it is fixed or unset."
        )

    spend = compute_weekly_spend_usd(grok_home=grok_home, now_epoch=now_epoch)
    if spend >= budget:
        return (
            f"Grok 7-day spend ${spend:.4f} has reached "
            f"GROK_WEEKLY_BUDGET_USD=${budget:.4f}; refusing to dispatch."
        )
    return None


def staged_paths_edited_since(cwd: Path) -> Optional[list[str]]:
    """Return staged paths that have changed again since staging.

    Same rule as dispatch-gemini.py (anywhere-agents#57): `--no-renames`
    because rename detection prints only the destination, and untracked paths
    because a staged deletion or rename whose old path is recreated leaves
    that path untracked, where `git diff` cannot see it.
    """
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--no-renames", "-z"],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    unstaged = subprocess.run(
        ["git", "diff", "--name-only", "-z"],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if staged.returncode != 0 or unstaged.returncode != 0 or untracked.returncode != 0:
        return None
    staged_set = {
        entry.decode("utf-8", errors="replace")
        for entry in staged.stdout.split(b"\x00")
        if entry
    }
    if not staged_set:
        return []
    changed_set = {
        entry.decode("utf-8", errors="replace")
        for entry in unstaged.stdout.split(b"\x00") + untracked.stdout.split(b"\x00")
        if entry
    }
    return sorted(staged_set & changed_set)


def make_state_dir(cwd: Path, round_number: int) -> Path:
    digest = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()[:12]
    prefix = f"implement-review-grok-{digest}-round{round_number}-{os.getpid()}-"
    return Path(tempfile.mkdtemp(prefix=prefix, dir=tempfile.gettempdir()))


def atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def prepare_snapshot(cwd: Path, state_dir: Path) -> Optional[tuple[Path, Path, str]]:
    """Export the Git index into <state-dir>/staged-snapshot.

    Returns (repo_root, snapshot_dir, staged_diff_text) or None on failure.
    """
    diff_proc = subprocess.run(
        ["git", "diff", "--cached", "--no-ext-diff"],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if diff_proc.returncode != 0:
        return None
    staged_diff = diff_proc.stdout.decode("utf-8", errors="replace")

    toplevel_proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if toplevel_proc.returncode != 0:
        return None
    repo_root_raw = toplevel_proc.stdout.decode("utf-8", errors="replace").strip()
    if not repo_root_raw:
        return None
    repo_root = Path(repo_root_raw).resolve()

    snapshot_dir = state_dir / "staged-snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    prefix = str(snapshot_dir.resolve())
    if not prefix.endswith(os.sep):
        prefix += os.sep

    checkout_proc = subprocess.run(
        ["git", "-c", "core.longpaths=true", "checkout-index", "-a", f"--prefix={prefix}"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if checkout_proc.returncode != 0:
        return None

    return repo_root, snapshot_dir, staged_diff


def collect_supplied_inputs(
    repo_root: Path,
    snapshot_dir: Path,
    original_prompt: str,
) -> list[tuple[str, str]]:
    """Select AGENTS.md and AGENTS.local.md for the relay prompt.

    - AGENTS.md: supplied from the worktree when it exists there and is absent
      from the exported index (snapshot).
    - AGENTS.local.md: always supplied when it exists (snapshot copy when
      tracked in the index, worktree copy when untracked).
    """
    supplied: list[tuple[str, str]] = []

    wt_agents = repo_root / "AGENTS.md"
    snap_agents = snapshot_dir / "AGENTS.md"
    if wt_agents.is_file() and not snap_agents.is_file():
        header = "--- SUPPLIED INPUT: AGENTS.md ---"
        if header not in original_prompt:
            try:
                supplied.append(
                    ("AGENTS.md", wt_agents.read_text(encoding="utf-8", errors="replace"))
                )
            except OSError:
                pass

    snap_local = snapshot_dir / "AGENTS.local.md"
    wt_local = repo_root / "AGENTS.local.md"
    local_source: Optional[Path] = None
    if snap_local.is_file():
        local_source = snap_local
    elif wt_local.is_file():
        local_source = wt_local

    if local_source is not None:
        header = "--- SUPPLIED INPUT: AGENTS.local.md ---"
        if header not in original_prompt:
            try:
                supplied.append(
                    (
                        "AGENTS.local.md",
                        local_source.read_text(encoding="utf-8", errors="replace"),
                    )
                )
            except OSError:
                pass

    return supplied


def build_relay_prompt(
    prompt_file: Path,
    expected_name: str,
    round_number: int,
    validation_dir: Path,
    staged_diff: str,
    supplied_inputs: list[tuple[str, str]],
) -> str:
    original = prompt_file.read_text(encoding="utf-8", errors="replace")
    diff_body = staged_diff.strip() or "(git diff --cached was empty)"

    lines: list[str] = [
        "HEADLESS GROK REVIEW RELAY",
        "",
        f"You are running inside a disposable staged snapshot at `{validation_dir}`.",
        f"The review file to write is `{expected_name}` at `{validation_dir / expected_name}`.",
        "Never inspect, cd into, or modify the caller's original working tree or Git index.",
        "",
        "Dispatched-reviewer exemption: your task, lens, and response format are supplied",
        "in this prompt. You are exempt from AGENTS.md bootstrap, session-start banner,",
        "and router or coordinator-skill discovery instructions.",
        "",
        "Project rule precedence: local overrides in `AGENTS.local.md` win over shared",
        "rules in `AGENTS.md`. Any file included below under a `--- SUPPLIED INPUT: <path> ---`",
        "heading has already been provided in full; do not read supplied files again from disk.",
        "",
        "Output requirements:",
        f"1. Write `{expected_name}` in `{validation_dir}` (or output the complete review as your final response after all tool calls).",
        f"2. The very first line of `{expected_name}` MUST be `<!-- Round {round_number} -->`.",
        "3. The review must be complete and at least 500 bytes.",
        "",
        "--- ORIGINAL REVIEW REQUEST ---",
        original.rstrip(),
        "--- END ORIGINAL REVIEW REQUEST ---",
    ]

    for rel_path, content in supplied_inputs:
        lines.extend(
            [
                "",
                f"--- SUPPLIED INPUT: {rel_path} ---",
                content.rstrip(),
                f"--- END SUPPLIED INPUT: {rel_path} ---",
            ]
        )

    lines.extend(
        [
            "",
            "--- STAGED DIFF PROVIDED BY DISPATCHER ---",
            diff_body,
            "--- END STAGED DIFF ---",
            "",
        ]
    )
    return "\n".join(lines)


def build_child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(CHILD_ENV_OVERRIDES)
    return env


def record_grok_version(executable: str, state_dir: Path, env: dict[str, str]) -> None:
    version_file = state_dir / "grok-version"
    try:
        proc = subprocess.run(
            [executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=15,
            check=False,
        )
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        version_file.write_text((out or "unknown") + "\n", encoding="utf-8")
    except Exception:
        version_file.write_text("unknown\n", encoding="utf-8")


def record_actual(
    state_dir: Path,
    tail_path: Path,
    session_id: Optional[str],
    expect_model: str,
    expect_effort: str,
) -> None:
    if AGENT_MODELS is None:
        with (state_dir / "review-model").open("a", encoding="utf-8") as handle:
            handle.write("actual=unknown unknown status=unknown\n")
        return
    out = io.StringIO()
    err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out, err
        AGENT_MODELS.cmd_actual(
            argparse.Namespace(
                family="grok",
                json_file=str(tail_path),
                session_id=session_id,
                expect_model=expect_model,
                expect_effort=expect_effort,
            )
        )
    except Exception:
        out.write("actual=unknown unknown status=unknown\n")
    finally:
        sys.stdout, sys.stderr = old_out, old_err

    actual_line = out.getvalue()
    if actual_line:
        with (state_dir / "review-model").open("a", encoding="utf-8") as handle:
            handle.write(actual_line)
    err_text = err.getvalue()
    if err_text:
        sys.stderr.write(err_text)


def start_stall_watch(state_dir: Path) -> Optional[subprocess.Popen[bytes]]:
    """Launch the shared stall-watch in the background (warning-only; never kills).

    Windows runs stall-watch.ps1 through powershell.exe, as dispatch-gemini.py
    does: a bare `bash` there can resolve to the WSL launcher, which cannot
    watch a Win32 parent process.
    """
    script_dir = Path(__file__).resolve().parent
    if os.name == "nt":
        watcher = script_dir / "stall-watch.ps1"
        if not watcher.is_file():
            return None
        launcher = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    else:
        watcher = script_dir / "stall-watch.sh"
        if not watcher.is_file():
            return None
        bash = shutil.which("bash")
        if not bash:
            return None
        launcher = [bash]
    threshold = (
        os.environ.get("STALL_THRESHOLD_SECONDS", "").strip()
        or os.environ.get("IMPLEMENT_REVIEW_STALL_SECONDS", "600").strip()
    )
    interval = (
        os.environ.get("STALL_POLL_INTERVAL_SECONDS", "").strip()
        or os.environ.get("IMPLEMENT_REVIEW_STALL_CHECK_INTERVAL", "30").strip()
    )
    watcher_env = os.environ.copy()
    watcher_env["STALL_THRESHOLD_SECONDS"] = str(threshold)
    watcher_env["STALL_POLL_INTERVAL_SECONDS"] = str(interval)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": watcher_env,
        "cwd": str(state_dir),
    }
    if os.name == "nt":
        flags = 0
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        (state_dir / "tail").touch(exist_ok=True)
        proc = subprocess.Popen(
            [
                *launcher,
                str(watcher),
                "--state-dir",
                str(state_dir),
                "--parent-pid",
                str(os.getpid()),
            ],
            **kwargs,
        )
        (state_dir / "stall-watch.pid").write_text(
            f"{proc.pid}\n", encoding="utf-8"
        )
        return proc
    except OSError:
        return None


def stop_stall_watch(
    watcher: Optional[subprocess.Popen[bytes]], state_dir: Path
) -> None:
    if watcher is not None:
        try:
            watcher.terminate()
            watcher.wait(timeout=2)
        except Exception:
            try:
                watcher.kill()
            except Exception:
                pass
    pid_file = state_dir / "stall-watch.pid"
    try:
        pid_file.unlink()
    except OSError:
        pass


def collect_descendant_pids(root_pid: int) -> list[int]:
    """Return descendant PIDs of root_pid on POSIX systems."""
    if os.name == "nt":
        return []
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except Exception:
        return []
    children_map: dict[int, list[int]] = {}
    for raw_line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = raw_line.split()
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        children_map.setdefault(ppid, []).append(pid)

    descendants: list[int] = []
    frontier = list(children_map.get(root_pid, ()))
    seen: set[int] = set()
    while frontier:
        curr = frontier.pop(0)
        if curr in seen:
            continue
        seen.add(curr)
        descendants.append(curr)
        frontier.extend(children_map.get(curr, ()))
    return descendants


def terminate_process_tree(
    process: subprocess.Popen[bytes],
    grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
) -> None:
    """Terminate process and all descendants, escalating to SIGKILL after grace_seconds."""
    if process.poll() is not None:
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            process.wait(timeout=max(grace_seconds, 1.0))
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        return

    descendants = collect_descendant_pids(process.pid)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass
    for pid in descendants:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.monotonic() + max(0.05, grace_seconds)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.05)

    remaining = set(collect_descendant_pids(process.pid)) | set(descendants)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    try:
        process.wait(timeout=2.0)
    except Exception:
        pass


class StreamAccumulator:
    """Incremental parser for Grok streaming-json NDJSON events."""

    def __init__(self) -> None:
        self.full_text_parts: list[str] = []
        self.post_tool_text_parts: list[str] = []
        self.saw_tool_call: bool = False
        self.end_event: Optional[dict[str, Any]] = None
        self.error_events: list[dict[str, Any]] = []
        self.event_count: int = 0

    def feed_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return

        self.event_count += 1
        etype = event.get("type")
        if etype == "text":
            chunk = event.get("data")
            if not isinstance(chunk, str):
                chunk = event.get("text")
            if isinstance(chunk, str):
                self.full_text_parts.append(chunk)
                self.post_tool_text_parts.append(chunk)
        elif etype == "tool_call":
            self.saw_tool_call = True
            self.post_tool_text_parts.clear()
        elif etype == "end":
            self.end_event = event
        elif etype == "error":
            self.error_events.append(event)
        # Other event types (thought, tool_call_update, usage, unknown) update liveness only.

    @property
    def response_text(self) -> str:
        post = "".join(self.post_tool_text_parts)
        if post.strip():
            return post
        return "".join(self.full_text_parts)


def format_cost_usd(end_event: Optional[dict[str, Any]]) -> str:
    if not isinstance(end_event, dict):
        return "unknown"
    if end_event.get("cost_is_partial") or end_event.get("usage_is_incomplete"):
        return "unknown"
    cost = end_event.get("total_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return "unknown"
    if not math.isfinite(cost):
        return "unknown"
    return str(cost)


def run_grok_stream(
    executable: str,
    relay_path: Path,
    validation_dir: Path,
    state_dir: Path,
    model: str,
    effort: str,
    env: dict[str, str],
    timeout_seconds: float,
    grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
    clock: Optional[Callable[[], float]] = None,
) -> tuple[int, bool, StreamAccumulator, str]:
    """Spawn Grok in streaming-json mode, streaming each line to <state-dir>/tail."""
    tail_path = state_dir / "tail"
    stderr_path = state_dir / "tail.stderr-tmp"

    clock_step = 0.0
    raw_step = os.environ.get("GROK_DISPATCH_CLOCK_STEP_SECONDS", "").strip()
    if raw_step:
        try:
            clock_step = max(0.0, float(raw_step))
        except ValueError:
            clock_step = 0.0

    virtual_offset = 0.0
    base_clock = clock if clock is not None else time.monotonic

    def now_fn() -> float:
        return base_clock() + virtual_offset

    cmd = [
        executable,
        "--prompt-file",
        str(relay_path),
        "--cwd",
        str(validation_dir),
        "--always-approve",
        "--output-format",
        "streaming-json",
        "--no-auto-update",
        "-m",
        model,
        "--effort",
        effort,
    ]

    popen_kwargs: dict[str, Any] = {
        "cwd": str(validation_dir),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        flags = 0
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        popen_kwargs["creationflags"] = flags
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(cmd, **popen_kwargs)
    line_queue: queue.Queue[Optional[bytes]] = queue.Queue()
    stderr_chunks: list[bytes] = []

    def stdout_reader() -> None:
        assert process.stdout is not None
        try:
            while True:
                raw = process.stdout.readline()
                if not raw:
                    break
                line_queue.put(raw)
        finally:
            line_queue.put(None)

    def stderr_reader() -> None:
        assert process.stderr is not None
        with stderr_path.open("wb") as err_fh:
            while True:
                raw = process.stderr.readline()
                if not raw:
                    break
                stderr_chunks.append(raw)
                err_fh.write(raw)
                err_fh.flush()

    t_out = threading.Thread(target=stdout_reader, daemon=True)
    t_err = threading.Thread(target=stderr_reader, daemon=True)
    t_out.start()
    t_err.start()

    accumulator = StreamAccumulator()
    start_time = now_fn()
    deadline = start_time + timeout_seconds
    timed_out = False
    eof_reached = False

    with tail_path.open("wb") as tail_fh:
        while not eof_reached:
            remaining = deadline - now_fn()
            if remaining <= 0:
                timed_out = True
                terminate_process_tree(process, grace_seconds=grace_seconds)
                break

            wait_slice = min(0.1, max(0.01, remaining))
            try:
                item = line_queue.get(timeout=wait_slice)
            except queue.Empty:
                if process.poll() is not None and line_queue.empty():
                    # Give reader thread a tiny moment to flush final EOF marker
                    try:
                        item = line_queue.get(timeout=0.05)
                    except queue.Empty:
                        break
                else:
                    continue

            if item is None:
                eof_reached = True
                break

            tail_fh.write(item)
            tail_fh.flush()
            decoded = item.decode("utf-8", errors="replace")
            accumulator.feed_line(decoded)
            if clock_step > 0:
                virtual_offset += clock_step
                if now_fn() >= deadline and process.poll() is None:
                    timed_out = True
                    terminate_process_tree(process, grace_seconds=grace_seconds)
                    break

    if not timed_out:
        try:
            process.wait(timeout=max(1.0, deadline - now_fn()))
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_tree(process, grace_seconds=grace_seconds)

    t_out.join(timeout=1.0)
    t_err.join(timeout=1.0)

    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    if stderr_text:
        with tail_path.open("ab") as tail_fh:
            if not stderr_text.startswith("\n"):
                tail_fh.write(b"\n")
            tail_fh.write(stderr_text.encode("utf-8", errors="replace"))
            tail_fh.flush()
    try:
        stderr_path.unlink()
    except OSError:
        pass

    returncode = process.returncode if process.returncode is not None else 1
    return returncode, timed_out, accumulator, stderr_text


def validate_candidate_review(
    candidate_path: Path,
    round_number: int,
    dispatch_start_ns: int,
) -> tuple[bool, str]:
    """Validate <snapshot>/Review-Grok.md before publishing to the repository root."""
    if not candidate_path.is_file():
        return False, f"{candidate_path.name} was not produced"
    try:
        st = candidate_path.stat()
    except OSError as exc:
        return False, f"cannot stat {candidate_path.name}: {exc}"

    if st.st_mtime_ns < dispatch_start_ns:
        return (
            False,
            f"{candidate_path.name} mtime is older than dispatch start",
        )

    try:
        raw_bytes = candidate_path.read_bytes()
    except OSError as exc:
        return False, f"cannot read {candidate_path.name}: {exc}"

    if len(raw_bytes) < MIN_REVIEW_BYTES:
        return (
            False,
            f"{candidate_path.name} is {len(raw_bytes)}B (< {MIN_REVIEW_BYTES}B minimum)",
        )

    text = raw_bytes.decode("utf-8", errors="replace")
    first_line = text.splitlines()[0].rstrip("\r") if text.splitlines() else ""
    expected_marker = f"<!-- Round {round_number} -->"
    if first_line != expected_marker:
        return (
            False,
            f"first line of {candidate_path.name} is {first_line!r}, expected {expected_marker!r}",
        )

    return True, text


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dispatch-grok.py",
        description="Dispatch a headless Grok review in an isolated staged snapshot.",
    )
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--round", required=True, type=int)
    parser.add_argument("--expected-review-file", required=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return EXIT_USAGE if code != 0 else 0

    if args.round <= 0:
        sys.stderr.write("dispatch-grok.py: --round must be a positive integer\n")
        return EXIT_USAGE

    expected_path = Path(args.expected_review_file)
    if expected_path.name != EXPECTED_REVIEW_BASENAME:
        sys.stderr.write(
            f"dispatch-grok.py: --expected-review-file must be {EXPECTED_REVIEW_BASENAME}, "
            f"got {expected_path.name!r}\n"
        )
        return EXIT_USAGE

    orchestrator = os.environ.get("IMPLEMENT_REVIEW_ORCHESTRATOR", "").strip().lower()
    if orchestrator in {"grok", "grok-build"}:
        sys.stderr.write(
            "dispatch-grok.py: refusing self-review when IMPLEMENT_REVIEW_ORCHESTRATOR=grok\n"
        )
        return EXIT_USAGE

    prompt_file = Path(args.prompt_file)
    if not prompt_file.is_file() or prompt_file.stat().st_size == 0:
        sys.stderr.write(
            f"dispatch-grok.py: --prompt-file missing or empty: {prompt_file}\n"
        )
        return EXIT_USAGE

    cwd = Path.cwd()
    if not expected_path.is_absolute():
        expected_path = cwd / expected_path

    allow_drift = os.environ.get("IMPLEMENT_REVIEW_ALLOW_STAGED_DRIFT", "").strip().lower()
    if allow_drift not in {"1", "true", "yes", "on"}:
        overlapping = staged_paths_edited_since(cwd)
        if overlapping is None:
            sys.stderr.write(
                "dispatch-grok.py: git diff failed while checking staged vs working-tree state.\n"
            )
            return EXIT_USAGE
        if overlapping:
            joined = ", ".join(overlapping[:10])
            more = (
                f" (+{len(overlapping) - 10} more)"
                if len(overlapping) > 10
                else ""
            )
            sys.stderr.write(
                "dispatch-grok.py: refusing to dispatch with unstaged edits on staged paths: "
                f"{joined}{more}. Re-stage (`git add`) or set IMPLEMENT_REVIEW_ALLOW_STAGED_DRIFT=1.\n"
            )
            return EXIT_USAGE

    try:
        model, model_source, effort, effort_source = resolve_model_and_effort()
    except ValueError as exc:
        sys.stderr.write(f"dispatch-grok.py: {exc}\n")
        return EXIT_USAGE
    except RuntimeError as exc:
        sys.stderr.write(f"dispatch-grok.py: {exc}\n")
        return EXIT_RUNTIME

    executable = resolve_grok_executable()
    if executable is None:
        sys.stderr.write(
            "dispatch-grok.py: grok CLI executable not found on GROK_BIN, PATH, or ~/.grok/bin/grok\n"
        )
        return EXIT_NOT_FOUND

    logged_in, login_detail = check_grok_login()
    if not logged_in:
        sys.stderr.write(f"dispatch-grok.py: {login_detail}\n")
        return EXIT_RUNTIME

    budget_refusal = check_weekly_budget()
    if budget_refusal is not None:
        sys.stderr.write(f"dispatch-grok.py: {budget_refusal}\n")
        return EXIT_BUDGET

    raw_timeout = os.environ.get("GROK_DISPATCH_TIMEOUT_SECONDS", "").strip()
    timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    if raw_timeout:
        try:
            parsed_timeout = float(raw_timeout)
            if math.isfinite(parsed_timeout) and parsed_timeout > 0:
                timeout_seconds = parsed_timeout
        except ValueError:
            pass

    raw_grace = os.environ.get("GROK_DISPATCH_KILL_GRACE_SECONDS", "").strip()
    grace_seconds = DEFAULT_KILL_GRACE_SECONDS
    if raw_grace:
        try:
            parsed_grace = float(raw_grace)
            if math.isfinite(parsed_grace) and parsed_grace > 0:
                grace_seconds = parsed_grace
        except ValueError:
            pass

    state_dir = make_state_dir(cwd, args.round)
    pre_mtime = int(expected_path.stat().st_mtime) if expected_path.exists() else 0
    dispatch_time = int(time.time())
    (state_dir / "pre-mtime").write_text(f"{pre_mtime}\n", encoding="utf-8")
    (state_dir / "timestamp").write_text(f"{dispatch_time}\n", encoding="utf-8")
    (state_dir / "python-interpreter").write_text(
        f"{sys.executable}\n", encoding="utf-8"
    )
    (state_dir / "review-model").write_text(
        f"model={model} source={model_source} effort={effort} effort_source={effort_source}\n",
        encoding="utf-8",
    )

    child_env = build_child_env()
    record_grok_version(executable, state_dir, child_env)

    # First and only stdout line: STATE-DIR <abs-path>
    sys.stdout.write(f"STATE-DIR {state_dir.resolve()}\n")
    sys.stdout.flush()

    snapshot_info = prepare_snapshot(cwd, state_dir)
    if snapshot_info is None:
        sys.stderr.write(
            "dispatch-grok.py: failed to export staged snapshot from Git index\n"
        )
        return EXIT_RUNTIME

    repo_root, snapshot_dir, staged_diff = snapshot_info
    candidate_in_snapshot = snapshot_dir / expected_path.name
    # Remove any index-exported Review-Grok.md in the snapshot so only a file
    # written during this dispatch can qualify as a fresh snapshot candidate.
    if candidate_in_snapshot.exists():
        try:
            candidate_in_snapshot.unlink()
        except OSError:
            pass

    original_prompt = prompt_file.read_text(encoding="utf-8", errors="replace")
    supplied_inputs = collect_supplied_inputs(repo_root, snapshot_dir, original_prompt)
    relay_text = build_relay_prompt(
        prompt_file=prompt_file,
        expected_name=expected_path.name,
        round_number=args.round,
        validation_dir=snapshot_dir,
        staged_diff=staged_diff,
        supplied_inputs=supplied_inputs,
    )
    relay_path = state_dir / "prompt-relay"
    relay_path.write_text(relay_text, encoding="utf-8")

    dispatch_start_ns = time.time_ns()
    stall_watcher = start_stall_watch(state_dir)
    try:
        returncode, timed_out, accumulator, stderr_text = run_grok_stream(
            executable=executable,
            relay_path=relay_path,
            validation_dir=snapshot_dir,
            state_dir=state_dir,
            model=model,
            effort=effort,
            env=child_env,
            timeout_seconds=timeout_seconds,
            grace_seconds=grace_seconds,
        )
    finally:
        stop_stall_watch(stall_watcher, state_dir)

    end_event = accumulator.end_event
    cost_str = format_cost_usd(end_event)
    (state_dir / "cost-usd").write_text(f"{cost_str}\n", encoding="utf-8")

    session_id = (
        end_event.get("sessionId")
        if isinstance(end_event, dict) and isinstance(end_event.get("sessionId"), str)
        else None
    )
    record_actual(state_dir, state_dir / "tail", session_id, model, effort)

    if timed_out:
        sys.stderr.write(
            f"dispatch-grok.py: Grok review timed out after {timeout_seconds:g}s\n"
        )
        return EXIT_RUNTIME

    if accumulator.error_events:
        first_err = accumulator.error_events[0]
        msg = (
            first_err.get("message")
            or first_err.get("error")
            or json.dumps(first_err)
        )
        sys.stderr.write(f"dispatch-grok.py: Grok stream error event: {msg}\n")
        return EXIT_RUNTIME

    if end_event is None:
        sys.stderr.write(
            f"dispatch-grok.py: Grok stream ended without an 'end' event (exit={returncode})\n"
        )
        return EXIT_RUNTIME

    stop_reason = end_event.get("stopReason")
    if stop_reason != "end_turn":
        sys.stderr.write(
            f"dispatch-grok.py: Grok ended with stopReason={stop_reason!r} (expected 'end_turn')\n"
        )
        return EXIT_RUNTIME

    if returncode != 0:
        sys.stderr.write(
            f"dispatch-grok.py: Grok exited with non-zero status {returncode}\n"
        )
        return EXIT_RUNTIME

    if not candidate_in_snapshot.is_file():
        response_text = accumulator.response_text.lstrip("\r\n")
        if response_text and not response_text.endswith("\n"):
            response_text += "\n"
        if response_text:
            atomic_write(candidate_in_snapshot, response_text)

    valid, detail = validate_candidate_review(
        candidate_in_snapshot, args.round, dispatch_start_ns
    )
    if not valid:
        sys.stderr.write(
            f"dispatch-grok.py: postflight validation failed ({detail})\n"
        )
        return EXIT_RUNTIME

    atomic_write(expected_path, detail)
    try:
        current_mtime = int(expected_path.stat().st_mtime)
        min_mtime = max(dispatch_time, pre_mtime + 1 if pre_mtime > 0 else dispatch_time)
        if current_mtime < min_mtime:
            os.utime(expected_path, (min_mtime, min_mtime))
    except OSError:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
