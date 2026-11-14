"""Integration tests for scripts/compose_packs.py (v0.4.0 Phase 1 entry point).

Covers the Phase 1 wrapper behavior: manifest filename resolution (packs.yaml
preferred; rule-packs.yaml alias honored); delegation to the v0.3.x composer
for passive-only v1 manifests; explicit early rejection of v2 manifests and
active-entry manifests that Phase 3 will handle.

No network activity; the v0.3.x composer's URL fetch is not exercised here
(covered separately in test_compose_rule_packs.py). The happy path uses
``--print-yaml`` which returns without fetching.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

# tests/ is on sys.path under `unittest discover -s tests` but not under
# `python -m unittest tests.<module>`, which validate.yml uses for the
# Sentinel redaction smoke. Put it there before the sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compose_packs  # noqa: E402
from packs import codex_links  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    """Invoke compose_packs.main(argv) and capture stdout/stderr."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = compose_packs.main(argv)
    return rc, out_buf.getvalue(), err_buf.getvalue()


class ManifestResolutionTests(unittest.TestCase):
    """Loader-level filename alias: prefer packs.yaml; fall back to rule-packs.yaml."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
        self.bootstrap_dir.mkdir(parents=True)

    def _legacy_manifest_text(self) -> str:
        return (
            "version: 1\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source: https://example.com/{ref}/rule-pack.md\n"
            "    default-ref: v0.3.2\n"
        )

    def test_resolves_packs_yaml_when_present(self) -> None:
        _write(self.bootstrap_dir / "packs.yaml", self._legacy_manifest_text())
        path = compose_packs._resolve_manifest_path(self.root, None)
        self.assertEqual(path.name, "packs.yaml")

    def test_falls_back_to_rule_packs_yaml(self) -> None:
        """Pre-v0.4.0 sparse clones only have rule-packs.yaml; alias must resolve."""
        _write(
            self.bootstrap_dir / "rule-packs.yaml", self._legacy_manifest_text()
        )
        path = compose_packs._resolve_manifest_path(self.root, None)
        self.assertEqual(path.name, "rule-packs.yaml")

    def test_prefers_packs_yaml_over_rule_packs_yaml(self) -> None:
        _write(self.bootstrap_dir / "packs.yaml", self._legacy_manifest_text())
        _write(
            self.bootstrap_dir / "rule-packs.yaml", self._legacy_manifest_text()
        )
        path = compose_packs._resolve_manifest_path(self.root, None)
        self.assertEqual(path.name, "packs.yaml")

    def test_explicit_manifest_overrides_default(self) -> None:
        explicit = self.root / "custom.yaml"
        _write(explicit, self._legacy_manifest_text())
        path = compose_packs._resolve_manifest_path(self.root, explicit)
        self.assertEqual(path, explicit)


class V2CompositionTests(unittest.TestCase):
    """V2 manifests are dispatched through the unified composer in Phase 3.
    The Phase-1 rejection branches (now obsolete) have been replaced by
    real dispatch; these tests exercise the error paths that remain at
    the wrapper layer."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
        self.bootstrap_dir.mkdir(parents=True)

    def test_schema_error_surfaces_as_exit_1(self) -> None:
        _write(
            self.bootstrap_dir / "packs.yaml",
            "version: 2\npacks:\n  - name: x\n    source:\n"
            "      repo: git@github.com:me/private.git\n",
        )
        rc, _out, err = _invoke(["--root", str(self.root)])
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)


class PrintYamlHelperTests(unittest.TestCase):
    """--print-yaml is pure stdout; delegate unchanged to the v0.3.x helper."""

    def test_print_yaml_delegates(self) -> None:
        rc, out, _err = _invoke(["--print-yaml", "agent-style"])
        self.assertEqual(rc, 0)
        self.assertIn("agent-config.yaml", out)
        self.assertIn("agent-style", out)


class CLIEntryPointTests(unittest.TestCase):
    """compose_packs.py runs as a standalone script under the same Python
    interpreter bootstrap.sh / bootstrap.ps1 use. Verify sys.path handling
    resolves `compose_rule_packs` and `packs.schema` imports when invoked
    by absolute path (the shape bootstrap uses)."""

    def test_script_invocation_imports_resolve(self) -> None:
        script = ROOT / "scripts" / "compose_packs.py"
        result = subprocess.run(
            [sys.executable, str(script), "--print-yaml", "agent-style"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("agent-config.yaml", result.stdout)


@unittest.skipUnless(
    codex_links.platform_supported() and shutil.which("git") is not None,
    "links need macOS or Linux and git",
)
class CodexLocalSkillWiringTests(unittest.TestCase):
    """Compose wiring for project-local Codex skill links."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.root = base / "consumer"
        self.root.mkdir()
        self.home = base / "home"
        self.home.mkdir()
        _write(self.root / ".agent-config" / "AGENTS.md", "# upstream\n")
        _write(
            self.root / ".agent-config" / "repo" / "skills" / "alpha" / "SKILL.md",
            "# alpha\n",
        )
        self.pack = {
            "name": "pack-a",
            "source": "bundled:aa",
            "hosts": ["claude-code", "codex"],
            "active": [
                {
                    "kind": "skill",
                    "files": [{"from": "skills/alpha/", "to": ".claude/skills/alpha/"}],
                }
            ],
        }

    def _compose(self, packs: list[dict]) -> tuple[int, str, str]:
        by_name = {pack["name"]: pack for pack in packs}
        selections = [{"name": pack["name"], "ref": "bundled"} for pack in packs]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.object(
            compose_packs.config_mod, "resolved_for_project", return_value=selections
        ), patch.object(
            compose_packs,
            "_process_selection",
            side_effect=lambda selection, **_kw: (by_name[selection["name"]], None),
        ), patch.object(compose_packs.Path, "home", return_value=self.home):
            rc = compose_packs._do_compose_v2(
                self.root, {"version": 2, "packs": []}, no_cache=True
            )
        return rc, out.getvalue(), err.getvalue()

    def test_no_pack_change_links_newly_added_local_skill(self) -> None:
        rc, _out, err = self._compose([self.pack])
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            os.readlink(self.root / ".agents" / "skills" / "alpha"),
            "../../.claude/skills/alpha",
        )

        _write(self.root / "skills" / "local-skill" / "SKILL.md", "# local\n")
        rc, out, err = self._compose([self.pack])
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            os.readlink(self.root / ".agents" / "skills" / "local-skill"),
            "../../skills/local-skill",
        )
        self.assertIn("Codex skill links: 2 linked, 1 new.", out)

    def test_opt_out_packs_empty_leaves_links_alone(self) -> None:
        _write(self.root / "skills" / "local-skill" / "SKILL.md", "# local\n")
        rc, _out, err = self._compose([self.pack])
        self.assertEqual(rc, 0, err)
        _write(self.root / "skills" / "added-later" / "SKILL.md", "# later\n")

        rc, _out, err = self._compose([])
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.root / ".agents" / "skills" / "alpha").is_symlink())
        self.assertTrue((self.root / ".agents" / "skills" / "local-skill").is_symlink())
        self.assertFalse(os.path.lexists(self.root / ".agents" / "skills" / "added-later"))


if __name__ == "__main__":
    unittest.main()
