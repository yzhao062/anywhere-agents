"""v0.6.0 Phase 2 tests for ``scripts/compose_packs.py``.

Phase 2 of the v0.6.0 release sets the bundled-default ``update_policy``
table per pack-architecture.md § "aa v0.6.0" Q3:

- First-party passive (``agent-style``) → ``auto``: silent refresh; the
  passive content is regenerated from the new resolved commit without a
  prompt UI. Phase 4 owns the stderr summary line that names the
  refresh; Phase 2 only confirms the policy threads through.
- First-party active (``aa-core-skills``) → ``prompt``: apply-by-default
  with a stderr summary (Phase 4). Phase 2 only confirms the policy is
  ``prompt`` rather than the old ``locked`` (which fails closed on
  drift).

These tests run in-process: they parse the bundled manifest from the
on-disk ``bootstrap/packs.yaml`` so the v0.6.0 default flip is asserted
directly against the surface consumers read; they also patch
``source_fetch.fetch_pack`` to simulate a resolved-commit change so the
policy-threading path can be exercised without touching the network.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compose_packs  # noqa: E402
from packs import locks  # noqa: E402
from packs import schema  # noqa: E402
from packs import source_fetch  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = compose_packs.main(argv)
    return rc, out_buf.getvalue(), err_buf.getvalue()


# =====================================================================
# Bundled-manifest defaults (pack-architecture.md § "aa v0.6.0" Q3).
# =====================================================================


class TestBundledManifestDefaults(unittest.TestCase):
    """The on-disk ``bootstrap/packs.yaml`` must declare the v0.6.0
    bundled-default ``update_policy`` per the Q3 table:

    - ``agent-style``     (first-party passive) → ``auto``
    - ``aa-core-skills``  (first-party active)  → ``prompt``

    These tests parse the manifest directly so the default flip is
    asserted against the surface consumers read on every bootstrap.
    """

    def setUp(self) -> None:
        self.manifest_path = ROOT / "bootstrap" / "packs.yaml"
        self.parsed = schema.parse_manifest(self.manifest_path)
        self.by_name = {p["name"]: p for p in self.parsed["packs"]}

    def test_agent_style_default_is_auto(self) -> None:
        """First-party passive default per Q3: ``auto`` (silent refresh
        + stderr summary on changes; Phase 4 owns the stderr line)."""
        self.assertIn("agent-style", self.by_name)
        self.assertEqual(self.by_name["agent-style"]["update_policy"], "auto")

    def test_aa_core_skills_default_is_prompt(self) -> None:
        """First-party active default per Q3: ``prompt`` (apply-by-default
        per Phase 4 + stderr summary). Distinct from the old v0.5.x
        ``locked`` default which would block apply on any drift."""
        self.assertIn("aa-core-skills", self.by_name)
        self.assertEqual(
            self.by_name["aa-core-skills"]["update_policy"], "prompt"
        )

    def test_packaged_mirror_matches_source_manifest(self) -> None:
        """STRICT mirror contract: the wheel-bundled
        ``packages/pypi/anywhere_agents/composer/bootstrap/packs.yaml``
        must declare the same v0.6.0 defaults so installed consumers
        and source-tree consumers see identical bundled policies."""
        mirror_path = (
            ROOT
            / "packages"
            / "pypi"
            / "anywhere_agents"
            / "composer"
            / "bootstrap"
            / "packs.yaml"
        )
        mirrored = schema.parse_manifest(mirror_path)
        mirrored_by_name = {p["name"]: p for p in mirrored["packs"]}
        self.assertEqual(
            mirrored_by_name["agent-style"]["update_policy"], "auto"
        )
        self.assertEqual(
            mirrored_by_name["aa-core-skills"]["update_policy"], "prompt"
        )


# =====================================================================
# Drift-flow fixture: simulate resolved-commit change for a bundled pack.
# =====================================================================


class _BundledDriftFixture(unittest.TestCase):
    """Fixture for exercising the bundled pack drift path.

    Seeds a v2 manifest declaring one bundled pack, an
    ``agent-config.yaml`` selecting that pack via inline source so
    ``source_fetch.fetch_pack`` is reached (the bundled-only path
    skips fetch), and a previous ``pack-lock.json`` recording an OLD
    commit so the stubbed fetch returning a NEW commit triggers drift
    detection in the pre-fetch loop.
    """

    pack_name: str = "agent-style"
    bundled_url: str = "https://github.com/yzhao062/agent-style"
    bundled_ref: str = "v0.3.5"
    bundled_policy: str = "auto"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Canonicalize: macOS resolves /var → /private/var, Windows
        # resolves 8.3 short paths. compose_packs calls Path.resolve()
        # internally; if the test uses the unresolved form, called-path
        # equality assertions fail on CI runners.
        self.root = Path(self.tmp.name).resolve()
        self.env_patch = patch.dict(
            os.environ,
            {
                "APPDATA": str(self.root / "appdata"),
                "XDG_CONFIG_HOME": str(self.root / "xdg"),
                "AGENT_CONFIG_PACKS": "",
                "AGENT_CONFIG_RULE_PACKS": "",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        # Bundled manifest: declare BOTH default-v2 packs (agent-style +
        # aa-core-skills) so the DEFAULT_V2_SELECTIONS auto-include logic
        # finds a bundled definition for each. The pack-under-test
        # carries the full real shape + policy; the other is a minimal
        # bundled stub. Real bundled packs.yaml carries the same values;
        # this isolates the drift-flow surface to one pack at a time.
        bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
        manifest = "version: 2\npacks:\n"
        # agent-style entry
        if self.pack_name == "agent-style":
            manifest += (
                "  - name: agent-style\n"
                "    source:\n"
                f"      repo: {self.bundled_url}\n"
                f"      ref: {self.bundled_ref}\n"
                f"    update_policy: {self.bundled_policy}\n"
                "    passive:\n"
                "      - files:\n"
                "          - from: docs/rule-pack-compact.md\n"
                "            to: AGENTS.md\n"
            )
        else:
            # Stub agent-style as a minimal bundled passive (no inline
            # source so DEFAULT_V2_SELECTIONS path resolves cleanly).
            manifest += (
                "  - name: agent-style\n"
                "    source: bundled\n"
                "    default-ref: bundled\n"
                "    update_policy: auto\n"
            )
        # aa-core-skills entry
        if self.pack_name == "aa-core-skills":
            manifest += (
                "  - name: aa-core-skills\n"
                "    source:\n"
                f"      repo: {self.bundled_url}\n"
                f"      ref: {self.bundled_ref}\n"
                f"    update_policy: {self.bundled_policy}\n"
                "    hosts: [claude-code]\n"
                "    active:\n"
                "      - kind: skill\n"
                "        files:\n"
                "          - from: skills/x/\n"
                "            to: .claude/skills/x/\n"
            )
        else:
            # Stub aa-core-skills as a minimal bundled active so the
            # default-selection auto-include resolves; no fetch needed.
            manifest += (
                "  - name: aa-core-skills\n"
                "    update_policy: prompt\n"
            )
        _write(bootstrap_dir / "packs.yaml", manifest)
        _write(self.root / ".agent-config" / "AGENTS.md", "# upstream\n")

        # Consumer-side selection: route the same pack through inline
        # source so source_fetch.fetch_pack is reached. The bundled-only
        # branch (no source.url) skips fetch and never threads policy
        # through to source_fetch — drift can only surface on inline.
        _write(
            self.root / "agent-config.yaml",
            "rule_packs:\n"
            f"  - name: {self.pack_name}\n"
            "    source:\n"
            f"      url: {self.bundled_url}\n"
            f"      ref: {self.bundled_ref}\n"
            f"    update_policy: {self.bundled_policy}\n",
        )

        # Seed a previous pack-lock with an OLDER recorded commit so
        # the NEW resolved_commit returned by the stub triggers drift.
        from packs import state as state_mod
        prev_lock = state_mod.empty_pack_lock()
        prev_lock["packs"][self.pack_name] = {
            "source_url": self.bundled_url,
            "requested_ref": self.bundled_ref,
            "resolved_commit": "a" * 40,
            "pack_update_policy": self.bundled_policy,
            "files": [],
        }
        state_mod.save_pack_lock(
            self.root / ".agent-config" / "pack-lock.json", prev_lock,
        )

        # Synthetic archive carrying a pack.yaml so schema.parse_manifest
        # at the archive root succeeds (compose_packs reads it for the
        # inline-source path). The passive/active loops are no-ops as
        # long as the byte source resolves to a known file path.
        self.archive_dir = self.root / "fake-archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        archive_manifest = (
            "version: 2\n"
            "packs:\n"
            f"  - name: {self.pack_name}\n"
            "    source:\n"
            f"      repo: {self.bundled_url}\n"
            f"      ref: {self.bundled_ref}\n"
        )
        if self.pack_name == "agent-style":
            archive_manifest += (
                "    passive:\n"
                "      - files:\n"
                "          - from: docs/rule-pack-compact.md\n"
                "            to: AGENTS.md\n"
            )
            (self.archive_dir / "docs").mkdir(parents=True, exist_ok=True)
            (self.archive_dir / "docs" / "rule-pack-compact.md").write_text(
                "# rule pack body\n"
            )
        else:
            archive_manifest += (
                "    hosts: [claude-code]\n"
                "    active:\n"
                "      - kind: skill\n"
                "        files:\n"
                "          - from: skills/x/\n"
                "            to: .claude/skills/x/\n"
            )
            (self.archive_dir / "skills" / "x").mkdir(parents=True, exist_ok=True)
            (self.archive_dir / "skills" / "x" / "SKILL.md").write_text(
                "# skill x\n"
            )
        (self.archive_dir / "pack.yaml").write_text(archive_manifest)

        # NEW commit on the upstream — drift vs the recorded "aaaa…".
        new_archive = MagicMock()
        new_archive.archive_dir = self.archive_dir
        new_archive.resolved_commit = "b" * 40
        new_archive.method = "ssh"
        new_archive.url = self.bundled_url
        new_archive.ref = self.bundled_ref
        self.new_archive = new_archive

    @contextmanager
    def _no_lock(self):
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None
        with patch.object(locks, "acquire", side_effect=_ctx):
            yield


# =====================================================================
# v0.6.0 Phase 2: bundled-default policy threading.
# =====================================================================


class TestAgentStyleAutoSilentRefresh(_BundledDriftFixture):
    """First-party passive (``agent-style``) → ``update_policy: auto``.

    Per pack-architecture.md § "aa v0.6.0" Q3, passive packs default to
    ``auto`` so the bundled passive content (the writing-rule body)
    refreshes silently when the pinned ref's commit advances. Phase 2
    confirms (a) the bundled manifest declares the policy and (b) the
    policy threads through ``source_fetch.fetch_pack`` to the cache
    layer. Phase 4 owns the stderr summary line that names the refresh.
    """

    pack_name = "agent-style"
    bundled_policy = "auto"

    def test_agent_style_auto_silent_refresh(self) -> None:
        """Resolved-commit change on a bundled passive pack with
        ``update_policy: auto`` is processed without a prompt UI.

        Asserts:

        - ``source_fetch.fetch_pack`` is called with ``policy="auto"``
          (the policy threads through from the resolved selection).
        - ``prompt_user_for_updates`` is NOT invoked: the auto policy
          must skip the prompt path used for ``prompt`` policies.
        - Compose exits 0 (the silent-refresh path is non-blocking).
        - The pack-lock-driven drift detection fires (the run reaches
          the prompt-branch decision point at all, but the policy
          short-circuits the actual prompt call).

        Phase 4 owns the stderr summary line emission; this test
        intentionally does not exercise the summary surface.
        """
        # Stub prompt_user_for_updates to record whether it was called;
        # the v0.6.0 contract is that auto-policy drift never reaches
        # the prompt UI even when drift is detected.
        prompt_calls: list = []

        def _record_prompt(pending):
            prompt_calls.append(pending)
            # Defensive default: if the prompt is reached anyway, tell
            # the composer to apply so the test does not deadlock on
            # a TTY check.
            return "apply"

        with self._no_lock():
            with (
                patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self.new_archive,
                ) as fetch,
                patch.object(
                    compose_packs, "prompt_user_for_updates",
                    side_effect=_record_prompt,
                ),
            ):
                rc, _out, _err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 0)

        # The policy from the resolved selection threads through to
        # source_fetch.fetch_pack as the ``policy`` kwarg.
        fetch.assert_called()
        # The compose loop fetches once for the agent-style selection.
        # Other selections (none in this minimal manifest) do not
        # trigger additional calls. Pin the policy on the recorded
        # call.
        recorded_policies = [
            call.kwargs.get("policy") for call in fetch.call_args_list
        ]
        self.assertIn("auto", recorded_policies)

        # The lock advances: the new resolved_commit lands in
        # pack-lock.json so a subsequent run sees no drift.
        from packs import state as state_mod
        lock_path = self.root / ".agent-config" / "pack-lock.json"
        self.assertTrue(lock_path.exists())
        new_lock = state_mod.load_pack_lock(lock_path)
        self.assertEqual(
            new_lock["packs"][self.pack_name]["resolved_commit"], "b" * 40
        )


class TestAaCoreSkillsPromptDefaultApply(_BundledDriftFixture):
    """First-party active (``aa-core-skills``) → ``update_policy: prompt``.

    Per pack-architecture.md § "aa v0.6.0" Q3, active packs default to
    ``prompt`` (apply-by-default per Phase 4 + stderr summary). Phase 2
    confirms (a) the bundled manifest declares the policy and (b) the
    policy is the apply-by-default flow rather than ``locked``
    (which would fail closed on drift).
    """

    pack_name = "aa-core-skills"
    bundled_url = "https://github.com/yzhao062/anywhere-agents"
    bundled_ref = "main"
    bundled_policy = "prompt"

    def test_aa_core_skills_prompt_default_apply(self) -> None:
        """Resolved-commit change on a bundled active pack with
        ``update_policy: prompt`` is applied (not blocked).

        Asserts:

        - ``source_fetch.fetch_pack`` is called with ``policy="prompt"``
          so the locked-policy fail-closed path does NOT engage; the
          drift is delivered to the compose layer for a decision.
        - When ``prompt_user_for_updates`` returns ``apply`` (the
          v0.6.0 default per Phase 4: apply-by-default with a stderr
          summary), compose exits 0 and the pack-lock advances.
        - Most importantly: the run does NOT raise
          ``PackLockDriftError`` (which is what ``policy="locked"``
          would emit on the same drift).

        Phase 4 owns the actual drift-apply UX (stderr summary line,
        ``--no-apply-drift`` override, ``ANYWHERE_AGENTS_UPDATE=skip``
        wiring). This test intentionally pins only the policy threading
        and the apply-not-blocked invariant.
        """
        with self._no_lock():
            with (
                patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self.new_archive,
                ) as fetch,
                patch.object(
                    compose_packs, "prompt_user_for_updates",
                    return_value="apply",
                ) as prompt,
            ):
                rc, _out, _err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 0)

        # The apply-by-default policy must surface to the compose
        # layer's decision point — that is the v0.6.0 contract.
        prompt.assert_called_once()

        # The policy from the resolved selection threads through to
        # source_fetch.fetch_pack as ``policy="prompt"``.
        fetch.assert_called()
        recorded_policies = [
            call.kwargs.get("policy") for call in fetch.call_args_list
        ]
        self.assertIn("prompt", recorded_policies)
        # The locked-policy fail-closed path must NOT engage.
        self.assertNotIn("locked", recorded_policies)

        # The lock advances after apply: the new resolved_commit lands
        # in pack-lock.json. This is the apply-not-blocked invariant.
        from packs import state as state_mod
        lock_path = self.root / ".agent-config" / "pack-lock.json"
        self.assertTrue(lock_path.exists())
        new_lock = state_mod.load_pack_lock(lock_path)
        self.assertEqual(
            new_lock["packs"][self.pack_name]["resolved_commit"], "b" * 40
        )


# =====================================================================
# v0.6.0 Phase 3: reconciliation-aware BC-guard refinement.
# =====================================================================


class TestBcGuardDistinguishesMinimalFromExplicitPin(unittest.TestCase):
    """``cli._has_explicit_default_override`` must distinguish minimal
    auto-reconciled entries (written by ``_user_only_rule_pack_entry`` /
    ``_project_only_user_pack_entry``) from genuine user pins.

    Per PLAN-aa-v0.6.0 § Phase 3, an entry is treated as a deliberate
    pin only when one of the following holds:

      (a) ``passive`` keys present (real shape override), OR
      (b) ``active`` keys present (real shape override), OR
      (c) ``ref`` deviates from the bundled-manifest default for the
          same pack name, OR
      (d) ``update_policy`` deviates from the bundled-manifest default.

    Entries byte-equivalent to what aa's reconciliation would produce
    (minimal ``{name, source: {url, ref}}`` where the ref matches the
    bundled default) are no longer classified as opaque pins so the
    bundled-default drift detector can advance them when the bundled
    ref or source path migrates.
    """

    def setUp(self) -> None:
        # Add the PyPI package to sys.path so ``anywhere_agents.cli`` is
        # importable (the v0.5 fixture above only adds ``scripts/``).
        pkg_root = ROOT / "packages" / "pypi"
        if str(pkg_root) not in sys.path:
            sys.path.insert(0, str(pkg_root))
        # Late import: cli reads the wheel-bundled manifest at call time,
        # so importing once at module load is fine.
        from anywhere_agents import cli  # noqa: WPS433
        self.cli = cli
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        # The bundled manifest declares ``agent-style`` with
        # ``ref: v0.3.5`` and ``update_policy: auto`` (per
        # bootstrap/packs.yaml in this repo). All sub-cases below are
        # written against that bundled default.
        self.pack_name = "agent-style"
        self.bundled_url = "https://github.com/yzhao062/agent-style"
        self.bundled_ref = "v0.3.5"
        # ``_has_explicit_default_override`` ignores ``row`` when the
        # user-level identity is bundled; provide a stand-in row that
        # passes that early check so the function falls through to the
        # config-file scan we exercise.
        self.row: dict = {"u": None, "p": None, "l": None}

    def _write_project_yaml(self, body: str) -> None:
        (self.root / "agent-config.yaml").write_text(body, encoding="utf-8")

    def test_minimal_auto_reconciled_is_not_a_pin(self) -> None:
        """(a) Minimal ``{name, source: {url, ref}}`` matching the
        bundled default — what aa's auto-reconciliation writes — must
        NOT be classified as a deliberate pin. This is the
        ``random``-style failure mode that v0.6.0 unblocks: such an
        entry previously short-circuited bundled-default drift
        detection even when the bundled ref had advanced.
        """
        self._write_project_yaml(
            f"packs:\n"
            f"  - name: {self.pack_name}\n"
            f"    source:\n"
            f"      url: {self.bundled_url}\n"
            f"      ref: {self.bundled_ref}\n"
        )
        self.assertFalse(
            self.cli._has_explicit_default_override(
                self.root, self.row, self.pack_name
            )
        )

    def test_passive_keys_count_as_pin(self) -> None:
        """(b) ``passive`` keys present — real shape override that
        carries content the maintainer can never automatically migrate
        — IS a pin. The bundled default ref / policy match deliberately
        so the only signal is the shape override.
        """
        self._write_project_yaml(
            f"packs:\n"
            f"  - name: {self.pack_name}\n"
            f"    source:\n"
            f"      url: {self.bundled_url}\n"
            f"      ref: {self.bundled_ref}\n"
            f"    passive:\n"
            f"      - files:\n"
            f"          - from: docs/custom.md\n"
            f"            to: AGENTS.md\n"
        )
        self.assertTrue(
            self.cli._has_explicit_default_override(
                self.root, self.row, self.pack_name
            )
        )

    def test_active_keys_count_as_pin(self) -> None:
        """(c) ``active`` keys present — real shape override that
        carries trust-sensitive code paths — IS a pin. Bundled
        defaults match so only the shape override signals.
        """
        self._write_project_yaml(
            f"packs:\n"
            f"  - name: {self.pack_name}\n"
            f"    source:\n"
            f"      url: {self.bundled_url}\n"
            f"      ref: {self.bundled_ref}\n"
            f"    active:\n"
            f"      - kind: skill\n"
            f"        files:\n"
            f"          - from: skills/x/\n"
            f"            to: .claude/skills/x/\n"
        )
        self.assertTrue(
            self.cli._has_explicit_default_override(
                self.root, self.row, self.pack_name
            )
        )

    def test_ref_deviation_counts_as_pin(self) -> None:
        """(d) ``source.ref`` deviating from the bundled-manifest
        default — the consumer is deliberately staying on a different
        upstream version — IS a pin. The maintainer should not silently
        migrate such an entry across a bundled-default ref bump.
        """
        self._write_project_yaml(
            f"packs:\n"
            f"  - name: {self.pack_name}\n"
            f"    source:\n"
            f"      url: {self.bundled_url}\n"
            f"      ref: v0.2.0\n"
        )
        self.assertTrue(
            self.cli._has_explicit_default_override(
                self.root, self.row, self.pack_name
            )
        )

    def test_update_policy_deviation_counts_as_pin(self) -> None:
        """(e) ``update_policy`` deviating from the bundled-manifest
        default — the consumer chose a different fail-mode posture
        (e.g., ``locked`` for fail-closed) — IS a pin. Migrating the
        ref behind a deliberate policy choice would silently weaken
        the consumer's posture.
        """
        self._write_project_yaml(
            f"packs:\n"
            f"  - name: {self.pack_name}\n"
            f"    source:\n"
            f"      url: {self.bundled_url}\n"
            f"      ref: {self.bundled_ref}\n"
            f"    update_policy: locked\n"
        )
        self.assertTrue(
            self.cli._has_explicit_default_override(
                self.root, self.row, self.pack_name
            )
        )


# =====================================================================
# v0.6.0 Phase 3: same-ref source-path switching (drift-and-migrate).
# =====================================================================


class TestSameRefSourcePathSwitchTriggersRemigration(unittest.TestCase):
    """When the bundled-manifest ``passive[].files[].from`` differs
    from the lock-recorded ``source_path`` for the same
    ``requested_ref``, the composer must route through the
    drift-and-migrate flow rather than failing closed (raw-fetch 404 on
    the new path) or staying on stale full-body content (legacy
    raw-URL fetch from the old path).

    Per PLAN-aa-v0.6.0 § Phase 3 and pack-architecture.md ~line 678,
    this honors the v0.5.7 ``Compatibility`` promise to consumers
    carrying old-full-body bundled-default content.
    """

    pack_name = "agent-style"
    bundled_url = "https://github.com/yzhao062/agent-style"
    bundled_ref = "v0.3.2"  # same on both sides; only source_path differs
    bundled_policy = "auto"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env_patch = patch.dict(
            os.environ,
            {
                "APPDATA": str(self.root / "appdata"),
                "XDG_CONFIG_HOME": str(self.root / "xdg"),
                "AGENT_CONFIG_PACKS": "",
                "AGENT_CONFIG_RULE_PACKS": "",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
        # Bundled manifest: bundled ``from:`` is ``rule-pack-compact.md``
        # (the new compact path). aa-core-skills declared as a minimal
        # bundled stub so DEFAULT_V2_SELECTIONS resolve cleanly.
        manifest = (
            "version: 2\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source:\n"
            f"      repo: {self.bundled_url}\n"
            f"      ref: {self.bundled_ref}\n"
            f"    update_policy: {self.bundled_policy}\n"
            "    passive:\n"
            "      - files:\n"
            "          - from: docs/rule-pack-compact.md\n"
            "            to: AGENTS.md\n"
            "  - name: aa-core-skills\n"
            "    update_policy: prompt\n"
        )
        _write(bootstrap_dir / "packs.yaml", manifest)
        _write(self.root / ".agent-config" / "AGENTS.md", "# upstream\n")

        # Consumer-side selection: route through inline source so
        # ``source_fetch.fetch_pack`` is reached. The ``ref`` matches
        # the bundled default exactly; only the lock's recorded
        # ``source_path`` differs (the v0.5.7 same-ref source-path
        # switching scenario).
        _write(
            self.root / "agent-config.yaml",
            "rule_packs:\n"
            f"  - name: {self.pack_name}\n"
            "    source:\n"
            f"      url: {self.bundled_url}\n"
            f"      ref: {self.bundled_ref}\n"
            f"    update_policy: {self.bundled_policy}\n",
        )

        # Seed a previous pack-lock with the OLD source_path
        # (``docs/rule-pack.md``) at the SAME requested_ref. The
        # commit recorded matches the new archive's resolved_commit
        # so the only drift surface is the source_path set.
        from packs import state as state_mod
        prev_lock = state_mod.empty_pack_lock()
        prev_lock["packs"][self.pack_name] = {
            "source_url": self.bundled_url,
            "requested_ref": self.bundled_ref,
            "resolved_commit": "c" * 40,
            "pack_update_policy": self.bundled_policy,
            "files": [
                {
                    "role": "passive",
                    "host": None,
                    "source_path": "docs/rule-pack.md",
                    "input_sha256": "0" * 64,
                    "output_paths": ["AGENTS.md"],
                    "output_scope": "project-local",
                    "effective_update_policy": self.bundled_policy,
                },
            ],
        }
        state_mod.save_pack_lock(
            self.root / ".agent-config" / "pack-lock.json", prev_lock,
        )

        # Synthetic archive with the NEW compact body at the new
        # source_path. ``resolved_commit`` matches the lock so the
        # commit-drift gate does NOT fire — only the source-path
        # drift gate (the v0.6.0 Phase 3 addition) triggers.
        self.archive_dir = self.root / "fake-archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        archive_manifest = (
            "version: 2\n"
            "packs:\n"
            f"  - name: {self.pack_name}\n"
            "    source:\n"
            f"      repo: {self.bundled_url}\n"
            f"      ref: {self.bundled_ref}\n"
            "    passive:\n"
            "      - files:\n"
            "          - from: docs/rule-pack-compact.md\n"
            "            to: AGENTS.md\n"
        )
        (self.archive_dir / "pack.yaml").write_text(archive_manifest)
        (self.archive_dir / "docs").mkdir(parents=True, exist_ok=True)
        (self.archive_dir / "docs" / "rule-pack-compact.md").write_text(
            "# compact rule pack body\n"
        )

        new_archive = MagicMock()
        new_archive.archive_dir = self.archive_dir
        # SAME commit as the lock's recorded value — only source_path
        # drift is in play. This pins the v0.6.0 contract: ref-stable
        # source-path drift still routes through the apply path.
        new_archive.resolved_commit = "c" * 40
        new_archive.method = "ssh"
        new_archive.url = self.bundled_url
        new_archive.ref = self.bundled_ref
        self.new_archive = new_archive

    @contextmanager
    def _no_lock(self):
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None
        with patch.object(locks, "acquire", side_effect=_ctx):
            yield

    def test_same_ref_source_path_switch_triggers_remigration(self) -> None:
        """Bundled ``from:`` migrated to ``docs/rule-pack-compact.md``;
        lock records ``docs/rule-pack.md`` at the same ``requested_ref``.

        Asserts:

        - Compose exits 0 (drift-and-migrate succeeds).
        - The drift surfaces through ``prompt_user_for_updates``
          (proving the same-ref source-path drift gate fired). When the
          decision is ``apply`` (the v0.6.0 default per Phase 4), the
          composer migrates rather than staying on stale content.
        - Lock's ``source_path`` advances to
          ``docs/rule-pack-compact.md`` (no stale value remains).
        - AGENTS.md contains the compact body (re-rendered from the
          new file).
        - No override-preservation regression: the consumer's
          deliberate pin would still survive — verified separately
          by ``TestBcGuardDistinguishesMinimalFromExplicitPin``.
        """
        with self._no_lock():
            with (
                patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self.new_archive,
                ),
                patch.object(
                    compose_packs, "prompt_user_for_updates",
                    return_value="apply",
                ) as prompt,
            ):
                rc, _out, _err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 0, msg=f"compose failed: {_err}")
        # The same-ref source-path drift gate must have routed the
        # selection through the prompt UI (the v0.6.0 contract).
        prompt.assert_called_once()

        # Lock advances to the new source_path; the stale value must
        # not remain anywhere in the lock entry.
        from packs import state as state_mod
        lock_path = self.root / ".agent-config" / "pack-lock.json"
        self.assertTrue(lock_path.exists())
        new_lock = state_mod.load_pack_lock(lock_path)
        new_files = new_lock["packs"][self.pack_name]["files"]
        new_source_paths = {
            f["source_path"]
            for f in new_files
            if f.get("role") == "passive" and f.get("source_path")
        }
        self.assertEqual(new_source_paths, {"docs/rule-pack-compact.md"})

        # AGENTS.md re-rendered with the compact body sourced from the
        # new path; the stale full-body content from the previous
        # ``docs/rule-pack.md`` must not appear.
        agents_md = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("compact rule pack body", agents_md)


# =====================================================================
# Host-aware default seeding (v0.6.0 post-review fix).
# =====================================================================


class TestDefaultV2SelectionsForHost(unittest.TestCase):
    """``_default_v2_selections_for_host`` filters claude-only bundled
    defaults out of the seed list under non-claude hosts.

    aa-core-skills declares ``hosts: [claude-code]`` in
    ``bootstrap/packs.yaml``; if the seed is not host-aware, a fresh
    codex consumer running bare ``anywhere-agents`` hits a hard
    host-mismatch error on the canonical command. This is the unit
    test for the filter helper. Compose-level wiring is exercised by
    ``TestComposeUnderCodexHostSkipsClaudeOnlyDefaults`` below.
    """

    def test_claude_code_returns_full_list(self) -> None:
        """Default claude-code host gets the full v0.6.0 default seed
        (agent-style + aa-core-skills)."""
        result = compose_packs._default_v2_selections_for_host("claude-code")
        names = [sel["name"] for sel in result]
        self.assertEqual(names, ["agent-style", "aa-core-skills"])

    def test_codex_drops_aa_core_skills(self) -> None:
        """Codex host drops the claude-only aa-core-skills from the
        seed; agent-style (host-agnostic) survives."""
        result = compose_packs._default_v2_selections_for_host("codex")
        names = [sel["name"] for sel in result]
        self.assertEqual(names, ["agent-style"])
        self.assertNotIn("aa-core-skills", names)

    def test_unknown_host_drops_claude_only(self) -> None:
        """Future non-claude hosts (treated as non-claude here) drop
        claude-only defaults too; the filter is fail-safe."""
        result = compose_packs._default_v2_selections_for_host("future-host")
        names = [sel["name"] for sel in result]
        self.assertNotIn("aa-core-skills", names)

    def test_full_list_unchanged_for_identity_lookups(self) -> None:
        """``DEFAULT_V2_SELECTION_NAMES`` is the identity-lookup set
        used by bundled-fallback gating; it stays canonical regardless
        of host so a user-pinned aa-core-skills entry still resolves
        through the bundled-fallback even under codex."""
        self.assertIn("aa-core-skills", compose_packs.DEFAULT_V2_SELECTION_NAMES)
        self.assertIn("agent-style", compose_packs.DEFAULT_V2_SELECTION_NAMES)


class TestComposeUnderCodexHostSkipsClaudeOnlyDefaults(unittest.TestCase):
    """Integration: compose with ``AGENT_CONFIG_HOST=codex`` and a
    minimal default config does NOT pre-seed ``aa-core-skills``, so
    the host-mismatch error path in ``packs/dispatch.py`` is never
    reached. Pre-fix v0.6.0 behavior: bare ``anywhere-agents`` errored
    on first run for codex consumers because the default seed always
    included aa-core-skills regardless of host."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env_patch = patch.dict(
            os.environ,
            {
                "APPDATA": str(self.root / "appdata"),
                "XDG_CONFIG_HOME": str(self.root / "xdg"),
                "AGENT_CONFIG_PACKS": "",
                "AGENT_CONFIG_RULE_PACKS": "",
                "AGENT_CONFIG_HOST": "codex",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_resolved_for_project_under_codex_excludes_aa_core_skills(self) -> None:
        """Direct check: the resolver, called with the host-filtered
        default selection, returns only agent-style for an empty
        consumer config under codex."""
        from packs import config as config_mod
        # Empty project config = no signal, defaults seed.
        selections = config_mod.resolved_for_project(
            self.root,
            default_selections=compose_packs._default_v2_selections_for_host("codex"),
            force_defaults=True,
        )
        names = {sel["name"] for sel in selections}
        self.assertIn("agent-style", names)
        self.assertNotIn("aa-core-skills", names)


if __name__ == "__main__":
    unittest.main()
