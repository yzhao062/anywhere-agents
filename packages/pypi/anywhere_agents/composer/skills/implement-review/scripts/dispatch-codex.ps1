# dispatch-codex.ps1 -- Auto-terminal channel dispatch for implement-review skill.
# See skills/implement-review/SKILL.md > Phase 1c Auto-terminal path for the contract.
#
# Self-review guard: refuses to dispatch when the invoking orchestrator is
# Codex itself (would be self-review, which the fungibility principle
# disallows). Delegates to _codex_guard.ps1.
#
# Args (named, --foo style for cross-platform parity with .sh):
#   --prompt-file <path>           Path to file containing the review prompt
#   --round <N>                    Round number (positive integer)
#   --expected-review-file <name>  Review file the reviewer is expected to write
#                                  (resolved relative to cwd for pre-mtime snapshot)
#
# Env:
#   CODEX_BIN                      Codex binary name or path (default: codex)
#   IMPLEMENT_REVIEW_ORCHESTRATOR  Who is driving this dispatch: claude / codex / user.
#                                  When 'codex' (case-insensitive), refuses to
#                                  dispatch with exit 2. When unset/empty AND
#                                  either CODEX_THREAD_ID or CODEX_SESSION_ID is
#                                  set to a non-empty value, also refuses. Any
#                                  other value (claude / user) proceeds.
#   CODEX_THREAD_ID                Set by Codex in its tool subprocesses (observed
#                                  with codex-cli 0.156.1). Fall-through guard
#                                  signal; child processes inherit it.
#   CODEX_SESSION_ID               Same as CODEX_THREAD_ID, for the session ID.
#   ANYWHERE_AGENTS_PYTHON         Explicit Python interpreter override
#   CODEX_DISPATCH_MODEL           One-run override of the reviewer model.
#   CODEX_DISPATCH_REASONING       One-run override of the reviewer effort.
#   AGENT_MODELS_FILE              Alternate registry file (tests, one-off runs).
#   TMPDIR / TEMP / TMP            Temp dir for state-dir (Windows uses TEMP by default)
#
# Model and effort: the registry (user/agent-models.json, read through
# scripts/agent_models.py) names both; see dispatch-codex.sh for the order of
# sources and <state-dir>/review-model for what is recorded.
#
# Stdout:
#   First (and only) machine-readable line: STATE-DIR <abs-path>
#
# Stderr:
#   Dispatch diagnostics + last 80 lines of codex-exec combined stdout+stderr.
#   MODEL-MISMATCH when the run did not use the requested model or effort.
#
# Exit code:
#   Propagates codex exec's exit code unchanged.
#   Returns 2 on usage errors (missing/invalid args) or self-review refusal.
#   Returns 70 when no source names the reviewer model and effort.

$ErrorActionPreference = 'Stop'

# Release the deployed path before the review child can invoke bootstrap.
# PowerShell closes parsed script files, so the parent can wait for the copy,
# remove it deterministically, and propagate the copied script's exit status.
if ($env:IMPLEMENT_REVIEW_DISPATCH_REEXEC -ne '1') {
    $reexecTmpBase = $env:TMPDIR
    if (-not $reexecTmpBase) { $reexecTmpBase = $env:TEMP }
    if (-not $reexecTmpBase) { $reexecTmpBase = $env:TMP }
    if (-not $reexecTmpBase) { $reexecTmpBase = [System.IO.Path]::GetTempPath() }
    $reexecTmpBase = $reexecTmpBase.TrimEnd('\', '/')
    $reexecDir = Join-Path $reexecTmpBase "implement-review-dispatch-codex-reexec-$PID"
    $reexecCopy = Join-Path $reexecDir 'dispatch-codex.ps1'

    try {
        New-Item -ItemType Directory -Path $reexecDir | Out-Null
        Copy-Item -LiteralPath $PSCommandPath -Destination $reexecCopy
    } catch {
        [Console]::Error.WriteLine("dispatch-codex: failed to create re-exec copy: $reexecCopy")
        if (Test-Path -LiteralPath $reexecCopy -PathType Leaf) {
            Remove-Item -LiteralPath $reexecCopy -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $reexecDir -Force -ErrorAction SilentlyContinue
        exit 2
    }

    $env:IMPLEMENT_REVIEW_DISPATCH_REEXEC = '1'
    $env:IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR = $PSScriptRoot
    $reexecHost = if ($PSVersionTable.PSEdition -eq 'Core') {
        Join-Path $PSHOME 'pwsh.exe'
    } else {
        Join-Path $PSHOME 'powershell.exe'
    }
    $reexecExit = 2
    try {
        & $reexecHost -NoProfile -ExecutionPolicy Bypass -File $reexecCopy @args
        $reexecExit = $LASTEXITCODE
    } catch {
        [Console]::Error.WriteLine("dispatch-codex: failed to launch re-exec copy: $reexecCopy")
    } finally {
        Remove-Item -LiteralPath $reexecCopy -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $reexecDir -Force -ErrorAction SilentlyContinue
    }
    exit $reexecExit
}

$sourceScriptDir = if ($env:IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR) {
    $env:IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR
} else {
    $PSScriptRoot
}

# Parse args manually to support --foo style (cross-platform parity with .sh)
$PromptFile = $null
$Round = $null
$ExpectedReviewFile = $null

$i = 0
while ($i -lt $args.Length) {
    switch ($args[$i]) {
        '--prompt-file' {
            $PromptFile = $args[$i + 1]; $i += 2
        }
        '--round' {
            $Round = $args[$i + 1]; $i += 2
        }
        '--expected-review-file' {
            $ExpectedReviewFile = $args[$i + 1]; $i += 2
        }
        default {
            [Console]::Error.WriteLine("dispatch-codex: unknown argument: $($args[$i])")
            [Console]::Error.WriteLine("Usage: dispatch-codex.ps1 --prompt-file <path> --round <N> --expected-review-file <name>")
            exit 2
        }
    }
}

if (-not $PromptFile -or -not $Round -or -not $ExpectedReviewFile) {
    [Console]::Error.WriteLine("dispatch-codex: missing required argument")
    [Console]::Error.WriteLine("Usage: dispatch-codex.ps1 --prompt-file <path> --round <N> --expected-review-file <name>")
    exit 2
}

if (-not (Test-Path -LiteralPath $PromptFile -PathType Leaf)) {
    [Console]::Error.WriteLine("dispatch-codex: prompt file not found: $PromptFile")
    exit 2
}

if (-not ($Round -match '^\d+$')) {
    [Console]::Error.WriteLine("dispatch-codex: --round must be a positive integer, got: $Round")
    exit 2
}

# Self-review safety check. Logic lives in a small helper to keep this
# launcher focused on dispatch mechanics. See SKILL.md for the contract.
$guardScript = Join-Path $sourceScriptDir '_codex_guard.ps1'
if (Test-Path -LiteralPath $guardScript -PathType Leaf) {
    & $guardScript
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE) {
        # Guard already wrote its own stderr and exit code; propagate.
        exit $LASTEXITCODE
    }
}

# Resolve a Python interpreter for the reviewer before spending a Codex round.
# App Execution Aliases are discoverable zero-length reparse points, so path
# lookup alone is insufficient. Each candidate must execute isolated code,
# report an absolute sys.executable, and pass a second direct execution probe.
function Resolve-ProbedPython {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$PrefixArgs = @()
    )

    if (-not $Exe -or $Exe -match '(?i)[\\/]WindowsApps[\\/]') { return $null }

    $launchPath = $Exe
    if (-not (Test-Path -LiteralPath $launchPath -PathType Leaf)) {
        $command = Get-Command -Name $launchPath -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $command -or -not $command.Source) { return $null }
        $launchPath = [string]$command.Source
    }
    if ($launchPath -match '(?i)[\\/]WindowsApps[\\/]') { return $null }

    try {
        $probeArgs = @($PrefixArgs) + @(
            '-I', '-c',
            # The -c argument carries no double quote: Windows PowerShell 5.1
            # rebuilds the native command line without escaping one, so Python
            # receives a truncated program and the probe rejects a working
            # interpreter. Same defect as anywhere-agents#34 in bootstrap.ps1.
            "import os,sys;print('IMPLEMENT_REVIEW_PYTHON='+sys.executable);print('IMPLEMENT_REVIEW_REALPATH='+os.path.realpath(sys.executable))"
        )
        $probeOutput = @(& $launchPath @probeArgs 2>$null)
        $probeExit = $LASTEXITCODE
    } catch {
        return $null
    }
    if ($probeExit -ne 0) { return $null }

    $resolvedPath = $null
    $resolvedRealPath = $null
    foreach ($line in $probeOutput) {
        $text = ([string]$line).Trim()
        if ($text.StartsWith('IMPLEMENT_REVIEW_PYTHON=')) {
            $resolvedPath = $text.Substring('IMPLEMENT_REVIEW_PYTHON='.Length)
        } elseif ($text.StartsWith('IMPLEMENT_REVIEW_REALPATH=')) {
            $resolvedRealPath = $text.Substring('IMPLEMENT_REVIEW_REALPATH='.Length)
        }
    }
    if (-not $resolvedPath -or -not $resolvedRealPath -or
        -not [System.IO.Path]::IsPathRooted($resolvedPath)) {
        return $null
    }
    if ($resolvedPath -match '(?i)[\\/]WindowsApps[\\/]') { return $null }
    # Canonicalization is only an alias-location check. Keep the selected
    # sys.executable unchanged so POSIX venv symlinks retain their environment.
    if ($resolvedRealPath -match '(?i)[\\/]WindowsApps[\\/]') { return $null }

    $resolvedItem = Get-Item -LiteralPath $resolvedPath -ErrorAction SilentlyContinue
    if (-not $resolvedItem -or $resolvedItem.PSIsContainer -or $resolvedItem.Length -le 0) {
        return $null
    }
    try {
        & $resolvedPath -I -c 'import sys; sys.exit(0)' >$null 2>&1
        $directExit = $LASTEXITCODE
    } catch {
        return $null
    }
    if ($directExit -ne 0) { return $null }
    return $resolvedPath
}

# The reviewer model and effort, in the order dispatch-codex.sh documents: the
# env overrides, the registry, then the top-level model and
# model_reasoning_effort of config.toml. A config model is skipped under a
# non-OpenAI model_provider, which isolation drops. A value no source names
# stays empty; the caller stops the dispatch.
function Resolve-CodexReview {
    $review = @{ Model = ''; ModelSource = ''; Effort = ''; EffortSource = '' }
    if ($env:CODEX_DISPATCH_MODEL) {
        $review.Model = $env:CODEX_DISPATCH_MODEL
        $review.ModelSource = 'env'
    } elseif ($registryModel) {
        $review.Model = $registryModel
        $review.ModelSource = 'registry'
    }
    if ($env:CODEX_DISPATCH_REASONING) {
        $review.Effort = $env:CODEX_DISPATCH_REASONING
        $review.EffortSource = 'env'
    } elseif ($registryEffort) {
        $review.Effort = $registryEffort
        $review.EffortSource = 'registry'
    }
    if ($review.Model -and $review.Effort) { return $review }
    $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
    $configFile = Join-Path $codexHome 'config.toml'
    $configModel = ''
    $configEffort = ''
    $provider = ''
    if (Test-Path -LiteralPath $configFile -PathType Leaf) {
        try {
            foreach ($line in Get-Content -LiteralPath $configFile -ErrorAction Stop) {
                if ($line -match '^\s*\[') { break }
                if ($line -cmatch '^\s*(?<key>model_provider|model_reasoning_effort|model)\s*=\s*(["''])(?<val>.*?)\1') {
                    $key = $Matches['key']
                    $value = $Matches['val']
                    if ($key -eq 'model_provider') {
                        $provider = $value
                    } elseif ($key -eq 'model_reasoning_effort') {
                        if (-not $configEffort -and $value -cmatch '^[A-Za-z0-9._:/-]+$') { $configEffort = $value }
                    } elseif (-not $configModel -and $value -cmatch '^[A-Za-z0-9._:/-]+$') {
                        $configModel = $value
                    }
                }
            }
        } catch {
            # An unreadable config supplies nothing.
        }
    }
    if (-not $review.Model -and $configModel -and (-not $provider -or $provider -eq 'openai')) {
        $review.Model = $configModel
        $review.ModelSource = 'config'
    }
    if (-not $review.Effort -and $configEffort) {
        $review.Effort = $configEffort
        $review.EffortSource = 'config'
    }
    return $review
}

# Run agent_models.py with the resolved interpreter; exit code, stdout, and
# stderr come back as text. Stderr lines are the error stream's records.
function Invoke-AgentModels {
    param([string[]]$AgentArgs)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $result = @{ Exit = 1; Out = ''; Err = '' }
    try {
        $lines = @(& $pythonBin $agentModelsPy @AgentArgs 2>&1)
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

function Use-PythonPath {
    param([string]$Candidate)
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }
    $resolved = Resolve-ProbedPython -Exe $Candidate
    if (-not $resolved) { return $false }
    $script:pythonBin = $resolved
    return $true
}

$pythonBin = $null

# 1. Explicit shared override. A broken configured override is a hard preflight
# failure so machine drift cannot be hidden by a lower-priority interpreter.
if ($env:ANYWHERE_AGENTS_PYTHON) {
    $pythonBin = Resolve-ProbedPython -Exe $env:ANYWHERE_AGENTS_PYTHON
    if (-not $pythonBin) {
        [Console]::Error.WriteLine("dispatch-codex: ANYWHERE_AGENTS_PYTHON is not an executable Python interpreter: $($env:ANYWHERE_AGENTS_PYTHON)")
        exit 2
    }
}

# 2. Project virtual environments, then an activated virtual environment.
if (-not $pythonBin) {
    foreach ($candidate in @(
        (Join-Path (Get-Location).Path '.venv\Scripts\python.exe'),
        (Join-Path (Get-Location).Path '.venv/bin/python'),
        (Join-Path (Get-Location).Path 'venv\Scripts\python.exe'),
        (Join-Path (Get-Location).Path 'venv/bin/python')
    )) {
        if (Use-PythonPath $candidate) { break }
    }
}
if (-not $pythonBin -and $env:VIRTUAL_ENV) {
    foreach ($candidate in @(
        (Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'),
        (Join-Path $env:VIRTUAL_ENV 'bin/python')
    )) {
        if (Use-PythonPath $candidate) { break }
    }
}

# 3. Active conda environment, then discovered Miniforge/conda roots and envs.
$condaRoots = @()
if (-not $pythonBin -and $env:CONDA_PREFIX) {
    foreach ($candidate in @(
        (Join-Path $env:CONDA_PREFIX 'python.exe'),
        (Join-Path $env:CONDA_PREFIX 'bin/python'),
        (Join-Path $env:CONDA_PREFIX 'bin/python3')
    )) {
        if (Use-PythonPath $candidate) { break }
    }
}
if ($env:CONDA_PREFIX) {
    $prefixParent = Split-Path $env:CONDA_PREFIX -Parent
    if ($prefixParent -and (Split-Path $prefixParent -Leaf) -eq 'envs') {
        $condaRoots += (Split-Path $prefixParent -Parent)
    } else {
        $condaRoots += $env:CONDA_PREFIX
    }
}
if ($env:CONDA_ROOT) { $condaRoots += $env:CONDA_ROOT }
if ($env:MAMBA_ROOT_PREFIX) { $condaRoots += $env:MAMBA_ROOT_PREFIX }
foreach ($managerName in @('conda', 'mamba')) {
    $manager = Get-Command -Name $managerName -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($manager -and $manager.Source) {
        $managerRoot = Split-Path (Split-Path ([string]$manager.Source) -Parent) -Parent
        if ($managerRoot) { $condaRoots += $managerRoot }
    }
}
foreach ($homeDir in @($HOME, $env:USERPROFILE)) {
    if (-not $homeDir -or -not (Test-Path -LiteralPath $homeDir -PathType Container)) {
        continue
    }
    Get-ChildItem -LiteralPath $homeDir -Directory -Force -ErrorAction SilentlyContinue |
        ForEach-Object {
            if (Test-Path -LiteralPath (Join-Path $_.FullName 'conda-meta') -PathType Container) {
                $condaRoots += $_.FullName
            }
        }
}
if (-not $pythonBin) {
    :rootLoop foreach ($root in ($condaRoots | Where-Object { $_ } | Select-Object -Unique)) {
        foreach ($candidate in @(
            (Join-Path $root 'python.exe'),
            (Join-Path $root 'bin/python'),
            (Join-Path $root 'bin/python3')
        )) {
            if (Use-PythonPath $candidate) { break rootLoop }
        }
        $envsDir = Join-Path $root 'envs'
        if (Test-Path -LiteralPath $envsDir -PathType Container) {
            foreach ($environment in (Get-ChildItem -LiteralPath $envsDir -Directory -ErrorAction SilentlyContinue)) {
                foreach ($candidate in @(
                    (Join-Path $environment.FullName 'python.exe'),
                    (Join-Path $environment.FullName 'bin/python'),
                    (Join-Path $environment.FullName 'bin/python3')
                )) {
                    if (Use-PythonPath $candidate) { break rootLoop }
                }
            }
        }
    }
}

# 4. Windows Python launcher. Resolve through sys.executable so the reviewer
# receives an absolute interpreter path rather than the launcher command.
if (-not $pythonBin) {
    foreach ($candidate in @(Get-Command -Name py -All -CommandType Application -ErrorAction SilentlyContinue)) {
        if (-not $candidate.Source) { continue }
        $pythonBin = Resolve-ProbedPython -Exe ([string]$candidate.Source) -PrefixArgs @('-3')
        if ($pythonBin) { break }
    }
}

# 5. Enumerate all PATH entries so a first-position Store alias cannot hide a
# later interpreter. Functional execution remains the deciding test.
if (-not $pythonBin) {
    :pathLoop foreach ($commandName in @('python3', 'python')) {
        foreach ($candidate in @(Get-Command -Name $commandName -All -CommandType Application -ErrorAction SilentlyContinue)) {
            if (-not $candidate.Source) { continue }
            $pythonBin = Resolve-ProbedPython -Exe ([string]$candidate.Source)
            if ($pythonBin) { break pathLoop }
        }
    }
}

# Not finding an interpreter is a degradation, not a dispatch failure. The
# probe exists to keep a reviewer from claiming verification it never ran, and
# health check 10 already enforces that independently by refusing a PASS or
# BLOCK verdict without a VERIFIED status. Reviews also run on repositories
# whose verification is not Python at all (LaTeX, Node, docs), so a missing
# interpreter must not make those repositories unreviewable.
if (-not $pythonBin) {
    [Console]::Error.WriteLine('dispatch-codex: no working Python interpreter found (checked ANYWHERE_AGENTS_PYTHON, project virtualenvs, conda/Miniforge, py -3, and non-WindowsApps PATH entries); dispatching without a pre-resolved interpreter')
}

$pwshBin = $null
$pwshRejected = $null
foreach ($candidate in @(Get-Command -Name pwsh -All -CommandType Application -ErrorAction SilentlyContinue)) {
    if (-not $candidate.Source) { continue }
    $candidatePath = [string]$candidate.Source
    if ($candidatePath -match '(?i)[\\/]WindowsApps[\\/]') {
        if (-not $pwshRejected) { $pwshRejected = $candidatePath }
        continue
    }
    $candidateItem = Get-Item -LiteralPath $candidatePath -ErrorAction SilentlyContinue
    if (-not $candidateItem -or $candidateItem.PSIsContainer -or $candidateItem.Length -le 0) {
        if (-not $pwshRejected) { $pwshRejected = $candidatePath }
        continue
    }
    $pwshBin = $candidatePath
    break
}
if (-not $pwshBin) {
    $rejectedNote = if ($pwshRejected) { " (rejected Store-alias or zero-length candidate: $pwshRejected)" } else { '' }
    [Console]::Error.WriteLine("dispatch-codex: no usable pwsh found$rejectedNote; the reviewer will be told to avoid pwsh")
}

# Resolve temp base (TMPDIR > TEMP > TMP > sane fallback)
$tmpBase = $env:TMPDIR
if (-not $tmpBase) { $tmpBase = $env:TEMP }
if (-not $tmpBase) { $tmpBase = $env:TMP }
if (-not $tmpBase) { $tmpBase = [System.IO.Path]::GetTempPath() }
$tmpBase = $tmpBase.TrimEnd('\', '/')

# Repo-hash from cwd (8-char prefix of sha256)
$cwdBytes = [System.Text.Encoding]::UTF8.GetBytes((Get-Location).Path)
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $hashBytes = $sha.ComputeHash($cwdBytes)
    $repoHash = ([System.BitConverter]::ToString($hashBytes)).Replace('-', '').Substring(0, 8).ToLower()
} finally {
    $sha.Dispose()
}

# Nonce: 8 random bytes hex (16 chars)
$nonceBytes = New-Object byte[] 8
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $rng.GetBytes($nonceBytes)
    $nonce = ([System.BitConverter]::ToString($nonceBytes)).Replace('-', '').ToLower()
} finally {
    $rng.Dispose()
}

$stateDirName = "implement-review-codex-$repoHash-round$Round-$PID-$nonce"
$stateDir = Join-Path $tmpBase $stateDirName

try {
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
} catch {
    [Console]::Error.WriteLine("dispatch-codex: failed to create state-dir: $stateDir")
    exit 2
}

# Record pre-dispatch mtime of any existing Review-Codex.md (Unix epoch seconds)
$preMtime = 0
if (Test-Path -LiteralPath $ExpectedReviewFile -PathType Leaf) {
    $utc = (Get-Item -LiteralPath $ExpectedReviewFile).LastWriteTimeUtc
    $preMtime = [int]([DateTimeOffset]$utc).ToUnixTimeSeconds()
}
[System.IO.File]::WriteAllText((Join-Path $stateDir 'pre-mtime'), "$preMtime`n")

# Record dispatch start wall time (Unix epoch seconds)
$nowUnix = [int]([DateTimeOffset]::UtcNow).ToUnixTimeSeconds()
[System.IO.File]::WriteAllText((Join-Path $stateDir 'timestamp'), "$nowUnix`n")

# One logical dispatch may retry only a confirmed response-stream death. The
# state directory stays stable so the single STATE-DIR stdout contract remains
# valid; failed attempts are archived under attempt-N.
$streamRetryLimit = 1
$parsedStreamRetryLimit = 0
if ([int]::TryParse(
        [string]$env:IMPLEMENT_REVIEW_STREAM_RETRY_LIMIT,
        [ref]$parsedStreamRetryLimit
    ) -and $parsedStreamRetryLimit -ge 0) {
    $streamRetryLimit = $parsedStreamRetryLimit
}
$streamRetryCount = 0
$streamRetryLimitPath = Join-Path $stateDir 'stream-retry-limit'
$streamRetryCountPath = Join-Path $stateDir 'stream-retry-count'
[System.IO.File]::WriteAllText($streamRetryLimitPath, "$streamRetryLimit`n")
[System.IO.File]::WriteAllText($streamRetryCountPath, "0`n")

# Record exactly what was passed to the reviewer for caller diagnostics.
# Absence of the marker means the probe resolved nothing. Writing a sentinel
# instead would let a reader mistake it for a path.
if ($pythonBin) {
    [System.IO.File]::WriteAllText((Join-Path $stateDir 'python-interpreter'), "$pythonBin`n")
}
if ($pwshBin) {
    [System.IO.File]::WriteAllText((Join-Path $stateDir 'pwsh-interpreter'), "$pwshBin`n")
}

# Resolve the review model and effort here, well away from the cmdBody
# construction, and record which source won. The registry is read through
# scripts/agent_models.py (see dispatch-codex.sh); no literal lives here.
# Isolation off leaves codex to read its own config.
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
$agentModelsPy = Find-AgentModels -Start $sourceScriptDir
if (-not $agentModelsPy) { $agentModelsPy = Join-Path $sourceScriptDir 'agent_models.py.not-found' }
$registryModel = ''
$registryEffort = ''
$registryWhy = 'scripts/agent_models.py or a working Python interpreter was not found'
if ($pythonBin -and (Test-Path -LiteralPath $agentModelsPy -PathType Leaf)) {
    $registryWhy = 'the resolver did not answer'
    $answer = Invoke-AgentModels -AgentArgs @('resolve', '--family', 'codex', '--role', 'reviewer')
    if ($answer.Exit -eq 0 -and $answer.Out -match '^([A-Za-z0-9._:/-]+) ([A-Za-z0-9._:/-]+)$') {
        $registryModel = $Matches[1]
        $registryEffort = $Matches[2]
    } elseif ($answer.Err) {
        $registryWhy = $answer.Err -replace '\s+', ' '
    }
}
$model = ''
$reasoning = ''
$expectArgs = @()
$reviewModelPath = Join-Path $stateDir 'review-model'
if ($env:CODEX_DISPATCH_ISOLATE_MCP -ne 'off') {
    $review = Resolve-CodexReview
    $model = $review.Model
    $reasoning = $review.Effort
    if (-not $model -or -not $reasoning) {
        $registryMessage = @(
            "dispatch-codex: cannot determine the reviewer model and effort. The registry (user/agent-models.json, resolved by scripts/agent_models.py) did not answer ($registryWhy), and the top-level model and model_reasoning_effort of config.toml do not supply the rest.",
            'Fix the registry or the Python setup (see the agent-models skill), or set CODEX_DISPATCH_MODEL and CODEX_DISPATCH_REASONING for this run. Refusing to spend a review round.'
        ) -join "`r`n"
        [System.IO.File]::WriteAllText((Join-Path $stateDir 'tail'), "$registryMessage`r`n")
        [Console]::Out.WriteLine("STATE-DIR $stateDir")
        [Console]::Out.Flush()
        [Console]::Error.WriteLine($registryMessage)
        exit 70
    }
    [System.IO.File]::WriteAllText($reviewModelPath, "model=$model source=$($review.ModelSource) effort=$reasoning effort_source=$($review.EffortSource)`n")
    $expectArgs = @('--expect-model', $model, '--expect-effort', $reasoning)
} else {
    [System.IO.File]::WriteAllText($reviewModelPath, "model=- source=user-config effort=- effort_source=user-config`n")
    if ($registryModel -and $registryEffort) {
        $expectArgs = @('--expect-model', $registryModel, '--expect-effort', $registryEffort)
    }
}

# Emit STATE-DIR on stdout (first and only machine-readable line)
[Console]::Out.WriteLine("STATE-DIR $stateDir")
[Console]::Out.Flush()

# Launch stall-watch in background if present (B2 will land the script)
# Use $PSScriptRoot (auto-populated when this script is invoked as a file)
# instead of Split-Path $PSCommandPath, which can fail with "Parameter set
# cannot be resolved" if PowerShell sees $PSCommandPath as null under some
# invocation contexts.
$scriptDir = if ($env:IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR) {
    $env:IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR
} else {
    $PSScriptRoot
}
$stallWatch = Join-Path $scriptDir 'stall-watch.ps1'
$stallProc = $null
function Start-StallWatch {
    $script:stallProc = $null
    if (Test-Path -LiteralPath $stallWatch -PathType Leaf) {
        $script:stallProc = Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $stallWatch,
                            '--state-dir', $stateDir, '--parent-pid', $PID) `
            -WindowStyle Hidden -PassThru -ErrorAction SilentlyContinue
    }
}
Start-StallWatch
Remove-Item Env:IMPLEMENT_REVIEW_DISPATCH_REEXEC -ErrorAction SilentlyContinue
Remove-Item Env:IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR -ErrorAction SilentlyContinue

# Resolve codex binary -----------------------------------------------------
# Two real-world Windows pitfalls covered:
# (1) npm / pnpm / yarn install codex as TWO files in the same PATH dir --
#     an extensionless bash shim (`codex`) plus a `.cmd`/`.ps1` wrapper.
#     CreateProcess matches the extensionless shim by exact name and
#     fails ("not a valid Win32 application") because it has no PE header.
#     Get-Command honors PathExt and returns the runnable .cmd/.exe.
# (2) Microsoft Store App Execution Aliases under \WindowsApps\ would
#     launch the Store rather than execute codex; skip those.
# POSIX builds (pwsh on Linux/macOS) hit neither pitfall but the same
# Get-Command lookup still resolves the extensionless executable; safe.
#
# Selection: prefer the FIRST extension-bearing candidate in PATH order.
# Earlier revision used Sort-Object with a custom Expression scriptblock
# to put extension-bearing entries before extensionless ones. Windows
# PowerShell 5.1's Sort-Object is NOT stable under such custom keys --
# reproduced live with PATH=A;B holding both A\codex.cmd and B\codex.cmd:
# Sort-Object returned B first, so the dispatcher would pick the wrong
# install on a user with multiple codex installs. The explicit two-pass
# filter below preserves Get-Command's PATH-order output on every PS
# version (5.1 and 7.x both verified).
$codexBin = if ($env:CODEX_BIN) { $env:CODEX_BIN } else { 'codex' }
$candidates = @(Get-Command -Name $codexBin -CommandType Application -ErrorAction SilentlyContinue |
    Where-Object {
        $src = [string]$_.Source
        $src -and ($src -notlike '*\WindowsApps\*')
    })
$resolved = $candidates | Where-Object { $_.Extension } | Select-Object -First 1
if (-not $resolved) {
    $resolved = $candidates | Select-Object -First 1
}
if ($resolved) {
    $codexBin = [string]$resolved.Source
}
# If still nothing resolvable, $codexBin may be an absolute path the
# caller passed via $env:CODEX_BIN; let the invocation surface its own
# error to the tail rather than guessing here.

$tailPath = Join-Path $stateDir 'tail'

$ErrorActionPreference = 'Continue'

# Run codex exec with prompt via stdin (NOT positional arg; not codex exec
# review). Spawn via a transient .cmd helper script. Three reasons:
#
# 1. Byte-parity: cmd's `< file > tail 2>&1` are byte-level OS-handle
#    redirections that preserve the byte-identical prompt invariant
#    declared in SKILL.md Phase 1c (no BOM injection, no CRLF drift).
#    Matches the .sh path's `< \$PROMPT_FILE > tail 2>&1` semantics.
#
# 2. Session-token preservation: PowerShell's `Start-Process` with
#    -RedirectStandardInput uses CreateProcessAsUserW under the hood,
#    which strips the user's logon-session token when chaining child
#    processes. codex then cannot spawn its own git/grep subprocess
#    (Windows error 1312: "no logon session"). cmd /c uses plain
#    CreateProcess, which inherits the calling thread's token cleanly
#    so codex's children spawn normally.
#
# 3. AV behavior: standard Set-Content + invocation operator. The
#    System.Diagnostics.Process.StandardInput.BaseStream.Write pattern
#    used in an earlier revision was flagged by some AVs as a process-
#    injection signature.
#
# Path handling: cmd treats `%NAME%` as env-var expansion. A user / tmp
# / state-dir path containing `%` (e.g. project named `foo%bar`) would
# be silently rewritten by cmd before codex saw the redirection. Escape
# every `%` to `%%` in interpolated path values so cmd reads them as
# literals. The helper is written as UTF-8 (no BOM) with a `chcp 65001`
# prefix so non-ASCII user names (Cyrillic, CJK, accented Latin) survive
# round-tripping through cmd's codepage layer; the earlier `-Encoding
# ASCII` write rejected non-ASCII bytes entirely.
#
# Stream interleaving: stdout and stderr merge into <state-dir>/tail in
# real time as cmd writes them, so stall-watch observes live growth (no
# need for the dual-file workaround used in the prior revision).
# --sandbox danger-full-access aligns Auto-terminal's trust model with
# Terminal-relay: the user invoked /implement-review on their own machine
# and Codex has the same fs / network / shell access it would have in an
# interactive Codex terminal. This also sidesteps Codex's workspace-write
# sandbox CreateProcessAsUserW failed: 1312 bug on Windows 0.130.0, where
# Codex's own shell runner could not spawn git / grep / pwsh subprocesses
# so the review came back as "could not access files". Scope discipline
# (review-only, save to Review-Codex.md, no commits / pushes) is enforced
# at the prompt level, identical to Terminal-relay. For CI / shared
# environments where this trust posture is too broad, override via
# $env:CODEX_DISPATCH_SANDBOX (default: danger-full-access).
$cmdHelper = Join-Path $stateDir 'run-codex.cmd'
$codexBinEsc = $codexBin -replace '%', '%%'
$tailPathEsc = $tailPath -replace '%', '%%'
$promptFileEsc = $PromptFile -replace '%', '%%'
$sandboxMode = if ($env:CODEX_DISPATCH_SANDBOX) { $env:CODEX_DISPATCH_SANDBOX } else { 'danger-full-access' }
$sandboxModeEsc = $sandboxMode -replace '%', '%%'
# Reviewer isolation (default on; CODEX_DISPATCH_ISOLATE_MCP=off opts out).
# --ignore-user-config drops user MCP/plugins/hooks; model and reasoning are
# re-passed. See dispatch-codex.sh for the defaults and what is not kept.
$isolateArg = if ($env:CODEX_DISPATCH_ISOLATE_MCP -eq 'off') { '' } else { "--ignore-user-config -c model=$model -c model_reasoning_effort=$reasoning " }
$pythonInstruction = if ($pythonBin) {
    "A working Python interpreter was execution-probed before dispatch. Use this exact absolute path for every Python command: $pythonBin. Do not invoke bare python, python3, or py."
} else {
    'No Python interpreter could be pre-resolved on this machine. If a verification command needs Python, locate a working interpreter yourself first; if none exists, verify by whatever means this repository actually uses.'
}
$pwshInstruction = if ($pwshBin) {
    "PowerShell 7 was resolved at this absolute path: $pwshBin. If a command needs it, invoke that exact path rather than bare pwsh."
} else {
    'No usable PowerShell 7 was found on this machine; a bare pwsh call may be a Microsoft Store execution alias that fails instantly under this spawn context and retries forever. Do not shell out to pwsh. Use bash, Windows PowerShell 5.1 (powershell.exe), or plain git commands instead.'
}
$childSessionInstructions = "The parent agent session already completed the repository bootstrap at startup. Skip bootstrap and shared configuration refresh commands in this child review session. Skip the session-start banner as well: no human reads a dispatched child's terminal, so rendering it only spends shell spawns. Use the shared configuration currently on disk. This session is an assigned reviewer. Its task, lens, and response format are already supplied. Skip router dispatch and coordinator-workflow discovery at all skill lookup locations (skills/implement-review/, .claude/skills/implement-review/, .agent-config/repo/skills/implement-review/, .agents/skills/implement-review/). Do not load coordinator skill files or example reviews to discover the workflow or response format. Files under review remain readable. A targeted read outside the review scope is permitted when it supplies missing context, resolves truncation, or answers a concrete verification question. Apply the substantive project instructions supplied in context, including applicable local overrides. Read an applicable instruction file once only when its rules have not been supplied. For a staged-change review, obtain the selected diff once with the command in the request; if the request embeds that diff, use it and do not obtain another copy. For a plan review, the named plan and evidence files are the review input: read each once from the repository or from the supplied copy, as the request directs. Do not read the same content twice. $pythonInstruction $pwshInstruction Before issuing a PASS or BLOCK commit verdict, execute relevant verification commands. In $ExpectedReviewFile, add one standalone line exactly 'Verification status: VERIFIED' if at least one relevant verification command completed, otherwise add 'Verification status: UNVERIFIED'. If the status is UNVERIFIED, write 'Commit verdict: UNVERIFIED'; never issue PASS or BLOCK. Verification notes must list the exact commands and outcomes."
$childSessionInstructionsEsc = $childSessionInstructions -replace '%', '%%'
# project_doc_max_bytes raises Codex's 32 KiB instruction budget with isolation on or off (see dispatch-codex.sh).
$childSessionArg = "-c ""developer_instructions=$childSessionInstructionsEsc"" -c project_doc_max_bytes=262144 "
$cmdBody = "@echo off`r`nchcp 65001 >NUL`r`n""$codexBinEsc"" exec --sandbox $sandboxModeEsc $isolateArg$childSessionArg- > ""$tailPathEsc"" 2>&1 < ""$promptFileEsc""`r`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($cmdHelper, $cmdBody, $utf8NoBom)

function Save-StreamDeathAttempt {
    param([int]$AttemptNumber)
    $attemptDir = Join-Path $stateDir "attempt-$AttemptNumber"
    $activeFiles = @(
        'tail', 'tail.stderr-tmp', 'stall-warning', 'stream-death',
        'stream-retry-request', 'stream-reap-complete', 'worker-roots',
        'reap-targets', 'reap-reason', 'reap-worker.cmd'
    )
    try {
        New-Item -ItemType Directory -Path $attemptDir -ErrorAction Stop | Out-Null
        foreach ($name in @('pre-mtime', 'timestamp', 'stream-retry-limit', 'stream-retry-count')) {
            Copy-Item -LiteralPath (Join-Path $stateDir $name) `
                -Destination (Join-Path $attemptDir $name) -ErrorAction Stop
        }
        foreach ($name in $activeFiles) {
            $sourcePath = Join-Path $stateDir $name
            if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
                Copy-Item -LiteralPath $sourcePath `
                    -Destination (Join-Path $attemptDir $name) -ErrorAction Stop
            }
        }
        foreach ($name in @('tail', 'stream-death', 'stream-retry-request', 'stream-reap-complete')) {
            if (-not (Test-Path -LiteralPath (Join-Path $attemptDir $name) -PathType Leaf)) {
                return $false
            }
        }
        foreach ($name in $activeFiles) {
            Remove-Item -LiteralPath (Join-Path $stateDir $name) `
                -Force -ErrorAction SilentlyContinue
        }
        return $true
    } catch {
        return $false
    }
}

while ($true) {
    & $cmdHelper
    $codexExit = $LASTEXITCODE

    $streamDeathPath = Join-Path $stateDir 'stream-death'
    $streamRetryRequestPath = Join-Path $stateDir 'stream-retry-request'
    $streamReapCompletePath = Join-Path $stateDir 'stream-reap-complete'
    if (-not (Test-Path -LiteralPath $streamDeathPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $streamRetryRequestPath -PathType Leaf) -or
        $streamRetryCount -ge $streamRetryLimit) {
        break
    }

    $handoffDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath $streamReapCompletePath -PathType Leaf) -and
        [DateTime]::UtcNow -lt $handoffDeadline) {
        Start-Sleep -Milliseconds 200
    }
    if (-not (Test-Path -LiteralPath $streamReapCompletePath -PathType Leaf)) {
        [Console]::Error.WriteLine(
            "AUTO-REDISPATCH-SKIPPED reason=stream-reap-handoff-timeout state-dir=$stateDir"
        )
        break
    }
    if ($stallProc -and -not $stallProc.WaitForExit(5000)) {
        [Console]::Error.WriteLine(
            "AUTO-REDISPATCH-SKIPPED reason=stall-watch-did-not-exit state-dir=$stateDir"
        )
        break
    }
    if (-not (Save-StreamDeathAttempt ($streamRetryCount + 1))) {
        [Console]::Error.WriteLine(
            "AUTO-REDISPATCH-SKIPPED reason=attempt-archive-failed state-dir=$stateDir"
        )
        break
    }

    $streamRetryCount += 1
    [System.IO.File]::WriteAllText($streamRetryCountPath, "$streamRetryCount`n")
    $retryStart = [int]([DateTimeOffset]::UtcNow).ToUnixTimeSeconds()
    [System.IO.File]::WriteAllText((Join-Path $stateDir 'timestamp'), "$retryStart`n")
    [Console]::Error.WriteLine(
        "AUTO-REDISPATCH attempt=$($streamRetryCount + 1)/$($streamRetryLimit + 1) " +
        "reason=codex-response-stream-disconnected state-dir=$stateDir"
    )
    Start-StallWatch
}

Remove-Item -LiteralPath $cmdHelper -Force -ErrorAction SilentlyContinue

# Ensure the tail file exists even if codex emitted nothing -- Phase 2
# Health check 8 distinguishes "tail empty" from "tail missing".
if (-not (Test-Path -LiteralPath $tailPath -PathType Leaf)) {
    Set-Content -LiteralPath $tailPath -Value '' -NoNewline -ErrorAction SilentlyContinue
}

# Pipe last 80 lines of tail to stderr for caller visibility
if (Test-Path -LiteralPath $tailPath -PathType Leaf) {
    try {
        Get-Content -LiteralPath $tailPath -Tail 80 | ForEach-Object {
            [Console]::Error.WriteLine($_)
        }
    } catch {
        # Best-effort; do not fail dispatch if tail read errors
    }
}

# Effective, not configured: the header codex printed is in the combined tail.
# Append what it says to the record; a mismatch prints MODEL-MISMATCH on
# stderr (from the resolver), and an unreadable run prints MODEL-UNVERIFIED.
$actualLine = ''
if ($pythonBin -and (Test-Path -LiteralPath $agentModelsPy -PathType Leaf)) {
    $actualArgs = @('actual', '--family', 'codex', '--stderr-file', $tailPath) + $expectArgs
    $actual = Invoke-AgentModels -AgentArgs $actualArgs
    if ($actual.Out -match '^actual=') { $actualLine = $actual.Out }
    if ($actual.Err) { [Console]::Error.WriteLine($actual.Err) }
}
if (-not $actualLine) {
    $actualLine = 'actual=unknown status=unknown'
    [Console]::Error.WriteLine('dispatch-codex: MODEL-UNVERIFIED the run could not be read back (no Python interpreter or resolver)')
}
[System.IO.File]::AppendAllText($reviewModelPath, "$actualLine`n")

# Do NOT force-kill stall-watch. It already polls our PID via Get-Process and
# will exit silently on its next interval after we die. Stopping it on the hot
# path can erase a stall period that crossed the threshold during the final
# poll window, leaving Phase 2.0 Health check 9 with no record.
# The cost is one extra polling interval of lingering observer; the benefit is
# preserving the Check 9 signal that justified stall-watch's existence.

exit $codexExit
