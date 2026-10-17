#!/usr/bin/env python3
"""Vendor scripts/packs/{auth,source_fetch,schema,locks,residue}.py into the PyPI package.

Source of truth: scripts/packs/*.py
Vendored copies: packages/pypi/anywhere_agents/packs/*.py

CI runs this and fails if the vendored copies don't match.
"""
import pathlib, shutil, sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "scripts" / "packs"
DST = REPO / "packages" / "pypi" / "anywhere_agents" / "packs"
# Kept explicit rather than computed, so the shipped surface stays
# reviewable. The cost is that every module reachable from a listed
# module's imports has to be added here by hand: miss one and the
# vendored package imports cleanly from the repo but raises ImportError
# once installed. ``check()`` compares text only and cannot catch that,
# so ``tests/test_vendored_package_imports.py`` imports the vendored tree
# in an isolated subprocess and statically asserts the import closure
# (anywhere-agents#18).
MODULES = ("auth.py", "source_fetch.py", "schema.py", "locks.py", "residue.py")


def vendor():
    DST.mkdir(parents=True, exist_ok=True)
    # Path.write_text(newline=) is 3.10+. Use open() with newline="\n" so
    # vendored output is LF-only on Windows too, supporting Python 3.9+.
    with open(DST / "__init__.py", "w", encoding="utf-8", newline="\n") as f:
        f.write('"""Vendored copies of scripts/packs/* for the installed CLI."""\n')
    for name in MODULES:
        src_file = SRC / name
        dst_file = DST / name
        # Rewrite imports: scripts.packs.X → anywhere_agents.packs.X
        body = src_file.read_text(encoding="utf-8")
        body = body.replace(
            "from scripts.packs",
            "from anywhere_agents.packs",
        )
        body = body.replace(
            "import scripts.packs",
            "import anywhere_agents.packs",
        )
        with open(dst_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)


def _vendored_text(src_file: pathlib.Path) -> str:
    body = src_file.read_text(encoding="utf-8")
    body = body.replace("from scripts.packs", "from anywhere_agents.packs")
    body = body.replace("import scripts.packs", "import anywhere_agents.packs")
    return body


def check() -> None:
    """Fail with non-zero exit if vendored copies don't match the source.

    Compares the destination files byte-for-byte against the rewrite
    that would be produced by `vendor()`. Used by CI vendor-sync job.
    """
    errors = []
    expected_init = '"""Vendored copies of scripts/packs/* for the installed CLI."""\n'
    init_path = DST / "__init__.py"
    if not init_path.exists() or init_path.read_text(encoding="utf-8") != expected_init:
        errors.append(str(init_path))
    for name in MODULES:
        expected = _vendored_text(SRC / name)
        actual_path = DST / name
        actual = actual_path.read_text(encoding="utf-8") if actual_path.exists() else ""
        if actual != expected:
            errors.append(str(actual_path))
    if errors:
        print("vendored pack modules are out of sync with scripts/packs/*:", file=sys.stderr)
        for path in errors:
            print(f"  {path}", file=sys.stderr)
        print("Run `python scripts/vendor-packs.py` to regenerate.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if sys.argv[1:2] == ["check"]:
        check()
    else:
        vendor()
