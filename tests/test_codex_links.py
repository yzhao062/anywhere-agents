"""Tests for scripts/packs/codex_links.py and its compose and uninstall wiring.

Codex reads repository skills only from ``.agents/skills/``. The composer
links each committed codex-eligible skill there after the transaction, and
both uninstall entry points remove links whose target directory is gone.
Link creation runs on macOS and Linux only, so tests that create links skip
elsewhere; the platform notice and name checks run everywhere.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compose_packs  # noqa: E402
from packs import codex_links  # noqa: E402
from packs import dispatch  # noqa: E402
from packs import handlers  # noqa: E402,F401 — side-effect: registers handlers
from packs import locks  # noqa: E402
from packs import state as state_mod  # noqa: E402
from packs import transaction as txn_mod  # noqa: E402
from packs import uninstall as uninstall_mod  # noqa: E402

HEADER = codex_links.MANAGED_HEADER
GIT = shutil.which("git") is not None
SUPPORTED = codex_links.platform_supported()
needs_links = unittest.skipUnless(SUPPORTED and GIT, "links need macOS or Linux and git")


def _missing_git(args, **_kwargs):
    raise FileNotFoundError(2, "No such file or directory", "git")


def _git_timeout(args, **_kwargs):
    raise subprocess.TimeoutExpired(args, codex_links.GIT_TIMEOUT_SECONDS)


def _dubious_repo(args, **_kwargs):
    return subprocess.CompletedProcess(
        args, 128, b"", b"fatal: detected dubious ownership in repository\n"
    )


UNKNOWN_TRACKING_RUNNERS = (_missing_git, _git_timeout, _dubious_repo)


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


class _Repo(unittest.TestCase):
    """A temporary consumer root; git is isolated from the user's config."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "consumer"
        self.root.mkdir()
        self.skills = self.root / ".agents" / "skills"
        # Newer git starts detached auto-maintenance after a commit; if it is
        # still writing into .git when the temporary directory is removed,
        # cleanup fails with "Directory not empty" (seen on macOS py3.13 CI).
        env = patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "maintenance.auto",
                "GIT_CONFIG_VALUE_0": "false",
                "GIT_CONFIG_KEY_1": "gc.auto",
                "GIT_CONFIG_VALUE_1": "0",
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.root), *args], check=True, capture_output=True
        )

    def init_git(self) -> None:
        self.git("init", "-q")

    def commit_all(self) -> None:
        self.git("add", "-A")
        self.git(
            "-c", "user.name=test", "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture",
        )

    def claude_skill(self, name: str) -> Path:
        directory = self.root / ".claude" / "skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        return directory

    def link(self, name: str, target: str | None = None, where: Path | None = None) -> Path:
        where = self.skills if where is None else where
        where.mkdir(parents=True, exist_ok=True)
        os.symlink(target or codex_links.link_target(name), where / name)
        return where / name

    def record(self, *names: str, lines: list[str] | None = None) -> None:
        self.skills.mkdir(parents=True, exist_ok=True)
        body = lines if lines is not None else (
            [HEADER, "/.gitignore", "/.gitignore.*.tmp"] + [f"/{n}" for n in names]
        )
        (self.skills / ".gitignore").write_text("\n".join(body) + "\n", encoding="utf-8")

    def recorded(self) -> list[str] | None:
        path = self.skills / ".gitignore"
        if not path.exists():
            return None
        return [line[1:] for line in path.read_text(encoding="utf-8").splitlines()[3:]]

    def entries(self) -> list[str]:
        return sorted(os.listdir(self.skills)) if self.skills.exists() else []

    def snapshot(self) -> dict[str, tuple[str, object]]:
        result: dict[str, tuple[str, object]] = {}
        base = self.root / ".agents"
        if not os.path.lexists(base):
            return result
        for dirpath, dirnames, filenames in os.walk(base):
            for name in dirnames + filenames:
                path = Path(dirpath) / name
                rel = path.relative_to(self.root).as_posix()
                if path.is_symlink():
                    result[rel] = ("link", os.readlink(path))
                elif path.is_file():
                    result[rel] = ("file", path.read_bytes())
                else:
                    result[rel] = ("dir", None)
        return result

    def sync(self, *names: str, **kwargs) -> codex_links.LinkReport:
        return codex_links.sync_links(self.root, names, **kwargs)

    def reasons(self, report: codex_links.LinkReport) -> dict[str, str]:
        return dict(report.left_alone)


@needs_links
class SyncCreatesAndKeepsLinks(_Repo):
    def test_creates_relative_link_and_managed_record(self) -> None:
        self.init_git()
        self.claude_skill("foo")
        report = self.sync("foo")
        self.assertEqual(os.readlink(self.skills / "foo"), "../../.claude/skills/foo")
        self.assertEqual((self.skills / "foo" / "SKILL.md").read_text(encoding="utf-8"), "# foo\n")
        self.assertEqual(self.recorded(), ["foo"])
        self.assertEqual(report.created, ["foo"])
        self.assertEqual(report.summary_line(), "Codex skill links: 1 linked, 1 new.")
        status = subprocess.run(
            ["git", "-C", str(self.root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn(".agents", status)

    def test_steady_state_writes_nothing(self) -> None:
        self.claude_skill("foo")
        self.sync("foo")
        record = self.skills / ".gitignore"
        os.utime(record, (1_000_000_000, 1_000_000_000))
        report = self.sync("foo")
        self.assertEqual(record.stat().st_mtime, 1_000_000_000)
        self.assertEqual(report.created, [])
        self.assertEqual(report.linked, ["foo"])
        self.assertEqual(report.summary_line(), "Codex skill links: 1 linked.")

    def test_non_git_directory_gets_links(self) -> None:
        self.claude_skill("foo")
        self.sync("foo")
        self.assertTrue((self.skills / "foo").is_symlink())

    def test_listed_exact_link_stays_owned(self) -> None:
        self.claude_skill("foo")
        self.record("foo")
        self.link("foo")
        report = self.sync("foo")
        self.assertEqual(report.linked, ["foo"])
        self.assertEqual(self.recorded(), ["foo"])


@needs_links
class SyncLeavesForeignEntriesAlone(_Repo):
    def test_tracked_exact_link_is_never_recorded_or_removed(self) -> None:
        self.init_git()
        self.link("foo")
        self.commit_all()
        report = self.sync("foo")
        self.assertEqual(self.reasons(report)["foo"], "tracked")
        self.assertIsNone(self.recorded())
        self.sync()
        self.assertTrue((self.skills / "foo").is_symlink())

    def test_tracked_case_variant_blocks_the_name(self) -> None:
        self.init_git()
        self.link("Foo", target=codex_links.link_target("foo"))
        self.commit_all()
        self.claude_skill("foo")
        report = self.sync("foo")
        self.assertEqual(self.reasons(report)["foo"], "tracked")
        self.assertEqual(self.entries(), ["Foo"])

    def test_tracked_parent_in_another_case_blocks_adoption(self) -> None:
        self.init_git()
        other = self.root / ".Agents" / "Skills"
        self.link("foo", where=other)
        (self.root / ".agentsX").mkdir()
        (self.root / ".agentsX" / "y").write_text("y\n", encoding="utf-8")
        self.commit_all()
        # Respell the parents on disk only; the index keeps .Agents/Skills.
        os.rename(self.root / ".Agents", self.root / ".agents-tmp")
        os.rename(self.root / ".agents-tmp", self.root / ".agents")
        os.rename(self.root / ".agents" / "Skills", self.root / ".agents" / "skills-tmp")
        os.rename(self.root / ".agents" / "skills-tmp", self.skills)
        self.record("foo")
        self.claude_skill("foo")
        self.claude_skill("bar")
        report = self.sync("foo", "bar")
        self.assertEqual(self.reasons(report)["foo"], "tracked")
        self.assertEqual(report.created, ["bar"])
        self.assertEqual(self.recorded(), ["bar"])
        self.sync()
        self.assertTrue((self.skills / "foo").is_symlink())

    def test_untracked_case_variant_is_a_collision(self) -> None:
        (self.skills / "Foo").mkdir(parents=True)
        (self.skills / "Foo" / "SKILL.md").write_text("desktop copy\n", encoding="utf-8")
        self.claude_skill("foo")
        report = self.sync("foo")
        self.assertEqual(self.reasons(report)["foo"], "case collision with Foo")
        self.assertEqual(self.entries(), ["Foo"])

    def test_tracked_entry_deleted_from_worktree_gets_no_link(self) -> None:
        self.init_git()
        (self.skills / "foo").mkdir(parents=True)
        (self.skills / "foo" / "SKILL.md").write_text("tracked\n", encoding="utf-8")
        self.commit_all()
        shutil.rmtree(self.skills / "foo")
        self.claude_skill("foo")
        report = self.sync("foo")
        self.assertEqual(self.reasons(report)["foo"], "tracked")
        self.assertFalse(os.path.lexists(self.skills / "foo"))

    def test_occupied_name_reports_the_manual_step(self) -> None:
        (self.skills / "foo").mkdir(parents=True)
        self.claude_skill("foo")
        report = self.sync("foo")
        self.assertEqual(self.reasons(report)["foo"], "occupied")
        self.assertIn(codex_links.OCCUPIED_STEP, report.summary_line())
        self.assertTrue((self.skills / "foo").is_dir())

    def test_unlisted_exact_link_is_never_owned(self) -> None:
        self.claude_skill("foo")
        self.link("foo")
        report = self.sync("foo")
        self.assertEqual(self.reasons(report)["foo"], "working link, not owned")
        self.assertIsNone(self.recorded())
        self.sync()
        self.assertTrue((self.skills / "foo").is_symlink())

    def test_names_differing_only_by_case_are_not_linked(self) -> None:
        report = self.sync("Foo", "foo")
        self.assertEqual(
            self.reasons(report),
            {"Foo": "differs only by case from another skill",
             "foo": "differs only by case from another skill"},
        )
        self.assertFalse(os.path.lexists(self.root / ".agents"))

    def test_unsupported_names_are_reported(self) -> None:
        report = self.sync(".hidden", "a/b")
        self.assertEqual(set(self.reasons(report).values()), {"unsupported name"})
        self.assertFalse(os.path.lexists(self.root / ".agents"))


@needs_links
class SyncFailsClosed(_Repo):
    def test_unknown_tracking_changes_nothing_then_recovers(self) -> None:
        for runner in UNKNOWN_TRACKING_RUNNERS:
            with self.subTest(runner=runner.__name__):
                shutil.rmtree(self.root)
                self.root.mkdir()
                self.claude_skill("foo")
                report = self.sync("foo", run=runner)
                self.assertEqual(report.skipped, "git tracking could not be read")
                self.assertEqual(report.deferred, ["foo"])
                self.assertFalse(os.path.lexists(self.root / ".agents"))

                self.claude_skill("bar")
                self.record("bar")
                self.link("bar")
                (self.skills / ".gitignore.abc123.tmp").write_text("stale\n", encoding="utf-8")
                before = self.snapshot()
                self.sync("foo", "bar", run=runner)
                self.assertEqual(self.snapshot(), before)

                report = self.sync("foo", "bar")
                self.assertEqual(report.created, ["foo"])
                self.assertEqual(self.recorded(), ["bar", "foo"])
                self.assertEqual(self.entries(), [".gitignore", "bar", "foo"])

    def test_tracked_parent_reported_by_git_stops_link_work(self) -> None:
        def runner(args, **_kwargs):
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args, 0, b"true\n", b"")
            return subprocess.CompletedProcess(args, 0, b".agents/skills\0", b"")

        self.claude_skill("foo")
        report = self.sync("foo", run=runner)
        self.assertEqual(report.skipped, ".agents or .agents/skills is tracked by git")
        self.assertFalse(os.path.lexists(self.root / ".agents"))

    def test_symlinked_parents_and_record_write_nothing(self) -> None:
        outside = Path(self.tmp.name).resolve() / "outside"
        outside.mkdir()
        self.claude_skill("foo")
        cases = {
            ".agents": lambda: os.symlink(outside, self.root / ".agents"),
            ".agents/skills": lambda: (
                (self.root / ".agents").mkdir(),
                os.symlink(outside, self.skills),
            ),
            ".agents/skills/.gitignore": lambda: (
                self.skills.mkdir(parents=True),
                (outside / "target").write_text("keep\n", encoding="utf-8"),
                os.symlink(outside / "target", self.skills / ".gitignore"),
            ),
        }
        for label, arrange in cases.items():
            with self.subTest(case=label):
                agents = self.root / ".agents"
                if agents.is_symlink():
                    agents.unlink()
                elif agents.exists():
                    for child in sorted(agents.rglob("*"), reverse=True):
                        child.unlink() if (child.is_symlink() or child.is_file()) else child.rmdir()
                    agents.rmdir()
                for child in outside.iterdir():
                    child.unlink()
                arrange()
                before = sorted(p.name for p in outside.iterdir())
                report = self.sync("foo")
                self.assertIsNotNone(report.skipped)
                self.assertEqual(sorted(p.name for p in outside.iterdir()), before)
                if (outside / "target").exists():
                    self.assertEqual((outside / "target").read_text(encoding="utf-8"), "keep\n")

    def test_agents_file_stops_link_work_and_uninstall_check(self) -> None:
        (self.root / ".agents").write_text("not a directory\n", encoding="utf-8")
        self.claude_skill("foo")
        report = self.sync("foo")
        self.assertEqual(report.skipped, ".agents is not a real directory")
        self.assertFalse(codex_links.has_record(self.root))
        self.assertEqual(
            (self.root / ".agents").read_text(encoding="utf-8"), "not a directory\n"
        )

    def test_recorded_name_with_only_a_case_variant_is_dropped_quietly(self) -> None:
        self.claude_skill("foo")
        self.record("Foo")
        if _case_insensitive(self.skills):
            self.link("foo")
            report = self.sync("foo")
            self.assertNotIn("Foo", self.reasons(report))
            self.assertEqual(self.reasons(report)["foo"], "working link, not owned")
            self.assertEqual(self.recorded(), [])

    def test_parent_spelled_in_another_case_stops_link_work(self) -> None:
        (self.root / ".Agents").mkdir()
        self.claude_skill("foo")
        report = self.sync("foo")
        self.assertEqual(report.skipped, ".Agents differs from .agents only by case")
        self.assertEqual(sorted(os.listdir(self.root)), [".Agents", ".claude"])

    def test_foreign_tracked_or_malformed_record_stops_link_work(self) -> None:
        base = [HEADER, "/.gitignore", "/.gitignore.*.tmp"]
        cases = {
            "foreign": ["# my own rules", "/foo"],
            "duplicate": base + ["/bar", "/bar"],
            "invalid name": base + ["/.hidden"],
            "unknown line": base + ["*.log"],
            "missing temp rule": [HEADER, "/.gitignore"],
        }
        self.claude_skill("foo")
        for label, lines in cases.items():
            with self.subTest(case=label):
                self.record(lines=lines)
                report = self.sync("foo")
                self.assertIsNotNone(report.skipped)
                self.assertFalse(os.path.lexists(self.skills / "foo"))
        self.init_git()
        self.record()
        # The managed file ignores itself, so tracking it takes a forced add.
        self.git("add", "-f", ".agents/skills/.gitignore")
        self.commit_all()
        report = self.sync("foo")
        self.assertEqual(report.skipped, ".agents/skills/.gitignore is tracked by git")


@needs_links
class SyncWriteOrder(_Repo):
    def test_failed_link_creation_is_not_recorded(self) -> None:
        self.claude_skill("foo")
        self.claude_skill("bar")
        real_symlink = os.symlink

        def flaky(src, dst, *args, **kwargs):
            if Path(dst).name == "bar":
                raise PermissionError(13, "Permission denied")
            return real_symlink(src, dst, *args, **kwargs)

        with patch.object(codex_links.os, "symlink", side_effect=flaky):
            report = self.sync("foo", "bar")
        self.assertEqual(report.created, ["foo"])
        self.assertEqual(self.reasons(report)["bar"], "could not link: Permission denied")
        self.assertEqual(self.recorded(), ["foo"])

    def test_unwritable_record_stops_a_removal_only_run(self) -> None:
        self.record("old")
        self.link("old")
        with patch.object(
            codex_links, "_write_record", side_effect=PermissionError(13, "Permission denied")
        ):
            report = self.sync()
        self.assertIn("could not write .agents/skills/.gitignore", report.skipped)
        self.assertEqual(report.deferred, ["old"])
        self.assertTrue((self.skills / "old").is_symlink())
        self.assertEqual(self.recorded(), ["old"])

    def test_crash_between_the_two_writes_is_reconciled(self) -> None:
        self.claude_skill("foo")
        with patch.object(codex_links.os, "symlink", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.sync("foo")
        self.assertEqual(self.recorded(), ["foo"])
        self.assertFalse(os.path.lexists(self.skills / "foo"))
        report = self.sync("foo")
        self.assertEqual(report.created, ["foo"])
        self.assertEqual(self.recorded(), ["foo"])

    def test_stale_temp_file_is_removed_after_the_checks(self) -> None:
        self.claude_skill("foo")
        self.record()
        (self.skills / ".gitignore.abc123.tmp").write_text("stale\n", encoding="utf-8")
        self.sync("foo")
        self.assertEqual(self.entries(), [".gitignore", "foo"])

    def test_case_only_rename_completes_in_one_run(self) -> None:
        self.claude_skill("foo")
        self.record("Foo")
        self.link("Foo", target=codex_links.link_target("Foo"))
        report = self.sync("foo")
        self.assertEqual(report.removed, ["Foo"])
        self.assertEqual(report.created, ["foo"])
        self.assertEqual(self.entries(), [".gitignore", "foo"])
        self.assertEqual(os.readlink(self.skills / "foo"), codex_links.link_target("foo"))
        self.assertEqual(self.recorded(), ["foo"])

    def test_de_management_removes_only_exact_recorded_links(self) -> None:
        self.record("old", "moved")
        self.link("old")
        self.link("moved", target="../../elsewhere/moved")
        report = self.sync()
        self.assertEqual(report.removed, ["old"])
        self.assertEqual(self.reasons(report)["moved"], "no longer eligible; not an exact link")
        self.assertTrue((self.skills / "moved").is_symlink())
        self.assertEqual(self.recorded(), [])


class PlatformGate(_Repo):
    def test_unsupported_platform_reports_and_creates_nothing(self) -> None:
        with patch.object(codex_links, "_current_platform", return_value="win32"):
            report = self.sync("foo")
            self.assertEqual(
                report.summary_line(),
                "Codex skill links are not created on Windows in this release.",
            )
            self.assertIsNone(self.sync().summary_line())
        with patch.object(codex_links, "_current_platform", return_value="freebsd14"):
            self.assertIn("freebsd14", self.sync("foo").summary_line())
        self.assertFalse(os.path.lexists(self.root / ".agents"))

    def test_uninstall_ignores_links_on_unsupported_platforms(self) -> None:
        with patch.object(codex_links, "_current_platform", return_value="win32"):
            self.assertFalse(codex_links.has_record(self.root))


@needs_links
class PruneLinks(_Repo):
    def test_removes_dangling_links_and_keeps_live_ones(self) -> None:
        self.record("gone", "live")
        self.link("gone")
        self.link("live")
        self.claude_skill("live")
        report = codex_links.prune_links(self.root, remove_record_when_empty=False)
        self.assertEqual(report.removed, ["gone"])
        self.assertEqual(self.entries(), [".gitignore", "live"])
        self.assertEqual(self.recorded(), ["live"])

    def test_uninstall_all_removes_record_and_empty_parents(self) -> None:
        self.record("gone")
        self.link("gone")
        codex_links.prune_links(self.root, remove_record_when_empty=True)
        self.assertFalse(os.path.lexists(self.root / ".agents"))

    def test_uninstall_all_keeps_a_non_empty_agents_directory(self) -> None:
        self.record("gone")
        self.link("gone")
        (self.root / ".agents" / "notes.md").write_text("keep\n", encoding="utf-8")
        codex_links.prune_links(self.root, remove_record_when_empty=True)
        self.assertFalse(os.path.lexists(self.skills))
        self.assertTrue((self.root / ".agents" / "notes.md").exists())

    def test_unknown_tracking_changes_nothing(self) -> None:
        self.record("gone")
        self.link("gone")
        before = self.snapshot()
        for runner in UNKNOWN_TRACKING_RUNNERS:
            report = codex_links.prune_links(
                self.root, remove_record_when_empty=True, run=runner
            )
            self.assertEqual(report.skipped, "git tracking could not be read")
            self.assertEqual(self.snapshot(), before)


class HandlerEligibility(unittest.TestCase):
    """The skill handler records a name only for entries that dispatch
    actually ran and whose effective hosts include codex."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.source = base / "src"
        self.project = base / "project"
        self.project.mkdir()
        for name in ("dual", "claude", "codex", "inherited", "nested"):
            (self.source / "skills" / name).mkdir(parents=True)
            (self.source / "skills" / name / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        self.lock_path = base / "peer.lock"
        self.lock_path.write_text("0\n", encoding="utf-8")
        self.staging = base / "stage.staging-codex"

    def _ctx(self, txn, *, host="claude-code", pack_hosts=None, eligible=None):
        return dispatch.DispatchContext(
            pack_name="p",
            pack_source_url="bundled:aa",
            pack_requested_ref="bundled",
            pack_resolved_commit="bundled",
            pack_update_policy="locked",
            pack_source_dir=self.source,
            project_root=self.project,
            user_home=self.project,
            repo_id="test",
            txn=txn,
            pack_lock=state_mod.empty_pack_lock(),
            project_state=state_mod.empty_project_state(),
            user_state=state_mod.empty_user_state(),
            current_host=host,
            pack_hosts=pack_hosts,
            codex_eligible_skills=eligible,
        )

    @staticmethod
    def _entry(name, hosts=None, required=None, to=None):
        entry = {
            "kind": "skill",
            "files": [{"from": f"skills/{name}/", "to": to or f".claude/skills/{name}/"}],
        }
        if hosts is not None:
            entry["hosts"] = hosts
        if required is not None:
            entry["required"] = required
        return entry

    def test_only_dispatched_dual_host_directories_are_eligible(self) -> None:
        eligible: set[str] = set()
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = self._ctx(txn, pack_hosts=["claude-code", "codex"], eligible=eligible)
            for entry in (
                self._entry("dual", hosts=["claude-code", "codex"]),
                self._entry("claude", hosts=["claude-code"]),
                self._entry("codex", hosts=["codex"], required=False),
                self._entry("inherited"),
                self._entry("nested", hosts=["claude-code", "codex"],
                            to=".claude/skills/nested/sub/"),
            ):
                dispatch.dispatch_active(entry, ctx)
        self.assertEqual(eligible, {"dual", "inherited"})

    def test_required_claude_only_entry_aborts_under_codex(self) -> None:
        eligible: set[str] = set()
        with self.assertRaises(dispatch.DispatchError):
            with txn_mod.Transaction(self.staging, self.lock_path) as txn:
                ctx = self._ctx(txn, host="codex", eligible=eligible)
                dispatch.dispatch_active(self._entry("claude", hosts=["claude-code"]), ctx)
        self.assertEqual(eligible, set())

    def test_context_without_a_set_collects_nothing(self) -> None:
        with txn_mod.Transaction(self.staging, self.lock_path) as txn:
            ctx = self._ctx(txn, eligible=None)
            dispatch.dispatch_active(self._entry("dual", hosts=["claude-code", "codex"]), ctx)
        self.assertIsNone(ctx.codex_eligible_skills)


def _skill_pack(name: str, skills: list[str], hosts: list[str], required: bool | None = None) -> dict:
    entry: dict = {
        "kind": "skill",
        "files": [{"from": f"skills/{s}/", "to": f".claude/skills/{s}/"} for s in skills],
    }
    if required is not None:
        entry["required"] = required
    return {"name": name, "source": "bundled:aa", "hosts": hosts, "active": [entry]}


class _Consumer(_Repo):
    """Runs the real v2 composer against bundled packs under the repo root."""

    def setUp(self) -> None:
        super().setUp()
        self.home = Path(self.tmp.name).resolve() / "home"
        self.home.mkdir()
        (self.root / ".agent-config").mkdir()
        (self.root / ".agent-config" / "AGENTS.md").write_text("# upstream\n", encoding="utf-8")
        for name in ("alpha", "beta", "gamma", "shared"):
            directory = self.root / ".agent-config" / "repo" / "skills" / name
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")

    def compose(self, packs: list[dict], *, host: str = "claude-code") -> tuple[int, str, str]:
        by_name = {pack["name"]: pack for pack in packs}
        selections = [{"name": pack["name"], "ref": "bundled"} for pack in packs]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.object(
            compose_packs.config_mod, "resolved_for_project", return_value=selections,
        ), patch.object(
            compose_packs, "_process_selection",
            side_effect=lambda selection, **_kw: (by_name[selection["name"]], None),
        ), patch.object(compose_packs.Path, "home", return_value=self.home):
            rc = compose_packs._do_compose_v2(
                self.root, {"version": 2, "packs": []}, no_cache=True, host=host
            )
        return rc, out.getvalue(), err.getvalue()


@needs_links
class ComposeWiring(_Consumer):
    def test_shared_set_links_dual_host_skills_across_packs(self) -> None:
        rc, out, err = self.compose([
            _skill_pack("pack-a", ["alpha"], ["claude-code", "codex"]),
            _skill_pack("pack-b", ["beta"], ["claude-code", "codex"]),
            _skill_pack("pack-c", ["gamma"], ["claude-code"]),
        ])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entries(), [".gitignore", "alpha", "beta"])
        self.assertIn("Codex skill links: 2 linked, 2 new.", out)
        self.assertEqual(
            (self.skills / "alpha" / "SKILL.md").read_text(encoding="utf-8"), "# alpha\n"
        )

    def test_failed_compose_leaves_links_untouched(self) -> None:
        self.compose([_skill_pack("pack-a", ["alpha"], ["claude-code", "codex"])])
        before = self.snapshot()
        rc, _out, err = self.compose(
            [
                _skill_pack("pack-a", ["alpha"], ["claude-code", "codex"]),
                _skill_pack("pack-c", ["gamma"], ["claude-code"], required=True),
            ],
            host="codex",
        )
        self.assertEqual(rc, 1)
        self.assertIn("host-mismatch", err)
        self.assertEqual(self.snapshot(), before)

    def test_unsupported_platform_prints_the_notice(self) -> None:
        with patch.object(codex_links, "_current_platform", return_value="win32"):
            rc, out, err = self.compose([_skill_pack("pack-a", ["alpha"], ["claude-code", "codex"])])
        self.assertEqual(rc, 0, err)
        self.assertIn("Codex skill links are not created on Windows in this release.", out)
        self.assertFalse(os.path.lexists(self.root / ".agents"))

    def test_opt_out_compose_keeps_outputs_and_links(self) -> None:
        self.compose([_skill_pack("pack-a", ["alpha"], ["claude-code", "codex"])])
        rc, _out, err = self.compose([])
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.root / ".claude" / "skills" / "alpha").is_dir())
        self.assertTrue((self.skills / "alpha").is_symlink())

    def test_removed_pack_is_de_managed_on_the_next_compose(self) -> None:
        self.compose([
            _skill_pack("pack-a", ["alpha"], ["claude-code", "codex"]),
            _skill_pack("pack-b", ["beta"], ["claude-code", "codex"]),
        ])
        rc, out, err = self.compose([_skill_pack("pack-a", ["alpha"], ["claude-code", "codex"])])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entries(), [".gitignore", "alpha"])
        self.assertIn("removed beta", out)


@needs_links
class UninstallWiring(_Consumer):
    def uninstall_pack(self, name: str) -> uninstall_mod.UninstallOutcome:
        return uninstall_mod.run_uninstall_pack(self.root, name, user_home=self.home)

    def uninstall_all(self) -> uninstall_mod.UninstallOutcome:
        return uninstall_mod.run_uninstall_all(self.root, user_home=self.home)

    def test_single_pack_removal_keeps_other_and_shared_links(self) -> None:
        self.compose([
            _skill_pack("pack-a", ["alpha", "shared"], ["claude-code", "codex"]),
            _skill_pack("pack-b", ["beta", "shared"], ["claude-code", "codex"]),
        ])
        outcome = self.uninstall_pack("pack-a")
        self.assertEqual(outcome.status, uninstall_mod.STATUS_CLEAN, outcome.details)
        self.assertEqual(self.entries(), [".gitignore", "beta", "shared"])
        self.assertTrue((self.root / ".claude" / "skills" / "shared").is_dir())
        self.assertIn("removed Codex skill link(s): .agents/skills/alpha", outcome.details)

    def test_drift_stop_removes_only_links_whose_directory_is_gone(self) -> None:
        self.compose([_skill_pack("pack-a", ["alpha", "beta"], ["claude-code", "codex"])])
        (self.root / ".claude" / "skills" / "beta" / "SKILL.md").write_text("edited\n", encoding="utf-8")
        outcome = self.uninstall_pack("pack-a")
        self.assertEqual(outcome.status, uninstall_mod.STATUS_DRIFT)
        self.assertFalse((self.root / ".claude" / "skills" / "alpha").exists())
        self.assertEqual(self.entries(), [".gitignore", "beta"])
        self.assertEqual(self.recorded(), ["beta"])

    def test_uninstall_all_removes_links_record_and_empty_parents(self) -> None:
        self.compose([_skill_pack("pack-a", ["alpha"], ["claude-code", "codex"])])
        target = self.root / ".claude" / "skills" / "alpha"
        outcome = self.uninstall_all()
        self.assertEqual(outcome.status, uninstall_mod.STATUS_CLEAN, outcome.details)
        self.assertFalse(os.path.lexists(self.root / ".agents"))
        self.assertFalse(target.exists())

    def test_retry_with_missing_state_removes_the_dangling_link(self) -> None:
        for runner in (self.uninstall_all, lambda: self.uninstall_pack("pack-a")):
            with self.subTest(entry=getattr(runner, "__name__", "uninstall_pack")):
                self.record("gone", "live")
                for name in ("gone", "live"):
                    if not os.path.lexists(self.skills / name):
                        self.link(name)
                self.claude_skill("live")
                for state_file in ("pack-lock.json", "pack-state.json"):
                    path = self.root / ".agent-config" / state_file
                    if path.exists():
                        path.unlink()
                outcome = runner()
                self.assertEqual(outcome.status, uninstall_mod.STATUS_CLEAN, outcome.details)
                self.assertEqual(self.entries(), [".gitignore", "live"])
                self.assertEqual(self.recorded(), ["live"])

    def test_empty_record_cleanup_reports_clean(self) -> None:
        self.record()
        outcome = self.uninstall_all()
        self.assertEqual(outcome.status, uninstall_mod.STATUS_CLEAN, outcome.details)
        self.assertIn("removed .agents/skills/.gitignore", outcome.details)
        self.assertFalse(os.path.lexists(self.root / ".agents"))

    def test_unwritable_record_keeps_dangling_links_and_reports_partial(self) -> None:
        self.record("gone")
        self.link("gone")
        with patch.object(
            codex_links, "_write_record", side_effect=PermissionError(13, "Permission denied")
        ):
            outcome = self.uninstall_pack("pack-a")
        self.assertEqual(outcome.status, uninstall_mod.STATUS_PARTIAL, outcome.details)
        self.assertTrue((self.skills / "gone").is_symlink())
        self.assertEqual(self.recorded(), ["gone"])

    def test_failed_final_rewrite_reports_partial(self) -> None:
        self.record("gone")
        self.link("gone")
        real_write = codex_links._write_record
        calls = []

        def first_write_only(skills, names, *, force=False):
            calls.append(force)
            if len(calls) > 1:
                raise PermissionError(13, "Permission denied")
            return real_write(skills, names, force=force)

        with patch.object(codex_links, "_write_record", side_effect=first_write_only):
            outcome = self.uninstall_pack("pack-a")
        self.assertEqual(calls, [True, False])
        self.assertEqual(outcome.status, uninstall_mod.STATUS_PARTIAL, outcome.details)
        self.assertFalse(os.path.lexists(self.skills / "gone"))

    def test_foreign_ignore_file_is_not_a_record(self) -> None:
        self.record(lines=["# Interim anywhere-agents fix (2026-09-24): not committed.", "/.gitignore", "/gone"])
        self.link("gone")
        before = self.snapshot()
        self.assertFalse(codex_links.has_record(self.root))
        outcome = self.uninstall_all()
        self.assertEqual(outcome.status, uninstall_mod.STATUS_NO_OP, outcome.details)
        self.assertEqual(self.snapshot(), before)

    def test_lock_timeout_removes_nothing(self) -> None:
        self.record("gone")
        self.link("gone")
        before = self.snapshot()
        timeout = locks.LockTimeout(self.home / "lock", 0.1, None)
        with patch.object(uninstall_mod.locks, "acquire", side_effect=timeout):
            outcome = self.uninstall_all()
        self.assertEqual(outcome.status, uninstall_mod.STATUS_LOCK_TIMEOUT)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
