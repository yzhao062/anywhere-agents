# Changelog

All notable changes to `anywhere-agents` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version tags apply uniformly to the repo content **and** the matching `anywhere-agents` PyPI / npm packages — they share one release stream. Consumers pinned to a specific tag get a stable snapshot; consumers on `main` receive ongoing updates.

## [Unreleased]

_No unreleased changes queued._

## [0.5.7] — 2026-04-29

### Changed

- **Bundled `agent-style` switched to compact + ref bumped to `v0.3.5`.** Two-axis update in `bootstrap/packs.yaml` and the v0.5.6 wheel-bundled mirror at `packages/pypi/anywhere_agents/composer/bootstrap/packs.yaml` (aa-internal STRICT): `ref: v0.3.2 → v0.3.5` and `from: docs/rule-pack.md → docs/rule-pack-compact.md`. Existing aa 0.5.6 projects upgrade with two commands — `pipx install --force anywhere-agents==0.5.7` then `anywhere-agents pack verify --fix` — and see the inlined `agent-style` body in `AGENTS.md` drop from ~89 KB (full rule-pack: 21 rules + metadata bullets + 5+ BAD/GOOD pairs + rationale) to ~21 KB (compact rule-pack: 21 rules + directive + 1 illustrative BAD/GOOD pair). Total `AGENTS.md` size drops ~50% on a maintainer-style project (5 packs, base + agent-style + agent-pack/{acad,paper,profile}). `pack-lock.json` advances `agent-style.resolved_commit == v0.3.5` and `source_path == docs/rule-pack-compact.md`.

### Notes

- **`update_policy: locked` unchanged.** Bundled manifest keeps `update_policy: locked`. The policy change (to `update_policy: prompt`, banner-driven update notifications) is deferred to **aa v0.6.0** because it requires the `pack verify --fix` inline-apply UX fix to make `prompt` policy ergonomic for consumers; see `agent-config/docs/pack-architecture.md` § "aa v0.6.0 — Update-UX revisit". Shipping the policy change without the UX fix would extend the existing `agent-pack` prompt-drift confusion (reproduced on `usc-slides`) to a second source.
- **`pack verify` does not migrate old explicit pins; a full composer/bootstrap run does.** This release is asymmetric across the two repair entry points. The `pack verify --fix` path leaves an existing project alone when it already has an explicit `agent-style` override at an older ref with full-body `passive.files[].from: docs/rule-pack.md` — verify reports the pinned ref and the inlined body stays as last composed. However, a full composer / bootstrap run on aa v0.5.7 (e.g., `anywhere-agents` with no args, or any direct composer invocation) re-derives the pack definition from the bundled `bootstrap/packs.yaml` because `agent-style` ships no `pack.yaml` of its own. The bundled definition now points at `docs/rule-pack-compact.md`, so compact output is expected on a bootstrap run regardless of the consumer-side `passive` override. Net effect: a v0.5.6 consumer with an old full-body explicit pin gets compact on the next bootstrap, not on the next `pack verify --fix`. Consumer-side compact-to-full switching as a first-class supported action is deferred to aa v0.6.0 (same-ref source-path switching). Consumers who must keep the bundled full-body default should stay on aa v0.5.6 until v0.6.0.
- **Documentation refresh.** `docs/rule-pack-composition.md` (3 illustrative example places), `compose_rule_packs.py` user-help snippet, and `bootstrap/bootstrap.{ps1,sh}` dry-helper hint all show `v0.3.5` in their examples to match the new bundled default.

### Compatibility

- **Existing explicit pins are only verify-stable.** `pack verify --fix` leaves a project with `name: agent-style` and an older `ref` alone when its deployed output still matches the lock. A full composer/bootstrap run on aa v0.5.7 re-derives the pack definition from the bundled manifest and can rewrite that project to compact, as noted above. Consumers that need the old full-body bundled output should stay on aa v0.5.6 until same-ref source-path switching lands in aa v0.6.0.
- **Lock-file ref bump under `update_policy: locked` is allowed** because v0.5.7 declares a new bundled default; `locked` policy gates *upstream HEAD drift on the same ref*, not maintainer-declared bundled-default updates. The composer resolves `agent-style` from the current bundled manifest and writes the lock entry with the new ref/source_path.
- **Content equivalence.** Compact and full both ship the same 21 rule directives and the same rule headings/order. Compact drops per-rule metadata bullets (source / agent-instruction evidence / severity / scope / enforcement), additional BAD/GOOD pairs, and the `Rationale for AI Agent` subsection. Rules apply identically at runtime.
- **Tests pinned to v0.3.2 fixtures stay v0.3.2.** Test fixtures in `tests/test_compose_*` and `tests/test_banner_updates.py` reference `v0.3.2` as known-state input; not changed by this release.
- **Legacy markerless content detection (`cli.py:1234`)** continues to recognize v0.3.2-era AGENTS.md content; not changed.

## [0.5.6] — 2026-04-28

### Fixed

- **Package-owned composer for repair commands.** `pack add`, `pack verify --fix`, `pack remove`, and `pack update` now invoke the composer shipped with the installed PyPI package after confirming the project is already bootstrapped. This fixes the v0.5.5 failure mode where `pipx install --force anywhere-agents==0.5.5` upgraded the CLI, but `pack verify --fix` still ran an older `.agent-config/repo/scripts/compose_packs.py` and left `aa-core-skills` missing.
- **Bundled pack sources are read from the composer owner.** The composer now reads bundled manifests and bundled active-pack files from the source tree that owns the running composer. In a normal bootstrap this is `.agent-config/repo`; in the PyPI repair path this is the wheel's bundled `anywhere_agents/composer` tree. Stale project caches can no longer keep the current CLI from writing current bundled defaults.

### Tests

- Added direct coverage that `_invoke_composer` prefers the package-bundled composer when a project-local composer exists, while still returning the existing "Run bootstrap first" error when the project has not been bootstrapped.

## [0.5.5] — 2026-04-27

### Highlights

- **Idempotent multi-repo refresh.** v0.5.4 still let a broken post-migration project lose bundled-default rows on the next compose run because the composer used the old fallback resolver path. v0.5.5 switches the composer to the four-layer resolver, seeds bundled defaults on every run, and proves the refresh path against real broken project copies without hand-editing those repos.
- **Windows cache self-heal.** Real migrations also exposed malformed cache slots on Windows: read-only git pack files and long asset paths could make stale-slot deletion fail, after which `shutil.move` nested an `aa-clone-*` directory inside the old cache slot. v0.5.5 makes stale cache removal strict and Windows-aware, and can recover a single nested clone inside an already URL-and-commit-keyed cache slot.

### Changed

- **Composer resolver migration** (`scripts/compose_packs.py`, `scripts/packs/config.py`). `_do_compose_v2` now reads all durable layers through `resolved_for_project(..., default_selections=DEFAULT_V2_SELECTIONS, force_defaults=True)`, so user-level packs and bundled defaults merge deterministically. Explicit empty project config still clears non-default user packs while preserving bundled defaults.
- **Pack verify default visibility** (`packages/pypi/anywhere_agents/cli.py`). `pack verify` now displays every bundled default with deployment status from lock and disk, even when user/project config does not mention it. `pack verify --fix` no longer returns rc=0 while bundled defaults are missing or deployed-but-not-locked.
- **Bundled-default remove warning** (`packages/pypi/anywhere_agents/cli.py`). `pack remove <name>` warns when the requested name is one of the bundled defaults because the next composer run will materialize it again unless defaults are changed at the composer level.
- **Source-fetch cache hardening** (`scripts/packs/source_fetch.py`, `packages/pypi/anywhere_agents/packs/source_fetch.py`). Cache cleanup now retries after clearing read-only bits, uses Windows long-path forms for deep asset trees, falls back to manual recursive deletion when platform `rmtree` fails, and raises if stale-slot cleanup still leaves files behind. Cached reads recover exactly one nested `aa-clone-*` archive root with `pack.yaml` or `.git`; zero or multiple candidates keep fail-loud behavior.

### Internals

- **Lock metadata preservation.** `_build_ctx` preserves prior `latest_known_head` and `fetched_at` only when `(source_url, requested_ref, resolved_commit)` still matches. Invalid SHA metadata is dropped, and uppercase SHA input is canonicalized to lowercase before writing.
- **Agent-style disk detection.** The verify classifier recognizes legacy markerless `agent-style v0.3.2` content by requiring all three upstream section signatures, with source comments documenting the coupling and revisit trigger.
- **Tests and verification.** Added resolver, verify, cache-recovery, long-path, and metadata-canonicalization coverage. Local validation for the release candidate: focused pytest `236 passed, 4 skipped`; non-integration pytest `790 passed, 5 skipped`; temp-copy validation across three real broken projects with inherited cache and cold cache all reached a deterministic five-pack lock without modifying the original repos. `implement-review` Round 4 reported no blocking findings and recommended shipping v0.5.5.

### Migration

- Existing v0.5.4 users should upgrade and rerun `anywhere-agents` or `anywhere-agents pack verify --fix --yes` in each project. The command refreshes project config and pack-lock state from the current user-level config plus bundled defaults. If a Windows cache slot was malformed by an earlier run, v0.5.5 either recovers the single nested clone or deletes and refetches the stale slot; no manual cache cleanup should be required.

## [0.5.4] — 2026-04-27

### Highlights

- **End-to-end migration in one shot.** v0.5.3's drift-gate adopt-on-match closed the byte-identical case, but four remaining gaps still blocked AC->AA+AP migration: (a) composer aborted when an upstream lacked the v0.4.0 `pack.yaml` even for bundled-default names like `agent-style v0.3.x`; (b) `pack verify --fix` reconciled bundled-default packs across layers when the user-level entry carried a non-bundled URL, propagating the URL form into project YAML on every run; (c) `pack add` left duplicate `name` rows in user-level config when the dedup logic predated the current shape; (d) running `anywhere-agents` after migration deployed only the bundled defaults, leaving user-level packs (e.g., `agent-pack` profile / paper-workflow / acad-skills) un-deployed until a separate `pack verify --fix` run. v0.5.4 closes all four. End-to-end test: a legacy AC project with a corrupt user-level config and stale `.claude/commands/*.md` files lands at the AA+AP target deployment after one `anywhere-agents` invocation.

### Changed

- **Composer bundled fallback for missing upstream `pack.yaml`** (`scripts/compose_packs.py`). When the inline-source path fetches an archive that has no `pack.yaml` at the root OR has one that doesn't declare the requested name, the composer now falls back to the bundled `bootstrap/packs.yaml` pack-def for `DEFAULT_V2_SELECTIONS` names. The fetched `archive_dir` is still threaded into `DispatchContext.pack_source_dir` so passive handlers read file bytes from the local archive instead of the v0.4.0 raw-URL fetch fallback. Closes the `agent-style v0.3.x` "manifest not found" abort that surfaced when the user-level config carried the URL form for a bundled-default name. Non-bundled packs without a usable `pack.yaml` still raise `ComposeError` with a clearer error message.
- **`pack verify --fix` never reconciles bundled-default packs** (`packages/pypi/anywhere_agents/cli.py`). `_is_bundled_default_row` now returns `True` for any name in `DEFAULT_V2_SELECTIONS` regardless of the layer's URL form. v0.5.3 still reconciled when the user explicitly named a non-bundled URL, which let a lock-side URL form (emitted because the bundled pack-def points to an upstream source for passive content) propagate into user-level config and back into project YAML on every subsequent run. The composer always materializes `agent-style` and `aa-core-skills` from the bundled manifest; reconciling them across layers is churn that v0.5.4 removes.
- **AC->AA migration moves `.claude/commands/` aside** (`packages/pypi/anywhere_agents/cli.py`). `_migrate_legacy_ac` now renames any non-empty `.claude/commands/` directory to `.claude/commands.bak-<UTC-timestamp>/` before AA's bootstrap reclones. AC's bootstrap copies pointer files into `.claude/commands/`; AA's `aa-core-skills` pack writes pointer files at the same paths but with bytes that may differ. Without the move, the drift gate refused to clobber the AC-leftover pointers as `PRESTATE_UNMANAGED` and the migration aborted. Backup directory preserves user-customized files for manual merge.
- **`anywhere-agents` auto-runs `pack verify --fix --yes` after bootstrap** (`packages/pypi/anywhere_agents/cli.py`). When the bootstrap script returns rc=0 and a user-level config exists, `_bootstrap_main` re-enters `main(["pack", "verify", "--fix", "--yes"])` so user-level packs reconcile to project `agent-config.yaml` and the composer subprocess deploys them in the same shot. Skipped when bootstrap fails (intermediate state is preserved for retry) or when no user-level config file exists (fast no-op for fresh installs without registered packs). The reconcile failure is logged as a warning; the bootstrap rc still wins.
- **User-level config self-heal dedup** (`packages/pypi/anywhere_agents/cli.py`). New `_dedup_user_packs()` helper drops later occurrences of the same `name`, keeping the first. Plumbed through `_load_or_create_user_config` (silent dedup at every read; next write persists the cleaned list) and `_load_user_observations` (verify sees deduped data). `_pack_verify_fix` runs an explicit early dedup-and-rewrite pass so the on-disk file is normalized even when no reconcile is otherwise needed. Defensive against earlier `pack add` versions and concurrent invocations that could leave duplicates.

### Internals

- **`_bundled_pack_def(bundled_manifest, name)`** factored out of `_process_selection`. Shared by the bundled-lookup path and the v0.5.4 inline-source fallback so the lookup is byte-identical between the two entry points.
- **Tests**: `tests/test_compose_packs_v0_5.py` adds three `_process_selection` cases (no upstream `pack.yaml` -> bundled fallback; upstream `pack.yaml` present but missing the name -> bundled fallback for default names, `ComposeError` for non-default; future-bundled non-default name -> fallback NOT consumed, gated on `DEFAULT_V2_SELECTION_NAMES`). `tests/test_packs_cli_v0_5.py` adds a `V054DedupTests` class (helper, loader, and `--fix` self-heal), a `V054BundledDefaultsNotReconciledTests` class (URL form for `agent-style` does not appear in the planned project YAML write), a `V054MigrateLegacyAcTests` class (`.claude/commands/` backup + skip-when-absent), and a `V054BootstrapAutoReconcileTests` class (bootstrap success invokes `main(["pack", "verify", "--fix", "--yes"])`; bootstrap failure or missing user-config skips it). 13 new tests, all green; full suite at 769 passed.

### Migration

- **Existing AA users**: re-run `anywhere-agents` to pick up the post-bootstrap reconcile. Existing `pack-lock.json` entries are unchanged; the bundled-default no-reconcile rule applies on the next `pack verify --fix`.
- **Legacy AC consumers**: re-run `anywhere-agents` from the project root. The CLI detects the AC bootstrap state (`.agent-config/upstream` containing `yzhao062/agent-config` or `.agent-config/repo/.git/config` pointing at `agent-config`), backs up `.claude/commands/` to `.claude/commands.bak-<timestamp>/`, re-clones from `anywhere-agents`, deploys bundled defaults, then auto-reconciles user-level packs. User-customized files in the backup directory can be manually re-applied if needed.
- **User-level config corruption**: any duplicate-name entries are silently dropped on the next `pack verify [--fix]` or `pack add` invocation. The `--fix` path explicitly logs the drop count.

## [0.5.3] — 2026-04-27

### Highlights

- **Drift gate adopt-on-match.** v0.5.2's drift gate rejects any pre-existing unmanaged file at a target path, blocking AC→AA migrations, interrupted `pack add` resumptions, team-clone first runs (where `.claude/skills/` is committed but `.agent-config/pack-lock.json` is gitignored), and manual / sibling-tool deploys to skill paths. v0.5.3 keeps the rejection for content drift but adopts the file into `pack-lock.json` when its on-disk sha256 already matches what the pack would write. The protection against silent user-edit clobber is unchanged: hash mismatch still aborts with the same `DriftAbort` and recovery line.

### Changed

- **`Transaction._validate_prestate` (`scripts/packs/transaction.py`).** The `PRESTATE_UNMANAGED` branch now compares the on-disk sha256 against the staged op's `new_content_sha256`. Match → record path on the new `Transaction.adopted_paths` list and skip the drift; subsequent `_apply_op` rewrites byte-identical content, so the lockfile entry the dispatcher already recorded becomes the authoritative ownership record. Mismatch (or `new_content_sha256` missing) → keep the v0.5.2 reject behavior. `OP_DELETE` and the four non-unmanaged categories are unchanged.
- **Composer adoption logging (`scripts/compose_packs.py`).** After commit, when `Transaction.adopted_paths` is non-empty, the composer emits one stdout line — `ℹ composer adopted N pre-existing file(s) into pack-lock.json (content matched pack output):` — followed by the absolute paths in the same 2-space-indent style as the drift error. Silent adoption is avoided so the user sees what crossed the gate.

### Internals

- **`Transaction.adopted_paths`.** New instance attribute, initialized empty in `__init__`. Populated by `_validate_prestate` on every gate run; the same field is used by single-file collisions and per-file directory-handler scenarios (the `kind: skill` handler stages one op per file under `.claude/skills/<name>/`, so a 19-file skill that fully matches reports 19 entries).
- **Five new tests in `TransactionDriftGateTests`.** `test_unmanaged_collision_matching_content_adopted` asserts the new adoption path; `test_unmanaged_collision_mismatched_content_rejected` pins the negative case; `test_unmanaged_collision_partial_match_rejects_only_mismatch` covers the per-file directory scenario where some files match and some are user-edited; `test_adopted_paths_initialized_empty` and `test_unmanaged_no_collision_clean_install_no_adopt` pin the no-adopt baseline.

### Migration

- v0.5.2 users hitting the drift gate on AC→AA migration / interrupted `pack add` / team-clone first run / manual deploy can now upgrade to v0.5.3 and rerun the original command. No backup / move dance required when content already matches what the pack would write. If the pre-existing file's content differs (genuine user edit), the recovery path is unchanged: back up local edits, then rerun.

## [0.5.2] — 2026-04-27

### Highlights

- **One-shot pack lifecycle.** v0.5.1's nine-command pack-deploy dance collapses into one invocation per command. `anywhere-agents pack add <url>` now writes user-level config, project-level `agent-config.yaml`, and runs the composer subprocess in a single shot, replacing the v0.5.1 split where `pack add` only registered. `pack verify --fix` similarly reconciles user ↔ project configs bidirectionally and invokes the composer; `pack remove <name>` deletes from both configs and runs the new single-pack composer uninstall mode. Identity rules apply at every layer: same `(name, normalized_url, ref)` is idempotent; same `name` with different identity returns rc=1 with no writes.
- **AC→AA migration on `anywhere-agents` (no subcommand).** When the cwd has a legacy `agent-config` bootstrap state — `.agent-config/repo/.git/config` pointing at `yzhao062/agent-config` OR `.agent-config/upstream` containing `yzhao062/agent-config` — the CLI silently wipes the cached repo + bootstrap files and re-clones from anywhere-agents. Removes the v0.5.1 silent no-op where the existing-dir check skipped re-clone and the AA composer never landed.
- **Cross-OS bootstrap entries.** Both `bootstrap/bootstrap.sh` and `bootstrap/bootstrap.ps1` now copy each other from the sparse clone after the clone completes. A Windows user running Git Bash / WSL on a project bootstrapped from PowerShell no longer hits "No such file or directory" on `.agent-config/bootstrap.sh`, and vice versa.
- **Banner item 7 reports pack updates.** Session start banner item 7 now shows both `gap_count` (user-level packs not deployed in project) and `update_count` (packs whose remote HEAD has moved past `resolved_commit`). The composer records `latest_known_head = resolved_commit` at fetch time; `pack verify` runs `git ls-remote --exit-code` opportunistically (5s per-pack timeout, mutable refs only — 40-char SHAs skip the network call) and lock-bracket-merges the resolved head into `.agent-config/pack-lock.json`.

### Added

- **`scripts/packs/transaction.py` drift gate.** `Transaction.commit()` accepts an `expected_prestate` map keyed by absolute target path; each value is a `(category, recorded_sha256)` tuple. Five categories: pack outputs (recorded sha256 must match current), internal state files / composer-owned core / declared JSON merge targets (stage-time hash must equal commit-time hash — optimistic concurrency), and unmanaged (existing file → reject as collision; absent → allow). The composer builds the map after every staged write, before commit. Mismatches raise `DriftAbort` with a per-path reason; the staging dir is rolled back so no on-disk target is mutated. Empty `expected_prestate` (the v0.4.0/v0.5.0 default) preserves the previous behavior unchanged.
- **`scripts/packs/uninstall.py:run_uninstall_pack(name)`.** Single-pack uninstall path used by `pack remove`. Filters user-state owners by composite key `(repo_id, pack)` so two repos installing the same pack name from different upstreams don't decrement each other; computes remaining-output-claims from other lock entries before deleting shared outputs; preserves drift-safe retry semantics (any owned output whose hash drifted leaves the pack's lock + state ownership records intact). File-like user-level outputs follow the empty-owners-AND-hash-match delete rule; active-permission entries only prune state-side owner records and never touch `~/.claude/settings.json` (JSON unmerge stays out of v0.5.2 scope).
- **Lock schema fields `latest_known_head` + `fetched_at`.** Both optional. Composer populates them at fetch time (head equals `resolved_commit` because the fetch just happened); `pack verify` updates `latest_known_head` later. Old locks without these fields parse cleanly and contribute zero to `update_count`.
- **`compose_packs.py uninstall <name>` mode.** New subcommand on the composer subprocess that drives `run_uninstall_pack`. Used by `pack remove`. Composer self-locks; the CLI does not hold outer locks across the subprocess invocation.

### Changed

- **`pack add` behavior split**. Outside a bootstrapped project (no `.agent-config/repo/scripts/compose_packs.py` and no bootstrap scripts in `.agent-config/`) the CLI registers user-level only and prints `ℹ Registered globally. Run anywhere-agents in a bootstrapped project to deploy.` In-project the CLI also writes `agent-config.yaml` and invokes the composer.
- **`pack verify --fix` is now bidirectional.** User-only rows write to `agent-config.yaml`; project-only rows write to user-level config; mismatch rows return rc=1 with no writes. Bundled-default packs (`agent-style`, `aa-core-skills`) are excluded from reconcile to avoid churn since the composer always materializes them via `DEFAULT_V2_SELECTIONS`. After config writes, the composer subprocess runs.
- **`pack remove` is cascade delete.** Now removes from user-level config + project `rule_packs:` + invokes composer's uninstall mode for the single pack. v0.5.1's user-level-only behavior is gone. Not-found (in any of user / project / lock) now returns rc=1 instead of rc=0.
- **`pack verify` writes the lock.** Round-tripping `git ls-remote` results into `pack-lock.json` is the only side effect; the audit logic itself is unchanged. Network failures and timeouts skip per-pack so an offline `pack verify` still classifies the local state cleanly.

### Internals

- **DispatchContext gains `pack_latest_known_head` + `pack_fetched_at`.** Both default to `None`; composer populates them when threading inline-source archives. `finalize_pack_lock` writes the optional fields into the per-pack lock entry.
- **Five-category drift-gate classification in `_do_compose_v2`.** Pack outputs (from prior `pack-state.json`), internal state files (`pack-lock.json`, `pack-state.json`, user-level `pack-state.json`), composer-owned core (`AGENTS.md`), declared JSON merge targets (user-level `settings.json` from `active-permission` entries), and unmanaged. The fifth category prevents the permission handler's full-file rewrite of an existing `settings.json` from being rejected as an unmanaged collision.
- **CLI banner item 7 update path.** `_pack_verify` snapshots the lock before issuing `git ls-remote`, then takes the repo lock, re-reads, and merges per-entry by `(source_url, requested_ref, resolved_commit)` identity tuple. Concurrent composer / pack add writes between snapshot and merge cause that result to be skipped — no overwrite of a moved `resolved_commit`.

### Migration

- v0.5.1 users who scripted `pack add` then `pack verify --fix` then `bootstrap.sh` see redundant work but no failure. The new one-shot `pack add` does it all in one invocation; the legacy three-step recipe still works.
- A composer drift-abort after CLI's config writes have landed leaves a "registered but not deployed" intermediate state. Recovery: back up local edits to managed files, then rerun `pack add` (idempotent on configs) or `pack verify --fix`. Cross-command atomicity (true WAL-style commit-or-fail-together) is deferred to v0.5.3+.

## [0.5.1] — 2026-04-27

### Added

- `anywhere-agents pack verify [--fix]`: audits pack deployment state across user-level config, project-level `rule_packs:` in `agent-config.yaml`, and `pack-lock.json`, by `(name, source_url, requested_ref)` identity. Seven priority-ordered states: `deployed`, `user-level only`, `config mismatch`, `declared, not bootstrapped`, `broken state`, `lock schema stale`, `orphan`. Exit codes: 0 (all deployed), 1 (any gap), 2 (parse error). `--fix` writes matching `rule_packs:` entries to `agent-config.yaml` for `user-level only` packs under the project repo lock; the lock-held write re-gathers state and exits with rc=1 + named packs if any `user_only`, `mismatch`, `broken`, `orphan`, or `lock_stale` row remains after the write. Atomic write via temp + `os.replace`; never modifies `pack-lock.json` or generated outputs. Credential URLs (`https://TOKEN@host/...`) are case-insensitively rejected before any print or write so token-bearing entries cannot leak to stdout or get persisted.
- **Session Start Check item 7 — pack-deployment audit.** The session-start banner gains a Session check line entry that compares user-level pack identity tuples against project-level identity tuples (after URL normalization via `normalize_pack_source_url`) and emits `⚠ N user-level pack(s) not deployed (run anywhere-agents pack verify)` when the count is non-zero. Mirrors byte-for-byte across aa+ac `AGENTS.md` plus the generated `CLAUDE.md` and `agents/codex.md` per repo (six files total). `tests/test_check_parity.py` pins the cross-variant equality.

### Internals

- Vendored `scripts/packs/locks.py` into the PyPI wheel so `pipx install anywhere-agents` always has a working repo-lock helper. `scripts/vendor-packs.py` now lists `locks.py` alongside `auth.py`, `source_fetch.py`, and `schema.py`.
- `auth.reject_credential_url` and `auth.redact_url_userinfo` are now case-insensitive on URL schemes (`HTTPS://`, `Git+SSH://`, etc.) per RFC 3986; uppercase token URLs are rejected and redacted the same as lowercase forms.
- `source_fetch.normalize_pack_source_url` lowercases GitHub host case and owner/repo case for identity-tuple comparison; non-GitHub hosts get a minimal lowercase-host normalization.
- `_load_project_observations` preserves same-file duplicate names so two `profile` rows in `agent-config.yaml` with different refs surface as `config mismatch` instead of last-wins collapse. Cross-file local-overrides-tracked semantics preserved.
- `_identity_for_default_selection` reads `.agent-config/repo/bootstrap/packs.yaml` so default-seeded packs (`agent-style`, `aa-core-skills`) compare against the same source/ref the composer writes into the lock. Malformed bundled `packs.yaml` now propagates as exit 2 instead of silent fallback.

## [0.5.0] — 2026-04-26

### Highlights

- **Direct-URL pack consumption.** `agent-config.yaml` entries can now point at any GitHub URL with `source.url` and `source.ref`. The 4-method auth chain (SSH agent, `gh` CLI token, `GITHUB_TOKEN` env, anonymous fallback) negotiates access automatically; private repos work without manual checkouts. Public URLs succeed on anonymous; private URLs succeed on whichever authenticated method the host has configured.
- **Trust-model shift.** The default `update_policy` flipped from `locked` to `prompt`. Each bootstrap surfaces upstream drift via a banner listing the affected packs and files; the consumer applies or skips per-run. `ANYWHERE_AGENTS_UPDATE=apply` short-circuits the prompt for CI / scripted refresh; `update_policy: locked` remains available as an explicit per-pack opt-in for content that must never auto-refresh.
- **agent-pack v0.1.0 one-line install.** `anywhere-agents pack add https://github.com/yzhao062/agent-pack --ref v0.1.0` is the v0.5.0 acceptance test for the direct-URL path. Installs the three packs declared in the upstream `pack.yaml` (`profile`, `paper-workflow`, `acad-skills`) by default; `--pack <name>` filters to a subset.
- **CLI additions.** `anywhere-agents pack update <name>` performs an auth-aware ref refresh against the configured source. `anywhere-agents pack list --drift` runs a read-only scan against `.agent-config/pack-lock.json` and reports any pack whose resolved commit or input hash has drifted from the lock.
- **Internals.** `scripts/packs/{auth,source_fetch,schema,config,reconciliation}.py` are new. The `reconcile_orphans` orchestrator wrapper is now invoked by the composer's bootstrap entry path before the main compose step. The PyPI wheel vendors `auth`, `source_fetch`, and `schema` so `pipx install anywhere-agents` exposes the full CLI surface without requiring a sibling source clone.
- **Deferred to v0.6.0: end-to-end cmd-log harness for stricter CI token-leak coverage.** v0.5.0 ships the redaction primitives (`auth.redact_url_userinfo` + `auth.redact_secret_text`) and the `tests/test_packs_auth_chain.TestRedact*` unit assertions as the v0.5.0 token-leak guard. The CI smoke step that previously gated on `.agent-config/.test-cmd-log.jsonl` was a no-op because no v0.5.0 code wrote that file; the harness will land in v0.6.0.

### Migration

See `MIGRATIONS.md` for the 2026-04-26 entry. Existing bootstrap caches must seed-refresh once; `bash .agent-config/bootstrap.sh` (or the PowerShell equivalent) refreshes the cache and picks up the new env wirings.

### Also in this release

- **Docs overhaul: README, README.zh-CN, and docs/index.md aligned to v0.4.0 pack-architecture positioning.** Replaces the v0.3-era scenario-first narrative with a 4-paragraph Why section (multi-agent, many-repos, review loop, writing rules), a 4-paragraph How It Works that explicitly carries the v0.4.0 vs v0.4.x boundary (CLI writes user-level `packs:` today; bootstrap consumes user-level + project-level `packs:` in v0.4.x; legacy project-level `rule_packs:` remains the only bootstrap-active project key in v0.4.0), a new "What This Looks Like" section with five examples in five different visual formats (session-banner screenshot, post-bootstrap repo tree, dual-agent generation Mermaid, Without/With writing-style HTML table, guard-deny terminal mock), a Pack Management CLI section with embedded `docs/pack-cli-demo.gif`, and a What's Next paragraph. zh-CN README mirrors the same structure with warm conversational tone (你, not 您) and English technical terms preserved (`pack`, `composer`, `bootstrap`, `skill`, `hook`, `guard`). Title Case applied globally to README + docs/index.md headings per RULE-G.
- **Visual identity: USC cardinal `#990000` → warm burgundy `#8b2635`.** Hero PNG (`docs/hero.html` + re-rendered `docs/hero.png`), session-banner PNG (`docs/banner.html` + re-rendered `docs/session-banner.png`), README badge colors, Mermaid theme variables in README and three docs Mermaid blocks, and `docs/stylesheets/extra.css` for the Material light + slate schemes. Slate-mode body link contrast tuned to `#d36b77` / `#e4939b` for WCAG AA pass (4.71:1 / 6.86:1). New `docs/_render_hero.py` and `docs/_render_banner.py` helpers keep both PNGs reproducible.
- **CLI demo: `docs/pack-cli-demo.gif` (vhs).** New 18.8s GIF demonstrating `pack list` → `pack add aa-core-skills --ref v0.4.0` → `pack list` → `pack remove aa-core-skills` → `pack list`, rendered via the official `ghcr.io/charmbracelet/vhs` image pinned by digest. Tape (`docs/pack-cli-demo.tape`) + render helper (`docs/_render_gif.sh`) + demo wrapper scripts (`docs/_demo-helpers/`) committed for reproducibility.
- **README cleanup.** Dropped the Day-to-Day Usage collapsible (3 of 4 rows duplicated the Install section). Removed the duplicate "Claude Code + Codex primary support" bullet from Limitations and Caveats (already in What This Is Not). Updated Repo Layout to cover the v0.4.0 pack architecture (`bootstrap/packs.yaml`, `scripts/compose_packs.py`, `scripts/packs/`). Updated What This Is Not to reflect the v0.4.0 CLI + YAML manifest reality.

## [0.4.0] — 2026-04-23

### Added

- **Unified pack manifest (v2 schema).** `bootstrap/packs.yaml` replaces `bootstrap/rule-packs.yaml` as the canonical manifest; the old path continues to work as a loader alias through v0.5.x. The v2 schema adds an `active:` list alongside the existing `passive:` list, with four active `kind:` values (`skill`, `hook`, `permission`, `command`) and per-entry `hosts:` / `required:` semantics. Every v1 (legacy passive-only) manifest keeps parsing unchanged; consumer-visible `AGENTS.md` output is byte-identical for unchanged pack content.
- **Four active-kind handlers.** `kind: skill` deep-copies a skill directory into `.claude/skills/<name>/` and auto-emits a canonical `.claude/commands/<name>.md` pointer unless the manifest supplies an explicit pointer mapping (the four aa-shipped skills keep their custom pointer content via explicit mappings). `kind: hook` deploys hook files under `~/.claude/hooks/<pack>/NN-<name>.py` with manifest-order prefixes and merges an `owners:` list into `~/.claude/pack-state.json`. `kind: permission` merges declarative JSON into `~/.claude/settings.json`; distinct permission values from different packs coexist freely, matching values join owners, and the same logical output with different expected content fails closed with `user-level-output-conflict`. `kind: command` is a forward-compat slot (parse + warn + no-op in v0.4.0).
- **Pack-emitted command pointers for the four shipped skills.** `implement-review`, `my-router`, `ci-mockup-figure`, and `readme-polish` convert to `kind: skill` entries in `bootstrap/packs.yaml` bundled under the `aa-core-skills` pack. The four `.claude/commands/*.md` pointer files remain in the aa source tree during v0.4.0 for the no-PyYAML fallback path and drop from `scripts/check-parity.sh`'s STRICT list on the ac mirror side.
- **State files tracking pack lifecycle.** `.agent-config/pack-lock.json` records per-pack source identity (URL, requested ref, resolved commit) + per-file sha256 for every composed output. `.agent-config/pack-state.json` records project-local outputs (skill directories, command pointers). `~/.claude/pack-state.json` records shared user-level outputs (hooks, settings.json merge targets) with an `owners:` list keyed by `(kind, target_path)` so two consumer repos installing the same pack coexist safely. Full schema in `docs/pack-architecture.md` § "pack-lock.json schema".
- **Recoverable staged-transaction primitives.** The transaction journal (`transaction.json`), staging directory, per-file atomic rename (`os.replace`), and Windows 2-retry fallback for AV / IDE sharing violations ship as building blocks in `scripts/packs/transaction.py`, and composition routes output writes through them. Per-user and per-repo file locks (`flock` POSIX, `msvcrt.locking` Windows) and orphan reconciliation helpers (`scripts/packs/reconciliation.py`, classifying `LIVE` / `ROLLBACK_OK` / `ROLLFORWARD_OK` / `PARTIAL` / `DRIFT` / `MALFORMED`) are implemented and unit-tested with cross-process contention smokes, but **automatic startup reconciliation and composer-side multi-lock wiring are not yet enabled in `scripts/compose_packs.py`** — that wiring is a v0.4.x follow-up.
- **User-level config groundwork (CLI-writable; composer not yet consuming).** User-level config at `$XDG_CONFIG_HOME/anywhere-agents/config.yaml` (or `$HOME/.config/anywhere-agents/config.yaml`) on POSIX and `%APPDATA%\anywhere-agents\config.yaml` on Windows is writable via the new CLI (see below). `scripts/packs/config.py` implements the full 4-layer resolver (user-level → project-tracked `agent-config.yaml` → project-local `agent-config.local.yaml` → `AGENT_CONFIG_PACKS` env var) with explicit `packs: []` clear semantics and names-only env-var grammar (`AGENT_CONFIG_PACKS="name1,-name2"` with `-name` subtract). **In v0.4.0, `scripts/compose_packs.py` still resolves bootstrap selections through the legacy project-tracked / project-local / env path**; wiring the composer to the new 4-layer resolver so user-level entries actually drive consumer bootstrap is a v0.4.x follow-up. Writing to user-level config via the CLI is useful now for pipx / pip users who want to stage their pack preferences ahead of the composer-side wiring.
- **CLI subcommands: `pack add/remove/list` + `uninstall --all`.** `anywhere-agents pack add <source> [--name NAME] [--ref REF]` writes to the user-level config file atomically (temp + `os.replace`) with malformed-YAML refuse-to-rewrite. First-add default preservation: on an empty user-level config, `pack add` seeds `[{name: agent-style}, {user pack}]` so the default rule pack is not silently dropped. Legacy `rule_packs:` keys are migrated to `packs:` on first write. `anywhere-agents uninstall --all` delegates to the composer's internal uninstall engine (invoked via `.agent-config/repo/scripts/packs/uninstall.py`) and maps six typed outcomes to CLI exit codes: `0` (clean or no-op), `10` (lock timeout), `20` (drift), `30` (malformed state), `40` (partial cleanup). Drift is fail-closed: state files stay unchanged so the user can retry after manually resolving the drift.
- **Auth safety preconditions.** Credential-bearing source URLs are rejected at parse time across every config layer: HTTP(S) userinfo (`user@`, `user:pass@`, `<token>@`) and SSH URLs with a password component (`ssh://user:secret@host`) both reject; SSH transport usernames (`git@host:path`, `ssh://git@host/path`) pass through. Composer subprocess env sets `GIT_TERMINAL_PROMPT=0` and `GIT_SSH_COMMAND=ssh -o BatchMode=yes -o ConnectTimeout=10` so missing credentials surface as a bounded fetch error instead of hanging on an interactive prompt. GitHub URL normalization (`github.com` only) extracts the canonical `<owner>/<repo>` identity from the three URL shapes so the v0.5.0 auth chain can retry alternate methods on the same identity.
- **npm CLI pack-command delegation.** `npx anywhere-agents pack add/remove/list` and `npx anywhere-agents uninstall --all` shell out to the pipx-installed Python entry point (or `python -m anywhere_agents.cli` fallback). npm-only users who only invoke the bootstrap path see no behavior change; users who need pack management get an install hint pointing at `pipx install anywhere-agents`.

### Changed

- **`bootstrap/bootstrap.sh` and `bootstrap/bootstrap.ps1` call `compose_packs.py`** (the new unified composer) with a BC fallback to `compose_rule_packs.py` on pre-v0.4.0 sparse clones that predate the new script.
- **Generator emits LF on all platforms.** `scripts/generate_agent_configs.py` now passes `newline="\n"` to `write_text()` so regenerated `CLAUDE.md` / `agents/codex.md` match their committed LF-normalized form on Windows without relying on `.gitattributes` checkout conversion. Removes a false-positive `diff -q` failure in `scripts/pre-push-smoke.sh` on Windows.
- **Deprecation warnings.** `rule_packs:` key in `agent-config.yaml` and `AGENT_CONFIG_RULE_PACKS` env var are accepted with a `DeprecationWarning` through v0.6.x. Behavior preserved for v0.3.x consumers; a v1.0.0 release will promote these to hard-fail with an actionable migration error. `rule-packs.yaml` path in `.agent-config/repo/bootstrap/` similarly continues to work as a loader alias with `packs.yaml` taking precedence when both exist.

### Not yet available

- **Private-source packs.** The v2 schema accepts structured `source: {repo, ref}` URLs but v0.4.0's composer rejects any private source (SSH, explicit `auth:` field) at parse time with a "v0.5.0 feature" message. The noninteractive fetch env + URL normalization helpers ship in this release as preparation; the actual auth chain (SSH agent → `gh` CLI token → `GITHUB_TOKEN` → anonymous fallback) activates in v0.5.0.
- **User-level config + 4-layer resolver in consumer bootstrap.** The resolver exists in `scripts/packs/config.py` and the CLI writes the user-level file; bootstrap composition still resolves selections through the legacy project-tracked / project-local / env path. Wiring `scripts/compose_packs.py` to the new resolver (so user-level entries drive bootstrap) is a v0.4.x follow-up.
- **Composer-side lifecycle wiring.** Per-user and per-repo locks around the composition transaction, plus automatic startup orphan reconciliation, are implemented as primitives in Phase 2 and fully unit-tested but not yet invoked by `scripts/compose_packs.py`. Single-process composition in a well-behaved environment works correctly; concurrent-composer and mid-crash recovery scenarios are protected by the transaction layer's per-file atomic rename but not by the lock + reconciliation outer layer until the wiring lands in v0.4.x.
- **Default switch to `agent-style-field`.** The 9-rule slim variant planned for v0.4.x depends on `agent-style` shipping the slim pack first; `DEFAULT_SELECTIONS` stays on the full `agent-style` pack in v0.4.0.

### Review history

Five combined implementation phases (Phases 1-4+5 per `docs/pack-architecture.md`) went through `implement-review` Phase 0 plan-review (3 rounds on `archive/plans/PLAN-aa-v0.4.0.md`) plus per-phase code review with Codex (every phase) and GitHub Copilot (Phases 3 + 4+5 portions). Total findings: 14 High + 9 Medium + 4 Low across all rounds, every one Fixed or Deferred-with-rationale and closed before the corresponding phase commit. Copilot's first-pass review on Phase 3 suffered 10 hallucinated findings against non-existent files; a model switch for subsequent rounds resolved the issue. Codex was the only reviewer for Phase 4+5 after Copilot hit usage limits.

470 tests pass on Windows + Linux CI at the release-candidate commit, covering `os.replace` atomicity, cross-platform lock primitives (`fcntl.flock` POSIX / `msvcrt.locking` Windows), merkle `dir-sha256` computation on forward-slash and backslash path separators, and multiprocessing-based cross-process contention tests.

## [0.3.0] — 2026-04-21

### Added

- **Rule-pack composition in bootstrap.** Bootstrap can now stitch external always-on instruction bundles ("rule packs") into the composed `AGENTS.md` at install time. First rule pack: [`agent-style`](https://github.com/yzhao062/agent-style) (21 writing rules, pinned at `v0.3.2`), enabled by default. Consumers opt out with `rule_packs: []` in `agent-config.yaml` at the project root; customize with `rule_packs: - name: agent-style` plus optional `ref:`, or layer with `agent-config.local.yaml` (gitignored machine-local override) or `AGENT_CONFIG_RULE_PACKS` env var (transient one-run). Composition requires Python 3 + PyYAML; bootstrap attempts a best-effort `pip install --user pyyaml` when missing, and falls back to the verbatim upstream `AGENTS.md` plus a one-line tip when Python or PyYAML still are not available — no hard error. Covers the `bash` and `powershell` bootstrap paths symmetrically with matching CLI contracts (`--rule-packs PACK` dry helper, `--no-cache` refetch flag, `--help` usage).
- **`bootstrap/rule-packs.yaml` manifest** registering known rule packs. Adding a second rule pack is a PR that adds a manifest entry plus the pack author publishing `docs/rule-pack.md` at a stable git ref.
- **`scripts/compose_rule_packs.py` helper** implementing manifest parsing, config resolution across the four opt-in layers (tracked / local / env / dry helper flag), raw-GitHub fetch with SHA-256 cache, routing-marker validation regex as conservative superset of the per-agent generator grammar, and atomic temp-plus-rename write of the composed `AGENTS.md`.
- **`tests/test_compose_rule_packs.py`** with 90+ tests covering parser, composition golden-file, cache semantics (fetch-first / fallback / `--no-cache` always errors), path-traversal regression (user-controlled ref percent-encoded in cache filename), PyYAML-missing fallback, and CLI contracts for both `bootstrap.sh` and `bootstrap.ps1`.
- **`docs/rule-pack-composition.md`** long-form spec: rule-pack vs skill-pack layering, default behavior, opt-in precedence, pack-author anatomy, composition flow, manifest schema, dependency contract, cache and offline behavior, failure modes, registration process, and a note on the historical `.agent-config/` scratch directory name.
- **README `Rule packs` section** with opt-out, pin-ref, dry-helper recipes, plus a collapsible Historical naming block.

### Changed

- **`bootstrap/bootstrap.sh` and `bootstrap/bootstrap.ps1` reordered the sparse clone** to happen before the root `AGENTS.md` write so the composer helper and manifest are available inside `.agent-config/repo/` at compose time. Runs that set `rule_packs: []` as an explicit opt-out, or that fall back because Python / PyYAML are unavailable, still receive a verbatim upstream `AGENTS.md`; the default no-config path now attempts rule-pack composition.
- **`agent-config.local.yaml` auto-gitignored** alongside `.agent-config/` on every bootstrap run, so machine-local rule-pack overrides do not leak into commits.

## [0.2.0] — 2026-04-18

README and RTD redesign on top of the plan-first workflow in `implement-review`. The README is re-centered around three pillars (portable sync, review workflow, mechanical enforcement) earned from daily use, and the 0.1.8 PreToolUse gates are surfaced as a first-class feature via a reframed Scenario D covering four reader-facing gate families. The RTD `/skills/` rendering bug (Material icon shortcodes showing as literal text) is fixed.

### Added

- **`skills/implement-review/SKILL.md` "When to plan-review first" section.** Formalizes plan-review as Phase 0 before the existing staged-change review loop, for complex tasks where the shape of the work precedes and constrains execution (code refactors, paper outlines, proposal structure, data-pipeline redesigns, migration plans, release-process changes, etc.). Process: write `PLAN-<identifier>.md` in the most natural location for the task (repo root for code, paper-repo root for Overleaf-style docs, local scratch directory for non-git work), send to Codex as a pre-execution design review that reads the plan path rather than `git diff --cached`, iterate until clean, then execute and run the normal review cycle on the staged output. Scenario B in both READMEs gains a one-sentence mention.
- **Pre-scenarios "A day in this config" prelude** in `README.md` and `README.zh-CN.md`. Light phase-of-day rhythm (morning setup, midday review, afternoon drafting, evening safety check, session defaults in the background) so the scenarios read as daily behavior, not a feature list.
- **Section 0 benefit-preview sentence**: *"It is not only a style guide: hooks stop risky commands from proceeding silently and block flagged prose writes before they land."* Sets up the enforcement story before the scenarios. Covers both ask-behavior (destructive Git/GitHub) and deny-behavior (compound `cd`, writing-style, banner).
- **Scenario C enforcement cross-reference.** One sentence noting the ~40-word AI-tell ban is enforced by a PreToolUse hook on `.md`/`.tex`/`.rst`/`.txt` writes; pointer to Scenario D for the mechanism.
- **Limitations escape-hatch entry.** `AGENT_CONFIG_GATES=off` documented in both the English and Chinese Limitations sections for discoverability when a false positive blocks real work. Pairs with the Scenario D footer coverage.

### Changed

- **Section 0 "Why you want this" rewrite** in `README.md` and `README.zh-CN.md`. Keeps the cross-project drift opener (scattered per-repo `CLAUDE.md`, copy-paste divergence, only-in-your-head) and adds a daily-use evolution paragraph naming the three shipped pillars (portable sync, review workflow, mechanical enforcement) as the output of daily use rather than a curated feature list.
- **Scenario D renamed: "Git safety catches mistakes before they happen" → "Mechanical enforcement."** Restructured around four reader-facing gate families: destructive Git/GitHub asks for confirmation; compound `cd` is denied; writing-style banned-word writes on `.md`/`.tex`/`.rst`/`.txt` are denied; user-visible mutating tool calls before the session banner lands are denied (read-only and dispatch tools like `Read`, `Grep`, `Skill`, `Task` stay available so the agent can inspect state). Organizing idea: *what the hook intercepts before an agent action proceeds.* Keeps the force-push deny example as the vivid lead.
- **Scenario E default-stack table.** Guard-hook row expanded to describe all four gate families (asks for confirmation on destructive Git/GitHub; denies compound `cd`, writing-style banned words, and pre-banner user-visible mutating tool calls) instead of only "muscle-memory destructive commands."
- **`mkdocs.yml` adds `pymdownx.emoji` block** with Material's `emoji_index` and `emoji_generator`. Required by mkdocs-material to render `:material-*:` and `:octicons-*:` shortcodes as SVG icons; without it, `attr_list` alone leaves the shortcodes as literal text.
- **Reference-section sweep (both READMEs).** Repo layout now lists `packages/` (PyPI + npm CLI sources), `.githooks/`, all 5 files in `scripts/` (including `pre-push-smoke.sh` and `remote-smoke.sh`), root-level `CHANGELOG.md`/`CONTRIBUTING.md`/`RELEASING.md`/`LICENSE`/`mkdocs.yml`/`.readthedocs.yaml`, and the expanded CI matrix (Ubuntu + Windows + macOS, Python 3.9-3.13, 3 workflows). `scripts/guard.py` description upgraded from "blocks destructive commands" to the four-family framing; `skills/implement-review/` description mentions Phase 0 plan-review. "What is opinionated and why" table refreshed: safety-first row acknowledges the `AGENT_CONFIG_GATES=off` escape hatch; dual-agent row mentions optional Phase 0 plan-review; writing-style row notes PreToolUse enforcement. Scenario A "what appears in your project" tree adds `CLAUDE.md` and `agents/codex.md` (generated at bootstrap since 0.1.3).
- **`README.zh-CN.md` Fork step 3 catch-up.** The Chinese README had been carrying a pre-0.1.6 one-liner ("改他们 bootstrap 块里的 URL"). Replaced with the full argv / `AGENT_CONFIG_UPSTREAM` env var / persisted-file cascade explanation already in the English README since 0.1.6.

### Fixed

- **RTD `/skills/` page icon-shortcode rendering.** Material icon shortcodes in the skills card grid (`:material-magnify-scan:`, `:material-routes:`, `:material-image-frame:`, `:material-book-open-outline:`, `:octicons-arrow-right-24:`) rendered as literal text. Adding the `pymdownx.emoji` block restores icons on all four skill cards and the "Deep docs" arrows.

### Review history

0.2.0 went through two rounds of `implement-review` Phase 0 plan-review (plan-first mode against `agent-config/PLAN-readme-redesign.md`) plus the execution-phase staged-diff review.

- **Plan-review Round 1**: 5 findings (2 Medium + 3 Low) covering release-scope inconsistency on plan-first, "three gates" undercounting the canonical Mechanical Enforcement table, escape-hatch discoverability, Section 0 implementation-first wording, and a `zh-CN` scope-statement mismatch. All Fixed in Revision 2 of the plan.
- **Plan-review Round 2**: 5 findings (2 Medium + 3 Low) covering Section 0 over-centering on enforcement (dropping the portability story and the self-defeating "eventually give up" phrasing), prelude restating scenarios too literally, Move 3 (origin markers on scenario headings) not worth the cost, benefit-sentence accuracy across ask/deny families, and collapsing release-scope branches once 0.2.0 was accepted. All Fixed in Revision 3; Move 3 deferred.
- **Maintainer spot-check** (between Round 2 and execution review): caught stale content in reference collapsibles that the plan-review rounds did not scope — the Repo layout tree, the "What is opinionated and why" Safety-first row, the Scenario A project tree (both EN + zh-CN), and `README.zh-CN.md` Fork step 3 which had been carrying a pre-0.1.6 one-liner. Fixed in-place before the execution review.
- **Execution review Round 3**: 2 findings (1 Medium + 1 Low) covering banner-gate wording that overstated "all tool calls" (canonical behavior exempts read-only and dispatch tools like `Read`, `Grep`, `Skill`, `Task`, so only user-visible mutating tools are denied) and this review-history list's completeness. All Fixed.
- No High-priority findings in any round.

## [0.1.9] — 2026-04-18

Stabilization of 0.1.8's mechanical enforcement. 0.1.8 shipped writing-style and banner gates backed by user-level global flag files at `~/.claude/hooks/`, which caused three production regressions within hours: multi-session ping-pong between different consumer projects, `Skill` / `Task` / `TodoWrite` dispatch blocked on turn-1 slash commands, and source-repo maintenance friction. 0.1.9 moves the banner-gate state per-project, expands the exempt list to cover observation and dispatch tools, and tightens the ack-file path exemption to exact equality.

### Fixed

- **Multi-session ping-pong** between different consumer projects. Flag files move from `~/.claude/hooks/session-event.json` / `banner-emitted.json` (global) to `<project-root>/.agent-config/session-event.json` / `banner-emitted.json` (per-project). `<project-root>` is resolved by walking up from `os.getcwd()` until a directory with `.agent-config/bootstrap.{sh,ps1}` is found. Same helper duplicated in `guard.py` and `session_bootstrap.py` so both ends of the contract agree. Opening Claude Code in two different consumer repos no longer cross-invalidates each other's banner acks.
- **Banner gate blocked slash-command dispatch on turn 1.** `BANNER_GATE_EXEMPT_TOOLS` now also exempts `Skill`, `Task`, `TodoWrite`, `BashOutput`, `WebFetch`, `WebSearch`, `ToolSearch`, `LS`, `NotebookRead`. `/implement-review`, `/loop`, `/schedule` and similar slash commands dispatch without a forced round-trip. User-visible write tools (`Bash`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `KillShell`, MCP mutating tools) remain gated.
- **Source-repo maintenance hit the banner gate.** `_find_consumer_root()` returns `None` in `agent-config` / `anywhere-agents` themselves (no `.agent-config/` at the root), so the banner gate skips and maintainers can edit without friction. The writing-style gate is unchanged — `.md` / `.tex` / `.rst` / `.txt` writes still block banned AI-tell words, in source repos as well as consumer repos.
- **Ack-file path exemption tightened.** 0.1.8 exempted any `Write`/`Edit`/`MultiEdit` whose path ended in `.agent-config/banner-emitted.json`, which allowed off-root or cross-project ack writes to bypass the gate. 0.1.9 resolves `consumer_root` first and requires exact normalized equality (`normcase(normpath(abspath(...)))`) with `<consumer_root>/.agent-config/banner-emitted.json`.

### Added

- **`tests/test_session_bootstrap.py`** — subprocess-based tests covering per-project event write from cwd and nested-cwd launches, no-op behavior in unrelated directories, legacy-flag cleanup with temp `HOME`/`USERPROFILE`, and source-repo no-event-write.
- **Expanded banner-gate tests in `tests/test_guard.py`** — per-project isolation across two tmp consumer dirs, walk-up from nested cwd, exact-path ack exemption (with off-root and cross-project denial), source-repo gate skip, and the new exempt tools (`Skill`, `Task`, `TodoWrite`, `LS`, `NotebookRead`).

### Changed

- **`scripts/session_bootstrap.py`** — now writes `session-event.json` at the walked-up consumer root (not raw `os.getcwd()`), so a nested-cwd launch still places the event file where `guard.py` will look for it. Also runs a one-time cleanup of 0.1.8's orphan global flag files.

### Compatibility

- **Transition note.** The first Claude Code session after upgrading from 0.1.8 to 0.1.9 may not be mechanically banner-gated. Mechanism: Claude Code's `SessionStart` hook invokes the old `session_bootstrap.py` (still 0.1.8 at the moment of the hook fire), which writes the legacy global flag; the old `session_bootstrap.py` then runs `bootstrap.sh`, which pulls upstream and deploys the new 0.1.9 `guard.py` + `session_bootstrap.py` to `~/.claude/hooks/`. For the remainder of that session, the new `guard.py` reads the per-project ack file, which has not been created yet by the (old) `session_bootstrap.py`, so the gate silently passes. Next `SessionStart` runs the new `session_bootstrap.py`, writes the per-project event, and the gate resumes normal operation. The banner rule still fires at prompt level during the transition session, so the banner itself should still appear.
- **0.1.8's global flag files** under `~/.claude/hooks/` are obsoleted by 0.1.9. `session_bootstrap.py` cleans them up on each run. No user action required.
- **`AGENT_CONFIG_GATES=off` escape hatch** still works identically.

## [0.1.8] — 2026-04-17

Mechanical enforcement upgrade. Adds two `PreToolUse` gates to `scripts/guard.py` (deployed to `~/.claude/hooks/guard.py` by bootstrap) so that writing-style rules and the session-start banner are now enforced by hooks, not just prompt-level compliance. Session banner also re-emits on resume / compact / clear via a flag-file mechanism. Closes multiple observed gaps where 0.1.7's rules were skipped in practice.

### Added

- **Writing-style gate in `scripts/guard.py`.** `PreToolUse` hook now denies any `Write` / `Edit` / `MultiEdit` to `.md` / `.tex` / `.rst` / `.txt` files when the outgoing content contains a banned AI-tell word from `AGENTS.md` Writing Defaults. The deny message lists the offending words so the agent can revise. Code files (`.py`, `.js`, etc.) are not checked — banned words rarely appear naturally in code, and docstring false positives would be a usability regression. Close-variant matching via word boundaries.
- **Banner emission gate in `scripts/guard.py`.** `PreToolUse` hook now denies any tool call (other than `Read`, `Grep`, `Glob`, or a `Write` to `~/.claude/hooks/banner-emitted.json`) while `~/.claude/hooks/session-event.json.ts > ~/.claude/hooks/banner-emitted.json.ts` — i.e., while a SessionStart event is pending but the banner has not been emitted for it. Forces the agent to emit the banner before doing real work; the gate lifts after the agent writes the acknowledgment file. Read/Grep/Glob remain exempt so the agent can still inspect state.
- **`session_bootstrap.py` writes `~/.claude/hooks/session-event.json`** on every SessionStart hook fire (fresh startup, resume, clear, compact all produce a fresh timestamp). The file contains a single `{"ts": <unix-ts>}`, overwritten each time. Combined with the banner gate, a fresh SessionStart event mechanically blocks work until the banner is re-emitted.
- **Dual-runtime turn-start banner procedure in `AGENTS.md`.**
  - *Claude Code branch:* read `session-event.json` and `banner-emitted.json` before each response. If the event timestamp is newer than the emitted timestamp (or only `session-event.json` exists), emit the banner as the literal first content of the response and write the event `ts` into `banner-emitted.json`. The flag-file mechanism covers all four SessionStart lifecycle events; the banner gate in `guard.py` enforces it mechanically.
  - *Codex branch:* Codex has no `SessionStart` hook equivalent and no guard.py hook runs during a Codex invocation. Each Codex invocation is a new session; emit the banner on the turn with no prior assistant turns in context (the first response of the invocation) and skip on subsequent turns. Enforcement remains prompt-level for Codex.
- **Mechanical Enforcement section in `AGENTS.md`** documenting the gates, their tool scope, triggers, and actions; plus the `AGENT_CONFIG_GATES=off` escape hatch.
- **`RELEASING.md` check #6: dual-OS pre-release test.** Maintainer runs the full test suite on the Spark release-gate box (ARM64 Ubuntu) via SSH before tagging, using the shared-core agent-config clone. Windows-only local coverage misses POSIX path handling and shell differences; CI runs x86_64 Ubuntu, so Spark adds ARM64. Command + interpretation documented inline.

### Changed

- **`scripts/guard.py` is no longer Bash-only.** The hook now dispatches by `tool_name`, runs the two new gates first (for tools they cover), and falls through to the existing Bash-only checks (compound-cd, destructive git, destructive gh). Legacy hook payloads without `tool_name` fall through to the Bash path for backward compatibility.

### Fixed

- **Writing-style rules are now enforced, not only prompt-level.** Prior releases listed ~40 banned AI-tell words in `AGENTS.md` Writing Defaults but relied on agent compliance. Observed behavior showed occasional slips. The new writing-style gate blocks at tool-call time so the banned words cannot reach prose files.
- **Banner fires on task-oriented first prompts.** 0.1.6 addressed `superpowers:using-superpowers` skill-first behavior, but did not cover the plain "user types a task and agent jumps in" case. 0.1.7 sessions still occasionally missed the banner for that reason (observed in a real fresh-session screenshot). The new banner gate + the checklist-style turn-start procedure make the emission unambiguous, and the gate mechanically blocks any tool-based progress until the banner lands.
- **Banner re-appears on resume / compact / clear in Claude Code**, not only on turn 1 of a fresh conversation. SessionStart hook fires on all four lifecycle events; the flag-file mechanism + banner gate now route each event into a fresh banner emission.

### Escape hatch

Set `AGENT_CONFIG_GATES=off` (or `0` / `disabled` / `false` / `no`) via the `env` block in `~/.claude/settings.json` to disable the two new gates. The compound-cd / destructive-git / destructive-gh checks remain active. Useful when working on meta-documentation that quotes banned words as examples, or to bypass a false positive while a real fix is in flight.

### Compatibility

- Existing consumers on 0.1.7 caches: self-update pulls the 0.1.8 bootstrap on next session. The updated `session_bootstrap.py` starts writing `session-event.json` on every SessionStart fire; the updated `AGENTS.md` rule takes effect on the next session after bootstrap. No user action required.

## [0.1.7] — 2026-04-17

Session-start banner now surfaces Claude Code + Codex version status (current → latest + auto-update state). Bootstrap heals a Claude Code auto-update gotcha left over from npm/winget-era installs.

### Added

- **Version-aware session banner.** The Claude Code and Codex lines now show current version, latest version (drift indicated with ` → `), and Claude Code's auto-update state:

  ![session-start banner example](https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/docs/session-banner.png)

  Text form:

  ```
  📦 anywhere-agents active
     ├── OS: win32
     ├── Claude Code: 2.1.112 → 2.1.115 (auto-update: on) · Opus 4.7 · effort=max
     ├── Codex: 0.121.0 → 0.122.0 · gpt-5.4 · xhigh · fast · fast_mode=true
     ├── Skills: 4 shared (ci-mockup-figure, implement-review, my-router, readme-polish)
     ├── Hooks: PreToolUse guard.py, SessionStart session_bootstrap.py
     └── Session check: all clear
  ```

  When versions match, the ` → <latest>` half is omitted and the banner just shows `Claude Code: 2.1.115 …`. `auto-update: off` appears when `autoUpdates: false` is still present in `~/.claude.json` (see Fixed below) or `DISABLE_AUTOUPDATER=1` is set in the effective env.

- **`session_bootstrap.py` version cache.** The SessionStart hook now refreshes `~/.claude/hooks/version-cache.json` from the npm registry (`@anthropic-ai/claude-code` and `@openai/codex`) once per 24 hours. The banner reads this cache; on cache hit the session starts with zero extra latency. On network failure, the cache keeps the last-known values and the banner still shows current versions without the `→ latest` half.

### Fixed

- **Bootstrap heals legacy `autoUpdates: false` in `~/.claude.json`.** Consumers who migrated from npm or winget to the native Claude Code installer may have a stale `"autoUpdates": false` flag blocking the native updater daemon from spawning at launch (observed behavior: `autoUpdatesProtectedForNative: true` does not actually neutralize it in that path). Bootstrap now flips the stale flag to `true` on every run. To genuinely disable auto-updates, use `DISABLE_AUTOUPDATER=1` via the `env` block in `~/.claude/settings.json` — that takes precedence and is the only supported opt-out path going forward.
- **`AGENTS.md` Environment Notes updated** to match the real fix path: the prior claim that `autoUpdatesProtectedForNative` neutralizes the legacy flag has been replaced with the observed behavior and the new bootstrap heal.

### Compatibility

- Existing consumers on 0.1.6 caches: self-update pulls the 0.1.7 bootstrap on next session. On the run after that, the autoUpdates heal fires if needed and the version cache populates. No user action required.

## [0.1.6] — 2026-04-17

Fork-friendly bootstrap — pass your upstream as the bootstrap argv, env var, or persisted file. Forkers no longer have to edit bootstrap scripts to point consumers at their fork; one command per consumer now carries the upstream for the life of that project. Also fixes a session-start-banner suppression by `superpowers` and a stale-origin bug on subsequent runs.

### Added

- **Upstream cascade in `bootstrap/bootstrap.{ps1,sh}`.** Resolution order is argv > env var (`AGENT_CONFIG_UPSTREAM`) > persisted file (`.agent-config/upstream`) > hardcoded default. Whichever value resolves is persisted to `.agent-config/upstream`, so any of the three entrypoints seeds the consumer's long-term upstream choice — you only pass it once per consumer project. Setting the env var on a later run updates the persisted value for all subsequent hook-triggered runs; it is not transient.
- **Fork instructions in the README now include the concrete install command** with the `<your-user>/<your-repo>` argv, in both Bash and PowerShell.

### Changed

- **Curl and `git clone` URLs inside bootstrap scripts are now parameterized** against the resolved upstream instead of hardcoded. The hardcoded default remains `yzhao062/anywhere-agents`, so consumers who never pass argv / env var / persisted file behave identically to 0.1.5.

### Fixed

- **Banner rule in `AGENTS.md` Session Start Check now explicitly overrides any skill's "invoke before responding" rule.** When a plugin like `superpowers:using-superpowers` fired a skill (e.g. `brainstorming`) as the first action on turn 1, the banner was silently dropped and replaced by the skill's output. The updated rule makes banner-first mandatory and allows the skill to run on the same turn after the banner text.
- **Sparse-clone origin now follows the resolved upstream on every run.** Prior versions only used the resolved upstream for the initial `git clone`; on subsequent runs (hook-triggered refreshes after an argv/env-var upstream switch), `git pull` fetched against whatever `origin` was set at first clone, so AGENTS.md came from the new upstream but skills/hooks/settings came from the old one. Both scripts now `git remote set-url origin "$REPO_URL"` before pulling.
- **Bash cascade tolerates an empty persisted upstream file.** If `.agent-config/upstream` exists but is empty (e.g. after a failed or interrupted write), resolution now falls through to the hardcoded default instead of producing a malformed URL. PowerShell already handled this via `Trim()`.

### Compatibility

- Existing consumers on 0.1.5 caches: self-update pulls the 0.1.6 bootstrap on next session. With no argv, env var, or persisted upstream file, the cascade falls through to the same hardcoded default, so behavior is unchanged. No user action required.
- Forkers: stop editing URLs inside bootstrap scripts. Tell consumers to install with `bash .agent-config/bootstrap.sh <your-user>/<your-repo>` (or the PowerShell equivalent).

## [0.1.5] — 2026-04-17

Bootstrap self-update — cached `.agent-config/bootstrap.{ps1,sh}` now copies itself forward from the sparse clone at the end of every run, so future bootstrap improvements reach existing consumers without a manual re-download.

### Fixed

- **Bootstrap self-update** in `bootstrap/bootstrap.ps1` and `bootstrap/bootstrap.sh`. At the end of each run, the cached entrypoint (`.agent-config/bootstrap.ps1` / `.agent-config/bootstrap.sh`) is overwritten with the fresh version pulled via sparse clone (`.agent-config/repo/bootstrap/bootstrap.{ps1,sh}`). Without this, any consumer who bootstrapped before a bootstrap-script change was permanently frozen on the old version and would never receive future improvements (e.g., the 0.1.3 generator step that creates `CLAUDE.md` and `agents/codex.md`).
- **Sparse-checkout now includes `bootstrap/`.** Prior versions limited the sparse clone to `skills .claude scripts user`, so the self-update source (`.agent-config/repo/bootstrap/bootstrap.{ps1,sh}`) did not exist in the sparse tree and the guard was silently a no-op. Caught in post-commit Codex review.
- **Self-update is best-effort.** PowerShell wraps `Copy-Item` in `try/catch` with `Write-Warning`; Bash uses `|| printf '...' >&2`. An anti-virus lock or read-only cache no longer turns a successful refresh into a reported bootstrap failure.

### Rollout note

The self-update block can only run once a consumer already has a bootstrap script containing that block. Existing consumers whose `.agent-config/bootstrap.ps1` or `.agent-config/bootstrap.sh` predates 0.1.5 need one seed refresh — run the bootstrap block in `AGENTS.md`, re-invoke `pipx run anywhere-agents` / `npx anywhere-agents`, or manually re-download the raw bootstrap script from `main`. After that single seed update, the cached entrypoint self-refreshes automatically on every subsequent session.

## [0.1.4] — 2026-04-16

User-visible session start banner, real-agent smoke (local + CI), pre-push safety hook, broadened validate matrix (macOS + Python 3.9-3.13), and published-package registry smoke. No breaking changes to the install flow.

### Added

- **Session Start banner** in `AGENTS.md` Session Start Check. Agents are now required to emit a structured banner as the first lines of their first response, showing `📦 anywhere-agents active`, OS, Agent, Codex config summary, Skills count + names, Hooks status, and a Session check line. Makes "bootstrap actually ran" visible to the user instead of silent.
- **`scripts/remote-smoke.sh`** — real-agent smoke for post-publish / published-install verification. Bootstraps a throwaway project via the published `pipx run anywhere-agents`, `npx anywhere-agents`, or raw-shell install, verifies expected files + user-level hooks deploy, then runs `claude -p` and `codex exec` non-interactively and asserts each response mentions the four shipped skills. Auto-detects install method (pipx → npx → raw curl). Distinct from `scripts/pre-push-smoke.sh`, which validates the release-candidate checkout before tagging. Validated on Windows daily-driver and on the Ubuntu DGX Spark via `ssh -6 spark 'bash -s' < scripts/remote-smoke.sh`.
- **`.githooks/pre-push`** — runs `scripts/pre-push-smoke.sh` when a push includes agent-critical files (`AGENTS.md`, `bootstrap/`, `scripts/`, `skills/`). `pre-push-smoke.sh` validates the CURRENT checkout (generator determinism + `claude -p` + `codex exec` against committed rule files) — distinct from `scripts/remote-smoke.sh`, which validates the published install path. Pure doc / test / CI-workflow pushes skip the smoke automatically and push fast. Bypass with `git push --no-verify`. Enable per-clone with `git config core.hooksPath .githooks`.
- **`.github/workflows/real-agent-smoke.yml`** — CI workflow that installs Claude Code and Codex CLIs on ubuntu-latest, invokes them against the committed `CLAUDE.md` / `agents/codex.md` using API key secrets, and asserts each response lists the shipped skills. Narrow triggers (`release: published` + `workflow_dispatch`) keep per-token API cost low (~$0.04 per run). Requires `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` repo secrets.
- **`.github/workflows/package-smoke.yml`** — triggered on `release: published` + weekly cron + manual dispatch. Installs the published PyPI and npm artifacts on a cross-OS × cross-Python/Node matrix (ubuntu × py 3.9/3.12/3.13, ubuntu × node 18/20/22, plus latest on Windows + macOS) and asserts `--version`, `--help`, `--dry-run` all succeed. Catches registry drift and cross-runtime install regressions that unit tests cannot see.

### Changed

- **`.github/workflows/validate.yml` matrix expanded** from 2 OS × Python 3.12 to 3 OS × Python 3.9-3.13 (9 jobs: ubuntu × 3.9/3.10/3.11/3.12/3.13, windows × 3.12/3.13, macos × 3.12/3.13). Added a separate **`docs-strict-build`** job (Ubuntu, Python 3.12) that runs `mkdocs build --strict --clean` on every push to catch Read-the-Docs regressions before they hit the live site.
- README stars badge (English and Simplified Chinese) carries `cacheSeconds=300`, shortening Shields.io server-side cache from the default (up to 1 hour) to 5 minutes. Users see new star counts reflected faster.
- `CONTRIBUTING.md` documents the pre-push hook enable step and lists the four shipped skills (previously listed only two — drive-by correction).
- `agent-config/docs/anywhere-agents.md` release workflow gains step 6 (real-agent smoke before tagging, with cross-reference to the private DGX setup doc and the CI equivalent). Subsequent steps renumbered.

### Fixed

- **`scripts/remote-smoke.sh` stdin leak** — when invoked via `ssh 'bash -s' < script`, `claude -p` and `codex exec` consumed the remaining stdin (the rest of the script), silently aborting later steps with a misleading exit code 0. Now redirects stdin from `/dev/null` on both agent calls.
- **`scripts/remote-smoke.sh` argv parsing** — `$INSTALL_CMD` was unquoted, word-splitting multi-command strings like `mkdir -p X && curl …` into literal argv for `mkdir`. Now uses `eval "$INSTALL_CMD"` so shell operators in the install string parse correctly.
- **`scripts/remote-smoke.sh` bootstrap file check** was hardcoded to `bootstrap.sh`, which failed on Windows Git Bash where the npm shim downloads `bootstrap.ps1` instead. Now accepts either platform-appropriate variant.
- **Session Start Check "not configured" phrasing** originally said `not configured — see Codex MCP Integration below`, which leaked a broken self-reference into the generated `CLAUDE.md` (Codex MCP section is stripped there). Shortened to `not configured`; regression test in `test_generator.py` asserts the strip invariant.

### Review history

0.1.4 passed `implement-review` with Codex before release. Resolved findings:

- **Medium** — pre-push hook initially invoked `scripts/remote-smoke.sh`, which tests the published package; this was a false-positive gate that could pass while the release-candidate checkout was broken. Fixed by adding `scripts/pre-push-smoke.sh` (validates the current checkout via generator determinism + `claude -p` / `codex exec` against the committed rule files) and pointing the pre-push hook at it. `remote-smoke.sh` retained for post-publish / published-install verification.
- **Medium** — `.github/workflows/package-smoke.yml` did not pin the install spec to the release tag, so a release event could pass while testing an older version the registry still served as latest. Fixed: workflow now resolves the expected version from `github.event.release.tag_name` (or `inputs.version` for manual dispatch), pins the install, and asserts the CLI's `--version` output contains the expected version.
- **Medium** — `CHANGELOG.md` lost the `## [0.1.3] — 2026-04-16` heading when the 0.1.4 section was inserted, folding old 0.1.3 content under 0.1.4. Fixed: heading restored.
- **Medium** — `RELEASING.md` pre-tag gate and `agent-config/docs/anywhere-agents.md` release-workflow step 6 pointed at `scripts/remote-smoke.sh` (published-package path) rather than the candidate checkout. Fixed: both now invoke `pre-push-smoke.sh` for the candidate, with `remote-smoke.sh` documented separately for post-publish verification.
- **Low** — stale `remote-smoke.sh` references in `.githooks/pre-push` header comment and skip message, `CONTRIBUTING.md` pre-push section (including wrong prerequisites), `agent-config/README.md` pre-push subsection, and the `CHANGELOG.md` 0.1.4 bullet describing `remote-smoke.sh` as "local / pre-tag validation." Fixed: all references distinguish the two scripts correctly.
- **Low** — `agent-config/docs/anywhere-agents.md` said the CI real-agent smoke runs "on every push," contradicting the narrow `release: published` + `workflow_dispatch` triggers in `real-agent-smoke.yml`. Fixed.
- **Low** — cost estimate for the real-agent CI workflow said `~$0.02` in the workflow header and `~$0.04` in the CHANGELOG. Fixed: both say `~$0.04 per run` (two short API calls).

No High-priority findings at any round.

## [0.1.3] — 2026-04-16

Central `AGENTS.md` → per-agent file generator (`CLAUDE.md`, `agents/codex.md`), Claude Code SessionStart hook that enforces bootstrap automatically, Scenario E in the README for the "you are running suboptimal defaults without knowing" pitch, and a 1:1 Simplified Chinese README.

### Added

- **Central source + per-agent generator.** `AGENTS.md` becomes the single source of truth for agent rule files. New HTML-comment markers (`<!-- agent:claude -->` / `<!-- agent:codex -->`) tag agent-specific sections. `scripts/generate_agent_configs.py` reads `AGENTS.md` and emits `CLAUDE.md` (Claude Code auto-loads this natively) and `agents/codex.md`. Each generated file carries a `GENERATED FILE` header and a documented precedence ladder. Bootstrap re-runs the generator on every session.
- **Hand-authored file protection.** The generator preserves any `CLAUDE.md` / `agents/codex.md` that lacks the `GENERATED FILE` header and prints a loud warning until the user resolves it. To keep a custom rule file, rename to `CLAUDE.local.md` — which wins over the generated file in the precedence ladder.
- **SessionStart hook enforces bootstrap.** `scripts/session_bootstrap.py` deploys to `~/.claude/hooks/session_bootstrap.py` on first bootstrap run. On every subsequent Claude Code session, the hook runs `.agent-config/bootstrap.sh` (or `.ps1`) if present, no-op otherwise. Users no longer need to type a reminder to keep the config fresh — for Claude Code, updates are fully automatic.
- **Configuration Precedence section in `AGENTS.md`.** Documents the three config layers (rule files, Claude Code settings, env vars) with explicit precedence rules.
- **Scenario E in README** — "The settings you did not know you were missing." Makes the selling point explicit: most Claude Code / Codex users never touch effort levels, model selection, or Codex MCP config; `anywhere-agents` ships the recommended default stack in one install.
- **README Install section** now documents the verbal fallback for non-Claude agents (Codex, Cursor, etc.) that lack SessionStart hook support — tell the agent `read @AGENTS.md to run bootstrap, session checks, and task routing` on the first message of each session.
- **`README.zh-CN.md`** — a 1:1 Simplified Chinese translation of `README.md`. Language switcher at the top of both files (`English · 中文`). Code blocks, Mermaid diagram labels, file paths, URLs, and skill names stay in English for consistency; narrative, section titles, table contents, and callouts are translated.

### Changed

- `user/settings.json` declares a `SessionStart` hook alongside the existing `PreToolUse` guard hook.
- `bootstrap/bootstrap.sh` and `bootstrap.ps1` run the generator after fetching `AGENTS.md`, and deploy `session_bootstrap.py` alongside `guard.py` to `~/.claude/hooks/`.
- `AGENTS.md` "What gets shared" table lists the new generated files and the user-level hook set (guard + session bootstrap).
- `AGENTS.md` "Environment Notes" Claude Code install + effort-level bullets are now tagged with `<!-- agent:claude -->` so Codex does not see the noise.
- `AGENTS.md` "Codex MCP Integration" section is tagged with `<!-- agent:codex -->` so Claude Code does not see the Codex setup noise.

### Fixed

- **Claude Code settings precedence wording in `AGENTS.md` Configuration Precedence section** — the ordering was reversed relative to Claude Code's documented behavior. Corrected to `managed policy > command-line args > .claude/settings.local.json > .claude/settings.json > ~/.claude/settings.json`. Regenerated `CLAUDE.md` and `agents/codex.md` so the correction propagates.
- **SessionStart hook noise** — `scripts/session_bootstrap.py` now captures subprocess stdout and emits one concise line (`anywhere-agents: bootstrap refreshed`) to avoid flooding Claude Code's session-start context with `git pull` status, clone progress, or generator messages. Errors surface to stderr with the last ~2 KB of child output for debugging.
- **Generator preserve-warning path for nested outputs** — the rename hint previously dropped the `agents/` prefix for `agents/codex.md`. Now the warning includes the full relative path, and a regression test covers the nested case.
- **Whitespace normalization in generator** — `extract_for()` now strips trailing whitespace on every line, so generated files do not inherit whitespace-only source lines that fail `git diff --cached --check`.

### Review history

0.1.3 passed `implement-review` with Codex before release. Resolved findings:

- **Medium** — Claude Code settings precedence wording in `AGENTS.md` reversed managed-policy order; corrected to match the documented `managed policy > command-line args > .local > project > user` chain.
- **Medium** — `scripts/session_bootstrap.py` forwarded raw subprocess stdout into Claude Code's session-start context; now captures and emits one concise summary line.
- **Low** — Generator preserve-warning dropped the `agents/` prefix for nested outputs; fixed, with a regression test.
- **Low** — Private repo `AGENTS.md` source had a whitespace-only line that propagated into generated files and failed `git diff --cached --check`; source corrected and generator now normalizes trailing whitespace on every line.
- **Low** — Private repo `AGENTS.md` "What gets shared" table did not yet list the new generated files and SessionStart hook ownership; added.
- **Medium** — `README.zh-CN.md` translated comments inside code blocks, violating the "keep code blocks verbatim in English" contract; restored English comments inside fences and kept translation outside.
- **Low** — Both READMEs introduced the scenarios as "Four" after Scenario E was added; corrected to "Five concrete scenarios" / "五个具体场景".
- **Low** — Repo-layout tree in the collapsible was stale (missed `CLAUDE.md`, `agents/codex.md`, `scripts/generate_agent_configs.py`, `scripts/session_bootstrap.py`); updated in both READMEs.
- **Low** — Chinese README used half-width punctuation in prose (`,`, `:`, `(`, `)` between Chinese characters); converted to full-width Chinese punctuation (`，` `。` `；` `：` `（）`) in prose while leaving code blocks, URLs, file paths, badge IDs, and English literals unchanged.
- No High-priority findings.

## [0.1.2] — 2026-04-16

Two new shipped skills (`ci-mockup-figure`, `readme-polish`), Read the Docs site launch, scenario-first README, reframed hero (project is the subject, author credentials become supporting evidence), Scenario B visualized as a left-to-right flowchart, and the usual round of Codex-driven corrections.

### Added

- **Skill: `ci-mockup-figure`** — build HTML mockups of systems, dashboards, and timelines, then capture as space-efficient PNG / PDF figures via headless Chrome. Includes an abstract-diagram path using TikZ or skia-canvas for architecture figures that need arrow routing between non-adjacent nodes. Covers tool selection, design principles, capture workflow, and LaTeX / Markdown insertion.
- **Skill: `readme-polish`** — audit a GitHub README and rewrite using modern 2025-2026 patterns: centered header, Shield.io badges, dot-separated nav, hero image, `> [!NOTE]` / `> [!TIP]` callouts, emoji-prefixed feature bullets, collapsible `<details>` for reference material, Mermaid diagrams, tables over dense prose. Ships with a patterns reference catalog and a pre-publish audit checklist.
- **Read the Docs site** — [anywhere-agents.readthedocs.io](https://anywhere-agents.readthedocs.io/). MkDocs + Material with a custom USC cardinal palette (`#990000`). Covers install, per-skill deep docs (via `mkdocs-include-markdown-plugin` so each skill page pulls directly from its `SKILL.md`), an `AGENTS.md` section-by-section reference, and a collapsible FAQ. Changelog is mirrored from the repo. New repo files: `.readthedocs.yaml`, `mkdocs.yml`, `docs/requirements.txt`, `docs/stylesheets/extra.css`, and `docs/*.md` content. Dependencies are upper-bounded so a future MkDocs 2.0 release cannot silently break the build.
- **`docs/skills/references/`** — pass-through pages for `review-lenses.md`, `routing-table.md`, `patterns.md`, and `checklist.md`. Links from inside each `SKILL.md` now resolve to real docs pages on RTD. `mkdocs build --strict --clean` is clean.
- **README RTD docs badge** — next to PyPI / npm / License / CI / Stars.
- **README "How to update" section** inside Install explaining that re-running the install command updates, plus the one-liner force refresh for mid-session.

### Changed

- **README restructured around four scenarios.** Replaces the adjacent "The agentic workflow this encodes" principle table and "What you get after setup" feature list with **What it does in practice** — A: add to any project, B: review before you push (left-to-right flowchart), C: writing that does not sound like an AI (with highlighted banned words and a before / after), D: Git safety catches mistakes. Reference-shaped content (day-to-day table, "what is opinionated" table) moves into collapsibles. Dot-nav points at Scenarios / Docs.
- **Hero reframed.** The project is visually primary; the author avatar is removed; PyOD credentials are supporting evidence under a "Built by…" line with PyOD described in context ("a widely used Python anomaly detection library") and numbers smaller and muted. Sig-strip pill changed from "What you get" to "Condensed experience" with the lead line "distilled from daily use since early 2026 across research, paper writing, and dev work." Panel 4 shows dispatch across the four shipped skills. Panel 5 removes `rm -rf` (not guard-scoped) and shows `git rebase` instead. Footer shows `4 shipped skills · anywhere-agents.readthedocs.io`.
- **Maintainer callout reframed** from "Maintained by [Yue Zhao]…" opener to "**Condensed from daily use.**" opener, with credentials moved to the end as backing evidence.
- **Scenario B visual** changed from a sequence diagram to a left-to-right flowchart with a loop-back arrow. Actor lanes disappear; the flow reads as one linear pipeline with an explicit `{clean?}` decision.
- **`skills/my-router/references/routing-table.md`** lists concrete keyword, file-type, and directory rules for all four shipped skills (previously only `implement-review`).
- **`skills/my-router/SKILL.md`** intro reflects the four-skill shipped set and includes dispatch examples for `ci-mockup-figure` and `readme-polish`.
- **`AGENTS.md`** "What gets shared" table lists all four shipped skills.
- **`.gitignore`** — added `site/` so local `mkdocs build` output does not leak into `git add -A`.

### Fixed

- **MkDocs strict build broken on included-link warnings** (Round 1, Medium). Included `SKILL.md` bodies referenced `references/*.md` that were not docs pages. Resolved by adding pass-through pages under `docs/skills/references/`, setting `rewrite_relative_urls: false` on the include-markdown plugin, and reorganizing the nav so reference pages appear as sub-items under each skill. `mkdocs build --strict --clean` now completes with zero warnings.
- **`tests/test_repo.py:260` stale two-skill-era failure message** (Round 1, Low). Replaced with a version-agnostic message pointing the maintainer at `SHIPPED_SKILLS` and the public docs together.
- **CHANGELOG scenario order mismatch** (Round 1, Low). Parenthetical listed scenarios as A-C-B-D; corrected to A-B-C-D.
- **Docs build dependency drift risk** (Round 2, Medium). `docs/requirements.txt` now carries upper bounds on every pin (`mkdocs<2.0`, `mkdocs-material<10.0`, `mkdocs-include-markdown-plugin<8.0`, `pymdown-extensions<11.0`) so a future RTD rebuild cannot pick up a breaking major.
- **Generated `site/` directory not ignored** (Round 2, Low). Added to `.gitignore` under a "MkDocs build output" comment.

### Review history

0.1.2 passed two rounds of `implement-review` with Codex before release:

- **Round 1**: 5 findings (2 Medium + 3 Low) covering MkDocs broken links, stale test message, CHANGELOG scenario order, and stale two-skill framing in the private relationship doc. All Fixed.
- **Round 2**: 3 findings (2 Medium + 1 Low) covering docs build dependency bounds, `site/` in `.gitignore`, and adding `mkdocs build --strict --clean` to the private release gate. All Fixed.
- No High-priority findings in either round.

## [0.1.1] — 2026-04-16

Release-hygiene follow-up. Documentation and layout improvements since 0.1.0, and package source is now fully reproducible from the repository.

### Added

- `docs/hero.png` + `docs/hero.html` + `docs/avatar.jpg` — README hero image with a 6-panel feature grid (cardinal-red branding), self-contained HTML source for regeneration, and vendored avatar so the hero does not depend on an external URL.
- README "The agentic workflow this encodes" section — educational narrative covering git-as-substrate, implementer + gatekeeper pattern, and IDE / MCP tradeoffs across operating systems.
- Mermaid review-loop sequence diagram (collapsed by default).
- Agent-friendly Install section with PyPI, npm, and raw-shell paths; `> [!TIP]` callout explains the "ask your agent to install" pattern.
- Package-local LICENSE files (`packages/pypi/LICENSE`, `packages/npm/LICENSE`) so published artifacts include the Apache-2.0 text.
- `packages/pypi/` and `packages/npm/` directories in the public repo so package source lives in the repo (was previously in an external scratch workspace — see 0.1.0 "Not included").

### Changed

- README restructured for scannability: centered header with badges and dot-nav; tables replaced dense bullet lists where the content was reference-like; collapsibles hide detail from first-read while keeping it one click away.
- Maintainer paragraph now sits inside a `> [!NOTE]` callout and is roughly half its previous length.
- CLI version reads from a single source of truth:
  - Python: `anywhere_agents.cli` imports `__version__` from `anywhere_agents/__init__.py`.
  - Node.js: `bin/anywhere-agents.js` reads `version` from its sibling `package.json` at runtime.
- Release workflow in the private relationship doc reflects the new `packages/` layout and the single-source version pattern.

### Fixed

- Guard-hook scope claim corrected in README, CHANGELOG, and hero source: `rm -rf` goes through Claude Code permission prompts via settings, not through `guard.py`. The `STOP! HAMMER TIME!` warning is for guard-covered Git/GitHub commands only.
- Raw shell install path now creates `.agent-config/` before downloading the bootstrap script on both macOS/Linux and Windows PowerShell. The install section also shows both shells (previously only Bash).
- CHANGELOG version numbering unified: one release stream covers both repo content and PyPI/npm packages.

## [0.1.0] — 2026-04-16

Initial public release. The sanitized downstream of the author's private daily-driver agent config, refined over months across many repositories, machines, and workflows. This release covers both the GitHub repo content (bootstrap, skills, guard hook, settings, tests, docs) and the matching PyPI / npm CLI packages.

### Added — repository

- **Bootstrap** (`bootstrap/bootstrap.sh`, `bootstrap/bootstrap.ps1`) — idempotent sync scripts for macOS, Linux, and Windows. Fetch `AGENTS.md`, sparse-clone skills, merge settings, deploy the guard hook, update `.gitignore`. Safe to run every session.
- **`AGENTS.md`** — opinionated agent configuration covering:
  - Source-vs-consumer repo detection.
  - Session start checks (OS, model and effort level, Codex config, GitHub Actions version pins).
  - User profile placeholder (intended for customization in forks).
  - Agent roles (Claude Code implementer + Codex reviewer).
  - Task routing via `my-router`.
  - Codex MCP integration guide.
  - Writing defaults (~40 AI-tell words to avoid, punctuation rules, format preservation).
  - Formatting defaults, Git safety, shell command style.
  - GitHub Actions version standards (Node.js 24 minimums).
  - Environment notes.
  - Local skills precedence and cross-tool skill sharing conventions.
- **Skill: `implement-review`** — structured dual-agent review loop with content-type-specific lenses (code via Google eng-practices, paper via NeurIPS/ICLR/ICML, proposal via NSF Merit Review or NIH Simplified Peer Review), focused sub-lenses (code/security, paper/formatting, proposal/compliance, etc.), multi-target reviews, round history tracking, and reviewer save contract. Includes example reviews covering code, paper, and proposal tracks.
- **Skill: `my-router`** — context-aware skill dispatcher shipped as a template. Ships with `implement-review` as the only concrete routing rule plus an extension template so users register their own skills in a fork.
- **Guard hook** (`scripts/guard.py`) — PreToolUse hook that intercepts destructive Git and GitHub commands (`git push`, `git commit`, `git reset --hard`, `git merge`, `git rebase`, `gh pr merge`, `gh pr create`, etc.) and compound `cd <path> && <cmd>` chains with deliberately memorable warnings ("STOP! HAMMER TIME!", etc.) to prevent muscle-memory auto-approval. Tuned to keep read-only operations fast. Shell deletes (`rm -rf`) go through Claude Code's built-in permission prompts via the user-level `ask` settings, not the guard hook itself.
- **Claude Code commands** (`.claude/commands/`) — pointer files for both shipped skills (local-first, bootstrap fallback lookup).
- **Claude Code settings** (`.claude/settings.json`) — curated project-level permissions.
- **User-level settings** (`user/settings.json`) — permissions, guard hook wiring, and `CLAUDE_CODE_EFFORT_LEVEL=max` env default.
- **Tests** (`tests/`) — bootstrap contract validation, skill layout checks, settings merge preservation, and Windows + Linux bootstrap smoke tests running in GitHub Actions CI.
- **CI** (`.github/workflows/validate.yml`) — validation on `ubuntu-latest` and `windows-latest` with `actions/checkout@v6` and `actions/setup-python@v6`.
- **README** with problem framing, "What you get" benefit list, install paths (PyPI / npm / raw shell), day-to-day usage notes, collapsible reference sections, and maintainer context. Includes a hero image (`docs/hero.png`, with `docs/hero.html` source and a vendored avatar at `docs/avatar.jpg`), a Mermaid review-loop sequence diagram, the "agentic workflow this encodes" educational section, and GitHub-style `> [!NOTE]` / `> [!TIP]` callouts.
- **`CONTRIBUTING.md`** — scope and process for PRs, bug reports, and customizations (customizations go in a fork; upstream takes bug fixes and clear improvements).
- **`LICENSE`** — Apache 2.0.

### Added — packages

- **PyPI `anywhere-agents` 0.1.0** — installable via `pip install anywhere-agents` or `pipx run anywhere-agents`. Ships a thin CLI (`anywhere_agents.cli:main`) that downloads the latest shell bootstrap from the repo and runs it in the current directory. Supports `--dry-run`, `--version`, `--help`.
- **npm `anywhere-agents` 0.1.0** — installable via `npx anywhere-agents` or `npm install -g anywhere-agents`. Same behavior as the PyPI CLI, implemented in Node.js.
- **Agent-native install path**: users can tell their AI agent _"install anywhere-agents in this project"_ and the agent will pick whichever command (pipx, npx, or raw shell) matches the environment. The packages exist purely as agent-friendly entry points; install logic stays single-source in the shell bootstrap scripts.

### Not included (out of scope for 0.1.0)

- No YAML manifest or config file — files in the repo are the configuration.
- No selective-update tooling — Git is the subscription engine (`git pull upstream main`, `git cherry-pick`).
- No environment auto-install — `AGENTS.md` documents required tools; users install them.
- No multi-agent expansion beyond Claude Code + Codex — forks can add Cursor, Aider, Gemini CLI support.
- No profiles system — there is one configuration; forks are how other "profiles" exist.
- No marketplace, registry, or web UI.

### Review history

0.1.0 passed multiple rounds of `implement-review` with Codex before release. Resolved findings:

- **High** — Bootstrap scripts were silently running `git config --global core.autocrlf false`, reaching beyond the consuming repo. Removed; regression test added.
- **High** — Raw shell install path in README missed `mkdir -p .agent-config` and omitted the Windows PowerShell variant; fixed with both shells in a collapsible.
- **Medium** — `AGENTS.md` "What gets shared" table listed unshipped skills. Corrected to the actually-shipped set (`implement-review`, `my-router`).
- **Medium** — README maintainer paragraph overstated this repo's role relative to the private canonical source. Revised to describe this as the "sanitized public release of the working agent config."
- **Medium** — README / CHANGELOG / hero overstated the guard hook's scope by listing `rm -rf` alongside Git/GitHub commands. Corrected to distinguish guard-covered commands from settings-based permission prompts.
- **Low** — Trailing whitespace in `AGENTS.md`; `docs/hero.html` external avatar URL (vendored to `docs/avatar.jpg` for reproducibility). Both fixed.

[Unreleased]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.6...HEAD
[0.5.6]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/yzhao062/anywhere-agents/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/yzhao062/anywhere-agents/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/yzhao062/anywhere-agents/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/yzhao062/anywhere-agents/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.2.0
[0.1.9]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.9
[0.1.8]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.8
[0.1.7]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.7
[0.1.6]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.6
[0.1.5]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.5
[0.1.4]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.4
[0.1.3]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.3
[0.1.2]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.2
[0.1.1]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.1
[0.1.0]: https://github.com/yzhao062/anywhere-agents/releases/tag/v0.1.0
