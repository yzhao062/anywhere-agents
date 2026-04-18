"""Tests for session_bootstrap.py hook. Subprocess-based to mirror how
Claude Code invokes it on SessionStart. Uses temp `HOME` / `USERPROFILE`
overrides so the legacy-flag cleanup cannot touch the developer's real
`~/.claude/hooks/*.json`, and pre-populates `version-cache.json` with a
fresh timestamp so the hook never hits the npm registry during tests.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSION_BOOTSTRAP = ROOT / "scripts" / "session_bootstrap.py"


def _make_fresh_cache(hooks_dir: Path) -> None:
    """Pre-populate version-cache.json so update_version_cache short-circuits."""
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "version-cache.json").write_text(
        json.dumps(
            {
                "checked_at": time.time(),
                "claude_latest": "0.0.0",
                "codex_latest": "0.0.0",
            }
        )
    )


def run_session_bootstrap(cwd: str, env_overrides: dict | None = None, timeout: int = 60):
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, str(SESSION_BOOTSTRAP)],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def _make_consumer(tmp: Path) -> Path:
    agent_dir = tmp / ".agent-config"
    agent_dir.mkdir(parents=True)
    # A marker so _find_consumer_root treats this as a consumer.
    (agent_dir / "bootstrap.sh").write_text("# marker\n")
    return agent_dir


class ConsumerRootEventWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp_project = tempfile.mkdtemp(prefix="sb-proj-")
        self.tmp_home = tempfile.mkdtemp(prefix="sb-home-")
        self.agent_dir = _make_consumer(Path(self.tmp_project))
        _make_fresh_cache(Path(self.tmp_home) / ".claude" / "hooks")
        self.env = {"HOME": self.tmp_home, "USERPROFILE": self.tmp_home}

    def tearDown(self):
        shutil.rmtree(self.tmp_project, ignore_errors=True)
        shutil.rmtree(self.tmp_home, ignore_errors=True)

    def test_writes_event_at_consumer_root_from_cwd(self):
        rc, out, err = run_session_bootstrap(self.tmp_project, env_overrides=self.env)
        self.assertEqual(rc, 0, msg=err)
        event_path = self.agent_dir / "session-event.json"
        self.assertTrue(event_path.exists(), msg=f"missing event file; stderr={err}")
        data = json.loads(event_path.read_text())
        self.assertIn("ts", data)
        self.assertIsInstance(data["ts"], (int, float))

    def test_walks_up_from_nested_cwd(self):
        nested = Path(self.tmp_project) / "src" / "nested"
        nested.mkdir(parents=True)
        rc, out, err = run_session_bootstrap(str(nested), env_overrides=self.env)
        self.assertEqual(rc, 0, msg=err)
        # Event must land at the root .agent-config/, NOT under nested/.
        self.assertTrue((self.agent_dir / "session-event.json").exists())
        self.assertFalse(
            (nested / ".agent-config" / "session-event.json").exists(),
            msg="event file was written at nested cwd instead of the walked-up consumer root",
        )

    def test_bootstrap_command_resolves_from_consumer_root(self):
        """Regression guard: a future edit could keep write_session_event
        walk-up correct but accidentally resolve the bootstrap subprocess
        path from raw cwd. Create a platform-specific bootstrap script at
        the ROOT that touches a sentinel, launch from a nested cwd, and
        assert the sentinel appears only at the root.
        """
        sentinel = self.agent_dir / "bootstrap-ran.txt"
        if platform.system() == "Windows":
            ps_script = self.agent_dir / "bootstrap.ps1"
            ps_script.write_text(
                "New-Item -ItemType File -Path '"
                + str(sentinel).replace("'", "''")
                + "' -Force | Out-Null\n"
            )
        else:
            sh_script = self.agent_dir / "bootstrap.sh"
            sh_script.write_text(
                "#!/bin/bash\ntouch '"
                + str(sentinel).replace("'", "'\\''")
                + "'\n"
            )
        nested = Path(self.tmp_project) / "src" / "nested"
        nested.mkdir(parents=True)
        rc, out, err = run_session_bootstrap(str(nested), env_overrides=self.env)
        self.assertEqual(rc, 0, msg=err)
        self.assertTrue(
            sentinel.exists(),
            msg=(
                "bootstrap sentinel was not created — the subprocess command "
                f"was not resolved from the consumer root; stderr={err}"
            ),
        )
        # And no nested .agent-config/ should exist.
        self.assertFalse(
            (nested / ".agent-config").exists(),
            msg="session_bootstrap.py created .agent-config/ in the nested cwd",
        )


class UnrelatedDirectoryTests(unittest.TestCase):
    def test_no_state_mutation_in_unrelated_dir(self):
        tmp = tempfile.mkdtemp(prefix="sb-unrelated-")
        home = tempfile.mkdtemp(prefix="sb-home-")
        try:
            _make_fresh_cache(Path(home) / ".claude" / "hooks")
            rc, out, err = run_session_bootstrap(
                tmp, env_overrides={"HOME": home, "USERPROFILE": home}
            )
            self.assertEqual(rc, 0, msg=err)
            self.assertFalse(
                (Path(tmp) / ".agent-config").exists(),
                msg="session_bootstrap.py should not create .agent-config/ in an unrelated dir",
            )
            self.assertEqual(out.strip(), "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)


class LegacyCleanupTests(unittest.TestCase):
    def test_removes_legacy_flag_files(self):
        tmp_project = tempfile.mkdtemp(prefix="sb-legacy-proj-")
        home = tempfile.mkdtemp(prefix="sb-legacy-home-")
        try:
            _make_consumer(Path(tmp_project))
            legacy_hooks = Path(home) / ".claude" / "hooks"
            legacy_hooks.mkdir(parents=True)
            (legacy_hooks / "session-event.json").write_text('{"ts": 1}')
            (legacy_hooks / "banner-emitted.json").write_text('{"ts": 1}')
            _make_fresh_cache(legacy_hooks)
            rc, out, err = run_session_bootstrap(
                tmp_project, env_overrides={"HOME": home, "USERPROFILE": home}
            )
            self.assertEqual(rc, 0, msg=err)
            self.assertFalse((legacy_hooks / "session-event.json").exists())
            self.assertFalse((legacy_hooks / "banner-emitted.json").exists())
            # version-cache.json must NOT be deleted.
            self.assertTrue((legacy_hooks / "version-cache.json").exists())
        finally:
            shutil.rmtree(tmp_project, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)

    def test_no_op_when_no_legacy_files(self):
        tmp_project = tempfile.mkdtemp(prefix="sb-nolegacy-proj-")
        home = tempfile.mkdtemp(prefix="sb-nolegacy-home-")
        try:
            _make_consumer(Path(tmp_project))
            _make_fresh_cache(Path(home) / ".claude" / "hooks")
            rc, out, err = run_session_bootstrap(
                tmp_project, env_overrides={"HOME": home, "USERPROFILE": home}
            )
            self.assertEqual(rc, 0, msg=err)
        finally:
            shutil.rmtree(tmp_project, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)


class SourceRepoTests(unittest.TestCase):
    def test_source_repo_without_agent_config_no_event_write(self):
        """cwd has bootstrap/ + skills/ but no .agent-config/ (source repo
        layout). No per-project event file can exist; the script must exit
        cleanly without creating .agent-config/ anywhere."""
        tmp = tempfile.mkdtemp(prefix="sb-source-")
        home = tempfile.mkdtemp(prefix="sb-home-")
        try:
            os.makedirs(os.path.join(tmp, "bootstrap"))
            Path(tmp, "bootstrap", "bootstrap.sh").write_text("# src\n")
            Path(tmp, "bootstrap", "bootstrap.ps1").write_text("# src\n")
            os.makedirs(os.path.join(tmp, "skills"))
            _make_fresh_cache(Path(home) / ".claude" / "hooks")
            rc, out, err = run_session_bootstrap(
                tmp, env_overrides={"HOME": home, "USERPROFILE": home}
            )
            self.assertEqual(rc, 0, msg=err)
            # Source repo gets is_source_repo=True → _cleanup_legacy_flag_files
            # runs but no .agent-config/ is created (there is nowhere to write
            # a per-project event file).
            self.assertFalse((Path(tmp) / ".agent-config").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
