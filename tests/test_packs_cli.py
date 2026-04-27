"""Tests for the anywhere-agents CLI subcommands (pack add/remove/list, uninstall).

Imports the PyPI-package CLI directly; exercises pack management by
setting the user-level config path via env vars and invoking main()
with argv lists.
"""
from __future__ import annotations

import io
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# Add both the PyPI package and the scripts dir to sys.path so the CLI
# imports work (cli.py + packs.uninstall).
sys.path.insert(0, str(ROOT / "packages" / "pypi"))
sys.path.insert(0, str(ROOT / "scripts"))

from anywhere_agents import cli  # noqa: E402


@contextmanager
def _mock_remote_pack(name: str = "cool", *, active: bool = False, extra_packs: list[dict] | None = None):
    """Patch ``source_fetch.fetch_pack`` to return a minimal in-memory
    archive with a synthesized ``pack.yaml`` containing ``name`` (plus
    any ``extra_packs``). v0.4 tests used non-existent URLs that v0.5
    would now hit the network for; this fixture gives them a deterministic
    in-memory manifest so the original assertions still apply where they
    still make sense."""
    from anywhere_agents.packs import source_fetch
    with tempfile.TemporaryDirectory() as d:
        archive_dir = pathlib.Path(d)
        body = "version: 2\npacks:\n"
        body += f"  - name: {name}\n    description: x\n    source: {{repo: https://example/x, ref: main}}\n"
        body += "    passive: [{files: [{from: a.md, to: b.md}]}]\n"
        if active:
            body += "    active: [{kind: skill, required: false, files: [{from: x/, to: y/}]}]\n"
        for extra in extra_packs or []:
            extra_active = "    active: [{kind: skill, required: false, files: [{from: x/, to: y/}]}]\n" if extra.get("active") else ""
            body += (
                f"  - name: {extra['name']}\n    description: x\n"
                f"    source: {{repo: https://example/x, ref: main}}\n"
                f"    passive: [{{files: [{{from: a.md, to: b.md}}]}}]\n"
                f"{extra_active}"
            )
        (archive_dir / "pack.yaml").write_text(body)

        archive = source_fetch.PackArchive(
            url="https://example/x",
            ref="main",
            resolved_commit="ab" * 20,
            method="anonymous",
            archive_dir=archive_dir,
            canonical_id=None,
            cache_key="abcd1234/" + "ab" * 20,
        )
        with patch.object(source_fetch, "fetch_pack", return_value=archive) as patched:
            yield patched


def _invoke(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Invoke cli.main(argv) with an optional env override; capture I/O."""
    original_env = dict(os.environ)
    if env is not None:
        os.environ.clear()
        os.environ.update(env)
    out_buf, err_buf = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = cli.main(argv)
    except SystemExit as exc:
        rc = int(exc.code) if exc.code is not None else 0
    finally:
        os.environ.clear()
        os.environ.update(original_env)
    return rc, out_buf.getvalue(), err_buf.getvalue()


class _TmpHome(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Force the user-level config into a temp dir via env vars.
        if sys.platform == "win32":
            self.env = {
                "APPDATA": str(self.root / "AppData"),
                "PATH": os.environ.get("PATH", ""),
            }
            self.expected_config = (
                self.root / "AppData" / "anywhere-agents" / "config.yaml"
            )
        else:
            self.env = {
                "HOME": str(self.root),
                "PATH": os.environ.get("PATH", ""),
            }
            self.expected_config = (
                self.root / ".config" / "anywhere-agents" / "config.yaml"
            )


class PackAddTests(_TmpHome):
    """v0.5.0 ``pack add`` fetches the remote pack.yaml and expands one
    user-level row per remote pack. Tests mock ``fetch_pack`` so they
    don't reach the network. The pre-v0.5 behaviors of agent-style
    seeding and same-name dedup were removed by Phase 9; the v0.5.0
    semantic is "append every selected pack name to the list"."""

    def test_first_add_writes_one_row_per_remote_pack(self) -> None:
        with _mock_remote_pack("cool"):
            rc, _, err = _invoke(
                ["pack", "add", "https://github.com/me/cool-pack"],
                env=self.env,
            )
        self.assertEqual(rc, 0, msg=err)
        self.assertTrue(self.expected_config.exists())

        import yaml
        data = yaml.safe_load(self.expected_config.read_text(encoding="utf-8"))
        names = [p["name"] for p in data["packs"]]
        self.assertEqual(names, ["cool"])

    def test_credential_url_rejected(self) -> None:
        rc, _, err = _invoke(
            ["pack", "add", "https://ghp_xyz@github.com/me/pack"],
            env=self.env,
        )
        self.assertEqual(rc, 2)
        self.assertIn("credentials", err)

    def test_second_add_appends(self) -> None:
        with _mock_remote_pack("a"):
            _invoke(["pack", "add", "https://github.com/me/a"], env=self.env)
        with _mock_remote_pack("b"):
            rc, _, _ = _invoke(
                ["pack", "add", "https://github.com/me/b"], env=self.env
            )
        self.assertEqual(rc, 0)
        import yaml
        data = yaml.safe_load(self.expected_config.read_text(encoding="utf-8"))
        names = [p["name"] for p in data["packs"]]
        self.assertEqual(sorted(names), ["a", "b"])


class LegacyAliasMigrationTests(_TmpHome):
    """Regression for Round 1 Codex High #3: `pack add` / `pack remove`
    on a user-level file that contains only legacy `rule_packs:` must
    migrate the existing entries into `packs:` rather than silently
    shadow them. Without the migration, the composer's preference for
    `packs:` over `rule_packs:` drops the legacy selections."""

    def _prewrite_legacy(self) -> None:
        import yaml
        self.expected_config.parent.mkdir(parents=True, exist_ok=True)
        self.expected_config.write_text(
            yaml.safe_dump(
                {"rule_packs": [{"name": "legacy-pack"}]},
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def test_add_migrates_legacy(self) -> None:
        self._prewrite_legacy()
        with _mock_remote_pack("new-pack"):
            rc, _, err = _invoke(
                ["pack", "add", "https://github.com/me/new-pack"], env=self.env,
            )
        self.assertEqual(rc, 0, msg=err)
        import yaml
        data = yaml.safe_load(self.expected_config.read_text(encoding="utf-8"))
        # rule_packs: gone; packs: contains legacy-pack + new-pack.
        self.assertNotIn("rule_packs", data)
        self.assertIn("packs", data)
        names = [p.get("name") if isinstance(p, dict) else p for p in data["packs"]]
        self.assertIn("legacy-pack", names)
        self.assertIn("new-pack", names)

    def test_remove_migrates_legacy(self) -> None:
        self._prewrite_legacy()
        rc, _, err = _invoke(
            ["pack", "remove", "legacy-pack"], env=self.env,
        )
        self.assertEqual(rc, 0, msg=err)
        import yaml
        data = yaml.safe_load(self.expected_config.read_text(encoding="utf-8"))
        self.assertNotIn("rule_packs", data)
        self.assertEqual(data.get("packs", []), [])


class PackRemoveTests(_TmpHome):
    def test_remove_existing(self) -> None:
        with _mock_remote_pack("foo"):
            _invoke(
                ["pack", "add", "https://github.com/me/foo"], env=self.env,
            )
        rc, _, err = _invoke(["pack", "remove", "foo"], env=self.env)
        self.assertEqual(rc, 0, msg=err)
        import yaml
        data = yaml.safe_load(self.expected_config.read_text(encoding="utf-8"))
        names = [p.get("name") if isinstance(p, dict) else p for p in data["packs"]]
        self.assertNotIn("foo", names)

    def test_remove_nonexistent_returns_1(self) -> None:
        # v0.5.2: pack remove returns rc=1 when the pack is not in any
        # of (user config, project rule_packs, pack-lock). Plan § 3
        # step 1: "Not found in any → rc=1".
        rc, _, _ = _invoke(["pack", "remove", "never-added"], env=self.env)
        self.assertEqual(rc, 1)


class PackListTests(_TmpHome):
    def test_list_empty(self) -> None:
        rc, out, _ = _invoke(["pack", "list"], env=self.env)
        self.assertEqual(rc, 0)
        self.assertIn("(not created yet)", out)

    def test_list_after_add(self) -> None:
        with _mock_remote_pack("my-pack"):
            _invoke(
                ["pack", "add", "https://github.com/me/my-pack"], env=self.env
            )
        rc, out, _ = _invoke(["pack", "list"], env=self.env)
        self.assertEqual(rc, 0)
        self.assertIn("my-pack", out)


class BootstrapBackwardCompatTests(unittest.TestCase):
    def test_version_flag_unchanged(self) -> None:
        rc, out, _ = _invoke(["--version"])
        # argparse exits via SystemExit; _invoke catches it.
        self.assertEqual(rc, 0)
        self.assertIn("anywhere-agents", out)

    def test_dry_run_flag_unchanged(self) -> None:
        """The bootstrap path must still respect --dry-run without
        reaching out to the network or spawning subprocesses."""
        rc, _, err = _invoke(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("Would fetch", err)


class UninstallWiringTests(_TmpHome):
    def test_uninstall_without_bootstrap_surfaces_hint(self) -> None:
        """`uninstall --all` in a directory without .agent-config/repo/
        must not crash — it exits 2 with an actionable hint."""
        # Change cwd to the tmp root (no .agent-config/repo/).
        import os as _os
        original = _os.getcwd()
        try:
            _os.chdir(self.root)
            rc, _, err = _invoke(["uninstall", "--all"], env=self.env)
        finally:
            _os.chdir(original)
        self.assertEqual(rc, 2)
        self.assertIn("requires a project bootstrapped", err)


if __name__ == "__main__":
    unittest.main()
