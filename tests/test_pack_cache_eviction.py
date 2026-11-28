"""Pack cache eviction: slots the committed pack-lock no longer names."""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compose_packs  # noqa: E402
from packs import source_fetch  # noqa: E402
from packs import state as state_mod  # noqa: E402

URL = "https://github.com/owner/pack"
OLD = "a" * 40
LOCKED = "b" * 40
HEAD = "c" * 40
FRESH = "d" * 40
DAY = 24 * 3600


def _make_slot(cache: Path, sha: str, age: float, url: str = URL) -> Path:
    slot = cache / source_fetch.compute_cache_key(url, sha)
    (slot / ".git").mkdir(parents=True)
    (slot / "pack.yaml").write_text("name: pack\n")
    stamp = time.time() - age
    os.utime(slot, (stamp, stamp))
    return slot


def _lock(resolved: str = LOCKED, head: str | None = HEAD) -> dict:
    entry = {
        "source_url": URL,
        "requested_ref": "main",
        "resolved_commit": resolved,
        "files": [],
    }
    if head is not None:
        entry["latest_known_head"] = head
    return {
        "version": state_mod.SCHEMA_VERSION,
        "packs": {
            "pack": entry,
            "core": {
                "source_url": "bundled:aa",
                "requested_ref": "bundled",
                "resolved_commit": "bundled",
                "files": [],
            },
        },
    }


class ReferencedCacheKeysTest(unittest.TestCase):
    def test_both_lock_commits_count_and_bundled_entries_do_not(self):
        keys = source_fetch.referenced_cache_keys(_lock())
        self.assertEqual(
            keys,
            {
                source_fetch.compute_cache_key(URL, LOCKED),
                source_fetch.compute_cache_key(URL, HEAD),
            },
        )

    def test_uppercase_commit_matches_the_lowercase_slot(self):
        keys = source_fetch.referenced_cache_keys(_lock(resolved=LOCKED.upper(), head=None))
        self.assertEqual(keys, {source_fetch.compute_cache_key(URL, LOCKED)})

    def test_malformed_payloads_name_nothing(self):
        for payload in (None, {}, {"packs": []}, {"packs": {"p": "x"}}):
            self.assertEqual(source_fetch.referenced_cache_keys(payload), set())


class WindowsLinkGuardTest(unittest.TestCase):
    """Simulated Windows behavior, so the guard is checked on every platform."""

    def test_a_reparse_point_is_never_a_real_directory(self):
        import stat as stat_mod
        from types import SimpleNamespace

        junction = SimpleNamespace(st_mode=stat_mod.S_IFDIR | 0o755, st_file_attributes=0x400)
        plain = SimpleNamespace(st_mode=stat_mod.S_IFDIR | 0o755, st_file_attributes=0x10)
        self.assertFalse(source_fetch._is_real_dir(junction))
        self.assertTrue(source_fetch._is_real_dir(plain))

    def test_the_eviction_path_helper_never_resolves_links(self):
        class StubPath:
            """Stands in for WindowsPath, which cannot be built off Windows."""

            def __init__(self, raw):
                self.raw = str(raw)

            def __str__(self):
                return self.raw

            def absolute(self):
                return "C:\\repo\\.agent-config\\cache\\slot"

            def resolve(self):
                raise AssertionError("resolve() follows junctions")

        with patch.object(source_fetch.os, "name", "nt"), patch.object(
            source_fetch.pathlib, "Path", StubPath
        ):
            out = source_fetch._lexical_path("cache\\slot")
        self.assertEqual(out, "\\\\?\\C:\\repo\\.agent-config\\cache\\slot")


class ReadOnlyRetryTest(unittest.TestCase):
    """The rmtree hook retries only a Windows read-only plain file."""

    def _stat(self, attributes, nlink=1, mode=0o100444):
        from types import SimpleNamespace
        return SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_nlink=nlink)

    def _run(self, os_name, exc, st, func=None):
        func = func or MagicMock(name="unlink")
        with patch.object(source_fetch.os, "name", os_name), \
                patch.object(source_fetch.os, "lstat", return_value=st), \
                patch.object(source_fetch.os, "chmod") as chmod, \
                patch.object(source_fetch.os, "unlink", func), \
                patch.object(source_fetch.os, "remove", func):
            try:
                source_fetch._retry_plain_readonly_file(func, "p", (type(exc), exc, None))
                return "retried", chmod
            except OSError:
                return "raised", chmod

    def test_windows_read_only_plain_file_is_retried_once(self):
        unlink = MagicMock(name="unlink")
        outcome, chmod = self._run("nt", PermissionError("ro"), self._stat(0x1), unlink)
        self.assertEqual(outcome, "retried")
        chmod.assert_called_once_with("p", 0o100444 | stat.S_IWRITE)
        unlink.assert_called_once_with("p")

    def test_everything_else_re_raises_without_changing_a_mode(self):
        cases = [
            ("posix", PermissionError("ro"), self._stat(0x1)),
            ("nt", OSError("busy"), self._stat(0x1)),
            ("nt", PermissionError("ro"), self._stat(0x0)),
            ("nt", PermissionError("ro"), self._stat(0x1 | 0x400)),
            ("nt", PermissionError("ro"), self._stat(0x1, nlink=2)),
            ("nt", PermissionError("ro"), self._stat(0x1, mode=0o040555)),
        ]
        for os_name, exc, st in cases:
            outcome, chmod = self._run(os_name, exc, st)
            self.assertEqual(outcome, "raised", (os_name, exc, st))
            chmod.assert_not_called()


class EvictUnreferencedSlotsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.cache = self.base / "cache"
        self.cache.mkdir()
        self.keep = source_fetch.referenced_cache_keys(_lock())

    def tearDown(self):
        self._tmp.cleanup()

    def test_only_old_unreferenced_slots_go(self):
        old = _make_slot(self.cache, OLD, 3 * DAY)
        locked = _make_slot(self.cache, LOCKED, 30 * DAY)
        head = _make_slot(self.cache, HEAD, 30 * DAY)
        fresh = _make_slot(self.cache, FRESH, 60)
        stray = self.cache / "notes"
        stray.mkdir()
        removed, errors = source_fetch.evict_unreferenced_slots(self.cache, self.keep)
        self.assertEqual(errors, [])
        self.assertEqual(removed, [source_fetch.compute_cache_key(URL, OLD)])
        self.assertFalse(old.exists())
        for survivor in (locked, head, fresh, stray):
            self.assertTrue(survivor.exists(), survivor)

    def test_a_key_directory_left_empty_is_removed(self):
        other = "https://github.com/owner/other"
        gone = _make_slot(self.cache, OLD, 3 * DAY, url=other)
        source_fetch.evict_unreferenced_slots(self.cache, self.keep)
        self.assertFalse(gone.exists())
        self.assertFalse(gone.parent.exists())
        kept_parent = _make_slot(self.cache, LOCKED, 3 * DAY).parent
        source_fetch.evict_unreferenced_slots(self.cache, self.keep)
        self.assertTrue(kept_parent.exists())

    @unittest.skipUnless(hasattr(os, "symlink") and os.name != "nt", "POSIX symlinks")
    def test_links_are_never_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("must survive")
        key_dir = self.cache / source_fetch.compute_cache_key(URL, OLD).split("/")[0]
        key_dir.mkdir()
        os.symlink(outside, key_dir / OLD)
        linked_key = self.cache / ("f" * 16)
        os.symlink(outside, linked_key)
        removed, errors = source_fetch.evict_unreferenced_slots(self.cache, self.keep)
        self.assertEqual((removed, errors), ([], []))
        self.assertTrue(sentinel.exists())
        self.assertTrue(os.path.islink(key_dir / OLD))
        self.assertTrue(os.path.islink(linked_key))

    @unittest.skipUnless(hasattr(os, "symlink") and os.name != "nt", "POSIX symlinks")
    def test_a_linked_cache_root_is_left_alone(self):
        real = self.base / "real-cache"
        real.mkdir()
        slot = _make_slot(real, OLD, 3 * DAY)
        linked = self.base / "linked-cache"
        os.symlink(real, linked)
        self.assertEqual(source_fetch.evict_unreferenced_slots(linked, self.keep), ([], []))
        self.assertTrue(slot.exists())

    @unittest.skipUnless(hasattr(os, "symlink") and os.name != "nt", "POSIX symlinks")
    def test_a_linked_parent_directory_is_left_alone(self):
        real_parent = self.base / "real-agent-config"
        slot = _make_slot(real_parent / "cache", OLD, 3 * DAY)
        linked_parent = self.base / "linked-agent-config"
        os.symlink(real_parent, linked_parent)
        self.assertEqual(
            source_fetch.evict_unreferenced_slots(linked_parent / "cache", self.keep), ([], [])
        )
        self.assertTrue(slot.exists())

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "POSIX permissions as a normal user")
    def test_a_slot_that_cannot_be_removed_survives_and_is_reported(self):
        slot = _make_slot(self.cache, OLD, 3 * DAY)
        inner = slot / ".git"
        (inner / "pack").write_text("x")
        os.chmod(inner / "pack", 0o644)
        os.chmod(inner, 0o500)
        try:
            removed, errors = source_fetch.evict_unreferenced_slots(self.cache, self.keep)
        finally:
            os.chmod(inner, 0o700)
        self.assertEqual(removed, [])
        self.assertEqual(len(errors), 1)
        self.assertTrue((inner / "pack").exists())
        self.assertEqual(os.stat(inner / "pack").st_mode & 0o777, 0o644)

    def test_a_key_directory_cleanup_error_is_reported(self):
        gone = _make_slot(self.cache, OLD, 3 * DAY)
        real_rmdir = os.rmdir

        def refuse(path, *args, **kwargs):
            if str(path).endswith(gone.parent.name):
                raise PermissionError("denied")
            return real_rmdir(path, *args, **kwargs)

        with patch.object(source_fetch.os, "rmdir", side_effect=refuse):
            removed, errors = source_fetch.evict_unreferenced_slots(self.cache, self.keep)
        self.assertEqual(removed, [source_fetch.compute_cache_key(URL, OLD)])
        self.assertEqual(len(errors), 1)
        self.assertIn("denied", errors[0])

    def test_a_missing_cache_root_is_not_an_error(self):
        self.assertEqual(
            source_fetch.evict_unreferenced_slots(self.base / "absent", self.keep), ([], [])
        )

    def test_a_removal_failure_is_reported_not_raised(self):
        slot = _make_slot(self.cache, OLD, 3 * DAY)
        with patch.object(source_fetch, "_remove_slot", side_effect=OSError("busy")):
            removed, errors = source_fetch.evict_unreferenced_slots(self.cache, self.keep)
        self.assertEqual(removed, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("busy", errors[0])
        self.assertTrue(slot.exists())


class ComposerEvictionGateTest(unittest.TestCase):
    """compose_packs._evict_stale_cache_slots runs only on a clean apply."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.agent_dir = self.root / ".agent-config"
        self.cache = self.agent_dir / "cache"
        self.cache.mkdir(parents=True)
        self.old = _make_slot(self.cache, OLD, 3 * DAY)
        self.locked = _make_slot(self.cache, LOCKED, 30 * DAY)
        (self.agent_dir / "pack-lock.json").write_text(json.dumps(_lock()))

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, decision="apply", per_pack=None):
        err = io.StringIO()
        with redirect_stderr(err):
            removed = compose_packs._evict_stale_cache_slots(
                self.root, decision, per_pack or {}
            )
        return removed, err.getvalue()

    def test_a_clean_apply_evicts_and_keeps_the_locked_slot(self):
        removed, err = self._run()
        self.assertEqual(removed, [source_fetch.compute_cache_key(URL, OLD)])
        self.assertEqual(err, "")
        self.assertFalse(self.old.exists())
        self.assertTrue(self.locked.exists())

    def test_a_skipped_or_deferred_update_keeps_everything(self):
        self.assertEqual(self._run(decision="skip")[0], [])
        self.assertEqual(self._run(per_pack={"pack": "skip"})[0], [])
        (self.agent_dir / "pending-updates.json").write_text("{}")
        self.assertEqual(self._run()[0], [])
        self.assertTrue(self.old.exists())

    def test_a_leftover_staging_transaction_keeps_everything(self):
        (self.agent_dir / "pack-compose.staging-123").mkdir()
        self.assertEqual(self._run()[0], [])
        self.assertTrue(self.old.exists())

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "POSIX permissions as a normal user")
    def test_an_unlistable_agent_dir_keeps_everything(self):
        (self.agent_dir / "pack-compose.staging-123").mkdir()
        os.chmod(self.agent_dir, 0o311)
        try:
            removed, err = self._run()
        finally:
            os.chmod(self.agent_dir, 0o755)
        self.assertEqual(removed, [])
        self.assertIn("pack cache eviction skipped", err)
        self.assertTrue(self.old.exists())

    def test_a_missing_or_unreadable_lock_keeps_everything(self):
        lock = self.agent_dir / "pack-lock.json"
        lock.write_text("{not json")
        removed, err = self._run()
        self.assertEqual(removed, [])
        self.assertIn("pack cache eviction skipped", err)
        lock.unlink()
        self.assertEqual(self._run()[0], [])
        self.assertTrue(self.old.exists())

    def test_a_lock_that_names_no_slot_keeps_everything(self):
        payload = _lock()
        del payload["packs"]["pack"]
        (self.agent_dir / "pack-lock.json").write_text(json.dumps(payload))
        self.assertEqual(self._run()[0], [])
        self.assertTrue(self.old.exists())

    def test_an_eviction_error_becomes_a_warning(self):
        with patch.object(source_fetch, "_remove_slot", side_effect=OSError("busy")):
            removed, err = self._run()
        self.assertEqual(removed, [])
        self.assertIn("warning: pack cache eviction:", err)
        self.assertTrue(self.old.exists())


if __name__ == "__main__":
    unittest.main()
