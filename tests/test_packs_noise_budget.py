"""Tests for scripts/packs/noise_budget.py (v0.7.0 noise-audit gate).

Verifies the five cases the v0.7.0 plan calls out:

  (a) third-party deny-no-hint -> warning
  (b) same entry with reroute_hint -> silent
  (c) consumer noise-audit-override: accept-deny -> silent
  (d) host-mismatched noisy hook -> filtered before counting
  (e) BC: legacy manifest without reroute_hint field -> parses + composes
       unchanged (the BC half lives in test_packs_schema.py; here we just
       confirm the gate handles absent field as "no reroute")

No first-party hook fixture per plan-review Round 3 finding 2: first-party
guard.py lives outside the pack manifest in v0.7.0; the maintainer invariant
ships with the v1.0 guard.py extraction.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs.noise_budget import (  # noqa: E402
    NoiseWarning,
    evaluate_noise_budget,
    render_warnings_block,
)


def _make_pack(
    name: str,
    *,
    decision: str = "deny",
    fp_risk: str = "high",
    impact: str = "low",
    reroute_hint: str | None = None,
    hosts: list[str] | None = None,
    kind: str = "hook",
    files_to: str = "/etc/x.hook",
) -> tuple[str, dict]:
    """Build a (pack_name, pack_def) tuple in the shape evaluate_noise_budget
    expects. Knobs default to a noisy configuration; callers tweak individual
    fields for negative cases."""
    entry = {
        "kind": kind,
        "decision": decision,
        "false-positive-risk": fp_risk,
        "impact-if-allowed": impact,
        "files": [{"from": "hooks/x.py", "to": files_to}],
    }
    if reroute_hint is not None:
        entry["reroute_hint"] = reroute_hint
    pack_def = {
        "hosts": hosts if hosts is not None else ["claude-code"],
        "active": [entry],
    }
    return (name, pack_def)


class NoiseBudgetCoreTests(unittest.TestCase):
    """Plan A.6 cases (a)-(e)."""

    def test_a_deny_without_reroute_warns(self) -> None:
        pack = _make_pack("third-party-noisy")
        warnings = evaluate_noise_budget([pack], None, "claude-code")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].pack_name, "third-party-noisy")
        self.assertIn("missing reroute_hint", warnings[0].reason)

    def test_b_reroute_hint_silences(self) -> None:
        pack = _make_pack("third-party-fixed", reroute_hint="use git -C <path> <cmd>")
        warnings = evaluate_noise_budget([pack], None, "claude-code")
        self.assertEqual(warnings, [])

    def test_b_empty_string_reroute_still_noisy(self) -> None:
        # The plan explicitly says empty / absent / literal "none" all mean
        # "no reroute"; only a non-empty meaningful string silences.
        pack = _make_pack("third-party-emptyhint", reroute_hint="")
        warnings = evaluate_noise_budget([pack], None, "claude-code")
        self.assertEqual(len(warnings), 1)

    def test_b_literal_none_string_still_noisy(self) -> None:
        pack = _make_pack("third-party-nonehint", reroute_hint="none")
        warnings = evaluate_noise_budget([pack], None, "claude-code")
        self.assertEqual(len(warnings), 1)

    def test_c_consumer_override_silences(self) -> None:
        pack = _make_pack("third-party-noisy")
        warnings = evaluate_noise_budget(
            [pack],
            {"third-party-noisy": "accept-deny"},
            "claude-code",
        )
        self.assertEqual(warnings, [])

    def test_c_unknown_override_value_does_not_silence(self) -> None:
        # Defense: only the literal "accept-deny" silences; any other value
        # is a typo / misread and the warning persists. Prevents a permissive
        # default from silently masking real noisy packs.
        pack = _make_pack("third-party-noisy")
        warnings = evaluate_noise_budget(
            [pack], {"third-party-noisy": "yes"}, "claude-code"
        )
        self.assertEqual(len(warnings), 1)

    def test_c_override_targets_specific_pack_only(self) -> None:
        pack_a = _make_pack("noisy-a")
        pack_b = _make_pack("noisy-b")
        warnings = evaluate_noise_budget(
            [pack_a, pack_b],
            {"noisy-a": "accept-deny"},
            "claude-code",
        )
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].pack_name, "noisy-b")

    def test_d_host_mismatch_filters_before_counting(self) -> None:
        # A claude-code-only noisy hook does NOT warn under --host codex.
        pack = _make_pack("third-party-noisy", hosts=["claude-code"])
        warnings = evaluate_noise_budget([pack], None, "codex")
        self.assertEqual(warnings, [])

    def test_d_entry_level_hosts_override_pack_hosts(self) -> None:
        # Entry-level hosts wins over pack-level (schema invariant). A
        # pack default of [claude-code] with an entry-level [codex] is
        # filtered out under host=claude-code.
        entry = {
            "kind": "hook",
            "decision": "deny",
            "false-positive-risk": "high",
            "impact-if-allowed": "low",
            "hosts": ["codex"],  # entry-level override
            "files": [{"to": "/etc/x.hook"}],
        }
        pack_def = {"hosts": ["claude-code"], "active": [entry]}
        warnings = evaluate_noise_budget(
            [("entry-override", pack_def)], None, "claude-code"
        )
        self.assertEqual(warnings, [])

    def test_e_legacy_manifest_without_reroute_hint_parses(self) -> None:
        # Absent reroute_hint is the canonical "no reroute" case; this test
        # documents that legacy manifests (pre-v0.7.0) trip the gate by
        # default, which is the intended behavior — third-party pack authors
        # opt into the silence by adding the field.
        pack = _make_pack("legacy-pack")  # no reroute_hint set
        warnings = evaluate_noise_budget([pack], None, "claude-code")
        self.assertEqual(len(warnings), 1)


class NoiseBudgetThresholdEdgesTests(unittest.TestCase):
    """One axis at a time: each criterion field must trip for a warning to
    fire. Off-axis values silence the gate without involving reroute_hint."""

    def test_decision_ask_not_counted(self) -> None:
        pack = _make_pack("ask-pack", decision="ask")
        warnings = evaluate_noise_budget([pack], None, "claude-code")
        self.assertEqual(warnings, [])

    def test_decision_allow_not_counted(self) -> None:
        pack = _make_pack("allow-pack", decision="allow")
        warnings = evaluate_noise_budget([pack], None, "claude-code")
        self.assertEqual(warnings, [])

    def test_low_fp_risk_not_counted(self) -> None:
        pack = _make_pack("lowfp-pack", fp_risk="low")
        warnings = evaluate_noise_budget([pack], None, "claude-code")
        self.assertEqual(warnings, [])

    def test_high_impact_not_counted(self) -> None:
        # impact-if-allowed: high is by definition a real-stakes guard;
        # the noise-audit framework excludes it from the noise class.
        pack = _make_pack("highimpact-pack", impact="high")
        warnings = evaluate_noise_budget([pack], None, "claude-code")
        self.assertEqual(warnings, [])

    def test_non_hook_kind_skipped(self) -> None:
        # kind: skill / permission / command never contribute to noise.
        pack = _make_pack("skill-pack", kind="skill")
        warnings = evaluate_noise_budget([pack], None, "claude-code")
        self.assertEqual(warnings, [])

    def test_multiple_active_entries_count_independently(self) -> None:
        # A pack with two noisy hooks yields two warnings.
        entry_a = {
            "kind": "hook",
            "decision": "deny",
            "false-positive-risk": "high",
            "impact-if-allowed": "low",
            "files": [{"to": "/etc/a.hook"}],
        }
        entry_b = {
            "kind": "hook",
            "decision": "deny",
            "false-positive-risk": "high",
            "impact-if-allowed": "medium",
            "files": [{"to": "/etc/b.hook"}],
        }
        pack_def = {"hosts": ["claude-code"], "active": [entry_a, entry_b]}
        warnings = evaluate_noise_budget(
            [("two-hooks", pack_def)], None, "claude-code"
        )
        self.assertEqual(len(warnings), 2)
        self.assertEqual({w.files_to for w in warnings}, {"/etc/a.hook", "/etc/b.hook"})


class NoiseBudgetRenderTests(unittest.TestCase):
    """Output formatting: render_warnings_block produces a stable
    human-readable summary."""

    def test_empty_warnings_render_empty(self) -> None:
        self.assertEqual(render_warnings_block([]), "")

    def test_warning_renders_includes_pack_name_and_path(self) -> None:
        w = NoiseWarning(
            pack_name="example",
            entry_index=2,
            files_to="/etc/my.hook",
            reason="reason text",
        )
        block = render_warnings_block([w])
        self.assertIn("example", block)
        self.assertIn("active[2]", block)
        self.assertIn("/etc/my.hook", block)
        self.assertIn("reroute_hint", block)
        self.assertIn("noise-audit-override", block)


if __name__ == "__main__":
    unittest.main()
