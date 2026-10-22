"""Tests for guard.py hook. Discovers guard.py relative to the repo root."""
from __future__ import annotations

import builtins
import importlib.util
import json
import os
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

# tests/ is on sys.path under `unittest discover -s tests` but not under
# `python -m unittest tests.<module>`, which validate.yml uses for the
# Sentinel redaction smoke. Put it there before the sibling import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _quiet_spawn  # noqa: E402,F401  installs a windowless spawn default on Windows


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


class NestedGitInitTests(unittest.TestCase):
    """A git init inside an existing worktree is denied, elsewhere it is not.

    Four review packets left in one proposal repo held 85 staged-and-never-
    committed files across four nested .git directories. Git never mentioned
    them, because the enclosing directory was ignored; PyCharm registered each
    as a VCS root and offered to commit all of them at once (#56). No skill
    creates those packets, so the durable fix is a gate rather than an edit.

    Every case below was raised by a reviewer against a first draft that
    denied `npm init`, ignored `git -C`, read a flag value as the target, and
    missed a git init after a newline or a pipe. They run through the hook
    rather than the helper, because the first draft's one negative case passed
    against a defect that denied every `<tool> init` in the repository.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        (self.repo / "out" / "packet").mkdir(parents=True)
        result = subprocess.run(
            ["git", "init", "-q", str(self.repo)], capture_output=True, text=True)
        if result.returncode != 0:
            self.skipTest("git is not available")
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.inside = self.repo / "out" / "packet"

    def _decide(self, command, cwd, env=None):
        response = run_guard_with_payload(
            {"tool_name": "Bash", "tool_input": {"command": command}},
            env=env, cwd=str(cwd),
        )
        if response is None:
            return "PASSED"
        return response["hookSpecificOutput"]["permissionDecision"].upper()

    def test_a_git_init_inside_a_worktree_is_denied(self) -> None:
        for command, cwd, label in (
            ("git init", self.inside, "bare init inside"),
            ("git init packet", self.repo / "out", "relative target"),
            (f"git init {self.inside}", self.outside, "absolute target"),
            ("git init ../out/packet", self.repo / "out", "a .. that stays inside"),
        ):
            with self.subTest(label):
                self.assertEqual(self._decide(command, cwd), "DENY", label)

    def test_a_target_outside_every_worktree_is_allowed(self) -> None:
        for command, cwd, label in (
            ("git init", self.outside, "bare init outside"),
            (f"git init {self.outside / 'fresh'}", self.repo, "absolute target outside"),
            ("git init ../outside/fresh", self.repo, "a .. that escapes the repo"),
            (f"git init {self.repo}", self.outside, "re-initializing an existing root"),
        ):
            with self.subTest(label):
                self.assertEqual(self._decide(command, cwd), "PASSED", label)

    def test_another_tool_named_init_is_not_a_git_init(self) -> None:
        # The defect that made this class table-driven: extract_git_subcommand
        # starts at token 1 and assumes its caller checked the executable, so
        # every `<tool> init` in every consumer repository was denied.
        for command in ("npm init", "yarn init -y", "cargo init", "terraform init",
                        "uv init", "echo init", "pnpm init", "go mod init"):
            with self.subTest(command):
                self.assertEqual(self._decide(command, self.inside), "PASSED", command)

    def test_the_directory_git_would_run_in_decides(self) -> None:
        # `git -C` is the form this repository's own compound-cd gate steers
        # agents toward, and ignoring it inverted the decision in both
        # directions: it passed an init that lands inside and denied one that
        # lands outside.
        for command, cwd, expected, label in (
            (f"git -C {self.inside} init", self.outside, "DENY", "-C into a repo"),
            (f"git -C{self.inside} init", self.outside, "DENY", "attached -Cpath"),
            (f"git -C {self.outside} init", self.repo, "PASSED", "-C out of a repo"),
        ):
            with self.subTest(label):
                self.assertEqual(self._decide(command, cwd), expected, label)

    def test_a_flag_value_is_never_read_as_the_target(self) -> None:
        for command, label in (
            (f"git init -b main {self.inside}", "-b"),
            (f"git init --object-format sha1 {self.inside}", "--object-format"),
            (f"git init --ref-format files {self.inside}", "--ref-format"),
            (f"git init --object-format=sha1 {self.inside}", "--object-format= inline"),
        ):
            with self.subTest(label):
                self.assertEqual(self._decide(command, self.outside), "DENY", label)

    def test_ordinary_shell_forms_do_not_slip_past(self) -> None:
        for command, label in (
            (f"& git init {self.inside}", "PowerShell call operator"),
            (f"&git init {self.inside}", "call operator with no space"),
            (f"echo hello | git init {self.inside}", "after a pipe"),
            (f"echo hello\ngit init {self.inside}", "after a newline"),
            (f"true && git init {self.inside}", "after &&"),
        ):
            with self.subTest(label):
                self.assertEqual(self._decide(command, self.outside), "DENY", label)

    def test_a_command_that_redirects_is_declined_rather_than_misread(self) -> None:
        # A redirection's operands are not arguments, and every attempt to tell
        # them apart read one as the target: `git init >/dev/null` was denied at
        # a repository root, and `git init *> $null <outside>` was denied for a
        # directory git created outside the worktree. Declining is the contract
        # now, so none of these may deny.
        for command, cwd, label in (
            ("git init >/dev/null", self.repo, "attached redirect, no target"),
            ("git init > /dev/null", self.repo, "bare redirect, no target"),
            ("git init *> $null", self.repo, "PowerShell redirect"),
            (f"git init *> $null {self.outside / 'fresh'}", self.repo, "target outside"),
            (f"git init >/dev/null {self.inside}", self.outside, "target inside"),
            (f"git init 2>&1 {self.inside}", self.outside, "attached descriptor"),
            (f"git init>/dev/null {self.inside}", self.outside, "no space before the operator"),
        ):
            with self.subTest(label):
                self.assertEqual(self._decide(command, cwd), "PASSED", label)

    def test_a_heredoc_makes_the_whole_command_data(self) -> None:
        # A heredoc body reads exactly like commands, and deciding where it ends
        # is the part that went wrong twice: stripping the opener line deleted a
        # real command, and an indented terminator ended the body early. The
        # command is declined as a whole instead.
        body = f"cat <<'EOF'\ngit init {self.inside}\nEOF"
        unquoted = f"cat <<EOF\ngit init {self.inside}\nEOF"
        indented = f"cat <<'EOF'\n EOF\ngit init {self.inside}\nEOF"
        two = f"cat <<A <<B\nnothing\nA\ngit init {self.inside}\nB"
        suffix = f"cat <<'EOF'; git init {self.inside}\nnothing\nEOF"
        for command, label in (
            (body, "quoted heredoc"),
            (unquoted, "unquoted heredoc"),
            (indented, "an indented line is not the terminator"),
            (two, "two heredocs on one header"),
            (suffix, "a real command after the opener, declined with it"),
        ):
            with self.subTest(label):
                self.assertEqual(self._decide(command, self.outside), "PASSED", label)

    def test_a_comment_after_an_operator_is_still_a_comment(self) -> None:
        # A shell word ends at an operator as well as at whitespace, so `;#`
        # and `|#` begin comments. Reading them as arguments turned a printed
        # note into a denied command under both shells.
        for command, tool, label in (
            (f"echo hello;# note; git init {self.inside}", "Bash", "after ;"),
            (f"echo hello |# note; git init {self.inside}\ncat", "Bash", "after |"),
            (f"Write-Output hello;# note; git init {self.inside}", "PowerShell",
             "PowerShell after ;"),
        ):
            with self.subTest(label):
                response = run_guard_with_payload(
                    {"tool_name": tool, "tool_input": {"command": command}},
                    cwd=str(self.outside),
                )
                self.assertIsNone(response, label)

    def test_an_escaped_operator_does_not_start_a_comment(self) -> None:
        # Extending the comment boundary to the operator characters brought
        # its own false denial. `\;` and `` `; `` are literal, so the word
        # continues and the `#` beside it is an ordinary character. Stripping
        # from there deleted a string opener and exposed the string body as
        # commands. Declining is the policy the quote escapes already take.
        bash = f"printf '%s' \\;#'payload\ngit init {self.inside}\n'"
        pwsh = f"Write-Output `;#'payload\ngit init {self.inside}\n'"
        for command, tool, label in (
            (bash, "Bash", "backslash-escaped operator"),
            (pwsh, "PowerShell", "backtick-escaped operator"),
        ):
            with self.subTest(label):
                response = run_guard_with_payload(
                    {"tool_name": tool, "tool_input": {"command": command}},
                    cwd=str(self.outside),
                )
                self.assertIsNone(response, label)

    def test_an_escaped_hash_is_literal_text_not_a_comment(self) -> None:
        # The boundary test reads the character before the boundary, not the
        # one before the `#`, and an escape is not a boundary character, so
        # `\#` and `` `# `` never reach it. The hash stays the literal
        # argument text every shell reads it as, and the initializer after the
        # separator is judged. Both reviewers asked for the shipped
        # description and the code to agree here.
        for command, tool, label in (
            (f"printf '%s' \\#; git init {self.inside}", "Bash", "backslash hash"),
            (f"Write-Output `#; git init {self.inside}", "PowerShell", "backtick hash"),
        ):
            with self.subTest(label):
                response = run_guard_with_payload(
                    {"tool_name": tool, "tool_input": {"command": command}},
                    cwd=str(self.outside),
                )
                self.assertIsNotNone(response, label)

    def test_a_quoted_operator_beside_a_hash_is_not_a_comment(self) -> None:
        # The control for the case above. An operator inside quotes is not an
        # operator, so the `#` beside it opens nothing, and the initializer
        # after the real separator is still judged.
        self.assertEqual(
            self._decide(f"printf '%s' ';#' ; git init {self.inside}", self.outside),
            "DENY",
        )

    def test_a_block_comment_carries_its_body_like_a_heredoc(self) -> None:
        # PowerShell's `<# ... #>` spans lines, so the body reads as commands
        # once the command is split. The whole command is declined instead.
        command = f"<#\ngit init {self.inside}\n#>\nWrite-Output hello"
        response = run_guard_with_payload(
            {"tool_name": "PowerShell", "tool_input": {"command": command}},
            cwd=str(self.outside),
        )
        self.assertIsNone(response, "a block comment body was judged as a command")

    def test_an_escaped_quote_makes_the_command_undecidable(self) -> None:
        # An escape moves where a quoted region ends, and the splitter and
        # tokenizer downstream both assume it does not, so one fix in the
        # scanner would not be enough. Capturing a command example inside a
        # double-quoted string is ordinary in exactly this repository's work.
        bash = f'printf "%s" "literal \\"; git init {self.inside}; \\""'
        pwsh = f'Write-Output "literal `"; git init {self.inside}; `""'
        for command, tool, label in (
            (bash, "Bash", "backslash escape"),
            (pwsh, "PowerShell", "backtick escape"),
        ):
            with self.subTest(label):
                response = run_guard_with_payload(
                    {"tool_name": tool, "tool_input": {"command": command}},
                    cwd=str(self.outside),
                )
                self.assertIsNone(response, label)

    def test_a_windows_path_is_not_an_escaped_quote(self) -> None:
        # The escape check keys on a quote after the escape character, so a
        # backslash-separated path is unaffected. This is the control for the
        # tokenizer fix an earlier round needed.
        self.assertEqual(
            self._decide(f"git init {self.inside}", self.outside), "DENY")

    def test_a_here_string_is_a_redirection_not_a_heredoc(self) -> None:
        # `<<<` puts its word on the same line, so only that segment is
        # declined and a real initializer after it is still judged.
        command = f"cat <<<EOF\ngit init {self.inside}"
        self.assertEqual(self._decide(command, self.outside), "DENY")

    def test_a_marker_inside_quotes_or_a_comment_is_not_a_heredoc(self) -> None:
        # The mirror of the case above. Arming heredoc mode from a quoted or
        # commented `<<EOF` declined real commands that follow, and resetting
        # quote state per line made a multiline quoted argument containing `#`
        # swallow its own closing quote.
        for command, label in (
            (f"printf '%s' '<<EOF'\ngit init {self.inside}", "quoted marker"),
            (f"echo hello # <<EOF\ngit init {self.inside}", "commented marker"),
            (f"printf '%s' 'payload\n# data'\ngit init {self.inside}",
             "multiline quoted argument"),
        ):
            with self.subTest(label):
                self.assertEqual(self._decide(command, self.outside), "DENY", label)

    def test_a_comment_is_not_executed(self) -> None:
        self.assertEqual(
            self._decide(f"echo hello # note; git init {self.inside}", self.outside),
            "PASSED",
        )

    def test_the_word_init_elsewhere_does_not_trigger_it(self) -> None:
        self.assertNotEqual(self._decide("git commit -m 'init'", self.repo), "DENY")
        self.assertEqual(self._decide("echo 'git init'", self.repo), "PASSED")
        self.assertEqual(self._decide("git status", self.repo), "PASSED")

    def test_the_escape_env_releases_it(self) -> None:
        # A submodule is a deliberate inner repository, so the gate has to be
        # openable for one call rather than only argued with.
        self.assertEqual(
            self._decide("git init", self.inside,
                         env={"AGENT_NESTED_GIT_INIT_HOOK": "off"}),
            "PASSED",
        )

    def test_the_deny_names_the_reroute_and_the_escape(self) -> None:
        response = run_guard_with_payload(
            {"tool_name": "Bash", "tool_input": {"command": "git init"}},
            cwd=str(self.inside),
        )
        reason = response["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Suggested rewrite:", reason)
        self.assertIn("scratch", reason)
        self.assertIn("AGENT_NESTED_GIT_INIT_HOOK", reason)


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


class ShellProseWriteTests(unittest.TestCase):
    """A shell command that writes a prose file meets the same banned-word
    gate as Write.

    On 2026-10-21 a heredoc put two banned words into a Markdown file and the
    hook said nothing, while the same text through Write was denied. Each
    denied shape below is paired with a neighbour that must pass, because the
    commands an agent runs most often read prose files rather than write them,
    and a gate that stopped a `grep` for a banned word would be switched off.
    """

    @staticmethod
    def _decision(command, tool="Bash", env=None):
        resp = run_guard_with_payload(
            {"tool_name": tool, "tool_input": {"command": command}, "cwd": "/repo"},
            env=env,
        )
        if not resp:
            return None
        return resp["hookSpecificOutput"]["permissionDecision"]

    @staticmethod
    def _scratch(*parts):
        # The form a Bash command carries: Git Bash on Windows reads an
        # unquoted backslash as an escape, so a native path would not survive.
        return Path(tempfile.gettempdir(), "ir-probe", *parts).as_posix()

    def test_backslash_inside_double_quotes_is_kept(self):
        resp = run_guard_with_payload({
            "tool_name": "Bash", "cwd": "/repo",
            "tool_input": {"command": 'echo delve > "notes\\draft.md"'},
        })
        self.assertIn("notes\\draft.md",
                      resp["hookSpecificOutput"]["permissionDecisionReason"])

    def test_heredoc_into_markdown_is_denied(self):
        resp = run_guard_with_payload({
            "tool_name": "Bash",
            "tool_input": {"command": "cat > notes.md <<'EOF'\nWe delve into the realm.\nEOF"},
            "cwd": "/repo",
        })
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")
        reason = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("delve, realm", reason)
        self.assertIn("Suggested rewrite:", reason)
        self.assertIn("Edit tool", reason)

    def test_heredoc_with_redirect_after_it_is_denied(self):
        self.assertEqual(
            self._decision("cat <<'EOF' > notes.md\nA pivotal result.\nEOF"), "deny")

    def test_clean_heredoc_passes(self):
        self.assertIsNone(
            self._decision("cat > notes.md <<'EOF'\nA central result.\nEOF"))

    def test_echo_append_is_denied(self):
        self.assertEqual(self._decision('echo "We delve." >> notes.md'), "deny")

    def test_tee_is_denied(self):
        self.assertEqual(
            self._decision("printf 'A vast set.\\n' | tee -a notes.txt"), "deny")

    def test_in_place_sed_is_denied(self):
        self.assertEqual(
            self._decision("sed -i '' 's/look at/delve into/' notes.md"), "deny")

    def test_in_place_sed_that_removes_the_word_is_denied_too(self):
        """The documented cost of reading the whole command. The Edit tool,
        whose new text carries no banned word, is the reroute."""
        self.assertEqual(
            self._decision("sed -i 's/delve/examine/' notes.md"), "deny")

    def test_in_place_perl_is_denied(self):
        self.assertEqual(
            self._decision("perl -pi -e 's/a/pivotal/' paper.tex"), "deny")

    def test_powershell_set_content_is_denied(self):
        self.assertEqual(
            self._decision('Set-Content -Path notes.md -Value "We delve."',
                           tool="PowerShell"),
            "deny",
        )

    def test_writes_inside_a_bash_c_payload_are_denied(self):
        self.assertEqual(
            self._decision("bash -c \"sed -i 's/x/pivotal/' notes.md\""), "deny")
        self.assertEqual(
            self._decision("bash -c 'echo pivotal > notes.md'"), "deny")

    def test_reading_a_prose_file_for_a_banned_word_passes(self):
        self.assertIsNone(self._decision('grep -rn "delve" docs/*.md 2>/dev/null'))
        self.assertIsNone(self._decision('sed -n "/delve/p" notes.md'))

    def test_writing_a_code_file_passes(self):
        self.assertIsNone(self._decision('echo "pivotal" > out.py'))

    def test_inline_code_in_markdown_is_ignored(self):
        self.assertIsNone(
            self._decision("echo 'The word `pivotal` is banned.' > notes.md"))

    def test_commit_message_is_left_to_the_git_gate(self):
        # Not a prose file, so this gate stays out; the destructive-git row
        # still asks for the commit itself.
        self.assertEqual(self._decision('git commit -m "A pivotal fix"'), "ask")

    def test_declared_scratch_path_built_in_the_command_passes(self):
        scratch = self._scratch()
        command = (
            f"P={scratch}; cat > \"$P/agent-io/prompt.txt\" <<'EOF'\n"
            "Check for delve and realm.\nEOF"
        )
        self.assertIsNone(self._decision(command))

    def test_declared_repo_path_is_still_denied(self):
        self.assertEqual(
            self._decision("cat > /repo/agent-io/prompt.md <<'EOF'\ndelve\nEOF"),
            "deny",
        )

    def test_unresolvable_path_is_declined(self):
        self.assertNotIn("GUARD_TEST_UNSET_DIR", os.environ)
        self.assertIsNone(self._decision(
            'cat > "$GUARD_TEST_UNSET_DIR/notes.md" <<EOF\ndelve\nEOF'))

    def test_script_that_writes_the_file_itself_is_opaque(self):
        self.assertIsNone(self._decision(
            "python3 - <<'EOF'\nopen('notes.md', 'w').write('delve')\nEOF"))

    def test_style_hook_off_disables_the_shell_gate(self):
        self.assertIsNone(self._decision(
            'echo "We delve." >> notes.md', env={"AGENT_STYLE_HOOK": "off"}))

    # Where a command writes comes from the shell's structure. The first
    # version matched redirections over the raw text, and review round 1
    # reproduced a deny for each command below.

    def test_redirection_spelled_inside_quotes_is_not_a_write(self):
        self.assertIsNone(self._decision("rg -n 'delve > notes.md' tests/"))
        self.assertIsNone(self._decision("git log --all --grep='delve > notes.md'"))

    def test_redirection_inside_a_heredoc_body_is_not_a_write(self):
        self.assertIsNone(self._decision(
            "cat > test_example.py <<'EOF'\ncommand = 'echo delve > notes.md'\nEOF"))
        self.assertIsNone(self._decision("cat <<'EOF'\necho delve > notes.md\nEOF"))

    def test_scratch_prompt_quoting_a_write_passes(self):
        """A review or dispatch prompt carries diffs and command examples."""
        scratch = self._scratch("agent-io")
        self.assertIsNone(self._decision(
            f"cat > {scratch}/prompt.txt <<'EOF'\n"
            "Review this command: echo delve > notes.md\nEOF"))
        self.assertIsNone(self._decision(
            f"cat > {scratch}/prompt.md <<'EOF'\nCheck delve.\ntee notes.md\nEOF"))

    def test_redirection_in_a_comment_is_not_a_write(self):
        self.assertIsNone(self._decision("true # echo delve > notes.md"))

    def test_redirection_operand_is_read_whole(self):
        self.assertIsNone(self._decision("echo delve > out.md-file.log"))
        self.assertIsNone(self._decision("echo delve > result.md/log.txt.gz"))

    def test_sed_expression_attached_to_e_is_not_in_place(self):
        self.assertIsNone(self._decision("sed -e's/x/pivotal/' notes.md"))
        self.assertIsNone(self._decision("sed --expression='s/x/pivotal/' notes.md"))
        self.assertEqual(
            self._decision("sed -i.bak -e 's/a/pivotal/' notes.md"), "deny")
        self.assertEqual(self._decision("sed -Ei 's/a/pivotal/' notes.md"), "deny")

    def test_filename_held_in_a_variable_is_resolved(self):
        self.assertEqual(
            self._decision('OUT=notes.md; echo delve > "$OUT"'), "deny")
        self.assertEqual(
            self._decision('echo delve > "$GUARD_TEST_OUT"',
                           env={"GUARD_TEST_OUT": "notes.md"}),
            "deny",
        )

    def test_powershell_value_is_not_a_destination(self):
        self.assertIsNone(self._decision(
            "Set-Content -Path output.log -Value 'Read pivotal notes.md'",
            tool="PowerShell"))
        self.assertIsNone(self._decision(
            'Set-Content output.json -Value "delve in notes.md"', tool="PowerShell"))

    def test_powershell_whatif_writes_nothing(self):
        self.assertIsNone(self._decision(
            "Set-Content -Path notes.md -Value delve -WhatIf", tool="PowerShell"))

    def test_powershell_assignment_is_resolved(self):
        for command in ('$out="notes.md"; Set-Content -Path $out -Value "delve"',
                        '$out = "notes.md"; Set-Content -Path $out -Value "delve"'):
            with self.subTest(command=command):
                self.assertEqual(self._decision(command, tool="PowerShell"), "deny")

    def test_assignment_inside_a_heredoc_body_is_not_one(self):
        """Round 1 (Agy): a body line `P=foo` overrode the real `P=`."""
        scratch = self._scratch()
        self.assertIsNone(self._decision(
            f"P={scratch}; cat > \"$P/agent-io/prompt.txt\" <<'EOF'\n"
            "In the script, P=foo sets the parameter.\nWe delve.\nEOF"))

    def test_blockquote_in_a_scratch_prompt_is_not_a_write(self):
        scratch = os.path.join(tempfile.gettempdir(), "ir-probe", "agent-io")
        self.assertIsNone(self._decision(
            f"cat > \"{scratch}/prompt.txt\" <<'EOF'\n"
            "> AGENTS.md lists delve as a banned word.\nEOF"))

    def test_commit_message_quoting_a_redirection_reaches_the_git_gate(self):
        self.assertEqual(self._decision(
            'git commit -m "Close the bypass where echo delve > notes.md passed"'),
            "ask")

    def test_read_only_sed_with_attached_script_passes(self):
        self.assertIsNone(self._decision("sed -n -e'/pivotal/p' notes.md"))

    def test_default_expansion_resolves_a_scratch_path(self):
        self.assertIsNone(self._decision(
            "cat > \"${TMPDIR:-/tmp}/agent-io/prompt.txt\" <<'EOF'\ndelve\nEOF",
            env={"TMPDIR": tempfile.gettempdir()}))
        self.assertEqual(self._decision(
            'cat > "${GUARD_TEST_UNSET_DIR:-notes.md}" <<EOF\ndelve\nEOF'), "deny")

    def test_command_substitution_leaves_the_path_unknown(self):
        for command in (
            "P=`mktemp -d`; cat > \"$P/agent-io/prompt.txt\" <<'EOF'\ndelve\nEOF",
            "P=$(mktemp -d); cat > \"$P/agent-io/prompt.txt\" <<'EOF'\ndelve\nEOF",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self._decision(command))

    def test_powershell_environment_variable_is_resolved(self):
        self.assertEqual(self._decision(
            'Set-Content -Path "$env:GUARD_TEST_DIR/notes.md" -Value "We delve."',
            tool="PowerShell", env={"GUARD_TEST_DIR": "/repo"}), "deny")

    # Round 2 (Codex): a variable is read as it stood when its command ran.

    def test_later_reassignment_does_not_reach_back(self):
        self.assertIsNone(self._decision(
            'OUT=build.log; echo delve > "$OUT"; OUT=notes.md'))
        self.assertEqual(self._decision(
            'OUT=notes.md; echo delve > "$OUT"; OUT=build.log'), "deny")
        scratch = self._scratch()
        self.assertIsNone(self._decision(
            f"P={scratch}; cat > \"$P/agent-io/prompt.txt\" <<'EOF'\n"
            "delve\nEOF\nP=/repo"))

    def test_command_local_and_child_assignments_do_not_persist(self):
        self.assertIsNone(self._decision('OUT=notes.md true; echo delve > "$OUT"'))
        self.assertIsNone(self._decision(
            "bash -c 'OUT=notes.md'; echo delve > \"$OUT\""))

    # Round 3 (Codex): the child shell sees its command-local assignment.

    def test_command_local_assignment_reaches_the_child(self):
        self.assertIsNone(self._decision(
            "OUT=notes.md; OUT=build.log bash -c 'echo delve > \"$OUT\"'"))
        self.assertEqual(self._decision(
            "OUT=notes.md; OUT=build.log bash -c 'true'; echo delve > \"$OUT\""),
            "deny")
        scratch = self._scratch("agent-io")
        self.assertIsNone(self._decision(
            f"P=/repo; P={scratch} bash -c 'echo delve > \"$P/prompt.txt\"'"))
        self.assertEqual(self._decision(
            "OUT=build.log; OUT=notes.md bash -c 'echo delve > \"$OUT\"'"),
            "deny")

    # Round 2 (Codex): a PowerShell here-string is one quoted span.

    def test_powershell_here_string_is_data(self):
        scratch = os.path.join(tempfile.gettempdir(), "ir-probe", "agent-io")
        for command in (
            "@'\nReview the user's command:\necho delve > notes.md\n'@ | "
            f"Set-Content {scratch}/prompt.txt",
            "@'\nReview the user's command:\necho delve > notes.md\n'@",
            '@"\nfixture = "x"; echo delve > notes.md\n"@ | Set-Content fixture.py',
        ):
            with self.subTest(command=command):
                self.assertIsNone(self._decision(command, tool="PowerShell"))
        for quote in ("'", '"'):
            with self.subTest(quote=quote):
                self.assertEqual(self._decision(
                    f"@{quote}\nThe user's delve.\n{quote}@ | Set-Content notes.md",
                    tool="PowerShell"), "deny")

    def test_powershell_redirection_and_out_file_are_denied(self):
        self.assertEqual(self._decision('"delve" > notes.md', tool="PowerShell"), "deny")
        self.assertEqual(
            self._decision('"delve" | Out-File notes.md', tool="PowerShell"), "deny")


class CodexHookTests(unittest.TestCase):
    """`guard.py --agent codex`, the hook bootstrap registers with Codex.

    Codex sends its shell tool as `Bash` and its file edits as `apply_patch`
    with the raw patch, and it rejects an `ask` as invalid output and runs the
    call, so the approval gates deny there.
    """

    @staticmethod
    def _run(tool, command, env=None, cwd="/repo"):
        env_dict = dict(os.environ)
        env_dict.pop("AGENT_CONFIG_GATES", None)
        env_dict.pop("AGENT_STYLE_HOOK", None)
        env_dict.update(env or {})
        payload = {
            "hook_event_name": "PreToolUse", "turn_id": "t1", "cwd": cwd,
            "tool_name": tool, "tool_input": {"command": command},
        }
        result = subprocess.run(
            [sys.executable, str(GUARD), "--agent", "codex"],
            input=json.dumps(payload), capture_output=True, text=True,
            check=False, env=env_dict,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def _decision(self, tool, command, env=None, cwd="/repo"):
        resp = self._run(tool, command, env, cwd)
        return resp["hookSpecificOutput"]["permissionDecision"] if resp else None

    @staticmethod
    def _patch(*body):
        return "\n".join(("*** Begin Patch",) + body + ("*** End Patch",))

    def test_patch_adding_prose_is_denied(self):
        resp = self._run("apply_patch", self._patch(
            "*** Add File: notes.md", "+We delve into the results."))
        self.assertEqual(set(resp), {"hookSpecificOutput"})
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")
        reason = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(os.path.join("/repo", "notes.md"), reason)
        self.assertNotIn("~/.claude/settings.json", reason)

    def test_patch_reads_added_lines_only(self):
        self.assertEqual(self._decision("apply_patch", self._patch(
            "*** Update File: notes.md", "@@", " context", "+We delve here.")), "deny")
        self.assertIsNone(self._decision("apply_patch", self._patch(
            "*** Update File: notes.md", "@@", "-We delve here.", "+We look here.")))
        self.assertIsNone(self._decision("apply_patch", self._patch(
            "*** Delete File: notes.md")))

    def test_patch_fences_come_from_the_patched_file(self):
        # Round 1 (Codex): a line changed inside a fence is code, even when
        # the hunk does not show the fence. Round 2 (Codex): a hunk that
        # starts inside one block must not hide prose added before the next.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "notes.md").write_text(
                'Intro.\n\n```python\nfirst = 0\nvalue = "old"\nlast = 1\n```\n'
                "\n\n```\nnext = 2\n```\n")
            self.assertIsNone(self._decision("apply_patch", self._patch(
                "*** Update File: notes.md", "@@",
                '-value = "old"', '+value = "delve"'), cwd=tmp))
            self.assertEqual(self._decision("apply_patch", self._patch(
                "*** Update File: notes.md", "@@", " last = 1", " ```", " ",
                "+We delve here.", " ", " ```", " next = 2"), cwd=tmp), "deny")
        # Round 3 (Codex): the edit lands where apply_patch puts it, so an
        # end-of-file hunk and each matching pass pick the same line Codex does.
        cases = [
            ("```\nold\n```\n\nold\n", ("-old", "+We delve here.", "*** End of File"), "deny"),
            ("```\n old\n```\n\nold  \n", ("-old", "+We delve here."), "deny"),
            ('```\nvalue = “old”\n```\n',
             ('-value = "old"', '+value = "delve"'), None),
        ]
        for content, body, expected in cases:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "notes.md").write_text(content, encoding="utf-8")
                self.assertEqual(self._decision("apply_patch", self._patch(
                    "*** Update File: notes.md", "@@", *body), cwd=tmp), expected)
        # A file that cannot be read leaves the added lines to be judged alone.
        self.assertEqual(self._decision("apply_patch", self._patch(
            "*** Update File: notes.md", "@@", " ```python",
            '-value = "old"', '+value = "delve"', " ```")), "deny")

    def test_patch_scope_follows_the_write_gate(self):
        scratch = os.path.join(tempfile.gettempdir(), "ir-probe", "agent-io", "p.md")
        self.assertIsNone(self._decision("apply_patch", self._patch(
            "*** Add File: tool.py", "+# delve")))
        self.assertIsNone(self._decision("apply_patch", self._patch(
            f"*** Add File: {scratch}", "+delve")))
        self.assertEqual(self._decision("apply_patch", self._patch(
            "*** Update File: tool.py", "*** Move to: notes.md", "@@", "+delve")), "deny")

    def test_shell_writes_and_approval_gates(self):
        resp = self._run("Bash", "cat > notes.md <<'EOF'\ndelve\nEOF")
        reason = resp["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("with apply_patch", reason)
        self.assertNotIn("Edit tool", reason)
        self.assertIsNone(self._decision("Bash", "echo delve > build.log"))
        resp = self._run("Bash", "git push origin main")
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("`!`", resp["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self._decision("Bash", "git status"))

    def test_claude_only_gates_and_other_tools_pass(self):
        self.assertIsNone(self._decision("Bash", "cd /tmp && ls"))
        self.assertIsNone(self._decision("mcp__docs__write", "delve"))
        self.assertIsNone(self._decision("apply_patch", self._patch(
            "*** Add File: notes.md", "+delve"), env={"AGENT_STYLE_HOOK": "off"}))

    def test_claude_path_ignores_apply_patch(self):
        self.assertIsNone(run_guard_with_payload({
            "tool_name": "apply_patch",
            "tool_input": {"command": self._patch("*** Add File: notes.md", "+delve")},
        }))


class CodexHookRegistrationTests(unittest.TestCase):
    """scripts/merge_codex_hooks.py adds the guard group to a Codex home's
    hooks.json and leaves every other hook alone."""

    MERGE = ROOT / "scripts" / "merge_codex_hooks.py"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / ".codex"
        self.home.mkdir()
        self.hooks = self.home / "hooks.json"

    def _merge(self, home=None):
        result = subprocess.run(
            [sys.executable, str(self.MERGE), str(home or self.home)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout, result.stderr

    def test_registers_beside_existing_hooks_once(self):
        other = {"hooks": [{"type": "command", "command": "notify"}]}
        self.hooks.write_text(json.dumps({"hooks": {
            "Stop": [other], "PreToolUse": [dict(other, matcher="*")]}}))
        out, _ = self._merge()
        self.assertIn("registered", out)
        self.assertIn("Trust", out)
        data = json.loads(self.hooks.read_text())
        self.assertEqual(data["hooks"]["Stop"], [other])
        groups = data["hooks"]["PreToolUse"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[1]["matcher"], "Bash|apply_patch")
        self.assertIn("guard.py\" --agent codex", groups[1]["hooks"][0]["command"])
        before = self.hooks.read_bytes()
        out, _ = self._merge()
        self.assertNotIn("registered", out)
        self.assertEqual(self.hooks.read_bytes(), before)

    def _state(self, body):
        key = f'"{self.hooks}:pre_tool_use:0:0"'
        (self.home / "config.toml").write_text(f"[hooks.state.{key}]\n{body}\n")

    def test_trust_follows_the_recorded_hash(self):
        spec = importlib.util.spec_from_file_location("merge_codex_hooks", self.MERGE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._merge()
        self._state(f'trusted_hash = "{module.trust_hash(module.GROUP)}"')
        self.assertEqual(self._merge(), ("", ""))
        # Round 1 (Codex): a stale hash is not trust, and a disabled hook is
        # the user's choice.
        self._state('trusted_hash = "sha256:stale"')
        self.assertIn("Trust", self._merge()[0])
        self._state('enabled = false')
        self.assertEqual(self._merge(), ("", ""))
        # Round 1 (Agy): an undecodable config.toml still exits quietly.
        (self.home / "config.toml").write_bytes(b"\xff\xfe")
        self.assertIn("Trust", self._merge()[0])

    def test_shared_group_is_left_alone(self):
        self._merge()
        data = json.loads(self.hooks.read_text())
        data["hooks"]["PreToolUse"][0]["hooks"].append(
            {"type": "command", "command": "echo audit"})
        self.hooks.write_text(json.dumps(data))
        before = self.hooks.read_bytes()
        out, err = self._merge()
        self.assertEqual(self.hooks.read_bytes(), before)
        self.assertIn("left", err)

    @unittest.skipIf(os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
                     "needs POSIX permissions and a non-root user")
    def test_unwritable_home_warns(self):
        self.home.chmod(0o500)
        self.addCleanup(self.home.chmod, 0o700)
        out, err = self._merge()
        self.assertIn("could not write", err)
        self.assertFalse(self.hooks.exists())

    def test_changed_group_is_replaced_in_place(self):
        self._merge()
        data = json.loads(self.hooks.read_text())
        data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = 5
        self.hooks.write_text(json.dumps(data))
        out, _ = self._merge()
        self.assertIn("registered", out)
        groups = json.loads(self.hooks.read_text())["hooks"]["PreToolUse"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["hooks"][0]["timeout"], 30)

    def test_unreadable_file_and_missing_home_are_left_alone(self):
        self.hooks.write_text("{not json")
        out, err = self._merge()
        self.assertEqual(self.hooks.read_text(), "{not json")
        self.assertIn("left", err)
        missing = self.home.parent / "absent"
        self.assertEqual(self._merge(missing), ("", ""))
        self.assertFalse(missing.exists())


# Deliberately over both RULE-12 thresholds: thirty words, which the
# per-sentence rule of agent-style before 0.8.0 flags, and eighty, which the
# profile of 0.8.0 and later flags for a single sentence. Counted, not
# eyeballed: an earlier version of this fixture sat at twenty-nine words and
# silently tested nothing, which is the failure mode the paired clean-prose
# test above exists to catch. At 88 words it keeps firing whichever release
# of the package the machine carries.
_LONG_SENTENCE = (
    "This is a deliberately long sentence written purely so that it will "
    "comfortably exceed both the thirty word threshold that the per-sentence "
    "RULE-12 enforces and the eighty word threshold that the RULE-12 profile "
    "applies to a single sentence, and therefore reach the advisory code path "
    "that is under test in this particular case, whichever release of the rule "
    "pack happens to be installed on the machine that runs the suite, because a "
    "fixture that quietly stopped firing would leave every test built on it "
    "passing without exercising anything."
)


def _load_guard_module():
    """Import guard.py as a module so its helpers can be called directly."""
    spec = importlib.util.spec_from_file_location("guard_under_test", GUARD)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_fake_agent_style(root: Path) -> str:
    """Plant a stand-in `agent_style` and return a PYTHONPATH entry for it.

    The one-response contract belongs to guard.py's control flow, not to the
    detectors, so the test for it should not sit behind an optional
    third-party import. This fake reports one finding on any text, which is
    all that path needs, and it makes that test run on a machine with no
    agent-style installed. PYTHONPATH precedes site-packages, so it also
    shadows a real installation and keeps the test deterministic either way.
    """
    pkg = root / "agent_style" / "review"
    pkg.mkdir(parents=True)
    (root / "agent_style" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "detectors_mech.py").write_text(
        "class _V:\n"
        "    def __init__(self, detail):\n"
        "        self.detail = detail\n"
        "\n"
        "def _rule_12(text):\n"
        "    return [_V('sentence length 99 words (>30)')]\n"
        "\n"
        "def _nothing(text):\n"
        "    return []\n"
        "\n"
        "_rule_05 = _rule_06 = _rule_b = _rule_d = _rule_i = _nothing\n",
        encoding="utf-8",
    )
    return str(root)


_SIX_DETECTORS = (
    "_rule_05", "_rule_06", "_rule_12", "_rule_b", "_rule_d", "_rule_i",
)

# The six with the profile in the place of `_rule_12`, and the full set a
# 0.8.0 package is held to: the display rules and RULE-08 on top. A real 0.8.0
# package may still carry `_rule_12` (the CLI dispatches to it). Tests that
# need it add it to `detectors` themselves.
_PROFILE_DETECTORS = (
    "_rule_05", "_rule_06", "_rule_12_profile", "_rule_b", "_rule_d", "_rule_i",
)
_RELEASE_0_8_DETECTORS = _PROFILE_DETECTORS + (
    "_rule_03", "_rule_a", "_rule_08_repeats",
)

# Detectors the hook hands the file path to, and what each one echoes of it.
_FAKE_PATH_PARAMS = {
    "_rule_03": "path=",
    "_rule_a": "a-path=",
    "_rule_12_profile": "profile-path=",
    "_rule_08_repeats": "repeats-path=",
}


def _write_release_fake_agent_style(
    root: Path, *, version, detectors, raising=(), silent=(),
) -> str:
    """Plant a stand-in `agent_style` whose release and detector set are chosen.

    The advisory's rule set follows the package's `__version__`, so these tests
    need to say which release they are standing in for. Every detector reports
    exactly one finding, which makes "how many rules ran" readable from the
    advisory's own count: six findings mean six detectors ran, seven mean
    seven. `_rule_03`, `_rule_a`, `_rule_12_profile` and `_rule_08_repeats`
    echo the `file_path` they were given into their findings, so a test can
    see the path arrive. A detector named in `raising` raises instead of
    reporting, and one named in `silent` returns nothing, which lets a test
    read one rule's finding without the five-finding cap hiding it. Passing
    `version=None` plants no `__version__` at all.
    """
    pkg = root / "agent_style" / "review"
    pkg.mkdir(parents=True)
    init = "" if version is None else f"__version__ = {version!r}\n"
    (root / "agent_style" / "__init__.py").write_text(init, encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    source = [
        "class _V:\n"
        "    def __init__(self, detail):\n"
        "        self.detail = detail\n"
    ]
    for name in detectors:
        params = "text, file_path=''" if name in _FAKE_PATH_PARAMS else "text"
        if name in raising:
            body = "    raise RuntimeError('simulated detector failure')\n"
        elif name in silent:
            body = "    return []\n"
        elif name in _FAKE_PATH_PARAMS:
            body = f"    return [_V({_FAKE_PATH_PARAMS[name]!r} + file_path)]\n"
        else:
            body = f"    return [_V('{name} ran')]\n"
        source.append(f"\ndef {name}({params}):\n{body}")
    (pkg / "detectors_mech.py").write_text("".join(source), encoding="utf-8")
    return str(root)


def _fake_agent_style_modules(version, detectors):
    """In-process stand-in for the package, for tests that read the loader.

    Returns `(modules, functions)`: a mapping for `unittest.mock.patch.dict`
    over `sys.modules`, and the planted callables by name. A test uses them
    to ask whether the loader handed back the package's own function or a
    wrapper.
    """
    import types

    functions = {}
    for name in detectors:
        def make(label):
            if label in _FAKE_PATH_PARAMS:
                def fn(text, file_path=None):
                    return [f"{label} ran"]
            else:
                # Text only, as the real `_rule_12` is: handing it a path is
                # a TypeError, which the loader's callers must never provoke.
                def fn(text):
                    return [f"{label} ran"]
            fn.__name__ = label
            return fn
        functions[name] = make(name)
    mech = types.ModuleType("agent_style.review.detectors_mech")
    for name, fn in functions.items():
        setattr(mech, name, fn)
    review = types.ModuleType("agent_style.review")
    review.detectors_mech = mech
    package = types.ModuleType("agent_style")
    package.__version__ = version
    package.review = review
    modules = {
        "agent_style": package,
        "agent_style.review": review,
        "agent_style.review.detectors_mech": mech,
    }
    return modules, functions


class AgentStyleAdvisoryTests(unittest.TestCase):
    """agent-style's deterministic findings are reported, never blocking.

    The banned-word gate denies because each hit has a one-word substitution.
    These rules do not: RULE-12 fires on any sentence over thirty words, which
    is a judgement call in hand-written prose. Denying on it would make the
    hook something to switch off, so the contract under test is report the
    findings and set no permission decision.
    """

    def _require_agent_style(self) -> None:
        """Skip on a developer machine, fail in CI.

        These tests exist to exercise the advisory, and the advisory needs the
        package. Skipping when it is absent is right locally, where most
        checkouts have no reason to carry it. In CI the same skip turns the
        whole feature green while running none of it, which is how the gate
        this change repairs went unnoticed for a release line.

        Asserting on the import is what makes that load-bearing. An earlier
        version instead scanned `validate.yml` for the package name, and a
        mutation probe passed it by adding a commented-out install line: the
        workflow was broken and the test agreed with it.
        """
        try:
            from agent_style.review import detectors_mech  # noqa: F401
        except Exception as exc:
            if os.environ.get("CI"):
                self.fail(
                    f"agent_style must be importable in CI or these tests "
                    f"cover nothing; the workflow is expected to install it. "
                    f"Import failed with: {exc!r}"
                )
            self.skipTest("agent_style not importable in this environment")

    @staticmethod
    def _advice(resp) -> str:
        """The advisory text a response carries, or "" for a silent response.

        Reads the model-facing field. The paired channel test below is what
        keeps the human-facing one from being dropped.
        """
        if not resp:
            return ""
        return resp.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_long_sentence_is_reported_without_a_permission_decision(self) -> None:
        self._require_agent_style()
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/probe.md", "content": _LONG_SENTENCE},
        })
        advice = self._advice(resp)
        self.assertIn("[agent-style]", advice)
        self.assertIn("RULE-12", advice)
        self.assertIn("Advisory only", advice)
        self.assertNotIn(
            "permissionDecision", resp["hookSpecificOutput"],
            "an advisory must leave the permission flow alone; carrying a "
            "decision here would silently grant or block the write",
        )

    def test_the_advisory_reaches_both_readers(self) -> None:
        """Two fields, two readers, and neither is redundant.

        `additionalContext` is what reaches the model, the reader that can act
        on a finding while it drafts. `systemMessage` is what reaches the
        human. Probing Claude Code 2.1.229 found that writing to stderr and
        exiting 0 reached neither, so dropping either field here would put the
        advisory back in front of one audience only.
        """
        self._require_agent_style()
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/probe.md", "content": _LONG_SENTENCE},
        })
        self.assertIn("[agent-style]", resp.get("systemMessage", ""))
        self.assertEqual(
            resp["systemMessage"], self._advice(resp),
            "both readers should be told the same thing",
        )

    def test_clean_prose_is_silent(self) -> None:
        """No finding, no response. Paired with the test above so an advisory
        that fired unconditionally could not pass the suite."""
        self._require_agent_style()
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/probe.md", "content": "Short and clean.\n"},
        })
        self.assertIsNone(resp)

    def test_sentence_case_headings_are_not_reported(self) -> None:
        """RULE-G is deliberately out of the advisory's rule set.

        Measured over this repo it produced 40% of all findings and flagged
        the sentence-case headings the corpus writes on purpose, so it would
        crowd the cap with nothing to act on. Asserting the behaviour rather
        than the absence of a name means re-adding the rule fails here.
        """
        self._require_agent_style()
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/probe.md",
                "content": "## Non-goals\n\nShort and clean.\n\n### Empirical note\n\nAlso fine.\n",
            },
        })
        self.assertIsNone(resp, f"unexpected finding: {self._advice(resp)}")

    def test_code_files_are_not_scanned(self) -> None:
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/probe.py", "content": f"# {_LONG_SENTENCE}\n"},
        })
        self.assertIsNone(resp)

    def test_a_denied_write_emits_one_message_not_two(self) -> None:
        """The advisory runs only after the banned-word gate declines to deny.

        Content that trips both would otherwise produce a deny plus a separate
        advisory about the same write, which reads as two unrelated complaints.
        """
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/probe.md",
                "content": "This pivotal and " + _LONG_SENTENCE,
            },
        })
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "deny",
        )
        self.assertNotIn("[agent-style]", json.dumps(resp))

    def test_stdout_carries_one_json_document_when_the_banner_also_denies(self) -> None:
        """A pending banner plus an advisory-worthy write is one call, so it
        gets one response.

        This failed silently when first written. The advisory printed where it
        was computed, then the banner deny printed after it, and stdout held
        two concatenated JSON documents. A reader taking the first would act on
        an advisory and lose the deny, which is a gate rather than a note. The
        subprocess harness parses stdout, so it raises here rather than
        reporting a diff.

        Runs against the fake detector rather than the real package: this is
        guard.py's control flow, and it should not go untested on a machine
        that happens not to have agent-style installed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            consumer = Path(tmp) / "consumer"
            (consumer / ".agent-config").mkdir(parents=True)
            (consumer / ".agent-config" / "bootstrap.sh").write_text("", encoding="utf-8")
            (consumer / ".agent-config" / "session-event.json").write_text(
                json.dumps({"ts": 9999999999}), encoding="utf-8"
            )
            fake = _write_fake_agent_style(Path(tmp) / "fakepkg")
            resp = run_guard_with_payload(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": "/tmp/probe.md", "content": _LONG_SENTENCE},
                },
                cwd=str(consumer),
                env={"PYTHONPATH": fake},
            )
        self.assertEqual(
            resp["hookSpecificOutput"]["permissionDecision"], "deny",
            "the banner gate outranks the advisory; a blocked write gets no note",
        )
        self.assertNotIn("[agent-style]", json.dumps(resp))

    def test_style_hook_off_silences_the_advisory(self) -> None:
        """The advisory shares the writing-style escape hatch rather than
        introducing a second env var, which the literal-scan test would
        otherwise require registering."""
        resp = run_guard_with_payload(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/probe.md", "content": _LONG_SENTENCE},
            },
            env={"AGENT_STYLE_HOOK": "off"},
        )
        self.assertIsNone(resp)

    def test_exactly_the_documented_rules_for_the_installed_release_are_selected(
        self,
    ) -> None:
        """The rule list is a documented contract, so it is asserted as one.

        Every other test here checks behaviour, and behaviour cannot see the
        difference between six rules and one that happens to fire. The
        shipped guard-hook docs name the set. A release that renamed the private
        functions behind some of them should fail here, before it quietly
        narrows what the advisory covers. The set is six before
        agent-style 0.6.0, seven from it, eight from 0.7.0, and nine from 0.8.0,
        where RULE-12 stays in the set as the profile. CI installs the package
        unpinned, so the expectation is read from the installed release. It is
        parsed here independently of the guard's own parser on purpose.
        """
        self._require_agent_style()
        guard = _load_guard_module()
        import agent_style

        release = tuple(
            int(p) for p in agent_style.__version__.split(".")[:3]
        )
        expected = {"RULE-05", "RULE-06", "RULE-12", "RULE-B", "RULE-D", "RULE-I"}
        if release >= (0, 6, 0):
            expected.add("RULE-03")
        if release >= (0, 7, 0):
            expected.add("RULE-A")
        if release >= (0, 8, 0):
            expected.add("RULE-08")
        labels = {label for label, _ in guard._agent_style_mechanical()}
        self.assertEqual(labels, expected)

    def test_the_installed_0_8_0_detectors_report_through_the_advisory(self) -> None:
        """The real profile and RULE-08, called as the hook calls them.

        The fake releases above prove the wiring; this proves the wiring fits
        the package it was written for, which it can do only once a 0.8.0
        package is installed. Until then it skips. The text is synthetic: 50
        short sentences read as a clipped paper, and one caveat stated three
        times in different sections reads as a repeat. The assertions check
        the label and a count. Rewording a message therefore does not fail it.
        """
        self._require_agent_style()
        import agent_style

        release = tuple(
            int(p) for p in agent_style.__version__.split(".")[:3]
        )
        if release < (0, 8, 0):
            self.skipTest("the RULE-12 profile and RULE-08 ship with agent-style 0.8.0")
        guard = _load_guard_module()
        clipped = (
            "\\documentclass{article}\n\\begin{document}\n\\section{Method}\n"
            + "The model reads the input. It returns a label. " * 25
            + "\n\\end{document}\n"
        )
        advice = guard.advise_writing_style(
            "Write", {"file_path": "/tmp/paper.tex", "content": clipped})
        self.assertIn("RULE-12: ", advice)
        self.assertIn("50 sentences", advice)
        self.assertNotIn("_PROFILE", advice)

        caveat = (
            "The analysis does not establish whether the effect transfers to "
            "larger crystal lattices."
        )
        repeated = (
            "\\begin{document}\n"
            f"\\section{{Results}}\n{caveat} The gain is steady.\n\n"
            f"\\section{{Discussion}}\nAs before, {caveat[0].lower()}{caveat[1:]} "
            "Cost falls with batch size.\n\n"
            f"\\section{{Conclusion}}\nAgain, {caveat[0].lower()}{caveat[1:]}\n"
            "\\end{document}\n"
        )
        advice = guard.advise_writing_style(
            "Write", {"file_path": "/tmp/paper.tex", "content": repeated})
        self.assertIn("RULE-08: ", advice)
        self.assertIn("stated 3 times", advice)
        self.assertNotIn("_REPEATS", advice)

    def test_a_detector_that_raises_disables_the_whole_advisory(self) -> None:
        """Incompatibility that only shows up at call time gets the same
        treatment as incompatibility visible at load time.

        The loader's all-or-nothing check catches a renamed or removed
        function. It cannot catch a callable whose signature changed, whose
        return value is no longer iterable, or that fails on some particular
        input. Skipping the rule that raised and reporting the rest produces
        an advisory that looks complete and covers five rules, which is the
        state the loader refuses one layer up.
        """
        self._require_agent_style()
        guard = _load_guard_module()
        import agent_style
        from agent_style.review import detectors_mech

        release = tuple(
            int(p) for p in agent_style.__version__.split(".")[:3]
        )
        rule_12 = "_rule_12_profile" if release >= (0, 8, 0) else "_rule_12"

        def boom(*_args, **_kwargs):
            raise RuntimeError("simulated incompatible detector")

        # Three positions, because each catches a different way of getting
        # this wrong. RULE-12 is the only rule that fires on this content, so:
        # `_rule_05` raises before anything is collected, the RULE-12 function
        # removes the one finding-producing rule, and `_rule_i` raises after
        # RULE-12 has already contributed. Only the last distinguishes
        # discarding the collected findings from stopping at the failure and
        # reporting them, which is partial coverage wearing a complete
        # advisory's output. The RULE-12 function is the profile from 0.8.0.
        for broken in ("_rule_05", rule_12, "_rule_i"):
            with self.subTest(broken=broken):
                with unittest.mock.patch.object(detectors_mech, broken, boom):
                    self.assertIsNone(guard.advise_writing_style(
                        "Write",
                        {"file_path": "/tmp/probe.md", "content": _LONG_SENTENCE},
                    ))

    def test_an_incompatible_package_disables_the_whole_advisory(self) -> None:
        """Partial coverage is worse than none, because it looks the same.

        If a future agent-style keeps `_rule_12` and renames the rest, a
        subset loader would report one rule's findings under documentation
        promising six, and no output would reveal the gap. All or nothing.
        From 0.8.0 the RULE-12 function the hook needs is the profile, and
        that is the one removed when the installed release is that new.
        """
        self._require_agent_style()
        guard = _load_guard_module()
        import agent_style
        from agent_style.review import detectors_mech

        release = tuple(
            int(p) for p in agent_style.__version__.split(".")[:3]
        )
        rule_12 = "_rule_12_profile" if release >= (0, 8, 0) else "_rule_12"
        for missing in (rule_12, "_rule_i"):
            with self.subTest(missing=missing):
                with unittest.mock.patch.object(detectors_mech, missing, None):
                    self.assertIsNone(guard._agent_style_mechanical())
                    self.assertIsNone(guard.advise_writing_style(
                        "Write",
                        {"file_path": "/tmp/probe.md", "content": _LONG_SENTENCE},
                    ))

    def _advice_from_fake_release(
        self, *, version, detectors, raising=(), silent=(),
    ):
        """Run the real hook against a planted package; return its advisory.

        Goes through the subprocess harness with PYTHONPATH, the pattern the
        one-response test above uses. PYTHONPATH precedes site-packages, so the
        fake shadows a real installation and these tests do not depend on, or
        skip for the want of, the agent-style the machine happens to carry.
        """
        with tempfile.TemporaryDirectory() as tmp:
            fake = _write_release_fake_agent_style(
                Path(tmp) / "fakepkg",
                version=version, detectors=detectors, raising=raising,
                silent=silent,
            )
            resp = run_guard_with_payload(
                {
                    "tool_name": "Write",
                    "tool_input": {
                        "file_path": "/tmp/abstract.md",
                        "content": "Short and clean.\n",
                    },
                },
                env={"PYTHONPATH": fake},
            )
        return self._advice(resp)

    def test_a_release_before_0_6_0_runs_the_six_rule_baseline(self) -> None:
        """Six detectors present, release 0.4.2: six findings, no RULE-03.

        Pins the old-package half of the contract. The advisory has to keep
        working on a machine that has not upgraded agent-style, with the same
        six rules the documentation promised before RULE-03 existed.
        """
        advice = self._advice_from_fake_release(
            version="0.4.2", detectors=_SIX_DETECTORS,
        )
        self.assertIn("6 finding(s)", advice)
        self.assertNotIn("RULE-03", advice)

    def test_an_old_release_does_not_run_a_rule_03_it_happens_to_carry(self) -> None:
        """The release picks the set, not the presence of the function.

        A package older than 0.6.0 that exposes `_rule_03` anyway (a
        development build, an unrelated attribute with that name) still runs
        the six the documentation names. Probing for the function would run
        seven here, and the advertised set would stop matching what runs.
        """
        advice = self._advice_from_fake_release(
            version="0.5.9", detectors=_SIX_DETECTORS + ("_rule_03",),
        )
        self.assertIn("6 finding(s)", advice)
        self.assertNotIn("RULE-03", advice)

    def test_0_6_0_with_all_seven_rules_runs_seven_and_passes_the_path(self) -> None:
        """From 0.6.0 the set is the six plus RULE-03, and RULE-03 gets the path.

        The path is what lets the detector recognise an abstract or an
        introduction file. It must arrive as the tool call's `file_path`;
        dropping it, moving it into the text slot, or cutting it to a
        basename fails here. RULE-03 is listed first, so it is inside the advisory's cap.
        """
        advice = self._advice_from_fake_release(
            version="0.6.0", detectors=_SIX_DETECTORS + ("_rule_03",),
        )
        self.assertIn("7 finding(s)", advice)
        self.assertIn("RULE-03: path=/tmp/abstract.md", advice)

    def test_the_release_is_compared_numerically(self) -> None:
        """0.10.0 is newer than 0.6.0, though it sorts before it as a string.

        Both halves matter: with the nine functions of the 0.8.0 set present
        it runs nine, and with `_rule_08_repeats` missing the advisory is off.
        A string comparison reads 0.10.0 as old, runs six in both cases, and
        fails each assertion.
        """
        advice = self._advice_from_fake_release(
            version="0.10.0", detectors=_RELEASE_0_8_DETECTORS,
        )
        self.assertIn("9 finding(s)", advice)
        self.assertEqual(
            self._advice_from_fake_release(
                version="0.10.0",
                detectors=tuple(
                    d for d in _RELEASE_0_8_DETECTORS if d != "_rule_08_repeats"
                ),
            ),
            "",
            "0.10.0 without _rule_08_repeats is incompatible and must disable "
            "the advisory",
        )

    def test_0_6_0_without_rule_03_disables_the_whole_advisory(self) -> None:
        """A release that promises seven rules and carries six is incompatible.

        Falling back to the six would produce an advisory that looks complete
        and covers one rule fewer than the documentation says. All or nothing,
        and the seven-rule set is the one a 0.6.0 package is held to. The six
        detectors present here would each report, so silence means the loader
        refused the package and not that nothing fired.
        """
        advice = self._advice_from_fake_release(
            version="0.6.0", detectors=_SIX_DETECTORS,
        )
        self.assertEqual(advice, "")

    def test_a_0_6_release_does_not_run_a_rule_a_it_happens_to_carry(self) -> None:
        """Between 0.6.0 and 0.7.0 the set is seven, whatever else is exposed."""
        advice = self._advice_from_fake_release(
            version="0.6.5", detectors=_SIX_DETECTORS + ("_rule_03", "_rule_a"),
        )
        self.assertIn("7 finding(s)", advice)
        self.assertNotIn("RULE-A", advice)

    def test_0_7_0_runs_eight_and_passes_the_path_to_rule_a(self) -> None:
        """From 0.7.0 RULE-A joins, takes the path, and sits inside the cap.

        The path is how the detector tells a LaTeX source and an appendix
        apart, so it must arrive as the tool call's `file_path`. RULE-A runs
        second, after RULE-03, so both display rules show before RULE-12 can
        fill the cap.
        """
        advice = self._advice_from_fake_release(
            version="0.7.0", detectors=_SIX_DETECTORS + ("_rule_03", "_rule_a"),
        )
        self.assertIn("8 finding(s)", advice)
        self.assertIn("RULE-03: path=/tmp/abstract.md", advice)
        self.assertIn("RULE-A: a-path=/tmp/abstract.md", advice)

    def test_0_7_0_without_rule_a_disables_the_whole_advisory(self) -> None:
        """A release that promises eight rules and carries seven is incompatible."""
        advice = self._advice_from_fake_release(
            version="0.7.0", detectors=_SIX_DETECTORS + ("_rule_03",),
        )
        self.assertEqual(advice, "")

    def test_a_raising_rule_a_disables_the_whole_advisory(self) -> None:
        """RULE-A is held to the same all-or-nothing contract as the rest."""
        advice = self._advice_from_fake_release(
            version="0.7.0", detectors=_SIX_DETECTORS + ("_rule_03", "_rule_a"),
            raising=("_rule_a",),
        )
        self.assertEqual(advice, "")

    @staticmethod
    def _silent_except(detectors, *loud):
        """Every detector in `detectors` that is not named in `loud`."""
        return tuple(d for d in detectors if d not in loud)

    def test_0_8_0_runs_the_profile_in_place_of_rule_12(self) -> None:
        """From 0.8.0 RULE-12 is the profile, and `_rule_12` does not run.

        The package here still carries `_rule_12`, as a real 0.8.0 does for
        the CLI. Both would report if both ran. A count of one finding with
        the profile's text therefore shows the swap. The profile takes the path, and the
        label stays RULE-12.
        """
        detectors = _RELEASE_0_8_DETECTORS + ("_rule_12",)
        advice = self._advice_from_fake_release(
            version="0.8.0", detectors=detectors,
            silent=self._silent_except(detectors, "_rule_12_profile", "_rule_12"),
        )
        self.assertIn("1 finding(s)", advice)
        self.assertIn("RULE-12: profile-path=/tmp/abstract.md", advice)
        self.assertNotIn("_rule_12 ran", advice)

    def test_a_release_before_0_8_0_keeps_the_per_sentence_rule_12(self) -> None:
        """The release picks the set, not the presence of the function.

        A 0.7.9 package that carries the profile and RULE-08 anyway runs the
        eight rules the 0.7.0 documentation names: `_rule_12` reports, and
        neither new function is called. `_rule_12` here takes the text alone,
        as the real one does. A path handed to it would raise and switch
        the advisory off. Silence would then fail the first assertion.
        """
        detectors = (
            _SIX_DETECTORS + ("_rule_03", "_rule_a")
            + ("_rule_12_profile", "_rule_08_repeats")
        )
        advice = self._advice_from_fake_release(
            version="0.7.9", detectors=detectors,
            silent=self._silent_except(
                detectors, "_rule_12", "_rule_12_profile", "_rule_08_repeats"),
        )
        self.assertIn("1 finding(s)", advice)
        self.assertIn("RULE-12: _rule_12 ran", advice)
        self.assertNotIn("profile-path", advice)
        self.assertNotIn("RULE-08", advice)

    def test_0_8_0_labels_are_spelled_out_not_derived(self) -> None:
        """`_rule_12_profile` is RULE-12 and `_rule_08_repeats` is RULE-08.

        Deriving the label from the function name gives `RULE-12_PROFILE` and
        `RULE-08_REPEATS`, which no rule pack uses and which the audit would
        list as rules of their own.
        """
        advice = self._advice_from_fake_release(
            version="0.8.0", detectors=_RELEASE_0_8_DETECTORS,
            silent=self._silent_except(
                _RELEASE_0_8_DETECTORS, "_rule_12_profile", "_rule_08_repeats"),
        )
        self.assertIn("2 finding(s)", advice)
        self.assertIn("RULE-12: profile-path=/tmp/abstract.md", advice)
        self.assertIn("RULE-08: repeats-path=/tmp/abstract.md", advice)
        self.assertNotIn("_PROFILE", advice)
        self.assertNotIn("_REPEATS", advice)

    def test_0_8_0_keeps_the_display_rules_first_and_rule_08_after_them(self) -> None:
        """The five-finding cap shows RULE-03, RULE-A, RULE-08, then the baseline.

        All nine detectors report. Four findings are therefore withheld. The
        first five are the display rules and RULE-08, then RULE-05 and
        RULE-06, and the profile sits in RULE-12's old place below the cap.
        This set also works without `_rule_12`, which 0.8.0 no longer
        requires.
        """
        advice = self._advice_from_fake_release(
            version="0.8.0", detectors=_RELEASE_0_8_DETECTORS,
        )
        self.assertIn("9 finding(s)", advice)
        self.assertIn("(+4 more)", advice)
        order = [
            advice.index(label)
            for label in ("RULE-03:", "RULE-A:", "RULE-08:", "RULE-05:", "RULE-06:")
        ]
        self.assertEqual(order, sorted(order), advice)
        self.assertNotIn("RULE-12", advice)

    def test_0_8_0_without_the_profile_or_rule_08_disables_the_whole_advisory(self) -> None:
        """A release that promises nine rules and carries eight is incompatible.

        The first case is the one a fallback would hide: a package that kept
        `_rule_12` and lost the profile must not run the old rule under the
        documentation of the new one. The other detectors would all report.
        Silence therefore means the loader refused the package.
        """
        for missing in ("_rule_12_profile", "_rule_08_repeats"):
            with self.subTest(missing=missing):
                detectors = tuple(
                    d for d in _RELEASE_0_8_DETECTORS + ("_rule_12",)
                    if d != missing
                )
                self.assertEqual(
                    self._advice_from_fake_release(
                        version="0.8.0", detectors=detectors),
                    "",
                )

    def test_a_raising_profile_or_rule_08_disables_the_whole_advisory(self) -> None:
        """The two new rules are held to the all-or-nothing contract at call time."""
        for broken in ("_rule_12_profile", "_rule_08_repeats"):
            with self.subTest(broken=broken):
                self.assertEqual(
                    self._advice_from_fake_release(
                        version="0.8.0", detectors=_RELEASE_0_8_DETECTORS,
                        raising=(broken,)),
                    "",
                )

    def test_the_loader_hands_back_the_profile_itself_and_a_path_tolerant_rule_12(
        self,
    ) -> None:
        """RULE-12 is one label for two functions, and the loader reconciles them.

        `_PATH_AWARE_RULES` is keyed by label and therefore lists RULE-12. The
        profile takes the path and is returned as the package defines it,
        which the audit relies on to recognise it. Before 0.8.0 the same label
        is the text-only per-sentence rule, returned behind an adapter that
        accepts and drops the path.
        """
        guard = _load_guard_module()
        self.assertEqual(
            guard._PATH_AWARE_RULES,
            frozenset({"RULE-03", "RULE-A", "RULE-08", "RULE-12"}),
        )
        every = _RELEASE_0_8_DETECTORS + ("_rule_12",)

        modules, fns = _fake_agent_style_modules("0.8.0", every)
        with unittest.mock.patch.dict(sys.modules, modules):
            rules = guard._agent_style_mechanical()
        self.assertEqual(
            [label for label, _ in rules],
            ["RULE-03", "RULE-A", "RULE-08", "RULE-05", "RULE-06", "RULE-12",
             "RULE-B", "RULE-D", "RULE-I"],
        )
        self.assertIs(dict(rules)["RULE-12"], fns["_rule_12_profile"])
        self.assertIs(dict(rules)["RULE-08"], fns["_rule_08_repeats"])

        modules, fns = _fake_agent_style_modules("0.7.0", every)
        with unittest.mock.patch.dict(sys.modules, modules):
            rules = guard._agent_style_mechanical()
        self.assertEqual(
            [label for label, _ in rules],
            ["RULE-03", "RULE-A", "RULE-05", "RULE-06", "RULE-12", "RULE-B",
             "RULE-D", "RULE-I"],
        )
        legacy = dict(rules)["RULE-12"]
        self.assertIsNot(legacy, fns["_rule_12"])
        self.assertEqual(legacy("text", file_path="/tmp/x.md"), ["_rule_12 ran"])

    def test_the_summary_marks_departures_and_lets_a_good_sentence_stay(self) -> None:
        """The advisory's own line says what a finding is, in a few words.

        Humans break any single default now and then. A finding marks where
        the text leaves typical human prose and does not call it wrong. The
        closing sentence, which says the hook did not block the write, stays.
        """
        advice = self._advice_from_fake_release(
            version="0.7.0", detectors=_SIX_DETECTORS + ("_rule_03", "_rule_a"),
        )
        self.assertIn("8 finding(s)", advice)
        self.assertRegex(
            advice,
            r"These mark departures from typical human prose; one that reads "
            r"well can stay\. Advisory only; this hook did not block the write\.$",
        )

    def test_an_unreadable_version_is_treated_as_an_older_release(self) -> None:
        """Missing and unparsable versions both run the six-rule baseline.

        Older is the safe direction: the baseline is what every supported
        package carries. Reading an unparsable version as new would demand a
        seventh rule from a package that may lack it and switch the advisory
        off. The first case also covers the existing fake, whose package
        carries no version at all.
        """
        for version in (None, "not-a-version", ""):
            with self.subTest(version=version):
                advice = self._advice_from_fake_release(
                    version=version, detectors=_SIX_DETECTORS,
                )
                self.assertIn("6 finding(s)", advice)
                self.assertNotIn("RULE-03", advice)

    def test_a_raising_rule_03_disables_the_whole_advisory(self) -> None:
        """RULE-03 is held to the same contract as the other six.

        The six would each report here, so an advisory that skipped the failed
        detector and kept the rest would print six findings. Nothing printing
        means the failure was treated as a broken package.
        """
        advice = self._advice_from_fake_release(
            version="0.6.0", detectors=_SIX_DETECTORS + ("_rule_03",),
            raising=("_rule_03",),
        )
        self.assertEqual(advice, "")

    def test_the_workflow_installs_what_ci_will_require(self) -> None:
        """Fail here, before the push, rather than in CI.

        `_require_agent_style` is the guarantee: it turns a missing package
        into a CI failure instead of a skip. This is the earlier and clearer
        signal for the same problem, so a maintainer who drops the install
        learns about it from a local run.

        The workflow is read through a YAML parser and only active `run`
        values are inspected. Two weaker versions failed a mutation probe
        first: a whole-file search was satisfied by the explanatory comment,
        and a line scanner was satisfied by a commented-out install sitting
        beside a broken command.
        """
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not available to parse the workflow")
        workflow = ROOT / ".github" / "workflows" / "validate.yml"
        parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        installed = set()
        for job in parsed.get("jobs", {}).values():
            for step in job.get("steps", []) or []:
                run = step.get("run") if isinstance(step, dict) else None
                if not run:
                    continue
                for line in run.splitlines():
                    _, sep, args = line.partition("pip install")
                    if sep:
                        installed.update(
                            a for a in args.split() if not a.startswith("-")
                        )
        self.assertIn(
            "agent-style", installed,
            f"{workflow.name} must install agent-style, or _require_agent_style "
            f"fails every advisory test in CI. Packages installed by active "
            f"run steps: {sorted(installed)}",
        )

    def test_missing_agent_style_degrades_silently(self) -> None:
        """This hook runs in every repository, most of which have no reason to
        carry the package. An unimportable `agent_style` must cost nothing and
        block nothing.

        Exercised against the helper directly rather than through the
        subprocess harness, because the import has to be made to fail
        deterministically regardless of what is installed on the machine
        running the suite.
        """
        spec = importlib.util.spec_from_file_location("guard_under_test", GUARD)
        guard = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = guard
        spec.loader.exec_module(guard)

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name.startswith("agent_style"):
                raise ImportError("blocked by test")
            return real_import(name, *args, **kwargs)

        with unittest.mock.patch.object(builtins, "__import__", blocked):
            self.assertIsNone(guard._agent_style_mechanical())
            self.assertIsNone(guard.advise_writing_style(
                "Write",
                {"file_path": "/tmp/probe.md", "content": _LONG_SENTENCE},
            ))

    def test_the_advisory_is_capped(self) -> None:
        """A wall of findings is one the reader learns to skip.

        A rule that reports each hit can run to hundreds on a large CHANGELOG.
        The line is therefore capped and says how many it withheld. This test
        writes a contraction repeated twelve times: RULE-I reports every one in every
        release. Long sentences would not do, because from 0.8.0 the RULE-12
        profile folds them into a single finding.
        """
        self._require_agent_style()
        many = "\n\n".join("We don't claim more here." for _ in range(12))
        resp = run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/probe.md", "content": many},
        })
        advice = self._advice(resp)
        self.assertIn("[agent-style]", advice)
        self.assertIn("12 finding(s)", advice)
        self.assertIn("more)", advice, "a truncated list must say what it withheld")
        self.assertEqual(
            advice.count("RULE-I:"), 5,
            "exactly the cap should be listed",
        )

    def test_the_profile_folds_long_sentences_into_one_finding(self) -> None:
        """From 0.8.0 twelve long sentences are one RULE-12 finding, not twelve.

        This is why the cap test above does not use them. One finding per
        document keeps a paper full of long sentences from crowding the share
        and `, so` findings out of the five the hook shows. Skips until a
        0.8.0 package is installed.
        """
        self._require_agent_style()
        import agent_style

        release = tuple(int(p) for p in agent_style.__version__.split(".")[:3])
        if release < (0, 8, 0):
            self.skipTest("the RULE-12 profile ships with agent-style 0.8.0")
        guard = _load_guard_module()
        many = "\n\n".join(_LONG_SENTENCE for _ in range(12))
        advice = guard.advise_writing_style(
            "Write", {"file_path": "/tmp/probe.md", "content": many})
        self.assertIn("1 finding(s)", advice)
        self.assertIn("12 sentences over 80 words", advice)


class StyleCheckLineNumberTests(unittest.TestCase):
    """The text a detector reads keeps the line numbers of the text written.

    The advisory strips fenced code, inline code and LaTeX verbatim before it
    runs the detectors, so that examples quoted in a style guide produce no
    findings. A stripped span used to collapse to one space. Every detector
    that reports where it found something, RULE-08 most visibly ("also on
    lines 9, 13"), then reported lines counted from a file with the fences
    deleted, and the author looked at the wrong place.
    """

    @staticmethod
    def _marked_lines(text, marker="CAVEAT"):
        return [n for n, line in enumerate(text.split("\n"), 1) if marker in line]

    def test_a_stripped_markdown_fence_keeps_every_later_line_number(self) -> None:
        guard = _load_guard_module()
        fence = "```python\n" + "x = 1\n" * 8 + "```"
        text = f"# Notes\n\n{fence}\n\nCAVEAT one.\n\nCAVEAT two.\n"
        out = guard._content_for_style_check(text, ".md")
        self.assertNotIn("x = 1", out)
        self.assertEqual(out.count("\n"), text.count("\n"))
        self.assertEqual(self._marked_lines(out), self._marked_lines(text))

    def test_every_stripped_form_keeps_its_newlines(self) -> None:
        guard = _load_guard_module()
        cases = {
            ".md": [
                "a\n~~~\nline\nline\n~~~\nCAVEAT\n",
                "a ``tick ` inside`` b\nCAVEAT\n",
                "a `code` b `more` c\nCAVEAT\n",
                "````\n```\nnested\n```\n````\nCAVEAT\n",
            ],
            ".rst": ["a\n```\nline\n```\nCAVEAT\n"],
            ".tex": [
                "a\n\\begin{verbatim}\nl1\nl2\nl3\n\\end{verbatim}\nCAVEAT\n",
                "a \\verb|x| b\nCAVEAT\n",
                "a \\texttt{split\nacross\nlines} b\nCAVEAT\n",
            ],
        }
        for ext, texts in cases.items():
            for text in texts:
                with self.subTest(ext=ext, text=text):
                    out = guard._content_for_style_check(text, ext)
                    self.assertEqual(out.count("\n"), text.count("\n"))
                    self.assertEqual(
                        self._marked_lines(out), self._marked_lines(text))

    def test_an_inline_span_is_still_one_space(self) -> None:
        """Nothing changes for a span without a newline, which is most of them."""
        guard = _load_guard_module()
        self.assertEqual(
            guard._content_for_style_check("a `code` b", ".md"), "a   b")
        self.assertEqual(
            guard._content_for_style_check("a \\verb|x| b", ".tex"), "a   b")

    def test_the_banned_word_scan_still_skips_the_stripped_span(self) -> None:
        """The gate shares the stripping and must still skip the stripped code."""
        guard = _load_guard_module()
        text = "Intro.\n```\nThis is pivotal.\n```\nOutro.\n"
        self.assertIsNone(guard.check_writing_style(
            "Write", {"file_path": "/tmp/notes.md", "content": text}))
        self.assertIsNotNone(guard.check_writing_style(
            "Write", {"file_path": "/tmp/notes.md", "content": text.replace(
                "```", "")}))

    def _lines_a_detector_sees(self, path, content):
        """Run the real advisory loop against a detector that reports lines.

        The planted RULE-08 reads the text it was handed and names the lines
        that carry the marker. Whatever it names is what an author would be
        told. The test compares it with the lines of the file as written.
        """
        import types

        guard = _load_guard_module()
        modules, fns = _fake_agent_style_modules("0.8.0", _RELEASE_0_8_DETECTORS)

        def repeats(text, file_path=None):
            seen = [n for n, line in enumerate(text.split("\n"), 1) if "CAVEAT" in line]
            return [types.SimpleNamespace(detail=f"caveat on lines {seen}")]

        modules["agent_style.review.detectors_mech"]._rule_08_repeats = repeats
        with unittest.mock.patch.dict(sys.modules, modules):
            advice = guard.advise_writing_style(
                "Write", {"file_path": path, "content": content})
        found = [part for part in advice.split("; ") if "caveat on lines" in part]
        self.assertEqual(len(found), 1, advice)
        return found[0].split("caveat on lines ")[1].split("]")[0] + "]"

    def test_three_caveats_after_a_fence_are_reported_on_their_own_lines(self) -> None:
        fence = "```text\n" + "log line\n" * 8 + "```"
        text = (
            "# Notes\n\n" + fence + "\n\n"
            "CAVEAT the first.\n\nFiller sentence here.\n\n"
            "CAVEAT the second.\n\nMore filler here.\n\n"
            "CAVEAT the third.\n"
        )
        expected = self._marked_lines(text)
        self.assertEqual(expected, [14, 18, 22])
        self.assertEqual(
            self._lines_a_detector_sees("/tmp/notes.md", text), str(expected))

    def test_three_caveats_after_a_verbatim_block_keep_their_lines(self) -> None:
        verbatim = "\\begin{verbatim}\n" + "log line\n" * 6 + "\\end{verbatim}"
        text = (
            "\\section{Results}\n" + verbatim + "\n"
            "CAVEAT the first.\n\nFiller sentence here.\n\n"
            "CAVEAT the second.\n\nCAVEAT the third.\n"
        )
        expected = self._marked_lines(text)
        self.assertEqual(
            self._lines_a_detector_sees("/tmp/paper.tex", text), str(expected))

    def test_the_real_rule_08_names_the_true_lines_after_a_fence(self) -> None:
        """The failure as reported, with the real detector. Skips before 0.8.0."""
        AgentStyleAdvisoryTests._require_agent_style(self)
        import agent_style

        release = tuple(int(p) for p in agent_style.__version__.split(".")[:3])
        if release < (0, 8, 0):
            self.skipTest("RULE-08 ships with agent-style 0.8.0")
        guard = _load_guard_module()
        caveat = (
            "The analysis does not establish whether the effect "
            "transfers to larger crystal lattices."
        )
        fence = "```text\n" + "log line\n" * 8 + "```"
        text = (
            "# Notes\n\n" + fence + "\n\n"
            f"{caveat} The gain is steady.\n\n"
            "The cost falls with batch size.\n\n"
            f"As before, {caveat[0].lower()}{caveat[1:]} Cost falls.\n\n"
            "The runs finished on time.\n\n"
            f"Again, {caveat[0].lower()}{caveat[1:]}\n"
        )
        expected = [n for n, line in enumerate(text.split("\n"), 1)
                    if "analysis does not establish" in line.lower()]
        self.assertEqual(len(expected), 3)
        advice = guard.advise_writing_style(
            "Write", {"file_path": "/tmp/notes.md", "content": text})
        self.assertIn("RULE-08", advice)
        match = re.search(r"also on lines (\d+), (\d+)", advice)
        self.assertIsNotNone(match, advice)
        self.assertEqual([int(match.group(1)), int(match.group(2))], expected[1:])


def load_guard_module():
    """Import guard.py for the predicates that are cheaper to test directly."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_guard_direct", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AgentIoScopeTests(unittest.TestCase):
    """A caller can declare that a file holds text it is carrying.

    The writing-style guards pick their scope by extension, and extension
    does not separate prose an agent is writing from text an agent is moving.
    A scratch directory holds a dispatch prompt beside a draft proposal
    section. Measured across 34 local session transcripts, 23% of
    prose-extension writes landed in a scratch directory, and the most
    frequent names there were the review loop's own `ir-prompt-r1.txt` and
    `ir-round1.txt`. Findings on those cannot be acted on. A prompt is an
    instruction to another agent, so rewriting it changes what was asked, and
    a captured round output is another agent's words.

    So the writer declares it, by putting the file under an `agent-io`
    directory. The two guards then trust that marker to different depths, and
    the split is the point of this class. The advisory honors it anywhere,
    because a wrong exemption there costs one message. The deny gate honors
    it only under a temp root, because a marker trusted anywhere would be a
    one-token bypass of the banned-word check on real prose.

    Every exemption is paired with a case that must stay covered, since a
    marker that swallowed its neighbours would otherwise pass.
    """

    @staticmethod
    def _scratch(*parts):
        """A path under the real temp root, which is the trust boundary."""
        return os.path.join(tempfile.gettempdir(), "ir-probe", *parts)

    @staticmethod
    def _decision(resp):
        if not resp:
            return None
        return resp.get("hookSpecificOutput", {}).get("permissionDecision")

    def _write(self, path, content="This result was pivotal."):
        return run_guard_with_payload({
            "tool_name": "Write",
            "tool_input": {"file_path": path, "content": content},
        })

    def test_the_deny_gate_skips_a_declared_scratch_path(self) -> None:
        resp = self._write(self._scratch("agent-io", "round1.md"))
        self.assertIsNone(resp)

    def test_the_deny_gate_still_covers_a_declared_repo_path(self) -> None:
        """The bypass this closes.

        Without the temp-root condition an agent could route any prose
        through `repo/agent-io/` and skip the banned-word denial entirely.
        The marker is a declaration about carried text, and carried text does
        not live in the repository.
        """
        resp = self._write("/repo/agent-io/proposal.md")
        self.assertEqual(self._decision(resp), "deny")

    def test_the_deny_gate_still_covers_the_scratch_sibling(self) -> None:
        resp = self._write(self._scratch("round1.md"))
        self.assertEqual(self._decision(resp), "deny",
                         "only the declared directory is exempt")

    def test_the_advisory_skips_a_declared_path_anywhere(self) -> None:
        """The advisory is the guard the noise complaint was about, so its
        exemption stays broad. It reports rather than blocks, so a wrong
        exemption costs a message rather than a write."""
        AgentStyleAdvisoryTests._require_agent_style(self)
        resp = self._write("/repo/agent-io/round1.md", _LONG_SENTENCE)
        self.assertIsNone(resp)

    def test_the_advisory_skips_a_declared_scratch_path(self) -> None:
        AgentStyleAdvisoryTests._require_agent_style(self)
        resp = self._write(self._scratch("agent-io", "ir-round1.txt"),
                           _LONG_SENTENCE)
        self.assertIsNone(resp)

    def test_the_advisory_still_covers_the_sibling(self) -> None:
        AgentStyleAdvisoryTests._require_agent_style(self)
        resp = self._write(self._scratch("ir-round1.txt"), _LONG_SENTENCE)
        self.assertIn("[agent-style]", AgentStyleAdvisoryTests._advice(resp))

    def test_the_marker_is_a_directory_not_a_file_name(self) -> None:
        """`agent-io.md` is a document about the convention rather than one
        covered by it. A substring match would exempt the documentation."""
        resp = self._write("/repo/docs/agent-io.md")
        self.assertEqual(self._decision(resp), "deny")

    def test_backslash_separators_and_case_are_recognised(self) -> None:
        """Two separate claims, tested separately.

        The earlier version built a native path and called `.replace("/",
        os.sep)`, which is a no-op on POSIX, so it only ever exercised
        backslashes on the platform that already used them. The marker split
        is checked here against a literal backslash string on every platform;
        scratch-root recognition is checked by the cases above using a native
        path.
        """
        guard = load_guard_module()
        self.assertTrue(guard.is_agent_io_path(r"C:\Users\x\Temp\Agent-IO\r.md"))
        self.assertTrue(guard.is_agent_io_path("/var/tmp/AGENT-IO/r.md"))
        self.assertFalse(guard.is_agent_io_path(r"C:\Users\x\Temp\agentio\r.md"))

    def test_a_repository_below_a_temp_root_is_not_scratch(self) -> None:
        """The Round 2 reopen.

        CI routinely checks a repository out below the system temp
        directory. A lexical prefix test would trust `agent-io` there and
        hand any prose a bypass of the deny gate.
        """
        guard = load_guard_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "ci-checkout")
            os.makedirs(os.path.join(repo, "agent-io"))
            subprocess.run(["git", "init", "-q", repo], capture_output=True)
            target = os.path.join(repo, "agent-io", "proposal.md")
            self.assertFalse(guard.is_scratch_path(target))
            self.assertEqual(
                guard.prose_extension(target, agent_io_requires_scratch=True),
                ".md", "the deny gate must still cover repository prose")
            # The same tree without a .git is ordinary scratch.
            plain = os.path.join(tmp, "agent-io", "round1.md")
            os.makedirs(os.path.dirname(plain), exist_ok=True)
            self.assertTrue(guard.is_scratch_path(plain))

    def test_a_symlink_out_of_temp_is_not_scratch(self) -> None:
        """`abspath` does not resolve links, so a lexical check can be
        satisfied by a link that lands inside a repository."""
        guard = load_guard_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "outside-repo")
            os.makedirs(os.path.join(repo, "agent-io"))
            subprocess.run(["git", "init", "-q", repo], capture_output=True)
            real = os.path.join(repo, "agent-io", "proposal.md")
            with open(real, "w", encoding="utf-8") as fh:
                fh.write("x\n")
            link_dir = os.path.join(tempfile.gettempdir(), "ir-link-probe")
            link = os.path.join(link_dir, "agent-io", "proposal.md")
            os.makedirs(os.path.dirname(link), exist_ok=True)
            if os.path.lexists(link):
                os.remove(link)
            try:
                os.symlink(real, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable to this account")
            try:
                self.assertFalse(guard.is_scratch_path(link))
            finally:
                os.remove(link)

    def test_both_guards_read_the_same_predicate(self) -> None:
        """AGENTS.md says the two guards share a gate because they scan the
        same writes. They now differ in how far they trust the marker, and
        that difference belongs in an argument rather than in a second copy
        of the extension test, which is what this scans for.
        """
        source = GUARD.read_text(encoding="utf-8")
        self.assertEqual(
            source.count("not in PROSE_EXTENSIONS"), 1,
            "the membership test belongs to prose_extension() alone; a second "
            "copy lets the deny gate and the advisory drift apart",
        )
        self.assertEqual(
            source.count("ext = prose_extension(file_path"), 2,
            "both guards should reach scope through the shared predicate",
        )
        self.assertEqual(
            source.count("agent_io_requires_scratch=True"), 3,
            "only the callers that deny should take the narrower trust: the "
            "Write gate, the shell-write gate, and the Codex patch gate",
        )


class HookReachabilityTests(unittest.TestCase):
    """Every tool guard.py gates must be wired to it in `user/settings.json`.

    This closes a gap that shipped the writing-style gate dead. `PreToolUse`
    listed `Bash` and `PowerShell` only, while `check_writing_style` acts on
    `Write` / `Edit` / `MultiEdit` alone, so a write carrying a banned word was
    denied in the suite and sailed through in a real session. Everything else
    about that feature was correct: the gate, its tests, its documentation, and
    the escape hatch. Nothing connected them to a running agent, and nothing
    noticed for the length of a release line.

    The probes below are behavioural rather than a list of tool names copied
    from guard.py, since a copied list agrees with itself no matter what the
    hook does. Each probe asserts the decision it expects before checking
    coverage, so a probe that stops triggering fails here instead of quietly
    testing nothing.
    """

    PROBES = (
        ("Write", {"file_path": "/tmp/probe.md", "content": "A pivotal idea.\n"}, "deny"),
        ("Edit", {"file_path": "/tmp/probe.md", "new_string": "A pivotal idea.\n"}, "deny"),
        ("MultiEdit", {"file_path": "/tmp/probe.md",
                       "edits": [{"new_string": "A pivotal idea.\n"}]}, "deny"),
        ("Bash", {"command": "git push origin main"}, "ask"),
        ("PowerShell", {"command": "Remove-Item -Recurse -Force C:\\tmp\\x"}, "ask"),
    )

    @staticmethod
    def _matchers():
        settings = json.loads(
            (ROOT / "user" / "settings.json").read_text(encoding="utf-8")
        )
        return [
            entry.get("matcher", "")
            for entry in settings.get("hooks", {}).get("PreToolUse", [])
            if "guard.py" in json.dumps(entry.get("hooks", []))
        ]

    def test_every_gated_tool_is_wired_to_the_hook(self) -> None:
        # `fullmatch` is an approximation of Claude Code's matcher handling,
        # not a model of it: a matcher like `Write|Edit|MultiEdit` is read as
        # a list of exact tool names, and other supported spellings
        # (comma-separated lists, unanchored patterns) would not survive this
        # check. The approximation errs toward reporting a live matcher as
        # uncovered, never the reverse, so it cannot pass while a gated tool
        # is genuinely unreachable. Widen it if a matcher spelling that this
        # repo actually ships starts failing here.
        import re as _re

        matchers = self._matchers()
        self.assertTrue(matchers, "no PreToolUse entry invokes guard.py")
        for tool, tool_input, expected in self.PROBES:
            with self.subTest(tool=tool):
                resp = run_guard_with_payload(
                    {"tool_name": tool, "tool_input": tool_input}
                )
                self.assertIsNotNone(
                    resp, f"probe for {tool} no longer triggers a gate"
                )
                self.assertEqual(
                    resp["hookSpecificOutput"]["permissionDecision"], expected,
                    f"probe for {tool} no longer produces {expected}",
                )
                covered = any(
                    m in ("", "*") or _re.fullmatch(m, tool)
                    for m in matchers
                )
                self.assertTrue(
                    covered,
                    f"guard.py gates {tool} but no PreToolUse matcher in "
                    f"user/settings.json selects it, so the gate cannot fire "
                    f"in a real session. Matchers present: {matchers}",
                )


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

    def test_first_arm_completes_with_read_and_the_ack_write_alone(self):
        """The Session Start Check needs no shell on the first arm. With an
        event pending and no report (or a stale one), the agent Reads
        .agent-config/banner.txt, decides on the fallback, and Writes the
        exact acknowledgement; nothing else is needed before work resumes.
        A Bash repair attempt in between is denied, which is the point: the
        fallback must be reachable without one."""
        ts = 1789685280.5
        self._write_event(ts)
        report = str(self.agent_dir / "banner.txt")
        ack = str(self.agent_dir / "banner-emitted.json")
        # Step 1: reading the report is allowed while the gate is armed,
        # whether or not the file exists.
        self.assertIsNone(self._run({"tool_name": "Read", "tool_input": {"file_path": report}}))
        # A shell repair is not part of the sequence and stays denied.
        resp = self._run({"tool_name": "Bash", "tool_input": {"command": "bash .agent-config/bootstrap.sh"}})
        self.assertEqual(resp["hookSpecificOutput"]["permissionDecision"], "deny")
        # Step 2: the exact acknowledgement write is allowed.
        self.assertIsNone(self._run({
            "tool_name": "Write",
            "tool_input": {"file_path": ack, "content": json.dumps({"ts": ts})},
        }))
        self._write_emitted(ts)
        # Step 3: with the acknowledgement current, ordinary work resumes.
        self.assertIsNone(self._run({"tool_name": "Bash", "tool_input": {"command": "git status"}}))

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
