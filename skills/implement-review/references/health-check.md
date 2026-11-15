# Health Check Reference
Read this when `health-check` prints any `FAIL`, or a `WARN` marker the coordinator does not recognize, or when a Phase 2.5 claim needs a method.

## Phase 2.0 prologue: the ten Health checks (Auto-terminal channel only; Terminal-relay skips)

For the Auto-terminal channel only, run 10 structural Health checks plus 3 Substance heuristics on `Review-Codex.md` before the existing freshness + scope partition begins. In a multi-reviewer round, run them once per reviewer with that reviewer's state directory and exact review file. `Review-Codex.md` in the table below then means the file passed to that run. Required dispatch state files (`<state-dir>/pre-mtime`, `<state-dir>/timestamp`, `<state-dir>/tail`, plus optionally `<state-dir>/stall-warning` when stall-watch wrote it) come from the `dispatch-codex` and `stall-watch` scripts via dispatch-codex's stdout `STATE-DIR <abs-path>` line. Health checks 2, 7, 8, 9 and the Substance heuristics depend on these files.

| # | Check | Failure → |
|---|---|---|
| 1 | `Review-Codex.md` exists at repo root | Surface failure; route through the failover queue in section 1c of `SKILL.md` |
| 2 | File mtime is later than the Phase 1c dispatch timestamp AND later than `<state-dir>/pre-mtime` (catches stale file from prior round or task) | Same as 1 |
| 3 | First line equals `<!-- Round N -->` | Same as 1 |
| 4 | File size ≥ 500 chars | Same as 1 |
| 5 | "Verification notes" section present (`Verification notes:` paragraph form OR `## Verification notes` / `**Verification notes**` heading) | Same as 1 |
| 6 | If the dispatch prompt named a plan file or staged file list, the review's file scope mentions that current scope | Same as 1 |
| 7 | **Review-text** suspicious-phrase scan: case-insensitive match in the saved review body on any of `could not`, `i cannot`, `failed to`, `permission denied`, `rate limit`, `unable to access`, `unable to execute`, `unable to run`, `not able to execute`, `not able to run`, `do not have access`, `not authenticated`, `authentication failed`, `unauthorized`, `timed out`, `timeout`, `quota`, `command not found`, `no such file`, `sandbox.*fail`. Exclude content inside backtick code spans (Codex meta-discussing the pattern list is not failure narration). | Do not hard-fail. Inspect the `lines=` it names in `Review-<Agent>.md`: a hit on quoted prose is noise, and a hit that narrates a tool failure blocking verification, or a truncated or placeholder review, is a defect. |
| 8 | **Dispatch-tail** tool-failure scan: case-insensitive regex match in `<state-dir>/tail` (full codex-exec stdout+stderr, NOT the review body) for `tool ... failed`, `mcp tool failed`, `HTTP/status 429/5xx`, `rate limit`, `quota exceeded`, `insufficient_quota`, `connection refused/reset/timed out`, `context_length_exceeded`, `maximum context length`, Windows sandbox launch failures such as `CreateProcessAsUserW failed: 1312` or `windows sandbox: runner error`, the Windows NTSTATUS spawn-failure family `-107374\d{4}` (signed-decimal `0xC000xxxx` exit codes printed when a spawned shell dies before entry), and errno forms `ENOSPC` / `EACCES` / `ETIMEDOUT` / `ECONNRESET` / `ECONNREFUSED`. The scan is line-level (Fix A + Fix B; see `health-check.py`): strip backtick code spans, then skip modeled pattern-echo lines (a line-numbered source citation, or a line quoting a literal regex-source fragment such as `\bENOSPC\b` or `sandbox.*runner error`), then on the surviving lines count **intrinsic** failure forms (`HTTP/status 429/5xx`, `Too Many Requests`, `rate limit exceeded`, quota / `insufficient_quota`, `context_length_exceeded`, the 1312 / sandbox-runner shapes, signed-decimal NTSTATUS exits, errno forms) on their own, and **generic** words (bare `rate limit`) only when an error-frame token sits on the same or an adjacent line. A bare line-number prefix or a backslash never suppresses a line, so a real line-numbered or Windows-path failure still surfaces. This check also runs when Check `1` fails. See `health-check.py` for the canonical pattern lists. A `backend-failure` record in the state directory counts as one marker (`recovered`). The Agy dispatcher writes it just before publishing a review from a result that ended in a backend error. | Do not hard-fail. Inspect the matching failure lines in `<state-dir>/tail` and, when present, `<state-dir>/tail.stderr-tmp`, where the Claude and Agy dispatchers write stderr. When `breakdown=` includes `recovered:`, also read `<state-dir>/backend-failure` and check whether the review ends cleanly. |
| 9 | **Stall-watch** check: `<state-dir>/stall-warning` file does NOT exist. The `stall-watch` background daemon (launched by `dispatch-codex` alongside `codex exec`) appends to this file when `<state-dir>/tail` has zero growth for at least 600 seconds (10 minutes by default). This is a low-confidence silence signal that can reflect long model generation as well as a CLI or network stall; it never kills the worker. File present = at least one silence period occurred. | Do not hard-fail. Inspect `<state-dir>/stall-warning` and whether the review ended cleanly. |
| 10 | **Verdict-verification contract**: A labeled `PASS` or `BLOCK` verdict has a standalone `Verification status: VERIFIED` marker. An `UNVERIFIED` status or verdict, conflicting status or verdict values, `PASS` or `BLOCK` with a missing status, or `VERIFIED` with Verification notes that explicitly report no execution is invalid. A review with no labeled verdict remains accepted for compatibility. For a `Commit verdict` or `Plan verdict` label, the check takes the value from anywhere after the label or from the first nonempty line within the next four. A bare `Verdict` label, including a heading, counts only when its value is the first word, and the next line is read only when the label stands alone. | Hard-fail intake and surface the exact `FAIL check-10 ...` detail. Do not treat the verdict as execution-backed. |

A `WARN` from Check 7, 8, or 9 follows the inspection in step 7 of the round checklist of `SKILL.md`, which also says which defects ask the user.

> Implementer note: pipes inside Check 7 and Check 8 regex patterns are escaped as `\|` where Markdown-table parsing requires it. The runtime regex engine (Python `re`, etc.) must receive unescaped alternation (`|`). The health-check script should compile patterns from a source-of-truth list rather than from the rendered Markdown.

### Substance heuristics (Auto-terminal only; soft signals; a hit is inspected; never hard-fails)

The 10 structural Health checks above verify the file shape, dispatch signals, and verdict-verification marker. They do NOT catch a review that is structurally clean but substantively shallow (Codex's tools silently failed mid-run; `rate limit`; context overflow; or the model did not engage). Three lens-aware heuristics fire after the 10 Health checks and surface independently. **A hit follows the same inspection as Checks 7 to 9**: `substance-1` by its `elapsed=` and `prompt=` pair, `substance-2` by whether the review has file:line anchors (`0-anchors`), and `substance-3` by the scope-challenge axes named in `missing-axes=`.

| Heuristic | Signal | Surface format |
|---|---|---|
| Time-to-completion floor | Elapsed wall time from `<state-dir>/timestamp` to `Review-Codex.md` finalization is less than 30s, when the dispatched prompt is ≥ 2000 chars | `Substance heuristic: review completed in <T>s for <P>-char prompt -- Codex may have bailed early. Spot-check before trusting.` |
| Anchor density | Review body > 1000 chars AND zero file-line anchors matching `:\d+`, `\bline \d+\b`, `\blines? \d+\s*[-–]\s*\d+\b`, or `<file>:<line>` patterns | `Substance heuristic: review has <N> chars and 0 file:line anchors -- may be generic prose without code/line grounding. Spot-check High findings.` |
| Scope-challenge engagement (plan-review lens only) | Review does NOT contain visible evidence of ALL three scope-challenge axes. Evidence per axis = `(a)` / `(b)` / `(c)` enumeration OR keyword coverage (case-insensitive): **Axis 1** (`scope` AND one of `smaller` / `larger`), **Axis 2** (one of `deferral` / `process tax` / `release cycle`), **Axis 3** (one of `simplest` / `do nothing` / `doing nothing` / `smaller path` / `shrink` / `docs only` / `document only` / `script only` / `no-op`). Axis-3 keyword set is intentionally broad to avoid false positives on substantive reviews. | `Substance heuristic: plan-review did not visibly engage scope-challenge axes <missing-axis-numbers>. May be incomplete per skill mandate.` |

Substance heuristics are skipped for Terminal-relay (the user has direct eyes on the run).

### False-positive tuning principle

The Health checks (especially Check 7) and Substance heuristics are intentionally soft (surface and inspect, never hard-fail) because false positives are expected. Observed FP modes during dogfooding:

- Check 7 firing on Codex's own meta-discussion of the regex pattern list. Mitigated: Check 7 excludes content inside backtick code spans.
- Check 8 firing on Codex stdout echoing pattern strings, especially when /implement-review reviews the implement-review skill itself or any review prompt that names the patterns. Mitigated in three layers: (1) backtick-code-span exclusion, as Check 7; (2) Fix A, a line-level echo classifier that skips line-numbered source citations and literal regex-source lines; (3) Fix B, an intrinsic/generic split so terse real failures (a bare `Rate limit exceeded` or `HTTP/1.1 429` line) still count while bare ambiguous words need an error frame nearby. After these, a normal review whose tail has no failure vocabulary scans to 0 markers. A review whose subject IS the failure vocabulary (the implement-review skill itself) still surfaces a reduced count: prose discussion of intrinsic terms is indistinguishable from real failures, so the residual is the safe floor (real-failure-wins), recognized downstream via the breakdown shape. A live run on the preserved R1 self-review tail dropped 48 markers to 40 under this rule while every injected real-failure line still surfaced.
- Substance axis-3 firing on synonym phrasings (`shrink to docs only` rather than `simplest path`). Mitigated: axis-3 keyword set broadened.
- Anchor density firing on a precise convergence review with low anchor count. Accepted as rare; inspect the review (file:line anchors, `0-anchors`) and advance.

Operational principle: better to under-flag a real signal than to cry wolf so often that the coordinator inspects every flag by rote and the user stops reading the round summary. Alarm fatigue is not recoverable; once flags are routinely waved through, the silent-failure mitigation collapses. Track FP rate per check; if any check class exceeds 30% FP, tune the trigger criteria (broaden keywords, exclude additional contexts, raise size threshold) until signal-to-noise is workable.

## Phase 2.5 methods

Pick the cheapest applicable method per claim type:

| Claim type | Verification method |
|---|---|
| Citation / paper / author existence | `WebSearch` for title and author; `WebFetch` arXiv / DOI URL; cross-check against the project's `.bib` file |
| Code behavior assertion | Run the targeted test (`pytest path::test_name`); read the source to trace; eval a small repro snippet |
| Link reachability | `WebFetch` of the URL; HEAD request via curl |
| Page limit / word count / file size | `wc -w`, `wc -l`, `ls -la`; compile and read the LaTeX log |
| Compile-time / type error | Run the compiler (`latexmk`, `tsc`, etc.) or type checker (`mypy`, `pyright`) on the affected file |
| Anonymization or leak claim | `Grep` for the alleged term; cross-check against the project's de-anonymization checklist |

If no listed method applies, ask the user before either accepting the claim or pushing back.

## Phase 2.5 example

Round 2 of a paper review. Reviewer flags High: "Citation [Smith2023] does not exist; could not find this paper on arXiv or Google Scholar."

1. Trigger fires (High priority + checkable factual claim).
2. Verification path: `WebSearch "Smith 2023 <topic keywords>"`, then `WebFetch arxiv.org/abs/<id>` if a candidate surfaces.
3. Outcome:
   - If a real paper is found → **Refuted**. Reply to the reviewer with the URL and arXiv ID, ask them to confirm before re-flagging. Do not edit the citation.
   - If no paper is found after a reasonable search → **Verified**. Remove the citation or replace it with a real one.
4. Record in round history: `[Smith2023] reference (Verified in round 2 — not present on arXiv or Google Scholar)` or `[Smith2023] reference (Refuted in round 2 — arxiv.org/abs/2023.XXXXX confirms paper)`.

## Root Review Sink (per reviewer)

When a review produces substantial written feedback, each reviewer saves the latest review to `Review-<AgentName>.md` in the repository root. Normalize `<AgentName>` as follows: choose the stable agent or product name visible to the user (not a transient model/version list unless that is the only identity available); convert any run of whitespace to a single dash; delete every character except ASCII letters, digits, and dashes; collapse repeated dashes; trim leading and trailing dashes. Examples: `Codex` → `Review-Codex.md`, `Antigravity` → `Review-Antigravity.md`, `Claude Code` → `Review-Claude-Code.md`, `Grok` or `Grok Build` → `Review-Grok.md`. If the normalized result is empty or a reviewer cannot identify itself with reasonable confidence, it uses `Review-Unknown.md` and notes the uncertainty at the top of the file.

One file per reviewer, one round per file. Treat each file as a reusable scratch file for the current review round, not as a permanent archive. By default, publish each new round by writing a complete temporary file beside the target and atomically renaming it over the reusable review file; do not create per-directory review files, and do not append multiple rounds, unless the user explicitly asks to preserve history. Running two reviewers in the same round produces two files (e.g., `Review-Codex.md` and `Review-Antigravity.md`), which Phase 2 reads together and consolidates.

Legacy `CodexReview.md` files from pre-upgrade sessions are ignored by Phase 2 intake. If one is present in the repo root or under `docs/`, treat it as stale scratch output from the old single-reviewer flow unless the user explicitly asks to inspect it.

The purpose of the `Review-*.md` files is to let the user and Claude Code read, reuse, and forward the latest review(s) without copy-pasting from chat. Keep each file in plain Markdown and make it directly useful on its own. Include:

- a `<!-- Round N -->` HTML comment on the first line (used by Phase 2 to verify freshness)
- a `Verification notes` paragraph or short bulleted list at the top of the review (immediately after `# Review`), stating what was compiled, run, or verified; write "none" if nothing at runtime
- one standalone `Verification status: VERIFIED` or `Verification status: UNVERIFIED` line after the notes, which Check 10 reads; use `VERIFIED` only when at least one relevant verification command completed, and pair `UNVERIFIED` with `Commit verdict: UNVERIFIED` rather than a `PASS` or `BLOCK` verdict
- the file or diff scope reviewed
- the review lens or context
- findings separated into **New** and **Previously raised** sections (previously raised items tagged Fixed, Still open, Reopened, or Deferred; on Round 1 the Previously raised section may be omitted or shown as "None")
- concrete recommended changes, with exact values when relevant
- for any finding flagged High priority, an exact suggested rewrite with file path and line range (use a fenced code block for multi-line rewrites)
