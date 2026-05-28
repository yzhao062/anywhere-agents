"""``kind: skill`` handler for the unified pack composer (v0.4.0 Phase 3).

Reads each ``from`` → ``to`` mapping in the manifest's ``files:`` list and
stages copies through the active transaction. Directory mappings are
deep-copied per-file; a merkle-style sha256 over the source tree is
recorded in pack-lock for later drift detection.

Pointer-file handling (pack-architecture.md:184): every ``kind: skill``
entry that targets ``.claude/skills/<name>/`` auto-emits a canonical
``.claude/commands/<name>.md`` pointer UNLESS the manifest's own
``files:`` list already contains an explicit mapping for that pointer
path. The explicit mapping wins on conflict; this preserves custom
per-skill pointer content for aa-shipped skills (e.g., my-router's
routing-table.md hint) while giving third-party packs the documented
"ship only the skill directory" contract.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

from ..dispatch import DispatchContext

_IS_WINDOWS = sys.platform == "win32"


def _match_key(rel: str) -> str:
    """Normalize a manifest ``to:`` path to a stable match key.

    Windows accepts both separators and is case-insensitive; collapsing
    both variants into one key lets the auto-emit suppressor match an
    explicit mapping regardless of how the manifest wrote the pointer
    path. On POSIX, case remains significant.
    """
    key = rel.replace("\\", "/").rstrip("/")
    return key.lower() if _IS_WINDOWS else key


_POINTER_TEMPLATE = (
    "Read and follow the skill definition. Look for it at "
    "`skills/{name}/SKILL.md` first, then "
    "`.claude/skills/{name}/SKILL.md`, then "
    "`.agent-config/repo/skills/{name}/SKILL.md`.\n"
    "\n"
    "Apply it to the user's current task. Also read the supporting files "
    "under the skill's references/ directory as needed.\n"
)


def handle_skill(entry: dict[str, Any], ctx: DispatchContext) -> None:
    """Dispatch a ``kind: skill`` active entry.

    For each ``files[i]`` mapping:
      - Resolve ``from`` relative to ``ctx.pack_source_dir``.
      - Resolve ``to`` relative to ``ctx.project_root``.
      - If the source is a directory, stage a per-file copy preserving
        structure and record a single lock entry with a merkle ``dir-sha256``.
        If the directory targets ``.claude/skills/<name>/`` AND no
        explicit mapping in the same ``files:`` list already covers
        ``.claude/commands/<name>.md``, auto-emit the canonical pointer.
      - If the source is a file, stage one copy and record the file hash.

    All outputs are recorded as ``role: active-skill``. Skills never
    touch user-level state; this handler mutates ``ctx.txn`` (via
    ``stage_write``), the current pack's lock file entries, and
    project-local ``pack-state``.
    """
    files = entry["files"]  # schema guarantees list with >=1 {from, to}
    # Pre-scan: collect NORMALIZED target paths the manifest maps
    # explicitly (forward slashes, no trailing slash, lowercased on
    # Windows) so auto-emit suppression matches regardless of how the
    # manifest author spelled the path.
    explicit_targets = {_match_key(mapping["to"]) for mapping in files}

    for mapping in files:
        src_rel = mapping["from"]
        dst_rel = mapping["to"]
        src = (ctx.pack_source_dir / src_rel).resolve()
        dst = (ctx.project_root / dst_rel).resolve()

        if not src.exists():
            raise FileNotFoundError(
                f"pack {ctx.pack_name!r} skill entry: source {src} does not "
                f"exist (manifest 'from': {src_rel!r})"
            )

        if src.is_dir():
            input_sha = _stage_dir_copy(src, dst, ctx)
            _maybe_auto_emit_pointer(
                dst_rel, src_rel, ctx, explicit_targets
            )
        else:
            content = src.read_bytes()
            ctx.txn.stage_write(dst, content)
            input_sha = hashlib.sha256(content).hexdigest()

        ctx.record_lock_file(
            {
                "role": "active-skill",
                "host": ctx.current_host,
                "source_path": src_rel,
                "input_sha256": input_sha,
                "output_paths": [dst_rel],
                "output_scope": "project-local",
                "effective_update_policy": ctx.pack_update_policy,
            }
        )

        ctx.project_state.setdefault("entries", []).append(
            {
                "pack": ctx.pack_name,
                "output_path": dst_rel,
                "sha256": input_sha,
            }
        )


def _maybe_auto_emit_pointer(
    dst_rel: str,
    src_rel: str,
    ctx: DispatchContext,
    explicit_targets: set[str],
) -> None:
    """Auto-emit ``.claude/commands/<name>.md`` for a skill dir mapping
    unless the manifest already ships an explicit mapping for that path.

    Non-skill directory targets (``dst_rel`` not under
    ``.claude/skills/``) are ignored — callers may route arbitrary
    directory trees through ``kind: skill`` and we should not silently
    generate pointer files for unrelated paths.
    """
    # Normalize for posix comparison so Windows paths don't miss the match.
    dst_norm = dst_rel.replace("\\", "/").rstrip("/")
    # Prefix match is case-insensitive on Windows, case-sensitive on POSIX,
    # mirroring the filesystem's own path semantics.
    prefix_match = (
        dst_norm.lower().startswith(".claude/skills/")
        if _IS_WINDOWS
        else dst_norm.startswith(".claude/skills/")
    )
    if not prefix_match:
        return
    skill_name = Path(dst_norm).name
    pointer_rel = f".claude/commands/{skill_name}.md"
    # Explicit manifest mapping wins; skip auto-emit. Compare via the
    # same _match_key normalization used to build explicit_targets so
    # Windows variant spellings (backslashes, case differences) suppress
    # consistently. Without this, a manifest with `to: .claude\\commands\\foo.md`
    # or `to: .CLAUDE/COMMANDS/foo.md` on Windows would produce duplicate
    # lock/state records (Round 3 Codex High).
    if _match_key(pointer_rel) in explicit_targets:
        return

    pointer_text = _POINTER_TEMPLATE.format(name=skill_name)
    pointer_bytes = pointer_text.encode("utf-8")
    pointer_abs = ctx.project_root / pointer_rel
    ctx.txn.stage_write(pointer_abs, pointer_bytes)

    pointer_sha = hashlib.sha256(pointer_bytes).hexdigest()
    ctx.record_lock_file(
        {
            "role": "generated-command",
            "host": ctx.current_host,
            "source_path": None,
            "input_sha256": None,
            "output_paths": [pointer_rel],
            "output_scope": "project-local",
            "effective_update_policy": ctx.pack_update_policy,
            "generated_from": f"active-skill:{skill_name}",
            # Phase 3 stores the template version + pointer sha together
            # so generated-command drift detection (Phase 4+) can tell a
            # template change from an on-disk tamper.
            "source_input_sha256": pointer_sha,
            "template_sha256": f"aa-composer-skill-pointer-v2:{pointer_sha}",
            "output_sha256": pointer_sha,
        }
    )
    ctx.project_state.setdefault("entries", []).append(
        {
            "pack": ctx.pack_name,
            "output_path": pointer_rel,
            "sha256": pointer_sha,
        }
    )


def _stage_dir_copy(
    src_dir: Path, dst_dir: Path, ctx: DispatchContext
) -> str:
    """Stage a per-file copy from ``src_dir`` to ``dst_dir`` preserving
    the directory tree and return a merkle-style ``dir-sha256:<hex>``
    hash over the copied content.

    Files are iterated in sorted path order so the merkle hash is
    reproducible across filesystems whose ``rglob`` order differs.
    """
    hasher = hashlib.sha256()
    entries: list[Path] = sorted(
        (p for p in src_dir.rglob("*") if p.is_file()),
        key=lambda p: str(p.relative_to(src_dir)).replace("\\", "/"),
    )
    for src_file in entries:
        rel = src_file.relative_to(src_dir)
        rel_posix = str(rel).replace("\\", "/")
        dst_file = dst_dir / rel
        content = src_file.read_bytes()
        ctx.txn.stage_write(dst_file, content)
        # Merkle encoding: path (posix-normalized) + null + content + null.
        hasher.update(rel_posix.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(content)
        hasher.update(b"\0")
    return f"dir-sha256:{hasher.hexdigest()}"
