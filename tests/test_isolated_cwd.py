"""The CLI tests must not write into the directory they are started from.

Three layers: the helper itself, the modules that use it (each started as a
separate process from a directory it used to write into), and the import path
of the helper under a dotted unittest run.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# tests/ is on sys.path under `unittest discover -s tests` but not under
# `python -m unittest tests.<module>`. Put it there before the sibling imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows
from _isolated_cwd import enter_isolated_cwd, isolated_cwd, leave_isolated_cwd  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent

# (module, `-k` pattern, files the caller's directory holds beforehand).
# The two pack modules wrote agent-config.yaml into a directory that looks
# like a bootstrapped consumer. test_cli_robustness.py created .agent-config/
# in any directory that did not already have one.
FORMERLY_WRITING = (
    ("test_packs_cli.py", "test_first_add_writes_one_row_per_remote_pack", (".agent-config/bootstrap.sh",)),
    ("test_packs_cli_v0_5.py", "test_pack_add_with_name_on_single_pack_renames_output", (".agent-config/bootstrap.sh",)),
    ("test_cli_robustness.py", "BootstrapMainFallThroughTests", ()),
)


def _listing(directory: str) -> list[str]:
    return sorted(p.relative_to(directory).as_posix() for p in Path(directory).rglob("*"))


class IsolatedCwdHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = os.getcwd()
        self.addCleanup(os.chdir, self.start)

    def test_enter_moves_to_an_empty_directory_and_leave_restores(self) -> None:
        self.assertIsNone(isolated_cwd())
        path = enter_isolated_cwd()
        try:
            self.assertEqual(Path.cwd().resolve(), Path(path).resolve())
            self.assertNotEqual(Path(path).resolve(), Path(self.start).resolve())
            self.assertEqual(list(Path(path).iterdir()), [])
            self.assertEqual(isolated_cwd(), path)
        finally:
            leave_isolated_cwd()
        self.assertEqual(Path.cwd().resolve(), Path(self.start).resolve())
        self.assertFalse(Path(path).exists())
        self.assertIsNone(isolated_cwd())

    def test_nested_enter_unwinds_in_order(self) -> None:
        outer = enter_isolated_cwd()
        inner = enter_isolated_cwd()
        try:
            self.assertNotEqual(outer, inner)
            self.assertEqual(Path.cwd().resolve(), Path(inner).resolve())
        finally:
            leave_isolated_cwd()
        try:
            self.assertEqual(Path.cwd().resolve(), Path(outer).resolve())
            self.assertFalse(Path(inner).exists())
        finally:
            leave_isolated_cwd()
        self.assertEqual(Path.cwd().resolve(), Path(self.start).resolve())

    def test_leave_without_enter_is_a_no_op(self) -> None:
        leave_isolated_cwd()
        self.assertEqual(Path.cwd().resolve(), Path(self.start).resolve())


class CliTestsLeaveTheCallersDirectoryAloneTests(unittest.TestCase):
    """Start each module from a directory it used to write into."""

    def test_tests_pass_and_the_callers_directory_is_unchanged(self) -> None:
        for filename, pattern, seeded in FORMERLY_WRITING:
            with self.subTest(module=filename), tempfile.TemporaryDirectory() as caller:
                for rel in seeded:
                    target = Path(caller) / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("", encoding="utf-8")
                before = _listing(caller)
                proc = subprocess.run(
                    [
                        sys.executable, "-B", "-m", "unittest", "discover",
                        "-s", str(TESTS_DIR), "-p", filename, "-k", pattern,
                    ],
                    cwd=caller,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])
                # A `-k` that matches nothing can also exit 0, so require a test.
                ran = re.search(r"^Ran (\d+) tests?", proc.stderr, re.M)
                self.assertIsNotNone(ran, proc.stderr[-2000:])
                self.assertGreater(int(ran.group(1)), 0, proc.stderr[-2000:])
                self.assertEqual(_listing(caller), before)


class IsolatedCwdImportPathTests(unittest.TestCase):
    """The sibling import must survive every way the suite is started.

    Same rule as QuietSpawnImportPathTests in test_bootstrap_preflight.py: a
    bare `from _isolated_cwd import ...` resolves under `unittest discover -s
    tests` and raises ModuleNotFoundError under `python -m unittest
    tests.<module>`.
    """

    # The whole line, because these modules also insert other directories.
    GUARD = "sys.path.insert(0, str(Path(__file__).resolve().parent))"
    IMPORT = "from _isolated_cwd import"

    def _modules_importing_the_helper(self) -> list[Path]:
        return [
            path
            for path in sorted(TESTS_DIR.glob("test_*.py"))
            if re.search(r"^from _isolated_cwd import\b", path.read_text(encoding="utf-8"), re.M)
        ]

    def test_every_import_site_carries_a_sys_path_guard(self) -> None:
        modules = self._modules_importing_the_helper()
        self.assertGreaterEqual(len(modules), 4, "expected this file and the three CLI modules")
        offenders = []
        for path in modules:
            before = path.read_text(encoding="utf-8").split(self.IMPORT, 1)[0]
            if self.GUARD not in before:
                offenders.append(path.name)
        self.assertEqual([], offenders)

    def test_every_module_imports_under_a_dotted_unittest_run(self) -> None:
        # One process per module. In a shared process the first module to load
        # puts tests/ on sys.path and hides a missing guard in every later one.
        for path in self._modules_importing_the_helper():
            with self.subTest(module=path.name):
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "unittest", "-k", "zzz_matches_no_test", "tests." + path.stem],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                combined = result.stdout + result.stderr
                self.assertNotIn("ModuleNotFoundError", combined, combined[-2000:])
                self.assertNotIn("Traceback", combined, combined[-2000:])


if __name__ == "__main__":
    unittest.main()
