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
# Windows: (1) PATH `python` may be the Microsoft Store App Execution Alias,
# which Get-Command sees but cannot use; (2) `python3` is also usually an
# Alias; (3) a conda/Miniforge install is often absent from PATH for
# non-interactive subprocesses launched via `pwsh -File`. Strategy: probe an
# active conda env and its sibling envs first (discovered from CONDA_PREFIX /
# CONDA_ROOT and from a resolvable conda/mamba launcher, with no hard-coded
# install dir or env name), then `py -3`, then PATH `python3` / `python`; skip
# WindowsApps Store shims; verify each survivor can actually execute
# `-c 'import sys; sys.exit(0)'`.
$pythonCandidates = @()

# Conda/Miniforge discovery, derived entirely from the environment so nothing
# machine-specific ships in this file.
$condaRoots = @()
if ($env:CONDA_PREFIX) {
    $activePy = Join-Path $env:CONDA_PREFIX 'python.exe'
    if (Test-Path -LiteralPath $activePy -PathType Leaf) {
        $pythonCandidates += @{ Exe = $activePy; Args = @() }
    }
    # <root>\envs\<name> -> <root>; a base-env prefix is itself the root.
    $cpParent = Split-Path $env:CONDA_PREFIX -Parent
    if ($cpParent -and (Split-Path $cpParent -Leaf) -eq 'envs') {
        $condaRoots += (Split-Path $cpParent -Parent)
    } else {
        $condaRoots += $env:CONDA_PREFIX
    }
}
if ($env:CONDA_ROOT) { $condaRoots += $env:CONDA_ROOT }
foreach ($mgr in @('conda', 'mamba')) {
    $mgrCmd = Get-Command $mgr -ErrorAction SilentlyContinue
    if ($mgrCmd -and $mgrCmd.Source) {
        $mgrRoot = Split-Path (Split-Path $mgrCmd.Source -Parent) -Parent
        if ($mgrRoot) { $condaRoots += $mgrRoot }
    }
}
# Roots discovered under the home dir by the conda-meta base-env marker (locates
# an install dir by signature, without assuming its name). Mirrors _python 2c so
# the no-active-env / no-conda-on-PATH case still resolves a real interpreter.
foreach ($homeDir in @($HOME, $env:USERPROFILE)) {
    if (-not $homeDir) { continue }
    if (-not (Test-Path -LiteralPath $homeDir -PathType Container)) { continue }
    Get-ChildItem -LiteralPath $homeDir -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
        if (Test-Path -LiteralPath (Join-Path $_.FullName 'conda-meta') -PathType Container) {
            $condaRoots += $_.FullName
        }
    }
}
foreach ($root in ($condaRoots | Where-Object { $_ } | Select-Object -Unique)) {
    $rootPy = Join-Path $root 'python.exe'
    if (Test-Path -LiteralPath $rootPy -PathType Leaf) {
        $pythonCandidates += @{ Exe = $rootPy; Args = @() }
    }
    $envsDir = Join-Path $root 'envs'
    if (Test-Path -LiteralPath $envsDir -PathType Container) {
        Get-ChildItem -LiteralPath $envsDir -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $envPy = Join-Path $_.FullName 'python.exe'
            if (Test-Path -LiteralPath $envPy -PathType Leaf) {
                $pythonCandidates += @{ Exe = $envPy; Args = @() }
            }
        }
    }
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
    [Console]::Error.WriteLine("health-check: no usable Python interpreter found (probed conda env/root discovery, py launcher, python3, python; WindowsApps Store aliases skipped)")
    exit 2
}

$ErrorActionPreference = 'Continue'
$invokeArgs = @($pyCmd.Args) + @($pyHelper) + $args
& ([string]$pyCmd.Exe) @invokeArgs
exit $LASTEXITCODE
