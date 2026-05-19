"""Noise-budget composer gate (v0.7.0).

Counts third-party active ``kind: hook`` entries that look "noisy" under the
Round 6 criterion and emits one warning per offending entry that the compose
summary can print. Reads manifest metadata only; never parses hook source
text and never executes hooks.

Round 6 criterion (see ``pack-architecture.md`` § "aa v0.7.0 — Noise audit"):
a deny gate is "noisy" when it triggers a deny without an agent-side reroute.
The composer-side approximation uses four manifest fields per hook entry:

    decision: deny
    false-positive-risk: high
    impact-if-allowed: low | medium
    reroute_hint: <string>   # absent / empty / literal "none" = no reroute

A hook entry is counted when ALL four hold (the first three indicate the
ergonomic class the noise audit targets; the fourth is the structural
signal that no concrete reroute is published). First-party packs are skipped
entirely in v0.7.0 because their guards (``guard.py``) are bootstrap-deployed
outside the pack manifest; the first-party invariant ships with the v1.0
``guard.py`` extraction to ``agent-behave``.

Consumer override: set ``noise-audit-override: accept-deny`` on a pack entry
in ``agent-config.yaml`` to silence the warning for that pack.

The module returns warnings as data; callers (``compose_packs.print_compose_summary``)
decide where to render them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class NoiseWarning:
    """One noisy hook entry produced by a third-party pack manifest."""

    pack_name: str
    entry_index: int  # index in active[] for diagnostic citation
    files_to: str  # target path of the offending hook (first files[].to)
    reason: str  # human-readable reason

    def render(self) -> str:
        """Format as a single warning line for the compose summary."""
        return (
            f"  ⚠ noise-budget: pack {self.pack_name!r} active[{self.entry_index}] "
            f"-> {self.files_to}: {self.reason}"
        )


# Manifest field names + literal values, centralized so a future schema
# rename only touches one place.
_DECISION_FIELD = "decision"
_FP_RISK_FIELD = "false-positive-risk"
_IMPACT_FIELD = "impact-if-allowed"
_REROUTE_FIELD = "reroute_hint"

_NOISY_DECISION = "deny"
_NOISY_FP_RISK = "high"
_NOISY_IMPACTS = frozenset({"low", "medium"})
_EMPTY_REROUTE_LITERALS = frozenset({"", "none"})

# Consumer-side override key in agent-config.yaml's pack entry, e.g.
#
#   packs:
#     - name: third-party-noisy-pack
#       noise-audit-override: accept-deny
#
# Honored only when the value is exactly "accept-deny"; any other value
# leaves the warning in place to avoid silent permissive misreads.
_CONSUMER_OVERRIDE_FIELD = "noise-audit-override"
_CONSUMER_OVERRIDE_ACCEPT = "accept-deny"


def _is_empty_reroute(value: Any) -> bool:
    """Return True for absent, None, empty string, or the literal "none"."""
    if value is None:
        return True
    if not isinstance(value, str):
        # A non-string reroute_hint is a schema violation rejected by
        # schema.py; treat as "no reroute" defensively.
        return True
    return value.strip().lower() in _EMPTY_REROUTE_LITERALS


def _is_noisy_hook_entry(entry: dict[str, Any]) -> str | None:
    """Return a reason string if the entry meets the noise criterion,
    else None. Composer treats None as "skip"."""
    if entry.get("kind") != "hook":
        return None
    decision = entry.get(_DECISION_FIELD)
    if decision != _NOISY_DECISION:
        return None
    fp_risk = entry.get(_FP_RISK_FIELD)
    if fp_risk != _NOISY_FP_RISK:
        return None
    impact = entry.get(_IMPACT_FIELD)
    if impact not in _NOISY_IMPACTS:
        return None
    if not _is_empty_reroute(entry.get(_REROUTE_FIELD)):
        return None
    return (
        f"decision: deny + false-positive-risk: high + impact-if-allowed: "
        f"{impact} + missing reroute_hint -> autonomous agents will block on "
        f"this guard with no reroute. Either lower the false-positive risk, "
        f"reclassify impact, or add a reroute_hint string to the manifest."
    )


def _entry_first_to(entry: dict[str, Any]) -> str:
    """Return the first ``files[].to`` for diagnostics (fallback marker)."""
    files = entry.get("files")
    if isinstance(files, list):
        for f in files:
            if isinstance(f, dict):
                to = f.get("to")
                if isinstance(to, str) and to:
                    return to
    return "<files[].to missing>"


def _is_host_matched(entry: dict[str, Any], host: str, pack_hosts: Any) -> bool:
    """Filter active entries by host (schema-validated at parse time).

    Mirrors the entry-vs-pack host precedence in schema.py: entry-level
    ``hosts:`` wins over pack-level. An entry without hosts inherits from
    the pack-level default. Returns True if the entry would deploy under
    the given host.
    """
    hosts = entry.get("hosts")
    if hosts is None:
        hosts = pack_hosts
    if not isinstance(hosts, list):
        # Schema rejected non-list at parse; defensive False here.
        return False
    return host in hosts


def evaluate_noise_budget(
    third_party_pack_defs: Iterable[tuple[str, dict[str, Any]]],
    consumer_overrides: dict[str, str] | None,
    host: str,
) -> list[NoiseWarning]:
    """Evaluate the noise budget for a set of third-party pack definitions.

    Parameters
    ----------
    third_party_pack_defs : iterable of (pack_name, pack_def) tuples
        Each ``pack_def`` follows the v2 manifest shape (``active:`` list of
        entries with ``kind:``, ``hosts:``, etc.). The caller is responsible
        for filtering first-party packs OUT before calling — first-party
        packs do not currently ship hook entries through the pack manifest
        (v0.7.0 scope decision per Round 3 finding 2; first-party invariant
        deferred to v1.0 alongside guard.py extraction).
    consumer_overrides : mapping pack_name -> override string, or None
        From ``agent-config.yaml`` per-pack ``noise-audit-override:`` value.
        Only the literal ``accept-deny`` silences a pack's warnings.
    host : str
        Active host name (``claude-code``, ``codex``, ...). Host-mismatched
        hook entries are filtered before counting.

    Returns
    -------
    list of NoiseWarning, one per offending hook entry.
    """
    warnings: list[NoiseWarning] = []
    overrides = consumer_overrides or {}
    for pack_name, pack_def in third_party_pack_defs:
        if overrides.get(pack_name) == _CONSUMER_OVERRIDE_ACCEPT:
            continue
        active = pack_def.get("active")
        if not isinstance(active, list):
            continue
        pack_hosts = pack_def.get("hosts")
        for idx, entry in enumerate(active):
            if not isinstance(entry, dict):
                continue
            if not _is_host_matched(entry, host, pack_hosts):
                continue
            reason = _is_noisy_hook_entry(entry)
            if reason is None:
                continue
            warnings.append(
                NoiseWarning(
                    pack_name=pack_name,
                    entry_index=idx,
                    files_to=_entry_first_to(entry),
                    reason=reason,
                )
            )
    return warnings


def render_warnings_block(warnings: list[NoiseWarning]) -> str:
    """Render a multiline summary block for warnings, or empty string when
    no warnings. The compose summary printer concatenates this to its
    per-pack outcome block; callers that want stderr-only routing can
    write the returned string themselves."""
    if not warnings:
        return ""
    lines = [
        f"Noise-budget: {len(warnings)} third-party hook(s) flagged as noisy:"
    ]
    lines.extend(w.render() for w in warnings)
    lines.append(
        "  Pack authors: add a reroute_hint string to the active hook entry"
    )
    lines.append(
        "  Consumers: silence per-pack via `noise-audit-override: accept-deny` "
        "in agent-config.yaml"
    )
    return "\n".join(lines)
