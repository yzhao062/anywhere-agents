"""Tests for v0.5.2 AC→AA migration detection in ``anywhere-agents``.

Covers two detection signals (per PLAN-aa-v0.5.2.md § 4):

- ``.agent-config/repo/.git/config`` ``[remote "origin"]`` URL matching
  ``yzhao062/agent-config(\\.git)?$``.
- ``.agent-config/upstream`` content equals ``yzhao062/agent-config``
  (whitespace + CR stripped).

Either signal → migrate. Both unrecognized → no-op.

Also covers the cross-platform delete sequence for the migration step.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "pypi"))


class DetectLegacyACTests(unittest.TestCase):
    """Detection signals for legacy ``yzhao062/agent-config`` bootstrap."""

    def test_no_agent_config_directory_returns_false(self) -> None:
        from anywhere_agents.cli import _detect_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                self.assertFalse(_detect_legacy_ac())
            finally:
                os.chdir(cwd_before)

    def test_upstream_file_matches_legacy_ac(self) -> None:
        from anywhere_agents.cli import _detect_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / ".agent-config").mkdir()
            (project / ".agent-config" / "upstream").write_text(
                "yzhao062/agent-config\n", encoding="utf-8"
            )
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                self.assertTrue(_detect_legacy_ac())
            finally:
                os.chdir(cwd_before)

    def test_upstream_file_with_crlf_matches(self) -> None:
        """CRLF line endings on Windows: stripper must remove ``\\r\\n``."""
        from anywhere_agents.cli import _detect_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / ".agent-config").mkdir()
            (project / ".agent-config" / "upstream").write_bytes(
                b"yzhao062/agent-config\r\n"
            )
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                self.assertTrue(_detect_legacy_ac())
            finally:
                os.chdir(cwd_before)

    def test_git_config_remote_origin_matches_legacy_ac(self) -> None:
        from anywhere_agents.cli import _detect_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            git_dir = project / ".agent-config" / "repo" / ".git"
            git_dir.mkdir(parents=True)
            git_config = git_dir / "config"
            git_config.write_text(
                "[core]\n"
                "\trepositoryformatversion = 0\n"
                "[remote \"origin\"]\n"
                "\turl = https://github.com/yzhao062/agent-config.git\n"
                "\tfetch = +refs/heads/*:refs/remotes/origin/*\n",
                encoding="utf-8",
            )
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                self.assertTrue(_detect_legacy_ac())
            finally:
                os.chdir(cwd_before)

    def test_git_config_remote_origin_no_dot_git_suffix(self) -> None:
        from anywhere_agents.cli import _detect_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            git_dir = project / ".agent-config" / "repo" / ".git"
            git_dir.mkdir(parents=True)
            git_config = git_dir / "config"
            git_config.write_text(
                "[remote \"origin\"]\n"
                "\turl = https://github.com/yzhao062/agent-config\n",
                encoding="utf-8",
            )
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                self.assertTrue(_detect_legacy_ac())
            finally:
                os.chdir(cwd_before)

    def test_git_config_remote_anywhere_agents_returns_false(self) -> None:
        """Already-migrated project must NOT trigger another migration."""
        from anywhere_agents.cli import _detect_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            git_dir = project / ".agent-config" / "repo" / ".git"
            git_dir.mkdir(parents=True)
            git_config = git_dir / "config"
            git_config.write_text(
                "[remote \"origin\"]\n"
                "\turl = https://github.com/yzhao062/anywhere-agents.git\n",
                encoding="utf-8",
            )
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                self.assertFalse(_detect_legacy_ac())
            finally:
                os.chdir(cwd_before)

    def test_git_config_unrelated_remote_returns_false(self) -> None:
        from anywhere_agents.cli import _detect_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            git_dir = project / ".agent-config" / "repo" / ".git"
            git_dir.mkdir(parents=True)
            git_config = git_dir / "config"
            git_config.write_text(
                "[remote \"origin\"]\n"
                "\turl = https://github.com/some/other-repo.git\n",
                encoding="utf-8",
            )
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                self.assertFalse(_detect_legacy_ac())
            finally:
                os.chdir(cwd_before)

    def test_git_config_fork_origin_with_ac_upstream_remote_returns_false(self) -> None:
        """Round 1 regression: a third-party fork with a separate ``upstream``
        remote pointing at agent-config must NOT be classified as legacy AC.
        The detection scan must be bounded to the ``[remote "origin"]``
        section only.
        """
        from anywhere_agents.cli import _detect_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            git_dir = project / ".agent-config" / "repo" / ".git"
            git_dir.mkdir(parents=True)
            git_config = git_dir / "config"
            git_config.write_text(
                "[remote \"origin\"]\n"
                "\turl = https://github.com/example/fork.git\n"
                "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
                "[remote \"upstream\"]\n"
                "\turl = https://github.com/yzhao062/agent-config.git\n"
                "\tfetch = +refs/heads/*:refs/remotes/upstream/*\n",
                encoding="utf-8",
            )
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                self.assertFalse(_detect_legacy_ac())
            finally:
                os.chdir(cwd_before)

    def test_upstream_unrecognized_value_returns_false(self) -> None:
        from anywhere_agents.cli import _detect_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / ".agent-config").mkdir()
            (project / ".agent-config" / "upstream").write_text(
                "yzhao062/anywhere-agents\n", encoding="utf-8"
            )
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                self.assertFalse(_detect_legacy_ac())
            finally:
                os.chdir(cwd_before)

    def test_either_signal_alone_triggers_migration(self) -> None:
        """``.agent-config/upstream`` with the legacy value should trigger
        migration even when ``.git/config`` is missing entirely. Mirrors
        the consumer who bootstrapped via the old shell flow that wrote
        ``upstream`` but no longer has the cloned repo on disk.
        """
        from anywhere_agents.cli import _detect_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / ".agent-config").mkdir()
            (project / ".agent-config" / "upstream").write_text(
                "yzhao062/agent-config", encoding="utf-8"
            )
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                self.assertTrue(_detect_legacy_ac())
            finally:
                os.chdir(cwd_before)


class MigrateLegacyACTests(unittest.TestCase):
    """Cross-platform delete sequence for the migration step."""

    def test_migrate_deletes_repo_directory(self) -> None:
        from anywhere_agents.cli import _migrate_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            repo_dir = project / ".agent-config" / "repo"
            (repo_dir / "scripts").mkdir(parents=True)
            (repo_dir / "scripts" / "compose_packs.py").write_text("# stub")
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                _migrate_legacy_ac()
                self.assertFalse(repo_dir.exists())
            finally:
                os.chdir(cwd_before)

    def test_migrate_deletes_upstream_file(self) -> None:
        from anywhere_agents.cli import _migrate_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / ".agent-config").mkdir()
            upstream_file = project / ".agent-config" / "upstream"
            upstream_file.write_text("yzhao062/agent-config")
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                _migrate_legacy_ac()
                self.assertFalse(upstream_file.exists())
            finally:
                os.chdir(cwd_before)

    def test_migrate_deletes_both_bootstrap_scripts(self) -> None:
        from anywhere_agents.cli import _migrate_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / ".agent-config").mkdir()
            sh = project / ".agent-config" / "bootstrap.sh"
            ps1 = project / ".agent-config" / "bootstrap.ps1"
            sh.write_text("# stub")
            ps1.write_text("# stub")
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                _migrate_legacy_ac()
                self.assertFalse(sh.exists())
                self.assertFalse(ps1.exists())
            finally:
                os.chdir(cwd_before)

    def test_migrate_idempotent_when_artifacts_missing(self) -> None:
        from anywhere_agents.cli import _migrate_legacy_ac
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / ".agent-config").mkdir()
            cwd_before = os.getcwd()
            try:
                os.chdir(d)
                # Should not raise even when none of the artifacts exist.
                _migrate_legacy_ac()
            finally:
                os.chdir(cwd_before)


if __name__ == "__main__":
    unittest.main()
