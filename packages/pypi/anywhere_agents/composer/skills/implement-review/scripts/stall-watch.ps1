# stall-watch.ps1 -- Background stall observer (Windows mirror of stall-watch.sh).
# See skills/implement-review/SKILL.md > Script contract invariants and
# Phase 2.0 Health check 9 for the contract.
#
# Spawned in background by dispatch-codex.ps1. Polls <state-dir>/tail size+mtime;
# appends one STALL line to <state-dir>/stall-warning when no growth is observed
# for >= STALL_THRESHOLD_SECONDS (default 300 = 5 min). Each subsequent stall
# period (growth followed by another threshold of silence) is logged again.
#
# Args (named, --foo style for cross-platform parity with .sh):
#   --state-dir <abs-path>   Directory created by dispatch-codex.ps1
#   --parent-pid <PID>       PID of dispatch-codex.ps1 (informational liveness)
#
# Env (test hooks):
#   STALL_THRESHOLD_SECONDS  Default 300 (5 min)
#   STALL_POLL_INTERVAL_SECONDS  Default 30
#
# Invariant: never kills any process. Only uses Get-Process for liveness check.
# On any error or when --parent-pid is dead, exits silently (status 0).

$ErrorActionPreference = 'Stop'

$StateDir = $null
$ParentProcessId = $null

$i = 0
while ($i -lt $args.Length) {
    switch ($args[$i]) {
        '--state-dir' { $StateDir = $args[$i + 1]; $i += 2 }
        '--parent-pid' { $ParentProcessId = $args[$i + 1]; $i += 2 }
        default { exit 0 }
    }
}

if (-not $StateDir -or -not $ParentProcessId) { exit 0 }
if (-not (Test-Path -LiteralPath $StateDir -PathType Container)) { exit 0 }

$threshold = if ($env:STALL_THRESHOLD_SECONDS) {
    [int]$env:STALL_THRESHOLD_SECONDS
} else {
    300
}
$interval = if ($env:STALL_POLL_INTERVAL_SECONDS) {
    [int]$env:STALL_POLL_INTERVAL_SECONDS
} else {
    30
}

$tailPath = Join-Path $StateDir 'tail'
$tailStderrPath = Join-Path $StateDir 'tail.stderr-tmp'
$warnPath = Join-Path $StateDir 'stall-warning'

# Both files contribute to "Codex is alive and emitting" growth. The .ps1
# dispatch redirects stdout to <state-dir>/tail and stderr to a side file
# during the run; if Codex only writes to stderr we still treat that as
# progress so we do not false-positive a stall while it is producing
# diagnostics. The .sh dispatch already merges streams with `2>&1`, so
# <state-dir>/tail.stderr-tmp simply does not exist on bash and the extra
# probe is a harmless no-op there.
$watchedPaths = @($tailPath, $tailStderrPath)

$lastSize = [int64]-1
$lastMtimeTicks = [int64]0
$lastGrowth = [DateTimeOffset]::UtcNow
$stalledLogged = $false

# Switch to non-terminating mode so transient errors don't abort the loop.
$ErrorActionPreference = 'Continue'

while ($true) {
    # Parent liveness check (informational only; never kills anything).
    # When the parent dies we still run one final poll so any stall that
    # crossed the threshold right before dispatch exited gets recorded,
    # then exit. This closes the Phase 2.0 Health check 9 race noted in
    # SKILL.md > Phase 2.0.
    $parentAlive = $true
    try {
        $null = Get-Process -Id $ParentProcessId -ErrorAction Stop
    } catch {
        $parentAlive = $false
    }

    # Combined growth check across watched paths: sum sizes, take max mtime.
    $currentSize = [int64]0
    $currentMtimeTicks = [int64]0
    $anyExists = $false
    foreach ($p in $watchedPaths) {
        if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { continue }
        try {
            $item = Get-Item -LiteralPath $p -ErrorAction Stop
            $currentSize += [int64]$item.Length
            $itemTicks = [int64]$item.LastWriteTimeUtc.Ticks
            if ($itemTicks -gt $currentMtimeTicks) {
                $currentMtimeTicks = $itemTicks
            }
            $anyExists = $true
        } catch {
            # Best-effort; skip this file but consider any other watched path.
        }
    }
    if ($anyExists) {
        $now = [DateTimeOffset]::UtcNow
        if ($currentSize -gt $lastSize -or $currentMtimeTicks -gt $lastMtimeTicks) {
            $lastSize = $currentSize
            $lastMtimeTicks = $currentMtimeTicks
            $lastGrowth = $now
            $stalledLogged = $false
        } else {
            $elapsed = [int]($now - $lastGrowth).TotalSeconds
            if ($elapsed -ge $threshold -and -not $stalledLogged) {
                $iso = $now.ToString("yyyy-MM-ddTHH:mm:ssZ")
                try {
                    Add-Content -LiteralPath $warnPath `
                        -Value ("STALL {0} tail-no-growth-for-{1}s" -f $iso, $elapsed) `
                        -ErrorAction Stop
                    $stalledLogged = $true
                } catch {
                    exit 0
                }
            }
        }
    }

    if (-not $parentAlive) {
        exit 0
    }

    Start-Sleep -Seconds $interval
}
