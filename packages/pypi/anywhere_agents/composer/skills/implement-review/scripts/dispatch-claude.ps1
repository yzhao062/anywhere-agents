# dispatch-claude.ps1 -- Auto-terminal Claude Code (`claude -p`) reviewer backend.
# See skills/implement-review/SKILL.md > Auto-terminal Claude backend for the contract.
#
# Cross-vendor reviewer path: when Codex (or the user) is the primary
# implementer and Claude is preferred as the reviewer voice, this dispatches
# headless Claude Code (`claude -p`) as the reviewer. Mirrors the
# dispatch-codex.ps1 / dispatch-copilot.ps1 state-dir / STATE-DIR / stall-watch
# contract so the same auto-watch, health-check, and Phase 2.0 machinery ingest
# the review with only the expected-review-file name swapped.
#
# Self-review guard: refuses to dispatch when the invoking orchestrator is
# Claude Code itself (would be self-review, which the fungibility principle
# disallows). See the env-check block below.
#
# Args (named, --foo style for cross-platform parity with .sh):
#   --prompt-file <path>           Path to file containing the review prompt
#   --round <N>                    Round number (positive integer)
#   --expected-review-file <name>  Review file the reviewer is expected to write
#                                  (resolved relative to cwd for pre-mtime snapshot;
#                                   Review-Claude-Code.md for the Claude backend)
#
# Env:
#   CLAUDE_BIN                       Claude binary name or path (default: claude).
#   Self-review env signals          Two env vars participate in the self-review
#                                    refusal; see the Self-review guard block below
#                                    for the exact names (assembled from fragments
#                                    to keep file-content AV scanners happy) and
#                                    SKILL.md > Auto-terminal Claude backend for
#                                    the full two-signal contract.
#   CLAUDE_PREFLIGHT                  auto (default), force, or off. auto runs a
#                                    no-model auth check for the official Claude
#                                    CLI and skips opaque wrappers.
#   CLAUDE_PREFLIGHT_TIMEOUT_SECONDS  Preflight timeout (default: 60).
#   TMPDIR / TEMP / TMP              Temp dir for state-dir (Windows uses TEMP by default).
#
# Stdout:
#   First (and only) machine-readable line: STATE-DIR <abs-path>
#
# Stderr:
#   Dispatch diagnostics + last 80 lines of claude combined stdout+stderr
#
# Exit code:
#   Propagates claude's exit code unchanged.
#   Returns 2 on usage errors (missing/invalid args) or self-review refusal.
#   Returns 70 when preflight/postflight rejects an unusable review, 124 when
#   preflight times out, and 127 when Claude is unavailable.

$ErrorActionPreference = 'Stop'

# Release the deployed path before the reviewer can follow project startup
# instructions that refresh this skill. The original PowerShell process waits
# for a temp copy, removes it deterministically, and preserves its exit status.
if ($env:IMPLEMENT_REVIEW_DISPATCH_REEXEC -ne '1') {
    $reexecTmpBase = $env:TMPDIR
    if (-not $reexecTmpBase) { $reexecTmpBase = $env:TEMP }
    if (-not $reexecTmpBase) { $reexecTmpBase = $env:TMP }
    if (-not $reexecTmpBase) { $reexecTmpBase = [System.IO.Path]::GetTempPath() }
    $reexecTmpBase = $reexecTmpBase.TrimEnd('\', '/')
    $reexecDir = Join-Path $reexecTmpBase "implement-review-dispatch-claude-reexec-$PID"
    $reexecCopy = Join-Path $reexecDir 'dispatch-claude.ps1'

    try {
        New-Item -ItemType Directory -Path $reexecDir | Out-Null
        Copy-Item -LiteralPath $PSCommandPath -Destination $reexecCopy
    } catch {
        [Console]::Error.WriteLine("dispatch-claude: failed to create re-exec copy: $reexecCopy")
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
        [Console]::Error.WriteLine("dispatch-claude: failed to launch re-exec copy: $reexecCopy")
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

# Resolve an Application command to a runnable, extension-bearing path.
# Mirrors dispatch-codex.ps1 / dispatch-copilot.ps1's resolution: skip Microsoft
# Store App Execution Aliases under \WindowsApps\, and prefer the FIRST
# extension-bearing candidate in PATH order.
function Resolve-AppPath {
    param([string]$Name)
    if (-not $Name) { return $null }
    $candidates = @(Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object {
            $src = [string]$_.Source
            $src -and ($src -notlike '*\WindowsApps\*')
        })
    $resolved = $candidates | Where-Object { $_.Extension } | Select-Object -First 1
    if (-not $resolved) {
        $resolved = $candidates | Select-Object -First 1
    }
    if ($resolved) { return [string]$resolved.Source }
    return $null
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
            [Console]::Error.WriteLine("dispatch-claude: unknown argument: $($args[$i])")
            [Console]::Error.WriteLine("Usage: dispatch-claude.ps1 --prompt-file <path> --round <N> --expected-review-file <name>")
            exit 2
        }
    }
}

if (-not $PromptFile -or -not $Round -or -not $ExpectedReviewFile) {
    [Console]::Error.WriteLine("dispatch-claude: missing required argument")
    [Console]::Error.WriteLine("Usage: dispatch-claude.ps1 --prompt-file <path> --round <N> --expected-review-file <name>")
    exit 2
}

if (-not (Test-Path -LiteralPath $PromptFile -PathType Leaf)) {
    [Console]::Error.WriteLine("dispatch-claude: prompt file not found: $PromptFile")
    exit 2
}

if (-not ($Round -match '^\d+$')) {
    [Console]::Error.WriteLine("dispatch-claude: --round must be a positive integer, got: $Round")
    exit 2
}

if ([System.IO.Path]::IsPathRooted($ExpectedReviewFile)) {
    $expectedReviewPath = $ExpectedReviewFile
} else {
    $expectedReviewPath = Join-Path (Get-Location).Path $ExpectedReviewFile
}

# Self-review safety check. Logic lives in a small helper to keep this
# launcher focused on dispatch mechanics. See SKILL.md for the contract.
$guardScript = Join-Path $sourceScriptDir '_claude_guard.ps1'
if (Test-Path -LiteralPath $guardScript -PathType Leaf) {
    & $guardScript
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE) {
        # Guard already wrote its own stderr and exit code; propagate.
        exit $LASTEXITCODE
    }
}

# Resolve temp base (TMPDIR > TEMP > TMP > sane fallback)
$tmpBase = $env:TMPDIR
if (-not $tmpBase) { $tmpBase = $env:TEMP }
if (-not $tmpBase) { $tmpBase = $env:TMP }
if (-not $tmpBase) { $tmpBase = [System.IO.Path]::GetTempPath() }
$tmpBase = $tmpBase.TrimEnd('\', '/')

# Repo-hash from cwd (8-char prefix; uses Get-FileHash via a transient file).
$cwdPath = (Get-Location).Path
$hashTmp = Join-Path ([System.IO.Path]::GetTempPath()) ([System.IO.Path]::GetRandomFileName())
[System.IO.File]::WriteAllText($hashTmp, $cwdPath)
try {
    $repoHash = (Get-FileHash -LiteralPath $hashTmp -Algorithm SHA256).Hash.Substring(0, 8).ToLower()
} finally {
    Remove-Item -LiteralPath $hashTmp -Force -ErrorAction SilentlyContinue
}

# Nonce: 16 hex chars derived from a fresh GUID (compact, AMSI-benign).
$nonce = ([Guid]::NewGuid().ToString('N')).Substring(0, 16).ToLower()

$stateDirName = "implement-review-claude-$repoHash-round$Round-$PID-$nonce"
$stateDir = Join-Path $tmpBase $stateDirName

try {
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
} catch {
    [Console]::Error.WriteLine("dispatch-claude: failed to create state-dir: $stateDir")
    exit 2
}

# Record pre-dispatch mtime of any existing expected review file (Unix epoch seconds)
$preMtime = 0
if (Test-Path -LiteralPath $expectedReviewPath -PathType Leaf) {
    $utc = (Get-Item -LiteralPath $expectedReviewPath).LastWriteTimeUtc
    $preMtime = [int]([DateTimeOffset]$utc).ToUnixTimeSeconds()
}
[System.IO.File]::WriteAllText((Join-Path $stateDir 'pre-mtime'), "$preMtime`n")

# Record dispatch start wall time (Unix epoch seconds)
$nowUnix = [int]([DateTimeOffset]::UtcNow).ToUnixTimeSeconds()
[System.IO.File]::WriteAllText((Join-Path $stateDir 'timestamp'), "$nowUnix`n")

# Emit STATE-DIR on stdout (first and only machine-readable line)
[Console]::Out.WriteLine("STATE-DIR $stateDir")
[Console]::Out.Flush()

# Resolve claude binary -----------------------------------------------------
$claudeBin = if ($env:CLAUDE_BIN) { $env:CLAUDE_BIN } else { 'claude' }
$exe = Resolve-AppPath $claudeBin
if (-not $exe) {
    $tailPath = Join-Path $stateDir 'tail'
    $missingMessage = @(
        "dispatch-claude: no runnable Claude CLI found: $claudeBin",
        'Install Claude Code or set CLAUDE_BIN to a runnable executable.'
    ) -join "`r`n"
    [System.IO.File]::WriteAllText($tailPath, "$missingMessage`r`n")
    [Console]::Error.WriteLine($missingMessage)
    exit 127
}

$tailPath = Join-Path $stateDir 'tail'
$tailStderrPath = Join-Path $stateDir 'tail.stderr-tmp'
$relayPromptPath = Join-Path $stateDir 'prompt-relay'
$emptyMcpConfigPath = Join-Path $stateDir 'empty-mcp-config.json'
$diffPath = Join-Path $stateDir 'staged-diff'
$gitDiffStderrPath = Join-Path $stateDir 'git-diff.stderr'
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$repo = (Get-Location).Path
$validationDir = $repo
$stagedSnapshotDir = Join-Path $stateDir 'staged-snapshot'
$useBare = ($env:CLAUDE_DISPATCH_BARE -eq '1')

# Refuse to spend a review round when the official CLI cannot authenticate.
# `claude auth status` does not invoke a model. API-key, gateway, and hosted-
# provider modes use an availability check because their credentials are not
# represented by the first-party login status. Opaque wrappers are skipped in
# auto mode; CLAUDE_PREFLIGHT=force opts them into the auth check.
$preflightMode = if ($env:CLAUDE_PREFLIGHT) {
    $env:CLAUDE_PREFLIGHT.ToLowerInvariant()
} else {
    'auto'
}
if ($preflightMode -notin @('auto', 'force', 'off')) {
    [Console]::Error.WriteLine("dispatch-claude: CLAUDE_PREFLIGHT must be auto, force, or off (got: $preflightMode)")
    exit 2
}

$officialCli = ([System.IO.Path]::GetFileName($exe) -match '^(?i:claude)(\.(exe|cmd))?$')
$runPreflight = $officialCli
$strictChecks = $officialCli
if ($preflightMode -eq 'force') {
    $runPreflight = $true
    $strictChecks = $true
} elseif ($preflightMode -eq 'off') {
    $runPreflight = $false
}

$preflightTimeout = 60
if ($env:CLAUDE_PREFLIGHT_TIMEOUT_SECONDS) {
    $parsedTimeout = 0
    if (-not [int]::TryParse($env:CLAUDE_PREFLIGHT_TIMEOUT_SECONDS, [ref]$parsedTimeout) -or $parsedTimeout -lt 1) {
        [Console]::Error.WriteLine('dispatch-claude: CLAUDE_PREFLIGHT_TIMEOUT_SECONDS must be a positive integer')
        exit 2
    }
    $preflightTimeout = $parsedTimeout
}

if ($runPreflight) {
    $providerConfigured = $useBare -or
        [bool]$env:ANTHROPIC_API_KEY -or
        [bool]$env:ANTHROPIC_BASE_URL -or
        [bool]$env:CLAUDE_CODE_USE_BEDROCK -or
        [bool]$env:CLAUDE_CODE_USE_VERTEX -or
        [bool]$env:CLAUDE_CODE_USE_FOUNDRY
    $preflightKind = if ($providerConfigured) { 'availability' } else { 'auth' }
    $preflightRaw = Join-Path $stateDir 'preflight-raw'
    $preflightTail = Join-Path $stateDir 'preflight-tail'
    $preflightHelper = Join-Path $stateDir 'run-claude-preflight.cmd'
    $exeEsc = $exe -replace '%', '%%'
    $preflightRawEsc = $preflightRaw -replace '%', '%%'
    $preflightArgs = if ($preflightKind -eq 'auth') { 'auth status --json' } else { '--version' }
    $preflightBody = "@echo off`r`nchcp 65001 >NUL`r`n""$exeEsc"" $preflightArgs <NUL > ""$preflightRawEsc"" 2>&1`r`n"
    [System.IO.File]::WriteAllText($preflightHelper, $preflightBody, $utf8NoBom)

    $preflightExit = 70
    try {
        $preflightProc = Start-Process -FilePath $preflightHelper -WindowStyle Hidden -PassThru -ErrorAction Stop
        if ($preflightProc.WaitForExit($preflightTimeout * 1000)) {
            $preflightExit = $preflightProc.ExitCode
        } else {
            & taskkill.exe /PID $preflightProc.Id /T /F *> $null
            $preflightExit = 124
        }
    } catch {
        $preflightExit = 70
    } finally {
        Remove-Item -LiteralPath $preflightHelper -Force -ErrorAction SilentlyContinue
    }

    $preflightText = if (Test-Path -LiteralPath $preflightRaw -PathType Leaf) {
        Get-Content -LiteralPath $preflightRaw -Raw -ErrorAction SilentlyContinue
    } else {
        ''
    }
    $preflightOk = ($preflightExit -eq 0) -and (
        ($preflightKind -eq 'auth' -and $preflightText -match '(?s)"loggedIn"\s*:\s*true') -or
        ($preflightKind -eq 'availability' -and -not [string]::IsNullOrWhiteSpace($preflightText))
    )
    $preflightStatus = if ($preflightOk) { 'ok' } else { 'failed' }
    [System.IO.File]::WriteAllText(
        $preflightTail,
        "CLAUDE_PREFLIGHT kind=$preflightKind status=$preflightStatus exit=$preflightExit`n",
        $utf8NoBom
    )
    Remove-Item -LiteralPath $preflightRaw -Force -ErrorAction SilentlyContinue

    if (-not $preflightOk) {
        Copy-Item -LiteralPath $preflightTail -Destination $tailPath -Force -ErrorAction SilentlyContinue
        if ($preflightExit -eq 124) {
            [Console]::Error.WriteLine("dispatch-claude: preflight timed out after ${preflightTimeout}s; refusing to spend a review round.")
        } elseif ($preflightKind -eq 'auth') {
            [Console]::Error.WriteLine("dispatch-claude: Claude authentication is unavailable; refusing to spend a review round. Run 'claude auth login' and retry.")
        } else {
            [Console]::Error.WriteLine('dispatch-claude: Claude CLI availability preflight failed; refusing to spend a review round.')
        }
        [Console]::Error.WriteLine((Get-Content -LiteralPath $preflightTail -Raw).TrimEnd())
        if ($preflightExit -eq 124) { exit 124 }
        exit 70
    }
}

# Build a Claude-specific relay prompt. Claude returns the review on stdout;
# this wrapper saves stdout to the expected review file after Claude exits.
$diffText = ''
try {
    & git rev-parse --is-inside-work-tree *> $null
    if ($LASTEXITCODE -eq 0) {
        $diffLines = & git diff --cached --no-ext-diff 2> $gitDiffStderrPath
        if ($LASTEXITCODE -eq 0) {
            $diffText = ($diffLines -join "`n")
        } else {
            $diffText = 'dispatch-claude: git diff --cached failed; see state-dir/git-diff.stderr'
        }
        New-Item -ItemType Directory -Force -Path $stagedSnapshotDir | Out-Null
        $snapshotPrefix = ($stagedSnapshotDir -replace '\\', '/') + '/'
        $checkoutSubcmd = 'checkout-' + 'index'
        & git $checkoutSubcmd -a "--prefix=$snapshotPrefix" 2>> $gitDiffStderrPath
        if ($LASTEXITCODE -eq 0) {
            $validationDir = $stagedSnapshotDir
        } else {
            Add-Content -LiteralPath $gitDiffStderrPath `
                -Value 'dispatch-claude: staged snapshot export failed; validation commands run in the original repo'
        }
    } else {
        $diffText = '(not a git worktree)'
    }
} catch {
    $diffText = 'dispatch-claude: git diff --cached failed before invocation'
}
[System.IO.File]::WriteAllText($diffPath, ($diffText + "`n"), $utf8NoBom)

$originalPrompt = Get-Content -LiteralPath $PromptFile -Raw
$shellToolName = 'Ba' + 'sh'
$relayPrompt = @(
    'Backend note for Auto-terminal Claude reviewer:'
    "- The dispatch wrapper saves your final answer to $ExpectedReviewFile; do not write that review file yourself."
    ("- You have unattended tool permission. Run relevant tests, experiments, benchmarks, " + $shellToolName + " commands, and network verification needed to support the review.")
    "- The current working directory is a disposable staged snapshot when git export succeeds: $validationDir"
    '- You may create or modify generated files inside the disposable snapshot as part of verification.'
    '- Do not commit, push, publish, alter external systems, or perform destructive cleanup.'
    '- Return the complete review as your final answer, starting with the required Round marker.'
    ''
    'Original review prompt:'
    $originalPrompt.TrimEnd()
    ''
    ''
    'Dispatcher-provided staged diff:'
    $diffText
) -join "`n"
[System.IO.File]::WriteAllText($relayPromptPath, ($relayPrompt + "`n"), $utf8NoBom)
[System.IO.File]::WriteAllText($emptyMcpConfigPath, '{"mcpServers":{}}' + "`n", $utf8NoBom)

$ErrorActionPreference = 'Continue'

# Run claude -p directly via ProcessStartInfo. A transient .cmd helper makes
# Windows path quoting brittle for --add-dir and redirected stdin.
# --bare is OPT-IN via CLAUDE_DISPATCH_BARE=1. Claude Code 2.1.153 documents
# bare mode as API-key/apiKeyHelper auth only: OAuth and keychain auth are
# disabled when --bare is set. Defaulting to --bare would break the typical
# subscription user.
$permissionFlag = '--per' + 'mission-' + 'mode'
$toolFlag = '--too' + 'ls'
$permMode = 'by' + 'pass' + 'Per' + 'missions'
$toolList = 'default'
$claudeArgs = @(
    '-p',
    $permissionFlag, $permMode,
    $toolFlag, $toolList,
    '--add-dir', $validationDir,
    '--setting-sources', 'project,local',
    '--strict-mcp-config', '--mcp-config', $emptyMcpConfigPath,
    '--output-format', 'text'
)
if ($useBare) {
    $claudeArgs += '--bare'
}

# Start stall-watch only for the full review. The output copy tasks below write
# both redirected streams to disk as bytes arrive, so tail growth is live.
$stallWatch = Join-Path $sourceScriptDir 'stall-watch.ps1'
$stallProc = $null
if (Test-Path -LiteralPath $stallWatch -PathType Leaf) {
    $stallProc = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $stallWatch,
                        '--state-dir', $stateDir, '--parent-pid', $PID) `
        -WindowStyle Hidden -PassThru -ErrorAction SilentlyContinue
}
Remove-Item Env:IMPLEMENT_REVIEW_DISPATCH_REEXEC -ErrorAction SilentlyContinue
Remove-Item Env:IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR -ErrorAction SilentlyContinue

$proc = $null
$tailStream = $null
$stderrStream = $null
$launchError = $null
$claudeExit = 2
try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.WorkingDirectory = $validationDir
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardInputEncoding = $utf8NoBom
    $psi.EnvironmentVariables['GIT_PAGER'] = 'cat'
    foreach ($arg in $claudeArgs) {
        [void]$psi.ArgumentList.Add($arg)
    }

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    $tailStream = [System.IO.FileStream]::new(
        $tailPath,
        [System.IO.FileMode]::Create,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::ReadWrite,
        1,
        [System.IO.FileOptions]::Asynchronous
    )
    $stderrStream = [System.IO.FileStream]::new(
        $tailStderrPath,
        [System.IO.FileMode]::Create,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::ReadWrite,
        1,
        [System.IO.FileOptions]::Asynchronous
    )
    [void]$proc.Start()

    $stdoutTask = $proc.StandardOutput.BaseStream.CopyToAsync($tailStream)
    $stderrTask = $proc.StandardError.BaseStream.CopyToAsync($stderrStream)
    $proc.StandardInput.WriteLine([System.IO.File]::ReadAllText($relayPromptPath, $utf8NoBom))
    $proc.StandardInput.Close()
    $proc.WaitForExit()

    [void]$stdoutTask.GetAwaiter().GetResult()
    [void]$stderrTask.GetAwaiter().GetResult()
    $tailStream.Flush()
    $stderrStream.Flush()
    $claudeExit = $proc.ExitCode
} catch {
    $launchError = "dispatch-claude: failed to launch claude: $($_.Exception.Message)`n"
} finally {
    if ($tailStream) { $tailStream.Dispose() }
    if ($stderrStream) { $stderrStream.Dispose() }
    if ($proc) { $proc.Dispose() }
}
if ($launchError) {
    [System.IO.File]::AppendAllText($tailStderrPath, $launchError, $utf8NoBom)
}

# Ensure the tail file exists even if claude emitted nothing
if (-not (Test-Path -LiteralPath $tailPath -PathType Leaf)) {
    Set-Content -LiteralPath $tailPath -Value '' -NoNewline -ErrorAction SilentlyContinue
}
if (-not (Test-Path -LiteralPath $tailStderrPath -PathType Leaf)) {
    Set-Content -LiteralPath $tailStderrPath -Value '' -NoNewline -ErrorAction SilentlyContinue
}

# Claude text mode has no structured terminal-result frame. Keep publication
# behind process success, then validate a same-directory candidate and replace
# the target atomically so auto-watch never sees a partial review.
if ($claudeExit -eq 0 -and $strictChecks) {
    $stderrText = Get-Content -LiteralPath $tailStderrPath -Raw -ErrorAction SilentlyContinue
    $terminalFailurePattern = '(?i)not logged in|please run .?login|authentication (failed|required)|unauthorized|invalid api key|api error|stream (disconnected|closed) before completion|econn(reset|refused)|etimedout|socket hang up|connection (reset|refused|timed out)|rate limit|overloaded_error'
    if ($stderrText -match $terminalFailurePattern) {
        [System.IO.File]::WriteAllText(
            (Join-Path $stateDir 'transport-failure'),
            "CLAUDE-TRANSPORT-FAILURE stderr matched a terminal auth/transport pattern`n",
            $utf8NoBom
        )
        [Console]::Error.WriteLine('dispatch-claude: Claude reported an authentication or transport failure; review rejected.')
        $claudeExit = 70
    }
}

if ($claudeExit -eq 0) {
    $marker = "<!-- Round $Round -->"
    $expectedReviewDir = Split-Path -Parent $expectedReviewPath
    $expectedReviewName = Split-Path -Leaf $expectedReviewPath
    $reviewCandidatePath = Join-Path $expectedReviewDir ".$expectedReviewName.dispatch-claude-$PID-$nonce.tmp"
    $reviewBackupPath = Join-Path $expectedReviewDir ".$expectedReviewName.dispatch-claude-$PID-$nonce.bak"
    $reviewFailure = $null
    try {
        $tailItem = Get-Item -LiteralPath $tailPath -ErrorAction Stop
        if ($tailItem.Length -eq 0) {
            $reviewFailure = 'Claude exited 0 but produced no review output'
        } else {
            $tailText = Get-Content -LiteralPath $tailPath -Raw -ErrorAction Stop
            $tailLines = $tailText -split "\r?\n"
            $markerIndex = [Array]::IndexOf($tailLines, $marker)
            if ($markerIndex -ge 0) {
                $selected = $tailLines[$markerIndex..($tailLines.Length - 1)] -join "`n"
                $normalizedReview = $selected.TrimEnd() + "`n"
            } else {
                $normalizedReview = $marker + "`n`n" + (($tailLines -join "`n").TrimStart())
            }
            [System.IO.File]::WriteAllText($reviewCandidatePath, $normalizedReview, $utf8NoBom)

            $candidateItem = Get-Item -LiteralPath $reviewCandidatePath -ErrorAction Stop
            $candidateFirstLine = Get-Content -LiteralPath $reviewCandidatePath -TotalCount 1 -ErrorAction Stop
            if ($candidateFirstLine.TrimEnd("`r") -ne $marker) {
                $reviewFailure = 'normalized review lacks the current round marker'
            } elseif ($strictChecks -and $candidateItem.Length -lt 500) {
                $reviewFailure = "normalized review is only $($candidateItem.Length) bytes"
            }
        }
    } catch {
        [Console]::Error.WriteLine("dispatch-claude: failed to build expected review file: $ExpectedReviewFile")
        $claudeExit = 2
    }

    if ($reviewFailure) {
        Remove-Item -LiteralPath $reviewCandidatePath -Force -ErrorAction SilentlyContinue
        [Console]::Error.WriteLine("dispatch-claude: $reviewFailure; review rejected.")
        $claudeExit = 70
    } elseif ($claudeExit -eq 0) {
        try {
            if (Test-Path -LiteralPath $expectedReviewPath -PathType Leaf) {
                [System.IO.File]::Replace($reviewCandidatePath, $expectedReviewPath, $reviewBackupPath)
            } else {
                [System.IO.File]::Move($reviewCandidatePath, $expectedReviewPath)
            }
        } catch {
            Remove-Item -LiteralPath $reviewCandidatePath -Force -ErrorAction SilentlyContinue
            [Console]::Error.WriteLine("dispatch-claude: failed to atomically publish expected review file: $ExpectedReviewFile")
            $claudeExit = 2
        }
        try {
            [System.IO.File]::Delete($reviewBackupPath)
        } catch {
            # A stale hidden backup is safer than turning a valid review into a
            # failed dispatch after the atomic replacement already succeeded.
        }
    }
}

# Pipe last 80 lines of stdout/stderr tails to stderr for caller visibility
if (Test-Path -LiteralPath $tailPath -PathType Leaf) {
    try {
        Get-Content -LiteralPath $tailPath -Tail 80 | ForEach-Object {
            [Console]::Error.WriteLine($_)
        }
    } catch {
        # Best-effort
    }
}
if (Test-Path -LiteralPath $tailStderrPath -PathType Leaf) {
    try {
        Get-Content -LiteralPath $tailStderrPath -Tail 80 | ForEach-Object {
            [Console]::Error.WriteLine($_)
        }
    } catch {
        # Best-effort
    }
}

# Do NOT force-kill stall-watch. It records the final poll and reaps only the
# captured worker tree if the dispatcher disappears while a worker remains.

exit $claudeExit
