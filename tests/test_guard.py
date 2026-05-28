"""Tests for guard.py hook. Discovers guard.py relative to the repo root."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "guard.py"


def run_guard(command: str) -> str:
    """Run guard.py with a simulated hook input and return the decision."""
    payload = json.dumps({"tool_input": {"command": command}})
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"guard.py crashed (exit {result.returncode}): {result.stderr.strip()}"
        )
    stdout = result.stdout.strip()
    if not stdout:
        return "PASSED"
    data = json.loads(stdout)
    return data["hookSpecificOutput"]["permissionDecision"].upper()


def run_guard_full(command: str) -> dict | None:
    """Run guard.py and return the full parsed JSON payload, or None if no output."""
    payload = json.dumps({"tool_input": {"command": command}})
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"guard.py crashed (exit {result.returncode}): {result.stderr.strip()}"
        )
    stdout = result.stdout.strip()
    if not stdout:
        return None
    return json.loads(stdout)


class CompoundCdTests(unittest.TestCase):
    """Compound cd commands should be denied."""

    def test_cd_and_pdflatex(self) -> None:
        self.assertEqual(run_guard("cd papers/foo && pdflatex main.tex"), "DENY")

    def test_cd_semicolon_rm(self) -> None:
        self.assertEqual(run_guard("cd /tmp; rm -rf /"), "DENY")

    def test_cd_and_bibtex(self) -> None:
        self.assertEqual(run_guard("cd papers/foo && bibtex main"), "DENY")

    def test_cd_with_leading_space(self) -> None:
        self.assertEqual(run_guard("  cd /tmp && ls"), "DENY")

    def test_set_e_semicolon_cd(self) -> None:
        self.assertEqual(run_guard("set -e; cd repo && make"), "DENY")

    def test_set_ex_semicolon_cd(self) -> None:
        self.assertEqual(run_guard("set -ex; cd repo && make"), "DENY")

    def test_set_e_and_cd(self) -> None:
        self.assertEqual(run_guard("set -e && cd repo && make"), "DENY")

    def test_cd_semicolon_ls(self) -> None:
        self.assertEqual(run_guard("cd /tmp; ls -la"), "DENY")

    def test_cd_or_exit(self) -> None:
        self.assertEqual(run_guard("cd repo || exit 1"), "DENY")


class DestructiveGitDirectTests(unittest.TestCase):
    """Destructive git commands in direct form should ask."""

    def test_push(self) -> None:
        self.assertEqual(run_guard("git push origin main"), "ASK")

    def test_push_bare(self) -> None:
        self.assertEqual(run_guard("git push"), "ASK")

    def test_commit(self) -> None:
        self.assertEqual(run_guard('git commit -m "fix bug"'), "ASK")

    def test_merge(self) -> None:
        self.assertEqual(run_guard("git merge origin/main"), "ASK")

    def test_rebase(self) -> None:
        self.assertEqual(run_guard("git rebase main"), "ASK")

    def test_reset_hard(self) -> None:
        self.assertEqual(run_guard("git reset --hard HEAD~1"), "ASK")

    def test_clean(self) -> None:
        self.assertEqual(run_guard("git clean -fd"), "ASK")

    def test_branch_D(self) -> None:
        self.assertEqual(run_guard("git branch -D feature"), "ASK")

    def test_branch_d(self) -> None:
        self.assertEqual(run_guard("git branch -d feature"), "ASK")

    def test_branch_delete(self) -> None:
        self.assertEqual(run_guard("git branch --delete feature"), "ASK")

    def test_tag_d(self) -> None:
        self.assertEqual(run_guard("git tag -d v1.0"), "ASK")

    def test_tag_delete(self) -> None:
        self.assertEqual(run_guard("git tag --delete v1.0"), "ASK")

    def test_stash_drop(self) -> None:
        self.assertEqual(run_guard("git stash drop stash@{0}"), "ASK")

    def test_stash_clear(self) -> None:
        self.assertEqual(run_guard("git stash clear"), "ASK")

    def test_checkout_dash_dash_file(self) -> None:
        self.assertEqual(run_guard("git checkout -- src/main.py"), "ASK")


class DestructiveGitFlagVariantTests(unittest.TestCase):
    """Destructive git commands with global flags should still ask."""

    def test_C_push(self) -> None:
        self.assertEqual(run_guard("git -C papers/repo push origin main"), "ASK")

    def test_C_commit(self) -> None:
        self.assertEqual(run_guard('git -C papers/repo commit -m "msg"'), "ASK")

    def test_C_merge(self) -> None:
        self.assertEqual(run_guard("git -C papers/repo merge origin/main"), "ASK")

    def test_C_branch_D(self) -> None:
        self.assertEqual(run_guard("git -C papers/repo branch -D feature"), "ASK")

    def test_C_checkout_dash_dash(self) -> None:
        self.assertEqual(run_guard("git -C papers/repo checkout -- file.py"), "ASK")

    def test_C_tag_d(self) -> None:
        self.assertEqual(run_guard("git -C papers/repo tag -d v1.0"), "ASK")

    def test_c_config_push(self) -> None:
        self.assertEqual(run_guard("git -c color.ui=always push origin main"), "ASK")

    def test_C_quoted_path_push(self) -> None:
        self.assertEqual(run_guard('git -C "repo with space" push origin main'), "ASK")

    def test_exec_path_push(self) -> None:
        self.assertEqual(run_guard("git --exec-path /tmp push origin main"), "ASK")

    def test_git_dir_push(self) -> None:
        self.assertEqual(run_guard("git --git-dir /tmp/.git push origin main"), "ASK")

    def test_work_tree_push(self) -> None:
        self.assertEqual(run_guard("git --work-tree /tmp push origin main"), "ASK")


class DestructiveGitWrapperTests(unittest.TestCase):
    """Destructive git commands with env/var wrappers should still ask."""

    def test_env_var_push(self) -> None:
        self.assertEqual(run_guard("env FOO=1 git push origin main"), "ASK")

    def test_inline_var_push(self) -> None:
        self.assertEqual(run_guard("FOO=1 git push origin main"), "ASK")

    def test_env_unset_commit(self) -> None:
        self.assertEqual(run_guard("env -u VAR git commit -m msg"), "ASK")

    def test_multi_var_push(self) -> None:
        self.assertEqual(run_guard("A=1 B=2 git push origin main"), "ASK")


class DestructiveGhTests(unittest.TestCase):
    """Destructive gh commands should ask."""

    def test_pr_create(self) -> None:
        self.assertEqual(run_guard('gh pr create --title "fix"'), "ASK")

    def test_pr_merge(self) -> None:
        self.assertEqual(run_guard("gh pr merge 42"), "ASK")

    def test_pr_close(self) -> None:
        self.assertEqual(run_guard("gh pr close 42"), "ASK")

    def test_repo_delete(self) -> None:
        self.assertEqual(run_guard("gh repo delete owner/repo"), "ASK")

    def test_R_pr_create(self) -> None:
        self.assertEqual(run_guard("gh -R owner/repo pr create"), "ASK")

    def test_R_pr_merge(self) -> None:
        self.assertEqual(run_guard("gh -R owner/repo pr merge 42"), "ASK")

    def test_repo_pr_create(self) -> None:
        self.assertEqual(run_guard("gh --repo owner/repo pr create"), "ASK")

    def test_pr_R_create(self) -> None:
        self.assertEqual(run_guard("gh pr -R owner/repo create --title x"), "ASK")

    def test_repo_R_delete(self) -> None:
        self.assertEqual(run_guard("gh repo -R owner/repo delete"), "ASK")


class BypassRegressionTests(unittest.TestCase):
    """Branch names containing safe-looking substrings should still ask."""

    def test_merge_feature_merge_base_fix(self) -> None:
        self.assertEqual(run_guard("git merge feature/merge-base-fix"), "ASK")

    def test_rebase_topic_commit_graph(self) -> None:
        self.assertEqual(run_guard("git rebase topic/commit-graph-cleanup"), "ASK")


class SafeCommandTests(unittest.TestCase):
    """Safe commands should pass through without intervention."""

    def test_pdflatex(self) -> None:
        self.assertEqual(run_guard("pdflatex main.tex"), "PASSED")

    def test_bibtex(self) -> None:
        self.assertEqual(run_guard("bibtex main"), "PASSED")

    def test_latexmk(self) -> None:
        self.assertEqual(run_guard("latexmk -pdf main.tex"), "PASSED")

    def test_echo_with_cd_text(self) -> None:
        self.assertEqual(run_guard('echo "cd repo && make"'), "PASSED")

    def test_grep(self) -> None:
        self.assertEqual(run_guard("grep -r pattern src/"), "PASSED")

    def test_ls(self) -> None:
        self.assertEqual(run_guard("ls -la"), "PASSED")

    def test_python(self) -> None:
        self.assertEqual(run_guard("python script.py"), "PASSED")

    def test_git_status(self) -> None:
        self.assertEqual(run_guard("git status"), "PASSED")

    def test_git_log(self) -> None:
        self.assertEqual(run_guard("git log --oneline -5"), "PASSED")

    def test_git_diff(self) -> None:
        self.assertEqual(run_guard("git diff HEAD"), "PASSED")

    def test_git_branch_list(self) -> None:
        self.assertEqual(run_guard("git branch"), "PASSED")

    def test_git_branch_v(self) -> None:
        self.assertEqual(run_guard("git branch -v"), "PASSED")

    def test_git_tag_list(self) -> None:
        self.assertEqual(run_guard("git tag --list"), "PASSED")

    def test_git_stash_list(self) -> None:
        self.assertEqual(run_guard("git stash list"), "PASSED")

    def test_git_show(self) -> None:
        self.assertEqual(run_guard("git show HEAD"), "PASSED")

    def test_git_fetch(self) -> None:
        self.assertEqual(run_guard("git fetch origin"), "PASSED")

    def test_git_pull(self) -> None:
        self.assertEqual(run_guard("git pull origin main"), "PASSED")

    def test_git_merge_base(self) -> None:
        self.assertEqual(run_guard("git merge-base HEAD origin/main"), "PASSED")

    def test_git_show_branch_d(self) -> None:
        self.assertEqual(run_guard("git show-branch -d"), "PASSED")

    def test_git_commit_tree(self) -> None:
        self.assertEqual(run_guard("git commit-tree abc123"), "PASSED")

    def test_git_commit_graph(self) -> None:
        self.assertEqual(run_guard("git commit-graph write"), "PASSED")

    def test_git_reset_soft(self) -> None:
        self.assertEqual(run_guard("git reset --soft HEAD~1"), "PASSED")

    def test_git_checkout_branch(self) -> None:
        self.assertEqual(run_guard("git checkout feature"), "PASSED")

    def test_git_tag_create(self) -> None:
        self.assertEqual(run_guard("git tag v1.0"), "PASSED")

    def test_git_C_status(self) -> None:
        self.assertEqual(run_guard("git -C papers/repo status"), "PASSED")

    def test_git_C_log(self) -> None:
        self.assertEqual(run_guard("git -C papers/repo log --oneline"), "PASSED")

    def test_git_C_diff(self) -> None:
        self.assertEqual(run_guard("git -C papers/repo diff"), "PASSED")

    def test_git_C_fetch(self) -> None:
        self.assertEqual(run_guard("git -C papers/repo fetch origin"), "PASSED")

    def test_git_notes_add(self) -> None:
        self.assertEqual(run_guard('git notes add -m "commit docs"'), "PASSED")

    def test_gh_pr_list(self) -> None:
        self.assertEqual(run_guard("gh pr list"), "PASSED")

    def test_gh_pr_view(self) -> None:
        self.assertEqual(run_guard("gh pr view 42"), "PASSED")

    def test_gh_pr_status(self) -> None:
        self.assertEqual(run_guard("gh pr status"), "PASSED")

    def test_gh_issue_list(self) -> None:
        self.assertEqual(run_guard("gh issue list"), "PASSED")

    def test_empty_command(self) -> None:
        self.assertEqual(run_guard(""), "PASSED")

    def test_cd_alone(self) -> None:
        self.assertEqual(run_guard("cd /tmp"), "PASSED")


class JsonPayloadTests(unittest.TestCase):
    """Verify the full JSON output structure, not just the decision string."""

    def _assert_valid_payload(self, command: str, expected_decision: str) -> None:
        data = run_guard_full(command)
        self.assertIsNotNone(data, f"Expected output for: {command}")
        self.assertIn("hookSpecificOutput", data)
        hook = data["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "PreToolUse")
        self.assertEqual(hook["permissionDecision"], expected_decision)
        self.assertIsInstance(hook["permissionDecisionReason"], str)
        self.assertTrue(len(hook["permissionDecisionReason"]) > 0)

    def test_git_commit_payload(self) -> None:
        self._assert_valid_payload('git commit -m "msg"', "ask")

    def test_git_push_payload(self) -> None:
        self._assert_valid_payload("git push origin main", "ask")

    def test_gh_pr_create_payload(self) -> None:
        self._assert_valid_payload("gh pr create --title t", "ask")

    def test_compound_cd_payload(self) -> None:
        self._assert_valid_payload("cd /tmp && ls", "deny")

    def test_safe_command_no_output(self) -> None:
        data = run_guard_full("git status")
        self.assertIsNone(data)


# --- 0.1.8 gates: writing-style + banner ---------------------------------

import os
import re
import shutil
import tempfile
import time


def run_guard_with_payload(payload, env=None, cwd=None):
    """Run guard.py with a full hook payload (includes tool_name) and optional
    env overrides. Returns parsed response JSON or None if no output.

    Scrubs AGENT_CONFIG_GATES from the inherited environment so that a
    developer shell or CI environment with the escape hatch set cannot
    silently disable the new gates during tests. Callers that want the
    escape-hatch on must pass it explicitly via env=.

    Pass cwd to run the subprocess inside a specific directory (so guard.py's
    os.getcwd() sees that directory, which lets _find_consumer_root resolve
    the per-project flag files for banner-gate tests)."""
    env_dict = dict(os.environ)
    env_dict.pop("AGENT_CONFIG_GATES", None)
    if env:
        env_dict.update(env)
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=env_dict,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"guard.py crashed (exit {result.returncode}): {result.stderr.strip()}"
        )
    stdout = result.stdout.strip()
    return json.loads(stdout) if stdout else None


def run_guard_with_payload_capture(payload, env=None, cwd=None):
    """Same as run_guard_with_payload but returns ``(response, stderr_text)``
    so tests can assert on observability lines emitted by the hook (e.g., the
    re-arm advisory notice). Stdout still encodes the deny/ask/allow decision;
    stderr is human-facing diagnostic output that does not affect the gate."""
    env_dict = dict(os.environ)
    env_dict.pop("AGENT_CONFIG_GATES", None)
    if env:
        env_dict.update(env)
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=env_dict,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"guard.py crashed (exit {result.returncode}): {result.stderr.strip()}"
        )
    stdout = result.stdout.strip()
    response = json.loads(stdout) if stdout else None
    return response, result.stderr


class WritingStyleGateTests(unittest.TestCase):
    def test_banned_word_in_markdown_denied(self):
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/notes.md", "content": "This result was pivotal."},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("pivotal", resp["hookSpecificOutput"]["permissionDecisionReason"])

    def test_banned_word_in_code_file_allowed(self):
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.py", "content": "# pivotal insight\npass"},
        })
        self.assertIsNone(resp)

    def test_banned_word_in_code_fence_allowed(self):
        content = "Regular prose.\n\n```python\nlabel = 'pivotal'\n```\n"
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": content},
        })
        self.assertIsNone(resp)

    def test_banned_word_in_inline_code_allowed(self):
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "The word `pivotal` is not allowed in prose."},
        })
        self.assertIsNone(resp)

    def test_close_variant_denied(self):
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "We delved into the issue."},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_hyphenated_banned_word_denied(self):
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "A game-changing result."},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_gates_disabled_via_env(self):
        resp = run_guard_with_payload(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/x.md", "content": "This was pivotal."},
            },
            env={"AGENT_CONFIG_GATES": "off"},
        )
        self.assertIsNone(resp)

    def test_edit_tool_new_string_scanned(self):
        resp = run_guard_with_payload({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/tmp/x.md",
                "old_string": "old text",
                "new_string": "new foster-based approach",
            },
        })
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_honest_is_not_banned_hone_variant(self):
        # Regression: earlier stem-match `\bhone\w*\b` caught "honest" as a
        # false positive. Finite-inflection matching must not.
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "This is an honest assessment."},
        })
        self.assertIsNone(resp)

    def test_pavement_is_not_banned_pave_variant(self):
        # Regression: "pavement" must not match the `pave` banned word.
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "The pavement is wet."},
        })
        self.assertIsNone(resp)

    def test_faceted_is_not_banned_facet_variant(self):
        # Regression: technical writing about "faceted search UI" must pass.
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "A faceted search UI."},
        })
        self.assertIsNone(resp)

    def test_honed_still_denied_as_hone_variant(self):
        # Positive regression: the verb form "honed" must still deny.
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "We honed the prompt."},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_paving_still_denied_as_pave_variant(self):
        # Positive regression: the verb form "paving" must still deny.
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "Paving the way forward."},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_doubled_consonant_underpinned_denied(self):
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "The method underpinned the result."},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_burgeoned_denied_as_burgeoning_variant(self):
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "The field burgeoned rapidly."},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_adverb_monumentally_denied(self):
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "The result was monumentally different."},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_adverb_profoundly_denied(self):
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "This changed the result profoundly."},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_embargo_is_not_banned_embark_variant(self):
        # Negative regression: "embargo" must not match "embark".
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "An embargo was placed."},
        })
        self.assertIsNone(resp)


class BannerGateTests(unittest.TestCase):
    """Per-project flag-file behavior. Each test sets up a fake consumer repo
    in a temp dir, runs guard.py with cwd=<consumer>, and writes/reads
    session-event.json / banner-emitted.json under <consumer>/.agent-config/.
    HOME/USERPROFILE are overridden to a separate temp dir so the legacy-flag
    cleanup cannot touch the developer's real ~/.claude/hooks/*.json.
    """

    def setUp(self):
        self.tmp_home = tempfile.mkdtemp(prefix="guard-banner-home-")
        self.tmp_project = tempfile.mkdtemp(prefix="guard-banner-proj-")
        self.agent_dir = Path(self.tmp_project) / ".agent-config"
        self.agent_dir.mkdir(parents=True)
        # Marker so _find_consumer_root treats this as a consumer repo.
        (self.agent_dir / "bootstrap.sh").write_text("# marker\n")
        self.env = {"HOME": self.tmp_home, "USERPROFILE": self.tmp_home}

    def tearDown(self):
        shutil.rmtree(self.tmp_home, ignore_errors=True)
        shutil.rmtree(self.tmp_project, ignore_errors=True)

    def _run(self, payload, extra_env=None, cwd=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return run_guard_with_payload(
            payload, env=env, cwd=cwd or self.tmp_project
        )

    def _run_capture(self, payload, extra_env=None, cwd=None):
        """Same as ``_run`` but returns ``(response, stderr_text)`` so a test
        can assert on the re-arm advisory line (``[banner-gate]
        SessionStart re-fire detected ...``) emitted by ``guard.py`` on the
        re-arm path. Without this, stderr is discarded and a future edit
        that silently removes the advisory notice would still pass."""
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return run_guard_with_payload_capture(
            payload, env=env, cwd=cwd or self.tmp_project
        )

    def _write_event(self, ts):
        (self.agent_dir / "session-event.json").write_text(json.dumps({"ts": ts}))

    def _write_emitted(self, ts):
        (self.agent_dir / "banner-emitted.json").write_text(json.dumps({"ts": ts}))

    def test_denies_bash_when_event_pending(self):
        self._write_event(100)
        resp = self._run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("banner", resp["hookSpecificOutput"]["permissionDecisionReason"].lower())

    def test_allows_when_emitted_current(self):
        self._write_event(100)
        self._write_emitted(100)
        resp = self._run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertIsNone(resp)

    def test_allows_read_when_event_pending(self):
        self._write_event(100)
        resp = self._run({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}})
        self.assertIsNone(resp)

    def test_allows_skill_when_event_pending(self):
        self._write_event(100)
        resp = self._run({"tool_name": "Skill", "tool_input": {"skill": "implement-review"}})
        self.assertIsNone(resp)

    def test_allows_task_when_event_pending(self):
        self._write_event(100)
        resp = self._run({"tool_name": "Task", "tool_input": {"description": "x"}})
        self.assertIsNone(resp)

    def test_allows_todowrite_when_event_pending(self):
        self._write_event(100)
        resp = self._run({"tool_name": "TodoWrite", "tool_input": {"todos": []}})
        self.assertIsNone(resp)

    def test_allows_ls_when_event_pending(self):
        self._write_event(100)
        resp = self._run({"tool_name": "LS", "tool_input": {"path": "/tmp"}})
        self.assertIsNone(resp)

    def test_allows_notebookread_when_event_pending(self):
        self._write_event(100)
        resp = self._run({"tool_name": "NotebookRead", "tool_input": {"notebook_path": "/tmp/x.ipynb"}})
        self.assertIsNone(resp)

    def test_allows_write_to_exact_ack_path(self):
        self._write_event(100)
        ack_path = str(self.agent_dir / "banner-emitted.json")
        resp = self._run({
            "tool_name": "Write",
            "tool_input": {"file_path": ack_path, "content": json.dumps({"ts": 100})},
        })
        self.assertIsNone(resp)

    def test_denies_write_to_wrong_ack_path(self):
        # Path ends in .agent-config/banner-emitted.json but is OUTSIDE the
        # resolved consumer root. 0.1.9 exact-path comparison must deny.
        self._write_event(100)
        resp = self._run({
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/random/.agent-config/banner-emitted.json",
                "content": "{}",
            },
        })
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_denies_write_to_different_consumer_ack_path(self):
        # Consumer A pending event. Agent tries to Write consumer B's ack.
        # Must deny — exact-path check rejects any path outside A's root.
        self._write_event(100)
        other_project = tempfile.mkdtemp(prefix="guard-other-")
        try:
            other_agent = Path(other_project) / ".agent-config"
            other_agent.mkdir(parents=True)
            (other_agent / "bootstrap.sh").write_text("# marker\n")
            ack_path_b = str(other_agent / "banner-emitted.json")
            resp = self._run({
                "tool_name": "Write",
                "tool_input": {"file_path": ack_path_b, "content": "{}"},
            })
            self.assertIsNotNone(resp)
            self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")
        finally:
            shutil.rmtree(other_project, ignore_errors=True)

    def test_allows_when_event_file_missing(self):
        resp = self._run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertIsNone(resp)

    def test_bypassed_via_gates_off(self):
        self._write_event(100)
        resp = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            extra_env={"AGENT_CONFIG_GATES": "off"},
        )
        self.assertIsNone(resp)

    def test_legacy_payload_without_tool_name_falls_through(self):
        self._write_event(100)
        resp = self._run({"tool_input": {"command": "ls"}})
        self.assertIsNone(resp)

    def test_source_repo_skips_gate(self):
        # cwd has no .agent-config/ → _find_consumer_root returns None → gate
        # skipped regardless of any legacy or foreign state files.
        source_dir = tempfile.mkdtemp(prefix="guard-source-")
        try:
            resp = self._run(
                {"tool_name": "Bash", "tool_input": {"command": "ls"}},
                cwd=source_dir,
            )
            self.assertIsNone(resp)
        finally:
            shutil.rmtree(source_dir, ignore_errors=True)

    def test_per_project_isolation(self):
        # Two separate consumer dirs. Project A has a pending event; project B
        # does not. Each subprocess call with cwd=<proj> sees only its own
        # state, so a guard invocation in project B passes even though project
        # A still has a pending banner.
        self._write_event(100)
        other_project = tempfile.mkdtemp(prefix="guard-proj-b-")
        try:
            other_agent = Path(other_project) / ".agent-config"
            other_agent.mkdir(parents=True)
            (other_agent / "bootstrap.sh").write_text("# marker\n")
            resp = self._run(
                {"tool_name": "Bash", "tool_input": {"command": "ls"}},
                cwd=other_project,
            )
            self.assertIsNone(resp)
        finally:
            shutil.rmtree(other_project, ignore_errors=True)

    def test_walks_up_from_nested_cwd(self):
        # cwd is deep inside the consumer; walk-up must resolve the root.
        self._write_event(100)
        nested = Path(self.tmp_project) / "src" / "nested"
        nested.mkdir(parents=True)
        resp = self._run(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            cwd=str(nested),
        )
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_first_arm_denies_when_no_ack_file_exists(self):
        """No banner-emitted.json yet (first-time install / first session) -> deny.
        First arm is the only condition under which the banner gate denies; later
        SessionStart re-fires become advisory (issue anywhere-agents#7)."""
        self._write_event(time.time())
        # Do NOT create banner-emitted.json.
        resp = self._run({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        self.assertIsNotNone(resp)
        output = resp["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("Session banner not yet emitted", output["permissionDecisionReason"])

    def test_rearm_is_advisory_when_ack_file_exists_with_older_ts(self):
        """A subsequent SessionStart re-fire (banner-emitted.json exists with prior
        ts) must NOT deny the next gated call. First emission already happened in
        this consumer-root; further re-arms degrade to advisory and emit a
        stderr notice so an observer can still tell the gate fired."""
        older = time.time() - 60.0
        self._write_event(time.time())   # fresh event_ts
        self._write_emitted(older)        # ack file exists, just stale
        resp, stderr = self._run_capture(
            {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        )
        self.assertIsNone(resp, "re-arm must pass through; only first-arm denies")
        self.assertIn(
            "[banner-gate] SessionStart re-fire detected",
            stderr,
            "advisory notice must be emitted on stderr so removing it cannot regress silently",
        )

    def test_rearm_is_advisory_for_in_flight_skill_tool_call(self):
        """Realistic shape of the reported failure (issue anywhere-agents#7):
        after ``auto-watch`` fires DONE, the agent's next tool call (e.g.,
        reading the produced review file) lands while a SessionStart re-fire
        has just advanced event_ts above the ack ts. The gate must pass
        through, not deny.

        Note: the implement-review trusted PS helpers (``auto-watch.ps1``,
        ``health-check.ps1``, ``dispatch-codex.ps1``) are already auto-allowed
        by Check 0 regardless of banner state since v0.7.0; this test covers
        the broader case where the next tool call is NOT one of those
        trusted scripts, so the banner gate would otherwise apply.
        """
        older = time.time() - 5.0
        self._write_event(time.time())
        self._write_emitted(older)
        # The agent's continuation after auto-watch DONE: read the produced
        # Review-Codex.md to begin Phase 2 intake. A normal gated Bash call,
        # not in the implement-review trusted PS set.
        resp, stderr = self._run_capture({
            "tool_name": "Bash",
            "tool_input": {"command": "cat Review-Codex.md"},
        })
        self.assertIsNone(resp, "in-flight skill tool call must not be blocked by a re-arm")
        self.assertIn(
            "[banner-gate] SessionStart re-fire detected",
            stderr,
            "in-flight re-arm must still emit the advisory notice for visibility",
        )

    def test_corrupted_ack_file_is_advisory_when_file_exists(self):
        """Malformed banner-emitted.json parses as emitted_ts=0, but the ack
        file exists, so this is treated as a re-arm and passes through. This
        pins the current fail-open behavior explicitly; changing it to fail
        closed later should be a conscious contract change."""
        self._write_event(time.time())
        emitted_path = self.agent_dir / "banner-emitted.json"
        emitted_path.write_text("{not valid json", encoding="utf-8")
        resp, stderr = self._run_capture(
            {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        )
        # The file exists, so the os.path.exists branch keeps it on the
        # advisory path.
        self.assertIsNone(resp, "corrupted ack file with the file present is advisory (fail-open)")
        self.assertIn(
            "[banner-gate] SessionStart re-fire detected",
            stderr,
            "corrupted-ack re-arm must still emit the advisory notice",
        )


class ImplReviewPsAllowTests(unittest.TestCase):
    """The implement-review skill's shipped PowerShell helpers (auto-watch,
    health-check, dispatch-codex) should auto-allow on the PowerShell tool
    when invoked via the call-operator. Unrelated PowerShell calls fall
    through to the existing permission flow. Bash invocations referencing
    the same path tail are not affected because the auto-allow is keyed on
    tool_name.
    """

    def test_auto_watch_allowed_with_backslash(self):
        # Native Windows-style invocation: forward slash normalization in the
        # check should match path tails written with backslashes.
        cmd = (
            "& 'C:\\Users\\me\\PycharmProjects\\proj\\skills\\implement-review"
            "\\scripts\\auto-watch.ps1' 'Review-*.md' 1 'Codex'"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    def test_auto_watch_allowed_with_forward_slash(self):
        cmd = (
            "& '/home/user/proj/skills/implement-review/scripts/auto-watch.ps1' "
            "'Review-*.md' 1 'Codex'"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    def test_unrelated_powershell_command_falls_through(self):
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": "Get-Date"},
        })
        self.assertIsNone(resp)

    def test_other_auto_watch_path_not_allowed(self):
        # Path tail must match the implement-review skill's shipped script;
        # an arbitrary auto-watch.ps1 elsewhere on disk is not auto-allowed.
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": "& 'C:\\tmp\\auto-watch.ps1'"},
        })
        self.assertIsNone(resp)

    def test_bash_with_matching_path_falls_through(self):
        # Auto-allow is keyed on the PowerShell tool_name; a Bash invocation
        # that happens to mention the same path tail does not get the bypass.
        cmd = "bash skills/implement-review/scripts/auto-watch.ps1 args"
        resp = run_guard_with_payload({
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        })
        # Bash side falls through to the destructive-git/gh checks; this is
        # neither, so the response is no output (PASSED).
        self.assertIsNone(resp)

    def test_allow_works_with_gates_off(self):
        # AGENT_CONFIG_GATES disables deny-style gates only; allow is always-on.
        cmd = (
            "& 'C:\\proj\\skills\\implement-review\\scripts\\auto-watch.ps1'"
        )
        resp = run_guard_with_payload(
            {"tool_name": "PowerShell", "tool_input": {"command": cmd}},
            env={"AGENT_CONFIG_GATES": "off"},
        )
        self.assertIsNotNone(resp)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    def test_auto_watch_path_as_argument_does_not_allow(self):
        # The path tail must not auto-allow when it appears only as a string
        # argument under a different verb. Round-1 review caught this as the
        # security flaw of an over-broad substring match: any PowerShell
        # command mentioning the watcher path would have been allowed.
        cmd = (
            "Write-Output "
            "'C:\\proj\\skills\\implement-review\\scripts\\auto-watch.ps1'"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    def test_auto_watch_allowed_with_repo_local_relative_path(self):
        # Realistic invocation shape from the source repo or project-local
        # working directory: the skill's lookup order names the script with
        # no leading prefix (`skills/implement-review/scripts/auto-watch.ps1`).
        # The exact-tail branch in `_is_auto_watch_script_path` covers this.
        cmd = (
            "& 'skills\\implement-review\\scripts\\auto-watch.ps1' "
            "'Review-*.md' 1 'Codex'"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    def test_auto_watch_allowed_with_quoted_path_containing_spaces(self):
        # Realistic Windows path with spaces inside the quoted argument. The
        # call-operator regex must capture the full quoted path including
        # spaces and accept the trailing watcher args.
        cmd = (
            "& 'C:\\Users\\me\\Project With Spaces\\skills\\implement-review"
            "\\scripts\\auto-watch.ps1' 'Review-*.md' 1 'Codex'"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    def test_payload_shape_is_valid(self):
        cmd = (
            "& 'C:\\proj\\skills\\implement-review\\scripts\\auto-watch.ps1'"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNotNone(resp)
        hook = resp["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "PreToolUse")
        self.assertEqual(hook["permissionDecision"], "allow")
        self.assertIsInstance(hook["permissionDecisionReason"], str)
        self.assertTrue(len(hook["permissionDecisionReason"]) > 0)

    # --- health-check.ps1 (Phase 2.0) ---

    def test_health_check_allowed_with_state_dir_args(self):
        # Realistic Phase 2.0 invocation shape captured from a live session:
        # `& '<path>\health-check.ps1' --state-dir '<tmp>...' --round 1
        # --review-file Review-Codex.md`. Without auto-allow this triggers a
        # manual approval prompt every review round.
        cmd = (
            "& 'C:\\Users\\me\\proj\\.claude\\skills\\implement-review"
            "\\scripts\\health-check.ps1' --state-dir 'C:\\Users\\me"
            "\\AppData\\Local\\Temp\\implement-review-codex-deadbeef-"
            "round1-9268-fefc3103aff76c0a' --round 1 --review-file "
            "Review-Codex.md"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    def test_health_check_unrelated_path_not_allowed(self):
        # Arbitrary health-check.ps1 elsewhere on disk does NOT auto-allow,
        # mirroring the security guard for auto-watch.
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": "& 'C:\\tmp\\health-check.ps1'"},
        })
        self.assertIsNone(resp)

    def test_health_check_reason_names_leaf_script(self):
        # The allow reason should name which leaf script matched so users
        # debugging a surprise auto-allow can grep for it.
        cmd = (
            "& 'C:\\proj\\.claude\\skills\\implement-review\\scripts"
            "\\health-check.ps1' --state-dir 'C:\\tmp\\x' --round 1"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNotNone(resp)
        reason = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("health-check.ps1", reason)

    # --- dispatch-codex.ps1 (Phase 1c) ---
    # Normally invoked via Bash tool / pwsh -File, but cover the call-op
    # shape for defense in depth in case a future flow uses it directly.

    def test_dispatch_codex_allowed_via_call_operator(self):
        cmd = (
            "& 'C:\\proj\\.claude\\skills\\implement-review\\scripts"
            "\\dispatch-codex.ps1' --prompt-file 'C:\\tmp\\p.txt' "
            "--round 1 --expected-review-file Review-Codex.md"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    # --- $env: prefix variants (real-world invocation shapes) ---

    def test_dispatch_codex_with_env_prefix_allowed(self):
        # Captured live: the skill sets $env:CODEX_BIN before dispatching
        # to dodge the npm `codex` vs `codex.cmd` PATH collision on
        # Windows. Without this branch the prefix breaks the leading-`&`
        # anchor and forces a manual approval every retry.
        cmd = (
            "$env:CODEX_BIN = 'codex.cmd'; & 'C:\\Users\\me"
            "\\PycharmProjects\\random\\.claude\\skills\\implement-review"
            "\\scripts\\dispatch-codex.ps1' --prompt-file 'C:\\Users\\me"
            "\\AppData\\Local\\Temp\\fhr-review-prompt-v2.txt' "
            "--round 1 --expected-review-file Review-Codex.md"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    def test_health_check_with_env_prefix_allowed(self):
        # Parity with dispatch: setting STALL_THRESHOLD_SECONDS or any
        # other env before health-check should also pass through.
        cmd = (
            "$env:STALL_THRESHOLD_SECONDS = '2'; "
            "& 'C:\\proj\\.claude\\skills\\implement-review"
            "\\scripts\\health-check.ps1' --state-dir 'C:\\tmp\\x' "
            "--round 1"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    def test_multiple_env_assignments_allowed(self):
        # Two env vars chained before the call operator must still
        # match. This guards against future invocation shapes that set
        # both CODEX_BIN and STALL_POLL_INTERVAL_SECONDS at once.
        cmd = (
            "$env:CODEX_BIN = 'codex.cmd'; "
            "$env:STALL_POLL_INTERVAL_SECONDS = '30'; "
            "& 'C:\\proj\\.claude\\skills\\implement-review"
            "\\scripts\\dispatch-codex.ps1' --prompt-file 'C:\\tmp\\p'"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNotNone(resp)
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    def test_env_prefix_with_untrusted_path_not_allowed(self):
        # Security: the env-var prefix must not let a path tail outside
        # the trusted set sneak through.
        cmd = (
            "$env:CODEX_BIN = 'malicious.exe'; "
            "& 'C:\\tmp\\dispatch-codex.ps1' --prompt-file 'C:\\tmp\\p'"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    # --- Round 1 regression: 4 guard-bypass patterns Codex flagged High ---
    # Each of these passed the old single-flat-`\S+` env value + leading-`&`
    # anchor + `(?=$|\s)` lookahead, smuggling arbitrary PowerShell past
    # the auto-allow. The new safe-token grammar with `fullmatch` rejects
    # them all; the tests below freeze that behavior so a future regex
    # weakening cannot silently regress.

    def test_bare_env_value_with_semicolon_not_allowed(self):
        """Bare-value semicolon: `$env:X = foo;bar; & '<trusted>'`.

        Old regex's bare value arm was `\\S+`, which matched `foo` and then
        `\\s*;\\s*` consumed the `;`, leaving `bar; ` as a second statement
        before the trusted call. PowerShell would execute `bar` as a bare
        command between the env assignment and the trusted script -- a
        full PS-execution channel through the auto-allow.
        """
        cmd = (
            "$env:X = foo;bar; & 'C:\\proj\\.claude\\skills\\implement-review"
            "\\scripts\\health-check.ps1' --state-dir 'C:\\tmp\\x' --round 1"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    def test_double_quoted_dollar_subexpression_not_allowed(self):
        """Double-quoted `$()`: `$env:X = "$(Write-Output evil)"; & '<trusted>'`.

        Old regex's double-quoted arm was `"[^"]*"`, accepting any chars
        inside the quotes. PowerShell expands `$()` inside double-quoted
        strings at assignment time, so `Write-Output evil` (or anything)
        ran before the trusted call.
        """
        cmd = (
            "$env:X = \"$(Write-Output evil)\"; & 'C:\\proj\\.claude"
            "\\skills\\implement-review\\scripts\\dispatch-codex.ps1' "
            "--prompt-file 'C:\\tmp\\p'"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    def test_post_script_semicolon_command_not_allowed(self):
        """Trailing `; <cmd>`: `& '<trusted>' ; Write-Output evil`.

        Old regex used a `(?=$|\\s)` lookahead after the script path and
        no end anchor, so any trailing statements after a space were
        accepted as long as the prefix matched. The new grammar requires
        every trailing arg to match a safe token (`;` is excluded),
        and `\\s*$` anchors the whole match.
        """
        cmd = (
            "& 'C:\\proj\\.claude\\skills\\implement-review"
            "\\scripts\\auto-watch.ps1' ; Write-Output evil"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    def test_splatting_after_script_not_allowed(self):
        """Splatting: `& '<trusted>' @params`.

        Old regex's bare-path / trailing-args used `\\S+`, which matches
        `@params`. PowerShell splatting expands `@params` into args from
        a hashtable / array, opening an indirection channel that a
        trusted-allow should not honor. The new grammar excludes `@` from
        bare-safe tokens.
        """
        cmd = (
            "& 'C:\\proj\\.claude\\skills\\implement-review"
            "\\scripts\\health-check.ps1' @params"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    # --- Round 2 regression: script-path-as-expansion + redirection ----
    # The Round 1 grammar still captured the script path with
    # `[^'"]+`, leaving two bypass classes open: (a) PS expansion inside
    # double-quoted paths (`$()` subexpressions, `$env:VAR` lookups), and
    # (b) redirection tokens (`>`, `2>`, `<`) accepted as bare args. Round
    # 2's regex restricts the double-quoted path to the same safe-char
    # set as env values and excludes `<>` from bare-safe.

    def test_double_quoted_subexpression_in_script_path_not_allowed(self):
        """`& "$(Write-Output evil)<trusted-tail>"`: PS expands $() at
        call resolution time. The literal string ends with a trusted
        tail, so the old tail-check approved it.
        """
        cmd = (
            '& "$(Write-Output evil)C:\\proj\\.claude\\skills'
            '\\implement-review\\scripts\\health-check.ps1" '
            "--state-dir 'C:\\tmp\\x' --round 1"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    def test_double_quoted_env_var_in_script_path_not_allowed(self):
        """`& "$env:VAR\\<trusted-tail>"`: attacker pre-sets $env:VAR
        before the call (env-prefix is auto-allowed). At runtime PS
        resolves the variable, redirecting the call to a file the
        attacker controls.
        """
        cmd = (
            "$env:X = 'C:\\evil'; "
            '& "$env:X\\skills\\implement-review\\scripts\\dispatch-codex.ps1" '
            "--prompt-file 'C:\\tmp\\p'"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    def test_stdout_redirect_after_script_not_allowed(self):
        """`& '<trusted>' > file`: redirection token in args.

        Old bare-safe set permitted `>` as a token, so dispatch's stdout
        could be redirected to attacker-controlled paths.
        """
        cmd = (
            "& 'C:\\proj\\.claude\\skills\\implement-review"
            "\\scripts\\health-check.ps1' > C:\\tmp\\out.txt"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    def test_stderr_redirect_after_script_not_allowed(self):
        """`& '<trusted>' 2> file`: stderr redirection."""
        cmd = (
            "& 'C:\\proj\\.claude\\skills\\implement-review"
            "\\scripts\\health-check.ps1' 2> C:\\tmp\\err.txt"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    def test_stdin_redirect_after_script_not_allowed(self):
        """`& '<trusted>' < file`: stdin redirection."""
        cmd = (
            "& 'C:\\proj\\.claude\\skills\\implement-review"
            "\\scripts\\health-check.ps1' --x < C:\\tmp\\in.txt"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    # --- Round 3 regression: PowerShell statement-break via newline ----
    # Python's `\s` character class matches `\r` and `\n`, but PowerShell
    # treats those as statement separators. A grammar using `\s*` between
    # tokens reads a newline as ordinary whitespace and accepts a second
    # statement as a "trailing arg". Round 4 fix: restrict whitespace
    # classes in the regex to [ \t] only, anchor with \A/\Z (not ^/$),
    # and strip only " \t" from the input so CR/LF in the original
    # command survives to break the match.

    def test_lf_after_script_not_allowed(self):
        """`& '<trusted>'\\nWrite-Output evil`: LF starts a new PS statement."""
        cmd = (
            "& 'C:\\proj\\.claude\\skills\\implement-review"
            "\\scripts\\health-check.ps1'\nWrite-Output evil"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    def test_crlf_after_script_not_allowed(self):
        """`& '<trusted>'\\r\\nWrite-Output evil`: CRLF also starts a new statement."""
        cmd = (
            "& 'C:\\proj\\.claude\\skills\\implement-review"
            "\\scripts\\health-check.ps1'\r\nWrite-Output evil"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)

    def test_lf_after_env_and_script_not_allowed(self):
        """env + script + LF + statement: realistic shape an attacker
        might paste after an auto-allowed dispatch invocation."""
        cmd = (
            "$env:X = 'y'; & 'C:\\proj\\.claude\\skills\\implement-review"
            "\\scripts\\health-check.ps1'\nWrite-Output evil"
        )
        resp = run_guard_with_payload({
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
        })
        self.assertIsNone(resp)



# --- v0.7.0 noise-audit: Suggested rewrite + per-guard escape envs --------


def _run_with_envs(payload, envs):
    """Helper: run guard.py with a dict of envs set (each value = "off"). All
    AGENT_*_HOOK and AGENT_CONFIG_GATES vars are first scrubbed from the
    inherited environment so the test environment is deterministic."""
    env_dict = dict(os.environ)
    for name in (
        "AGENT_CONFIG_GATES",
        "AGENT_STYLE_HOOK",
        "AGENT_COMPOUND_CD_HOOK",
        "AGENT_DESTRUCTIVE_HOOK",
    ):
        env_dict.pop(name, None)
    env_dict.update(envs)
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=env_dict,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"guard.py crashed (exit {result.returncode}): {result.stderr.strip()}"
        )
    stdout = result.stdout.strip()
    return json.loads(stdout) if stdout else None


class SuggestedRewriteInDenyMessageTests(unittest.TestCase):
    """v0.7.0 finding A: deny messages must embed an inline `Suggested
    rewrite:` line so autonomous agents can lift the reroute in one model
    turn instead of inferring it."""

    def test_writing_style_deny_contains_suggested_rewrite(self) -> None:
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "This was pivotal."},
        })
        self.assertIsNotNone(resp)
        reason = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Suggested rewrite:", reason)
        # Concrete alternative for the specific banned word must appear
        self.assertIn("pivotal", reason)
        self.assertIn("key", reason)  # from "key, central" reroute entry

    def test_writing_style_unknown_word_falls_back_to_generic(self) -> None:
        # All currently banned words have explicit reroute entries. This
        # test guards the fallback path: if a future entry is removed from
        # _BANNED_WORD_REROUTES, the generic phrasing keeps the message
        # parseable as a Suggested rewrite. We synthesize the case by
        # using a banned word that the reroute table covers, but the test
        # verifies the message shape regardless.
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.md", "content": "We must delve into this."},
        })
        self.assertIsNotNone(resp)
        reason = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Suggested rewrite:", reason)
        self.assertIn("delve", reason)

    def test_compound_cd_deny_contains_suggested_rewrite(self) -> None:
        resp = run_guard_with_payload({
            "tool_input": {"command": "cd /tmp && ls"},
        })
        self.assertIsNotNone(resp)
        reason = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Suggested rewrite:", reason)
        self.assertIn("/tmp", reason)
        self.assertIn("ls", reason)

    def test_compound_cd_git_followup_suggests_git_dash_C(self) -> None:
        resp = run_guard_with_payload({
            "tool_input": {"command": "cd papers/foo && git status"},
        })
        self.assertIsNotNone(resp)
        reason = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Suggested rewrite:", reason)
        # Reroute should name git -C specifically when the chained command is git
        self.assertIn("git -C papers/foo", reason)

    def test_compound_cd_or_handler_does_not_suggest_running_handler_in_dir(self) -> None:
        # Round 1 review finding M2: `cd repo || exit 1` is a failure handler
        # construct; the follow-up only runs when cd FAILS. The suggested
        # rewrite must NOT propose running the handler inside the directory.
        resp = run_guard_with_payload({
            "tool_input": {"command": "cd repo || exit 1"},
        })
        self.assertIsNotNone(resp)
        reason = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Suggested rewrite:", reason)
        # Negative invariant: the rewrite must NOT say "run `exit 1` from `repo`".
        self.assertNotIn("run `exit 1` from", reason)
        # Positive: should suggest splitting / handling the failure path
        # separately, or otherwise not pretending the handler is the reroute.
        self.assertIn("split", reason.lower())
        # Path still surfaces for context (so the user / agent recognizes
        # which cd target tripped the gate).
        self.assertIn("repo", reason)

    def test_compound_cd_semicolon_followup_acts_like_and(self) -> None:
        # `;` and `&&` both sequence the follow-up into the cd'd dir, so the
        # rewrite is the same shape as the `&&` case.
        resp = run_guard_with_payload({
            "tool_input": {"command": "cd /tmp ; ls"},
        })
        self.assertIsNotNone(resp)
        reason = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Suggested rewrite:", reason)
        self.assertIn("/tmp", reason)
        self.assertIn("ls", reason)


class CompoundCdQuoteAwareTests(unittest.TestCase):
    """Round 2 review M2-reopen: the compound-cd detector + Suggested
    rewrite builder must be quote-aware. Operators inside ``'...'`` /
    ``"..."`` / escaped via ``\\<char>`` are literal path content, not
    shell control operators.
    """

    def test_cd_to_quoted_path_with_or_operator_is_not_compound(self) -> None:
        # `cd "a || b"` cd's to a directory literally named ``a || b``.
        # This is a SINGLE cd command, not a compound. The hook must not
        # deny it.
        resp = run_guard_with_payload({
            "tool_input": {"command": 'cd "a || b"'},
        })
        self.assertIsNone(resp)

    def test_cd_to_quoted_path_with_and_operator_is_not_compound(self) -> None:
        resp = run_guard_with_payload({
            "tool_input": {"command": 'cd "a && b"'},
        })
        self.assertIsNone(resp)

    def test_cd_to_quoted_path_with_semicolon_is_not_compound(self) -> None:
        resp = run_guard_with_payload({
            "tool_input": {"command": 'cd "a ; b"'},
        })
        self.assertIsNone(resp)

    def test_cd_to_single_quoted_path_with_operators_is_not_compound(self) -> None:
        # Single quotes also protect operator chars from being parsed.
        resp = run_guard_with_payload({
            "tool_input": {"command": "cd 'a && b'"},
        })
        self.assertIsNone(resp)

    def test_cd_with_quoted_path_then_real_followup_is_compound(self) -> None:
        # `cd "with spaces" && ls` IS a compound; the path is quoted but
        # the operator after it is unquoted. Detector must catch the real
        # operator while preserving the quoted path.
        resp = run_guard_with_payload({
            "tool_input": {"command": 'cd "with spaces" && ls'},
        })
        self.assertIsNotNone(resp)
        reason = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Suggested rewrite:", reason)
        self.assertIn("with spaces", reason)
        self.assertIn("ls", reason)

    def test_mixed_or_and_after_cd_uses_split_into_steps_message(self) -> None:
        # `cd /tmp || echo nope && ls` mixes failure (``||``) and success
        # (``&&``) operators after cd. No single-line rewrite captures
        # user intent — the suggestion must explicitly say to split.
        resp = run_guard_with_payload({
            "tool_input": {"command": "cd /tmp || echo nope && ls"},
        })
        self.assertIsNotNone(resp)
        reason = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Suggested rewrite:", reason)
        self.assertIn("split", reason.lower())
        # The suggestion should NOT pretend `echo nope` is the reroute target
        # (that was the Round 2 reopen — separator-only branch ignored the
        # later && ls).
        self.assertNotIn("run `echo nope`", reason)


class PerGuardEscapeEnvTests(unittest.TestCase):
    """v0.7.0 finding A: narrowly-scoped per-guard escape envs. Each env
    disables only its target guard; destructive git/gh stay always-on."""

    def test_style_hook_off_disables_writing_style(self) -> None:
        resp = _run_with_envs(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/x.md", "content": "This was pivotal."},
            },
            envs={"AGENT_STYLE_HOOK": "off"},
        )
        self.assertIsNone(resp)

    def test_compound_cd_hook_off_disables_compound_cd(self) -> None:
        resp = _run_with_envs(
            {"tool_input": {"command": "cd /tmp && ls"}},
            envs={"AGENT_COMPOUND_CD_HOOK": "off"},
        )
        self.assertIsNone(resp)

    def test_style_hook_off_does_not_disable_compound_cd(self) -> None:
        resp = _run_with_envs(
            {"tool_input": {"command": "cd /tmp && ls"}},
            envs={"AGENT_STYLE_HOOK": "off"},
        )
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_compound_cd_hook_off_does_not_disable_writing_style(self) -> None:
        resp = _run_with_envs(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/x.md", "content": "This was pivotal."},
            },
            envs={"AGENT_COMPOUND_CD_HOOK": "off"},
        )
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_legacy_gates_off_disables_writing_style_unchanged(self) -> None:
        # BC: AGENT_CONFIG_GATES=off keeps its legacy scope (writing-style + banner).
        resp = _run_with_envs(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/x.md", "content": "This was pivotal."},
            },
            envs={"AGENT_CONFIG_GATES": "off"},
        )
        self.assertIsNone(resp)

    def test_legacy_gates_off_does_not_disable_compound_cd(self) -> None:
        # AGENT_CONFIG_GATES scope is writing-style + banner ONLY (not compound-cd).
        resp = _run_with_envs(
            {"tool_input": {"command": "cd /tmp && ls"}},
            envs={"AGENT_CONFIG_GATES": "off"},
        )
        self.assertIsNotNone(resp)
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")


class DestructiveNonBypassTests(unittest.TestCase):
    """v0.7.0 Round 1 finding 1 + Round 2 finding 5: NO escape env may turn
    destructive git/gh `ask` checks into pass-through. Parametrized over the
    advertised `_ESCAPE_HATCH_ENV_NAMES` constant in guard.py so future env
    additions automatically extend this negative-test surface."""

    DESTRUCTIVE_COMMANDS = [
        'git commit -m "test"',
        "git push origin main",
        "git reset --hard HEAD~1",
        "git merge feature",
        "git rebase main",
        "gh pr merge 123",
        "gh repo delete owner/repo",
    ]

    def _import_constant(self):
        """Read `_ESCAPE_HATCH_ENV_NAMES` from guard.py source (not via
        import — guard.py runs as a script with side effects). Returns the
        tuple of advertised env names.

        Round 1 review L1: regex must be quote-agnostic (single OR double
        quotes) and accept digits in env names so a future entry like
        ``"AGENT_HOOK_2"`` is captured.
        """
        src = GUARD.read_text(encoding="utf-8")
        match = re.search(
            r"_ESCAPE_HATCH_ENV_NAMES\s*=\s*\((.*?)\)", src, re.DOTALL
        )
        if not match:
            raise AssertionError(
                "Could not locate _ESCAPE_HATCH_ENV_NAMES in guard.py"
            )
        body = match.group(1)
        # Quote-agnostic + digit-tolerant.
        return tuple(re.findall(r"""['"]([A-Z0-9_]+)['"]""", body))

    def test_every_advertised_env_keeps_destructive_as_ask(self) -> None:
        envs = self._import_constant()
        self.assertTrue(envs, "constant must be non-empty")
        for env_name in envs:
            for cmd in self.DESTRUCTIVE_COMMANDS:
                resp = _run_with_envs(
                    {"tool_input": {"command": cmd}},
                    envs={env_name: "off"},
                )
                self.assertIsNotNone(
                    resp,
                    f"destructive `{cmd}` MUST surface even with {env_name}=off",
                )
                self.assertEqual(
                    resp["hookSpecificOutput"]["permissionDecision"],
                    "ask",
                    f"destructive `{cmd}` must stay 'ask' with {env_name}=off",
                )

    def test_all_envs_set_together_still_keep_destructive_as_ask(self) -> None:
        envs = self._import_constant()
        all_off = {name: "off" for name in envs}
        for cmd in self.DESTRUCTIVE_COMMANDS:
            resp = _run_with_envs(
                {"tool_input": {"command": cmd}}, envs=all_off
            )
            self.assertIsNotNone(
                resp,
                f"destructive `{cmd}` MUST surface with all escape envs set",
            )
            self.assertEqual(
                resp["hookSpecificOutput"]["permissionDecision"], "ask"
            )

    def test_unknown_env_var_is_fail_closed(self) -> None:
        # Smoke: an unrecognized AGENT_*_HOOK name has no effect — destructive
        # git/gh stay 'ask'. Confirms that the per-guard env mechanism does
        # not silently honor names outside the advertised constant.
        for cmd in self.DESTRUCTIVE_COMMANDS:
            resp = _run_with_envs(
                {"tool_input": {"command": cmd}},
                envs={"AGENT_DESTRUCTIVE_HOOK": "off"},
            )
            self.assertIsNotNone(resp)
            self.assertEqual(
                resp["hookSpecificOutput"]["permissionDecision"], "ask"
            )


class StaticEnvLiteralScanTests(unittest.TestCase):
    """v0.7.0 Round 4 finding R3 #4: forbid any `AGENT_[A-Z0-9_]*_HOOK`
    string literal in guard.py outside the `_ESCAPE_HATCH_ENV_NAMES`
    constant. Catches future code that adds a hook env via any access
    spelling (`os.environ.get`, `os.getenv`, `os.environ[...]`, helper
    wrappers) because every spelling eventually passes a string literal.

    Allowed locations: the constant definition itself, comments, and
    docstrings. The scan operates on raw source text (string literals
    only) — names mentioned in comments / docstrings are excluded by
    line-prefix and section heuristics that match the documented form.
    """

    # Round 1 review L1: quote-agnostic so single-quoted literals
    # (`'AGENT_FOO_HOOK'`) are also caught. A future code path that uses
    # any string-spelling — os.environ.get, os.getenv, os.environ[...],
    # helper wrappers — ultimately passes a string literal, so this
    # one regex covers all access shapes.
    HOOK_NAME_RE = re.compile(r"""['"](AGENT_[A-Z0-9_]*_HOOK)['"]""")

    def _extract_constant_block(self, src: str) -> str:
        """Return the raw text of the _ESCAPE_HATCH_ENV_NAMES = (...) tuple."""
        match = re.search(
            r"_ESCAPE_HATCH_ENV_NAMES\s*=\s*\((.*?)\)", src, re.DOTALL
        )
        if not match:
            raise AssertionError(
                "_ESCAPE_HATCH_ENV_NAMES constant not found in guard.py"
            )
        return match.group(0)

    def test_no_hook_env_literal_outside_constant(self) -> None:
        src = GUARD.read_text(encoding="utf-8")
        constant_block = self._extract_constant_block(src)
        constant_names = set(self.HOOK_NAME_RE.findall(constant_block))
        # Names allowed to appear elsewhere are exactly the ones in the
        # constant (their use in os.environ.get() / docstrings is what
        # the constant exists to enumerate).
        outside = src.replace(constant_block, "")
        # Strip Python triple-quoted strings AND single-line comments so
        # docstrings and inline notes that quote AGENT_*_HOOK as
        # documentation do not trigger the scan.
        outside_no_doc = re.sub(r'"""[\s\S]*?"""', "", outside)
        outside_no_doc = re.sub(r"'''[\s\S]*?'''", "", outside_no_doc)
        outside_no_doc = re.sub(r"#[^\n]*", "", outside_no_doc)
        leaked = set(self.HOOK_NAME_RE.findall(outside_no_doc))
        unregistered = leaked - constant_names
        self.assertFalse(
            unregistered,
            f"AGENT_*_HOOK literals outside _ESCAPE_HATCH_ENV_NAMES: "
            f"{sorted(unregistered)}. Add the name to the constant so the "
            f"parametrized destructive-non-bypass test covers it.",
        )

    def test_regex_matches_both_quote_styles(self) -> None:
        # Self-test for the quote-agnostic regex. Ensures a future regex
        # change cannot silently regress to double-quote-only matching.
        self.assertEqual(
            self.HOOK_NAME_RE.findall('"AGENT_STYLE_HOOK"'),
            ["AGENT_STYLE_HOOK"],
        )
        self.assertEqual(
            self.HOOK_NAME_RE.findall("'AGENT_COMPOUND_CD_HOOK'"),
            ["AGENT_COMPOUND_CD_HOOK"],
        )
        # Names with digits in the middle are also captured (regex is
        # `AGENT_[A-Z0-9_]*_HOOK`, so the suffix anchor is `_HOOK`).
        self.assertEqual(
            self.HOOK_NAME_RE.findall("'AGENT_V2_HOOK'"),
            ["AGENT_V2_HOOK"],
        )


# --- Phase 1: tool-agnostic mandatory risk classification -----------------


def _classify_payload(command, tool):
    return run_guard_with_payload(
        {"tool_name": tool, "tool_input": {"command": command}}
    )


class ToolAgnosticDestructiveTests(unittest.TestCase):
    """Destructive git/gh now ask on the PowerShell tool too, not only Bash."""

    def _decision(self, command, tool="PowerShell"):
        resp = _classify_payload(command, tool)
        return None if resp is None else resp["hookSpecificOutput"]["permissionDecision"]

    def test_ps_git_push_asks(self):
        self.assertEqual(self._decision("git push origin main"), "ask")

    def test_ps_git_commit_asks(self):
        self.assertEqual(self._decision('git commit -m "x"'), "ask")

    def test_ps_git_reset_hard_asks(self):
        self.assertEqual(self._decision("git reset --hard HEAD~1"), "ask")

    def test_ps_git_branch_D_asks(self):
        self.assertEqual(self._decision("git branch -D feature"), "ask")

    def test_ps_gh_pr_merge_asks(self):
        self.assertEqual(self._decision("gh pr merge 42"), "ask")

    def test_ps_git_status_passes(self):
        self.assertIsNone(self._decision("git status"))

    def test_ps_git_C_status_passes(self):
        self.assertIsNone(self._decision("git -C repo status"))

    def test_ps_get_date_passes(self):
        self.assertIsNone(self._decision("Get-Date"))

    def test_bash_tool_explicit_git_push_asks(self):
        self.assertEqual(self._decision("git push", tool="Bash"), "ask")


class PublishClassTests(unittest.TestCase):
    def _decision(self, command, tool="Bash"):
        resp = _classify_payload(command, tool)
        return None if resp is None else resp["hookSpecificOutput"]["permissionDecision"]

    def test_npm_publish_asks(self):
        self.assertEqual(self._decision("npm publish"), "ask")

    def test_npm_unpublish_asks(self):
        self.assertEqual(self._decision("npm unpublish pkg@1.0.0"), "ask")

    def test_twine_upload_asks(self):
        self.assertEqual(self._decision("twine upload dist/*"), "ask")

    def test_python_m_twine_upload_asks(self):
        self.assertEqual(self._decision("python -m twine upload dist/*"), "ask")

    def test_gh_release_create_asks(self):
        self.assertEqual(self._decision("gh release create v1.0.0"), "ask")

    def test_gh_release_create_asks_ps(self):
        self.assertEqual(self._decision("gh release create v1.0.0", tool="PowerShell"), "ask")

    def test_npm_install_passes(self):
        self.assertIsNone(self._decision("npm install"))

    def test_npm_run_test_passes(self):
        self.assertIsNone(self._decision("npm run test"))

    def test_gh_release_list_passes(self):
        self.assertIsNone(self._decision("gh release list"))


class FsDestructiveTests(unittest.TestCase):
    def _decision(self, command, tool):
        resp = _classify_payload(command, tool)
        return None if resp is None else resp["hookSpecificOutput"]["permissionDecision"]

    def test_bash_rm_rf_asks(self):
        self.assertEqual(self._decision("rm -rf /tmp/x", "Bash"), "ask")

    def test_bash_rm_fr_asks(self):
        self.assertEqual(self._decision("rm -fr /tmp/x", "Bash"), "ask")

    def test_bash_rm_r_f_separate_asks(self):
        self.assertEqual(self._decision("rm -r -f /tmp/x", "Bash"), "ask")

    def test_bash_rm_recursive_no_force_passes(self):
        # matches the existing native rule scope (rm -rf needs force too)
        self.assertIsNone(self._decision("rm -r /tmp/x", "Bash"))

    def test_bash_rm_single_file_passes(self):
        self.assertIsNone(self._decision("rm /tmp/x", "Bash"))

    def test_bash_dd_asks(self):
        self.assertEqual(self._decision("dd if=/dev/zero of=/dev/sda", "Bash"), "ask")

    def test_bash_mkfs_asks(self):
        self.assertEqual(self._decision("mkfs.ext4 /dev/sdb1", "Bash"), "ask")

    def test_bash_shred_asks(self):
        self.assertEqual(self._decision("shred -u secret.key", "Bash"), "ask")

    def test_ps_remove_item_recurse_asks(self):
        self.assertEqual(
            self._decision("Remove-Item -Recurse -Force C:\\tmp\\x", "PowerShell"), "ask"
        )

    def test_ps_rm_recurse_alias_asks(self):
        self.assertEqual(self._decision("rm -Recurse C:\\tmp\\x", "PowerShell"), "ask")

    def test_ps_remove_item_single_passes(self):
        self.assertIsNone(self._decision("Remove-Item C:\\tmp\\file.txt", "PowerShell"))


class WrapperRecursionTests(unittest.TestCase):
    """Built-in command-carrying wrappers are pierced; payloads classified."""

    def _decision(self, command, tool="Bash"):
        resp = _classify_payload(command, tool)
        return None if resp is None else resp["hookSpecificOutput"]["permissionDecision"]

    def test_ssh_rm_rf_asks(self):
        self.assertEqual(self._decision('ssh host "rm -rf /tmp/x"'), "ask")

    def test_ssh_echo_passes(self):
        self.assertIsNone(self._decision('ssh host "echo ok"'))

    def test_ssh_safe_unquoted_passes(self):
        self.assertIsNone(self._decision("ssh host ls -la"))

    def test_ssh_bash_c_git_push_asks(self):
        self.assertEqual(self._decision('ssh host "bash -c \'git push\'"'), "ask")

    def test_bash_c_git_push_asks(self):
        self.assertEqual(self._decision('bash -c "git push origin main"'), "ask")

    def test_bash_c_safe_passes(self):
        self.assertIsNone(self._decision('bash -c "echo hi"'))

    def test_docker_exec_rm_rf_asks(self):
        self.assertEqual(self._decision('docker exec c bash -lc "rm -rf /tmp/x"'), "ask")

    def test_docker_ps_passes(self):
        self.assertIsNone(self._decision("docker ps -a"))

    def test_ssh_with_port_flag_rm_rf_asks(self):
        self.assertEqual(self._decision('ssh -p 2222 host "rm -rf /data"'), "ask")

    def test_depth_within_limit_passes(self):
        # 3 ssh hops to a safe command is within MAX_WRAPPER_DEPTH
        self.assertIsNone(self._decision("ssh h1 ssh h2 ssh h3 echo ok"))

    def test_depth_exceeded_asks(self):
        # 4 ssh hops exceeds MAX_WRAPPER_DEPTH; the innermost payload cannot be
        # verified, so the guard asks rather than passing it silently.
        self.assertEqual(self._decision("ssh h1 ssh h2 ssh h3 ssh h4 echo ok"), "ask")


class ClassifierFalsePositiveTests(unittest.TestCase):
    """Leading-token classification must not flag dangerous strings that
    appear as quoted arguments, nor opaque interpreters / custom wrappers."""

    def _decision(self, command, tool="Bash"):
        resp = _classify_payload(command, tool)
        return None if resp is None else resp["hookSpecificOutput"]["permissionDecision"]

    def test_echo_rm_rf_string_passes(self):
        self.assertIsNone(self._decision('echo "rm -rf is dangerous"'))

    def test_grep_git_push_passes(self):
        self.assertIsNone(self._decision('grep -r "git push" .'))

    def test_ps_write_output_remove_item_passes(self):
        self.assertIsNone(
            self._decision('Write-Output "Remove-Item -Recurse -Force"', "PowerShell")
        )

    def test_git_commit_with_rm_rf_in_message_asks_for_commit(self):
        # asks because of `git commit`, not because the message says rm -rf
        self.assertEqual(self._decision('git commit -m "drop the rm -rf hack"'), "ask")

    def test_python_dash_c_is_opaque(self):
        # python -c carries Python source, not shell — treated as opaque
        self.assertIsNone(
            self._decision('python -c "import shutil; shutil.rmtree(\'/x\')"')
        )

    def test_custom_wrapper_is_opaque(self):
        # a private runner is not a built-in wrapper; its payload is not pierced
        self.assertIsNone(self._decision('myrunner.py run "rm -rf /tmp/x"'))


class CodeReviewRegressionTests(unittest.TestCase):
    """Round-1 execution-review (Codex) found these false negatives via live
    probes; freeze them fixed."""

    def _decision(self, command, tool="Bash"):
        resp = _classify_payload(command, tool)
        return None if resp is None else resp["hookSpecificOutput"]["permissionDecision"]

    # H1: path-qualified git / gh must still ask
    def test_path_qualified_git_push_asks(self):
        self.assertEqual(self._decision("/usr/bin/git push origin main"), "ask")

    def test_ps_full_path_git_exe_push_asks(self):
        self.assertEqual(
            self._decision('& "C:\\Program Files\\Git\\cmd\\git.exe" push origin main', "PowerShell"),
            "ask",
        )

    def test_ps_full_path_gh_exe_pr_merge_asks(self):
        self.assertEqual(
            self._decision('& "C:\\Program Files\\GitHub CLI\\gh.exe" pr merge 42', "PowerShell"),
            "ask",
        )

    def test_git_status_full_path_passes(self):
        self.assertIsNone(self._decision("/usr/bin/git status"))

    # H2: recursive delete flag variants (uppercase R, PowerShell abbreviation)
    def test_bash_rm_Rf_uppercase_asks(self):
        self.assertEqual(self._decision("rm -Rf /tmp/x"), "ask")

    def test_bash_rm_fR_asks(self):
        self.assertEqual(self._decision("rm -fR /tmp/x"), "ask")

    def test_ps_remove_item_rec_abbrev_asks(self):
        self.assertEqual(self._decision("Remove-Item -Rec -Force C:\\tmp\\x", "PowerShell"), "ask")

    # H3: wrapper option forms before the payload
    def test_powershell_executionpolicy_command_git_push_asks(self):
        self.assertEqual(
            self._decision('powershell -ExecutionPolicy Bypass -Command "git push"'), "ask"
        )

    def test_powershell_executionpolicy_command_remove_item_asks(self):
        self.assertEqual(
            self._decision(
                'powershell -ExecutionPolicy Bypass -Command "Remove-Item -Recurse -Force C:\\tmp\\x"'
            ),
            "ask",
        )

    def test_docker_global_context_exec_rm_rf_asks(self):
        self.assertEqual(
            self._decision('docker --context prod exec c bash -lc "rm -rf /tmp/x"'), "ask"
        )

    # M4: publish with global options before the verb
    def test_npm_registry_publish_asks(self):
        self.assertEqual(
            self._decision("npm --registry https://registry.npmjs.org publish"), "ask"
        )

    def test_twine_repository_upload_asks(self):
        self.assertEqual(self._decision("twine --repository pypi upload dist/*"), "ask")

    # negatives: the option-skipping must not over-fire
    def test_docker_global_context_safe_passes(self):
        self.assertIsNone(self._decision("docker --context prod ps -a"))

    def test_powershell_executionpolicy_safe_passes(self):
        self.assertIsNone(
            self._decision('powershell -ExecutionPolicy Bypass -Command "Get-Date"')
        )

    def test_npm_registry_install_passes(self):
        self.assertIsNone(self._decision("npm --registry https://registry.npmjs.org install"))


class CodeReviewRound2RegressionTests(unittest.TestCase):
    """Round-2 execution-review (Codex live probes) found adjacent forms that
    reopened H2/H3/M4 plus new sudo-prefix and python-twine over-fire cases."""

    def _decision(self, command, tool="Bash"):
        resp = _classify_payload(command, tool)
        return None if resp is None else resp["hookSpecificOutput"]["permissionDecision"]

    # H2: shorter PowerShell -Recurse prefixes
    def test_ps_remove_item_re_abbrev_asks(self):
        self.assertEqual(self._decision("Remove-Item -Re -Force C:\\tmp\\x", "PowerShell"), "ask")

    def test_ps_rm_re_abbrev_asks(self):
        self.assertEqual(self._decision("rm -Re C:\\tmp\\x", "PowerShell"), "ask")

    # H3: bash value-options before -c
    def test_bash_o_pipefail_c_git_push_asks(self):
        self.assertEqual(self._decision('bash -o pipefail -c "git push"'), "ask")

    def test_bash_O_extglob_c_git_push_asks(self):
        self.assertEqual(self._decision('bash -O extglob -c "git push"'), "ask")

    def test_bash_rcfile_c_git_push_asks(self):
        self.assertEqual(self._decision('bash --rcfile /tmp/x -c "git push"'), "ask")

    def test_bash_c_safe_with_options_passes(self):
        self.assertIsNone(self._decision('bash -o pipefail -c "echo hi"'))

    # H3: encoded PowerShell command -> fail closed
    def test_powershell_encodedcommand_asks(self):
        self.assertEqual(self._decision("powershell -EncodedCommand ZQBjAGgAbwA="), "ask")

    def test_powershell_enc_abbrev_asks(self):
        self.assertEqual(self._decision("powershell -enc ZQBjAGgAbwA="), "ask")

    def test_powershell_executionpolicy_not_mistaken_for_encoded(self):
        # -ExecutionPolicy must NOT trip the -EncodedCommand fail-closed path
        self.assertIsNone(self._decision('powershell -ExecutionPolicy Bypass -Command "Get-Date"'))

    # H3: docker TLS global options
    def test_docker_tlscacert_exec_rm_rf_asks(self):
        self.assertEqual(
            self._decision('docker --tlscacert cert.pem exec c bash -lc "rm -rf /tmp/x"'), "ask"
        )

    # M4: npm --scope
    def test_npm_scope_publish_asks(self):
        self.assertEqual(self._decision("npm --scope @org publish"), "ask")

    def test_npm_scope_install_passes(self):
        self.assertIsNone(self._decision("npm --scope @org install"))

    # N2: sudo / doas prefix wrappers
    def test_sudo_git_push_asks(self):
        self.assertEqual(self._decision("sudo git push"), "ask")

    def test_sudo_rm_rf_asks(self):
        self.assertEqual(self._decision("sudo rm -rf /tmp/x"), "ask")

    def test_sudo_u_user_git_push_asks(self):
        self.assertEqual(self._decision("sudo -u deploy git push"), "ask")

    def test_doas_rm_rf_asks(self):
        self.assertEqual(self._decision("doas rm -rf /tmp/x"), "ask")

    def test_sudo_safe_passes(self):
        self.assertIsNone(self._decision("sudo ls -la"))

    # N3: python twine must be the `-m twine` module form (no over-fire)
    def test_python_script_twine_upload_passes(self):
        self.assertIsNone(self._decision("python script.py twine upload"))

    def test_python_m_nottwine_passes(self):
        self.assertIsNone(self._decision("python -m nottwine upload twine"))

    def test_python_m_twine_upload_still_asks(self):
        self.assertEqual(self._decision("python -m twine upload dist/*"), "ask")


class CodeReviewRound3RegressionTests(unittest.TestCase):
    """Round-3 execution-review (Codex live probes) found realistic wrapper and
    interpreter forms that still passed: path-qualified env, the Windows `cmd /c`
    wrapper, docker --mount/--env-file value flags, and versioned python launchers."""

    def _decision(self, command, tool="Bash"):
        resp = _classify_payload(command, tool)
        return None if resp is None else resp["hookSpecificOutput"]["permissionDecision"]

    # N4: path-qualified env prefix
    def test_path_qualified_env_git_push_asks(self):
        self.assertEqual(self._decision("/usr/bin/env git push"), "ask")

    def test_path_qualified_env_rm_rf_asks(self):
        self.assertEqual(self._decision("/usr/bin/env rm -rf /tmp/x"), "ask")

    def test_path_qualified_env_safe_passes(self):
        self.assertIsNone(self._decision("/usr/bin/env ls -la"))

    # N4: Windows cmd /c | /k wrapper
    def test_cmd_c_git_push_asks(self):
        self.assertEqual(self._decision("cmd /c git push", "PowerShell"), "ask")

    def test_cmd_k_git_push_asks(self):
        self.assertEqual(self._decision("cmd /k git push", "PowerShell"), "ask")

    def test_cmd_c_rmdir_s_asks(self):
        self.assertEqual(self._decision("cmd /c rmdir /s C:\\tmp\\x", "PowerShell"), "ask")

    def test_cmd_c_safe_passes(self):
        self.assertIsNone(self._decision("cmd /c echo hi", "PowerShell"))

    # N4: docker run value flags (--mount, --env-file) before the image
    def test_docker_run_mount_rm_rf_asks(self):
        self.assertEqual(
            self._decision(
                "docker run --mount type=bind,src=/,dst=/host ubuntu rm -rf /host/tmp"
            ),
            "ask",
        )

    def test_docker_run_env_file_rm_rf_asks(self):
        self.assertEqual(
            self._decision("docker run --env-file .env ubuntu rm -rf /tmp/x"), "ask"
        )

    def test_docker_run_mount_safe_passes(self):
        self.assertIsNone(
            self._decision(
                "docker run --mount type=bind,src=/,dst=/host ubuntu echo hi"
            )
        )

    # N5: versioned / path-qualified python interpreters publishing via twine
    def test_python311_m_twine_upload_asks(self):
        self.assertEqual(self._decision("python3.11 -m twine upload dist/*"), "ask")

    def test_path_qualified_python311_m_twine_upload_asks(self):
        self.assertEqual(
            self._decision("/usr/bin/python3.11 -m twine upload dist/*"), "ask"
        )

    def test_python312_m_twine_upload_asks(self):
        self.assertEqual(self._decision("python3.12 -m twine upload dist/*"), "ask")

    def test_python311_script_twine_passes(self):
        self.assertIsNone(self._decision("python3.11 script.py twine upload"))

    def test_python311_m_pytest_passes(self):
        self.assertIsNone(self._decision("python3.11 -m pytest"))


class CodeReviewRound4RegressionTests(unittest.TestCase):
    """Round-4 execution-review (Codex live probes) found a High false negative
    in the PowerShell -Command extractor (only one trailing token kept) and a
    Medium prefix-runner boundary gap (command / timeout / xargs)."""

    def _decision(self, command, tool="Bash"):
        resp = _classify_payload(command, tool)
        return None if resp is None else resp["hookSpecificOutput"]["permissionDecision"]

    # N6: PowerShell -Command concatenates ALL trailing tokens (unquoted form)
    def test_ps_command_unquoted_git_push_asks(self):
        self.assertEqual(self._decision("powershell -Command git push", "PowerShell"), "ask")

    def test_ps_command_unquoted_remove_item_asks(self):
        self.assertEqual(
            self._decision("powershell -Command Remove-Item -Recurse -Force C:\\tmp\\x", "PowerShell"),
            "ask",
        )

    def test_pwsh_command_unquoted_gh_release_asks(self):
        self.assertEqual(
            self._decision("pwsh -Command gh release create v1.0.0", "PowerShell"), "ask"
        )

    def test_ps_command_quoted_git_push_still_asks(self):
        self.assertEqual(self._decision('powershell -Command "git push"', "PowerShell"), "ask")

    def test_ps_command_safe_passes(self):
        self.assertIsNone(self._decision("powershell -Command Get-Date", "PowerShell"))

    # N7: command / nohup / setsid transparent prefixes
    def test_command_git_push_asks(self):
        self.assertEqual(self._decision("command git push"), "ask")

    def test_command_p_git_push_asks(self):
        self.assertEqual(self._decision("command -p git push"), "ask")

    def test_command_v_git_passes(self):
        # `command -v git` resolves to the read-only path lookup, no subcommand
        self.assertIsNone(self._decision("command -v git"))

    def test_nohup_git_push_asks(self):
        self.assertEqual(self._decision("nohup git push"), "ask")

    def test_setsid_rm_rf_asks(self):
        self.assertEqual(self._decision("setsid rm -rf /tmp/x"), "ask")

    # N7: timeout DURATION COMMAND
    def test_timeout_git_push_asks(self):
        self.assertEqual(self._decision("timeout 30 git push"), "ask")

    def test_timeout_signal_flag_rm_rf_asks(self):
        self.assertEqual(self._decision("timeout -s KILL 30 rm -rf /tmp/x"), "ask")

    def test_timeout_safe_passes(self):
        self.assertIsNone(self._decision("timeout 30 ls -la"))

    # N7: xargs (commonly behind a pipe)
    def test_find_pipe_xargs_rm_rf_asks(self):
        self.assertEqual(
            self._decision("find . -type d -name __pycache__ | xargs rm -rf"), "ask"
        )

    def test_xargs_n_flag_rm_rf_asks(self):
        self.assertEqual(self._decision("xargs -n 1 rm -rf"), "ask")

    def test_xargs_safe_passes(self):
        self.assertIsNone(self._decision("echo x | xargs echo hi"))


if __name__ == "__main__":
    unittest.main()
