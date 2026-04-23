"""Cross-platform file locks for pack lifecycle operations (v0.4.0 Phase 2).

The composer acquires two locks during every lifecycle operation
(install / update / uninstall / re-stamp / reconciliation):

- **Per-user lock** (``~/.claude/.pack-lock.lock``) — serializes access to
  user-level state (``~/.claude/pack-state.json``, ``~/.claude/hooks/``,
  ``~/.claude/settings.json``). Two Claude Code sessions bootstrapping in
  two different consumer repos contend only here.
- **Per-repo lock** (``<project>/.agent-config/.pack-lock.lock``) —
  serializes access to project-local state. Uncontended across sessions
  in different consumer repos.

Both locks surface the same ``LockTimeout`` on contention after the
30-second default. The holder PID is recorded inside the lock file on
acquire so ``LockTimeout`` can name the holder when available; on Windows
the holder PID cannot always be proven (``msvcrt.locking`` gives no
portable holder query), in which case reconciliation treats the lock as
busy-advisory per pack-architecture.md § "Pack lifecycle operations".

Lock release happens when the file descriptor closes, which is guaranteed
by the context manager on normal exit and by OS cleanup on process exit
(including SIGKILL / TerminateProcess). A crashed composer does not leave
a lock held across process boundaries.
"""
from __future__ import annotations

import errno
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

# Default acquire timeout. 30 seconds is long enough to wait out a peer
# composer's normal lifecycle operation and short enough to surface a
# stuck lock as an actionable error.
DEFAULT_TIMEOUT_SECONDS = 30.0

# Polling interval while waiting for the lock. Short enough to feel
# responsive on a workstation; long enough not to spin CPU.
POLL_INTERVAL_SECONDS = 0.1

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import msvcrt  # type: ignore[import-not-found]
else:
    import fcntl  # type: ignore[import-not-found]


class LockTimeout(TimeoutError):
    """Acquire did not succeed before the timeout elapsed.

    Carries the lock path + holder-PID hint (``None`` if the holder
    identity could not be read from the lock file) so callers can surface
    an actionable message.
    """

    def __init__(
        self, path: Path, timeout: float, holder_pid: int | None
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.holder_pid = holder_pid
        holder_note = (
            f" (held by PID {holder_pid})" if holder_pid is not None else ""
        )
        super().__init__(
            f"could not acquire lock {path} within {timeout:.1f}s{holder_note}"
        )


def _try_lock_fd(fd: int) -> bool:
    """Attempt a non-blocking exclusive lock on ``fd``. Return True on
    success, False if another process holds the lock. Reraise any other
    OS error."""
    if _IS_WINDOWS:
        # msvcrt.locking needs a byte range. We lock one byte at offset 0
        # from the current position; seek(0) first so the lock is
        # deterministic regardless of what we've written.
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            # Windows raises errno EDEADLK / EACCES when the region is
            # already locked. Other codes are real errors.
            if exc.errno in (errno.EDEADLK, errno.EACCES):
                return False
            raise
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                return False
            raise


def _release_lock_fd(fd: int) -> None:
    """Release the exclusive lock on ``fd`` (best-effort).

    The OS also releases on ``close(fd)`` / process exit; explicit release
    lets a long-lived process acquire-release multiple times without
    spawning fresh fds.
    """
    try:
        if _IS_WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        # Unlock on a closed fd or unsupported filesystem should not block
        # cleanup. The OS releases on fd close regardless.
        pass


def _pid_sidecar_for(lock_path: Path) -> Path:
    """Return the sidecar PID file path for ``lock_path``.

    Separate file so readers can get holder PID even while the lock byte
    range on the main lock file is held. On Windows ``msvcrt.locking`` is
    a mandatory byte-range lock that blocks reads within the range — a
    peer trying to read the lock file directly would get PermissionError.
    """
    return lock_path.with_name(lock_path.name + ".pid")


def _read_holder_pid(lock_path: Path) -> int | None:
    """Read the holder PID recorded in the sidecar file, if any.

    Returns ``None`` on any read failure (sidecar missing, empty,
    non-int, I/O error). Callers treat ``None`` as "holder unknown"; it
    must not be conflated with "no holder".
    """
    sidecar = _pid_sidecar_for(lock_path)
    try:
        raw = sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return None


@contextmanager
def acquire(
    path: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> Iterator[IO[str]]:
    """Acquire an exclusive file lock at ``path``; release on context exit.

    Parameters
    ----------
    path
        Absolute path to the lock file. Parent directory must exist or be
        creatable; the lock file itself is created if absent.
    timeout
        Maximum seconds to wait for the lock before raising ``LockTimeout``.
        Defaults to 30 seconds.

    Yields
    ------
    file object
        The underlying text-mode file handle. Callers rarely need to
        touch it; the contract is the acquire / release, not I/O on the
        lock file itself. The file is positioned at byte 0 after acquire.

    Raises
    ------
    LockTimeout
        If the lock is still held by another process after ``timeout``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout

    # Open in text mode for PID write; re-use the same fd for locking.
    # On Windows, opening with "r+" on a missing file raises; fall back
    # to "w+" to create, then re-open "r+" so read-back still works.
    try:
        fh = open(path, "r+", encoding="utf-8")
    except FileNotFoundError:
        fh = open(path, "w+", encoding="utf-8")

    try:
        while True:
            if _try_lock_fd(fh.fileno()):
                break
            if time.monotonic() >= deadline:
                holder = _read_holder_pid(path)
                raise LockTimeout(path, timeout, holder)
            time.sleep(POLL_INTERVAL_SECONDS)

        # Record holder PID in a sidecar file so peers can read it without
        # contending the locked byte range. Replace atomically so a stale
        # PID from a prior holder never overlaps.
        sidecar = _pid_sidecar_for(path)
        try:
            tmp_sidecar = sidecar.with_suffix(sidecar.suffix + ".tmp")
            tmp_sidecar.write_text(f"{os.getpid()}\n", encoding="utf-8")
            os.replace(str(tmp_sidecar), str(sidecar))
        except OSError:
            # Failing to write the sidecar is non-fatal; the lock itself is
            # still held. Holder identity will just be unknown to peers.
            pass

        yield fh
    finally:
        # Clear the sidecar before releasing the lock so a racing peer
        # doesn't read a stale PID between release and next acquire.
        sidecar = _pid_sidecar_for(path)
        try:
            sidecar.unlink()
        except OSError:
            pass
        _release_lock_fd(fh.fileno())
        try:
            fh.close()
        except OSError:
            pass


def is_held(path: Path) -> bool:
    """Best-effort probe: is the lock at ``path`` currently held?

    Returns ``True`` if another process is holding the lock (contention
    detected on a non-blocking probe). Returns ``False`` if we could
    acquire + immediately release the lock, or if the lock file does not
    exist at all.

    Used by reconciliation to distinguish a live transaction (contention)
    from an orphan (no contention). Per pack-architecture.md § "Atomicity
    contract", contention is **authoritative**: the caller must treat
    ``True`` as "live, skip and retry next startup" even on Windows
    where the holder PID cannot always be proven separately.
    """
    if not path.exists():
        return False
    try:
        fh = open(path, "r+", encoding="utf-8")
    except (FileNotFoundError, PermissionError):
        # Windows can deny open while another process holds the file
        # exclusively; treat that as "held" for reconciliation purposes.
        return True
    try:
        if _try_lock_fd(fh.fileno()):
            _release_lock_fd(fh.fileno())
            return False
        return True
    finally:
        try:
            fh.close()
        except OSError:
            pass


def user_lock_path(home: Path | None = None) -> Path:
    """Return the canonical per-user lock file path."""
    if home is None:
        home = Path.home()
    return home / ".claude" / ".pack-lock.lock"


def repo_lock_path(project_root: Path) -> Path:
    """Return the canonical per-repo lock file path for a consumer repo."""
    return project_root / ".agent-config" / ".pack-lock.lock"
