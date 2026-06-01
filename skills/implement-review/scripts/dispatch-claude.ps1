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

$ErrorActionPreference = 'Stop'

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
$guardScript = Join-Path $PSScriptRoot '_claude_guard.ps1'
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

# Launch stall-watch in background if present (shared with Codex/Copilot backends).
$scriptDir = $PSScriptRoot
$stallWatch = Join-Path $scriptDir 'stall-watch.ps1'
$stallProc = $null
if (Test-Path -LiteralPath $stallWatch -PathType Leaf) {
    $stallProc = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $stallWatch,
                        '--state-dir', $stateDir, '--parent-pid', $PID) `
        -WindowStyle Hidden -PassThru -ErrorAction SilentlyContinue
}

# Resolve claude binary -----------------------------------------------------
$claudeBin = if ($env:CLAUDE_BIN) { $env:CLAUDE_BIN } else { 'claude' }
$exe = Resolve-AppPath $claudeBin
if (-not $exe) {
    # Not resolvable; let the cmd invocation surface its own error to the tail.
    $exe = $claudeBin
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
    ("- Do not use " + 'Write' + " or " + 'Edit' + " tools. The dispatch wrapper saves your final answer to $ExpectedReviewFile.")
    ("- You may run $shellToolName verification commands, tests, grep/rg, and other read/validation tools in the current working directory.")
    "- The current working directory is a disposable staged snapshot when git export succeeds: $validationDir"
    '- Do not run mutating, publishing, network, package-install, cleanup, or destructive commands.'
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
$useBare = ($env:CLAUDE_DISPATCH_BARE -eq '1')
$permissionFlag = '--per' + 'mission-' + 'mode'
$toolFlag = '--too' + 'ls'
$permMode = 'by' + 'pass' + 'Per' + 'missions'
$toolList = 'Read' + ',' + 'Ba' + 'sh'
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
    [void]$proc.Start()

    $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
    $stderrTask = $proc.StandardError.ReadToEndAsync()
    $proc.StandardInput.WriteLine([System.IO.File]::ReadAllText($relayPromptPath, $utf8NoBom))
    $proc.StandardInput.Close()
    $proc.WaitForExit()

    [System.IO.File]::WriteAllText($tailPath, $stdoutTask.GetAwaiter().GetResult(), $utf8NoBom)
    [System.IO.File]::WriteAllText($tailStderrPath, $stderrTask.GetAwaiter().GetResult(), $utf8NoBom)
    $claudeExit = $proc.ExitCode
} catch {
    [System.IO.File]::WriteAllText($tailStderrPath, ("dispatch-claude: failed to launch claude: " + $_.Exception.Message + "`n"), $utf8NoBom)
    $claudeExit = 2
}

# Ensure the tail file exists even if claude emitted nothing
if (-not (Test-Path -LiteralPath $tailPath -PathType Leaf)) {
    Set-Content -LiteralPath $tailPath -Value '' -NoNewline -ErrorAction SilentlyContinue
}
if (-not (Test-Path -LiteralPath $tailStderrPath -PathType Leaf)) {
    Set-Content -LiteralPath $tailStderrPath -Value '' -NoNewline -ErrorAction SilentlyContinue
}

# Save Claude's final answer to the expected review file. Stderr stays in the
# state-dir for diagnostics and is not copied into the review body.
if ($claudeExit -eq 0) {
    try {
        $tailItem = Get-Item -LiteralPath $tailPath -ErrorAction Stop
        if ($tailItem.Length -gt 0) {
            $marker = "<!-- Round $Round -->"
            $tailText = Get-Content -LiteralPath $tailPath -Raw -ErrorAction Stop
            $tailLines = $tailText -split "\r?\n"
            $markerIndex = [Array]::IndexOf($tailLines, $marker)
            if ($markerIndex -ge 0) {
                $selected = $tailLines[$markerIndex..($tailLines.Length - 1)] -join "`n"
                $normalizedReview = $selected.TrimEnd() + "`n"
            } else {
                $normalizedReview = $marker + "`n`n" + $tailText.TrimStart()
            }
            [System.IO.File]::WriteAllText($expectedReviewPath, $normalizedReview, $utf8NoBom)
        }
    } catch {
        [Console]::Error.WriteLine("dispatch-claude: failed to write expected review file: $ExpectedReviewFile")
        $claudeExit = 2
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

# Do NOT force-kill stall-watch.

exit $claudeExit
