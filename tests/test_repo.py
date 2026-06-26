from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "AGENTS.md"
README = ROOT / "README.md"
GITIGNORE = ROOT / ".gitignore"
GITATTRIBUTES = ROOT / ".gitattributes"
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"
SKILLS_DIR = ROOT / "skills"
POINTER_DIR = ROOT / ".claude" / "commands"
CLAUDE_SETTINGS = ROOT / ".claude" / "settings.json"
BOOTSTRAP_DIR = ROOT / "bootstrap"
SCRIPTS_DIR = ROOT / "scripts"
USER_DIR = ROOT / "user"
CLAUDE_MD = ROOT / "CLAUDE.md"
CODEX_MD = ROOT / "agents" / "codex.md"
GENERATOR_SCRIPT = SCRIPTS_DIR / "generate_agent_configs.py"
SESSION_HOOK_SCRIPT = SCRIPTS_DIR / "session_bootstrap.py"

SHIPPED_SKILLS = {"implement-review", "my-router", "ci-mockup-figure", "prun", "readme-polish"}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def skill_dirs() -> list[Path]:
    return sorted(path for path in SKILLS_DIR.iterdir() if path.is_dir())


def extract_fenced_block(text: str, language: str) -> str:
    match = re.search(rf"```{language}\n(.*?)\n```", text, re.DOTALL)
    if not match:
        raise AssertionError(f"Missing ```{language} code block in AGENTS.md")
    return match.group(1)


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


class RepoValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.agents_text = read_text(AGENTS)
        cls.readme_text = read_text(README)
        cls.gitignore_text = read_text(GITIGNORE)
        cls.gitattributes_text = read_text(GITATTRIBUTES)
        cls.workflow_text = read_text(WORKFLOW)
        cls.bootstrap_powershell_text = read_text(BOOTSTRAP_DIR / "bootstrap.ps1")
        cls.bootstrap_bash_text = read_text(BOOTSTRAP_DIR / "bootstrap.sh")
        cls.skills = skill_dirs()
        cls.powershell_bootstrap = extract_fenced_block(cls.agents_text, "powershell")
        cls.bash_bootstrap = extract_fenced_block(cls.agents_text, "bash")

    def assert_command_ok(
        self, result: subprocess.CompletedProcess[str], context: str
    ) -> None:
        if result.returncode != 0:
            self.fail(
                f"{context} failed with exit code {result.returncode}\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )

    def tracked_files(self) -> set[str]:
        result = run_command(["git", "ls-files"], ROOT)
        self.assert_command_ok(result, "git ls-files")
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def create_remote_repo_snapshot(self, base_dir: Path) -> Path:
        remote_dir = base_dir / "remote-src"
        remote_dir.mkdir()
        shutil.copy2(AGENTS, remote_dir / "AGENTS.md")
        shutil.copytree(BOOTSTRAP_DIR, remote_dir / "bootstrap")
        shutil.copytree(SKILLS_DIR, remote_dir / "skills")
        shutil.copytree(POINTER_DIR, remote_dir / ".claude" / "commands")
        shutil.copy2(CLAUDE_SETTINGS, remote_dir / ".claude" / "settings.json")
        if SCRIPTS_DIR.exists():
            shutil.copytree(SCRIPTS_DIR, remote_dir / "scripts")
        if USER_DIR.exists():
            shutil.copytree(USER_DIR, remote_dir / "user")
        self.localize_remote_bootstrap_scripts(remote_dir)

        init = run_command(["git", "init"], remote_dir)
        self.assert_command_ok(init, "git init in remote snapshot")

        branch = run_command(["git", "checkout", "-b", "main"], remote_dir)
        self.assert_command_ok(branch, "git checkout -b main in remote snapshot")

        email = run_command(
            ["git", "config", "user.email", "repo-validation@example.com"], remote_dir
        )
        self.assert_command_ok(email, "git config user.email in remote snapshot")

        name = run_command(
            ["git", "config", "user.name", "Repo Validation"], remote_dir
        )
        self.assert_command_ok(name, "git config user.name in remote snapshot")

        add = run_command(["git", "add", "."], remote_dir)
        self.assert_command_ok(add, "git add in remote snapshot")

        commit = run_command(["git", "commit", "-m", "snapshot"], remote_dir)
        self.assert_command_ok(commit, "git commit in remote snapshot")
        return remote_dir

    def localize_remote_bootstrap_scripts(self, remote_dir: Path) -> None:
        agents_copy = str((remote_dir / "AGENTS.md")).replace("'", "''")
        remote_uri = remote_dir.as_uri()

        powershell_script = remote_dir / "bootstrap" / "bootstrap.ps1"
        powershell_text = read_text(powershell_script).replace(
            'Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/$Upstream/main/AGENTS.md" -OutFile .agent-config/AGENTS.md',
            f"Copy-Item -LiteralPath '{agents_copy}' -Destination .agent-config/AGENTS.md",
        )
        powershell_script.write_text(
            powershell_text.replace(
                '$RepoUrl = "https://github.com/$Upstream.git"',
                f"$RepoUrl = '{remote_uri}'",
            ),
            encoding="utf-8",
        )

        bash_script = remote_dir / "bootstrap" / "bootstrap.sh"
        bash_text = read_text(bash_script).replace(
            'curl -sfL "https://raw.githubusercontent.com/$UPSTREAM/main/AGENTS.md" -o .agent-config/AGENTS.md',
            f"cp {shlex.quote((remote_dir / 'AGENTS.md').as_posix())} .agent-config/AGENTS.md",
        )
        bash_script.write_text(
            bash_text.replace(
                'REPO_URL="https://github.com/$UPSTREAM.git"',
                f"REPO_URL='{remote_uri}'",
            ),
            encoding="utf-8",
        )

    def prepare_project_dir(self, base_dir: Path) -> Path:
        project_dir = base_dir / "project"
        commands_dir = project_dir / ".claude" / "commands"
        commands_dir.mkdir(parents=True)
        (commands_dir / "local-only.md").write_text("local-only\n", encoding="utf-8")
        for pointer_file in POINTER_DIR.glob("*.md"):
            (commands_dir / pointer_file.name).write_text(
                "stale-pointer\n", encoding="utf-8"
            )
        (project_dir / "AGENTS.md").write_text("stale-root-agents\n", encoding="utf-8")
        (project_dir / "AGENTS.local.md").write_text("## Local Rules\n- keep me\n", encoding="utf-8")
        (project_dir / ".gitignore").write_text("node_modules/\n/.agent-config/\n", encoding="utf-8")
        # Opt out of default-on rule-pack composition so this smoke test's
        # byte-identical upstream-AGENTS assertion still holds. Rule-pack
        # composition has dedicated tests in test_compose_rule_packs.
        (project_dir / "agent-config.yaml").write_text("rule_packs: []\n", encoding="utf-8")
        return project_dir

    def verify_bootstrap_result(self, project_dir: Path) -> None:
        fetched_agents = project_dir / ".agent-config" / "AGENTS.md"
        local_only_pointer = project_dir / ".claude" / "commands" / "local-only.md"
        project_settings = project_dir / ".claude" / "settings.json"

        self.assertTrue(fetched_agents.exists(), "Expected fetched AGENTS.md")
        self.assertIn("## User Profile", read_text(fetched_agents))
        self.assertEqual(read_text(project_dir / "AGENTS.md"), read_text(AGENTS))
        self.assertEqual(read_text(project_dir / "AGENTS.local.md"), "## Local Rules\n- keep me\n")
        for skill_dir in self.skills:
            cloned_skill = (
                project_dir
                / ".agent-config"
                / "repo"
                / "skills"
                / skill_dir.name
                / "SKILL.md"
            )
            self.assertTrue(cloned_skill.exists(), f"Expected cloned skill: {skill_dir.name}")
        for pointer_file in POINTER_DIR.glob("*.md"):
            cloned_pointer = (
                project_dir
                / ".agent-config"
                / "repo"
                / ".claude"
                / "commands"
                / pointer_file.name
            )
            project_pointer = project_dir / ".claude" / "commands" / pointer_file.name
            self.assertTrue(cloned_pointer.exists(), f"Expected cloned pointer: {pointer_file.name}")
            self.assertEqual(read_text(project_pointer), read_text(pointer_file))
        self.assertEqual(read_text(local_only_pointer), "local-only\n")
        self.assertTrue(project_settings.exists(), "Expected Claude settings")
        project_data = json.loads(read_text(project_settings))
        shared_data = json.loads(read_text(CLAUDE_SETTINGS))
        for key, value in shared_data.items():
            if isinstance(value, dict):
                for sk, sv in value.items():
                    self.assertIn(sk, project_data.get(key, {}), f"shared nested key '{key}.{sk}'")
                    if isinstance(sv, list):
                        for item in sv:
                            self.assertIn(item, project_data[key][sk], f"shared list item '{item}' in '{key}.{sk}'")
                    else:
                        self.assertEqual(project_data[key][sk], sv, f"shared key '{key}.{sk}'")
            else:
                self.assertEqual(project_data.get(key), value, f"shared key '{key}'")
        self.assertEqual(
            project_data.get("projectOnlyKey"), "keep-me",
            "project-only key should survive merge",
        )
        self.assertIn(
            "Bash(project-only:*)",
            project_data.get("permissions", {}).get("allow", []),
            "project-only permission should survive deep merge",
        )

        cloned_guard = (
            project_dir / ".agent-config" / "repo" / "scripts" / "guard.py"
        )
        cloned_user_settings = (
            project_dir / ".agent-config" / "repo" / "user" / "settings.json"
        )
        self.assertTrue(
            cloned_guard.exists(),
            "Expected guard.py in cloned repo scripts/",
        )
        self.assertTrue(
            cloned_user_settings.exists(),
            "Expected settings.json in cloned repo user/",
        )

        gitignore_text = (project_dir / ".gitignore").read_text(encoding="utf-8")
        agent_config_lines = [
            line for line in gitignore_text.splitlines()
            if line.rstrip("/").endswith(".agent-config")
        ]
        self.assertEqual(
            len(agent_config_lines), 1,
            f"Expected exactly one .agent-config/ ignore entry, got: {agent_config_lines}",
        )

    def render_powershell_smoke_script(self, remote_dir: Path) -> str:
        bootstrap_copy = str((remote_dir / "bootstrap" / "bootstrap.ps1")).replace("'", "''")
        return self.powershell_bootstrap.replace(
            "Invoke-WebRequest -UseBasicParsing -Uri https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/bootstrap/bootstrap.ps1 -OutFile .agent-config/bootstrap.ps1",
            f"Copy-Item -LiteralPath '{bootstrap_copy}' -Destination .agent-config/bootstrap.ps1",
        )

    def render_bash_smoke_script(self, remote_dir: Path) -> str:
        bootstrap_copy = shlex.quote((remote_dir / "bootstrap" / "bootstrap.sh").as_posix())
        return self.bash_bootstrap.replace(
            "curl -sfL https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/bootstrap/bootstrap.sh -o .agent-config/bootstrap.sh",
            f"cp {bootstrap_copy} .agent-config/bootstrap.sh",
        )

    def test_shipped_skill_set_matches_expectation(self) -> None:
        skill_names = {path.name for path in self.skills}
        self.assertEqual(
            skill_names, SHIPPED_SKILLS,
            "Unexpected shipped skill set. Update SHIPPED_SKILLS and the public docs together.",
        )

    def test_agents_has_fetched_copy_guard(self) -> None:
        # The top-of-file note must distinguish source-repo behavior from
        # consumer-repo behavior using the file-existence markers and
        # an imperative consumer-path instruction.
        self.assertIn("**Source repo test:**", self.agents_text)
        self.assertIn("`bootstrap/bootstrap.sh`", self.agents_text)
        self.assertIn("`bootstrap/bootstrap.ps1`", self.agents_text)
        self.assertIn("`skills/`", self.agents_text)
        self.assertIn(
            "proceed directly to `## Session Start Check`",
            self.agents_text,
        )
        self.assertIn("**Consumer repo path:**", self.agents_text)
        self.assertIn("You MUST execute", self.agents_text)
        self.assertIn("idempotent", self.agents_text)

    def test_agents_bootstrap_covers_windows_and_unix_shells(self) -> None:
        required_fragments = [
            "PowerShell (Windows):",
            "```powershell",
            "Invoke-WebRequest -UseBasicParsing -Uri https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/bootstrap/bootstrap.ps1 -OutFile .agent-config/bootstrap.ps1",
            "& .\\.agent-config\\bootstrap.ps1",
            "Bash (macOS/Linux):",
            "```bash",
            "curl -sfL https://raw.githubusercontent.com/yzhao062/anywhere-agents/main/bootstrap/bootstrap.sh -o .agent-config/bootstrap.sh",
            "bash .agent-config/bootstrap.sh",
            "refreshes the consuming repo's root `AGENTS.md`",
            "AGENTS.local.md",
            ".claude/settings.json",
            "effortLevel",
        ]
        for fragment in required_fragments:
            self.assertIn(fragment, self.agents_text)

    def test_bootstrap_scripts_cover_sync_steps(self) -> None:
        required_fragments = [
            "Invoke-WebRequest -UseBasicParsing -Uri \"https://raw.githubusercontent.com/$Upstream/main/AGENTS.md\" -OutFile .agent-config/AGENTS.md",
            "Copy-Item .agent-config/AGENTS.md AGENTS.md -Force",
            "git clone --depth 1 --filter=blob:none --sparse $RepoUrl .agent-config/repo",
            "git -C .agent-config/repo sparse-checkout set skills .claude scripts user",
            "Copy-Item .agent-config/repo/.claude/commands/*.md .claude/commands/ -Force",
            "Copy-Item .agent-config/repo/.claude/settings.json .claude/settings.json -Force",
            "ConvertFrom-Json",
            "Add-Member",
            "Add-Content -Path .gitignore -Value \"`n.agent-config/\"",
            "curl -sfL \"https://raw.githubusercontent.com/$UPSTREAM/main/AGENTS.md\" -o .agent-config/AGENTS.md",
            "cp -f .agent-config/AGENTS.md AGENTS.md",
            "cp -f .agent-config/repo/.claude/commands/*.md .claude/commands/",
            "cp -f .agent-config/repo/.claude/settings.json .claude/settings.json",
            "dict.fromkeys",
            "Merge-Json",
            "git -C .agent-config/repo sparse-checkout set skills .claude scripts user",
            "echo '.agent-config/' >> .gitignore",
        ]
        bootstrap_text = self.bootstrap_powershell_text + "\n" + self.bootstrap_bash_text
        for fragment in required_fragments:
            self.assertIn(fragment, bootstrap_text)

    def test_bootstrap_does_not_modify_global_git_config(self) -> None:
        # Regression: bootstrap must not reach outside the consuming repo. .gitattributes
        # handles line endings locally; user-level Git config is off-limits.
        bootstrap_text = self.bootstrap_powershell_text + "\n" + self.bootstrap_bash_text
        self.assertNotIn("git config --global", bootstrap_text)

    def test_agents_declares_non_destructive_claude_sync(self) -> None:
        self.assertIn(
            "does not delete unrelated project-local commands",
            self.agents_text,
        )
        self.assertIn(
            "should not delete unrelated project-local commands",
            self.agents_text,
        )

    def test_gitignore_excludes_local_state(self) -> None:
        for entry in (
            ".agent-config/",
            "/.claude/settings.local.json",
            "/.idea/",
            "__pycache__/",
        ):
            self.assertIn(entry, self.gitignore_text)

    def test_gitattributes_enforces_lf_for_text_files(self) -> None:
        self.assertIn("* text=auto eol=lf", self.gitattributes_text)

    def test_tracked_files_exclude_local_state_paths(self) -> None:
        tracked = self.tracked_files()
        self.assertNotIn(".claude/settings.local.json", tracked)
        self.assertFalse(any(path.startswith(".idea/") for path in tracked))

    def test_readme_mentions_shipped_skills(self) -> None:
        for skill in SHIPPED_SKILLS:
            self.assertIn(f"`{skill}`", self.readme_text)

    def test_each_skill_has_core_files(self) -> None:
        for skill_dir in self.skills:
            skill_name = skill_dir.name
            self.assertTrue((skill_dir / "SKILL.md").exists(), skill_name)
            self.assertTrue((skill_dir / "agents" / "openai.yaml").exists(), skill_name)
            self.assertTrue((POINTER_DIR / f"{skill_name}.md").exists(), skill_name)

    def test_shared_claude_settings_file_is_tracked(self) -> None:
        tracked = self.tracked_files()
        self.assertIn(".claude/settings.json", tracked)

    def test_max_effort_is_set_via_env_var_in_user_settings(self) -> None:
        tracked = self.tracked_files()
        self.assertIn("user/settings.json", tracked)
        user_settings = json.loads(
            read_text(ROOT / "user" / "settings.json")
        )
        self.assertEqual(
            user_settings.get("env", {}).get("CLAUDE_CODE_EFFORT_LEVEL"),
            "max",
        )
        claude_settings = json.loads(read_text(CLAUDE_SETTINGS))
        self.assertNotIn("effortLevel", claude_settings)

    def test_skill_core_files_are_tracked(self) -> None:
        tracked = self.tracked_files()
        for skill_dir in self.skills:
            skill_name = skill_dir.name
            self.assertIn(f"skills/{skill_name}/SKILL.md", tracked, skill_name)
            self.assertIn(
                f"skills/{skill_name}/agents/openai.yaml", tracked, skill_name
            )
            self.assertIn(f".claude/commands/{skill_name}.md", tracked, skill_name)

    def test_pointer_files_match_skill_directories(self) -> None:
        skill_names = {path.name for path in self.skills}
        pointer_names = {path.stem for path in POINTER_DIR.glob("*.md")}
        self.assertEqual(skill_names, pointer_names)

    def test_pointer_files_reference_matching_skill(self) -> None:
        for pointer_file in POINTER_DIR.glob("*.md"):
            skill_name = pointer_file.stem
            pointer_text = read_text(pointer_file)
            local_path = f"skills/{skill_name}/SKILL.md"
            fallback_path = f".agent-config/repo/skills/{skill_name}/SKILL.md"
            self.assertIn(local_path, pointer_text, pointer_file.name)
            self.assertIn(fallback_path, pointer_text, pointer_file.name)
            local_pos = pointer_text.index(local_path)
            fallback_pos = pointer_text.index(fallback_path)
            self.assertLess(
                local_pos, fallback_pos,
                f"{pointer_file.name}: local path must appear before fallback path",
            )

    def test_openai_wrappers_reference_matching_skill(self) -> None:
        for skill_dir in self.skills:
            wrapper_text = read_text(skill_dir / "agents" / "openai.yaml")
            self.assertIn("display_name:", wrapper_text, skill_dir.name)
            self.assertIn(f"${skill_dir.name}", wrapper_text, skill_dir.name)

    def test_skill_markdown_links_resolve(self) -> None:
        link_pattern = re.compile(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)")
        for skill_dir in self.skills:
            text = read_text(skill_dir / "SKILL.md")
            for raw_target in link_pattern.findall(text):
                if "://" in raw_target or raw_target.startswith("#"):
                    continue
                linked_path = (skill_dir / raw_target).resolve()
                self.assertTrue(
                    linked_path.exists(),
                    f"{skill_dir.name} links to missing file: {raw_target}",
                )

    def test_generator_and_session_hook_scripts_tracked(self) -> None:
        tracked = self.tracked_files()
        self.assertIn("scripts/generate_agent_configs.py", tracked)
        self.assertIn("scripts/session_bootstrap.py", tracked)
        self.assertTrue(GENERATOR_SCRIPT.exists())
        self.assertTrue(SESSION_HOOK_SCRIPT.exists())

    def test_pre_push_hook_tracked_and_wired(self) -> None:
        tracked = self.tracked_files()
        self.assertIn(".githooks/pre-push", tracked)
        hook_path = ROOT / ".githooks" / "pre-push"
        self.assertTrue(hook_path.exists())
        body = read_text(hook_path)
        self.assertIn("scripts/pre-push-smoke.sh", body,
                      "pre-push hook must invoke pre-push-smoke.sh (current checkout), not remote-smoke.sh (published package)")
        self.assertIn("AGENTS.md", body,
                      "pre-push hook must gate on AGENTS.md changes")
        self.assertIn("--no-verify", body,
                      "pre-push hook must document the bypass path")

    def test_remote_smoke_script_tracked(self) -> None:
        tracked = self.tracked_files()
        self.assertIn("scripts/remote-smoke.sh", tracked)
        script = ROOT / "scripts" / "remote-smoke.sh"
        self.assertTrue(script.exists())
        body = read_text(script)
        self.assertIn("EXPECTED_SKILLS", body)
        self.assertIn("claude -p", body,
                      "remote-smoke must exercise claude -p (single-turn)")
        self.assertIn("codex exec", body,
                      "remote-smoke must exercise codex exec (single-turn)")

    def test_pre_push_smoke_script_tracked(self) -> None:
        tracked = self.tracked_files()
        self.assertIn("scripts/pre-push-smoke.sh", tracked)
        script = ROOT / "scripts" / "pre-push-smoke.sh"
        self.assertTrue(script.exists())
        body = read_text(script)
        self.assertIn("git rev-parse --show-toplevel", body,
                      "pre-push-smoke must locate the repo root of the CURRENT checkout")
        self.assertIn("generate_agent_configs.py", body,
                      "pre-push-smoke must run the generator for determinism check")
        self.assertIn("claude -p", body,
                      "pre-push-smoke must exercise claude -p (current checkout)")
        self.assertIn("codex exec", body,
                      "pre-push-smoke must exercise codex exec (current checkout)")

    def test_generated_per_agent_files_tracked_with_marker(self) -> None:
        tracked = self.tracked_files()
        self.assertIn("CLAUDE.md", tracked)
        self.assertIn("agents/codex.md", tracked)
        self.assertIn("GENERATED FILE", read_text(CLAUDE_MD))
        self.assertIn("GENERATED FILE", read_text(CODEX_MD))

    def test_agents_md_has_configuration_precedence_section(self) -> None:
        self.assertIn("Configuration Precedence", self.agents_text)

    def test_agents_md_has_agent_scope_tags(self) -> None:
        self.assertIn("<!-- agent:claude -->", self.agents_text)
        self.assertIn("<!-- /agent:claude -->", self.agents_text)
        self.assertIn("<!-- agent:codex -->", self.agents_text)
        self.assertIn("<!-- /agent:codex -->", self.agents_text)

    def test_bootstrap_scripts_run_generator_and_deploy_session_hook(self) -> None:
        both = self.bootstrap_bash_text + "\n" + self.bootstrap_powershell_text
        self.assertIn("generate_agent_configs.py", both,
                      "bootstrap scripts must invoke the generator")
        self.assertIn("session_bootstrap.py", both,
                      "bootstrap scripts must deploy session_bootstrap.py")

    def test_bootstrap_scripts_auto_update_codex_npm_cli(self) -> None:
        both = self.bootstrap_bash_text + "\n" + self.bootstrap_powershell_text
        required_fragments = [
            "ANYWHERE_AGENTS_CODEX_AUTO_UPDATE",
            "npm outdated -g @openai/codex --json",
            "npm install -g @openai/codex@latest",
            "Do not let a local npm or registry problem block project bootstrap.",
        ]
        for fragment in required_fragments:
            self.assertIn(fragment, both)

    def test_user_settings_wires_session_start_hook(self) -> None:
        user_settings = json.loads(read_text(ROOT / "user" / "settings.json"))
        session_hooks = (
            user_settings.get("hooks", {}).get("SessionStart", [])
        )
        self.assertTrue(session_hooks, "user/settings.json must declare a SessionStart hook entry")
        flattened = json.dumps(session_hooks)
        self.assertIn("session_bootstrap.py", flattened,
                      "SessionStart entry must invoke ~/.claude/hooks/session_bootstrap.py")

    def test_github_actions_runs_validation_on_windows_and_ubuntu(self) -> None:
        required_fragments = [
            "name: Validate",
            "pull_request:",
            "ubuntu-latest",
            "windows-latest",
            "actions/checkout@v6",
            "actions/setup-python@v6",
            "python -B -m unittest discover -s tests -p \"test_*.py\" -v",
        ]
        for fragment in required_fragments:
            self.assertIn(fragment, self.workflow_text)

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows-only PowerShell smoke test")
    def test_powershell_bootstrap_smoke_test(self) -> None:
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            self.skipTest("PowerShell is not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            remote_dir = self.create_remote_repo_snapshot(base_dir)
            project_dir = self.prepare_project_dir(base_dir)
            script = self.render_powershell_smoke_script(remote_dir)

            first_run = run_command(
                [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                project_dir,
            )
            self.assert_command_ok(first_run, "first PowerShell bootstrap run")

            ps = project_dir / ".claude" / "settings.json"
            data = json.loads(read_text(ps))
            data["projectOnlyKey"] = "keep-me"
            data.setdefault("permissions", {}).setdefault("allow", []).append("Bash(project-only:*)")
            ps.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

            second_run = run_command(
                [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                project_dir,
            )
            self.assert_command_ok(second_run, "second PowerShell bootstrap run")

            self.verify_bootstrap_result(project_dir)

    @unittest.skipIf(sys.platform.startswith("win"), "Unix-only bash smoke test")
    def test_bash_bootstrap_smoke_test(self) -> None:
        shell = shutil.which("bash")
        if not shell:
            self.skipTest("bash is not available")

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            remote_dir = self.create_remote_repo_snapshot(base_dir)
            project_dir = self.prepare_project_dir(base_dir)
            script = self.render_bash_smoke_script(remote_dir)

            first_run = run_command([shell, "-lc", script], project_dir)
            self.assert_command_ok(first_run, "first bash bootstrap run")

            ps = project_dir / ".claude" / "settings.json"
            data = json.loads(read_text(ps))
            data["projectOnlyKey"] = "keep-me"
            data.setdefault("permissions", {}).setdefault("allow", []).append("Bash(project-only:*)")
            ps.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

            second_run = run_command([shell, "-lc", script], project_dir)
            self.assert_command_ok(second_run, "second bash bootstrap run")

            self.verify_bootstrap_result(project_dir)


if __name__ == "__main__":
    unittest.main()
