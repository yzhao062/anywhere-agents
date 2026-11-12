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
import sys
import tempfile
import threading
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

    def test_decimal_balance_is_rounded(self):
        # Codex reports the balance as a ten-place decimal string.
        seg, row = _render({
            "primary": _window(0, 10080),
            "credits": {"has_credits": True, "balance": "62500.0000000000"},
        })
        self.assertIn("cr 62,500", seg)
        self.assertIn("credits 62,500", row)
        self.assertNotIn("0000", seg)
        self.assertNotIn("0000", row)

    def test_decimal_zero_balance_hidden(self):
        # "0.0000000000" is zero; it must not pass as nonzero against "0".
        seg, row = _render({
            "primary": _window(13, 10080),
            "credits": {"has_credits": False, "balance": "0.0000000000"},
        })
        self.assertNotIn("cr", seg)
        self.assertNotIn("credits", row)

    def test_no_windows_no_credits(self):
        # rate_limits present but empty -> no broken row.
        seg, row = _render({"primary": None, "secondary": None,
                            "credits": {"has_credits": False, "balance": "0"}})
        self.assertIsNone(seg)
        self.assertIn("(no windows)", row)


class TestFmtCredits(unittest.TestCase):
    def test_compact_statusline(self):
        fn = statusline.fmt_credits
        self.assertEqual(fn("62500.0000000000", compact=True), "62.5k")
        self.assertEqual(fn("1000", compact=True), "1k")
        self.assertEqual(fn("1500000", compact=True), "1.5M")
        self.assertEqual(fn("42.5000000000", compact=True), "42.5")
        self.assertEqual(fn("42", compact=True), "42")

    def test_full_row(self):
        for fn in (statusline.fmt_credits, agent_quota._fmt_credits):
            self.assertEqual(fn("62500.0000000000"), "62,500")
            self.assertEqual(fn("1234.5600000000"), "1,234.56")
            self.assertEqual(fn("42"), "42")

    def test_unparseable_shown_as_given(self):
        for fn in (statusline.fmt_credits, agent_quota._fmt_credits):
            self.assertEqual(fn("unlimited"), "unlimited")
            self.assertEqual(fn("nan"), "nan")

    def test_nonzero(self):
        for fn in (statusline.credits_nonzero, agent_quota._credits_nonzero):
            self.assertFalse(fn(None))
            self.assertFalse(fn(""))
            self.assertFalse(fn("0"))
            self.assertFalse(fn("0.0000000000"))
            self.assertTrue(fn("62500.0000000000"))
            self.assertTrue(fn("unlimited"))


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
    def setUp(self):
        for mod in (statusline, agent_quota):
            patcher = mock.patch.object(mod, "_grok_binary", return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

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


class TestClaudeWindows(unittest.TestCase):
    """A verified Ultra account has no 5h limit, so `five_hour` is missing from
    the statusLine stdin and null in the usage response. Rendering a dash for
    it read as a broken meter, so an absent window is left out of both
    readouts, and a window that returns shows again."""

    WEEK = {"used_percentage": 7}
    FIVE = {"used_percentage": 18}

    def _row(self, rate_limits):
        with tempfile.TemporaryDirectory() as tmp:
            cache = pathlib.Path(tmp) / "rate-limits-cache.json"
            cache.write_text(
                json.dumps({"model": "Opus 5.5", "rate_limits": rate_limits,
                            "ts": time.time()}),
                encoding="utf-8",
            )
            with mock.patch.object(agent_quota, "CLAUDE_RL_CACHE", str(cache)):
                return agent_quota.claude_row()

    def _segments(self, rate_limits):
        data = {"model": {"display_name": "Opus 5.5"}, "rate_limits": rate_limits}
        return (statusline.claude_segment(data, compact=True),
                statusline.claude_segment(data))

    def test_absent_five_hour_is_left_out(self):
        for rate_limits in ({"seven_day": self.WEEK},
                            {"five_hour": None, "seven_day": self.WEEK},
                            {"five_hour": {}, "seven_day": self.WEEK}):
            compact, full = self._segments(rate_limits)
            row = self._row(rate_limits)
            self.assertEqual(compact, "cc 7d93%")
            self.assertEqual(full, "🤖 Opus 5.5 · 7d 93%")
            self.assertIn("7d 93% left", row)
            self.assertNotIn("5h", row)
            self.assertNotIn("—", row)

    def test_absent_seven_day_is_left_out(self):
        rate_limits = {"five_hour": self.FIVE}
        compact, full = self._segments(rate_limits)
        row = self._row(rate_limits)
        self.assertEqual(compact, "cc 5h82%")
        self.assertEqual(full, "🤖 Opus 5.5 · 5h 82%")
        self.assertIn("5h 82% left", row)
        self.assertNotIn("7d", row)

    def test_zero_usage_keeps_its_meter(self):
        """A window at 0% used is present, so it must not be mistaken for an
        absent one."""
        rate_limits = {"five_hour": {"used_percentage": 0},
                       "seven_day": {"used_percentage": 0}}
        compact, full = self._segments(rate_limits)
        row = self._row(rate_limits)
        self.assertEqual(compact, "cc 5h100% 7d100%")
        self.assertEqual(full, "🤖 Opus 5.5 · 5h 100% · 7d 100%")
        self.assertIn("5h 100% left", row)
        self.assertIn("7d 100% left", row)

    def test_both_windows_keep_their_order_and_row_layout(self):
        rate_limits = {"five_hour": self.FIVE, "seven_day": self.WEEK}
        compact, full = self._segments(rate_limits)
        self.assertEqual(compact, "cc 5h82% 7d93%")
        self.assertEqual(full, "🤖 Opus 5.5 · 5h 82% · 7d 93%")
        self.assertIn(f"5h {'82% left':<20}  7d {'93% left':<20}  [",
                      self._row(rate_limits))

    def test_no_window_still_renders_a_placeholder(self):
        """Before the first API response and in an API-key session, no window
        exists at all; the segment stays visible rather than collapsing."""
        for rate_limits in ({}, None, {"five_hour": None, "seven_day": None}):
            compact, full = self._segments(rate_limits)
            self.assertEqual(compact, "cc —")
            self.assertEqual(full, "🤖 Opus 5.5 · —")
            self.assertIn("(no windows)", self._row(rate_limits))


class TestGrokUsage(unittest.TestCase):
    """Tests for Grok 7-day local list-price usage in statusline and agent-quota."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.grok_home = os.path.join(self.tmp, ".grok")
        self.grok_cache = os.path.join(self.tmp, ".claude", "grok-usage-cache.json")
        self.agy_cache = os.path.join(self.tmp, ".claude", "agy-quota-cache.json")
        self.claude_cache = os.path.join(self.tmp, ".claude", "rate-limits-cache.json")

        p_home = mock.patch("os.path.expanduser", _fake_home(self.tmp))
        p_home.start()
        self.addCleanup(p_home.stop)

        env_overrides = {
            "GROK_HOME": self.grok_home,
            "GROK_USAGE_CACHE": self.grok_cache,
        }
        p_env = mock.patch.dict(os.environ, env_overrides, clear=False)
        p_env.start()
        self.addCleanup(p_env.stop)

        for mod in (statusline, agent_quota):
            for attr, val in (
                ("CLAUDE_RL_CACHE", self.claude_cache),
                ("AGY_QUOTA_CACHE", self.agy_cache),
                ("AGY_QUOTA_ATTEMPT", self.agy_cache + ".last-attempt"),
            ):
                if hasattr(mod, attr):
                    p = mock.patch.object(mod, attr, val)
                    p.start()
                    self.addCleanup(p.stop)
        p_lock = mock.patch.object(
            agent_quota, "AGY_QUOTA_LOCK", self.agy_cache + ".refresh.lock"
        )
        p_lock.start()
        self.addCleanup(p_lock.stop)

    def _write_session(self, proj, sid, payload, mtime=None):
        sess_dir = pathlib.Path(self.grok_home) / "sessions" / proj / sid
        sess_dir.mkdir(parents=True, exist_ok=True)
        usage_file = sess_dir / "usage.json"
        if isinstance(payload, str):
            usage_file.write_text(payload, encoding="utf-8")
        else:
            usage_file.write_text(json.dumps(payload), encoding="utf-8")
        if mtime is not None:
            os.utime(usage_file, (mtime, mtime))
        return usage_file

    def _populate_other_agents(self):
        os.makedirs(os.path.dirname(self.claude_cache), exist_ok=True)
        now = time.time()
        pathlib.Path(self.claude_cache).write_text(
            json.dumps({
                "model": "Opus 5.5",
                "rate_limits": {
                    "five_hour": {"used_percentage": 18},
                    "seven_day": {"used_percentage": 62},
                },
                "ts": now - 60,
            }),
            encoding="utf-8",
        )
        pathlib.Path(self.agy_cache).write_text(
            json.dumps({
                "cached_at": now - 120,
                "plan_tier": "ULTRA",
                "usage": _agy_payload()["command"]["data"],
            }),
            encoding="utf-8",
        )
        sess = pathlib.Path(self.tmp) / ".codex" / "sessions" / "s"
        sess.mkdir(parents=True, exist_ok=True)
        (sess / "rollout-1.jsonl").write_text(
            json.dumps({
                "timestamp": _ts(120),
                "payload": {"rate_limits": PLAN},
            }) + "\n",
            encoding="utf-8",
        )

    def test_resumed_session_counts_only_recent_turns_and_ignores_session_total(self):
        now = time.time()
        # Resumed session: recent file mtime, inflated session.costUsdTicks,
        # one turn older than 7d, and two turns inside the 7d window ($1.25 + $2.97 = $4.22).
        self._write_session(
            "proj-a",
            "sess-resumed",
            {
                "sessionId": "sess-resumed",
                "session": {"costUsdTicks": 999 * 10_000_000_000, "turnCount": 3},
                "turns": [
                    {
                        "turnNumber": 1,
                        "endedAt": _ts(9 * 86400),
                        "costUsdTicks": 50 * 10_000_000_000,
                    },
                    {
                        "turnNumber": 2,
                        "endedAt": _ts(2 * 86400),
                        "costUsdTicks": 12_500_000_000,
                    },
                    {
                        "turnNumber": 3,
                        "endedAt": _ts(3600),
                        "costUsdTicks": 29_700_000_000,
                    },
                ],
            },
            mtime=now - 60,
        )
        # File older than 7 days by mtime: skipped without opening even if a
        # turn inside claims a recent endedAt.
        old_file = self._write_session(
            "proj-a",
            "sess-old-mtime",
            {
                "sessionId": "sess-old-mtime",
                "session": {"costUsdTicks": 100 * 10_000_000_000},
                "turns": [
                    {
                        "turnNumber": 1,
                        "endedAt": _ts(60),
                        "costUsdTicks": 100 * 10_000_000_000,
                    }
                ],
            },
            mtime=now - 10 * 86400,
        )
        # Recent file mtime but all turns older than 7 days: 0 counted turns,
        # so it must not increment the session count.
        self._write_session(
            "proj-b",
            "sess-touched-only",
            {
                "sessionId": "sess-touched-only",
                "session": {"costUsdTicks": 20 * 10_000_000_000},
                "turns": [
                    {
                        "turnNumber": 1,
                        "endedAt": _ts(8 * 86400),
                        "costUsdTicks": 20 * 10_000_000_000,
                    }
                ],
            },
            mtime=now - 30,
        )

        real_open = open

        def guarded_open(path, *args, **kwargs):
            if os.path.abspath(str(path)) == os.path.abspath(str(old_file)):
                raise AssertionError("mtime filter failed to skip >7d usage.json")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(agent_quota, "_grok_binary", return_value="/bin/grok"), \
                mock.patch.object(statusline, "_grok_binary", return_value="/bin/grok"), \
                mock.patch("builtins.open", side_effect=guarded_open):
            self.assertEqual(agent_quota._refresh_grok_cache(), 0)
            seg = statusline.grok_segment(compact=True)
            row = agent_quota.grok_row()

        cached = json.loads(pathlib.Path(self.grok_cache).read_text(encoding="utf-8"))
        self.assertAlmostEqual(cached["cost_usd"], 4.22, places=6)
        self.assertEqual(cached["cost_usd_ticks"], 42_200_000_000)
        self.assertEqual(cached["sessions"], 1)
        self.assertFalse(cached["partial"])
        self.assertEqual(seg, "gk $4.2/7d")
        self.assertEqual(
            row, "Grok     7d $4.22 local list-price usage (1 sessions)  [just now]"
        )

    def test_partial_cost_flags_full_row_only_and_ignores_uncounted_turns(self):
        now = time.time()
        # Session 1: old turn (>7d) lacked cost, recent turn has full cost.
        # Uncounted old turn must not mark the 7d window partial.
        self._write_session(
            "proj-a",
            "sess-1",
            {
                "turns": [
                    {"turnNumber": 1, "endedAt": _ts(9 * 86400)},
                    {
                        "turnNumber": 2,
                        "endedAt": _ts(1800),
                        "costUsdTicks": 42_200_000_000,
                    },
                ]
            },
            mtime=now - 60,
        )
        with mock.patch.object(agent_quota, "_grok_binary", return_value="/bin/grok"), \
                mock.patch.object(statusline, "_grok_binary", return_value="/bin/grok"):
            self.assertEqual(agent_quota._refresh_grok_cache(force=True), 0)
            self.assertNotIn("partial", agent_quota.grok_row())

        # Session 2: recent counted turn lacks costUsdTicks -> partial=True.
        self._write_session(
            "proj-a",
            "sess-2",
            {
                "turns": [
                    {"turnNumber": 1, "endedAt": _ts(600), "costUsdTicks": None},
                ]
            },
            mtime=now - 30,
        )
        with mock.patch.object(agent_quota, "_grok_binary", return_value="/bin/grok"), \
                mock.patch.object(statusline, "_grok_binary", return_value="/bin/grok"):
            self.assertEqual(agent_quota._refresh_grok_cache(force=True), 0)
            seg = statusline.grok_segment(compact=True)
            row = agent_quota.grok_row()

        cached = json.loads(pathlib.Path(self.grok_cache).read_text(encoding="utf-8"))
        self.assertTrue(cached["partial"])
        self.assertEqual(cached["sessions"], 2)
        self.assertAlmostEqual(cached["cost_usd"], 4.22, places=6)
        self.assertEqual(seg, "gk $4.2/7d")
        self.assertEqual(
            row,
            "Grok     7d $4.22 local list-price usage (2 sessions, partial)  [just now]",
        )

    def test_unreadable_and_corrupt_files_are_skipped(self):
        now = time.time()
        self._write_session(
            "proj-ok",
            "sess-ok",
            {
                "turns": [
                    {
                        "turnNumber": 1,
                        "endedAt": _ts(300),
                        "costUsdTicks": 30_000_000_000,
                    }
                ]
            },
            mtime=now - 20,
        )
        self._write_session("proj-bad", "sess-corrupt", "{not-json", mtime=now - 20)
        self._write_session("proj-bad", "sess-list", "[]", mtime=now - 20)
        self._write_session(
            "proj-bad", "sess-bad-turns", {"turns": "nope"}, mtime=now - 20
        )
        locked_file = self._write_session(
            "proj-bad",
            "sess-unreadable",
            {"turns": [{"endedAt": _ts(100), "costUsdTicks": 90_000_000_000}]},
            mtime=now - 20,
        )

        real_open = open

        def failing_open(path, *args, **kwargs):
            if os.path.abspath(str(path)) == os.path.abspath(str(locked_file)):
                raise PermissionError(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(agent_quota, "_grok_binary", return_value="/bin/grok"), \
                mock.patch.object(statusline, "_grok_binary", return_value="/bin/grok"), \
                mock.patch("builtins.open", side_effect=failing_open):
            self.assertEqual(agent_quota._refresh_grok_cache(force=True), 0)
            self.assertEqual(statusline.grok_segment(compact=True), "gk $3.0/7d")
            self.assertIn("7d $3.00 local list-price usage (1 sessions)", agent_quota.grok_row())

        # Corrupt cache file on disk -> segment and row return None cleanly.
        pathlib.Path(self.grok_cache).write_text("{corrupt", encoding="utf-8")
        with mock.patch.object(agent_quota, "_grok_binary", return_value="/bin/grok"), \
                mock.patch.object(statusline, "_grok_binary", return_value="/bin/grok"):
            self.assertIsNone(statusline.grok_segment(compact=True))
            self.assertIsNone(agent_quota.grok_row())

    def test_without_grok_output_is_byte_identical_even_with_stale_grok_cache(self):
        self._populate_other_agents()
        stdin_payload = json.dumps({
            "model": {"display_name": "Opus 5.5"},
            "rate_limits": {
                "five_hour": {"used_percentage": 18},
                "seven_day": {"used_percentage": 62},
            },
        })

        def run_both():
            sl_out = io.StringIO()
            aq_out = io.StringIO()
            with mock.patch.object(statusline, "_grok_binary", return_value=None), \
                    mock.patch.object(statusline, "persist_claude"), \
                    mock.patch.object(statusline, "start_agy_refresh"), \
                    mock.patch.object(statusline.subprocess, "Popen") as sl_popen, \
                    mock.patch("sys.stdin", io.StringIO(stdin_payload)), \
                    mock.patch("sys.stdout", sl_out):
                statusline.main()
            sl_popen.assert_not_called()

            with mock.patch.object(agent_quota, "_grok_binary", return_value=None), \
                    mock.patch.object(agent_quota, "_agy_binary", return_value="/bin/agy"), \
                    mock.patch.object(agent_quota, "_start_agy_refresh", return_value=False), \
                    mock.patch.object(agent_quota.subprocess, "Popen") as aq_popen, \
                    mock.patch.object(agent_quota.sys, "argv", ["agent-quota.py"]), \
                    mock.patch("sys.stdout", aq_out):
                self.assertEqual(agent_quota.main(), 0)
            aq_popen.assert_not_called()
            return sl_out.getvalue(), aq_out.getvalue()

        sl_without_cache, aq_without_cache = run_both()

        # Write a stale Grok cache on disk and re-run without grok installed.
        pathlib.Path(self.grok_cache).write_text(
            json.dumps({
                "cached_at": time.time() - 3600,
                "cost_usd": 19.85,
                "cost_usd_ticks": 198_500_000_000,
                "sessions": 5,
                "partial": False,
            }),
            encoding="utf-8",
        )
        os.utime(self.grok_cache, (time.time() - 3600, time.time() - 3600))
        sl_with_stale_cache, aq_with_stale_cache = run_both()

        self.assertEqual(sl_without_cache, sl_with_stale_cache)
        self.assertEqual(aq_without_cache, aq_with_stale_cache)
        self.assertNotIn("gk", sl_with_stale_cache)
        self.assertNotIn("Grok", aq_with_stale_cache)

    def test_no_helper_launch_without_grok(self):
        with mock.patch.object(statusline, "_grok_binary", return_value=None), \
                mock.patch.object(statusline.subprocess, "Popen") as sl_popen:
            self.assertFalse(statusline.start_grok_refresh())
            sl_popen.assert_not_called()

        with mock.patch.object(agent_quota, "_grok_binary", return_value=None), \
                mock.patch.object(agent_quota.subprocess, "Popen") as aq_popen:
            self.assertFalse(agent_quota._start_grok_refresh())
            aq_popen.assert_not_called()

    def test_hanging_or_failing_refresh_leaves_other_segments_unchanged(self):
        self._populate_other_agents()
        stdin_payload = json.dumps({
            "model": {"display_name": "Opus 5.5"},
            "rate_limits": {
                "five_hour": {"used_percentage": 18},
                "seven_day": {"used_percentage": 62},
            },
        })

        # Baseline without Grok.
        base_sl = io.StringIO()
        base_aq = io.StringIO()
        with mock.patch.object(statusline, "_grok_binary", return_value=None), \
                mock.patch.object(statusline, "persist_claude"), \
                mock.patch.object(statusline, "start_agy_refresh"), \
                mock.patch("sys.stdin", io.StringIO(stdin_payload)), \
                mock.patch("sys.stdout", base_sl):
            statusline.main()
        with mock.patch.object(agent_quota, "_grok_binary", return_value=None), \
                mock.patch.object(agent_quota, "_agy_binary", return_value="/bin/agy"), \
                mock.patch.object(agent_quota, "_start_agy_refresh", return_value=False), \
                mock.patch.object(agent_quota.sys, "argv", ["agent-quota.py"]), \
                mock.patch("sys.stdout", base_aq):
            agent_quota.main()

        # Grok installed, refresh spawns detached (simulated hanging helper holding lock
        # and producing no cache yet): Claude, Codex, and Agy output must be identical.
        hang_sl = io.StringIO()
        hang_aq = io.StringIO()
        with mock.patch.object(statusline, "_grok_binary", return_value="/bin/grok"), \
                mock.patch.object(statusline, "persist_claude"), \
                mock.patch.object(statusline, "start_agy_refresh"), \
                mock.patch.object(statusline.subprocess, "Popen") as sl_popen, \
                mock.patch("sys.stdin", io.StringIO(stdin_payload)), \
                mock.patch("sys.stdout", hang_sl):
            statusline.main()
        sl_popen.assert_called_once()
        self.assertEqual(sl_popen.call_args.args[0][2], "--refresh-grok")

        with mock.patch.object(agent_quota, "_grok_binary", return_value="/bin/grok"), \
                mock.patch.object(agent_quota, "_agy_binary", return_value="/bin/agy"), \
                mock.patch.object(agent_quota, "_start_agy_refresh", return_value=False), \
                mock.patch.object(agent_quota.subprocess, "Popen") as aq_popen, \
                mock.patch.object(agent_quota.sys, "argv", ["agent-quota.py"]), \
                mock.patch("sys.stdout", hang_aq):
            agent_quota.main()
        aq_popen.assert_called_once()

        self.assertEqual(hang_sl.getvalue(), base_sl.getvalue())
        self.assertEqual(hang_aq.getvalue(), base_aq.getvalue())

        # Failing/timed-out synchronous refresh writes .last-attempt, releases
        # the lock, and throttles subsequent attempts within the TTL.
        with mock.patch.object(agent_quota, "_grok_binary", return_value="/bin/grok"), \
                mock.patch.object(agent_quota, "_scan_grok_usage", return_value=None) as scan:
            self.assertEqual(agent_quota._refresh_grok_cache(), 1)
            self.assertEqual(agent_quota._refresh_grok_cache(), 0)
            self.assertEqual(scan.call_count, 1)
            self.assertTrue(os.path.isfile(agent_quota._grok_attempt_path()))
            self.assertFalse(os.path.exists(agent_quota._grok_lock_path()))

        with mock.patch.object(statusline, "_grok_binary", return_value="/bin/grok"), \
                mock.patch.object(statusline.subprocess, "Popen") as throttled_popen:
            self.assertFalse(statusline.start_grok_refresh())
            throttled_popen.assert_not_called()

    def test_compact_and_full_rendering_with_grok_present(self):
        self._populate_other_agents()
        now = time.time()
        pathlib.Path(self.grok_cache).write_text(
            json.dumps({
                "cached_at": now - 120,
                "cost_usd": 4.22,
                "cost_usd_ticks": 42_200_000_000,
                "sessions": 3,
                "partial": False,
            }),
            encoding="utf-8",
        )
        stdin_payload = json.dumps({
            "model": {"display_name": "Opus 5.5"},
            "rate_limits": {
                "five_hour": {"used_percentage": 18},
                "seven_day": {"used_percentage": 62},
            },
        })

        with mock.patch.object(statusline, "_grok_binary", return_value="/bin/grok"), \
                mock.patch.object(agent_quota, "_grok_binary", return_value="/bin/grok"):
            compact_seg = statusline.grok_segment(compact=True)
            full_seg = statusline.grok_segment(compact=False)
            row = agent_quota.grok_row()

            sl_out = io.StringIO()
            with mock.patch.object(statusline, "persist_claude"), \
                    mock.patch.object(statusline, "start_agy_refresh"), \
                    mock.patch.object(statusline.subprocess, "Popen") as sl_popen, \
                    mock.patch("sys.stdin", io.StringIO(stdin_payload)), \
                    mock.patch("sys.stdout", sl_out):
                statusline.main()
            sl_popen.assert_not_called()

            aq_out = io.StringIO()
            with mock.patch.object(agent_quota, "_agy_binary", return_value="/bin/agy"), \
                    mock.patch.object(agent_quota, "_start_agy_refresh", return_value=False), \
                    mock.patch.object(agent_quota.subprocess, "Popen") as aq_popen, \
                    mock.patch.object(agent_quota.sys, "argv", ["agent-quota.py"]), \
                    mock.patch("sys.stdout", aq_out):
                self.assertEqual(agent_quota.main(), 0)
            aq_popen.assert_not_called()

        self.assertEqual(compact_seg, "gk $4.2/7d")
        self.assertEqual(
            full_seg, "Grok · 7d $4.22 local list-price usage (3 sessions) · 2m ago"
        )
        self.assertEqual(
            row, "Grok     7d $4.22 local list-price usage (3 sessions)  [2m ago]"
        )
        self.assertTrue(sl_out.getvalue().rstrip("\n").endswith(" | gk $4.2/7d"))
        aq_lines = aq_out.getvalue().rstrip("\n").splitlines()
        self.assertEqual(
            aq_lines[-1],
            "Grok     7d $4.22 local list-price usage (3 sessions)  [2m ago]",
        )

    def test_binary_resolution_lazy_cache_path_and_cli_refresh(self):
        # 1. Lazy cache path follows os.path.expanduser("~") when GROK_USAGE_CACHE is unset.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GROK_USAGE_CACHE", None)
            os.environ.pop("GROK_HOME", None)
            self.assertEqual(
                statusline._grok_cache_path(),
                os.path.join(self.tmp, ".claude", "grok-usage-cache.json"),
            )
            self.assertEqual(
                agent_quota._grok_cache_path(),
                os.path.join(self.tmp, ".claude", "grok-usage-cache.json"),
            )
            self.assertEqual(
                agent_quota._grok_home(),
                os.path.join(self.tmp, ".grok"),
            )

        # 2. Binary resolution: GROK_BIN -> PATH -> ~/.grok/bin/grok.
        fallback_bin = pathlib.Path(self.grok_home) / "bin" / (
            "grok.exe" if os.name == "nt" else "grok"
        )
        fallback_bin.parent.mkdir(parents=True, exist_ok=True)
        fallback_bin.write_text("", encoding="utf-8")
        custom_bin = pathlib.Path(self.tmp) / "custom-grok"
        custom_bin.write_text("", encoding="utf-8")

        for mod in (statusline, agent_quota):
            with mock.patch.dict(os.environ, {"GROK_BIN": str(custom_bin)}, clear=False):
                self.assertEqual(mod._grok_binary(), os.path.abspath(str(custom_bin)))
            with mock.patch.dict(os.environ, {}, clear=False), \
                    mock.patch.object(mod.shutil, "which", return_value="/opt/bin/grok"):
                os.environ.pop("GROK_BIN", None)
                self.assertEqual(mod._grok_binary(), "/opt/bin/grok")
            with mock.patch.dict(os.environ, {}, clear=False), \
                    mock.patch.object(mod.shutil, "which", return_value=None):
                os.environ.pop("GROK_BIN", None)
                self.assertEqual(mod._grok_binary(), str(fallback_bin))

        # 3. CLI --refresh-grok entry point.
        self._write_session(
            "proj-cli",
            "sess-cli",
            {"turns": [{"endedAt": _ts(60), "costUsdTicks": 15_000_000_000}]},
            mtime=time.time() - 10,
        )
        # The real watchdog ends its process; in-process it would end pytest,
        # so a stand-in records that the CLI armed it and that it was cancelled.
        armed = threading.Event()
        with mock.patch.object(agent_quota, "_grok_binary", return_value=str(custom_bin)), \
                mock.patch.object(agent_quota, "_arm_grok_watchdog", return_value=armed) as arm, \
                mock.patch.object(agent_quota.sys, "argv", ["agent-quota.py", "--refresh-grok"]):
            self.assertEqual(agent_quota.main(), 0)
        arm.assert_called_once()
        self.assertTrue(armed.is_set())
        cached = json.loads(pathlib.Path(self.grok_cache).read_text(encoding="utf-8"))
        self.assertAlmostEqual(cached["cost_usd"], 1.5, places=6)
        self.assertEqual(cached["sessions"], 1)


class TestGrokRefreshHardTimeout(unittest.TestCase):
    """The detached --refresh-grok helper must end even when the scan blocks."""

    def test_blocked_scan_is_ended_by_the_watchdog_and_the_lock_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            fake_grok = tmp / "grok"
            fake_grok.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_grok.chmod(0o755)
            cache = tmp / "grok-usage-cache.json"
            env = dict(
                os.environ,
                HOME=str(tmp),
                USERPROFILE=str(tmp),
                GROK_HOME=str(tmp / ".grok"),
                GROK_BIN=str(fake_grok),
                GROK_USAGE_CACHE=str(cache),
                GROK_USAGE_TIMEOUT_SECONDS="1",
            )
            # A scan that blocks far past its deadline stands in for a stuck
            # open or read, which the in-loop deadline checks cannot interrupt.
            driver = (
                "import importlib.util, sys, time\n"
                "spec = importlib.util.spec_from_file_location('aq', sys.argv[1])\n"
                "aq = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(aq)\n"
                "aq._scan_grok_usage = lambda **kw: time.sleep(60)\n"
                "sys.exit(aq._refresh_grok_cache(force=True, hard_timeout=True))\n"
            )
            started = time.monotonic()
            proc = subprocess.run(
                [sys.executable, "-c", driver, str(REPO / "scripts" / "agent-quota.py")],
                env=env, capture_output=True, text=True, timeout=45,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(proc.returncode, 124, msg=proc.stderr)
            self.assertLess(elapsed, 30)
            self.assertFalse(pathlib.Path(str(cache) + ".refresh.lock").exists())
            self.assertFalse(cache.exists())

    def test_a_finished_refresh_cancels_its_watchdog(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            fake_grok = tmp / "grok"
            fake_grok.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_grok.chmod(0o755)
            cache = tmp / "grok-usage-cache.json"
            env = dict(
                os.environ,
                HOME=str(tmp),
                USERPROFILE=str(tmp),
                GROK_HOME=str(tmp / ".grok"),
                GROK_BIN=str(fake_grok),
                GROK_USAGE_CACHE=str(cache),
                GROK_USAGE_TIMEOUT_SECONDS="1",
            )
            # After a normal refresh the watchdog thread must end at once; a
            # live one would end any longer-lived process that ran the refresh.
            driver = (
                "import importlib.util, sys, threading\n"
                "spec = importlib.util.spec_from_file_location('aq', sys.argv[1])\n"
                "aq = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(aq)\n"
                "rc = aq._refresh_grok_cache(force=True, hard_timeout=True)\n"
                "for t in threading.enumerate():\n"
                "    if t.name == 'grok-refresh-watchdog':\n"
                "        t.join(2)\n"
                "        if t.is_alive():\n"
                "            print('watchdog alive')\n"
                "            sys.exit(3)\n"
                "sys.exit(rc)\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", driver, str(REPO / "scripts" / "agent-quota.py")],
                env=env, capture_output=True, text=True, timeout=45,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            self.assertTrue(cache.exists())
            self.assertFalse(pathlib.Path(str(cache) + ".refresh.lock").exists())

    def test_a_fifo_named_usage_json_is_skipped_not_opened(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            session = pathlib.Path(tmp) / "sessions" / "proj" / "s1"
            session.mkdir(parents=True)
            os.mkfifo(session / "usage.json")
            with mock.patch.dict(os.environ, {"GROK_HOME": tmp}):
                started = time.monotonic()
                result = agent_quota._scan_grok_usage(deadline=time.monotonic() + 5)
            self.assertLess(time.monotonic() - started, 2)
            self.assertIsNotNone(result)
            self.assertEqual(result["sessions"], 0)


if __name__ == "__main__":
    unittest.main()
