"""Tests for skills/implement-review/scripts/style-audit.py.

The audit is advisory by construction, and "by construction" is a claim a
test has to hold up. Most of what follows checks that the script cannot fail,
because a step that can fail is a step that can hold the review loop open.
"""
from __future__ import annotations

import contextlib
import io
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows

# Loading the audit through SourceFileLoader caches bytecode beside it, and
# a __pycache__ inside a shipped skill directory shows up as parity drift
# between the two repos. Nothing here needs the cache.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / "skills" / "implement-review" / "scripts" / "style-audit.py"
GUARD = ROOT / "scripts" / "guard.py"

_DIRTY = (
    "This result was pivotal for the team.\n\n"
    "A sentence that goes on and on and on and on and on and on and on and "
    "on and on and on and on and on and on and on and on and on and on for "
    "far too long indeed truly.\n"
)


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_audit(args, env=None):
    """Run the script as the review loop would, and return (rc, stdout)."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    proc = subprocess.run(
        [sys.executable, str(AUDIT)] + args,
        capture_output=True, text=True, timeout=180, env=merged,
    )
    return proc.returncode, proc.stdout


class AlwaysExitsZeroTests(unittest.TestCase):
    """The contract, stated four ways.

    Each case is one way the audit could plausibly fail a pipeline step: it
    found something, the package is gone, the file is unreadable, nothing was
    staged. A non-zero exit in any of them is the block the design rules out.
    """

    def test_findings_still_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.md"
            probe.write_text(_DIRTY, encoding="utf-8")
            rc, out = run_audit(["--repo", tmp, str(probe)])
        self.assertEqual(rc, 0, "findings must not fail the step")
        self.assertIn("Style status: FINDINGS:", out)

    def test_missing_agent_style_still_exits_zero(self) -> None:
        """A consumer without the package gets a reported skip, not a failure.

        Shadowing the package on PYTHONPATH is what makes this real. Asserting
        the absent-import branch by reading the source would agree with a
        version that had stopped catching the exception.
        """
        with tempfile.TemporaryDirectory() as tmp:
            shadow = Path(tmp) / "shadow"
            shadow.mkdir()
            (shadow / "agent_style.py").write_text(
                "raise ImportError('shadowed for test')\n", encoding="utf-8")
            probe = Path(tmp) / "probe.md"
            probe.write_text(_DIRTY, encoding="utf-8")
            rc, out = run_audit(
                ["--repo", tmp, str(probe)],
                env={"PYTHONPATH": str(shadow)},
            )
        self.assertEqual(rc, 0)
        self.assertIn("Style status: SKIPPED:", out)
        self.assertIn("agent_style", out)

    def test_a_missing_file_still_exits_zero(self) -> None:
        """Named for what it does. The permission-failure case it used to
        claim is covered by FileLevelProblemTests, which patches the detector
        rather than relying on a chmod fixture that Windows ignores."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.md"
            rc, out = run_audit(["--repo", tmp, str(missing)])
        self.assertEqual(rc, 0)
        self.assertIn("Style status:", out)

    def test_nothing_staged_still_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = run_audit(["--repo", tmp])
        self.assertEqual(rc, 0)
        self.assertIn("Style status: SKIPPED:", out)


class ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_module(AUDIT, "_style_audit")

    def test_carried_text_is_out_of_scope(self) -> None:
        self.assertFalse(self.mod.is_prose_target("/tmp/x/agent-io/round1.md"))
        self.assertFalse(self.mod.is_prose_target("/tmp/x/agent-io/prompt.txt"))

    def test_the_sibling_is_still_in_scope(self) -> None:
        self.assertTrue(self.mod.is_prose_target("/tmp/x/round1.md"))

    def test_code_is_out_of_scope(self) -> None:
        self.assertFalse(self.mod.is_prose_target("/tmp/x/main.py"))

    def test_fallback_constants_match_guard(self) -> None:
        """The script carries its own copy for the pack-deployed case.

        It prefers guard.py when it can find one, so the copy is reached only
        where the repo is not. Drift there would be invisible at runtime, and
        this is the only place it can be caught.
        """
        guard = load_module(GUARD, "_guard_for_audit")
        self.assertEqual(
            set(self.mod._FALLBACK_PROSE_EXTENSIONS), set(guard.PROSE_EXTENSIONS))
        self.assertEqual(
            self.mod._FALLBACK_AGENT_IO_SEGMENT, guard.AGENT_IO_SEGMENT)


def git(repo, *args):
    """Run git in repo, raising on failure so a broken fixture is loud."""
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, check=True)


def make_repo(tmp):
    """An initialised repo with identity set, so commits and diffs work."""
    subprocess.run(["git", "init", "-q", str(tmp)], check=True,
                   capture_output=True)
    git(tmp, "config", "user.email", "probe@example.invalid")
    git(tmp, "config", "user.name", "probe")
    return Path(tmp)


def stage(repo, name, content):
    path = Path(repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", name)
    return path


def run_main(mod, argv):
    """Call main() in-process and return (rc, stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(argv)
    return rc, buf.getvalue()


class StagedContentTests(unittest.TestCase):
    """The audit must read what is staged, not what is on disk.

    A staged file with further unstaged edits holds different bytes in each
    place. Auditing the working tree while filtering by staged line numbers
    reports on neither, so a staged violation can print CLEAN.
    """

    def setUp(self) -> None:
        self.mod = load_module(AUDIT, "_style_audit_staged")
        self._require_agent_style()

    def _require_agent_style(self) -> None:
        try:
            import agent_style.review  # noqa: F401
        except Exception as exc:
            if os.environ.get("CI"):
                self.fail(f"agent_style must be importable in CI: {exc!r}")
            self.skipTest("agent_style not importable in this environment")

    def test_a_staged_violation_survives_a_clean_working_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            stage(repo, "notes.md", "This result was pivotal.\n")
            # The working tree is then cleaned without restaging, which is
            # exactly the state that used to report CLEAN.
            (repo / "notes.md").write_text("Short and clean.\n",
                                           encoding="utf-8")
            rc, out = run_main(self.mod, ["--repo", str(repo), "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["audited_from"], "index")
        rules = {f["rule"] for f in payload["findings"]}
        self.assertIn("RULE-06", rules,
                      "the staged banned word must still be reported")

    def test_an_unstaged_violation_is_not_attributed_to_the_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            stage(repo, "notes.md", "Short and clean.\n")
            (repo / "notes.md").write_text("This result was pivotal.\n",
                                           encoding="utf-8")
            rc, out = run_main(self.mod, ["--repo", str(repo), "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(
            [f for f in payload["findings"] if f["rule"] == "RULE-06"], [],
            "an edit that is not staged is not part of this change",
        )


_FAKE_GUARD = (
    "class _V:\n"
    "    def __init__(self, detail):\n"
    "        self.line = 1\n"
    "        self.column = 1\n"
    "        self.detail = detail\n"
    "        self.excerpt = ''\n"
    "\n"
    "def _echo(text, file_path=''):\n"
    "    return [_V('path=' + file_path)]\n"
    "\n"
    "def _boom(text, file_path=''):\n"
    "    raise RuntimeError('simulated')\n"
    "\n"
    "_PATH_AWARE_RULES = frozenset({{'RULE-03', 'RULE-A'}})\n"
    "\n"
    "def _agent_style_mechanical():\n"
    "    return [({label!r}, {fn})]\n"
)


class HookOnlyDetectorTests(unittest.TestCase):
    """Detectors only hooks run reach the audit through the guard's loader.

    `audit()` runs the CLI's fixed bench scope, which leaves out RULE-03 and,
    from agent-style 0.7.0, RULE-A. Before this, the pre-review audit never
    reported an interval written into prose, the finding RULE-03 exists for.
    """

    def setUp(self) -> None:
        self.mod = load_module(AUDIT, "_style_audit_hook")
        try:
            import agent_style.review  # noqa: F401
        except Exception as exc:
            if os.environ.get("CI"):
                self.fail(f"agent_style must be importable in CI: {exc!r}")
            self.skipTest("agent_style not importable in this environment")

    def _guard(self, tmp, label="RULE-03", fn="_echo"):
        path = Path(tmp) / "fake_guard.py"
        path.write_text(_FAKE_GUARD.format(label=label, fn=fn), encoding="utf-8")
        return self.mod._load_hook_detectors([str(path)])

    def test_a_hook_only_rule_runs_with_the_repository_path(self) -> None:
        """The detector gets the repository path, not the temp blob's name.

        The real detectors read the file name to recognise an abstract, an
        introduction, or an appendix. The staged blob is materialised under a
        hashed name, so passing the blob path would hide all three.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            stage(repo, "paper/results.tex", "Short and clean.\n")
            findings, coverage, error = self.mod.audit_files(
                ["paper/results.tex"], str(repo), from_index=True,
                hook_rules=self._guard(tmp))
        self.assertIsNone(error)
        self.assertIn(
            ("RULE-03", "path=paper/results.tex"),
            {(f["rule"], f["detail"]) for f in findings},
        )
        self.assertIn("RULE-03", coverage["partial"])

    def test_a_rule_the_cli_audit_already_ran_is_not_run_twice(self) -> None:
        """RULE-12 is in the CLI scope, so the guard's copy must not run.

        The guard's RULE-12 here raises; running it would surface as an AUDIT
        problem, so its absence shows it was skipped.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            stage(repo, "notes.md", "Short and clean.\n")
            findings, _, _ = self.mod.audit_files(
                ["notes.md"], str(repo), from_index=True,
                hook_rules=self._guard(tmp, label="RULE-12", fn="_boom"))
        self.assertEqual([f for f in findings if f["rule"] == "AUDIT"], [])

    def test_a_raising_hook_detector_is_reported_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            stage(repo, "results.tex", "Short and clean.\n")
            findings, _, error = self.mod.audit_files(
                ["results.tex"], str(repo), from_index=True,
                hook_rules=self._guard(tmp, fn="_boom"))
        self.assertIsNone(error)
        problems = [f for f in findings if f["rule"] == "AUDIT"]
        self.assertEqual(len(problems), 1)
        self.assertIn("RULE-03 detector raised RuntimeError", problems[0]["detail"])

    def test_an_old_guard_without_the_loader_is_passed_over(self) -> None:
        """A consumer's deployed hook can predate the loader; try the next one."""
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old_guard.py"
            old.write_text("PROSE_EXTENSIONS = frozenset(['.md'])\n", encoding="utf-8")
            self.assertEqual(self.mod._load_hook_detectors([str(old)]), (None, frozenset()))
            new = Path(tmp) / "fake_guard.py"
            new.write_text(_FAKE_GUARD.format(label="RULE-03", fn="_echo"), encoding="utf-8")
            rules, path_aware = self.mod._load_hook_detectors([str(old), str(new)])
        self.assertEqual([label for label, _ in rules], ["RULE-03"])
        self.assertEqual(path_aware, frozenset({"RULE-03", "RULE-A"}))

    def test_the_guard_beside_the_script_wins_over_the_deployed_hook(self) -> None:
        """A deployed hook older than the package would leave a new detector out.

        The default order tries the guard shipped beside the audit first. The
        deployed hook here offers RULE-03 only; the paired guard offers RULE-A.
        """
        with tempfile.TemporaryDirectory() as tmp:
            deployed = Path(tmp) / "deployed_guard.py"
            deployed.write_text(_FAKE_GUARD.format(label="RULE-03", fn="_echo"), encoding="utf-8")
            paired = Path(tmp) / "paired_guard.py"
            paired.write_text(_FAKE_GUARD.format(label="RULE-A", fn="_echo"), encoding="utf-8")
            with unittest.mock.patch.object(
                    self.mod, "_guard_candidates", return_value=[str(deployed), str(paired)]):
                rules, _ = self.mod._load_hook_detectors()
            self.assertEqual([label for label, _ in rules], ["RULE-A"])
            with unittest.mock.patch.object(
                    self.mod, "_guard_candidates",
                    return_value=[str(deployed), str(Path(tmp) / "absent.py")]):
                rules, _ = self.mod._load_hook_detectors()
            self.assertEqual([label for label, _ in rules], ["RULE-03"],
                             "the deployed hook is the fallback")

    def _coverage(self, tmp, hook_rules, name="results.tex"):
        repo = make_repo(Path(tmp) / "repo")
        stage(repo, name, "Short and clean.\n")
        _, coverage, error = self.mod.audit_files(
            [name], str(repo), from_index=True, hook_rules=hook_rules)
        self.assertIsNone(error)
        return coverage

    def test_rule_a_runs_beside_the_structural_detector(self) -> None:
        """RULE-A's structural check runs in `audit()`; the table check must too.

        Only a mechanical run counts as the hook rule having run, so a
        structural RULE-A result must not suppress the guard's table check.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            stage(repo, "results.tex", "Short and clean.\n")
            findings, _, _ = self.mod.audit_files(
                ["results.tex"], str(repo), from_index=True,
                hook_rules=self._guard(tmp, label="RULE-A", fn="_echo"))
        self.assertIn(("RULE-A", "path=results.tex"),
                      {(f["rule"], f["detail"]) for f in findings})

    def test_a_raising_hook_detector_is_not_reported_as_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coverage = self._coverage(tmp, self._guard(tmp, label="RULE-A", fn="_boom"))
        self.assertNotIn("RULE-A", coverage["covered"])

    def test_a_hook_detector_that_ran_is_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coverage = self._coverage(tmp, self._guard(tmp, label="RULE-A", fn="_echo"))
        self.assertIn("RULE-A", coverage["covered"])

    def test_without_a_guard_the_package_hook_detectors_are_not_covered(self) -> None:
        """No guard means no hook detector ran, and coverage has to say so.

        RULE-A's structural detector still runs through `audit()`, so without
        this the table check would hide behind a covered RULE-A.
        """
        from agent_style.review import detectors_mech

        def quiet(text, file_path=""):
            return []

        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(detectors_mech, "_rule_a", quiet, create=True):
                coverage = self._coverage(tmp, (None, frozenset()))
        self.assertNotIn("RULE-A", coverage["covered"])
        self.assertIn("RULE-A", coverage["partial"])

    def test_the_real_guard_reports_an_interval_in_staged_prose(self) -> None:
        """End to end with this repository's guard and the installed package."""
        import agent_style

        release = tuple(int(p) for p in agent_style.__version__.split(".")[:3])
        if release < (0, 6, 0):
            self.skipTest("RULE-03 ships with agent-style 0.6.0")
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo")
            stage(repo, "results.tex",
                  "Our method reaches 0.71 [0.65, 0.77] on the main split.\n")
            findings, _, _ = self.mod.audit_files(
                ["results.tex"], str(repo), from_index=True,
                hook_rules=self.mod._load_hook_detectors([str(GUARD)]))
        self.assertIn("RULE-03", {f["rule"] for f in findings})


class PackageRuleLabelTests(unittest.TestCase):
    """Coverage names a package detector by the rule it stands for.

    `_rule_12_profile` and `_rule_08_repeats` carry a descriptor after the rule
    id. Reading the whole remainder as the id gives `RULE-12_PROFILE`, which
    no rule pack uses, and filtering such names out leaves their rules
    unaccounted for in the coverage report.
    """

    def setUp(self) -> None:
        self.mod = load_module(AUDIT, "_style_audit_labels")

    def test_a_function_name_gives_the_label_of_its_rule(self) -> None:
        for name, label in (
            ("_rule_03", "RULE-03"),
            ("_rule_a", "RULE-A"),
            ("_rule_12_profile", "RULE-12"),
            ("_rule_08_repeats", "RULE-08"),
            ("_rule_03_scan", "RULE-03"),
            ("_rule_12_profile_scan", "RULE-12"),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.mod._rule_label(name), label)

    def test_a_name_that_is_not_a_rule_function_gives_no_label(self) -> None:
        for name in ("rule_12", "_rules", "_rule_", "_rule__scan", "_rule_-x",
                     "_other_12", ""):
            with self.subTest(name=name):
                self.assertIsNone(self.mod._rule_label(name))

    def test_the_package_set_counts_multi_segment_detectors_under_their_rule(self) -> None:
        try:
            from agent_style.review import detectors_mech
        except Exception as exc:
            if os.environ.get("CI"):
                self.fail(f"agent_style must be importable in CI: {exc!r}")
            self.skipTest("agent_style not importable in this environment")

        def quiet(text, file_path=""):
            return []

        with unittest.mock.patch.object(
                detectors_mech, "_rule_12_profile", quiet, create=True), \
                unittest.mock.patch.object(
                    detectors_mech, "_rule_08_repeats", quiet, create=True):
            labels = self.mod._package_hook_rules()
        self.assertLessEqual({"RULE-08", "RULE-12"}, labels)
        self.assertEqual([label for label in labels if "_" in label], [],
                         "a descriptor must never reach the label")


# One sentence over RULE-12's thirty words. The CLI's per-sentence detector
# reports it as "sentence length 34 words (>30)", which is what the profile
# substitution has to keep out of the audit.
_LONG = (
    "This is a deliberately long sentence written purely so that it will "
    "comfortably exceed the thirty word threshold that RULE-12 enforces on "
    "prose, and therefore reach the audit path under test in this case.\n"
)


class Rule12ProfileSubstitutionTests(unittest.TestCase):
    """From agent-style 0.8.0 the hook's RULE-12 is a document-level profile.

    The CLI audit still runs the per-sentence `_rule_12`. Without a
    substitution the /vet audit would list every sentence over thirty words
    beside a profile that treats most of them as ordinary. Substitution
    applies when the guard's set holds the package's own `_rule_12_profile`, and the
    coverage report has to stay true in both directions: a profile that ran
    covers RULE-12, and one that failed leaves it uncovered rather than
    resting on a CLI result that was thrown away.
    """

    def setUp(self) -> None:
        self.mod = load_module(AUDIT, "_style_audit_profile")
        try:
            import agent_style.review  # noqa: F401
        except Exception as exc:
            if os.environ.get("CI"):
                self.fail(f"agent_style must be importable in CI: {exc!r}")
            self.skipTest("agent_style not importable in this environment")

    @staticmethod
    def _profile(text, file_path=""):
        class V:
            line, column, excerpt = 1, 1, ""
            detail = "profile-path=" + str(file_path)
        return [V()]

    @staticmethod
    def _failing_profile(text, file_path=""):
        raise RuntimeError("simulated")

    def _audit(self, tmp, hook_rules, text=_LONG, name="notes.md"):
        repo = make_repo(Path(tmp) / "repo")
        stage(repo, name, text)
        return self.mod.audit_files(
            [name], str(repo), from_index=True, hook_rules=hook_rules)

    def _details(self, findings, rule="RULE-12"):
        return sorted(f["detail"] for f in findings if f["rule"] == rule)

    def test_the_profile_stands_in_for_the_cli_rule_12(self) -> None:
        from agent_style.review import detectors_mech

        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(
                detectors_mech, "_rule_12_profile", self._profile, create=True):
            findings, coverage, error = self._audit(
                tmp, ([("RULE-12", self._profile)], frozenset({"RULE-12"})))
        self.assertIsNone(error)
        self.assertEqual(
            self._details(findings), ["profile-path=notes.md"],
            "the profile reports once, with the repository path, and the "
            "per-sentence hit on the same text is not listed",
        )
        self.assertIn("RULE-12", coverage["covered"])

    def test_a_per_sentence_rule_12_under_the_same_label_keeps_the_cli_result(self) -> None:
        """Older releases run `_rule_12` in the hook under the label RULE-12.

        That function is not the package's `_rule_12_profile`. The audit
        therefore keeps the CLI's own result, and the hook's copy still does
        not run twice. Keying the swap on the label alone would silence RULE-12.
        """
        from agent_style.review import detectors_mech

        def legacy(text, file_path=""):
            raise AssertionError("the CLI already ran RULE-12; this must not run")

        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(
                detectors_mech, "_rule_12_profile", self._profile, create=True):
            findings, coverage, _ = self._audit(
                tmp, ([("RULE-12", legacy)], frozenset({"RULE-12"})))
        self.assertEqual(self._details(findings), ["sentence length 34 words (>30)"])
        self.assertEqual([f for f in findings if f["rule"] == "AUDIT"], [])
        self.assertIn("RULE-12", coverage["covered"])

    def test_a_failing_profile_leaves_rule_12_uncovered(self) -> None:
        from agent_style.review import detectors_mech

        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(
                detectors_mech, "_rule_12_profile", self._failing_profile, create=True):
            findings, coverage, _ = self._audit(
                tmp, ([("RULE-12", self._failing_profile)], frozenset({"RULE-12"})))
        self.assertEqual(self._details(findings), [],
                         "the discarded CLI hit must not come back as a finding")
        problems = [f["detail"] for f in findings if f["rule"] == "AUDIT"]
        self.assertEqual(problems, ["RULE-12 detector raised RuntimeError"])
        self.assertNotIn("RULE-12", coverage["covered"])
        self.assertNotIn("RULE-12", coverage["partial"])
        self.assertIn("RULE-12", coverage["uncovered"])

    def test_the_profile_is_recognised_only_by_function_and_label_together(self) -> None:
        from agent_style.review import detectors_mech

        with unittest.mock.patch.object(
                detectors_mech, "_rule_12_profile", self._profile, create=True):
            self.assertEqual(
                self.mod._replaced_cli_rules([("RULE-12", self._profile)]),
                frozenset({"RULE-12"}))
            self.assertEqual(
                self.mod._replaced_cli_rules([("RULE-12", self._failing_profile)]),
                frozenset(), "another function under the label is not the profile")
            self.assertEqual(
                self.mod._replaced_cli_rules([("RULE-08", self._profile)]),
                frozenset(), "the profile reports as RULE-12 and replaces nothing else")
        self.assertEqual(self.mod._replaced_cli_rules([]), frozenset())

    def _patched_release(self, version):
        """Real guard, real package, but a release the package does not have yet."""
        import contextlib
        import agent_style
        from agent_style.review import detectors_mech

        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.object(
            agent_style, "__version__", version))
        for name in ("_rule_12_profile", "_rule_08_repeats"):
            stack.enter_context(unittest.mock.patch.object(
                detectors_mech, name, self._profile, create=True))
        return stack

    def test_the_real_guard_at_0_8_0_swaps_the_profile_in(self) -> None:
        """End to end through guard.py's loader, with the release patched.

        At 0.8.0 the guard's set holds the profile under RULE-12 and RULE-08
        under its own label, both path-aware, and the audit drops the CLI's
        per-sentence hit. RULE-08 has no CLI mechanical detector and is
        therefore partial: its semantic half is skipped and the hook half ran.
        """
        with tempfile.TemporaryDirectory() as tmp, self._patched_release("0.8.0"):
            hook_rules = self.mod._load_hook_detectors([str(GUARD)])
            rules, path_aware = hook_rules
            self.assertEqual(
                [label for label, _ in rules],
                ["RULE-03", "RULE-A", "RULE-08", "RULE-05", "RULE-06", "RULE-12",
                 "RULE-B", "RULE-D", "RULE-I"])
            self.assertLessEqual({"RULE-08", "RULE-12"}, path_aware)
            findings, coverage, error = self._audit(tmp, hook_rules)
        self.assertIsNone(error)
        self.assertEqual(self._details(findings), ["profile-path=notes.md"])
        self.assertEqual(self._details(findings, "RULE-08"), ["profile-path=notes.md"])
        self.assertIn("RULE-12", coverage["covered"])
        self.assertIn("RULE-08", coverage["partial"])

    def test_the_real_guard_at_0_7_0_keeps_the_cli_rule_12(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self._patched_release("0.7.0"):
            hook_rules = self.mod._load_hook_detectors([str(GUARD)])
            self.assertNotIn("RULE-08", [label for label, _ in hook_rules[0]])
            findings, coverage, _ = self._audit(tmp, hook_rules)
        self.assertEqual(self._details(findings), ["sentence length 34 words (>30)"])
        self.assertIn("RULE-12", coverage["covered"])
        self.assertEqual(self._details(findings, "RULE-08"), [])

    # Over eighty words in one sentence, as the profile counts them.
    _EXTREME = "The committee met" + ", and then it met again" * 20 + "."

    def _revise_the_third_line(self, tmp, hook_rules):
        """Stage a file whose line 1 is unchanged and whose line 3 was rewritten.

        Line 1 holds an extreme sentence in both versions. Line 3 is a short
        sentence in the base and a second extreme sentence in the revision.
        Returns the audit's JSON payload for that staged revision.
        """
        base = f"{self._EXTREME}\n\nThe committee adjourned.\n"
        revised = f"{self._EXTREME}\n\n{self._EXTREME}\n"
        repo = make_repo(Path(tmp) / "repo")
        stage(repo, "notes.md", base)
        git(repo, "commit", "-qm", "base")
        stage(repo, "notes.md", revised)
        with unittest.mock.patch.object(
                self.mod, "_load_hook_detectors", return_value=hook_rules):
            rc, out = run_main(self.mod, ["--repo", str(repo), "--json"])
        self.assertEqual(rc, 0)
        return json.loads(out)

    def test_a_profile_finding_outlives_a_change_to_a_later_sentence(self) -> None:
        """The collapsed extreme-sentence finding sits on the FIRST such sentence.

        A new extreme sentence further down leaves that line untouched, and the
        count in the finding is what changed. Every finding of the profile is
        about the document, and line scoping must not drop it. The same text from
        an ordinary detector at the same line is about one place and is dropped.
        """
        from agent_style.review import detectors_mech

        class V:
            column, excerpt = 1, ""

            def __init__(self, detail):
                self.line, self.detail = 1, detail

        def collapsed(text, file_path=""):
            return [V("2 sentences over 80 words, the first at line 1, a length "
                      "that is uncommon in human-written prose; consider whether "
                      "each carries one connected thought")]

        def local(text, file_path=""):
            return [V("an interval written into the first sentence")]

        hook_rules = ([("RULE-12", collapsed), ("RULE-03", local)],
                      frozenset({"RULE-12", "RULE-03"}))
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(
                detectors_mech, "_rule_12_profile", collapsed, create=True):
            payload = self._revise_the_third_line(tmp, hook_rules)
        listed = {(f["rule"], f["line"]) for f in payload["findings"]}
        self.assertIn(("RULE-12", 1), listed)
        self.assertNotIn(("RULE-03", 1), listed)
        profile = [f for f in payload["findings"] if f["rule"] == "RULE-12"]
        self.assertTrue(all(f["document_level"] is True for f in profile))

    def test_the_real_profile_keeps_the_collapsed_finding_after_a_later_change(self) -> None:
        """End to end with the package's own profile, from agent-style 0.8.0."""
        import agent_style

        release = tuple(int(p) for p in agent_style.__version__.split(".")[:3])
        if release < (0, 8, 0):
            self.skipTest("the RULE-12 profile ships with agent-style 0.8.0")
        hook_rules = self.mod._load_hook_detectors([str(GUARD)])
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._revise_the_third_line(tmp, hook_rules)
        rule_12 = [(f["line"], f["detail"]) for f in payload["findings"]
                   if f["rule"] == "RULE-12"]
        self.assertEqual(len(rule_12), 1, rule_12)
        line, detail = rule_12[0]
        self.assertEqual(line, 1)
        self.assertTrue(detail.startswith("2 sentences over 80 words, the first "
                                          "at line 1"), detail)


class FileLevelProblemTests(unittest.TestCase):
    """A finding about a file has no line, and must not be filtered by line.

    An AUDIT finding sits at line 0. Line 0 is in no changed-line set, so
    before this it was counted as pre-existing and dropped, and a file the
    audit could not read printed CLEAN.
    """

    def setUp(self) -> None:
        self.mod = load_module(AUDIT, "_style_audit_problem")

    def test_an_unreadable_blob_is_reported_not_filtered_away(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            stage(repo, "notes.md", "Short and clean.\n")
            with unittest.mock.patch.object(
                    self.mod, "_materialize_index_blob", return_value=None):
                rc, out = run_main(self.mod, ["--repo", str(repo), "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["status"].startswith("FINDINGS:"),
                        f"expected FINDINGS, got {payload['status']!r}")
        self.assertEqual([f["rule"] for f in payload["findings"]], ["AUDIT"])

    def test_a_detector_that_raises_is_reported_in_staged_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            stage(repo, "notes.md", "Short and clean.\n")

            def boom(*a, **k):
                raise PermissionError("denied")

            # Patch the detector the module imports lazily.
            import agent_style.review as review
            with unittest.mock.patch.object(review, "audit", boom):
                rc, out = run_main(self.mod, ["--repo", str(repo), "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual([f["rule"] for f in payload["findings"]], ["AUDIT"])
        self.assertIn("PermissionError", payload["findings"][0]["detail"])


class ChangedLinesTests(unittest.TestCase):
    """The hunk parser, and what it does when it cannot trust its answer."""

    def setUp(self) -> None:
        self.mod = load_module(AUDIT, "_style_audit_lines")

    def test_a_new_file_marks_every_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            stage(repo, "a.md", "one\ntwo\nthree\n")
            self.assertEqual(self.mod.changed_lines(str(repo), "a.md"),
                             {1, 2, 3})

    def test_a_single_line_hunk_has_no_comma(self) -> None:
        """git emits `+7` rather than `+7,1` for a one-line hunk."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            stage(repo, "a.md", "".join(f"line{i}\n" for i in range(1, 10)))
            git(repo, "commit", "-qm", "base")
            path = Path(repo) / "a.md"
            body = path.read_text(encoding="utf-8").splitlines()
            body[6] = "CHANGED"
            path.write_text("\n".join(body) + "\n", encoding="utf-8")
            git(repo, "add", "a.md")
            self.assertEqual(self.mod.changed_lines(str(repo), "a.md"), {7})

    def test_a_pure_deletion_marks_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            stage(repo, "a.md", "one\ntwo\nthree\n")
            git(repo, "commit", "-qm", "base")
            (Path(repo) / "a.md").write_text("one\nthree\n", encoding="utf-8")
            git(repo, "add", "a.md")
            self.assertEqual(self.mod.changed_lines(str(repo), "a.md"), set())

    def test_multiple_hunks_are_unioned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            stage(repo, "a.md", "".join(f"line{i}\n" for i in range(1, 21)))
            git(repo, "commit", "-qm", "base")
            path = Path(repo) / "a.md"
            body = path.read_text(encoding="utf-8").splitlines()
            body[1] = "EDIT-A"
            body[15] = "EDIT-B"
            path.write_text("\n".join(body) + "\n", encoding="utf-8")
            git(repo, "add", "a.md")
            self.assertEqual(self.mod.changed_lines(str(repo), "a.md"), {2, 16})

    def test_an_unparseable_header_falls_back_rather_than_skipping(self) -> None:
        """Skipping a header it cannot read would leave every finding inside
        that hunk looking pre-existing, which prints CLEAN. None means report
        everything, which the caller then says out loud."""
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="@@ -1 +nonsense @@\n", stderr="")
        with unittest.mock.patch.object(self.mod, "_git", return_value=fake):
            self.assertIsNone(self.mod.changed_lines("/repo", "a.md"))

    def test_a_binary_attribute_does_not_suppress_the_patch(self) -> None:
        """`.gitattributes` can mark a prose path binary, and git would then
        emit no hunk header at all. `--text` overrides that, so scoping keeps
        working rather than falling back."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            stage(repo, ".gitattributes", "*.md binary\n")
            stage(repo, "a.md", "one\ntwo\n")
            self.assertEqual(self.mod.changed_lines(str(repo), "a.md"), {1, 2})

    def test_a_patch_with_no_hunk_header_falls_back(self) -> None:
        """The remaining guard. A file in the staged list changed, so a patch
        carrying no hunk header means git suppressed it by some route
        `--text` does not cover. An empty set there would drop every finding
        as pre-existing and print CLEAN.
        """
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="diff --git a/a.md b/a.md\nBinary files differ\n", stderr="")
        with unittest.mock.patch.object(self.mod, "_git", return_value=fake):
            self.assertIsNone(self.mod.changed_lines("/repo", "a.md"))

    def test_git_failure_falls_back(self) -> None:
        with unittest.mock.patch.object(self.mod, "_git", return_value=None):
            self.assertIsNone(self.mod.changed_lines("/repo", "a.md"))

    def test_the_fallback_is_announced(self) -> None:
        findings = [{"file": "a.md", "line": 5, "column": 1, "rule": "RULE-12",
                     "detail": "d", "excerpt": ""}]
        with unittest.mock.patch.object(self.mod, "changed_lines",
                                        return_value=None):
            kept, dropped, fell_back = self.mod.scope_to_changed_lines(
                findings, "/repo")
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 0)
        self.assertEqual(fell_back, ["a.md"])

    def test_git_is_asked_once_per_file_not_once_per_finding(self) -> None:
        """359 whole-file findings would otherwise launch 359 git processes,
        each with a 60-second timeout. An audit that cannot hold the loop
        open must not be able to spend hours before returning 0."""
        findings = [{"file": "a.md", "line": i, "column": 1,
                     "rule": "RULE-12", "detail": "d", "excerpt": ""}
                    for i in range(1, 200)]
        findings += [{"file": "b.md", "line": i, "column": 1,
                      "rule": "RULE-12", "detail": "d", "excerpt": ""}
                     for i in range(1, 200)]
        with unittest.mock.patch.object(
                self.mod, "changed_lines", return_value={1}) as spy:
            self.mod.scope_to_changed_lines(findings, "/repo")
        self.assertEqual(spy.call_count, 2,
                         "one git query per file, regardless of finding count")


class DocumentLevelScopingTests(unittest.TestCase):
    """A finding about the whole document survives the changed-line filter.

    agent-style 0.8.0 opens the findings that describe a document (a share of
    long sentences, a run of `, so` joins, a caveat stated again and again)
    with `_PF_DOCUMENT_PREFIX`, and anchors each on the first sentence, join
    or member behind it. Revising another part of the file leaves that line
    untouched. Line scoping alone then reports the file clean, although the
    revision is what moved the profile. A finding about one place in the text
    keeps the line scoping.
    """

    PREFIX = "Across the document, "

    def setUp(self) -> None:
        self.mod = load_module(AUDIT, "_style_audit_document")
        try:
            from agent_style.review import detectors_mech  # noqa: F401
        except Exception as exc:
            if os.environ.get("CI"):
                self.fail(f"agent_style must be importable in CI: {exc!r}")
            self.skipTest("agent_style not importable in this environment")

    def _prefix(self, value):
        """The detector module's marker, set to `value` for one test."""
        from agent_style.review import detectors_mech

        return unittest.mock.patch.object(
            detectors_mech, "_PF_DOCUMENT_PREFIX", value, create=True)

    @staticmethod
    def _finding(rule, line, detail):
        return {"file": "a.md", "line": line, "column": 1, "rule": rule,
                "detail": detail, "excerpt": ""}

    def test_the_marker_is_read_from_the_package(self) -> None:
        with self._prefix(self.PREFIX):
            self.assertEqual(self.mod._document_level_prefix(), self.PREFIX)

    def test_a_missing_empty_or_odd_marker_marks_nothing(self) -> None:
        """An empty prefix is the dangerous value: every string starts with it."""
        from agent_style.review import detectors_mech

        for value in ("", None, 0, 5, b"Across", ["Across"]):
            with self.subTest(value=value), self._prefix(value):
                self.assertIsNone(self.mod._document_level_prefix())
        # A release that predates the constant has no such attribute at all.
        with unittest.mock.patch.dict(vars(detectors_mech)):
            vars(detectors_mech).pop("_PF_DOCUMENT_PREFIX", None)
            self.assertIsNone(self.mod._document_level_prefix())

    def test_an_unimportable_package_marks_nothing(self) -> None:
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name.startswith("agent_style"):
                raise ImportError("blocked by test")
            return real_import(name, *args, **kwargs)

        with unittest.mock.patch.object(builtins, "__import__", blocked):
            self.assertIsNone(self.mod._document_level_prefix())

    def test_a_document_level_finding_is_kept_when_its_line_is_untouched(self) -> None:
        findings = [
            self._finding("RULE-12", 3, self.PREFIX + "about 50% of the 60 sentences"),
            self._finding("RULE-12", 4, self.PREFIX + "7 sentences join clauses"),
            self._finding("RULE-08", 5, self.PREFIX + "the same caveat framing"),
        ]
        with self._prefix(self.PREFIX), unittest.mock.patch.object(
                self.mod, "changed_lines", return_value={40}):
            kept, dropped, fell_back = self.mod.scope_to_changed_lines(
                findings, "/repo")
        self.assertEqual(kept, findings)
        self.assertEqual((dropped, fell_back), (0, []))

    def test_a_finding_about_one_place_keeps_the_line_scoping(self) -> None:
        """A local detector's finding names the text it sits on and is scoped."""
        findings = [
            self._finding("RULE-03", 3, "an interval written into prose"),
            self._finding("RULE-B", 4, "a casual em dash"),
            self._finding("RULE-03", 40, "an interval written into prose"),
        ]
        with self._prefix(self.PREFIX), unittest.mock.patch.object(
                self.mod, "changed_lines", return_value={40}):
            kept, dropped, _ = self.mod.scope_to_changed_lines(findings, "/repo")
        self.assertEqual([f["line"] for f in kept], [40])
        self.assertEqual(dropped, 2)

    def test_a_flagged_finding_is_kept_whatever_its_text(self) -> None:
        """The profile's collapsed finding names the first extreme sentence.

        Line 1 keeps the first extreme sentence, and line 3 adds another.
        Without a document marker, the finding survives only because the
        audit marks the profile result as document-level. Removing that flag
        restores changed-line scoping.
        """
        detail = ("2 sentences over 80 words, the first at line 1, a length "
                  "that is uncommon in human-written prose")
        flagged = dict(self._finding("RULE-12", 1, detail), document_level=True)
        plain = self._finding("RULE-12", 1, detail)
        with self._prefix(self.PREFIX), unittest.mock.patch.object(
                self.mod, "changed_lines", return_value={3}):
            kept, dropped, _ = self.mod.scope_to_changed_lines(
                [flagged, plain], "/repo")
        self.assertEqual(kept, [flagged])
        self.assertEqual(dropped, 1)

    def test_the_flag_needs_no_marker_from_the_package(self) -> None:
        """An old package has no marker, and the flag still holds."""
        flagged = dict(self._finding("RULE-12", 1, "text"), document_level=True)
        with self._prefix(None), unittest.mock.patch.object(
                self.mod, "changed_lines", return_value={3}):
            kept, dropped, _ = self.mod.scope_to_changed_lines([flagged], "/repo")
        self.assertEqual((kept, dropped), ([flagged], 0))

    def test_a_flag_that_is_off_or_odd_marks_nothing(self) -> None:
        findings = [dict(self._finding("RULE-12", 1, "text"), document_level=value)
                    for value in (False, None, 0, "")]
        with self._prefix(None), unittest.mock.patch.object(
                self.mod, "changed_lines", return_value={3}):
            kept, dropped, _ = self.mod.scope_to_changed_lines(findings, "/repo")
        self.assertEqual((kept, dropped), ([], 4))

    def test_the_prefix_counts_only_at_the_start_of_the_detail(self) -> None:
        findings = [self._finding(
            "RULE-12", 3, "a sentence that begins " + self.PREFIX + "here")]
        with self._prefix(self.PREFIX), unittest.mock.patch.object(
                self.mod, "changed_lines", return_value={40}):
            kept, dropped, _ = self.mod.scope_to_changed_lines(findings, "/repo")
        self.assertEqual((kept, dropped), ([], 1))

    def test_without_a_marker_the_old_package_scopes_every_finding_by_line(self) -> None:
        """agent-style before 0.8.0 has no document-level findings to keep."""
        findings = [self._finding("RULE-12", 3, self.PREFIX + "about 50%")]
        for value in (None, ""):
            with self.subTest(value=value), self._prefix(value), \
                    unittest.mock.patch.object(
                        self.mod, "changed_lines", return_value={40}):
                kept, dropped, _ = self.mod.scope_to_changed_lines(
                    findings, "/repo")
            self.assertEqual((kept, dropped), ([], 1))

    def test_a_detail_that_is_not_text_does_not_raise(self) -> None:
        findings = [self._finding("RULE-12", 3, None),
                    {"file": "a.md", "line": 4, "rule": "RULE-12"}]
        with self._prefix(self.PREFIX), unittest.mock.patch.object(
                self.mod, "changed_lines", return_value={40}):
            kept, dropped, _ = self.mod.scope_to_changed_lines(findings, "/repo")
        self.assertEqual((kept, dropped), ([], 2))

    def test_the_staged_audit_keeps_the_document_finding_of_a_revised_file(self) -> None:
        """End to end: the diff touches the last line, the anchors sit on line 1.

        The planted detectors stand for RULE-08, which anchors a document-level
        finding on its first member, and for a finding about one place. The CLI
        runs neither label, which leaves both to the hook set. Only the
        document-level one may appear, and the other is counted as pre-existing
        rather than listed.
        """
        prefix = self.PREFIX

        class V:
            column, excerpt = 1, ""

            def __init__(self, line, detail):
                self.line, self.detail = line, detail

        def document_level(text, file_path=""):
            return [V(1, prefix + "about 50% of the 60 sentences run past 30 words")]

        def one_place(text, file_path=""):
            return [V(1, "an interval written into the first sentence")]

        hook_rules = (
            [("RULE-08", document_level), ("RULE-03", one_place)],
            frozenset({"RULE-08", "RULE-03"}),
        )
        base = "".join(f"Sentence number {i} is short.\n\n" for i in range(1, 13))
        with tempfile.TemporaryDirectory() as tmp, self._prefix(prefix):
            repo = make_repo(Path(tmp) / "repo")
            stage(repo, "notes.md", base)
            git(repo, "commit", "-qm", "base")
            stage(repo, "notes.md", base + "A final sentence was added.\n")
            with unittest.mock.patch.object(
                    self.mod, "_load_hook_detectors", return_value=hook_rules):
                rc, out = run_main(self.mod, ["--repo", str(repo), "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        listed = {(f["rule"], f["line"]) for f in payload["findings"]}
        self.assertIn(("RULE-08", 1), listed)
        self.assertNotIn(("RULE-03", 1), listed)
        self.assertEqual(payload["pre_existing_not_listed"], 1)
        self.assertTrue(payload["status"].startswith("FINDINGS:"))


class InvocationSurfaceTests(unittest.TestCase):
    """The remaining ways a caller can invoke it, all of which exit 0."""

    def setUp(self) -> None:
        self.mod = load_module(AUDIT, "_style_audit_surface")

    def test_an_empty_index_in_a_real_repo_is_a_skip(self) -> None:
        """Distinct from a non-git directory, which the earlier test used."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp)
            rc, out = run_main(self.mod, ["--repo", str(repo)])
        self.assertEqual(rc, 0)
        self.assertIn("Style status: SKIPPED:", out)

    def test_a_directory_passed_as_a_file_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "adir.md"
            sub.mkdir()
            rc, out = run_main(self.mod, ["--repo", tmp, str(sub)])
        self.assertEqual(rc, 0)
        self.assertIn("Style status:", out)

    def test_zero_limit_lists_nothing_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.md"
            probe.write_text(_DIRTY, encoding="utf-8")
            rc, out = run_main(self.mod,
                               ["--repo", tmp, "--limit", "0", str(probe)])
        self.assertEqual(rc, 0)
        self.assertIn("more not listed", out)

    def test_an_unexpected_exception_becomes_a_reported_failure(self) -> None:
        """The boundary around the run. Without it an unforeseen error is a
        failed step in someone's review rather than a reported outcome."""
        with unittest.mock.patch.object(
                self.mod, "_run", side_effect=RuntimeError("boom")):
            rc, out = run_main(self.mod, ["--repo", "."])
        self.assertEqual(rc, 0)
        self.assertIn("Style status: FAILED:", out)
        self.assertIn("RuntimeError", out)

    def test_a_usage_error_still_exits_two(self) -> None:
        """Deliberately outside the contract: a malformed command line is a
        caller defect, and no review-loop invocation produces one."""
        with self.assertRaises(SystemExit) as ctx:
            self.mod.main(["--no-such-flag"])
        self.assertEqual(ctx.exception.code, 2)


class GuardLookupTests(unittest.TestCase):
    """The script prefers guard.py's definitions and must refuse a partial one.

    Every consumer runs an older ~/.claude/hooks/guard.py until it bootstraps
    again, and that hook carries PROSE_EXTENSIONS without AGENT_IO_SEGMENT.
    Adopting the half it offers would scope by extension with no marker, which
    is the behaviour this change replaces. Observed live against dgx-spark,
    whose deployed hook predates the constant.
    """

    def setUp(self) -> None:
        self.mod = load_module(AUDIT, "_style_audit_lookup")

    @staticmethod
    def _guard_stub(tmp, body):
        path = Path(tmp) / "guard.py"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def _assert_fell_back(self, exts, seg):
        self.assertEqual(set(exts), set(self.mod._FALLBACK_PROSE_EXTENSIONS))
        self.assertEqual(seg, self.mod._FALLBACK_AGENT_IO_SEGMENT)

    def test_a_complete_guard_is_adopted(self) -> None:
        body = 'PROSE_EXTENSIONS = frozenset([".zz"])\nAGENT_IO_SEGMENT = "carried"\n'
        with tempfile.TemporaryDirectory() as tmp:
            exts, seg = self.mod._load_scope_predicates([self._guard_stub(tmp, body)])
        self.assertEqual(set(exts), {".zz"})
        self.assertEqual(seg, "carried")

    def test_an_older_guard_without_the_marker_falls_back(self) -> None:
        body = 'PROSE_EXTENSIONS = frozenset([".zz"])\n'
        with tempfile.TemporaryDirectory() as tmp:
            exts, seg = self.mod._load_scope_predicates([self._guard_stub(tmp, body)])
        self._assert_fell_back(exts, seg)

    def test_a_guard_that_raises_falls_back(self) -> None:
        body = "raise RuntimeError('broken')\n"
        with tempfile.TemporaryDirectory() as tmp:
            exts, seg = self.mod._load_scope_predicates([self._guard_stub(tmp, body)])
        self._assert_fell_back(exts, seg)

    def test_no_candidate_at_all_falls_back(self) -> None:
        exts, seg = self.mod._load_scope_predicates([])
        self._assert_fell_back(exts, seg)


class CoverageReportTests(unittest.TestCase):
    """A partial audit must not read like a complete one."""

    def _require_agent_style(self) -> None:
        try:
            import agent_style.review  # noqa: F401
        except Exception as exc:
            if os.environ.get("CI"):
                self.fail(f"agent_style must be importable in CI: {exc!r}")
            self.skipTest("agent_style not importable in this environment")

    def test_partial_and_uncovered_are_reported_apart(self) -> None:
        """RULE-06 runs mechanically and its semantic half does not, so
        listing it beside a rule no detector reaches would misstate both."""
        self._require_agent_style()
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.md"
            probe.write_text(_DIRTY, encoding="utf-8")
            rc, out = run_audit(["--repo", tmp, "--json", str(probe)])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        coverage = payload["coverage"]
        self.assertIn("RULE-06", coverage["partial"])
        self.assertIn("RULE-07", coverage["uncovered"])
        self.assertNotIn("RULE-06", coverage["uncovered"])
        self.assertFalse(set(coverage["covered"]) & set(coverage["uncovered"]))

    def test_rule_g_is_opt_in(self) -> None:
        """Measured at 40% of all findings on this corpus, and it flags the
        sentence-case headings written on purpose. On by default it would
        bury the findings worth acting on."""
        self._require_agent_style()
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.md"
            probe.write_text("## a lowercase heading\n\nShort and clean.\n",
                             encoding="utf-8")
            rc_default, out_default = run_audit(["--repo", tmp, "--json", str(probe)])
            rc_optin, out_optin = run_audit(
                ["--repo", tmp, "--json", "--include-rule-g", str(probe)])
        self.assertEqual((rc_default, rc_optin), (0, 0))
        default_rules = {f["rule"] for f in json.loads(out_default)["findings"]}
        optin_rules = {f["rule"] for f in json.loads(out_optin)["findings"]}
        self.assertNotIn("RULE-G", default_rules)
        self.assertIn("RULE-G", optin_rules)


    def test_a_suppressed_rule_is_not_counted_as_covered(self) -> None:
        """RULE-G's detector runs, so `ran_detectors` holds it even while
        every finding is discarded. Leaving it in `covered` made the report
        claim an audit it did not perform, which is the failure the
        four-way split exists to prevent."""
        self._require_agent_style()
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.md"
            probe.write_text('## a lowercase heading\n\nShort and clean.\n',
                             encoding="utf-8")
            rc_default, out_default = run_audit(["--repo", tmp, "--json",
                                                str(probe)])
            rc_optin, out_optin = run_audit(
                ["--repo", tmp, "--json", "--include-rule-g", str(probe)])
        self.assertEqual((rc_default, rc_optin), (0, 0))
        default_cov = json.loads(out_default)["coverage"]
        self.assertIn("RULE-G", default_cov["suppressed"])
        for bucket in ("covered", "uncovered", "partial"):
            self.assertNotIn("RULE-G", default_cov[bucket], bucket)

        optin_cov = json.loads(out_optin)["coverage"]
        self.assertEqual(optin_cov["suppressed"], [],
                         "opting in must empty the suppressed bucket")
        self.assertIn("RULE-G", optin_cov["covered"])

    def test_the_suppressed_bucket_is_named_in_the_text_report(self) -> None:
        """A JSON-only signal would not reach the reader of the pre-flight."""
        self._require_agent_style()
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.md"
            probe.write_text('## a lowercase heading\n\nShort and clean.\n',
                             encoding="utf-8")
            rc, out = run_audit(["--repo", tmp, str(probe)])
        self.assertEqual(rc, 0)
        self.assertIn("Suppressed by policy", out)
        self.assertIn("--include-rule-g", out)

if __name__ == "__main__":
    unittest.main()
