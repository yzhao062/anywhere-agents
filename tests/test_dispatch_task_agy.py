"""Contract tests for prun's Antigravity task dispatcher."""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISPATCH = ROOT / "skills" / "prun" / "scripts" / "dispatch-task-agy.py"
PYTHON = Path(sys.executable).resolve()


def load_dispatch_module():
    spec = importlib.util.spec_from_file_location("dispatch_task_agy", DISPATCH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WORKER_RESULT_TEXT = (
    "# unit_a result\nConclusion: worker-written\nFiles: none\n"
    "Open items: none\nVerification: mock\n\n| row | value |\n|---|---|\n| a | 1 |\n"
)

MOCK_AGY = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

log = Path(os.environ["MOCK_AGY_LOG"])
log.mkdir(parents=True, exist_ok=True)
if sys.argv[1:] == ["--version"]:
    print("Antigravity CLI 1.2.0")
    raise SystemExit(0)
if sys.argv[1:] == ["models"]:
    print(os.environ.get("MOCK_AGY_MODELS", "gemini-3.8-flash-high"))
    raise SystemExit(0)

(log / "args.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
# Agy 1.2.16 rejects an --effort that conflicts with the tier in the slug.
argv = sys.argv[1:]
if "--model" in argv and "--effort" in argv:
    slug = argv[argv.index("--model") + 1]
    effort = argv[argv.index("--effort") + 1]
    tier = slug.rsplit("-", 1)[-1]
    if tier in ("low", "medium", "high") and tier != effort:
        print(f"error: --model {slug} conflicts with --effort={effort}", file=sys.stderr)
        raise SystemExit(2)
(log / "cwd.txt").write_text(os.getcwd(), encoding="utf-8")
event = json.loads(sys.stdin.readline())
(log / "prompt.txt").write_text(event["message"]["content"], encoding="utf-8")
init_event = {"event": "init", "model": "gemini-3.8-flash-high"}
if os.environ.get("MOCK_AGY_NO_INIT_CONVERSATION_ID") != "1":
    init_event["conversation_id"] = os.environ.get(
        "MOCK_AGY_CONVERSATION_ID", "conv-fixed-0001"
    )
print(json.dumps(init_event))
worker_result = os.environ.get("MOCK_AGY_WRITE_RESULT")
if worker_result:
    Path(worker_result).write_text(
        os.environ["MOCK_AGY_WORKER_TEXT"], encoding="utf-8"
    )
if os.environ.get("MOCK_AGY_NO_RESULT") != "1":
    response = os.environ.get(
        "MOCK_AGY_RESPONSE",
        "# unit_a result\nConclusion: complete\nFiles: none\nOpen items: none\nVerification: mock\n",
    )
    payload = {"response": response}
    # Older Agy builds omit status, so the fixture carries the field only when
    # a test asks for it.
    for key in ("status", "error"):
        value = os.environ.get("MOCK_AGY_" + key.upper())
        if value:
            payload[key] = value
    print(json.dumps({"event": "result", "result": payload}))
    if os.environ.get("MOCK_AGY_TRAILING_RESULT_WITHOUT_STATUS") == "1":
        print(json.dumps({"event": "result", "result": {"response": response}}))
raise SystemExit(int(os.environ.get("MOCK_AGY_EXIT", "0")))
'''


class DispatchTaskAgyUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_dispatch_module()

    def test_script_exists(self) -> None:
        self.assertTrue(DISPATCH.is_file())

    def test_defaults_pin_flash_high(self) -> None:
        self.assertEqual(self.module.DEFAULT_MODEL, "gemini-3.8-flash-high")
        self.assertEqual(self.module.DEFAULT_EFFORT, "high")

    def test_unit_id_is_narrow(self) -> None:
        self.assertIsNotNone(self.module.UNIT_RE.fullmatch("paper_review-12"))
        self.assertIsNone(self.module.UNIT_RE.fullmatch("../escape"))

    def test_model_pool_keys_on_the_name_agy_uses(self) -> None:
        self.assertEqual(self.module.model_pool("gemini-3.8-flash-high"), "gemini")
        self.assertEqual(self.module.model_pool("claude-opus-4-6-thinking"), "second")
        self.assertEqual(self.module.model_pool("gpt-oss-120b-medium"), "second")
        # An unknown name is not gated: Agy names its models, and refusing one
        # for belonging to no known group would block a model that works.
        self.assertIsNone(self.module.model_pool("some-future-model"))

    def test_newest_in_family(self) -> None:
        newest = self.module.newest_in_family
        # 3.10 beats 3.9
        self.assertEqual(
            newest(
                "gemini-3.9-flash-high",
                ["gemini-3.9-flash-high", "gemini-3.10-flash-high"],
            ),
            "gemini-3.10-flash-high",
        )
        # 4 beats 3.8
        self.assertEqual(
            newest(
                "gemini-3.8-flash-high",
                ["gemini-3.8-flash-high", "gemini-4-flash-high"],
            ),
            "gemini-4-flash-high",
        )
        # Components: 3.8.1 beats 3.8
        self.assertEqual(
            newest(
                "gemini-3.8-flash-high",
                ["gemini-3.8-flash-high", "gemini-3.8.1-flash-high"],
            ),
            "gemini-3.8.1-flash-high",
        )
        # A tier change stays in the family; a name change (lite, pro) does not.
        self.assertEqual(
            newest(
                "gemini-3.8-flash-high",
                ["gemini-3.8-flash-lite-high", "gemini-3.1-pro-high"],
            ),
            "gemini-3.8-flash-high",
        )
        self.assertEqual(
            newest(
                "gemini-3.8-flash-high",
                ["gemini-3.8-flash-medium", "gemini-3.8-flash-high", "gemini-3.8-flash-low"],
            ),
            "gemini-3.8-flash-high",
        )
        # Agy 1.2.16 moved the tier into the Claude slugs and retired
        # claude-sonnet-4-6; the old template still reaches Sonnet 5.5 at high.
        live = [
            "claude-opus-5-5-high",
            "claude-sonnet-5-5-low",
            "claude-sonnet-5-5-medium",
            "claude-sonnet-5-5-high",
            "gpt-oss-120b-medium",
        ]
        self.assertEqual(newest("claude-sonnet-4-6", live), "claude-sonnet-5-5-high")
        self.assertEqual(
            newest(
                "claude-sonnet-5-5-high",
                live + ["claude-sonnet-5-6-medium", "claude-sonnet-5-6-high"],
            ),
            "claude-sonnet-5-6-high",
        )
        # A tier outranks a version, so the dispatcher's high effort stays valid.
        self.assertEqual(
            newest("gemini-3.8-flash-high", ["gemini-3.8-flash-medium", "gemini-3.7-flash-high"]),
            "gemini-3.7-flash-high",
        )
        # An untiered template prefers high over another untiered slug.
        self.assertEqual(
            newest("claude-sonnet-4-6", ["claude-sonnet-5-5", "claude-sonnet-5-5-high"]),
            "claude-sonnet-5-5-high",
        )
        # With no preferred tier listed, the newest member wins.
        self.assertEqual(
            newest("gemini-3.8-flash-high", ["gemini-3.9-flash-medium", "gemini-3.9-flash-low"]),
            "gemini-3.9-flash-medium",
        )
        # Without the template itself: its own tier, then high, then untiered.
        self.assertEqual(
            newest(
                "claude-sonnet-5-5-medium",
                ["claude-sonnet-5-6-high", "claude-sonnet-5-6-medium"],
            ),
            "claude-sonnet-5-6-medium",
        )
        self.assertEqual(
            newest("claude-sonnet-5-5-low", ["claude-sonnet-5-6", "claude-sonnet-5-6-high"]),
            "claude-sonnet-5-6-high",
        )
        self.assertEqual(
            newest("claude-sonnet-5-5-low", ["claude-sonnet-5-6-medium", "claude-sonnet-5-6"]),
            "claude-sonnet-5-6",
        )
        # -thinking suffix respected both ways
        self.assertEqual(
            newest(
                "claude-sonnet-4-6",
                ["claude-sonnet-4-6", "claude-sonnet-4-6-thinking"],
            ),
            "claude-sonnet-4-6",
        )
        self.assertEqual(
            newest(
                "claude-opus-4-6-thinking",
                ["claude-opus-4-6", "claude-opus-4-6-thinking"],
            ),
            "claude-opus-4-6-thinking",
        )
        self.assertEqual(
            newest(
                "claude-opus-4-6-thinking",
                ["claude-opus-4-6", "claude-opus-4-7-thinking"],
            ),
            "claude-opus-4-7-thinking",
        )
        # no candidates returns the template
        self.assertEqual(newest("gemini-3.8-flash-high", []), "gemini-3.8-flash-high")
        self.assertEqual(
            newest("gemini-3.8-flash-high", ["claude-sonnet-4-6"]),
            "gemini-3.8-flash-high",
        )
        # unparseable template unchanged
        self.assertEqual(newest("unknown", ["unknown-2"]), "unknown")
        self.assertEqual(newest("not_a_model", ["not_a_model-2"]), "not_a_model")
        # tie handling: keep template if among tied candidates
        self.assertEqual(
            newest(
                "gemini-3.8-flash-high",
                ["gemini-3-8-flash-high", "gemini-3.8-flash-high"],
            ),
            "gemini-3.8-flash-high",
        )
        self.assertEqual(
            newest(
                "gemini-3.8-flash-high",
                ["gemini-3.8-flash-high", "gemini-3-8-flash-high"],
            ),
            "gemini-3.8-flash-high",
        )
        # tie handling: first tied candidate in input order if template not among tied
        self.assertEqual(
            newest(
                "gemini-3.7-flash-high",
                ["gemini-3-8-flash-high", "gemini-3.8-flash-high"],
            ),
            "gemini-3-8-flash-high",
        )
        self.assertEqual(
            newest(
                "gemini-3.7-flash-high",
                ["gemini-3.8-flash-high", "gemini-3-8-flash-high"],
            ),
            "gemini-3.8-flash-high",
        )

    def test_a_group_is_read_at_its_emptiest_bucket(self) -> None:
        # A full weekly allowance is no help to a unit the 5-hour bucket
        # stops, and the 5-hour bucket is the one that emptied on 2026-09-11.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cache = Path(temp.name) / "quota.json"
        cache.write_text(
            json.dumps(
                {
                    "usage": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [
                                    {"remaining_fraction": 0.9, "reset_time": "w"},
                                    {"remaining_fraction": 0.1, "reset_time": "5h"},
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        os.environ["AGY_QUOTA_CACHE"] = str(cache)
        self.addCleanup(os.environ.pop, "AGY_QUOTA_CACHE", None)
        states = self.module.read_pool_states()
        self.assertEqual(states["gemini"].remaining, 0.1)
        self.assertEqual(states["gemini"].reset, "5h")
        # Neither bucket names its window, so the reading carries no coverage.
        self.assertEqual(states["gemini"].windows, frozenset())

    def test_an_unreported_group_is_unknown_rather_than_empty(self) -> None:
        # The gate stops a dispatch only into a group it read as empty. A
        # snapshot that carries one group says nothing about the other, and
        # refusing on that would block work whenever Agy renames a group.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cache = Path(temp.name) / "quota.json"
        cache.write_text(
            json.dumps(
                {
                    "usage": {
                        "groups": [
                            {
                                "name": "Claude and GPT models",
                                "buckets": [
                                    {"remaining_fraction": 0.0, "reset_time": "r"}
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        os.environ["AGY_QUOTA_CACHE"] = str(cache)
        self.addCleanup(os.environ.pop, "AGY_QUOTA_CACHE", None)
        model, note, blocked = self.module.quota_route("claude-opus-4-6-thinking")
        self.assertEqual(model, self.module.DEFAULT_MODEL)
        self.assertIn("MODEL-FALLBACK", note)
        self.assertEqual(blocked, "")

    def _snapshot(self, **fractions: float | dict[str, float]) -> None:
        """Point the module at a snapshot carrying the named groups.

        A plain number reports that fraction in both metered windows. A dict
        reports only the windows it names, which is the partial snapshot the
        routing has to treat as evidence about one window alone.
        """
        names = {"gemini": "Gemini Models", "second": "Claude and GPT models"}
        groups = []
        for pool, value in fractions.items():
            per_window = value if isinstance(value, dict) else {"5h": value, "weekly": value}
            groups.append(
                {
                    "name": names[pool],
                    "buckets": [
                        {"remaining_fraction": share, "reset_time": "r", "window": window}
                        for window, share in per_window.items()
                    ],
                }
            )
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cache = Path(temp.name) / "quota.json"
        cache.write_text(json.dumps({"usage": {"groups": groups}}), encoding="utf-8")
        os.environ["AGY_QUOTA_CACHE"] = str(cache)
        self.addCleanup(os.environ.pop, "AGY_QUOTA_CACHE", None)

    def test_an_unpinned_unit_routes_to_the_group_with_the_freer_meter(self) -> None:
        # A unit is shallow work either group handles, so the meter decides.
        self._snapshot(gemini=0.2, second=1.0)
        model, note, blocked = self.module.quota_route(self.module.DEFAULT_MODEL)
        self.assertEqual(model, self.module.SECOND_MODEL)
        self.assertIn("MODEL-BALANCE", note)
        self.assertEqual(blocked, "")
        # A model the caller named is a choice the meters do not overrule.
        self.assertEqual(
            self.module.quota_route(self.module.DEFAULT_MODEL, True),
            (self.module.DEFAULT_MODEL, "", ""),
        )

    def test_the_lead_a_move_needs_is_fifteen_points(self) -> None:
        # Literal fractions, so a change to BALANCE_MARGIN fails here rather
        # than moving the expectation with it. 20/35 and 50/65 are both exactly
        # fifteen points and subtract to either side of it in binary floating
        # point, so both have to decide the same way.
        for gemini, second, expected in (
            (0.20, 0.34, "gemini"),
            (0.20, 0.35, "second"),
            (0.20, 0.36, "second"),
            (0.50, 0.65, "second"),
        ):
            with self.subTest(gemini=gemini, second=second):
                self._snapshot(gemini=gemini, second=second)
                model, _, blocked = self.module.quota_route(self.module.DEFAULT_MODEL)
                self.assertEqual(blocked, "")
                self.assertEqual(
                    model,
                    self.module.SECOND_MODEL if expected == "second" else self.module.DEFAULT_MODEL,
                )

    def test_an_empty_own_group_moves_an_unpinned_unit_under_the_margin(self) -> None:
        self._snapshot(gemini=0.0, second=0.1)
        model, note, blocked = self.module.quota_route(self.module.DEFAULT_MODEL)
        self.assertEqual(model, self.module.SECOND_MODEL)
        self.assertIn("MODEL-BALANCE", note)
        self.assertEqual(blocked, "")

    def test_a_healthy_unit_needs_both_windows_of_the_destination(self) -> None:
        # A group entry is its emptiest bucket, so a destination that reports
        # only the weekly window may be empty in the five-hour one. Moving a
        # unit its own group could have run would strand it there.
        self._snapshot(gemini={"5h": 0.4, "weekly": 0.8}, second={"weekly": 1.0})
        self.assertEqual(
            self.module.quota_route(self.module.DEFAULT_MODEL),
            (self.module.DEFAULT_MODEL, "", ""),
        )
        # The same destination with both windows read is evidence of headroom.
        self._snapshot(gemini={"5h": 0.4, "weekly": 0.8}, second={"5h": 1.0, "weekly": 1.0})
        self.assertEqual(
            self.module.quota_route(self.module.DEFAULT_MODEL)[0], self.module.SECOND_MODEL
        )
        # An own group that is empty stops the unit outright, so a partly read
        # destination still beats not running.
        self._snapshot(gemini={"5h": 0.0, "weekly": 0.8}, second={"weekly": 1.0})
        self.assertEqual(
            self.module.quota_route(self.module.DEFAULT_MODEL)[0], self.module.SECOND_MODEL
        )

    def test_balancing_needs_both_groups_in_the_snapshot(self) -> None:
        # A group the snapshot does not report says nothing about its headroom.
        self._snapshot(gemini=0.2)
        self.assertEqual(
            self.module.quota_route(self.module.DEFAULT_MODEL),
            (self.module.DEFAULT_MODEL, "", ""),
        )

    def test_both_groups_empty_still_block_an_unpinned_unit(self) -> None:
        self._snapshot(gemini=0.0, second=0.0)
        model, note, blocked = self.module.quota_route(self.module.DEFAULT_MODEL)
        self.assertEqual(model, self.module.DEFAULT_MODEL)
        self.assertEqual(note, "")
        self.assertIn("both Agy quota groups are exhausted", blocked)

    def test_the_gate_is_switchable_off(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cache = Path(temp.name) / "quota.json"
        cache.write_text(
            json.dumps(
                {
                    "usage": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [{"remaining_fraction": 0.0}],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        os.environ["AGY_QUOTA_CACHE"] = str(cache)
        self.addCleanup(os.environ.pop, "AGY_QUOTA_CACHE", None)
        self.assertNotEqual(self.module.quota_route(self.module.DEFAULT_MODEL)[2], "")
        os.environ["PRUN_AGY_QUOTA_GATE"] = "off"
        self.addCleanup(os.environ.pop, "PRUN_AGY_QUOTA_GATE", None)
        self.assertEqual(
            self.module.quota_route(self.module.DEFAULT_MODEL), (self.module.DEFAULT_MODEL, "", "")
        )

    def test_a_non_finite_fraction_is_not_an_empty_group(self) -> None:
        # NaN compares false against every threshold, so admitting one read as
        # an exhausted group and refused the dispatch.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cache = Path(temp.name) / "quota.json"
        cache.write_text(
            '{"usage": {"groups": [{"name": "Gemini Models", "buckets": '
            '[{"remaining_fraction": NaN}]}]}}',
            encoding="utf-8",
        )
        os.environ["AGY_QUOTA_CACHE"] = str(cache)
        self.addCleanup(os.environ.pop, "AGY_QUOTA_CACHE", None)
        self.assertIsNone(self.module.read_pool_states())
        self.assertEqual(
            self.module.quota_route(self.module.DEFAULT_MODEL),
            (self.module.DEFAULT_MODEL, "", ""),
        )

    def test_an_empty_bucket_expires_at_its_own_reset(self) -> None:
        # A five-hour bucket that read empty an hour ago says nothing about
        # now, and the refusal would otherwise stand until something else
        # refreshed the snapshot.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
        cache = Path(temp.name) / "quota.json"

        def write(reset: datetime.datetime) -> None:
            cache.write_text(
                json.dumps(
                    {
                        "usage": {
                            "groups": [
                                {
                                    "name": "Gemini Models",
                                    "buckets": [
                                        {
                                            "remaining_fraction": 0.0,
                                            "reset_time": reset.isoformat().replace(
                                                "+00:00", "Z"
                                            ),
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

        os.environ["AGY_QUOTA_CACHE"] = str(cache)
        self.addCleanup(os.environ.pop, "AGY_QUOTA_CACHE", None)
        write(future)
        self.assertNotEqual(self.module.quota_route(self.module.DEFAULT_MODEL)[2], "")
        write(past)
        self.assertEqual(
            self.module.quota_route(self.module.DEFAULT_MODEL),
            (self.module.DEFAULT_MODEL, "", ""),
        )

    def test_a_snapshot_that_cannot_be_refreshed_reads_as_unknown(self) -> None:
        # The managed snapshot has a lifetime. One that stayed stale through a
        # refresh attempt is no evidence, and refusing on it would block work
        # for as long as the readout stays unavailable.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name) / "home"
        (home / ".claude").mkdir(parents=True)
        cache = home / ".claude" / "agy-quota-cache.json"
        cache.write_text(
            json.dumps(
                {
                    "usage": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [{"remaining_fraction": 0.0}],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        old = time.time() - (self.module.QUOTA_CACHE_MAX_AGE_SECONDS + 600)
        os.utime(cache, (old, old))
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "AGY_QUOTA_CACHE")}

        def restore() -> None:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        os.environ.pop("AGY_QUOTA_CACHE", None)
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        # No agent-quota.py under this home, so the refresh cannot land.
        self.assertIsNone(self.module.read_pool_states())

    def test_failure_reason_reads_the_final_result_status(self) -> None:
        self.assertEqual(self.module.failure_reason(None, None), "")
        self.assertEqual(self.module.failure_reason("SUCCESS", None), "")
        self.assertEqual(self.module.failure_reason("", None), "")
        self.assertEqual(self.module.failure_reason("  success  ", None), "")
        reason = self.module.failure_reason(
            "ERROR", "Individual quota reached.\nResets in 34m7s."
        )
        self.assertIn("ERROR", reason)
        # The reason becomes one line of the FALLBACK header.
        self.assertIn("Individual quota reached. Resets in 34m7s.", reason)
        self.assertNotIn("\n", reason)

    def test_copy_stream_writes_each_chunk_before_eof(self) -> None:
        # A running unit's tail sat at 0 bytes and then jumped to exactly
        # 65536, because read(65536) waits for a full 64 KiB. The monitors
        # and their stall threshold read the tail while the unit runs, so a
        # chunk has to land as the pipe delivers it.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        tail_path = Path(temp.name) / "tail"
        read_fd, write_fd = os.pipe()
        # bufsize -1 is what subprocess.Popen hands the dispatcher, so this
        # read end buffers exactly the way process.stdout does.
        source = open(read_fd, "rb")
        self.addCleanup(source.close)
        writer = open(write_fd, "wb", buffering=0)
        line = b'{"event": "init", "conversation_id": "conv-pump-0001"}\n'
        observed = b""
        with tail_path.open("wb") as tail:
            pump = threading.Thread(
                target=self.module.copy_stream, args=(source, tail), daemon=True
            )
            pump.start()
            try:
                writer.write(line)
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    observed = tail_path.read_bytes()
                    if observed == line:
                        break
                    time.sleep(0.02)
            finally:
                # Closing before the join keeps a failed assertion from
                # leaving the pump blocked on a pipe nobody will write to.
                writer.close()
                pump.join(timeout=10.0)
        self.assertEqual(observed, line, "tail did not grow while the pipe stayed open")
        self.assertFalse(pump.is_alive(), "the pump did not return at EOF")


class DispatchTaskAgyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_dispatch_module()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("Audit section 3 and report evidence.\n", encoding="utf-8")
        self.log = self.root / "log"
        self.mock = self._write_mock()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _quota(self, gemini: float, second: float) -> str:
        """Write a snapshot in the shape agent-quota.py publishes.

        Each call gets its own file, so a scenario's snapshot survives the
        healthy default `_run` writes for every other case. No bucket carries
        a reset time: a dated one expires under the runtime expiry rule and
        would turn these routing cases red on a later calendar day. The
        expiry rule has its own test, which builds explicit past and future
        reset times.
        """
        self._quota_seq = getattr(self, "_quota_seq", 0) + 1
        cache = self.root / f"agy-quota-{self._quota_seq}.json"
        cache.write_text(
            json.dumps(
                {
                    "cached_at": int(time.time()),
                    "usage": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [
                                    {
                                        "id": f"gemini-{window}",
                                        "remaining_fraction": gemini,
                                    }
                                    for window in ("5h", "weekly")
                                ],
                            },
                            {
                                "name": "Claude and GPT models",
                                "buckets": [
                                    {
                                        "id": f"3p-{window}",
                                        "remaining_fraction": second,
                                    }
                                    for window in ("5h", "weekly")
                                ],
                            },
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        return str(cache)

    def _write_mock(self) -> Path:
        script = self.root / "mock_agy.py"
        script.write_text(MOCK_AGY, encoding="utf-8")
        if os.name == "nt":
            wrapper = self.root / "agy.cmd"
            wrapper.write_text(f'@"{PYTHON}" "{script}" %*\r\n', encoding="utf-8")
            return wrapper
        wrapper = self.root / "agy"
        wrapper.write_text(
            f"#!{PYTHON}\n" + "\n".join(MOCK_AGY.splitlines()[1:]) + "\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return wrapper

    def _run(
        self,
        result_path: Path | None = None,
        mode: str | None = "plan",
        extra_env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
        own_cwd: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        target = result_path or (self.root / "unit-result.md")
        env = os.environ.copy()
        # The default-argv assertions must not inherit an operator's opt-in.
        env.pop("PRUN_AGY_SANDBOX", None)
        env.update(
            {
                "ANTIGRAVITY_BIN": str(self.mock),
                "ANTIGRAVITY_PREFLIGHT_TIMEOUT_SECONDS": "10",
                "MOCK_AGY_LOG": str(self.log),
                "MOCK_AGY_WORKER_TEXT": WORKER_RESULT_TEXT,
                "PRUN_SCRATCH_CWD": str(self.work),
                "TEMP": str(self.root),
                "TMP": str(self.root),
                "TMPDIR": str(self.root),
                # Every dispatch now consults the Agy meter, so without a
                # private snapshot these cases would read the operator's real
                # one and fail on a machine whose Gemini quota is spent.
                "AGY_QUOTA_CACHE": self._quota(gemini=1.0, second=1.0),
            }
        )
        if own_cwd:
            # Let the dispatcher create its own scratch directory, which is
            # the only case where an omitted --mode is allowed.
            env.pop("PRUN_SCRATCH_CWD", None)
        if extra_env:
            for key, value in extra_env.items():
                if value == "":
                    env.pop(key, None)
                else:
                    env[key] = value
        argv = [
            str(PYTHON),
            str(DISPATCH),
            "--prompt-file",
            str(self.prompt),
            "--result-file",
            str(target),
            "--unit-id",
            "unit_a",
        ]
        if mode is not None:
            argv += ["--mode", mode]
        if extra_args:
            argv += extra_args
        result = subprocess.run(
            argv,
            cwd=self.root,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=30,
        )
        return result, target

    def test_plan_dispatch_streams_and_atomically_publishes(self) -> None:
        result, target = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^STATE-DIR .+\n$")
        self.assertIn("Conclusion: complete", target.read_text(encoding="utf-8"))

        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("gemini-3.8-flash-high", args)
        self.assertIn("high", args)
        self.assertEqual(args[args.index("--mode") + 1], "plan")
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertNotIn("--disable-slash-commands", args)
        self.assertNotIn("--sandbox", args)
        actual_cwd = Path((self.log / "cwd.txt").read_text(encoding="utf-8"))
        self.assertTrue(
            actual_cwd.samefile(self.work), f"{actual_cwd} != {self.work}"
        )
        relay = (self.log / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("Audit section 3", relay)
        self.assertIn("Never commit, push", relay)

        state_dir = Path(result.stdout.strip().split(" ", 1)[1])
        for name in (
            "timestamp",
            "pre-mtime",
            "result-file",
            "dispatch-pid",
            "python-interpreter",
            "worker-pid-unverified",
            "tail",
            "tail.stderr-tmp",
        ):
            self.assertTrue((state_dir / name).is_file(), name)

    def test_default_mode_is_accept_edits_with_skip_permissions(self) -> None:
        result, _ = self._run(mode=None, own_cwd=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--mode") + 1], "accept-edits")
        self.assertIn("--dangerously-skip-permissions", args)
        state_dir = Path(result.stdout.strip().split(" ", 1)[1])
        actual_cwd = Path((self.log / "cwd.txt").read_text(encoding="utf-8"))
        self.assertTrue(actual_cwd.samefile(state_dir / "work"))

    def test_omitted_mode_with_caller_workspace_fails_before_launch(self) -> None:
        # Review finding: the unattended default must not reach a directory
        # the caller supplied, which could be the real checkout.
        for label, kwargs in (
            ("PRUN_SCRATCH_CWD", {}),
            ("--add-dir", {"own_cwd": True, "extra_args": ["--add-dir", str(self.work)]}),
        ):
            with self.subTest(label):
                if (self.log / "args.json").exists():
                    (self.log / "args.json").unlink()
                result, target = self._run(mode=None, **kwargs)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("explicit", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(target.exists())
                self.assertFalse((self.log / "args.json").exists())

    def test_explicit_accept_edits_in_clone_keeps_unattended_permissions(self) -> None:
        result, _ = self._run(mode="accept-edits")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("--dangerously-skip-permissions", args)
        actual_cwd = Path((self.log / "cwd.txt").read_text(encoding="utf-8"))
        self.assertTrue(actual_cwd.samefile(self.work))

    def test_empty_add_dir_and_continue_from_fail_closed(self) -> None:
        # Review finding: an unset shell variable must not resolve to the
        # invocation directory or silently start a fresh conversation.
        for label, extra in (
            ("--add-dir", ["--add-dir", ""]),
            ("--add-dir-blank", ["--add-dir", "   "]),
            ("--continue-from", ["--continue-from", ""]),
            ("--continue-from-blank", ["--continue-from", " "]),
        ):
            with self.subTest(label):
                if (self.log / "args.json").exists():
                    (self.log / "args.json").unlink()
                result, target = self._run(extra_args=extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("non-empty", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(target.exists())
                self.assertFalse((self.log / "args.json").exists())

    def test_sandbox_is_off_by_default_and_opt_in_via_env(self) -> None:
        # On Windows Agy's sandbox spawns an elevated admin broker
        # (agy --exebox-admin-broker), one UAC prompt per unit that runs a
        # command, so the dispatcher must not ask for it unless told to.
        result, _ = self._run(mode="accept-edits")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertNotIn("--sandbox", args)

        (self.log / "args.json").unlink()
        result, _ = self._run(
            result_path=self.root / "unit-result-sandbox.md",
            mode="accept-edits",
            extra_env={"PRUN_AGY_SANDBOX": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("--sandbox", args)
        self.assertIn("--dangerously-skip-permissions", args)

    def test_sandbox_env_spellings_are_case_and_whitespace_insensitive(self) -> None:
        for index, (value, expected) in enumerate(
            (("true", True), (" YES ", True), ("On", True), ("0", False), ("False", False), (" off", False))
        ):
            with self.subTest(value=value):
                if (self.log / "args.json").exists():
                    (self.log / "args.json").unlink()
                result, _ = self._run(
                    result_path=self.root / f"unit-result-spelling-{index}.md",
                    mode="plan",
                    extra_env={"PRUN_AGY_SANDBOX": value},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
                self.assertEqual("--sandbox" in args, expected)
                self.assertEqual(args[args.index("--mode") + 1], "plan")
                self.assertNotIn("--dangerously-skip-permissions", args)

    def test_unrecognized_sandbox_env_fails_before_launch(self) -> None:
        for value in ("maybe", "sandbox"):
            with self.subTest(value):
                if (self.log / "args.json").exists():
                    (self.log / "args.json").unlink()
                result, target = self._run(extra_env={"PRUN_AGY_SANDBOX": value})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("PRUN_AGY_SANDBOX", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(target.exists())
                self.assertFalse((self.log / "args.json").exists())

    def test_plan_mode_carries_neither_accept_edits_nor_skip(self) -> None:
        result, _ = self._run(mode="plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--mode") + 1], "plan")
        self.assertNotIn("--dangerously-skip-permissions", args)

    def test_conversation_id_is_recorded_from_init_event(self) -> None:
        result, _ = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        state_dir = Path(result.stdout.strip().split(" ", 1)[1])
        recorded = (state_dir / "conversation-id").read_text(encoding="utf-8")
        self.assertEqual(recorded, "conv-fixed-0001\n")

    def test_no_init_event_conversation_id_writes_no_file(self) -> None:
        result, _ = self._run(extra_env={"MOCK_AGY_NO_INIT_CONVERSATION_ID": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        state_dir = Path(result.stdout.strip().split(" ", 1)[1])
        self.assertFalse((state_dir / "conversation-id").exists())

    def test_add_dir_is_forwarded_for_each_directory(self) -> None:
        dir_a = self.root / "extra-a"
        dir_b = self.root / "extra-b"
        dir_a.mkdir()
        dir_b.mkdir()
        result, _ = self._run(
            extra_args=["--add-dir", str(dir_a), "--add-dir", str(dir_b)]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        positions = [i for i, token in enumerate(args) if token == "--add-dir"]
        self.assertEqual(len(positions), 2)
        forwarded = {args[i + 1] for i in positions}
        self.assertEqual(forwarded, {str(dir_a.resolve()), str(dir_b.resolve())})

    def test_add_dir_rejects_missing_directory(self) -> None:
        missing = self.root / "does-not-exist"
        result, target = self._run(extra_args=["--add-dir", str(missing)])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertFalse(target.exists())
        self.assertFalse((self.log / "args.json").exists())

    def test_continue_from_forwards_conversation_flag(self) -> None:
        first_result, _ = self._run()
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        first_state_dir = Path(first_result.stdout.strip().split(" ", 1)[1])
        recorded_id = (first_state_dir / "conversation-id").read_text(
            encoding="utf-8"
        ).strip()
        self.assertTrue(recorded_id)

        second_result, _ = self._run(
            result_path=self.root / "unit-result-continued.md",
            extra_args=["--continue-from", str(first_state_dir)],
        )
        self.assertEqual(second_result.returncode, 0, second_result.stderr)
        args = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--conversation") + 1], recorded_id)

    def test_continue_from_missing_conversation_id_file_exits_before_launch(
        self,
    ) -> None:
        empty_dir = self.root / "state-without-id"
        empty_dir.mkdir()
        result, target = self._run(
            extra_args=["--continue-from", str(empty_dir)]
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertFalse(target.exists())
        self.assertFalse((self.log / "args.json").exists())

        blank_id_dir = self.root / "state-with-blank-id"
        blank_id_dir.mkdir()
        (blank_id_dir / "conversation-id").write_text("\n", encoding="utf-8")
        result2, target2 = self._run(
            result_path=self.root / "unit-result-2.md",
            extra_args=["--continue-from", str(blank_id_dir)],
        )
        self.assertEqual(result2.returncode, 2)
        self.assertEqual(result2.stdout, "")
        self.assertFalse(target2.exists())

    def test_an_exhausted_second_group_falls_back_to_the_gemini_default(self) -> None:
        # The unit runs instead of failing, because the Gemini default is what
        # it would have used anyway. Seven units of the 2026-09-11 fan-out were
        # aimed at the Claude group and four died in a row on its 5-hour meter.
        result, target = self._run(
            extra_env={
                "AGY_QUOTA_CACHE": self._quota(gemini=0.86, second=0.0),
                "ANTIGRAVITY_DISPATCH_MODEL": "claude-opus-4-6-thinking",
                "MOCK_AGY_MODELS": "gemini-3.8-flash-high\nclaude-opus-4-6-thinking",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("gemini-3.8-flash-high", argv)
        self.assertNotIn("claude-opus-4-6-thinking", argv)
        # Recorded rather than silent: the ledger must not read as though the
        # unit ran on the model the caller named.
        self.assertIn("MODEL-FALLBACK", result.stderr)
        self.assertIn("claude-and-gpt-quota-exhausted", result.stderr)
        state_dir = Path(result.stdout.split("STATE-DIR ", 1)[1].strip())
        self.assertEqual(
            (state_dir / "model").read_text(encoding="utf-8").strip(),
            "gemini-3.8-flash-high",
        )
        self.assertTrue(target.is_file())

    def test_a_named_gemini_model_is_not_escalated_when_its_group_empties(self) -> None:
        # Spending the smaller metered group on a model someone chose is that
        # person's call. An agent took it unasked once and left it at 60% in a
        # day. An unpinned unit routes on the meters instead; see below.
        result, target = self._run(
            extra_env={
                "AGY_QUOTA_CACHE": self._quota(gemini=0.0, second=1.0),
                "ANTIGRAVITY_DISPATCH_MODEL": "gemini-3.8-flash-high",
            }
        )
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn("ANTIGRAVITY_DISPATCH_MODEL", result.stderr)
        self.assertFalse((self.log / "args.json").is_file())
        self.assertIn("FALLBACK", target.read_text(encoding="utf-8"))

    def test_an_unpinned_unit_runs_on_the_group_with_the_freer_meter(self) -> None:
        # A unit is shallow work that either group handles, so on 2026-09-15 the
        # rule became "whichever meter has room". The Gemini five-hour bucket
        # was at 20% that afternoon while the second group had not been touched.
        result, target = self._run(
            extra_env={
                "AGY_QUOTA_CACHE": self._quota(gemini=0.2, second=1.0),
                "MOCK_AGY_MODELS": "gemini-3.8-flash-high\n" + self.module.SECOND_MODEL,
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn(self.module.SECOND_MODEL, argv)
        self.assertNotIn("--effort", argv)
        self.assertIn("MODEL-BALANCE", result.stderr)
        state_dir = Path(result.stdout.split("STATE-DIR ", 1)[1].strip())
        self.assertEqual(
            (state_dir / "model").read_text(encoding="utf-8").strip(),
            self.module.SECOND_MODEL,
        )
        self.assertIn("MODEL-BALANCE", (state_dir / "quota-note").read_text(encoding="utf-8"))
        self.assertTrue(target.is_file())

    def test_an_empty_gemini_group_moves_an_unpinned_unit_rather_than_stopping_it(self) -> None:
        # The lead here is under the margin, so only the empty own group moves
        # the unit. This is the case that used to exit 75 and stall a batch.
        result, target = self._run(
            extra_env={
                "AGY_QUOTA_CACHE": self._quota(gemini=0.0, second=0.1),
                "MOCK_AGY_MODELS": "gemini-3.8-flash-high\n" + self.module.SECOND_MODEL,
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn(self.module.SECOND_MODEL, argv)
        self.assertTrue(target.is_file())

    def test_both_groups_exhausted_stops_the_dispatch(self) -> None:
        result, _ = self._run(
            extra_env={"AGY_QUOTA_CACHE": self._quota(gemini=0.0, second=0.0)}
        )
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn("both Agy quota groups are exhausted", result.stderr)
        self.assertFalse((self.log / "args.json").is_file())

    def test_a_claude_model_is_dispatched_without_effort(self) -> None:
        # Agy rejects `--effort` for that group, so the unconditional flag made
        # the whole group unreachable.
        result, _ = self._run(
            extra_env={
                "AGY_QUOTA_CACHE": self._quota(gemini=0.86, second=0.6),
                "ANTIGRAVITY_DISPATCH_MODEL": "claude-opus-4-6-thinking",
                "MOCK_AGY_MODELS": "gemini-3.8-flash-high\nclaude-opus-4-6-thinking",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("claude-opus-4-6-thinking", argv)
        self.assertNotIn("--effort", argv)

    def test_a_gemini_model_still_carries_effort(self) -> None:
        result, _ = self._run(
            extra_env={"AGY_QUOTA_CACHE": self._quota(gemini=0.86, second=0.6)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_an_unreadable_snapshot_does_not_gate_the_dispatch(self) -> None:
        # The gate exists to stop a known-empty group. A missing or malformed
        # snapshot is not that, and refusing on it would block work whenever
        # the readout is unavailable.
        missing = self.root / "no-such-quota.json"
        result, target = self._run(extra_env={"AGY_QUOTA_CACHE": str(missing)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(target.is_file())

    def test_a_backend_failure_forces_a_refresh_past_the_readout_ttl(self) -> None:
        # The readout keeps a five-minute TTL, so an unforced refresh after a
        # quota death asks nothing and the next unit routes on the fraction
        # that was already wrong. That is the repeat this gate exists to stop.
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        calls = self.root / "refresh-calls.txt"
        stub = home / ".claude" / "agent-quota.py"
        stub.write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['REFRESH_CALLS']).open('a', encoding='utf-8')"
            ".write(' '.join(sys.argv[1:]) + '\\n')\n"
            "cache = Path.home() / '.claude' / 'agy-quota-cache.json'\n"
            "cache.write_text(json.dumps({'usage': {'groups': ["
            "{'name': 'Gemini Models', 'buckets': [{'remaining_fraction': 0.9}]},"
            "{'name': 'Claude and GPT models', 'buckets': [{'remaining_fraction': 0.0}]}"
            "]}}), encoding='utf-8')\n",
            encoding="utf-8",
        )
        cache = home / ".claude" / "agy-quota-cache.json"
        cache.write_text(
            json.dumps(
                {
                    "usage": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [{"remaining_fraction": 0.9}],
                            },
                            {
                                "name": "Claude and GPT models",
                                "buckets": [{"remaining_fraction": 0.4}],
                            },
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        result, _ = self._run(
            extra_env={
                # Empty removes the key, so the dispatcher reads the managed
                # snapshot under the fake home and may refresh it.
                "AGY_QUOTA_CACHE": "",
                "HOME": str(home),
                "USERPROFILE": str(home),
                "REFRESH_CALLS": str(calls),
                "ANTIGRAVITY_DISPATCH_MODEL": "claude-opus-4-6-thinking",
                "MOCK_AGY_MODELS": "gemini-3.8-flash-high\nclaude-opus-4-6-thinking",
                "MOCK_AGY_STATUS": "ERROR",
                "MOCK_AGY_ERROR": "Individual quota reached. Resets in 34m7s.",
            }
        )
        self.assertEqual(result.returncode, 70, result.stderr)
        self.assertIn("--force", calls.read_text(encoding="utf-8"))
        refreshed = json.loads(cache.read_text(encoding="utf-8"))
        second = refreshed["usage"]["groups"][1]["buckets"][0]["remaining_fraction"]
        self.assertEqual(second, 0.0)

    def test_nonzero_exit_publishes_fallback(self) -> None:
        result, target = self._run(extra_env={"MOCK_AGY_EXIT": "9"})
        self.assertEqual(result.returncode, 9)
        body = target.read_text(encoding="utf-8")
        self.assertIn("FALLBACK", body)
        self.assertIn("Agy exited with code 9", body)

    def test_missing_result_event_publishes_fallback(self) -> None:
        result, target = self._run(extra_env={"MOCK_AGY_NO_RESULT": "1"})
        self.assertEqual(result.returncode, 70)
        self.assertIn("without a final result", target.read_text(encoding="utf-8"))

    def test_unavailable_model_fails_before_task_and_publishes_fallback(self) -> None:
        result, target = self._run(
            extra_env={"MOCK_AGY_MODELS": "gemini-3.1-pro-high"}
        )
        self.assertEqual(result.returncode, 70)
        self.assertFalse((self.log / "prompt.txt").exists())
        self.assertIn("unavailable", target.read_text(encoding="utf-8"))

    def test_worker_written_result_is_kept_and_response_stored_beside(self) -> None:
        # A live unit wrote its trace table to the result file and then replied
        # with a one-line summary; the dispatcher replaced the table with the
        # summary. The worker's file must win and the response must land beside it.
        target = self.root / "unit-result.md"
        result, target = self._run(
            result_path=target,
            extra_env={
                "MOCK_AGY_WRITE_RESULT": str(target),
                "MOCK_AGY_RESPONSE": "done",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        body = target.read_text(encoding="utf-8")
        self.assertIn("Conclusion: worker-written", body)
        self.assertIn("| a | 1 |", body)
        beside = self.root / "unit-result.response.md"
        self.assertEqual(beside.read_text(encoding="utf-8"), "done\n")
        self.assertNotIn("FALLBACK", body)

    def test_error_status_publishes_fallback_with_the_backend_error(self) -> None:
        # Agy exits 0 after a quota stop and still fills the ERROR event's
        # response with the model's opening narration. Four units of one
        # 2026-09-11 fan-out published that narration as their result.
        narration = (
            "I will begin by reading the required documents in order, "
            "starting with the plan file, and then work through each section.\n"
        )
        result, target = self._run(
            extra_env={
                "MOCK_AGY_STATUS": "ERROR",
                "MOCK_AGY_ERROR": "Individual quota reached. Resets in 34m7s.",
                "MOCK_AGY_RESPONSE": narration,
            }
        )
        self.assertEqual(result.returncode, 70, result.stderr)
        body = target.read_text(encoding="utf-8")
        lines = body.splitlines()
        self.assertIn("FALLBACK", lines[0])
        # Assert on the header, not anywhere in the body: publish_fallback
        # embeds the captured tail, which carries the same error text, so a
        # body-wide search passes even when the reason never reaches the reader.
        conclusion = next(x for x in lines if x.startswith("Conclusion:"))
        self.assertIn("ERROR", conclusion)
        self.assertIn("Individual quota reached", conclusion)
        self.assertIn("Individual quota reached", result.stderr)
        self.assertNotEqual(lines[0], narration.splitlines()[0])

    def test_error_status_keeps_worker_result_and_exits_nonzero(self) -> None:
        # The stream-interrupted case: the worker had written its result before
        # the run was cut short, so that file must survive while the dispatcher
        # still exits non-zero. The monitors read only the first line, so they
        # report the unit done; the exit code is what records the stop.
        target = self.root / "unit-result.md"
        result, target = self._run(
            result_path=target,
            extra_env={
                "MOCK_AGY_WRITE_RESULT": str(target),
                "MOCK_AGY_STATUS": "ERROR",
                "MOCK_AGY_ERROR": "The stream was interrupted.",
                "MOCK_AGY_RESPONSE": "partial narration before the stop",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), WORKER_RESULT_TEXT)
        beside = self.root / "unit-result.response.md"
        self.assertEqual(
            beside.read_text(encoding="utf-8"),
            "partial narration before the stop\n",
        )
        self.assertIn("The stream was interrupted.", result.stderr)

    def test_trailing_status_free_result_event_cannot_erase_an_error(self) -> None:
        # A second result event that omits the field must not read as "no
        # verdict"; otherwise the quota stop republishes the narration.
        result, target = self._run(
            extra_env={
                "MOCK_AGY_STATUS": "ERROR",
                "MOCK_AGY_ERROR": "Individual quota reached.",
                "MOCK_AGY_RESPONSE": "narration long enough to clear the floor",
                "MOCK_AGY_TRAILING_RESULT_WITHOUT_STATUS": "1",
            }
        )
        self.assertEqual(result.returncode, 70, result.stderr)
        self.assertIn("FALLBACK", target.read_text(encoding="utf-8").splitlines()[0])

    def test_nonzero_exit_keeps_a_worker_written_result(self) -> None:
        # The hard-exit path shares the worker-result branch, so a crash after
        # the worker published must not replace its file with a FALLBACK.
        target = self.root / "unit-result.md"
        result, target = self._run(
            result_path=target,
            extra_env={
                "MOCK_AGY_WRITE_RESULT": str(target),
                "MOCK_AGY_EXIT": "9",
                "MOCK_AGY_RESPONSE": "partial narration before the crash",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), WORKER_RESULT_TEXT)
        beside = self.root / "unit-result.response.md"
        self.assertEqual(
            beside.read_text(encoding="utf-8").strip(),
            "partial narration before the crash",
        )

    def test_success_status_publishes_normally(self) -> None:
        result, target = self._run(extra_env={"MOCK_AGY_STATUS": "SUCCESS"})
        self.assertEqual(result.returncode, 0, result.stderr)
        body = target.read_text(encoding="utf-8")
        self.assertIn("Conclusion: complete", body)
        self.assertNotIn("FALLBACK", body)

    def test_result_event_without_status_publishes_normally(self) -> None:
        # An Agy build that omits the field has to keep working.
        result, target = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Conclusion: complete", target.read_text(encoding="utf-8"))
        state_dir = Path(result.stdout.strip().split(" ", 1)[1])
        events = [
            json.loads(line)
            for line in (state_dir / "tail").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        final = [event for event in events if event.get("event") == "result"][-1]
        self.assertNotIn("status", final["result"])

    def test_existing_result_is_never_overwritten(self) -> None:
        target = self.root / "existing.md"
        target.write_text("keep me\n", encoding="utf-8")
        result, _ = self._run(result_path=target)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(result.stdout, "")

    def test_unpinned_unit_floats_to_newest_gemini_model(self) -> None:
        result, target = self._run(
            extra_env={
                "MOCK_AGY_MODELS": (
                    "gemini-3.8-flash-high\n"
                    "gemini-3.9-flash-high\n"
                    "claude-sonnet-4-6"
                ),
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("gemini-3.9-flash-high", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        state_dir = Path(result.stdout.split("STATE-DIR ", 1)[1].strip())
        self.assertEqual(
            (state_dir / "model").read_text(encoding="utf-8").strip(),
            "gemini-3.9-flash-high",
        )
        self.assertIn(
            "MODEL-RESOLVE from=gemini-3.8-flash-high to=gemini-3.9-flash-high reason=newest-in-family",
            result.stderr,
        )
        self.assertTrue(target.is_file())

    def test_balanced_unit_floats_to_newest_claude_model(self) -> None:
        result, target = self._run(
            extra_env={
                "AGY_QUOTA_CACHE": self._quota(gemini=0.2, second=1.0),
                "MOCK_AGY_MODELS": (
                    "claude-opus-5-5-high\n"
                    "claude-sonnet-5-5-high\n"
                    "claude-sonnet-5-6-medium\n"
                    "claude-sonnet-5-6-high"
                ),
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("claude-sonnet-5-6-high", argv)
        self.assertNotIn("--effort", argv)
        state_dir = Path(result.stdout.split("STATE-DIR ", 1)[1].strip())
        self.assertEqual(
            (state_dir / "model").read_text(encoding="utf-8").strip(),
            "claude-sonnet-5-6-high",
        )
        self.assertIn(
            "MODEL-RESOLVE from=claude-sonnet-5-5-high to=claude-sonnet-5-6-high reason=newest-in-family",
            result.stderr,
        )
        self.assertTrue(target.is_file())

    def test_an_unlisted_second_template_falls_back_to_gemini(self) -> None:
        # Agy 1.2.16 retired claude-sonnet-4-6 for slugs that carry the effort
        # tier, so the old template matched nothing and every balanced unit
        # failed its preflight. A balanced unit now runs on the Gemini default
        # instead, and the saved trail says why.
        result, target = self._run(
            extra_env={
                "AGY_QUOTA_CACHE": self._quota(gemini=0.2, second=1.0),
                "MOCK_AGY_MODELS": (
                    "gemini-3.8-flash-high\n"
                    "gemini-3.9-flash-high\n"
                    "claude-sonnet-9-0-renamed"
                ),
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("gemini-3.9-flash-high", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertIn(
            f"MODEL-FALLBACK from={self.module.SECOND_MODEL} to=gemini-3.9-flash-high "
            "reason=template-unlisted",
            result.stderr,
        )
        state_dir = Path(result.stdout.split("STATE-DIR ", 1)[1].strip())
        self.assertEqual(
            (state_dir / "model").read_text(encoding="utf-8").strip(),
            "gemini-3.9-flash-high",
        )
        note = (state_dir / "quota-note").read_text(encoding="utf-8").splitlines()
        self.assertTrue(note[0].startswith("MODEL-BALANCE"))
        self.assertEqual(
            note[1],
            f"MODEL-FALLBACK from={self.module.SECOND_MODEL} to=gemini-3.9-flash-high "
            "reason=template-unlisted",
        )
        self.assertTrue(target.is_file())

    def test_a_fallback_into_an_empty_gemini_group_stops_before_launch(self) -> None:
        # The router chose the second group because Gemini was empty; falling
        # back to Gemini must not skip the quota gate it just passed.
        result, target = self._run(
            extra_env={
                "AGY_QUOTA_CACHE": self._quota(gemini=0.0, second=1.0),
                "MOCK_AGY_MODELS": "gemini-3.8-flash-high\nclaude-sonnet-9-0-renamed",
            }
        )
        self.assertEqual(result.returncode, self.module.QUOTA_EXHAUSTED_EXIT, result.stderr)
        self.assertIn("MODEL-FALLBACK", result.stderr)
        self.assertFalse((self.log / "args.json").is_file())
        # The saved trail explains both steps even though the unit stopped.
        state_dir = Path(result.stdout.split("STATE-DIR ", 1)[1].strip())
        note = (state_dir / "quota-note").read_text(encoding="utf-8").splitlines()
        self.assertTrue(note[0].startswith("MODEL-BALANCE"))
        self.assertTrue(note[1].startswith("MODEL-FALLBACK"))
        self.assertIn("reason=template-unlisted", note[1])
        self.assertIn("FALLBACK", target.read_text(encoding="utf-8"))

    def test_effort_follows_a_resolved_tier_other_than_high(self) -> None:
        # The strict mock rejects a conflicting --effort, as Agy 1.2.16 does.
        result, target = self._run(
            extra_env={"MOCK_AGY_MODELS": "gemini-3.9-flash-medium\ngemini-3.9-flash-low"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("gemini-3.9-flash-medium", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertTrue(target.is_file())

    def test_newest_in_family_copies_are_identical(self) -> None:
        # The two skills deploy separately, so each carries its own copy.
        reviewer = ROOT / "skills" / "implement-review" / "scripts" / "dispatch-gemini.py"

        def helper(path: Path) -> str:
            text = path.read_text(encoding="utf-8")
            start = text.index("def newest_in_family(")
            return text[start:text.index("\n\n\ndef ", start)]

        self.assertEqual(helper(DISPATCH), helper(reviewer))

    def test_an_unlisted_named_model_still_fails_the_preflight(self) -> None:
        # The fallback covers the shipped template only; a model the caller
        # named is a choice, so its absence is reported rather than replaced.
        result, target = self._run(
            extra_env={
                "ANTIGRAVITY_DISPATCH_MODEL": "claude-sonnet-5-5-high",
                "MOCK_AGY_MODELS": "gemini-3.8-flash-high\nclaude-sonnet-9-0-renamed",
            }
        )
        self.assertEqual(result.returncode, 70, result.stderr)
        self.assertIn("unavailable", result.stderr)
        self.assertNotIn("MODEL-FALLBACK", result.stderr)
        self.assertFalse((self.log / "args.json").is_file())
        self.assertIn("FALLBACK", target.read_text(encoding="utf-8"))

    def test_named_model_is_never_floated(self) -> None:
        result, target = self._run(
            extra_env={
                "ANTIGRAVITY_DISPATCH_MODEL": "claude-sonnet-4-6",
                "MOCK_AGY_MODELS": "claude-sonnet-4-6\nclaude-sonnet-4-7",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("claude-sonnet-4-6", argv)
        self.assertNotIn("claude-sonnet-4-7", argv)
        self.assertNotIn("MODEL-RESOLVE", result.stderr)
        state_dir = Path(result.stdout.split("STATE-DIR ", 1)[1].strip())
        self.assertEqual(
            (state_dir / "model").read_text(encoding="utf-8").strip(),
            "claude-sonnet-4-6",
        )
        self.assertTrue(target.is_file())

    def test_model_fallback_path_floats_to_newest_gemini(self) -> None:
        result, target = self._run(
            extra_env={
                "AGY_QUOTA_CACHE": self._quota(gemini=0.86, second=0.0),
                "ANTIGRAVITY_DISPATCH_MODEL": "claude-opus-4-6-thinking",
                "MOCK_AGY_MODELS": (
                    "gemini-3.8-flash-high\n"
                    "gemini-3.9-flash-high\n"
                    "claude-opus-4-6-thinking"
                ),
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("gemini-3.9-flash-high", argv)
        self.assertNotIn("claude-opus-4-6-thinking", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertIn("MODEL-FALLBACK", result.stderr)
        self.assertIn(
            "MODEL-RESOLVE from=gemini-3.8-flash-high to=gemini-3.9-flash-high reason=newest-in-family",
            result.stderr,
        )
        state_dir = Path(result.stdout.split("STATE-DIR ", 1)[1].strip())
        self.assertEqual(
            (state_dir / "model").read_text(encoding="utf-8").strip(),
            "gemini-3.9-flash-high",
        )
        # The saved trail explains the gap between the routed template and
        # the model that ran.
        note = (state_dir / "quota-note").read_text(encoding="utf-8").splitlines()
        self.assertTrue(note[0].startswith("MODEL-FALLBACK from=claude-opus-4-6-thinking"))
        self.assertEqual(
            note[1],
            "MODEL-RESOLVE from=gemini-3.8-flash-high to=gemini-3.9-flash-high reason=newest-in-family",
        )
        self.assertTrue(target.is_file())

    def test_preflight_off_keeps_constant_model(self) -> None:
        result, target = self._run(
            extra_env={
                "ANTIGRAVITY_PREFLIGHT": "off",
                "MOCK_AGY_MODELS": "gemini-3.9-flash-high",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("gemini-3.8-flash-high", argv)
        self.assertNotIn("gemini-3.9-flash-high", argv)
        self.assertNotIn("MODEL-RESOLVE", result.stderr)
        state_dir = Path(result.stdout.split("STATE-DIR ", 1)[1].strip())
        self.assertEqual(
            (state_dir / "model").read_text(encoding="utf-8").strip(),
            "gemini-3.8-flash-high",
        )
        self.assertTrue(target.is_file())

    def test_user_named_default_model_is_not_floated(self) -> None:
        result, target = self._run(
            extra_env={
                "ANTIGRAVITY_DISPATCH_MODEL": "gemini-3.8-flash-high",
                "MOCK_AGY_MODELS": "gemini-3.8-flash-high\ngemini-3.9-flash-high",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads((self.log / "args.json").read_text(encoding="utf-8"))
        self.assertIn("gemini-3.8-flash-high", argv)
        self.assertNotIn("gemini-3.9-flash-high", argv)
        self.assertNotIn("MODEL-RESOLVE", result.stderr)
        state_dir = Path(result.stdout.split("STATE-DIR ", 1)[1].strip())
        self.assertEqual(
            (state_dir / "model").read_text(encoding="utf-8").strip(),
            "gemini-3.8-flash-high",
        )
        self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
