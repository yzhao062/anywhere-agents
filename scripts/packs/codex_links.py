"""Codex skill links: a derived view of pack and local skills under ``.agents/skills/``.

Codex discovers repository skills only in ``.agents/skills/``, which the
composer never writes. After each committed compose, :func:`sync_links`
links ``.agents/skills/<name>`` for every skill whose effective hosts
include ``codex`` (``../../.claude/skills/<name>``) and every project-local
skill under ``skills/<name>/SKILL.md`` (``../../skills/<name>``), with
local skills taking precedence when a name appears in both sets, only where
that is provably safe. :func:`prune_links` is the uninstall counterpart: it
removes recorded links whose target is gone (or all local-form links when
``remove_local`` is set).

The links sit outside pack-lock. Their ownership record is the nested,
self-ignoring ``.agents/skills/.gitignore``: a name is ours only when that
file listed it at the start of a run, or when the run created the link.
Every other entry (tracked, case-variant, occupied, or an unlisted link) is
left alone and reported. Nothing here resolves a link before deciding what
it is, and removal is always ``os.unlink`` on the link itself, so a link can
never lead a write or delete into ``.claude/skills`` or ``skills``.

Links are created only on macOS and Linux in this release. Elsewhere the
compose summary says so whenever pack or local Codex-eligible skills exist.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

MANAGED_HEADER = "# Managed by anywhere-agents; regenerated on every compose."
_SELF_RULE = "/.gitignore"
_TEMP_RULE = "/.gitignore.*.tmp"
IGNORE_FILE = ".gitignore"

# An eligible name is a single path component that can never be the ignore
# file or one of its temp files (no leading dot).
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Temp files come from tempfile.mkstemp(prefix=".gitignore.", suffix=".tmp").
_TEMP_RE = re.compile(r"\.gitignore\..+\.tmp")

GIT_TIMEOUT_SECONDS = 20

OCCUPIED_STEP = (
    "Codex still reads .agents/skills/<name>; move it aside and the next "
    "bootstrap links the current skill."
)

Runner = Callable[..., Any]


def link_target(name: str, *, local: bool = False) -> str:
    """Return the relative target text of the link for ``name``."""
    if local:
        return f"../../skills/{name}"
    return f"../../.claude/skills/{name}"


def _current_platform() -> str:
    """Return ``sys.platform``; tests patch this seam, never ``sys`` itself."""
    return sys.platform


def platform_supported(platform: str | None = None) -> bool:
    """Links are created on macOS and Linux only in this release."""
    current = _current_platform() if platform is None else platform
    return current == "darwin" or current.startswith("linux")


@dataclass
class LinkReport:
    """What one reconciliation pass did, for the compose summary."""

    linked: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    retargeted: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    left_alone: list[tuple[str, str]] = field(default_factory=list)
    record_removed: bool = False
    skipped: str | None = None
    deferred: list[str] = field(default_factory=list)
    notice: str | None = None

    def summary_line(self) -> str | None:
        """Return the one summary line, or ``None`` when there is nothing to say."""
        if self.notice:
            return self.notice
        if self.skipped:
            line = f"Codex skill links: no change ({self.skipped})"
            if self.deferred:
                line += f"; deferred: {', '.join(self.deferred)}"
            return line + "."
        if not (self.linked or self.retargeted or self.removed or self.left_alone):
            return None
        parts = [f"{len(self.linked)} linked"]
        if self.created:
            parts.append(f"{len(self.created)} new")
        if self.retargeted:
            parts.append(f"retargeted {', '.join(self.retargeted)}")
        if self.removed:
            parts.append(f"removed {', '.join(self.removed)}")
        line = "Codex skill links: " + ", ".join(parts)
        if self.left_alone:
            shown = ", ".join(f"{name} ({reason})" for name, reason in self.left_alone)
            line += f"; left alone: {shown}"
            if any(reason == "occupied" for _name, reason in self.left_alone):
                line += f". {OCCUPIED_STEP}"
        return line + ("" if line.endswith(".") else ".")


@dataclass(frozen=True)
class _Tracking:
    parents: bool
    names: frozenset


def _lstat(path: Path) -> os.stat_result | None:
    """``os.lstat`` that reports a missing path, or a file in a parent position, as ``None``."""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _is_real_dir(st: os.stat_result | None) -> bool:
    return st is not None and stat.S_ISDIR(st.st_mode)


def _read_link_text(path: Path) -> str | None:
    st = _lstat(path)
    if st is None or not stat.S_ISLNK(st.st_mode):
        return None
    return os.readlink(path)


def _is_legal_link(path: Path, name: str) -> bool:
    text = _read_link_text(path)
    return text in (link_target(name), link_target(name, local=True))


def _is_local_skill_dir(skill_dir: Path) -> bool:
    if not _is_real_dir(_lstat(skill_dir)):
        return False
    md_st = _lstat(skill_dir / "SKILL.md")
    return md_st is not None and stat.S_ISREG(md_st.st_mode)


def _is_local_skill(root: Path, name: str) -> bool:
    if not _NAME_RE.fullmatch(name):
        return False
    skills_dir = root / "skills"
    if not _is_real_dir(_lstat(skills_dir)):
        return False
    return _is_local_skill_dir(skills_dir / name)


def local_skill_names(root: Path) -> list[str]:
    """Return sorted project-local skill names under ``<root>/skills``."""
    skills_dir = root / "skills"
    if not _is_real_dir(_lstat(skills_dir)):
        return []
    try:
        entries = os.listdir(skills_dir)
    except (FileNotFoundError, NotADirectoryError):
        return []
    return [
        name
        for name in sorted(entries)
        if _NAME_RE.fullmatch(name) and _is_local_skill_dir(skills_dir / name)
    ]


def _read_tracking(root: Path, run: Runner) -> _Tracking | None:
    """Return the tracked parents and names under ``.agents``, or ``None`` when unknown.

    Git runs with ``LC_ALL=C`` so the "not a git repository" message is not
    translated. The ``:(icase)`` pathspec also lists a tracked parent spelled
    in another case, which an ordinary pathspec misses.
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    base = ["git", "-C", str(root)]
    try:
        probe = run(base + ["rev-parse", "--is-inside-work-tree"],
                    capture_output=True, env=env, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        if b"not a git repository" in (probe.stderr or b""):
            return _Tracking(False, frozenset())
        return None
    if (probe.stdout or b"").strip() != b"true":
        return None
    try:
        listing = run(base + ["ls-files", "-z", "--", ":(icase).agents"],
                      capture_output=True, env=env, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if listing.returncode != 0:
        return None
    parents = False
    names = set()
    for raw in (listing.stdout or b"").split(b"\0"):
        if not raw:
            continue
        parts = os.fsdecode(raw).split("/")
        folded = [part.casefold() for part in parts]
        if folded[0] != ".agents":
            continue
        if len(parts) == 1:
            parents = True
        elif folded[1] != "skills":
            continue
        elif len(parts) == 2:
            parents = True
        else:
            names.add(folded[2])
    return _Tracking(parents, frozenset(names))


def _spelling_problem(directory: Path, expected: str) -> str | None:
    """Report an entry of ``directory`` that equals ``expected`` only after casefold."""
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return None
    for entry in entries:
        if entry != expected and entry.casefold() == expected.casefold():
            return f"{entry} differs from {expected} only by case"
    return None


def _read_record(path: Path) -> tuple[list[str] | None, str | None]:
    """Parse the ownership file; ``([], None)`` when it is absent.

    The whole file must match the managed format: the header, ``/.gitignore``
    and ``/.gitignore.*.tmp`` once each, and otherwise unique ``/<name>``
    lines with valid names. Anything else makes the record untrustworthy.
    """
    st = _lstat(path)
    if st is None:
        return [], None
    if not stat.S_ISREG(st.st_mode):
        return None, ".agents/skills/.gitignore is not a regular file"
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f".agents/skills/.gitignore is unreadable: {exc}"
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != MANAGED_HEADER:
        return None, ".agents/skills/.gitignore is not managed by anywhere-agents"
    names: list[str] = []
    rules = {_SELF_RULE: 0, _TEMP_RULE: 0}
    for line in lines[1:]:
        if line in rules:
            rules[line] += 1
            continue
        name = line[1:]
        if not line.startswith("/") or not _NAME_RE.fullmatch(name) or name in names:
            return None, f".agents/skills/.gitignore is malformed at {line!r}"
        names.append(name)
    if any(count != 1 for count in rules.values()):
        return None, ".agents/skills/.gitignore is malformed (self rules)"
    return names, None


def _render_record(names: Iterable[str]) -> bytes:
    lines = [MANAGED_HEADER, _SELF_RULE, _TEMP_RULE]
    lines += [f"/{name}" for name in sorted(set(names))]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_record(skills: Path, names: Iterable[str], *, force: bool = False) -> None:
    """Write the ownership file atomically.

    An unchanged file is skipped unless ``force`` is set; the pass forces the
    write that precedes any link change, so an unwritable record stops the
    pass before a link is created or removed.
    """
    path = skills / IGNORE_FILE
    data = _render_record(names)
    try:
        if not force and path.read_bytes() == data:
            return
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(skills), prefix=IGNORE_FILE + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _remove_stale_temps(skills: Path, tracking: _Tracking) -> None:
    for entry in os.listdir(skills):
        if not _TEMP_RE.fullmatch(entry) or entry.casefold() in tracking.names:
            continue
        st = _lstat(skills / entry)
        if st is not None and stat.S_ISREG(st.st_mode):
            try:
                os.unlink(skills / entry)
            except OSError:
                pass


def _precheck(root: Path, run: Runner) -> tuple[_Tracking | None, list[str] | None, str | None]:
    """Run the read-only checks; return (tracking, recorded names, problem)."""
    agents = root / ".agents"
    skills = agents / "skills"
    for directory in (agents, skills):
        st = _lstat(directory)
        if st is not None and not _is_real_dir(st):
            return None, None, f"{directory.relative_to(root)} is not a real directory"
    ignore_st = _lstat(skills / IGNORE_FILE)
    if ignore_st is not None and not stat.S_ISREG(ignore_st.st_mode):
        return None, None, ".agents/skills/.gitignore is not a regular file"
    problem = _spelling_problem(root, ".agents")
    if problem is None and _lstat(agents) is not None:
        problem = _spelling_problem(agents, "skills")
    if problem:
        return None, None, problem
    tracking = _read_tracking(root, run)
    if tracking is None:
        return None, None, "git tracking could not be read"
    if tracking.parents:
        return None, None, ".agents or .agents/skills is tracked by git"
    if IGNORE_FILE in tracking.names:
        return None, None, ".agents/skills/.gitignore is tracked by git"
    recorded, problem = _read_record(skills / IGNORE_FILE)
    if problem:
        return None, None, problem
    return tracking, recorded, None


def _fold_map(entries: Iterable[str]) -> dict[str, list[str]]:
    folded: dict[str, list[str]] = {}
    for entry in entries:
        folded.setdefault(entry.casefold(), []).append(entry)
    return folded


def _ensure_dir(directory: Path) -> str | None:
    if _lstat(directory) is None:
        try:
            os.mkdir(directory)
        except FileExistsError:
            pass
    if not _is_real_dir(_lstat(directory)):
        return f"{directory.name} could not be created as a real directory"
    return None


def sync_links(
    root: Path,
    eligible: Iterable[str],
    *,
    local: Iterable[str] | None = None,
    platform: str | None = None,
    run: Runner = subprocess.run,
) -> LinkReport:
    """Reconcile ``.agents/skills`` links with pack-deployed and local skills."""
    report = LinkReport()
    current = _current_platform() if platform is None else platform
    eligible_set = set(eligible)
    if not platform_supported(current):
        if eligible_set:
            label = "Windows" if current == "win32" else current
            report.notice = f"Codex skill links are not created on {label} in this release."
            return report
        try:
            local_set = set(local_skill_names(root) if local is None else local)
        except OSError as exc:
            report.skipped = f"filesystem error: {exc}"
            return report
        if local_set:
            label = "Windows" if current == "win32" else current
            report.notice = f"Codex skill links are not created on {label} in this release."
        return report
    try:
        local_set = set(local_skill_names(root) if local is None else local)
        wanted = sorted(eligible_set | local_set)
        valid: list[str] = []
        folds = _fold_map(name for name in wanted if _NAME_RE.fullmatch(name))
        for name in wanted:
            if not _NAME_RE.fullmatch(name):
                report.left_alone.append((name, "unsupported name"))
            elif len(folds[name.casefold()]) > 1:
                report.left_alone.append((name, "differs only by case from another skill"))
            else:
                valid.append(name)
        _sync(root, valid, local_set, report, run)
    except OSError as exc:
        report.skipped = f"filesystem error: {exc}"
    return report


def _sync(
    root: Path,
    valid: list[str],
    local_set: set[str],
    report: LinkReport,
    run: Runner,
) -> None:
    agents = root / ".agents"
    skills = agents / "skills"
    tracking, recorded, problem = _precheck(root, run)
    if problem:
        report.skipped = problem
        report.deferred = list(valid)
        return
    assert tracking is not None and recorded is not None
    skills_exists = _lstat(skills) is not None
    if not skills_exists:
        # Nothing is recorded without the directory; create it only when a
        # link is due.
        if not [name for name in valid if name.casefold() not in tracking.names]:
            report.left_alone += [(name, "tracked") for name in valid]
            return
        for directory in (agents, skills):
            problem = _ensure_dir(directory)
            if problem:
                report.skipped = problem
                report.deferred = list(valid)
                return
    _remove_stale_temps(skills, tracking)
    entries = os.listdir(skills)
    by_fold = _fold_map(entries)
    valid_set = set(valid)

    # Plan removals first (item 7) so a case-only rename replaces its link
    # in one run: a planned removal counts as absent while classifying.
    removals: list[str] = []
    for name in recorded:
        if name in valid_set:
            continue
        spellings = by_fold.get(name.casefold(), [])
        if name.casefold() in tracking.names:
            if spellings:
                report.left_alone.append((name, "tracked"))
        elif name in spellings and _is_legal_link(skills / name, name):
            removals.append(name)
        elif name in spellings:
            report.left_alone.append((name, "no longer eligible; not an exact link"))
    present = _fold_map(entry for entry in entries if entry not in removals)

    owned: list[str] = []
    to_create: list[str] = []
    to_retarget: list[str] = []
    for name in valid:
        spellings = present.get(name.casefold(), [])
        variants = [entry for entry in spellings if entry != name]
        if name.casefold() in tracking.names:
            report.left_alone.append((name, "tracked"))
        elif variants:
            report.left_alone.append((name, f"case collision with {variants[0]}"))
        elif name not in spellings:
            to_create.append(name)
        else:
            is_local = name in local_set
            text = _read_link_text(skills / name)
            wanted_target = link_target(name, local=is_local)
            other_target = link_target(name, local=not is_local)
            if text == wanted_target:
                if name in recorded:
                    owned.append(name)
                else:
                    report.left_alone.append((name, "working link, not owned"))
            elif text == other_target and name in recorded:
                to_retarget.append(name)
            else:
                report.left_alone.append((name, "occupied"))

    record_exists = _lstat(skills / IGNORE_FILE) is not None
    if to_create or to_retarget or removals:
        # Record prospective links before creating them, and prove the record
        # is writable before any link changes; a failure changes no link.
        try:
            _write_record(skills, list(recorded) + to_create, force=True)
        except OSError as exc:
            report.skipped = f"could not write .agents/skills/.gitignore: {exc}"
            report.deferred = to_create + to_retarget + removals
            report.linked = sorted(owned)
            return
        record_exists = True
    kept_for_retry: list[str] = []
    for name in removals:
        try:
            os.unlink(skills / name)
        except OSError as exc:
            kept_for_retry.append(name)
            report.left_alone.append((name, f"could not remove: {exc.strerror or exc}"))
        else:
            report.removed.append(name)
    for name in to_retarget:
        try:
            os.unlink(skills / name)
        except OSError as exc:
            kept_for_retry.append(name)
            report.left_alone.append((name, f"could not remove: {exc.strerror or exc}"))
            continue
        try:
            os.symlink(link_target(name, local=(name in local_set)), skills / name)
        except OSError as exc:
            kept_for_retry.append(name)
            report.left_alone.append((name, f"could not link: {exc.strerror or exc}"))
        else:
            report.retargeted.append(name)
            owned.append(name)
    for name in to_create:
        try:
            os.symlink(link_target(name, local=(name in local_set)), skills / name)
        except OSError as exc:
            report.left_alone.append((name, f"could not link: {exc.strerror or exc}"))
        else:
            report.created.append(name)
            owned.append(name)
    final = owned + kept_for_retry
    if record_exists or final:
        try:
            _write_record(skills, final)
        except OSError as exc:
            report.left_alone.append((IGNORE_FILE, f"could not rewrite: {exc.strerror or exc}"))
    report.linked = sorted(owned)


def has_record(root: Path, *, platform: str | None = None) -> bool:
    """Return whether the repository holds a managed Codex link record.

    Uninstall runs link cleanup only then. An ignore file that another tool
    or person wrote is not ours, so it neither triggers cleanup nor makes an
    uninstall partial.
    """
    if not platform_supported(platform):
        return False
    path = root / ".agents" / "skills" / IGNORE_FILE
    try:
        st = _lstat(path)
        if st is None or not stat.S_ISREG(st.st_mode):
            return False
        with open(path, "rb") as handle:
            first = handle.readline(len(MANAGED_HEADER) + 2)
    except OSError:
        return False
    return first.rstrip(b"\r\n") == MANAGED_HEADER.encode("utf-8")


def prune_links(
    root: Path,
    *,
    remove_record_when_empty: bool,
    remove_local: bool = False,
    platform: str | None = None,
    run: Runner = subprocess.run,
) -> LinkReport:
    """Remove recorded exact links whose target is gone (or local links when requested).

    Uninstall calls this on every run that finds the ownership file, whatever
    the pack-state result, so a retry after a crash still removes a dangling
    link. A pack-form link stays while ``.claude/skills/<name>`` exists; a
    local-form link is removed when ``remove_local`` is true or
    ``skills/<name>/SKILL.md`` is no longer a valid local skill, and stays
    otherwise. With ``remove_record_when_empty`` (uninstall-all), an empty
    record is removed, then ``.agents/skills`` and ``.agents`` if they are
    empty.
    """
    report = LinkReport()
    if not has_record(root, platform=platform):
        return report
    try:
        _prune(root, remove_record_when_empty, remove_local, report, run)
    except OSError as exc:
        report.skipped = f"filesystem error: {exc}"
    return report


def _prune(
    root: Path,
    remove_record_when_empty: bool,
    remove_local: bool,
    report: LinkReport,
    run: Runner,
) -> None:
    agents = root / ".agents"
    skills = agents / "skills"
    tracking, recorded, problem = _precheck(root, run)
    if problem:
        report.skipped = problem
        return
    assert tracking is not None and recorded is not None
    _remove_stale_temps(skills, tracking)
    by_fold = _fold_map(os.listdir(skills))
    kept: list[str] = []
    dangling: list[str] = []
    for name in recorded:
        spellings = by_fold.get(name.casefold(), [])
        if name.casefold() in tracking.names or name not in spellings:
            continue
        text = _read_link_text(skills / name)
        if text == link_target(name):
            if _lstat(root / ".claude" / "skills" / name) is not None:
                kept.append(name)
            else:
                dangling.append(name)
        elif text == link_target(name, local=True):
            if not remove_local and _is_local_skill(root, name):
                kept.append(name)
            else:
                dangling.append(name)
        else:
            report.left_alone.append((name, "not an exact link"))
    if dangling:
        # Prove the record is writable before any link changes, as the
        # compose pass does; a failure here removes nothing.
        _write_record(skills, recorded, force=True)
    for name in dangling:
        try:
            os.unlink(skills / name)
        except OSError as exc:
            kept.append(name)
            report.left_alone.append((name, f"could not remove: {exc.strerror or exc}"))
        else:
            report.removed.append(name)
    report.linked = sorted(kept)
    if remove_record_when_empty and not kept:
        os.unlink(skills / IGNORE_FILE)
        report.record_removed = True
        for directory in (skills, agents):
            try:
                os.rmdir(directory)
            except OSError:
                break
        return
    _write_record(skills, kept)
