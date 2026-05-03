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
import shutil
import tempfile


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


class AutoWatchAllowTests(unittest.TestCase):
    """The implement-review skill's auto-watch.ps1 watcher should auto-allow
    on the PowerShell tool. Unrelated PowerShell calls fall through to the
    existing permission flow. Bash invocations referencing the same path tail
    are not affected because the auto-allow is keyed on tool_name.
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


if __name__ == "__main__":
    unittest.main()
