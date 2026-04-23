"""Tests for scripts/compose_rule_packs.py."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compose_rule_packs as crp  # noqa: E402


# ---------- helpers ----------


def _fake_urlopen(
    content_by_url: dict[str, bytes | Exception],
):
    """Build a mock for urllib.request.urlopen keyed by URL."""

    def _opener(url, *args, **kwargs):
        if url not in content_by_url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        value = content_by_url[url]
        if isinstance(value, Exception):
            raise value
        resp = mock.MagicMock()
        resp.read.return_value = value
        resp.__enter__ = lambda self_: resp
        resp.__exit__ = lambda self_, *a: None
        return resp

    return _opener


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------- parse_manifest ----------


class ParseManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _manifest(self, text: str) -> Path:
        p = self.root / "rule-packs.yaml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_valid_manifest(self) -> None:
        path = self._manifest(
            "version: 1\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source: https://example.com/{ref}/docs/rule-pack.md\n"
            "    default-ref: v0.3.2\n"
        )
        packs = crp.parse_manifest(path)
        self.assertIn("agent-style", packs)
        self.assertEqual(packs["agent-style"]["default-ref"], "v0.3.2")

    def test_missing_file_raises(self) -> None:
        with self.assertRaisesRegex(crp.RulePackError, "not found"):
            crp.parse_manifest(self.root / "missing.yaml")

    def test_missing_version_key(self) -> None:
        path = self._manifest("packs: []\n")
        with self.assertRaisesRegex(crp.RulePackError, "missing required keys"):
            crp.parse_manifest(path)

    def test_missing_packs_key(self) -> None:
        path = self._manifest("version: 1\n")
        with self.assertRaisesRegex(crp.RulePackError, "missing required keys"):
            crp.parse_manifest(path)

    def test_unsupported_version(self) -> None:
        path = self._manifest("version: 2\npacks: []\n")
        with self.assertRaisesRegex(crp.RulePackError, "version .* unsupported"):
            crp.parse_manifest(path)

    def test_pack_missing_required_field(self) -> None:
        path = self._manifest(
            "version: 1\n"
            "packs:\n"
            "  - name: foo\n"
            "    source: https://example.com/{ref}\n"
        )
        with self.assertRaisesRegex(crp.RulePackError, "missing 'default-ref'"):
            crp.parse_manifest(path)

    def test_duplicate_pack_name(self) -> None:
        path = self._manifest(
            "version: 1\n"
            "packs:\n"
            "  - name: foo\n"
            "    source: https://example.com/{ref}\n"
            "    default-ref: v1\n"
            "  - name: foo\n"
            "    source: https://example.com/{ref}\n"
            "    default-ref: v1\n"
        )
        with self.assertRaisesRegex(crp.RulePackError, "duplicate pack 'foo'"):
            crp.parse_manifest(path)

    def test_malformed_yaml(self) -> None:
        path = self._manifest("version: [unclosed\n")
        with self.assertRaisesRegex(crp.RulePackError, "malformed manifest"):
            crp.parse_manifest(path)


# ---------- parse_user_config ----------


class ParseUserConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _config(self, text: str, name: str = "agent-config.yaml") -> Path:
        p = self.root / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_missing_file_returns_none(self) -> None:
        """Missing file = no signal (None)."""
        self.assertIsNone(crp.parse_user_config(self.root / "missing.yaml"))

    def test_empty_file_returns_none(self) -> None:
        """Empty YAML file = no signal (None)."""
        p = self._config("")
        self.assertIsNone(crp.parse_user_config(p))

    def test_file_without_rule_packs_key_returns_none(self) -> None:
        """File with other keys but no rule_packs key = no signal (None)."""
        p = self._config("some_other_key: value\n")
        self.assertIsNone(crp.parse_user_config(p))

    def test_null_rule_packs_is_explicit_opt_out(self) -> None:
        """`rule_packs:` with null value = explicit opt-out ([])."""
        p = self._config("rule_packs:\n")
        self.assertEqual(crp.parse_user_config(p), [])

    def test_empty_list_rule_packs_is_explicit_opt_out(self) -> None:
        """`rule_packs: []` = explicit opt-out ([])."""
        p = self._config("rule_packs: []\n")
        self.assertEqual(crp.parse_user_config(p), [])

    def test_string_shorthand(self) -> None:
        p = self._config("rule_packs:\n  - agent-style\n")
        self.assertEqual(crp.parse_user_config(p), [{"name": "agent-style"}])

    def test_dict_with_ref_override(self) -> None:
        p = self._config(
            "rule_packs:\n"
            "  - name: agent-style\n"
            "    ref: v0.3.3\n"
        )
        self.assertEqual(
            crp.parse_user_config(p),
            [{"name": "agent-style", "ref": "v0.3.3"}],
        )

    def test_pack_missing_name(self) -> None:
        p = self._config("rule_packs:\n  - ref: v1\n")
        with self.assertRaisesRegex(crp.RulePackError, "missing 'name'"):
            crp.parse_user_config(p)

    def test_rule_packs_not_list(self) -> None:
        p = self._config("rule_packs: agent-style\n")
        with self.assertRaisesRegex(crp.RulePackError, "must be a list"):
            crp.parse_user_config(p)

    def test_pack_bad_type(self) -> None:
        p = self._config("rule_packs:\n  - 42\n")
        with self.assertRaisesRegex(crp.RulePackError, "must be a string or mapping"):
            crp.parse_user_config(p)

    def test_malformed_yaml(self) -> None:
        p = self._config("rule_packs: [unclosed\n")
        with self.assertRaisesRegex(crp.RulePackError, "malformed user config"):
            crp.parse_user_config(p)


# ---------- parse_env_packs ----------


class ParseEnvPacksTests(unittest.TestCase):
    def test_single_name(self) -> None:
        self.assertEqual(
            crp.parse_env_packs("agent-style"),
            [{"name": "agent-style"}],
        )

    def test_comma_separated(self) -> None:
        self.assertEqual(
            crp.parse_env_packs("a,b,c"),
            [{"name": "a"}, {"name": "b"}, {"name": "c"}],
        )

    def test_whitespace_separated(self) -> None:
        self.assertEqual(
            crp.parse_env_packs("a b c"),
            [{"name": "a"}, {"name": "b"}, {"name": "c"}],
        )

    def test_mixed_separators_and_padding(self) -> None:
        self.assertEqual(
            crp.parse_env_packs(" a,  b   c  ,  d"),
            [{"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d"}],
        )

    def test_duplicates_deduped(self) -> None:
        self.assertEqual(
            crp.parse_env_packs("a,b,a,c,b"),
            [{"name": "a"}, {"name": "b"}, {"name": "c"}],
        )


# ---------- merge_pack_selections ----------


class ResolveSelectionsTests(unittest.TestCase):
    def test_no_signal_returns_default(self) -> None:
        """All None / empty-env → apply default."""
        default = [{"name": "agent-style"}]
        self.assertEqual(
            crp.resolve_selections(None, None, [], default=default),
            default,
        )

    def test_no_signal_no_default_returns_empty(self) -> None:
        """All None / empty-env + no default → []."""
        self.assertEqual(crp.resolve_selections(None, None, []), [])

    def test_tracked_empty_is_explicit_opt_out(self) -> None:
        """tracked=[] is a signal; default NOT applied."""
        default = [{"name": "agent-style"}]
        self.assertEqual(
            crp.resolve_selections([], None, [], default=default),
            [],
        )

    def test_local_empty_is_explicit_opt_out(self) -> None:
        """local=[] is a signal; default NOT applied."""
        default = [{"name": "agent-style"}]
        self.assertEqual(
            crp.resolve_selections(None, [], [], default=default),
            [],
        )

    def test_env_alone_suppresses_default(self) -> None:
        """Env var alone is a signal; default NOT applied."""
        default = [{"name": "agent-style"}]
        self.assertEqual(
            crp.resolve_selections(None, None, [{"name": "foo"}], default=default),
            [{"name": "foo"}],
        )

    def test_tracked_only(self) -> None:
        tracked = [{"name": "a", "ref": "v1"}]
        self.assertEqual(
            crp.resolve_selections(tracked, [], []),
            [{"name": "a", "ref": "v1"}],
        )

    def test_local_overrides_tracked_ref(self) -> None:
        tracked = [{"name": "a", "ref": "v1"}]
        local = [{"name": "a", "ref": "v2"}]
        self.assertEqual(
            crp.resolve_selections(tracked, local, []),
            [{"name": "a", "ref": "v2"}],
        )

    def test_env_adds_new_pack(self) -> None:
        tracked = [{"name": "a"}]
        env = [{"name": "b"}]
        names = [e["name"] for e in crp.resolve_selections(tracked, [], env)]
        self.assertEqual(sorted(names), ["a", "b"])

    def test_env_does_not_override_existing_ref(self) -> None:
        tracked = [{"name": "a", "ref": "v1"}]
        env = [{"name": "a"}]
        merged = crp.resolve_selections(tracked, [], env)
        self.assertEqual(merged, [{"name": "a", "ref": "v1"}])

    def test_duplicate_in_tracked_warns(self) -> None:
        tracked = [
            {"name": "a", "ref": "v1"},
            {"name": "a", "ref": "v2"},
        ]
        with mock.patch.object(sys, "stderr", io.StringIO()) as err:
            merged = crp.resolve_selections(tracked, [], [])
            self.assertIn("duplicate pack 'a'", err.getvalue())
        self.assertEqual(merged, [{"name": "a", "ref": "v2"}])


# ---------- validate_ref ----------


class ValidateRefTests(unittest.TestCase):
    def test_semver_tag(self) -> None:
        crp.validate_ref("agent-style", "v0.3.2")  # does not raise

    def test_branch_name_with_slash(self) -> None:
        crp.validate_ref("agent-style", "feature/new-rules")

    def test_commit_sha(self) -> None:
        crp.validate_ref("agent-style", "a1b2c3d4e5f6")

    def test_reject_newline(self) -> None:
        with self.assertRaisesRegex(crp.RulePackError, "outside"):
            crp.validate_ref("agent-style", "v1\nmalicious")

    def test_reject_space(self) -> None:
        with self.assertRaisesRegex(crp.RulePackError, "outside"):
            crp.validate_ref("agent-style", "v1 x")

    def test_reject_shell_metachar(self) -> None:
        with self.assertRaisesRegex(crp.RulePackError, "outside"):
            crp.validate_ref("agent-style", "v1;rm")


# ---------- validate_rule_pack (marker rejection) ----------


class ValidateRulePackTests(unittest.TestCase):
    def test_clean_content_passes(self) -> None:
        crp.validate_rule_pack("agent-style", "# Rules\n\nSome rules here.\n")

    def test_reject_agent_claude(self) -> None:
        with self.assertRaisesRegex(crp.RulePackError, "routing marker"):
            crp.validate_rule_pack("p", "<!-- agent:claude -->\nfoo\n")

    def test_reject_agent_codex(self) -> None:
        with self.assertRaisesRegex(crp.RulePackError, "routing marker"):
            crp.validate_rule_pack("p", "<!-- agent:codex -->\n")

    def test_reject_agent_future_tag(self) -> None:
        with self.assertRaisesRegex(crp.RulePackError, "routing marker"):
            crp.validate_rule_pack("p", "<!-- agent:gemini -->\n")

    def test_reject_closing_form(self) -> None:
        with self.assertRaisesRegex(crp.RulePackError, "routing marker"):
            crp.validate_rule_pack("p", "<!-- /agent:claude -->\n")

    def test_reject_whitespace_variant(self) -> None:
        with self.assertRaisesRegex(crp.RulePackError, "routing marker"):
            crp.validate_rule_pack("p", "<!--   agent:claude  -->\n")

    def test_reject_no_space_variant(self) -> None:
        with self.assertRaisesRegex(crp.RulePackError, "routing marker"):
            crp.validate_rule_pack("p", "<!--agent:claude-->\n")

    def test_allow_rule_pack_delimiter(self) -> None:
        # Our own rule-pack:<name>:begin/end delimiters must NOT be rejected.
        crp.validate_rule_pack(
            "p",
            "<!-- rule-pack:agent-style:begin version=v1 sha256=abc -->\n"
            "content\n"
            "<!-- rule-pack:agent-style:end -->\n",
        )


# ---------- compose_agents_md (with mocked fetch) ----------


class ComposeAgentsMdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_dir = Path(self.tmp.name) / "rule-packs"
        self.manifest = {
            "agent-style": {
                "name": "agent-style",
                "source": "https://example.com/{ref}/docs/rule-pack.md",
                "default-ref": "v0.3.2",
            }
        }

    def test_empty_selections_returns_upstream_verbatim(self) -> None:
        upstream = "aa defaults\nline 2\n"
        out = crp.compose_agents_md(upstream, [], self.manifest, self.cache_dir, False)
        self.assertEqual(out, upstream)

    def test_single_pack_golden(self) -> None:
        upstream = "aa defaults\nend\n"
        pack_content = "# Writing rules\n\nBe concise.\n"
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: pack_content.encode("utf-8")}),
        ):
            out = crp.compose_agents_md(
                upstream,
                [{"name": "agent-style"}],
                self.manifest,
                self.cache_dir,
                False,
            )
        expected_sha = _sha(pack_content)
        expected = (
            "aa defaults\n"
            "end\n"
            "\n"
            f"<!-- rule-pack:agent-style:begin version=v0.3.2 sha256={expected_sha} -->\n"
            "# Writing rules\n"
            "\n"
            "Be concise.\n"
            "<!-- rule-pack:agent-style:end -->\n"
        )
        self.assertEqual(out, expected)
        # Cache files should exist.
        self.assertTrue((self.cache_dir / "agent-style-v0.3.2.md").exists())
        self.assertTrue((self.cache_dir / "agent-style-v0.3.2.md.sha256").exists())

    def test_ref_override_in_url(self) -> None:
        upstream = "aa\n"
        pack_content = "# Rules\n"
        url = "https://example.com/v9.9.9/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: pack_content.encode("utf-8")}),
        ):
            out = crp.compose_agents_md(
                upstream,
                [{"name": "agent-style", "ref": "v9.9.9"}],
                self.manifest,
                self.cache_dir,
                False,
            )
        self.assertIn("version=v9.9.9", out)

    def test_unknown_pack_raises(self) -> None:
        with self.assertRaisesRegex(crp.RulePackError, "unknown rule pack 'nope'"):
            crp.compose_agents_md(
                "aa\n",
                [{"name": "nope"}],
                self.manifest,
                self.cache_dir,
                False,
            )

    def test_rejects_pack_with_marker(self) -> None:
        upstream = "aa\n"
        bad_content = "# Rules\n\n<!-- agent:claude -->\nevil\n"
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: bad_content.encode("utf-8")}),
        ):
            with self.assertRaisesRegex(crp.RulePackError, "routing marker"):
                crp.compose_agents_md(
                    upstream,
                    [{"name": "agent-style"}],
                    self.manifest,
                    self.cache_dir,
                    False,
                )

    def test_fetch_failure_with_cache_uses_cache(self) -> None:
        upstream = "aa\n"
        pack_content = "# Cached rules\n"
        # Pre-populate cache.
        cache_md = self.cache_dir / "agent-style-v0.3.2.md"
        _write(cache_md, pack_content)
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: urllib.error.URLError("no net")}),
        ):
            # First call with cache available should succeed.
            out = crp.compose_agents_md(
                upstream,
                [{"name": "agent-style"}],
                self.manifest,
                self.cache_dir,
                no_cache=False,
            )
        self.assertIn("# Cached rules", out)

    def test_fetch_failure_no_cache_raises(self) -> None:
        upstream = "aa\n"
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: urllib.error.URLError("no net")}),
        ):
            with self.assertRaisesRegex(crp.RulePackError, "failed to fetch"):
                crp.compose_agents_md(
                    upstream,
                    [{"name": "agent-style"}],
                    self.manifest,
                    self.cache_dir,
                    no_cache=False,
                )

    def test_no_cache_flag_refetches(self) -> None:
        upstream = "aa\n"
        fresh = "# Fresh content\n"
        stale = "# Stale cached\n"
        # Pre-populate cache with stale content.
        cache_md = self.cache_dir / "agent-style-v0.3.2.md"
        _write(cache_md, stale)
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: fresh.encode("utf-8")}),
        ):
            out = crp.compose_agents_md(
                upstream,
                [{"name": "agent-style"}],
                self.manifest,
                self.cache_dir,
                no_cache=True,
            )
        self.assertIn("# Fresh content", out)
        self.assertNotIn("# Stale cached", out)

    def test_idempotent_rerun(self) -> None:
        upstream = "aa defaults\n"
        pack_content = "# Rules\nbe concise\n"
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: pack_content.encode("utf-8")}),
        ):
            out1 = crp.compose_agents_md(
                upstream,
                [{"name": "agent-style"}],
                self.manifest,
                self.cache_dir,
                False,
            )
            out2 = crp.compose_agents_md(
                upstream,
                [{"name": "agent-style"}],
                self.manifest,
                self.cache_dir,
                False,
            )
        self.assertEqual(out1, out2)

    def test_cache_path_cannot_escape_cache_dir(self) -> None:
        """Regression (H1): a ref with '/' or '..' must not escape cache_dir.

        validate_ref currently allows '/' (branch names) and '.' (semver
        tags), so refs like '../../../escape' pass ref validation. The
        quote() applied when constructing the cache filename must still
        confine every cache artifact inside cache_dir.
        """
        upstream = "aa\n"
        pack_content = "# rules\n"
        tmp_root = Path(self.tmp.name).resolve()
        cache_root = self.cache_dir.resolve()
        bad_ref = "../../../escape"
        url = f"https://example.com/{bad_ref}/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: pack_content.encode("utf-8")}),
        ):
            crp.compose_agents_md(
                upstream,
                [{"name": "agent-style", "ref": bad_ref}],
                self.manifest,
                self.cache_dir,
                False,
            )
        # Every file under the tmp root must live inside cache_dir.
        for p in tmp_root.rglob("*"):
            if p.is_file():
                self.assertTrue(
                    p.resolve().is_relative_to(cache_root),
                    f"cache artifact {p.resolve()} escapes {cache_root}",
                )

    def test_no_cache_true_fetch_failure_raises_even_with_cache(self) -> None:
        """Regression (M2): --no-cache forces refetch; a stale cache must not be silently served on fetch failure."""
        upstream = "aa\n"
        cached_content = "# Stale cached rules\n"
        cache_md = self.cache_dir / "agent-style-v0.3.2.md"
        _write(cache_md, cached_content)
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: urllib.error.URLError("no net")}),
        ):
            with self.assertRaisesRegex(crp.RulePackError, "--no-cache"):
                crp.compose_agents_md(
                    upstream,
                    [{"name": "agent-style"}],
                    self.manifest,
                    self.cache_dir,
                    no_cache=True,
                )
        # Stale cache remains untouched (we did not overwrite or clear it).
        self.assertEqual(
            cache_md.read_text(encoding="utf-8"),
            cached_content,
            "cache file should not be modified when --no-cache fetch fails",
        )


# ---------- atomic_write ----------


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_writes_new_file(self) -> None:
        target = self.root / "out.md"
        crp.atomic_write(target, "hello\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")

    def test_overwrites_existing(self) -> None:
        target = self.root / "out.md"
        target.write_text("old\n", encoding="utf-8")
        crp.atomic_write(target, "new\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_preserves_lf_newlines(self) -> None:
        target = self.root / "out.md"
        crp.atomic_write(target, "a\nb\nc\n")
        # Read as bytes to confirm no \r\n translation.
        self.assertEqual(target.read_bytes(), b"a\nb\nc\n")

    def test_failure_preserves_existing_file(self) -> None:
        target = self.root / "out.md"
        target.write_text("original\n", encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                crp.atomic_write(target, "replacement\n")
        # Original file must remain untouched.
        self.assertEqual(target.read_text(encoding="utf-8"), "original\n")


# ---------- do_compose (integration) ----------


class DoComposeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Minimal consumer-repo layout.
        self.agent_cfg_dir = self.root / ".agent-config"
        self.agent_cfg_dir.mkdir()
        (self.agent_cfg_dir / "AGENTS.md").write_text(
            "aa upstream content\n", encoding="utf-8"
        )
        self.manifest_path = self.agent_cfg_dir / "rule-packs.yaml"
        self.manifest_path.write_text(
            "version: 1\n"
            "packs:\n"
            "  - name: agent-style\n"
            "    source: https://example.com/{ref}/docs/rule-pack.md\n"
            "    default-ref: v0.3.2\n",
            encoding="utf-8",
        )

    def test_no_config_applies_default_agent_style(self) -> None:
        """With no user signal, the internal default rule pack [agent-style] is applied."""
        pack_content = "# default agent-style rules\n"
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: pack_content.encode("utf-8")}),
        ):
            rc = crp.do_compose(self.root, self.manifest_path, no_cache=False)
        self.assertEqual(rc, 0)
        out = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("aa upstream content", out)
        self.assertIn("# default agent-style rules", out)
        self.assertIn("rule-pack:agent-style:begin", out)

    def test_explicit_opt_out_empty_list_produces_upstream_byte_identical(self) -> None:
        """`rule_packs: []` disables all packs; AGENTS.md matches upstream exactly."""
        (self.root / "agent-config.yaml").write_text(
            "rule_packs: []\n", encoding="utf-8"
        )
        rc = crp.do_compose(self.root, self.manifest_path, no_cache=False)
        self.assertEqual(rc, 0)
        self.assertEqual(
            (self.root / "AGENTS.md").read_bytes(),
            b"aa upstream content\n",
        )

    def test_explicit_opt_out_null_value_produces_upstream_byte_identical(self) -> None:
        """`rule_packs:` with null value disables all packs."""
        (self.root / "agent-config.yaml").write_text(
            "rule_packs:\n", encoding="utf-8"
        )
        rc = crp.do_compose(self.root, self.manifest_path, no_cache=False)
        self.assertEqual(rc, 0)
        self.assertEqual(
            (self.root / "AGENTS.md").read_bytes(),
            b"aa upstream content\n",
        )

    def test_file_without_rule_packs_key_applies_default(self) -> None:
        """agent-config.yaml present but without rule_packs key = no signal → default applies."""
        (self.root / "agent-config.yaml").write_text(
            "some_other_key: value\n", encoding="utf-8"
        )
        pack_content = "# default rules\n"
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: pack_content.encode("utf-8")}),
        ):
            rc = crp.do_compose(self.root, self.manifest_path, no_cache=False)
        self.assertEqual(rc, 0)
        out = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("# default rules", out)

    def test_tracked_yaml_opt_in(self) -> None:
        (self.root / "agent-config.yaml").write_text(
            "rule_packs:\n  - agent-style\n", encoding="utf-8"
        )
        pack_content = "# writing rules\n"
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: pack_content.encode("utf-8")}),
        ):
            rc = crp.do_compose(self.root, self.manifest_path, no_cache=False)
        self.assertEqual(rc, 0)
        out = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("aa upstream content", out)
        self.assertIn("# writing rules", out)
        self.assertIn(f"sha256={_sha(pack_content)}", out)

    def test_env_var_opt_in(self) -> None:
        pack_content = "# writing rules\n"
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch.dict(os.environ, {"AGENT_CONFIG_RULE_PACKS": "agent-style"}):
            with mock.patch(
                "urllib.request.urlopen",
                _fake_urlopen({url: pack_content.encode("utf-8")}),
            ):
                rc = crp.do_compose(self.root, self.manifest_path, no_cache=False)
        self.assertEqual(rc, 0)
        out = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("# writing rules", out)

    def test_missing_upstream_errors(self) -> None:
        (self.agent_cfg_dir / "AGENTS.md").unlink()
        rc = crp.do_compose(self.root, self.manifest_path, no_cache=False)
        self.assertEqual(rc, 1)

    def test_fetch_failure_does_not_modify_agents_md(self) -> None:
        (self.root / "AGENTS.md").write_text(
            "old composed content\n", encoding="utf-8"
        )
        (self.root / "agent-config.yaml").write_text(
            "rule_packs:\n  - agent-style\n", encoding="utf-8"
        )
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: urllib.error.URLError("no net")}),
        ):
            rc = crp.do_compose(self.root, self.manifest_path, no_cache=False)
        self.assertEqual(rc, 1)
        # AGENTS.md must remain unchanged.
        self.assertEqual(
            (self.root / "AGENTS.md").read_text(encoding="utf-8"),
            "old composed content\n",
        )

    def test_unknown_pack_errors_without_writing(self) -> None:
        (self.root / "AGENTS.md").write_text(
            "old composed content\n", encoding="utf-8"
        )
        (self.root / "agent-config.yaml").write_text(
            "rule_packs:\n  - nonexistent\n", encoding="utf-8"
        )
        rc = crp.do_compose(self.root, self.manifest_path, no_cache=False)
        self.assertEqual(rc, 1)
        self.assertEqual(
            (self.root / "AGENTS.md").read_text(encoding="utf-8"),
            "old composed content\n",
        )

    def test_local_override_ref(self) -> None:
        (self.root / "agent-config.yaml").write_text(
            "rule_packs:\n  - name: agent-style\n    ref: v0.3.2\n",
            encoding="utf-8",
        )
        (self.root / "agent-config.local.yaml").write_text(
            "rule_packs:\n  - name: agent-style\n    ref: v9.9.9\n",
            encoding="utf-8",
        )
        pack_content = "# v9.9.9 rules\n"
        url = "https://example.com/v9.9.9/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: pack_content.encode("utf-8")}),
        ):
            rc = crp.do_compose(self.root, self.manifest_path, no_cache=False)
        self.assertEqual(rc, 0)
        out = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("version=v9.9.9", out)

    def test_idempotent_rerun(self) -> None:
        (self.root / "agent-config.yaml").write_text(
            "rule_packs:\n  - agent-style\n", encoding="utf-8"
        )
        pack_content = "# Rules\n"
        url = "https://example.com/v0.3.2/docs/rule-pack.md"
        with mock.patch(
            "urllib.request.urlopen",
            _fake_urlopen({url: pack_content.encode("utf-8")}),
        ):
            rc1 = crp.do_compose(self.root, self.manifest_path, no_cache=False)
            first = (self.root / "AGENTS.md").read_bytes()
            rc2 = crp.do_compose(self.root, self.manifest_path, no_cache=False)
            second = (self.root / "AGENTS.md").read_bytes()
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(first, second)


# ---------- manifest-file references agent-style at expected default-ref ----------


class ShippedManifestTests(unittest.TestCase):
    """Sanity checks on the real bootstrap/packs.yaml we ship."""

    MANIFEST = ROOT / "bootstrap" / "packs.yaml"

    def test_manifest_file_exists(self) -> None:
        self.assertTrue(self.MANIFEST.exists())

    def test_legacy_rule_packs_yaml_absent(self) -> None:
        """After v0.4.0 rename, only packs.yaml is shipped; rule-packs.yaml is gone."""
        legacy = ROOT / "bootstrap" / "rule-packs.yaml"
        self.assertFalse(
            legacy.exists(),
            "bootstrap/rule-packs.yaml should be renamed to packs.yaml in v0.4.0",
        )

    def test_manifest_parses(self) -> None:
        packs = crp.parse_manifest(self.MANIFEST)
        self.assertIn("agent-style", packs)
        entry = packs["agent-style"]
        self.assertIn("{ref}", entry["source"])
        self.assertTrue(entry["default-ref"].startswith("v"))


# ---------- do_print_yaml ----------


class DoPrintYamlTests(unittest.TestCase):
    def test_snippet_contains_pack_name(self) -> None:
        with mock.patch.object(sys, "stdout", io.StringIO()) as out:
            rc = crp.do_print_yaml("agent-style")
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("rule_packs:", text)
        self.assertIn("- name: agent-style", text)
        self.assertIn("bash bootstrap.sh", text)


# ---------- PyYAML-missing behavior (M3) ----------


class PyYamlMissingTests(unittest.TestCase):
    """Regression (M3): helper emits a distinct PyYAML-required error when yaml is absent.

    PLAN dependency contract declares the rule-pack opt-in path requires
    Python 3.x plus PyYAML. This test simulates PyYAML absence by setting
    sys.modules['yaml'] = None in a fresh Python subprocess, then
    importing the helper module. The helper must exit cleanly with a
    message that points the user at `pip install pyyaml`.
    """

    def test_helper_exits_cleanly_when_pyyaml_missing(self) -> None:
        script = (
            "import sys\n"
            "sys.modules['yaml'] = None\n"
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
            "import compose_rule_packs\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            2,
            f"expected exit 2 for PyYAML-missing, got {result.returncode}; stderr={result.stderr!r}",
        )
        self.assertIn("PyYAML", result.stderr)
        self.assertIn("pip install pyyaml", result.stderr)


# ---------- bootstrap.sh CLI contract ----------


class BashCliTests(unittest.TestCase):
    """CLI-contract regressions for bootstrap/bootstrap.sh.

    These exercise arg-parsing and dry-helper paths only; they do not run
    the full bootstrap flow (no curl, no git clone). Each subprocess call
    uses a fresh tmp cwd so there is no filesystem mutation outside the
    throwaway directory.
    """

    BOOTSTRAP_SH = ROOT / "bootstrap" / "bootstrap.sh"

    @classmethod
    def setUpClass(cls) -> None:
        # Windows ships a WSL launcher at C:\WINDOWS\system32\bash.exe that
        # fails with "execvpe(/bin/bash) failed" when WSL itself is not
        # installed, and it leaks a lock on the subprocess cwd so tmp
        # cleanup breaks too. shutil.which("bash") finds the WSL launcher
        # first on default Windows PATH, which would turn these tests into
        # noise across environments. Prefer Git Bash's known install paths
        # on Windows, then fall back to PATH, and probe each candidate by
        # running bootstrap.sh --help to confirm it actually works.
        candidates: list[str] = []
        if os.name == "nt":
            candidates.extend(
                [
                    r"C:\Program Files\Git\bin\bash.exe",
                    r"C:\Program Files\Git\usr\bin\bash.exe",
                ]
            )
        from_path = shutil.which("bash")
        if from_path:
            candidates.append(from_path)

        seen: set[str] = set()
        for shell in candidates:
            if shell in seen or not Path(shell).exists():
                continue
            seen.add(shell)
            probe = subprocess.run(
                [shell, str(cls.BOOTSTRAP_SH), "--help"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            if probe.returncode == 0 and "Usage:" in probe.stdout:
                cls.shell = shell
                return
        raise unittest.SkipTest("usable bash not available")

    def _run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run(
                [self.shell, str(self.BOOTSTRAP_SH), *args],
                cwd=tmp,
                capture_output=True,
                text=True,
                env=env,
            )

    def test_rule_packs_equals_empty_rejected(self) -> None:
        """Regression (R2 M1): `--rule-packs=` with empty value must not fall through to a real bootstrap."""
        result = self._run("--rule-packs=")
        self.assertEqual(result.returncode, 1, f"stderr={result.stderr!r}")
        self.assertIn("--rule-packs requires a pack name", result.stderr)

    def test_rule_packs_space_form_missing_value_rejected(self) -> None:
        result = self._run("--rule-packs")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--rule-packs requires a pack name", result.stderr)

    def test_rule_packs_equals_prints_yaml_snippet(self) -> None:
        """Happy path: `--rule-packs=agent-style` prints the YAML snippet and exits."""
        result = self._run("--rule-packs=agent-style")
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr!r}")
        self.assertIn("rule_packs:", result.stdout)
        self.assertIn("- name: agent-style", result.stdout)

    def test_rule_packs_space_form_prints_yaml_snippet(self) -> None:
        result = self._run("--rule-packs", "agent-style")
        self.assertEqual(result.returncode, 0)
        self.assertIn("- name: agent-style", result.stdout)

    def test_unknown_flag_rejected_with_usage(self) -> None:
        result = self._run("--no-such-flag")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown flag", result.stderr)
        self.assertIn("--rule-packs", result.stderr)

    def test_help_prints_usage(self) -> None:
        result = self._run("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)

    def test_flag_over_env_var_precedence(self) -> None:
        """`--rule-packs` wins over `AGENT_CONFIG_RULE_PACKS`; env-var ignored in dry-helper mode, notice emitted."""
        env = dict(os.environ)
        env["AGENT_CONFIG_RULE_PACKS"] = "foo"
        result = self._run("--rule-packs=agent-style", env=env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("- name: agent-style", result.stdout)
        self.assertIn("AGENT_CONFIG_RULE_PACKS env var is ignored", result.stderr)


# ---------- bootstrap.ps1 Python-probe regression (R6 H1) ----------


class PowerShellPythonProbeTests(unittest.TestCase):
    """Regression (R6 H1): the Python one-liner embedded in bootstrap.ps1's
    Test-PythonRuns function must be valid Python syntax and exit 0 on a
    working Python 3 interpreter. This guards against using PowerShell
    operators (like `-ge`) inside the Python expression, which would make
    every valid Python fail the probe and silently disable default-on
    rule-pack composition on Windows."""

    BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"

    def test_python_probe_is_valid_python_and_exits_zero(self) -> None:
        import re

        text = self.BOOTSTRAP_PS1.read_text(encoding="utf-8")
        # Find the probe command: & $PythonPath -c "import sys; ... SystemExit(...)"
        match = re.search(
            r'-c\s+"([^"]*import sys[^"]*SystemExit[^"]*)"', text
        )
        self.assertIsNotNone(
            match, "bootstrap.ps1 must contain a Test-PythonRuns probe command"
        )
        expr = match.group(1)
        result = subprocess.run(
            [sys.executable, "-c", expr],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"PS1 python-probe expression {expr!r} must exit 0 on Python 3; "
            f"stderr={result.stderr!r}",
        )


# ---------- bootstrap.ps1 CLI contract ----------


class PowerShellCliTests(unittest.TestCase):
    """CLI-contract regressions for bootstrap/bootstrap.ps1.

    Mirrors BashCliTests. Exercises param parsing and dry-helper paths
    only; no full bootstrap flow (no web request, no git clone). Each
    subprocess call uses a fresh tmp cwd. Selection probes pwsh and
    powershell via --help and picks one that actually runs the script.
    """

    BOOTSTRAP_PS1 = ROOT / "bootstrap" / "bootstrap.ps1"

    @classmethod
    def setUpClass(cls) -> None:
        candidates: list[str] = []
        if os.name == "nt":
            candidates.extend(
                [
                    r"C:\Program Files\PowerShell\7\pwsh.exe",
                    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                ]
            )
        for cmd in ("pwsh", "powershell"):
            found = shutil.which(cmd)
            if found:
                candidates.append(found)
        seen: set[str] = set()
        for shell in candidates:
            if shell in seen or not Path(shell).exists():
                continue
            seen.add(shell)
            probe = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(cls.BOOTSTRAP_PS1),
                    "-Help",
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            if probe.returncode == 0 and "Usage:" in probe.stdout:
                cls.shell = shell
                return
        raise unittest.SkipTest("usable PowerShell not available")

    def _run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run(
                [
                    self.shell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self.BOOTSTRAP_PS1),
                    *args,
                ],
                cwd=tmp,
                capture_output=True,
                text=True,
                env=env,
            )

    def test_help_prints_usage(self) -> None:
        result = self._run("-Help")
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr!r}")
        self.assertIn("Usage:", result.stdout)

    def test_rule_packs_empty_rejected(self) -> None:
        """Regression mirror of BashCliTests R2 M1: -RulePacks '' must error before bootstrap runs."""
        result = self._run("-RulePacks", "")
        self.assertEqual(result.returncode, 1, f"stderr={result.stderr!r}")
        self.assertIn("-RulePacks requires a pack name", result.stderr)

    def test_rule_packs_prints_yaml_snippet(self) -> None:
        result = self._run("-RulePacks", "agent-style")
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr!r}")
        self.assertIn("rule_packs:", result.stdout)
        self.assertIn("- name: agent-style", result.stdout)

    def test_unknown_param_rejected(self) -> None:
        """PowerShell natively rejects unknown named parameters."""
        result = self._run("-NoSuchParam")
        self.assertNotEqual(result.returncode, 0)

    def test_flag_over_env_var_precedence(self) -> None:
        """`-RulePacks` wins over `AGENT_CONFIG_RULE_PACKS`; env-var ignored in dry-helper mode, notice emitted."""
        env = dict(os.environ)
        env["AGENT_CONFIG_RULE_PACKS"] = "foo"
        result = self._run("-RulePacks", "agent-style", env=env)
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr!r}")
        self.assertIn("- name: agent-style", result.stdout)
        self.assertIn("AGENT_CONFIG_RULE_PACKS env var is ignored", result.stderr)


if __name__ == "__main__":
    unittest.main()
