"""v0.5.0 integration tests for ``scripts/compose_packs.py``.

Phase 6 (Deferral 1): outer-lock acquire wraps the v2 composition path so
the user-level and project-level lock files are both held while pack-state
is mutated. ``locks.LockTimeout`` translates to exit code 10, matching the
v0.4.0 uninstall contract for "could not acquire lock".

Phase 4 adds: host-identity resolution (``detect_host``), inline-source
selection branching (``_process_selection``), and threading the detected
host through ``DispatchContext.current_host`` (preserves v0.4.0 ABI per
Codex Round 2 M3).

Phase 8 adds: prompt UX (``prompt_user_for_updates``), pending-updates
JSON invariant (``write_pending_updates_json`` /
``clear_pending_updates_json``), compose summary
(``print_compose_summary``), and the carry-forwards from earlier phases
— ``validate_url_fn`` wiring (Phase 5 carry-forward A), and
``reconcile_orphans`` wiring (Phase 7 carry-forward B).

These tests run in-process: they patch ``packs.locks.acquire`` to inspect
the path / timeout each call receives without touching real lock files,
and they patch the v2 composer body with a stub so the lock wiring can be
asserted in isolation from the (separately-tested) composition pipeline.
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
sys.path.insert(0, str(ROOT / "scripts"))

import compose_packs  # noqa: E402
from packs import auth as auth_mod  # noqa: E402
from packs import locks  # noqa: E402
from packs import reconciliation as reconciliation_mod  # noqa: E402
from packs import source_fetch  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = compose_packs.main(argv)
    return rc, out_buf.getvalue(), err_buf.getvalue()


class _V2ManifestFixture(unittest.TestCase):
    """Minimal on-disk fixture so ``main`` reaches the v2 composition path
    (where the outer locks are wired)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Canonicalize: macOS resolves /var → /private/var, Windows resolves
        # 8.3 short paths (RUNNER~1) → long paths. The composer calls
        # Path.resolve() internally; if the test uses the unresolved form,
        # called-path equality assertions fail on CI runners.
        self.root = Path(self.tmp.name).resolve()
        self.bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
        self.bootstrap_dir.mkdir(parents=True)
        # v2 manifest with one minimal pack so schema parsing succeeds.
        _write(
            self.bootstrap_dir / "packs.yaml",
            "version: 2\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source: bundled\n"
            "    default-ref: bundled\n",
        )
        # Upstream AGENTS.md so v2 composition does not error before we
        # reach the lock-wrapped body.
        _write(self.root / ".agent-config" / "AGENTS.md", "# upstream\n")


class OuterLockAcquireTests(_V2ManifestFixture):
    """Phase 6 Step 1 contract: v2 composition acquires the per-user lock
    AND the per-repo lock at the top of the transaction; both are released
    on the way out."""

    def test_compose_acquires_both_locks_at_entry(self) -> None:
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None

        with patch.object(locks, "acquire", side_effect=_ctx) as acquire, \
                patch.object(
                    compose_packs, "_do_compose_v2", return_value=0
                ) as inner:
            rc, _out, _err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 0)
        self.assertEqual(acquire.call_count, 2)
        # The two acquire() calls must use distinct paths (one user-level,
        # one project-level). Order is fixed by the implementation: user
        # first so two repos contending for user state serialize cleanly.
        called_paths = [
            call.args[0] if call.args else call.kwargs["path"]
            for call in acquire.call_args_list
        ]
        self.assertEqual(len(called_paths), 2)
        self.assertEqual(len(set(called_paths)), 2)
        self.assertEqual(called_paths[0], locks.user_lock_path(Path.home()))
        self.assertEqual(called_paths[1], locks.repo_lock_path(self.root))
        # Both calls use the documented 30-second default.
        for call in acquire.call_args_list:
            timeout = call.kwargs.get("timeout")
            if timeout is None and len(call.args) > 1:
                timeout = call.args[1]
            self.assertEqual(timeout, 30)
        # Inner composer was reached only after locks were held.
        inner.assert_called_once()

    def test_lock_timeout_exits_10(self) -> None:
        """If the user-level lock is still held after the timeout, surface
        ``locks.LockTimeout`` as exit code 10 with an actionable stderr
        line (path + holder PID hint)."""
        # Use a path under the test root so str() renders consistently
        # on POSIX and Windows (mixed forward / backslash separators).
        lock_path = self.root / "fake-user.lock"
        timeout_exc = locks.LockTimeout(lock_path, 30, holder_pid=12345)

        with patch.object(locks, "acquire", side_effect=timeout_exc):
            rc, _out, err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 10)
        self.assertIn("compose aborted", err)
        self.assertIn("fake-user.lock", err)
        self.assertIn("12345", err)

    def test_repo_lock_timeout_exits_10(self) -> None:
        """User lock acquired, but the per-repo lock is contended: still
        exit 10. The user lock context unwinds cleanly because LockTimeout
        propagates out of the inner ``with``."""
        @contextmanager
        def _ok(*_args, **_kwargs):
            yield None

        timeout_exc = locks.LockTimeout(
            self.root / ".agent-config" / ".pack-lock.lock",
            30,
            holder_pid=None,
        )

        # First call (user lock) succeeds; second call (repo lock) raises.
        side_effects = [_ok(), timeout_exc]

        def _acquire(*_args, **_kwargs):
            effect = side_effects.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            return effect

        with patch.object(locks, "acquire", side_effect=_acquire):
            rc, _out, err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 10)
        self.assertIn("compose aborted", err)

    def test_inner_nonzero_return_releases_both_locks(self) -> None:
        """A non-zero return from ``_do_compose_v2`` must not leave either
        lock held: both context managers unwind via normal-return through
        the chained ``with`` and the rc propagates to the caller."""
        released = {"user": False, "repo": False}

        @contextmanager
        def _ctx(path, timeout=None):
            if path == locks.user_lock_path(Path.home()):
                key = "user"
            else:
                key = "repo"
            try:
                yield None
            finally:
                released[key] = True

        with patch.object(locks, "acquire", side_effect=_ctx), \
                patch.object(
                    compose_packs, "_do_compose_v2", return_value=1
                ):
            rc, _out, _err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 1)
        self.assertTrue(released["user"])
        self.assertTrue(released["repo"])

    def test_inner_raise_releases_both_locks(self) -> None:
        """A raise inside ``_do_compose_v2`` must also unwind both
        context managers (the Pythonic ``with`` chain handles this, but
        we pin it as a behavioral guarantee). The exception propagates."""
        released = {"user": False, "repo": False}

        @contextmanager
        def _ctx(path, timeout=None):
            if path == locks.user_lock_path(Path.home()):
                key = "user"
            else:
                key = "repo"
            try:
                yield None
            finally:
                released[key] = True

        with patch.object(locks, "acquire", side_effect=_ctx), \
                patch.object(
                    compose_packs,
                    "_do_compose_v2",
                    side_effect=RuntimeError("inner failure"),
                ):
            with self.assertRaises(RuntimeError):
                _invoke(["--root", str(self.root)])

        self.assertTrue(released["user"])
        self.assertTrue(released["repo"])


class LockBypassTests(_V2ManifestFixture):
    """``--print-yaml`` is a stdout-only helper; it must not contend with
    a peer composer. v1-legacy delegation also writes only AGENTS.md and
    is handled by the legacy atomic-write path; outer locks are reserved
    for v2 transactions where pack-state.json may mutate."""

    def test_print_yaml_does_not_acquire_locks(self) -> None:
        with patch.object(locks, "acquire") as acquire:
            rc, out, _err = _invoke(["--print-yaml", "agent-style"])
        self.assertEqual(rc, 0)
        self.assertIn("agent-style", out)
        acquire.assert_not_called()

    def test_v1_manifest_does_not_acquire_locks(self) -> None:
        # Replace the v2 manifest with a v1 (legacy) one.
        _write(
            self.bootstrap_dir / "packs.yaml",
            "version: 1\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source: https://example.com/{ref}/rule-pack.md\n"
            "    default-ref: v0.3.2\n",
        )
        with patch.object(locks, "acquire") as acquire, \
                patch.object(
                    compose_packs, "legacy"
                ) as legacy_mod:
            legacy_mod.main.return_value = 0
            rc, _out, _err = _invoke(["--root", str(self.root)])
        self.assertEqual(rc, 0)
        acquire.assert_not_called()


# =====================================================================
# Phase 4: detect_host() - host identity resolution.
# =====================================================================


class TestDetectHost(unittest.TestCase):
    """Resolution order (Codex Round 1 H3 + Round 2 M3):

       1. ``args_host`` (CLI flag) — explicit caller-provided value.
       2. ``AGENT_CONFIG_HOST`` env var.
       3. ``"claude-code"`` default (v0.4.0 backward-compat for
          consumers that have neither flag nor env set).

    The function rejects unknown values with ``ValueError`` so a typo
    surfaces at parse time, before the v2 composition starts mutating
    state files."""

    @patch.dict(os.environ, {}, clear=True)
    def test_default_is_claude_code(self) -> None:
        self.assertEqual(compose_packs.detect_host(args_host=None), "claude-code")

    @patch.dict(os.environ, {"AGENT_CONFIG_HOST": "codex"})
    def test_env_var_takes_precedence_over_default(self) -> None:
        self.assertEqual(compose_packs.detect_host(args_host=None), "codex")

    @patch.dict(os.environ, {"AGENT_CONFIG_HOST": "codex"})
    def test_cli_flag_overrides_env(self) -> None:
        self.assertEqual(
            compose_packs.detect_host(args_host="claude-code"), "claude-code"
        )

    @patch.dict(os.environ, {}, clear=True)
    def test_invalid_host_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compose_packs.detect_host(args_host="bogus")

    @patch.dict(os.environ, {"AGENT_CONFIG_HOST": "bogus"})
    def test_invalid_env_var_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compose_packs.detect_host(args_host=None)

    @patch.dict(os.environ, {}, clear=True)
    def test_empty_env_var_falls_back_to_default(self) -> None:
        """Empty env-var value should not be treated as an explicit
        choice; fall through to the default. Without this guard, a
        `AGENT_CONFIG_HOST=` line in a shell init file would crash
        composition with an ambiguous error."""
        with patch.dict(os.environ, {"AGENT_CONFIG_HOST": ""}):
            self.assertEqual(
                compose_packs.detect_host(args_host=None), "claude-code"
            )


# =====================================================================
# Phase 4: _process_selection() - inline-source vs bundled lookup.
# =====================================================================


class TestInlineSourceBranch(unittest.TestCase):
    """Per-selection dispatch helper. The loop in ``_do_compose_v2``
    calls this once per resolved selection; the helper decides whether
    to fetch a remote pack archive (inline ``source.url``) or look up
    the pack in the bundled v2 manifest."""

    def _bundled_manifest(self) -> dict:
        return {
            "version": 2,
            "packs": [
                {
                    "name": "agent-style",
                    "source": {
                        "repo": "https://github.com/yzhao062/agent-style",
                        "ref": "v0.3.2",
                    },
                },
                {
                    "name": "aa-core-skills",
                    "source": "bundled",
                    "default-ref": "bundled",
                },
            ],
        }

    @patch("compose_packs.source_fetch.fetch_pack")
    def test_entry_with_source_url_calls_fetch_pack(self, fetch) -> None:
        """An entry whose ``source.url`` is set must trigger a remote
        fetch. The composer must thread URL, ref, policy, explicit
        auth, and pack-lock recorded commit through to
        ``source_fetch.fetch_pack``."""
        archive = MagicMock()
        archive.archive_dir = Path("/tmp/fake-archive")
        fetch.return_value = archive

        with unittest.mock.patch(
            "compose_packs.schema.parse_manifest",
            return_value={
                "version": 2,
                "packs": [
                    {"name": "profile", "source": {"repo": "x", "ref": "v0.1.0"}}
                ],
            },
        ):
            selection = {
                "name": "profile",
                "source": {
                    "url": "https://github.com/yzhao062/agent-pack",
                    "ref": "v0.1.0",
                },
                "update_policy": "prompt",
            }
            pack_def, archive_dir = compose_packs._process_selection(
                selection,
                bundled_manifest={"packs": []},
                cache_root=Path("/tmp"),
                host="claude-code",
            )

        fetch.assert_called_once()
        args, kwargs = fetch.call_args
        # First positional argument is the URL; second is the ref. The
        # composer normalizes ``source.url`` into the URL slot before
        # calling source_fetch.
        self.assertEqual(args[0], "https://github.com/yzhao062/agent-pack")
        self.assertEqual(args[1], "v0.1.0")
        self.assertEqual(kwargs.get("policy"), "prompt")
        self.assertEqual(archive_dir, archive.archive_dir)
        self.assertEqual(pack_def["name"], "profile")

    @patch("compose_packs.source_fetch.fetch_pack")
    def test_entry_without_source_uses_bundled_lookup(self, fetch) -> None:
        """An entry that's just ``{"name": ...}`` looks up the pack in
        the bundled manifest passed in by the caller. No fetch is made."""
        selection = {"name": "agent-style"}
        pack_def, archive_dir = compose_packs._process_selection(
                selection,
                bundled_manifest=self._bundled_manifest(),
                cache_root=Path("/tmp"),
                host="claude-code",
            )
        fetch.assert_not_called()
        self.assertEqual(pack_def["name"], "agent-style")
        # Bundled lookups have no fetched archive; downstream uses the
        # consumer's `.agent-config/repo/` cache.
        self.assertIsNone(archive_dir)

    @patch("compose_packs.source_fetch.fetch_pack")
    def test_inline_source_with_repo_field_synonym(self, fetch) -> None:
        """``source.repo`` is the v0.4.0-shipped manifest key. v0.5.0
        adds ``source.url`` as the spec'd consumer-facing key. Both
        must work."""
        archive = MagicMock()
        archive.archive_dir = Path("/tmp/fake-archive")
        fetch.return_value = archive

        with unittest.mock.patch(
            "compose_packs.schema.parse_manifest",
            return_value={
                "version": 2,
                "packs": [{"name": "x", "source": {"repo": "https://example", "ref": "v1"}}],
            },
        ):
            selection = {
                "name": "x",
                "source": {"repo": "https://github.com/x/y", "ref": "v1"},
            }
            pack_def, archive_dir = compose_packs._process_selection(
                selection,
                bundled_manifest={"packs": []},
                cache_root=Path("/tmp"),
                host="claude-code",
            )

        fetch.assert_called_once()
        args, _ = fetch.call_args
        self.assertEqual(args[0], "https://github.com/x/y")
        self.assertEqual(archive_dir, archive.archive_dir)

    @patch("compose_packs.source_fetch.fetch_pack")
    def test_inline_source_default_ref_is_main(self, fetch) -> None:
        """When the consumer omits ``source.ref``, default to ``main``.
        Per spec § C: pinning is the consumer's responsibility, but a
        sensible default avoids a confusing rejection at fetch time."""
        archive = MagicMock()
        archive.archive_dir = Path("/tmp/fake-archive")
        fetch.return_value = archive

        with unittest.mock.patch(
            "compose_packs.schema.parse_manifest",
            return_value={
                "version": 2,
                "packs": [{"name": "x", "source": {"repo": "https://example", "ref": "main"}}],
            },
        ):
            selection = {
                "name": "x",
                "source": {"url": "https://github.com/x/y"},
            }
            compose_packs._process_selection(
                selection,
                bundled_manifest={"packs": []},
                cache_root=Path("/tmp"),
                host="claude-code",
            )

        args, _ = fetch.call_args
        self.assertEqual(args[1], "main")

    @patch("compose_packs.source_fetch.fetch_pack")
    def test_inline_source_passes_pack_lock_recorded_commit(self, fetch) -> None:
        """When pack-lock has a previous resolved_commit recorded for
        this pack name, ``_process_selection`` threads it through to
        ``source_fetch.fetch_pack`` so the drift / locked-policy logic
        can compare upstream vs recorded."""
        archive = MagicMock()
        archive.archive_dir = Path("/tmp/fake-archive")
        fetch.return_value = archive

        pack_lock = {
            "version": 1,
            "packs": {
                "profile": {"resolved_commit": "abc123"},
            },
        }

        with unittest.mock.patch(
            "compose_packs.schema.parse_manifest",
            return_value={
                "version": 2,
                "packs": [{"name": "profile", "source": {"repo": "x", "ref": "v1"}}],
            },
        ):
            selection = {
                "name": "profile",
                "source": {"url": "https://github.com/y/agent-pack", "ref": "v1"},
                "update_policy": "locked",
            }
            compose_packs._process_selection(
                selection,
                bundled_manifest={"packs": []},
                cache_root=Path("/tmp"),
                host="claude-code",
                pack_lock=pack_lock,
            )

        _, kwargs = fetch.call_args
        self.assertEqual(kwargs.get("pack_lock_recorded_commit"), "abc123")
        self.assertEqual(kwargs.get("policy"), "locked")

    @patch("compose_packs.source_fetch.fetch_pack")
    def test_inline_source_remote_manifest_missing_pack_name_raises(self, fetch) -> None:
        """If the remote ``pack.yaml`` does not declare a pack with the
        consumer-requested name, fail with an actionable error rather
        than crashing later in the dispatch loop."""
        archive = MagicMock()
        archive.archive_dir = Path("/tmp/fake-archive")
        fetch.return_value = archive

        with unittest.mock.patch(
            "compose_packs.schema.parse_manifest",
            return_value={
                "version": 2,
                "packs": [
                    {"name": "profile", "source": {"repo": "x", "ref": "v0.1.0"}},
                    {"name": "paper-workflow", "source": {"repo": "x", "ref": "v0.1.0"}},
                ],
            },
        ):
            selection = {
                "name": "no-such-pack",
                "source": {
                    "url": "https://github.com/yzhao062/agent-pack",
                    "ref": "v0.1.0",
                },
            }
            with self.assertRaises(compose_packs.ComposeError) as cm:
                compose_packs._process_selection(
                    selection,
                    bundled_manifest={"packs": []},
                    cache_root=Path("/tmp"),
                    host="claude-code",
                )
            msg = str(cm.exception)
            self.assertIn("no-such-pack", msg)
            self.assertIn("agent-pack", msg)

    def test_bundled_lookup_unknown_pack_raises(self) -> None:
        """Bundled-name lookup with an unknown pack name surfaces as
        ``ComposeError``, not a silent skip — consumers should see
        ``unknown bundled pack``."""
        selection = {"name": "no-such-bundled"}
        with self.assertRaises(compose_packs.ComposeError) as cm:
            compose_packs._process_selection(
                selection,
                bundled_manifest=self._bundled_manifest(),
                cache_root=Path("/tmp"),
                host="claude-code",
            )
        self.assertIn("no-such-bundled", str(cm.exception))

    @patch("compose_packs.source_fetch.fetch_pack")
    def test_inline_source_remote_manifest_file_missing(self, fetch) -> None:
        """Consumer points at a non-pack repo (no pack.yaml). The composer
        must surface a clean error, not a traceback."""
        import pathlib
        with tempfile.TemporaryDirectory() as d:
            archive_dir = pathlib.Path(d)
            # Note: no pack.yaml written. Simulating consumer who pointed
            # at a generic GitHub repo with no v0.5.0 pack manifest.
            fetch.return_value = source_fetch.PackArchive(
                url="https://github.com/x/y", ref="main",
                resolved_commit="ab" * 20, method="ssh",
                archive_dir=archive_dir,
                canonical_id="x/y", cache_key="abcd1234/ab12",
            )
            selection = {
                "name": "profile",
                "source": {
                    "url": "https://github.com/x/y",
                    "ref": "main",
                },
            }
            with self.assertRaises(Exception) as ctx:
                compose_packs._process_selection(
                    selection,
                    bundled_manifest={"packs": []},
                    cache_root=pathlib.Path(d),
                    host="claude-code",
                )
            # Either a clean ComposeError or a parse-related ValueError
            # is acceptable; what we're guarding against is a generic
            # FileNotFoundError or KeyError tracing into schema internals.
            msg = str(ctx.exception)
            self.assertTrue(
                "pack.yaml" in msg or "manifest" in msg.lower(),
                f"error should mention manifest/pack.yaml: {msg!r}",
            )


# =====================================================================
# Phase 4: end-to-end host wiring through DispatchContext.
# =====================================================================


class TestHostWiringThroughDispatchContext(unittest.TestCase):
    """``_build_ctx`` is the only place where ``DispatchContext`` is
    constructed in compose. Phase 4 contract: whatever host the
    composer detected, the constructed context must carry it as
    ``current_host``. Calling ``_build_ctx`` directly with a stubbed
    transaction sidesteps the v2 composition pipeline so the host-wiring
    contract is asserted in isolation."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _build_with_host(
        self, *, host: str, archive_dir: Path | None = None
    ) -> "compose_packs.dispatch.DispatchContext":
        from packs import state as state_mod
        # Real transaction context unnecessary for ABI test; pass a
        # stub that has the attributes the dataclass requires.
        return compose_packs._build_ctx(
            root=self.root,
            pack={"name": "x", "source": "bundled"},
            selection={"name": "x"},
            txn=MagicMock(),
            pack_lock=state_mod.empty_pack_lock(),
            project_state=state_mod.empty_project_state(),
            user_state=state_mod.empty_user_state(),
            host=host,
            archive_dir=archive_dir,
        )

    def test_host_value_threads_to_current_host(self) -> None:
        """The detected host string lands on
        ``DispatchContext.current_host`` verbatim. v0.4.0 ABI per
        Codex Round 2 M3."""
        ctx = self._build_with_host(host="codex")
        self.assertEqual(ctx.current_host, "codex")

    def test_default_host_is_claude_code(self) -> None:
        """Builders that omit ``host`` keep the v0.4.0 default so
        existing call sites do not regress."""
        from packs import state as state_mod
        ctx = compose_packs._build_ctx(
            root=self.root,
            pack={"name": "x", "source": "bundled"},
            selection={"name": "x"},
            txn=MagicMock(),
            pack_lock=state_mod.empty_pack_lock(),
            project_state=state_mod.empty_project_state(),
            user_state=state_mod.empty_user_state(),
        )
        self.assertEqual(ctx.current_host, "claude-code")

    def test_archive_dir_threads_to_pack_source_dir(self) -> None:
        """Inline-source packs (``archive_dir`` set) should land that
        path in ``DispatchContext.pack_source_dir`` so the passive
        archive adapter and active handlers read from the fetched
        snapshot."""
        archive = self.root / "fake-archive"
        archive.mkdir()
        ctx = self._build_with_host(host="claude-code", archive_dir=archive)
        self.assertEqual(ctx.pack_source_dir, archive)

    def test_bundled_pack_uses_consumer_repo_cache(self) -> None:
        """Bundled packs (no archive_dir) get
        ``<consumer>/.agent-config/repo`` as ``pack_source_dir`` so
        v0.4.0 active handlers continue reading from the consumer's
        bootstrapped repo cache."""
        ctx = self._build_with_host(host="claude-code", archive_dir=None)
        self.assertEqual(
            ctx.pack_source_dir, self.root / ".agent-config" / "repo"
        )


# =====================================================================
# Phase 8 Task 8.1: prompt_user_for_updates — TTY + env-var dispatch.
# =====================================================================


def _pending_fixture(name: str = "profile") -> tuple:
    """Return a synthetic ``(selection, archive, pack_def)`` triple."""
    selection = {
        "name": name,
        "source": {"url": f"https://example.com/{name}", "ref": "main"},
    }
    archive = MagicMock()
    archive.resolved_commit = "ef" * 20
    pack_def = {"name": name, "active": []}
    return selection, archive, pack_def


class TestPromptUserForUpdates(unittest.TestCase):
    """``prompt_user_for_updates`` — TTY detection + env-var fallback.

    When stdin/stdout are TTYs, ask interactively. When non-interactive,
    consult ``ANYWHERE_AGENTS_UPDATE`` (apply / skip / fail; default
    skip; fail raises ``PackLockDriftAborted``). Per Round 3 plan."""

    def test_tty_apply_returns_apply(self) -> None:
        """Interactive path: stdin/stdout TTYs + Y → ``apply``."""
        with (
            patch("compose_packs.sys.stdin") as stdin,
            patch("compose_packs.sys.stdout") as stdout,
            patch("builtins.input", return_value="Y"),
            patch("builtins.print"),  # silence interactive prompt output
        ):
            stdin.isatty.return_value = True
            stdout.isatty.return_value = True
            result = compose_packs.prompt_user_for_updates([_pending_fixture()])
        self.assertEqual(result, "apply")

    def test_tty_default_yes_returns_apply(self) -> None:
        """Default response (empty enter) is treated as Y."""
        with (
            patch("compose_packs.sys.stdin") as stdin,
            patch("compose_packs.sys.stdout") as stdout,
            patch("builtins.input", return_value=""),
            patch("builtins.print"),
        ):
            stdin.isatty.return_value = True
            stdout.isatty.return_value = True
            result = compose_packs.prompt_user_for_updates([_pending_fixture()])
        self.assertEqual(result, "apply")

    def test_tty_n_returns_skip(self) -> None:
        """Interactive 'n' — keep current locked commit."""
        with (
            patch("compose_packs.sys.stdin") as stdin,
            patch("compose_packs.sys.stdout") as stdout,
            patch("builtins.input", return_value="n"),
            patch("builtins.print"),
        ):
            stdin.isatty.return_value = True
            stdout.isatty.return_value = True
            result = compose_packs.prompt_user_for_updates([_pending_fixture()])
        self.assertEqual(result, "skip")

    @patch.dict(os.environ, {"ANYWHERE_AGENTS_UPDATE": "skip"}, clear=False)
    def test_non_tty_skip_default(self) -> None:
        """Non-TTY + ``ANYWHERE_AGENTS_UPDATE=skip`` → ``skip``."""
        with (
            patch("compose_packs.sys.stdin") as stdin,
            patch("compose_packs.sys.stdout") as stdout,
        ):
            stdin.isatty.return_value = False
            stdout.isatty.return_value = False
            result = compose_packs.prompt_user_for_updates([_pending_fixture()])
        self.assertEqual(result, "skip")

    @patch.dict(os.environ, {"ANYWHERE_AGENTS_UPDATE": "apply"}, clear=False)
    def test_non_tty_apply_returns_apply(self) -> None:
        """Non-TTY + ``ANYWHERE_AGENTS_UPDATE=apply`` → ``apply``."""
        with (
            patch("compose_packs.sys.stdin") as stdin,
            patch("compose_packs.sys.stdout") as stdout,
        ):
            stdin.isatty.return_value = False
            stdout.isatty.return_value = False
            result = compose_packs.prompt_user_for_updates([_pending_fixture()])
        self.assertEqual(result, "apply")

    @patch.dict(os.environ, {"ANYWHERE_AGENTS_UPDATE": "fail"}, clear=False)
    def test_non_tty_fail_raises(self) -> None:
        """Non-TTY + ``ANYWHERE_AGENTS_UPDATE=fail`` → ``PackLockDriftAborted``."""
        with (
            patch("compose_packs.sys.stdin") as stdin,
            patch("compose_packs.sys.stdout") as stdout,
        ):
            stdin.isatty.return_value = False
            stdout.isatty.return_value = False
            with self.assertRaises(compose_packs.PackLockDriftAborted):
                compose_packs.prompt_user_for_updates([_pending_fixture()])

    @patch.dict(os.environ, {}, clear=True)
    def test_non_tty_unset_defaults_to_skip(self) -> None:
        """Non-TTY + env var unset → ``skip`` (no surprise apply)."""
        with (
            patch("compose_packs.sys.stdin") as stdin,
            patch("compose_packs.sys.stdout") as stdout,
        ):
            stdin.isatty.return_value = False
            stdout.isatty.return_value = False
            result = compose_packs.prompt_user_for_updates([_pending_fixture()])
        self.assertEqual(result, "skip")

    @patch.dict(os.environ, {"ANYWHERE_AGENTS_UPDATE": "garbage"}, clear=False)
    def test_non_tty_unknown_value_raises_value_error(self) -> None:
        """An unrecognized env-var value should fail loudly so a typo
        surfaces at first use rather than silently picking a default."""
        with (
            patch("compose_packs.sys.stdin") as stdin,
            patch("compose_packs.sys.stdout") as stdout,
        ):
            stdin.isatty.return_value = False
            stdout.isatty.return_value = False
            with self.assertRaises(ValueError):
                compose_packs.prompt_user_for_updates([_pending_fixture()])


# =====================================================================
# Phase 8 Task 8.2: pending-updates.json — Round 3 M4 stale-file invariant.
# =====================================================================


class TestPendingUpdatesJSONInvariant(unittest.TestCase):
    """Round 3 M4: ``pending-updates.json`` must reflect current state.

    - Written when the user defers ("skip" path).
    - Cleared on every apply path: interactive Y, non-TTY apply,
      pack update, and a subsequent run with no drift remaining.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / ".agent-config" / "pending-updates.json"

    def test_written_on_skip(self) -> None:
        """``write_pending_updates_json`` produces a JSON file with
        ``ts`` (UTC ISO-8601), ``host``, and a list of pack entries."""
        sel, arc, pack = _pending_fixture("profile")
        compose_packs.write_pending_updates_json(
            self.root, "claude-code", [(sel, arc, pack)],
        )
        self.assertTrue(self.target.exists())
        payload = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(payload["host"], "claude-code")
        self.assertEqual(len(payload["packs"]), 1)
        entry = payload["packs"][0]
        self.assertEqual(entry["name"], "profile")
        self.assertEqual(entry["available"], "ef" * 20)
        self.assertIn("kind", entry)
        # Timestamp must be timezone-aware ISO-8601 in UTC.
        self.assertIn("T", payload["ts"])
        self.assertTrue(
            payload["ts"].endswith("+00:00") or payload["ts"].endswith("Z"),
        )

    def test_cleared_on_apply(self) -> None:
        """``clear_pending_updates_json`` removes the file. Idempotent —
        a second clear on an already-clean state must not raise."""
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_text('{"ts":"x","host":"y","packs":[]}')
        compose_packs.clear_pending_updates_json(self.root)
        self.assertFalse(self.target.exists())
        # Idempotent.
        compose_packs.clear_pending_updates_json(self.root)
        self.assertFalse(self.target.exists())

    def test_cleared_when_no_drift_remains(self) -> None:
        """A stale ``pending-updates.json`` left from a previous run is
        cleared when the current run has no pending updates. The
        invariant is unconditional (Round 3 M4): if pending_updates is
        empty, the file must not exist after the run."""
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_text('{"ts":"x","host":"y","packs":[]}')
        # Empty pending list → must clear.
        compose_packs.clear_pending_updates_json(self.root)
        self.assertFalse(self.target.exists())

    def test_pack_kind_passive_when_no_active_entries(self) -> None:
        """``kind`` field reflects the pack's active list:
        ``passive`` when ``pack["active"]`` is empty / absent,
        ``active`` when at least one active entry."""
        sel, arc, pack_passive = _pending_fixture("profile")
        pack_passive["active"] = []
        compose_packs.write_pending_updates_json(
            self.root, "claude-code", [(sel, arc, pack_passive)],
        )
        payload = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(payload["packs"][0]["kind"], "passive")

    def test_pack_kind_active_when_active_entries_present(self) -> None:
        sel, arc, pack_active = _pending_fixture("profile")
        pack_active["active"] = [{"kind": "skill"}]
        compose_packs.write_pending_updates_json(
            self.root, "claude-code", [(sel, arc, pack_active)],
        )
        payload = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(payload["packs"][0]["kind"], "active")


# =====================================================================
# Phase 8 Task 8.3: print_compose_summary.
# =====================================================================


class TestComposeSummary(unittest.TestCase):
    """``print_compose_summary`` prints a per-pack outcome line, the host,
    and a hint when there are pending updates so the consumer knows how
    to apply them."""

    def test_summary_lists_each_pack_outcome_and_host(self) -> None:
        selections = [
            {"name": "profile"},
            {"name": "agent-style"},
        ]
        outcomes = {
            "profile": "fetched (ssh)",
            "agent-style": "no change",
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            compose_packs.print_compose_summary(
                selections, outcomes, [], host="claude-code",
            )
        out = buf.getvalue()
        self.assertIn("profile", out)
        self.assertIn("fetched (ssh)", out)
        self.assertIn("agent-style", out)
        self.assertIn("no change", out)
        self.assertIn("claude-code", out)
        # No pending hint when pending_updates is empty.
        self.assertNotIn("pending", out.lower())

    def test_summary_prints_pending_hint_when_drift(self) -> None:
        selections = [{"name": "profile"}]
        outcomes = {"profile": "deferred (drift)"}
        pending = [_pending_fixture("profile")]
        buf = io.StringIO()
        with redirect_stdout(buf):
            compose_packs.print_compose_summary(
                selections, outcomes, pending, host="claude-code",
            )
        out = buf.getvalue()
        self.assertIn("pending", out.lower())
        # Hint should mention how to apply (env var) AND the bootstrap
        # entry-point so the consumer can run it interactively.
        self.assertIn("ANYWHERE_AGENTS_UPDATE", out)


# =====================================================================
# Phase 8 carry-forward A: validate_url_fn wiring at production path.
# =====================================================================


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestCredentialURLRejectedAtProductionPath(unittest.TestCase):
    """Carry-forward A: ``compose_packs.main`` must reject an inline-source
    URL that embeds credentials in userinfo. The rejection must happen
    at parse time (before any network call), with the URL redacted in
    the surfaced error message."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Minimal v2 manifest so main reaches the validation pre-pass.
        bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
        _write(
            bootstrap_dir / "packs.yaml",
            "version: 2\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source: bundled\n"
            "    default-ref: bundled\n",
        )
        _write(self.root / ".agent-config" / "AGENTS.md", "# upstream\n")

    def test_credentialed_url_in_agent_config_rejected_with_redaction(self) -> None:
        """An inline-source entry with ``ghp_*`` token in userinfo is
        rejected by ``compose_packs.main``. Stderr carries the redacted
        URL and never the raw token bytes."""
        _write(
            self.root / "agent-config.yaml",
            "packs:\n"
            "  - name: profile\n"
            "    source:\n"
            "      url: https://ghp_secrettoken@github.com/x/y\n"
            "      ref: v0.1.0\n",
        )
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = compose_packs.main(["--root", str(self.root)])
        self.assertEqual(rc, 1)
        err = err_buf.getvalue()
        self.assertIn("<redacted>", err)
        self.assertNotIn("ghp_secrettoken", err)

    def test_credential_url_rejected_before_lock_acquisition(self) -> None:
        """The URL pre-validation runs BEFORE ``locks.acquire`` so a
        bad URL fails fast without serializing on the user lock for 30
        seconds. Patching ``locks.acquire`` confirms it was never reached."""
        _write(
            self.root / "agent-config.yaml",
            "packs:\n"
            "  - name: profile\n"
            "    source:\n"
            "      url: https://ghp_secrettoken@github.com/x/y\n"
            "      ref: v0.1.0\n",
        )
        with patch.object(locks, "acquire") as acquire:
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                rc = compose_packs.main(["--root", str(self.root)])
        self.assertEqual(rc, 1)
        acquire.assert_not_called()


# =====================================================================
# Phase 8 carry-forward B: reconcile_orphans wiring before composition.
# =====================================================================


class TestReconcileOrphansWiredIntoCompose(unittest.TestCase):
    """Carry-forward B: ``compose_packs.main`` must call
    ``reconciliation.reconcile_orphans(project_root, user_root,
    locks_held=True)`` exactly once, after lock acquisition and before
    any pack-fetch begins. A clean-startup run (no orphans) is enough to
    pin the wiring contract."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
        _write(
            bootstrap_dir / "packs.yaml",
            "version: 2\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source: bundled\n"
            "    default-ref: bundled\n",
        )
        _write(self.root / ".agent-config" / "AGENTS.md", "# upstream\n")

    def test_reconcile_orphans_called_once_with_locks_held_true(self) -> None:
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None

        # Order-of-call observer: append the helper name when called so
        # we can pin "reconcile runs first, then composition body".
        order: list[str] = []

        def _reconcile_observer(*args, **kwargs):
            order.append("reconcile")
            return reconciliation_mod.ReconciliationReport()

        def _inner_observer(*args, **kwargs):
            order.append("inner")
            return 0

        with (
            patch.object(locks, "acquire", side_effect=_ctx),
            patch.object(
                reconciliation_mod,
                "reconcile_orphans",
                side_effect=_reconcile_observer,
            ) as reconcile,
            patch.object(
                compose_packs, "_do_compose_v2", side_effect=_inner_observer
            ) as inner,
        ):
            rc, _out, _err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 0)
        reconcile.assert_called_once()
        args, kwargs = reconcile.call_args
        # Kwargs may carry locks_held=True (positional or keyword).
        self.assertEqual(kwargs.get("locks_held"), True)
        # The two roots passed first must be the project + user homes.
        self.assertEqual(args[0], self.root.resolve())
        self.assertEqual(args[1], Path.home())
        # Reconciliation must run BEFORE the inner composer body.
        self.assertEqual(order, ["reconcile", "inner"])
        inner.assert_called_once()

    def test_reconcile_blocking_orphans_aborts_compose(self) -> None:
        """When reconciliation surfaces a blocking orphan (DRIFT or
        unreapplyable PARTIAL), the composer must NOT proceed with
        pack-fetch — it should surface the drift to stderr and exit
        non-zero so the consumer's bootstrap fails fast."""
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None

        report = reconciliation_mod.ReconciliationReport()
        report.blocking.append(self.root / "stuck.staging-x")

        with (
            patch.object(locks, "acquire", side_effect=_ctx),
            patch.object(
                reconciliation_mod, "reconcile_orphans", return_value=report,
            ),
            patch.object(
                compose_packs, "_do_compose_v2", return_value=0
            ) as inner,
        ):
            rc, _out, err = _invoke(["--root", str(self.root)])

        self.assertNotEqual(rc, 0)
        # Inner composer must not have run.
        inner.assert_not_called()
        self.assertIn("stuck.staging-x", err)


# =====================================================================
# Phase 8 Round 4 fix: _interactive_prompt EOF handling.
# =====================================================================


class TestPromptUserForUpdatesEOF(unittest.TestCase):
    """Round 4 Issue 5: a Ctrl-D / EOF at the interactive prompt must
    surface as ``skip`` rather than letting EOFError leak out of the
    composer. Empty stdin closing mid-pipe is the same path."""

    def test_eof_returns_skip(self) -> None:
        with (
            patch("compose_packs.sys.stdin") as stdin,
            patch("compose_packs.sys.stdout") as stdout,
            patch("builtins.input", side_effect=EOFError),
            patch("builtins.print"),  # silence prompt + cleanup newline
        ):
            stdin.isatty.return_value = True
            stdout.isatty.return_value = True
            result = compose_packs.prompt_user_for_updates([_pending_fixture()])
        self.assertEqual(result, "skip")


# =====================================================================
# Phase 8 Round 4 fix: reconciliation summary silent on clean startup.
# =====================================================================


class TestReconcileOrphansSummary(unittest.TestCase):
    """Round 4 Issue 3: ``reconcile_orphans`` summary must NOT be printed
    when only ``live`` is non-empty or every bucket is empty (clean
    startup). Only the four "did real work" buckets (rolled_back,
    rolled_forward, partial_reapplied, blocking) trigger the line."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
        _write(
            bootstrap_dir / "packs.yaml",
            "version: 2\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source: bundled\n"
            "    default-ref: bundled\n",
        )
        _write(self.root / ".agent-config" / "AGENTS.md", "# upstream\n")

    def test_silent_on_clean_startup(self) -> None:
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None

        with (
            patch.object(locks, "acquire", side_effect=_ctx),
            patch.object(
                reconciliation_mod,
                "reconcile_orphans",
                return_value=reconciliation_mod.ReconciliationReport(),
            ),
            patch.object(
                compose_packs, "_do_compose_v2", return_value=0,
            ),
        ):
            rc, _out, err = _invoke(["--root", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertNotIn("reconciliation:", err)

    def test_silent_on_live_only_count(self) -> None:
        """A run where the only non-empty bucket is ``live`` (peer
        composer holds an orphan) should also stay silent. The peer
        will clean its own staging dir on commit/rollback."""
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None

        report = reconciliation_mod.ReconciliationReport()
        report.live.append(self.root / "peer.staging-1")

        with (
            patch.object(locks, "acquire", side_effect=_ctx),
            patch.object(
                reconciliation_mod, "reconcile_orphans", return_value=report,
            ),
            patch.object(
                compose_packs, "_do_compose_v2", return_value=0,
            ),
        ):
            rc, _out, err = _invoke(["--root", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertNotIn("reconciliation:", err)

    def test_prints_summary_when_real_work_done(self) -> None:
        """Conversely, when reconciliation actually rolled-back / forward
        / reapplied / blocked an orphan, the summary line MUST appear so
        the operator can see it happened."""
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None

        report = reconciliation_mod.ReconciliationReport()
        report.rolled_back.append(self.root / "rb.staging-1")

        with (
            patch.object(locks, "acquire", side_effect=_ctx),
            patch.object(
                reconciliation_mod, "reconcile_orphans", return_value=report,
            ),
            patch.object(
                compose_packs, "_do_compose_v2", return_value=0,
            ),
        ):
            rc, _out, err = _invoke(["--root", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn("reconciliation:", err)
        self.assertIn("rolled_back=1", err)


# =====================================================================
# Phase 8 Round 4 fix: helpers wired into _do_compose_v2 production path.
# =====================================================================


class TestComposeFlowWiresPhase8Helpers(unittest.TestCase):
    """Round 4 Issues 1+2: the drift-detect → prompt → write/clear
    pending-updates round-trip is wired into the production
    ``_do_compose_v2`` path. Each test seeds a previous pack-lock with a
    recorded commit, stubs ``source_fetch.fetch_pack`` to return a NEW
    commit (drift), and asserts the appropriate Phase 8 helper was
    called with the expected arguments."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # v2 manifest with one inline-source pack so drift detection has
        # something to fire on.
        bootstrap_dir = self.root / ".agent-config" / "repo" / "bootstrap"
        _write(
            bootstrap_dir / "packs.yaml",
            "version: 2\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source: bundled\n"
            "    default-ref: bundled\n",
        )
        _write(self.root / ".agent-config" / "AGENTS.md", "# upstream\n")
        # Consumer-side selection: profile from agent-pack via inline source.
        # ``parse_user_config`` reads the ``rule_packs`` key (legacy v0.4.0
        # name; the v0.5.0 spec uses ``packs`` but the v2 composer still
        # routes through the legacy resolver for selection assembly).
        _write(
            self.root / "agent-config.yaml",
            "rule_packs:\n"
            "  - name: profile\n"
            "    source:\n"
            "      url: https://github.com/yzhao062/agent-pack\n"
            "      ref: v0.1.0\n",
        )
        # Seed previous pack-lock with an OLDER recorded commit so the
        # NEW resolved_commit returned by the stub triggers drift.
        from packs import state as state_mod
        prev_lock = state_mod.empty_pack_lock()
        prev_lock["packs"]["profile"] = {
            "source_url": "https://github.com/yzhao062/agent-pack",
            "requested_ref": "v0.1.0",
            "resolved_commit": "a" * 40,
            "pack_update_policy": "prompt",
            "files": [],
        }
        state_mod.save_pack_lock(
            self.root / ".agent-config" / "pack-lock.json", prev_lock,
        )
        # Build a synthetic archive at a remote-looking dir with a pack.yaml
        # so schema.parse_manifest succeeds. The archive_dir contents are
        # read by the composer; stub the manifest so passive/active loops
        # are no-ops.
        self.archive_dir = self.root / "fake-archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        (self.archive_dir / "pack.yaml").write_text(
            "version: 2\n"
            "packs:\n"
            "  - name: profile\n"
            "    source:\n"
            "      repo: https://github.com/yzhao062/agent-pack\n"
            "      ref: v0.1.0\n"
        )
        # NEW commit on the upstream — drift vs the recorded "aaaa…".
        new_archive = MagicMock()
        new_archive.archive_dir = self.archive_dir
        new_archive.resolved_commit = "b" * 40
        new_archive.method = "ssh"
        self.new_archive = new_archive
        # The locked-version archive (recorded commit) used by the skip
        # path. Same archive dir is fine; only resolved_commit differs.
        locked_archive = MagicMock()
        locked_archive.archive_dir = self.archive_dir
        locked_archive.resolved_commit = "a" * 40
        locked_archive.method = "ssh"
        self.locked_archive = locked_archive

    @contextmanager
    def _no_lock(self):
        @contextmanager
        def _ctx(*_args, **_kwargs):
            yield None
        with patch.object(locks, "acquire", side_effect=_ctx):
            yield

    def _drift_run_patches(
        self,
        *,
        prompt_return: str | None = None,
        prompt_side_effect=None,
    ):
        """Stack the patches a drift-flow test needs: stub fetch_pack to
        return the NEW archive on the pre-fetch leg and the LOCKED
        archive on any subsequent revert call; patch
        ``prompt_user_for_updates`` to drive the apply / skip decision.
        """
        fetch_returns = [self.new_archive, self.locked_archive]

        def _fetch(*_args, **_kwargs):
            return fetch_returns.pop(0) if fetch_returns else self.locked_archive

        return (
            patch.object(
                source_fetch, "fetch_pack", side_effect=_fetch,
            ),
            patch.object(
                compose_packs,
                "prompt_user_for_updates",
                side_effect=prompt_side_effect or (
                    (lambda *_a, **_k: prompt_return)
                    if prompt_return is not None else None
                ),
                return_value=prompt_return,
            ),
        )

    def test_drift_detected_prompts_user(self) -> None:
        """Pre-fetch loop detects archive.resolved_commit != recorded
        commit and calls prompt_user_for_updates with a list of
        (selection, archive, pack_def) triples."""
        captured: list = []

        def _capture(pending):
            captured.extend(pending)
            return "apply"

        with self._no_lock():
            with (
                patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self.new_archive,
                ),
                patch.object(
                    compose_packs, "prompt_user_for_updates",
                    side_effect=_capture,
                ) as prompt,
            ):
                rc, _out, _err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 0)
        prompt.assert_called_once()
        # The pending list must include profile with the new archive.
        self.assertEqual(len(captured), 1)
        sel, archive, pack_def = captured[0]
        self.assertEqual(sel["name"], "profile")
        self.assertIs(archive, self.new_archive)
        self.assertEqual(pack_def["name"], "profile")

    def test_apply_path_clears_pending_updates_json(self) -> None:
        """Drift + prompt returns ``apply`` → clear_pending_updates_json
        is called (Round 3 M4: apply path always clears). Pre-create the
        file so we can assert it was removed (or never existed)."""
        target = self.root / ".agent-config" / "pending-updates.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"ts":"x","host":"y","packs":[{"name":"profile"}]}')

        with self._no_lock():
            with (
                patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self.new_archive,
                ),
                patch.object(
                    compose_packs, "prompt_user_for_updates",
                    return_value="apply",
                ),
                patch.object(
                    compose_packs, "clear_pending_updates_json",
                ) as clear,
            ):
                rc, _out, _err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 0)
        clear.assert_called()
        # The argument is the project root.
        args, _kwargs = clear.call_args
        self.assertEqual(args[0], self.root.resolve())

    def test_skip_path_writes_pending_updates_json(self) -> None:
        """Drift + prompt returns ``skip`` → write_pending_updates_json
        called with (project_root, host, pending). The skip path must NOT
        also clear after commit."""
        with self._no_lock():
            with (
                patch.object(
                    source_fetch, "fetch_pack",
                    side_effect=[self.new_archive, self.locked_archive],
                ),
                patch.object(
                    compose_packs, "prompt_user_for_updates",
                    return_value="skip",
                ),
                patch.object(
                    compose_packs, "write_pending_updates_json",
                ) as write_pending,
                patch.object(
                    compose_packs, "clear_pending_updates_json",
                ) as clear,
            ):
                rc, _out, _err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 0)
        write_pending.assert_called_once()
        args, _kwargs = write_pending.call_args
        self.assertEqual(args[0], self.root.resolve())
        # host arg present.
        self.assertEqual(args[1], "claude-code")
        # pending list shaped as (selection, archive, pack_def).
        pending = args[2]
        self.assertEqual(len(pending), 1)
        sel, archive, pack_def = pending[0]
        self.assertEqual(sel["name"], "profile")
        # Skip path MUST NOT clear; the file is the deferred-state record.
        clear.assert_not_called()

    def test_skip_path_uses_load_cached_archive_no_network(self) -> None:
        """Production bug regression: the skip path must NOT call
        ``fetch_pack`` a second time to revert the drifted entry.

        ``fetch_pack`` always runs ``resolve_ref_with_auth_chain`` first,
        and ``ls-remote <url> <sha> <sha>^{}`` returns empty for a 40-char
        SHA (refs match by refname only). The auth chain then exhausts
        and raises ``AuthChainExhaustedError`` as an uncaught traceback.
        The fix routes the revert through ``load_cached_archive``, a
        pure cache lookup with no network calls. This test pins that
        contract: ``fetch_pack`` is called once for drift detection,
        ``load_cached_archive`` is called once for the revert, and the
        recorded SHA is passed through full-length (not truncated)."""
        with self._no_lock():
            with (
                patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self.new_archive,
                ) as fetch,
                patch.object(
                    source_fetch, "load_cached_archive",
                    return_value=self.locked_archive,
                ) as load_cached,
                patch.object(
                    compose_packs, "prompt_user_for_updates",
                    return_value="skip",
                ),
                patch.object(
                    compose_packs, "write_pending_updates_json",
                ),
            ):
                rc, _out, _err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 0)
        # fetch_pack: only the drift-detection pre-fetch leg ran. The
        # buggy code would have called it twice (second call with the
        # SHA as ref triggered the AuthChainExhaustedError traceback).
        self.assertEqual(fetch.call_count, 1)
        # load_cached_archive: called exactly once for the one drifted
        # entry, with the recorded full-length SHA.
        self.assertEqual(load_cached.call_count, 1)
        args, kwargs = load_cached.call_args
        self.assertEqual(args[0], "https://github.com/yzhao062/agent-pack")
        # Full 40-char SHA, not truncated and not a refname.
        self.assertEqual(args[1], "a" * 40)
        self.assertEqual(len(args[1]), 40)
        # cache_root flows through as a kwarg.
        self.assertIn("cache_root", kwargs)

    def test_skip_path_warns_on_cold_cache_and_keeps_new_archive(
        self,
    ) -> None:
        """When ``load_cached_archive`` returns ``None`` (cold cache for
        the recorded commit), the skip path emits a warning naming the
        pack and the truncated SHA, keeps the newly-fetched archive in
        ``resolved`` (no replacement), and still writes
        pending-updates.json so the deferred-state banner can fire."""
        with self._no_lock():
            with (
                patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self.new_archive,
                ),
                patch.object(
                    source_fetch, "load_cached_archive",
                    return_value=None,
                ),
                patch.object(
                    compose_packs, "prompt_user_for_updates",
                    return_value="skip",
                ),
                patch.object(
                    compose_packs, "write_pending_updates_json",
                ) as write_pending,
            ):
                rc, _out, err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 0)
        # Warning text is user-facing, so check exact substrings.
        self.assertIn("cannot revert 'profile'", err)
        self.assertIn("not in local cache", err)
        # Truncated SHA in the warning (first 7 chars).
        self.assertIn("aaaaaaa", err)
        # Pending-updates still written so the deferred state surfaces.
        write_pending.assert_called_once()

    def test_no_drift_clears_stale_pending_updates_json(self) -> None:
        """A run with no drift remaining must clear any stale
        pending-updates.json from a previous skip (Round 3 M4 PATH 4).
        Stub the previous lock to record the SAME commit as the archive
        so no drift fires."""
        from packs import state as state_mod
        # Override previous lock with the NEW commit so no drift.
        prev_lock = state_mod.empty_pack_lock()
        prev_lock["packs"]["profile"] = {
            "source_url": "https://github.com/yzhao062/agent-pack",
            "requested_ref": "v0.1.0",
            "resolved_commit": "b" * 40,
            "pack_update_policy": "prompt",
            "files": [],
        }
        state_mod.save_pack_lock(
            self.root / ".agent-config" / "pack-lock.json", prev_lock,
        )
        # Stale pending file from a previous run.
        stale = self.root / ".agent-config" / "pending-updates.json"
        stale.write_text('{"ts":"x","host":"y","packs":[{"name":"profile"}]}')

        with self._no_lock():
            with (
                patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self.new_archive,
                ),
                patch.object(
                    compose_packs, "prompt_user_for_updates",
                ) as prompt,
                patch.object(
                    compose_packs, "clear_pending_updates_json",
                ) as clear,
            ):
                rc, _out, _err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 0)
        # No drift → prompt NOT called.
        prompt.assert_not_called()
        # Clear MUST be called so stale file doesn't mislead next session.
        clear.assert_called()
        args, _kwargs = clear.call_args
        self.assertEqual(args[0], self.root.resolve())

    @patch.dict(
        os.environ, {"ANYWHERE_AGENTS_UPDATE": "fail"}, clear=False,
    )
    def test_pack_lock_drift_aborted_exits_11(self) -> None:
        """Non-TTY + ANYWHERE_AGENTS_UPDATE=fail + drift → exit 11.
        Distinct from generic compose error (1) and lock timeout (10)."""
        with self._no_lock():
            with (
                patch.object(
                    source_fetch, "fetch_pack",
                    return_value=self.new_archive,
                ),
                # Force the prompt's TTY check to fall through to env
                # var, where ANYWHERE_AGENTS_UPDATE=fail raises
                # PackLockDriftAborted.
                patch("compose_packs.sys.stdin") as stdin,
                patch("compose_packs.sys.stdout") as stdout,
            ):
                stdin.isatty.return_value = False
                stdout.isatty.return_value = False
                rc, _out, err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 11)
        self.assertIn(
            "ANYWHERE_AGENTS_UPDATE=fail", err,
        )

    def test_inline_source_lock_records_resolved_commit_not_ref(self) -> None:
        """Codex Round 2 H1 regression: pack-lock.json must record the
        40-char ``archive.resolved_commit`` for inline-source packs, NOT
        the requested ref string (e.g. ``"v0.1.0"``).

        Previously ``_build_ctx`` was called with ``archive_dir`` only
        and derived ``pack_resolved_commit=pack_ref`` (the ref), so the
        lock recorded ``"v0.1.0"`` instead of the SHA. Every subsequent
        run then reported drift, defeating the peeled-annotated-tag
        acceptance test. Fix routes the full ``PackArchive`` into
        ``_build_ctx`` so inline-source entries write
        ``archive.resolved_commit`` into the lock.
        """
        from packs import state as state_mod
        # Override the previous lock to record the SAME commit as the
        # archive so no drift fires (we want the no-drift apply path so
        # the lock-write code runs without any prompt or skip noise).
        prev_lock = state_mod.empty_pack_lock()
        prev_lock["packs"]["profile"] = {
            "source_url": "https://github.com/yzhao062/agent-pack",
            "requested_ref": "v0.1.0",
            "resolved_commit": "b" * 40,
            "pack_update_policy": "prompt",
            "files": [],
        }
        state_mod.save_pack_lock(
            self.root / ".agent-config" / "pack-lock.json", prev_lock,
        )
        # Re-write the archive's pack.yaml with a passive entry pointing
        # at a real ``from`` file so the passive handler runs and calls
        # ``record_lock_file``; without that the lock has no entry to
        # check (``finalize_pack_lock`` skips when no files accumulated).
        (self.archive_dir / "docs").mkdir(exist_ok=True)
        (self.archive_dir / "docs" / "profile.md").write_text(
            "# profile content\n"
        )
        (self.archive_dir / "pack.yaml").write_text(
            "version: 2\n"
            "packs:\n"
            "  - name: profile\n"
            "    source:\n"
            "      repo: https://github.com/yzhao062/agent-pack\n"
            "      ref: v0.1.0\n"
            "    passive:\n"
            "      - files:\n"
            "          - {from: docs/profile.md, to: AGENTS.md}\n"
        )
        # Build a REAL ``PackArchive`` (not a MagicMock) so the
        # ``_build_ctx`` path can read ``archive.url`` / ``archive.ref``
        # as real strings; the state-save validation refuses non-string
        # values for ``source_url``.
        real_archive = source_fetch.PackArchive(
            url="https://github.com/yzhao062/agent-pack",
            ref="v0.1.0",
            resolved_commit="b" * 40,
            method="ssh",
            archive_dir=self.archive_dir,
            canonical_id="yzhao062/agent-pack",
            cache_key="abcd1234/" + "b" * 40,
        )
        with self._no_lock():
            with patch.object(
                source_fetch, "fetch_pack",
                return_value=real_archive,
            ):
                rc, _out, err = _invoke(["--root", str(self.root)])

        self.assertEqual(rc, 0, f"compose failed: {err}")
        # Read pack-lock.json directly and assert the entry's
        # resolved_commit is the 40-char SHA, NOT the ref string.
        post_lock = json.loads(
            (self.root / ".agent-config" / "pack-lock.json")
            .read_text(encoding="utf-8")
        )
        entry = post_lock["packs"]["profile"]
        self.assertEqual(entry["resolved_commit"], "b" * 40)
        self.assertEqual(len(entry["resolved_commit"]), 40)
        # Defense against the bug: the recorded value must NOT be the
        # human-readable ref string. Without the H1 fix, this assertion
        # caught the bug — the lock wrote "v0.1.0" instead of the SHA.
        self.assertNotEqual(entry["resolved_commit"], "v0.1.0")


if __name__ == "__main__":
    unittest.main()
