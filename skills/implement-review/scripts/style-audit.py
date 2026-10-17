#!/usr/bin/env python3
"""Deterministic agent-style audit over the prose a change introduces.

Findings are scoped to the lines the staged diff added or rewrote. Auditing
whole files instead reports a file's entire backlog against whatever change
happens to touch it, and a report the author cannot act on is one the author
stops reading. Pass --all-lines for the whole-file view.

In the default staged mode the bytes audited come from the index, not from
the working tree. A staged file with further unstaged edits holds different
content in each, and auditing the working tree while filtering by staged line
numbers reports on neither: a staged violation can print CLEAN, and an
unstaged one can be attributed to the commit.

Shipped as a script so every reviewer runs the same code. The hand-rolled
version this replaces was reconstructed per review out of `agent-style review
--audit-only` plus a grep for the rules that CLI reports as skipped, and an
observed run had its dash grep fail on the shell locale and match nothing.
A locale-dependent grep that matches nothing looks exactly like prose with no
violations.

ADVISORY, BY CONSTRUCTION. Every audit outcome exits 0: findings, absent
`agent_style`, an unreadable file, an unreadable index blob, nothing staged,
and any unexpected exception, which a boundary around the run converts into a
reported FAILED. That is not politeness, it is the contract: the audit runs
before Round 1, and a step that can fail is a step that can hold the review
loop open. The single exception is an argparse usage error, which exits 2,
because a malformed command line is a caller defect rather than an audit
outcome and no review-loop invocation produces one.

Style findings never enter Round history, never count toward the verdict, and
never earn another round. Three properties make that risk concrete rather
than theoretical. RULE-12 fires on any sentence over thirty words, so
splitting one long sentence can produce another, and the rule has no fixed
point. A prose fix is a diff change, and a diff change earns a round under
the normal loop rules. Handed a list of style findings, a reviewer will treat
them as findings and ask for them to be resolved.

Callers that want an exit code should read `Style status:` from stdout, or
pass --json. Nothing in the review loop should do either.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile

# Kept in sync with scripts/guard.py by tests/test_style_audit.py. The script
# imports the real definitions when it can find guard.py and falls back to
# these, because a pack-deployed skill has no fixed path back to the repo and
# an audit that cannot start is worse than one built on a checked copy.
_FALLBACK_PROSE_EXTENSIONS = frozenset([".md", ".tex", ".rst", ".txt"])
_FALLBACK_AGENT_IO_SEGMENT = "agent-io"

# RULE-G asks for title-case headings. Measured over the 155 markdown files in
# agent-config it produced 1018 of 2561 findings, and it flags the
# sentence-case headings that corpus writes on purpose. In a pre-flight report
# it would bury the findings worth acting on, so it is opt-in here. The
# `style-review` skill still runs it, where a human asked for the full audit.
NOISY_RULES = frozenset(["RULE-G"])

DEFAULT_LIMIT = 40
GIT_TIMEOUT = 60


def _guard_candidates():
    """Where guard.py lives, in the order a machine actually has it."""
    here = os.path.dirname(os.path.abspath(__file__))
    return [
        os.path.join(os.path.expanduser("~"), ".claude", "hooks", "guard.py"),
        os.path.join(here, "..", "..", "..", "scripts", "guard.py"),
    ]


def _load_scope_predicates(candidates=None):
    """Return (prose_extensions, agent_io_segment) from guard.py if reachable.

    A candidate has to supply BOTH constants to be adopted. Every consumer
    runs an older deployed hook until it bootstraps again, and that hook has
    PROSE_EXTENSIONS but not AGENT_IO_SEGMENT. Taking the half it offers
    would leave the script scoping by extension with no marker, which is the
    behaviour this change exists to replace.
    """
    if candidates is None:
        candidates = _guard_candidates()
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location("_ir_guard", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            exts = getattr(module, "PROSE_EXTENSIONS", None)
            seg = getattr(module, "AGENT_IO_SEGMENT", None)
            if exts and seg:
                return frozenset(exts), seg
        except Exception:
            continue
    return _FALLBACK_PROSE_EXTENSIONS, _FALLBACK_AGENT_IO_SEGMENT


PROSE_EXTENSIONS, AGENT_IO_SEGMENT = _load_scope_predicates()


def _load_hook_detectors(candidates=None):
    """Return (rules, path_aware) from guard.py's release-gated loader.

    `audit()` follows the CLI, which keeps its published bench scope fixed.
    It therefore omits the detectors only hooks run: RULE-03 from agent-style
    0.6.0 and RULE-A from 0.7.0. The guard already decides which detectors a
    release promises, so the audit asks the guard rather than keeping a
    second list.

    The guard shipped beside this script comes first and the deployed hook is
    the fallback. A consumer's deployed hook can be older than the package it
    runs against, and a release-gated loader that predates a detector would
    leave it out without a trace. A candidate must expose both the loader and
    `_PATH_AWARE_RULES`; one that lacks either is passed over. With no usable
    guard, `rules` is None, and the caller reports the package's own hook
    detectors as not run.
    """
    if candidates is None:
        candidates = list(reversed(_guard_candidates()))
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location("_ir_guard_rules", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            loader = getattr(module, "_agent_style_mechanical", None)
            path_aware = getattr(module, "_PATH_AWARE_RULES", None)
            if not callable(loader) or path_aware is None:
                continue
            return list(loader() or []), frozenset(path_aware)
        except Exception:
            continue
    return None, frozenset()


def _package_hook_rules():
    """Labels of the `_rule_*` detectors the installed package carries.

    Used only for coverage. A detector here that neither `audit()` nor the
    guard's set ran is reported as a detector that did not run, so a missing
    guard or a failed hook check cannot read as full coverage.
    """
    try:
        from agent_style.review import detectors_mech
    except Exception:
        return set()
    return {
        name.replace("_rule_", "RULE-").upper()
        for name in dir(detectors_mech)
        if name.startswith("_rule_") and name[6:].isalnum()
        and callable(getattr(detectors_mech, name, None))
    }


def is_prose_target(path):
    """True when path is prose the author owns, rather than text being carried.

    Same question guard.py asks about a file being written, asked here about a
    file being staged. A dispatch prompt or a captured review under an
    `agent-io` directory is another agent's text, so a finding on it cannot be
    acted on.
    """
    ext = os.path.splitext(str(path).lower())[1]
    if ext not in PROSE_EXTENSIONS:
        return False
    parts = str(path).replace("\\", "/").lower().split("/")
    return AGENT_IO_SEGMENT not in parts


def _git(repo, args, binary=False):
    """Run git and return CompletedProcess, or None when it could not run.

    ValueError is caught alongside the process errors because an embedded NUL
    in a path is rejected by argv before the process starts.
    """
    try:
        return subprocess.run(
            ["git", "-C", repo] + args,
            capture_output=True, text=not binary, timeout=GIT_TIMEOUT,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def staged_prose_files(repo):
    """Prose files in the staged diff, or [] when git cannot answer.

    Read as bytes and split on NUL. The `-z` interface exists because a
    pathname is bytes, and decoding it through the process locale first can
    raise or, worse, produce a name that then fails at `git show :<path>`.
    `surrogateescape` round-trips whatever git emitted, so the same string
    re-encodes to the original bytes when it goes back to git.
    """
    out = _git(repo, ["diff", "--cached", "--name-only",
                      "--diff-filter=ACMR", "-z"], binary=True)
    if out is None or out.returncode != 0:
        return []
    names = [n for n in out.stdout.split(b"\0") if n]
    decoded = [n.decode("utf-8", "surrogateescape") for n in names]
    return [n for n in decoded if is_prose_target(n)]


def changed_lines(repo, path):
    """Line numbers this staged change added or rewrote in `path`.

    Returns None when the answer cannot be trusted, which the caller reads as
    "report everything" rather than "report nothing". A silent empty report is
    the failure mode worth avoiding, because it looks exactly like a clean
    audit. An unparseable hunk header returns None for the same reason rather
    than being skipped, since skipping one leaves every finding inside it
    looking pre-existing. So does a patch with no hunk header at all, which
    means git suppressed it rather than that nothing changed.
    """
    out = _git(repo, ["diff", "--cached", "-U0", "--no-ext-diff", "--text",
                      "--", path])
    if out is None or out.returncode != 0:
        return None
    lines = set()
    saw_hunk = False
    for raw in out.stdout.splitlines():
        if not raw.startswith("@@"):
            continue
        saw_hunk = True
        # @@ -old[,count] +new[,count] @@
        try:
            plus = raw.split("+", 1)[1].split("@@", 1)[0].strip()
            start, _, count = plus.partition(",")
            start = int(start)
            count = int(count) if count else 1
        except (IndexError, ValueError):
            return None
        lines.update(range(start, start + count))
    if not saw_hunk:
        # The file is in the staged list, so it changed. No hunk header means
        # the patch was suppressed rather than absent: an attributes entry
        # marking it binary, or a diff driver. An empty set here would drop
        # every finding as pre-existing and print CLEAN.
        return None
    return lines


def _materialize_index_blob(repo, rel, tmpdir):
    """Write the STAGED bytes of `rel` to a file, preserving its suffix.

    The suffix matters because the audit dispatches on extension. The name is
    hashed so a nested path cannot collide with another or escape tmpdir.
    """
    out = _git(repo, ["show", ":" + rel], binary=True)
    if out is None or out.returncode != 0:
        return None
    suffix = os.path.splitext(rel)[1]
    stem = hashlib.sha1(rel.encode("utf-8", "replace")).hexdigest()[:16]
    path = os.path.join(tmpdir, stem + suffix)
    try:
        with open(path, "wb") as handle:
            handle.write(out.stdout)
    except OSError:
        return None
    return path


def audit_files(paths, repo, noisy_rules=NOISY_RULES, from_index=False,
                hook_rules=None):
    """Run the deterministic audit over paths.

    `hook_rules` is `(rules, path_aware)` as `_load_hook_detectors` returns
    it, and is loaded from the guard when None. Each of those rules runs only
    where `audit()` did not already run its mechanical detector, so no rule is
    reported twice.

    With `from_index`, each file is audited as it is STAGED rather than as it
    sits in the working tree. Returns (findings, coverage, error). `coverage`
    splits the rule set four ways, because a rule can carry more than one
    detector and they do not all run. RULE-06 has a mechanical detector that
    catches the banned-word list and a semantic one that is skipped, so
    calling it covered overstates the audit and calling it uncovered
    understates it. `error` is a string when the audit could not run at all,
    and the caller reports it without failing.
    """
    try:
        from agent_style.review import audit
    except Exception as exc:
        return [], {}, f"agent_style is not importable ({exc.__class__.__name__})"

    import dataclasses

    if hook_rules is None:
        hook_rules = _load_hook_detectors()
    extra_rules, path_aware = hook_rules

    findings = []
    ran_detectors = {}
    skipped_detectors = {}

    def problem(rel, detail):
        # Line 0 marks a finding about the file rather than about a line in
        # it. The diff filter must never drop these.
        findings.append({"file": rel, "line": 0, "column": 0, "rule": "AUDIT",
                         "detail": detail, "excerpt": ""})

    with tempfile.TemporaryDirectory() as tmpdir:
        for rel in paths:
            if from_index:
                target = _materialize_index_blob(repo, rel, tmpdir)
                if target is None:
                    problem(rel, "could not read the staged blob")
                    continue
            else:
                target = rel if os.path.isabs(rel) else os.path.join(repo, rel)
            try:
                result = dataclasses.asdict(audit(target, project_root=repo))
            except Exception as exc:
                # One unreadable file must not cost the whole report.
                problem(rel, f"could not audit: {exc.__class__.__name__}")
                continue
            for rule_result in result.get("rule_results", []):
                rule = rule_result.get("rule", "?")
                is_skipped = rule_result.get("status") == "skipped"
                bucket = skipped_detectors if is_skipped else ran_detectors
                bucket.setdefault(rule, set()).add(rule_result.get("detector", "?"))
                if is_skipped or rule in noisy_rules:
                    continue
                for v in rule_result.get("violations", []):
                    findings.append({
                        "file": rel,
                        "line": v.get("line", 0),
                        "column": v.get("column", 0),
                        "rule": v.get("rule", rule),
                        "detail": v.get("detail", ""),
                        "excerpt": v.get("excerpt", ""),
                    })
            if not extra_rules:
                continue
            already = {
                r.get("rule") for r in result.get("rule_results", [])
                if r.get("detector") == "mechanical" and r.get("status") != "skipped"
            }
            try:
                with open(target, encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError as exc:
                problem(rel, f"could not read for hook-only rules: {exc.__class__.__name__}")
                for label, _ in extra_rules:
                    skipped_detectors.setdefault(label, set()).add("mechanical")
                continue
            for label, fn in extra_rules:
                if label in already:
                    continue
                try:
                    # The repository path, not the temp blob's hashed name:
                    # these detectors read the file name to recognise an
                    # abstract, an introduction, or an appendix.
                    violations = fn(text, file_path=rel) if label in path_aware else fn(text)
                except Exception as exc:
                    problem(rel, f"{label} detector raised {exc.__class__.__name__}")
                    skipped_detectors.setdefault(label, set()).add("mechanical")
                    continue
                ran_detectors.setdefault(label, set()).add("mechanical")
                if label in noisy_rules:
                    continue
                for v in violations:
                    findings.append({
                        "file": rel,
                        "line": getattr(v, "line", 0),
                        "column": getattr(v, "column", 0),
                        "rule": label,
                        "detail": getattr(v, "detail", ""),
                        "excerpt": getattr(v, "excerpt", ""),
                    })

    # A detector the package carries that nothing ran, because no guard could
    # supply it or its release gate left it out, is a detector that did not
    # run. Without this, a structural RULE-A pass reads as full coverage while
    # the table check never ran.
    if ran_detectors:
        for label in _package_hook_rules():
            if "mechanical" not in ran_detectors.get(label, set()):
                skipped_detectors.setdefault(label, set()).add("mechanical")

    findings.sort(key=lambda f: (f["file"], f["line"], f["column"]))
    # A suppressed rule ran and had its findings discarded, so it belongs in
    # neither `covered` nor `uncovered`. Reporting it as covered was the exact
    # failure this four-way split exists to prevent: the reader would count a
    # rule as audited while every one of its findings was dropped on the floor.
    suppressed = sorted(r for r in ran_detectors if r in noisy_rules)
    coverage = {
        "covered": sorted(r for r in ran_detectors
                          if r not in skipped_detectors and r not in noisy_rules),
        "partial": sorted(r for r in ran_detectors
                          if r in skipped_detectors and r not in noisy_rules),
        "uncovered": sorted(r for r in skipped_detectors if r not in ran_detectors),
        "suppressed": suppressed,
    }
    return findings, coverage, None


def scope_to_changed_lines(findings, repo):
    """Drop findings outside the staged hunks.

    Returns (kept, dropped_count, fell_back_files). `changed_lines` is asked
    once per FILE. A whole-file audit can produce hundreds of findings in one
    file, and asking per finding would launch that many git processes, each
    with its own timeout. An audit advertised as unable to hold the loop open
    must not be able to spend hours before returning 0.
    """
    kept = []
    dropped = 0
    fell_back = set()
    cache = {}
    for f in findings:
        path = f["file"]
        if path not in cache:
            cache[path] = changed_lines(repo, path)
        touched = cache[path]
        if touched is None:
            fell_back.add(path)
        # An AUDIT finding is about the file, so it has no line to match.
        if (f["rule"] == "AUDIT" or f["line"] <= 0
                or touched is None or f["line"] in touched):
            kept.append(f)
        else:
            dropped += 1
    return kept, dropped, sorted(fell_back)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Deterministic agent-style audit over staged prose. "
                    "Always exits 0; read `Style status:` for the outcome.",
    )
    parser.add_argument("files", nargs="*",
                        help="files to audit; default is the staged prose")
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"max findings to list (default {DEFAULT_LIMIT})")
    parser.add_argument("--include-rule-g", action="store_true",
                        help="also report RULE-G title-case headings")
    parser.add_argument("--all-lines", action="store_true",
                        help="report the whole file, not only changed lines")
    parser.add_argument("--json", action="store_true",
                        help="emit the full result as JSON")
    return parser.parse_args(argv)


ADVISORY_NOTE = ("Advisory only: these findings do not go to the reviewer, do "
                 "not enter Round history, and do not gate the verdict.")


def _run(args):
    noisy = frozenset() if args.include_rule_g else NOISY_RULES
    repo = os.path.abspath(args.repo)
    from_diff = not args.files
    paths = args.files or staged_prose_files(repo)

    pre_existing = 0
    fell_back = []
    if not paths:
        status = "SKIPPED: no staged prose files"
        findings, coverage, error = [], {}, None
    else:
        findings, coverage, error = audit_files(paths, repo, noisy,
                                                from_index=from_diff)
        if from_diff and not args.all_lines:
            # A change of six lines to AGENTS.md otherwise reports every
            # pre-existing em dash in the file. Measured on this repo's own
            # commit: 359 findings whole-file against 2 on the changed lines.
            findings, pre_existing, fell_back = scope_to_changed_lines(
                findings, repo)
        if error:
            status = f"SKIPPED: {error}"
        elif findings:
            status = f"FINDINGS: {len(findings)}"
        else:
            status = "CLEAN"

    if args.json:
        print(json.dumps({
            "status": status, "files": paths, "findings": findings,
            "coverage": coverage, "pre_existing_not_listed": pre_existing,
            "diff_scope_fell_back": fell_back,
            "audited_from": "index" if from_diff else "worktree",
        }, indent=2))
        return 0

    print(f"Style status: {status}")
    print(f"Files audited: {len(paths)}"
          + (" (staged content)" if from_diff and paths else ""))
    if pre_existing:
        # Counted rather than listed. The number tells the author the file has
        # a backlog; the list would bury the lines this change is answerable
        # for. `--all-lines` prints them.
        print(f"Pre-existing findings outside this change: {pre_existing} "
              f"(not listed; --all-lines to see them)")
    if fell_back:
        # Loud, because a silent fallback looks identical to a successfully
        # scoped audit that happened to find the whole file dirty.
        print(f"Diff scoping unavailable for {len(fell_back)} file(s); "
              f"reporting all lines there: {', '.join(fell_back)}")
    if findings:
        shown = findings[:max(args.limit, 0)]
        for f in shown:
            loc = f"{f['file']}:{f['line']}:{f['column']}"
            print(f"  {loc}  {f['rule']}  {f['detail']}")
            if f["excerpt"]:
                print(f"      {f['excerpt']}")
        if len(findings) > len(shown):
            print(f"  ... {len(findings) - len(shown)} more not listed")
    # Naming the gap keeps it visible. An audit that silently covers half the
    # rule set reads exactly like one that covers all of it.
    if coverage.get("partial"):
        print(f"Partially covered ({len(coverage['partial'])}): "
              f"{', '.join(coverage['partial'])}")
        print("  Some detectors ran and others did not: the semantic half, or a "
              "hook-only check that failed or that no guard could supply.")
    if coverage.get("suppressed"):
        print(f"Suppressed by policy ({len(coverage['suppressed'])}): "
              f"{', '.join(coverage['suppressed'])}")
        print("  The detector ran and its findings were discarded; pass "
              "--include-rule-g to see them.")
    if coverage.get("uncovered"):
        print(f"Not covered ({len(coverage['uncovered'])}): "
              f"{', '.join(coverage['uncovered'])}")
        print("  Apply these by reading the rule pack; no detector here "
              "reaches them.")
    print(ADVISORY_NOTE)
    return 0


def main(argv=None):
    args = _parse_args(argv)
    try:
        return _run(args)
    except Exception as exc:
        # The last line of the contract. Anything unforeseen becomes a
        # reported outcome rather than a failed step in someone's review.
        print(f"Style status: FAILED: {exc.__class__.__name__}: {exc}")
        print(ADVISORY_NOTE)
        return 0


if __name__ == "__main__":
    sys.exit(main())
