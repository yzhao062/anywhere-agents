"""Run a test module from an empty temporary directory.

Two CLI paths write relative to the working directory. ``pack add`` treats a
directory that holds ``.agent-config/bootstrap.sh`` as the project and writes
``agent-config.yaml`` there. ``_bootstrap_main`` creates ``.agent-config/``
through a relative path, which a patched ``Path.cwd`` does not redirect. A
module whose tests reach either path must not inherit the directory the suite
was started from: on 2026-11-15 a suite started from inside a bootstrapped
consumer appended nine test rows to that consumer's real file, and seven tests
failed for a reason that had nothing to do with the change under test.

Use it at module level in a test file::

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _isolated_cwd import enter_isolated_cwd, leave_isolated_cwd

    def setUpModule() -> None:
        enter_isolated_cwd()

    def tearDownModule() -> None:
        leave_isolated_cwd()

The ``sys.path`` line matters: ``unittest discover -s tests`` puts this
directory on the path, and ``python -m unittest tests.<module>`` does not.
A test that needs a project of its own still changes into it and back, as
before; "back" is then the module's temporary directory.
"""
from __future__ import annotations

import os
import tempfile

_stack: list[tuple[str, tempfile.TemporaryDirectory]] = []


def enter_isolated_cwd() -> str:
    """Change into a fresh, empty temporary directory and return its path."""
    original = os.getcwd()
    holder = tempfile.TemporaryDirectory(prefix="aa-test-cwd-")
    os.chdir(holder.name)
    _stack.append((original, holder))
    return holder.name


def leave_isolated_cwd() -> None:
    """Return to the directory that was current before the matching enter.

    Does nothing when no enter is outstanding, so a failed ``setUpModule``
    cannot turn into a second error in ``tearDownModule``.
    """
    if not _stack:
        return
    original, holder = _stack.pop()
    try:
        os.chdir(original)
    finally:
        holder.cleanup()


def isolated_cwd() -> str | None:
    """Path of the innermost isolated directory, or None outside one."""
    return _stack[-1][1].name if _stack else None
