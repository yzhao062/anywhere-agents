# Codex

Codex is the gatekeeper in the default division of labor. It reviews what Claude Code or a person produced, and `/vet` is how it is reached. `AGENTS.md` § "Environment" holds the policy lines: approval policy, tier, and the `project_doc_max_bytes` budget. The model and effort live in one registry, `user/agent-models.json` (see the `agent-models` skill), and no other file names them. This page holds the configuration those lines summarize, the effort ladder, the service tiers, the CLI floors as history, and the status of the routes this project no longer uses.

## Recommended `config.toml`

`~/.codex/config.toml` on macOS and Linux, `%USERPROFILE%\.codex\config.toml` on Windows:

```toml
# model and model_reasoning_effort: written here by bootstrap from the registry
# (agent_models.py sync); do not keep a second copy by hand.
service_tier = "standard"
approval_policy = "on-request"
project_doc_max_bytes = 262144

[features]
fast_mode = false

[desktop]
conversationDetailMode = "DEFAULT"
```

`conversationDetailMode = "DEFAULT"` keeps terminal output concise; `STEPS_PROSE` shows command-level progress during turns and is rarely wanted. `approval_policy = "on-request"` is for interactive sessions; the dispatcher sets its own policy for a review.

Bootstrap runs `agent_models.py sync`, which sets the top-level `model` and `model_reasoning_effort` in that file to the registry's interactive Codex values and leaves every other key alone (it keeps a `config.toml.agent-models.bak` copy of the file it rewrote). `sync` leaves the model untouched when the file sets a `model_provider` other than `openai`, because the registry's model names belong to the default provider. It leaves the whole file as it is, and says so, when it cannot check its own edit: a layout its line editor does not handle, or a Python before 3.11, which has no TOML parser. `AGENT_MODELS_SYNC=off` in the environment skips the step. To change the model or effort, change the registry, not this file.

## The Byte Budget

Codex limits the combined size of the instruction files it discovers (`AGENTS.override.md` or `AGENTS.md` at each level, root first) to `project_doc_max_bytes`, 32 KiB by default. The root file is read first and truncated to what remains of the budget, with a warning that only tracing-level logging shows. Later files in the chain are dropped once the budget is spent. The pre-rewrite baseline placed Writing Defaults and Git Safety beyond that limit, at bytes 36,208 and 40,549 of the 66 KB file. A review that ran with the default budget therefore lost both sections and every appended pack. The compact composition is about 55 KB and still exceeds the default. Set `project_doc_max_bytes = 262144` for interactive sessions; the dispatcher supplies the same value for reviews, and the banner flags a missing or smaller value.

`dispatch-codex` passes `-c project_doc_max_bytes=262144` on its own command line, beside `developer_instructions`, because a review runs under `--ignore-user-config` by default and a value in `config.toml` alone would not survive that flag. The key in the file therefore covers interactive sessions; the dispatcher covers reviews. Codex discovers `AGENTS.override.md`, then `AGENTS.md`, per directory from the repo root to the working directory. It never reads `AGENTS.local.md` or `agents/codex.md` on its own. A rule that must reach a Codex session therefore belongs in `AGENTS.md`, and `agents/codex.md` applies only where a rule or a person points at it.

## Models and CLI Floors

Each model generation carries a CLI floor. Below it the service rejects the first turn with an HTTP 400 that names the model: `The '<model>' model requires a newer version of Codex.` The GPT-5.6 family (`gpt-5.6-sol` flagship, `gpt-5.6-terra` mid, `gpt-5.6-luna` cheapest) has a floor of 0.144.0. On that family 0.142.5 and 0.143.0 fail, and 0.144.0 and 0.144.1 work. GPT-6 Astra (`gpt-6-astra`) fails at 0.149.0-alpha.4.3 and works at 0.153.3. No build between the two was tested, so the banner uses 0.150.0 as its floor. Sol (`gpt-6-sol`) and Luna (`gpt-6-luna`) joined GPT-6 on 2026-09-22; Sol works at 0.155.1, and no older build was tried. GPT-6.1 Sol (`gpt-6.1-sol`) works at 0.159.0, the first build it was run on. When the model errors, `npm install -g @openai/codex@latest` first. <!-- agent-models: allow -->

The registry decides which model Codex runs, interactively and for a review, and a review runs on the interactive model unless the registry lists an override for the reviewer role. `CODEX_DISPATCH_MODEL` gives one round another model, for example for an account without the usual one. The banner compares the configured model and effort with the registry and flags a CLI below the floor of the configured generation or a model older than the GPT-5.6 family.

`codex debug models` prints the model catalog the installed CLI received from the service, cached in `~/.codex/models_cache.json`: each model's slug, `priority`, `supported_reasoning_levels`, and service tiers. Check the installed CLI's catalog when a model arrives; this page records a dated snapshot.

## Effort Ladder

`model_reasoning_effort` is not validated client-side. An unknown value reaches the service, which rejects the first turn with an HTTP 400 and exits nonzero, so a typo fails loudly. That error's enumerated list (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`) is incomplete: a model also accepts `max` or `ultra` when its catalog entry lists them under `supported_reasoning_levels`.

`ultra` is not simply more reasoning than `max`. Single-agent reasoning tops out at `max`; `ultra` keeps that same maximum and switches the harness into automatic task delegation. The rollout records `multi_agent_mode: proactive` for `ultra` and `explicitRequestOnly` for `max`. This was measured on GPT-5.6 Sol and on Astra at 0.153.3, where `xhigh` also records `explicitRequestOnly`. The registry holds the default level; raise a single task above it when the task earns the extra depth, and use `ultra` only when proactive delegation is wanted.

To confirm which mode landed, read `~/.codex/sessions/**/rollout-*.jsonl` (`payload.model`, `payload.effort`, and `multi_agent_mode`) rather than the config file. A sibling `collaboration_mode` block records `mode: default` at every effort and does not carry the delegation signal, and `service_tier` is not recorded there at all.

The `implement-review` dispatcher holds no effort floor of its own. A review runs at the registry's effort for the reviewer role, which equals the interactive effort unless the registry lists an override, so a review is no longer pinned above the interactive session. `CODEX_DISPATCH_REASONING` changes one round, and the skill lets the dispatching agent raise a round above the registry's effort when the change is hard enough to earn it, never lower it.

## Service Tiers

`service_tier` buys latency, never quality. It selects the serving queue only: the model, its weights, and `model_reasoning_effort` are identical across tiers, so `standard` returns the answer `fast` would, generated more slowly. The three tiers are `flex` (a lower-priority queue at roughly half rate, availability not guaranteed), `standard` (the tier used when the key is unset), and `fast` (faster generation at a higher credit rate). For ChatGPT authentication, `fast` bills at 2.5x the standard credit rate on GPT-6 (Astra, Sol, and Luna), GPT-5.6, and GPT-5.5. API-key authentication uses API token pricing for the selected processing tier instead. The catalog describes `fast` as 2x speed on Astra and 1.5x on the other models. Sources: the [pricing page](https://learn.chatgpt.com/docs/pricing) and the [speed page](https://learn.chatgpt.com/docs/agent-configuration/speed). Local probes confirmed that Codex 0.153.3 accepts the tier for the GPT-6 flagship without a warning. The older 0.149.0-alpha.4.3 warns that the tier is not advertised there. That warning is stale client metadata rather than a server rule. `fast` is the current config spelling and maps to the request value `priority`. A file that still reads `priority` names the same tier.

Default to `standard`. Since Codex is the reviewer by default, no person waits on `fast` tokens. A session earns the 2.5x when a second account absorbs the rate or the work is time-critical. Then dial up with `/fast on` mid-session, or keep a second profile with `service_tier = "fast"` selected by `codex -p <name>`.

## The Dispatcher and User Config Isolation

By default `dispatch-codex` passes `--ignore-user-config` to `codex exec`, so a review does not inherit the interactive tier or MCP servers. The dispatcher passes the model, the reasoning effort, and the byte budget itself. Without the model, a review would run on the model Codex recommends, which the service chooses and can change. Both values come from the registry at run time, through `scripts/agent_models.py`, which the dispatcher finds by walking up from its own directory. The order for each is the per-run override (`CODEX_DISPATCH_MODEL`, `CODEX_DISPATCH_REASONING`), then the registry's Codex reviewer row, then the top-level `model` and `model_reasoning_effort` in `$CODEX_HOME/config.toml`, which bootstrap syncs from the registry and which are read back out because isolation drops them. With no source the dispatch exits with a message that names the registry; no model or effort is written into the script as a fallback. A config value counts only when it is quoted and made of `A-Za-z0-9._:/-`. Under a top-level `model_provider` other than `openai`, the config model is set aside: isolation drops the provider, and its model names would reach the default one.

A value counts only when Codex reports it ran on it. After the run the dispatcher reads the `model:` and `reasoning effort:` header lines `codex exec` prints and writes `<state-dir>/review-model` with the requested model and effort, the source of each, and the actual values with a status of `ok`, `mismatch`, or `unknown`. A difference prints `MODEL-MISMATCH` on stderr, and a header that cannot be read prints `MODEL-UNVERIFIED`; neither changes the exit code. `agent_models.py doctor` compares the live configs with the registry, and `verify` launches each CLI once and reads back what it ran on. `CODEX_DISPATCH_ISOLATE_MCP=off` restores the full user config, model and tier included, while the explicit byte budget still applies. Hardcoding `fast` into the isolated path was avoided deliberately, since it would fail every round for an account without the tier.

## Repository Skills

Codex discovers repository skills only under `.agents/skills/`, from the working directory up to the repository root. Bootstrap deploys every pack skill to `.claude/skills/<name>/`, and a project keeps its own skills in `skills/<name>/`. On macOS and Linux, the composer therefore links both kinds after each committed compose. A pack skill whose effective hosts include `codex` (an entry's own `hosts:` overrides its pack's) gets `.agents/skills/<name>` pointing at `../../.claude/skills/<name>`. A project-local skill gets `.agents/skills/<name>` pointing at `../../skills/<name>`. The links are relative, so Codex reads the copy that bootstrap just refreshed or the file the project just edited, and no second copy can fall behind. `aa-core-skills` and agent-pack's `acad-skills` declare both hosts.

A skill is project-local when `skills/` and `skills/<name>/` are real directories, `skills/<name>/SKILL.md` is a regular file, and the name starts with a letter or digit and holds only letters, digits, `.`, `_`, and `-`. A symlink at any of the three paths is skipped, so a link never leads Codex out of the repository through a second hop, and a directory under another name is skipped without a report. When a name exists both ways, the link points at the project-local copy, which is the copy the lookup order in `AGENTS.md` gives every agent. A project-local skill added between sessions is linked by the next bootstrap, with or without a pack change. The one exception is a project that opts out of every pack with `packs: []`: its compose returns before the link pass, so nothing is linked or removed there.

The composer owns a link only when `.agents/skills/.gitignore` lists its name. That file starts with `# Managed by anywhere-agents; regenerated on every compose.` and ignores itself and every owned link, so `git status` stays clean. An owned link that points at the other kind of copy is replaced in the same run, and the summary reports it as retargeted. Every other entry stays untouched, and the compose summary names it with the reason: a path git tracks, a name that differs only by case, a directory or file already at the name, or a link the composer did not create. A link made by hand to the pack copy therefore keeps hiding a project-local skill of the same name until someone removes it. Nothing under `.agents` changes when git cannot report what is tracked. The same holds when a parent directory or the ignore file is a symlink, is tracked, or is not in the managed format; the next compose tries again.

Uninstall removes an owned link once what it points at is gone: the `.claude/skills/<name>` directory for a pack skill, and for a project-local one the layout described above, so a skill directory that was replaced by a symlink loses its link too. A completed `uninstall --all` also removes the links to project-local skills that still exist, since the links are the tool's and the skills are the project's. A link that should have gone and could not be removed turns a `clean` or `no-op` uninstall into `partial-cleanup`; a `drift` or `malformed-state` result stays as it is, and the detail line names the link. An uninstall that stops on drift or on a malformed state keeps every link whose target is still there.

A repository that ships skills from its own `skills/` directory and is never composed, such as this one, carries one tracked link instead: `.agents/skills -> ../skills`. It covers every skill in the directory, including one added later.

Codex Desktop's Settings > Import copies `.claude/skills` and `.claude/commands` into `.agents/skills` as one-time copies with "Claude" rewritten to "Codex". In an aa consumer, leave "Keep imports in sync" off and deselect Skills and Commands when importing from Claude Code. A copy at a skill's name blocks its link, and the summary reports it; move the copy out of `.agents/skills/`, and the next bootstrap links the current skill. A tool that writes into the linked name of a pack skill writes into `.claude/skills/<name>`. The next compose then stops on the drift gate, as it does after any edit to a managed skill; remove the edited skill directory and run bootstrap again. A write into the linked name of a project-local skill lands in the project's own `skills/<name>/`, which no gate covers, so it shows up in `git status` like any other edit.

Windows gets no links in this release, and the compose summary says so whenever a Codex-eligible pack skill or a project-local skill exists. A Windows run also leaves alone any link that an earlier run on another platform created. What Codex reads is as current as the last bootstrap run. Claude Code runs bootstrap at session start through its hook; a Codex session runs it when it follows the block at the top of `AGENTS.md` (see [Hooks](#hooks)).

## Hooks

Codex 0.153.3 and later ship hooks. Bootstrap registers `guard.py` as a `PreToolUse` hook in `~/.codex/hooks.json`, and the hook takes effect once the user chooses Trust at Codex's startup review. [Guard hook](guard-hook.md#under-codex) describes what it checks under Codex and where that differs from Claude Code. `session_bootstrap.py` is not wired to `SessionStart` (anywhere-agents#50), so a Codex session still runs bootstrap through the block at the top of `AGENTS.md` and prints the session banner on its first reply; see [Session banner](session-banner.md).

## The MCP Route (Retired)

Earlier versions of the shared file described registering Codex as an MCP server inside Claude Code (`claude mcp add codex -s user -- codex mcp-server -c approval_policy=never`), with notes on Windows PATH handling and approval dialogs. Measured over 60 days of local transcripts, the route appeared in 17 Codex invocations against 93 percent through `codex exec`, and the maintainer confirmed it unused; the 2026-09 rewrite removed it. Reviews run through `/vet`, which dispatches `codex exec` from a terminal on every platform. A fork that wants the MCP route registers the server at user scope (project-scoped entries do not propagate across directories) and restarts the session for `/mcp` to pick it up.
