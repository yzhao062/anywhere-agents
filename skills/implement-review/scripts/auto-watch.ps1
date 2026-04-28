# Auto-watch -- implement-review Phase 1d watcher (PowerShell variant).
#
# Polls every 5s until a Review-<reviewer>.md file lands that satisfies
# all three trigger conditions, then emits "DONE <abs-path>" on stdout
# and exits 0. Times out after 60 minutes by default with "TIMEOUT" and
# exit 2.
#
# Stdout schema (matches auto-watch.sh exactly):
#   WATCH-START round=<N> reviewers=<csv> timeout=<seconds>s
#   DONE <abs-path>     |     TIMEOUT
#
# Usage:
#   pwsh auto-watch.ps1 <FileGlob> <Round> <Reviewers>
#   powershell -File auto-watch.ps1 <FileGlob> <Round> <Reviewers>
#
# Env:
#   AGENT_CONFIG_AUTO_WATCH_TIMEOUT  override timeout in seconds (tests).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$FileGlob,
    [Parameter(Mandatory = $true, Position = 1)][int]$Round,
    [Parameter(Mandatory = $true, Position = 2)][string]$Reviewers
)

$ErrorActionPreference = 'Stop'

$timeout = if ($env:AGENT_CONFIG_AUTO_WATCH_TIMEOUT) {
    [int]$env:AGENT_CONFIG_AUTO_WATCH_TIMEOUT
} else { 3600 }
$pollSeconds  = 5
$stableWindow = 10

$expected = $Reviewers -split ','

function Get-EpochSeconds {
    param([datetime]$Utc = ([datetime]::UtcNow))
    [long](($Utc - [datetime]'1970-01-01').TotalSeconds)
}

# Snapshot existing matching files' mtimes (UTC epoch seconds). Without
# this, a file that already exists with the matching round marker would
# fire immediately on the first poll, defeating the "wait for reviewer
# to write" intent.
$preSnap = @{}
foreach ($f in @(Get-ChildItem -Path $FileGlob -File -ErrorAction SilentlyContinue)) {
    $preSnap[$f.FullName] = Get-EpochSeconds -Utc $f.LastWriteTimeUtc
}

[Console]::Out.WriteLine("WATCH-START round=$Round reviewers=$Reviewers timeout=${timeout}s")

$startEpoch = Get-EpochSeconds
while ($true) {
    $now = Get-EpochSeconds
    if (($now - $startEpoch) -ge $timeout) {
        [Console]::Out.WriteLine('TIMEOUT')
        exit 2
    }

    foreach ($f in @(Get-ChildItem -Path $FileGlob -File -ErrorAction SilentlyContinue)) {
        $name = $f.BaseName -replace '^Review-', ''
        if ($expected -notcontains $name) { continue }

        $cur = Get-EpochSeconds -Utc $f.LastWriteTimeUtc
        $pre = if ($preSnap.ContainsKey($f.FullName)) {
            $preSnap[$f.FullName]
        } else { 0 }

        # (a) mtime advanced past the watcher's startup snapshot.
        if ($cur -le $pre) { continue }
        # (b) mtime has been quiet for $stableWindow seconds.
        if (($now - $cur) -lt $stableWindow) { continue }
        # (c) first line equals "<!-- Round N -->" after \r + trailing-
        # whitespace stripping. Windows newline tolerance.
        $first = Get-Content -Path $f.FullName -TotalCount 1 -ErrorAction SilentlyContinue
        if ($null -eq $first) { continue }
        $first = ($first -replace "`r$", '') -replace '\s+$', ''
        if ($first -ne ("<!-- Round $Round -->")) { continue }

        [Console]::Out.WriteLine("DONE $($f.FullName)")
        exit 0
    }

    Start-Sleep -Seconds $pollSeconds
}
