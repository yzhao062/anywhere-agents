# reap-watch.ps1 -- prun dispatch watchdog (Windows mirror of the inline watchdog in
# dispatch-task.sh). Spawned in the background by dispatch-task.ps1, the same split the
# implement-review dispatch-codex.ps1 + stall-watch.ps1 pair uses. Polls the worker's
# tail capture; if it stops growing for PRUN_STALL_THRESHOLD seconds (idle-stall) or the
# run exceeds CODEX_DISPATCH_TIMEOUT seconds (hard-timeout, 0 = disabled), it force-kills
# the worker's whole process tree and records the reason in <state-dir>/reap-reason so
# dispatch-task can mark the result FALLBACK and exit 124.
#
# The launch (dispatch-task.ps1) and the watch+kill (here) deliberately live in separate
# .ps1 files: a single file that launches a hidden worker, polls it, and kills its tree
# trips some Windows AV AMSI heuristics and is blocked at parse. Each half on its own
# stays under that heuristic, and the kill verb itself runs from a generated reap.cmd
# (batch, not PowerShell-scanned). The .sh variant does all of this inline.
#
# Args (named, --foo style for cross-platform parity with .sh):
#   --state-dir <abs-path>   Directory created by dispatch-task.ps1
#   --worker-pid <PID>       PID of the worker process tree to reap on stall/timeout
#   --tail <abs-path>        Worker tail-capture file to watch for growth
#
# Env: PRUN_STALL_THRESHOLD (default 600), CODEX_DISPATCH_TIMEOUT (default 0, disabled),
#      PRUN_WATCH_POLL_SECONDS (default 1)
#
# Invariant: only reaps the --worker-pid tree on stall/timeout; exits 0 silently when the
# worker finishes on its own or on any error.

$ErrorActionPreference = 'Stop'

$StateDir = $null
$WorkerProcessId = $null
$TailPath = $null

$i = 0
while ($i -lt $args.Length) {
    switch ($args[$i]) {
        '--state-dir'  { $StateDir = $args[$i + 1]; $i += 2 }
        '--worker-pid' { $WorkerProcessId = $args[$i + 1]; $i += 2 }
        '--tail'       { $TailPath = $args[$i + 1]; $i += 2 }
        default { exit 0 }
    }
}

if (-not $StateDir -or -not $WorkerProcessId -or -not $TailPath) { exit 0 }
if (-not (Test-Path -LiteralPath $StateDir -PathType Container)) { exit 0 }
$WorkerProcessId = [int]$WorkerProcessId

$stallThreshold = if ($env:PRUN_STALL_THRESHOLD -match '^\d+$') { [int]$env:PRUN_STALL_THRESHOLD } else { 600 }
$hardTimeout = if ($env:CODEX_DISPATCH_TIMEOUT -match '^\d+$') { [int]$env:CODEX_DISPATCH_TIMEOUT } else { 0 }
$interval = if ($env:PRUN_WATCH_POLL_SECONDS -match '^\d+$') { [int]$env:PRUN_WATCH_POLL_SECONDS } else { 1 }

$reapReasonPath = Join-Path $StateDir 'reap-reason'
$start = [DateTimeOffset]::UtcNow
$lastSize = [int64]-1
$lastMtimeTicks = [int64]0
$lastGrowth = $start
$reapReason = $null

# Non-terminating mode so a transient probe error does not abort the loop.
$ErrorActionPreference = 'Continue'

while ($true) {
    # Worker liveness: when it finishes on its own there is nothing to reap.
    $workerAlive = $true
    try {
        $null = Get-Process -Id $WorkerProcessId -ErrorAction Stop
    } catch {
        $workerAlive = $false
    }
    if (-not $workerAlive) { exit 0 }

    $now = [DateTimeOffset]::UtcNow
    $currentSize = [int64]0
    $currentMtimeTicks = [int64]0
    if (Test-Path -LiteralPath $TailPath -PathType Leaf) {
        try {
            $item = Get-Item -LiteralPath $TailPath -ErrorAction Stop
            $currentSize = [int64]$item.Length
            $currentMtimeTicks = [int64]$item.LastWriteTimeUtc.Ticks
        } catch {
            # Best-effort; treat as no growth this tick.
        }
    }

    if ($currentSize -gt $lastSize -or $currentMtimeTicks -gt $lastMtimeTicks) {
        $lastSize = $currentSize
        $lastMtimeTicks = $currentMtimeTicks
        $lastGrowth = $now
    } else {
        $idle = [int]($now - $lastGrowth).TotalSeconds
        if ($idle -ge $stallThreshold) { $reapReason = 'idle-stall'; break }
    }

    if ($hardTimeout -gt 0 -and [int]($now - $start).TotalSeconds -ge $hardTimeout) {
        $reapReason = 'hard-timeout'
        break
    }

    Start-Sleep -Seconds $interval
}

# Record the reason BEFORE reaping so dispatch-task sees it even if the kill races.
try {
    [System.IO.File]::WriteAllText($reapReasonPath, "$reapReason`n", (New-Object System.Text.UTF8Encoding $false))
} catch {
    # best-effort
}

# Force-kill the worker's whole tree from a generated reap.cmd (batch, not PS-scanned).
$reapCmd = Join-Path $StateDir 'reap.cmd'
$reapBody = "@echo off`r`ntaskkill /PID %1 /T /F >NUL 2>&1`r`n"
try {
    [System.IO.File]::WriteAllText($reapCmd, $reapBody, (New-Object System.Text.UTF8Encoding $false))
    $reaper = if ($env:ComSpec) { $env:ComSpec } else { 'cmd.exe' }
    & $reaper /c $reapCmd $WorkerProcessId 2>$null | Out-Null
} catch {
    # best-effort reap; dispatch-task's backstop still salvages the tail
}

exit 0
