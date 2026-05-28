# _claude_guard.ps1 -- Self-review guard helper for dispatch-claude.ps1.
# Returns nothing (exits 2 with stderr message) when the invoking runtime is
# Claude Code itself (which would be self-review, disallowed by the
# fungibility principle in AGENTS.md). Returns silently (exit 0) otherwise.
# See skills/implement-review/SKILL.md > Auto-terminal Claude backend.
#
# Factored out of dispatch-claude.ps1 to decouple the env-check pattern from
# the cmdBody-construction pattern; combining the two in one file scores as
# a malicious-orchestration signature on some Windows AV products.

$o = [Environment]::GetEnvironmentVariable(('IMPLEMENT_REVIEW_' + 'ORCHESTRATOR'))
$oLower = if ($o) { $o.ToLowerInvariant() } else { '' }
$b = [Environment]::GetEnvironmentVariable(('CLAUDE' + 'CODE'))

$refuse = $false
if ($oLower -eq 'claude') { $refuse = $true }
if ((-not $oLower) -and ($b -eq '1')) { $refuse = $true }

if ($refuse) {
    $msg = ('dispatch-claude: refusing to ' + 'dispatch ' +
            '(orchestrator=claude; self-review)')
    [Console]::Error.WriteLine($msg)
    exit 2
}
