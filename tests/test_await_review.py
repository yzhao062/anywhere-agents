"""Tests for await-review.py.

The script exists because a harness task-notification carrying `killed` says
nothing about the dispatched reviewer: it is detached and outlives its wrapper.
Measured on 2026-08-21 across two repositories, six such notifications all
arrived while the reviewer was still writing, and every one of those rounds
published its review afterwards. Phase 2 therefore resolves the round from
state-dir evidence rather than from the task status.

Two properties carry the design and get the most attention below.

**Silence is not death.** A quiet tail proves nothing; `stall-watch` treats ten
minutes of it as a soft signal for the same reason. If idleness were terminal
here, the script would rebuild the false failure it exists to remove. `DEAD`
needs a stream death whose identity-checked reap completed. A published review
carrying the wrong round marker is reported and left to the round's timeout,
because a reviewer can publish twice.

**REVIEW-READY may not contradict the health check's state contract or its
Checks 1 to 3.** It says nothing about Checks 4 to 10, which stay Phase 2's job.
The integration tests at the bottom run both scripts over one fixture set,
because two hand-copied rule sets drifted apart once already: await-review
compared float mtimes while health-check truncated them, so a review written
inside the dispatch's own second was ready to one and stale to the other.
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "implement-review" / "scripts"
AWAIT_PY = SCRIPTS_DIR / "await-review.py"
HEALTH_PY = SCRIPTS_DIR / "health-check.py"

ROUND = 3
MARKER = f"<!-- Round {ROUND} -->"


def load_module():
    """Import the script by path without leaving a __pycache__ beside it.

    skills/implement-review/ is compared recursively and byte-for-byte between
    the two repositories, so a bytecode directory written there by running the
    tests reports as parity drift on a clean checkout.
    """
    spec = importlib.util.spec_from_file_location("await_review", AWAIT_PY)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


await_review = load_module()


def set_mtime(path: Path, when: float) -> None:
    os.utime(path, (when, when))


class AwaitReviewCase(unittest.TestCase):
    """Fixture: one dispatch state dir plus a review path beside it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.state_dir = base / "state"
        self.state_dir.mkdir()
        self.review = base / "Review-Codex.md"
        # The dispatch happened two minutes ago; any review that predates it
        # belongs to an earlier round.
        self.dispatch_time = time.time() - 120
        self.pre_mtime = self.dispatch_time - 60
        (self.state_dir / "timestamp").write_text(
            str(int(self.dispatch_time)), encoding="utf-8")
        (self.state_dir / "pre-mtime").write_text(
            str(int(self.pre_mtime)), encoding="utf-8")
        self.tail = self.state_dir / "tail"

    # -- helpers -------------------------------------------------------
    def write_tail(self, text: str = "codex output\n", age: float = 0.0) -> None:
        self.tail.write_text(text, encoding="utf-8")
        set_mtime(self.tail, time.time() - age)

    def write_review(self, marker: str = MARKER, age: float = 60.0,
                     body: str = "\n\n# Review\n\nfindings\n") -> None:
        self.review.write_text(marker + body, encoding="utf-8")
        set_mtime(self.review, time.time() - age)

    def mark(self, name: str, age: float = 0.0, body: str = "1\n") -> Path:
        p = self.state_dir / name
        p.write_text(body, encoding="utf-8")
        set_mtime(p, time.time() - age)
        return p

    def run_await(self, **overrides) -> tuple[int, str]:
        argv = [
            "--state-dir", str(overrides.pop("state_dir", self.state_dir)),
            "--round", str(overrides.pop("round_num", ROUND)),
            "--review-file", str(overrides.pop("review_file", self.review)),
            "--timeout", str(overrides.pop("timeout", 0)),
            "--idle", str(overrides.pop("idle", 60)),
            "--poll", str(overrides.pop("poll", 0.05)),
            "--quiet-for", str(overrides.pop("quiet_for", 10)),
            "--round-budget", str(overrides.pop("round_budget", 3600)),
        ]
        self.assertEqual(overrides, {}, "unknown override passed to run_await")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = await_review.main(argv)
        return rc, buf.getvalue().strip()


class TestReviewReady(AwaitReviewCase):
    def test_fresh_marked_and_quiet_review_is_ready(self):
        self.write_tail(age=600)
        self.write_review(age=60)
        rc, out = self.run_await()
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith("REVIEW-READY "), out)
        self.assertIn("Review-Codex.md", out)

    def test_idle_tail_does_not_beat_a_ready_review(self):
        """A finished reviewer stops writing; that must not read as death."""
        self.write_tail(age=3600)
        self.write_review(age=60)
        rc, out = self.run_await(idle=1)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith("REVIEW-READY "), out)

    def test_settling_becomes_ready_inside_one_invocation(self):
        """The transition has to happen in the loop, not between two calls.

        Ageing the file between two snapshot calls proves only that two
        instants classify differently. This ages it from another thread while a
        single invocation is polling, so a loop that never re-read the review
        would time out at ALIVE instead.
        """
        self.write_tail(age=3600)
        self.write_review(age=0)
        target = self.review

        def age_it():
            time.sleep(0.4)
            set_mtime(target, time.time() - 30)

        ager = threading.Thread(target=age_it, daemon=True)
        ager.start()
        try:
            rc, out = self.run_await(timeout=6, idle=1, poll=0.05, quiet_for=20)
        finally:
            ager.join(timeout=5)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith("REVIEW-READY "), out)


class TestSilenceIsNotDeath(AwaitReviewCase):
    """The property the whole script exists to preserve."""

    def test_absent_review_with_idle_tail_is_alive_not_dead(self):
        self.write_tail(age=3600)
        rc, out = self.run_await(idle=60)
        self.assertEqual(rc, 3, out)
        self.assertIn("reason=tail-idle-unproven", out)

    def test_stale_review_with_idle_tail_is_alive_not_dead(self):
        """A review left over from an earlier round is not this round's."""
        self.write_tail(age=3600)
        self.write_review(age=300)  # older than the dispatch timestamp
        rc, out = self.run_await(idle=60)
        self.assertEqual(rc, 3, out)
        self.assertIn("reason=tail-idle-unproven", out)

    def test_stream_death_without_a_completed_reap_is_never_dead(self):
        """stall-watch writes stream-death first and reaps second.

        Between those two writes the worker tree may still be running, so the
        marker alone cannot end the round. Both watchers swallow a failed
        completion write, so this state can also be permanent; it surfaces as a
        checkpoint for a person rather than as a verdict or an hour of silence.
        """
        self.write_tail(age=3600)
        self.mark("stream-death", age=600, body="STREAM-DEATH\n")
        rc, out = self.run_await(idle=1)
        self.assertEqual(rc, 2, out)
        self.assertTrue(out.startswith("REAP-UNKNOWN "), out)

    def test_a_fresh_stream_death_is_inside_the_handoff_grace(self):
        """The reap runs right after the marker; give it that long before a
        checkpoint, and never turn the wait itself into a verdict."""
        self.write_tail(age=0)
        self.mark("stream-death", age=0, body="STREAM-DEATH\n")
        rc, out = self.run_await(idle=60)
        self.assertEqual(rc, 3, out)
        self.assertIn("reason=working", out)

    def test_working_reason_below_the_idle_threshold(self):
        self.write_tail(age=1)
        rc, out = self.run_await(idle=60)
        self.assertEqual(rc, 3, out)
        self.assertIn("reason=working", out)

    def test_missing_tail_early_in_a_dispatch_is_alive(self):
        """No tail yet: the idle clock falls back to the dispatch timestamp."""
        rc, out = self.run_await(idle=300)
        self.assertEqual(rc, 3, out)


class TestDead(AwaitReviewCase):
    def test_completed_reap_is_terminal(self):
        self.write_tail(age=600)
        self.mark("stream-death", body="STREAM-DEATH\n")
        self.mark("stream-reap-complete")
        rc, out = self.run_await(idle=60)
        self.assertEqual(rc, 4, out)
        self.assertIn("reason=stream-death", out)

    def test_completed_reap_is_terminal_even_on_a_busy_tail(self):
        """The reap proves the worker tree is gone; idleness is not consulted."""
        self.write_tail(age=0)
        self.mark("stream-death", body="STREAM-DEATH\n")
        self.mark("stream-reap-complete")
        rc, out = self.run_await(idle=600)
        self.assertEqual(rc, 4, out)

    def test_a_settling_review_outranks_the_death_markers(self):
        """The last attempt can publish and then die on the terminal suffix.

        Both markers exist, the retry budget is spent, and a complete review is
        sitting inside its quiet window. Returning DEAD here would discard it
        and licence a sticky downgrade, which is the exact stopped-task harm in
        a new disguise.
        """
        self.write_tail(age=600)
        self.write_review(age=0)
        self.mark("stream-death", body="STREAM-DEATH\n")
        self.mark("stream-reap-complete")
        rc, out = self.run_await(idle=1, quiet_for=30)
        self.assertEqual(rc, 3, out)
        set_mtime(self.review, time.time() - 31)
        rc, out = self.run_await(idle=1, quiet_for=30)
        self.assertEqual(rc, 0, out)

    def test_a_live_retry_handoff_holds_the_verdict_open(self):
        """Between the request and the reap, the round is still in play."""
        self.write_tail(age=600)
        self.mark("stream-death", body="STREAM-DEATH\n")
        self.mark("stream-reap-complete")
        self.mark("stream-retry-request", age=0)
        self.mark("stream-retry-count", body="0\n")
        self.mark("stream-retry-limit", body="1\n")
        rc, out = self.run_await(idle=1)
        self.assertEqual(rc, 3, out)

    def test_an_abandoned_retry_request_stops_suppressing_the_verdict(self):
        """The dispatcher waits 10s for the reap marker, then gives up.

        A request left behind by a timed-out handoff, a dead stall-watch, or a
        failed archival keeps `count < limit` forever. Without the age bound it
        would suppress every terminal verdict for the rest of the round.
        """
        self.write_tail(age=600)
        self.mark("stream-death", body="STREAM-DEATH\n")
        self.mark("stream-reap-complete")
        self.mark("stream-retry-request", age=900)
        self.mark("stream-retry-count", body="0\n")
        self.mark("stream-retry-limit", body="1\n")
        rc, out = self.run_await(idle=1)
        self.assertEqual(rc, 4, out)

    def test_unreadable_retry_markers_do_not_suppress_the_verdict(self):
        self.write_tail(age=600)
        self.mark("stream-death", body="STREAM-DEATH\n")
        self.mark("stream-reap-complete")
        self.mark("stream-retry-request", age=0)
        self.mark("stream-retry-count", body="not-a-number\n")
        self.mark("stream-retry-limit", body="1\n")
        rc, out = self.run_await(idle=1)
        self.assertEqual(rc, 4, out)

    def test_a_published_review_with_the_wrong_round_marker_is_reported_only(self):
        """Reported through the ALIVE reason, never terminal.

        A reviewer can publish more than once in a round: on 2026-08-21 a
        research-impact round satisfied its watcher at 22:12:13 and rewrote
        the same file at 22:15:59. Ending the round on the first artifact would
        discard the second, which is the harm this script exists to prevent.
        """
        self.write_tail(age=1)
        self.write_review(marker="<!-- Round 2 -->", age=60)
        rc, out = self.run_await(idle=600)
        self.assertEqual(rc, 3, out)
        self.assertIn("reason=round-marker-mismatch", out)

    def test_a_wrong_marker_inside_the_quiet_window_reports_working(self):
        """A file still being written must not be judged on its first line."""
        self.write_tail(age=1)
        self.write_review(marker="<!-- Round 2 -->", age=0)
        rc, out = self.run_await(idle=600, quiet_for=30)
        self.assertEqual(rc, 3, out)
        self.assertIn("reason=working", out)


class TestLiveness(AwaitReviewCase):
    def test_idle_is_seeded_from_the_tail_not_from_probe_start(self):
        """A tail quiet since before the probe reads as idle immediately.

        Seeding at probe start would report `working` for one full idle window
        on a dispatch that has been silent for fifteen minutes.
        """
        self.write_tail(age=900)
        rc, out = self.run_await(idle=300)
        self.assertEqual(rc, 3, out)
        self.assertIn("reason=tail-idle-unproven", out)
        idle = int(out.split("tail-idle=")[1].split()[0])
        self.assertGreaterEqual(idle, 890, out)

    def test_growth_during_the_probe_resets_the_idle_clock(self):
        self.write_tail(age=0)
        stop = threading.Event()

        def grow():
            n = 0
            while not stop.is_set():
                n += 1
                with self.tail.open("a", encoding="utf-8") as fh:
                    fh.write(f"line {n}\n")
                time.sleep(0.05)

        writer = threading.Thread(target=grow, daemon=True)
        writer.start()
        self.addCleanup(stop.set)
        try:
            rc, out = self.run_await(timeout=1.0, idle=0.3, poll=0.05)
        finally:
            stop.set()
            writer.join(timeout=5)
        self.assertEqual(rc, 3, out)
        self.assertIn("reason=working", out)

    def test_a_same_length_rewrite_counts_as_progress(self):
        """Size alone misses a tail that is replaced rather than appended to.

        The dispatcher archives an attempt and starts another one in the same
        directory, and a reviewer that rewrites a line in place changes no byte
        count at all.
        """
        self.write_tail(text="aaaa\n", age=900)
        stop = threading.Event()

        def rewrite():
            time.sleep(0.3)
            while not stop.is_set():
                self.tail.write_text("bbbb\n", encoding="utf-8")
                time.sleep(0.05)

        writer = threading.Thread(target=rewrite, daemon=True)
        writer.start()
        self.addCleanup(stop.set)
        try:
            rc, out = self.run_await(timeout=1.5, idle=0.2, poll=0.05)
        finally:
            stop.set()
            writer.join(timeout=5)
        self.assertEqual(rc, 3, out)
        self.assertIn("reason=working", out)


class TestRoundDeadline(AwaitReviewCase):
    """The bound has to outlive the session that started the round.

    A stop takes the wrapper that held the dispatch timeout, and usually the
    watcher's timer with it. If the deadline lives only in the calling agent's
    context, a compaction or a handoff leaves ALIVE looping with nothing to end
    it, which was the Round 2 finding. The state directory is the one thing a
    cold reader still has.
    """

    def deadline_file(self) -> Path:
        return self.state_dir / "round-deadline"

    def test_the_deadline_is_stamped_on_first_use(self):
        self.write_tail(age=1)
        self.run_await(idle=60)
        self.assertTrue(self.deadline_file().is_file())
        stored = int(self.deadline_file().read_text(encoding="utf-8").strip())
        self.assertEqual(stored, int(self.dispatch_time) + 3600)

    def test_a_redispatch_cannot_extend_it(self):
        """The dispatcher rewrites `timestamp` on an automatic retry.

        Deriving the bound from that file every call would hand the round a
        fresh hour per retry, which is why the stamp is written once and read
        back afterwards. The second call gets a newer timestamp and a larger
        budget, and has to keep acting on the bound already stamped.
        """
        self.write_tail(age=1)
        rc, first = self.run_await(idle=60, round_budget=1)
        self.assertEqual(rc, 2, first)
        stamped = self.deadline_file().read_text(encoding="utf-8").strip()
        (self.state_dir / "timestamp").write_text(
            str(int(time.time())), encoding="utf-8")
        rc, out = self.run_await(idle=60, round_budget=3600)
        self.assertEqual(rc, 2, out)
        self.assertIn(f"round-deadline={stamped}", out)
        self.assertEqual(self.deadline_file().read_text(encoding="utf-8").strip(),
                         stamped)

    def test_a_first_call_after_a_retry_stamps_the_original_origin(self):
        """The dispatcher archives attempt 1, then rewrites the root timestamp.

        Nothing forces a resolver to have run before that happens. A first
        call that lands afterwards and reads only the root value hands the
        round the time attempt 1 already spent, which is the same unbounded
        wait in slower motion.
        """
        original = int(self.dispatch_time)
        attempt = self.state_dir / "attempt-1"
        attempt.mkdir()
        (attempt / "timestamp").write_text(str(original), encoding="utf-8")
        (self.state_dir / "timestamp").write_text(
            str(original + 900), encoding="utf-8")
        self.write_tail(age=1)
        self.run_await(idle=60)
        stored = int(self.deadline_file().read_text(encoding="utf-8").strip())
        self.assertEqual(stored, original + 3600)

    def test_an_unreadable_archived_origin_is_a_contract_failure(self):
        """Never guess past a corrupt archive: the root value is later."""
        attempt = self.state_dir / "attempt-1"
        attempt.mkdir()
        (attempt / "timestamp").write_text("not-an-epoch", encoding="utf-8")
        self.write_tail(age=1)
        rc, out = self.run_await(idle=60)
        self.assertEqual(rc, 1, out)
        self.assertIn("state-contract", out)

    def test_a_reader_waits_out_a_concurrent_publication(self):
        """`open("x")` publishes the name before the value.

        A second resolver that reads inside that window sees an empty file.
        Refusing the round for it turns a harmless race into a state-contract
        failure, so the reader gives the winner a moment to finish.
        """
        self.deadline_file().write_text(
            str(int(time.time()) + 1800), encoding="utf-8")
        real_read = await_review.read_int_file
        unpublished = {"reads": 2}

        def mid_publication(path: Path):
            """Two empty reads, the way a reader inside the window sees it."""
            if path.name == "round-deadline" and unpublished["reads"]:
                unpublished["reads"] -= 1
                return None
            return real_read(path)

        await_review.read_int_file = mid_publication
        self.addCleanup(setattr, await_review, "read_int_file", real_read)
        self.write_tail(age=1)
        rc, out = self.run_await(idle=60)
        self.assertEqual(unpublished["reads"], 0, "the empty reads never happened")
        self.assertEqual(rc, 3, out)
        self.assertNotIn("state-contract", out)

    def test_an_exclusive_create_loser_reads_the_winners_deadline(self):
        """The other half of the race: this caller creates second and loses.

        Two resolvers can see different archive sets or be invoked with
        different budgets, so the loser has to adopt the stored value rather
        than the candidate it computed. Precreating the file exercises the
        reader path instead; only a failed exclusive create reaches this one.
        """
        deadline = self.deadline_file()
        winner = int(self.dispatch_time) + 123
        real_open = Path.open

        def lose_create(path: Path, mode="r", *args, **kwargs):
            if path == deadline and mode == "x":
                with real_open(path, "w", encoding="utf-8") as handle:
                    handle.write(f"{winner}\n")
                raise FileExistsError
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "open", new=lose_create):
            resolved = await_review.resolve_round_deadline(
                self.state_dir, int(self.dispatch_time), 3600)
        self.assertEqual(resolved, float(winner))

    def test_a_cold_reader_inherits_the_original_bound(self):
        """No caller state: the deadline comes from the directory alone."""
        self.deadline_file().write_text(str(int(time.time()) - 5), encoding="utf-8")
        self.write_tail(age=1)
        rc, out = self.run_await(idle=60)
        self.assertEqual(rc, 2, out)
        self.assertTrue(out.startswith("TIMEOUT "), out)

    def test_a_passed_deadline_is_a_checkpoint_not_a_verdict(self):
        self.deadline_file().write_text(str(int(time.time()) - 1), encoding="utf-8")
        self.write_tail(age=3600)
        rc, out = self.run_await(idle=1)
        self.assertEqual(rc, 2, out)
        self.assertNotIn("DEAD", out)

    def test_a_call_ends_at_the_round_deadline_not_at_its_own_timeout(self):
        """A generous --timeout cannot buy time the round no longer has.

        The loop reads the deadline on every pass and caps its sleep against
        it, so the checkpoint arrives when the round ends rather than when
        this call would have given up. The poll here is longer than the
        assertion window on purpose: with a short one, a run that ignored the
        deadline while sleeping would still wake in time to look busy.
        """
        self.deadline_file().write_text(str(int(time.time()) + 1), encoding="utf-8")
        self.write_tail(age=1)
        started = time.time()
        rc, out = self.run_await(timeout=30, idle=60, poll=30)
        elapsed = time.time() - started
        self.assertEqual(rc, 2, out)
        self.assertTrue(out.startswith("TIMEOUT "), out)
        self.assertLess(elapsed, 15, f"ran {elapsed:.1f}s past the deadline")

    def test_an_unreadable_deadline_is_a_contract_failure(self):
        """Never invent a replacement: a bad bound must be seen, not reset."""
        self.deadline_file().write_text("not-an-epoch", encoding="utf-8")
        self.write_tail(age=1)
        rc, out = self.run_await(idle=60)
        self.assertEqual(rc, 1, out)
        self.assertIn("state-contract", out)


class TestStateContract(AwaitReviewCase):
    def test_missing_state_dir_is_a_contract_failure(self):
        missing = self.state_dir.parent / "absent"
        rc, out = self.run_await(state_dir=missing)
        self.assertEqual(rc, 1, out)
        self.assertIn("state-contract", out)

    def test_missing_pre_mtime_is_a_contract_failure(self):
        (self.state_dir / "pre-mtime").unlink()
        self.write_tail(age=1)
        rc, out = self.run_await()
        self.assertEqual(rc, 1, out)
        self.assertIn("state-contract", out)


class TestCommandLine(unittest.TestCase):
    """The module is invoked as a script, so exercise the real entry point."""

    def test_help_runs_under_the_current_interpreter(self):
        proc = subprocess.run(
            [sys.executable, str(AWAIT_PY), "--help"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--state-dir", proc.stdout)

    def test_missing_required_flag_exits_two(self):
        proc = subprocess.run(
            [sys.executable, str(AWAIT_PY), "--round", "1"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 2, proc.stdout)

    def test_non_positive_poll_is_rejected(self):
        proc = subprocess.run(
            [sys.executable, str(AWAIT_PY), "--state-dir", ".",
             "--round", "1", "--poll", "0"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 2, proc.stdout)


class TestAgreesWithHealthChecks1To3(AwaitReviewCase):
    """Run both scripts over one fixture set and require the same answer.

    The promise is bounded: await-review tells Phase 2 that a REVIEW-READY
    satisfies the state contract and Health checks 1 to 3. Size, verification
    notes, and the verdict-verification contract are Checks 4 to 10 and remain
    Phase 2's job, so a REVIEW-READY review can still fail them.

    The bounded promise was false once, when the two scripts parsed and compared
    the state files differently. Prose cannot hold it; only a test that executes
    both can.
    """

    def health_check(self) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(HEALTH_PY),
             "--state-dir", str(self.state_dir),
             "--round", str(ROUND),
             "--review-file", str(self.review)],
            capture_output=True, text=True, timeout=120,
        )
        return proc.returncode, proc.stdout

    def assert_ready_survives_health_check(self):
        rc, out = self.run_await()
        self.assertEqual(rc, 0, f"await said: {out}")
        hrc, hout = self.health_check()
        self.assertNotIn("FAIL check-1", hout)
        self.assertNotIn("FAIL check-2", hout)
        self.assertNotIn("FAIL check-3", hout)
        self.assertNotIn("FAIL state-contract", hout)

    def test_a_comfortably_fresh_review_agrees(self):
        self.write_tail(age=600)
        self.write_review(age=60)
        self.assert_ready_survives_health_check()

    def test_a_review_written_in_the_dispatch_second_agrees(self):
        """The exact divergence Round 1 of the review found.

        health-check truncates the review mtime with int() before comparing,
        so a review landing inside the dispatch's own second is not fresh
        there. Comparing untruncated floats here called it ready.
        """
        self.write_tail(age=600)
        self.write_review(age=60)
        # Inside the dispatch's own integer second: the state file holds the
        # truncated value, so the fraction has to be added to that, not to the
        # unrounded time the fixture started from.
        set_mtime(self.review, float(int(self.dispatch_time)) + 0.75)
        rc, out = self.run_await()
        self.assertEqual(rc, 3, f"await must not call this ready: {out}")
        _, hout = self.health_check()
        self.assertIn("FAIL check-2", hout)

    def test_float_epoch_state_files_are_rejected_by_both(self):
        """SKILL.md requires integer Unix epoch state files.

        Both readers reject a fractional spelling, and the producers write
        integers, so this pins the two readers to the same refusal.
        """
        (self.state_dir / "timestamp").write_text(
            f"{self.dispatch_time:.3f}", encoding="utf-8")
        (self.state_dir / "pre-mtime").write_text(
            f"{self.pre_mtime:.3f}", encoding="utf-8")
        self.write_tail(age=600)
        self.write_review(age=60)
        rc, out = self.run_await()
        self.assertEqual(rc, 1, out)
        self.assertIn("state-contract", out)
        hrc, hout = self.health_check()
        self.assertEqual(hrc, 1, hout)
        self.assertIn("FAIL state-contract", hout)

    def test_a_stale_review_is_refused_by_both(self):
        self.write_tail(age=600)
        self.write_review(age=300)
        rc, out = self.run_await(idle=60)
        self.assertEqual(rc, 3, out)
        _, hout = self.health_check()
        self.assertIn("FAIL check-2", hout)

    def test_a_wrong_round_marker_is_refused_by_both(self):
        """await-review declines to call it ready; health-check names why."""
        self.write_tail(age=600)
        self.write_review(marker="<!-- Round 2 -->", age=60)
        rc, out = self.run_await(idle=60)
        self.assertEqual(rc, 3, out)
        self.assertIn("reason=round-marker-mismatch", out)
        _, hout = self.health_check()
        self.assertIn("FAIL check-3", hout)


if __name__ == "__main__":
    unittest.main()
