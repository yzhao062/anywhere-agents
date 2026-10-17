"""Tests for scripts/render_banner.py and scripts/pack_identity.py.

The renderer replaces the 10.9 KB of banner instructions the shared
AGENTS.md used to carry. These tests pin three things: the fields it derives
from disk, the report contract an agent reads (one metadata comment, then
the seven banner lines), and the acceptance rule that decides between the
report and the fixed fallback banner. Field collectors that shell out
(``claude --version``, ``codex --version``) are patched so the suite is
hermetic; the home directory is redirected so a developer's real
``~/.claude`` and ``~/.codex`` never enter a measurement.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# tests/ is on sys.path under `unittest discover -s tests` but not under
# `python -m unittest tests.<module>`, which validate.yml uses for the
# Sentinel redaction smoke. Put it there before the sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pack_identity  # noqa: E402
import render_banner  # noqa: E402

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ledger(run_id: str = "111-222", completed: bool = True, last_phase: str = "finalize",
            steps=None) -> str:
    return json.dumps({
        "schema": 1,
        "emitted_by": "test",
        "run_id": run_id,
        "started_at": "2026-09-17T00:00:00Z",
        "upstream": "yzhao062/anywhere-agents",
        "completed": completed,
        "last_phase": last_phase,
        "steps": steps or [],
    })


class _HermeticCase(unittest.TestCase):
    """A consumer root plus a private home, with the CLI probes patched."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="banner-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.root = self.tmp / "project"
        (self.home / ".claude" / "hooks").mkdir(parents=True)
        _write(self.home / ".claude" / "hooks" / "guard.py", "# guard\n")
        _write(self.home / ".claude" / "hooks" / "session_bootstrap.py", "# hook\n")
        # A fresh checked_at keeps the hook's update_version_cache off the
        # network when a test launches the real hook.
        _write(self.home / ".claude" / "hooks" / "version-cache.json",
               json.dumps({"checked_at": time.time(), "claude_latest": "2.1.0", "codex_latest": "0.150.0"}))
        _write(self.home / ".claude" / "settings.json",
               json.dumps({"env": {"CLAUDE_CODE_EFFORT_LEVEL": "max"}}))
        _write(self.home / ".codex" / "config.toml",
               'model = "gpt-6-astra"\nmodel_reasoning_effort = "xhigh"\nservice_tier = "standard"\n'
               "project_doc_max_bytes = 262144\n\n[features]\nfast_mode = false\n")
        (self.root / ".agent-config").mkdir(parents=True)
        _write(self.root / ".agent-config" / "bootstrap.sh", "# marker\n")
        # The lock records the seeded defaults' identity, which is what a
        # consumer bootstrapped from anywhere-agents carries; without it the
        # pack check reports unavailable rather than a zero.
        _write(self.root / ".agent-config" / "pack-lock.json", json.dumps({"version": 1, "packs": {
            "agent-style": {"source_url": "https://github.com/yzhao062/agent-style", "requested_ref": "v0.4.1",
                            "resolved_commit": "abc", "latest_known_head": "abc"},
            "aa-core-skills": {"source_url": "bundled:aa", "requested_ref": "bundled",
                               "resolved_commit": "bundled"},
        }}))
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        for name in ("DISABLE_AUTOUPDATER", "CLAUDE_CODE_EFFORT_LEVEL", "AGENT_CONFIG_HOST",
                     "XDG_CONFIG_HOME", "APPDATA", "AGENT_STYLE_HOOK", "AGENT_CONFIG_GATES"):
            env[name] = ""
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        self.addCleanup(self._env.stop)
        for name in ("DISABLE_AUTOUPDATER", "CLAUDE_CODE_EFFORT_LEVEL", "AGENT_CONFIG_HOST",
                     "XDG_CONFIG_HOME", "APPDATA", "AGENT_STYLE_HOOK", "AGENT_CONFIG_GATES"):
            os.environ.pop(name, None)
        versions = {"claude": "2.1.0", "codex": "0.150.0"}
        self._versions = mock.patch.object(render_banner, "_run_version", side_effect=versions.get)
        self._versions.start()
        self.addCleanup(self._versions.stop)
        # The agent_style probe imports from the test interpreter; pin it so
        # the result does not depend on what that interpreter has installed.
        self._style = mock.patch.object(render_banner, "agent_style_installed", return_value="0.7.0")
        self._style.start()
        self.addCleanup(self._style.stop)


class MetadataContractTests(unittest.TestCase):
    """The one line an agent reads to decide between report and fallback."""

    def test_round_trip(self) -> None:
        line = render_banner.metadata_line(1789685280.1234, "52516-1789685000", True)
        self.assertTrue(line.startswith(render_banner.METADATA_PREFIX))
        self.assertTrue(line.endswith("-->"))
        meta = render_banner.parse_metadata(line + "\nbody\n")
        self.assertEqual(meta["event_ts"], 1789685280.1234)
        self.assertEqual(meta["run_id"], "52516-1789685000")
        self.assertIs(meta["completed"], True)

    def test_an_event_too_large_for_a_float_reads_as_no_event(self) -> None:
        """A JSON integer has no width limit and a float does.

        `isinstance(ts, int)` passes for a 400-digit number and the conversion
        then raises, which ended the render and left the consumer with no
        report at all. Reading it as no timestamp is what every other unusable
        value here does.
        """
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".agent-config"
            agent_dir.mkdir()
            (agent_dir / "session-event.json").write_text(
                '{"ts": %s}' % ("1" + "0" * 400), encoding="utf-8"
            )
            self.assertIsNone(render_banner.read_event_ts(tmp))

    def test_no_event_is_recorded_as_none(self) -> None:
        meta = render_banner.parse_metadata(render_banner.metadata_line(None, "r1", False))
        self.assertIsNone(meta["event_ts"])
        self.assertIs(meta["completed"], False)

    def test_old_format_or_absent_report_is_never_current(self) -> None:
        for text in ("", "📦 anywhere-agents active\n   └── Session check: all clear\n", "<!-- unrelated -->\nx\n"):
            self.assertIsNone(render_banner.parse_metadata(text))
            self.assertFalse(render_banner.report_is_current(text, 1.0, "r1"))

    def test_matching_event_and_run_id_is_current(self) -> None:
        report = render_banner.metadata_line(100.5, "r1", True) + "\nline\n"
        self.assertTrue(render_banner.report_is_current(report, 100.5, "r1"))

    def test_mismatched_event_is_not_current(self) -> None:
        report = render_banner.metadata_line(100.5, "r1", True) + "\nline\n"
        self.assertFalse(render_banner.report_is_current(report, 101.0, "r1"))

    def test_mismatched_run_id_is_not_current(self) -> None:
        report = render_banner.metadata_line(100.5, "r1", True) + "\nline\n"
        self.assertFalse(render_banner.report_is_current(report, 100.5, "r2"))
        self.assertFalse(render_banner.report_is_current(report, 100.5, None))

    def test_invocation_agent_without_event_needs_only_the_run_id(self) -> None:
        """Codex runs bootstrap itself and has no Claude event: the report is
        current when it carries the attempt's run_id, whatever event it
        recorded, and stale when a newer attempt wrote a different run_id."""
        report = render_banner.metadata_line(None, "r1", True) + "\nline\n"
        self.assertTrue(render_banner.report_is_current(report, None, "r1"))
        self.assertFalse(render_banner.report_is_current(report, None, "r2"))
        with_event = render_banner.metadata_line(55.0, "r1", True) + "\nline\n"
        self.assertTrue(render_banner.report_is_current(with_event, None, "r1"))

    def test_report_with_event_none_does_not_satisfy_a_pending_claude_event(self) -> None:
        report = render_banner.metadata_line(None, "r1", True) + "\nline\n"
        self.assertFalse(render_banner.report_is_current(report, 100.0, "r1"))

    def test_banner_body_drops_the_metadata_only(self) -> None:
        report = render_banner.metadata_line(1.0, "r1", True) + "\nA\n\nB\n"
        self.assertEqual(render_banner.banner_body(report), ["A", "B"])

    def test_fallback_is_the_fixed_three_lines(self) -> None:
        self.assertEqual(render_banner.FALLBACK_LINES, (
            "📦 anywhere-agents active",
            "   ├── Agent: <model>",
            "   └── Session check: checks unavailable (run bootstrap or read .agent-config/last-run.json)",
        ))
        agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for line in render_banner.FALLBACK_LINES:
            self.assertIn(line, agents_md, "the rule and the renderer must quote the same fallback")

    def test_shared_rule_states_the_lifecycle_the_renderer_implements(self) -> None:
        """The rule in AGENTS.md and this script are two halves of one
        contract: the metadata an agent checks, the fallback, the source-repo
        invocation, the invocation-agent branch, and the dispatched-reviewer
        exemption all have to be stated where the agent reads them."""
        agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for phrase in (
            "read `.agent-config/banner.txt`",
            "records the pending event's timestamp and the `run_id` of the current `last-run.json`",
            "copy the event `ts` into `banner-emitted.json`",
            "run `scripts/render_banner.py`",
            "a completed attempt requires the report carrying that attempt's `run_id`",
            "A dispatched reviewer told to skip the banner keeps skipping it.",
        ):
            self.assertIn(phrase, agents_md, phrase)


class ConsumerRenderTests(_HermeticCase):
    def test_seven_lines_after_the_metadata(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        report, lines = render_banner.render_consumer(str(self.root), 123.5, 0)
        self.assertEqual(len(lines), 7)
        self.assertEqual(report[0].split(" event_ts=")[0], render_banner.METADATA_PREFIX)
        self.assertEqual(report[1:], lines)
        self.assertEqual(lines[0], render_banner.TITLE)
        self.assertTrue(lines[1].endswith("OS: %s" % sys.platform))
        self.assertIn("Claude Code: 2.1.0 (auto-update: on) · <model> · effort=max", lines[2])
        self.assertIn("Codex: 0.150.0 · gpt-6-astra · xhigh · standard · fast_mode=false", lines[3])
        self.assertIn("Hooks: PreToolUse guard.py (agent-style 0.7.0), SessionStart session_bootstrap.py", lines[5])
        self.assertTrue(lines[6].endswith("Session check: all clear"), lines[6])
        meta = render_banner.parse_metadata("\n".join(report))
        self.assertEqual(meta["event_ts"], 123.5)
        self.assertEqual(meta["run_id"], "111-222")
        self.assertIs(meta["completed"], True)

    def test_event_ts_is_read_from_the_event_file_when_not_given(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        _write(self.root / ".agent-config" / "session-event.json", json.dumps({"ts": 77.25}))
        report, _ = render_banner.render_consumer(str(self.root), None, None)
        self.assertEqual(render_banner.parse_metadata("\n".join(report))["event_ts"], 77.25)

    def test_nonzero_bootstrap_result_never_reads_all_clear(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger(completed=True))
        report, lines = render_banner.render_consumer(str(self.root), None, 1)
        self.assertIn("bootstrap exited 1 at finalize", lines[6])
        self.assertNotIn("all clear", lines[6])
        self.assertIs(render_banner.parse_metadata("\n".join(report))["completed"], False)

    def test_exit_zero_incomplete_composition_names_the_skipped_step(self) -> None:
        steps = [
            {"phase": "fetch", "scope": "repo", "status": "ok", "rc": None, "targets": []},
            {"phase": "compose", "scope": "repo", "status": "skipped", "rc": None, "targets": [],
             "reason": "no Python 3 interpreter found"},
        ]
        _write(self.root / ".agent-config" / "last-run.json",
               _ledger(completed=False, last_phase="finalize", steps=steps))
        report, lines = render_banner.render_consumer(str(self.root), None, 0)
        self.assertIn("bootstrap incomplete: stopped at finalize", lines[6])
        self.assertIn("compose skipped (no Python 3 interpreter found)", lines[6])
        self.assertIs(render_banner.parse_metadata("\n".join(report))["completed"], False)

    def test_failed_step_names_its_phase_and_rc(self) -> None:
        steps = [{"phase": "compose", "scope": "repo", "status": "failed", "rc": 3, "targets": []}]
        _write(self.root / ".agent-config" / "last-run.json",
               _ledger(completed=False, last_phase="compose", steps=steps))
        _, lines = render_banner.render_consumer(str(self.root), None, 3)
        self.assertIn("bootstrap exited 3 at compose", lines[6])
        self.assertIn("compose failed rc=3", lines[6])

    def test_missing_ledger_reports_unavailable_and_no_run_id(self) -> None:
        report, lines = render_banner.render_consumer(str(self.root), None, None)
        self.assertIn("checks unavailable (no .agent-config/last-run.json)", lines[6])
        meta = render_banner.parse_metadata("\n".join(report))
        self.assertIsNone(meta["run_id"])
        self.assertIsNone(meta["completed"])
        self.assertFalse(render_banner.report_is_current("\n".join(report), None, "anything"))

    def test_missing_agent_style_under_loaded_rules_is_named_with_the_fix(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        _write(self.root / "AGENTS.md", "# rules\n<!-- rule-pack:agent-style:begin version=v0.7.0 sha256=abc -->\nbody\n")
        with mock.patch.object(render_banner, "agent_style_installed", return_value=None):
            _, lines = render_banner.render_consumer(str(self.root), None, 0)
        self.assertIn("PreToolUse guard.py (no agent-style)", lines[5])
        self.assertIn("agent-style advisory off", lines[6])
        self.assertIn("-m pip install agent-style==0.7.0", lines[6])

    def test_older_agent_style_than_the_loaded_rules_is_named(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        _write(self.root / "AGENTS.md", "<!-- rule-pack:agent-style:begin version=v0.7.0 sha256=abc -->\n")
        with mock.patch.object(render_banner, "agent_style_installed", return_value="0.6.0"):
            _, lines = render_banner.render_consumer(str(self.root), None, 0)
        self.assertIn("PreToolUse guard.py (agent-style 0.6.0)", lines[5])
        self.assertIn("agent-style 0.6.0 in", lines[6])
        self.assertIn("older than the v0.7.0 rules", lines[6])
        self.assertIn("-m pip install -U agent-style==0.7.0", lines[6])

    def test_a_consumer_without_the_pack_expects_no_agent_style(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        _write(self.root / "AGENTS.md", "# rules without the agent-style block\n")
        with mock.patch.object(render_banner, "agent_style_installed", return_value=None):
            _, lines = render_banner.render_consumer(str(self.root), None, 0)
        self.assertIn("PreToolUse guard.py (no agent-style)", lines[5])
        self.assertTrue(lines[6].endswith("Session check: all clear"), lines[6])

    def test_the_advisory_switch_turns_the_check_off(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        _write(self.root / "AGENTS.md", "<!-- rule-pack:agent-style:begin version=v0.7.0 sha256=abc -->\n")
        _write(self.home / ".claude" / "settings.json",
               json.dumps({"env": {"CLAUDE_CODE_EFFORT_LEVEL": "max", "AGENT_STYLE_HOOK": "off"}}))
        with mock.patch.object(render_banner, "agent_style_installed", return_value=None):
            _, lines = render_banner.render_consumer(str(self.root), None, 0)
        self.assertIn("PreToolUse guard.py (agent-style advisory off)", lines[5])
        self.assertTrue(lines[6].endswith("Session check: all clear"), lines[6])

    def test_the_legacy_gates_switch_also_turns_the_check_off(self) -> None:
        """The guard drops its advisory under AGENT_CONFIG_GATES as well, so
        the banner must not ask for a package the guard would not use."""
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        _write(self.root / "AGENTS.md", "<!-- rule-pack:agent-style:begin version=v0.7.0 sha256=abc -->\n")
        with mock.patch.object(render_banner, "agent_style_installed", return_value=None), \
                mock.patch.dict(os.environ, {"AGENT_CONFIG_GATES": "off", "AGENT_STYLE_HOOK": "on"}):
            _, lines = render_banner.render_consumer(str(self.root), None, 0)
        self.assertIn("PreToolUse guard.py (agent-style advisory off)", lines[5])
        self.assertTrue(lines[6].endswith("Session check: all clear"), lines[6])
        _write(self.home / ".claude" / "settings.json",
               json.dumps({"env": {"CLAUDE_CODE_EFFORT_LEVEL": "max", "AGENT_CONFIG_GATES": "0"}}))
        with mock.patch.object(render_banner, "agent_style_installed", return_value="0.7.0"), \
                mock.patch.dict(os.environ, {"AGENT_STYLE_HOOK": "on"}):
            _, lines = render_banner.render_consumer(str(self.root), None, 0)
        self.assertIn("PreToolUse guard.py (agent-style advisory off)", lines[5])

    def test_missing_hooks_and_effort_drift_are_named(self) -> None:
        os.remove(self.home / ".claude" / "hooks" / "guard.py")
        _write(self.home / ".claude" / "settings.json", json.dumps({"effortLevel": "high"}))
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        _, lines = render_banner.render_consumer(str(self.root), None, 0)
        self.assertIn("PreToolUse guard.py missing", lines[5])
        self.assertIn("guard.py missing from ~/.claude/hooks", lines[6])
        self.assertIn("effort=high", lines[2])
        self.assertIn("Claude effort is high", lines[6])

    def test_effort_accepts_xhigh_and_max_and_names_xhigh_otherwise(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        for level in ("xhigh", "max"):
            _write(self.home / ".claude" / "settings.json",
                   json.dumps({"env": {"CLAUDE_CODE_EFFORT_LEVEL": level}}))
            _, lines = render_banner.render_consumer(str(self.root), None, 0)
            self.assertIn("effort=%s" % level, lines[2])
            self.assertNotIn("Claude effort is", lines[6])
        _write(self.home / ".claude" / "settings.json",
               json.dumps({"env": {"CLAUDE_CODE_EFFORT_LEVEL": "high"}}))
        _, lines = render_banner.render_consumer(str(self.root), None, 0)
        self.assertIn("Claude effort is high; set CLAUDE_CODE_EFFORT_LEVEL=xhigh", lines[6])

    def test_auto_update_off_from_env_block_or_claude_json(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        _write(self.home / ".claude" / "settings.json",
               json.dumps({"env": {"CLAUDE_CODE_EFFORT_LEVEL": "max", "DISABLE_AUTOUPDATER": "1"}}))
        _, lines = render_banner.render_consumer(str(self.root), None, 0)
        self.assertIn("(auto-update: off)", lines[2])
        _write(self.home / ".claude" / "settings.json", json.dumps({"env": {"CLAUDE_CODE_EFFORT_LEVEL": "max"}}))
        _write(self.home / ".claude.json", json.dumps({"autoUpdates": False}))
        _, lines = render_banner.render_consumer(str(self.root), None, 0)
        self.assertIn("(auto-update: off)", lines[2])

    def test_skill_buckets_apply_the_shadowing_rule(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        for bucket, names in (
            ("skills", ["alpha"]),
            (".claude/skills", ["alpha", "beta"]),
            (".agent-config/repo/skills", ["alpha", "beta", "gamma"]),
        ):
            for name in names:
                _write(self.root / bucket / name / "SKILL.md", "# %s\n" % name)
        _write(self.root / ".agent-config" / "repo" / "skills" / "nodoc" / "README.md", "not a skill\n")
        _, lines = render_banner.render_consumer(str(self.root), None, 0)
        self.assertIn("Skills: 1 local (alpha) + 1 pack (beta) + 1 shared (gamma)", lines[4])

    def test_write_report_publishes_atomically(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        report, _ = render_banner.render_consumer(str(self.root), 5.0, 0)
        path = render_banner.write_report(str(self.root), report)
        self.assertEqual(Path(path), self.root / ".agent-config" / "banner.txt")
        text = Path(path).read_text(encoding="utf-8")
        self.assertEqual(text, "\n".join(report) + "\n")
        leftovers = [p for p in (self.root / ".agent-config").iterdir() if p.name.startswith("banner.txt.tmp")]
        self.assertEqual(leftovers, [])


class CheckTests(unittest.TestCase):
    def test_workflow_pins_below_minimum_and_sha_pins(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="banner-wf-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        _write(tmp / ".github" / "workflows" / "ci.yml", "\n".join([
            "jobs:",
            "  build:",
            "    steps:",
            "      - uses: actions/checkout@v4",
            "      - uses: actions/setup-python@v6",
            "      - uses: actions/upload-artifact@0123456789abcdef0123456789abcdef01234567",
            "      - uses: someone/other-action@v1",
            "",
        ]))
        findings = render_banner.workflow_findings(str(tmp))
        self.assertEqual(findings, [
            "actions/checkout@v4 in .github/workflows/ci.yml:4, bump to v5",
            "actions/upload-artifact@0123456 in .github/workflows/ci.yml:6 pinned by SHA, review manually",
        ])

    def test_codex_issues(self) -> None:
        base = {"installed": True, "configured": True, "version": "0.154.0", "latest": "",
                "model": "gpt-6-astra", "reasoning": "xhigh", "tier": "standard",
                "fast_mode": False, "project_doc_max_bytes": 262144}
        self.assertEqual(render_banner.codex_issues(base), [])
        old_model = dict(base, model="gpt-5.5-terra")
        self.assertIn("older than the GPT-5.6 family", render_banner.codex_issues(old_model)[0])
        old_cli = dict(base, version="0.149.0")
        self.assertIn("below the GPT-6 CLI floor", render_banner.codex_issues(old_cli)[0])
        old_cli_56 = dict(base, model="gpt-5.6-sol", version="0.143.0")
        self.assertIn("below the GPT-5.6 CLI floor", render_banner.codex_issues(old_cli_56)[0])
        no_budget = dict(base, project_doc_max_bytes=None)
        self.assertIn("set project_doc_max_bytes = 262144", render_banner.codex_issues(no_budget)[0])
        small_budget = dict(base, project_doc_max_bytes=65536)
        self.assertIn("set project_doc_max_bytes = 262144", render_banner.codex_issues(small_budget)[0])
        not_installed = dict(base, installed=False)
        self.assertEqual(render_banner.codex_issues(not_installed), [])

    def test_agent_style_issues(self) -> None:
        base = {"installed": "0.7.0", "expected": "v0.7.0", "python": "/env/bin/python",
                "command": "/env/bin/python", "disabled": False}
        self.assertEqual(render_banner.agent_style_issues(base), [])
        self.assertEqual(render_banner.agent_style_issues(dict(base, installed="0.8.1")), [])
        self.assertEqual(render_banner.agent_style_issues(dict(base, installed="0.10.0")), [])
        older = render_banner.agent_style_issues(dict(base, installed="0.6.0"))
        self.assertEqual(older, ["agent-style 0.6.0 in /env/bin/python is older than the v0.7.0 rules "
                                 "(run `/env/bin/python -m pip install -U agent-style==0.7.0`)"])
        missing = render_banner.agent_style_issues(dict(base, installed=None))
        self.assertEqual(missing, ["agent-style advisory off: /env/bin/python has no agent_style "
                                   "(run `/env/bin/python -m pip install agent-style==0.7.0`)"])
        self.assertIn("older than", render_banner.agent_style_issues(dict(base, installed="unknown"))[0])
        windows = "C:/Program Files/py/python.exe"
        spaced = dict(base, python=windows, command=render_banner.python_command(windows, "win32"))
        missing = render_banner.agent_style_issues(dict(spaced, installed=None))[0]
        self.assertIn("advisory off: C:/Program Files/py/python.exe has no agent_style", missing)
        self.assertIn('`& "C:/Program Files/py/python.exe" -m pip install agent-style==0.7.0`', missing)
        older = render_banner.agent_style_issues(dict(spaced, installed="0.6.0"))[0]
        self.assertIn('`& "C:/Program Files/py/python.exe" -m pip install -U agent-style==0.7.0`', older)
        # Only a whole stable release is pinned; a branch, a prerelease, or a suffixed ref is not.
        for ref in ("main", "v0.7.0-dev", "0.7.0-feature", "v0.8.0rc1", "v0.7", "0123abc"):
            self.assertEqual(render_banner.agent_style_issues(dict(base, installed=None, expected=ref)), [], ref)
        self.assertEqual(len(render_banner.agent_style_issues(dict(base, installed=None, expected="0.7.0"))), 1)
        for quiet in (dict(base, expected=None), dict(base, disabled=True),
                      dict(base, installed=None, disabled=True)):
            self.assertEqual(render_banner.agent_style_issues(quiet), [], quiet)
        self.assertEqual(render_banner.agent_style_issues(None), [])

    def test_python_command_quotes_a_spaced_path_and_calls_it_on_windows(self) -> None:
        self.assertEqual(render_banner.python_command("/env/bin/python", "darwin"), "/env/bin/python")
        self.assertEqual(render_banner.python_command("/my env/bin/python", "linux"), '"/my env/bin/python"')
        self.assertEqual(render_banner.python_command("C:/Program Files/py/python.exe", "win32"),
                         '& "C:/Program Files/py/python.exe"')
        self.assertEqual(render_banner.python_command("C:/py/python.exe", "win32"), "C:/py/python.exe")

    def test_agent_style_expected(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="banner-style-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertIsNone(render_banner.agent_style_expected(str(tmp), True))
        _write(tmp / "AGENTS.md", "x\n<!-- rule-pack:agent-style:begin version=v0.7.0 sha256=1 -->\n")
        self.assertEqual(render_banner.agent_style_expected(str(tmp), True), "v0.7.0")
        # A source repository reads the bundled default; another pack's ref never answers.
        _write(tmp / "bootstrap" / "packs.yaml", "\n".join([
            "version: 2", "packs:",
            "  - name: agent-style", "    description: >", "      Writing rules.",
            "    source:", "      repo: https://github.com/yzhao062/agent-style", "      ref: v0.7.0",
            "  - name: other", "    source:", "      ref: v9.9.9", "",
        ]))
        self.assertEqual(render_banner.agent_style_expected(str(tmp), False), "v0.7.0")
        _write(tmp / "bootstrap" / "packs.yaml", "\n".join([
            "packs:", "  - name: agent-style", "    source:", "      repo: x",
            "  - name: other", "    source:", "      ref: v9.9.9", "",
        ]))
        self.assertIsNone(render_banner.agent_style_expected(str(tmp), False))

    def test_agent_style_installed_reads_the_imported_release(self) -> None:
        fake = mock.Mock(__version__="0.7.0")
        with mock.patch.dict(sys.modules, {"agent_style": fake}):
            self.assertEqual(render_banner.agent_style_installed(), "0.7.0")
        with mock.patch.dict(sys.modules, {"agent_style": None}):
            self.assertIsNone(render_banner.agent_style_installed())

    def test_latest_arrow_only_when_newer(self) -> None:
        self.assertEqual(render_banner._with_latest("2.1.275", "2.1.274"), "2.1.275")
        self.assertEqual(render_banner._with_latest("2.1.274", "2.1.275"), "2.1.274 → 2.1.275")
        self.assertEqual(render_banner._with_latest("unknown", "2.1.275"), "unknown")
        self.assertEqual(render_banner._with_latest(None, "2.1.275"), "unknown")

    def test_minimal_toml_reader(self) -> None:
        data = render_banner._parse_toml_minimal(
            '# comment\nmodel = "gpt-6-astra"  # trailing\nproject_doc_max_bytes = 262_144\n'
            "approval_policy = 'on-request'\n[features]\nfast_mode = true\n[other]\nx = 1\n"
        )
        self.assertEqual(data["model"], "gpt-6-astra")
        self.assertEqual(data["project_doc_max_bytes"], 262144)
        self.assertEqual(data["approval_policy"], "on-request")
        self.assertIs(data["features"]["fast_mode"], True)
        self.assertEqual(data["other"]["x"], 1)

    def test_minimal_toml_reader_ignores_comments_before_reading_values(self) -> None:
        """A commented boolean stays a boolean and a commented header still
        opens its table; a `#` inside quotes is part of the string."""
        data = render_banner._parse_toml_minimal(
            'model = "gpt-6-astra" # the flagship\n'
            "service_tier = 'stand#ard' # keep the hash\n"
            "[features] # options\n"
            "fast_mode = false # disabled\n"
            "project_doc_max_bytes = 262144 # bytes\n"
        )
        self.assertEqual(data["model"], "gpt-6-astra")
        self.assertEqual(data["service_tier"], "stand#ard")
        self.assertIs(data["features"]["fast_mode"], False)
        self.assertEqual(data["features"]["project_doc_max_bytes"], 262144)
        self.assertNotIn("features] # options", data)

    def test_invalid_toml_is_unavailable_not_repaired(self) -> None:
        """A duplicate key is a TOML error. The line parser would keep the
        last assignment and the banner would read all clear, so a parse
        rejection is reported instead, through read_toml and the render."""
        tmp = Path(tempfile.mkdtemp(prefix="banner-toml-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "config.toml"
        _write(path, 'model = "gpt-6-astra"\nmodel = "gpt-5.6-sol"\nproject_doc_max_bytes = 262144\n')
        try:
            import tomllib  # noqa: F401
        except ImportError:
            self.skipTest("tomllib is needed to reject invalid TOML")
        with self.assertRaises(render_banner.TomlError):
            render_banner.read_toml(str(path))
        with mock.patch.object(render_banner, "home_dir", return_value=str(tmp)), \
                mock.patch.object(render_banner, "_run_version", return_value="0.154.0"):
            (tmp / ".codex").mkdir()
            shutil.copy2(path, tmp / ".codex" / "config.toml")
            fields = render_banner.codex_fields()
        self.assertFalse(fields["configured"])
        self.assertIn("config_error", fields)
        issues = render_banner.codex_issues(fields)
        self.assertEqual(len(issues), 1)
        self.assertIn("not valid TOML", issues[0])
        self.assertNotIn("\n", issues[0])

    def test_read_toml_falls_back_when_tomllib_is_missing(self) -> None:
        """Python 3.9 and 3.10 have no tomllib; the fallback must read the
        same keys, commented or not, through read_toml itself."""
        tmp = Path(tempfile.mkdtemp(prefix="banner-toml-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "config.toml"
        _write(path, 'model = "gpt-6-astra"\nmodel_reasoning_effort = "xhigh" # floor\n'
                     "[features] # table\nfast_mode = false # off\n")
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            data = render_banner.read_toml(str(path))
        self.assertEqual(data["model"], "gpt-6-astra")
        self.assertEqual(data["model_reasoning_effort"], "xhigh")
        self.assertIs(data["features"]["fast_mode"], False)


class ModeTests(_HermeticCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        # The renderer writes UTF-8 whatever the console code page; decode it
        # as such, because text=True would use the runner's locale (cp1252
        # on GitHub's Windows image) and turn the title emoji into mojibake.
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "render_banner.py"), *args],
            capture_output=True, encoding="utf-8", errors="replace",
            env=dict(os.environ), timeout=120,
        )

    def test_consumer_cli_publishes_and_prints_only_with_stdout(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger(run_id="cli-1"))
        result = self._run("--root", str(self.root), "--event-ts", "9.5", "--bootstrap-rc", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        text = (self.root / ".agent-config" / "banner.txt").read_text(encoding="utf-8")
        self.assertTrue(render_banner.report_is_current(text, 9.5, "cli-1"))
        result = self._run("--root", str(self.root), "--stdout")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 7)
        self.assertEqual(lines[0], render_banner.TITLE)
        self.assertFalse(any(line.startswith(render_banner.METADATA_PREFIX) for line in lines))

    def test_source_root_prints_seven_lines_and_creates_nothing(self) -> None:
        source = self.tmp / "source"
        for rel in ("bootstrap/bootstrap.sh", "bootstrap/bootstrap.ps1", "scripts/generate_agent_configs.py",
                    "skills/one/SKILL.md"):
            _write(source / rel, "# x\n")
        result = self._run("--root", str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 7)
        self.assertEqual(lines[0], render_banner.TITLE)
        self.assertIn("Skills: 1 local (one)", lines[4])
        self.assertFalse((source / ".agent-config").exists())

    def test_unrelated_root_exits_two(self) -> None:
        other = self.tmp / "other"
        other.mkdir()
        result = self._run("--root", str(other))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")


@unittest.skipUnless(HAVE_YAML, "PyYAML is required for the pack identity tests")
class PackIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="packid-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "project"
        (self.root / ".agent-config").mkdir(parents=True)
        self.user_cfg = self.tmp / "user" / "config.yaml"
        self.env = {"AGENT_CONFIG_HOST": ""}

    def _lock(self, packs: dict) -> None:
        _write(self.root / ".agent-config" / "pack-lock.json", json.dumps({"version": 1, "packs": packs}))

    def _manifest(self) -> None:
        _write(self.root / ".agent-config" / "repo" / "bootstrap" / "packs.yaml", "\n".join([
            "packs:",
            "  - name: agent-style",
            "    source:",
            "      repo: https://github.com/yzhao062/agent-style",
            "      ref: v0.4.1",
            "  - name: aa-core-skills",
            "",
        ]))

    def test_normalize_url(self) -> None:
        n = pack_identity.normalize_pack_source_url
        self.assertEqual(n("https://GitHub.com/YZhao062/Agent-Pack.git/"), "https://github.com/yzhao062/agent-pack")
        self.assertEqual(n("git@github.com:yzhao062/agent-pack.git"), "https://github.com/yzhao062/agent-pack")
        self.assertEqual(n("https://Gitea.Example/Owner/Repo.git/"), "https://gitea.example/Owner/Repo")
        self.assertEqual(n("not a url"), "not a url")
        self.assertEqual(n(""), "")

    def test_user_config_path_by_platform(self) -> None:
        self.assertEqual(
            pack_identity.user_config_path({"APPDATA": r"C:\Users\x\AppData\Roaming"}, "win32"),
            os.path.join(r"C:\Users\x\AppData\Roaming", "anywhere-agents", "config.yaml"))
        self.assertIsNone(pack_identity.user_config_path({}, "win32"))
        self.assertEqual(
            pack_identity.user_config_path({"XDG_CONFIG_HOME": "/xdg", "HOME": "/h"}, "linux"),
            os.path.join("/xdg", "anywhere-agents", "config.yaml"))
        self.assertEqual(
            pack_identity.user_config_path({"HOME": "/h"}, "linux"),
            os.path.join("/h", ".config", "anywhere-agents", "config.yaml"))

    def test_absent_user_config_is_an_empty_list(self) -> None:
        self.assertEqual(pack_identity.user_packs(str(self.user_cfg)), [])
        self.assertEqual(pack_identity.user_packs(None), [])

    def test_user_bundled_default_without_source_is_bundled_identity(self) -> None:
        _write(self.user_cfg, "packs:\n  - name: agent-style\n  - name: profile\n    source:\n"
                              "      url: https://github.com/yzhao062/agent-pack\n      ref: main\n")
        self.assertEqual(pack_identity.user_packs(str(self.user_cfg)), [
            ("agent-style", "bundled:aa", "bundled"),
            ("profile", "https://github.com/yzhao062/agent-pack", "main"),
        ])

    def test_seeds_stand_alone_when_both_project_layers_are_absent(self) -> None:
        self._lock({"agent-style": {"source_url": "https://github.com/yzhao062/agent-style", "requested_ref": "v0.4.1"},
                    "aa-core-skills": {"source_url": "bundled:aa", "requested_ref": "bundled"}})
        lock = pack_identity.read_lock(str(self.root))
        self.assertEqual(pack_identity.project_packs(str(self.root), self.env, lock), [
            ("agent-style", "https://github.com/yzhao062/agent-style", "v0.4.1"),
            ("aa-core-skills", "bundled:aa", "bundled"),
        ])

    def test_codex_host_drops_the_claude_only_seed(self) -> None:
        self._lock({"agent-style": {"source_url": "https://github.com/yzhao062/agent-style", "requested_ref": "v0.4.1"}})
        lock = pack_identity.read_lock(str(self.root))
        self.assertEqual(
            [i[0] for i in pack_identity.project_packs(str(self.root), {"AGENT_CONFIG_HOST": "codex"}, lock)],
            ["agent-style"])

    def test_layers_replace_by_name_and_an_empty_packs_clears(self) -> None:
        self._manifest()
        _write(self.root / "agent-config.yaml", "packs:\n  - name: profile\n    source:\n"
                                                "      url: https://github.com/yzhao062/agent-pack\n      ref: main\n")
        idents = pack_identity.project_packs(str(self.root), self.env, None)
        self.assertEqual([i[0] for i in idents], ["agent-style", "aa-core-skills", "profile"])
        self.assertEqual(idents[0], ("agent-style", "https://github.com/yzhao062/agent-style", "v0.4.1"))
        self.assertEqual(idents[1], ("aa-core-skills", "bundled:aa", "bundled"))
        _write(self.root / "agent-config.local.yaml", "packs:\n  - name: profile\n    source:\n"
                                                      "      url: https://github.com/yzhao062/agent-pack\n      ref: dev\n")
        idents = pack_identity.project_packs(str(self.root), self.env, None)
        self.assertEqual(idents[2], ("profile", "https://github.com/yzhao062/agent-pack", "dev"))
        _write(self.root / "agent-config.yaml", "packs: []\n")
        idents = pack_identity.project_packs(str(self.root), self.env, None)
        self.assertEqual([i[0] for i in idents], ["profile"])
        _write(self.root / "agent-config.local.yaml", "packs:\n")
        self.assertEqual(pack_identity.project_packs(str(self.root), self.env, None), [])

    def test_missing_identity_evidence_is_unavailable_not_zero(self) -> None:
        result = pack_identity.pack_checks(str(self.root), self.env, "linux")
        self.assertIsNone(result["gap_count"])
        self.assertIn("no identity recorded for bundled default", result["unavailable"])
        self.assertIsNone(result["update_count"])
        self.assertIn("pack-lock.json is absent", result["unavailable"])

    def test_missing_lock_is_missing_update_evidence_even_with_a_manifest(self) -> None:
        """The manifest can name the seeded defaults' identity, so the gap
        count is computable; nothing records upstream heads, so the update
        count is unavailable rather than zero. An existing empty lock is a
        valid zero."""
        self._manifest()
        result = pack_identity.pack_checks(str(self.root), self.env, "linux")
        self.assertEqual(result["gap_count"], 0)
        self.assertIsNone(result["update_count"])
        self.assertIn("pack-lock.json is absent", result["unavailable"])
        self._lock({})
        result = pack_identity.pack_checks(str(self.root), self.env, "linux")
        self.assertEqual(result, {"gap_count": 0, "update_count": 0, "unavailable": None})

    def test_gap_and_update_counts(self) -> None:
        self._manifest()
        self._lock({
            "agent-style": {"source_url": "https://github.com/yzhao062/agent-style", "requested_ref": "v0.4.1",
                            "resolved_commit": "aaa", "latest_known_head": "bbb"},
            "profile": {"source_url": "https://github.com/yzhao062/agent-pack", "requested_ref": "main",
                        "resolved_commit": "ccc", "latest_known_head": "ccc"},
            "old-entry": {"source_url": "x", "requested_ref": "y", "resolved_commit": "ddd"},
        })
        _write(self.root / "agent-config.yaml", "packs:\n  - name: profile\n    source:\n"
                                                "      url: https://github.com/yzhao062/agent-pack\n      ref: main\n")
        _write(self.user_cfg, "\n".join([
            "packs:",
            "  - name: profile",
            "    source: {url: 'https://GITHUB.com/yzhao062/agent-pack.git', ref: main}",
            "  - name: paper-workflow",
            "    source: {url: 'https://github.com/yzhao062/agent-pack', ref: main}",
            "  - name: agent-style",
            "    source: {url: 'https://github.com/yzhao062/agent-style', ref: v0.3.5}",
            "",
        ]))
        env = {"AGENT_CONFIG_HOST": "", "XDG_CONFIG_HOME": str(self.tmp / "user-xdg")}
        _write(self.tmp / "user-xdg" / "anywhere-agents" / "config.yaml", self.user_cfg.read_text(encoding="utf-8"))
        result = pack_identity.pack_checks(str(self.root), env, "linux")
        self.assertIsNone(result["unavailable"])
        # paper-workflow is not deployed; agent-style differs by ref; profile matches.
        self.assertEqual(result["gap_count"], 2)
        # agent-style has a newer head; profile does not; old-entry lacks a head.
        self.assertEqual(result["update_count"], 1)

    def test_malformed_inputs_are_unavailable(self) -> None:
        self._manifest()
        _write(self.root / ".agent-config" / "pack-lock.json", "{not json")
        result = pack_identity.pack_checks(str(self.root), self.env, "linux")
        self.assertIsNone(result["update_count"])
        self.assertIn("malformed", result["unavailable"])
        self._lock({})
        _write(self.root / "agent-config.yaml", "packs: {not: a list}\n")
        result = pack_identity.pack_checks(str(self.root), self.env, "linux")
        self.assertIsNone(result["gap_count"])
        self.assertIn("must be a list", result["unavailable"])
        self.assertEqual(result["update_count"], 0)

    def test_malformed_rows_inside_valid_yaml_are_unavailable(self) -> None:
        """A row that is neither a name nor a mapping with one, or a source
        of the wrong shape, cannot be read as a selection; skipping it would
        report the project as clean. The same holds for the user layer and
        for a lock entry that is not an object."""
        self._manifest()
        self._lock({})
        cases = (
            "packs: [123]\n",
            "packs:\n  - source: {url: https://github.com/yzhao062/agent-pack}\n",
            "packs:\n  - name: profile\n    source: 7\n",
            "packs:\n  - name: profile\n    source: {url: [not, a, string]}\n",
        )
        for text in cases:
            _write(self.root / "agent-config.yaml", text)
            result = pack_identity.pack_checks(str(self.root), self.env, "linux")
            self.assertIsNone(result["gap_count"], text)
            self.assertIn("agent-config.yaml", result["unavailable"], text)
            self.assertEqual(result["update_count"], 0, text)
        os.remove(self.root / "agent-config.yaml")
        _write(self.tmp / "user-xdg" / "anywhere-agents" / "config.yaml", "packs:\n  - {name: 5}\n")
        env = {"AGENT_CONFIG_HOST": "", "XDG_CONFIG_HOME": str(self.tmp / "user-xdg")}
        result = pack_identity.pack_checks(str(self.root), env, "linux")
        self.assertIsNone(result["gap_count"])
        self.assertIn("config.yaml", result["unavailable"])
        os.remove(self.tmp / "user-xdg" / "anywhere-agents" / "config.yaml")
        _write(self.root / ".agent-config" / "pack-lock.json",
               json.dumps({"version": 1, "packs": {"profile": "not an object"}}))
        result = pack_identity.pack_checks(str(self.root), self.env, "linux")
        self.assertIsNone(result["update_count"])
        self.assertIn("not an object", result["unavailable"])

    def test_missing_pyyaml_is_unavailable(self) -> None:
        self._manifest()
        code = (
            "import sys; sys.modules['yaml'] = None; sys.path.insert(0, %r); import pack_identity, json; "
            "print(json.dumps(pack_identity.pack_checks(%r, {'AGENT_CONFIG_HOST': ''}, 'linux')))"
            % (str(SCRIPTS), str(self.root))
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIsNone(data["gap_count"])
        self.assertIn("PyYAML", data["unavailable"])


class RendererIsSharedAndVendoredTests(unittest.TestCase):
    def test_renderer_carries_its_helper_beside_it(self) -> None:
        self.assertTrue((SCRIPTS / "render_banner.py").is_file())
        self.assertTrue((SCRIPTS / "pack_identity.py").is_file())
        text = (SCRIPTS / "render_banner.py").read_text(encoding="utf-8")
        self.assertIn("import pack_identity", text)
        self.assertNotIn("_verify_gather", (SCRIPTS / "pack_identity.py").read_text(encoding="utf-8"))

    def test_entry_points_render_after_the_ledger_on_every_exit(self) -> None:
        sh = (ROOT / "bootstrap" / "bootstrap.sh").read_text(encoding="utf-8")
        ps1 = (ROOT / "bootstrap" / "bootstrap.ps1").read_text(encoding="utf-8")
        self.assertIn("trap '_bootstrap_rc=$?; _render_banner_report \"$_bootstrap_rc\"; exit $_bootstrap_rc' EXIT", sh)
        self.assertLess(sh.index("_ledger_init\n"), sh.index("trap '_bootstrap_rc=$?"))
        # PowerShell renders at the end of the run and before each explicit
        # exit after the ledger exists; a try/finally around the body would
        # turn a printed-and-continued .NET exception into an abort.
        self.assertTrue(ps1.rstrip().endswith("Invoke-BannerRender 0"), ps1[-200:])
        self.assertLess(ps1.index("try { Initialize-Ledger } catch {}"), ps1.index("function Invoke-BannerRender"))
        self.assertIn("Exit-Bootstrap $composerRc", ps1)
        self.assertNotIn("\n} finally {\n  Invoke-BannerRender", ps1)
        body = ps1[ps1.index("function Exit-Bootstrap"):]
        body = body[body.index("Invoke-GitPreflight\n"):]
        bare_exits = [line for line in body.splitlines()
                      if line.strip().startswith("exit ") and "PREFLIGHT_TEST" not in line]
        self.assertEqual(bare_exits, [], "every exit after the ledger renders first: %r" % bare_exits)
        # A helper deployment that throws ends the run through Exit-Bootstrap
        # rather than as an uncaught exception, so it renders too.
        bare_deploys = [line for line in body.splitlines()
                        if "Copy-HelperAtomic .agent-config/repo/scripts/" in line]
        self.assertEqual(bare_deploys, [], "helper deploys go through Deploy-UserHelper: %r" % bare_deploys)
        self.assertIn("Deploy-UserHelper .agent-config/repo/scripts/guard.py", ps1)
        self.assertIn("render_banner.py --root .", sh)
        self.assertIn("render_banner.py --root .", ps1)


class OutputEncodingTests(_HermeticCase):
    """The banner is UTF-8 on every stdout, whatever the console code page.

    A Windows console without PYTHONUTF8 hands Python a cp1252 stdout, and
    the tree glyphs and the package emoji are outside it. The env below
    reproduces that console for the renderer in both modes and for the hook,
    independently of the developer's inherited UTF-8 settings.
    """

    ANSI_ENV = {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(self.ANSI_ENV)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "render_banner.py"), *args],
            capture_output=True, env=env, timeout=120,
        )

    def test_source_stdout_is_utf8_under_an_ansi_console(self) -> None:
        source = self.tmp / "source"
        for rel in ("bootstrap/bootstrap.sh", "bootstrap/bootstrap.ps1", "scripts/generate_agent_configs.py",
                    "skills/one/SKILL.md"):
            _write(source / rel, "# x\n")
        result = self._run("--root", str(source))
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        lines = result.stdout.decode("utf-8").splitlines()
        self.assertEqual(lines[0], render_banner.TITLE)
        self.assertTrue(lines[1].startswith("   ├── OS: "))

    def test_consumer_stdout_is_utf8_under_an_ansi_console(self) -> None:
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        result = self._run("--root", str(self.root), "--stdout")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        lines = result.stdout.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 7)
        self.assertEqual(lines[0], render_banner.TITLE)
        report = (self.root / ".agent-config" / "banner.txt").read_bytes().decode("utf-8")
        self.assertIn(render_banner.TITLE, report)

    def test_hook_stdout_is_utf8_under_an_ansi_console(self) -> None:
        hooks = self.root / ".agent-config" / "repo" / "scripts"
        hooks.mkdir(parents=True)
        for name in ("render_banner.py", "pack_identity.py"):
            shutil.copy2(SCRIPTS / name, hooks / name)
        ledger = _ledger(run_id="ansi-1").replace("'", "''")
        if sys.platform.startswith("win"):
            _write(self.root / ".agent-config" / "bootstrap.ps1",
                   "$enc = New-Object System.Text.UTF8Encoding $false\n"
                   "[System.IO.File]::WriteAllText((Join-Path (Get-Location).Path '.agent-config/last-run.json'), '%s', $enc)\n"
                   "exit 0\n" % ledger)
        else:
            _write(self.root / ".agent-config" / "bootstrap.sh",
                   "#!/bin/bash\nprintf '%%s\\n' '%s' > .agent-config/last-run.json\nexit 0\n"
                   % _ledger(run_id="ansi-1").replace("'", "'\\''"))
        env = dict(os.environ)
        env.update(self.ANSI_ENV)
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "session_bootstrap.py")],
            capture_output=True, env=env, cwd=str(self.root), stdin=subprocess.DEVNULL, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        lines = result.stdout.decode("utf-8").splitlines()
        self.assertIn(render_banner.TITLE, lines)
        self.assertTrue(any(line.startswith("   └── Session check:") for line in lines), lines)


class DiagnosticShapeTests(_HermeticCase):
    def test_malformed_yaml_keeps_the_report_at_seven_physical_lines(self) -> None:
        if not HAVE_YAML:
            self.skipTest("PyYAML is required")
        _write(self.root / ".agent-config" / "last-run.json", _ledger())
        _write(self.root / "agent-config.yaml", "packs: [\n")
        report, lines = render_banner.render_consumer(str(self.root), None, 0)
        path = render_banner.write_report(str(self.root), report)
        physical = Path(path).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(physical), 8, physical)  # metadata plus seven
        self.assertEqual(len("\n".join(lines).splitlines()), 7)
        self.assertIn("pack checks unavailable", lines[6])
        self.assertIn("not valid YAML", lines[6])


class EntryPointReportTests(unittest.TestCase):
    """The real bootstrap entry points publish the report through the wiring.

    Reuses the preflight fixture (stub composer and generator, stub git, a
    private home) with the renderer planted where the sparse clone carries
    it. Three ends of a run: normal completion, a composer failure that
    exits 3, and a helper deployment refused because a live process holds
    the destination open (Windows only, where a rename onto an open file is
    refused). Each publishes a report whose run_id is the ledger's, and the
    exit status survives the render.
    """

    def _run(self, entrypoint: str, shell=None, composer_rc: int = 0, hold_open: str = ""):
        import test_bootstrap_preflight as preflight

        if entrypoint == "bash" and not preflight.BASH:
            self.skipTest("bash not available")
        if entrypoint == "powershell" and not shell:
            self.skipTest("PowerShell not available")
        tmp = Path(tempfile.mkdtemp(prefix="banner-entry-%s-" % entrypoint))
        holder = None
        try:
            work, python_path, home = preflight._prepare_full_bootstrap_fixture(
                tmp, composer_rc=composer_rc, generator_rc=0)
            scripts = work / ".agent-config" / "repo" / "scripts"
            for name in ("render_banner.py", "pack_identity.py"):
                shutil.copy2(SCRIPTS / name, scripts / name)
            hooks = home / ".claude" / "hooks"
            hooks.mkdir(parents=True)
            if hold_open:
                # The destination must differ from the source or the deploy
                # is skipped as identical before the rename.
                (scripts / hold_open).write_text("# upstream copy\n", encoding="utf-8")
                target = hooks / hold_open
                target.write_text("# installed copy\n", encoding="utf-8")
                ready = tmp / "holder-ready"
                holder = subprocess.Popen(
                    [shell or preflight.POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                     "$f = [System.IO.File]::Open('%s', 'Open', 'Read', 'None'); "
                     "Set-Content -LiteralPath '%s' -Value ready; Start-Sleep -Seconds 120"
                     % (str(target).replace("'", "''"), str(ready).replace("'", "''"))],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                deadline = time.time() + 30
                while not ready.exists() and time.time() < deadline:
                    time.sleep(0.1)
                self.assertTrue(ready.exists(), "the holder never opened the destination")
            stub_dir = tmp / "stub_path"
            preflight._make_stub_git(stub_dir, "git version 2.50.0")
            preflight._write_executable(stub_dir / "curl", "#!/bin/sh\nexit 0\n")
            if entrypoint == "bash":
                env = preflight._stripped_env(stub_dir)
                command = [preflight.BASH, str(preflight.BOOTSTRAP_SH)]
                python_command = str(Path(sys.executable)).replace("\\", "/")
            else:
                env = os.environ.copy()
                env["PATH"] = os.pathsep.join(
                    (str(preflight.powershell_stub_dir(stub_dir)), env.get("PATH", "")))
                python_command = sys.executable
                wrapper = tmp / "invoke-bootstrap.ps1"
                literal = str(preflight.BOOTSTRAP_PS1).replace("'", "''")
                wrapper.write_text(
                    "function Invoke-WebRequest { param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile) }\n"
                    "& '%s'\nif (-not $?) { exit $LASTEXITCODE }\nexit 0\n" % literal,
                    encoding="utf-8")
                command = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(wrapper)]
            env.pop("AGENT_CONFIG_PREFLIGHT_TEST", None)
            env["AGENT_CONFIG_UPSTREAM"] = "example/repo"
            env["ANYWHERE_AGENTS_PYTHON"] = python_command
            env["ANYWHERE_AGENTS_CODEX_AUTO_UPDATE"] = "off"
            env["PYTHONPATH"] = str(python_path)
            env["HOME"] = str(home)
            env["USERPROFILE"] = str(home)
            env["APPDATA"] = str(home / "AppData" / "Roaming")
            env["XDG_CONFIG_HOME"] = str(home / ".config")
            result = subprocess.run(command, cwd=str(work), env=env, capture_output=True, text=True,
                                    timeout=preflight.SUBPROCESS_TIMEOUT)
            ledger_path = work / ".agent-config" / "last-run.json"
            self.assertTrue(ledger_path.is_file(), result.stderr)
            ledger = json.loads(ledger_path.read_text(encoding="utf-8-sig"))
            report_path = work / ".agent-config" / "banner.txt"
            report = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
            return result, ledger, report
        finally:
            if holder is not None:
                holder.kill()
                holder.wait(timeout=30)
            shutil.rmtree(tmp, ignore_errors=True)

    def _assert_published(self, result, ledger, report, rc: int) -> None:
        self.assertEqual(result.returncode, rc, result.stderr)
        self.assertTrue(report, "no report was published; stderr=%r" % result.stderr)
        meta = render_banner.parse_metadata(report)
        self.assertIsNotNone(meta, report)
        self.assertEqual(meta["run_id"], ledger["run_id"])
        self.assertTrue(render_banner.report_is_current(report, None, ledger["run_id"]))
        self.assertEqual(meta["completed"], ledger["completed"] if rc == 0 else False)
        check = render_banner.banner_body(report)[6]
        if rc == 0:
            self.assertNotIn("bootstrap exited", check)
        else:
            self.assertIn("bootstrap exited %d" % rc, check)

    def _cases(self):
        import test_bootstrap_preflight as preflight

        yield "bash", None
        for path in preflight.POWERSHELL_EDITIONS:
            yield "powershell", path

    def test_normal_completion_publishes_a_current_report(self) -> None:
        for entrypoint, shell in self._cases():
            with self.subTest(entrypoint=entrypoint, shell=shell):
                result, ledger, report = self._run(entrypoint, shell)
                self._assert_published(result, ledger, report, 0)
                self.assertIs(ledger["completed"], True)

    def test_composer_failure_publishes_and_keeps_its_exit_status(self) -> None:
        for entrypoint, shell in self._cases():
            with self.subTest(entrypoint=entrypoint, shell=shell):
                result, ledger, report = self._run(entrypoint, shell, composer_rc=3)
                self._assert_published(result, ledger, report, 3)
                self.assertIn("compose", render_banner.banner_body(report)[6])

    @unittest.skipUnless(sys.platform.startswith("win"), "a rename onto an open file is refused on Windows only")
    def test_refused_helper_deployment_publishes_and_exits_one(self) -> None:
        """The failure Round 9 reproduced: a live session holds guard.py open,
        Copy-HelperAtomic throws, and the run must still end at exit 1 with a
        report naming the phase it stopped at, on both editions and on the
        bash lane."""
        for entrypoint, shell in self._cases():
            with self.subTest(entrypoint=entrypoint, shell=shell):
                result, ledger, report = self._run(entrypoint, shell, hold_open="guard.py")
                self._assert_published(result, ledger, report, 1)
                self.assertIs(ledger["completed"], False)
                self.assertIn("could not atomically deploy", result.stderr)


if __name__ == "__main__":
    unittest.main()
