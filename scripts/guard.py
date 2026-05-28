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

- AGENT_CONFIG_GATES=off (legacy blanket): disables writing-style + banner only.
- AGENT_STYLE_HOOK=off (per-guard): disables writing-style only.
- AGENT_COMPOUND_CD_HOOK=off (per-guard): disables compound cd only.

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
)


def _env_disabled(name):
    """Return True iff env var `name` is set to a disable-truthy value."""
    val = (os.environ.get(name) or "").strip().lower()
    return val in ("0", "off", "false", "disabled", "no")


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
        # (resume / clear / leaked compact) must not block in-flight skill
        # tool calls. Re-emit advisory: pass through; the agent re-emits
        # the banner on its next textual response if it chooses
        # (issue anywhere-agents#7).
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
        # Honors the AGENT_STYLE_HOOK per-guard env in addition to AGENT_CONFIG_GATES.
        if writing_style_enabled():
            deny = check_writing_style(tool_name, tool_input)
            if deny:
                print(make_response("deny", deny))
                return

        # Check 2 (new): banner gate on most tool calls until banner acknowledged.
        deny = check_banner_emission(tool_name, tool_input)
        if deny:
            print(make_response("deny", deny))
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