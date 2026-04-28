"""Tests for scripts/packs/config.py (XDG paths + 4-layer merge + env var)."""
from __future__ import annotations

import sys
import tempfile
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import auth  # noqa: E402
from packs import config  # noqa: E402


class UserConfigHomeTests(unittest.TestCase):
    def test_posix_xdg_wins(self) -> None:
        env = {"XDG_CONFIG_HOME": "/tmp/xdg", "HOME": "/home/u"}
        if sys.platform == "win32":
            self.skipTest("POSIX-only")
        self.assertEqual(
            config.user_config_home(env),
            Path("/tmp/xdg") / "anywhere-agents",
        )

    def test_posix_home_fallback(self) -> None:
        env = {"HOME": "/home/u"}
        if sys.platform == "win32":
            self.skipTest("POSIX-only")
        self.assertEqual(
            config.user_config_home(env),
            Path("/home/u/.config/anywhere-agents"),
        )

    def test_windows_appdata(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only")
        env = {"APPDATA": "C:\\Users\\u\\AppData\\Roaming"}
        self.assertEqual(
            config.user_config_home(env),
            Path("C:\\Users\\u\\AppData\\Roaming") / "anywhere-agents",
        )

    def test_missing_returns_none(self) -> None:
        self.assertIsNone(config.user_config_home({}))


class LoadSaveConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_absent_file_returns_none(self) -> None:
        self.assertIsNone(config.load_config_file(self.root / "none.yaml"))

    def test_round_trip(self) -> None:
        path = self.root / "config.yaml"
        payload = {"packs": [{"name": "foo", "ref": "main"}]}
        config.save_config_file(path, payload)
        loaded = config.load_config_file(path)
        self.assertEqual(loaded, payload)

    def test_malformed_yaml_raises(self) -> None:
        path = self.root / "config.yaml"
        path.write_text("key: [unclosed\n", encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, r"malformed YAML"):
            config.load_config_file(path)

    def test_non_mapping_top_level_rejects(self) -> None:
        path = self.root / "config.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")
        with self.assertRaisesRegex(config.ConfigError, r"must be a mapping"):
            config.load_config_file(path)


class EnvVarGrammarTests(unittest.TestCase):
    def test_empty_env_returns_empty(self) -> None:
        add, sub = config.parse_env_var({})
        self.assertEqual(add, [])
        self.assertEqual(sub, [])

    def test_add_and_subtract(self) -> None:
        env = {"AGENT_CONFIG_PACKS": "foo,-bar,baz"}
        add, sub = config.parse_env_var(env)
        self.assertEqual(add, ["foo", "baz"])
        self.assertEqual(sub, ["bar"])

    def test_url_rejected(self) -> None:
        env = {"AGENT_CONFIG_PACKS": "https://example.com/foo"}
        with self.assertRaisesRegex(config.ConfigError, r"names-only"):
            config.parse_env_var(env)

    def test_legacy_env_accepted_with_warning(self) -> None:
        env = {"AGENT_CONFIG_RULE_PACKS": "foo,bar"}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            add, sub = config.parse_env_var(env)
        self.assertEqual(add, ["foo", "bar"])
        self.assertTrue(
            any(issubclass(w.category, DeprecationWarning) for w in caught)
        )

    def test_canonical_wins_over_legacy(self) -> None:
        env = {
            "AGENT_CONFIG_PACKS": "new",
            "AGENT_CONFIG_RULE_PACKS": "old",
        }
        add, _ = config.parse_env_var(env)
        self.assertEqual(add, ["new"])


class FourLayerMergeTests(unittest.TestCase):
    def test_no_signals_returns_default(self) -> None:
        result = config.resolve_selections(
            default_selections=[{"name": "agent-style"}],
        )
        self.assertEqual(result, [{"name": "agent-style"}])

    def test_force_defaults_merge_with_user_layer(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": [{"name": "profile"}]},
            default_selections=[
                {"name": "agent-style"},
                {"name": "aa-core-skills"},
            ],
            force_defaults=True,
        )
        self.assertEqual(
            [p["name"] for p in result],
            ["agent-style", "aa-core-skills", "profile"],
        )

    def test_force_defaults_explicit_empty_clears_defaults(self) -> None:
        result = config.resolve_selections(
            project_tracked={"packs": []},
            default_selections=[{"name": "agent-style"}],
            force_defaults=True,
        )
        self.assertEqual(result, [])

    def test_force_defaults_env_subtract_removes_default(self) -> None:
        result = config.resolve_selections(
            default_selections=[{"name": "agent-style"}],
            force_defaults=True,
            env_subtract=["agent-style"],
        )
        self.assertEqual(result, [])

    def test_no_signals_no_default_empty(self) -> None:
        result = config.resolve_selections()
        self.assertEqual(result, [])

    def test_user_level_sets_base(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": [{"name": "a"}, {"name": "b"}]},
        )
        self.assertEqual(
            sorted(p["name"] for p in result), ["a", "b"]
        )

    def test_more_specific_overrides_same_name(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": [{"name": "a", "ref": "user"}]},
            project_tracked={"packs": [{"name": "a", "ref": "tracked"}]},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ref"], "tracked")

    def test_explicit_empty_clears_earlier_layers(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": [{"name": "a"}]},
            project_tracked={"packs": []},
        )
        self.assertEqual(result, [])

    def test_env_var_overlay_adds_after_clear(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": [{"name": "a"}]},
            project_tracked={"packs": []},
            env_add=["new-from-env"],
        )
        self.assertEqual([p["name"] for p in result], ["new-from-env"])

    def test_env_subtract_removes_from_resolved(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": [{"name": "a"}, {"name": "b"}]},
            env_subtract=["a"],
        )
        self.assertEqual([p["name"] for p in result], ["b"])

    def test_legacy_rule_packs_key_accepted(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = config.resolve_selections(
                user_level={"rule_packs": [{"name": "legacy-pack"}]},
            )
        self.assertEqual([p["name"] for p in result], ["legacy-pack"])

    def test_packs_wins_when_both_keys_present(self) -> None:
        result = config.resolve_selections(
            user_level={
                "packs": [{"name": "new"}],
                "rule_packs": [{"name": "old"}],
            },
        )
        self.assertEqual([p["name"] for p in result], ["new"])

    def test_short_form_name_normalized_to_dict(self) -> None:
        result = config.resolve_selections(
            user_level={"packs": ["a", "b"]},
        )
        self.assertEqual(result, [{"name": "a"}, {"name": "b"}])


class TestResolveSelectionsURLValidation(unittest.TestCase):
    """v0.5.0 R1 M8 + Deferral 3: per-layer credential URL validation hook.

    ``resolve_selections`` accepts a ``validate_url_fn`` keyword that
    receives every source URL it encounters with the layer name as
    ``source_layer=...``. Passing ``auth.reject_credential_url`` enforces
    parse-time credential-URL rejection at the user-level,
    project-tracked, and project-local layers.
    """

    def test_credential_url_in_user_layer_rejected(self) -> None:
        user_layer = {"packs": [{
            "name": "x",
            "source": {"url": "https://ghp_secret@github.com/y/z", "ref": "main"},
        }]}
        with self.assertRaises(auth.CredentialURLError) as cm:
            config.resolve_selections(
                user_level=user_layer,
                project_tracked=None,
                project_local=None,
                validate_url_fn=auth.reject_credential_url,
            )
        # The error message must identify the offending layer so the
        # user can find the bad URL in their config files.
        self.assertIn("user-level", str(cm.exception))

    def test_credential_url_in_project_tracked_rejected(self) -> None:
        tracked = {"packs": [{
            "name": "x",
            "source": {"url": "https://token@github.com/y/z", "ref": "main"},
        }]}
        with self.assertRaises(auth.CredentialURLError) as cm:
            config.resolve_selections(
                user_level=None,
                project_tracked=tracked,
                project_local=None,
                validate_url_fn=auth.reject_credential_url,
            )
        self.assertIn("project-tracked", str(cm.exception))

    def test_credential_url_in_project_local_rejected(self) -> None:
        local_layer = {"packs": [{
            "name": "x",
            "source": {"url": "https://user:pass@github.com/y/z", "ref": "main"},
        }]}
        with self.assertRaises(auth.CredentialURLError) as cm:
            config.resolve_selections(
                user_level=None,
                project_tracked=None,
                project_local=local_layer,
                validate_url_fn=auth.reject_credential_url,
            )
        self.assertIn("project-local", str(cm.exception))

    def test_clean_urls_pass_through(self) -> None:
        """Happy path: non-credential URLs do not trigger rejection.
        Covers HTTPS without userinfo, scp-style SSH, and ssh:// without
        password — all three forms are explicitly allowed by
        ``auth.reject_credential_url``.
        """
        result = config.resolve_selections(
            user_level={"packs": [{
                "name": "https-clean",
                "source": {"url": "https://github.com/y/z", "ref": "main"},
            }]},
            project_tracked={"packs": [{
                "name": "ssh-scp",
                "source": {"url": "git@github.com:y/z.git", "ref": "main"},
            }]},
            project_local={"packs": [{
                "name": "ssh-url",
                "source": {"url": "ssh://git@github.com/y/z", "ref": "main"},
            }]},
            validate_url_fn=auth.reject_credential_url,
        )
        self.assertEqual(
            sorted(p["name"] for p in result),
            ["https-clean", "ssh-scp", "ssh-url"],
        )

    def test_validator_receives_layer_name_kwarg(self) -> None:
        """The validate_url_fn must receive ``source_layer`` so callers
        can produce per-layer error messages. Use a recording stub to
        verify each layer dispatches with the right name."""
        calls: list[tuple[str, str]] = []

        def recorder(url: str, *, source_layer: str = "manifest") -> None:
            calls.append((url, source_layer))

        config.resolve_selections(
            user_level={"packs": [{
                "name": "u",
                "source": {"url": "https://github.com/u/u", "ref": "main"},
            }]},
            project_tracked={"packs": [{
                "name": "t",
                "source": {"url": "https://github.com/t/t", "ref": "main"},
            }]},
            project_local={"packs": [{
                "name": "l",
                "source": {"url": "https://github.com/l/l", "ref": "main"},
            }]},
            validate_url_fn=recorder,
        )
        # Assert the full (url, layer) mapping, not just the layer set —
        # a buggy implementation that swapped two URL→layer pairs (e.g.
        # dispatched the user-level URL with source_layer="project-local")
        # would still pass a layer-name-only check.
        self.assertEqual(
            sorted(calls),
            sorted([
                ("https://github.com/u/u", "user-level"),
                ("https://github.com/t/t", "project-tracked"),
                ("https://github.com/l/l", "project-local"),
            ]),
        )

    def test_string_form_source_validated(self) -> None:
        """``source: <url>`` (string short-form) goes through the
        validator just like the dict form."""
        with self.assertRaises(auth.CredentialURLError):
            config.resolve_selections(
                user_level={"packs": [{
                    "name": "x",
                    "source": "https://ghp_secret@github.com/y/z",
                }]},
                validate_url_fn=auth.reject_credential_url,
            )

    def test_repo_alias_in_source_dict_validated(self) -> None:
        """v0.4.0 schema accepts both ``source.url`` (legacy) and
        ``source.repo`` (canonical); the validator checks whichever is
        present, preferring ``repo`` when both exist (matches schema.py)."""
        with self.assertRaises(auth.CredentialURLError):
            config.resolve_selections(
                user_level={"packs": [{
                    "name": "x",
                    "source": {"repo": "https://token@github.com/y/z", "ref": "main"},
                }]},
                validate_url_fn=auth.reject_credential_url,
            )

    def test_no_validator_skips_url_check(self) -> None:
        """When validate_url_fn is None (or unset), no URL inspection
        happens — preserves backward compatibility with existing v0.4.0
        callers that did not pass the new keyword argument."""
        # A credential URL would normally be rejected; with no
        # validator, resolve_selections passes it through to the merge
        # logic. The pack still emerges in the resolved selection.
        result = config.resolve_selections(
            user_level={"packs": [{
                "name": "x",
                "source": {"url": "https://ghp_secret@github.com/y/z", "ref": "main"},
            }]},
        )
        self.assertEqual([p["name"] for p in result], ["x"])

    def test_url_with_userinfo_redacted_in_error(self) -> None:
        """Per Phase 2 H1, the credential URL is redacted in the error
        message so logs and stderr never contain the raw token."""
        with self.assertRaises(auth.CredentialURLError) as cm:
            config.resolve_selections(
                user_level={"packs": [{
                    "name": "x",
                    "source": {"url": "https://ghp_abc123def456@github.com/y/z", "ref": "main"},
                }]},
                validate_url_fn=auth.reject_credential_url,
            )
        msg = str(cm.exception)
        self.assertNotIn("ghp_abc123def456", msg)
        self.assertIn("<redacted>", msg)


if __name__ == "__main__":
    unittest.main()
