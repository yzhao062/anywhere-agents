#!/usr/bin/env python3
"""await-review.py -- resolve a dispatch whose harness task was reported stopped.

A background-task notification carrying `killed` describes the harness's task
wrapper, not the reviewer. The dispatched reviewer is detached, so it outlives
its wrapper and keeps writing. This script answers the only question Phase 2
needs at that moment: is the round's review published, is the reviewer still
working, or did it provably end. See skills/implement-review/SKILL.md > Phase 1d
for when to run it.

**Silence is never death here.** The round's own timeout is what bounds a
reviewer that has gone quiet; `stall-watch` already treats ten minutes of
silence as a soft signal for the same reason, because long model generation is
silent. Turning a shorter silence into a terminal verdict would rebuild the
false-failure this script exists to remove, under a new name. `DEAD` therefore
requires evidence that the round ended, which today means one thing: a stream
death whose identity-checked reap completed. Everything else is `ALIVE`,
carrying the reason that mattered at that instant, so the caller can escalate to
its own timeout or to a human checkpoint. A published review with the wrong
round marker is reported through that reason and does not end the round, because
a reviewer can publish twice and the second write is the one worth having.

It reads the same state files as health-check.py and applies byte-identical
freshness and round-marker rules, so a REVIEW-READY verdict cannot be
contradicted by that script's state contract or by its Checks 1 to 3. It says
nothing about Checks 4 to 10: size, verification notes, and the commit
verdict stay Phase 2's job, and a REVIEW-READY review can still fail them.
tests/test_await_review.py pins the narrower agreement by running both scripts
over the same fixtures.

Usage:
  await-review.py --state-dir <abs-path> --round <N>
                  [--review-file <path>] [--timeout <s>] [--idle <s>]
                  [--poll <s>] [--quiet-for <s>] [--round-budget <s>]

The round's deadline is stamped once into <state-dir>/round-deadline, at the
round's origin plus --round-budget, and read back by every later call. It is
never rewritten, so a redispatch cannot extend the round and a caller that
lost the dispatching session's context inherits the same bound. The origin is
the earliest readable timestamp in the state directory: the root `timestamp`,
or an earlier `attempt-N/timestamp` when an automatic redispatch archived one
and rewrote the root. --timeout bounds one call; the deadline bounds the
round, and whichever comes first ends the call.

Stdout: exactly one terminal line.
  REVIEW-READY <abs-path>
  ALIVE <state-dir> reason=<reason> tail-idle=<s> tail-size=<bytes>
  DEAD <state-dir> reason=stream-death tail-idle=<s>
  TIMEOUT <state-dir> round-deadline=<epoch>
  REAP-UNKNOWN <state-dir> tail-idle=<s>

Exit code:
  0  REVIEW-READY   the round's review is published and has gone quiet
  3  ALIVE          call budget spent, round still open; run again
  4  DEAD           the round ended without a usable review
  2  checkpoint for a person: the round deadline passed (TIMEOUT), or a stream
     death never produced its reap-completion marker (REAP-UNKNOWN). Neither is
     proof of death and neither may set sticky downgrade.
  1  state-contract failure (same line shape health-check.py reports)

The default timeout stays under the 600-second cap a single foreground tool call
gets, because running this in the foreground is the point: a background probe is
exposed to the same stop it exists to diagnose.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 540.0
DEFAULT_IDLE_SECONDS = 180.0
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_QUIET_SECONDS = 10.0

# The dispatcher waits up to 10 seconds for `stream-reap-complete` before it
# archives and retries. A request older than this grace was abandoned: the reap
# timed out, stall-watch died, or archival failed. Without the bound, one stale
# marker suppresses every terminal verdict for the rest of the round.
RETRY_HANDOFF_GRACE_SECONDS = 30.0

# The round's own bound, matching what auto-watch would have enforced. It is
# written into the state directory on the first call and never rewritten, so a
# reader that lost the dispatching session's context still finds the original
# deadline. Anchoring to `timestamp` alone would not do: the dispatcher rewrites
# it on an automatic redispatch, which would hand the round a fresh hour.
DEFAULT_ROUND_BUDGET_SECONDS = 3600.0
ROUND_DEADLINE_FILE = "round-deadline"
# Bound on how long a reader waits for a concurrent creator to publish.
PUBLICATION_RETRIES = 21
PUBLICATION_RETRY_SECONDS = 0.01


def emit(kind: str, *parts: str) -> None:
    rest = (" " + " ".join(parts)) if parts else ""
    print(f"{kind}{rest}", flush=True)


def note(message: str) -> None:
    sys.stderr.write(f"[await-review] {message}\n")
    sys.stderr.flush()


def read_int_file(path: Path) -> int | None:
    """Read a state file the way health-check.py reads it.

    Byte-identical to `health-check.py:read_int_file`, including the rejection
    of a float spelling. The two scripts have to answer the same question the
    same way; a more permissive reader here would hand Phase 2 a REVIEW-READY
    that the health check then refuses, which is the contradiction this script
    promises not to create.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
        return int(text)
    except (OSError, ValueError):
        return None


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def tail_fingerprint(path: Path) -> tuple[int, float]:
    """Size and mtime together, so a same-length rewrite still reads as growth."""
    try:
        st = path.stat()
    except OSError:
        return (0, 0.0)
    return (st.st_size, st.st_mtime)


def first_line(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    return lines[0].rstrip() if lines else ""


class ReviewState:
    """How the review file stands against this round, at one instant."""

    def __init__(self, verdict: str, mtime: int | None = None) -> None:
        self.verdict = verdict
        self.mtime = mtime

    @property
    def ready(self) -> bool:
        return self.verdict == "ready"

    @property
    def settling(self) -> bool:
        """Fresh and correctly marked, still inside the quiet window."""
        return self.verdict == "settling"

    @property
    def published_wrong_round(self) -> bool:
        """Published for this dispatch, carrying another round's marker.

        Reported, never terminal. A reviewer can publish more than once in a
        round: on 2026-08-21 a research-impact round satisfied its
        watcher at 22:12:13 and rewrote the same file at 22:15:59. Ending the
        round on the first artifact would discard the second, which is the
        harm this whole script exists to prevent.
        """
        return self.verdict == "round-marker-mismatch"


def classify_review(
    review_path: Path,
    round_num: int,
    pre_mtime: int,
    dispatch_time: int,
    quiet_for: float,
    now: float,
) -> ReviewState:
    raw_mtime = file_mtime(review_path)
    if raw_mtime is None:
        return ReviewState("absent")
    # health-check.py truncates with int() before comparing; match it exactly or
    # a review written inside the dispatch's own second is ready here and stale
    # there.
    mtime = int(raw_mtime)
    if not (mtime > dispatch_time and mtime > pre_mtime):
        return ReviewState("stale", mtime)
    if now - raw_mtime < quiet_for:
        return ReviewState("settling", mtime)
    if first_line(review_path) != f"<!-- Round {round_num} -->":
        return ReviewState("round-marker-mismatch", mtime)
    return ReviewState("ready", mtime)


def retry_in_flight(state_dir: Path, now: float) -> bool:
    """True only while the dispatcher's stream-death retry handoff is live.

    The failed attempt's tail is archived and the next attempt has not started
    writing, so an idle tail inside that window says nothing. Outside it, the
    request is abandoned state and must not suppress anything.
    """
    request = state_dir / "stream-retry-request"
    request_mtime = file_mtime(request)
    if request_mtime is None:
        return False
    count = read_int_file(state_dir / "stream-retry-count")
    limit = read_int_file(state_dir / "stream-retry-limit")
    if count is None or limit is None:
        return False
    if count >= limit:
        return False
    return now - request_mtime <= RETRY_HANDOFF_GRACE_SECONDS


def reap_completed(state_dir: Path) -> bool:
    """Stream death observed AND the identity-checked reap finished."""
    return ((state_dir / "stream-death").exists()
            and (state_dir / "stream-reap-complete").exists())


def read_published_deadline(path: Path) -> float | None:
    """Read a deadline another process may still be publishing.

    `open("x")` makes the creation exclusive, but the name is visible before
    the winner writes and closes it. A reader that arrives inside that window
    sees an empty file, and reporting a state-contract failure for it would
    turn a race between two resolvers into a refused round.
    """
    for _ in range(PUBLICATION_RETRIES):
        stored = read_int_file(path)
        if stored is not None:
            return float(stored)
        time.sleep(PUBLICATION_RETRY_SECONDS)
    return None


def round_origin(state_dir: Path, dispatch_time: int) -> int | None:
    """The logical start of the round, which is not always `timestamp`.

    A successful automatic redispatch copies the state directory into
    `attempt-N/` and then rewrites the root `timestamp` for the new attempt.
    Deriving the deadline from the root value after that hands the round the
    time attempt 1 already spent. The archived copies are the earlier origins,
    so the earliest readable timestamp is the one the round started at. An
    archive that exists and cannot be read is a state-contract failure, the
    same as an unreadable root timestamp.
    """
    origin = dispatch_time
    for archived in sorted(state_dir.glob("attempt-*/timestamp")):
        stamped = read_int_file(archived)
        if stamped is None:
            return None
        origin = min(origin, stamped)
    return origin


def resolve_round_deadline(state_dir: Path, dispatch_time: int,
                           budget: float) -> float | None:
    """Read the round's absolute deadline, creating it once if it is absent.

    The first caller stamps `round_origin + budget` into the state directory
    with an exclusive create, and every later caller reads that value back. The
    stamp never moves, so an automatic redispatch, a fresh process, or an agent
    that lost the dispatching session's context all see the same bound. Returns
    None when the file exists and cannot be read as an integer epoch, which the
    caller reports as a state-contract failure rather than papering over with a
    new deadline.
    """
    path = state_dir / ROUND_DEADLINE_FILE
    if not path.exists():
        origin = round_origin(state_dir, dispatch_time)
        if origin is None:
            return None
        try:
            with path.open("x", encoding="utf-8") as handle:
                print(int(origin + budget), file=handle)
        except FileExistsError:
            pass  # another caller won the race; read theirs below
        except OSError:
            # An unwritable state directory is not a reason to refuse the
            # round. The origin is immutable, so a later process in the same
            # round derives the same bound from the same files.
            return float(origin + budget)
    return read_published_deadline(path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--state-dir", required=True, type=Path)
    p.add_argument("--round", required=True, type=int, dest="round_num")
    p.add_argument("--review-file", default=Path("Review-Codex.md"), type=Path)
    p.add_argument("--timeout", default=DEFAULT_TIMEOUT_SECONDS, type=float)
    p.add_argument("--idle", default=DEFAULT_IDLE_SECONDS, type=float,
                   help="seconds of tail silence after which ALIVE reports "
                        "reason=tail-idle-unproven; never terminal on its own")
    p.add_argument("--poll", default=DEFAULT_POLL_SECONDS, type=float)
    p.add_argument("--quiet-for", default=DEFAULT_QUIET_SECONDS, type=float,
                   dest="quiet_for")
    p.add_argument("--round-budget", default=DEFAULT_ROUND_BUDGET_SECONDS,
                   type=float, dest="round_budget",
                   help="seconds from the dispatch timestamp to the round "
                        "deadline, used only when the state directory does not "
                        "already carry one")
    args = p.parse_args(argv)
    for name in ("timeout", "idle", "quiet_for", "round_budget"):
        if getattr(args, name) < 0:
            p.error(f"--{name.replace('_', '-')} must not be negative")
    if args.poll <= 0:
        p.error("--poll must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    state_dir: Path = args.state_dir
    review_path: Path = args.review_file

    if not state_dir.is_dir():
        emit("FAIL", "state-contract", f"state-dir-missing:{state_dir}")
        return 1

    pre_mtime = read_int_file(state_dir / "pre-mtime")
    dispatch_time = read_int_file(state_dir / "timestamp")
    if pre_mtime is None or dispatch_time is None:
        emit("FAIL", "state-contract",
             "missing-or-unreadable "
             f"pre-mtime={(state_dir / 'pre-mtime').exists()} "
             f"timestamp={(state_dir / 'timestamp').exists()}")
        return 1

    round_deadline = resolve_round_deadline(
        state_dir, dispatch_time, args.round_budget)
    if round_deadline is None:
        emit("FAIL", "state-contract",
             f"round-deadline unreadable at {state_dir / ROUND_DEADLINE_FILE}")
        return 1

    tail_file = state_dir / "tail"

    # Seed the idle clock from evidence that predates this probe. A tail quiet
    # since twenty minutes before the probe has to read as twenty minutes idle,
    # or every stalled dispatch looks busy for one full idle window.
    last_print = tail_fingerprint(tail_file)
    tail_mtime = file_mtime(tail_file)
    last_change = tail_mtime if tail_mtime is not None else float(dispatch_time)

    # --timeout bounds this call; the round deadline bounds the round. The
    # loop checks the round deadline first on every pass, so a call started
    # with less than --timeout left still ends at the deadline.
    deadline = time.monotonic() + args.timeout
    settle_reported = False

    while True:
        now = time.time()
        review = classify_review(
            review_path, args.round_num, pre_mtime, dispatch_time,
            args.quiet_for, now,
        )
        if review.ready:
            emit("REVIEW-READY", str(review_path.resolve()))
            return 0

        print_now = tail_fingerprint(tail_file)
        if print_now != last_print:
            last_print = print_now
            last_change = now
        idle = max(0.0, now - last_change)

        if review.settling:
            # A published review outranks the death markers. The final attempt
            # can write its review and then end with the terminal stream
            # suffix, and on 2026-08-21 a reviewer published twice inside one
            # round. Ending here would discard a complete review that is one
            # quiet window from passing the health check.
            if not settle_reported:
                note("review present and correctly marked; waiting out the "
                     f"{args.quiet_for:g}s quiet window")
                settle_reported = True
        elif reap_completed(state_dir) and not retry_in_flight(state_dir, now):
            emit("DEAD", str(state_dir), "reason=stream-death",
                 f"tail-idle={idle:.0f}")
            return 4

        if time.time() >= round_deadline:
            # A person decides from here. The round outlived the bound that
            # auto-watch would have applied, and nothing observed says the
            # reviewer died.
            emit("TIMEOUT", str(state_dir), f"round-deadline={int(round_deadline)}")
            return 2

        death_mtime = file_mtime(state_dir / "stream-death")
        if (death_mtime is not None and not reap_completed(state_dir)
                and not retry_in_flight(state_dir, now)
                and now - death_mtime > RETRY_HANDOFF_GRACE_SECONDS):
            # stall-watch saw the terminal suffix and never confirmed its reap
            # within the handoff grace. Both watchers swallow a failed
            # completion write, so this state can be permanent. Waiting out the
            # full round for a failure that already announced itself buys
            # nothing, and the missing proof is not a death certificate either,
            # so this is a checkpoint rather than a verdict.
            emit("REAP-UNKNOWN", str(state_dir), f"tail-idle={idle:.0f}")
            return 2

        if time.monotonic() >= deadline:
            if review.published_wrong_round:
                reason = "round-marker-mismatch"
            elif idle >= args.idle:
                reason = "tail-idle-unproven"
            else:
                reason = "working"
            emit("ALIVE", str(state_dir), f"reason={reason}",
                 f"tail-idle={idle:.0f}", f"tail-size={print_now[0]}")
            return 3

        time.sleep(min(args.poll,
                       max(0.0, deadline - time.monotonic()),
                       max(0.0, round_deadline - time.time())))


if __name__ == "__main__":
    raise SystemExit(main())
