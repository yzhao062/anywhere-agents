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
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compose_packs  # noqa: E402


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


class V2CompositionRejectTests(unittest.TestCase):
    """Passive-only version-2 manifests are accepted by the schema parser but
    rejected for composition by the Phase 1 wrapper, so callers see a clear
    Phase-1-specific error rather than the legacy composer's generic
    'version unsupported' message bubbling up from a deeper layer."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
        self.bootstrap_dir.mkdir(parents=True)

    def test_passive_only_v2_rejected_at_phase_1(self) -> None:
        _write(
            self.bootstrap_dir / "packs.yaml",
            "version: 2\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source:\n"
            "      repo: https://github.com/example/agent-style\n"
            "      ref: v0.3.2\n"
            "    passive:\n"
            "      - files: [{from: docs/rule-pack.md, to: AGENTS.md}]\n",
        )
        rc, _out, err = _invoke(["--root", str(self.root)])
        self.assertEqual(rc, 2)
        self.assertIn("version-2", err)
        self.assertIn("Phase 1 build", err)

    def test_active_entry_rejected_with_phase_3_message(self) -> None:
        _write(
            self.bootstrap_dir / "packs.yaml",
            "version: 2\n"
            "packs:\n"
            "  - name: implement-review\n"
            "    active:\n"
            "      - kind: skill\n"
            "        hosts: [claude-code]\n"
            "        files: [{from: skills/x/, to: .claude/skills/x/}]\n",
        )
        rc, _out, err = _invoke(["--root", str(self.root)])
        self.assertEqual(rc, 2)
        self.assertIn("active", err)
        self.assertIn("Phase 3", err)

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


if __name__ == "__main__":
    unittest.main()
