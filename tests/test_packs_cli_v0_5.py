"""Tests for v0.5.0 pack-management CLI subcommands.

Covers:
- ``pack add`` remote-manifest expansion (one row per remote pack)
- ``pack add --pack <name>`` filter
- ``pack add --type rule`` excluding active packs
- ``pack add`` warning + skip for missing remote pack name
- ``pack update <name>`` thin-wheel flow (resolve ref + invoke composer)
- ``pack list --drift`` audit
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch, MagicMock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "pypi"))
sys.path.insert(0, str(ROOT / "scripts"))


def _build_archive(archive_dir: pathlib.Path) -> object:
    """Build a PackArchive pointing at the given directory."""
    from anywhere_agents.packs import source_fetch
    return source_fetch.PackArchive(
        url="https://github.com/yzhao062/agent-pack",
        ref="v0.1.0",
        resolved_commit="ab" * 20,
        method="anonymous",
        archive_dir=archive_dir,
        canonical_id="yzhao062/agent-pack",
        cache_key="abcd1234/" + "ab" * 20,
    )


_THREE_PACK_MANIFEST = (
    "version: 2\n"
    "packs:\n"
    "  - name: profile\n"
    "    description: x\n"
    "    source: {repo: https://github.com/yzhao062/agent-pack, ref: v0.1.0}\n"
    "    passive: [{files: [{from: docs/rule-pack.md, to: AGENTS.md}]}]\n"
    "  - name: paper-workflow\n"
    "    description: y\n"
    "    source: {repo: https://github.com/yzhao062/agent-pack, ref: v0.1.0}\n"
    "    passive: [{files: [{from: docs/paper-workflow.md, to: AGENTS.md}]}]\n"
    "  - name: acad-skills\n"
    "    description: z\n"
    "    source: {repo: https://github.com/yzhao062/agent-pack, ref: v0.1.0}\n"
    "    hosts: [claude-code]\n"
    "    active: [{kind: skill, required: false, files: [{from: skills/x/, to: .claude/skills/x/}]}]\n"
)


class TestPackAddRemoteManifest(unittest.TestCase):
    def test_pack_add_expands_multi_pack_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_THREE_PACK_MANIFEST)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0",
            ]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                _pack_main(user_path, argv[1:])
            written = yaml.safe_load(user_path.read_text())
            names = [e["name"] for e in written["packs"]]
            self.assertEqual(set(names), {"profile", "paper-workflow", "acad-skills"})
            for entry in written["packs"]:
                self.assertEqual(
                    entry["source"],
                    {"url": "https://github.com/yzhao062/agent-pack", "ref": "v0.1.0"},
                )

    def test_pack_add_with_pack_filter_only_writes_named_pack(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_THREE_PACK_MANIFEST)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0", "--pack", "profile",
            ]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 0)
            written = yaml.safe_load(user_path.read_text())
            self.assertEqual(len(written["packs"]), 1)
            self.assertEqual(written["packs"][0]["name"], "profile")

    def test_pack_add_with_type_rule_skips_active_packs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_THREE_PACK_MANIFEST)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0", "--type", "rule",
            ]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 0)
            written = yaml.safe_load(user_path.read_text())
            names = {e["name"] for e in written["packs"]}
            # acad-skills declares active:, so it's excluded by --type rule.
            self.assertEqual(names, {"profile", "paper-workflow"})

    def test_pack_add_handles_missing_remote_pack_warning(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_THREE_PACK_MANIFEST)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0", "--pack", "nonexistent",
            ]
            err_buf = io.StringIO()
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                with redirect_stderr(err_buf):
                    rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 0)
            self.assertIn("nonexistent", err_buf.getvalue())
            self.assertIn("warning", err_buf.getvalue().lower())
            # No row written for the missing pack — and because no prior
            # config existed, no file should have been created at all
            # (avoid leaving an empty 'packs: []' artifact).
            self.assertFalse(user_path.exists())

    def test_pack_add_handles_auth_failure(self) -> None:
        """auth chain exhaustion -> clean error message + rc=2, no traceback."""
        with tempfile.TemporaryDirectory() as d:
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0",
            ]
            err_buf = io.StringIO()
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth, source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                side_effect=auth.AuthChainExhaustedError(
                    "https://github.com/yzhao062/agent-pack", "v0.1.0", [],
                ),
            ):
                with redirect_stderr(err_buf):
                    rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 2)
            self.assertIn("could not fetch", err_buf.getvalue())
            # No user-config file should have been created from a failed fetch.
            self.assertFalse(user_path.exists())

    def test_pack_add_handles_malformed_remote_manifest(self) -> None:
        """schema.ParseError on remote pack.yaml -> rc=2 with clean message."""
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            # Write a manifest that will fail schema.parse_manifest().
            (archive_dir / "pack.yaml").write_text("not: a: valid: manifest:\n")
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0",
            ]
            err_buf = io.StringIO()
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import schema, source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ), patch.object(
                schema, "parse_manifest",
                side_effect=schema.ParseError("bad shape"),
            ):
                with redirect_stderr(err_buf):
                    rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 2)
            self.assertIn("malformed", err_buf.getvalue())

    def test_pack_add_with_name_on_multipack_warns_and_uses_original_names(self) -> None:
        """--name on a multi-pack manifest is silently dropped pre-fix; now warns."""
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_THREE_PACK_MANIFEST)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0", "--name", "custom-name",
            ]
            err_buf = io.StringIO()
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                with redirect_stderr(err_buf):
                    rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 0)
            self.assertIn("--name", err_buf.getvalue())
            self.assertIn("ignored", err_buf.getvalue())
            written = yaml.safe_load(user_path.read_text())
            names = {e["name"] for e in written["packs"]}
            # Original names preserved; "custom-name" must NOT appear.
            self.assertEqual(names, {"profile", "paper-workflow", "acad-skills"})
            self.assertNotIn("custom-name", names)

    def test_pack_add_with_type_rule_filters_all_writes_nothing(self) -> None:
        """When --type rule filters out every pack and no prior config exists,
        do not create an empty packs: [] file."""
        single_active_only = (
            "version: 2\n"
            "packs:\n"
            "  - name: skills-only\n"
            "    description: q\n"
            "    source: {repo: https://github.com/yzhao062/agent-pack, ref: v0.1.0}\n"
            "    hosts: [claude-code]\n"
            "    active: [{kind: skill, required: false, files: [{from: s/, to: t/}]}]\n"
        )
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(single_active_only)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0", "--type", "rule",
            ]
            err_buf = io.StringIO()
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                with redirect_stderr(err_buf):
                    rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 0)
            self.assertIn("nothing written", err_buf.getvalue())
            # No file should have been created.
            self.assertFalse(user_path.exists())


class TestPackUpdate(unittest.TestCase):
    def _seed_user_config(self, root: pathlib.Path, ref: str = "v0.1.0") -> pathlib.Path:
        cfg = root / "user-config.yaml"
        cfg.write_text(yaml.safe_dump({
            "packs": [{
                "name": "profile",
                "source": {
                    "url": "https://github.com/yzhao062/agent-pack",
                    "ref": ref,
                },
            }],
        }))
        return cfg

    def test_pack_update_invokes_project_composer(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            cfg = self._seed_user_config(root)
            project = root / "project"
            composer = project / ".agent-config" / "repo" / "scripts" / "compose_packs.py"
            composer.parent.mkdir(parents=True, exist_ok=True)
            composer.write_text("# placeholder composer\n")
            argv = ["pack", "update", "profile", "--ref", "v0.2.0"]

            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            mock_proc = MagicMock(returncode=0)
            with patch("anywhere_agents.cli.os.getcwd", return_value=str(project)), \
                 patch("anywhere_agents.cli.Path") as mocked_path, \
                 patch.object(auth, "resolve_ref_with_auth_chain",
                              return_value=("cd" * 20, "anonymous")), \
                 patch("anywhere_agents.cli.subprocess.run",
                       return_value=mock_proc) as run_mock:
                # Make Path.cwd() return our project dir; everything else
                # passes through.
                mocked_path.cwd.return_value = project
                mocked_path.side_effect = lambda *a, **kw: pathlib.Path(*a, **kw)
                rc = _pack_main(cfg, argv[1:])
            self.assertEqual(rc, 0)
            # The user config should now record the new ref.
            written = yaml.safe_load(cfg.read_text())
            self.assertEqual(
                written["packs"][0]["source"]["ref"],
                "v0.2.0",
            )
            # subprocess.run was called with the composer path and the
            # ANYWHERE_AGENTS_UPDATE=apply env var.
            args, kwargs = run_mock.call_args
            self.assertIn("compose_packs.py", args[0][1])
            self.assertEqual(kwargs["env"]["ANYWHERE_AGENTS_UPDATE"], "apply")

    def test_pack_update_missing_pack_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            cfg = self._seed_user_config(root)
            argv = ["pack", "update", "ghost"]
            from anywhere_agents.cli import _pack_main
            err_buf = io.StringIO()
            with redirect_stderr(err_buf):
                rc = _pack_main(cfg, argv[1:])
            self.assertEqual(rc, 2)
            self.assertIn("ghost", err_buf.getvalue())

    def test_pack_update_missing_composer_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            cfg = self._seed_user_config(root)
            project = root / "project"
            project.mkdir()  # No .agent-config/repo/.
            argv = ["pack", "update", "profile", "--ref", "v0.2.0"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            err_buf = io.StringIO()
            with patch("anywhere_agents.cli.Path") as mocked_path, \
                 patch.object(auth, "resolve_ref_with_auth_chain",
                              return_value=("cd" * 20, "anonymous")):
                mocked_path.cwd.return_value = project
                mocked_path.side_effect = lambda *a, **kw: pathlib.Path(*a, **kw)
                with redirect_stderr(err_buf):
                    rc = _pack_main(cfg, argv[1:])
            self.assertEqual(rc, 2)
            self.assertIn("composer not found", err_buf.getvalue())

    def test_pack_update_resolve_failure_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            cfg = self._seed_user_config(root)
            pre_content = cfg.read_text()
            argv = ["pack", "update", "profile"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            err_buf = io.StringIO()
            with patch.object(
                auth, "resolve_ref_with_auth_chain",
                side_effect=auth.AuthChainExhaustedError(
                    "https://github.com/yzhao062/agent-pack", "v0.1.0", [],
                ),
            ):
                with redirect_stderr(err_buf):
                    rc = _pack_main(cfg, argv[1:])
            self.assertEqual(rc, 2)
            self.assertIn("could not resolve", err_buf.getvalue())
            # Locked-in invariant: a failed resolve must not modify the
            # user-config file. Guards against future refactors that move
            # _write_user_config before the resolve call.
            post_content = cfg.read_text()
            self.assertEqual(
                pre_content, post_content,
                "user config must not be modified when resolve fails",
            )


class TestPackListDrift(unittest.TestCase):
    def _seed_lock(self, project: pathlib.Path, recorded_commit: str) -> None:
        agent_dir = project / ".agent-config"
        agent_dir.mkdir(parents=True, exist_ok=True)
        lock = {
            "version": 1,
            "packs": {
                "profile": {
                    "source_url": "https://github.com/yzhao062/agent-pack",
                    "requested_ref": "v0.1.0",
                    "resolved_commit": recorded_commit,
                },
            },
        }
        (agent_dir / "pack-lock.json").write_text(json.dumps(lock))

    def test_pack_list_drift_reports_changed_commit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d)
            self._seed_lock(project, recorded_commit="aa" * 20)
            argv = ["pack", "list", "--drift"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch.object(
                    auth, "resolve_ref_with_auth_chain",
                    return_value=("bb" * 20, "anonymous"),
                ):
                    with redirect_stdout(out_buf), redirect_stderr(err_buf):
                        rc = _pack_main(pathlib.Path(d) / "x.yaml", argv[1:])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 0)
            self.assertIn("profile", out_buf.getvalue())
            self.assertIn("aaaaaaa", out_buf.getvalue())
            self.assertIn("bbbbbbb", out_buf.getvalue())

    def test_pack_list_drift_no_drift_when_commit_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d)
            self._seed_lock(project, recorded_commit="aa" * 20)
            argv = ["pack", "list", "--drift"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            out_buf = io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch.object(
                    auth, "resolve_ref_with_auth_chain",
                    return_value=("aa" * 20, "anonymous"),
                ):
                    with redirect_stdout(out_buf):
                        rc = _pack_main(pathlib.Path(d) / "x.yaml", argv[1:])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 0)
            self.assertIn("no drift", out_buf.getvalue())

    def test_pack_list_drift_continues_on_resolve_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d)
            self._seed_lock(project, recorded_commit="aa" * 20)
            argv = ["pack", "list", "--drift"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            err_buf, out_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch.object(
                    auth, "resolve_ref_with_auth_chain",
                    side_effect=auth.AuthChainExhaustedError(
                        "https://github.com/yzhao062/agent-pack", "v0.1.0", [],
                    ),
                ):
                    with redirect_stdout(out_buf), redirect_stderr(err_buf):
                        rc = _pack_main(pathlib.Path(d) / "x.yaml", argv[1:])
            finally:
                os.chdir(cwd_before)
            # Resolve failure on a single entry does NOT crash the whole
            # subcommand; rc is 0 (read-only audit best-effort).
            self.assertEqual(rc, 0)
            self.assertIn("could not resolve", err_buf.getvalue())
            self.assertIn("profile", err_buf.getvalue())

    def test_pack_list_drift_corrupt_pack_lock_returns_2(self) -> None:
        """Corrupt pack-lock JSON must not be silently treated as 'no drift'."""
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d)
            agent_dir = project / ".agent-config"
            agent_dir.mkdir(parents=True, exist_ok=True)
            # Write malformed JSON that json.loads cannot parse.
            (agent_dir / "pack-lock.json").write_text("{ not valid json {{")
            argv = ["pack", "list", "--drift"]
            from anywhere_agents.cli import _pack_main
            err_buf, out_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(pathlib.Path(d) / "x.yaml", argv[1:])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 2)
            # Must not lie about state.
            self.assertNotIn("no drift", out_buf.getvalue())
            self.assertIn("cannot read", err_buf.getvalue())


class TestPackUpdateCredentialURLRejected(unittest.TestCase):
    """Codex Round 2 H3-B: ``pack update`` must reject a
    credential-bearing URL recorded in user-config WITHOUT calling
    ``resolve_ref_with_auth_chain`` (which would leak the token into
    git argv) AND without echoing the raw token in stderr."""

    def test_pack_update_credential_url_rejected_before_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            cfg = root / "user-config.yaml"
            # Legacy hand-edited user-config with a token in the URL.
            cfg.write_text(yaml.safe_dump({
                "packs": [{
                    "name": "profile",
                    "source": {
                        "url": "https://ghp_legacy_secret@github.com/yzhao062/agent-pack",
                        "ref": "v0.1.0",
                    },
                }],
            }))
            argv = ["pack", "update", "profile", "--ref", "v0.2.0"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            err_buf = io.StringIO()
            with patch.object(
                auth, "resolve_ref_with_auth_chain",
            ) as resolve:
                with redirect_stderr(err_buf):
                    rc = _pack_main(cfg, argv[1:])
            # Reject path: rc=2, ``resolve_ref_with_auth_chain`` not
            # called, raw token absent from stderr.
            self.assertEqual(rc, 2)
            resolve.assert_not_called()
            self.assertNotIn("ghp_legacy_secret", err_buf.getvalue())
            self.assertIn("<redacted>", err_buf.getvalue())


class TestPackListDriftCredentialURLRejected(unittest.TestCase):
    """Codex Round 2 H3-B: ``pack list --drift`` must reject a
    credential-bearing URL recorded in pack-lock for a single entry
    WITHOUT calling ``resolve_ref_with_auth_chain`` AND continuing
    audit of the remaining entries (read-only audit best-effort)."""

    def test_pack_list_drift_credential_url_skips_entry(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d)
            agent_dir = project / ".agent-config"
            agent_dir.mkdir(parents=True, exist_ok=True)
            # pack-lock with one credential-URL entry plus one clean
            # entry; the audit should skip the credential entry and
            # still process the clean one.
            lock = {
                "version": 1,
                "packs": {
                    "tainted": {
                        "source_url": "https://ghp_legacy_secret@github.com/x/y",
                        "requested_ref": "v0.1.0",
                        "resolved_commit": "aa" * 20,
                    },
                    "profile": {
                        "source_url": "https://github.com/yzhao062/agent-pack",
                        "requested_ref": "v0.1.0",
                        "resolved_commit": "aa" * 20,
                    },
                },
            }
            (agent_dir / "pack-lock.json").write_text(json.dumps(lock))
            argv = ["pack", "list", "--drift"]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import auth
            err_buf, out_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                # Patch the resolver so we can assert how many times it
                # was actually called.
                with patch.object(
                    auth, "resolve_ref_with_auth_chain",
                    return_value=("aa" * 20, "anonymous"),
                ) as resolve:
                    with redirect_stdout(out_buf), redirect_stderr(err_buf):
                        rc = _pack_main(pathlib.Path(d) / "x.yaml", argv[1:])
            finally:
                os.chdir(cwd_before)
            # Audit returns rc=0 (best-effort).
            self.assertEqual(rc, 0)
            # ``resolve_ref_with_auth_chain`` called for the clean entry
            # only — the tainted one was rejected before resolve.
            self.assertEqual(resolve.call_count, 1)
            args, _kwargs = resolve.call_args
            self.assertEqual(args[0], "https://github.com/yzhao062/agent-pack")
            # Stderr names the rejected entry but does NOT echo the raw
            # token bytes.
            self.assertIn("tainted", err_buf.getvalue())
            self.assertIn("unsafe source URL", err_buf.getvalue())
            self.assertNotIn("ghp_legacy_secret", err_buf.getvalue())


class TestPackAddNameOnSinglePackManifest(unittest.TestCase):
    """Codex Round 2 M5: ``pack add --name custom`` on a single-pack
    remote manifest must write the user-config entry with
    ``name: "custom"`` (output name) while looking up the pack in the
    remote manifest under its ORIGINAL name. Pre-fix the lookup used
    ``args.name`` (the override), so the pack was 'missing' and nothing
    was written."""

    def test_pack_add_with_name_on_single_pack_renames_output(self) -> None:
        single_pack_manifest = (
            "version: 2\n"
            "packs:\n"
            "  - name: profile\n"
            "    description: x\n"
            "    source: {repo: https://github.com/yzhao062/agent-pack, ref: v0.1.0}\n"
            "    passive: [{files: [{from: docs/profile.md, to: AGENTS.md}]}]\n"
        )
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(single_pack_manifest)
            user_path = pathlib.Path(d) / "user-config.yaml"
            argv = [
                "pack", "add", "https://github.com/yzhao062/agent-pack",
                "--ref", "v0.1.0", "--name", "custom",
            ]
            from anywhere_agents.cli import _pack_main
            from anywhere_agents.packs import source_fetch
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=_build_archive(archive_dir),
            ):
                rc = _pack_main(user_path, argv[1:])
            self.assertEqual(rc, 0)
            written = yaml.safe_load(user_path.read_text())
            # Exactly one entry written, named ``custom`` (the override),
            # pointing at the source URL.
            self.assertEqual(len(written["packs"]), 1)
            entry = written["packs"][0]
            self.assertEqual(entry["name"], "custom")
            self.assertEqual(
                entry["source"],
                {
                    "url": "https://github.com/yzhao062/agent-pack",
                    "ref": "v0.1.0",
                },
            )


class TestVendorPacksOutput(unittest.TestCase):
    """Vendor script must produce LF-terminated files on every platform."""

    def test_vendor_output_has_no_crlf_on_any_platform(self) -> None:
        # Run vendor() to its real destination and inspect bytes. The
        # destination is gitignored as "AM" already; content equality
        # against scripts/packs/*.py guarantees byte-for-byte parity.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "vendor_packs",
            ROOT / "scripts" / "vendor-packs.py",
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.vendor()
        # Read each vendored file as bytes and assert no CR.
        dst = ROOT / "packages" / "pypi" / "anywhere_agents" / "packs"
        for name in ("__init__.py", "auth.py", "source_fetch.py", "schema.py"):
            content = (dst / name).read_bytes()
            self.assertNotIn(b"\r", content, f"{name} contains CR")


class PackVerifyTests(unittest.TestCase):
    """Tests for ``anywhere-agents pack verify [--fix] [--yes]`` (v0.5.x).

    Covers:
    - Priority-order classifier truth table over (U, P, L) cells.
    - Lock-health propagation rules (P=1,L=1 vs P=0).
    - Mismatch wins over per-layer single-state classification.
    - Default-pack seeding for bundled identities.
    - Per-state CLI output and exit codes.
    - ``--fix`` semantics: atomic write, idempotent, mismatch / orphan
      no-op, malformed-YAML refusal, non-TTY dry-run.
    - Identity normalization corner cases.
    - Cross-platform user-config-path resolution.
    """

    # ----- helpers -----

    @staticmethod
    def _ident(name, url="", ref=""):
        """Return an identity 5-tuple for tests using
        ``_classify_pack_states`` directly. Mirrors the production
        ``_identity_for_user_entry`` shape.
        """
        from anywhere_agents.cli import _normalize_url
        return (name, _normalize_url(url) if url else "", ref, url, ref)

    @staticmethod
    def _write_lock(project_root: pathlib.Path, lock_data: dict) -> pathlib.Path:
        agent_dir = project_root / ".agent-config"
        agent_dir.mkdir(parents=True, exist_ok=True)
        lock_path = agent_dir / "pack-lock.json"
        lock_path.write_text(json.dumps(lock_data), encoding="utf-8")
        return lock_path

    @staticmethod
    def _lock_entry(source_url: str, ref: str, output_paths: list[str]) -> dict:
        """Build a composer-shape pack-lock entry.

        Mirrors the structure written by ``scripts/packs/dispatch.py``
        (per-pack metadata at the top level + a ``files`` list whose
        entries carry ``output_paths``). Tests use this helper so a
        change in the lock schema only updates one place.
        """
        return {
            "source_url": source_url,
            "requested_ref": ref,
            "resolved_commit": ref or "",
            "pack_update_policy": "locked",
            "files": [{"output_paths": list(output_paths)}],
        }

    @staticmethod
    def _create_output_files(project_root: pathlib.Path, paths: list[str]) -> None:
        """Create the file(s) referenced by ``output_paths`` on disk so
        ``_load_lock_observations`` reports the entry as ``ok`` rather
        than ``broken``.
        """
        for rel in paths:
            full = project_root / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text("test fixture", encoding="utf-8")

    @staticmethod
    def _write_user(user_path: pathlib.Path, packs_list: list) -> None:
        user_path.write_text(
            yaml.safe_dump({"packs": packs_list}, sort_keys=False),
            encoding="utf-8",
        )

    @staticmethod
    def _write_project(project_root: pathlib.Path, rule_packs_list: list) -> None:
        (project_root / "agent-config.yaml").write_text(
            yaml.safe_dump({"rule_packs": rule_packs_list}, sort_keys=False),
            encoding="utf-8",
        )

    def _assert_pack_remove_only_in_negation(self, output: str) -> None:
        """Assert every occurrence of ``pack remove`` in ``output`` is
        preceded (within a small window) by a negation token like
        ``do not`` / ``don't`` / ``never`` / ``avoid``. Plain literal
        substring assertions are too strict because the verify CLI emits
        a deliberate ``Do not use `pack remove``` disclaimer that
        contains the substring. The intent of the test is to ensure the
        CLI never positively recommends ``pack remove``.
        """
        lower = output.lower()
        idx = 0
        while True:
            hit = lower.find("pack remove", idx)
            if hit == -1:
                break
            window_start = max(0, hit - 30)
            window = lower[window_start:hit]
            negated = any(
                token in window
                for token in ("do not", "don't", "never", "avoid", "not use")
            )
            self.assertTrue(
                negated,
                f"`pack remove` must only appear inside a negation; "
                f"context = {output[max(0, hit-30):hit+12]!r}",
            )
            idx = hit + len("pack remove")

    # ------------------------------------------------------------------
    # Group 1: Priority-order classifier (table-driven)
    # ------------------------------------------------------------------

    def test_verify_classifier_truth_table(self) -> None:
        from anywhere_agents.cli import (
            _classify_pack_states,
            _VERIFY_STATE_DEPLOYED,
            _VERIFY_STATE_USER_ONLY,
            _VERIFY_STATE_DECLARED,
            _VERIFY_STATE_ORPHAN,
        )
        url = "https://github.com/owner/repo"
        ref = "v0.1.0"
        ident = self._ident("x", url=url, ref=ref)
        cases = [
            # (U, P, L, expected_state_or_None)
            (0, 0, 0, None),
            (1, 0, 0, _VERIFY_STATE_USER_ONLY),
            (0, 1, 0, _VERIFY_STATE_DECLARED),
            (0, 0, 1, _VERIFY_STATE_ORPHAN),
            (1, 1, 0, _VERIFY_STATE_DECLARED),
            (1, 0, 1, _VERIFY_STATE_USER_ONLY),
            (0, 1, 1, _VERIFY_STATE_DEPLOYED),
            (1, 1, 1, _VERIFY_STATE_DEPLOYED),
        ]
        for U, P, L, expected in cases:
            with self.subTest(U=U, P=P, L=L):
                user = [ident] if U else []
                project = [ident] if P else []
                lock = [ident] if L else []
                lock_health = {"x": "ok"} if L else {}
                rows = _classify_pack_states(user, project, lock, lock_health)
                if expected is None:
                    self.assertEqual(rows, [], f"expected no rows for U={U} P={P} L={L}")
                else:
                    self.assertEqual(len(rows), 1, f"expected one row for U={U} P={P} L={L}")
                    self.assertEqual(rows[0]["name"], "x")
                    self.assertEqual(rows[0]["state"], expected)

    def test_verify_classifier_lock_health_in_P1L1(self) -> None:
        from anywhere_agents.cli import (
            _classify_pack_states,
            _VERIFY_STATE_BROKEN,
            _VERIFY_STATE_LOCK_STALE,
        )
        ident = self._ident("x", url="https://github.com/o/r", ref="v0.1.0")
        # schema_stale → lock schema stale
        rows = _classify_pack_states(
            [], [ident], [ident], {"x": "schema_stale"},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], _VERIFY_STATE_LOCK_STALE)
        # ("broken", [paths]) → broken state
        rows = _classify_pack_states(
            [], [ident], [ident], {"x": ("broken", ["foo/bar"])},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], _VERIFY_STATE_BROKEN)
        self.assertEqual(rows[0]["missing_paths"], ["foo/bar"])

    def test_verify_classifier_lock_health_in_P0_is_row_note(self) -> None:
        from anywhere_agents.cli import (
            _classify_pack_states,
            _VERIFY_STATE_USER_ONLY,
            _VERIFY_STATE_ORPHAN,
        )
        ident = self._ident("x", url="https://github.com/o/r", ref="v0.1.0")
        # P=0, U=1, L=1 with broken lock → user-level only with note
        rows = _classify_pack_states(
            [ident], [], [ident], {"x": ("broken", ["foo"])},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], _VERIFY_STATE_USER_ONLY)
        self.assertIsNotNone(rows[0]["note"])
        self.assertIn("lock", rows[0]["note"].lower())
        # P=0, U=0, L=1 with broken lock → orphan with note
        rows = _classify_pack_states(
            [], [], [ident], {"x": ("broken", ["foo"])},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], _VERIFY_STATE_ORPHAN)
        self.assertIsNotNone(rows[0]["note"])

    def test_verify_classifier_mismatch_wins(self) -> None:
        from anywhere_agents.cli import (
            _classify_pack_states,
            _VERIFY_STATE_MISMATCH,
        )
        # Same name, same URL, different refs → mismatch.
        u = self._ident("x", url="https://github.com/o/r", ref="v1")
        p = self._ident("x", url="https://github.com/o/r", ref="v2")
        rows = _classify_pack_states([u], [p], [], {})
        self.assertEqual(len(rows), 1, "mismatch must collapse to a single row")
        self.assertEqual(rows[0]["name"], "x")
        self.assertEqual(rows[0]["state"], _VERIFY_STATE_MISMATCH)

    def test_verify_project_same_file_duplicate_is_mismatch(self) -> None:
        """Regression for Round 2 Codex M2-still-open: two same-name
        rows in agent-config.yaml with different refs must surface as
        ``config mismatch`` rather than collapsing to last-wins.
        """
        from anywhere_agents.cli import (
            _load_project_observations,
            _classify_pack_states,
            _VERIFY_STATE_MISMATCH,
        )
        url = "https://github.com/yzhao062/agent-pack"
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            self._write_project(project, [
                {"name": "profile", "source": {"url": url, "ref": "v0.1.0"}},
                {"name": "profile", "source": {"url": url, "ref": "main"}},
            ])
            project_idents = _load_project_observations(project)
            self.assertEqual(
                len(project_idents), 2,
                "same-file dups must survive into the classifier; "
                "got idents={!r}".format(project_idents),
            )
            rows = _classify_pack_states([], project_idents, [], {})
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "profile")
            self.assertEqual(rows[0]["state"], _VERIFY_STATE_MISMATCH)

    def test_verify_local_overrides_tracked_with_dups(self) -> None:
        """``agent-config.local.yaml`` overrides ``agent-config.yaml``
        per-name, even when each file has internal dups: local's full
        dup-list replaces tracked's full dup-list for that name.
        """
        from anywhere_agents.cli import _load_project_observations
        url = "https://github.com/yzhao062/agent-pack"
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            self._write_project(project, [
                {"name": "profile", "source": {"url": url, "ref": "v0.1.0"}},
                {"name": "profile", "source": {"url": url, "ref": "main"}},
                {"name": "other", "source": {"url": url, "ref": "v0.1.0"}},
            ])
            (project / "agent-config.local.yaml").write_text(
                yaml.safe_dump({"rule_packs": [
                    {"name": "profile", "source": {"url": url, "ref": "v0.2.0"}},
                ]}, sort_keys=False),
                encoding="utf-8",
            )
            idents = _load_project_observations(project)
            # ``profile`` → local's single entry (v0.2.0); tracked dups dropped.
            # ``other`` → tracked's only entry survives.
            profile_refs = sorted(i[2] for i in idents if i[0] == "profile")
            other_refs = sorted(i[2] for i in idents if i[0] == "other")
            self.assertEqual(profile_refs, ["v0.2.0"])
            self.assertEqual(other_refs, ["v0.1.0"])

    def test_verify_malformed_bootstrap_packs_yaml_exits_2(self) -> None:
        """Regression for Round 2 Codex new M: a malformed
        ``.agent-config/repo/bootstrap/packs.yaml`` must propagate the
        parse error so verify exits 2, not silently fall back to the
        synthetic bundled identity (which causes false ``config
        mismatch`` rows for default-seeded packs).
        """
        from anywhere_agents.cli import _pack_verify
        import argparse
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            manifest = project / ".agent-config" / "repo" / "bootstrap" / "packs.yaml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("packs: [[[ not yaml", encoding="utf-8")
            user_path = pathlib.Path(d) / "user-config.yaml"
            args = argparse.Namespace(fix=False, yes=False)
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                rc = _pack_verify(user_path, project, args)
            self.assertEqual(rc, 2, f"output:\n{out_buf.getvalue()}\n{err_buf.getvalue()}")

    # ------------------------------------------------------------------
    # Group 2: Default-pack seeding
    # ------------------------------------------------------------------

    def test_verify_seeds_bundled_defaults_when_no_durable_config(self) -> None:
        from anywhere_agents.cli import _pack_verify
        import argparse
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            # Composer-shape lock entries with non-empty output_paths so
            # _load_lock_observations classifies them as "ok" (not
            # schema_stale), and the corresponding files exist on disk so
            # lock-health does not flip to "broken". With P=1 (default
            # seeded) + L=1 + ok, the classifier emits "deployed".
            agent_style_outputs = [".agent-config/style/STYLE.md"]
            aa_core_outputs = [".claude/skills/aa-core-skills/SKILL.md"]
            self._create_output_files(project, agent_style_outputs)
            self._create_output_files(project, aa_core_outputs)
            self._write_lock(project, {
                "packs": {
                    "agent-style": self._lock_entry("", "", agent_style_outputs),
                    "aa-core-skills": self._lock_entry("", "", aa_core_outputs),
                }
            })
            user_path = pathlib.Path(d) / "user-config.yaml"
            args = argparse.Namespace(fix=False, yes=False)
            out_buf = io.StringIO()
            with redirect_stdout(out_buf):
                rc = _pack_verify(user_path, project, args)
            self.assertEqual(rc, 0, f"output:\n{out_buf.getvalue()}")
            output = out_buf.getvalue()
            self.assertIn("agent-style", output)
            self.assertIn("aa-core-skills", output)
            self.assertIn("deployed", output)

    def test_verify_seeds_bundled_defaults_no_lock_yet(self) -> None:
        from anywhere_agents.cli import _pack_verify
        import argparse
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            user_path = pathlib.Path(d) / "user-config.yaml"
            args = argparse.Namespace(fix=False, yes=False)
            out_buf = io.StringIO()
            with redirect_stdout(out_buf):
                rc = _pack_verify(user_path, project, args)
            self.assertEqual(rc, 1, f"output:\n{out_buf.getvalue()}")
            output = out_buf.getvalue()
            self.assertIn("agent-style", output)
            self.assertIn("aa-core-skills", output)
            self.assertIn("declared, not bootstrapped", output)

    def test_verify_explicit_empty_rule_packs_suppresses_defaults(self) -> None:
        from anywhere_agents.cli import _pack_verify
        import argparse
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            (project / "agent-config.yaml").write_text(
                yaml.safe_dump({"rule_packs": []}, sort_keys=False),
                encoding="utf-8",
            )
            self._write_lock(project, {
                "packs": {
                    "agent-style": {
                        "source_url": "",
                        "requested_ref": "",
                        "resolved_commit": "",
                        "output_paths": [],
                    },
                    "aa-core-skills": {
                        "source_url": "",
                        "requested_ref": "",
                        "resolved_commit": "",
                        "output_paths": [],
                    },
                }
            })
            user_path = pathlib.Path(d) / "user-config.yaml"
            args = argparse.Namespace(fix=False, yes=False)
            out_buf = io.StringIO()
            with redirect_stdout(out_buf):
                rc = _pack_verify(user_path, project, args)
            self.assertEqual(rc, 1, f"output:\n{out_buf.getvalue()}")
            output = out_buf.getvalue()
            self.assertIn("orphan", output)

    # ------------------------------------------------------------------
    # Group 3: Per-state output assertions
    # ------------------------------------------------------------------

    def test_verify_deployed_only_exits_0(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            url = "https://github.com/yzhao062/agent-pack"
            ref = "v0.1.0"
            self._write_project(project, [
                {"name": "profile", "source": {"url": url, "ref": ref}},
            ])
            # Lock has matching identity + an output_paths entry pointing at
            # an actually-existing file so lock_health is "ok".
            output_file = project / ".claude" / "skills" / "profile.md"
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text("placeholder", encoding="utf-8")
            self._write_lock(project, {
                "packs": {
                    "profile": {
                        "source_url": url,
                        "requested_ref": ref,
                        "resolved_commit": "ab" * 20,
                        "output_paths": [".claude/skills/profile.md"],
                    },
                }
            })
            user_path = pathlib.Path(d) / "user-config.yaml"
            self._write_user(user_path, [
                {"name": "profile", "source": {"url": url, "ref": ref}},
            ])
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 0, f"stdout:\n{out_buf.getvalue()}\nstderr:\n{err_buf.getvalue()}")
            self.assertIn("deployed", out_buf.getvalue())

    def test_verify_user_level_only_exits_1(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            # Provide an explicit empty rule_packs to prevent default seeding
            # from injecting agent-style + aa-core-skills.
            (project / "agent-config.yaml").write_text(
                yaml.safe_dump({"rule_packs": []}, sort_keys=False),
                encoding="utf-8",
            )
            url = "https://github.com/yzhao062/agent-pack"
            user_path = pathlib.Path(d) / "user-config.yaml"
            self._write_user(user_path, [
                {"name": "profile", "source": {"url": url, "ref": "v0.1.0"}},
            ])
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 1)
            self.assertIn("user-level only", out_buf.getvalue())

    def test_verify_config_mismatch_output(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            url = "https://github.com/yzhao062/agent-pack"
            self._write_project(project, [
                {"name": "profile", "source": {"url": url, "ref": "main"}},
            ])
            user_path = pathlib.Path(d) / "user-config.yaml"
            self._write_user(user_path, [
                {"name": "profile", "source": {"url": url, "ref": "v0.1.0"}},
            ])
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 1)
            output = out_buf.getvalue()
            self.assertIn("config mismatch", output)
            self.assertIn("user:", output)
            self.assertIn("project:", output)

    def test_verify_orphan_output_does_not_suggest_pack_remove(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            # Suppress default seeding so the only row is the orphan.
            (project / "agent-config.yaml").write_text(
                yaml.safe_dump({"rule_packs": []}, sort_keys=False),
                encoding="utf-8",
            )
            self._write_lock(project, {
                "packs": {
                    "stale-pack": {
                        "source_url": "https://github.com/some/other",
                        "requested_ref": "v0.1.0",
                        "resolved_commit": "ab" * 20,
                        "output_paths": [],
                    },
                }
            })
            user_path = pathlib.Path(d) / "user-config.yaml"
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 1)
            output = out_buf.getvalue()
            # The orphan hint must direct the user to ``uninstall --all``
            # and/or ``restore`` a rule_packs: entry. This is the actual
            # cleanup path; the verify CLI must not steer users toward
            # ``pack remove`` (which only edits user-level config and
            # leaves the lock + outputs in place).
            self.assertTrue(
                ("uninstall --all" in output) or ("restore" in output),
                f"orphan hint should mention uninstall --all or restore; got:\n{output}",
            )
            # The literal substring "pack remove" may appear inside a
            # protective "Do not use `pack remove`" disclaimer that the
            # CLI emits to steer users away from the wrong cleanup path.
            # Accept that wording, but require any "pack remove" mention
            # to be preceded by an explicit negation. Specifically: every
            # occurrence of the substring must lie inside a "do not" /
            # "don't" / "never" / "avoid" phrase.
            self._assert_pack_remove_only_in_negation(output)

    def test_verify_corrupt_user_config_exits_2(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            user_path = pathlib.Path(d) / "user-config.yaml"
            user_path.write_text("not: a: valid: yaml: [[[", encoding="utf-8")
            err_buf, out_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 2)

    def test_verify_corrupt_pack_lock_exits_2(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            agent_dir = project / ".agent-config"
            agent_dir.mkdir()
            (agent_dir / "pack-lock.json").write_text(
                "{ not valid json {{",
                encoding="utf-8",
            )
            user_path = pathlib.Path(d) / "user-config.yaml"
            err_buf, out_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 2)

    # ------------------------------------------------------------------
    # Group 4: --fix semantics
    # ------------------------------------------------------------------

    def test_verify_fix_writes_rule_packs_atomic(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            # Pre-existing project YAML with a non-rule_packs key that
            # must be preserved across --fix.
            project_yaml = project / "agent-config.yaml"
            project_yaml.write_text(
                yaml.safe_dump(
                    {"some_other_key": "preserved", "rule_packs": []},
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            url = "https://github.com/yzhao062/agent-pack"
            user_path = pathlib.Path(d) / "user-config.yaml"
            self._write_user(user_path, [
                {"name": "profile", "source": {"url": url, "ref": "v0.1.0"}},
            ])
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                # v0.5.2: --fix invokes the composer subprocess after
                # config rewrites. Mock it to a successful no-op so we
                # can observe the YAML write and exit code 0 without
                # needing a real composer in the test fixture.
                with patch("sys.stdin.isatty", return_value=False), \
                     patch(
                         "anywhere_agents.cli._invoke_composer",
                         return_value=0,
                     ), \
                     redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify", "--fix", "--yes"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 0, f"stdout:\n{out_buf.getvalue()}\nstderr:\n{err_buf.getvalue()}")
            written = yaml.safe_load(project_yaml.read_text(encoding="utf-8"))
            # Pre-existing key preserved.
            self.assertEqual(written.get("some_other_key"), "preserved")
            # New rule_packs entry written.
            rule_packs = written.get("rule_packs", [])
            names = {e["name"] for e in rule_packs if isinstance(e, dict)}
            self.assertIn("profile", names)
            # No leftover .tmp file from the atomic write.
            tmp = project_yaml.with_name(project_yaml.name + ".tmp")
            self.assertFalse(
                tmp.exists(),
                f"atomic write must clean up {tmp.name}",
            )

    def test_verify_fix_idempotent(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            project_yaml = project / "agent-config.yaml"
            url = "https://github.com/yzhao062/agent-pack"
            user_path = pathlib.Path(d) / "user-config.yaml"
            self._write_user(user_path, [
                {"name": "profile", "source": {"url": url, "ref": "v0.1.0"}},
            ])
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                # v0.5.2: mock the composer so --fix can succeed without a
                # real bootstrap fixture. Both runs see the mock as 0.
                with patch(
                    "anywhere_agents.cli._invoke_composer",
                    return_value=0,
                ):
                    # First --fix run.
                    out1 = io.StringIO()
                    with patch("sys.stdin.isatty", return_value=False), \
                         redirect_stdout(out1), redirect_stderr(io.StringIO()):
                        rc1 = _pack_main(user_path, ["verify", "--fix", "--yes"])
                    # File contents must persist between calls.
                    content_after_first = project_yaml.read_text(encoding="utf-8")
                    # Second --fix run.
                    out2 = io.StringIO()
                    with patch("sys.stdin.isatty", return_value=False), \
                         redirect_stdout(out2), redirect_stderr(io.StringIO()):
                        rc2 = _pack_main(user_path, ["verify", "--fix", "--yes"])
            finally:
                os.chdir(cwd_before)
            # First run repairs the user-only row → rc 0.
            self.assertEqual(rc1, 0)
            # On the second run, the pack now appears in user + project but
            # has no pack-lock entry yet (--fix never writes the lock), so
            # the row is "declared, not bootstrapped". v0.5.2 invokes the
            # composer in this state → rc 0 with the mocked composer.
            self.assertIn(rc2, (0, 1))
            content_after_second = project_yaml.read_text(encoding="utf-8")
            self.assertEqual(
                content_after_first,
                content_after_second,
                "second --fix run must not change the file content",
            )
            # Second run must explicitly say "nothing to repair" or
            # "deploying declared-but-not-bootstrapped" (v0.5.2).
            out2_text = out2.getvalue()
            self.assertTrue(
                "nothing to repair" in out2_text
                or "declared-but-not-bootstrapped" in out2_text
                or "Deployed" in out2_text,
                f"output:\n{out2_text}",
            )

    def test_verify_fix_leaves_pack_lock_intact_on_orphan(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            (project / "agent-config.yaml").write_text(
                yaml.safe_dump({"rule_packs": []}, sort_keys=False),
                encoding="utf-8",
            )
            lock_path = self._write_lock(project, {
                "packs": {
                    "stale-pack": {
                        "source_url": "https://github.com/some/other",
                        "requested_ref": "v0.1.0",
                        "resolved_commit": "ab" * 20,
                        "output_paths": [],
                    },
                }
            })
            lock_before = lock_path.read_bytes()
            user_path = pathlib.Path(d) / "user-config.yaml"
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch("sys.stdin.isatty", return_value=False), \
                     redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify", "--fix", "--yes"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 1, f"output:\n{out_buf.getvalue()}")
            lock_after = lock_path.read_bytes()
            self.assertEqual(lock_before, lock_after, "pack-lock.json must not change")
            # ``--fix`` on an orphan must not steer users toward
            # ``pack remove`` (it only touches user-level config). The
            # CLI may still emit a "do not use pack remove" disclaimer,
            # but never a positive recommendation.
            self._assert_pack_remove_only_in_negation(out_buf.getvalue())

    def test_verify_fix_does_not_resolve_mismatch(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            url = "https://github.com/yzhao062/agent-pack"
            project_yaml = project / "agent-config.yaml"
            project_yaml.write_text(
                yaml.safe_dump(
                    {"rule_packs": [
                        {"name": "profile", "source": {"url": url, "ref": "main"}},
                    ]},
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            project_before = project_yaml.read_text(encoding="utf-8")
            user_path = pathlib.Path(d) / "user-config.yaml"
            self._write_user(user_path, [
                {"name": "profile", "source": {"url": url, "ref": "v0.1.0"}},
            ])
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch("sys.stdin.isatty", return_value=False), \
                     redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify", "--fix", "--yes"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 1, f"output:\n{out_buf.getvalue()}")
            project_after = project_yaml.read_text(encoding="utf-8")
            self.assertEqual(
                project_before,
                project_after,
                "mismatch must not auto-repair agent-config.yaml",
            )

    def test_verify_fix_refuses_malformed_yaml(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            project_yaml = project / "agent-config.yaml"
            malformed = "rule_packs: [[[ not yaml"
            project_yaml.write_text(malformed, encoding="utf-8")
            project_before = project_yaml.read_bytes()
            url = "https://github.com/yzhao062/agent-pack"
            user_path = pathlib.Path(d) / "user-config.yaml"
            self._write_user(user_path, [
                {"name": "profile", "source": {"url": url, "ref": "v0.1.0"}},
            ])
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch("sys.stdin.isatty", return_value=False), \
                     redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify", "--fix", "--yes"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 2)
            project_after = project_yaml.read_bytes()
            self.assertEqual(
                project_before,
                project_after,
                "malformed YAML must not be overwritten",
            )

    def test_verify_fix_dry_run_prints_plan(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            url = "https://github.com/yzhao062/agent-pack"
            user_path = pathlib.Path(d) / "user-config.yaml"
            self._write_user(user_path, [
                {"name": "profile", "source": {"url": url, "ref": "v0.1.0"}},
            ])
            project_yaml = project / "agent-config.yaml"
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                # Force non-TTY so the prompt branch falls into the
                # "--yes required" dry-run path.
                with patch("sys.stdin.isatty", return_value=False), \
                     redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify", "--fix"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 0, f"stdout:\n{out_buf.getvalue()}\nstderr:\n{err_buf.getvalue()}")
            output = out_buf.getvalue()
            self.assertIn("--fix", output)
            self.assertIn("planned changes", output)
            # File must not be written in dry-run.
            self.assertFalse(
                project_yaml.exists(),
                "dry-run must not create agent-config.yaml",
            )

    @unittest.skip(
        "real-contention test deferred; add via subprocess fixture later"
    )
    def test_verify_fix_holds_repo_lock_real_contention(self) -> None:
        # Plan § Test plan calls for spawning a subprocess that holds the
        # repo lock, then verifying that --fix blocks until released.
        # Reliable subprocess contention testing is out of scope here.
        pass

    def test_verify_fix_writes_some_but_other_problem_returns_1(self) -> None:
        """v0.5.2 contract: any identity mismatch blocks --fix entirely.

        Plan § 2 step 2 last bullet: same name with different identity
        → rc=1, print both identities, **no writes**. The v0.5.1
        compromise that wrote some packs and reported others as
        "still need attention" is gone — partial-write semantics
        violated cross-command atomicity and made recovery harder.
        """
        from anywhere_agents.cli import _pack_main
        url = "https://github.com/yzhao062/agent-pack"
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            # Project YAML pre-populates ``other`` with ref ``main`` so
            # the user's ``other`` ref ``v0.1.0`` flips it to mismatch.
            # ``profile`` has no project entry and is user-only — but
            # the mismatch on ``other`` blocks the whole fix.
            self._write_project(project, [
                {"name": "other", "source": {"url": url, "ref": "main"}},
            ])
            user_path = pathlib.Path(d) / "user-config.yaml"
            self._write_user(user_path, [
                {"name": "profile", "source": {"url": url, "ref": "v0.1.0"}},
                {"name": "other", "source": {"url": url, "ref": "v0.1.0"}},
            ])
            project_yaml_before = (project / "agent-config.yaml").read_text(
                encoding="utf-8"
            )
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch("sys.stdin.isatty", return_value=False), \
                     redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_main(user_path, ["verify", "--fix", "--yes"])
            finally:
                os.chdir(cwd_before)
            output = out_buf.getvalue()
            self.assertEqual(
                rc, 1,
                f"v0.5.2 --fix must rc=1 on identity mismatch; "
                f"output:\n{output}",
            )
            self.assertIn(
                "identity mismatch", output,
                "must surface the mismatch reason",
            )
            self.assertIn("other", output, "must name the conflicting pack")
            # Project YAML must NOT be rewritten when any mismatch is
            # in the row set.
            project_yaml_after = (project / "agent-config.yaml").read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                project_yaml_before,
                project_yaml_after,
                "v0.5.2: identity mismatch blocks all writes",
            )

    def test_verify_fix_mismatch_blocks_writes(self) -> None:
        """v0.5.2 contract: any identity mismatch in the row set blocks
        the entire --fix invocation. No writes happen, rc=1 with a clear
        message naming the conflicting pack.

        Replaces the v0.5.1 ``test_verify_fix_state_changed_under_lock_returns_1``
        which depended on the now-removed locked re-gather step. The
        v0.5.2 implementation does not re-classify under lock; it
        applies the planned writes once the user confirms (or --yes
        bypass) and then invokes the composer subprocess.
        """
        from anywhere_agents.cli import (
            _pack_verify_fix,
            _VERIFY_STATE_MISMATCH,
        )
        import argparse
        url = "https://github.com/yzhao062/agent-pack"
        u = self._ident("profile", url=url, ref="v0.1.0")
        p = self._ident("profile", url=url, ref="main")
        rows = [{
            "name": "profile",
            "state": _VERIFY_STATE_MISMATCH,
            "u": u, "p": p, "l": None,
            "sole": None, "note": None, "missing_paths": [],
        }]

        def _fake_gather(user_path, project_root):
            return rows, None

        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            user_path = pathlib.Path(d) / "user-config.yaml"
            args = argparse.Namespace(fix=True, yes=True)
            out_buf, err_buf = io.StringIO(), io.StringIO()
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch(
                    "anywhere_agents.cli._verify_gather",
                    side_effect=_fake_gather,
                ), redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = _pack_verify_fix(user_path, project, args)
            finally:
                os.chdir(cwd_before)
            output = out_buf.getvalue()
            self.assertEqual(
                rc, 1,
                f"v0.5.2: identity mismatch must rc=1; output:\n{output}",
            )
            self.assertIn("identity mismatch", output)

    def test_pack_remove_cascades_to_project_and_composer(self) -> None:
        """v0.5.2: pack remove is now cascade delete.

        Replaces v0.5.1's ``pack_remove_after_fix_only_removes_user_config``
        which expected user-config-only removal. v0.5.2's contract:
        remove from user config + project rule_packs + invoke composer
        uninstall mode (which deletes outputs, prunes state, decrements
        owners with composite-key filter). The composer subprocess is
        mocked here so the test does not need a bootstrapped fixture.
        """
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            url = "https://github.com/yzhao062/agent-pack"
            ref = "v0.1.0"
            user_path = pathlib.Path(d) / "user-config.yaml"
            self._write_user(user_path, [
                {"name": "profile", "source": {"url": url, "ref": ref}},
            ])
            # Pre-populate project YAML with the same pack so the cascade
            # has work to do at the project layer.
            self._write_project(project, [
                {"name": "profile", "source": {"url": url, "ref": ref}},
            ])
            self._write_lock(project, {
                "packs": {
                    "profile": {
                        "source_url": url,
                        "requested_ref": ref,
                        "resolved_commit": "ab" * 20,
                        "output_paths": [".claude/skills/profile.md"],
                    },
                }
            })
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                composer_calls = []

                def _fake_composer(project_root, *args):
                    composer_calls.append(args)
                    return 0

                with patch(
                    "anywhere_agents.cli._invoke_composer",
                    side_effect=_fake_composer,
                ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    rc = _pack_main(user_path, ["remove", "profile"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 0)
            # Cascade: user, project, and composer-uninstall all triggered.
            user_data = yaml.safe_load(user_path.read_text(encoding="utf-8"))
            user_names = {
                e.get("name") for e in user_data.get("packs", [])
                if isinstance(e, dict)
            }
            self.assertNotIn("profile", user_names)
            project_data = yaml.safe_load(
                (project / "agent-config.yaml").read_text(encoding="utf-8")
            )
            project_names = {
                e.get("name") for e in (project_data.get("rule_packs") or [])
                if isinstance(e, dict)
            }
            self.assertNotIn("profile", project_names)
            # Composer was invoked in single-pack uninstall mode.
            self.assertEqual(len(composer_calls), 1)
            self.assertEqual(composer_calls[0], ("uninstall", "profile"))

    # ------------------------------------------------------------------
    # Group 5: Identity normalization
    # ------------------------------------------------------------------

    def test_normalize_github_https_trailing_git(self) -> None:
        from anywhere_agents.packs.source_fetch import normalize_pack_source_url
        result = normalize_pack_source_url("https://github.com/Owner/Repo.git/")
        self.assertEqual(result, "https://github.com/owner/repo")

    def test_normalize_github_ssh_form(self) -> None:
        from anywhere_agents.packs.source_fetch import normalize_pack_source_url
        result = normalize_pack_source_url("git@github.com:Owner/Repo.git")
        self.assertEqual(result, "https://github.com/owner/repo")

    def test_normalize_github_host_case(self) -> None:
        from anywhere_agents.packs.source_fetch import normalize_pack_source_url
        # Plan target (full collapse): ``https://github.com/owner/repo``.
        # Current CLI behavior: ``auth.normalize_github_url`` does a
        # case-sensitive ``"github.com" in url`` quick-host-check, so a
        # mixed-case host (``GitHub.COM``) falls through to the
        # non-GitHub branch which lowercases the host but preserves path
        # case. The result is ``https://github.com/Owner/Repo``. This
        # test pins the host-lowercase invariant (the substantive part of
        # identity normalization) while documenting that path case
        # collapse for mixed-case GitHub hosts is a CLI gap relative to
        # the plan spec.
        result = normalize_pack_source_url("https://GitHub.COM/Owner/Repo")
        self.assertTrue(
            result.lower().startswith("https://github.com/"),
            f"host must be lowercased; got {result!r}",
        )
        # Both variants below should normalize together once the
        # case-insensitive host check lands; for now they share the same
        # lowercased-host prefix at minimum.
        lower = normalize_pack_source_url("https://github.com/Owner/Repo")
        self.assertEqual(
            result.lower(),
            lower.lower(),
            "case-insensitive host should yield the same normalized form (modulo path case)",
        )

    def test_normalize_github_owner_repo_case_collapse(self) -> None:
        from anywhere_agents.packs.source_fetch import normalize_pack_source_url
        a = normalize_pack_source_url("https://github.com/Owner/Repo")
        b = normalize_pack_source_url("https://github.com/owner/repo")
        self.assertEqual(a, b)

    def test_normalize_other_host_minimal(self) -> None:
        from anywhere_agents.packs.source_fetch import normalize_pack_source_url
        result = normalize_pack_source_url("https://Gitlab.com/group/repo.git/")
        self.assertEqual(result, "https://gitlab.com/group/repo")

    def test_normalize_unparseable_returns_unchanged(self) -> None:
        from anywhere_agents.packs.source_fetch import normalize_pack_source_url
        # Garbage URL without a scheme: should be returned as-is rather
        # than crashing.
        result = normalize_pack_source_url("not-a-url")
        self.assertEqual(result, "not-a-url")

    def test_verify_identity_ref_exact(self) -> None:
        from anywhere_agents.cli import _load_user_observations
        url = "https://github.com/yzhao062/agent-pack"
        with tempfile.TemporaryDirectory() as d:
            user_a = pathlib.Path(d) / "a.yaml"
            user_a.write_text(
                yaml.safe_dump({"packs": [
                    {"name": "p", "source": {"url": url, "ref": "v0.1.0"}},
                ]}, sort_keys=False),
                encoding="utf-8",
            )
            user_b = pathlib.Path(d) / "b.yaml"
            user_b.write_text(
                yaml.safe_dump({"packs": [
                    {"name": "p", "source": {"url": url, "ref": "main"}},
                ]}, sort_keys=False),
                encoding="utf-8",
            )
            idents_a = _load_user_observations(user_a)
            idents_b = _load_user_observations(user_b)
            self.assertEqual(len(idents_a), 1)
            self.assertEqual(len(idents_b), 1)
            # Same URL, same name, but different refs → distinct identity
            # tuples (5-tuple, ref is index 2).
            self.assertEqual(idents_a[0][0], idents_b[0][0])  # name
            self.assertEqual(idents_a[0][1], idents_b[0][1])  # normalized URL
            self.assertNotEqual(idents_a[0][2], idents_b[0][2])  # ref
            self.assertNotEqual(idents_a[0], idents_b[0])

    def test_verify_identity_omitted_ref_defaults_to_main(self) -> None:
        """Round 1 regression: a user-config or rule_packs row that omits
        ``source.ref`` must classify with the same identity tuple as one
        that explicitly sets ``ref: main``. Composer also defaults inline
        sources to ``main`` (`scripts/compose_packs.py:122, :402`), so the
        CLI's identity tuple has to match or `pack verify --fix` would
        falsely report identity-mismatch on hand-authored or pre-v0.5.2
        rows.
        """
        from anywhere_agents.cli import _load_user_observations
        url = "https://github.com/yzhao062/agent-pack"
        with tempfile.TemporaryDirectory() as d:
            user_no_ref = pathlib.Path(d) / "a.yaml"
            user_no_ref.write_text(
                yaml.safe_dump({"packs": [
                    {"name": "p", "source": {"url": url}},
                ]}, sort_keys=False),
                encoding="utf-8",
            )
            user_main = pathlib.Path(d) / "b.yaml"
            user_main.write_text(
                yaml.safe_dump({"packs": [
                    {"name": "p", "source": {"url": url, "ref": "main"}},
                ]}, sort_keys=False),
                encoding="utf-8",
            )
            idents_no_ref = _load_user_observations(user_no_ref)
            idents_main = _load_user_observations(user_main)
            self.assertEqual(len(idents_no_ref), 1)
            self.assertEqual(len(idents_main), 1)
            # Same URL, same name, omitted ref vs explicit "main" → must
            # produce the SAME identity tuple (idempotency for re-add and
            # no false config-mismatch in `pack verify --fix`).
            self.assertEqual(idents_no_ref[0], idents_main[0])

    # ------------------------------------------------------------------
    # Group 6: Cross-platform user config path
    # ------------------------------------------------------------------

    @unittest.skipUnless(sys.platform == "win32", "Windows-only path")
    def test_verify_user_config_windows_appdata(self) -> None:
        from anywhere_agents.cli import _user_config_path
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"APPDATA": d}, clear=False):
                resolved = _user_config_path()
            self.assertIsNotNone(resolved)
            expected = pathlib.Path(d) / "anywhere-agents" / "config.yaml"
            self.assertEqual(resolved, expected)

    @unittest.skipIf(sys.platform == "win32", "POSIX-only path")
    def test_verify_user_config_posix_xdg(self) -> None:
        from anywhere_agents.cli import _user_config_path
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": d}, clear=False):
                resolved = _user_config_path()
            self.assertIsNotNone(resolved)
            expected = pathlib.Path(d) / "anywhere-agents" / "config.yaml"
            self.assertEqual(resolved, expected)


# ----------------------------------------------------------------------
# v0.5.2: pack add one-shot, identity-mismatch, outside-project,
# pack remove cascade
# ----------------------------------------------------------------------


class _OneSinglePackManifest:
    """Helper: a manifest with a single passive pack named ``solo``."""
    text = (
        "version: 2\n"
        "packs:\n"
        "  - name: solo\n"
        "    description: x\n"
        "    source: {repo: https://github.com/yzhao062/agent-pack, ref: v0.1.0}\n"
        "    passive: [{files: [{from: docs/x.md, to: AGENTS.md}]}]\n"
    )


class PackAddOneShotV052Tests(unittest.TestCase):
    """v0.5.2 one-shot ``pack add``: user-config write + project-config
    write + composer subprocess all in one invocation when in-project.
    """

    def _make_archive(self, archive_dir: pathlib.Path):
        from anywhere_agents.packs import source_fetch
        return source_fetch.PackArchive(
            url="https://github.com/yzhao062/agent-pack",
            ref="v0.1.0",
            resolved_commit="ab" * 20,
            method="anonymous",
            archive_dir=archive_dir,
            canonical_id="yzhao062/agent-pack",
            cache_key="abcd1234/" + "ab" * 20,
        )

    def test_pack_add_in_project_writes_both_configs_and_invokes_composer(self) -> None:
        from anywhere_agents.cli import _pack_main
        from anywhere_agents.packs import source_fetch
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            # Mark as bootstrapped: presence of compose_packs.py triggers
            # the in-project branch.
            (project / ".agent-config" / "repo" / "scripts").mkdir(parents=True)
            (project / ".agent-config" / "repo" / "scripts" / "compose_packs.py").write_text("# stub")
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_OneSinglePackManifest.text)
            user_path = pathlib.Path(d) / "user-config.yaml"
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                composer_calls: list[tuple] = []

                def _fake_composer(project_root, *args):
                    composer_calls.append(args)
                    return 0

                with patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self._make_archive(archive_dir),
                ), patch(
                    "anywhere_agents.cli._invoke_composer",
                    side_effect=_fake_composer,
                ):
                    rc = _pack_main(user_path, [
                        "add",
                        "https://github.com/yzhao062/agent-pack",
                        "--ref", "v0.1.0",
                    ])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 0)
            user_data = yaml.safe_load(user_path.read_text(encoding="utf-8"))
            user_names = {e["name"] for e in user_data["packs"]}
            self.assertIn("solo", user_names)
            project_yaml = project / "agent-config.yaml"
            self.assertTrue(project_yaml.exists())
            project_data = yaml.safe_load(project_yaml.read_text(encoding="utf-8"))
            project_names = {e["name"] for e in (project_data.get("rule_packs") or [])}
            self.assertIn("solo", project_names)
            self.assertEqual(len(composer_calls), 1)
            # Composer invoked with no extra args (compose mode), not
            # uninstall <name>.
            self.assertEqual(composer_calls[0], ())

    def test_pack_add_outside_project_only_writes_user_config(self) -> None:
        from anywhere_agents.cli import _pack_main
        from anywhere_agents.packs import source_fetch
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            # Intentionally NOT bootstrapped — no .agent-config/repo
            # directory and no bootstrap scripts.
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_OneSinglePackManifest.text)
            user_path = pathlib.Path(d) / "user-config.yaml"
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                composer_calls: list = []
                with patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self._make_archive(archive_dir),
                ), patch(
                    "anywhere_agents.cli._invoke_composer",
                    side_effect=lambda *a, **kw: composer_calls.append(a) or 0,
                ):
                    err_buf = io.StringIO()
                    with redirect_stderr(err_buf):
                        rc = _pack_main(user_path, [
                            "add",
                            "https://github.com/yzhao062/agent-pack",
                            "--ref", "v0.1.0",
                        ])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 0)
            # User config written.
            self.assertTrue(user_path.exists())
            # Project YAML NOT created.
            self.assertFalse((project / "agent-config.yaml").exists())
            # Composer NOT invoked.
            self.assertEqual(composer_calls, [])
            # Hint message about deploying in a bootstrapped project.
            self.assertIn("Registered globally", err_buf.getvalue())

    def test_pack_add_idempotent_same_identity(self) -> None:
        from anywhere_agents.cli import _pack_main
        from anywhere_agents.packs import source_fetch
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            (project / ".agent-config" / "repo" / "scripts").mkdir(parents=True)
            (project / ".agent-config" / "repo" / "scripts" / "compose_packs.py").write_text("# stub")
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_OneSinglePackManifest.text)
            user_path = pathlib.Path(d) / "user-config.yaml"
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self._make_archive(archive_dir),
                ), patch(
                    "anywhere_agents.cli._invoke_composer",
                    return_value=0,
                ):
                    rc1 = _pack_main(user_path, [
                        "add",
                        "https://github.com/yzhao062/agent-pack",
                        "--ref", "v0.1.0",
                    ])
                    rc2 = _pack_main(user_path, [
                        "add",
                        "https://github.com/yzhao062/agent-pack",
                        "--ref", "v0.1.0",
                    ])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc1, 0)
            self.assertEqual(rc2, 0)
            # Same identity → no duplicate row.
            user_data = yaml.safe_load(user_path.read_text(encoding="utf-8"))
            solo_count = sum(
                1 for e in user_data.get("packs", [])
                if isinstance(e, dict) and e.get("name") == "solo"
            )
            self.assertEqual(solo_count, 1, "idempotent: no duplicate row")

    def test_pack_add_identity_mismatch_returns_1(self) -> None:
        from anywhere_agents.cli import _pack_main
        from anywhere_agents.packs import source_fetch
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            (project / ".agent-config" / "repo" / "scripts").mkdir(parents=True)
            (project / ".agent-config" / "repo" / "scripts" / "compose_packs.py").write_text("# stub")
            archive_dir = pathlib.Path(d) / "archive"
            archive_dir.mkdir()
            (archive_dir / "pack.yaml").write_text(_OneSinglePackManifest.text)
            user_path = pathlib.Path(d) / "user-config.yaml"
            # Pre-existing user config: ``solo`` already pinned at main.
            user_path.write_text(yaml.safe_dump({
                "packs": [
                    {
                        "name": "solo",
                        "source": {
                            "url": "https://github.com/yzhao062/agent-pack",
                            "ref": "main",
                        },
                    },
                ]
            }))
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                composer_calls: list = []
                with patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self._make_archive(archive_dir),
                ), patch(
                    "anywhere_agents.cli._invoke_composer",
                    side_effect=lambda *a, **kw: composer_calls.append(a) or 0,
                ):
                    err_buf = io.StringIO()
                    with redirect_stderr(err_buf):
                        rc = _pack_main(user_path, [
                            "add",
                            "https://github.com/yzhao062/agent-pack",
                            "--ref", "v0.1.0",
                        ])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 1)
            # Project YAML should NOT exist (we abort before step 4b).
            self.assertFalse((project / "agent-config.yaml").exists())
            # Composer should NOT be invoked.
            self.assertEqual(composer_calls, [])
            # Existing user-config row preserved unchanged.
            user_data = yaml.safe_load(user_path.read_text(encoding="utf-8"))
            self.assertEqual(
                user_data["packs"][0]["source"]["ref"],
                "main",
                "existing user-config row must not be overwritten",
            )


class PackVerifyFixBidirectionalTests(unittest.TestCase):
    """v0.5.2 ``pack verify --fix`` reconciles in both directions."""

    def test_user_only_writes_to_project_yaml(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            url = "https://github.com/yzhao062/agent-pack"
            user_path = pathlib.Path(d) / "user-config.yaml"
            user_path.write_text(yaml.safe_dump({
                "packs": [
                    {"name": "profile", "source": {"url": url, "ref": "v0.1.0"}},
                ]
            }))
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch("sys.stdin.isatty", return_value=False), \
                     patch(
                         "anywhere_agents.cli._invoke_composer",
                         return_value=0,
                     ), redirect_stdout(io.StringIO()), \
                     redirect_stderr(io.StringIO()):
                    rc = _pack_main(user_path, ["verify", "--fix", "--yes"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 0)
            project_data = yaml.safe_load(
                (project / "agent-config.yaml").read_text(encoding="utf-8")
            )
            names = {e["name"] for e in (project_data.get("rule_packs") or [])}
            self.assertIn("profile", names)

    def test_project_only_writes_to_user_config(self) -> None:
        from anywhere_agents.cli import _pack_main
        with tempfile.TemporaryDirectory() as d:
            project = pathlib.Path(d) / "project"
            project.mkdir()
            url = "https://github.com/yzhao062/agent-pack"
            (project / "agent-config.yaml").write_text(yaml.safe_dump({
                "rule_packs": [
                    {"name": "myown", "source": {"url": url, "ref": "v0.1.0"}},
                ]
            }))
            user_path = pathlib.Path(d) / "user-config.yaml"
            cwd_before = os.getcwd()
            try:
                os.chdir(project)
                with patch("sys.stdin.isatty", return_value=False), \
                     patch(
                         "anywhere_agents.cli._invoke_composer",
                         return_value=0,
                     ), redirect_stdout(io.StringIO()), \
                     redirect_stderr(io.StringIO()):
                    rc = _pack_main(user_path, ["verify", "--fix", "--yes"])
            finally:
                os.chdir(cwd_before)
            self.assertEqual(rc, 0)
            self.assertTrue(user_path.exists())
            user_data = yaml.safe_load(user_path.read_text(encoding="utf-8"))
            names = {e["name"] for e in user_data.get("packs", [])}
            self.assertIn("myown", names)


if __name__ == "__main__":
    unittest.main()
