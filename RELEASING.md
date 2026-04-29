# Releasing anywhere-agents

This document captures the exact steps used to release a new version of `anywhere-agents`. The repo and both packages (PyPI + npm) share **one version number**, bumped together and tagged on the same commit so that checking out the tag reproduces exactly what is published.

## Prerequisites (one-time)

| Item | Needed for | Setup |
|------|------------|-------|
| Python 3.9+ with `build` and `twine` | PyPI publish | `pip install build twine` |
| Node.js 14+ with `npm` | npm publish | Any recent install |
| `git` | everything | — |
| PyPI API token in `~/.pypirc` | `twine upload` | Generate at https://pypi.org/manage/account/token/; for CI/unattended use, prefer a project-scoped token |
| npm token with **Bypass 2FA** enabled in `~/.npmrc` | `npm publish` without interactive OTP | Generate at https://www.npmjs.com/settings/<you>/tokens/new; check the "Bypass Two-Factor Authentication" box. Required because npm requires 2FA on publish by default and publishing with WebAuthn/Windows Hello cannot accept `--otp` |
| Chrome or Chromium headless | Regenerate `docs/hero.png` from `docs/hero.html` when hero content changes | — |

## Pre-release checks

From a clean `main` with no uncommitted changes:

```bash
# 1. Run the full test suite — must pass locally on your OS, and CI on Ubuntu + Windows must be green
python -B -m unittest discover -s tests -p "test_*.py" -v

# 2. Whitespace-clean diff (no trailing spaces, no tab/space mixing)
git diff --cached --check

# 3. Leak sweep: no personal identifiers slipped in
grep -rEi "yuezh|yzhao010|USC|miniforge3|py312|Overleaf" \
  --include="*.md" --include="*.py" --include="*.json" --include="*.yml" \
  --include="*.yaml" --include="*.sh" --include="*.ps1" \
  .
# Review hits: "USC" in the maintainer credential is OK; "co-PI" inside hyphenation examples is OK; any other match is a leak and must be fixed.

# 4. Bilingual README parity: if README.md changed in this release, README.zh-CN.md must mirror the change.
#    Structural drift (sections, figures, scenario order) is not allowed; translation nuance is fine.
git diff --name-only HEAD~1 HEAD -- README.md README.zh-CN.md
# If only one of the two paths appears in the output, something drifted — update the other to match
# before tagging. Specifically: if README.md gained or lost a figure, scenario, or heading, apply the
# same change to README.zh-CN.md (translated). New images used by README.md (e.g. docs/*.png) must
# also be referenced in README.zh-CN.md unless the image is English-only by design.

# 5. Cross-repo parity with the private `yzhao062/agent-config` source repo.
#    Run the automated checker from the agent-config clone (needs both repos side by side):
bash ../agent-config/scripts/check-parity.sh
#    The script splits shared-core files into two categories:
#      STRICT (must be byte-identical; any diff or missing file fails):
#        scripts/{guard.py, session_bootstrap.py, generate_agent_configs.py, pre-push-smoke.sh,
#        remote-smoke.sh}, .claude/settings.json, .githooks/pre-push,
#        .github/workflows/{real-agent-smoke.yml, validate.yml}, .claude/commands/*.md for
#        each of the 4 shipped skills, skills/{implement-review,ci-mockup-figure,readme-polish}
#        recursive trees.
#      BY-DESIGN (expected to differ; both sides must exist; a +/- line delta is
#      reported per file for eyeball):
#        AGENTS.md (USC / Overleaf / PyCharm stripping),
#        bootstrap/bootstrap.{sh,ps1} (default-upstream + CRLF-config stripping),
#        user/settings.json (additionalDirectories stripping),
#        skills/my-router (routing-table rewrite + extension guidance for forks).
#    Exit 0 means STRICT clean and every BY-DESIGN mirror present. Exit 1 means either
#    STRICT drift or a missing required BY-DESIGN mirror, and must be fixed before
#    tagging. A byte-for-byte match in BY-DESIGN is flagged as a warning because it
#    usually means a sanitization step was skipped during backport.
#
#    Single-side files (no mirror; script does not check these):
#      anywhere-agents only: README.md, README.zh-CN.md, CHANGELOG.md, RELEASING.md, packages/,
#        docs/ (hero, banner, RTD content), .readthedocs.yaml, mkdocs.yml,
#        .github/workflows/{docs-strict-build.yml, package-smoke.yml}
#      agent-config only: docs/anywhere-agents.md and other private docs, reference-skills/,
#        MIGRATIONS.md, private-only skills (bibref-filler, dual-pass-workflow, figure-prompt-builder),
#        scripts/check-parity.sh itself (maintainer-only tool).

# 6. Dual-OS local test: run the full suite on a Linux machine via SSH before tagging.
#    Windows-only local coverage misses POSIX-specific behavior (HOME vs USERPROFILE, path
#    separators, case sensitivity, symlinks, shell tooling). The Spark release-gate box
#    (ARM64 Ubuntu) is the maintainer's Linux target. CI runs on x86_64 Ubuntu workers, so
#    Spark adds ARM64 + a separate filesystem layout to the matrix.
#
#    The shared-core files that need Linux validation (scripts/guard.py, scripts/session_bootstrap.py,
#    tests/test_guard.py, bootstrap/bootstrap.sh) are byte-identical between agent-config and
#    anywhere-agents, so cloning EITHER repo on Spark and running its test suite validates the
#    code that will ship in both. Using the private agent-config clone is convenient because it
#    is where the shared-core Python tests live (anywhere-agents inherits the identical tests
#    via cross-repo parity check #5 above).
ssh yzhao062@spark-37f2.local '
  if [ -d ~/agent-config ]; then
    git -C ~/agent-config pull --ff-only
  else
    git clone https://github.com/yzhao062/agent-config.git ~/agent-config
  fi
  python3 -B -m unittest discover -s ~/agent-config/tests -p "test_*.py" 2>&1 | tail -5
'
# Must report "OK" (any number of passes, skipped allowed). If not, stop and investigate on Spark
# before tagging. Typical failures: Linux-specific path handling in new hook logic, shell script
# syntax that Git Bash on Windows tolerated but POSIX sh rejects, or a test that hardcoded a
# Windows drive letter / backslash.
```

If any of the six checks fail, stop and fix before continuing.

## Real-agent smoke tests

Two scripts exercise different layers. CI runs complementary workflows (`validate.yml`, `real-agent-smoke.yml`, `package-smoke.yml`) on every push, every release, and weekly.

### Pre-tag gate: validate the release candidate

`scripts/pre-push-smoke.sh` checks the commit you are about to tag — **not** the published package:

1. Regenerates `CLAUDE.md` / `agents/codex.md` in a temp dir from the committed `AGENTS.md` and diffs against the committed files. Catches stale generator output.
2. Runs `claude -p "..."` in the repo root and asserts the response lists every skill under `skills/`. Proves Claude actually loads the committed `CLAUDE.md`.
3. Runs `codex exec "..."` with the same assertion for Codex.

Agent calls are skipped (not failed) if the CLI is missing, so the script is useful on machines with only one agent configured.

```bash
bash scripts/pre-push-smoke.sh
```

The pre-push git hook (`.githooks/pre-push`, enable with `git config core.hooksPath .githooks`) runs this automatically when a push touches `AGENTS.md`, `bootstrap/`, `scripts/`, or `skills/`.

### Post-publish verification: validate the published artifacts

`scripts/remote-smoke.sh` bootstraps a throwaway project via the **published** package — `pipx run anywhere-agents`, `npx anywhere-agents`, or the raw-shell install — then asserts file structure + user-level hook deployment + agent behavior end-to-end. Useful for ad-hoc verification from a maintainer machine:

```bash
bash scripts/remote-smoke.sh

# From a dev machine, via SSH to an agent-equipped host (e.g., the DGX release-gate box):
ssh user@host 'bash -s' < scripts/remote-smoke.sh
```

CI runs `.github/workflows/package-smoke.yml` automatically on `release: published` and weekly, covering the same surface across a larger OS × Python/Node matrix. The manual `remote-smoke.sh` is convenient when you want to spot-check immediately after publishing without waiting for the weekly scheduled run.

Prerequisites on the agent-equipped machine: `git`, `python3`, and at least one of `pipx` / `npx` / `curl` for the install step; plus `claude` CLI with `ANTHROPIC_API_KEY` and `codex` CLI with `OPENAI_API_KEY` for the roster assertions (which are skipped gracefully if a CLI is missing).

## Version bump (single source of truth)

Only **three files** hold the release version. The CLIs read their version at runtime from these files, so there are no other strings to touch.

```bash
# PyPI package
#   - packages/pypi/pyproject.toml          → project.version
#   - packages/pypi/anywhere_agents/__init__.py  → __version__

# npm package
#   - packages/npm/package.json             → "version"
```

Use the same version number across all three files. Add a `[X.Y.Z] — YYYY-MM-DD` section to `CHANGELOG.md` with what changed, moving anything from `[Unreleased]`. Update the compare-link block at the bottom of the changelog so `[X.Y.Z]` resolves and `[Unreleased]` compares against the new tag.

## Verify version locally before tagging

Build, check metadata, then install into a scratch venv so the daily environment stays clean and any missing package-data / entry-point issue shows up before publish. Run the import check from outside the repo root, since Python's `sys.path[0]=''` would otherwise resolve `import anywhere_agents` to the source tree instead of the installed wheel and mask real packaging bugs.

```bash
rm -rf packages/pypi/dist packages/pypi/build packages/pypi/*.egg-info
python -m build packages/pypi --outdir packages/pypi/dist
python -m twine check packages/pypi/dist/*

SCRATCH=$(python -c "import tempfile; print(tempfile.mkdtemp(prefix='aa-prerelease-'))")
python -m venv "$SCRATCH/venv"
"$SCRATCH/venv/Scripts/python.exe" -m pip install --quiet packages/pypi/dist/*.whl
cd "$SCRATCH"
"$SCRATCH/venv/Scripts/python.exe" -c "import anywhere_agents; print(anywhere_agents.__version__)"   # should print X.Y.Z
"$SCRATCH/venv/Scripts/anywhere-agents.exe" --version   # should print anywhere-agents X.Y.Z
cd -

# Run the Node CLI directly (no install)
node packages/npm/bin/anywhere-agents.js --version   # should print anywhere-agents X.Y.Z
```

On macOS / Linux, replace `Scripts/python.exe` with `bin/python` and `Scripts/anywhere-agents.exe` with `bin/anywhere-agents`.

## Commit, tag, push

**MANDATORY**: do not push the tag (and therefore do not run `twine upload` or `npm publish`) until the `Validate` workflow on `main` is green for the release commit. PyPI and npm are immutable post-publish; if CI fails after upload, the only remediation is a fix-forward release. The agent-style v0.3.4 shipped a cosmetic dead-link bug because tag + publish ran before CI completed; this gate prevents the same class of failure here.

```bash
# Stage and review the entire release scope before committing. The
# agent-style v0.3.5 release demonstrated that narrow `git add packages/
# CHANGELOG.md` repeatedly under-stages — release content can span
# bootstrap/, scripts/, docs/, .github/, agents/, and config files. Always
# status-review before committing a release.
git status --short
git add -A
git diff --cached --name-only       # eyeball: every release file present?
git commit -m "release: vX.Y.Z — <short summary>"
git push origin main

# CI-green gate: block until validate.yml passes on THIS release commit.
# `gh run watch` accepts no branch/commit filter; the no-id form may attach to a
# different run or prompt interactively. Resolve the run id by commit first.
release_sha="$(git rev-parse HEAD)"
run_id=""
for attempt in $(seq 1 30); do
  run_id="$(
    gh run list \
      --repo yzhao062/anywhere-agents \
      --workflow validate.yml \
      --commit "$release_sha" \
      --limit 5 \
      --json databaseId \
      --jq '.[0].databaseId // empty'
  )"
  if [ -n "$run_id" ]; then
    break
  fi
  sleep 10
done
[ -n "$run_id" ] || { echo "FAIL: no validate.yml run found for $release_sha" >&2; exit 1; }
gh run watch "$run_id" --repo yzhao062/anywhere-agents --exit-status

# Only after CI is green: tag and push the tag, then publish (next sections).
git tag -a vX.Y.Z -m "Release X.Y.Z"
git push origin vX.Y.Z
```

The tag must be on the same commit as the version bump. Later reviewers verify that checking out the tag and running `python -m build packages/pypi` produces the same artifact that is on PyPI.

## Publish to PyPI

Two-step flow: upload to TestPyPI first to catch metadata or auth issues cheaply, then to real PyPI. Skip TestPyPI only for hotfixes with no packaging changes; it costs ~90 seconds and catches genuine regressions.

### Step 1 — TestPyPI dry run

Assumes a `pypitest` section in `~/.pypirc` pointing at `https://test.pypi.org/legacy/` with a TestPyPI-scoped token. TestPyPI's simple index can lag the JSON API by 30-60 seconds after upload; the `sleep 60` turns that into an executable wait rather than a flaky race.

```bash
python -m twine upload --repository pypitest packages/pypi/dist/*
sleep 60

SCRATCH=$(python -c "import tempfile; print(tempfile.mkdtemp(prefix='aa-testpypi-'))")
python -m venv "$SCRATCH/venv"
cd "$SCRATCH"
"$SCRATCH/venv/Scripts/python.exe" -m pip install --quiet \
    --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ anywhere-agents==X.Y.Z
"$SCRATCH/venv/Scripts/python.exe" -c "import anywhere_agents; assert anywhere_agents.__version__ == 'X.Y.Z'; print(anywhere_agents.__version__)"
cd -
```

On macOS / Linux, replace `Scripts/python.exe` with `bin/python`.

### Step 2 — Real PyPI upload

After the TestPyPI verify passes, upload to the real index:

```bash
python -m twine upload packages/pypi/dist/*
```

The `~/.pypirc` token authenticates automatically (no interactive prompt).

Verify the upload:

```bash
# Short delay may be needed if PyPI CDN is slow; add --no-cache-dir to bypass local pip cache
pip install --upgrade --force-reinstall --no-cache-dir anywhere-agents==X.Y.Z
anywhere-agents --version   # should print X.Y.Z
```

## Publish to npm

```bash
npm publish packages/npm --access public
```

The bypass-2FA token in `~/.npmrc` authenticates automatically. Verify:

```bash
npm view anywhere-agents version   # should print X.Y.Z
```

Then sanity-check from a throwaway directory:

```bash
cd "$(mktemp -d)"
npx anywhere-agents@X.Y.Z --version
```

## Post-release

- **Create the GitHub release.** Required to trigger `real-agent-smoke.yml` and `package-smoke.yml`; both workflows bind to `release: published`, not to tag push, so without a GitHub release the post-publish CI never fires and the release is not validated end-to-end. Draft the body in `vX.Y.Z-release-notes.md` and one-time-only add `v*-release-notes.md` to `.git/info/exclude` so drafts never land on `git add -A`:

  ```bash
  echo "v*-release-notes.md" >> .git/info/exclude   # one-time, repo-local ignore
  gh release create vX.Y.Z --target main --title "vX.Y.Z" --notes-file vX.Y.Z-release-notes.md
  ```

  Confirm both workflows go green on the Actions tab before announcing the release.
- Close any GitHub issues that were addressed by the release (reference the tag in the closing comment).
- Update `[Unreleased]` section of `CHANGELOG.md` to start fresh (`_No unreleased changes queued._`).
- Delete the local `vX.Y.Z-release-notes.md` scratch file.

## CI API cost exposure (for humans and agents)

Every workflow in `.github/workflows/` that calls a model API has a documented per-dispatch cost. Before any `gh workflow run` on a workflow that is not free, **agents MUST confirm with the user** — "the user approved a larger task" is not blanket approval for paid dispatch. Each workflow click is a separate billable action. The same table applies to the twin `agent-config` repo because `real-agent-smoke.yml` and `validate.yml` are STRICT byte-identical mirrors between the two repos (see cross-repo parity check under Pre-release checks).

### Workflow cost table

| Workflow | Trigger(s) | Cost per run | Safeguards |
|---|---|---|---|
| `validate.yml` | push + PR | $0 (no API calls) | — |
| `docs-strict-build.yml` | push + PR | $0 (no API calls) | — |
| `real-agent-smoke.yml` | `release: published` + manual | ~$0.04 | Sonnet pin on `claude -p`; handshake-only (skill-roster enumeration) |
| `package-smoke.yml` | `release: published` + weekly cron + manual | $0 (install/verify only, no API keys) | 12-attempt retry loop absorbs PyPI/npm CDN lag |

### Dispatch-approval policy (for agents)

Any `gh workflow run` on a workflow whose per-dispatch cost is **above $0.01** requires explicit per-dispatch user approval, even inside a broader approved task. Before dispatching:

1. Name the workflow and the measured or estimated cost for this specific set of inputs.
2. State the hypothesis you expect the run to verify.
3. Wait for an explicit `dispatch` / `go` / `yes` from the user.

`validate.yml`, `docs-strict-build.yml`, and `package-smoke.yml` (all $0) may be dispatched inside an approved task without per-run confirmation. Only `real-agent-smoke.yml` meets the approval threshold today.

### Annual cost forecast

| Category | Typical year |
|---|---|
| `real-agent-smoke` auto-triggered on release (both twin repos, ~4-6 releases each/year) | ~$0.40-$0.50 |
| Manual `real-agent-smoke` dispatches for debugging | ~$0-$0.50 |
| **Total (both repos)** | **~$0.50-$1.00** |

If a future workflow adds API-calling logic, update this table and the policy threshold in the same commit — the commit diff is the signal to humans + agents that the cost model changed.

## Common gotchas

- **PyPI CDN cache.** After `twine upload`, a fresh `pip install --upgrade` may still report the previous version for a minute or two. Use `--force-reinstall --no-cache-dir` with an explicit `==X.Y.Z` to verify.
- **npm without bypass-2FA.** If the token does not have bypass 2FA enabled and you use Windows Hello for npm 2FA, publishing fails with a 403 and cannot be completed with `--otp=` (WebAuthn does not produce a 6-digit code). Regenerate the token with bypass 2FA or switch to a classic Automation token.
- **Version drift.** If you change one of the three version files but forget another, the published package advertises one version in metadata and another via `--version`. The refactor (Python `__version__` import, Node `package.json` read-at-runtime) prevents this for the CLI output, but the package metadata is still authored separately in each ecosystem — keep them in sync by hand or script it.
- **Tag-before-publish.** Always tag before publishing. If publishing fails or you need to amend the release, it is easier to adjust a local tag than to retract a published package (PyPI and npm consider release versions immutable).

## Reference

- Private release workflow (two-repo sync + sanitization discipline): see `docs/anywhere-agents.md` in the private `yzhao062/agent-config` repo.
- Review history for each release: see `CHANGELOG.md` "Review history" sections.
- CI that guards the release: `.github/workflows/validate.yml` runs the test suite on Ubuntu + Windows.
