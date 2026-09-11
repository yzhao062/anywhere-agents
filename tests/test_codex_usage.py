#!/usr/bin/env python3
"""Regression tests for Codex usage rendering in the user-level quota scripts.

Covers two schema changes. After OpenAI removed the fixed 5h window on
2026-07-12, the rollout `payload.rate_limits` reports `primary` as the
weekly window (window_minutes 10080) with `secondary` null and a `credits`
block. Both scripts derive the window label from window_minutes and gate
credits null-safely.

The later change tags each snapshot with `limit_id` / `limit_name`. A
session running a per-model bucket writes that bucket instead of the
account plan, and such a bucket sits near 0% used, so the naive "last
rate_limits in the newest file" read rendered full quota off a Spark meter
while the plan was 58% spent. Both scripts now prefer the plan meter and
label a side meter when it is all that exists. Selection also moved off
file mtime, which NTFS defers while a session holds its rollout open.

Round 1 and Round 2 review added the rest. mtime still bounds which rollouts are
opened and the plan meter still outranks a newer unrecognized bucket, so
the chosen snapshot can be old; both renderers therefore show its age, and
the cases below pin the two shapes that produce a stale reading. Every
selection case asserts both renderers: the two scripts carry separate
copies of the selection helpers, and a mutation restoring mtime selection
in agent-quota alone passed the whole suite while the row went back to
reporting the stale reading.

Round 3 replaced glob plus getmtime with an os.scandir walk, so
discovery gained its own cases: what it finds, and the three things it
declines to fail on (an unreadable directory, a symlinked one, an entry
that disappears between the listing and the stat).

Round 2 removed the staleness threshold the first pass added. Hiding the
age below an hour left the reported shape reproducible with a snapshot
fifteen minutes old, so the age is now unconditional and the cases below
pin it across the range. A record stamped ahead of the clock is refused
rather than clamped, because clamping made the stalest available reading
render as the freshest.

These tests pin all of it against later drift.

Scope: `scripts/statusline.py` (Claude Code statusLine) and
`scripts/agent-quota.py` (standalone readout). ac-local test; check-parity
guarantees the anywhere-agents copies are byte-identical, so pinning the ac
copy pins both.
"""
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, REPO / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


statusline = _load("statusline.py", "cx_statusline")
agent_quota = _load("agent-quota.py", "cx_agent_quota")

# Both scripts define the same label helper (statusline: public name,
# agent-quota: underscore-prefixed).
LABEL_FNS = (statusline.codex_window_label, agent_quota._codex_window_label)


def _window(pct, minutes):
    return {"used_percent": float(pct), "window_minutes": minutes,
            "resets_at": time.time() + 600000}


def _ts(seconds_ago=0):
    """A rollout timestamp in the producer's exact spelling, aged from now.

    Relative rather than fixed so the age the renderers compute is the age
    the case intends, whatever day the suite runs on."""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                         time.gmtime(time.time() - seconds_ago))


def _fake_home(tmp):
    """expanduser replacement rooting "~" at a throwaway directory.

    Both scripts locate rollouts via os.path.expanduser("~"). Setting $HOME
    only redirects that on POSIX; Windows expanduser reads USERPROFILE.
    Patching os.path.expanduser is platform-independent, so the same test
    exercises the real glob/parse path on Linux, macOS, and the Windows CI
    lane alike."""
    real = os.path.expanduser

    def fake(path):
        if path == "~":
            return tmp
        if path.startswith("~" + os.sep) or path.startswith("~/"):
            return os.path.join(tmp, path[2:])
        return real(path)

    return fake


def _render_rollouts(specs):
    """Run both renderers against synthetic rollouts, returning
    (statusline_segment, agent_quota_row).

    Each spec is (filename, mtime, [(timestamp, rate_limits), ...]). Records
    are written in the given order, so the last one is the file's newest.
    mtime is set explicitly because the point of several of these tests is
    that file mtime and record recency disagree."""
    with tempfile.TemporaryDirectory() as tmp:
        sess = pathlib.Path(tmp) / ".codex" / "sessions" / "s"
        sess.mkdir(parents=True, exist_ok=True)
        for name, mtime, records in specs:
            f = sess / name
            f.write_text("".join(
                json.dumps({"timestamp": ts, "payload": {"rate_limits": rl}}) + "\n"
                for ts, rl in records
            ), encoding="utf-8")
            os.utime(f, (mtime, mtime))
        with mock.patch("os.path.expanduser", _fake_home(tmp)):
            return statusline.codex_segment(), agent_quota.codex_row()


def _touch(path, mtime, content="{}\n"):
    path.write_text(content, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


class _VanishedEntry:
    """A DirEntry-alike for a file removed between the listing and the stat.

    Codex writes this tree while it is being read, so the race is ordinary
    rather than exotic; there is no portable way to provoke it for real."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)

    def is_dir(self, follow_symlinks=True):
        return False

    def is_file(self, follow_symlinks=True):
        return True

    def stat(self, follow_symlinks=True):
        raise FileNotFoundError(self.path)


class _SymlinkedDirEntry:
    """A DirEntry-alike for a symlink pointing at a directory: is_dir answers
    True only for a caller willing to follow the link."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)

    def is_dir(self, follow_symlinks=True):
        return bool(follow_symlinks)

    def is_file(self, follow_symlinks=True):
        return False

    def stat(self, follow_symlinks=True):
        raise AssertionError("a linked directory should not have been stat'd")


class _Listing:
    """The context manager os.scandir returns, over a fixed entry list."""

    def __init__(self, entries):
        self._entries = entries

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *exc):
        return False


def _render_raw(files):
    """Run both renderers against rollouts written as literal text.

    Each entry is (filename, mtime, raw). _render_rollouts always emits
    well-formed lines, which is the wrong tool for asking what the tail
    reader does with a half-written one."""
    with tempfile.TemporaryDirectory() as tmp:
        sess = pathlib.Path(tmp) / ".codex" / "sessions" / "s"
        sess.mkdir(parents=True, exist_ok=True)
        for name, mtime, raw in files:
            f = sess / name
            f.write_text(raw, encoding="utf-8")
            os.utime(f, (mtime, mtime))
        with mock.patch("os.path.expanduser", _fake_home(tmp)):
            return statusline.codex_segment(), agent_quota.codex_row()


def _render(rate_limits):
    """Single untagged rollout, the pre-limit_id shape."""
    with tempfile.TemporaryDirectory() as tmp:
        sess = pathlib.Path(tmp) / ".codex" / "sessions" / "s"
        sess.mkdir(parents=True, exist_ok=True)
        (sess / "rollout-1.jsonl").write_text(
            json.dumps({"payload": {"rate_limits": rate_limits}}) + "\n"
        )
        with mock.patch("os.path.expanduser", _fake_home(tmp)):
            return statusline.codex_segment(), agent_quota.codex_row()


PLAN = {"primary": _window(58, 10080), "limit_id": "codex", "limit_name": None}
PLAN_UNUSED = {"primary": _window(0, 10080), "limit_id": "codex", "limit_name": None}
SPARK = {"primary": _window(0, 300), "secondary": _window(0, 10080),
         "limit_id": "codex_bengalfox", "limit_name": "GPT-5.3-Codex-Spark"}
# A hypothetical renamed plan bucket: the id this code does not recognize.
RENAMED = {"primary": _window(58, 10080), "limit_id": "codex_v2", "limit_name": None}


class TestWindowLabel(unittest.TestCase):
    def test_matrix(self):
        for fn in LABEL_FNS:
            self.assertEqual(fn({"window_minutes": 300}), "5h")
            self.assertEqual(fn({"window_minutes": 10080}), "7d")
            self.assertEqual(fn({"window_minutes": 1440}), "1d")
            self.assertEqual(fn({"window_minutes": 60}), "1h")
            self.assertEqual(fn({"window_minutes": 90}), "90m")
            self.assertEqual(fn({"window_minutes": 20160}), "14d")

    def test_missing_or_zero(self):
        for fn in LABEL_FNS:
            self.assertEqual(fn({}), "")
            self.assertEqual(fn({"window_minutes": 0}), "")
            self.assertEqual(fn({"window_minutes": None}), "")


class TestRender(unittest.TestCase):
    def test_weekly_only(self):
        # Current shape: primary is weekly, secondary null, zero credits.
        seg, row = _render({
            "primary": _window(13, 10080),
            "secondary": None,
            "credits": {"has_credits": False, "balance": "0"},
        })
        self.assertIn("7d 87%", seg)      # 100 - 13
        self.assertNotIn("5h", seg)       # not mislabeled
        self.assertNotIn("cr", seg)       # zero balance, credits off -> hidden
        self.assertIn("7d 87%", row)
        self.assertNotIn("5h", row)
        self.assertNotIn("credits", row)

    def test_dual_window_when_5h_restored(self):
        seg, row = _render({
            "primary": _window(50, 300),
            "secondary": _window(20, 10080),
            "credits": {"has_credits": False, "balance": "0"},
        })
        self.assertIn("5h 50%", seg)
        self.assertIn("7d 80%", seg)
        self.assertIn("5h 50%", row)
        self.assertIn("7d 80%", row)

    def test_credits_nonzero_shown(self):
        seg, row = _render({
            "primary": _window(0, 10080),
            "credits": {"has_credits": True, "balance": "42"},
        })
        self.assertIn("cr 42", seg)
        self.assertIn("credits 42", row)

    def test_has_credits_null_balance_is_null_safe(self):
        # The Medium finding: has_credits opens the branch, but balance may
        # be absent. Must render the tag alone, never "cr None"/"credits None".
        seg, row = _render({
            "primary": _window(0, 10080),
            "credits": {"has_credits": True, "balance": None},
        })
        self.assertNotIn("None", seg)
        self.assertNotIn("None", row)
        # The tag stands alone as its own segment. It no longer ends the
        # string: this rollout carries no timestamp, so the age qualifier
        # trails it.
        self.assertIn("cr", seg.split(" · "), seg)
        self.assertIn("credits", row)

    def test_no_windows_no_credits(self):
        # rate_limits present but empty -> no broken row.
        seg, row = _render({"primary": None, "secondary": None,
                            "credits": {"has_credits": False, "balance": "0"}})
        self.assertIsNone(seg)
        self.assertIn("(no windows)", row)


class TestMeterLabel(unittest.TestCase):
    LABEL_FNS = (statusline.codex_meter_label, agent_quota._codex_meter_label)
    MAIN_FNS = (statusline.codex_is_main_meter, agent_quota._codex_is_main_meter)

    def test_plan_meter_has_no_label(self):
        for fn in self.LABEL_FNS:
            self.assertEqual(fn(PLAN), "")
            self.assertEqual(fn({}), "")                       # pre-tag rollout
            self.assertEqual(fn({"limit_id": ""}), "")

    def test_side_meter_uses_trailing_segment(self):
        for fn in self.LABEL_FNS:
            self.assertEqual(fn(SPARK), "Spark")
            self.assertEqual(
                fn({"limit_id": "x", "limit_name": "GPT-9-Codex-Ember"}), "Ember")
            # No dash to split on: fall back to the whole name, then the id.
            self.assertEqual(fn({"limit_id": "x", "limit_name": "Ember"}), "Ember")
            self.assertEqual(fn({"limit_id": "codex_bengalfox"}), "codex_bengalfox")

    def test_main_meter_predicate(self):
        for fn in self.MAIN_FNS:
            self.assertTrue(fn(PLAN))
            self.assertTrue(fn({}))                            # pre-tag rollout
            self.assertTrue(fn({"limit_id": None}))
            self.assertFalse(fn(SPARK))


class TestMeterSelection(unittest.TestCase):
    def test_plan_meter_wins_over_fresher_side_meter(self):
        # The reported bug: a session switched to Spark, whose bucket is near
        # 0% used, and its rollout was both newest by mtime and newest by
        # record timestamp. Rendering it showed full quota off a plan that
        # was 58% spent.
        seg, row = _render_rollouts([
            ("rollout-plan.jsonl", 1000, [(_ts(150), PLAN)]),
            ("rollout-spark.jsonl", 9000, [(_ts(60), SPARK)]),
        ])
        self.assertIn("7d 42%", seg)          # 100 - 58, the plan meter
        self.assertNotIn("100%", seg)
        self.assertNotIn("Spark", seg)
        self.assertNotIn("5h", seg)           # Spark's 5h window is not borrowed
        self.assertIn("7d 42% left", row)
        self.assertNotIn("100% left", row)
        self.assertNotIn("Spark", row)

    def test_side_meter_labeled_when_it_is_the_only_one(self):
        seg, row = _render_rollouts([
            ("rollout-spark.jsonl", 9000, [(_ts(60), SPARK)]),
        ])
        self.assertIn("[Spark]", seg)
        self.assertIn("5h 100%", seg)
        self.assertIn("[Spark]", row)
        self.assertIn("5h 100% left", row)
        # The tag replaces the model name so the row cannot read as the plan.
        self.assertNotIn("gpt-", row)

    def test_plan_meter_found_behind_a_side_meter_in_one_file(self):
        # A single session that switched models mid-run writes both meters;
        # the side meter is last. The plan reading is still the one to show.
        seg, row = _render_rollouts([
            ("rollout-1.jsonl", 9000, [
                (_ts(9000), PLAN),
                (_ts(60), SPARK),
            ]),
        ])
        self.assertIn("7d 42%", seg)
        self.assertNotIn("Spark", seg)
        self.assertIn("7d 42% left", row)
        self.assertNotIn("Spark", row)

    def test_record_timestamp_beats_file_mtime(self):
        # NTFS defers the mtime update while a session holds its rollout
        # open, so a finished session can look newer than a running one.
        stale = dict(PLAN, primary=_window(90, 10080))
        fresh = dict(PLAN, primary=_window(58, 10080))
        seg, row = _render_rollouts([
            ("rollout-stale.jsonl", 9000, [(_ts(600), stale)]),
            ("rollout-fresh.jsonl", 1000, [(_ts(60), fresh)]),
        ])
        self.assertIn("7d 42%", seg)
        self.assertNotIn("7d 10%", seg)
        self.assertIn("7d 42% left", row)
        self.assertNotIn("7d 10% left", row)

    def test_scan_is_bounded(self):
        # Only MAX_ROLLOUT_SCAN rollouts are opened. A plan reading buried
        # past that bound is not reached, so the visible side meter is what
        # renders, labeled rather than silently passed off as the plan.
        bound = statusline.MAX_ROLLOUT_SCAN
        specs = [("rollout-plan.jsonl", 1000, [(_ts(60), PLAN)])]
        specs += [("rollout-s%02d.jsonl" % i, 9000 + i, [(_ts(120), SPARK)])
                  for i in range(bound)]
        seg, row = _render_rollouts(specs)
        self.assertIn("[Spark]", seg)
        self.assertIn("[Spark]", row)
        self.assertEqual(bound, agent_quota.MAX_ROLLOUT_SCAN)

    def test_stale_plan_beyond_the_bound_renders_but_is_age_qualified(self):
        # Round 1 R1. The live plan reading sits at mtime rank 13 and is not
        # opened; the bound holds twelve finished rollouts whose plan
        # readings are hours old and near zero used. The 100% that renders
        # is real but not current, and both renderers say so.
        bound = statusline.MAX_ROLLOUT_SCAN
        specs = [("rollout-live.jsonl", 1000, [(_ts(60), PLAN)])]
        specs += [("rollout-old%02d.jsonl" % i, 9000 + i,
                   [(_ts(6 * 3600), PLAN_UNUSED)]) for i in range(bound)]
        seg, row = _render_rollouts(specs)
        self.assertIn("7d 100%", seg)
        self.assertIn("6h ago", seg)          # the qualifier that makes it readable
        self.assertNotIn("7d 42%", seg)
        self.assertIn("7d 100% left", row)
        self.assertIn("6h0m ago", row)

    def test_stale_plan_beyond_the_bound_is_qualified_below_an_hour_too(self):
        # Round 2 reopened R1 with this shape. The first pass only qualified
        # a reading past an hour, so at fifteen minutes the 100% rendered
        # bare again. The excluded rank-13 record is itself the proof that
        # prompts ran in those fifteen minutes.
        bound = statusline.MAX_ROLLOUT_SCAN
        specs = [("rollout-live.jsonl", 1000, [(_ts(150), PLAN)])]
        specs += [("rollout-old%02d.jsonl" % i, 9000 + i,
                   [(_ts(15 * 60), PLAN_UNUSED)]) for i in range(bound)]
        seg, row = _render_rollouts(specs)
        self.assertIn("7d 100%", seg)
        self.assertIn("15m ago", seg)
        self.assertIn("15m ago", row)

    def test_unrecognized_bucket_never_displaces_a_plan_reading(self):
        # Round 1 R1, second shape. If the plan bucket is ever renamed, an
        # older recognized reading still wins, because pinning the plan is
        # the point. The age qualifier is what keeps that from reading as
        # current, and no label appears since the winner is a plan reading.
        seg, row = _render_rollouts([
            ("rollout-old.jsonl", 1000, [(_ts(6 * 3600), PLAN_UNUSED)]),
            ("rollout-new.jsonl", 9000, [(_ts(60), RENAMED)]),
        ])
        self.assertIn("7d 100%", seg)
        self.assertIn("6h ago", seg)
        self.assertNotIn("codex_v2", seg)
        self.assertIn("7d 100% left", row)
        self.assertIn("6h0m ago", row)
        self.assertNotIn("codex_v2", row)

    def test_unrecognized_bucket_case_is_qualified_below_an_hour_too(self):
        # The fifteen-minute sibling of the case above, for the same reason.
        seg, row = _render_rollouts([
            ("rollout-old.jsonl", 1000, [(_ts(15 * 60), PLAN_UNUSED)]),
            ("rollout-new.jsonl", 9000, [(_ts(150), RENAMED)]),
        ])
        self.assertIn("7d 100%", seg)
        self.assertIn("15m ago", seg)
        self.assertIn("15m ago", row)

    def test_fallback_selection_also_goes_by_record_timestamp(self):
        # Round 2 R5. With no plan reading anywhere the fallback decides, and
        # it has to compare timestamps for the same reason the plan branch
        # does. Replacing only agent-quota's fallback update with
        # first-by-mtime passed the whole Round 1 suite.
        stale = dict(SPARK, primary=_window(90, 300), secondary=None)
        fresh = dict(SPARK, primary=_window(58, 300), secondary=None)
        seg, row = _render_rollouts([
            ("rollout-stale.jsonl", 9000, [(_ts(900), stale)]),
            ("rollout-fresh.jsonl", 1000, [(_ts(150), fresh)]),
        ])
        self.assertIn("5h 42%", seg)
        self.assertNotIn("5h 10%", seg)
        self.assertIn("2m ago", seg)
        self.assertIn("5h 42% left", row)
        self.assertNotIn("5h 10% left", row)
        self.assertIn("2m ago", row)

    def test_unrecognized_bucket_renders_labeled_when_alone(self):
        seg, row = _render_rollouts([
            ("rollout-new.jsonl", 9000, [(_ts(60), RENAMED)]),
        ])
        self.assertIn("[codex_v2]", seg)
        self.assertIn("7d 42%", seg)
        self.assertIn("[codex_v2]", row)


class TestSnapshotAge(unittest.TestCase):
    """The age is unconditional. Round 1 hid it below an hour, which left the
    reported shape reproducible with a fifteen-minute snapshot."""

    def test_age_is_shown_for_a_fresh_reading(self):
        seg, row = _render_rollouts([("rollout-1.jsonl", 9000, [(_ts(150), PLAN)])])
        self.assertIn("2m ago", seg)
        self.assertIn("2m ago", row)

    def test_age_is_shown_for_an_old_reading(self):
        seg, row = _render_rollouts([("rollout-1.jsonl", 9000, [(_ts(6 * 3600), PLAN)])])
        self.assertIn("6h ago", seg)
        self.assertIn("6h0m ago", row)

    @mock.patch("time.time", return_value=1788906000.0)
    def test_no_threshold_hides_the_age(self, _clock):
        # Spans the hour the removed cutoff sat on. Every one of these
        # renders an age; none of them is silent.
        #
        # The clock is frozen because _ts writes whole seconds while the
        # renderer reads the clock again afterwards. Crossing one second
        # boundary between the two puts the 3599s case at 3600s, where the
        # renderer correctly says "1h ago" and the assertion below does not.
        for secs, want in ((150, "2m ago"), (15 * 60, "15m ago"),
                           (3599, "59m ago"), (3600, "1h ago"),
                           (3601, "1h ago"), (2 * 86400, "2d ago")):
            seg, _ = _render_rollouts(
                [("rollout-1.jsonl", 9000, [(_ts(secs), PLAN)])])
            self.assertIn(want, seg, "age %ds rendered %r" % (secs, seg))

    def test_future_timestamp_is_refused_not_clamped(self):
        # Round 2 R4. A record stamped ahead of the clock still wins
        # selection, and clamping its age to zero made the stalest available
        # reading render as the freshest one.
        seg, row = _render_rollouts([
            ("rollout-future.jsonl", 9000, [(_ts(-2 * 3600), PLAN_UNUSED)]),
            ("rollout-real.jsonl", 1000, [(_ts(150), PLAN)]),
        ])
        self.assertIn("7d 100%", seg)      # it does win selection
        self.assertIn("age ?", seg)        # but it cannot claim to be fresh
        self.assertNotIn("ago", seg)
        self.assertIn("100% left", row)
        self.assertIn("[?]", row)
        self.assertNotIn("just now", row)

    def test_clock_jitter_inside_tolerance_is_absorbed(self):
        tol = statusline.CODEX_FUTURE_TOLERANCE_SECONDS
        self.assertEqual(tol, agent_quota.CODEX_FUTURE_TOLERANCE_SECONDS)
        seg, row = _render_rollouts(
            [("rollout-1.jsonl", 9000, [(_ts(-(tol - 20)), PLAN)])])
        self.assertIn("just now", seg)
        self.assertNotIn("age ?", seg)
        self.assertIn("just now", row)

    def test_absent_timestamp_falls_back_to_file_mtime(self):
        seg, row = _render_rollouts([("rollout-1.jsonl", 9000, [("", PLAN)])])
        self.assertIn("age ?", seg)        # statusline keeps no path to fall back on
        self.assertIn("ago", row)          # agent-quota ages off the mtime
        self.assertNotIn("[?]", row)

    def test_unparseable_timestamp_is_stated_not_hidden(self):
        seg, row = _render_rollouts(
            [("rollout-1.jsonl", 9000, [("not-a-timestamp", PLAN)])])
        self.assertIn("age ?", seg)
        self.assertIn("[?]", row)

    def test_epoch_helper(self):
        self.assertIsNone(agent_quota._epoch(""))
        self.assertIsNone(agent_quota._epoch(None))
        self.assertIsNone(agent_quota._epoch("not-a-timestamp"))
        self.assertAlmostEqual(
            agent_quota._epoch("2026-09-08T22:09:46.537Z"), 1788905386.537, places=2)

    def test_age_formatters_agree_on_wording(self):
        self.assertEqual(statusline.fmt_age(30), "just now")
        self.assertEqual(statusline.fmt_age(90), "1m ago")
        self.assertEqual(statusline.fmt_age(3600), "1h ago")
        self.assertEqual(statusline.fmt_age(6 * 3600), "6h ago")
        self.assertEqual(statusline.fmt_age(2 * 86400), "2d ago")
        # The row keeps the minutes the status line drops; nothing else differs.
        self.assertEqual(agent_quota._fmt_age(30), "just now")
        self.assertEqual(agent_quota._fmt_age(90), "1m ago")
        self.assertEqual(agent_quota._fmt_age(6 * 3600), "6h0m ago")
        self.assertEqual(agent_quota._fmt_age(2 * 86400), "2d ago")


class TestTailReader(unittest.TestCase):
    """A rollout is appended to while it is read, so the last line can be
    half-written. Round 1 verified these shapes by probe; they are pinned
    here so the guarantee survives a rewrite of the tail reader."""

    def _record(self, seconds_ago, rl):
        return json.dumps({"timestamp": _ts(seconds_ago),
                           "payload": {"rate_limits": rl}})

    def test_truncated_final_record_is_skipped(self):
        good = self._record(60, PLAN)
        seg, row = _render_raw([("rollout-1.jsonl", 9000, good + "\n" + good[:-20])])
        self.assertIn("7d 42%", seg)
        self.assertIn("7d 42% left", row)

    def test_final_record_without_trailing_newline_is_read(self):
        seg, row = _render_raw([("rollout-1.jsonl", 9000, self._record(60, PLAN))])
        self.assertIn("7d 42%", seg)
        self.assertIn("7d 42% left", row)

    def test_tail_of_only_fragments_yields_nothing(self):
        seg, row = _render_raw([("rollout-1.jsonl", 9000, self._record(60, PLAN)[:-20])])
        self.assertIsNone(seg)
        self.assertIn("no rate_limits", row)

    def test_repeated_ids_keep_the_newest_in_the_file(self):
        stale = dict(PLAN, primary=_window(90, 10080))
        raw = self._record(600, stale) + "\n" + self._record(60, PLAN) + "\n"
        seg, row = _render_raw([("rollout-1.jsonl", 9000, raw)])
        self.assertIn("7d 42%", seg)
        self.assertNotIn("7d 10%", seg)
        self.assertIn("7d 42% left", row)


class TestRolloutDiscovery(unittest.TestCase):
    """The os.scandir walk that replaced glob plus getmtime."""

    def _sessions(self, tmp):
        return pathlib.Path(tmp) / ".codex" / "sessions"

    def _both(self):
        return statusline.codex_rollouts(), agent_quota._codex_rollouts()

    def test_finds_rollouts_at_every_depth_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._sessions(tmp)
            (sess / "2026" / "09" / "07").mkdir(parents=True)
            _touch(sess / "rollout-flat.jsonl", 3000)
            _touch(sess / "2026" / "rollout-mid.jsonl", 6000)
            _touch(sess / "2026" / "09" / "07" / "rollout-deep.jsonl", 9000)
            with mock.patch("os.path.expanduser", _fake_home(tmp)):
                sl_found, aq_found = self._both()
        self.assertEqual([os.path.basename(f) for f in sl_found],
                         ["rollout-deep.jsonl", "rollout-mid.jsonl",
                          "rollout-flat.jsonl"])
        self.assertEqual(sl_found, aq_found)

    def test_ignores_what_only_looks_like_a_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._sessions(tmp)
            sess.mkdir(parents=True)
            _touch(sess / "rollout-1.jsonl", 5000)
            _touch(sess / "rollout-1.jsonl.tmp", 9000)
            _touch(sess / "notes.jsonl", 9000)
            _touch(sess / "rollout-2.json", 9000)
            (sess / "rollout-dir.jsonl").mkdir()      # a directory, not a file
            with mock.patch("os.path.expanduser", _fake_home(tmp)):
                sl_found, aq_found = self._both()
        self.assertEqual([os.path.basename(f) for f in sl_found], ["rollout-1.jsonl"])
        self.assertEqual(sl_found, aq_found)

    def test_missing_sessions_directory_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("os.path.expanduser", _fake_home(tmp)):
                sl_found, aq_found = self._both()
        self.assertEqual(sl_found, [])
        self.assertEqual(aq_found, [])

    def test_unreadable_directory_is_skipped_not_raised(self):
        # One session folder the user cannot read is no reason to lose the
        # readout on every prompt.
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._sessions(tmp)
            (sess / "ok").mkdir(parents=True)
            (sess / "locked").mkdir(parents=True)
            _touch(sess / "ok" / "rollout-visible.jsonl", 5000)
            _touch(sess / "locked" / "rollout-hidden.jsonl", 9000)
            real_scandir = os.scandir

            def fake(path):
                if os.path.basename(str(path)) == "locked":
                    raise PermissionError(str(path))
                return real_scandir(path)

            with mock.patch("os.path.expanduser", _fake_home(tmp)), \
                    mock.patch("os.scandir", fake):
                sl_found, aq_found = self._both()
        self.assertEqual([os.path.basename(f) for f in sl_found],
                         ["rollout-visible.jsonl"])
        self.assertEqual(sl_found, aq_found)

    def test_entry_that_vanishes_before_the_stat_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._sessions(tmp)
            sess.mkdir(parents=True)
            _touch(sess / "rollout-real.jsonl", 5000)
            real_scandir = os.scandir

            def fake(path):
                with real_scandir(path) as listing:
                    entries = list(listing)
                entries.append(_VanishedEntry(
                    os.path.join(str(path), "rollout-gone.jsonl")))
                return _Listing(entries)

            with mock.patch("os.path.expanduser", _fake_home(tmp)), \
                    mock.patch("os.scandir", fake):
                sl_found, aq_found = self._both()
        self.assertEqual([os.path.basename(f) for f in sl_found],
                         ["rollout-real.jsonl"])
        self.assertEqual(sl_found, aq_found)

    def test_symlinked_directory_is_not_followed(self):
        # The link points at the tree that contains it, so descending would
        # not terminate. glob's `**` did follow directory symlinks.
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._sessions(tmp)
            (sess / "real").mkdir(parents=True)
            _touch(sess / "real" / "rollout-1.jsonl", 5000)
            try:
                (sess / "loop").symlink_to(sess, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not permitted here")
            with mock.patch("os.path.expanduser", _fake_home(tmp)):
                sl_found, aq_found = self._both()
        self.assertEqual([os.path.basename(f) for f in sl_found], ["rollout-1.jsonl"])
        self.assertEqual(sl_found, aq_found)

    def test_directory_symlink_is_identified_without_following_it(self):
        # The real-symlink case above skips wherever links cannot be created.
        # This one runs everywhere: it pins that the walk asks
        # is_dir(follow_symlinks=False), which is what keeps a linked
        # directory off the stack. The stand-in is re-appended on every
        # listing, so following it would not terminate.
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._sessions(tmp)
            sess.mkdir(parents=True)
            _touch(sess / "rollout-1.jsonl", 5000)
            real_scandir = os.scandir
            visited = []

            def fake(path):
                visited.append(str(path))
                with real_scandir(path) as listing:
                    entries = list(listing)
                entries.append(_SymlinkedDirEntry(
                    os.path.join(str(path), "loop")))
                return _Listing(entries)

            with mock.patch("os.path.expanduser", _fake_home(tmp)),                     mock.patch("os.scandir", fake):
                sl_found, aq_found = self._both()
        self.assertEqual([os.path.basename(f) for f in sl_found], ["rollout-1.jsonl"])
        self.assertEqual(sl_found, aq_found)
        self.assertEqual(visited, [str(sess), str(sess)])   # one scan per script

    def test_discovery_takes_the_mtime_from_the_entry_not_the_path(self):
        # The whole point of the walk. Ordering alone cannot see the
        # difference, so a copy reverted to os.path.getmtime(entry.path)
        # passed the entire suite; this forbids the call outright.
        with tempfile.TemporaryDirectory() as tmp:
            sess = self._sessions(tmp)
            sess.mkdir(parents=True)
            for i, mtime in enumerate((5000, 9000, 1000)):
                _touch(sess / ("rollout-%d.jsonl" % i), mtime)

            def forbidden(path):
                raise AssertionError("discovery stat'd by path: %s" % path)

            with mock.patch("os.path.expanduser", _fake_home(tmp)),                     mock.patch("os.path.getmtime", forbidden):
                sl_found, aq_found = self._both()
            self.assertEqual([os.path.basename(f) for f in sl_found],
                             ["rollout-1.jsonl", "rollout-0.jsonl",
                              "rollout-2.jsonl"])
            self.assertEqual(sl_found, aq_found)
            # Outside the patch: the order it produced is the order a fresh
            # stat agrees with.
            self.assertEqual([os.path.getmtime(f) for f in sl_found],
                             [9000, 5000, 1000])


class TestNoRollouts(unittest.TestCase):
    def test_empty_sessions_dir(self):
        seg, row = _render_rollouts([])
        self.assertIsNone(seg)
        self.assertIn("no session rollout found", row)


def _agy_payload(five=0.75, weekly=0.40, third_five=0.90, third_weekly=0.80):
    reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 7200))
    return {
        "status": "SUCCESS",
        "num_turns": 0,
        "usage": {"total_tokens": 0},
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {
                                "id": "gemini-weekly",
                                "window": "weekly",
                                "remaining_fraction": weekly,
                                "reset_time": reset,
                            },
                            {
                                "id": "gemini-5h",
                                "window": "5h",
                                "remaining_fraction": five,
                                "reset_time": reset,
                            },
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "buckets": [
                            {
                                "id": "3p-weekly",
                                "window": "weekly",
                                "remaining_fraction": third_weekly,
                            },
                            {
                                "id": "3p-5h",
                                "window": "5h",
                                "remaining_fraction": third_five,
                            },
                        ],
                    },
                ]
            },
        },
    }


class TestAgyQuota(unittest.TestCase):
    def _paths(self, tmp):
        cache = os.path.join(tmp, "agy-quota-cache.json")
        return cache, cache + ".last-attempt", cache + ".refresh.lock"

    def test_missing_agy_binary_is_named_and_starts_no_refresh(self):
        # Spark (no agy installed) rendered "(refresh started)" forever: the
        # helper exited 1 in the background and nothing said why.
        with tempfile.TemporaryDirectory() as tmp:
            cache, attempt, lock = self._paths(tmp)
            with mock.patch.object(agent_quota, "AGY_QUOTA_CACHE", cache),                     mock.patch.object(agent_quota, "AGY_QUOTA_ATTEMPT", attempt),                     mock.patch.object(agent_quota, "AGY_QUOTA_LOCK", lock),                     mock.patch.object(agent_quota, "_agy_binary", return_value=None),                     mock.patch.object(agent_quota, "_start_agy_refresh") as start,                     mock.patch.object(agent_quota, "claude_row", return_value="Claude"),                     mock.patch.object(agent_quota, "codex_row", return_value="Codex"),                     mock.patch.object(agent_quota.sys, "argv", ["agent-quota.py"]),                     mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(agent_quota.main(), 0)
        start.assert_not_called()
        self.assertIn("agy not installed", out.getvalue())
        self.assertNotIn("refresh started", out.getvalue())

    def test_detailed_quota_shows_both_agy_pools_but_statusline_stays_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, attempt, lock = self._paths(tmp)
            result = subprocess.CompletedProcess(
                [], 0, json.dumps(_agy_payload()), ""
            )
            with mock.patch.object(agent_quota, "AGY_QUOTA_CACHE", cache), \
                    mock.patch.object(agent_quota, "AGY_QUOTA_ATTEMPT", attempt), \
                    mock.patch.object(agent_quota, "AGY_QUOTA_LOCK", lock), \
                    mock.patch.object(agent_quota, "_agy_binary", return_value="agy"), \
                    mock.patch.object(
                        agent_quota.subprocess, "run", return_value=result
                    ) as run:
                self.assertEqual(agent_quota._refresh_agy_cache(), 0)
                row = agent_quota.agy_row()
            with mock.patch.object(statusline, "AGY_QUOTA_CACHE", cache):
                segment = statusline.agy_segment()

        self.assertIn("Agy", row)
        self.assertIn("Gemini", row)
        self.assertIn("5h 75% left", row)
        self.assertIn("7d 40% left", row)
        self.assertIn("Claude/GPT", row)
        self.assertIn("5h 90% left", row)
        self.assertIn("7d 80% left", row)
        self.assertIn("Agy Gemini", segment)
        self.assertIn("5h 75%", segment)
        self.assertIn("7d 40%", segment)
        self.assertNotIn("90%", segment)
        self.assertNotIn("80%", segment)
        self.assertEqual(run.call_args.kwargs["cwd"], os.path.expanduser("~"))
        if os.name == "nt":
            self.assertEqual(
                run.call_args.kwargs["creationflags"],
                getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    def test_failed_refresh_is_throttled_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, attempt, lock = self._paths(tmp)
            with mock.patch.object(agent_quota, "AGY_QUOTA_CACHE", cache), \
                    mock.patch.object(agent_quota, "AGY_QUOTA_ATTEMPT", attempt), \
                    mock.patch.object(agent_quota, "AGY_QUOTA_LOCK", lock), \
                    mock.patch.object(agent_quota, "_agy_binary", return_value="agy"), \
                    mock.patch.object(
                        agent_quota.subprocess,
                        "run",
                        side_effect=subprocess.TimeoutExpired("agy", 5),
                    ) as run:
                self.assertEqual(agent_quota._refresh_agy_cache(), 1)
                self.assertEqual(agent_quota._refresh_agy_cache(), 0)
                self.assertEqual(run.call_count, 1)
                self.assertTrue(os.path.isfile(attempt))
                self.assertFalse(os.path.exists(lock))

    def test_fresh_cache_does_not_start_background_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, attempt, _ = self._paths(tmp)
            pathlib.Path(cache).write_text("{}", encoding="utf-8")
            with mock.patch.object(statusline, "AGY_QUOTA_CACHE", cache), \
                    mock.patch.object(statusline, "AGY_QUOTA_ATTEMPT", attempt), \
                    mock.patch.object(statusline.subprocess, "Popen") as popen:
                self.assertFalse(statusline.start_agy_refresh())
                popen.assert_not_called()

    def test_compact_statusline_removes_padding_but_keeps_resets_and_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, _, _ = self._paths(tmp)
            payload = _agy_payload()
            pathlib.Path(cache).write_text(
                json.dumps(
                    {
                        "cached_at": time.time() - 120,
                        "plan_tier": "ULTRA",
                        "usage": payload["command"]["data"],
                    }
                ),
                encoding="utf-8",
            )
            claude = statusline.claude_segment(
                {
                    "model": {"display_name": "Opus 5 (1M context)"},
                    "rate_limits": {
                        "five_hour": {"used_percentage": 18},
                        "seven_day": {"used_percentage": 62},
                    },
                },
                compact=True,
            )
            with mock.patch.object(statusline, "AGY_QUOTA_CACHE", cache):
                agy = statusline.agy_segment(compact=True)

        self.assertEqual(claude, "cc 5h82% 7d38%")
        self.assertIn("agy 5h75% (", agy)
        self.assertIn("7d40% (", agy)
        self.assertIn("@2m", agy)
        self.assertNotIn("ULTRA", agy)
        self.assertNotIn(" · ", agy)
        self.assertNotIn("AgyG", agy)

    def test_compact_expired_window_uses_named_reset_marker(self):
        shown = statusline.fmt_window_compact(
            {"used_percentage": 10, "resets_at": time.time() - 1},
            statusline.CLAUDE_PCT_FIELD,
        )
        self.assertEqual(shown, "90% (reset)")

    def test_compact_main_joins_pools_with_pipes_and_cc_label(self):
        """main() is the only place the pool separator is assembled; the
        segment functions never insert it themselves, so this pins the
        join directly instead of re-deriving it from the pieces."""
        with tempfile.TemporaryDirectory() as tmp:
            cache, _, _ = self._paths(tmp)
            payload = _agy_payload()
            pathlib.Path(cache).write_text(
                json.dumps(
                    {
                        "cached_at": time.time() - 120,
                        "plan_tier": "ULTRA",
                        "usage": payload["command"]["data"],
                    }
                ),
                encoding="utf-8",
            )
            stdin = io.StringIO(json.dumps({
                "model": {"display_name": "Opus 5 (1M context)"},
                "rate_limits": {
                    "five_hour": {"used_percentage": 18},
                    "seven_day": {"used_percentage": 62},
                },
            }))
            out = io.StringIO()
            # A fresh, patched home has no ~/.codex/sessions, so codex_segment
            # yields nothing here and the line carries only the cc and agy
            # pools; that still proves the " | " join, and persist_claude /
            # start_agy_refresh are stubbed so the run touches no real disk
            # state or subprocess.
            with mock.patch("os.path.expanduser", _fake_home(tmp)), \
                    mock.patch.object(statusline, "AGY_QUOTA_CACHE", cache), \
                    mock.patch.object(statusline, "persist_claude"), \
                    mock.patch.object(statusline, "start_agy_refresh"), \
                    mock.patch("sys.stdin", stdin), \
                    mock.patch("sys.stdout", out):
                statusline.main()
            line = out.getvalue().rstrip("\n")

        self.assertTrue(line.startswith("cc "))
        self.assertEqual(line.count(" | "), 1)
        self.assertIn(" | agy ", line)


if __name__ == "__main__":
    unittest.main()
