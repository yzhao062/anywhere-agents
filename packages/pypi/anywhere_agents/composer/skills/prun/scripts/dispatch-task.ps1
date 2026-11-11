# dispatch-task.ps1 -- prun generic task dispatch (Codex worker, PowerShell variant).
# Generalized from implement-review/scripts/dispatch-codex.ps1. Runs codex from a
# per-unit scratch cwd; see dispatch-task.sh for the full contract. Use it only
# when the user named Codex for the unit or the batch; every other unit goes
# through dispatch-task-agy.py.
#
# Args (named, --foo style for parity with .sh):
#   --prompt-file <path>   Task prompt (fed to codex on stdin)
#   --result-file <path>   File the unit will write its result to (mtime snapshot)
#   --unit-id <id>         Label (alnum/dash/underscore; names the state-dir)
#
# Env: CODEX_BIN, TMPDIR/TEMP/TMP, CODEX_DISPATCH_SANDBOX (default danger-full-access),
#      CODEX_DISPATCH_ISOLATE_MCP (off disables isolation), CODEX_DISPATCH_MODEL and
#      CODEX_DISPATCH_REASONING (one-run overrides of the registry row),
#      AGENT_MODELS_FILE, ANYWHERE_AGENTS_PYTHON, PRUN_SCRATCH_CWD (default
#      <state-dir>\work), PRUN_STALL_THRESHOLD (default 600), CODEX_DISPATCH_TIMEOUT
#      (default 0, disabled)
#
# Model and effort: the registry's prun-worker row for the codex family, read
# through scripts/agent_models.py; see dispatch-task.sh for the order of sources,
# <state-dir>/model, and <state-dir>/model-actual.
#
# Stdout: STATE-DIR <abs-path> (first and only machine-readable line)
# Exit:   propagates codex exec's exit code; 124 when dispatch reaps the worker;
#         2 on usage error; 70 when no source names the worker model and effort.

$ErrorActionPreference = 'Stop'

$PromptFile = $null
$ResultFile = $null
$UnitId = $null

$i = 0
while ($i -lt $args.Length) {
    switch ($args[$i]) {
        '--prompt-file' { $PromptFile = $args[$i + 1]; $i += 2 }
        '--result-file' { $ResultFile = $args[$i + 1]; $i += 2 }
        '--unit-id'     { $UnitId = $args[$i + 1]; $i += 2 }
        default {
            [Console]::Error.WriteLine("dispatch-task: unknown argument: $($args[$i])")
            [Console]::Error.WriteLine("Usage: dispatch-task.ps1 --prompt-file <path> --result-file <path> --unit-id <id>")
            exit 2
        }
    }
}

if (-not $PromptFile -or -not $ResultFile -or -not $UnitId) {
    [Console]::Error.WriteLine("dispatch-task: missing required argument")
    [Console]::Error.WriteLine("Usage: dispatch-task.ps1 --prompt-file <path> --result-file <path> --unit-id <id>")
    exit 2
}

if (-not (Test-Path -LiteralPath $PromptFile -PathType Leaf)) {
    [Console]::Error.WriteLine("dispatch-task: prompt file not found: $PromptFile")
    exit 2
}

# Resolve to absolute: the cmd helper opens it via stdin AFTER cd-ing into the
# scratch cwd, so a relative path would otherwise open the wrong file.
$PromptFile = (Resolve-Path -LiteralPath $PromptFile).Path

# Record the result path absolute, as dispatch-task.sh does: report-state reads
# a relative entry against the state dir, and .NET file calls use another cwd.
if (-not [System.IO.Path]::IsPathRooted($ResultFile)) {
    $ResultFile = Join-Path (Get-Location).Path $ResultFile
}
$ResultFile = [System.IO.Path]::GetFullPath($ResultFile)
$resultParent = Split-Path -Parent $ResultFile
if ($resultParent -and -not (Test-Path -LiteralPath $resultParent)) {
    New-Item -ItemType Directory -Path $resultParent -Force | Out-Null
}

if ($UnitId -notmatch '^[A-Za-z0-9_-]+$') {
    [Console]::Error.WriteLine("dispatch-task: --unit-id must be alphanumeric/dash/underscore, got: $UnitId")
    exit 2
}

# Worker model and effort from the registry; see dispatch-task.sh. The
# resolver is found by walking up from this script.
function Find-AgentModels {
    param([string]$Start)
    try { $dir = (Resolve-Path -LiteralPath $Start -ErrorAction Stop).Path } catch { return $null }
    while ($dir) {
        foreach ($root in @($dir, (Join-Path $dir '.agent-config/repo'))) {
            $candidate = Join-Path $root 'scripts/agent_models.py'
            $registry = Join-Path $root 'user/agent-models.json'
            if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and (Test-Path -LiteralPath $registry -PathType Leaf)) {
                return $candidate
            }
        }
        $parent = Split-Path -Parent $dir
        if (-not $parent -or $parent -eq $dir) { break }
        $dir = $parent
    }
    return $null
}
$agentModelsPy = Find-AgentModels -Start $PSScriptRoot
if (-not $agentModelsPy) { $agentModelsPy = Join-Path $PSScriptRoot 'agent_models.py.not-found' }

# Python for the lookup: the override, a conda or Miniforge interpreter found
# from the environment, then py -3, python3, and python. A Microsoft Store
# alias is skipped, and every survivor must run code.
function Find-RegistryPython {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $found = @()
        $roots = @()
        if ($env:ANYWHERE_AGENTS_PYTHON) {
            # Authoritative, as in dispatch-codex: a broken value stops the dispatch.
            if (Test-Path -LiteralPath $env:ANYWHERE_AGENTS_PYTHON -PathType Leaf) {
                & $env:ANYWHERE_AGENTS_PYTHON -c 'import sys; sys.exit(0)' > $null 2>&1
                if ($LASTEXITCODE -eq 0) { return @{ Exe = $env:ANYWHERE_AGENTS_PYTHON; Args = @() } }
            }
            return $null
        }
        if ($env:CONDA_PREFIX) {
            $found += @{ Exe = (Join-Path $env:CONDA_PREFIX 'python.exe'); Args = @() }
            $found += @{ Exe = (Join-Path $env:CONDA_PREFIX 'bin/python'); Args = @() }
            $prefixParent = Split-Path $env:CONDA_PREFIX -Parent
            if ($prefixParent -and (Split-Path $prefixParent -Leaf) -eq 'envs') {
                $roots += (Split-Path $prefixParent -Parent)
            } else {
                $roots += $env:CONDA_PREFIX
            }
        }
        if ($env:CONDA_ROOT) { $roots += $env:CONDA_ROOT }
        foreach ($managerName in @('conda', 'mamba')) {
            $manager = Get-Command $managerName -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($manager -and $manager.Source) {
                $roots += (Split-Path (Split-Path ([string]$manager.Source) -Parent) -Parent)
            }
        }
        foreach ($homeDir in @($HOME, $env:USERPROFILE)) {
            if (-not $homeDir -or -not (Test-Path -LiteralPath $homeDir -PathType Container)) { continue }
            foreach ($child in @(Get-ChildItem -LiteralPath $homeDir -Directory -Force -ErrorAction SilentlyContinue)) {
                if (Test-Path -LiteralPath (Join-Path $child.FullName 'conda-meta') -PathType Container) {
                    $roots += $child.FullName
                }
            }
        }
        foreach ($root in @($roots | Where-Object { $_ } | Select-Object -Unique)) {
            $found += @{ Exe = (Join-Path $root 'python.exe'); Args = @() }
            $found += @{ Exe = (Join-Path $root 'bin/python'); Args = @() }
            $envsDir = Join-Path $root 'envs'
            if (Test-Path -LiteralPath $envsDir -PathType Container) {
                foreach ($environment in @(Get-ChildItem -LiteralPath $envsDir -Directory -ErrorAction SilentlyContinue)) {
                    $found += @{ Exe = (Join-Path $environment.FullName 'python.exe'); Args = @() }
                    $found += @{ Exe = (Join-Path $environment.FullName 'bin/python'); Args = @() }
                }
            }
        }
        $found += @{ Exe = 'py'; Args = @('-3') }
        $found += @{ Exe = 'python3'; Args = @() }
        $found += @{ Exe = 'python'; Args = @() }
        foreach ($candidate in $found) {
            $exe = [string]$candidate.Exe
            if (-not $exe) { continue }
            # Every PATH entry, not the first: a Microsoft Store alias often sits
            # ahead of the real interpreter, and it is skipped, not run.
            foreach ($info in @(Get-Command $exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
                $resolved = [string]$info.Source
                if (-not $resolved -or $resolved -like '*\WindowsApps\*') { continue }
                $probeArgs = @($candidate.Args) + @('-c', 'import sys; sys.exit(0)')
                & $resolved @probeArgs > $null 2>&1
                if ($LASTEXITCODE -eq 0) { return @{ Exe = $resolved; Args = @($candidate.Args) } }
            }
        }
        return $null
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $saved
    }
}

# Run agent_models.py and return its exit code, stdout, and stderr as text.
function Invoke-AgentModels {
    param([string[]]$AgentArgs)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $result = @{ Exit = 1; Out = ''; Err = '' }
    try {
        $invokeArgs = @($script:registryPython.Args) + @($agentModelsPy) + @($AgentArgs)
        $lines = @(& ([string]$script:registryPython.Exe) @invokeArgs 2>&1)
        $result.Exit = $LASTEXITCODE
        $outLines = @($lines | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | ForEach-Object { [string]$_ })
        $errLines = @($lines | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { $_.ToString() })
        $result.Out = ($outLines -join "`n").Trim()
        $result.Err = ($errLines -join "`n").Trim()
    } catch {
        $result.Err = $_.Exception.Message
    } finally {
        $ErrorActionPreference = $saved
    }
    return $result
}

foreach ($override in @($env:CODEX_DISPATCH_MODEL, $env:CODEX_DISPATCH_REASONING)) {
    if ($override -and $override -notmatch '^[A-Za-z0-9._:/-]+$') {
        [Console]::Error.WriteLine('dispatch-task: CODEX_DISPATCH_MODEL and CODEX_DISPATCH_REASONING may hold only letters, digits, and . _ : / -')
        exit 2
    }
}
$registryPython = $null
if (Test-Path -LiteralPath $agentModelsPy -PathType Leaf) {
    $registryPython = Find-RegistryPython
}
$registryWhy = 'scripts/agent_models.py or a Python interpreter was not found'
$registryModel = ''
$registryEffort = ''
if ($registryPython -and (-not $env:CODEX_DISPATCH_MODEL -or -not $env:CODEX_DISPATCH_REASONING)) {
    $registryWhy = 'the resolver did not answer'
    $answer = Invoke-AgentModels -AgentArgs @('resolve', '--family', 'codex', '--role', 'prun-worker')
    if ($answer.Exit -eq 0 -and $answer.Out -match '^([A-Za-z0-9._:/-]+) ([A-Za-z0-9._:/-]+)$') {
        $registryModel = $Matches[1]
        $registryEffort = $Matches[2]
    } elseif ($answer.Err) {
        $registryWhy = $answer.Err -replace '\s+', ' '
    } elseif ($answer.Out) {
        $registryWhy = $answer.Out -replace '\s+', ' '
    }
}
$workerModel = if ($env:CODEX_DISPATCH_MODEL) { $env:CODEX_DISPATCH_MODEL } else { $registryModel }
$workerEffort = if ($env:CODEX_DISPATCH_REASONING) { $env:CODEX_DISPATCH_REASONING } else { $registryEffort }
if ($env:CODEX_DISPATCH_ISOLATE_MCP -ne 'off' -and (-not $workerModel -or -not $workerEffort)) {
    [Console]::Error.WriteLine("dispatch-task: cannot determine the Codex worker model and effort. The registry (user/agent-models.json, resolved by scripts/agent_models.py) names them in its prun-worker row for the codex family, and it did not answer: $registryWhy")
    [Console]::Error.WriteLine('Fix the registry or the Python setup (see the agent-models skill), or set CODEX_DISPATCH_MODEL and CODEX_DISPATCH_REASONING for this run. Refusing to start the unit.')
    exit 70
}
$requestedModel = if ($workerModel) { $workerModel } else { '-' }
$requestedEffort = if ($workerEffort) { $workerEffort } else { '-' }

$tmpBase = $env:TMPDIR
if (-not $tmpBase) { $tmpBase = $env:TEMP }
if (-not $tmpBase) { $tmpBase = $env:TMP }
if (-not $tmpBase) { $tmpBase = [System.IO.Path]::GetTempPath() }
$tmpBase = $tmpBase.TrimEnd('\', '/')

$cwdBytes = [System.Text.Encoding]::UTF8.GetBytes((Get-Location).Path)
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $hashBytes = $sha.ComputeHash($cwdBytes)
    $repoHash = ([System.BitConverter]::ToString($hashBytes)).Replace('-', '').Substring(0, 8).ToLower()
} finally {
    $sha.Dispose()
}

$nonceBytes = New-Object byte[] 8
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $rng.GetBytes($nonceBytes)
    $nonce = ([System.BitConverter]::ToString($nonceBytes)).Replace('-', '').ToLower()
} finally {
    $rng.Dispose()
}

$stateDirName = "prun-task-$repoHash-$UnitId-$PID-$nonce"
$stateDir = Join-Path $tmpBase $stateDirName
try {
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
} catch {
    [Console]::Error.WriteLine("dispatch-task: failed to create state-dir: $stateDir")
    exit 2
}

# Per-unit scratch working dir; codex runs from here so accidental relative
# writes, downloads, and caches stay out of the user's repo.
$scratchCwd = if ($env:PRUN_SCRATCH_CWD) { $env:PRUN_SCRATCH_CWD } else { Join-Path $stateDir 'work' }
try {
    New-Item -ItemType Directory -Path $scratchCwd -Force | Out-Null
} catch {
    [Console]::Error.WriteLine("dispatch-task: failed to create scratch cwd: $scratchCwd")
    exit 2
}

# Pre-dispatch mtime of any existing result file (Unix epoch seconds, not FILETIME).
$preMtime = 0
if (Test-Path -LiteralPath $ResultFile -PathType Leaf) {
    $utc = (Get-Item -LiteralPath $ResultFile).LastWriteTimeUtc
    $preMtime = [int]([DateTimeOffset]$utc).ToUnixTimeSeconds()
}
[System.IO.File]::WriteAllText((Join-Path $stateDir 'pre-mtime'), "$preMtime`n")
$nowUnix = [int]([DateTimeOffset]::UtcNow).ToUnixTimeSeconds()
[System.IO.File]::WriteAllText((Join-Path $stateDir 'timestamp'), "$nowUnix`n")
[System.IO.File]::WriteAllText((Join-Path $stateDir 'result-file'), "$ResultFile`n")
# Record this dispatcher's PID so monitor.ps1 can tell a stalled-but-alive unit
# from a dead dispatch (killed mid-run) that will never produce a result.
[System.IO.File]::WriteAllText((Join-Path $stateDir 'dispatch-pid'), "$PID`n")
[System.IO.File]::WriteAllText((Join-Path $stateDir 'model'), "$requestedModel`n")

[Console]::Out.WriteLine("STATE-DIR $stateDir")
[Console]::Out.Flush()

# Resolve codex binary (PathExt-aware; skip Store aliases). See dispatch-codex.ps1
# for the two Windows pitfalls (extensionless shim, WindowsApps alias).
$codexBin = if ($env:CODEX_BIN) { $env:CODEX_BIN } else { 'codex' }
$candidates = @(Get-Command -Name $codexBin -CommandType Application -ErrorAction SilentlyContinue |
    Where-Object {
        $src = [string]$_.Source
        $src -and ($src -notlike '*\WindowsApps\*')
    })
$resolved = $candidates | Where-Object { $_.Extension } | Select-Object -First 1
if (-not $resolved) { $resolved = $candidates | Select-Object -First 1 }
if ($resolved) { $codexBin = [string]$resolved.Source }

$tailPath = Join-Path $stateDir 'tail'
$ErrorActionPreference = 'Continue'

# Run codex via a transient .cmd helper, as dispatch-codex.ps1 does.
$sandboxMode = if ($env:CODEX_DISPATCH_SANDBOX) { $env:CODEX_DISPATCH_SANDBOX } else { 'danger-full-access' }
$isolateArg = if ($env:CODEX_DISPATCH_ISOLATE_MCP -eq 'off') { '' } else { "--ignore-user-config -c model=$workerModel -c model_reasoning_effort=$workerEffort " }
$codexBinEsc   = $codexBin   -replace '%', '%%'
$tailPathEsc   = $tailPath   -replace '%', '%%'
$promptFileEsc = $PromptFile -replace '%', '%%'
$sandboxModeEsc = $sandboxMode -replace '%', '%%'
$scratchEsc    = $scratchCwd -replace '%', '%%'

$cmdHelper = Join-Path $stateDir 'run-task.cmd'
$cmdBody = "@echo off`r`nchcp 65001 >NUL`r`ncd /d ""$scratchEsc"" || exit /b 1`r`n""$codexBinEsc"" exec --sandbox $sandboxModeEsc --skip-git-repo-check $isolateArg- > ""$tailPathEsc"" 2>&1 < ""$promptFileEsc""`r`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($cmdHelper, $cmdBody, $utf8NoBom)

# The idle-stall and hard-timeout bounds are enforced by reap-watch.ps1 (spawned below),
# which reads PRUN_STALL_THRESHOLD (default 600) and CODEX_DISPATCH_TIMEOUT (default 0 =
# wall-clock cap disabled; the idle signal is primary) from the environment it inherits
# here, and writes 'idle-stall' or 'hard-timeout' into <state-dir>/reap-reason if it reaps.
$dispatchReapExit = 124
$cmdExe = if ($env:ComSpec) { $env:ComSpec } else { 'cmd.exe' }
# Async launch via Start-Process -PassThru so reap-watch can be handed the worker PID and
# this dispatcher can block on WaitForExit. Without -RedirectStandardInput this uses
# ShellExecute, which does NOT strip the logon token (the cmd helper still does its own
# < > redirects, so codex's git / browser subprocesses keep the token; avoids Windows
# error 1312). The watch-and-kill loop deliberately lives in the SEPARATE reap-watch.ps1,
# NOT here: a single .ps1 that launches a hidden worker, polls it, and kills its tree
# trips some Windows AV AMSI heuristics and is blocked at parse. This mirrors the
# implement-review dispatch-codex.ps1 + stall-watch.ps1 split.
$worker = Start-Process -FilePath $cmdExe -ArgumentList @('/d', '/c', "`"$cmdHelper`"") -PassThru -WindowStyle Hidden
$codexExit = $null

# Spawn the background watchdog. It reaps the worker tree on idle-stall / hard-timeout and
# records the reason in <state-dir>/reap-reason. If the script is absent the dispatch
# still runs (just without the self-heal), so a partial deploy degrades gracefully.
$reapWatch = Join-Path $PSScriptRoot 'reap-watch.ps1'
if (Test-Path -LiteralPath $reapWatch -PathType Leaf) {
    $null = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$reapWatch`"",
                        '--state-dir', "`"$stateDir`"", '--worker-pid', $worker.Id, '--tail', "`"$tailPath`"") `
        -WindowStyle Hidden -PassThru -ErrorAction SilentlyContinue
}

# Block until the worker exits -- either codex finishes, or reap-watch kills the tree.
$worker.WaitForExit()
$codexExit = $worker.ExitCode

# Did reap-watch reap the worker? Its reason marker (written BEFORE the kill) tells us to
# surface exit 124 and a FALLBACK that names the trigger.
$reapReason = $null
$reapReasonPath = Join-Path $stateDir 'reap-reason'
if (Test-Path -LiteralPath $reapReasonPath -PathType Leaf) {
    try { $reapReason = (Get-Content -LiteralPath $reapReasonPath -Raw -ErrorAction Stop).Trim() } catch { $reapReason = $null }
}
if ($reapReason) {
    $codexExit = $dispatchReapExit
    [Console]::Error.WriteLine("dispatch-task: worker reaped by reap-watch ($reapReason)")
    try {
        [System.IO.File]::AppendAllText(
            $tailPath,
            "`ndispatch-task: worker reaped ($reapReason); exit code $dispatchReapExit.`n",
            $utf8NoBom
        )
    } catch {
        # best-effort tail note
    }
}

Remove-Item -LiteralPath $cmdHelper -Force -ErrorAction SilentlyContinue

# Ensure tail exists even if codex emitted nothing.
if (-not (Test-Path -LiteralPath $tailPath -PathType Leaf)) {
    Set-Content -LiteralPath $tailPath -Value '' -NoNewline -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath $tailPath -PathType Leaf) {
    try {
        Get-Content -LiteralPath $tailPath -Tail 80 | ForEach-Object { [Console]::Error.WriteLine($_) }
    } catch {
        # best-effort tail echo
    }
}

$actualLine = ''
if ($registryPython) {
    $actualArgs = @('actual', '--family', 'codex', '--stderr-file', $tailPath)
    if ($workerModel -and $workerEffort) { $actualArgs += @('--expect-model', $workerModel, '--expect-effort', $workerEffort) }
    $actual = Invoke-AgentModels -AgentArgs $actualArgs
    if ($actual.Out -match '^actual=') { $actualLine = $actual.Out }
    if ($actual.Err) { [Console]::Error.WriteLine($actual.Err) }
}
if (-not $actualLine) {
    $actualLine = 'actual=unknown status=unknown'
    [Console]::Error.WriteLine('dispatch-task: MODEL-UNVERIFIED the run could not be read back (no Python interpreter or resolver)')
}
$actualLine = $actualLine -replace '^actual=unknown status=', 'actual=- status='
[System.IO.File]::WriteAllText((Join-Path $stateDir 'model-actual'), "requested=$requestedModel $requestedEffort $actualLine`n")

# Result-loss backstop: if the unit did not write a non-empty result file (a
# worker's own result-write can fail, e.g. a fragile nested-PowerShell heredoc),
# salvage the captured tail into the result file so the unit is never SILENTLY
# missing when gather polls. The orchestrator still gets a non-empty result; the
# FALLBACK header flags that the structured result is absent and the body is raw
# worker output to review or re-dispatch.
$resultEmpty = $true
if (Test-Path -LiteralPath $ResultFile -PathType Leaf) {
    if ((Get-Item -LiteralPath $ResultFile).Length -gt 0) { $resultEmpty = $false }
}
if ($resultEmpty) {
    try {
        $tailText = if (Test-Path -LiteralPath $tailPath -PathType Leaf) { [System.IO.File]::ReadAllText($tailPath) } else { '' }
        if ($reapReason) {
            $fallbackConclusion = "INCOMPLETE; worker was reaped by dispatch-task ($reapReason) before writing a result; raw worker output salvaged from the dispatch tail below."
            $fallbackOpenItems = "worker was reaped by dispatch-task ($reapReason); review the raw output below or re-dispatch this unit."
            $fallbackVerification = "none (worker reaped by dispatch-task; tail salvaged by dispatch-task)"
        } else {
            $fallbackConclusion = "INCOMPLETE; structured result missing, raw worker output salvaged from the dispatch tail below."
            $fallbackOpenItems = "worker did not write its result file; review the raw output below or re-dispatch this unit."
            $fallbackVerification = "none (salvaged by dispatch-task, not by the worker)"
        }
        $fallback = "# $UnitId result (FALLBACK, worker wrote no result file)`n" +
            "Conclusion: $fallbackConclusion`n" +
            "Files: unknown`n" +
            "Open items: $fallbackOpenItems`n" +
            "Verification: $fallbackVerification`n`n" +
            $tailText
        $resultDir = Split-Path -Parent $ResultFile
        if ($resultDir -and -not (Test-Path -LiteralPath $resultDir)) {
            New-Item -ItemType Directory -Path $resultDir -Force | Out-Null
        }
        $utf8NoBomResult = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllText($ResultFile, $fallback, $utf8NoBomResult)
    } catch {
        # best-effort backstop; nothing more to do if even this salvage write fails
    }
}

exit $codexExit
