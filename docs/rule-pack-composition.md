# Rule-pack composition

`anywhere-agents` composes the root `AGENTS.md` from two sources: the base anywhere-agents configuration (fetched from upstream) and one or more **rule packs** that get injected into the file at bootstrap time. This page is the reference for how that composition works: who runs it, what it fetches, how it fails, and how to register a new pack.

## Rule pack vs skill pack

anywhere-agents ships two composition layers. They are independent; a project can use either or both.

| Layer | Mechanism | Examples |
|---|---|---|
| **Skill pack** | On-demand, invoked via the `Skill` tool when the agent routes to it | `implement-review`, `my-router`, `ci-mockup-figure`, `readme-polish` |
| **Rule pack** | Always-on; injected into `AGENTS.md` at bootstrap time so every prompt sees the content | `agent-style` (writing rules). Future candidates: `agent-security`, `agent-research-ethics` |

Rule packs are the right fit when the content should apply to every interaction (writing discipline, banned words, formatting defaults). Skill packs are the right fit for workflows the agent invokes explicitly (code review, README polish).

## Default behavior

Every anywhere-agents bootstrap run composes `AGENTS.md` with the `agent-style` rule pack enabled by default. Since v0.5.7, the bundled default fetches the **compact** render at `agent-style`'s `docs/rule-pack-compact.md` (~21 KB, 21 rules with directive + first BAD/GOOD pair) instead of the full `docs/rule-pack.md` (~89 KB, full reference with metadata + 5+ pairs + rationale). Composition validates the fetched body against the per-agent routing-marker grammar, computes a SHA-256 content hash, caches the result, and writes the composed `AGENTS.md` atomically (temp file plus rename) before any downstream step runs.

Pack authors writing new packs follow the generic convention `docs/rule-pack.md`; the compact path is `agent-style`-specific. **Consumer-side compact-to-full switching is not supported in v0.5.7**: the composer derives pack definitions from the bundled `bootstrap/packs.yaml` (since `agent-style` ships no `pack.yaml` of its own), so a consumer-supplied `passive.files[].from: docs/rule-pack.md` is silently ignored regardless of ref. Old explicit `agent-style` pins are not migrated by `pack verify --fix`, but a full composer / bootstrap run on aa v0.5.7 re-derives the pack definition from the bundled manifest and produces compact output regardless of consumer override. To keep the old full-body bundled default until consumer-side switching is supported, stay on aa v0.5.6; same-ref source-path switching is queued for aa v0.6.0.

The composed block inside `AGENTS.md` looks like:

```text
...base anywhere-agents AGENTS.md content...

<!-- rule-pack:agent-style:begin version=v0.3.5 sha256=abc123... -->
...agent-style rules body (21 rules) ...
<!-- rule-pack:agent-style:end -->
```

The block is regenerated on every bootstrap. Editing content inside the delimiters is not supported: bootstrap overwrites it on the next run. Repo-local overrides belong in `AGENTS.local.md` (handled separately by the runtime precedence ladder; never stitched into the composed file).

## Opt-in layers and precedence

Four places can signal rule-pack selection, ordered from durable to transient:

### 1. `agent-config.yaml` (tracked, durable)

At the consumer repo root. Committed to git. Every collaborator and every bootstrap run sees the same selection.

```yaml
# agent-config.yaml
rule_packs:
  - name: agent-style
    ref: v0.3.5       # optional override of the manifest default-ref
```

### 2. `agent-config.local.yaml` (gitignored, machine-local override)

At the consumer repo root, added to `.gitignore` automatically by bootstrap. Same schema as the tracked form; merged on top of the tracked config for per-developer experimentation. Writes here do not affect collaborators.

### 3. `AGENT_CONFIG_RULE_PACKS` env var (transient one-run override)

```bash
AGENT_CONFIG_RULE_PACKS="agent-style" bash .agent-config/bootstrap.sh
```

Runs a single bootstrap with the listed rule packs added on top of whatever the config files declare. No config file is modified. Intended for CI runs and short local experiments that must not leave committed state behind.

### 4. `--rule-packs PACK` (dry helper)

Prints the equivalent `agent-config.yaml` snippet and exits without running a bootstrap:

```bash
bash .agent-config/bootstrap.sh --rule-packs agent-style
# PowerShell:
# & .\.agent-config\bootstrap.ps1 -RulePacks agent-style
```

Use this to see what YAML to commit. The printed snippet has a commented-out `ref:` line you can uncomment to pin a specific version.

### Precedence rules

| Case | Effective selection |
|---|---|
| No config, no env var | Internal default: `[agent-style]` |
| `rule_packs: []` in agent-config.yaml | Explicit opt-out — no rule packs applied |
| `rule_packs:` with null value in agent-config.yaml | Same as opt-out |
| `agent-config.yaml` with tracked packs | Tracked packs applied (default suppressed) |
| Tracked + local | Local overrides tracked by pack name (merged) |
| Tracked / local + env | Env packs added on top; existing entries keep their config ref |
| Flag `--rule-packs` plus env var | Flag wins; dry-helper mode only, env var ignored for that run (notice emitted) |

## Rule-pack anatomy (for pack authors)

A rule pack is a public GitHub repo that exposes:

1. **A canonical instruction file at a stable path.** Convention: `docs/rule-pack.md` at the pack repo root. Plain Markdown; no frontmatter required.
2. **Semver-style tags for versioning.** Convention: `vX.Y.Z`. Consumers pin refs against these tags.
3. **Content contract.** `docs/rule-pack.md` MUST NOT contain any HTML comment matching the per-agent routing grammar:

   ```regex
   <!--\s*/?agent:[\w-]+\s*-->
   ```

   These markers are reserved for anywhere-agents' own per-agent generator (`scripts/generate_agent_configs.py`) which splits shared `AGENTS.md` into per-agent files. anywhere-agents' composer rejects a rule pack containing any marker matching this grammar and fails bootstrap with a named error before modifying any files.

4. **Optional README cross-reference.** The pack's README should link to `docs/rule-pack.md` so readers know what anywhere-agents fetches.

For a complete reference example that follows all four conventions, see [`yzhao062/agent-pack`](https://github.com/yzhao062/agent-pack). It declares three packs in the v2 manifest format (two passive rule packs and one active skill pack queued for v0.5.0), ships the matching content at the conventional paths, and documents the v0.4.0-vs-v0.5.0 loadability split honestly. Fork it as a starting point for your own pack repo.

## Composition flow

On every anywhere-agents bootstrap with rule packs enabled, the composer:

1. Reads upstream `AGENTS.md` (already fetched into `.agent-config/AGENTS.md` by the bootstrap curl step) into memory.
2. Parses `agent-config.yaml`, `agent-config.local.yaml`, and `AGENT_CONFIG_RULE_PACKS` env var to compute effective selections.
3. For each selected pack: resolves the manifest entry, fetches the path declared by the entry's `passive[].files[].from` (for `agent-style` since v0.5.7 that is `docs/rule-pack-compact.md` by default; pack authors typically use `docs/rule-pack.md`) at the pinned ref via the raw GitHub URL, decodes as UTF-8, computes SHA-256, caches under `.agent-config/rule-packs/<name>-<ref>.md` plus a `.sha256` sidecar.
4. Validates the fetched Markdown: rejects any routing-marker match with a named error listing the pack and the exact marker text.
5. Builds the composed `AGENTS.md` in memory: base upstream content, then each pack in order, each inside its `begin`/`end` block carrying the version and SHA-256.
6. Writes the composed content atomically: temp file in the same directory, then `os.replace` to `AGENTS.md`. Existing `AGENTS.md` is replaced only when the full composition succeeds.
7. Only after the atomic write does bootstrap run the per-agent generator (`scripts/generate_agent_configs.py`) that produces `CLAUDE.md` and `agents/codex.md` from the composed `AGENTS.md`.

## Manifest schema

anywhere-agents keeps the list of known rule packs in `bootstrap/rule-packs.yaml`:

```yaml
version: 1
packs:
  - name: agent-style
    description: >
      Writing-rule pack. Banned-word list, formatting defaults,
      contraction rules, and dash usage guidance.
    source: https://raw.githubusercontent.com/yzhao062/agent-style/{ref}/docs/rule-pack-compact.md
    default-ref: v0.3.5
    maintainer: yzhao062
```

Fields:

| Field | Required | Notes |
|---|---|---|
| `version` | yes | Manifest schema version. Currently `1`. |
| `packs[].name` | yes | Stable identifier, referenced by `agent-config.yaml`. |
| `packs[].source` | yes | Raw-content URL template. `{ref}` is substituted with the effective ref. |
| `packs[].default-ref` | yes | Fallback ref used when consumers do not override. |
| `packs[].description` | no | Short description. Surfaces in docs and error messages. |
| `packs[].maintainer` | no | GitHub handle of the pack maintainer. |

## `update_policy` boundary (v0.6.0)

The v2 manifest accepts `update_policy:` per pack with three values: `auto` (silent refresh + stderr summary), `prompt` (apply by default + stderr summary), and `locked` (fail-closed). v0.6.0 restores the parse-time boundary first stated in the trust-model paragraph at `pack-architecture.md` line 208:

| Entry kind | `update_policy: auto` | `update_policy: prompt` | `update_policy: locked` |
|---|---|---|---|
| Passive (raw text injected into `AGENTS.md`) | accepted | accepted | accepted |
| Active (skill files, hooks, permission rules, command pointers) | **rejected at parse** | accepted | accepted |

The bundled-default policy table flips in v0.6.0: `agent-style` (passive) → `auto`, `aa-core-skills` (active) → `prompt`. Third-party packs default to `prompt`. Consumers can pin `update_policy: locked` in `agent-config.yaml` for any pack where they want fail-closed behavior; the bundled defaults are the *default*, not the only option.

The active-entry rejection of `auto` is doc-coherence repair, not new policy. The trust-model rationale (silent install of arbitrary code from a mutable ref is the supply-chain risk `prompt` was designed to gate) has stood since the v0.4.0 manifest contract; v0.5.0 silently dropped the parser check, and v0.6.0 restores it. The parse error names the pack, the `files[].to` path of the offending active entry, the policy literal, and the required rewrite (`prompt` for default-apply behavior, `locked` for fail-closed).

## Dependency contract

| Path | Requires |
|---|---|
| No-rule-pack path (opt-out or graceful fallback) | Shell-native. No Python required. |
| Rule-pack composition path | Python 3.x + PyYAML (`pip install pyyaml`). |

Bootstrap attempts a best-effort `pip install --user --quiet pyyaml` when Python is present but PyYAML is missing. If Python itself is missing, or the auto-install fails, bootstrap falls back to the verbatim upstream `AGENTS.md` and prints a one-line tip. Bootstrap never exits with a hard error when dependencies are missing; it only exits on genuine composition failures (fetch errors with no cache available, validation rejections, unknown pack names, malformed YAML, or the atomic rename failing).

## Cache and offline behavior

Cached files: `.agent-config/rule-packs/<name>-<ref>.md` plus a matching `.sha256` sidecar.

| Condition | Behavior |
|---|---|
| Fetch succeeds | Overwrites cache, uses fresh content, writes SHA sidecar. |
| Fetch fails + cache present + `--no-cache` NOT set | Falls back to cached content, emits warning, continues. |
| Fetch fails + cache absent | Raises `RulePackError` with pack name, source URL, resolved ref, and a next-step suggestion. Does NOT modify `AGENTS.md`. |
| Fetch fails + `--no-cache` set | Raises regardless of cache; `--no-cache` forces refetch semantics. |

`--no-cache` propagates from the bootstrap flag to the composer:

```bash
bash .agent-config/bootstrap.sh --no-cache
# PowerShell: & .\.agent-config\bootstrap.ps1 -NoCache
```

## Failure modes

Each of these surfaces with a named error and no partial write of `AGENTS.md`:

- Malformed `agent-config.yaml` / `agent-config.local.yaml` YAML
- Unknown pack name (error lists the known packs from the manifest)
- Duplicate pack name in a single source (warning; last occurrence wins)
- Rule-pack Markdown contains a per-agent routing marker (error includes the matched text)
- Fetch 404 or wrong ref
- Network unavailable on first run with no cache
- Cache path escape via a malicious ref (caught by percent-encoding in the cache filename; ref contents never form actual filesystem path components)
- Atomic rename fails at write time (existing `AGENTS.md` remains intact)

Idempotent reruns produce identical output under the same inputs (same pack, same ref, same upstream `AGENTS.md`).

## Register a new rule pack

To add a second rule pack to the anywhere-agents shared manifest:

1. Publish `docs/rule-pack.md` in your pack's public GitHub repo. Tag a release (for example, `v0.1.0`). Make sure the file contains no per-agent routing markers.
2. Open a PR against `yzhao062/anywhere-agents` adding a manifest entry to `bootstrap/rule-packs.yaml`:

   ```yaml
     - name: your-pack-name
       description: >
         One-sentence description.
       source: https://raw.githubusercontent.com/<you>/<repo>/{ref}/docs/rule-pack.md
       default-ref: v0.1.0
       maintainer: <your-gh-handle>
   ```

3. PR reviewers exercise a fixture compose against your ref to confirm the composer accepts the file.

Consumers can then opt in via `agent-config.yaml`:

```yaml
rule_packs:
  - name: agent-style
  - name: your-pack-name
```

## Historical naming

The consumer-repo scratch directory is called `.agent-config/`. The name predates `anywhere-agents` as a public project: it came from the private source repo `agent-config`, which was the original canonical source for anywhere-agents' shared content. Consumers see the name `.agent-config/` even though they are using `anywhere-agents`, not the private source. New config files added by the rule-pack feature follow the same historical prefix for consistency (`agent-config.yaml`, `agent-config.local.yaml`).

A rename was considered and scoped (around 30 files / 292 occurrences across anywhere-agents, plus a graceful-migration story for existing consumers with `.agent-config/` already on disk). The cost-to-benefit did not pencil out at the current scale of anywhere-agents. The historical name stays; this page documents the reason so first-time consumers are not confused by the name mismatch.
