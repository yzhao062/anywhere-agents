"""Tests for scripts/agent_models.py and the no-literal drift guard.

The resolver tests run against a temporary registry (AGENT_MODELS_FILE), so
they do not depend on the values the maintainer has chosen. Two tests read the
real repository on purpose: `render --check` (user/settings.json must mirror
the registry) and the drift guard (no tracked file outside an allowlist may
name a concrete model or effort).
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import agent_models  # noqa: E402

RESOLVER = SCRIPTS / "agent_models.py"
# The Codex config editor refuses a change it cannot parse back, and the
# standard library has a TOML parser only from Python 3.11.
needs_toml_parser = unittest.skipUnless(
    sys.version_info >= (3, 11), "editing config.toml needs the standard library TOML parser"
)
REAL_REGISTRY = ROOT / "user" / "agent-models.json"
REAL_SETTINGS = ROOT / "user" / "settings.json"

# Placeholder names, not the maintainer's real choices: the tests assert how
# the resolver treats a registry, not what the registry says.
FIXTURE = {
    "schema": 1,
    "families": {
        "claude": {"model": "fable", "effort": "medium"},
        "codex": {"model": "gpt-9-test", "effort": "low"},
        "agy": {
            "model": "gemini-9-test",
            "effort": "high",
            "fallback_model": "gemini-8-fast",
        },
    },
    "roles": {
        "interactive": {},
        "reviewer": {},
        "worker": {"claude": {"model": "sonnet", "effort": "high"}},
        "prun-worker": {
            "agy": {
                "model": "gemini-8-fast",
                "effort": "medium",
                "second_model": "claude-sonnet-9",
                "fallback_model": None,
            },
            "codex": {"effort": "high"},
        },
    },
}

SETTINGS_FIXTURE = {
    "permissions": {"allow": ["Bash(ls)"]},
    "effortLevel": "low",
    "model": "stale",
    "env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "65", "CLAUDE_CODE_EFFORT_LEVEL": "low"},
}
# Roles that inherit the family row. The advisor is the one role that does not,
# so the fixture registry lists none and a test adds the row it needs.
INHERITING_ROLES = tuple(r for r in agent_models.ROLES if r != "advisor")


def with_advisor(model: str = "opus"):
    return lambda d: d["roles"].update(advisor={"claude": {"model": model}})


def fixture(mutate=None) -> dict:
    data = copy.deepcopy(FIXTURE)
    if mutate:
        mutate(data)
    return data


def fixture_with_grok(mutate=None) -> dict:
    data = copy.deepcopy(FIXTURE)
    data["families"]["grok"] = {"model": "grok-9-test", "effort": "high"}
    data["roles"]["prun-worker"]["grok"] = {"model": "grok-9-test", "effort": "high"}
    if mutate:
        mutate(data)
    return data


def run_main(argv: list, env: dict) -> tuple:
    """Call agent_models.main in-process; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = agent_models.main(argv)
    return code, out.getvalue(), err.getvalue()


class TempRegistryCase(unittest.TestCase):
    """Base class: a temp directory with a registry file and isolated tool homes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.registry = self.dir / "agent-models.json"
        self.write_registry(FIXTURE)
        self.env = {
            "AGENT_MODELS_FILE": str(self.registry),
            "CODEX_HOME": str(self.dir / "codex-home"),
            "CLAUDE_CONFIG_DIR": str(self.dir / "claude-home"),
            "ANTIGRAVITY_SETTINGS_FILE": str(self.dir / "agy-home" / "settings.json"),
        }
        for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            Path(self.env[key]).mkdir()
        Path(self.env["ANTIGRAVITY_SETTINGS_FILE"]).parent.mkdir()

    def write_registry(self, data: dict) -> None:
        self.registry.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class ResolveTests(TempRegistryCase):
    def test_every_role_inherits_the_family_row_unless_overridden(self) -> None:
        data = fixture()
        for role in ("interactive", "reviewer"):
            row = agent_models.resolve("claude", role, data)
            self.assertEqual((row["model"], row["effort"]), ("fable", "medium"), role)
            row = agent_models.resolve("codex", role, data)
            self.assertEqual((row["model"], row["effort"]), ("gpt-9-test", "low"), role)

    def test_worker_role_overrides_claude_model_and_effort(self) -> None:
        row = agent_models.resolve("claude", "worker", fixture())
        self.assertEqual((row["model"], row["effort"]), ("sonnet", "high"))

    def test_partial_override_keeps_the_other_field(self) -> None:
        row = agent_models.resolve("codex", "prun-worker", fixture())
        self.assertEqual((row["model"], row["effort"]), ("gpt-9-test", "high"))

    def test_agy_interactive_has_slug_and_fallback_slug_but_no_second_slug(self) -> None:
        row = agent_models.resolve("agy", "interactive", fixture())
        self.assertEqual(row["model"], "gemini-9-test")
        self.assertEqual(row["slug"], "gemini-9-test-high")
        self.assertEqual(row["fallback_slug"], "gemini-8-fast-high")
        self.assertIsNone(row["second_slug"])

    def test_agy_prun_worker_uses_its_tier_second_slug_and_drops_the_fallback(self) -> None:
        row = agent_models.resolve("agy", "prun-worker", fixture())
        self.assertEqual(row["slug"], "gemini-8-fast-medium")
        self.assertEqual(row["second_slug"], "claude-sonnet-9-medium")
        self.assertIsNone(row["fallback_slug"])

    def test_an_agy_fallback_takes_its_own_effort_and_ends_on_the_family_fallback(self) -> None:
        def reviewer(data: dict) -> None:
            data["roles"]["reviewer"]["agy"] = {
                "model": "gemini-9-peak",
                "effort": "medium",
                "fallback_model": "gemini-9-test",
                "fallback_effort": "high",
            }

        row = agent_models.resolve("agy", "reviewer", fixture(reviewer))
        self.assertEqual(row["slug"], "gemini-9-peak-medium")
        self.assertEqual(row["fallback_slug"], "gemini-9-test-high")
        # The role's fallback first, then the one the family row carries.
        self.assertEqual(row["fallback_slugs"], ["gemini-9-test-high", "gemini-8-fast-high"])

        def same_tier(data: dict) -> None:
            reviewer(data)
            del data["roles"]["reviewer"]["agy"]["fallback_effort"]

        row = agent_models.resolve("agy", "reviewer", fixture(same_tier))
        self.assertEqual(row["fallback_slugs"], ["gemini-9-test-medium", "gemini-8-fast-high"])

    def test_a_fallback_is_built_in_the_row_that_names_it(self) -> None:
        def family_tier(data: dict) -> None:
            data["families"]["agy"]["fallback_effort"] = "low"

        def effort_only(data: dict) -> None:
            family_tier(data)
            data["roles"]["reviewer"]["agy"] = {"model": "gemini-9-peak", "effort": "medium"}

        # A role that changes its effort keeps the family's fallback at the family's tier.
        row = agent_models.resolve("agy", "reviewer", fixture(effort_only))
        self.assertEqual(row["slug"], "gemini-9-peak-medium")
        self.assertEqual(row["fallback_slugs"], ["gemini-8-fast-low"])
        row = agent_models.resolve(
            "agy", "reviewer",
            fixture(lambda d: d["roles"]["reviewer"].update(agy={"effort": "medium"})),
        )
        self.assertEqual(row["fallback_slugs"], ["gemini-8-fast-high"])

        def own_stem(data: dict) -> None:
            family_tier(data)
            data["roles"]["reviewer"]["agy"] = {
                "model": "gemini-9-peak", "effort": "medium", "fallback_model": "gemini-9-test",
            }

        # A role's own fallback takes the role's effort, never the family's fallback tier.
        row = agent_models.resolve("agy", "reviewer", fixture(own_stem))
        self.assertEqual(row["fallback_slugs"], ["gemini-9-test-medium", "gemini-8-fast-low"])
        # A row that sets its fallback to null has none, whatever the family carries.
        row = agent_models.resolve("agy", "prun-worker", fixture(family_tier))
        self.assertIsNone(row["fallback_slug"])
        self.assertEqual(row["fallback_slugs"], [])

    def test_fallback_slugs_of_the_rows_that_name_no_second_step(self) -> None:
        self.assertEqual(
            agent_models.resolve("agy", "interactive", fixture())["fallback_slugs"], ["gemini-8-fast-high"]
        )
        # A row that drops its fallback has none: the family's is not put back.
        self.assertEqual(agent_models.resolve("agy", "prun-worker", fixture())["fallback_slugs"], [])
        self.assertNotIn("fallback_slugs", agent_models.resolve("codex", "reviewer", fixture()))

    def test_unknown_family_and_role_raise(self) -> None:
        with self.assertRaises(agent_models.RegistryError):
            agent_models.resolve("gemini", "interactive", fixture())
        with self.assertRaises(agent_models.RegistryError):
            agent_models.resolve("claude", "judge", fixture())

    def test_resolve_reads_the_registry_named_by_the_environment(self) -> None:
        with mock.patch.dict(os.environ, self.env):
            row = agent_models.resolve("claude", "worker")
        self.assertEqual(row["model"], "sonnet")

    def test_missing_registry_error_names_the_file(self) -> None:
        missing = self.dir / "absent.json"
        with mock.patch.dict(os.environ, {"AGENT_MODELS_FILE": str(missing)}):
            with self.assertRaises(agent_models.RegistryError) as raised:
                agent_models.resolve("claude", "interactive")
        self.assertIn(str(missing), str(raised.exception))

    def test_cli_prints_model_and_effort_or_one_field(self) -> None:
        code, out, _ = run_main(["resolve", "--family", "claude", "--role", "worker"], self.env)
        self.assertEqual((code, out.strip()), (0, "sonnet high"))
        code, out, _ = run_main(
            ["resolve", "--family", "agy", "--role", "prun-worker", "--field", "second_slug"], self.env
        )
        self.assertEqual((code, out.strip()), (0, "claude-sonnet-9-medium"))
        code, out, _ = run_main(["resolve", "--family", "codex", "--json"], self.env)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["effort"], "low")

    def test_cli_exits_two_when_a_field_is_absent_or_the_registry_is_bad(self) -> None:
        code, out, err = run_main(
            ["resolve", "--family", "agy", "--role", "interactive", "--field", "second_slug"], self.env
        )
        self.assertEqual((code, out), (2, ""))
        self.assertIn("second_slug", err)
        env = dict(self.env, AGENT_MODELS_FILE=str(self.dir / "absent.json"))
        code, _, err = run_main(["resolve", "--family", "claude"], env)
        self.assertEqual(code, 2)
        self.assertIn("registry not found", err)

    def test_cli_runs_as_a_script_with_the_registry_override(self) -> None:
        env = dict(os.environ, AGENT_MODELS_FILE=str(self.registry))
        result = subprocess.run(
            [sys.executable, str(RESOLVER), "resolve", "--family", "agy", "--field", "slug"],
            env=env, capture_output=True, text=True, check=False,
        )
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "gemini-9-test-high"), result.stderr)

    def test_show_lists_every_family_and_role(self) -> None:
        code, out, _ = run_main(["show", "--json"], self.env)
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), len(FIXTURE["families"]) * len(INHERITING_ROLES))

    def test_show_lists_the_advisor_row_only_when_the_registry_has_one(self) -> None:
        code, out, _ = run_main(["show", "--json"], self.env)
        self.assertEqual(code, 0)
        self.assertNotIn("advisor", {r["role"] for r in json.loads(out)})
        code, text, _ = run_main(["show"], self.env)
        self.assertEqual(code, 0)
        self.assertRegex(text, r"(?m)^claude\s+advisor\s+\(none\)\s+-\s")
        self.write_registry(fixture(with_advisor("opus")))
        code, out, _ = run_main(["show", "--json"], self.env)
        advisors = [r for r in json.loads(out) if r["role"] == "advisor"]
        self.assertEqual(advisors, [{"family": "claude", "role": "advisor", "model": "opus"}])
        code, text, _ = run_main(["show"], self.env)
        self.assertRegex(text, r"(?m)^claude\s+advisor\s+opus\s+-\s")
        self.assertNotIn("(none)", text)

    def test_show_lists_only_present_families_with_and_without_grok(self) -> None:
        code, out, _ = run_main(["show", "--json"], self.env)
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual({r["family"] for r in rows}, {"claude", "codex", "agy"})
        self.write_registry(fixture_with_grok())
        code, out, _ = run_main(["show", "--json"], self.env)
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), len(agent_models.FAMILIES) * len(INHERITING_ROLES))
        self.assertEqual({r["family"] for r in rows}, {"claude", "codex", "agy", "grok"})

    def test_resolve_grok_when_present_and_absent(self) -> None:
        with self.assertRaises(agent_models.RegistryError) as raised:
            agent_models.resolve("grok", "interactive", fixture())
        self.assertIn("families.grok", str(raised.exception))
        code, out, err = run_main(["resolve", "--family", "grok"], self.env)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("families.grok", err)
        self.write_registry(fixture_with_grok(lambda d: d["roles"]["prun-worker"]["grok"].update(effort="medium")))
        code, out, _ = run_main(["resolve", "--family", "grok", "--role", "reviewer"], self.env)
        self.assertEqual((code, out.strip()), (0, "grok-9-test high"))
        code, out, _ = run_main(["resolve", "--family", "grok", "--role", "prun-worker"], self.env)
        self.assertEqual((code, out.strip()), (0, "grok-9-test medium"))

    def test_the_advisor_inherits_nothing_from_the_family_row(self) -> None:
        # The fixture's claude family row names a model and an effort; neither
        # becomes an advisor. No advisor row means no advisor, in every family.
        data = fixture()
        self.assertIsNone(agent_models.advisor_model(data))
        for family in ("claude", "codex", "agy"):
            with self.assertRaises(agent_models.RegistryError, msg=family) as raised:
                agent_models.resolve(family, "advisor", data)
            self.assertIn("no advisor", str(raised.exception))
        code, out, err = run_main(["resolve", "--family", "claude", "--role", "advisor"], self.env)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("no advisor", err)

    def test_the_advisor_row_is_a_model_and_no_effort(self) -> None:
        data = fixture(with_advisor("opus"))
        self.assertEqual(agent_models.advisor_model(data), "opus")
        self.assertEqual(
            agent_models.resolve("claude", "advisor", data),
            {"family": "claude", "role": "advisor", "model": "opus"},
        )
        with self.assertRaises(agent_models.RegistryError):
            agent_models.resolve("codex", "advisor", data)
        self.write_registry(data)
        for argv, expected in (
            (["--family", "claude", "--role", "advisor"], (0, "opus")),
            (["--family", "claude", "--role", "advisor", "--field", "model"], (0, "opus")),
            (["--family", "claude", "--role", "advisor", "--field", "effort"], (2, "")),
            (["--family", "codex", "--role", "advisor"], (2, "")),
        ):
            code, out, _ = run_main(["resolve"] + argv, self.env)
            self.assertEqual((code, out.strip()), expected, argv)
        # The other claude roles are untouched by the advisor row.
        self.assertEqual(agent_models.resolve("claude", "interactive", data)["model"], "fable")


class ValidateTests(unittest.TestCase):
    def assert_rejected(self, mutate, fragment: str) -> None:
        with self.assertRaises(agent_models.RegistryError) as raised:
            agent_models.validate(fixture(mutate))
        self.assertIn(fragment, str(raised.exception))

    def test_fixture_is_valid(self) -> None:
        agent_models.validate(fixture())

    def test_a_fallback_stem_with_a_tier_is_rejected(self) -> None:
        # The resolver appends the tier, so a stem that already has one would name no model.
        self.assert_rejected(
            lambda d: d["families"]["agy"].update(fallback_model="gemini-8-fast-high"), "stem without a tier"
        )

    def test_a_fallback_tier_needs_its_stem_and_a_stem_needs_a_plain_name(self) -> None:
        self.assert_rejected(
            lambda d: d["roles"]["reviewer"].update(agy={"fallback_effort": "low"}), "needs a fallback_model"
        )
        self.assert_rejected(lambda d: d["families"]["agy"].update(fallback_model="bad name"), "bad fallback_model")
        self.assert_rejected(
            lambda d: d["roles"]["prun-worker"]["agy"].update(second_model="claude-sonnet-9-high"),
            "stem without a tier",
        )

    def test_fallback_effort_is_an_agy_key_with_an_agy_tier(self) -> None:
        agent_models.validate(fixture(lambda d: d["families"]["agy"].update(fallback_effort="low")))
        self.assert_rejected(lambda d: d["families"]["agy"].update(fallback_effort="xhigh"), "fallback_effort")
        self.assert_rejected(lambda d: d["families"]["codex"].update(fallback_effort="low"), "unknown keys")

    def test_rejects_a_bad_effort_for_the_family(self) -> None:
        self.assert_rejected(lambda d: d["families"]["claude"].update(effort="ultra"), "effort")
        self.assert_rejected(lambda d: d["families"]["agy"].update(effort="xhigh"), "effort")
        self.assert_rejected(
            lambda d: d["roles"]["worker"]["claude"].update(effort="turbo"), "role worker"
        )
        for bad_effort in ("minimal", "max", "ultra"):
            with self.assertRaises(agent_models.RegistryError) as raised:
                agent_models.validate(
                    fixture_with_grok(lambda d, e=bad_effort: d["families"]["grok"].update(effort=e))
                )
            self.assertIn("effort", str(raised.exception))
        with self.assertRaises(agent_models.RegistryError) as raised:
            agent_models.validate(
                fixture_with_grok(lambda d: d["roles"]["prun-worker"]["grok"].update(effort="ultra"))
            )
        self.assertIn("role prun-worker", str(raised.exception))

    def test_rejects_a_role_override_naming_an_absent_family(self) -> None:
        self.assert_rejected(
            lambda d: d["roles"]["worker"].update(grok={"effort": "high"}), "may only list"
        )
        agent_models.validate(fixture_with_grok())
        with self.assertRaises(agent_models.RegistryError) as raised:
            agent_models.validate(
                fixture_with_grok(lambda d: d["families"].update(gemini={"model": "x", "effort": "low"}))
            )
        self.assertIn("families must be exactly", str(raised.exception))

    def test_rejects_an_unknown_role_or_family(self) -> None:
        self.assert_rejected(lambda d: d["roles"].update(judge={}), "unknown registry roles")
        self.assert_rejected(lambda d: d["roles"]["worker"].update(gemini={"effort": "low"}), "may only list")
        self.assert_rejected(lambda d: d["families"].pop("agy"), "families must be exactly")

    def test_the_advisor_row_is_claude_only_with_a_model_and_no_effort(self) -> None:
        agent_models.validate(fixture(with_advisor("opus")))
        agent_models.validate(fixture(lambda d: d["roles"].update(advisor={})))
        for advisor, fragment in (
            ({"claude": {"model": "opus", "effort": "high"}}, "model only"),
            ({"claude": {"effort": "high"}}, "model only"),
            ({"claude": {}}, "model is required"),
            ({"claude": {"model": None}}, "model is required"),
            ({"claude": {"model": "bad name"}}, "bad model name"),
            ({"claude": {"model": "opus", "fallback_model": "x"}}, "unknown keys"),
            ({"claude": "opus"}, "row must be an object"),
            ({"codex": {"model": "gpt-9-test"}}, "may only list claude"),
            ({"agy": {"model": "gemini-9-test"}}, "may only list claude"),
            ({"claude": {"model": "opus"}, "codex": {"model": "gpt-9-test"}}, "may only list claude"),
            ([], "may only list claude"),
        ):
            with self.subTest(advisor=advisor):
                self.assert_rejected(lambda d, a=advisor: d["roles"].update(advisor=a), fragment)

    def test_rejects_an_agy_stem_that_carries_a_tier(self) -> None:
        for tier in agent_models.AGY_TIERS:
            self.assert_rejected(
                lambda d, tier=tier: d["families"]["agy"].update(model=f"gemini-9-test-{tier}"),
                "stem without a tier",
            )

    def test_rejects_the_wrong_schema_and_missing_family_fields(self) -> None:
        self.assert_rejected(lambda d: d.update(schema=2), "schema must be 1")
        self.assert_rejected(lambda d: d["families"]["codex"].pop("effort"), "required")
        self.assert_rejected(lambda d: d["families"]["codex"].update(model="bad name"), "bad model name")
        self.assert_rejected(lambda d: d["families"]["codex"].update(second_model="x"), "unknown keys")

    def test_load_rejects_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(agent_models.RegistryError):
                agent_models.load(path)

    def test_load_accepts_a_byte_order_mark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bom.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps(FIXTURE).encode("utf-8"))
            self.assertEqual(agent_models.load(path)["schema"], 1)


class RealRegistryTests(unittest.TestCase):
    def test_real_registry_validates_and_resolves_every_family_and_role(self) -> None:
        data = agent_models.load(REAL_REGISTRY)
        for family in agent_models.FAMILIES:
            for role in INHERITING_ROLES:
                row = agent_models.resolve(family, role, data)
                self.assertTrue(row["model"] and row["effort"], (family, role))
                if family == "agy":
                    self.assertTrue(row["slug"].endswith("-" + row["effort"]), (family, role))

    def test_real_registry_lists_a_claude_advisor_and_no_effort_for_it(self) -> None:
        data = agent_models.load(REAL_REGISTRY)
        row = agent_models.resolve("claude", "advisor", data)
        self.assertTrue(row["model"])
        self.assertNotIn("effort", row)

    def test_user_settings_mirror_the_real_registry(self) -> None:
        """`render --check` on the real files: user/settings.json must equal the render."""
        env = {k: v for k, v in os.environ.items() if k != "AGENT_MODELS_FILE"}
        result = subprocess.run(
            [sys.executable, str(RESOLVER), "render", "--check"],
            env=env, capture_output=True, text=True, check=False, cwd=str(ROOT),
        )
        self.assertEqual(
            result.returncode, 0,
            "user/settings.json is stale; run scripts/agent_models.py render. " + result.stderr,
        )


class RenderTests(TempRegistryCase):
    def settings_file(self, data: dict = None) -> Path:
        path = self.dir / "settings.json"
        path.write_text(json.dumps(data or SETTINGS_FIXTURE, indent=2) + "\n", encoding="utf-8")
        return path

    def test_render_sets_the_four_mirrored_keys_and_keeps_the_rest(self) -> None:
        path = self.settings_file()
        code, _, _ = run_main(["render", "--settings", str(path)], self.env)
        self.assertEqual(code, 0)
        rendered = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rendered["model"], "fable")
        self.assertEqual(rendered["effortLevel"], "medium")
        self.assertEqual(rendered["env"]["CLAUDE_CODE_EFFORT_LEVEL"], "medium")
        self.assertEqual(rendered["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "sonnet")
        self.assertEqual(rendered["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "65")
        self.assertEqual(rendered["permissions"], SETTINGS_FIXTURE["permissions"])

    def test_check_exits_one_when_stale_then_zero_after_render(self) -> None:
        path = self.settings_file()
        code, _, err = run_main(["render", "--check", "--settings", str(path)], self.env)
        self.assertEqual(code, 1)
        self.assertIn("stale", err)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["model"], "stale")
        run_main(["render", "--settings", str(path)], self.env)
        code, _, _ = run_main(["render", "--check", "--settings", str(path)], self.env)
        self.assertEqual(code, 0)

    def test_render_is_idempotent(self) -> None:
        path = self.settings_file()
        run_main(["render", "--settings", str(path)], self.env)
        first = path.read_bytes()
        run_main(["render", "--settings", str(path)], self.env)
        self.assertEqual(path.read_bytes(), first)

    def test_a_max_effort_goes_to_the_env_entry_only(self) -> None:
        self.write_registry(fixture(lambda d: d["families"]["claude"].update(effort="max")))
        path = self.settings_file()
        run_main(["render", "--settings", str(path)], self.env)
        rendered = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("effortLevel", rendered)
        self.assertEqual(rendered["env"]["CLAUDE_CODE_EFFORT_LEVEL"], "max")

    def test_the_advisor_key_follows_the_registry(self) -> None:
        # (settings file carries the key, registry lists an advisor, value after render)
        for has_key, has_row, expected in (
            (False, True, "opus"),     # an advisor row adds the key
            (True, True, "opus"),      # and keeps an existing one equal to the registry
            (True, False, None),       # no advisor row removes the key
            (False, False, None),
        ):
            with self.subTest(has_key=has_key, has_row=has_row):
                self.write_registry(fixture(with_advisor("opus") if has_row else None))
                settings = dict(SETTINGS_FIXTURE, **({"advisorModel": "stale"} if has_key else {}))
                path = self.settings_file(settings)
                self.assertEqual(run_main(["render", "--settings", str(path)], self.env)[0], 0)
                rendered = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(rendered.get("advisorModel"), expected)
                self.assertEqual("advisorModel" in rendered, expected is not None)
                # The other mirrored keys and the file's own keys are unaffected.
                self.assertEqual(rendered["model"], "fable")
                self.assertEqual(rendered["permissions"], SETTINGS_FIXTURE["permissions"])
                self.assertEqual(run_main(["render", "--check", "--settings", str(path)], self.env)[0], 0)

    def test_render_then_bootstrap_merge_delivers_and_keeps_the_advisor(self) -> None:
        # The path to a machine: render the shared file, then bootstrap's merge.
        shared = self.settings_file()
        live = self.dir / "live-settings.json"
        off = {"CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1"}
        live.write_text(json.dumps({"theme": "dark", "env": off}) + "\n", encoding="utf-8")
        merge = [sys.executable, str(SCRIPTS / "merge_settings.py"), str(live), str(shared)]
        self.write_registry(fixture(with_advisor("opus")))
        run_main(["render", "--settings", str(shared)], self.env)
        self.assertEqual(subprocess.run(merge, capture_output=True).returncode, 0)
        merged = json.loads(live.read_text(encoding="utf-8"))
        self.assertEqual(merged["advisorModel"], "opus")
        self.assertEqual(merged["theme"], "dark")
        # The machine's off switch is not a shared key, so the merge keeps it.
        self.assertEqual(merged["env"]["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"], "1")
        # Removing the row renders the key out of the shared file; the merge never deletes it.
        self.write_registry(fixture())
        run_main(["render", "--settings", str(shared)], self.env)
        self.assertNotIn("advisorModel", json.loads(shared.read_text(encoding="utf-8")))
        self.assertEqual(subprocess.run(merge, capture_output=True).returncode, 0)
        self.assertEqual(json.loads(live.read_text(encoding="utf-8"))["advisorModel"], "opus")

    def test_check_reports_a_stale_advisor_value_and_render_repairs_it(self) -> None:
        self.write_registry(fixture(with_advisor("opus")))
        path = self.settings_file(dict(SETTINGS_FIXTURE, advisorModel="opus"))
        run_main(["render", "--settings", str(path)], self.env)
        rendered = json.loads(path.read_text(encoding="utf-8"))
        rendered["advisorModel"] = "sonnet"
        path.write_text(json.dumps(rendered, indent=2) + "\n", encoding="utf-8")
        code, _, err = run_main(["render", "--check", "--settings", str(path)], self.env)
        self.assertEqual(code, 1)
        self.assertIn("stale", err)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["advisorModel"], "sonnet")
        # A registry change reaches the file the same way.
        run_main(["render", "--settings", str(path)], self.env)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["advisorModel"], "opus")
        self.write_registry(fixture())
        self.assertEqual(run_main(["render", "--check", "--settings", str(path)], self.env)[0], 1)
        run_main(["render", "--settings", str(path)], self.env)
        self.assertNotIn("advisorModel", json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(run_main(["render", "--check", "--settings", str(path)], self.env)[0], 0)


class SetTests(TempRegistryCase):
    def test_set_changes_the_family_row_and_validates(self) -> None:
        code, _, _ = run_main(["set", "--family", "codex", "--effort", "high"], self.env)
        self.assertEqual(code, 0)
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(data["families"]["codex"]["effort"], "high")
        self.assertEqual(data["families"]["codex"]["model"], "gpt-9-test")

    def test_set_with_a_worker_role_writes_a_role_override(self) -> None:
        code, _, _ = run_main(
            ["set", "--family", "codex", "--role", "worker", "--model", "gpt-9-other"], self.env
        )
        self.assertEqual(code, 0)
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(data["roles"]["worker"]["codex"], {"model": "gpt-9-other"})
        self.assertEqual(data["families"]["codex"]["model"], "gpt-9-test")

    def test_set_writes_a_role_fallback_and_refuses_one_on_another_family(self) -> None:
        code, _, _ = run_main(
            ["set", "--family", "agy", "--role", "reviewer", "--role-only", "--model", "gemini-9-peak",
             "--effort", "medium", "--fallback-model", "gemini-9-test", "--fallback-effort", "high"],
            self.env,
        )
        self.assertEqual(code, 0)
        row = agent_models.resolve("agy", "reviewer", agent_models.load(self.registry))
        self.assertEqual(row["slug"], "gemini-9-peak-medium")
        self.assertEqual(row["fallback_slugs"], ["gemini-9-test-high", "gemini-8-fast-high"])
        # A new stem does not keep the tier that was written for the old one.
        code, _, _ = run_main(
            ["set", "--family", "agy", "--role", "reviewer", "--role-only", "--fallback-model", "gemini-9-other"],
            self.env,
        )
        self.assertEqual(code, 0)
        row = agent_models.resolve("agy", "reviewer", agent_models.load(self.registry))
        self.assertEqual(row["fallback_slug"], "gemini-9-other-medium")
        before = self.registry.read_bytes()
        code, _, _ = run_main(["set", "--family", "codex", "--fallback-effort", "high"], self.env)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_set_refuses_an_invalid_value_and_leaves_the_file_alone(self) -> None:
        before = self.registry.read_bytes()
        code, _, err = run_main(["set", "--family", "agy", "--effort", "max"], self.env)
        self.assertEqual(code, 2)
        self.assertIn("effort", err)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_set_writes_the_advisor_row_and_off_removes_it(self) -> None:
        code, out, _ = run_main(["set", "--family", "claude", "--role", "advisor", "--model", "opus"], self.env)
        self.assertEqual(code, 0)
        self.assertIn("opus", out)
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(data["roles"]["advisor"], {"claude": {"model": "opus"}})
        self.assertEqual(data["families"]["claude"], FIXTURE["families"]["claude"])
        code, _, _ = run_main(["set", "--family", "claude", "--role", "advisor", "--model", "sonnet"], self.env)
        self.assertEqual(agent_models.advisor_model(json.loads(self.registry.read_text(encoding="utf-8"))), "sonnet")
        code, out, _ = run_main(["set", "--family", "claude", "--role", "advisor", "--off"], self.env)
        self.assertEqual(code, 0)
        self.assertIn("off", out)
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(data["roles"]["advisor"], {})
        self.assertIsNone(agent_models.advisor_model(data))
        # Turning off an advisor that is already off is not an error.
        code, _, _ = run_main(["set", "--family", "claude", "--role", "advisor", "--off"], self.env)
        self.assertEqual(code, 0)

    def test_set_refuses_an_advisor_effort_a_wrong_family_and_a_misplaced_off(self) -> None:
        self.write_registry(fixture(with_advisor("opus")))
        before = self.registry.read_bytes()
        for argv, fragment in (
            (["--family", "claude", "--role", "advisor", "--effort", "high"], "model only"),
            (["--family", "claude", "--role", "advisor", "--model", "opus", "--effort", "high"], "model only"),
            (["--family", "codex", "--role", "advisor", "--model", "gpt-9-test"], "may only list claude"),
            (["--family", "codex", "--role", "advisor", "--off"], "may only list claude"),
            (["--family", "claude", "--role", "advisor", "--off", "--model", "opus"], "--off takes no other"),
            (["--family", "claude", "--role", "worker", "--off"], "--off applies to"),
            (["--family", "claude", "--off"], "--off applies to"),
            (["--family", "claude", "--role", "advisor"], "set needs"),
        ):
            with self.subTest(argv=argv):
                code, _, err = run_main(["set"] + argv, self.env)
                self.assertEqual(code, 2)
                self.assertIn(fragment, err)
                self.assertEqual(self.registry.read_bytes(), before)

    def test_set_on_the_real_paths_renders_the_advisor_key(self) -> None:
        tree = self.dir / "tree-settings.json"
        tree.write_text(json.dumps(SETTINGS_FIXTURE, indent=2) + "\n", encoding="utf-8")
        with mock.patch.object(agent_models, "DEFAULT_REGISTRY", self.registry), \
                mock.patch.object(agent_models, "DEFAULT_SETTINGS", tree):
            run_main(["set", "--family", "claude", "--role", "advisor", "--model", "opus"], self.env)
            self.assertEqual(json.loads(tree.read_text(encoding="utf-8"))["advisorModel"], "opus")
            run_main(["set", "--family", "claude", "--role", "advisor", "--off"], self.env)
            self.assertNotIn("advisorModel", json.loads(tree.read_text(encoding="utf-8")))

    def test_set_grok_without_family_row_raises_registry_error_and_with_row_updates(self) -> None:
        before = self.registry.read_bytes()
        args = agent_models.argparse.Namespace(
            family="grok", role=None, role_only=False, model="grok-9-test", effort="high"
        )
        with mock.patch.dict(os.environ, self.env):
            with self.assertRaises(agent_models.RegistryError) as raised:
                agent_models.cmd_set(args)
        self.assertIn("families.grok", str(raised.exception))
        code, _, err = run_main(["set", "--family", "grok", "--effort", "low"], self.env)
        self.assertEqual(code, 2)
        self.assertIn("families.grok", err)
        self.assertEqual(self.registry.read_bytes(), before)
        self.write_registry(fixture_with_grok())
        code, _, _ = run_main(["set", "--family", "grok", "--effort", "xhigh"], self.env)
        self.assertEqual(code, 0)
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(data["families"]["grok"]["effort"], "xhigh")
        code, _, _ = run_main(
            ["set", "--family", "grok", "--role", "prun-worker", "--effort", "medium"], self.env
        )
        self.assertEqual(code, 0)
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(data["roles"]["prun-worker"]["grok"]["effort"], "medium")


class TomlTests(unittest.TestCase):
    SAMPLE = (
        "# my Codex config\n"
        'model = "old-model"   # keep this note\n'
        'model_reasoning_effort = "low"\n'
        "approval_policy = \"on-request\"\n"
        "\n"
        "[profiles.review]\n"
        'model = "profile-model"\n'
        'model_reasoning_effort = "high"\n'
        "\n"
        "[features]\n"
        "fast_mode = false\n"
    )

    def test_top_level_reader_stops_at_the_first_table(self) -> None:
        values = agent_models.toml_top_level(self.SAMPLE)
        self.assertEqual(values["model"], "old-model")
        self.assertEqual(values["approval_policy"], "on-request")
        self.assertNotIn("fast_mode", values)

    @needs_toml_parser
    def test_edits_only_top_level_keys_and_keeps_comments(self) -> None:
        text = agent_models.toml_set_top_level(
            self.SAMPLE, {"model": "new-model", "model_reasoning_effort": "medium"}
        )
        self.assertIn('model = "new-model"   # keep this note\n', text)
        self.assertIn('model_reasoning_effort = "medium"\n', text)
        self.assertIn("# my Codex config\n", text)
        self.assertIn('[profiles.review]\nmodel = "profile-model"\nmodel_reasoning_effort = "high"\n', text)
        self.assertIn("[features]\nfast_mode = false\n", text)
        self.assertEqual(text.count("old-model"), 0)

    def test_no_change_when_values_already_match(self) -> None:
        text = agent_models.toml_set_top_level(
            self.SAMPLE, {"model": "old-model", "model_reasoning_effort": "low"}
        )
        self.assertEqual(text, self.SAMPLE)

    @needs_toml_parser
    def test_missing_key_is_inserted_before_the_first_table(self) -> None:
        source = "approval_policy = \"on-request\"\n\n[features]\nfast_mode = false\n"
        text = agent_models.toml_set_top_level(source, {"model": "added"})
        self.assertEqual(
            text, "approval_policy = \"on-request\"\nmodel = \"added\"\n\n[features]\nfast_mode = false\n"
        )
        self.assertEqual(agent_models.toml_top_level(text)["model"], "added")

    @needs_toml_parser
    def test_insertion_into_a_file_without_tables_or_a_final_newline(self) -> None:
        text = agent_models.toml_set_top_level('approval_policy = "x"', {"model": "added"})
        self.assertEqual(text, 'approval_policy = "x"\nmodel = "added"\n')
        self.assertEqual(agent_models.toml_set_top_level("", {"model": "added"}), 'model = "added"\n')

    @needs_toml_parser
    def test_crlf_line_endings_survive(self) -> None:
        source = self.SAMPLE.replace("\n", "\r\n")
        text = agent_models.toml_set_top_level(
            source, {"model": "new-model", "model_reasoning_effort": "medium", "service": "extra"}
        )
        self.assertNotRegex(text, r"(?<!\r)\n")
        self.assertIn('model = "new-model"   # keep this note\r\n', text)
        self.assertIn('service = "extra"\r\n', text)
        self.assertIn('[profiles.review]\r\nmodel = "profile-model"\r\n', text)

    @needs_toml_parser
    def test_a_key_that_is_a_prefix_of_another_is_not_confused(self) -> None:
        source = 'model_provider = "openai"\nmodel = "old"\n'
        text = agent_models.toml_set_top_level(source, {"model": "new"})
        self.assertEqual(text, 'model_provider = "openai"\nmodel = "new"\n')


class AgySlugTests(unittest.TestCase):
    def test_display_to_slug(self) -> None:
        self.assertEqual(agent_models.display_to_slug("Gemini 4 Argon (High)"), "gemini-4-argon-high")
        self.assertEqual(agent_models.display_to_slug("  Gemini 3.8 Flash (Low) "), "gemini-3.8-flash-low")

    def test_family_key_ignores_the_version_but_keeps_name_and_tier(self) -> None:
        key = agent_models.family_key("gemini-4-argon-high")
        self.assertEqual(key, ("gemini", ("argon",), "high"))
        self.assertEqual(agent_models.family_key("gemini-4.1-argon-high"), key)
        self.assertEqual(agent_models.family_key("gemini-4-5-argon-high"), key)
        self.assertEqual(agent_models.family_key("gemini-4-argon"), ("gemini", ("argon",), None))
        self.assertEqual(agent_models.family_key("claude-sonnet-9-high"), ("claude-sonnet", (), "high"))
        self.assertNotEqual(agent_models.family_key("gemini-4-flash-high"), key)
        self.assertIsNone(agent_models.family_key("no-version-here"))

    def test_newest_in_family_floats_the_version_within_the_tier(self) -> None:
        available = [
            "gemini-4-argon-high", "gemini-4.1-argon-high", "gemini-4.2-argon-low",
            "gemini-3.8-flash-high",
        ]
        self.assertEqual(agent_models.newest_in_family("gemini-4-argon-high", available), "gemini-4.1-argon-high")
        self.assertEqual(agent_models.newest_in_family("gemini-4-argon-low", available), "gemini-4.2-argon-low")
        self.assertEqual(agent_models.newest_in_family("gemini-3.8-flash-high", available), "gemini-3.8-flash-high")

    def test_newest_in_family_returns_the_template_when_nothing_matches(self) -> None:
        self.assertEqual(
            agent_models.newest_in_family("gemini-9-test-high", ["gemini-4-argon-high"]), "gemini-9-test-high"
        )
        self.assertEqual(agent_models.newest_in_family("gemini-9-test-high", []), "gemini-9-test-high")

    def test_newest_in_family_falls_back_to_high_when_the_tier_is_absent(self) -> None:
        available = ["gemini-4-argon-high", "gemini-5-argon-high"]
        self.assertEqual(agent_models.newest_in_family("gemini-4-argon-low", available), "gemini-5-argon-high")


class MatchesTests(unittest.TestCase):
    def test_claude_alias_matches_any_version_of_that_family(self) -> None:
        self.assertTrue(agent_models.matches("claude", "fable", "high", "claude-fable-5-1", "high"))
        self.assertTrue(agent_models.matches("claude", "sonnet", None, "claude-sonnet-5-5", "low"))

    def test_claude_alias_rejects_another_model(self) -> None:
        self.assertFalse(agent_models.matches("claude", "fable", "high", "claude-sonnet-5-5", "high"))
        self.assertFalse(agent_models.matches("claude", "sonnet", "high", "claude-sonnet", "high"))

    def test_claude_full_id_needs_an_exact_match(self) -> None:
        self.assertTrue(agent_models.matches("claude", "claude-x-1", None, "claude-x-1", None))
        self.assertFalse(agent_models.matches("claude", "claude-x-1", None, "claude-x-2", None))

    def test_effort_mismatch_fails_and_a_missing_expectation_passes(self) -> None:
        self.assertFalse(agent_models.matches("claude", "fable", "high", "claude-fable-5-1", "low"))
        self.assertFalse(agent_models.matches("claude", "fable", "high", "claude-fable-5-1", None))
        self.assertTrue(agent_models.matches("claude", "fable", None, "claude-fable-5-1", "low"))

    def test_codex_needs_the_exact_model(self) -> None:
        self.assertTrue(agent_models.matches("codex", "gpt-9-test", "low", "gpt-9-test", "low"))
        self.assertFalse(agent_models.matches("codex", "gpt-9-test", "low", "gpt-9-test-mini", "low"))
        self.assertFalse(agent_models.matches("codex", "gpt-9-test", "low", "gpt-9-test", "high"))

    def test_agy_ignores_the_version_but_not_the_family_or_tier(self) -> None:
        self.assertTrue(agent_models.matches("agy", "gemini-9-test", "high", "gemini-9-test-high", "high"))
        self.assertTrue(agent_models.matches("agy", "gemini-9-test", "high", "gemini-9.2-test-high", "high"))
        self.assertTrue(agent_models.matches("agy", "gemini-9-test-high", "high", "gemini-10-test-high", "high"))
        self.assertFalse(agent_models.matches("agy", "gemini-9-test", "high", "gemini-9-other-high", "high"))
        self.assertFalse(agent_models.matches("agy", "gemini-9-test", "high", "gemini-9-test-low", "low"))
        self.assertFalse(agent_models.matches("agy", "gemini-9-test", "high", "not-a-slug", "high"))

    def test_grok_matches_build_variant_and_rejects_build_fast_in_either_direction(self) -> None:
        compare = agent_models.compare
        self.assertEqual(compare("grok", "grok-4.7", "high", "grok-4.7", "high"), "ok")
        self.assertEqual(compare("grok", "grok-4.7", "high", "grok-4.7-build", "high"), "ok")
        self.assertEqual(compare("grok", "grok-4.7", "high", "grok-4.7-build-fast", "high"), "mismatch")
        self.assertEqual(compare("grok", "grok-4.7-build-fast", "high", "grok-4.7-build", "high"), "mismatch")
        self.assertEqual(compare("grok", "grok-4.7-build-fast", "high", "grok-4.7", "high"), "mismatch")
        self.assertEqual(compare("grok", "grok-4.7-build-fast", "high", "grok-4.7-build-fast", "high"), "ok")
        self.assertEqual(compare("grok", "grok-4.7", "high", "grok-4.6-build", "high"), "mismatch")
        self.assertEqual(compare("grok", "grok-4.7", "high", "grok-4.7-build", "low"), "mismatch")
        self.assertEqual(compare("grok", "grok-4.7", "high", "grok-4.7-build", None), "unknown")


class ActualReaderTests(unittest.TestCase):
    def test_claude_actual_reads_the_last_assistant_record(self) -> None:
        sid = "11111111-2222-4333-8444-555555555555"
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "projects" / "-some-project" / (sid + ".jsonl")
            transcript.parent.mkdir(parents=True)
            records = [
                {"type": "user", "message": {"role": "user", "content": "hi"}},
                {"type": "assistant", "message": {"model": "claude-fable-5-1"}, "effort": "high"},
                {"type": "assistant", "message": {"model": "claude-fable-5-2"}, "effort": "medium"},
            ]
            transcript.write_text(
                "\n".join(json.dumps(r) for r in records) + "\nnot json\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                self.assertEqual(agent_models.claude_actual(sid), ("claude-fable-5-2", "medium"))
                self.assertIsNone(agent_models.claude_actual("00000000-0000-4000-8000-000000000000"))

    def test_claude_actual_without_an_effort_field_returns_none_for_effort(self) -> None:
        sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "projects" / "p" / (sid + ".jsonl")
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5-5"}}) + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                self.assertEqual(agent_models.claude_actual(sid), ("claude-sonnet-5-5", None))

    def test_codex_actual_reads_the_stderr_header(self) -> None:
        stderr = (
            "OpenAI Codex v9.9\n--------\nworkdir: /tmp/x\nmodel: gpt-9-test\n"
            "provider: openai\nreasoning effort: medium\nsession id: abc\n--------\n"
        )
        self.assertEqual(agent_models.codex_actual(stderr), ("gpt-9-test", "medium"))
        self.assertEqual(agent_models.codex_actual("model: gpt-9-test\n"), ("gpt-9-test", None))
        self.assertIsNone(agent_models.codex_actual("nothing useful\n"))

    def test_agy_actual_takes_the_propagated_label_not_the_preauth_noise(self) -> None:
        log = (
            "Model ID gemini-4-argon-high not in local config, defaulting to CCPA\n"
            'Propagating selected model override to backend: label="Gemini 3.8 Flash (Medium)"\n'
            'Propagating selected model override to backend: label="Gemini 4 Argon (High)"\n'
        )
        self.assertEqual(agent_models.agy_actual(log), ("gemini-4-argon-high", "high"))
        self.assertIsNone(
            agent_models.agy_actual("Model ID x not in local config, defaulting to CCPA\n")
        )

    def run_actual(self, argv: list) -> tuple:
        return run_main(["actual"] + argv, {})

    def test_cmd_actual_reports_ok_mismatch_and_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stderr_file = Path(tmp) / "codex.err"
            stderr_file.write_text("model: gpt-9-test\nreasoning effort: low\n", encoding="utf-8")
            base = ["--family", "codex", "--stderr-file", str(stderr_file)]
            code, out, err = self.run_actual(base + ["--expect-model", "gpt-9-test", "--expect-effort", "low"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "actual=gpt-9-test low status=ok")
            self.assertEqual(err, "")
            code, out, err = self.run_actual(base + ["--expect-model", "gpt-9-test", "--expect-effort", "high"])
            self.assertEqual(code, 0)
            self.assertIn("status=mismatch", out)
            self.assertIn("MODEL-MISMATCH", err)
            code, out, err = self.run_actual(["--family", "codex", "--stderr-file", str(Path(tmp) / "missing")])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "actual=unknown status=unknown")
            self.assertIn("MODEL-UNVERIFIED", err)

    def test_cmd_actual_for_agy_compares_the_slug_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "agy.log"
            log.write_text(
                'Propagating selected model override to backend: label="Gemini 9.2 Test (High)"\n',
                encoding="utf-8",
            )
            code, out, err = self.run_actual(
                ["--family", "agy", "--log-file", str(log), "--expect-model", "gemini-9-test-high",
                 "--expect-effort", "high"]
            )
            self.assertEqual((code, out.strip(), err), (0, "actual=gemini-9.2-test-high high status=ok", ""))

    def test_grok_home_expands_a_leading_tilde_like_the_dispatchers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp, "GROK_HOME": "~/grok-home"}):
                self.assertEqual(agent_models.grok_home_path(), Path(tmp) / "grok-home")
            with mock.patch.dict(os.environ, {"GROK_HOME": "  "}):
                self.assertEqual(agent_models.grok_home_path(), Path.home() / ".grok")

    def test_grok_actual_reads_end_event_and_session_files_in_grok_home(self) -> None:
        sid = "01a1ccb0-fc1a-7703-9a57-1c45f1840ec3"
        with tempfile.TemporaryDirectory() as tmp:
            grok_home = Path(tmp) / "grok-home"
            session_dir = grok_home / "sessions" / "%2Ftmp%2F hashed-cwd" / sid
            session_dir.mkdir(parents=True)
            end_event = json.dumps({
                "type": "end",
                "stopReason": "end_turn",
                "sessionId": sid,
                "modelUsage": {"grok-4.7-build": {"inputTokens": 100, "outputTokens": 10, "modelCalls": 1}},
            })
            with mock.patch.dict(os.environ, {"GROK_HOME": str(grok_home)}):
                self.assertEqual(agent_models.grok_actual(end_event), ("grok-4.7-build", None))
                (session_dir / "summary.json").write_text(
                    json.dumps({
                        "info": {"id": sid},
                        "current_model_id": "grok-4.7",
                        "reasoning_effort": "high",
                    }),
                    encoding="utf-8",
                )
                self.assertEqual(agent_models.grok_actual(end_event), ("grok-4.7-build", "high"))
                (session_dir / "usage.json").write_text(
                    json.dumps({
                        "sessionId": sid,
                        "session": {
                            "primaryModelId": "grok-4.7-build",
                            "modelUsage": {"grok-4.7-build": {"modelCalls": 1}},
                        },
                    }),
                    encoding="utf-8",
                )
                self.assertEqual(agent_models.grok_actual(session_id=sid), ("grok-4.7-build", "high"))
                json_file = Path(tmp) / "grok-result.json"
                json_file.write_text('{"type":"text","data":"OK"}\n' + end_event + "\n", encoding="utf-8")
                code, out, err = run_main(
                    ["actual", "--family", "grok", "--json-file", str(json_file),
                     "--expect-model", "grok-4.7", "--expect-effort", "high"],
                    {"GROK_HOME": str(grok_home)},
                )
                self.assertEqual((code, out.strip(), err), (0, "actual=grok-4.7-build high status=ok", ""))
                code, out, err = run_main(
                    ["actual", "--family", "grok", "--session-id", sid,
                     "--expect-model", "grok-4.7-build-fast", "--expect-effort", "high"],
                    {"GROK_HOME": str(grok_home)},
                )
                self.assertEqual(code, 0)
                self.assertIn("status=mismatch", out)
                self.assertIn("MODEL-MISMATCH", err)


class MapTests(unittest.TestCase):
    def readers(self, text: str, suffix: str = ".md") -> set:
        return agent_models.map_text(text, suffix)[0]

    def test_a_resolver_call_names_its_family_and_role(self) -> None:
        self.assertEqual(
            self.readers("<resolver> resolve --family codex --role reviewer prints two words"),
            {("codex", "reviewer", "resolver")},
        )
        self.assertEqual(
            self.readers('[sys.executable, R, "resolve", "--family", "agy", "--role", "prun-worker"]', ".py"),
            {("agy", "prun-worker", "resolver")},
        )
        self.assertEqual(
            self.readers('row = module.resolve("agy", "reviewer")', ".py"), {("agy", "reviewer", "resolver")}
        )

    def test_a_role_passed_by_name_is_found_through_its_assignment(self) -> None:
        text = 'ROLE = "prun-worker"\nargv = ["resolve", "--family", family, "--role", ROLE, "--json"]\n'
        self.assertEqual(self.readers(text, ".py"), {("*", "prun-worker", "resolver")})
        # A family written out beside the named role is kept.
        text = 'ROLE = "prun-worker"\nargv = ["resolve", "--family", "agy", "--role", ROLE]\n'
        self.assertEqual(self.readers(text, ".py"), {("agy", "prun-worker", "resolver")})
        # A name the file never assigns a role to is not a reader.
        self.assertEqual(self.readers('argv = ["--family", "agy", "--role", role]\n', ".py"), set())

    def test_dispatchers_settings_and_slash_commands_stand_for_a_row(self) -> None:
        self.assertIn(("agy", "reviewer", "dispatch-gemini.py --print-model"),
                      self.readers("run dispatch-gemini.py --print-model first"))
        # The flag alone belongs to no dispatcher, and one call is one reader.
        self.assertEqual(self.readers("pass --print-model to see what a tool would run"), set())
        self.assertEqual(
            self.readers("run dispatch-gemini.py --print-model first"),
            {("agy", "reviewer", "dispatch-gemini.py --print-model")},
        )
        self.assertIn(("grok", "prun-worker", "dispatch-task-grok"), self.readers("dispatch-task-grok.py"))
        self.assertIn(("claude", "worker", "CLAUDE_CODE_SUBAGENT_MODEL"),
                      self.readers("follows CLAUDE_CODE_SUBAGENT_MODEL", ".js"))
        self.assertIn(("*", "reviewer", "/vet"), self.readers("then run /vet both on the staged batch"))
        self.assertIn(("*", "prun-worker", "/prun"), self.readers("dispatch bounded /prun workers"))
        # A slash command is prose: a path such as skills/vet is not one, nor is code.
        self.assertEqual(self.readers("see skills/vet/SKILL.md"), set())
        self.assertEqual(self.readers("x = a /vet", ".py"), set())
        self.assertEqual(
            self.readers("export const meta = {}\nawait agent('do it')\n", ".js"),
            {("claude", "worker", "Workflow agent()")},
        )

    def test_direct_launches_are_listed_as_candidates(self) -> None:
        launches = lambda text: agent_models.map_text(text, ".py")[1]  # noqa: E731
        self.assertEqual(launches("Direct `claude -p` and `codex exec` are routes."), {"claude -p", "codex exec"})
        self.assertEqual(launches('subprocess.run(["codex", "exec", "--json"])'), {"codex exec"})
        self.assertEqual(launches("run(['agy', '--model', slug])"), {"agy --model"})
        self.assertEqual(launches('exec claude --advisor "$ADVISOR"'), {"claude --advisor"})
        self.assertEqual(launches("a claude-powered tool, executed"), set())
        # A comment in a script is not a launch; prose in a skill file is still read.
        self.assertEqual(launches("    # + direct codex exec, because the wrapper hides it"), set())
        self.assertEqual(agent_models.map_text("# Run `codex exec` here", ".md")[1], {"codex exec"})

    def test_map_repo_lists_readers_and_launches_and_skips_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "consumer"
            panel = repo / "skills" / "panel"
            (panel / "references").mkdir(parents=True)
            (panel / "SKILL.md").write_text("Run the panel.\n", encoding="utf-8")
            (panel / "references" / "protocol.md").write_text(
                "Direct `claude -p`, `agy -p`, and `codex exec` are candidate routes.\n", encoding="utf-8")
            audit = repo / "skills" / "audit"
            audit.mkdir(parents=True)
            # Every direct launch is listed, read or not; a marked line is left out.
            (audit / "SKILL.md").write_text(
                "Launch `codex exec` with the values below, then `claude -p` for a second read.\n"
                "Run /vet afterwards.\n"
                "Probe with `grok -p` <!-- agent-models: allow -->\n", encoding="utf-8")
            (audit / "route.py").write_text('ARGS = ["resolve", "--family", "codex", "--role", "reviewer"]\n',
                                            encoding="utf-8")
            (repo / "README.md").write_text("Try `agy -p` by hand.\n", encoding="utf-8")
            # A dated record and a test fixture are not live launch sites.
            record = repo / "skills" / "panel" / "runs" / "2026-01-02-round"
            record.mkdir(parents=True)
            (record / "run.sh").write_text("codex exec --json\n", encoding="utf-8")
            (repo / "scripts" / "tests").mkdir(parents=True)
            (repo / "scripts" / "tests" / "t.sh").write_text("claude -p hi\n", encoding="utf-8")
            result = agent_models.map_repo(repo)
        self.assertEqual(
            result["launches"],
            [{"file": "skills/audit/SKILL.md", "launches": ["claude -p", "codex exec"]},
             {"file": "skills/panel/references/protocol.md", "launches": ["agy -p", "claude -p", "codex exec"]}],
        )
        self.assertEqual(
            [(r["file"], r["family"], r["role"], r["how"], r["purpose"]) for r in result["readers"]],
            [("skills/audit/SKILL.md", "*", "reviewer", "/vet", "review"),
             ("skills/audit/route.py", "codex", "reviewer", "resolver", "review")],
        )
        self.assertEqual(result["registry_copy"], "none")

    def test_a_dispatcher_name_is_a_reader_only_where_the_dispatcher_is_not_shipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            consumer = Path(tmp) / "consumer"
            (consumer / "scripts").mkdir(parents=True)
            (consumer / "scripts" / "audit.py").write_text("run dispatch-codex.sh for the audit\n", encoding="utf-8")
            source = Path(tmp) / "source"
            scripts = source / "skills" / "review" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "dispatch-codex.sh").write_text(
                "# sibling of dispatch-claude.sh\nresolve --family codex --role reviewer\n", encoding="utf-8")
            (scripts / "dispatch-claude.sh").write_text(
                "# mirrors dispatch-codex.sh\nresolve --family claude --role reviewer\n", encoding="utf-8")
            (source / "scripts").mkdir()
            (source / "scripts" / "agent_models.py").write_text("dispatch-codex dispatch-gemini\n", encoding="utf-8")
            # A file whose name only starts like a dispatcher does not make the repository its owner.
            (source / "scripts" / "dispatch-task-custom.py").write_text("calls dispatch-task.sh\n", encoding="utf-8")
            self.assertEqual(
                [(r["family"], r["how"]) for r in agent_models.map_repo(consumer)["readers"]],
                [("codex", "dispatch-codex")],
            )
            self.assertEqual(
                [(r["file"].rsplit("/", 1)[-1], r["family"], r["how"]) for r in agent_models.map_repo(source)["readers"]],
                [("dispatch-task-custom.py", "codex", "dispatch-task"),
                 ("dispatch-claude.sh", "claude", "resolver"), ("dispatch-codex.sh", "codex", "resolver")],
            )

    def test_map_reports_whether_a_registry_copy_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "consumer"
            copy_dir = repo / ".agent-config" / "repo" / "user"
            copy_dir.mkdir(parents=True)
            (copy_dir / "agent-models.json").write_bytes(b"{}")
            self.assertEqual(agent_models.map_repo(repo, b"{}")["registry_copy"], "current")
            self.assertEqual(agent_models.map_repo(repo, b"{ }")["registry_copy"], "differs")

    def test_every_role_has_a_purpose(self) -> None:
        self.assertEqual(set(agent_models.PURPOSES), set(agent_models.ROLES))


class ScanTests(unittest.TestCase):
    def hits(self, text: str) -> list:
        return [literal for _, literal, _ in agent_models.scan_text(text)]

    def test_flags_model_and_effort_literals(self) -> None:
        samples = [
            "use gpt-9-test here",
            "use grok-4.7-build here",
            "slug gemini-9-test-high",
            "id claude-sonnet-9 ran",
            "claude -p --model sonnet",
            "claude -p --effort max",
            'model_reasoning_effort = "low"',
            'CODEX_DISPATCH_REASONING="${CODEX_DISPATCH_REASONING:-xhigh}"',
            '"effortLevel": "high"',
            '"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"',
            # The advisor: a Claude alias or a claude-* id, as a key or a flag.
            '"advisorModel": "fable"',
            "advisorModel: 'opus'",
            "advisorModel = sonnet",
            'advisorModel: "claude-3-5-sonnet"',
            "claude --advisor opus",
            "claude --advisor=fable",
            '["claude", "--advisor", "sonnet"]',
            "claude --advisor claude-opus-latest",
        ]
        for sample in samples:
            self.assertTrue(self.hits(sample), sample)

    def test_ignores_prose_without_a_literal(self) -> None:
        for sample in (
            "The registry decides the model and effort.",
            "effort level high is described in the docs",
            "CLAUDE_CODE_EFFORT_LEVEL outranks --effort and /effort.",
            'env "CLAUDE_CODE_EFFORT_LEVEL": "<level>"',
            "The advisorModel key names the advisor; /advisor off turns it off.",
            '"advisorModel": "<model>"',
            "claude --advisor <alias>",
            "an advisor such as opus is consulted",
        ):
            self.assertEqual(self.hits(sample), [], sample)

    def test_an_advisor_literal_is_one_hit_whichever_pattern_names_the_id(self) -> None:
        for sample in (
            '"advisorModel": "fable"',
            'advisorModel: "claude-opus-5"',      # the claude-id pattern already names it
            'advisorModel: "claude-3-5-sonnet"',  # only the advisor pattern does
            "claude --advisor claude-opus-latest",
        ):
            self.assertEqual(len(self.hits(sample)), 1, sample)

    def test_allow_marker_exempts_a_line(self) -> None:
        self.assertEqual(self.hits("a note about gpt-9-test <!-- agent-models: allow -->"), [])
        self.assertEqual(self.hits("# agent-models: allow\nx = 1"), [])
        self.assertEqual(len(self.hits("gpt-9-test\ngpt-9-test <!-- agent-models: allow -->")), 1)

    def test_scan_reports_path_line_and_literal_for_a_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("fine\nuses gpt-9-test\n", encoding="utf-8")
            (root / "b.bin").write_bytes(b"\xff\xfe\x00gpt-9")
            (root / "skip").mkdir()
            (root / "skip" / "__pycache__").mkdir()
            (root / "skip" / "__pycache__" / "c.py").write_text("gpt-9-test\n", encoding="utf-8")
            hits = agent_models.scan([str(root)])
        self.assertEqual([(Path(p).name, n, lit) for p, n, lit, _ in hits], [("a.md", 2, "gpt-9-test")])

    def test_scan_command_exit_code_follows_the_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clean, dirty = Path(tmp) / "clean.md", Path(tmp) / "dirty.md"
            clean.write_text("nothing here\n", encoding="utf-8")
            dirty.write_text("gpt-9-test\n", encoding="utf-8")
            self.assertEqual(run_main(["scan", str(clean)], {})[0], 0)
            code, out, _ = run_main(["scan", str(dirty)], {})
            self.assertEqual(code, 1)
            self.assertIn("dirty.md:1: gpt-9-test", out)


class SyncTests(TempRegistryCase):
    def codex_config(self) -> Path:
        return Path(self.env["CODEX_HOME"]) / "config.toml"

    @needs_toml_parser
    def test_sync_codex_writes_the_interactive_values_and_keeps_the_rest(self) -> None:
        self.codex_config().write_text(
            '# header\nmodel = "old"\nmodel_reasoning_effort = "high"\napproval_policy = "on-request"\n'
            '\n[features]\nfast_mode = false\n',
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, self.env):
            message = agent_models.sync_codex(fixture(), dry_run=False)
        self.assertIn("set", message)
        text = self.codex_config().read_text(encoding="utf-8")
        self.assertIn('model = "gpt-9-test"\n', text)
        self.assertIn('model_reasoning_effort = "low"\n', text)
        self.assertIn('approval_policy = "on-request"\n', text)
        self.assertIn("# header\n", text)
        self.assertTrue(self.codex_config().with_name("config.toml.agent-models.bak").exists())
        with mock.patch.dict(os.environ, self.env):
            self.assertIn("current", agent_models.sync_codex(fixture(), dry_run=False))

    @needs_toml_parser
    def test_sync_codex_dry_run_writes_nothing(self) -> None:
        original = 'model = "old"\n'
        self.codex_config().write_text(original, encoding="utf-8")
        with mock.patch.dict(os.environ, self.env):
            message = agent_models.sync_codex(fixture(), dry_run=True)
        self.assertIn("would set", message)
        self.assertEqual(self.codex_config().read_text(encoding="utf-8"), original)

    @needs_toml_parser
    def test_sync_codex_leaves_the_model_alone_for_another_provider(self) -> None:
        self.codex_config().write_text(
            'model = "vendor-model"\nmodel_provider = "azure"\nmodel_reasoning_effort = "high"\n',
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, self.env):
            agent_models.sync_codex(fixture(), dry_run=False)
        values = agent_models.toml_top_level(self.codex_config().read_text(encoding="utf-8"))
        self.assertEqual(values["model"], "vendor-model")
        self.assertEqual(values["model_reasoning_effort"], "low")

    @needs_toml_parser
    def test_sync_codex_keeps_a_byte_order_mark(self) -> None:
        self.codex_config().write_bytes(b"\xef\xbb\xbf" + b'model = "old"\n')
        with mock.patch.dict(os.environ, self.env):
            agent_models.sync_codex(fixture(), dry_run=False)
        self.assertTrue(self.codex_config().read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_sync_agy_sets_the_newest_listed_display_name(self) -> None:
        listing = {
            "gemini-9-test-high": "Gemini 9 Test (High)",
            "gemini-9.1-test-high": "Gemini 9.1 Test (High)",
            "gemini-8-fast-high": "Gemini 8 Fast (High)",
        }
        settings = Path(self.env["ANTIGRAVITY_SETTINGS_FILE"])
        settings.write_text(json.dumps({"model": "Gemini 8 Fast (High)", "trusted": ["x"]}), encoding="utf-8")
        with mock.patch.dict(os.environ, self.env), mock.patch.object(agent_models, "agy_listing", lambda: listing):
            message = agent_models.sync_agy(fixture(), dry_run=False)
        self.assertIn("Gemini 9.1 Test (High)", message)
        written = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(written["model"], "Gemini 9.1 Test (High)")
        self.assertEqual(written["trusted"], ["x"])

    def test_sync_agy_uses_the_fallback_family_and_skips_when_nothing_is_listed(self) -> None:
        settings = Path(self.env["ANTIGRAVITY_SETTINGS_FILE"])
        settings.write_text(json.dumps({"model": "Other (High)"}), encoding="utf-8")
        listing = {"gemini-8-fast-high": "Gemini 8 Fast (High)"}
        with mock.patch.dict(os.environ, self.env), mock.patch.object(agent_models, "agy_listing", lambda: listing):
            agent_models.sync_agy(fixture(), dry_run=False)
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["model"], "Gemini 8 Fast (High)")
        settings.write_text(json.dumps({"model": "Other (High)"}), encoding="utf-8")
        with mock.patch.dict(os.environ, self.env), mock.patch.object(agent_models, "agy_listing", lambda: None):
            message = agent_models.sync_agy(fixture(), dry_run=False)
        self.assertIn("skipped", message)
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["model"], "Other (High)")

    def test_sync_can_be_switched_off_and_never_fails_on_a_bad_registry(self) -> None:
        code, out, _ = run_main(["sync"], dict(self.env, AGENT_MODELS_SYNC="off"))
        self.assertEqual(code, 0)
        self.assertIn("skipped", out)
        code, _, err = run_main(["sync"], dict(self.env, AGENT_MODELS_FILE=str(self.dir / "absent.json")))
        self.assertEqual(code, 0)
        self.assertIn("sync skipped", err)


class DoctorTests(TempRegistryCase):
    def test_doctor_reports_drift_and_agreement_per_tool(self) -> None:
        claude = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        claude.write_text(json.dumps({
            "model": "fable",
            "env": {"CLAUDE_CODE_EFFORT_LEVEL": "high", "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},
        }), encoding="utf-8")
        (Path(self.env["CODEX_HOME"]) / "config.toml").write_text(
            'model = "gpt-9-test"\nmodel_reasoning_effort = "low"\n', encoding="utf-8"
        )
        Path(self.env["ANTIGRAVITY_SETTINGS_FILE"]).write_text(
            json.dumps({"model": "Gemini 9 Test (High)"}), encoding="utf-8"
        )
        env = dict(self.env)
        env.pop("ANTHROPIC_MODEL", None)
        with mock.patch.dict(os.environ, env):
            for name in ("ANTHROPIC_MODEL", "CLAUDE_CODE_EFFORT_LEVEL"):
                os.environ.pop(name, None)
            checks = agent_models.doctor_checks(fixture())
        by_text = {message: status for status, message in checks}
        drift = [message for message, status in by_text.items() if status == "drift"]
        self.assertEqual(len(drift), 1, drift)
        self.assertIn("env.CLAUDE_CODE_EFFORT_LEVEL='high'", drift[0])
        self.assertTrue(any(s == "ok" and "codex" in m for m, s in by_text.items()))
        self.assertTrue(any(s == "ok" and "agy" in m for m, s in by_text.items()))

    def test_doctor_checks_the_live_advisor_and_honors_the_disable_switch(self) -> None:
        live_path = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        off = {"CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1"}
        # (registry advisor, live settings, process env, status of the advisor line or None)
        cases = (
            ("opus", {"advisorModel": "opus"}, {}, "ok"),
            ("opus", {"advisorModel": "sonnet"}, {}, "drift"),
            ("opus", {}, {}, "drift"),
            (None, {"advisorModel": "sonnet"}, {}, "drift"),   # left behind after --off
            (None, {}, {}, None),
            ("opus", {}, off, "skip"),                         # switch in the process env
            ("opus", {"env": off}, {}, "skip"),                # switch in the settings env block
            (None, {"advisorModel": "sonnet", "env": off}, {}, "skip"),
            # A CLI session writes the settings env entry over the shell value.
            ("opus", {"advisorModel": "sonnet", "env": off},
             {"CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "0"}, "skip"),
            ("opus", {"advisorModel": "opus", "env": {"CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "0"}},
             off, "ok"),
            ("opus", {"advisorModel": "sonnet", "env": {"CLAUDE_CODE_DISABLE_ADVISOR_TOOL": ""}},
             off, "drift"),                                    # an empty entry cancels the export
        )
        for advisor, live, process_env, status in cases:
            with self.subTest(advisor=advisor, live=live, process_env=process_env):
                live_path.write_text(json.dumps(live), encoding="utf-8")
                data = fixture(with_advisor(advisor) if advisor else None)
                with mock.patch.dict(os.environ, dict(self.env, **process_env)):
                    if not process_env:
                        os.environ.pop("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", None)
                    checks = agent_models.doctor_checks(data)
                lines = [(s, m) for s, m in checks if "advisorModel" in m]
                self.assertEqual([s for s, _ in lines], [status] if status else [])

    def test_sync_and_doctor_are_unchanged_with_and_without_grok(self) -> None:
        grok_home = self.dir / "grok-home"
        grok_home.mkdir()
        env = dict(self.env, GROK_HOME=str(grok_home))
        for reg in (fixture(), fixture_with_grok()):
            self.write_registry(reg)
            code, out, _ = run_main(["sync", "--dry-run"], env)
            self.assertEqual(code, 0)
            self.assertNotIn("grok", out.lower())
            self.assertFalse((grok_home / "config.toml").exists())
            with mock.patch.dict(os.environ, env):
                checks = agent_models.doctor_checks(reg)
            self.assertFalse(any("grok" in m.lower() for _, m in checks))

    def test_default_verify_never_launches_grok_and_named_verify_checks_dispatcher_route(self) -> None:
        grok_home = self.dir / "grok-home"
        sid = "sid-verify-grok"
        session_dir = grok_home / "sessions" / "cwd-hash" / sid
        session_dir.mkdir(parents=True)
        (session_dir / "summary.json").write_text(
            json.dumps({"reasoning_effort": "high"}), encoding="utf-8"
        )
        env = dict(self.env, GROK_HOME=str(grok_home))
        for reg in (fixture(), fixture_with_grok()):
            self.write_registry(reg)
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with mock.patch.object(agent_models.shutil, "which", return_value="/usr/bin/fake"), \
                    mock.patch.object(agent_models.subprocess, "run", side_effect=fake_run):
                code, out, _ = run_main(["verify"], env)
            self.assertEqual(code, 0)
            launched = [cmd[0] for cmd, _ in calls]
            self.assertEqual(launched, ["claude", "codex", "agy"])
            self.assertNotIn("grok", out.lower())

        self.write_registry(fixture_with_grok())
        grok_calls = []

        def fake_grok_run(cmd, **kwargs):
            grok_calls.append((cmd, kwargs))
            payload = json.dumps({
                "stopReason": "end_turn",
                "sessionId": sid,
                "modelUsage": {"grok-9-test-build": {"modelCalls": 1}},
            })
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

        with mock.patch.object(agent_models.shutil, "which", return_value="/usr/bin/grok"), \
                mock.patch.object(agent_models.subprocess, "run", side_effect=fake_grok_run):
            code, out, _ = run_main(["verify", "--family", "grok"], env)
        self.assertEqual(code, 0, out)
        self.assertIn("[ok] grok interactive: actual grok-9-test-build high", out)
        self.assertEqual(len(grok_calls), 1)
        cmd, kwargs = grok_calls[0]
        self.assertEqual(
            cmd,
            ["grok", "-p", "Reply OK", "--output-format", "json", "--no-auto-update",
             "-m", "grok-9-test", "--effort", "high"],
        )
        for key, expected in (
            ("GROK_FOLDER_TRUST", "0"),
            ("GROK_CLAUDE_HOOKS_ENABLED", "false"),
            ("GROK_CLAUDE_MCPS_ENABLED", "false"),
            ("GROK_CLAUDE_SKILLS_ENABLED", "false"),
            ("GROK_CURSOR_HOOKS_ENABLED", "false"),
            ("GROK_CURSOR_MCPS_ENABLED", "false"),
            ("GROK_DISABLE_AUTOUPDATER", "1"),
        ):
            self.assertEqual(kwargs["env"].get(key), expected, key)


# --------------------------------------------------------------------------
# Drift guard: no model or effort literal outside the registry
# --------------------------------------------------------------------------

# Paths that may name a concrete model or effort, by prefix or exact path.
DRIFT_ALLOW_PREFIXES = (
    "archive/",
    "legacy/",
    "docs/followups/",
    "tests/",
)
DRIFT_ALLOW_FILES = frozenset({
    "user/agent-models.json",
    "user/settings.json",
    "CHANGELOG.md",
    "scripts/agent_models.py",
    # The maintainer's own Mac notes record what a machine held on one day,
    # not what the repo configures.
    "docs/mac-mouse-preferences.md",
})
DRIFT_ALLOW_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")


def worktree_files() -> list:
    """Tracked files plus untracked, non-ignored files, so a new file is guarded too."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    return sorted(line for line in result.stdout.splitlines() if line.strip())


def drift_allowed(rel: str) -> bool:
    return (
        rel in DRIFT_ALLOW_FILES
        or rel.startswith(DRIFT_ALLOW_PREFIXES)
        or rel.lower().endswith(DRIFT_ALLOW_SUFFIXES)
    )


class ConfigEditSafetyTests(TempRegistryCase):
    """Bootstrap runs sync unattended, so an edit it cannot make safely is skipped."""

    UPDATES = {"model": "M", "model_reasoning_effort": "E"}

    def codex_config(self) -> Path:
        return Path(self.env["CODEX_HOME"]) / "config.toml"

    @needs_toml_parser
    def test_a_header_or_key_inside_a_multiline_string_is_not_edited(self) -> None:
        for delim in ('"""', "'''"):
            with self.subTest(delim=delim):
                text = (f'developer_instructions = {delim}\n[Review policy]\nmodel = "inner"\n{delim}\n'
                        'model = "old"\n\n[features]\nfast_mode = false\n')
                out = agent_models.toml_set_top_level(text, self.UPDATES)
                self.assertIn('[Review policy]\nmodel = "inner"\n', out)
                self.assertEqual(agent_models.toml_top_level(out)["model"], "M")
                self.assertEqual(out.count('model_reasoning_effort = "E"'), 1)
                self.assertLess(out.index('model_reasoning_effort = "E"'), out.index("[features]"))

    @needs_toml_parser
    def test_a_quoted_key_is_rewritten_in_place_and_not_duplicated(self) -> None:
        out = agent_models.toml_set_top_level('"model" = "old"\n[t]\nmodel = "in"\n', self.UPDATES)
        self.assertEqual(out.count('"model" = "M"'), 1)
        self.assertIn('[t]\nmodel = "in"\n', out)
        self.assertEqual(agent_models.toml_top_level('"model_provider" = "ollama"\n')["model_provider"], "ollama")

    def test_without_a_toml_parser_a_change_is_refused_and_the_file_is_untouched(self) -> None:
        # Python before 3.11: the line reader cannot vouch for its own edit
        # (an escaped key or a multi-line array defeats it), so nothing is written.
        cases = (
            'model = "old"\n',
            '"\\u006dodel" = "old"\n',
            'allowed = [\n  [1, 2],\n]\n',
        )
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            for text in cases:
                with self.subTest(text=text):
                    with self.assertRaises(agent_models.TomlUnsupported):
                        agent_models.toml_set_top_level(text, self.UPDATES)
                    self.codex_config().write_text(text, encoding="utf-8")
                    with mock.patch.dict(os.environ, self.env):
                        self.assertIn("skipped", agent_models.sync_codex(fixture(), dry_run=False))
                    self.assertEqual(self.codex_config().read_text(encoding="utf-8"), text)
            same = 'model = "M"\nmodel_reasoning_effort = "E"\n'
            self.assertEqual(agent_models.toml_set_top_level(same, self.UPDATES), same)

    def test_verify_agrees_with_doctor_on_a_fallback_only_account(self) -> None:
        row = agent_models.resolve("agy", "interactive", fixture())
        only_fallback = {"gemini-8-fast-high": "Gemini 8 Fast (High)"}
        with mock.patch.object(agent_models, "agy_listing", lambda: only_fallback):
            self.assertEqual(agent_models.agy_fallback_verdict(row)[0], "ok")
        with mock.patch.object(agent_models, "agy_listing", lambda: dict(only_fallback, **{"gemini-9-test-high": "x"})):
            self.assertEqual(agent_models.agy_fallback_verdict(row)[0], "drift")
        with mock.patch.object(agent_models, "agy_listing", lambda: None):
            self.assertEqual(agent_models.agy_fallback_verdict(row)[0], "skip")

    def test_verify_holds_a_fallback_run_to_the_fallback_tier(self) -> None:
        # A family whose fallback runs at another tier than the row's effort.
        row = agent_models.resolve(
            "agy", "interactive", fixture(lambda d: d["families"]["agy"].update(fallback_effort="low"))
        )
        self.assertEqual((row["effort"], row["fallback_slug"]), ("high", "gemini-8-fast-low"))
        only_fallback = {"gemini-8-fast-low": "Gemini 8 Fast (Low)"}
        with mock.patch.object(agent_models, "agy_listing", lambda: only_fallback):
            self.assertEqual(agent_models.agy_fallback_status(row, "gemini-8-fast-low", "low"), "ok")
            self.assertEqual(agent_models.agy_fallback_status(row, "gemini-8-fast-low", "high"), "mismatch")
        with mock.patch.object(agent_models, "agy_listing", lambda: None):
            self.assertEqual(agent_models.agy_fallback_status(row, "gemini-8-fast-low", "low"), "unknown")
        listed = dict(only_fallback, **{"gemini-9-test-high": "x"})
        with mock.patch.object(agent_models, "agy_listing", lambda: listed):
            self.assertEqual(agent_models.agy_fallback_status(row, "gemini-8-fast-low", "low"), "mismatch")

    def test_unsupported_shapes_raise_and_sync_leaves_the_file_alone(self) -> None:
        for text in ('x = """\n[t]\n', 'model = { a = 1 }\n'):
            with self.subTest(text=text):
                with self.assertRaises(agent_models.TomlUnsupported):
                    agent_models.toml_set_top_level(text, self.UPDATES)
                self.codex_config().write_text(text, encoding="utf-8")
                with mock.patch.dict(os.environ, self.env):
                    message = agent_models.sync_codex(fixture(), dry_run=False)
                self.assertIn("skipped", message)
                self.assertEqual(self.codex_config().read_text(encoding="utf-8"), text)

    @needs_toml_parser
    def test_a_second_sync_keeps_the_original_in_the_backup(self) -> None:
        original = 'model = "old"\nmodel_reasoning_effort = "high"\n'
        self.codex_config().write_text(original, encoding="utf-8")
        with mock.patch.dict(os.environ, self.env):
            agent_models.sync_codex(fixture(), dry_run=False)
            agent_models.sync_codex(fixture(), dry_run=False)
        backup = self.codex_config().with_name("config.toml.agent-models.bak")
        self.assertEqual(backup.read_text(encoding="utf-8"), original)

    @needs_toml_parser
    def test_a_held_lock_skips_and_a_stale_lock_is_taken_over(self) -> None:
        self.codex_config().write_text('model = "old"\n', encoding="utf-8")
        lock = self.codex_config().with_name("config.toml.agent-models.lock")
        lock.write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, self.env):
            self.assertIn("another sync", agent_models.sync_codex(fixture(), dry_run=False))
            self.assertIn('model = "old"', self.codex_config().read_text(encoding="utf-8"))
            stale = lock.stat().st_mtime - agent_models.SYNC_LOCK_STALE_SECONDS - 5
            os.utime(lock, (stale, stale))
            self.assertIn("set", agent_models.sync_codex(fixture(), dry_run=False))
        self.assertFalse(lock.exists())
        self.assertIn('model = "gpt-9-test"', self.codex_config().read_text(encoding="utf-8"))

    @needs_toml_parser
    def test_a_lock_nothing_can_release_is_taken_over(self) -> None:
        # A lock dated in the future, or a dangling symlink in its place, would
        # otherwise make every later sync skip.
        lock = self.codex_config().with_name("config.toml.agent-models.lock")
        for kind in ("future", "symlink"):
            with self.subTest(kind):
                if kind == "symlink" and os.name == "nt":
                    self.skipTest("a symlink needs a privilege on Windows")
                self.codex_config().write_text('model = "old"\n', encoding="utf-8")
                if kind == "future":
                    lock.write_text("", encoding="utf-8")
                    when = lock.stat().st_mtime + agent_models.SYNC_LOCK_STALE_SECONDS + 600
                    os.utime(lock, (when, when))
                else:
                    os.symlink(str(self.dir / "gone"), str(lock))
                    when = os.lstat(str(lock)).st_mtime - agent_models.SYNC_LOCK_STALE_SECONDS - 5
                    os.utime(str(lock), (when, when), follow_symlinks=False)
                with mock.patch.dict(os.environ, self.env):
                    self.assertIn("set", agent_models.sync_codex(fixture(), dry_run=False))
                self.assertFalse(os.path.lexists(str(lock)))

    @needs_toml_parser
    def test_values_already_right_are_current_whatever_the_layout(self) -> None:
        # The line editor cannot place a key below a nested multi-line array,
        # so the reader has to see that nothing needs placing.
        text = ('rules = [\n  ["allow", "git"],\n]\nmodel = "gpt-9-test"  # """\n'
                'model_reasoning_effort = "low"\n[features]\nmodel = "scoped"\n')
        self.assertEqual(agent_models.toml_top_level(text).get("model"), "gpt-9-test")
        self.codex_config().write_text(text, encoding="utf-8")
        with mock.patch.dict(os.environ, self.env):
            self.assertIn("current", agent_models.sync_codex(fixture(), dry_run=False))
        self.assertEqual(self.codex_config().read_text(encoding="utf-8"), text)

    def test_an_unreported_effort_is_unknown_and_a_wrong_model_is_a_mismatch(self) -> None:
        self.assertEqual(agent_models.compare("codex", "a", "medium", "a", None), "unknown")
        self.assertEqual(agent_models.compare("codex", "a", "medium", "b", None), "mismatch")
        self.assertEqual(agent_models.compare("codex", "a", "medium", "a", "high"), "mismatch")
        self.assertEqual(agent_models.compare("codex", "a", "medium", "a", "medium"), "ok")
        # An Agy slug with no version (an operator's override) follows the same three states.
        self.assertEqual(agent_models.compare("agy", "custom-slug", "high", "custom-slug", None), "unknown")
        self.assertEqual(agent_models.compare("agy", "custom-slug", "high", "custom-slug", "low"), "mismatch")
        self.assertEqual(agent_models.compare("agy", "custom-slug", "high", "custom-slug", "high"), "ok")
        header = self.dir / "tail"
        header.write_text("model: gpt-9-test\n", encoding="utf-8")
        code, out, err = run_main(
            ["actual", "--family", "codex", "--stderr-file", str(header),
             "--expect-model", "gpt-9-test", "--expect-effort", "low"], self.env)
        self.assertEqual(code, 0)
        self.assertIn("status=unknown", out)
        self.assertIn("MODEL-UNVERIFIED", err)
        self.assertNotIn("MODEL-MISMATCH", err)

    def test_doctor_accepts_the_fallback_only_when_the_account_lacks_the_primary(self) -> None:
        Path(self.env["ANTIGRAVITY_SETTINGS_FILE"]).write_text(
            json.dumps({"model": "Gemini 8 Fast (High)"}), encoding="utf-8")
        cases = (
            ({"gemini-8-fast-high": "Gemini 8 Fast (High)"}, "ok"),
            ({"gemini-8-fast-high": "x", "gemini-9-test-high": "y"}, "drift"),
            (None, "skip"),
        )
        for listing, expected in cases:
            with self.subTest(expected=expected):
                with mock.patch.dict(os.environ, self.env), \
                        mock.patch.object(agent_models, "agy_listing", lambda listing=listing: listing):
                    checks = agent_models.doctor_checks(fixture())
                self.assertEqual([st for st, msg in checks if msg.startswith("agy ")], [expected])

    def test_compare_treats_an_alias_as_itself_and_checks_the_agy_tier(self) -> None:
        compare = agent_models.compare
        self.assertEqual(compare("claude", "opus", None, "opus", None), "ok")
        self.assertEqual(compare("claude", "opus", None, "claude-opus-9-9", None), "ok")
        self.assertEqual(compare("claude", "opus", None, "claude-sonnet-9-9", None), "mismatch")
        self.assertEqual(compare("agy", "gemini-9-test-high", None, "gemini-9.2-test-high", None), "ok")
        self.assertEqual(compare("agy", "gemini-9-test-high", None, "gemini-9-test-low", None), "mismatch")
        self.assertEqual(compare("agy", "gemini-9-test", "high", "gemini-9-test-high", "high"), "ok")
        self.assertEqual(compare("agy", "custom-model", None, "custom-model", None), "ok")
        self.assertEqual(compare("agy", "custom-model", None, "other-model", None), "mismatch")
        # A report that names the model but no tier does not show the effort.
        self.assertEqual(compare("agy", "gemini-9-test-high", "high", "gemini-9-test", None), "unknown")
        self.assertEqual(compare("agy", "custom-model", "high", "custom-model", None), "unknown")
        self.assertEqual(compare("agy", "gemini-9-test-high", "high", "gemini-8-fast", None), "mismatch")

    def test_scan_sees_argument_arrays_and_the_model_variable(self) -> None:
        text = '["--model", "opus"]\n\'--effort\', \'high\'\nANTHROPIC_MODEL=fable\n--model <alias>\n'
        self.assertEqual(len([hit for hit in agent_models.scan_text(text)]), 3)

    def test_scan_sees_a_lowercase_effort_default_but_not_prose(self) -> None:
        hits = [literal for _, literal, _ in agent_models.scan_text(
            'effort = "high"\n"effort": "medium"\nreview_effort: \'low\'\nthe effort was high\n')]
        self.assertEqual(len(hits), 3)


class DriftGuardTests(unittest.TestCase):
    def test_allowlist_matches_by_prefix_exact_path_and_image_suffix(self) -> None:
        self.assertTrue(drift_allowed("archive/plans/x.md"))
        self.assertTrue(drift_allowed("tests/test_anything.py"))
        self.assertTrue(drift_allowed("assets/banner.png"))
        self.assertTrue(drift_allowed("user/settings.json"))
        self.assertFalse(drift_allowed("skills/prun/SKILL.md"))
        self.assertFalse(drift_allowed("README.md"))
        self.assertFalse(drift_allowed("scripts/agent-quota.py"))

    def test_no_model_or_effort_literal_outside_the_allowlist(self) -> None:
        files = worktree_files()
        if not files:
            self.skipTest("git file list unavailable")
        paths = []
        for rel in files:
            if drift_allowed(rel):
                continue
            path = ROOT / rel
            if path.is_file():
                paths.append(str(path))
        hits = []
        for path, number, literal, line in agent_models.scan(paths):
            rel = Path(path).relative_to(ROOT).as_posix()
            hits.append(f"{rel}:{number}: {literal}    {line[:100]}")
        self.assertEqual(
            hits, [],
            "model or effort literals outside user/agent-models.json (read them from the registry, or "
            "mark a history line with `agent-models: allow`):\n" + "\n".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
