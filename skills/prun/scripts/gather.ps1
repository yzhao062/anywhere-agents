# gather.ps1 -- prun result collector (PowerShell variant).
# Generalized from implement-review/scripts/auto-watch.ps1. Polls a fixed list of
# result files and emits "DONE <abs-path>" for each as it lands (exists, non-empty,
# and quiet for the stable window). Exits 0 once every file has landed, or 2 on
# timeout.
#
# Like gather.sh, it does NOT require an mtime advance past a startup snapshot
# (prun uses a fresh result path per run), so a unit that finished before gather
# started fires immediately. Tracks done by INDEX so duplicate paths are handled.
#
# Usage:
#   pwsh gather.ps1 <result-file> [<result-file> ...]
#   powershell -File gather.ps1 <result-file> [<result-file> ...]
#
# Env:
#   AGENT_CONFIG_GATHER_TIMEOUT   override timeout in seconds (default 3600)
#   PRUN_GATHER_POLL              poll interval seconds (default 5)
#   PRUN_GATHER_STABLE_WINDOW     quiet window seconds before firing (default 10)
#
# Stdout (schema matches gather.sh):
#   GATHER-START count=<N> timeout=<seconds>s
#   DONE <abs-path>          (one line per file, as it lands)
#   TIMEOUT remaining=<k>    (if timeout hits before all land)

[CmdletBinding()]
param(
    # NOT Mandatory: a mandatory param with zero args makes PowerShell prompt
    # interactively and hang in a non-interactive run. Validate explicitly.
    [Parameter(Mandatory = $false, ValueFromRemainingArguments = $true)]
    [string[]]$Files
)

$ErrorActionPreference = 'Stop'

if (-not $Files -or $Files.Count -lt 1) {
    [Console]::Error.WriteLine("usage: gather.ps1 <result-file> [<result-file> ...]")
    exit 2
}

$timeout = if ($env:AGENT_CONFIG_GATHER_TIMEOUT) { [int]$env:AGENT_CONFIG_GATHER_TIMEOUT } else { 3600 }
$pollSeconds = if ($env:PRUN_GATHER_POLL) { [int]$env:PRUN_GATHER_POLL } else { 5 }
$stableWindow = if ($env:PRUN_GATHER_STABLE_WINDOW) { [int]$env:PRUN_GATHER_STABLE_WINDOW } else { 10 }

function Get-EpochSeconds {
    param([datetime]$Utc = ([datetime]::UtcNow))
    [long](($Utc - [datetime]'1970-01-01').TotalSeconds)
}

$n = $Files.Count
# Per-INDEX done flags (handles duplicate paths; parity with gather.sh).
$done = New-Object 'bool[]' $n

[Console]::Out.WriteLine("GATHER-START count=$n timeout=${timeout}s")

$startEpoch = Get-EpochSeconds
$remaining = $n
while ($remaining -gt 0) {
    $now = Get-EpochSeconds
    if (($now - $startEpoch) -ge $timeout) {
        [Console]::Out.WriteLine("TIMEOUT remaining=$remaining")
        exit 2
    }
    for ($idx = 0; $idx -lt $n; $idx++) {
        if ($done[$idx]) { continue }
        $f = $Files[$idx]
        if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { continue }
        $item = Get-Item -LiteralPath $f
        if ($item.Length -le 0) { continue }   # non-empty only
        $cur = Get-EpochSeconds -Utc $item.LastWriteTimeUtc
        if (($now - $cur) -lt $stableWindow) { continue }
        $abs = (Resolve-Path -LiteralPath $f).Path
        [Console]::Out.WriteLine("DONE $abs")
        $done[$idx] = $true
        $remaining--
    }
    if ($remaining -gt 0) { Start-Sleep -Seconds $pollSeconds }
}
exit 0
