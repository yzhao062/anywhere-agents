"""
Claude Code PreToolUse hook guard. Codex runs it as `guard.py --agent codex`,
a narrower path described in the Codex block before main().

Dispatches by tool_name. Shared checks:

0. Auto-allow shipped trusted scripts — the implement-review skill's
   shipped PowerShell helpers (auto-watch.ps1 / health-check.ps1 /
   dispatch-codex.ps1) return permissionDecision: allow when invoked via
   the call-operator (`& '<path>' [args]`) on the PowerShell tool.
   Path-tail anchored to the shipped scripts so unrelated files on disk
   with the same names fall through to the normal permission flow.
   Independent of AGENT_CONFIG_GATES because that escape hatch disables
   deny-style gates only; a permission allow is always safe to honor.
1. Writing-style gate — Write/Edit/MultiEdit on prose files (.md/.tex/.rst/.txt)
   is denied when the outgoing content contains a banned AI-tell word from
   AGENTS.md Writing Defaults. A Bash or PowerShell command that writes a
   prose file (redirection, tee, in-place sed or perl, a PowerShell content
   cmdlet) is denied when its whole text carries one; see
   check_shell_prose_write(). Skips code files, and skips a path a caller
   marked as agent I/O when that path is under a temp root (see
   AGENT_IO_SEGMENT). The advisory in 1b honors the same marker anywhere.
1b. agent-style advisory — Write/Edit/MultiEdit writes (not shell writes) are
   additionally scanned by the
   agent-style package's mechanical detectors when it is importable, and any
   findings are reported to both the model and the user while the write
   proceeds. Runs only when step 1 did not deny, so a blocked write yields one
   message rather than two. See advise_writing_style() for why this reports
   instead of denying, and make_advisory_response() for how it reaches each
   reader.
2. Banner gate — every tool call except the exempt observation tools
   (Read/Grep/Glob/Skill/Task/TodoWrite/BashOutput/WebFetch/WebSearch/ToolSearch/LS/NotebookRead)
   and Write/Edit/MultiEdit whose target is exactly
   <consumer_root>/.agent-config/banner-emitted.json (the ack file) is denied
   when a SessionStart event is pending but the banner has not been emitted
   for it. Flag files are per-project under <consumer_root>/.agent-config/
   where consumer_root is found by walking up from os.getcwd() until a dir
   with .agent-config/bootstrap.{sh,ps1} is located. Source repos (no
   .agent-config/) and unrelated directories are not gated.
3. Compound cd commands (cd path && cmd, cd path; cmd) → deny  [Bash only]
4. Mandatory risk classification → ask  [Bash + PowerShell]
   - destructive git (push, commit, merge, reset --hard, branch -D, etc.)
   - destructive / publish gh (pr create|merge|close, repo delete, release ...)
   - outward publishes (npm publish, twine upload, python -m twine upload)
   - irreversible file/device deletes (rm -rf, dd, mkfs, shred; PowerShell
     Remove-Item and recursive aliases when -Recurse / -r / /s is present)
   Classification is tool-agnostic and sees through built-in command-carrying
   wrappers (ssh, bash -c, sh -c, zsh -c, docker exec/run, pwsh/powershell
   -Command) up to MAX_WRAPPER_DEPTH. `python -c` and custom/private wrappers
   are treated as opaque (not inferable from the command text). This set is
   NOT disabled by any escape env: human approval is the contract.

Escape hatches (v0.7.0):

- AGENT_CONFIG_GATES=off (legacy blanket): disables writing-style (with its
  advisory) + banner only.
- AGENT_STYLE_HOOK=off (per-guard): disables writing-style and the agent-style
  advisory, which share the gate because they scan the same writes.
- AGENT_COMPOUND_CD_HOOK=off (per-guard): disables compound cd only.
- AGENT_NESTED_GIT_INIT_HOOK=off (per-guard): disables the nested git init
  gate only. Set it to create a submodule or a deliberate inner repository.

Destructive git/gh `ask` checks have NO agent-side reroute (commit/push/reset/merge
need human approval) and are NOT disabled by any escape env. Adding any future
hook-escape env requires updating _ESCAPE_HATCH_ENV_NAMES; the static literal-scan
test in test_guard.py enforces that no AGENT_*_HOOK string literal exists in this
file outside that constant.
"""
import json
import os
import random
import re
import shlex
import sys
import tempfile


def make_response(decision, reason):
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    })


def make_advisory_response(message):
    """Report `message` without touching the permission decision.

    Two fields because they reach two different readers, and the choice was
    measured rather than assumed (Claude Code 2.1.229): `additionalContext`
    is the only one of the three candidate channels that reaches the model,
    which is the reader that can act on a style finding while it drafts.
    Writing to stderr and exiting 0, the shape the banner re-arm advisory
    uses, reached neither reader in a probe. `systemMessage` puts the same
    line in front of the human.

    Omitting `permissionDecision` leaves the normal permission flow intact,
    so this neither grants nor blocks anything.
    """
    return json.dumps({
        "systemMessage": message,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": message,
        },
    })


# --- Check 0: auto-allow shipped trusted scripts -------------------------
#
# Background: Claude Code's PowerShell(...) permission rule patterns do not
# match `& '<path>\<script>.ps1' <args>` invocations as documented, leaving
# an extra approval prompt every time the implement-review skill fires one
# of its trusted shipped helpers from Phase 1c / 1d / 2.0. This check
# closes the gap by returning permissionDecision: allow only when the
# PowerShell command is a call-operator invocation (`& '<path>' [args]`)
# of one of those shipped scripts. The script path must end with one of
# the canonical tails below (case-insensitive, slash-normalized) so
# source / bootstrap / wheel-bundled layouts all match, while a
# non-invocation PowerShell command that merely contains the path tail
# as a string argument falls through to the normal permission flow.
#
# Members:
#   - auto-watch.ps1   Phase 1d background watcher polling for Review-*.md
#   - health-check.ps1 Phase 2.0 wrapper that delegates to health-check.py
#   - dispatch-codex.ps1 Phase 1c Auto-terminal dispatcher (usually called
#                      via Bash tool / pwsh -File, but cover the call-op
#                      form too for defense in depth)
IMPL_REVIEW_TRUSTED_PS_TAILS = frozenset([
    "skills/implement-review/scripts/auto-watch.ps1",
    "skills/implement-review/scripts/health-check.ps1",
    "skills/implement-review/scripts/dispatch-codex.ps1",
])

# Call-operator invocation pattern: `& '<quoted path>' [args]` or
# `& <bare path> [args]`, optionally preceded by `$env:NAME = 'value';`
# assignments (zero or more). Anchored at start AND end of command so a
# path tail that appears inside an argument string or after a different
# verb (e.g. `Write-Output '...auto-watch.ps1'`) does not qualify, and
# no PowerShell can be smuggled after the trusted script call.
#
# The env-var prefix covers a real invocation shape: the implement-review
# skill on Windows sets `$env:CODEX_BIN = 'codex.cmd';` before calling
# dispatch-codex.ps1 to dodge the npm dual-file trap (`codex` bash shim
# vs `codex.cmd` Windows wrapper sharing one PATH dir). Setting env vars
# before a trusted script is no different in trust posture from the user
# typing the same line, so the auto-allow honors it.
#
# Safe-token grammar: every env-value, the script path, and every
# trailing arg must be one of (1) a single-quoted literal (PS does not
# expand anything inside `'...'`), (2) a double-quoted literal whose
# body excludes chars that trigger PS string-expansion (`$(...)`,
# `$env:VAR`, backtick escape) or break the statement (`;`, `|`, `&`,
# parens, braces) or hand off to a redirection (`<`, `>`), or (3) a
# bare token whose chars cannot start a new statement / pipeline /
# subexpression / scriptblock / splatting / redirection.
#
# Two attack classes are closed here, layered across Rounds 1 + 2:
#
# Round 1 (env-prefix smuggling) -- env values used to be `\S+`, the
# trailing args were unrestricted, and the regex used `re.match` with a
# trailing `(?=$|\s)` lookahead (no end anchor):
#   - bare semicolon in env value: `$env:X = foo;bar; & ...`
#   - double-quoted dollar-subexpression: `$env:X = "$(evil)"; & ...`
#   - trailing semicolon after script: `& '<trusted>' ; evil`
#   - splatting after script: `& '<trusted>' @params`
#
# Round 2 (script-path-as-expansion + redirection passthrough) -- the
# script PATH itself was captured with `[^'"]+`, which still allowed PS
# expansion inside double quotes, and the bare-token set still permitted
# redirection operators:
#   - double-quoted `$()` in path: `& "$(evil)C:\...\health-check.ps1"`
#     -- PS evaluates `$(evil)` at call resolution time
#   - double-quoted `$env:VAR\...` in path: attacker pre-set $env:VAR
#     redirects the runtime-resolved path to a file they control
#   - redirection tokens: `& '<trusted>' > out.txt`, `2> err.txt`,
#     `< in.txt` were all auto-allowed because `>` / `<` matched the
#     bare-token regex
#
# Path captures are split into three named groups so each shape uses its
# own appropriate exclusion set (single-quoted is permissive because PS
# does not expand inside `'...'`; double-quoted must match the strict
# safe-char set to prevent expansion; bare is also strict).
_PS_SINGLE_QUOTED = r"'[^']*'"
_PS_DOUBLE_QUOTED_SAFE = r'"[^"`$;|&(){}<>]*"'
# `\s` in Python regex matches `\r` and `\n`, but PowerShell treats those
# as statement separators. Using `\s` would let `& '<trusted>'\nWrite-Output
# evil` slip past the trailing-arg grammar as if the newline were ordinary
# whitespace. Restrict the call-operator regex to HORIZONTAL whitespace
# (space and tab) only; any CR/LF in the command kicks it out of the safe
# shape and falls through to normal permission flow. Bare-safe tokens
# similarly use `[^\s...]` -- defining _PS_HSPACE here keeps the env-prefix,
# call-op, and trailing-arg whitespace classes uniform.
_PS_HSPACE = r"[ \t]"
_PS_BARE_SAFE = r"""[^`'"@<>\s;|&(){}$]+"""
_PS_SAFE_TOKEN = rf"(?:{_PS_SINGLE_QUOTED}|{_PS_DOUBLE_QUOTED_SAFE}|{_PS_BARE_SAFE})"

_PS_SINGLE_QUOTED_PATH = r"'(?P<single_quoted_path>[^']+)'"
_PS_DOUBLE_QUOTED_SAFE_PATH = r'"(?P<double_quoted_path>[^"`$;|&(){}<>]+)"'

_PS_CALL_OPERATOR_RE = re.compile(
    rf"""(?x)
    \A
    (?:                              # zero or more env-var assignments
        {_PS_HSPACE}*
        \$env:[A-Za-z_]\w*           # $env:NAME
        {_PS_HSPACE}*={_PS_HSPACE}*
        {_PS_SAFE_TOKEN}             # safe-token value
        {_PS_HSPACE}*;{_PS_HSPACE}*  # statement separator (NO newline)
    )*
    {_PS_HSPACE}*&{_PS_HSPACE}*      # call operator
    (?:
        {_PS_SINGLE_QUOTED_PATH}
        |
        {_PS_DOUBLE_QUOTED_SAFE_PATH}
        |
        (?P<bare_path>{_PS_BARE_SAFE})
    )
    (?:{_PS_HSPACE}+{_PS_SAFE_TOKEN})*  # zero or more safe arg tokens
    {_PS_HSPACE}*\Z                  # anchor at end (only h-space allowed)
    """
)


def _is_trusted_impl_review_ps_path(path):
    """Return True if `path` ends with one of the trusted impl-review
    script tails. Slash- and case-normalized so Windows backslash +
    mixed-case path components match the lowercase POSIX-shaped tails in
    IMPL_REVIEW_TRUSTED_PS_TAILS. Accepts both the exact canonical tail
    (repo-local relative invocation: `& 'skills\\implement-review\\scripts\\
    auto-watch.ps1' ...`) and the tail under any prefix (absolute path,
    bootstrap clone, wheel-bundled layout); both are realistic invocation
    shapes the implement-review skill emits.
    """
    path_norm = path.replace("\\", "/").lower()
    for tail in IMPL_REVIEW_TRUSTED_PS_TAILS:
        if path_norm == tail or path_norm.endswith("/" + tail):
            return True
    return False


def check_impl_review_ps_allow(tool_name, tool_input):
    """Return an allow message only when a PowerShell command invokes one
    of the trusted implement-review shipped scripts as the call-operator
    target; return None otherwise.

    Independent of AGENT_CONFIG_GATES because the escape hatch disables
    deny-style gates only; failing to honor an allow just falls back to
    the user seeing the existing approval prompt, so this check is
    always-on.
    """
    if tool_name != "PowerShell":
        return None
    cmd = tool_input.get("command", "") or ""
    # fullmatch + strip(" \t") enforces the trailing `\Z` anchor while
    # preserving any newlines in the input -- a newline kicks the command
    # out of the safe shape (PS treats it as a statement separator).
    # Stripping with the default cmd.strip() would erase trailing CR/LF
    # and re-open the Round-3 newline bypass.
    match = _PS_CALL_OPERATOR_RE.fullmatch(cmd.strip(" \t"))
    if not match:
        return None
    script_path = (
        match.group("single_quoted_path")
        or match.group("double_quoted_path")
        or match.group("bare_path")
        or ""
    )
    if _is_trusted_impl_review_ps_path(script_path):
        # Surface which leaf script matched so users debugging surprise
        # allows can grep for it.
        leaf = script_path.replace("\\", "/").rsplit("/", 1)[-1]
        return (
            "Auto-allow: implement-review trusted shipped script "
            f"({leaf}). Path-tail anchored to the shipped script set."
        )
    return None


# Banned AI-tell words (mirrors AGENTS.md Writing Defaults). Matched with
# finite-inflection alternation — each banned word expands to a bounded set
# of variants (self, plural, and common verb inflections) alternated with
# `\b…\b` boundaries. Irregular cases and nouns/adjectives that would
# otherwise pick up legit tech terms (e.g., `facet` → `faceted search`) are
# given explicit overrides below. Hyphenated entries (e.g., `game-changing`)
# match literally with non-word boundaries.
BANNED_WORDS = frozenset([
    "encompass", "burgeoning", "pivotal", "realm", "keen", "adept",
    "endeavor", "uphold", "imperative", "profound", "ponder", "cultivate",
    "hone", "delve", "embrace", "pave", "embark", "monumental",
    "scrutinize", "vast", "versatile", "paramount", "foster", "necessitates",
    "provenance", "multifaceted", "nuance", "obliterate", "articulate",
    "acquire", "underpin", "underscore", "harmonize", "garner",
    "undermine", "gauge", "facet", "bolster", "groundbreaking",
    "game-changing", "reimagine", "turnkey", "intricate", "trailblazing",
    "unprecedented",
])

# Prose-content file extensions subject to writing-style enforcement.
PROSE_EXTENSIONS = frozenset([".md", ".tex", ".rst", ".txt"])

# Path segment a caller uses to declare that a file holds text it is carrying
# rather than prose it is writing: a prompt assembled for another agent, or
# that agent's captured output. Both writing-style guards skip such a path.
#
# The declaration has to come from the writer, because nothing in the tool
# call reveals it. Measured across 34 local session transcripts, 23% of
# prose-extension writes land in a scratch directory, and that directory
# holds `prompt.md` beside a draft proposal section, so neither the extension
# nor the file name separates the two. Findings on carried text are
# unactionable in two different ways. A dispatch prompt is an instruction to
# another agent, so rewriting it changes what was asked. A captured round
# output is another agent's words, so rewriting it falsifies the record. An
# advisory that mostly fires on those is one the reader learns to skip, which
# is the reason RULE-G is already left out of the advisory's rule set.
#
# A directory rather than a file-name suffix, so a caller declares the
# location once and everything written there is covered. An unmarked path is
# still scanned, so forgetting the marker costs noise rather than silence.
AGENT_IO_SEGMENT = "agent-io"


def is_agent_io_path(file_path):
    """True when any directory along file_path is the agent-io marker."""
    parts = str(file_path).replace("\\", "/").lower().split("/")
    return AGENT_IO_SEGMENT in parts


def _inside_git_worktree(path):
    """True when any ancestor directory of `path` holds a `.git` entry.

    A repository is repository content wherever it happens to sit. CI
    routinely checks one out below the system temp directory, and without
    this the marker would be trusted there.
    """
    current = os.path.dirname(path)
    seen = set()
    while current and current not in seen:
        seen.add(current)
        try:
            if os.path.exists(os.path.join(current, ".git")):
                return True
        except OSError:
            return False
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return False


def is_scratch_path(file_path):
    """True when file_path is carried text's home: a temp root, outside any
    repository.

    Two conditions, because a temp root alone is not a trust boundary. A
    repository checked out below one is still a repository, so the walk for
    an enclosing `.git` runs first. Symlinks and Windows junctions are
    resolved before either test, since a lexical prefix check on an
    unresolved path can be satisfied by a link that lands anywhere.
    """
    try:
        target = os.path.realpath(str(file_path))
    except (OSError, ValueError):
        return False
    if _inside_git_worktree(target):
        return False
    target = os.path.normcase(target)
    roots = [tempfile.gettempdir(), "/tmp"]
    roots.extend(v for v in (os.environ.get("TMPDIR"),
                             os.environ.get("TEMP"),
                             os.environ.get("TMP")) if v)
    for candidate in roots:
        try:
            base = os.path.normcase(os.path.realpath(candidate))
        except (OSError, ValueError):
            continue
        if target == base or target.startswith(base + os.sep):
            return True
        if target.startswith(base + "/"):
            return True
    return False


def prose_extension(file_path, agent_io_requires_scratch=False):
    """Return the extension the writing-style guards should scan, or None.

    Both guards ask this one question, so the extension test lives in exactly
    one place and their scope cannot drift apart. They differ in how far they
    trust the agent-io marker, and that difference is the argument of this
    function rather than a second copy of the logic.

    The advisory trusts the marker anywhere. It reports, so a wrong exemption
    costs a message nobody needed.

    The deny gate trusts it only under a temp root that encloses no
    repository. It blocks, so a marker honored anywhere would be a one-token
    bypass: an agent could write `repo/agent-io/proposal.md` and skip the
    banned-word check on real prose. Carried text belongs in the session
    scratch directory anyway, which is where both `implement-review` and
    `prun` are documented to put it.
    """
    _, ext = os.path.splitext(str(file_path).lower())
    if ext not in PROSE_EXTENSIONS:
        return None
    if is_agent_io_path(file_path):
        if not agent_io_requires_scratch or is_scratch_path(file_path):
            return None
    return ext

# Tools exempt from the banner gate. Intent: block user-visible work (Bash,
# Write, Edit, MultiEdit, NotebookEdit, KillShell, MCP write-style tools)
# until the banner is emitted, but let the agent read state and dispatch
# skills/subagents/todo-updates freely on turn 1.
#
# Write/Edit/MultiEdit to <consumer_root>/.agent-config/banner-emitted.json
# (the ack file) is also exempt via an exact-path check, handled separately
# in check_banner_emission.
BANNER_GATE_EXEMPT_TOOLS = frozenset([
    # File-system observation
    "Read", "Grep", "Glob", "LS", "NotebookRead",
    # Metadata / dispatch / internal state
    "Skill", "Task", "TodoWrite",
    # Read-only observation tools the agent may invoke on turn 1
    "BashOutput", "WebFetch", "WebSearch", "ToolSearch",
])


# --- v0.7.0 escape hatches (per-guard + legacy blanket) -------------------
#
# Round 6 reroute criterion: a guard with an agent-side reroute (rephrase a
# banned word, use `git -C` instead of `cd && git`) stays as `deny` so the
# agent course-corrects in one model turn. Per-guard escape envs let the
# user disable a specific noise-audit guard in meta-discussion contexts
# (e.g., editing a style-guide document that legitimately quotes banned
# words) without turning off the whole suite.
#
# Adding a future hook-escape env requires extending this constant; the
# static literal-scan test in test_guard.py asserts no AGENT_*_HOOK string
# literal exists in this file outside this constant.
_ESCAPE_HATCH_ENV_NAMES = (
    "AGENT_CONFIG_GATES",
    "AGENT_STYLE_HOOK",
    "AGENT_COMPOUND_CD_HOOK",
    "AGENT_NESTED_GIT_INIT_HOOK",
)


def _env_disabled(name):
    """Return True iff env var `name` is set to a disable-truthy value."""
    val = (os.environ.get(name) or "").strip().lower()
    return val in ("0", "off", "false", "disabled", "no")


def nested_git_init_enabled():
    """Return False if the per-guard env disables the nested git init gate.

    AGENT_CONFIG_GATES does NOT disable it: that legacy blanket predates this
    gate and covers the writing-style and banner gates only.
    """
    return not _env_disabled("AGENT_NESTED_GIT_INIT_HOOK")


def gates_enabled():
    """Return False if the legacy AGENT_CONFIG_GATES env disables the
    writing-style + banner gates. v0.7.0 retains this for BC; per-guard envs
    layer on top via writing_style_enabled() / compound_cd_enabled()."""
    return not _env_disabled("AGENT_CONFIG_GATES")


def writing_style_enabled():
    """Return False if writing-style gate is disabled by either the legacy
    blanket env (AGENT_CONFIG_GATES) or the per-guard env (AGENT_STYLE_HOOK)."""
    return gates_enabled() and not _env_disabled("AGENT_STYLE_HOOK")


def compound_cd_enabled():
    """Return False if compound-cd gate is disabled by the per-guard env
    (AGENT_COMPOUND_CD_HOOK). NOTE: AGENT_CONFIG_GATES does NOT disable
    compound-cd (legacy scope is writing-style + banner only)."""
    return not _env_disabled("AGENT_COMPOUND_CD_HOOK")


# --- Per-banned-word reroute hints ----------------------------------------
#
# Concrete alternative phrasing for each banned word so the agent can lift
# the reroute directly from the deny message instead of inferring it. Keep
# alternatives short (1-3 words) and contextually accurate — these surface
# inline in the deny message and become the user-visible suggestion.
#
# Words without an explicit entry fall back to a generic "rephrase without
# this term" hint; explicit entries are preferred to reduce model-side
# inference latency.
_BANNED_WORD_REROUTES = {
    "encompass": "cover, include",
    "burgeoning": "growing, emerging",
    "pivotal": "key, central",
    "realm": "area, field, domain",
    "keen": "strong, active",
    "adept": "skilled, capable",
    "endeavor": "effort, attempt, work",
    "uphold": "support, maintain",
    "imperative": "essential, required",
    "profound": "significant, deep",
    "ponder": "consider, think about",
    "cultivate": "build, develop",
    "hone": "refine, sharpen",
    "delve": "look at, examine, explore",
    "embrace": "adopt, accept, use",
    "pave": "lead to, enable",
    "embark": "start, begin",
    "monumental": "major, large",
    "scrutinize": "examine, review",
    "vast": "large, extensive",
    "versatile": "flexible, adaptable",
    "paramount": "primary, top",
    "foster": "support, promote, encourage",
    "necessitates": "requires, needs",
    "provenance": "origin, source",
    "multifaceted": "complex, varied",
    "nuance": "detail, distinction",
    "obliterate": "remove, erase",
    "articulate": "express, describe, state",
    "acquire": "get, obtain",
    "underpin": "support, ground",
    "underscore": "emphasize, highlight",
    "harmonize": "align, reconcile",
    "garner": "gather, get",
    "undermine": "weaken, damage",
    "gauge": "measure, assess",
    "facet": "aspect, side, part",
    "bolster": "strengthen, support",
    "groundbreaking": "novel, new",
    "game-changing": "significant, transformative",
    "reimagine": "rethink, redesign",
    "turnkey": "ready-to-use, complete",
    "intricate": "complex, detailed",
    "trailblazing": "pioneering, new",
    "unprecedented": "rare, first-time, new",
}


def _suggest_rewrite(hits):
    """Return a `Suggested rewrite:` line for a set of banned-word hits.

    For each hit, look up the concrete alternative in _BANNED_WORD_REROUTES
    (fallback: generic "rephrase without"). Returns a single line beginning
    with the literal `Suggested rewrite:` token that the noise-budget gate
    recognizes as a reroute hint.
    """
    rewrites = []
    for word in sorted(set(hits)):
        alts = _BANNED_WORD_REROUTES.get(word)
        if alts:
            rewrites.append(f"`{word}` -> {alts}")
        else:
            rewrites.append(f"`{word}` -> rephrase without it")
    return "Suggested rewrite: " + "; ".join(rewrites) + "."


def _content_for_write(tool_name, tool_input):
    """Extract the outgoing content from a Write/Edit/MultiEdit tool input."""
    if tool_name == "Write":
        return tool_input.get("content", "")
    if tool_name == "Edit":
        return tool_input.get("new_string", "")
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits", [])
        return "\n".join(e.get("new_string", "") for e in edits)
    return ""


def _stand_in(match):
    """What a stripped span leaves behind: one space and the span's newlines.

    The advisory's detectors report line numbers, and the author reads them
    against the file. A fence that collapsed to a single space moved every
    later line up by the fence's height, and a repeated caveat far below it
    was reported far above its place. Keeping the span's own newlines keeps
    each later line where the file has it. An inline span has none and still
    becomes one space, as before.
    """
    return " " + "\n" * match.group(0).count("\n")


def _content_for_style_check(content, ext):
    """Strip quoted/code examples before prose style matching. Banned words
    inside code fences (``` … ```) or inline code (`foo`) are almost always
    meta-discussion (examples of what to avoid), not AI-tell prose. Stripping
    these avoids flagging legitimate style-guide documentation, generated
    CLAUDE.md / agents/codex.md files, and CHANGELOG entries that quote the
    ban list. Similarly for LaTeX verbatim / \\verb / \\texttt environments.

    The line structure survives the stripping (see `_stand_in`): the text
    handed to a detector has the same number of lines as the text written,
    and every line that was not inside a stripped span is on its own number.
    """
    if ext in (".md", ".rst"):
        # Fenced code blocks (``` ... ``` or ~~~ ... ~~~)
        content = re.sub(r"(`{3,}|~{3,})[\s\S]*?\1", _stand_in, content)
        # Double-backtick inline code (for literals containing a single backtick)
        content = re.sub(r"``[^`\n]+``", _stand_in, content)
        # Single-backtick inline code
        content = re.sub(r"`[^`\n]+`", _stand_in, content)
    elif ext == ".tex":
        content = re.sub(
            r"\\begin\{verbatim\}[\s\S]*?\\end\{verbatim\}",
            _stand_in,
            content,
        )
        content = re.sub(r"\\verb(.).*?\1", _stand_in, content)
        content = re.sub(r"\\texttt\{[^{}]*\}", _stand_in, content)
    return content


# Explicit variant overrides for verbs whose default inflection rules would
# miss or mis-generate. Keep these tight: the cost of an over-broad set is
# false positives that block real prose (e.g., "honest" matching "hone").
_BANNED_VARIANT_OVERRIDES = {
    "delve": ("delve", "delves", "delved", "delving"),
    "hone": ("hone", "hones", "honed", "honing"),
    "pave": ("pave", "paves", "paved", "paving"),
    "necessitates": (
        "necessitate", "necessitates", "necessitated", "necessitating",
    ),
    # Doubled-consonant past tense that the default heuristic misses.
    "underpin": ("underpin", "underpins", "underpinned", "underpinning"),
    # Present participle is the banned entry; the verb stem is `burgeon`, so
    # explicitly include plain and past-tense forms.
    "burgeoning": ("burgeon", "burgeons", "burgeoned", "burgeoning"),
    # Adjective + adverb pairs the default heuristic would miss.
    "monumental": ("monumental", "monumentally"),
    "profound": ("profound", "profoundly"),
    # Noun only — default heuristic would generate "faceted" / "faceting",
    # and "faceted" is a common technical adjective ("faceted search UI")
    # that should not be denied. Restrict to plural only.
    "facet": ("facet", "facets"),
}


def _word_variants(word):
    """Return a finite set of inflections for a banned word. Uses simple
    English-verb heuristics (+s / +ed / +ing, y→ies, trailing-e → +d/+ing)
    plus the override table above for irregular cases. Deliberately bounded
    — we prefer to miss rare variants than to block legitimate prose."""
    if word in _BANNED_VARIANT_OVERRIDES:
        return set(_BANNED_VARIANT_OVERRIDES[word])

    variants = {word, word + "s"}
    if word.endswith("e"):
        variants.add(word + "d")
        variants.add(word[:-1] + "ing")
    elif word.endswith("y"):
        variants.add(word[:-1] + "ies")
        variants.add(word[:-1] + "ied")
        variants.add(word + "ing")
    else:
        variants.add(word + "ed")
        variants.add(word + "ing")
    return variants


def _banned_regex(word):
    """Compile a regex for a banned word using finite variant alternation so
    arbitrary prefix matches cannot block benign prose. Hyphenated terms
    match exactly with non-word boundaries."""
    if "-" in word:
        return re.compile(
            r"(?<!\w)" + re.escape(word) + r"(?!\w)", re.IGNORECASE
        )
    alternatives = sorted(_word_variants(word), key=len, reverse=True)
    return re.compile(
        r"\b(?:" + "|".join(re.escape(v) for v in alternatives) + r")\b",
        re.IGNORECASE,
    )


_BANNED_PATTERNS = [(w, _banned_regex(w)) for w in sorted(BANNED_WORDS)]


def check_writing_style(tool_name, tool_input):
    """Return a deny message if the write introduces banned AI-tell words into
    a prose file (.md/.tex/.rst/.txt). Code files are not checked — banned
    words rarely appear naturally in code, and docstring false positives would
    be a usability regression. Quoted/code-fenced examples within prose files
    are also stripped before matching (see _content_for_style_check).
    """
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return None

    file_path = tool_input.get("file_path", "")
    ext = prose_extension(file_path, agent_io_requires_scratch=True)
    if ext is None:
        return None

    content = _content_for_write(tool_name, tool_input)
    if not content:
        return None

    scan_target = _content_for_style_check(content, ext)

    found = []
    for word, pattern in _BANNED_PATTERNS:
        if pattern.search(scan_target):
            found.append(word)

    if found:
        hits = ", ".join(sorted(set(found)))
        rewrite_line = _suggest_rewrite(found)
        return (
            f"Writing-style: banned AI-tell words detected in {file_path}: {hits}. "
            f"{rewrite_line} "
            f"Per AGENTS.md Writing Defaults, revise without these terms "
            f"(close variants are caught too). Examples inside ``` code fences, "
            f"`inline code`, or LaTeX \\verb/\\texttt are ignored. If a real "
            f"meta-use is still blocking you, set AGENT_STYLE_HOOK=off (per-guard) "
            f"or AGENT_CONFIG_GATES=off (legacy blanket) in ~/.claude/settings.json env."
        )

    return None


# --- Prose written through a shell ----------------------------------------
#
# The gate above reads the Write, Edit, and MultiEdit tools, and a shell
# command writes the same file just as well. On 2026-10-21 a heredoc that put
# two banned words into a Markdown file passed with no output, while the same
# text through Write was denied. Auto mode tells the agent to prefer the shell
# for small edits, so this was a working route rather than a theoretical one.
#
# Two questions, answered by different readings of the command. Where does it
# write? That needs the shell's own structure: a quoted search pattern, a
# comment, and a heredoc body are not redirections even when they spell one,
# and a review prompt that quotes `echo x > notes.md` writes only the prompt.
# The first version matched redirections over the raw text and denied all
# three. What does it write? Once a real target is a prose file, the whole
# command text is scanned, heredoc body included, without trying to separate
# content from command. A command that removes a banned word
# (`sed -i 's/delve/examine/' notes.md`) is therefore denied too, and the Edit
# tool, whose new text carries no banned word, is the reroute that always
# exists.
#
# Writes are an output redirection, `tee`, an in-place `sed` or `perl`, and
# `Set-Content`, `Add-Content`, `Out-File`, and `Tee-Object` in PowerShell. A
# script that opens the file itself (`python - <<EOF`, a heredoc fed to
# `bash`) is opaque, as it is to the destructive-command classifier, and so is
# a path whose variable the command does not assign and the environment does
# not hold. A blind spot restores the status quo; reading either would mean
# guessing what a program does.
_PS_FILE_WRITERS = frozenset({"set-content", "add-content", "out-file", "tee-object"})
_PS_PATH_PARAMS = frozenset({"path", "literalpath", "pspath", "lp", "filepath"})
_PS_SWITCH_PARAMS = frozenset({
    "force", "nonewline", "passthru", "append", "noclobber", "asbytestream",
    "confirm", "usetransaction",
})
_PS_HERESTRING_OPEN = re.compile(r"""@(['"])[ \t]*\r?\n""")
_SHELL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SHELL_ASSIGNMENT_WORD = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
# `$env:NAME` (PowerShell), `${NAME}`, `${NAME:-default}`, `${NAME-default}`,
# and `$NAME`. Any other expansion keeps its `$` and leaves the path unknown.
_SHELL_VARIABLE = re.compile(
    r"\$(?:(?i:env):([A-Za-z_][A-Za-z0-9_]*)"
    r"|\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?-)([^}]*))?\}"
    r"|([A-Za-z_][A-Za-z0-9_]*))"
)
# A substring prefilter, the shared stem of each banned word's variants. Most
# commands carry none and leave here; the word-boundary patterns run only once
# a prose target is known, because a dispatch prompt that does carry one is
# usually bound for agent-io, and on a 390 KB prompt the patterns alone took
# about 85 ms where this takes a few.
_BANNED_STEMS = tuple(sorted({
    os.path.commonprefix(sorted(_word_variants(word))).lower()
    for word in BANNED_WORDS
}))


def _skip_heredoc_bodies(cmd, i, heredocs):
    """Return the index just past the bodies of `heredocs`, read from `i`."""
    n = len(cmd)
    for delimiter, strip_tabs in heredocs:
        while i < n:
            end = cmd.find("\n", i)
            line = cmd[i:] if end == -1 else cmd[i:end]
            i = n if end == -1 else end + 1
            if strip_tabs:
                line = line.lstrip("\t")
            if line.rstrip("\r") == delimiter:
                break
    return i


def _lex_shell(cmd, shell):
    """Split a command into simple commands of `(kind, text)` tokens.

    `kind` is "word", "out" for the operand of an output redirection, or "in"
    for any other redirection operand. Quotes are removed from the text,
    comments are dropped, and heredoc bodies are skipped. Simple commands end
    at an unquoted `;`, `&`, `|`, `(`, `)`, or newline. This reads the common
    shapes and stops there: it expands nothing and runs nothing.
    """
    escape = "`" if shell == "powershell" else "\\"
    commands, tokens, buf = [], [], []
    state = {"word": False, "next": None}
    heredocs = []

    def end_word():
        if not state["word"]:
            return
        text = "".join(buf)
        buf.clear()
        state["word"] = False
        kind, state["next"] = state["next"] or "word", None
        if isinstance(kind, tuple):
            heredocs.append((text, kind[1]))
        else:
            tokens.append((kind, text))

    def end_command():
        end_word()
        state["next"] = None
        if tokens:
            commands.append(list(tokens))
            tokens.clear()

    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "\n":
            end_command()
            i += 1
            if heredocs:
                i = _skip_heredoc_bodies(cmd, i, heredocs)
                heredocs.clear()
        elif ch in " \t\r":
            end_word()
            i += 1
        elif ch == escape:
            # An escaped newline continues the line; any other escaped
            # character is literal text.
            if i + 1 < n and cmd[i + 1] != "\n":
                buf.append(cmd[i + 1])
                state["word"] = True
            i += 2
        elif shell == "powershell" and _PS_HERESTRING_OPEN.match(cmd, i):
            # A here-string runs to its quote and `@` at the start of a line;
            # quotes inside it are text, so `user's` does not end it.
            opener = _PS_HERESTRING_OPEN.match(cmd, i)
            closing = re.compile(
                r"(?m)^" + re.escape(opener.group(1) + "@")
            ).search(cmd, opener.end())
            end = n if closing is None else closing.start()
            buf.append(cmd[opener.end():end])
            state["word"] = True
            i = n if closing is None else closing.end()
        elif ch == "'":
            end = cmd.find("'", i + 1)
            end = n if end == -1 else end
            buf.append(cmd[i + 1:end])
            state["word"] = True
            i = end + 1
        elif ch == '"':
            i += 1
            while i < n and cmd[i] != '"':
                # Inside bash double quotes a backslash escapes only these
                # characters, so `"C:\Users\me\notes.md"` keeps its path.
                if cmd[i] == escape and i + 1 < n and (
                        shell == "powershell" or cmd[i + 1] in '$`"\\\n'):
                    i += 1
                buf.append(cmd[i])
                i += 1
            state["word"] = True
            i += 1
        elif ch == "#" and not state["word"]:
            end = cmd.find("\n", i)
            i = n if end == -1 else end
        elif shell == "powershell" and cmd.startswith("<#", i):
            end = cmd.find("#>", i + 2)
            i = n if end == -1 else end + 2
        elif ch == "&" and cmd.startswith("&>", i):
            end_word()
            state["next"] = "out"
            i += 3 if cmd.startswith("&>>", i) else 2
        elif ch in ";&|()":
            end_command()
            i += 1
        elif ch in "<>":
            # Digits written against the operator (`2>`) name a descriptor.
            if state["word"] and "".join(buf).isdigit():
                buf.clear()
                state["word"] = False
            else:
                end_word()
            if cmd.startswith(">&", i):
                rest = cmd[i + 2:].lstrip(" \t")
                state["next"] = "in" if rest[:1].isdigit() or rest[:1] == "-" else "out"
                i += 2
            elif cmd.startswith((">>", ">|"), i):
                state["next"] = "out"
                i += 2
            elif ch == ">":
                state["next"] = "out"
                i += 1
            elif cmd.startswith("<<<", i):
                state["next"] = "in"
                i += 3
            elif cmd.startswith("<<-", i):
                state["next"] = ("heredoc", True)
                i += 3
            elif cmd.startswith("<<", i):
                state["next"] = ("heredoc", False)
                i += 2
            else:
                state["next"] = "in"
                i += 2 if cmd.startswith(("<&", "<>"), i) else 1
        else:
            buf.append(ch)
            state["word"] = True
            i += 1
    end_command()
    return commands


def _edit_in_place_operands(head, args):
    """Return the file operands of an in-place `sed` or `perl`, else [].

    Option clusters are read the way the programs read them: `-i` may carry a
    backup suffix, and `-e` or `-f` takes the rest of its cluster or the next
    argument as its value, so `sed -e's/x/y/'` is not in place. Without `-e`,
    the first operand is the script. An operand list that keeps a stray word
    costs nothing, since prose_extension() decides which are prose.
    """
    in_place = script_given = False
    operands = []
    value_flags = "ef" if head != "perl" else "eE"
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            operands.extend(args[i:])
            break
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            if name == "--in-place":
                in_place = True
            elif name in ("--expression", "--file", "--line-length"):
                script_given = script_given or name != "--line-length"
                if "=" not in arg:
                    i += 1
            continue
        if arg.startswith("-") and len(arg) > 1:
            for k, c in enumerate(arg[1:], 1):
                if c == "i":
                    in_place = True
                    break
                if c in value_flags:
                    script_given = True
                    if k == len(arg) - 1:
                        i += 1
                    break
                if c == "l" or (head == "perl" and c in "MmIxCdDF0"):
                    if head != "perl" and k == len(arg) - 1:
                        i += 1
                    break
            continue
        operands.append(arg)
    if not in_place:
        return []
    return operands if script_given else operands[1:]


def _ps_destinations(args):
    """Return the file a PowerShell content cmdlet writes, or [] for -WhatIf."""
    named, positional = [], []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg.startswith("-") and len(arg) > 1:
            name, sep, value = arg[1:].partition(":")
            name = name.lower()
            if name == "whatif":
                if value.lower() not in ("$false", "0"):
                    return []
                continue
            if name in _PS_SWITCH_PARAMS:
                continue
            if not sep:
                value = args[i] if i < len(args) else ""
                i += 1
            if name in _PS_PATH_PARAMS:
                named.append(value)
            continue
        positional.append(arg)
    return named or positional[:1]


def _record_assignments(words, shell, assignments):
    """Apply a simple command's lasting variable assignments to `assignments`.

    Read from lexed words, so an assignment spelled inside a heredoc body or
    a quoted argument is not one. In Bash an assignment lasts only when the
    command is nothing but assignments (`export` and its kin included);
    `OUT=x cmd` sets OUT for `cmd` alone and is skipped. PowerShell's
    `$name = value` counts only as a whole three-word command, since anything
    longer is an expression.
    """
    if shell == "powershell":
        if not words or not words[0].startswith("$"):
            return
        name, sep, value = words[0][1:].partition("=")
        if not sep:
            if len(words) != 3 or words[1] != "=":
                return
            value = words[2]
        if _SHELL_NAME.fullmatch(name):
            assignments[name] = value
        return
    found = {}
    for word in words:
        if word in ("export", "local", "declare", "readonly"):
            continue
        match = _SHELL_ASSIGNMENT_WORD.fullmatch(word)
        if not match:
            return
        found[match.group(1)] = match.group(2)
    assignments.update(found)


def _shell_prose_targets(commands, shell, assignments, depth=0):
    """Return `(operand, variables)` for every operand a command writes to.

    `commands` is the lexer's output. Redirections come from it directly. The
    named writers are read per simple command, through the same wrappers the
    destructive-command classifier sees through, so a `bash -c` payload is
    read as a command of its own. Each operand carries the variables as they
    stood when its command ran, so a later reassignment does not reach back,
    and a child shell works on a copy that never flows back to its parent.
    """
    targets = []
    for tokens in commands:
        state = dict(assignments)
        targets.extend((text, state) for kind, text in tokens if kind == "out")
        words = [text for kind, text in tokens if kind == "word"]
        _record_assignments(words, shell, assignments)
        parts = strip_wrappers(words)
        if not parts:
            continue
        payload, payload_shell = _wrapper_payload(parts, shell)
        if payload is not None:
            if depth < MAX_WRAPPER_DEPTH:
                # `OUT=x bash -c ...` hands OUT=x to the child alone.
                child = dict(state)
                if shell != "powershell":
                    for word in words[:len(words) - len(parts)]:
                        match = _SHELL_ASSIGNMENT_WORD.fullmatch(word)
                        if match:
                            child[match.group(1)] = match.group(2)
                targets.extend(_shell_prose_targets(
                    _lex_shell(payload, payload_shell), payload_shell,
                    child, depth + 1,
                ))
            continue
        head = _basename(parts[0])
        if head == "tee":
            operands = [a for a in parts[1:] if not a.startswith("-")]
        elif head in ("sed", "gsed", "perl"):
            operands = _edit_in_place_operands(head, parts[1:])
        elif head in _PS_FILE_WRITERS:
            operands = _ps_destinations(parts[1:])
        else:
            operands = []
        targets.extend((operand, state) for operand in operands)
    return targets


def _resolve_shell_path(word, cwd, assignments):
    """Return the path a shell word names, or None when it cannot be known.

    Variables come from assignments in the same command first, then from the
    environment, because a scratch path is usually built in the command
    itself (`P=/tmp/...; cat > "$P/agent-io/prompt.txt"`).
    """
    def expand(m):
        if m.group(1):
            return os.environ.get(m.group(1), m.group(0))
        name = m.group(2) or m.group(5)
        value = assignments.get(name, os.environ.get(name))
        if m.group(3) and (value is None or (m.group(3) == ":-" and not value)):
            return m.group(4)
        return m.group(0) if value is None else value

    path = word
    for _ in range(2):
        path = _SHELL_VARIABLE.sub(expand, path)
    # A `$` left over is an expansion this reads no further; a backtick is a
    # command substitution. Either way the path is not known.
    if "$" in path or "`" in path:
        return None
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.join(cwd, path)


def check_shell_prose_write(tool_name, tool_input, cwd=None):
    """Return a deny message if a Bash or PowerShell command writes banned
    AI-tell words into a prose file, judged as `check_writing_style` judges
    the same file written through Write."""
    cmd = tool_input.get("command", "")
    lowered = cmd.lower()
    if not any(stem in lowered for stem in _BANNED_STEMS):
        return None

    shell = "powershell" if tool_name == "PowerShell" else "bash"
    targets = _shell_prose_targets(_lex_shell(cmd, shell), shell, {})
    base = cwd or os.getcwd()
    judged = {}
    for word, variables in targets:
        path = _resolve_shell_path(word, base, variables)
        if path is None:
            continue
        ext = prose_extension(path, agent_io_requires_scratch=True)
        if ext is not None:
            judged.setdefault(ext, word)

    found = set()
    target = None
    for ext, word in judged.items():
        scan_target = _content_for_style_check(cmd, ext)
        hits = [w for w, p in _BANNED_PATTERNS if p.search(scan_target)]
        if hits:
            found.update(hits)
            target = target or word
    if not found:
        return None

    return (
        f"Writing-style: this command writes to {target} and carries banned "
        f"AI-tell words: {', '.join(sorted(found))}. {_suggest_rewrite(found)} "
        f"Per AGENTS.md Writing Defaults, revise without these terms. The gate "
        f"reads the whole command, so a command that only removes one of these "
        f"words is stopped too; make that change with the Edit tool. A command "
        f"that is not writing prose, such as a search saved to a file, can send "
        f"its output under an agent-io directory in the session scratchpad or "
        f"to a non-prose extension. If a real meta-use is still blocking you, "
        f"set AGENT_STYLE_HOOK=off (per-guard) or AGENT_CONFIG_GATES=off "
        f"(legacy blanket) in ~/.claude/settings.json env."
    )


# Cap on advisory findings per write. RULE-12 alone reports 268 hits on a
# large CHANGELOG, and a wall of findings is one the reader learns to skip.
_STYLE_ADVISORY_LIMIT = 5


# The rules the advisory promises, in the guard-hook docs both repos
# ship. Reaching into private names is a bet on a package the hook does not
# own, so the set is spelled once here and the loader below refuses a partial
# match: advertising six rules while silently running one is worse than
# running none, because the reader cannot tell the difference from the output.
#
# This is the baseline, the set every release before 0.8.0 carries. From 0.8.0
# the profile `_rule_12_profile` takes the place of `_rule_12`, as below.
# agent-style 0.6.0 added `_rule_03` (confidence intervals in prose, and
# precise figures stacked across consecutive sentences of an abstract or an
# introduction). The set therefore follows the package's release, read in
# `_agent_style_mechanical()`, and not whatever the package happens to
# expose. Probing for `_rule_03` instead would let a 0.6.0 package that lost
# the function narrow the advisory to six rules. The documentation would still
# promise seven, which is the partial coverage this loader exists to refuse.
# The release says what the package promises, and the loader holds it to that.
_ADVISORY_RULE_ATTRS = (
    "_rule_05", "_rule_06", "_rule_12", "_rule_b", "_rule_d", "_rule_i",
)

# From this release on the advisory requires `_RULE_03_ATTR` as well. A 0.6.0
# or newer package that lacks it is incompatible and disables the whole
# advisory, as a package missing any of the six does. It runs first because
# the findings are capped and RULE-12 can fill the cap on its own.
_RULE_03_ATTR = "_rule_03"
_RULE_03_MIN_VERSION = (0, 6, 0)

# agent-style 0.7.0 added `_rule_a` for prose written into LaTeX table cells.
# It is required from that release on, under the same contract. It runs right
# after RULE-03, so both display rules sit inside the cap.
_RULE_A_ATTR = "_rule_a"
_RULE_A_MIN_VERSION = (0, 7, 0)

# agent-style 0.8.0 swaps the per-sentence `_rule_12` for `_rule_12_profile`,
# which reads a whole document and reports under the same RULE-12 label, and
# adds `_rule_08_repeats` (a caveat restated three or more times outside the
# limitations). Both are required from that release on, under the same
# contract. `_rule_12` is not required: the profile replaces it in the set from
# 0.8.0, and a package that dropped the old function is still complete. RULE-08
# runs third, after the two display rules, because the findings are capped and
# RULE-12 can fill the cap on its own.
_RULE_12_ATTR = "_rule_12"
_RULE_12_PROFILE_ATTR = "_rule_12_profile"
_RULE_08_REPEATS_ATTR = "_rule_08_repeats"
_PROFILE_MIN_VERSION = (0, 8, 0)

# The label a finding carries, spelled out for the detectors whose function
# name does not derive it. `name.replace("_rule_", "RULE-").upper()` turns
# `_rule_12_profile` into `RULE-12_PROFILE`, a label no rule pack uses, and
# the profile has to report under RULE-12 so the audit and the documentation
# keep one name for the rule.
_ADVISORY_LABELS = {
    _RULE_12_PROFILE_ATTR: "RULE-12",
    _RULE_08_REPEATS_ATTR: "RULE-08",
}

# Detectors that take the path of the file being written as well as its text.
# RULE-03 reads it to recognise abstract and introduction files. RULE-A, the
# RULE-12 profile and RULE-08 read it to tell a LaTeX source and an appendix or
# supplement apart, and the profile and RULE-08 also read the file name to find
# an abstract, an introduction, or a limitations section. The others are
# called with the text alone, as they were before these existed.
#
# The set is keyed by label, and RULE-12 is one label for two functions: the
# profile takes the path and the per-sentence `_rule_12` of earlier releases
# does not. `_agent_style_mechanical()` therefore hands back the old function
# behind `_ignore_path`. A caller can then pass the path to every RULE-12,
# whichever release is installed.
_PATH_AWARE_RULES = frozenset({"RULE-03", "RULE-A", "RULE-08", "RULE-12"})


def _ignore_path(fn):
    """Wrap a text-only detector so it accepts, and drops, `file_path`."""
    def call(text, file_path=None):
        return fn(text)
    return call


def _agent_style_version():
    """Return agent_style's release as a three-integer tuple, or None.

    None means missing, not a string, or unparsable, and the caller treats it
    as an older release. That is the safe direction. An older release runs the
    six-rule baseline, which every supported package carries. Guessing "new"
    would demand a seventh rule the package may lack and switch the whole
    advisory off.

    Compared numerically because `0.10.0` sorts before `0.6.0` as a string.
    Only the leading dotted integers count, so `0.6.0rc1` and `0.6.0.dev0`
    read as 0.6.0. The hook runs on every tool call in every repository and
    carries no dependencies of its own, so it does not import `packaging`.
    """
    try:
        import agent_style
        raw = getattr(agent_style, "__version__", None)
        if not isinstance(raw, str):
            return None
        match = re.match(r"\s*v?(\d+(?:\.\d+)*)", raw)
        if not match:
            return None
        parts = [int(p) for p in match.group(1).split(".")][:3]
    except Exception:
        return None
    return tuple(parts + [0] * (3 - len(parts)))


def _agent_style_mechanical():
    """Return agent-style's mechanical detectors, or None when unavailable.

    Imported lazily and defensively. This hook runs on every tool call in
    every repository, most of which have no reason to carry the package, so a
    missing or broken `agent_style` must cost nothing and block nothing. The
    banned-word gate above is independent of it and keeps working either way.

    All of the release's rules or nothing. An earlier version kept whatever
    subset it found, so a release that renamed five of the six private
    functions would have left the hook running one rule, the documentation
    promising six, and every behaviour test green. The release decides which
    set that is. Before 0.6.0, or with a version that cannot be read, it is
    `_ADVISORY_RULE_ATTRS`. 0.6.0 adds `_rule_03`, 0.7.0 adds `_rule_a`, and
    0.8.0 adds `_rule_08_repeats` and runs `_rule_12_profile` in place of
    `_rule_12`.
    """
    try:
        from agent_style.review import detectors_mech
    except Exception:
        return None
    # RULE-G (headings must be title case) is deliberately absent. Measured over
    # the 155 markdown files in this repo it produced 1018 of 2561 findings, and
    # a 15-hit random sample contained nothing worth acting on: it flags the
    # sentence-case headings this corpus writes on purpose ("## Non-goals",
    # "### Empirical note") and lowercase project names ("agent-config") that
    # cannot be title-cased at all. At 40% of all findings it would fill the cap
    # below with headings the author already decided. The style-review skill
    # still runs it, where a human asked for the full audit.
    attrs = _ADVISORY_RULE_ATTRS
    version = _agent_style_version()
    if version is not None and version >= _PROFILE_MIN_VERSION:
        attrs = (_RULE_03_ATTR, _RULE_A_ATTR, _RULE_08_REPEATS_ATTR) + tuple(
            _RULE_12_PROFILE_ATTR if name == _RULE_12_ATTR else name
            for name in _ADVISORY_RULE_ATTRS
        )
    elif version is not None and version >= _RULE_A_MIN_VERSION:
        attrs = (_RULE_03_ATTR, _RULE_A_ATTR) + _ADVISORY_RULE_ATTRS
    elif version is not None and version >= _RULE_03_MIN_VERSION:
        attrs = (_RULE_03_ATTR,) + _ADVISORY_RULE_ATTRS
    rules = []
    for name in attrs:
        fn = getattr(detectors_mech, name, None)
        if not callable(fn):
            # An installed but incompatible agent-style. Treated exactly like
            # an absent one: silent, and the banned-word gate is unaffected.
            return None
        label = _ADVISORY_LABELS.get(name) or name.replace("_rule_", "RULE-").upper()
        if name == _RULE_12_ATTR:
            fn = _ignore_path(fn)
        rules.append((label, fn))
    return rules


def advise_writing_style(tool_name, tool_input):
    """Report agent-style's deterministic findings without blocking the write.

    Advisory rather than deny, deliberately. The banned-word gate denies
    because each hit has a one-word substitution; these rules do not. Before
    agent-style 0.8.0, RULE-12 fires on any sentence over thirty words, which
    is a judgement call in prose a human is typing and a mechanical fix only
    when an agent is drafting. From 0.8.0 it reports where a document's
    sentence lengths and clause joins leave the human range, a statement about
    the draft as a whole. Denying on either would make the hook something to
    switch off.

    Returns a message, or None. The caller passes the tool call through
    either way and hands any message to make_advisory_response().
    """
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return None

    file_path = tool_input.get("file_path", "")
    ext = prose_extension(file_path)
    if ext is None:
        return None

    rules = _agent_style_mechanical()
    if not rules:
        return None

    content = _content_for_write(tool_name, tool_input)
    if not content:
        return None

    # Same stripping the banned-word gate uses, so fenced examples and inline
    # code do not produce findings the author cannot act on.
    scan_target = _content_for_style_check(content, ext)

    findings = []
    for label, fn in rules:
        try:
            violations = (
                fn(scan_target, file_path=file_path)
                if label in _PATH_AWARE_RULES
                else fn(scan_target)
            )
            rule_findings = [
                f"{label}: {getattr(v, 'detail', None) or str(v)}"
                for v in violations
            ]
        except Exception:
            # Every rule in the set ran or none did. Skipping the rule that
            # raised and keeping the rest is the same hidden partial coverage
            # the loader refuses at import time, one layer down: a changed
            # signature or an input the detector cannot handle would quietly
            # narrow the advisory, and the output would look exactly like a
            # clean pass on those rules. A broken detector is treated as a
            # broken package.
            return None
        findings.extend(rule_findings)

    if not findings:
        return None

    shown = findings[:_STYLE_ADVISORY_LIMIT]
    more = len(findings) - len(shown)
    tail = f" (+{more} more)" if more else ""
    return (
        # "did not block" rather than "was allowed": this response omits
        # permissionDecision, so the native permission layer and any other
        # PreToolUse hook still get their say. Claiming the write happened
        # would be a promise this hook is not in a position to make.
        f"[agent-style] {len(findings)} finding(s) in {os.path.basename(file_path)}"
        f"{tail}: " + "; ".join(shown) +
        # Humans break any single default now and then, and a draft that obeys
        # every default reads as uniform. The findings mark where the text
        # leaves typical human prose, and the author keeps one that reads well.
        ". These mark departures from typical human prose; one that reads "
        "well can stay. Advisory only; this hook did not block the write."
    )


def _read_ts(path):
    """Read {'ts': N} from a JSON file. Returns 0 on any error or missing."""
    try:
        with open(path, encoding="utf-8") as f:
            return float(json.load(f).get("ts", 0))
    except Exception:
        return 0


def _find_consumer_root(start=None):
    """Walk up from `start` (default os.getcwd()) looking for a directory that
    contains .agent-config/bootstrap.sh or .agent-config/bootstrap.ps1.
    Returns the absolute consumer-repo root, or None if not inside one.

    Terminates at filesystem root via the cwd != prev invariant (POSIX:
    dirname('/') == '/'; Windows: dirname('C:\\') == 'C:\\'). Uses raw
    os.path (no symlink resolution) — agent tool calls see paths as the
    shell passes them, so not resolving keeps the comparison space simple.
    """
    cwd = os.path.abspath(start or os.getcwd())
    prev = None
    while cwd and cwd != prev:
        for marker in ("bootstrap.sh", "bootstrap.ps1"):
            if os.path.isfile(os.path.join(cwd, ".agent-config", marker)):
                return cwd
        prev = cwd
        cwd = os.path.dirname(cwd)
    return None


def check_banner_emission(tool_name, tool_input):
    """Return a deny message if a SessionStart event is pending but the banner
    has not yet been emitted for it. Non-consumer directories (source repos
    or unrelated dirs) skip the gate entirely. The exempt-tools set covers
    the agent's turn-1 observation and dispatch needs. Write/Edit/MultiEdit
    to the exact <consumer_root>/.agent-config/banner-emitted.json path is
    also exempt so the agent can acknowledge the event.
    """
    if tool_name in BANNER_GATE_EXEMPT_TOOLS:
        return None

    consumer_root = _find_consumer_root()
    if consumer_root is None:
        return None  # source repo or unrelated directory → no gate

    # Exact-path exemption for the ack file. Normalized equality prevents
    # off-root suffix spoofs and cross-project ack writes from bypassing.
    # Uses realpath (not abspath) so macOS /var -> /private/var symlinks and
    # Windows junctions resolve consistently on both sides of the comparison.
    if tool_name in ("Write", "Edit", "MultiEdit"):
        requested = tool_input.get("file_path", "")
        if requested:
            expected = os.path.join(
                consumer_root, ".agent-config", "banner-emitted.json"
            )
            try:
                req_norm = os.path.normcase(os.path.realpath(requested))
                exp_norm = os.path.normcase(os.path.realpath(expected))
            except OSError:
                # realpath rarely raises but fall back to abspath just in case
                req_norm = os.path.normcase(os.path.abspath(requested))
                exp_norm = os.path.normcase(os.path.abspath(expected))
            if req_norm == exp_norm:
                return None

    event_path = os.path.join(
        consumer_root, ".agent-config", "session-event.json"
    )
    emitted_path = os.path.join(
        consumer_root, ".agent-config", "banner-emitted.json"
    )

    if not os.path.exists(event_path):
        return None  # No event recorded yet → skip gate

    event_ts = _read_ts(event_path)
    emitted_ts = _read_ts(emitted_path)

    if event_ts > emitted_ts:
        # First-arm semantics: if no ack file has ever been written for
        # this consumer-root (emitted_ts == 0 from a missing file), enforce
        # by denying so the banner is emitted on turn 1.
        if emitted_ts == 0 and not os.path.exists(emitted_path):
            return (
                "Session banner not yet emitted for this SessionStart event. "
                "Per AGENTS.md Session Start Check, emit the banner as the first "
                "content of your response, then Write to "
                f"{emitted_path} with content '{{\"ts\": {event_ts}}}' "
                "to acknowledge. Only then retry this tool call. To bypass, set "
                "AGENT_CONFIG_GATES=off in ~/.claude/settings.json env."
            )
        # Re-arm semantics: ack file already exists, so the banner was
        # emitted at least once this consumer-root. A later SessionStart
        # (clear, a leaked compact, or a startup whose banner the agent
        # did not acknowledge) must not block in-flight skill tool calls.
        # Re-emit advisory: pass through; the agent re-emits the banner on
        # its next textual response if it chooses (issue anywhere-agents#7).
        # session_bootstrap no longer writes an event for resume, so a resume
        # creates no new re-arm; an event already pending before it still
        # reaches here.
        sys.stderr.write(
            f"[banner-gate] SessionStart re-fire detected (event_ts={event_ts}, "
            f"emitted_ts={emitted_ts}). Re-emit advisory; tool call allowed.\n"
        )
        return None

    return None


def _quote_aware_split_on_operators(cmd):
    """Split ``cmd`` on UNQUOTED shell control operators ``&&``, ``||``,
    and ``;``. Returns ``[seg, op, seg, op, ..., seg]`` matching the
    output shape of ``re.split(r"(...)")`` (operators at odd indices).

    Honors POSIX-shell quoting:

      - ``'...'`` single quotes: literal, no escaping inside.
      - ``"..."`` double quotes: literal for our purposes (we do not
        expand ``$()`` or ``$var``; we just preserve content as-is).
      - Backslash escape outside quotes (``\\<char>``): next char is
        literal (so ``\\&\\&`` is two literal ``&`` chars, not an operator).

    Round 2 review M2-reopen fix: the old raw regex ``re.split(r"&&|;|\\|\\|")``
    was quote-blind. It misclassified ``cd "a || b"`` (a single cd to a
    directory literally named ``a || b``) as compound-cd. The walker below
    correctly treats operators inside quotes as path content.
    """
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(cmd)
    in_single = False
    in_double = False
    while i < n:
        ch = cmd[i]
        # Backslash escape (POSIX outside single quotes): the next char is
        # literal. Inside single quotes, backslash has no special meaning.
        if ch == "\\" and not in_single and i + 1 < n:
            buf.append(ch)
            buf.append(cmd[i + 1])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            # Look for two-char operators first; ; is the only one-char.
            if i + 1 < n and cmd[i:i + 2] == "&&":
                parts.append("".join(buf))
                parts.append("&&")
                buf = []
                i += 2
                continue
            if i + 1 < n and cmd[i:i + 2] == "||":
                parts.append("".join(buf))
                parts.append("||")
                buf = []
                i += 2
                continue
            if ch == ";":
                parts.append("".join(buf))
                parts.append(";")
                buf = []
                i += 1
                continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def check_cd_compound(cmd):
    """Check if command contains a cd that chains into another command.

    Returns True/False for backward compatibility with callers that only
    care about the boolean outcome (and existing tests). Callers that want
    the contextual Suggested rewrite: hint should call
    cd_compound_deny_message(cmd) instead, which returns the full deny
    message string or None.

    Quote-aware (Round 2 M2 fix): only UNQUOTED ``&&`` / ``||`` / ``;``
    count as operators. ``cd "a && b"`` is a single cd to a directory
    literally named ``a && b`` and is NOT a compound command.
    """
    parts = _quote_aware_split_on_operators(cmd)
    # Walk segments (even indices). A segment that starts with ``cd `` and
    # is followed by another segment (i.e. an operator chained a next
    # command) is a compound-cd.
    for i in range(0, len(parts), 2):
        seg = parts[i].strip()
        if seg.startswith("cd ") and i + 2 < len(parts):
            return True
    return False


def _next_segment_after_cd(cmd):
    """Return ``(cd_path, separator, follow)`` for the first compound-cd
    match. ``separator`` is one of ``&&``, ``;``, ``||`` — the literal
    operator chaining the cd into the next command. Returns
    ``(None, None, None)`` when no compound-cd match exists.

    Capturing the separator matters for the deny-message reroute suggestion:
    ``&&`` and ``;`` chain a follow-up command that wants to run inside the
    target directory (legitimate reroute target — `git -C` for git,
    path-arg for the rest). ``||`` chains a failure handler that runs only
    when ``cd`` itself fails, so the follow-up is NOT a command to run
    inside the directory and the suggested rewrite must not pretend it is.

    Quote-aware (Round 2 M2 fix): operators inside ``'...'`` / ``"..."`` /
    escaped via ``\\<char>`` are treated as literal path content. See
    ``_quote_aware_split_on_operators``.
    """
    parts = _quote_aware_split_on_operators(cmd)
    for i in range(0, len(parts), 2):
        seg_stripped = parts[i].strip()
        if seg_stripped.startswith("cd ") and i + 2 < len(parts):
            cd_tokens = seg_stripped.split(None, 1)
            cd_path = cd_tokens[1].strip() if len(cd_tokens) > 1 else ""
            separator = parts[i + 1]
            # Find next non-empty segment after this cd; we may chain across
            # several empty segments (e.g. ``cd /tmp ;; ls``).
            for j in range(i + 2, len(parts), 2):
                follow_stripped = parts[j].strip()
                if follow_stripped:
                    return cd_path, separator, follow_stripped
            return cd_path, separator, ""
    return None, None, None


def _operators_after_cd(cmd):
    """Return the set of operator strings (``"&&"`` / ``"||"`` / ``";"``)
    that appear AFTER the first compound ``cd`` segment. Used to detect
    mixed-operator chains where a single suggested-rewrite cannot capture
    the user's full intent.
    """
    parts = _quote_aware_split_on_operators(cmd)
    for i in range(0, len(parts), 2):
        seg = parts[i].strip()
        if seg.startswith("cd ") and i + 2 < len(parts):
            return {parts[j] for j in range(i + 1, len(parts), 2)}
    return set()


def cd_compound_deny_message(cmd):
    """Return the deny message for a compound-cd command, with an inline
    Suggested rewrite: line that lifts the path and the chained command
    so the agent can copy the reroute directly. Returns None if cmd is not
    a compound-cd violation.

    Separator-aware suggestions:
      - ``cd <path> && <cmd>`` or ``cd <path>; <cmd>`` (sequencing): the
        follow-up command is meant to run inside ``<path>``, so we lift it
        to ``git -C <path> <rest>`` for git or to a path-as-argument hint
        for other commands.
      - ``cd <path> || <handler>`` (failure handler): the follow-up only
        runs when ``cd`` fails, so it is not a command to run inside the
        directory. Suggest splitting into two separate tool calls without
        suggesting any rewrite that runs ``handler`` inside ``<path>``.
      - Mixed ``||`` plus ``&&`` (Round 2 M2): the user wants both a
        failure handler AND a success follow-up; no single one-liner
        captures both. Suggest splitting into explicit steps.
    """
    if not check_cd_compound(cmd):
        return None
    cd_path, separator, follow = _next_segment_after_cd(cmd)
    operators = _operators_after_cd(cmd)
    has_mixed_modes = ("||" in operators) and ("&&" in operators or ";" in operators)
    if has_mixed_modes:
        # Mixed failure-and-success operators — a one-liner rewrite cannot
        # preserve user intent. Fall back to a generic split-into-steps
        # message that names the offending path for context.
        suggestion = (
            f"split into explicit steps (this chain mixes `||` failure "
            f"and `&&`/`;` success operators after `cd {cd_path}`, which "
            f"no single rewrite captures); use separate tool calls so "
            f"each step's success/failure handling stays explicit"
        )
    elif separator == "||":
        # Failure handler — do NOT suggest running the handler in cd_path;
        # that would be incorrect (it runs only when cd fails). Suggest
        # splitting the construct into two separate tool calls.
        suggestion = (
            f"split the construct into two tool calls — first attempt the "
            f"directory change separately (e.g., `git -C {cd_path} <cmd>` for "
            f"git, or check the path exists), then handle the failure path "
            f"with a separate command"
        )
    elif follow and follow.split(None, 1)[0] == "git" and cd_path:
        # git chained with `&&` / `;` — lift to git -C
        git_rest = follow.split(None, 1)[1] if " " in follow else ""
        suggestion = f"`git -C {cd_path} {git_rest}`".rstrip("` ").rstrip() + "`"
    elif follow and cd_path:
        suggestion = (
            f"run `{follow}` from `{cd_path}` (pass path as an argument or "
            f"use a separate tool call with cwd)"
        )
    else:
        suggestion = (
            "use `git -C <path> <cmd>` for git, or pass the path as an "
            "argument instead of cd-ing first"
        )
    return (
        "Compound cd command blocked (Claude Code flags `cd <path> && <cmd>` "
        "for approval even when both halves are individually allowed). "
        f"Suggested rewrite: {suggestion}. "
        "To bypass this guard set AGENT_COMPOUND_CD_HOOK=off in "
        "~/.claude/settings.json env."
    )


# A `git init` inside a directory that is already inside a repository creates a
# second repository the outer one cannot see. Git stays quiet about it, because
# the usual home for such a directory is an ignored one, so `git status` in the
# parent never mentions it. IDEs are the surface where it lands: PyCharm and VS
# Code both scan for nested `.git` directories and register each as a VCS root,
# after which every file staged in one appears in the changes view beside real
# work. Measured on this machine: four review packets left in one proposal repo
# over a single day held 85 staged-and-never-committed files across four roots,
# and the IDE offered to commit all of them under one checkbox (aa#56).
#
# The reroute is the session scratch directory, which is where the skills that
# carry text between agents are already documented to write. A deliberate inner
# repository, a submodule most often, sets the escape env for that one call.
_GIT_INIT_FLAGS_WITH_VALUES = (
    "--separate-git-dir", "--template", "--initial-branch", "-b",
    # Both take a value that reads as a path when skipped, so omitting them let
    # `git init --object-format sha1 <target>` publish a repository the gate
    # believed was named `sha1`.
    "--object-format", "--ref-format",
)


# What this gate will and will not read.
#
# Five review rounds established one pattern: every attempt to recover git's
# arguments from arbitrary shell text produced a new false positive. Reading a
# redirection as the target denied a directory that never existed. Splitting on
# newlines turned heredoc bodies into commands. Stripping heredocs then deleted
# real commands from the opener line. Ending a word at an operator, which is
# ordinary shell semantics, then read an escaped operator as one. Each repair
# was correct about the case it named and wrong about a neighbouring one.
#
# So the gate stops trying. It reads commands whose shape it can account for,
# and declines the rest rather than guessing. The two are not symmetric: a false
# positive blocks work an agent is entitled to do and no rewrite repairs it,
# while a declined command merely behaves as it did before this gate existed.
# A blind spot restores the status quo; a wrong deny creates a new problem.
#
# Declined, and documented as such: any command carrying a heredoc, because its
# body is data that reads exactly like commands; any single command carrying a
# redirection, because its operands are not arguments; a PowerShell block
# comment, for the heredoc's reason; a backslash or backtick immediately before
# a quote, because an escape moves where a quoted region ends; and, at a
# possible comment start, a boundary character that is itself preceded by either
# escape character. An escaped hash is not that shape, since an escape is not a
# boundary character, so `\#` stays the literal text a shell reads it as. Both
# escape checks ignore shell-specific escape rules and escape parity, so they
# decline a doubled escape as readily as a single one.
# `git init` through those forms is not how the packets in #56 were created.
_HEREDOC = re.compile(r"(?<!<)<<(?!<)")

# A shell word ends at an operator as well as at whitespace, so `;#` and `|#`
# begin comments too. Bash and PowerShell agree on that, and reading them as
# arguments turned a printed note into a denied command.
_WORD_BOUNDARIES = " \t\n;|&("

# Shapes whose extent this gate cannot establish, and therefore will not judge.
# A PowerShell block comment carries its body across lines the way a heredoc
# does. An escaped quote moves where a quoted region ends, and the splitter and
# tokenizer downstream repeat the same assumption, so recognizing it in one
# place would not be enough.
_BLOCK_COMMENT = "<#"
_QUOTE_ESCAPES = ('\\"', "\\'", '`"', "`'")


def _executable_text(cmd):
    """Strip comments, or return None when the command must not be judged.

    One quote-aware pass over the whole string rather than per line: resetting
    quote state at each newline made a multiline quoted argument containing a
    `#` look like a comment, which deleted a closing quote and made ordinary
    text that followed appear quoted.
    """
    # An escape before a quote moves the end of the quoted region, and every
    # helper below assumes it does not. Decline rather than mis-track: a
    # command example captured inside a double-quoted string is ordinary text,
    # and this file's own workflows carry them.
    if any(escape in cmd for escape in _QUOTE_ESCAPES):
        return None
    out = []
    i = 0
    n = len(cmd)
    in_single = in_double = False
    while i < n:
        ch = cmd[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            # `<<<` is a here-string, whose word sits on the same line, so it
            # is an ordinary redirection and the segment is declined below
            # rather than the whole command here.
            if ch == "<" and _HEREDOC.match(cmd, i):
                return None
            if cmd.startswith(_BLOCK_COMMENT, i):
                return None
            if ch == "#" and (i == 0 or cmd[i - 1] in _WORD_BOUNDARIES):
                # An escaped operator is literal, so it does not end a word
                # and what follows `#` is not a comment. Stripping there
                # deleted a string opener and exposed its contents as
                # commands. The test is the character before the boundary,
                # not before the `#`, so `\#` never reaches it: an escape is
                # not a boundary character, which leaves the hash as the
                # literal argument text every shell reads it as. Escape
                # parity is ignored, so a doubled escape declines like a
                # single one: the policy is to stop rather than to decide
                # whose escape rules apply, as with the quote escapes.
                if i >= 2 and cmd[i - 2] in ("\\", "`"):
                    return None
                newline = cmd.find("\n", i)
                if newline == -1:
                    break
                i = newline
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _carries_redirection(segment):
    """True when a segment redirects, so its tokens are not all arguments."""
    in_single = in_double = False
    for ch in segment:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double and ch in "<>":
            return True
    return False


def git_init_target(parts, cwd):
    """Return the directory a `git init` would create a repository in.

    `parts` is the tokenized command. Returns None when this is not a git init.
    A `git init` with no path argument targets the working directory, which is
    what the improvised packets did.

    The leading executable is checked here rather than assumed. Every other
    caller of ``extract_git_subcommand`` has already established that the
    command is git, and that helper simply starts scanning at token 1, so
    calling it without the check denied `npm init`, `cargo init`, `yarn init`,
    `terraform init` and even `echo init` inside any repository.
    """
    if not parts or _basename(parts[0]) != "git":
        return None
    idx, sub = extract_git_subcommand(parts)
    if sub != "init":
        return None
    # A global `-C <path>` changes the directory git runs in, so it decides
    # where a relative target lands. Reading it is not optional here: the
    # compound-cd gate in this same file tells agents to prefer `git -C`, so it
    # is the form they are steered toward.
    i = 1
    while i < idx:
        token = parts[i]
        if token == "-C" and i + 1 < idx:
            cwd = os.path.join(cwd, parts[i + 1])
            i += 2
        elif token.startswith("-C") and len(token) > 2:
            cwd = os.path.join(cwd, token[2:])
            i += 1
        elif token in _GIT_VALUE_FLAGS:
            i += 2
        else:
            i += 1
    cwd = os.path.realpath(cwd)
    rest = parts[idx + 1:]
    skip_next = False
    for token in rest:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            # `--flag=value` carries its value inline; the listed flags take a
            # separate argument that must not be read as the target path.
            if "=" not in token and token in _GIT_INIT_FLAGS_WITH_VALUES:
                skip_next = True
            continue
        target = token if os.path.isabs(token) else os.path.join(cwd, token)
        # realpath resolves `..` as well as symlinks. Walking lexical parents
        # of `<repo>/../outside/fresh` revisits `<repo>`, finds its `.git`, and
        # denies a target that git would create outside the worktree.
        return os.path.realpath(target)
    return cwd


def nested_git_init_deny_message(cmd, cwd):
    """Deny message when a git init would nest inside an existing worktree.

    Returns None when the command is not a git init, or when its target is not
    inside a repository, which is the ordinary case this must not disturb.
    """
    # _split_subcommands rather than the compound-cd splitter: this one also
    # breaks on newlines and single pipes, both of which carried a git init
    # past the gate. A leading PowerShell call operator is dropped so
    # `& git init` is seen as the command it runs.
    text = _executable_text(cmd)
    if text is None:
        return None
    for segment in _split_subcommands(text):
        if _carries_redirection(segment):
            continue
        # _tokenize_shell rather than shlex: it does not treat a backslash as
        # an escape, so a Windows target path survives tokenizing. shlex in
        # posix mode mangled a backslash-separated Windows target, which then
        # read as a relative path and pointed the check at the wrong directory.
        stripped = segment.strip()
        # PowerShell's call operator binds with or without a space, and
        # `&git init` reached the worktree untouched while `& git init` did not.
        if stripped.startswith("&") and not stripped.startswith("&&"):
            stripped = stripped[1:].lstrip()
        parts = _tokenize_shell(stripped)
        if not parts:
            continue
        try:
            target = git_init_target(strip_wrappers(parts), cwd)
        except (ValueError, IndexError):
            continue
        if target is None:
            continue
        # `target` itself, not a child of it: _inside_git_worktree starts at the
        # parent of what it is given, so passing the target asks whether the
        # target's surroundings are a repository. Passing a child asked whether
        # the target is one, which denied re-initializing an existing root even
        # though that creates nothing nested.
        if not _inside_git_worktree(target):
            continue
        return (
            "Nested git init blocked: this would create a second repository "
            f"inside an existing worktree, at {target}. Git will not mention "
            "it again, but IDEs register each nested .git as a VCS root, and "
            "its staged files then appear in the changes view beside real "
            "work (anywhere-agents#56). Suggested rewrite: create it under "
            "the session scratch directory instead, which is where carried "
            "artifacts belong. For a deliberate inner repository such as a "
            "submodule, set AGENT_NESTED_GIT_INIT_HOOK=off in "
            "~/.claude/settings.json env for that call."
        )
    return None


def strip_wrappers(parts):
    """Skip env, inline VAR=VALUE, and standard command-prefix wrappers
    (sudo / doas) so a dangerous command behind a transparent prefix is still
    classified. `_basename` is used for env/sudo/doas so a path-qualified prefix
    (e.g. /usr/bin/env, /usr/bin/sudo) is also stripped."""
    # env flags that consume the next token
    _env_value_flags = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}
    # sudo / doas flags that consume the next token
    _sudo_value_flags = {
        "-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
        "-C", "--close-from", "-T", "--command-timeout", "-R", "--chroot",
        "-D", "--chdir",
    }
    i = 0
    while i < len(parts):
        if _basename(parts[i]) == "env":
            i += 1
            # Skip env flags and their values
            while i < len(parts) and parts[i].startswith("-"):
                if parts[i] in _env_value_flags and i + 1 < len(parts):
                    i += 2
                else:
                    i += 1
            # Skip VAR=VALUE pairs
            while i < len(parts) and "=" in parts[i] and not parts[i].startswith("-"):
                i += 1
        elif _basename(parts[i]) in ("sudo", "doas"):
            i += 1
            # Skip sudo/doas flags and their values
            while i < len(parts) and parts[i].startswith("-"):
                flag = parts[i].split("=", 1)[0]
                if flag in _sudo_value_flags and "=" not in parts[i] and i + 1 < len(parts):
                    i += 2
                else:
                    i += 1
        elif _basename(parts[i]) in ("command", "nohup", "setsid"):
            # Transparent prefix runners that carry only boolean flags
            # (command -p/-v/-V, setsid -f/-c/-w, nohup none).
            i += 1
            while i < len(parts) and parts[i].startswith("-"):
                i += 1
        elif (
            "=" in parts[i]
            and not parts[i].startswith("-")
            and len(parts[i]) > 0
            and parts[i][0].isalpha()
        ):
            # Inline VAR=VALUE before command
            i += 1
        else:
            break
    return parts[i:]


# Git global options that consume the next token as a value.
# Options with = form (--git-dir=<path>) are handled by the "=" check.
_GIT_VALUE_FLAGS = {
    "-C", "-c",
    "--exec-path", "--git-dir", "--work-tree", "--namespace",
    "--super-prefix", "--config-env", "--attr-source", "--list-cmds",
}


def extract_git_subcommand(parts):
    """Skip git global flags and their values, return (index, subcommand)."""
    i = 1  # skip "git"
    while i < len(parts):
        if parts[i] in _GIT_VALUE_FLAGS and i + 1 < len(parts):
            i += 2
        elif parts[i].startswith("-"):
            if "=" in parts[i]:
                i += 1  # --flag=value consumed as one token
            else:
                i += 1  # boolean flag
        else:
            break
    return (i, parts[i]) if i < len(parts) else (i, "")


# gh inherited flags that consume the next token.
_GH_VALUE_FLAGS = {"-R", "--repo", "--hostname"}


def extract_gh_subcommand(parts):
    """Skip gh flags (before and after group), return (group, action)."""
    i = 1  # skip "gh"
    # Skip flags before group
    while i < len(parts):
        if parts[i] in _GH_VALUE_FLAGS and i + 1 < len(parts):
            i += 2
        elif parts[i].startswith("-"):
            i += 1
        else:
            break
    group = parts[i] if i < len(parts) else ""
    i += 1
    # Skip flags between group and action (inherited flags like -R can appear here)
    while i < len(parts):
        if parts[i] in _GH_VALUE_FLAGS and i + 1 < len(parts):
            i += 2
        elif parts[i].startswith("-"):
            i += 1
        else:
            break
    action = parts[i] if i < len(parts) else ""
    return group, action


def check_git_destructive(parts):
    """Check if a git command is destructive."""
    idx, sub = extract_git_subcommand(parts)
    if not sub:
        return False
    rest = parts[idx:]

    if sub in ("push", "commit", "merge", "rebase", "clean"):
        return True
    if sub == "reset":
        return "--hard" in rest
    if sub == "checkout":
        return "--" in rest
    if sub == "branch":
        return any(f in rest for f in ("-D", "-d", "--delete"))
    if sub == "tag":
        return any(f in rest for f in ("-d", "--delete"))
    if sub == "stash":
        return len(rest) > 1 and rest[1] in ("drop", "clear")
    return False


def check_gh_destructive(parts):
    """Check if a gh command is destructive or outward-facing (publish)."""
    group, action = extract_gh_subcommand(parts)
    destructive = {
        ("pr", "create"), ("pr", "merge"),
        ("pr", "close"), ("repo", "delete"),
        ("release", "create"), ("release", "delete"),
        ("release", "upload"), ("release", "edit"),
    }
    return (group, action) in destructive


# --- Mandatory risk classification (Check 4, tool-agnostic) ----------------
#
# The mandatory ask set is: destructive git, destructive/publish gh, outward
# package publishes, and irreversible file/device deletes. Classification is
# tool-agnostic (Bash + PowerShell), keys on the EXACT leading token of each
# sub-command (never a substring scan, which would false-positive on
# `echo "rm -rf"` / `grep "git push"`), and recurses through built-in
# command-carrying wrappers so a dangerous payload inside `ssh host "..."`,
# `bash -c "..."`, or `docker exec c bash -lc "..."` is still caught.
#
# `python -c` and custom/private wrappers (e.g. a personal job-runner) are NOT
# pierced: their argument semantics are not inferable from the command text,
# and treating them as opaque (like a script file) is the documented limit.

MAX_WRAPPER_DEPTH = 3

# Built-in wrappers whose payload semantics are universally known.
_BASH_C_WRAPPERS = frozenset({"bash", "sh", "zsh", "dash", "ash"})
_PS_C_WRAPPERS = frozenset({"pwsh", "powershell"})

# ssh / docker option flags that consume the following token as a value, so the
# host / container token is identified correctly when finding the payload.
_SSH_VALUE_FLAGS = frozenset({
    "-p", "-i", "-o", "-l", "-F", "-b", "-c", "-D", "-e", "-E", "-I", "-J",
    "-L", "-m", "-O", "-Q", "-R", "-S", "-W", "-w",
})
_DOCKER_VALUE_FLAGS = frozenset({
    "-u", "--user", "-e", "--env", "--env-file", "-w", "--workdir", "--name",
    "-v", "--volume", "--mount", "--entrypoint", "-l", "--label", "--label-file",
    "--network", "--net", "-p", "--publish", "--device", "--add-host",
    "--cidfile", "--volumes-from", "--gpus", "-m", "--memory", "--restart",
    "--log-driver", "--log-opt", "--tmpfs", "-h", "--hostname", "--ulimit",
    "--sysctl", "--security-opt", "--cap-add", "--cap-drop", "--dns", "--link",
    "--expose", "--shm-size", "--stop-signal", "--pid", "--ipc", "--runtime",
})
# bash/sh/zsh options that consume a value, skipped when scanning for `-c`
# (so `bash -o pipefail -c "..."` / `bash --rcfile f -c "..."` is pierced).
_BASH_VALUE_FLAGS = frozenset({"-o", "-O", "--rcfile", "--init-file"})
# PowerShell parameters that consume a value, skipped when scanning for
# `-Command` (so `powershell -ExecutionPolicy Bypass -Command "..."` is pierced).
_PS_VALUE_FLAGS = frozenset({
    "-executionpolicy", "-ex", "-ep", "-inputformat", "-outputformat",
    "-configurationname", "-args", "-file", "-windowstyle",
})
# docker GLOBAL options (before exec/run) that consume a value.
_DOCKER_GLOBAL_VALUE_FLAGS = frozenset({
    "-H", "--host", "--context", "--config", "--log-level",
    "--tlscacert", "--tlscert", "--tlskey",
})
# npm / twine global options that consume a value, skipped before resolving the
# publish subcommand (so `npm --registry <url> publish` is still classified).
_NPM_VALUE_FLAGS = frozenset({
    "--registry", "--userconfig", "--prefix", "-w", "--workspace",
    "--scope", "--otp", "--tag", "--access", "--cache", "--loglevel",
})
_TWINE_VALUE_FLAGS = frozenset({
    "--repository", "--repository-url", "-r", "-u", "--username",
    "-p", "--password", "--config-file", "--sign-with", "--cert",
    "--client-cert",
})
# timeout DURATION COMMAND ...: flags that consume the next token before DURATION.
_TIMEOUT_VALUE_FLAGS = frozenset({"-k", "--kill-after", "-s", "--signal"})
# xargs [opts] COMMAND ...: option flags that consume the next token. -i/-l are
# deprecated optional-arg flags and are intentionally treated as boolean.
_XARGS_VALUE_FLAGS = frozenset({
    "-a", "--arg-file", "-d", "--delimiter", "-E", "-I", "--replace",
    "-L", "--max-lines", "-n", "--max-args", "-P", "--max-procs",
    "-s", "--max-chars",
})


def _basename(path):
    """Lowercased final path component with a common executable suffix
    stripped, so `C:\\...\\bash.exe`, `/usr/bin/bash`, and `bash` all compare
    equal to `bash`."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _tokenize_shell(s):
    """Quote-aware whitespace tokenizer that does NOT treat backslash as an
    escape (so Windows paths survive) and strips surrounding matched quotes.
    Used for PowerShell-shaped text and for re-tokenizing wrapper payloads."""
    tokens = []
    buf = []
    in_single = in_double = False
    for ch in s:
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if ch in " \t\r\n" and not in_single and not in_double:
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


def _tok(subcmd, shell):
    """Tokenize one sub-command for classification. Bash/legacy uses shlex
    (preserving the existing git/gh detection behavior exactly); PowerShell
    uses the backslash-preserving tokenizer and strips a leading call
    operator `&`."""
    if shell == "powershell":
        toks = _tokenize_shell(subcmd)
        if toks and toks[0] == "&":
            toks = toks[1:]
        return toks
    try:
        return shlex.split(subcmd)
    except ValueError:
        return subcmd.split()


def _split_subcommands(cmd):
    """Split into sub-commands at UNQUOTED ``;`` ``&&`` ``||`` ``|`` and
    newlines. Quote-aware: operators inside ``'...'`` / ``"..."`` are literal.
    Operators inside a quoted wrapper payload are NOT split here — they are
    handled when the payload is re-classified one recursion level down."""
    segs = []
    buf = []
    i = 0
    n = len(cmd)
    in_single = in_double = False
    while i < n:
        ch = cmd[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            if cmd[i:i + 2] in ("&&", "||"):
                segs.append("".join(buf))
                buf = []
                i += 2
                continue
            if ch in ";|\n":
                segs.append("".join(buf))
                buf = []
                i += 1
                continue
        buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return [s.strip() for s in segs if s.strip()]


def _first_positional(tokens, value_flags):
    """First non-option token, skipping option flags and the value after any
    flag in `value_flags` (unless given as `--flag=value`)."""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if not t.startswith("-"):
            return t
        if t.split("=", 1)[0] in value_flags and "=" not in t and i + 1 < len(tokens):
            i += 2
        else:
            i += 1
    return None


def check_publish(parts):
    """Outward-facing package publish. Returns a label or None. Global options
    before the subcommand are skipped (e.g. `npm --registry <url> publish`,
    `twine --repository pypi upload`)."""
    if not parts:
        return None
    head = _basename(parts[0])
    rest = parts[1:]
    if head in ("npm", "pnpm", "yarn"):
        sub = _first_positional(rest, _NPM_VALUE_FLAGS)
        return f"{head} {sub}" if sub in ("publish", "unpublish") else None
    if head == "twine":
        sub = _first_positional(rest, _TWINE_VALUE_FLAGS)
        return "twine upload" if sub == "upload" else None
    if (head == "py" or re.fullmatch(r"python\d*(?:\.\d+)*", head)) and "-m" in rest:
        mi = rest.index("-m")
        if mi + 1 < len(rest) and rest[mi + 1] == "twine" and "upload" in rest[mi + 2:]:
            return "python -m twine upload"
    return None


def check_fs_destructive(parts, shell):
    """Irreversible file/device destruction. Returns a label or None.

    Bash: `rm` with BOTH recursive and force flags (mirrors the existing
    `rm -rf` / `rm -fr` / `rm -r -f` native ask rules), plus `dd`, `mkfs*`,
    `shred`. PowerShell: `Remove-Item` and its delete aliases when a recursive
    flag (`-Recurse` / `-r` / `/s`) is present."""
    if not parts:
        return None
    head = parts[0]
    name = _basename(head)
    if shell == "powershell":
        if name in ("remove-item", "ri", "rm", "del", "erase", "rd", "rmdir"):
            for t in parts[1:]:
                tl = t.lower()
                # PowerShell accepts unambiguous parameter prefixes; -Recurse
                # is the only Remove-Item parameter starting "Re", so -Re / -Rec
                # / -Recu / -Recurse all mean recursive. /s is the cmd-style form.
                if tl == "/s" or tl == "-r" or tl.startswith("-re"):
                    return f"{head} (recursive delete)"
        return None
    if name == "rm":
        flags = [f for f in parts[1:] if f.startswith("-")]
        has_r = any(
            f in ("-r", "-R", "--recursive")
            or (not f.startswith("--") and ("r" in f or "R" in f))
            for f in flags
        )
        has_f = any(
            f in ("-f", "--force")
            or (not f.startswith("--") and "f" in f)
            for f in flags
        )
        return "rm -rf" if (has_r and has_f) else None
    if name == "dd" or name.startswith("mkfs") or name == "shred":
        return name
    return None


def _payload_str(tokens):
    """Reconstruct a payload command string from token(s). A single token IS
    the inner command (it was a quoted shell string); multiple tokens are
    re-joined, re-quoting any token that contains whitespace so the inner
    re-tokenization preserves word boundaries."""
    if len(tokens) == 1:
        return tokens[0]
    out = []
    for t in tokens:
        if t == "" or any(c in t for c in " \t\r\n"):
            out.append('"' + t + '"')
        else:
            out.append(t)
    return " ".join(out)


def _find_c_flag(parts):
    """Index of the bash/sh/zsh command-string flag (`-c`, `-lc`, `-xc`, ...),
    skipping value options like `-o pipefail` / `--rcfile f` / `-O extglob`, or
    None if the first non-flag token (a script file, opaque) is reached first."""
    i = 1
    while i < len(parts):
        t = parts[i]
        flag = t.split("=", 1)[0]
        if t == "-c" or (
            t.startswith("-")
            and not t.startswith("--")
            and not t.startswith(("-o", "-O"))
            and "c" in t
        ):
            return i
        if flag in _BASH_VALUE_FLAGS and "=" not in t and i + 1 < len(parts):
            i += 2
            continue
        if t.startswith(("-o", "-O")) and len(t) > 2:
            i += 1
            continue
        if not t.startswith("-"):
            return None
        i += 1
    return None


def _find_ps_command_flag(parts):
    """Index of the PowerShell `-Command` / `-c` flag, or None. Skips value-
    taking parameters (e.g. `-ExecutionPolicy Bypass`) so the flag is found
    even when options precede it."""
    i = 1
    while i < len(parts):
        tl = parts[i].lower()
        if tl == "-c" or tl == "-command" or tl.startswith("-command"):
            return i
        if tl in _PS_VALUE_FLAGS and i + 1 < len(parts):
            i += 2
            continue
        if not tl.startswith("-"):
            return None
        i += 1
    return None


def _trailing_command(parts, start, value_flags):
    """Skip option flags (and their values for `value_flags`) starting at
    `start`, then skip one positional token (the ssh host or docker
    container/image), and return the remaining tokens (the command payload)."""
    i = start
    while i < len(parts) and parts[i].startswith("-"):
        if parts[i] in value_flags and i + 1 < len(parts):
            i += 2
        else:
            i += 1
    i += 1  # skip the host / container / image positional
    return parts[i:] if i < len(parts) else []


def _command_after_flags(parts, start, value_flags):
    """Skip option flags (and their values for `value_flags`) starting at
    `start`, then return the remaining tokens. Unlike `_trailing_command` this
    skips NO positional — used for xargs, where the command follows the options
    directly with no host/image token in between."""
    i = start
    while i < len(parts) and parts[i].startswith("-"):
        if parts[i] in value_flags and i + 1 < len(parts):
            i += 2
        else:
            i += 1
    return parts[i:] if i < len(parts) else []


def _wrapper_payload(parts, shell):
    """If `parts` is a built-in command-carrying wrapper, return
    (payload_str, payload_shell); otherwise (None, None). Custom/private
    wrappers are intentionally absent — they are opaque."""
    if not parts:
        return None, None
    name = _basename(parts[0])
    if name in _BASH_C_WRAPPERS:
        idx = _find_c_flag(parts)
        if idx is not None and idx + 1 < len(parts):
            return parts[idx + 1], "bash"
        return None, None
    if name in _PS_C_WRAPPERS:
        idx = _find_ps_command_flag(parts)
        if idx is not None and idx + 1 < len(parts):
            # PowerShell -Command concatenates ALL trailing args into one command
            # string, so unquoted `powershell -Command git push` is `git push`.
            # (bash -c, by contrast, takes a single command-string token.)
            return _payload_str(parts[idx + 1:]), "powershell"
        return None, None
    if name == "ssh":
        rest = _trailing_command(parts, 1, _SSH_VALUE_FLAGS)
        return (_payload_str(rest), "bash") if rest else (None, None)
    if name == "docker":
        i = 1
        while i < len(parts) and parts[i].startswith("-"):
            flag = parts[i].split("=", 1)[0]
            if flag in _DOCKER_GLOBAL_VALUE_FLAGS and "=" not in parts[i] and i + 1 < len(parts):
                i += 2
            else:
                i += 1
        if i < len(parts) and parts[i] in ("exec", "run"):
            rest = _trailing_command(parts, i + 1, _DOCKER_VALUE_FLAGS)
            return (_payload_str(rest), "bash") if rest else (None, None)
    if name == "cmd":
        # cmd /c <command> or cmd /k <command>. The payload uses Windows verbs
        # (rmdir/del + /s) and shell-agnostic git, so classify it as powershell.
        for j in range(1, len(parts)):
            if parts[j].lower() in ("/c", "/k"):
                rest = parts[j + 1:]
                return (_payload_str(rest), "powershell") if rest else (None, None)
        return None, None
    if name == "timeout":
        # timeout [opts] DURATION COMMAND ...: skip flags, then the DURATION
        # positional, leaving the wrapped command.
        rest = _trailing_command(parts, 1, _TIMEOUT_VALUE_FLAGS)
        return (_payload_str(rest), "bash") if rest else (None, None)
    if name == "xargs":
        # find ... | xargs rm -rf: the command follows xargs's options directly.
        rest = _command_after_flags(parts, 1, _XARGS_VALUE_FLAGS)
        return (_payload_str(rest), "bash") if rest else (None, None)
    return None, None


def _cowboy(label):
    """Attention-grabbing ask message for destructive git/gh (unchanged tone)."""
    return random.choice([
        f"WHOA THERE COWBOY! {label} wants to run. Are you SURE about this?!",
        f"STOP! HAMMER TIME! A wild {label} appeared! Think before you click!",
        f"RED ALERT! {label} is trying to sneak past you. Eyes on the screen!",
        f"HEY! WAKE UP! {label} needs your blessing. Do not sleepwalk through this!",
        f"A {label} walks into a bar. The bartender says: 'Are you authorized?'",
    ])


def classify_command(cmd, shell, depth=0):
    """Return (decision, reason) where decision is 'ask' or None. Splits cmd
    into sub-commands, classifies each by exact leading token, and recurses
    through built-in command wrappers up to MAX_WRAPPER_DEPTH. Returns the
    first ask hit. NOT gated by any escape env (callers invoke it
    unconditionally) — human approval is the contract for the mandatory set."""
    for sub in _split_subcommands(cmd):
        parts = strip_wrappers(_tok(sub, shell))
        if not parts:
            continue
        # Encoded PowerShell command: payload is base64 and cannot be inspected,
        # so fail closed with ASK. Match -e / -en* (abbreviations of
        # -EncodedCommand); -ex* is -ExecutionPolicy and is deliberately excluded.
        if _basename(parts[0]) in _PS_C_WRAPPERS and any(
            t.lower() == "-e" or t.lower().startswith("-en") for t in parts[1:]
        ):
            return "ask", (
                "Approval needed: encoded PowerShell command "
                "(-EncodedCommand); the payload cannot be inspected."
            )
        payload, payload_shell = _wrapper_payload(parts, shell)
        if payload is not None:
            if depth + 1 > MAX_WRAPPER_DEPTH:
                return "ask", (
                    f"Approval needed: command nesting exceeds depth "
                    f"{MAX_WRAPPER_DEPTH} inside a `{parts[0]}` wrapper; the "
                    f"guard cannot verify the innermost payload is safe."
                )
            decision, reason = classify_command(payload, payload_shell, depth + 1)
            if decision:
                return decision, reason
            continue
        head = _basename(parts[0])
        if head == "git" and check_git_destructive(parts):
            _, sub_g = extract_git_subcommand(parts)
            return "ask", _cowboy(f"git {sub_g}")
        if head == "gh" and check_gh_destructive(parts):
            group, action = extract_gh_subcommand(parts)
            return "ask", _cowboy(f"gh {group} {action}")
        pub = check_publish(parts)
        if pub:
            return "ask", (
                f"Approval needed: `{pub}` publishes to a shared registry or "
                f"remote (outward-facing, hard to reverse)."
            )
        fs = check_fs_destructive(parts, shell)
        if fs:
            return "ask", f"Approval needed: `{fs}` is an irreversible delete."
    return None, None


# --- Codex ------------------------------------------------------------------
#
# Codex 0.153.3 and later run PreToolUse hooks with Claude Code's payload and
# deny shape, and bootstrap registers this file there as `guard.py --agent
# codex`. Three differences shape the Codex path. Its shell tool arrives as
# `Bash` (a PowerShell command on Windows) and its file edits as `apply_patch`
# with the raw patch text. It has no `ask`: the decision is rejected as
# invalid and the call runs, so an approval gate denies instead and names the
# route the user owns, a `!` command typed at the Codex prompt, which no hook
# sees. The banner gate stays with Claude Code, whose acknowledgement file
# Codex never writes, and so does the compound-cd gate, which exists for a
# Claude Code permission prompt Codex does not show.
_PATCH_FILE = re.compile(r"\*\*\* (Add|Update|Delete) File: (.+)")
_CODEX_ASK = (
    " Codex hooks cannot ask for approval, so the call is denied. Show the "
    "user the command; once they approve, they run it themselves by typing "
    "`!` and the command at the Codex prompt."
)


def _patch_files(patch):
    """Return `[action, source, destination, hunks]` for each file an
    apply_patch adds or updates.

    A hunk is `[anchor, eof, body]`: the text after an `@@ ` marker (None for
    a bare `@@`), whether `*** End of File` closes it, and its lines as
    `(mark, text)` pairs, mark one of " ", "+", "-". The destination is the
    `*** Move to:` path, else the source.
    """
    files = []
    hunk = None
    for line in patch.splitlines():
        match = _PATCH_FILE.match(line)
        if match:
            hunk = None
            if match.group(1) != "Delete":
                path = match.group(2).strip()
                hunk = [None, False, []]
                files.append([match.group(1), path, path, [hunk]])
        elif hunk is None:
            continue
        elif line == "*** End of File":
            hunk[1] = True
        elif line.startswith("*** End"):
            continue
        elif line.startswith("*** Move to: "):
            files[-1][2] = line[len("*** Move to: "):].strip()
        elif line.startswith("@@"):
            hunk = [line[3:] if line.startswith("@@ ") else None, False, []]
            files[-1][3].append(hunk)
        else:
            mark = line[:1] if line[:1] in ("+", "-") else " "
            hunk[2].append((mark, line[1:]))
    return files


_PATCH_TYPOGRAPHY = str.maketrans({
    **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
    **dict.fromkeys("\u2018\u2019\u201a\u201b", "'"),
    **dict.fromkeys("\u201c\u201d\u201e\u201f", '"'),
    **dict.fromkeys("\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
                    "\u2009\u200a\u202f\u205f\u3000", " "),
})
_PATCH_FOLDS = (
    None,
    str.rstrip,
    str.strip,
    lambda s: s.strip().translate(_PATCH_TYPOGRAPHY),
)


def _seek(lines, target, start, eof=False):
    """Index of `target` in `lines`, searched as apply_patch searches (its
    seek_sequence.rs): from `start`, or for an end-of-file hunk from the last
    place it fits, matching exactly, then ignoring trailing whitespace, then
    surrounding whitespace, then also typographic dashes, quotes, and spaces.
    None when nothing matches."""
    if not target:
        return start
    if len(target) > len(lines):
        return None
    if eof:
        start = max(start, len(lines) - len(target))
    for fold in _PATCH_FOLDS:
        have = lines if fold is None else [fold(line) for line in lines]
        want = target if fold is None else [fold(line) for line in target]
        for i in range(start, len(lines) - len(target) + 1):
            if have[i:i + len(target)] == want:
                return i
    return None


def _patched_document(old_lines, hunks):
    """Apply `hunks` to `old_lines` as apply_patch does (its file_update.rs)
    and return the result as `(added, line)` pairs, or None when a hunk does
    not match, which Codex rejects too."""
    replacements = []
    index = 0
    for anchor, eof, body in hunks:
        if not body:
            continue
        if anchor is not None:
            at = _seek(old_lines, [anchor], index)
            if at is None:
                return None
            index = at + 1
        old = [text for mark, text in body if mark != "+"]
        new = [(mark == "+", text) for mark, text in body if mark != "-"]
        if not old:
            replacements.append((len(old_lines), 0, new))
            continue
        at = _seek(old_lines, old, index, eof)
        if at is None and old[-1] == "":
            # A trailing empty line can stand for the file's final newline.
            old = old[:-1]
            if new and new[-1][1] == "":
                new = new[:-1]
            at = _seek(old_lines, old, index, eof)
        if at is None:
            return None
        replacements.append((at, len(old), new))
        index = at + len(old)
    doc, cursor = [], 0
    for at, length, new in sorted(replacements, key=lambda r: r[0]):
        doc.extend((False, line) for line in old_lines[cursor:at])
        doc.extend(new)
        cursor = at + length
    doc.extend((False, line) for line in old_lines[cursor:])
    return doc


def check_patch_prose_write(patch, cwd=None):
    """Run the Write gate over the lines each Codex `apply_patch` adds.

    An updated file is patched in memory and code spans are found on the
    whole result, so a line inside a fence stays exempt as code wherever the
    fence opens. Only the added lines are then scanned, which lets a hunk that
    removes a word pass. When the file cannot be read or a hunk does not
    match it, the added lines are judged alone.
    """
    for action, source, path, hunks in _patch_files(patch):
        if cwd and not os.path.isabs(path):
            path = os.path.join(cwd, path)
        ext = prose_extension(path, agent_io_requires_scratch=True)
        if ext is None:
            continue
        doc = None
        if action == "Add":
            doc = _patched_document([], hunks)
        else:
            if cwd and not os.path.isabs(source):
                source = os.path.join(cwd, source)
            try:
                with open(source, encoding="utf-8", errors="replace", newline="") as f:
                    lines = re.split(r"\r\n|\r|\n", f.read())
            except OSError:
                lines = None
            if lines is not None:
                if lines[-1] == "":
                    lines.pop()
                doc = _patched_document(lines, hunks)
        if doc is None:
            doc = [(True, text) for _, _, body in hunks
                   for mark, text in body if mark == "+"]
        text = _content_for_style_check("\n".join(line for _, line in doc), ext)
        added = [line for (is_added, _), line in zip(doc, text.split("\n"))
                 if is_added]
        deny = check_writing_style(
            "Write", {"file_path": path, "content": "\n".join(added)})
        if deny:
            return deny
    return None


def main_codex(data):
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(cmd, str) or not cmd.strip():
        return
    cwd = data.get("cwd") or os.getcwd()
    style = gates_enabled() and writing_style_enabled()
    deny = None
    if tool_name == "apply_patch":
        deny = check_patch_prose_write(cmd, cwd) if style else None
    elif tool_name == "Bash":
        cmd = cmd.strip()
        shell = "powershell" if os.name == "nt" else "bash"
        if style:
            deny = check_shell_prose_write(
                "PowerShell" if shell == "powershell" else "Bash",
                {"command": cmd}, cwd,
            )
        if not deny and nested_git_init_enabled():
            deny = nested_git_init_deny_message(cmd, cwd)
        if not deny:
            decision, reason = classify_command(cmd, shell)
            if decision == "ask":
                deny = reason + _CODEX_ASK
    if deny:
        deny = deny.replace(
            "in ~/.claude/settings.json env", "in the environment Codex starts in",
        ).replace("with the Edit tool", "with apply_patch")
        print(make_response("deny", deny))


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    if sys.argv[1:] == ["--agent", "codex"]:
        if isinstance(data, dict):
            main_codex(data)
        return

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # Check 0: auto-allow shipped trusted scripts. Runs before any deny-style
    # gate because allow decisions are unrelated to the AGENT_CONFIG_GATES
    # escape hatch and should fire for the shipped watcher regardless of
    # banner / writing-style state.
    if tool_name:
        allow_reason = check_impl_review_ps_allow(tool_name, tool_input)
        if allow_reason:
            print(make_response("allow", allow_reason))
            return

    # New gates (writing-style + banner) require an explicit tool_name. If the
    # hook input omits it (older payload format used by some tests), skip the
    # new gates and fall through to the Bash-only checks below, which run
    # whenever `tool_input.command` is populated.
    if tool_name and gates_enabled():
        # Check 1 (new): writing-style gate on prose files, written through
        # Write/Edit/MultiEdit or through a shell command.
        # Honors the AGENT_STYLE_HOOK per-guard env in addition to AGENT_CONFIG_GATES.
        advice = None
        if writing_style_enabled():
            if tool_name in ("Bash", "PowerShell"):
                deny = check_shell_prose_write(tool_name, tool_input, data.get("cwd"))
            else:
                deny = check_writing_style(tool_name, tool_input)
            if deny:
                print(make_response("deny", deny))
                return
            # Advisory pass: agent-style's deterministic findings, reported and
            # then allowed. Computed here, next to the gate it shares an escape
            # hatch with, but held until every deny-style gate below has passed.
            # stdout carries exactly one JSON document, so printing it now would
            # concatenate a second one ahead of the banner deny and cost the
            # reader that decision.
            advice = advise_writing_style(tool_name, tool_input)

        # Check 2 (new): banner gate on most tool calls until banner acknowledged.
        deny = check_banner_emission(tool_name, tool_input)
        if deny:
            print(make_response("deny", deny))
            return

        # No gate above claimed stdout, so the advisory can have it. A blocked
        # write drops its advisory on purpose: the write is not happening, and
        # two complaints about one call read as two unrelated problems.
        if advice:
            print(make_advisory_response(advice))
            return

    # Shell-tool checks below apply to Bash AND PowerShell. Legacy payloads
    # (no tool_name) are treated as Bash. Non-shell tools have no command.
    if tool_name and tool_name not in ("Bash", "PowerShell"):
        return

    cmd = tool_input.get("command", "").strip()
    if not cmd:
        return

    shell = "powershell" if tool_name == "PowerShell" else "bash"

    # Check 3: compound cd (Bash + legacy only; PowerShell statement chaining
    # differs and Claude Code's compound-cd approval is a Bash construct).
    # Honors AGENT_COMPOUND_CD_HOOK. AGENT_CONFIG_GATES does NOT disable it.
    if shell != "powershell" and compound_cd_enabled():
        deny = cd_compound_deny_message(cmd)
        if deny:
            print(make_response("deny", deny))
            return

    # Check 3b: nested git init (Bash + PowerShell). A deny rather than an ask,
    # because the reroute exists and an unattended agent can take it in one
    # turn. Honors AGENT_NESTED_GIT_INIT_HOOK.
    if nested_git_init_enabled():
        deny = nested_git_init_deny_message(cmd, os.getcwd())
        if deny:
            print(make_response("deny", deny))
            return

    # Check 4: mandatory risk classification — destructive git/gh, outward
    # publishes, irreversible file/device deletes. Tool-agnostic, sees through
    # built-in command wrappers (ssh / bash -c / docker exec / pwsh -Command).
    # NOT gated by any escape env: human approval is the contract.
    decision, reason = classify_command(cmd, shell)
    if decision == "ask":
        print(make_response("ask", reason))
        return


if __name__ == "__main__":
    main()