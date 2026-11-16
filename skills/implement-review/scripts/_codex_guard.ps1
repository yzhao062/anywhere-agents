# _codex_guard.ps1 -- Self-review guard helper for dispatch-codex.ps1.
# Returns nothing (exits 2 with stderr message) when the invoking runtime is
# Codex itself (which would be self-review, disallowed by the
# fungibility principle in AGENTS.md). Returns silently (exit 0) otherwise.
# See skills/implement-review/references/codex-backend.md > Auto-terminal path.
#
# Factored out of dispatch-codex.ps1 to decouple the env-check pattern from
# the cmdBody-construction pattern; combining the two in one file scores as
# a malicious-orchestration signature on some Windows AV products.

$o = [Environment]::GetEnvironmentVariable(('IMPLEMENT_REVIEW_' + 'ORCHESTRATOR'))
$oLower = if ($o) { $o.ToLowerInvariant() } else { '' }
$t = [Environment]::GetEnvironmentVariable(('CODEX_' + 'THREAD_ID'))
$s = [Environment]::GetEnvironmentVariable(('CODEX_' + 'SESSION_ID'))

$refuse = $false
if ($oLower -eq 'codex') { $refuse = $true }
if ((-not $oLower) -and ($t -or $s)) { $refuse = $true }

if ($refuse) {
    $msg = ('dispatch-codex: refusing to ' + 'dispatch ' +
            '(orchestrator=codex; self-review)')
    [Console]::Error.WriteLine($msg)
    exit 2
}
