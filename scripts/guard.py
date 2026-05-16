"""
Claude Code PreToolUse hook guard.

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
   AGENTS.md Writing Defaults. Skips code files.
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
4. Destructive git subcommands (push, commit, merge, etc.) → ask  [Bash only]
5. Destructive gh subcommands (pr create, pr merge, etc.) → ask  [Bash only]

Escape hatch: set env var AGENT_CONFIG_GATES=off (or 0/disabled/false) to
disable the new writing-style and banner gates only. Existing Bash-level
checks (compound cd, destructive git/gh) remain active regardless.
"""
import json
import os
import random
import re
import shlex
import sys


def make_response(decision, reason):
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
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


def gates_enabled():
    """Return False if the escape-hatch env var disables the new gates."""
    val = (os.environ.get("AGENT_CONFIG_GATES") or "").strip().lower()
    return val not in ("0", "off", "false", "disabled", "no")


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


def _content_for_style_check(content, ext):
    """Strip quoted/code examples before prose style matching. Banned words
    inside code fences (``` … ```) or inline code (`foo`) are almost always
    meta-discussion (examples of what to avoid), not AI-tell prose. Stripping
    these avoids flagging legitimate style-guide documentation, generated
    CLAUDE.md / agents/codex.md files, and CHANGELOG entries that quote the
    ban list. Similarly for LaTeX verbatim / \\verb / \\texttt environments.
    """
    if ext in (".md", ".rst"):
        # Fenced code blocks (``` ... ``` or ~~~ ... ~~~)
        content = re.sub(r"(`{3,}|~{3,})[\s\S]*?\1", " ", content)
        # Double-backtick inline code (for literals containing a single backtick)
        content = re.sub(r"``[^`\n]+``", " ", content)
        # Single-backtick inline code
        content = re.sub(r"`[^`\n]+`", " ", content)
    elif ext == ".tex":
        content = re.sub(
            r"\\begin\{verbatim\}[\s\S]*?\\end\{verbatim\}",
            " ",
            content,
        )
        content = re.sub(r"\\verb(.).*?\1", " ", content)
        content = re.sub(r"\\texttt\{[^{}]*\}", " ", content)
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
    _, ext = os.path.splitext(file_path.lower())
    if ext not in PROSE_EXTENSIONS:
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
        return (
            f"Writing-style: banned AI-tell words detected in {file_path}: {hits}. "
            f"Per AGENTS.md Writing Defaults, revise without these terms "
            f"(close variants are caught too). Examples inside ``` code fences, "
            f"`inline code`, or LaTeX \\verb/\\texttt are ignored. If a real "
            f"meta-use is still blocking you, set AGENT_CONFIG_GATES=off in "
            f"~/.claude/settings.json env and retry."
        )

    return None


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
        return (
            "Session banner not yet emitted for this SessionStart event. "
            "Per AGENTS.md Session Start Check, emit the banner as the first "
            "content of your response, then Write to "
            f"{emitted_path} with content '{{\"ts\": {event_ts}}}' "
            "to acknowledge. Only then retry this tool call. To bypass, set "
            "AGENT_CONFIG_GATES=off in ~/.claude/settings.json env."
        )

    return None


def check_cd_compound(cmd):
    """Check if command contains a cd that chains into another command."""
    # Split on shell operators, preserving order
    segments = re.split(r"&&|;|\|\|", cmd)
    for i, seg in enumerate(segments):
        seg = seg.strip()
        if seg.startswith("cd ") and i < len(segments) - 1:
            return True
    return False


def strip_wrappers(parts):
    """Skip env, inline VAR=VALUE, and other wrapper prefixes."""
    # env flags that consume the next token
    _env_value_flags = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}
    i = 0
    while i < len(parts):
        if parts[i] == "env":
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
    """Check if a gh command is destructive."""
    group, action = extract_gh_subcommand(parts)
    destructive = {
        ("pr", "create"), ("pr", "merge"),
        ("pr", "close"), ("repo", "delete"),
    }
    return (group, action) in destructive


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
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
        # Check 1 (new): writing-style gate on Write/Edit/MultiEdit to prose files.
        deny = check_writing_style(tool_name, tool_input)
        if deny:
            print(make_response("deny", deny))
            return

        # Check 2 (new): banner gate on most tool calls until banner acknowledged.
        deny = check_banner_emission(tool_name, tool_input)
        if deny:
            print(make_response("deny", deny))
            return

    # Remaining checks are Bash-only. When tool_name is set, only Bash applies;
    # when tool_name is missing (legacy payload), the presence of `command` in
    # tool_input is the signal this is a Bash invocation.
    if tool_name and tool_name != "Bash":
        return

    cmd = tool_input.get("command", "").strip()
    if not cmd:
        return

    # Check 3: compound cd (on raw string, before shlex parsing)
    if check_cd_compound(cmd):
        print(make_response(
            "deny",
            "Compound cd command blocked. Use separate tool calls or path arguments."
        ))
        return

    # Parse into tokens with proper quote handling
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()  # fallback for malformed quoting

    if not parts:
        return

    # Strip wrapper prefixes (env, VAR=VALUE)
    parts = strip_wrappers(parts)
    if not parts:
        return

    # Check 4: destructive git — ask with attention-grabbing message
    if parts[0] == "git" and check_git_destructive(parts):
        _, sub = extract_git_subcommand(parts)
        warnings = [
            f"WHOA THERE COWBOY! git {sub} wants to run. Are you SURE about this?!",
            f"STOP! HAMMER TIME! A wild git {sub} appeared! Think before you click!",
            f"RED ALERT! git {sub} is trying to sneak past you. Eyes on the screen!",
            f"HEY! WAKE UP! git {sub} needs your blessing. Do not sleepwalk through this!",
            f"A git {sub} walks into a bar. The bartender says: 'Are you authorized?'",
        ]
        print(make_response("ask", random.choice(warnings)))
        return

    # Check 5: destructive gh — ask with attention-grabbing message
    if parts[0] == "gh" and check_gh_destructive(parts):
        group, action = extract_gh_subcommand(parts)
        warnings = [
            f"WHOA THERE COWBOY! gh {group} {action} wants to run. Are you SURE about this?!",
            f"STOP! HAMMER TIME! A wild gh {group} {action} appeared! Think before you click!",
            f"RED ALERT! gh {group} {action} is trying to sneak past you. Eyes on the screen!",
            f"HEY! WAKE UP! gh {group} {action} needs your blessing. Do not sleepwalk through this!",
            f"A gh {group} {action} walks into a bar. The bartender says: 'Are you authorized?'",
        ]
        print(make_response("ask", random.choice(warnings)))
        return


if __name__ == "__main__":
    main()