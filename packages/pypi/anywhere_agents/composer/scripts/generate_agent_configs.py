#!/usr/bin/env python3
"""Generate per-agent config files (CLAUDE.md, agents/codex.md) from AGENTS.md.

AGENTS.md is the central source. Sections of it are tagged with HTML comments:

    <!-- agent:claude -->
    [content only relevant to Claude Code]
    <!-- /agent:claude -->

    <!-- agent:codex -->
    [content only relevant to Codex]
    <!-- /agent:codex -->

Everything outside such blocks is shared across all agents.

For each agent, the generator emits a file that contains:
  - All shared (untagged) content.
  - Content from blocks tagged for that agent.
  - No content from blocks tagged for other agents.

Each generated file is prefixed with a header that identifies it as generated,
documents the precedence model, and lists the local override file.

Protection: if an output file already exists and does NOT contain the
GENERATED marker, the generator preserves the existing file and prints
a loud warning to stderr. The user is then prompted to rename their
hand-authored file to the corresponding .local.md (which always wins
over the generated file in the precedence ladder).

Usage:
    python generate_agent_configs.py [--root PATH] [--quiet]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

GENERATED_MARKER = "GENERATED FILE"

HEADER_TEMPLATE = """<!--
{marker} -- do not edit by hand.

This file is regenerated from AGENTS.md by scripts/generate_agent_configs.py.
Bootstrap re-runs the generator on every session, so edits here are lost.

Precedence for agent rule files (most specific wins):
  1. {local_file}      your per-agent, per-project overrides
  2. AGENTS.local.md   your cross-agent, per-project overrides
  3. {this_file}       generated from AGENTS.md (this file)
  4. AGENTS.md         upstream baseline

To customize just for {display_name} in this project, create {local_file}
(the generator never touches it). To customize for every agent in this
project, edit AGENTS.local.md. To change upstream rules for everyone,
edit AGENTS.md in your fork.
-->

"""

AGENTS = [
    {
        "tag": "claude",
        "output_rel": "CLAUDE.md",
        "local_rel": "CLAUDE.local.md",
        "display_name": "Claude Code",
    },
    {
        "tag": "codex",
        "output_rel": "agents/codex.md",
        "local_rel": "agents/codex.local.md",
        "display_name": "Codex",
    },
]

BLOCK_RE = re.compile(
    r"<!--\s*agent:(?P<tag>[\w-]+)\s*-->\n"
    r"(?P<body>.*?)"
    r"<!--\s*/agent:(?P=tag)\s*-->\n?",
    re.DOTALL,
)


def extract_for(content: str, keep_tag: str) -> str:
    """Return content with only the given tag's blocks kept; others stripped."""
    def replace(match: re.Match) -> str:
        if match.group("tag") == keep_tag:
            body = match.group("body")
            return body if body.endswith("\n") else body + "\n"
        return ""
    result = BLOCK_RE.sub(replace, content)
    # Strip trailing whitespace on every line so generated files do not
    # inherit whitespace-only lines that fail `git diff --check`.
    result = re.sub(r"[ \t]+\n", "\n", result)
    # Collapse runs of 3+ blank lines to 2.
    return re.sub(r"\n{3,}", "\n\n", result)


def write_output(
    output_path: Path,
    output_rel: str,
    local_rel: str,
    header: str,
    body: str,
    quiet: bool = False,
) -> None:
    """Write the generated file. If an existing file lacks the marker, warn.

    output_rel and local_rel are the canonical relative paths (e.g.,
    ``agents/codex.md`` and ``agents/codex.local.md``); using these keeps the
    rename instruction accurate for nested outputs, not just root-level ones.
    """
    if output_path.exists():
        existing = output_path.read_text(encoding="utf-8")
        if GENERATED_MARKER not in existing:
            warn = (
                f"WARNING: {output_rel} exists and is not managed by anywhere-agents.\n"
                f"  The agent will follow YOUR file, not the upstream rules in AGENTS.md.\n"
                f"  To adopt upstream rules:\n"
                f"    1. mv {output_rel} {local_rel}\n"
                f"       ({local_rel} still wins over the generated file, so nothing is lost.)\n"
                f"    2. Re-run bootstrap; the managed {output_rel} will be generated.\n"
                f"  To keep ignoring upstream, leave as-is. This warning repeats each session."
            )
            print(warn, file=sys.stderr)
            return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" forces LF on Windows; without it, Python's text-mode
    # write converts "\n" to "\r\n" and the committed LF-normalized file
    # byte-diffs against the regenerated file in pre-push-smoke.
    # Path.write_text(newline=) was added in 3.10; open() works on 3.9+.
    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(header + body)
    if not quiet:
        print(f"generated {output_rel}", file=sys.stderr)


def generate(root: Path, quiet: bool = False) -> int:
    agents_md = root / "AGENTS.md"
    if not agents_md.exists():
        print(f"error: AGENTS.md not found at {agents_md}", file=sys.stderr)
        return 1
    source = agents_md.read_text(encoding="utf-8")
    for agent in AGENTS:
        body = extract_for(source, agent["tag"])
        header = HEADER_TEMPLATE.format(
            marker=GENERATED_MARKER,
            local_file=agent["local_rel"],
            this_file=agent["output_rel"],
            display_name=agent["display_name"],
        )
        output_path = root / agent["output_rel"]
        write_output(
            output_path=output_path,
            output_rel=agent["output_rel"],
            local_rel=agent["local_rel"],
            header=header,
            body=body,
            quiet=quiet,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate CLAUDE.md and agents/codex.md from AGENTS.md."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Project root containing AGENTS.md. Default: current working directory.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress informational output. Warnings still print.",
    )
    args = parser.parse_args()
    return generate(args.root.resolve(), quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
