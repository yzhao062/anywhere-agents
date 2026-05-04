"""v0.6.0 Phase 4 / 5 tests for the ``anywhere-agents`` CLI.

Covers (per ``PLAN-aa-v0.6.0-update-flow-coherence.md`` § "Phase 4 -
Inline prompt-policy drift apply (Q1) with skip overrides" and
§ "Phase 5 - Compatibility alias dispatch + stderr notice"):

- Inline drift apply: by default, prompt-policy commit-drift on a
  mutable ref applies during the canonical apply path; the new
  ``applied-updates.json`` bridge file lets the CLI emit a stderr
  summary line per applied drift.
- Skip overrides: ``ANYWHERE_AGENTS_UPDATE=skip`` env var,
  ``--no-apply-drift`` CLI flag, and the precedence rule (CLI flag
  wins over env var when both are set).
- Locked-policy fail-closed: ``update_policy: locked`` drift continues
  to fail composition with a clear delta and exit nonzero.
- stderr summary line shapes for both commit-drift and same-ref
  source-path drift.
- Compatibility alias dispatch: ``pack verify --fix`` and ``pack update``
  print exactly one stderr notice each before executing the canonical
  apply path; the bare command does not print the notice.
- ``pack update <name>``: selective apply for the named pack only.
- ``pack verify`` (no ``--fix``) trailing message recommends
  ``anywhere-agents`` (not ``pack verify --fix``).
- Same-ref source-path drift skip preserves the lock's old source_path
  on revert (v0.6.0 Phase 4 concern (a) — no broken-state side effects).
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "pypi"))
sys.path.insert(0, str(ROOT / "scripts"))
# Repo root added so ``from scripts.packs import X`` (used by some
# composer modules) resolves consistently with ``from packs import X``.
sys.path.insert(0, str(ROOT))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# =====================================================================
# Fixture: in-process composer fixture for drift / apply round-trips.
# Mirrors the v0_6 compose-fixture style (resolved-commit change on a
# bundled inline-source pack), shared across multiple drift-flow tests.
# =====================================================================


class _BundledCommitDriftFixture(unittest.TestCase):
    """In-process fixture: bundled ``agent-style`` pack with one inline-
    source selection whose resolved_commit advances from ``a*40`` to
    ``b*40``.

    Used by the v0.6.0 Phase 4 tests that exercise the apply / skip
    decision and assert lock state, applied-updates.json, and the
    canonical-apply rc=0 contract.
    """

    pack_name: str = "agent-style"
    bundled_url: str = "https://github.com/yzhao062/agent-style"
    bundled_ref: str = "v0.3.5"
    bundled_policy: str = "auto"

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
                # Default to apply (the v0.6.0 inline-apply contract);
                # skip-flavor tests override this in the test method.
                "ANYWHERE_AGENTS_UPDATE": "apply",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        # Build a separate "old / locked" archive directory so the skip
        # path's load_cached_archive can return an archive whose commit
        # matches the recorded "a" * 40 — not the new "b" * 40 archive.
        self.locked_archive_dir = self.root / "fake-archive-locked"

        bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
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

        _write(
            self.root / "agent-config.yaml",
            "rule_packs:\n"
            f"  - name: {self.pack_name}\n"
            "    source:\n"
            f"      url: {self.bundled_url}\n"
            f"      ref: {self.bundled_ref}\n"
            f"    update_policy: {self.bundled_policy}\n",
        )

        from packs import state as state_mod
        prev_lock = state_mod.empty_pack_lock()
        prev_lock["packs"][self.pack_name] = {
            "source_url": self.bundled_url,
            "requested_ref": self.bundled_ref,
            "resolved_commit": "a" * 40,
            "pack_update_policy": self.bundled_policy,
            "files": [
                {
                    "role": "passive",
                    "host": None,
                    "source_path": "docs/rule-pack-compact.md",
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
            "# new compact body\n"
        )

        new_archive = MagicMock()
        new_archive.archive_dir = self.archive_dir
        new_archive.resolved_commit = "b" * 40
        new_archive.method = "ssh"
        new_archive.url = self.bundled_url
        new_archive.ref = self.bundled_ref
        self.new_archive = new_archive

        # Locked archive (recorded commit "a"*40) used by the skip-path
        # revert. Mirrors the new archive's directory contents so the
        # passive handler still resolves the from-path; the
        # ``resolved_commit`` differs so the revert is observable.
        self.locked_archive_dir.mkdir(parents=True, exist_ok=True)
        (self.locked_archive_dir / "pack.yaml").write_text(archive_manifest)
        (self.locked_archive_dir / "docs").mkdir(parents=True, exist_ok=True)
        (self.locked_archive_dir / "docs" / "rule-pack-compact.md").write_text(
            "# old locked body\n"
        )
        locked_archive = MagicMock()
        locked_archive.archive_dir = self.locked_archive_dir
        locked_archive.resolved_commit = "a" * 40
        locked_archive.method = "ssh"
        locked_archive.url = self.bundled_url
        locked_archive.ref = self.bundled_ref
        self.locked_archive = locked_archive

    @contextmanager
    def _no_lock(self):
        from packs import locks
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None
        with patch.object(locks, "acquire", side_effect=_ctx):
            yield

    def _invoke_composer(self, *extra_args: str) -> tuple[int, str, str]:
        """Invoke ``compose_packs.main`` in-process with env-var-driven
        prompt resolution.

        Pytest captures stdin/stdout, but the isatty signal may still
        return True on some platforms; defensively stub
        ``compose_packs._interactive_prompt`` so the env-var path is the
        only source of truth for the apply / skip decision in this
        fixture. The env var is set per test method.
        """
        import compose_packs
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            with self._no_lock():
                from packs import source_fetch
                # Force non-TTY path so prompt_user_for_updates honors
                # ANYWHERE_AGENTS_UPDATE; pytest may report stdin/stdout
                # as TTY on some Windows configurations.
                fake_stdin = MagicMock()
                fake_stdin.isatty.return_value = False
                fake_stdout = MagicMock()
                fake_stdout.isatty.return_value = False
                with patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self.new_archive,
                ), patch.object(sys, "stdin", fake_stdin), patch.object(
                    sys, "stdout", fake_stdout,
                ):
                    rc = compose_packs.main(
                        ["--root", str(self.root), *extra_args]
                    )
        return rc, out_buf.getvalue(), err_buf.getvalue()


# =====================================================================
# Phase 4: inline drift apply default (Q1).
# =====================================================================


class TestDriftInlineApplyDefault(_BundledCommitDriftFixture):
    """Drift on a mutable ref applies inline by default; the lock
    advances; AGENTS.md is re-rendered from the new content; the
    composer writes ``applied-updates.json`` so the CLI parent can
    emit the v0.6.0 stderr summary line.
    """

    pack_name = "agent-style"
    bundled_policy = "prompt"

    def test_drift_inline_apply_default(self) -> None:
        rc, _out, _err = self._invoke_composer()
        self.assertEqual(rc, 0)

        from packs import state as state_mod
        new_lock = state_mod.load_pack_lock(
            self.root / ".agent-config" / "pack-lock.json"
        )
        self.assertEqual(
            new_lock["packs"][self.pack_name]["resolved_commit"], "b" * 40,
            "lock must advance to the new resolved_commit on apply",
        )

        applied_path = self.root / ".agent-config" / "applied-updates.json"
        self.assertTrue(
            applied_path.exists(),
            "composer must write applied-updates.json on apply so the "
            "CLI parent can emit the stderr summary line",
        )
        applied = json.loads(applied_path.read_text(encoding="utf-8"))
        records = applied["applied"]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["name"], self.pack_name)
        self.assertEqual(record["drift_kind"], "commit")
        self.assertEqual(record["old_short"], "a" * 7)
        self.assertEqual(record["new_short"], "b" * 7)

        agents_md = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("new compact body", agents_md)


class TestDriftSkipViaEnvVar(_BundledCommitDriftFixture):
    """``ANYWHERE_AGENTS_UPDATE=skip`` defers the apply: the composer
    reverts to the cached locked archive, leaves the lock unchanged,
    and does NOT write ``applied-updates.json``.
    """

    pack_name = "agent-style"
    bundled_policy = "prompt"

    def test_drift_skip_via_env_var(self) -> None:
        # Pre-seed the cache with the recorded commit so the skip-path
        # revert can find the locked archive.
        from packs import source_fetch
        import compose_packs
        os.environ["ANYWHERE_AGENTS_UPDATE"] = "skip"

        # Stub ``prompt_user_for_updates`` to honor the env var
        # explicitly so the test does not depend on TTY heuristics
        # under pytest output capture.
        def env_aware_prompt(pending):
            return os.environ.get("ANYWHERE_AGENTS_UPDATE", "skip")

        with patch.object(
            source_fetch, "load_cached_archive",
            return_value=self.locked_archive,  # archive at recorded commit "a"
        ), patch.object(
            compose_packs, "prompt_user_for_updates",
            side_effect=env_aware_prompt,
        ):
            rc, _out, _err = self._invoke_composer()

        self.assertEqual(rc, 0, msg=f"err: {_err}")
        from packs import state as state_mod
        new_lock = state_mod.load_pack_lock(
            self.root / ".agent-config" / "pack-lock.json"
        )
        # Skip preserves lock at the recorded commit.
        self.assertEqual(
            new_lock["packs"][self.pack_name]["resolved_commit"], "a" * 40,
            "skip must preserve the lock at the recorded commit",
        )

        applied_path = self.root / ".agent-config" / "applied-updates.json"
        self.assertFalse(
            applied_path.exists(),
            "skip must not write applied-updates.json (no apply happened)",
        )


class TestDriftSkipViaCliFlag(_BundledCommitDriftFixture):
    """``--no-apply-drift`` defers the apply identically to the env
    var path. The CLI flag is the v0.6.0-introduced surface; the
    env-var path is preserved for v0.5.0 contract compatibility.
    """

    pack_name = "agent-style"
    bundled_policy = "prompt"

    def test_drift_skip_via_cli_flag(self) -> None:
        from packs import source_fetch
        # Env var defaults to "apply" in the fixture; without setting
        # it back to a non-skip value here we are testing CLI-flag-only.
        with patch.object(
            source_fetch, "load_cached_archive",
            return_value=self.locked_archive,  # archive at recorded commit "a"
        ):
            rc, _out, _err = self._invoke_composer("--no-apply-drift")

        self.assertEqual(rc, 0)
        from packs import state as state_mod
        new_lock = state_mod.load_pack_lock(
            self.root / ".agent-config" / "pack-lock.json"
        )
        self.assertEqual(
            new_lock["packs"][self.pack_name]["resolved_commit"], "a" * 40,
            "--no-apply-drift must preserve the lock at the recorded commit",
        )
        applied_path = self.root / ".agent-config" / "applied-updates.json"
        self.assertFalse(applied_path.exists())


class TestDriftSkipCliFlagWinsOverEnv(_BundledCommitDriftFixture):
    """When BOTH ``--no-apply-drift`` (CLI flag) and
    ``ANYWHERE_AGENTS_UPDATE=apply`` (env var) are set, the CLI flag
    wins: the run skips. Documents the v0.6.0 precedence rule (CLI
    flag > env var > default).
    """

    pack_name = "agent-style"
    bundled_policy = "prompt"

    def test_drift_skip_via_cli_flag_wins_over_env(self) -> None:
        from packs import source_fetch
        # Env says "apply" (loud opt-in); CLI flag says "no apply".
        # CLI flag must win.
        os.environ["ANYWHERE_AGENTS_UPDATE"] = "apply"

        with patch.object(
            source_fetch, "load_cached_archive",
            return_value=self.locked_archive,
        ):
            rc, _out, _err = self._invoke_composer("--no-apply-drift")

        self.assertEqual(rc, 0)
        from packs import state as state_mod
        new_lock = state_mod.load_pack_lock(
            self.root / ".agent-config" / "pack-lock.json"
        )
        self.assertEqual(
            new_lock["packs"][self.pack_name]["resolved_commit"], "a" * 40,
            "CLI flag must win over env var: lock stays at recorded commit",
        )


class TestDriftLockedStillFailsClosed(_BundledCommitDriftFixture):
    """``update_policy: locked`` drift continues to fail closed in
    v0.6.0. The composer surfaces a ``PackLockDriftError`` from the
    pre-fetch loop; rc != 0; no apply; clear delta in stderr.
    """

    pack_name = "agent-style"
    bundled_policy = "locked"

    def test_drift_locked_still_fails_closed(self) -> None:
        # Use source_fetch.fetch_pack stubbed to raise PackLockDriftError
        # to simulate the locked-policy fail-closed path (locked-policy
        # drift surfaces this exception inside the pre-fetch loop).
        from packs import source_fetch
        with patch.object(
            source_fetch, "fetch_pack",
            side_effect=source_fetch.PackLockDriftError(
                self.bundled_url, self.bundled_ref, "a" * 40, "b" * 40,
            ),
        ):
            with self._no_lock():
                import compose_packs
                out_buf, err_buf = io.StringIO(), io.StringIO()
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = compose_packs.main(["--root", str(self.root)])

        self.assertNotEqual(
            rc, 0, "locked-policy drift must fail closed (nonzero rc)",
        )
        # Locked policy must NOT advance the lock.
        from packs import state as state_mod
        new_lock = state_mod.load_pack_lock(
            self.root / ".agent-config" / "pack-lock.json"
        )
        self.assertEqual(
            new_lock["packs"][self.pack_name]["resolved_commit"], "a" * 40,
        )
        # Composer surfaces a clear delta on stderr (the URL plus the
        # recorded vs current commits, per PackLockDriftError.__str__).
        combined = err_buf.getvalue() + out_buf.getvalue()
        self.assertIn(self.bundled_url, combined)
        self.assertIn("pack-lock drift", combined)


# =====================================================================
# stderr summary format — both commit-drift and same-ref path-drift.
# =====================================================================


class TestStderrSummaryFormat(unittest.TestCase):
    """Pin both ``_emit_apply_summary`` line shapes:

    - commit-drift: ``applied 1 update for <pack> @ <ref>: <old> -> <new>``
    - same-ref source-path drift: ``migrated 1 path for <pack> @ <ref>: <old_path> -> <new_path>``
    """

    def test_stderr_summary_format_commit_drift(self) -> None:
        from anywhere_agents.cli import _emit_apply_summary
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".agent-config").mkdir(parents=True)
            applied = root / ".agent-config" / "applied-updates.json"
            applied.write_text(
                json.dumps({
                    "ts": "2026-05-03T12:00:00+00:00",
                    "host": "claude-code",
                    "applied": [
                        {
                            "name": "agent-pack",
                            "ref": "main",
                            "drift_kind": "commit",
                            "old_short": "1234567",
                            "new_short": "abcdef0",
                        },
                    ],
                }),
                encoding="utf-8",
            )
            err_buf = io.StringIO()
            with redirect_stderr(err_buf):
                count = _emit_apply_summary(root)
        self.assertEqual(count, 1)
        self.assertIn(
            "applied 1 update for agent-pack @ main: 1234567 -> abcdef0",
            err_buf.getvalue(),
        )
        # File is consumed (unlinked).
        self.assertFalse(applied.exists())

    def test_stderr_summary_format_same_ref_path_drift(self) -> None:
        from anywhere_agents.cli import _emit_apply_summary
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".agent-config").mkdir(parents=True)
            applied = root / ".agent-config" / "applied-updates.json"
            applied.write_text(
                json.dumps({
                    "ts": "2026-05-03T12:00:00+00:00",
                    "host": "claude-code",
                    "applied": [
                        {
                            "name": "agent-style",
                            "ref": "v0.3.2",
                            "drift_kind": "path",
                            "old_short": "abcabca",
                            "new_short": "abcabca",
                            "old_paths": ["docs/rule-pack.md"],
                            "new_paths": ["docs/rule-pack-compact.md"],
                        },
                    ],
                }),
                encoding="utf-8",
            )
            err_buf = io.StringIO()
            with redirect_stderr(err_buf):
                count = _emit_apply_summary(root)
        self.assertEqual(count, 1)
        self.assertIn(
            "migrated 1 path for agent-style @ v0.3.2: "
            "docs/rule-pack.md -> docs/rule-pack-compact.md",
            err_buf.getvalue(),
        )
        self.assertFalse(applied.exists())


# =====================================================================
# Phase 5: compatibility alias dispatch + stderr notice.
# =====================================================================


class TestPackVerifyFixAliasEmitsNotice(unittest.TestCase):
    """``pack verify --fix`` now prints a one-line stderr notice
    pointing at the canonical bare command, then dispatches to the
    canonical apply path. The bare command does NOT print this notice
    (the post-bootstrap heal pass sets a sentinel env var to suppress
    it).
    """

    def test_alias_path_emits_notice(self) -> None:
        # Path 1 (alias): direct ``pack verify --fix`` invocation.
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            user_path = Path(d) / "user-config.yaml"
            user_path.write_text("packs: []\n", encoding="utf-8")
            project = Path(d) / "project"
            project.mkdir()
            (project / "agent-config.yaml").write_text(
                "rule_packs: []\n", encoding="utf-8",
            )
            cwd_before = os.getcwd()
            err_buf = io.StringIO()
            try:
                os.chdir(project)
                # Pop the suppress env var if present from a parent
                # session so we test the un-suppressed path.
                prior = os.environ.pop("_AA_ALIAS_NOTICE_SUPPRESS", None)
                try:
                    with redirect_stdout(io.StringIO()), redirect_stderr(err_buf):
                        _pack_main(user_path, ["verify", "--fix", "--yes"])
                finally:
                    if prior is not None:
                        os.environ["_AA_ALIAS_NOTICE_SUPPRESS"] = prior
            finally:
                os.chdir(cwd_before)
            self.assertIn(
                "'pack verify --fix' is now an alias for 'anywhere-agents'",
                err_buf.getvalue(),
                "alias must print exactly one stderr notice before "
                "dispatching to the canonical apply path",
            )

    def test_canonical_path_does_not_emit_notice(self) -> None:
        # Path 2 (canonical): bare command's post-bootstrap heal pass.
        # ``_bootstrap_main`` sets ``_AA_ALIAS_NOTICE_SUPPRESS=1`` before
        # calling ``main(["pack", "verify", "--fix", "--yes"])`` so the
        # dispatch suppresses the notice.
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            user_path = Path(d) / "user-config.yaml"
            user_path.write_text("packs: []\n", encoding="utf-8")
            project = Path(d) / "project"
            project.mkdir()
            (project / "agent-config.yaml").write_text(
                "rule_packs: []\n", encoding="utf-8",
            )
            cwd_before = os.getcwd()
            err_buf = io.StringIO()
            try:
                os.chdir(project)
                os.environ["_AA_ALIAS_NOTICE_SUPPRESS"] = "1"
                try:
                    with redirect_stdout(io.StringIO()), redirect_stderr(err_buf):
                        _pack_main(user_path, ["verify", "--fix", "--yes"])
                finally:
                    os.environ.pop("_AA_ALIAS_NOTICE_SUPPRESS", None)
            finally:
                os.chdir(cwd_before)
            self.assertNotIn(
                "'pack verify --fix' is now an alias",
                err_buf.getvalue(),
                "canonical path (suppress env set) must NOT print the alias "
                "notice; only the user-facing alias surface does.",
            )


class TestPackUpdateAliasEmitsNotice(unittest.TestCase):
    """``pack update <name>`` likewise prints a one-line stderr
    notice before executing the (now selective) apply path.
    """

    def test_alias_path_emits_notice(self) -> None:
        from anywhere_agents.cli import _pack_main
        from anywhere_agents.packs import auth
        import yaml as _yaml
        with tempfile.TemporaryDirectory() as d:
            user_path = Path(d) / "user-config.yaml"
            user_path.write_text(
                _yaml.safe_dump({
                    "packs": [{
                        "name": "agent-pack",
                        "source": {
                            "url": "https://github.com/yzhao062/agent-pack",
                            "ref": "main",
                        },
                    }],
                }),
                encoding="utf-8",
            )
            project = Path(d) / "project"
            composer = (
                project / ".agent-config" / "repo" / "scripts" /
                "compose_packs.py"
            )
            composer.parent.mkdir(parents=True)
            composer.write_text("# placeholder\n", encoding="utf-8")
            cwd_before = os.getcwd()
            err_buf = io.StringIO()
            try:
                os.chdir(project)
                prior = os.environ.pop("_AA_ALIAS_NOTICE_SUPPRESS", None)
                try:
                    with patch.object(
                        auth, "resolve_ref_with_auth_chain",
                        return_value=("cd" * 20, "anonymous"),
                    ), patch(
                        "anywhere_agents.cli._invoke_composer_with_gen_fallback",
                        return_value=0,
                    ), redirect_stderr(err_buf), redirect_stdout(io.StringIO()):
                        _pack_main(user_path, ["update", "agent-pack"])
                finally:
                    if prior is not None:
                        os.environ["_AA_ALIAS_NOTICE_SUPPRESS"] = prior
            finally:
                os.chdir(cwd_before)
            self.assertIn(
                "'pack update' is now an alias for 'anywhere-agents'",
                err_buf.getvalue(),
                "pack update alias must emit the v0.6.0 stderr notice",
            )


class TestPackUpdateNamedPackAppliesOnlyThatPack(unittest.TestCase):
    """``pack update <name>`` threads ``--apply-name <name>`` through
    to the composer subprocess so only that pack's drift is applied;
    other drifted packs are left untouched.
    """

    def test_apply_name_is_passed_to_composer(self) -> None:
        from anywhere_agents.cli import _pack_main
        from anywhere_agents.packs import auth
        import yaml as _yaml
        with tempfile.TemporaryDirectory() as d:
            user_path = Path(d) / "user-config.yaml"
            user_path.write_text(
                _yaml.safe_dump({
                    "packs": [{
                        "name": "agent-pack",
                        "source": {
                            "url": "https://github.com/yzhao062/agent-pack",
                            "ref": "main",
                        },
                    }],
                }),
                encoding="utf-8",
            )
            project = Path(d) / "project"
            composer = (
                project / ".agent-config" / "repo" / "scripts" /
                "compose_packs.py"
            )
            composer.parent.mkdir(parents=True)
            composer.write_text("# placeholder\n", encoding="utf-8")
            cwd_before = os.getcwd()
            invocations: list = []
            try:
                os.chdir(project)
                with patch.object(
                    auth, "resolve_ref_with_auth_chain",
                    return_value=("cd" * 20, "anonymous"),
                ), patch(
                    "anywhere_agents.cli._invoke_composer_with_gen_fallback",
                    side_effect=lambda *a, **kw: invocations.append((a, kw)) or 0,
                ), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    _pack_main(user_path, ["update", "agent-pack"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(len(invocations), 1)
            args, kwargs = invocations[0]
            # First positional is project_root; remaining positionals are
            # composer-side flags. Assert ``--apply-name agent-pack`` is
            # in the flag list.
            flags = list(args[1:])
            self.assertIn("--apply-name", flags)
            self.assertIn("agent-pack", flags)
            # Apply-name's index must be followed by the pack name.
            apply_idx = flags.index("--apply-name")
            self.assertEqual(flags[apply_idx + 1], "agent-pack")
            # ANYWHERE_AGENTS_UPDATE=apply env extra preserved (selective
            # apply still routes through the apply env-var path so the
            # composer's drift gate cannot defer to skip).
            self.assertEqual(
                kwargs.get("env_extra", {}).get("ANYWHERE_AGENTS_UPDATE"),
                "apply",
            )


class TestPackUpdateAllCompatibilityForm(unittest.TestCase):
    """``pack update --all`` is the retained CI compatibility form for
    legacy v0.5.x scripts (pack-architecture.md ~line 653; PLAN lines
    114, 199; CHANGELOG entry). Unlike ``pack update <name>`` (selective
    power-user verb that threads ``--apply-name <name>``), ``--all``
    dispatches the canonical bare-command path with no per-pack filter.

    All forms must emit the v0.6.0 alias stderr notice before
    dispatching, mirroring the surface contract of
    :class:`TestPackUpdateAliasEmitsNotice`.
    """

    def _run_with_args(self, argv: list[str]):
        """Drive ``_pack_main`` with ``argv``; capture composer
        invocations + stderr output.

        Returns ``(rc, invocations, stderr_text)``. The composer
        subprocess is patched to record ``(args, kwargs)`` per call and
        return rc=0; tests inspect the captured argv to assert the
        canonical-apply form (no ``--apply-name``) is used.
        """
        from anywhere_agents.cli import _pack_main
        from anywhere_agents.packs import auth
        import yaml as _yaml
        with tempfile.TemporaryDirectory() as d:
            user_path = Path(d) / "user-config.yaml"
            user_path.write_text(
                _yaml.safe_dump({
                    "packs": [{
                        "name": "agent-pack",
                        "source": {
                            "url": "https://github.com/yzhao062/agent-pack",
                            "ref": "main",
                        },
                    }],
                }),
                encoding="utf-8",
            )
            project = Path(d) / "project"
            composer = (
                project / ".agent-config" / "repo" / "scripts" /
                "compose_packs.py"
            )
            composer.parent.mkdir(parents=True)
            composer.write_text("# placeholder\n", encoding="utf-8")
            cwd_before = os.getcwd()
            invocations: list = []
            err_buf = io.StringIO()
            try:
                os.chdir(project)
                # Pop any prior alias-notice suppressor so the test
                # exercises the user-facing alias surface.
                prior = os.environ.pop("_AA_ALIAS_NOTICE_SUPPRESS", None)
                try:
                    with patch.object(
                        auth, "resolve_ref_with_auth_chain",
                        return_value=("cd" * 20, "anonymous"),
                    ), patch(
                        "anywhere_agents.cli._invoke_composer_with_gen_fallback",
                        side_effect=lambda *a, **kw: invocations.append((a, kw)) or 0,
                    ), redirect_stderr(err_buf), redirect_stdout(io.StringIO()):
                        rc = _pack_main(user_path, argv)
                finally:
                    if prior is not None:
                        os.environ["_AA_ALIAS_NOTICE_SUPPRESS"] = prior
            finally:
                os.chdir(cwd_before)
        return rc, invocations, err_buf.getvalue()

    def test_pack_update_all_dispatches_canonical_apply(self) -> None:
        """``pack update --all`` calls the composer once with the project
        root as the first positional arg and NO ``--apply-name`` flag
        (the ``--all`` form dispatches the canonical bare-command path,
        not the selective form).
        """
        rc, invocations, stderr_text = self._run_with_args(
            ["update", "--all"]
        )
        self.assertEqual(rc, 0, msg=f"unexpected rc; stderr={stderr_text}")
        self.assertEqual(
            len(invocations), 1,
            "pack update --all must dispatch exactly one composer call",
        )
        args, _kwargs = invocations[0]
        # First positional is the project root (Path.cwd()).
        self.assertGreaterEqual(len(args), 1)
        self.assertIsInstance(args[0], Path)
        # Remaining positionals are composer-side flags. The ``--all``
        # form must NOT include ``--apply-name`` (that's selective-only).
        flags = list(args[1:])
        self.assertNotIn(
            "--apply-name", flags,
            "--all dispatches the canonical apply path; selective filter "
            "must not be threaded",
        )
        # Alias notice must be emitted on the alias path.
        self.assertIn(
            "'pack update' is now an alias for 'anywhere-agents'",
            stderr_text,
            "pack update --all must emit the v0.6.0 stderr alias notice",
        )

    def test_pack_update_all_with_no_apply_drift_threads_flag(self) -> None:
        """``pack update --all --no-apply-drift`` threads the flag to
        the composer subprocess, matching the v0.6.0 Phase 4 contract
        for per-run skip overrides.
        """
        rc, invocations, stderr_text = self._run_with_args(
            ["update", "--all", "--no-apply-drift"]
        )
        self.assertEqual(rc, 0, msg=f"unexpected rc; stderr={stderr_text}")
        self.assertEqual(len(invocations), 1)
        args, _kwargs = invocations[0]
        flags = list(args[1:])
        self.assertIn(
            "--no-apply-drift", flags,
            "--no-apply-drift CLI flag must propagate to the composer",
        )
        # Alias notice still emitted on the alias path.
        self.assertIn(
            "'pack update' is now an alias for 'anywhere-agents'",
            stderr_text,
        )

    def test_pack_update_all_rejects_name_or_ref(self) -> None:
        """``pack update --all`` is mutually exclusive with both ``<name>``
        and ``--ref``. Each invalid combination exits rc=2 with an error
        message naming the conflict.
        """
        # Both sub-cases run through the same alias-dispatch path; the
        # alias notice fires before argparse-side validation rejects the
        # combination, so the notice should appear on stderr in both.
        for argv in (
            ["update", "--all", "foo"],
            ["update", "--all", "--ref", "bar"],
        ):
            with self.subTest(argv=argv):
                rc, invocations, stderr_text = self._run_with_args(argv)
                self.assertEqual(
                    rc, 2,
                    f"argv={argv} must exit rc=2; got {rc}",
                )
                self.assertEqual(
                    len(invocations), 0,
                    "rejected combination must NOT dispatch the composer",
                )
                # Error message names the conflict.
                self.assertIn(
                    "--all",
                    stderr_text,
                    f"argv={argv} stderr must mention --all conflict",
                )
                # Alias notice still emitted on the alias path before
                # argparse-style validation rejects.
                self.assertIn(
                    "'pack update' is now an alias for 'anywhere-agents'",
                    stderr_text,
                    f"argv={argv} must emit the v0.6.0 stderr alias notice",
                )


class TestPackVerifyNoFixStillInspectsOnly(unittest.TestCase):
    """``pack verify`` (no ``--fix``) remains a read-only inspection
    verb. v0.6.0 updates the trailing message to recommend
    ``anywhere-agents`` instead of ``pack verify --fix``. No alias
    notice is printed (it is a real read-only verb, not an alias).
    """

    def test_no_fix_does_not_write_or_emit_alias_notice(self) -> None:
        from anywhere_agents.cli import _pack_verify
        import argparse
        with tempfile.TemporaryDirectory() as d:
            project_root = Path(d)
            user_path = project_root / "user-config.yaml"
            user_path.write_text(
                "packs:\n"
                "  - name: orphan-user-only\n"
                "    source:\n"
                "      url: https://github.com/example/orphan\n"
                "      ref: main\n",
                encoding="utf-8",
            )
            # No project agent-config.yaml so the user-only entry surfaces
            # as USER_ONLY and triggers the trailing recommendation.
            args = argparse.Namespace(fix=False, yes=False)
            out_buf = io.StringIO()
            err_buf = io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project_root)
                with patch(
                    "anywhere_agents.cli._ls_remote_head", return_value=None,
                ), redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_verify(user_path, project_root, args)
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 1)
            output = out_buf.getvalue()
            # v0.6.0: trailing message recommends 'anywhere-agents'.
            self.assertIn("anywhere-agents", output)
            # Alias notice is NOT printed for the read-only verb.
            self.assertNotIn(
                "'pack verify --fix' is now an alias", err_buf.getvalue()
            )
            # User config and project files remain unchanged (no writes).
            self.assertEqual(
                user_path.read_text(encoding="utf-8"),
                "packs:\n"
                "  - name: orphan-user-only\n"
                "    source:\n"
                "      url: https://github.com/example/orphan\n"
                "      ref: main\n",
                "pack verify (no --fix) must not modify user config",
            )


# =====================================================================
# v0.6.0 Phase 4 concern (a): same-ref source-path drift skip preserves
# the lock's old source_path on revert. Without this, a v0.3.2-with-no-
# compact-file consumer would be left in a broken state (404 raw fetch).
# =====================================================================


class TestSkipPreservesLockOnSameRefSourcePathDrift(unittest.TestCase):
    """When same-ref source-path drift is skipped (env var or CLI flag),
    the lock's recorded ``source_path`` must NOT be rewritten to the
    new bundled-default ``from:`` path. The composer reverts the
    substituted ``pack_def`` back to the lock's old ``from:`` paths so
    the passive handler reads the OLD path within the cached archive
    at the recorded commit (which exists at that commit by definition,
    since the lock was previously written for that path).
    """

    pack_name = "agent-style"
    bundled_url = "https://github.com/yzhao062/agent-style"
    bundled_ref = "v0.3.2"
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
                "ANYWHERE_AGENTS_UPDATE": "skip",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

        bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
        # Bundled manifest: ``from:`` points at the NEW compact path.
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

        _write(
            self.root / "agent-config.yaml",
            "rule_packs:\n"
            f"  - name: {self.pack_name}\n"
            "    source:\n"
            f"      url: {self.bundled_url}\n"
            f"      ref: {self.bundled_ref}\n"
            f"    update_policy: {self.bundled_policy}\n",
        )

        # Seed the lock with the OLD source_path at the SAME requested_ref.
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
                    "source_path": "docs/rule-pack.md",  # OLD path
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
            # Both old and new paths exist in the archive so the skip
            # path can read either; the test asserts the OLD path is
            # what ends up in the lock after skip.
            "          - from: docs/rule-pack-compact.md\n"
            "            to: AGENTS.md\n"
        )
        (self.archive_dir / "pack.yaml").write_text(archive_manifest)
        (self.archive_dir / "docs").mkdir(parents=True, exist_ok=True)
        (self.archive_dir / "docs" / "rule-pack.md").write_text(
            "# OLD full-body content\n"
        )
        (self.archive_dir / "docs" / "rule-pack-compact.md").write_text(
            "# NEW compact content\n"
        )

        new_archive = MagicMock()
        new_archive.archive_dir = self.archive_dir
        new_archive.resolved_commit = "c" * 40  # SAME commit
        new_archive.method = "ssh"
        new_archive.url = self.bundled_url
        new_archive.ref = self.bundled_ref
        self.new_archive = new_archive

    @contextmanager
    def _no_lock(self):
        from packs import locks
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None
        with patch.object(locks, "acquire", side_effect=_ctx):
            yield

    def test_skip_preserves_lock_on_same_ref_source_path_drift(self) -> None:
        from packs import source_fetch
        import compose_packs
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with self._no_lock(), redirect_stdout(out_buf), redirect_stderr(err_buf):
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=self.new_archive,
            ), patch.object(
                source_fetch, "load_cached_archive",
                return_value=self.new_archive,
            ):
                rc = compose_packs.main(["--root", str(self.root)])

        self.assertEqual(rc, 0, msg=f"compose failed: {err_buf.getvalue()}")

        from packs import state as state_mod
        new_lock = state_mod.load_pack_lock(
            self.root / ".agent-config" / "pack-lock.json"
        )
        # Critical invariant (Phase 4 concern (a)): lock's source_path
        # must remain the OLD path. If a future refactor breaks this,
        # the consumer is left in the v0.6.0-pre broken state where
        # passive.py 404s on the new path.
        files = new_lock["packs"][self.pack_name]["files"]
        old_paths = {
            f["source_path"]
            for f in files
            if f.get("role") == "passive" and f.get("source_path")
        }
        self.assertEqual(
            old_paths, {"docs/rule-pack.md"},
            "skip on same-ref source-path drift must preserve the lock's "
            "OLD source_path; no broken-state side effects",
        )
        # AGENTS.md is rendered from the OLD content (the passive handler
        # reads docs/rule-pack.md, not docs/rule-pack-compact.md).
        agents_md = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("OLD full-body content", agents_md)


# =====================================================================
# Smoke item 28: minimal auto-reconciled-entry fixture (PLAN lines
# 153-163; pack-architecture.md ~line 856). Exact acceptance fixture
# called out in Codex Round 1 "New" Finding 4 — the existing v0_6 same-
# ref tests put ``update_policy: auto`` in the project YAML, so they do
# NOT exercise the minimal-entry case. This class drives the canonical
# apply path end-to-end against the exact fixture and asserts both:
#
#   (1) Auto-reconciled minimal entries are NOT preserved as opaque
#       user pins (BC-guard refinement, Phase 3).
#   (2) Genuine user-authored ``passive`` / ``active`` /
#       ``update_policy`` keys SURVIVE unchanged (override preservation,
#       parallel fixture).
# =====================================================================


class TestSmoke28MinimalAutoReconciledFixture(unittest.TestCase):
    """Smoke item 28 acceptance fixture — drives the canonical apply
    path through ``_pack_main(user_path, ["verify", "--fix", "--yes"])``
    against (a) a minimal auto-reconciled-entry case (no shape /
    ref / policy overrides) where the BC-guard refinement must allow
    the bundled migration to converge lock + AGENTS.md, and
    (b) the parallel override-preservation case where genuine
    user-authored ``passive`` / ``update_policy`` keys must round-trip
    unchanged.

    Mirrors the mocking pattern of :class:`TestSkipPreservesLockOnSameRefSourcePathDrift`:
    inline-source pack with both old and new from-paths in a fake
    archive at the same ``requested_ref`` (so only ``source_path``
    advances; this isolates the refinement classifier without bringing
    a ref bump into play).
    """

    pack_name = "agent-style"
    bundled_url = "https://github.com/yzhao062/agent-style"
    # The bundled (post-flip) default ref shipped in
    # ``bootstrap/packs.yaml``. The minimal-entry fixture starts with the
    # stale ``stale_project_ref`` in both ``agent-config.yaml`` and the
    # lock; the canonical apply path must converge them to
    # ``bundled_ref``.
    bundled_ref = "v0.3.5"
    stale_project_ref = "v0.3.2"
    bundled_policy = "auto"
    # Synthetic commits that distinguish the stale-lock state from the
    # post-fetch state. The lock seed uses ``stale_resolved_commit``;
    # the fake archive returns ``new_resolved_commit`` so the auth-chain
    # mock and the in-process composer agree on the post-fetch value.
    stale_resolved_commit = "c" * 40
    new_resolved_commit = "d" * 40

    def _build_fixture(
        self,
        agent_config_yaml_text: str,
        project_ref: str | None = None,
    ) -> Path:
        """Lay down the smoke-28 fixture rooted at a fresh tempdir.

        Caller passes the full ``agent-config.yaml`` text so each test
        method can vary the project-side entry shape (minimal vs.
        explicit override) while sharing the bundled-manifest, lock,
        and fake-archive scaffolding.

        ``project_ref`` controls the seed ref written into
        ``pack-lock.json`` (and is also the value the caller is expected
        to embed in ``agent_config_yaml_text``). Defaults to
        ``self.bundled_ref`` (post-flip same-ref shape). Pass
        ``self.stale_project_ref`` to seed the stale-ref convergence
        path that smoke item 28 exercises.

        Returns the resolved root path (caller is responsible for cwd
        switching). ``self.archive_dir`` is populated for use with
        ``source_fetch.fetch_pack`` patches.
        """
        if project_ref is None:
            project_ref = self.bundled_ref
        # Lock-side fields. When seeding stale, the lock's
        # ``requested_ref`` and ``resolved_commit`` reflect the pre-flip
        # state; post-run they should advance to bundled.
        is_stale = project_ref != self.bundled_ref
        seeded_resolved_commit = (
            self.stale_resolved_commit if is_stale
            else self.new_resolved_commit
        )

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

        bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
        # Bundled manifest: post-flip shape — the bundled ref is always
        # ``self.bundled_ref`` regardless of project-side stale-ness.
        # The fixture's stale-vs-fresh asymmetry lives only in the
        # project YAML and lock seed.
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

        _write(self.root / "agent-config.yaml", agent_config_yaml_text)

        # Seed the lock with the OLD source_path (drift signal). When
        # ``project_ref`` is stale, ``requested_ref`` is also stale so
        # the lock-side advancement is observable post-run.
        from packs import state as state_mod
        prev_lock = state_mod.empty_pack_lock()
        prev_lock["packs"][self.pack_name] = {
            "source_url": self.bundled_url,
            "requested_ref": project_ref,
            "resolved_commit": seeded_resolved_commit,
            "pack_update_policy": self.bundled_policy,
            "files": [
                {
                    "role": "passive",
                    "host": None,
                    "source_path": "docs/rule-pack.md",  # OLD path
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

        # Fake archive carries both old and new from-paths so the
        # canonical apply path can read either; the test asserts the
        # NEW path lands in the lock after the run.
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
        (self.archive_dir / "docs" / "rule-pack.md").write_text(
            "# OLD full-body content\n"
        )
        (self.archive_dir / "docs" / "rule-pack-compact.md").write_text(
            "# NEW compact content\n"
        )

        new_archive = MagicMock()
        new_archive.archive_dir = self.archive_dir
        # Post-fetch commit reflects the bundled-ref tip; the apply path
        # uses this to advance the lock's ``resolved_commit``.
        new_archive.resolved_commit = self.new_resolved_commit
        new_archive.method = "ssh"
        new_archive.url = self.bundled_url
        new_archive.ref = self.bundled_ref
        self.new_archive = new_archive

        return self.root

    def _env_patch(self):
        """Patch env so the canonical apply path runs against a hermetic
        config home and the v0.6.0 default-apply behavior is in effect
        (no ``ANYWHERE_AGENTS_UPDATE`` override).
        """
        return patch.dict(
            os.environ,
            {
                "APPDATA": str(self.root / "appdata"),
                "XDG_CONFIG_HOME": str(self.root / "xdg"),
                "AGENT_CONFIG_PACKS": "",
                "AGENT_CONFIG_RULE_PACKS": "",
            },
            clear=False,
        )

    @contextmanager
    def _no_lock(self):
        from packs import locks
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None
        with patch.object(locks, "acquire", side_effect=_ctx):
            yield

    def _run_canonical_apply(self) -> tuple[int, str, str]:
        """Drive ``_pack_main(["verify", "--fix", "--yes"])`` against
        the fixture.

        End-to-end the canonical apply path subprocesses out to
        ``compose_packs.py``. To keep the test hermetic and avoid
        spawning a Python subprocess that would not see the fixture's
        ``source_fetch`` patches, patch
        ``_invoke_composer_with_gen_fallback`` to invoke
        ``compose_packs.main`` in-process under the same patches the
        out-of-process composer would otherwise need.

        ``_pack_main`` still drives the full verify table + classifier
        flow (``_detect_bundled_default_drift``, BC-guard refinement,
        reconcile, etc.) before the in-process composer call; only the
        subprocess boundary is shimmed.

        Returns ``(rc, stdout, stderr)``. rc=0 expected for the minimal
        fixture; the override fixture may legitimately rc=1 for
        locked-policy fail-closed paths.
        """
        # Drop the v0.6.0 ANYWHERE_AGENTS_UPDATE override so we test the
        # default-apply contract (no explicit env signal).
        prior = os.environ.pop("ANYWHERE_AGENTS_UPDATE", None)
        try:
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            from packs import source_fetch
            import compose_packs

            user_path = self.root / "user-config.yaml"
            user_path.write_text("packs: []\n", encoding="utf-8")

            def _in_process_composer(
                project_root, *composer_args, env_extra=None,
            ):
                """In-process shim for ``_invoke_composer_with_gen_fallback``.

                Threads the source_fetch patches through the composer's
                ``main`` entry point so the canonical apply runs against
                the fake archive. ``env_extra`` is honored by writing
                into ``os.environ`` for the duration of the call so
                ``compose_packs.prompt_user_for_updates`` reads the
                expected default-apply mode.
                """
                argv = ["--root", str(project_root), *composer_args]
                env_overrides: dict[str, str] = {}
                if env_extra:
                    for k, v in env_extra.items():
                        env_overrides[k] = os.environ.get(k, "<UNSET>")
                        os.environ[k] = v
                try:
                    with self._no_lock(), patch.object(
                        source_fetch, "fetch_pack",
                        return_value=self.new_archive,
                    ), patch.object(
                        source_fetch, "load_cached_archive",
                        return_value=self.new_archive,
                    ):
                        return compose_packs.main(argv)
                finally:
                    for k, prev in env_overrides.items():
                        if prev == "<UNSET>":
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = prev

            cwd_before = os.getcwd()
            out_buf, err_buf = io.StringIO(), io.StringIO()
            try:
                os.chdir(self.root)
                with self._env_patch(), self._no_lock(), patch.object(
                    auth, "resolve_ref_with_auth_chain",
                    return_value=(self.new_resolved_commit, "anonymous"),
                ), patch(
                    "anywhere_agents.cli._invoke_composer_with_gen_fallback",
                    side_effect=_in_process_composer,
                ), redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify", "--fix", "--yes"])
            finally:
                os.chdir(cwd_before)
        finally:
            if prior is not None:
                os.environ["ANYWHERE_AGENTS_UPDATE"] = prior
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_minimal_entry_yaml_lock_agents_md_all_converge(self) -> None:
        """Smoke 28 acceptance fixture: minimal auto-reconciled entry
        in ``agent-config.yaml`` (no ``passive`` / ``active`` /
        ``update_policy``) seeded at the STALE ref must NOT block the
        bundled migration. After the canonical apply path runs, all
        three artifacts must converge to the bundled state:

          1. ``agent-config.yaml`` advances from the stale ref to the
             bundled ref. The ``_rewrite_auto_reconciled_default_refs``
             helper in ``cli.py`` (called before ``_verify_gather``)
             rewrites the minimal-entry ref so the verify-gather
             classifier does not mis-classify it as a deliberate pin.
          2. ``pack-lock.json``'s ``requested_ref`` advances to the
             bundled ref. Because the YAML is now at the bundled ref,
             the composer treats the row as auto-reconciled and applies
             the bundled migration.
          3. ``pack-lock.json``'s ``files[*].source_path`` advances to
             ``docs/rule-pack-compact.md`` (the bundled NEW path).
          4. ``AGENTS.md`` reflects the NEW compact content.
          5. ``agent-config.yaml`` does not silently grow synthetic
             ``passive`` / ``active`` shape keys.

        This is the random-project shape: project + lock seeded at
        ``v0.3.2`` while the bundled default has flipped to
        ``v0.3.5``. Pre-Round 2, ``_has_explicit_default_override``
        returned True for any ref deviation, leaving the stale ref
        pinned indefinitely; this test is the regression guard for
        the rewrite-helper fix.
        """
        # Build a STALE-shape minimal entry: ref deviates from bundled
        # but no passive / active / update_policy keys are present.
        agent_config_text = (
            "rule_packs:\n"
            f"  - name: {self.pack_name}\n"
            "    source:\n"
            f"      url: {self.bundled_url}\n"
            f"      ref: {self.stale_project_ref}\n"
        )
        self._build_fixture(
            agent_config_text, project_ref=self.stale_project_ref,
        )
        rc, out, err = self._run_canonical_apply()
        self.assertEqual(
            rc, 0,
            msg=f"canonical apply rc={rc}; stdout={out!r}; stderr={err!r}",
        )

        # (1) ``agent-config.yaml`` advances from stale to bundled ref.
        post_yaml = (self.root / "agent-config.yaml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            f"ref: {self.stale_project_ref}", post_yaml,
            "minimal auto-reconciled entry must have its stale ref "
            "rewritten before verify-gather classifies the row",
        )
        self.assertIn(
            f"ref: {self.bundled_ref}", post_yaml,
            "minimal auto-reconciled entry must advance to the "
            "bundled ref so all three artifacts converge",
        )

        # (2) Lock's ``requested_ref`` advances to the bundled ref.
        from packs import state as state_mod
        new_lock = state_mod.load_pack_lock(
            self.root / ".agent-config" / "pack-lock.json"
        )
        self.assertEqual(
            new_lock["packs"][self.pack_name]["requested_ref"],
            self.bundled_ref,
            "lock requested_ref must advance to bundled ref because "
            "yaml is now at bundled ref after the rewrite-helper run",
        )

        # (3) Lock advances to NEW source_path.
        files = new_lock["packs"][self.pack_name]["files"]
        new_paths = {
            f["source_path"]
            for f in files
            if f.get("role") == "passive" and f.get("source_path")
        }
        self.assertEqual(
            new_paths, {"docs/rule-pack-compact.md"},
            "minimal auto-reconciled entry must NOT block bundled "
            "source_path migration; lock must advance to the NEW path",
        )

        # (4) AGENTS.md reflects NEW compact content.
        agents_md = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(
            "NEW compact content", agents_md,
            "AGENTS.md must re-render from the NEW compact source",
        )
        self.assertNotIn(
            "OLD full-body content", agents_md,
            "AGENTS.md must NOT carry stale OLD content after migration",
        )

        # (5) ``agent-config.yaml`` does not gain stale shape keys.
        # The BC-guard refinement should NOT silently rewrite a minimal
        # entry to add ``passive`` / ``active`` / ``update_policy``
        # blocks. The rewrite-helper only updates the ``ref`` value;
        # it must not promote the entry to an opaque pin with
        # manufactured shape.
        self.assertNotIn(
            "passive:", post_yaml,
            "minimal entry must not gain a synthetic passive: block",
        )
        self.assertNotIn(
            "active:", post_yaml,
            "minimal entry must not gain a synthetic active: block",
        )

    def test_override_preservation_parallel_fixture(self) -> None:
        """Parallel fixture: ``agent-config.yaml`` carries explicit
        user-authored ``passive`` and ``update_policy`` keys (a
        deliberate user pin). After the canonical apply path runs,
        these keys MUST survive unchanged in ``agent-config.yaml``.

        The BC-guard refinement (Phase 3) must NOT silently rewrite a
        genuine user-authored shape override. This fixture is the
        regression guard for that contract: if a future refactor
        broadens the auto-reconciled-minimal classification too
        aggressively and starts treating shape-override entries as
        minimal, this test fails.
        """
        agent_config_text = (
            "rule_packs:\n"
            f"  - name: {self.pack_name}\n"
            "    source:\n"
            f"      url: {self.bundled_url}\n"
            f"      ref: {self.bundled_ref}\n"
            "    update_policy: locked\n"
            "    passive:\n"
            "      - files:\n"
            "          - from: docs/rule-pack.md\n"
            "            to: AGENTS.md\n"
        )
        self._build_fixture(agent_config_text)
        # Stash the pre-run YAML body so we can compare exactly.
        pre_yaml = (self.root / "agent-config.yaml").read_text(
            encoding="utf-8"
        )
        # Before running, make a clean copy of the lock so we know what
        # changed (the locked override may legitimately preserve the
        # OLD source_path; we only verify the YAML-side override
        # survives, which is the Phase 3 contract).
        rc, out, err = self._run_canonical_apply()
        # ``locked`` policy + drift may legitimately fail closed (rc=1)
        # by design (PLAN line 89). Both rc=0 (no-op preserve) and rc=1
        # (locked fail-closed delta) are valid outcomes. The critical
        # invariant is that the YAML-side override keys SURVIVE the
        # run, regardless of rc.
        self.assertIn(
            rc, (0, 1),
            msg=(
                f"override fixture rc={rc} unexpected (expected 0 or 1); "
                f"stdout={out!r}; stderr={err!r}"
            ),
        )

        post_yaml = (self.root / "agent-config.yaml").read_text(
            encoding="utf-8"
        )
        # Critical Phase 3 invariant: explicit user-authored shape
        # override keys must survive byte-for-byte.
        self.assertIn(
            "update_policy: locked", post_yaml,
            "user-authored update_policy must survive canonical apply",
        )
        self.assertIn(
            "passive:", post_yaml,
            "user-authored passive: block must survive canonical apply",
        )
        self.assertIn(
            "from: docs/rule-pack.md", post_yaml,
            "user-authored from-path override must survive canonical apply",
        )
        # Additional safety: the post-yaml should not have lost any of
        # the explicit-override keys present pre-run. (Stronger
        # invariant than individual key presence.)
        for key_marker in (
            "update_policy: locked",
            "passive:",
            "from: docs/rule-pack.md",
            "to: AGENTS.md",
        ):
            self.assertIn(
                key_marker, pre_yaml,
                f"sanity: pre_yaml must seed {key_marker!r}",
            )
            self.assertIn(
                key_marker, post_yaml,
                f"override {key_marker!r} must survive the run",
            )

    def test_minimal_entry_with_unrecognized_ref_is_preserved_as_pin(self) -> None:
        """v0.6.0 deep-review Round 3 over-fire guard: a minimal
        default-pack row is NOT automatically aa residue for every old
        ref. Only refs in the explicit ``_AUTO_RECONCILED_DEFAULT_REF_REWRITES``
        allow-list may be advanced; any other minimal default-name ref
        is a deliberate user pin and must survive byte-for-byte (per
        pack-architecture.md:678 BC-guard contract).

        Probe shape: ``agent-style v0.2.0`` with no shape / policy keys.
        Even though it matches "minimal default-pack name + URL",
        ``v0.2.0`` is NOT in the allow-list (only ``v0.3.2`` is the
        known aa-reconciliation residue), so the helper must leave it
        alone.
        """
        pinned_ref = "v0.2.0"
        agent_config_text = (
            "packs:\n"
            f"  - name: {self.pack_name}\n"
            "    source:\n"
            f"      url: {self.bundled_url}\n"
            f"      ref: {pinned_ref}\n"
        )
        self._build_fixture(agent_config_text, project_ref=pinned_ref)

        from anywhere_agents import cli

        before = (self.root / "agent-config.yaml").read_text(encoding="utf-8")
        rewritten = cli._rewrite_auto_reconciled_default_refs(self.root)
        after = (self.root / "agent-config.yaml").read_text(encoding="utf-8")

        self.assertEqual(rewritten, set())
        self.assertEqual(after, before)


# =====================================================================
# Host-aware verify-seeding (v0.6.0 post-review fix).
# =====================================================================


class TestVerifySeedHostAware(unittest.TestCase):
    """``_default_v2_seed_for_host`` filters claude-only bundled
    defaults out of the verify-seed under non-claude hosts so a fresh
    codex consumer does not see aa-core-skills classified as
    "expected but missing" forever.

    Companion to the compose-side ``_default_v2_selections_for_host``
    in ``test_compose_packs_v0_6.py``; together they ensure verify and
    compose agree on what the host is supposed to deploy.
    """

    def test_active_host_defaults_to_claude_code(self) -> None:
        """Empty AGENT_CONFIG_HOST → claude-code (matches
        ``compose_packs.detect_host`` default-fallback)."""
        from anywhere_agents import cli
        with patch.dict(os.environ, {"AGENT_CONFIG_HOST": ""}):
            self.assertEqual(cli._active_host(), "claude-code")

    def test_active_host_reads_codex_env(self) -> None:
        """AGENT_CONFIG_HOST=codex is consulted at verify time."""
        from anywhere_agents import cli
        with patch.dict(os.environ, {"AGENT_CONFIG_HOST": "codex"}):
            self.assertEqual(cli._active_host(), "codex")

    def test_active_host_falls_back_on_unknown(self) -> None:
        """Unknown env value falls back to claude-code rather than
        propagating an unrecognized host that would mis-filter the
        seed."""
        from anywhere_agents import cli
        with patch.dict(os.environ, {"AGENT_CONFIG_HOST": "garbage"}):
            self.assertEqual(cli._active_host(), "claude-code")

    def test_seed_for_claude_code_includes_both_defaults(self) -> None:
        """Under claude-code the seed carries the full v0.6.0 default
        list (agent-style + aa-core-skills)."""
        from anywhere_agents import cli
        seed = cli._default_v2_seed_for_host("claude-code")
        self.assertEqual(set(seed), {"agent-style", "aa-core-skills"})

    def test_seed_for_codex_drops_aa_core_skills(self) -> None:
        """Under codex the seed drops aa-core-skills (the only
        claude-only v0.6.0 default); agent-style survives."""
        from anywhere_agents import cli
        seed = cli._default_v2_seed_for_host("codex")
        self.assertEqual(set(seed), {"agent-style"})

    def test_full_default_tuple_unchanged_for_identity_checks(self) -> None:
        """``_DEFAULT_V2_SELECTIONS`` is consulted by identity-check
        and BC-guard call sites elsewhere in cli.py. It must keep both
        names regardless of host so a user-pinned aa-core-skills row
        under codex still resolves to the synthetic bundled identity
        rather than the sourceless-sentinel path."""
        from anywhere_agents import cli
        self.assertIn("agent-style", cli._DEFAULT_V2_SELECTIONS)
        self.assertIn("aa-core-skills", cli._DEFAULT_V2_SELECTIONS)


class TestLoadProjectObservationsHostAware(unittest.TestCase):
    """Integration: ``_load_project_observations`` materializes the
    project-side identity list. Under codex with no project signal,
    aa-core-skills must not appear; under claude-code it must.

    Pre-fix v0.6.0 behavior: codex consumers got aa-core-skills in
    the project view and verify reported "missing" because the
    composer (correctly) refused to deploy a claude-only pack on
    codex; the resulting state was unhealable except via env-subtract
    workaround.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_observations_under_codex_exclude_claude_only(self) -> None:
        from anywhere_agents import cli
        with patch.dict(os.environ, {"AGENT_CONFIG_HOST": "codex"}):
            obs = cli._load_project_observations(self.root)
        names = {ident[0] for ident in obs}
        self.assertIn("agent-style", names)
        self.assertNotIn("aa-core-skills", names)

    def test_observations_under_claude_code_include_both(self) -> None:
        from anywhere_agents import cli
        with patch.dict(os.environ, {"AGENT_CONFIG_HOST": "claude-code"}):
            obs = cli._load_project_observations(self.root)
        names = {ident[0] for ident in obs}
        self.assertIn("agent-style", names)
        self.assertIn("aa-core-skills", names)

    def test_observations_under_codex_with_explicit_pin_keeps_pin(self) -> None:
        """A user who explicitly pins aa-core-skills under codex
        (knowing they are taking responsibility for the host-mismatch
        consequence) still surfaces in the project observations — the
        host-gate only filters the auto-seed, not user input."""
        from anywhere_agents import cli
        (self.root / "agent-config.yaml").write_text(
            "packs:\n  - name: aa-core-skills\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"AGENT_CONFIG_HOST": "codex"}):
            obs = cli._load_project_observations(self.root)
        names = {ident[0] for ident in obs}
        # User-pinned aa-core-skills survives even though the auto-seed
        # would have dropped it. agent-style auto-seed still applies.
        self.assertIn("aa-core-skills", names)
        self.assertIn("agent-style", names)


if __name__ == "__main__":
    unittest.main()
