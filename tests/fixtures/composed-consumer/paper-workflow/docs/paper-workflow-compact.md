<!-- SPDX-License-Identifier: CC-BY-4.0 -->

<!--
Compact paper-workflow pack: submodule, Overleaf merge, venue, and
revision rules for paper and proposal repos. docs/paper-workflow.md is the full reference.
-->

# Paper and Proposal Workflow

The submodule and Overleaf rules apply when the project has a `.gitmodules` file or an Overleaf-synced subdirectory; the venue and revision rules apply to any paper or proposal. Skip them all on prototype and OSS repos.

## Submodule Workflow

- At session start, if `.gitmodules` exists, run `git submodule status`. An uninitialized submodule (prefix `-`) gets a warning and the suggestion `git submodule update --init`.
- A submodule has its own `.git` and `origin`; commits and pushes inside it go to the submodule's upstream, not the parent.
- Submodules are shared repos: a push lands in a collaborator's Overleaf project or co-PI repo, and a careless force-push or overwrite destroys someone else's work. Treat every write inside a submodule as high-risk.
- When the user asks to push or pull a submodule:
  1. Before writing, run `git -C <submodule-path> fetch` and `status` for uncommitted changes, then `log --oneline -5` with and without `--remotes` for recent activity. This is a quick sanity check; a submodule is often in detached-HEAD state, where branch comparisons do not apply cleanly.
  2. Use `git -C <submodule-path>` inside the submodule, and confirm with the user before any commit, push, pull, or reset.
  3. Back in the parent repo, `git add <submodule-path>` and commit the updated pointer (also with confirmation).
- A submodule's `.gitignore` may exclude internal-only directories (`.agent/`, `guardrail/`, `figure-spec/`, `figure-src/`); they exist on disk but are not pushed, so a fresh clone lacks them. Warn the user when an expected internal directory is absent.
- Which directories, which upstream repos, and which files are internal-only belong in the project's `CLAUDE.md` or `AGENTS.local.md`, not here.

## Overleaf Merge Conflict Resolution

Overleaf's git bridge branches from its own snapshot, which may lag behind the latest local push. When a collaborator edits on Overleaf while we push structural changes locally, "theirs" in the merge means the older base plus the collaborator's edits. Taking that side with `git checkout --theirs` silently discards our work. Co-PI changes are the priority: our structural work (compaction, renames) can be redone in minutes, while a co-PI's content edits are their intellectual contribution and a silent drop goes unnoticed. Preserve both sides, and when in doubt, err toward the co-PI's content.

### Rules for Merging Overleaf Branches with Conflicts

1. Never use `git checkout --theirs` on a file where we have local structural changes.
2. Never stop at `git checkout --ours`. Our version is the right base, but the merge is not done until every co-PI content change is accounted for.
3. Inspect what the collaborator changed before resolving: `git merge-base HEAD <overleaf-branch>`, then `git diff <merge-base>..<overleaf-branch> -- <file>` isolates the co-PI's edits from our structural changes. Content (new or rewritten sentences, added references, deliberate deletions, terminology changes) is preserved, a deletion included. Formatting (spacing, font commands, styling) is applied if consistent with our version. A stale reversion (undoes our rename or compaction because they edited the pre-push snapshot) is discarded, with a note that the co-PI has not seen our change. When it might instead be a deliberate content choice, ask the user.
4. Apply their content changes onto our structural base: start from `git checkout --ours <file>`, then integrate every content change from step 3. Skipping one requires explicit user approval.
5. Verify both directions before committing. `git diff <pre-merge-commit> -- <file>` shows our structural changes survived. `git diff <overleaf-branch> -- <file>` shows that the only differences from the co-PI's version are our intended structural changes. New co-PI paragraphs or sections must appear in the merged file.
6. Screen for binary artifacts before staging. Overleaf branches often carry compiled PDFs, review screenshots (`out-review/`), and other build artifacts; the merge-base diff from checklist step 2 shows them. Add them to `.gitignore` before staging.

### Pre-Merge Checklist (Before `git merge <overleaf-branch>`)

1. `git fetch` for the latest Overleaf branch.
2. `git diff --stat $(git merge-base HEAD <overleaf-branch>)..<overleaf-branch>`: which files the co-PI touched, and any binary artifacts.
3. `git log --oneline HEAD..<overleaf-branch>`: what the collaborator did.
4. A file we modified structurally that appears in the diff is resolved by hand with the rules above.
5. A file we did not modify should auto-merge; still spot-check it afterwards for content loss.

### Recovery if `--theirs` Was Already Used

Restore our structural version with `git restore --source=<pre-merge-commit> --worktree -- <file>` (shell redirection on Windows brings encoding and line-ending issues), then reapply the collaborator's content and formatting changes on top. The reapply step is part of the recovery.

## Venue Conventions

For NSF and federal proposals, do not introduce DEI-related terms unless the solicitation explicitly requires them; a non-federal call that asks for DEI framing gets it. NSF Merit Review framework by default (intellectual merit and broader impacts); NIH Simplified Peer Review on biomedical proposals. For papers, follow the venue's own guidelines (NeurIPS, ICML, ICLR, KDD, ACL, EMNLP, CVPR), and generate venue-specific formatting (camera-ready, anonymous version) only when asked.

## Revision Discipline

1. **Filter revisions through the story.** The author records a one-sentence thesis and the headline claims in the README or plan file. A change enters the manuscript only if it changes a headline claim, helps a reader follow or check one, fixes an error, meets a venue requirement, or cuts. Triage review lists against the thesis, not item by item; the rest goes to the project's records.
2. **Keep the main text for the story.** Defenses against objections no reader raised, the project's path to a result, and small negative results off the main line go to the records. Main text grows only for a new result or for what a reader needs to follow or check a retained claim.
3. **Report the trend.** State the main pattern and how widely it holds. Report a statistical test only where the claim depends on it; qualify only where it changes the conclusion.
4. **Change direction only on purpose.** Run an experiment only if it can change a headline claim or a main table row. Before a finding narrows or reframes the story, state what it cuts and ask the author. Once AI panel scores stop moving, the next readers are people.
