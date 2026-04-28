"""source_fetch.py tests for v0.5.0."""
from __future__ import annotations

import dataclasses
import hashlib
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.packs import source_fetch  # noqa: E402


class TestComputeCacheKey(unittest.TestCase):
    def test_github_https_and_ssh_share_cache_key(self):
        """github.com URLs in different forms key into the same cache slot."""
        sha = "ab12cd34" * 5
        k1 = source_fetch.compute_cache_key("https://github.com/owner/repo", sha)
        k2 = source_fetch.compute_cache_key("git@github.com:owner/repo.git", sha)
        self.assertEqual(k1, k2)

    def test_non_github_uses_url_hash(self):
        sha = "ab12cd34" * 5
        k1 = source_fetch.compute_cache_key("https://gitlab.com/owner/repo", sha)
        k2 = source_fetch.compute_cache_key("https://gitlab.com/other/repo", sha)
        self.assertNotEqual(k1, k2)

    def test_includes_resolved_commit(self):
        sha1 = "ab" * 20
        sha2 = "cd" * 20
        k1 = source_fetch.compute_cache_key("https://github.com/x/y", sha1)
        k2 = source_fetch.compute_cache_key("https://github.com/x/y", sha2)
        self.assertNotEqual(k1, k2)
        self.assertTrue(k1.endswith(sha1))
        self.assertTrue(k2.endswith(sha2))


def _make_archive(tmp: pathlib.Path, url: str, ref: str, sha: str) -> source_fetch.PackArchive:
    """Build a PackArchive backed by a real temp staging dir.

    Used by the ordering and integrity tests below so that ``shutil.move``
    can rename the staging dir into the cache slot for real, exercising
    the same code path as a live fetch.
    """
    archive_dir = tmp / f"staging-{sha[:8]}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "pack.toml").write_text(f"name = 'demo-{sha[:6]}'\n")
    return source_fetch.PackArchive(
        url=url,
        ref=ref,
        resolved_commit=sha,
        method="anonymous",
        archive_dir=archive_dir,
        canonical_id=source_fetch.auth.canonical_github_identity(url),
        cache_key=source_fetch.compute_cache_key(url, sha),
    )


class TestFetchPackOrdering(unittest.TestCase):
    @patch("scripts.packs.auth.reject_credential_url")
    @patch("scripts.packs.auth.resolve_ref_with_auth_chain")
    @patch("scripts.packs.auth.fetch_with_auth_chain")
    def test_validate_runs_before_resolve(self, fetch, resolve, reject):
        """Credential-URL rejection short-circuits before any network call."""
        reject.side_effect = source_fetch.auth.CredentialURLError("reject")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(source_fetch.auth.CredentialURLError):
                source_fetch.fetch_pack(
                    "https://ghp_x@github.com/y/z", "main",
                    cache_root=pathlib.Path(tmp) / "aa-cache-test",
                )
        # resolve and fetch should NOT have been called after rejection.
        resolve.assert_not_called()
        fetch.assert_not_called()

    @patch("scripts.packs.auth.reject_credential_url")
    @patch("scripts.packs.auth.resolve_ref_with_auth_chain")
    @patch("scripts.packs.auth.fetch_with_auth_chain")
    def test_resolve_runs_before_fetch(self, fetch, resolve, reject):
        """Resolution always precedes the cloning fetch (one network call first)."""
        sha = "ab" * 20
        resolve.return_value = (sha, "anonymous")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            fetch.return_value = _make_archive(
                tmp_path, "https://github.com/y/z", "main", sha,
            )
            archive = source_fetch.fetch_pack(
                "https://github.com/y/z", "main",
                cache_root=tmp_path / "cache",
            )
        resolve.assert_called_once()
        fetch.assert_called_once()
        # The archive's resolved commit must come from resolve's return.
        self.assertEqual(archive.resolved_commit, sha)


class TestPackLockDriftDetection(unittest.TestCase):
    @patch("scripts.packs.auth.reject_credential_url")
    @patch("scripts.packs.auth.resolve_ref_with_auth_chain")
    @patch("scripts.packs.auth.fetch_with_auth_chain")
    def test_locked_policy_drift_raises(self, fetch, resolve, reject):
        """Under ``policy=locked``, drift must abort before the fetch runs."""
        new_sha = "a" * 40
        old_sha = "b" * 40
        resolve.return_value = (new_sha, "anonymous")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(source_fetch.PackLockDriftError) as ctx:
                source_fetch.fetch_pack(
                    "https://github.com/y/z", "main",
                    policy="locked",
                    pack_lock_recorded_commit=old_sha,
                    cache_root=pathlib.Path(tmp) / "cache",
                )
        msg = str(ctx.exception)
        self.assertIn(old_sha, msg)
        self.assertIn(new_sha, msg)
        # Drift error must point the user at the remediation command.
        self.assertIn("anywhere-agents pack update", msg)
        fetch.assert_not_called()

    @patch("scripts.packs.auth.reject_credential_url")
    @patch("scripts.packs.auth.resolve_ref_with_auth_chain")
    @patch("scripts.packs.auth.fetch_with_auth_chain")
    def test_prompt_policy_drift_returns_with_new_commit(
        self, fetch, resolve, reject,
    ):
        """Under ``policy=prompt``, drift returns the new archive (compose decides)."""
        new_sha = "a" * 40
        old_sha = "b" * 40
        resolve.return_value = (new_sha, "anonymous")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            fetch.return_value = _make_archive(
                tmp_path, "https://github.com/y/z", "main", new_sha,
            )
            archive = source_fetch.fetch_pack(
                "https://github.com/y/z", "main",
                policy="prompt",
                pack_lock_recorded_commit=old_sha,
                cache_root=tmp_path / "cache",
            )
            self.assertEqual(archive.resolved_commit, new_sha)

    @patch("scripts.packs.auth.reject_credential_url")
    @patch("scripts.packs.auth.resolve_ref_with_auth_chain")
    @patch("scripts.packs.auth.fetch_with_auth_chain")
    def test_auto_policy_drift_returns_with_new_commit(
        self, fetch, resolve, reject,
    ):
        """Under ``policy=auto``, drift also flows through (compose applies)."""
        new_sha = "a" * 40
        old_sha = "b" * 40
        resolve.return_value = (new_sha, "anonymous")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            fetch.return_value = _make_archive(
                tmp_path, "https://github.com/y/z", "main", new_sha,
            )
            archive = source_fetch.fetch_pack(
                "https://github.com/y/z", "main",
                policy="auto",
                pack_lock_recorded_commit=old_sha,
                cache_root=tmp_path / "cache",
            )
            self.assertEqual(archive.resolved_commit, new_sha)


class TestCacheHitAndIntegrity(unittest.TestCase):
    """Cache lookup, dir-sha256 verification, and refetch-on-mismatch."""

    @patch("scripts.packs.auth.reject_credential_url")
    @patch("scripts.packs.auth.resolve_ref_with_auth_chain")
    @patch("scripts.packs.auth.fetch_with_auth_chain")
    def test_cache_hit_skips_fetch_and_verifies_integrity(
        self, fetch, resolve, reject,
    ):
        """Second fetch_pack call hits the cache; fetch_with_auth_chain is not re-invoked."""
        sha = "ab" * 20
        resolve.return_value = (sha, "anonymous")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            cache_root = tmp_path / "cache"
            # First call: cold cache, fetch runs and populates the slot.
            fetch.return_value = _make_archive(
                tmp_path, "https://github.com/x/y", "main", sha,
            )
            first = source_fetch.fetch_pack(
                "https://github.com/x/y", "main", cache_root=cache_root,
            )
            self.assertEqual(first.method, "anonymous")
            # Second call: hot cache, fetch must not be called again.
            fetch.reset_mock()
            second = source_fetch.fetch_pack(
                "https://github.com/x/y", "main", cache_root=cache_root,
            )
            fetch.assert_not_called()
            self.assertEqual(second.method, "cached")
            self.assertEqual(second.resolved_commit, sha)
            self.assertEqual(second.archive_dir, first.archive_dir)

    @patch("scripts.packs.auth.reject_credential_url")
    @patch("scripts.packs.auth.resolve_ref_with_auth_chain")
    @patch("scripts.packs.auth.fetch_with_auth_chain")
    def test_cache_integrity_mismatch_triggers_refetch(
        self, fetch, resolve, reject,
    ):
        """Tampered cache content -> dir-sha256 mismatch -> slot dropped and refetched."""
        sha = "ab" * 20
        resolve.return_value = (sha, "anonymous")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            cache_root = tmp_path / "cache"
            # Cold cache: populate.
            fetch.return_value = _make_archive(
                tmp_path, "https://github.com/x/y", "main", sha,
            )
            first = source_fetch.fetch_pack(
                "https://github.com/x/y", "main", cache_root=cache_root,
            )
            cache_dir = first.archive_dir
            self.assertTrue((cache_dir / ".dir-sha256").exists())

            # Tamper: rewrite a content file so dir-sha256 mismatches.
            (cache_dir / "pack.toml").write_text("tampered = true\n")

            # Second call: integrity check fails, fetch runs again.
            fetch.reset_mock()
            fetch.return_value = _make_archive(
                tmp_path, "https://github.com/x/y", "main", sha,
            )
            second = source_fetch.fetch_pack(
                "https://github.com/x/y", "main", cache_root=cache_root,
            )
            fetch.assert_called_once()
            self.assertEqual(second.method, "anonymous")
            self.assertEqual(second.resolved_commit, sha)
            # The replacement slot must have a fresh, valid dir-sha256.
            recorded = (second.archive_dir / ".dir-sha256").read_text()
            self.assertTrue(recorded.startswith("dir-sha256:"))


class TestCanonicalIdDedup(unittest.TestCase):
    """Different URL forms for the same github.com repo share one cache slot."""

    @patch("scripts.packs.auth.reject_credential_url")
    @patch("scripts.packs.auth.resolve_ref_with_auth_chain")
    @patch("scripts.packs.auth.fetch_with_auth_chain")
    def test_https_and_scp_ssh_dedupe_to_one_cache_slot(
        self, fetch, resolve, reject,
    ):
        sha = "ab" * 20
        resolve.return_value = (sha, "anonymous")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            cache_root = tmp_path / "cache"
            # First call via HTTPS form: cold cache, fetch runs.
            fetch.return_value = _make_archive(
                tmp_path, "https://github.com/owner/repo", "main", sha,
            )
            first = source_fetch.fetch_pack(
                "https://github.com/owner/repo", "main", cache_root=cache_root,
            )
            # Second call via scp-style SSH form: hot cache, fetch must be skipped.
            fetch.reset_mock()
            second = source_fetch.fetch_pack(
                "git@github.com:owner/repo.git", "main", cache_root=cache_root,
            )
            fetch.assert_not_called()
            self.assertEqual(second.method, "cached")
            self.assertEqual(second.archive_dir, first.archive_dir)
            # Both archives must point at the same cache slot.
            self.assertEqual(first.cache_key, second.cache_key)


class TestDirSha256Helpers(unittest.TestCase):
    """``_compute_dir_sha256`` / ``_iter_content_files`` semantics."""

    def test_dir_sha256_excludes_dot_git_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            # Same content file in both archives; one has bogus .git metadata.
            d1 = tmp_path / "a"
            d2 = tmp_path / "b"
            d1.mkdir()
            d2.mkdir()
            (d1 / "pack.toml").write_text("name = 'x'\n")
            (d2 / "pack.toml").write_text("name = 'x'\n")
            (d2 / ".git").mkdir()
            (d2 / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            self.assertEqual(
                source_fetch._compute_dir_sha256(d1),
                source_fetch._compute_dir_sha256(d2),
            )

    def test_dir_sha256_excludes_marker_file(self):
        """Hash must be stable across calls; the marker file is not re-hashed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            (tmp_path / "pack.toml").write_text("name = 'x'\n")
            sha1 = source_fetch._compute_dir_sha256(tmp_path)
            (tmp_path / ".dir-sha256").write_text(sha1)
            sha2 = source_fetch._compute_dir_sha256(tmp_path)
            self.assertEqual(sha1, sha2)

    def test_dir_sha256_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            (tmp_path / "f.txt").write_text("hi")
            sha = source_fetch._compute_dir_sha256(tmp_path)
            self.assertTrue(sha.startswith("dir-sha256:"))
            self.assertEqual(len(sha), len("dir-sha256:") + 64)

    def test_archive_root_recovers_nested_clone_cache_slot(self):
        """Recover cache slots produced by failed Windows stale-dir cleanup."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = pathlib.Path(tmp) / "cache-slot"
            nested = cache_dir / "aa-clone-abcd1234"
            nested.mkdir(parents=True)
            (nested / "pack.yaml").write_text("version: 2\npacks: []\n")
            (cache_dir / "skills").mkdir()

            self.assertEqual(source_fetch._archive_root(cache_dir), nested)

    def test_rmtree_existing_fails_if_stale_slot_survives(self):
        """A failed stale-cache cleanup must not be followed by nested move."""
        with tempfile.TemporaryDirectory() as tmp:
            stale = pathlib.Path(tmp) / "cache-slot"
            stale.mkdir()
            (stale / "old.txt").write_text("old\n")
            with (
                patch.object(source_fetch.shutil, "rmtree", return_value=None),
                patch.object(source_fetch, "_manual_rmtree", return_value=None),
            ):
                with self.assertRaises(OSError):
                    source_fetch._rmtree_existing(stale)

    def test_rmtree_existing_falls_back_to_manual_remove(self):
        """If platform rmtree fails, remove children manually."""
        with tempfile.TemporaryDirectory() as tmp:
            stale = pathlib.Path(tmp) / "cache-slot"
            nested = stale / "deep"
            nested.mkdir(parents=True)
            (nested / "old.txt").write_text("old\n")
            with patch.object(
                source_fetch.shutil, "rmtree", side_effect=OSError("busy"),
            ):
                source_fetch._rmtree_existing(stale)
            self.assertFalse(stale.exists())

    @unittest.skipUnless(os.name == "nt", "Windows long-path behavior")
    def test_rmtree_existing_removes_long_paths_on_windows(self):
        """Cache cleanup must handle deep pack asset paths on Windows."""
        with tempfile.TemporaryDirectory() as tmp:
            stale = pathlib.Path(tmp) / "cache-slot"
            long_dir = stale
            while len(str(long_dir / "asset.png")) < 280:
                long_dir = long_dir / "very-long-path-segment"
            os.makedirs(source_fetch._fs_path(long_dir), exist_ok=True)
            with open(source_fetch._fs_path(long_dir / "asset.png"), "w") as fh:
                fh.write("asset\n")

            source_fetch._rmtree_existing(stale)

            self.assertFalse(source_fetch._path_exists(stale))

    def test_remove_readonly_ignores_already_removed_paths(self):
        """rmtree callbacks may race a path that disappeared mid-walk."""
        def missing(_path):
            raise FileNotFoundError(_path)

        source_fetch._remove_readonly(missing, "already-gone", None)


class TestLoadCachedArchive(unittest.TestCase):
    """Pure cache lookup: no network, no auth chain, no ls-remote.

    Used by the compose skip-path to revert to a pack-lock-recorded
    commit. The buggy alternative was to call ``fetch_pack`` with the
    recorded SHA as ``ref``, which always re-runs
    ``resolve_ref_with_auth_chain`` and exhausts the auth chain because
    ls-remote does not match by SHA.
    """

    def test_returns_none_for_missing_slot(self):
        """Empty cache root, recorded commit not present anywhere."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = pathlib.Path(tmp) / "cache"
            cache_root.mkdir(parents=True, exist_ok=True)
            result = source_fetch.load_cached_archive(
                "https://github.com/x/y", "ab" * 20,
                cache_root=cache_root,
            )
            self.assertIsNone(result)

    def test_returns_none_when_marker_missing(self):
        """Cache slot exists but the dir-sha256 marker file does not.
        This is the half-populated state from a crashed prior run; we
        treat it as a miss rather than trusting un-checked content.
        """
        sha = "ab" * 20
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            cache_root = tmp_path / "cache"
            cache_key = source_fetch.compute_cache_key(
                "https://github.com/x/y", sha,
            )
            cache_dir = cache_root / cache_key
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "pack.toml").write_text("name = 'x'\n")
            # Deliberately do NOT write the .dir-sha256 marker.
            result = source_fetch.load_cached_archive(
                "https://github.com/x/y", sha,
                cache_root=cache_root,
            )
            self.assertIsNone(result)

    def test_returns_archive_on_cache_hit_with_valid_marker(self):
        """Slot present + valid marker → return PackArchive shape."""
        sha = "ab" * 20
        url = "https://github.com/x/y"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            cache_root = tmp_path / "cache"
            cache_key = source_fetch.compute_cache_key(url, sha)
            cache_dir = cache_root / cache_key
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "pack.toml").write_text("name = 'demo'\n")
            # Compute and record the dir-sha256 the way fetch_pack would.
            recorded = source_fetch._compute_dir_sha256(cache_dir)
            (cache_dir / ".dir-sha256").write_text(recorded)

            result = source_fetch.load_cached_archive(
                url, sha, cache_root=cache_root,
            )
            self.assertIsNotNone(result)
            self.assertEqual(result.url, url)
            self.assertEqual(result.ref, sha)
            self.assertEqual(result.resolved_commit, sha)
            self.assertEqual(result.method, "cached")
            self.assertEqual(result.archive_dir, cache_dir)
            self.assertEqual(result.cache_key, cache_key)
            # canonical_id is computed from the URL the same way as
            # fetch_pack records it; for github.com URLs it must match
            # canonical_github_identity.
            self.assertEqual(
                result.canonical_id,
                source_fetch.auth.canonical_github_identity(url),
            )

    def test_valid_marker_with_nested_clone_returns_nested_archive_dir(self):
        """Existing malformed cache slots are still usable after upgrade."""
        sha = "ab" * 20
        url = "https://github.com/x/y"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            cache_root = tmp_path / "cache"
            cache_key = source_fetch.compute_cache_key(url, sha)
            cache_dir = cache_root / cache_key
            nested = cache_dir / "aa-clone-nested"
            nested.mkdir(parents=True)
            (nested / "pack.yaml").write_text("version: 2\npacks: []\n")
            (cache_dir / "skills").mkdir()
            recorded = source_fetch._compute_dir_sha256(cache_dir)
            (cache_dir / ".dir-sha256").write_text(recorded)

            result = source_fetch.load_cached_archive(
                url, sha, cache_root=cache_root,
            )

            self.assertIsNotNone(result)
            self.assertEqual(result.archive_dir, nested)
            self.assertEqual(result.cache_key, cache_key)

    def test_returns_none_on_integrity_mismatch(self):
        """Marker present but content tampered → return None.
        The caller can then decide whether to fall back to the
        newly-fetched archive (compose skip path) or refetch.
        """
        sha = "ab" * 20
        url = "https://github.com/x/y"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            cache_root = tmp_path / "cache"
            cache_key = source_fetch.compute_cache_key(url, sha)
            cache_dir = cache_root / cache_key
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "pack.toml").write_text("name = 'demo'\n")
            recorded = source_fetch._compute_dir_sha256(cache_dir)
            (cache_dir / ".dir-sha256").write_text(recorded)
            # Tamper with the content after the marker was recorded.
            (cache_dir / "pack.toml").write_text("tampered = true\n")

            result = source_fetch.load_cached_archive(
                url, sha, cache_root=cache_root,
            )
            self.assertIsNone(result)

    @patch("scripts.packs.auth.resolve_ref_with_auth_chain")
    @patch("scripts.packs.auth.fetch_with_auth_chain")
    def test_load_cached_archive_makes_no_network_calls(
        self, fetch, resolve,
    ):
        """The whole point of this helper: zero network round-trips.
        Even on a miss we must not call resolve or fetch.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = pathlib.Path(tmp) / "cache"
            cache_root.mkdir(parents=True, exist_ok=True)
            source_fetch.load_cached_archive(
                "https://github.com/x/y", "ab" * 20,
                cache_root=cache_root,
            )
        resolve.assert_not_called()
        fetch.assert_not_called()


class TestPostCloneShaMismatch(unittest.TestCase):
    """Codex Round 2 H2: when ``resolve_ref_with_auth_chain`` and
    ``fetch_with_auth_chain`` disagree on the resolved commit (the
    branch / tag moved between calls), the cache slot MUST key to the
    post-clone SHA so the ``dir-sha256`` marker and ``pack-lock.json``
    attest to the actually-fetched commit.

    The previous code keyed the cache slot to the pre-clone SHA and
    returned ``resolved_commit=<pre_sha>`` even after fetching a
    different tree, so the lock and on-disk content disagreed —
    silent locked-policy drift hidden.
    """

    @patch("scripts.packs.auth.reject_credential_url")
    @patch("scripts.packs.auth.resolve_ref_with_auth_chain")
    @patch("scripts.packs.auth.fetch_with_auth_chain")
    def test_post_clone_sha_rekeys_cache_under_prompt_policy(
        self, fetch, resolve, reject,
    ):
        """``policy="prompt"`` (default): re-key the cache to the
        post-clone SHA. The returned ``PackArchive.resolved_commit``
        must be the post-clone SHA, and the cache slot must live at
        ``compute_cache_key(url, post_clone_sha)`` (NOT the pre-clone
        slot)."""
        url = "https://github.com/x/y"
        pre_sha = "a" * 40
        post_sha = "b" * 40
        resolve.return_value = (pre_sha, "anonymous")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            cache_root = tmp_path / "cache"
            fetch.return_value = _make_archive(tmp_path, url, "main", post_sha)
            archive = source_fetch.fetch_pack(
                url, "main", cache_root=cache_root,
            )
            # The returned archive must carry the POST-clone SHA.
            self.assertEqual(archive.resolved_commit, post_sha)
            # The cache slot must be keyed to the POST-clone SHA, not
            # the pre-clone SHA.
            post_key = source_fetch.compute_cache_key(url, post_sha)
            pre_key = source_fetch.compute_cache_key(url, pre_sha)
            self.assertEqual(archive.cache_key, post_key)
            self.assertTrue((cache_root / post_key).exists())
            # Pre-clone slot must NOT have been populated (the bug).
            self.assertFalse((cache_root / pre_key).exists())

    @patch("scripts.packs.auth.reject_credential_url")
    @patch("scripts.packs.auth.resolve_ref_with_auth_chain")
    @patch("scripts.packs.auth.fetch_with_auth_chain")
    def test_post_clone_sha_under_locked_with_recorded_raises_drift(
        self, fetch, resolve, reject,
    ):
        """``policy="locked"`` + ``pack_lock_recorded_commit`` set + the
        clone returned a different SHA than ``resolve`` → raise
        ``PackLockDriftError`` with ``current=<post_clone_sha>``.

        Without this guard, the locked policy is silently bypassed when
        the ref moves between the resolve and fetch network round-trips.
        """
        url = "https://github.com/x/y"
        pre_sha = "a" * 40
        post_sha = "b" * 40
        recorded = pre_sha
        resolve.return_value = (pre_sha, "anonymous")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            cache_root = tmp_path / "cache"
            fetch.return_value = _make_archive(tmp_path, url, "main", post_sha)
            with self.assertRaises(source_fetch.PackLockDriftError) as ctx:
                source_fetch.fetch_pack(
                    url, "main",
                    policy="locked",
                    pack_lock_recorded_commit=recorded,
                    cache_root=cache_root,
                )
        # The drift error must report the POST-clone SHA as ``current``,
        # not the (matching-recorded) pre-clone SHA.
        self.assertEqual(ctx.exception.current, post_sha)
        self.assertEqual(ctx.exception.recorded, recorded)


class TestPackArchiveShape(unittest.TestCase):
    """Ensure the Phase 2 PackArchive shape is preserved (Phase 2 fetch driver depends on it)."""

    def test_pack_archive_has_required_fields(self):
        archive = source_fetch.PackArchive(
            url="https://github.com/x/y",
            ref="main",
            resolved_commit="ab" * 20,
            method="anonymous",
            archive_dir=pathlib.Path("/tmp/x"),
            canonical_id="x/y",
            cache_key="abcd1234/" + "ab" * 20,
        )
        self.assertEqual(archive.url, "https://github.com/x/y")
        self.assertEqual(archive.ref, "main")
        self.assertEqual(archive.resolved_commit, "ab" * 20)
        self.assertEqual(archive.method, "anonymous")
        self.assertEqual(archive.canonical_id, "x/y")
        self.assertEqual(archive.cache_key, "abcd1234/" + "ab" * 20)
        # Frozen dataclass: assignment raises FrozenInstanceError
        # specifically (broader Exception would mask any future regression
        # where mutation silently succeeds and a different exception fires
        # for an unrelated reason).
        with self.assertRaises(dataclasses.FrozenInstanceError):
            archive.url = "other"  # type: ignore[misc]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
