# monitor.ps1 -- prun active stall/fail/done monitor for dispatched units (PowerShell variant).
# See monitor.sh for the full contract. Wakes the turn-based coordinator by COMPLETING on the
# first actionable event (all units done / any stall / any fail), printing a per-unit digest.
# Reuses the tail size+mtime liveness logic from implement-review's stall-watch. Never kills
# any process.

$ErrorActionPreference = 'Continue'

if ($args.Count -lt 1) {
    [Console]::Error.WriteLine("monitor: need at least one <state-dir>")
    [Console]::Error.WriteLine("Usage: monitor.ps1 <state-dir> [<state-dir> ...]")
    [Console]::Out.WriteLine("MONITOR-EVENT usage-error")
    exit 2
}

function Get-IntEnv([string]$name, [int]$default) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ($v -and ($v -match '^\d+$')) { return [int]$v }
    return $default
}
$threshold    = Get-IntEnv 'PRUN_STALL_THRESHOLD' 600
$poll         = Get-IntEnv 'PRUN_MONITOR_POLL' 15
$timeout      = Get-IntEnv 'PRUN_MONITOR_TIMEOUT' 3600
$stableWindow = Get-IntEnv 'PRUN_MONITOR_STABLE_WINDOW' 10

function Get-Now() { [int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
function Get-FileSize([string]$p) {
    if (Test-Path -LiteralPath $p -PathType Leaf) { return [int](Get-Item -LiteralPath $p).Length }
    return 0
}
function Get-FileMtime([string]$p) {
    if (Test-Path -LiteralPath $p -PathType Leaf) {
        $utc = (Get-Item -LiteralPath $p).LastWriteTimeUtc
        return [int]([DateTimeOffset]$utc).ToUnixTimeSeconds()
    }
    return 0
}

$stateDirs  = @($args)
$n          = $stateDirs.Count
$lastSize   = New-Object 'int[]' $n
$lastMtime  = New-Object 'int[]' $n
$lastGrowth = New-Object 'int[]' $n
$status     = New-Object 'string[]' $n
for ($i = 0; $i -lt $n; $i++) {
    $lastSize[$i]   = -1
    $lastMtime[$i]  = 0
    $lastGrowth[$i] = Get-Now
    $status[$i]     = 'pending'
}

[Console]::Out.WriteLine("MONITOR-START units=$n stall-threshold=$($threshold)s timeout=$($timeout)s")

function Emit-AndExit([string]$reason, [int]$code) {
    [Console]::Out.WriteLine("MONITOR-EVENT $reason")
    for ($j = 0; $j -lt $n; $j++) {
        $name = Split-Path -Leaf $stateDirs[$j]
        [Console]::Out.WriteLine("UNIT $name $($status[$j])")
    }
    exit $code
}

$start = Get-Now
while ($true) {
    $allDone  = $true
    $hasFail  = $false
    $hasStall = $false
    for ($i = 0; $i -lt $n; $i++) {
        $sd  = $stateDirs[$i]
        $now = Get-Now
        $rf  = ''
        $rfMarker = Join-Path $sd 'result-file'
        if (Test-Path -LiteralPath $rfMarker -PathType Leaf) {
            $rf = (Get-Content -LiteralPath $rfMarker -TotalCount 1 -ErrorAction SilentlyContinue)
        }

        $terminal = $false
        $resultPresent = $false
        if ($rf -and (Test-Path -LiteralPath $rf -PathType Leaf) -and ((Get-Item -LiteralPath $rf).Length -gt 0)) {
            $resultPresent = $true
            $rmt = Get-FileMtime $rf
            if (($now - $rmt) -ge $stableWindow) {
                $terminal = $true
                # FALLBACK only when the dispatch-task backstop HEADER is on line 1,
                # never merely the word appearing inside a real worker's result body.
                $firstLine = (Get-Content -LiteralPath $rf -TotalCount 1 -ErrorAction SilentlyContinue)
                if ($firstLine -and ($firstLine -match 'result \(FALLBACK, worker wrote no result file\)')) {
                    $status[$i] = 'failed(fallback)'; $hasFail = $true
                } else {
                    $status[$i] = 'done'
                }
            }
        }

        if (-not $terminal) {
            $allDone = $false
            if ($resultPresent) {
                # A non-empty result is already written; it is only stabilizing toward
                # done/fallback. Never stall- or dead-classify a unit that produced a result.
                $status[$i] = 'finishing'
            } else {
                $tailF = Join-Path $sd 'tail'
                $csize = Get-FileSize $tailF
                $cmt   = Get-FileMtime $tailF
                if (($csize -gt $lastSize[$i]) -or ($cmt -gt $lastMtime[$i])) {
                    $lastSize[$i] = $csize; $lastMtime[$i] = $cmt; $lastGrowth[$i] = $now
                    $status[$i] = 'growing'
                } else {
                    $elapsed = $now - $lastGrowth[$i]
                    if ($elapsed -ge $threshold) {
                        $dpid = ''
                        $pidFile = Join-Path $sd 'dispatch-pid'
                        if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
                            $dpid = (Get-Content -LiteralPath $pidFile -TotalCount 1 -ErrorAction SilentlyContinue)
                        }
                        $alive = $false
                        if ($dpid -and ($dpid -match '^\d+$')) {
                            if (Get-Process -Id ([int]$dpid) -ErrorAction SilentlyContinue) { $alive = $true }
                        }
                        if ($dpid -and (-not $alive)) {
                            $status[$i] = 'failed(dispatch-dead)'; $hasFail = $true
                        } else {
                            $status[$i] = "stalled($($elapsed)s)"; $hasStall = $true
                        }
                    } else {
                        $status[$i] = 'growing'
                    }
                }
            }
        }
    }

    if ($hasFail)  { Emit-AndExit 'fail' 3 }
    if ($hasStall) { Emit-AndExit 'stall' 3 }
    if ($allDone)  { Emit-AndExit 'all-done' 0 }

    if (((Get-Now) - $start) -ge $timeout) { Emit-AndExit 'timeout' 2 }
    Start-Sleep -Seconds $poll
}
