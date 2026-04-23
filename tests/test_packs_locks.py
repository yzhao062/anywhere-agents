"""Tests for scripts/packs/locks.py.

Cross-platform exclusive file locks: acquire / release round-trip,
contention + timeout, holder-PID readback, is_held probe, path helpers.
"""
from __future__ import annotations

import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import locks  # noqa: E402


class _TmpDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class BasicAcquireReleaseTests(_TmpDirCase):
    def test_acquire_and_release(self) -> None:
        path = self.root / "foo.lock"
        with locks.acquire(path) as fh:
            self.assertTrue(path.exists())
            self.assertIsNotNone(fh)
        # After context exit, lock file still exists (we don't delete it;
        # next acquire re-uses it and truncates the PID).
        self.assertTrue(path.exists())

    def test_holder_pid_recorded_in_sidecar(self) -> None:
        """Holder PID is written to a sidecar file so peers can read it
        even while the lock byte range is held (mandatory on Windows)."""
        path = self.root / "foo.lock"
        sidecar = path.with_name(path.name + ".pid")
        with locks.acquire(path):
            content = sidecar.read_text(encoding="utf-8").strip()
            self.assertEqual(int(content.split()[0]), os.getpid())

    def test_sidecar_cleared_on_release(self) -> None:
        """Release must delete the sidecar so a stale PID doesn't linger."""
        path = self.root / "foo.lock"
        sidecar = path.with_name(path.name + ".pid")
        with locks.acquire(path):
            self.assertTrue(sidecar.exists())
        self.assertFalse(sidecar.exists())

    def test_lock_releases_on_context_exit_even_after_exception(self) -> None:
        path = self.root / "foo.lock"
        try:
            with locks.acquire(path):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # Should be re-acquirable immediately.
        with locks.acquire(path, timeout=2.0):
            pass


# ---------- contention + timeout ----------


def _hold_lock_for(path_str: str, hold_seconds: float, started_flag_path: str) -> None:
    """Subprocess target: acquire the lock, signal ready, hold for N seconds.

    Module-level so multiprocessing on Windows can pickle-import it.
    """
    path = Path(path_str)
    flag = Path(started_flag_path)
    with locks.acquire(path, timeout=5.0):
        flag.write_text("ready", encoding="utf-8")
        time.sleep(hold_seconds)


class ContentionTests(_TmpDirCase):
    def test_timeout_raises_lock_timeout(self) -> None:
        path = self.root / "foo.lock"
        started = self.root / "started.flag"
        proc = multiprocessing.Process(
            target=_hold_lock_for,
            args=(str(path), 3.0, str(started)),
        )
        proc.start()
        self.addCleanup(proc.join)
        # Wait for the child to signal it has the lock.
        # 15s tolerates slow CI process startup (GitHub Actions Windows
        # runners can take several seconds to launch a Python subprocess).
        deadline = time.monotonic() + 15.0
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(started.exists(), "child did not acquire in time")

        with self.assertRaises(locks.LockTimeout) as ctx:
            with locks.acquire(path, timeout=0.5):
                pass
        self.assertEqual(ctx.exception.path, path)
        # Holder PID may or may not be readable depending on timing; if
        # present it should match the child.
        if ctx.exception.holder_pid is not None:
            self.assertEqual(ctx.exception.holder_pid, proc.pid)

        proc.join(timeout=10.0)
        self.assertFalse(proc.is_alive(), "child did not finish in time")


# ---------- is_held probe ----------


class IsHeldTests(_TmpDirCase):
    def test_missing_file_not_held(self) -> None:
        path = self.root / "nope.lock"
        self.assertFalse(locks.is_held(path))

    def test_released_not_held(self) -> None:
        path = self.root / "foo.lock"
        with locks.acquire(path):
            pass
        self.assertFalse(locks.is_held(path))

    def test_held_by_peer_returns_true(self) -> None:
        path = self.root / "foo.lock"
        started = self.root / "started.flag"
        proc = multiprocessing.Process(
            target=_hold_lock_for,
            args=(str(path), 2.0, str(started)),
        )
        proc.start()
        self.addCleanup(proc.join)
        # 15s tolerates slow CI process startup (GitHub Actions Windows
        # runners can take several seconds to launch a Python subprocess).
        deadline = time.monotonic() + 15.0
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(started.exists())
        self.assertTrue(locks.is_held(path))
        proc.join(timeout=10.0)
        # After the child exits, the lock file lingers but nothing holds it.
        self.assertFalse(locks.is_held(path))


# ---------- path helpers ----------


class PathHelperTests(_TmpDirCase):
    def test_user_lock_path_default(self) -> None:
        path = locks.user_lock_path()
        self.assertTrue(str(path).endswith(os.sep + ".claude" + os.sep + ".pack-lock.lock"))

    def test_user_lock_path_custom_home(self) -> None:
        custom = self.root / "my-home"
        path = locks.user_lock_path(home=custom)
        self.assertEqual(path, custom / ".claude" / ".pack-lock.lock")

    def test_repo_lock_path(self) -> None:
        project = self.root / "project"
        path = locks.repo_lock_path(project)
        self.assertEqual(path, project / ".agent-config" / ".pack-lock.lock")


if __name__ == "__main__":
    unittest.main()
