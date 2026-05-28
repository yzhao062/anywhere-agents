"""Tests for scripts/packs/handlers/skill.py."""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from packs import dispatch  # noqa: E402
from packs import handlers  # noqa: E402 — side-effect: registers handlers
from packs import state as state_mod  # noqa: E402
from packs import transaction as txn_mod  # noqa: E402
from packs.handlers import skill  # noqa: E402


def _make_ctx(
    *,
    pack_name: str,
    pack_source_dir: Path,
    project_root: Path,
    user_home: Path,
    txn: txn_mod.Transaction,
) -> dispatch.DispatchContext:
    return dispatch.DispatchContext(
        pack_name=pack_name,
        pack_source_url="bundled:aa",
        pack_requested_ref="bundled",
        pack_resolved_commit="bundled",
        pack_update_policy="locked",
        pack_source_dir=pack_source_dir,
        project_root=project_root,
        user_home=user_home,
        repo_id="test-repo-id",
        txn=txn,
        pack_lock=state_mod.empty_pack_lock(),
        project_state=state_mod.empty_project_state(),
        user_state=state_mod.empty_user_state(),
    )


class _TmpDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pack_source_dir = self.root / "pack_src"
        self.pack_source_dir.mkdir()
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.user_home = self.root / "home"
        self.user_home.mkdir()
        self.staging = self.root / "stage.staging-skill"
        self.lock_path = self.root / "peer.lock"
        self.lock_path.write_text("0\n", encoding="utf-8")


class SkillDirectoryCopyTests(_TmpDirCase):
    def test_single_file_copy(self) -> None:
        """A file-mapping in files list produces one active-skill record
        and stages an atomic write through the transaction."""
        # Arrange a pointer file as the single source file.
        # Use write_bytes so Windows line-ending translation does not
        # change the on-disk content.
        src = self.pack_source_dir / ".claude" / "commands" / "foo.md"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"pointer content\n")

        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="foo",
                pack_source_dir=self.pack_source_dir,
                project_root=self.project_root,
                user_home=self.user_home,
                txn=txn,
            )
            skill.handle_skill(
                {
                    "kind": "skill",
                    "hosts": ["claude-code"],
                    "files": [
                        {
                            "from": ".claude/commands/foo.md",
                            "to": ".claude/commands/foo.md",
                        }
                    ],
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        # Verify the file was written to the project root.
        dst = self.project_root / ".claude" / "commands" / "foo.md"
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_bytes(), b"pointer content\n")

        # Verify the lock entry was recorded.
        pack_entry = ctx.pack_lock["packs"]["foo"]
        self.assertEqual(len(pack_entry["files"]), 1)
        file_entry = pack_entry["files"][0]
        self.assertEqual(file_entry["role"], "active-skill")
        self.assertEqual(file_entry["output_paths"], [".claude/commands/foo.md"])
        self.assertEqual(file_entry["output_scope"], "project-local")
        self.assertEqual(
            file_entry["input_sha256"],
            hashlib.sha256(b"pointer content\n").hexdigest(),
        )

    def test_directory_deep_copy_preserves_structure(self) -> None:
        """A directory mapping deep-copies every file, preserving nested
        structure, and records one active-skill lock entry with a
        dir-sha256 input hash."""
        skill_dir = self.pack_source_dir / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Skill\n", encoding="utf-8")
        (skill_dir / "references").mkdir()
        (skill_dir / "references" / "note.md").write_text(
            "reference\n", encoding="utf-8"
        )

        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="my-skill",
                pack_source_dir=self.pack_source_dir,
                project_root=self.project_root,
                user_home=self.user_home,
                txn=txn,
            )
            skill.handle_skill(
                {
                    "kind": "skill",
                    "hosts": ["claude-code"],
                    "files": [
                        {
                            "from": "skills/my-skill/",
                            "to": ".claude/skills/my-skill/",
                        }
                    ],
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        self.assertTrue(
            (self.project_root / ".claude" / "skills" / "my-skill" / "SKILL.md").exists()
        )
        self.assertTrue(
            (
                self.project_root / ".claude" / "skills" / "my-skill" / "references" / "note.md"
            ).exists()
        )
        # Find the active-skill entry by role (the auto-emitted pointer
        # also creates a generated-command entry in the same pack).
        active_skill_entries = [
            f for f in ctx.pack_lock["packs"]["my-skill"]["files"]
            if f["role"] == "active-skill"
        ]
        self.assertEqual(len(active_skill_entries), 1)
        file_entry = active_skill_entries[0]
        self.assertTrue(file_entry["input_sha256"].startswith("dir-sha256:"))

    def test_dir_sha256_is_stable_across_sort_order(self) -> None:
        """The merkle hash is path-sorted, so the same content in any
        iteration order produces the same sha."""
        d1 = self.pack_source_dir / "s1"
        d2 = self.pack_source_dir / "s2"
        d1.mkdir()
        d2.mkdir()
        # Same content; different file-creation order.
        (d1 / "a.txt").write_bytes(b"a")
        (d1 / "b.txt").write_bytes(b"b")
        (d2 / "b.txt").write_bytes(b"b")
        (d2 / "a.txt").write_bytes(b"a")

        def _run(src_name: str, pack_name: str) -> str:
            staging = self.root / f"stage.staging-{pack_name}"
            with txn_mod.Transaction(staging, self.lock_path) as txn:
                ctx = _make_ctx(
                    pack_name=pack_name,
                    pack_source_dir=self.pack_source_dir,
                    project_root=self.project_root / pack_name,
                    user_home=self.user_home,
                    txn=txn,
                )
                skill.handle_skill(
                    {
                        "kind": "skill",
                        "hosts": ["claude-code"],
                        "files": [
                            {"from": src_name, "to": f".claude/skills/{pack_name}/"}
                        ],
                    },
                    ctx,
                )
                ctx.finalize_pack_lock()
            return ctx.pack_lock["packs"][pack_name]["files"][0]["input_sha256"]

        sha_1 = _run("s1", "pack1")
        sha_2 = _run("s2", "pack2")
        self.assertEqual(sha_1, sha_2)

    def test_directory_only_skill_auto_emits_pointer(self) -> None:
        """Regression for Round 2 Codex High: kind:skill with ONLY a
        directory mapping (no explicit pointer file) must auto-emit the
        canonical pointer at .claude/commands/<name>.md per
        pack-architecture.md:184."""
        skill_dir = self.pack_source_dir / "skills" / "third-party-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(b"# third-party\n")

        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="third-party",
                pack_source_dir=self.pack_source_dir,
                project_root=self.project_root,
                user_home=self.user_home,
                txn=txn,
            )
            skill.handle_skill(
                {
                    "kind": "skill",
                    "hosts": ["claude-code"],
                    "files": [
                        {
                            "from": "skills/third-party-skill/",
                            "to": ".claude/skills/third-party-skill/",
                        }
                    ],
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        pointer_path = (
            self.project_root / ".claude" / "commands" / "third-party-skill.md"
        )
        self.assertTrue(pointer_path.exists())
        pointer_text = pointer_path.read_text(encoding="utf-8")
        self.assertIn("skills/third-party-skill/SKILL.md", pointer_text)
        self.assertIn(
            ".claude/skills/third-party-skill/SKILL.md",
            pointer_text,
        )
        self.assertIn(
            ".agent-config/repo/skills/third-party-skill/SKILL.md",
            pointer_text,
        )
        # Path order check: skills/ before .claude/skills/ before .agent-config/repo/skills/
        local_idx = pointer_text.index("skills/third-party-skill/SKILL.md")
        claude_idx = pointer_text.index(".claude/skills/third-party-skill/SKILL.md")
        bootstrap_idx = pointer_text.index(
            ".agent-config/repo/skills/third-party-skill/SKILL.md"
        )
        self.assertLess(
            local_idx, claude_idx,
            "Project-local skills/ must precede .claude/skills/ in lookup order",
        )
        self.assertLess(
            claude_idx, bootstrap_idx,
            ".claude/skills/ must precede .agent-config/repo/skills/ in lookup order",
        )
        # pack-lock records the auto-emitted pointer as role=generated-command.
        files = ctx.pack_lock["packs"]["third-party"]["files"]
        gc_entries = [f for f in files if f["role"] == "generated-command"]
        self.assertEqual(len(gc_entries), 1)
        self.assertEqual(
            gc_entries[0]["output_paths"], [".claude/commands/third-party-skill.md"]
        )
        self.assertEqual(
            gc_entries[0]["generated_from"], "active-skill:third-party-skill"
        )

    def test_backslash_explicit_pointer_suppresses_auto_emit(self) -> None:
        """Regression for Round 3 Codex: a manifest that writes the
        explicit pointer mapping with backslashes (``to: .claude\\commands\\foo.md``)
        must suppress auto-emit so pack-lock doesn't get duplicate entries
        for the same semantic output path."""
        skill_dir = self.pack_source_dir / "skills" / "bs-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(b"# bs\n")
        pointer_src = (
            self.pack_source_dir / ".claude" / "commands" / "bs-skill.md"
        )
        pointer_src.parent.mkdir(parents=True)
        pointer_src.write_bytes(b"Custom BS content\n")

        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="bs-pack",
                pack_source_dir=self.pack_source_dir,
                project_root=self.project_root,
                user_home=self.user_home,
                txn=txn,
            )
            skill.handle_skill(
                {
                    "kind": "skill",
                    "hosts": ["claude-code"],
                    "files": [
                        {
                            "from": "skills/bs-skill/",
                            "to": ".claude/skills/bs-skill/",
                        },
                        {
                            "from": ".claude/commands/bs-skill.md",
                            # Manifest uses backslashes (accepted by the
                            # schema; common on Windows-authored packs).
                            "to": ".claude\\commands\\bs-skill.md",
                        },
                    ],
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        # No generated-command record should exist — the backslash
        # variant should have suppressed auto-emit via _match_key
        # normalization.
        files = ctx.pack_lock["packs"]["bs-pack"]["files"]
        gc_entries = [f for f in files if f["role"] == "generated-command"]
        self.assertEqual(
            gc_entries, [],
            "Backslash explicit pointer mapping should suppress auto-emit",
        )

    @unittest.skipUnless(
        skill._IS_WINDOWS, "case-insensitive path match is Windows-only"
    )
    def test_case_variant_explicit_pointer_suppresses_auto_emit_windows(self) -> None:
        """Regression for Round 3 Codex: on Windows, `.CLAUDE/COMMANDS/foo.md`
        and `.claude/commands/foo.md` resolve to the same on-disk path.
        The suppressor must honor this so pack-lock doesn't get
        duplicate entries."""
        skill_dir = self.pack_source_dir / "skills" / "case-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(b"# case\n")
        pointer_src = (
            self.pack_source_dir / ".claude" / "commands" / "case-skill.md"
        )
        pointer_src.parent.mkdir(parents=True)
        pointer_src.write_bytes(b"Case variant content\n")

        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="case-pack",
                pack_source_dir=self.pack_source_dir,
                project_root=self.project_root,
                user_home=self.user_home,
                txn=txn,
            )
            skill.handle_skill(
                {
                    "kind": "skill",
                    "hosts": ["claude-code"],
                    "files": [
                        {
                            "from": "skills/case-skill/",
                            "to": ".claude/skills/case-skill/",
                        },
                        {
                            "from": ".claude/commands/case-skill.md",
                            # Case variant (valid on Windows).
                            "to": ".CLAUDE/COMMANDS/case-skill.md",
                        },
                    ],
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        files = ctx.pack_lock["packs"]["case-pack"]["files"]
        gc_entries = [f for f in files if f["role"] == "generated-command"]
        self.assertEqual(
            gc_entries, [],
            "Case-variant explicit pointer mapping should suppress "
            "auto-emit on Windows",
        )

    def test_explicit_pointer_mapping_suppresses_auto_emit(self) -> None:
        """When the manifest lists an explicit pointer file mapping for the
        same skill, auto-emit is skipped so custom pointer content is
        preserved (aa-shipped skills take this path)."""
        skill_dir = self.pack_source_dir / "skills" / "aa-shipped"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_bytes(b"# aa-shipped\n")
        pointer_src = (
            self.pack_source_dir / ".claude" / "commands" / "aa-shipped.md"
        )
        pointer_src.parent.mkdir(parents=True)
        pointer_src.write_bytes(
            b"Custom pointer with skill-specific instructions.\n"
        )

        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="aa-shipped",
                pack_source_dir=self.pack_source_dir,
                project_root=self.project_root,
                user_home=self.user_home,
                txn=txn,
            )
            skill.handle_skill(
                {
                    "kind": "skill",
                    "hosts": ["claude-code"],
                    "files": [
                        {
                            "from": "skills/aa-shipped/",
                            "to": ".claude/skills/aa-shipped/",
                        },
                        {
                            "from": ".claude/commands/aa-shipped.md",
                            "to": ".claude/commands/aa-shipped.md",
                        },
                    ],
                },
                ctx,
            )
            ctx.finalize_pack_lock()

        pointer_path = (
            self.project_root / ".claude" / "commands" / "aa-shipped.md"
        )
        # Explicit pointer content preserved; NOT the canonical template.
        self.assertEqual(
            pointer_path.read_bytes(),
            b"Custom pointer with skill-specific instructions.\n",
        )
        # pack-lock should NOT have a generated-command record (explicit
        # mapping wins; the pointer is an active-skill entry from the
        # explicit mapping).
        files = ctx.pack_lock["packs"]["aa-shipped"]["files"]
        gc_entries = [f for f in files if f["role"] == "generated-command"]
        self.assertEqual(gc_entries, [])

    def test_pack_level_hosts_inherited_at_dispatch(self) -> None:
        """Regression for Round 1 Codex #4: entry omits hosts:, pack-
        level default says [codex], current host is claude-code →
        host-mismatch must fire with required:true (default)."""
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = dispatch.DispatchContext(
                pack_name="wrong-host-pack",
                pack_source_url="bundled:aa",
                pack_requested_ref="bundled",
                pack_resolved_commit="bundled",
                pack_update_policy="locked",
                pack_source_dir=self.pack_source_dir,
                project_root=self.project_root,
                user_home=self.user_home,
                repo_id="r",
                txn=txn,
                pack_lock=state_mod.empty_pack_lock(),
                project_state=state_mod.empty_project_state(),
                user_state=state_mod.empty_user_state(),
                pack_hosts=["codex"],  # pack-level default
            )
            # Entry omits hosts: — should inherit pack-level [codex].
            entry = {
                "kind": "skill",
                # no "hosts" key
                "files": [{"from": "x", "to": "y"}],
            }
            with self.assertRaises(dispatch.DispatchError) as cm:
                dispatch.dispatch_active(entry, ctx)
            self.assertIn("host-mismatch", str(cm.exception))

    def test_missing_source_raises(self) -> None:
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = _make_ctx(
                pack_name="broken",
                pack_source_dir=self.pack_source_dir,
                project_root=self.project_root,
                user_home=self.user_home,
                txn=txn,
            )
            with self.assertRaises(FileNotFoundError):
                skill.handle_skill(
                    {
                        "kind": "skill",
                        "hosts": ["claude-code"],
                        "files": [
                            {"from": "does/not/exist/", "to": ".claude/skills/x/"}
                        ],
                    },
                    ctx,
                )


if __name__ == "__main__":
    unittest.main()
