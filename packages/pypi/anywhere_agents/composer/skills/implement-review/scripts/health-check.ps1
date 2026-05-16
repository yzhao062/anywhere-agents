# health-check.ps1 -- PowerShell wrapper that delegates to health-check.py.
# See skills/implement-review/SKILL.md > Phase 2.0 prologue for the contract.
# All real logic lives in health-check.py to keep the pattern list / regex /
# code-span exclusion in one place (avoids drift between sh and ps1).

$ErrorActionPreference = 'Stop'

$scriptDir = $PSScriptRoot
$pyHelper = Join-Path $scriptDir 'health-check.py'

if (-not (Test-Path -LiteralPath $pyHelper -PathType Leaf)) {
    [Console]::Error.WriteLine("health-check: helper script missing: $pyHelper")
    exit 2
}

# Pick a Python that can actually execute code. Three combined hazards on
# Windows in this user's setup: (1) PATH `python` may be the Microsoft Store
# App Execution Alias, which Get-Command sees but cannot use; (2) `python3`
# is also usually an Alias; (3) Miniforge -- the documented project default
# (see CLAUDE.md > Environment Notes) -- is installed under %USERPROFILE%
# but is not always on PATH for non-interactive subprocesses launched via
# `pwsh -File`. Strategy: probe Miniforge first, then `py -3`, then PATH
# `python3` / `python`; skip WindowsApps Store shims; verify each survivor
# can actually execute `-c 'import sys; sys.exit(0)'`.
$pythonCandidates = @()
if ($env:USERPROFILE) {
    $pythonCandidates += @{ Exe = (Join-Path $env:USERPROFILE 'miniforge3\envs\py312\python.exe'); Args = @() }
    $pythonCandidates += @{ Exe = (Join-Path $env:USERPROFILE 'miniforge3\python.exe'); Args = @() }
}
$pythonCandidates += @{ Exe = 'py';      Args = @('-3') }
$pythonCandidates += @{ Exe = 'python3'; Args = @() }
$pythonCandidates += @{ Exe = 'python';  Args = @() }

$pyCmd = $null
foreach ($candidate in $pythonCandidates) {
    $exe = [string]$candidate.Exe
    if (-not $exe) { continue }

    # Accept either an absolute-path leaf or a PATH-resolvable command.
    $exists = (Test-Path -LiteralPath $exe -PathType Leaf) -or `
              (Get-Command $exe -ErrorAction SilentlyContinue)
    if (-not $exists) { continue }

    # Skip Microsoft Store App Execution Aliases (would launch Store window).
    $cmdInfo = Get-Command $exe -ErrorAction SilentlyContinue
    if ($cmdInfo -and ([string]$cmdInfo.Source) -like '*\WindowsApps\*') { continue }

    $probeArgs = @($candidate.Args) + @('-c', 'import sys; sys.exit(0)')
    & $exe @probeArgs > $null 2>&1
    if ($LASTEXITCODE -eq 0) {
        $pyCmd = $candidate
        break
    }
}
if (-not $pyCmd) {
    [Console]::Error.WriteLine("health-check: no usable Python interpreter found (probed Miniforge paths under USERPROFILE, py launcher, python3, python; WindowsApps Store aliases skipped)")
    exit 2
}

$ErrorActionPreference = 'Continue'
$invokeArgs = @($pyCmd.Args) + @($pyHelper) + $args
& ([string]$pyCmd.Exe) @invokeArgs
exit $LASTEXITCODE
