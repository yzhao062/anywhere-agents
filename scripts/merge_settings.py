"""Merge a shared settings.json into a target settings.json, in place.

Both bootstrap entry points call this, so the merge semantics and the on-disk
format have one implementation instead of two. The PowerShell entry point used
`ConvertTo-Json`, whose indentation differs between Windows PowerShell 5.1 and
PowerShell 7 and matches neither `json.dumps`, so the same file was reformatted
by whichever machine touched it last. See anywhere-agents#36.

The target is user-level state shared by every consumer repo on the machine, so
two things guard it (anywhere-agents#32). The merged object is published by
rename rather than written over the target, because an interrupted in-place
write leaves a truncated file, and one such truncation cost a maintainer six
user-only keys that no backup on disk carried. A lock serializes the whole
read-merge-backup-write sequence, because a session-start burst runs one
bootstrap per consumer repo and they all write this one file. For the user-level
target that lock is the composer's per-user lock, which the composer's own
permission handler already takes; every other target gets a sibling lock. The previous
content is copied aside first, under a capped history, so a loss caused by
something outside this script stays recoverable.

A list of scalars merges by union, so a rule that the shared file stops
shipping would stay on every machine that once merged it. `--retire-withdrawn`
removes the rules listed in `WITHDRAWN` after the merge. Bootstrap passes it for
the user-level target only, so a project's own settings keep whatever they list.

Usage:
    merge_settings.py [--retire-withdrawn] <target.json> <shared.json>

Exit codes:
    0  merged, or nothing to do
    1  a file could not be read, parsed, merged, or published
    2  bad usage
    3  another writer held the lock; the target was left alone
"""
from __future__ import annotations

import errno
import json
import os
import pathlib
import sys
import time
from contextlib import contextmanager
from typing import Iterator

# Timestamped copies of the previous content, newest kept. Ten covers a burst
# of consumer-repo bootstraps plus several days of ordinary runs, and costs a
# few hundred kilobytes. The loss this guards against went unnoticed for 78
# days, which is why the history is bounded by count rather than by age.
BACKUP_KEEP = 10

# A peer bootstrap holds the lock only for the length of one merge, so this
# covers a queue of consumer repos without hiding a genuinely stuck holder.
LOCK_TIMEOUT_SECONDS = 60.0
LOCK_POLL_SECONDS = 0.1

# The only errors that mean "this filesystem cannot lock", rather than "a peer
# is holding it". A sharing violation or a permission error is a peer to wait
# for, so neither belongs here.
_UNSUPPORTED_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ("ENOSYS", "ENOTSUP", "EOPNOTSUPP", "ENOLCK", "EINVAL")
    if hasattr(errno, name)
)

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl


class LockBusy(Exception):
    """Another process held the lock for longer than the timeout."""


def _try_lock(fd: int) -> bool:
    """Take a non-blocking exclusive lock, or report that a peer holds it.

    Contention is a return value; every other error is raised, so a filesystem
    that cannot lock at all reaches the caller instead of being read as a peer.
    """
    if _IS_WINDOWS:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in (errno.EDEADLK, errno.EACCES):
                return False
            raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
            return False
        raise


def _unlock(fd: int) -> None:
    try:
        if _IS_WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        # The OS releases on close and on process exit regardless.
        pass


def _unsupported(exc: OSError) -> bool:
    """True only for an error that means this filesystem cannot lock at all.

    Everything else, a sharing violation and a permission error included, is a
    peer to wait for. Treating any failure as "no locking here" was how a
    Windows peer holding the file with exclusive sharing turned into an
    unlocked merge that reported success.
    """
    return exc.errno in _UNSUPPORTED_ERRNOS


def lock_path_for(target: pathlib.Path) -> pathlib.Path:
    """Pick the lock that every writer of ``target`` agrees on.

    ``~/.claude/settings.json`` has a second writer: the composer's permission
    handler stages a merge into it and publishes through its transaction, under
    the per-user lock its own module documents. A sibling lock would not exclude
    that writer, and a probe showed the resulting interleaving dropping a
    committed permission with exit 0. Every other target, the project-level
    settings file included, keeps a sibling lock, because the per-user lock has
    nothing to do with a file inside one repository.

    The lock name is taken from the canonical home path rather than from a
    resolved leaf, so a symlinked settings file still names the right directory.
    A target that cannot be resolved at all raises, because a guess between the
    two locks is the case that loses a write.
    """
    user_settings = pathlib.Path.home() / ".claude" / "settings.json"
    user_lock = user_settings.with_name(".pack-lock.lock")
    if target.absolute() == user_settings.absolute():
        return user_lock
    try:
        is_user_settings = target.resolve() == user_settings.resolve()
    except OSError as exc:
        # Not knowing which file this is means not knowing which lock guards it.
        # Guessing the sibling lock here published over a composer commit.
        raise LockBusy(
            "cannot determine settings lock for %s: %s" % (target, exc)
        ) from exc
    if is_user_settings:
        return user_lock
    return target.with_name(target.name + ".lock")


@contextmanager
def settings_lock(target: pathlib.Path) -> Iterator[bool]:
    """Serialize writers to ``target`` through the lock they all agree on.

    Yields True while the lock is held, and False only when this filesystem
    reports that locking is unsupported. That case still runs the merge:
    publishing by rename already rules out the truncation that made
    anywhere-agents#32 expensive, and refusing to bootstrap on an exotic
    filesystem would trade a rare lost update for a certain failure. Contention
    is the case the lock exists for, so it waits and then raises.
    """
    lock_path = lock_path_for(target)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    fd = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            if _unsupported(exc):
                yield False
                return
            # A peer can hold the lock file with sharing flags that refuse a
            # second open, so a failure here is contention until the deadline.
            if time.monotonic() >= deadline:
                raise LockBusy(
                    "could not open %s within %.0fs: %s"
                    % (lock_path, LOCK_TIMEOUT_SECONDS, exc)
                )
            time.sleep(LOCK_POLL_SECONDS)
    try:
        while True:
            try:
                if _try_lock(fd):
                    break
            except OSError as exc:
                if _unsupported(exc):
                    yield False
                    return
                raise
            if time.monotonic() >= deadline:
                raise LockBusy(
                    "another process held %s for more than %.0fs"
                    % (lock_path, LOCK_TIMEOUT_SECONDS)
                )
            time.sleep(LOCK_POLL_SECONDS)
        try:
            yield True
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def deep_merge(base: dict, over: dict) -> None:
    """Merge `over` into `base`.

    Kept byte-compatible with the inline program this replaced: a dict merges
    recursively; a list of objects replaces; a list of scalars appends with
    duplicates dropped and first-seen order kept; anything else overwrites.
    """
    for key, value in over.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            deep_merge(base[key], value)
        elif key in base and isinstance(base[key], list) and isinstance(value, list):
            if value and isinstance(value[0], dict):
                base[key] = value
            else:
                base[key] = list(dict.fromkeys(base[key] + value))
        else:
            base[key] = value


# Rules an earlier `user/settings.json` shipped and a later release withdrew,
# keyed by their path in the settings object. Only these exact strings are
# removed, and only while the shared file does not list them again. To keep one
# of them, put it in a project's `.claude/settings.local.json`, or delete it
# from this table in a fork.
WITHDRAWN = {
    # 2026-11-22: approval moved to the point where work leaves the machine
    # (git push, gh actions that change GitHub state, package publishes).
    # Local git steps and recursive deletes no longer ask.
    ("permissions", "ask"): (
        "Bash(git commit *)",
        "Bash(git reset --hard *)",
        "Bash(git checkout -- *)",
        "Bash(git clean *)",
        "Bash(git branch -D *)",
        "Bash(git branch -d *)",
        "Bash(git branch --delete *)",
        "Bash(git stash drop *)",
        "Bash(git stash clear *)",
        "Bash(git tag -d *)",
        "Bash(git merge *)",
        "Bash(git rebase *)",
        "Bash(rm -rf *)",
        "Bash(rm -fr *)",
        "Bash(rm -r -f *)",
    ),
}


def retire_withdrawn(target: dict, shared: dict) -> None:
    """Remove the `WITHDRAWN` rules from `target` in place, except any rule the
    shared file still lists at the same path."""
    for keys, rules in WITHDRAWN.items():
        node, shared_node = target, shared
        for key in keys[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            shared_node = shared_node.get(key) if isinstance(shared_node, dict) else None
        if not isinstance(node, dict) or not isinstance(node.get(keys[-1]), list):
            continue
        shipped = shared_node.get(keys[-1]) if isinstance(shared_node, dict) else None
        shipped = shipped if isinstance(shipped, list) else []
        drop = {rule for rule in rules if rule not in shipped}
        node[keys[-1]] = [
            value for value in node[keys[-1]]
            if not (isinstance(value, str) and value in drop)
        ]


def read_json(path: pathlib.Path):
    """Read JSON as UTF-8 whatever the machine's locale says.

    Text mode picks the ANSI codepage on Windows, which is cp1252 on a default
    install, and these files carry non-ASCII. `utf-8-sig` also heals a copy
    left with a BOM by an earlier `Set-Content -Encoding UTF8`.
    """
    return json.loads(path.read_bytes().decode("utf-8-sig"))


def backup_name(path: pathlib.Path, when: float) -> str:
    """Name a copy so that lexical order within one directory is time order.

    A burst of consumer-repo bootstraps writes several copies inside the same
    second, so a whole-second stamp would leave the prune ordering them by PID
    and dropping an arbitrary one. The microseconds settle the order; the PID
    only keeps two copies from colliding on one name.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(when))
    micros = int((when - int(when)) * 1_000_000)
    return "%s.bak-%s-%06d-%d" % (path.name, stamp, micros, os.getpid())


def snapshot(path: pathlib.Path) -> pathlib.Path | None:
    """Copy the current content aside, then prune to the newest BACKUP_KEEP.

    Returns the copy, or None when there was nothing to copy or the copy could
    not be written. A failed backup does not stop the merge: the publish below
    is atomic either way, and refusing to run because a spare copy could not be
    made would turn a recovery aid into a new way to fail.
    """
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    if not payload:
        return None
    copy = path.with_name(backup_name(path, time.time()))
    try:
        copy.write_bytes(payload)
    except OSError:
        return None
    prefix = path.name + ".bak-"
    try:
        existing = sorted(
            (p for p in path.parent.iterdir() if p.name.startswith(prefix)),
            key=lambda p: p.name,
        )
    except OSError:
        return copy
    for stale in existing[:-BACKUP_KEEP]:
        try:
            stale.unlink()
        except OSError:
            pass
    return copy


# Windows refuses a rename while any process holds the destination open without
# delete sharing, and a scanner or indexer touching the file for a few
# milliseconds is enough. Observed as ERROR_ACCESS_DENIED during a six-writer
# run. Failing the merge for that would report a loss that did not happen, so
# the publish waits briefly for the other reader to let go.
_TRANSIENT_WINERRORS = frozenset((5, 32))
REPLACE_RETRY_SECONDS = 2.0
REPLACE_RETRY_INTERVAL = 0.05


def _replace_with_retry(source: pathlib.Path, target: pathlib.Path) -> None:
    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            transient = getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS
            if not transient or time.monotonic() >= deadline:
                raise
            time.sleep(REPLACE_RETRY_INTERVAL)


def canonical_bytes(data) -> bytes:
    """The exact bytes a publish writes: UTF-8, no BOM, LF, trailing newline."""
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_json(path: pathlib.Path, data) -> None:
    """Publish the canonical form by rename: UTF-8, no BOM, LF, trailing newline.

    The bytes are parsed back before anything touches the target, so a value
    that serializes without round-tripping fails while the old content is still
    in place. The temporary file is a sibling, because rename is atomic only
    within one filesystem.
    """
    payload = canonical_bytes(data)
    json.loads(payload.decode("utf-8"))
    tmp = path.with_name(".%s.merge-%d.tmp" % (path.name, os.getpid()))
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def merge_into(target: pathlib.Path, shared: pathlib.Path, retire: bool = False) -> int:
    """Read both files, merge, back up, and publish. Returns an exit code.

    With `retire`, the `WITHDRAWN` rules are removed after the merge."""
    try:
        shared_data = read_json(shared)
    except Exception as exc:
        sys.stderr.write("merge_settings: cannot read %s: %s\n" % (shared, exc))
        return 1
    try:
        target_data = read_json(target)
    except FileNotFoundError:
        # First install. Creating here keeps it inside the lock, so a composer
        # committing a permission at the same moment cannot be overwritten by a
        # caller that checked for absence outside the critical section.
        target_data = {}
    except Exception as exc:
        sys.stderr.write("merge_settings: cannot read %s: %s\n" % (target, exc))
        return 1
    if not isinstance(target_data, dict) or not isinstance(shared_data, dict):
        sys.stderr.write("merge_settings: both files must hold a JSON object\n")
        return 1
    try:
        deep_merge(target_data, shared_data)
    except TypeError as exc:
        # An array holding a scalar first and an object later reaches
        # dict.fromkeys with an unhashable element. Reading only the first
        # element to choose the branch is deliberate parity with the inline
        # program this replaced, and the PowerShell fallback survives the same
        # input, so the two entry points disagree here. Report it and leave the
        # file alone rather than inventing a merge the other side would not
        # produce. Without this the run printed a traceback and the Bash entry
        # point, which does not read the exit code, carried on regardless.
        sys.stderr.write("merge_settings: cannot merge %s: %s\n" % (target, exc))
        return 1
    if retire:
        retire_withdrawn(target_data, shared_data)
    if not target_data:
        # A merge that produced nothing is a defect rather than a state worth
        # persisting; leaving the file alone keeps the old content readable.
        sys.stderr.write("merge_settings: refusing to write an empty object to %s\n" % target)
        return 1
    try:
        if target.read_bytes() == canonical_bytes(target_data):
            # The merge reproduced the bytes already on disk, so there is
            # nothing to publish and nothing worth keeping a copy of. Writing
            # anyway is how 27 consumer bootstraps left 27 identical backups
            # and pushed the recovery history #32 asked for out of the
            # BACKUP_KEEP window (anywhere-agents#58).
            return 0
    except (OSError, ValueError):
        # Unreadable or absent: fall through and publish, which is the path a
        # first install takes. ValueError covers the serializer as well, since
        # a target holding an escaped unpaired surrogate parses back and then
        # fails to encode. Before this guard existed that failure landed in the
        # publish handler below and printed a diagnostic, so catching only
        # OSError here would turn it into a traceback.
        pass
    snapshot(target)
    try:
        write_json(target, target_data)
    except (OSError, ValueError) as exc:
        sys.stderr.write("merge_settings: cannot publish %s: %s\n" % (target, exc))
        return 1
    return 0


def main(argv: list[str]) -> int:
    args = argv[1:]
    retire = bool(args) and args[0] == "--retire-withdrawn"
    if retire:
        args = args[1:]
    if len(args) != 2:
        sys.stderr.write(
            "usage: merge_settings.py [--retire-withdrawn] <target.json> <shared.json>\n"
        )
        return 2
    target = pathlib.Path(args[0])
    shared = pathlib.Path(args[1])
    try:
        with settings_lock(target) as held:
            if not held:
                sys.stderr.write(
                    "merge_settings: %s does not support locking; "
                    "merging without it\n" % target.parent
                )
            return merge_into(target, shared, retire)
    except LockBusy as exc:
        sys.stderr.write("merge_settings: %s\n" % exc)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
