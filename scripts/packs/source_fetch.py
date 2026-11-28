"""Source fetch v0.5.0 - fetch a pack archive from a URL with auth chain.

Phase 3 replaces the Phase 2 stub with the full
validate->resolve->compare->cache->fetch pipeline:

1. ``reject_credential_url`` (parse-time, no network).
2. ``_resolve_ref_cached`` -> resolved sha. A miss calls
   ``resolve_ref_with_auth_chain`` once, which itself tries each
   available auth method until one succeeds; a hit inside an open
   cache scope makes no network call.
3. Compare the resolved sha against the recorded pack-lock commit;
   under ``update_policy=locked`` drift raises ``PackLockDriftError``.
4. Cache lookup at ``(canonical_id_or_url_hash, resolved_commit)``;
   integrity check via ``dir-sha256``.
5. Cache miss -> ``fetch_with_auth_chain`` into staging; rename
   atomically into the cache slot.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import pathlib
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from scripts.packs import auth


@dataclass(frozen=True)
class PackArchive:
    """A fetched pack: source URL, ref, resolved commit, fetch method,
    on-disk archive directory, canonical github identity (or ``None``
    for non-github hosts), and the cache key under which the archive
    is stored.
    """
    url: str
    ref: str
    resolved_commit: str
    method: str
    archive_dir: Path
    canonical_id: str | None
    cache_key: str


class PackLockDriftError(Exception):
    """Raised when ``update_policy=locked`` detects ref drift vs pack-lock.

    Carries enough context for the compose layer to surface a precise
    remediation message: the offending URL, the requested ref, the
    commit recorded in pack-lock, and the new commit currently at HEAD
    of the remote ref.
    """

    def __init__(self, url: str, ref: str, recorded: str, current: str):
        self.url = url
        self.ref = ref
        self.recorded = recorded
        self.current = current
        super().__init__(
            f"pack-lock drift for {url}@{ref}: recorded {recorded}, "
            f"current upstream {current}. Run `anywhere-agents pack update "
            f"<name>` to accept the new commit, or pin ref to {recorded}."
        )


def normalize_pack_source_url(url: str) -> str:
    """Return a canonical form of ``url`` for identity comparison.

    Used by ``pack verify`` to detect when the same pack name carries
    different source URLs across user-level / project-level / pack-lock
    layers. Two URLs that resolve to the same repository must normalize
    to byte-equal strings; two URLs that resolve to different repositories
    must remain distinct.

    GitHub URLs (`github.com`, case-insensitive host) collapse to
    ``https://github.com/<owner>/<repo>`` with **lowercased owner and
    repo**, no trailing ``.git``, no trailing ``/``. GitHub repository
    URLs are case-insensitive in practice, so ``Owner/Repo`` and
    ``owner/repo`` must compare equal.

    Other hosts get a minimal normalization: lowercase host, strip a
    single trailing ``/``, strip a single trailing ``.git``. Path case is
    **preserved** for non-GitHub hosts (other forges may be
    case-sensitive, e.g., self-hosted Gitea).

    Unparseable URLs are returned unchanged so verify never crashes; it
    just counts them as a distinct identity.
    """
    if not isinstance(url, str) or not url:
        return url
    # auth.normalize_github_url uses a case-sensitive "github.com" host
    # check + literal `github\.com` regex patterns. Pre-lowercase the
    # host token so e.g. https://GitHub.COM/Owner/Repo is recognized
    # without losing owner/repo case (which the regex captures and we
    # lowercase below for case-insensitive identity comparison).
    import re
    candidate = re.sub(r"github\.com", "github.com", url, flags=re.IGNORECASE)
    try:
        github = auth.normalize_github_url(candidate)
    except Exception:
        github = None
    if github is not None:
        owner, repo = github
        return f"https://github.com/{owner.lower()}/{repo.lower()}"
    # Non-GitHub: minimal normalization. Parse host case-insensitively,
    # strip trailing .git and trailing /, leave path case intact.
    from urllib.parse import urlsplit, urlunsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url
    host = parts.netloc.lower()
    path = parts.path
    if path.endswith("/"):
        path = path[:-1]
    if path.endswith(".git"):
        path = path[:-4]
    return urlunsplit((parts.scheme, host, path, parts.query, parts.fragment))


def compute_cache_key(url: str, resolved_commit: str) -> str:
    """Cache key keying URL+commit to a stable directory layout.

    For github.com URLs, keys by ``canonical_github_identity`` so that
    different URL forms (https vs scp-style SSH) for the same repo
    dedupe into one cache slot. For other hosts, keys by ``sha256(url)``,
    which is URL-shape-sensitive (a deliberate trade-off: we cannot
    canonicalize an arbitrary host without host-specific knowledge).
    """
    canonical = auth.canonical_github_identity(url)
    if canonical:
        prefix = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    else:
        prefix = hashlib.sha256(url.encode()).hexdigest()[:16]
    return f"{prefix}/{resolved_commit}"


def _iter_content_files(archive_dir: pathlib.Path):
    """Yield only content files (skip ``.git/`` metadata and ``.dir-sha256``).

    Without this filter, the dir-sha256 marker file would be included
    in its own hash on subsequent runs, so the cache-hit integrity
    check would always fail. ``.git/`` is clone metadata, not pack
    content, and varies across clones of the same commit.

    **Do not replace this with :func:`packs.dirhash.is_artifact`.** The
    two filters look interchangeable and are not. This one guards the
    archive cache, where *any* file can be a manifest's ``files[].from``
    source: the schema puts no suffix restriction on that value, so a
    hook mapping may legitimately read ``hook.py.orig`` and deploy it as
    ``deployed.py``. Ignoring a name here would leave the cache marker
    valid after those bytes changed, and the handler would deploy the
    mutation. The deployed-tree predicate can afford a short ignore-list
    because it only ever skips files nothing reads; this side cannot.
    Reproduced during the anywhere-agents#18 review, which is why the
    unification attempted there was reverted.
    """
    for path in archive_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(archive_dir)
        if rel.parts and rel.parts[0] == ".git":
            continue
        if rel.as_posix() == ".dir-sha256":
            continue
        yield path


# Framing for the archive-cache digest. Deliberately duplicated from
# ``packs.dirhash`` rather than imported: this module is vendored into the
# installed wheel as a small standalone subset, and it must not depend on
# the deployed-tree predicate (see ``_iter_content_files`` above for why
# the two file-selection rules have to stay apart). The duplication is
# pinned by an encoder-equality test in ``tests/test_dirhash_artifacts.py``
# so the two cannot drift.
_DIRHASH_V2_DOMAIN = b"anywhere-agents-dir-sha256-v2\0"
_DIRHASH_V2_PREFIX = "dir-sha256-v2:"
_LEN_BYTES = 8


def _compute_dir_sha256(archive_dir: pathlib.Path) -> str:
    """Stable Merkle hash over the archive directory's content files.

    Produces ``dir-sha256-v2:<hex>``. Files are sorted by their path
    components; each contributes a component count, then every component
    length-prefixed, then length-prefixed content, all under a domain
    tag.

    Two ambiguities in the previous framing are what this shape closes.
    It separated fields with a null byte on the theory that paths cannot
    contain one; paths cannot, but **file content can**, so one file
    holding ``b"x\\0b\\0y"`` produced the same byte stream as two files
    holding ``b"x"`` and ``b"y"``. It also joined the relative path into
    one string, and on POSIX a backslash is a legal filename character,
    so a file named ``a\\b`` encoded the same as file ``b`` inside
    directory ``a``. Length prefixes fix the first; component-wise
    encoding fixes the second, because a separator never enters the byte
    stream for a filename to forge.

    Cache slots recorded under the old framing fail their integrity check
    once and are refetched, which is the intended recovery.
    """
    entries = sorted(
        (p.relative_to(archive_dir).parts, p)
        for p in _iter_content_files(archive_dir)
    )
    h = hashlib.sha256()
    h.update(_DIRHASH_V2_DOMAIN)
    for parts, p in entries:
        content = p.read_bytes()
        h.update(len(parts).to_bytes(_LEN_BYTES, "big"))
        for part in parts:
            part_bytes = part.encode("utf-8")
            h.update(len(part_bytes).to_bytes(_LEN_BYTES, "big"))
            h.update(part_bytes)
        h.update(len(content).to_bytes(_LEN_BYTES, "big"))
        h.update(content)
    return f"{_DIRHASH_V2_PREFIX}{h.hexdigest()}"


def _remove_readonly(func, path, _exc_info):
    """Retry failed tree deletion after clearing Windows read-only bits."""
    try:
        os.chmod(_fs_path(path), stat.S_IWRITE)
    except OSError:
        pass
    try:
        result = func(_fs_path(path))
        close = getattr(result, "close", None)
        if close is not None:
            close()
    except FileNotFoundError:
        pass


def _rmtree_existing(path: pathlib.Path) -> None:
    """Remove an existing tree, including read-only git pack files.

    Windows can mark files under ``.git/objects/pack`` read-only. If a
    stale cache slot is not fully removed before ``shutil.move``, Python
    moves the freshly cloned directory inside the old slot instead of
    replacing it, leaving ``pack.yaml`` below an ``aa-clone-*`` child.
    """
    if not _path_exists(path):
        return
    try:
        shutil.rmtree(path, onerror=_remove_readonly)
    except FileNotFoundError:
        return
    except OSError:
        _manual_rmtree(path)
    if _path_exists(path):
        _manual_rmtree(path)
    if _path_exists(path):
        raise OSError(f"failed to remove stale cache directory: {path}")


def _manual_rmtree(path: pathlib.Path) -> None:
    """Last-resort recursive removal for stubborn Windows cache trees."""
    try:
        fs_path = _fs_path(path)
        if os.path.islink(fs_path) or os.path.isfile(fs_path):
            _unlink_existing(path)
            return
        if os.path.isdir(fs_path):
            for child in _iter_children(path):
                _manual_rmtree(child)
            try:
                os.rmdir(fs_path)
            except PermissionError:
                os.chmod(fs_path, stat.S_IWRITE)
                os.rmdir(fs_path)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass


def _unlink_existing(path: pathlib.Path) -> None:
    fs_path = _fs_path(path)
    try:
        os.unlink(fs_path)
    except PermissionError:
        os.chmod(fs_path, stat.S_IWRITE)
        os.unlink(fs_path)
    except FileNotFoundError:
        pass


def _iter_children(path: pathlib.Path) -> list[pathlib.Path]:
    with os.scandir(_fs_path(path)) as entries:
        return [path / entry.name for entry in entries]


def _path_exists(path: pathlib.Path) -> bool:
    return os.path.lexists(_fs_path(path))


CACHE_EVICTION_MIN_AGE_SECONDS = 24 * 3600
_HEX = frozenset("0123456789abcdef")


def _is_hex(name: str, length: int) -> bool:
    return len(name) == length and set(name) <= _HEX


def _is_real_dir(st: os.stat_result) -> bool:
    """A directory that is neither a symlink nor a Windows reparse point."""
    reparse = getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    return stat.S_ISDIR(st.st_mode) and not reparse


def referenced_cache_keys(lock_data: dict) -> set[str]:
    """Cache keys (``<16-hex>/<40-hex>``) that a pack-lock payload names.

    Both ``resolved_commit`` and ``latest_known_head`` count: a skipped
    update reloads the locked commit from the cache, and ``pack verify``
    records the remote head it observed. Non-hex commits (bundled packs) name no
    cache slot.
    """
    keys: set[str] = set()
    packs = lock_data.get("packs") if isinstance(lock_data, dict) else None
    if not isinstance(packs, dict):
        return keys
    for entry in packs.values():
        if not isinstance(entry, dict):
            continue
        url = entry.get("source_url")
        if not isinstance(url, str) or not url:
            continue
        for field in ("resolved_commit", "latest_known_head"):
            sha = entry.get(field)
            if isinstance(sha, str) and _is_hex(sha.lower(), 40):
                keys.add(compute_cache_key(url, sha.lower()))
    return keys


def _lexical_path(path) -> str:
    """Like ``_fs_path`` but never resolves links or junctions.

    Eviction must inspect the link itself; ``Path.resolve`` on Windows would
    follow a junction and report its target as an ordinary directory.
    """
    path = pathlib.Path(path)
    s = str(path)
    if os.name != "nt" or s.startswith("\\\\?\\"):
        return s
    s = str(path.absolute())
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def _retry_plain_readonly_file(func, path, exc_info):
    """rmtree error hook: on Windows, clear read-only on a plain file once.

    Windows marks git pack files read-only, which blocks their unlink. Only a
    PermissionError on such a file (singly linked, not a reparse point) is
    retried. Other failures propagate to the caller, which reports the
    affected slot, and no other platform ever changes a mode here.
    """
    if os.name != "nt" or not isinstance(exc_info[1], PermissionError):
        raise exc_info[1]
    st = os.lstat(path)
    attributes = getattr(st, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(st.st_mode)
        or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or not attributes & stat.FILE_ATTRIBUTE_READONLY
        or st.st_nlink != 1
        or func not in (os.unlink, os.remove)
    ):
        raise exc_info[1]
    os.chmod(path, st.st_mode | stat.S_IWRITE)
    func(path)


def _remove_slot(slot: pathlib.Path) -> None:
    """Remove one cache slot without following links; raise on failure."""
    shutil.rmtree(_lexical_path(slot), onerror=_retry_plain_readonly_file)


def _list_names(path: pathlib.Path) -> list[str]:
    with os.scandir(_lexical_path(path)) as entries:
        return [entry.name for entry in entries]


def evict_unreferenced_slots(
    cache_root: pathlib.Path,
    keep_keys: set[str],
    *,
    min_age_seconds: float = CACHE_EVICTION_MIN_AGE_SECONDS,
    now: float | None = None,
) -> tuple[list[str], list[str]]:
    """Remove ``<16-hex>/<40-hex>`` cache slots that no lock entry names.

    The cache root, its parent, each key directory, and each slot must be
    real directories (no link or Windows junction anywhere on that path). A
    slot goes when its key is not in ``keep_keys`` and its modification time
    is at least ``min_age_seconds`` old; a key directory left empty goes too.
    Other names are never touched. The caller holds the repo lock. Returns
    ``(removed_keys, errors)``; errors are messages, never raised, so a
    committed compose stays successful.
    """
    removed: list[str] = []
    errors: list[str] = []
    now = time.time() if now is None else now
    try:
        if not _is_real_dir(os.lstat(_lexical_path(cache_root.parent))):
            return removed, errors
        if not _is_real_dir(os.lstat(_lexical_path(cache_root))):
            return removed, errors
        key_names = _list_names(cache_root)
    except FileNotFoundError:
        return removed, errors
    except OSError as exc:
        return removed, [f"{cache_root}: {exc}"]
    for key_name in sorted(key_names):
        if not _is_hex(key_name, 16):
            continue
        key_dir = cache_root / key_name
        try:
            if not _is_real_dir(os.lstat(_lexical_path(key_dir))):
                continue
            slot_names = _list_names(key_dir)
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{key_dir}: {exc}")
            continue
        for slot_name in sorted(slot_names):
            key = f"{key_name}/{slot_name}"
            if not _is_hex(slot_name, 40) or key in keep_keys:
                continue
            slot = key_dir / slot_name
            try:
                slot_stat = os.lstat(_lexical_path(slot))
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(f"{slot}: {exc}")
                continue
            if not _is_real_dir(slot_stat):
                continue
            if now - slot_stat.st_mtime < min_age_seconds:
                continue
            try:
                _remove_slot(slot)
            except OSError as exc:
                errors.append(f"{slot}: {exc}")
                continue
            removed.append(key)
        try:
            if not _list_names(key_dir):
                os.rmdir(_lexical_path(key_dir))
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"{key_dir}: {exc}")
    return removed, errors


def _fs_path(path) -> str:
    """Return a filesystem path string, using Windows long-path syntax."""
    path = pathlib.Path(path)
    s = str(path)
    if os.name != "nt" or s.startswith("\\\\?\\"):
        return s
    try:
        s = str(path.resolve())
    except OSError:
        s = str(path.absolute())
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def _archive_root(cache_dir: pathlib.Path) -> pathlib.Path:
    """Return the content root for a cache slot.

    Older Windows runs could leave a valid-hash cache slot whose real git
    clone is nested under an ``aa-clone-*`` child after a failed stale-slot
    cleanup. Prefer that child when the slot root has no ``pack.yaml`` so
    existing broken caches remain recoverable.
    """
    if (cache_dir / "pack.yaml").exists():
        return cache_dir
    candidates = sorted(
        child
        for child in cache_dir.iterdir()
        if (
            child.is_dir()
            and child.name.startswith("aa-clone-")
            and ((child / "pack.yaml").exists() or (child / ".git").exists())
        )
    )
    if len(candidates) == 1:
        return candidates[0]
    return cache_dir


# A compose run fetches several packs, and packs commonly share a source.
# The maintainer's own consumers pin `profile`, `paper-workflow`, and
# `acad-skills` to the same repository at the same ref, so a five-pack run
# asked the remote to resolve one identical cache key three times. The
# archive cache already deduplicated the clone, keyed on the resolved
# commit, but the resolve itself ran once per pack because it happens
# before the key is known.
#
# Caching it also removes a latent inconsistency. Without the cache, two
# packs sharing a cache key could resolve to different commits if the
# remote moved between their two `ls-remote` calls, and the run would
# record a lock whose entries disagree about what `main` meant. One resolve
# per (url, ref, explicit_auth) gives every pack on that key a consistent
# snapshot for the run.
#
# Only successful resolves are cached. A failure re-walks the auth chain so
# its diagnostics stay exactly as informative as before.
#
# The cache is off unless a caller opens a scope. Module-level state that is
# live by default would outlast the run that wanted it: a long-lived process
# such as the CLI running verify and then compose would reuse one resolve
# across both operations, and any test calling ``fetch_pack`` twice with the
# same fixture URL would silently receive the first test's commit.
_REF_RESOLUTION_CACHE: dict[tuple, tuple] | None = None


@contextlib.contextmanager
def ref_resolution_cache():
    """Resolve each ``(url, ref, explicit_auth)`` key once per block.

    The key includes the explicit auth method because a caller that pins one
    must be honored rather than served another method's result. Two packs on
    the same URL and ref but different explicit auth therefore still resolve
    independently, and the snapshot guarantee below applies per key rather
    than per ref.

    Nesting restores the enclosing scope's cache on exit, so a caller that
    opens a scope inside another caller's scope cannot clear it.
    """
    global _REF_RESOLUTION_CACHE
    previous = _REF_RESOLUTION_CACHE
    _REF_RESOLUTION_CACHE = {}
    try:
        yield
    finally:
        _REF_RESOLUTION_CACHE = previous


def _resolve_ref_cached(
    url: str,
    ref: str,
    explicit_auth: str | None,
) -> tuple:
    """Resolve ``(url, ref, explicit_auth)`` once per open cache scope."""
    if _REF_RESOLUTION_CACHE is None:
        return auth.resolve_ref_with_auth_chain(
            url, ref, explicit_method=explicit_auth,
        )
    key = (url, ref, explicit_auth)
    hit = _REF_RESOLUTION_CACHE.get(key)
    if hit is not None:
        return hit
    resolved = auth.resolve_ref_with_auth_chain(
        url, ref, explicit_method=explicit_auth,
    )
    _REF_RESOLUTION_CACHE[key] = resolved
    return resolved


def load_cached_archive(
    url: str,
    recorded_commit: str,
    *,
    cache_root: pathlib.Path | None = None,
) -> PackArchive | None:
    """Return the cached archive at ``(url, recorded_commit)``, or ``None``.

    No network call. Used by the compose skip-path to revert to a
    pack-lock-recorded commit without re-resolving the ref. Calling
    :func:`fetch_pack` with a 40-char SHA as ``ref`` would re-run
    :func:`auth.resolve_ref_with_auth_chain`, which calls
    ``git ls-remote <url> <sha> <sha>^{}``; ls-remote matches by refname
    and returns empty for a SHA, exhausting the auth chain and raising
    :class:`auth.AuthChainExhaustedError`. This helper sidesteps that by
    going straight to the cache slot.

    Returns ``None`` if the cache slot does not exist or fails its
    ``dir-sha256`` integrity check, in which case the caller falls back
    to whatever archive it already has.
    """
    if cache_root is None:
        cache_root = pathlib.Path(".agent-config/cache")
    cache_key = compute_cache_key(url, recorded_commit)
    cache_dir = cache_root / cache_key
    if not cache_dir.exists():
        return None
    marker = cache_dir / ".dir-sha256"
    if not marker.exists():
        return None
    recorded_sha = marker.read_text()
    if recorded_sha != _compute_dir_sha256(cache_dir):
        return None
    archive_dir = _archive_root(cache_dir)
    return PackArchive(
        url=url,
        ref=recorded_commit,
        resolved_commit=recorded_commit,
        method="cached",
        archive_dir=archive_dir,
        canonical_id=auth.canonical_github_identity(url),
        cache_key=cache_key,
    )


def fetch_pack(
    url: str,
    ref: str,
    *,
    policy: Literal["auto", "prompt", "locked"] = "prompt",
    explicit_auth: str | None = None,
    pack_lock_recorded_commit: str | None = None,
    cache_root: pathlib.Path | None = None,
) -> PackArchive:
    """Fetch (or cache-hit) the pack archive at ``url@ref``.

    Ordering (Codex Round 1 M5):

    1. Validate URL via :func:`auth.reject_credential_url` (no network).
    2. Resolve the cache key via :func:`_resolve_ref_cached`. Outside a
       cache scope, and on a miss inside one, this calls
       :func:`auth.resolve_ref_with_auth_chain` once; that call walks
       the auth chain and issues one ``ls-remote`` per attempted
       method, so a miss is one or more network calls. A hit makes
       none.
    3. Compare the resolved sha against ``pack_lock_recorded_commit``;
       under ``policy="locked"`` drift raises :class:`PackLockDriftError`.
       Under ``"auto"`` / ``"prompt"`` the compose layer decides whether
       to apply or defer the new commit.
    4. Cache lookup at ``(canonical_id_or_url_hash, resolved_commit)``.
       Hit -> re-verify the recorded ``dir-sha256``; on match, return
       a :class:`PackArchive` pointing into the cache. On mismatch the
       cache slot is dropped and the fetch proceeds (one retry).
    5. Cache miss -> :func:`auth.fetch_with_auth_chain` clones into
       staging; the staging directory is moved atomically into the
       cache slot and the ``dir-sha256`` recorded for next-run integrity.
    """
    if cache_root is None:
        cache_root = pathlib.Path(".agent-config/cache")
    cache_root.mkdir(parents=True, exist_ok=True)

    # 1. Validate (parse-time, no network).
    auth.reject_credential_url(url, source_layer="source_fetch")

    # 2. Resolve ref -> commit sha.
    resolved_commit, _resolve_method = _resolve_ref_cached(
        url, ref, explicit_auth,
    )

    # 3. Compare against pack-lock recorded commit.
    if (
        pack_lock_recorded_commit
        and resolved_commit != pack_lock_recorded_commit
    ):
        if policy == "locked":
            raise PackLockDriftError(
                url, ref, pack_lock_recorded_commit, resolved_commit,
            )
        # policy in {"auto", "prompt"}: drift is handled by the compose
        # layer, which decides apply-or-defer. We continue with the
        # newly resolved commit so the compose layer sees both values.

    # 4. Cache lookup.
    cache_key = compute_cache_key(url, resolved_commit)
    cache_dir = cache_root / cache_key
    if cache_dir.exists():
        marker = cache_dir / ".dir-sha256"
        recorded_sha = marker.read_text() if marker.exists() else None
        current_sha = _compute_dir_sha256(cache_dir)
        if recorded_sha == current_sha:
            archive_dir = _archive_root(cache_dir)
            return PackArchive(
                url=url,
                ref=ref,
                resolved_commit=resolved_commit,
                method="cached",
                archive_dir=archive_dir,
                canonical_id=auth.canonical_github_identity(url),
                cache_key=cache_key,
            )
        # Integrity mismatch: drop the slot and fall through to refetch.
        _rmtree_existing(cache_dir)

    # 5. Cache miss -> fetch.
    archive = auth.fetch_with_auth_chain(
        url, ref, explicit_method=explicit_auth,
    )

    # Codex Round 2 H2 fix: ``resolve_ref_with_auth_chain`` returned the
    # pre-clone SHA at step 2; ``fetch_with_auth_chain`` is a separate
    # network round-trip and may resolve to a different commit if the
    # branch / tag moved between calls. When the cloned tree's HEAD SHA
    # disagrees with the pre-clone SHA, we MUST re-key the cache to the
    # post-clone SHA so the ``dir-sha256`` marker and ``pack-lock.json``
    # attest to the actually-fetched commit. Under ``policy=locked``
    # with a recorded commit, the post-clone SHA is reported as drift
    # (so the operator sees the new commit, not the stale pre-clone one).
    if archive.resolved_commit != resolved_commit:
        if policy == "locked" and pack_lock_recorded_commit is not None:
            _rmtree_existing(archive.archive_dir)
            raise PackLockDriftError(
                url, ref, pack_lock_recorded_commit, archive.resolved_commit,
            )
        # ``policy in {"auto", "prompt"}``: re-key the cache so the slot
        # name matches what is actually inside it. The caller's drift /
        # apply logic already runs on ``archive.resolved_commit``.
        resolved_commit = archive.resolved_commit
        cache_key = compute_cache_key(url, resolved_commit)
        cache_dir = cache_root / cache_key

    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    if cache_dir.exists():
        # Best-effort cleanup of a stale cache_dir from a crashed prior
        # run. Real concurrent-fetch protection comes from the Phase 6
        # outer lock in compose_packs.py.
        _rmtree_existing(cache_dir)
    shutil.move(str(archive.archive_dir), str(cache_dir))
    sha = _compute_dir_sha256(cache_dir)
    (cache_dir / ".dir-sha256").write_text(sha)

    # The clone is authoritative once it lands: the ref may have moved
    # between ls-remote and clone, in which case the block above already
    # re-keyed the cache slot to the post-clone commit. Promote that commit
    # into the run cache too, or the next pack sharing this cache key is
    # handed the stale pre-clone SHA, misses the slot that was just written
    # under the post-clone SHA, and clones the moving ref a second time.
    # Leaving the pre-clone value would also break the consistent-snapshot
    # property this cache exists to provide: if the mutable ref moves again,
    # the run ends holding two commits for one cache key.
    if _REF_RESOLUTION_CACHE is not None:
        _REF_RESOLUTION_CACHE[(url, ref, explicit_auth)] = (
            resolved_commit,
            archive.method,
        )

    return PackArchive(
        url=url,
        ref=ref,
        resolved_commit=resolved_commit,
        method=archive.method,
        archive_dir=cache_dir,
        canonical_id=auth.canonical_github_identity(url),
        cache_key=cache_key,
    )
