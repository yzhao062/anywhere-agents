# dispatch-copilot.ps1 -- Auto-terminal Copilot-backend dispatch for implement-review skill.
# See skills/implement-review/SKILL.md > Auto-terminal Copilot backend for the contract.
#
# Cross-vendor reviewer path: when Claude Code is unavailable and Codex (or the
# user) is the primary implementer, this dispatches GitHub Copilot CLI as the
# reviewer. Mirrors the dispatch-codex.ps1 state-dir / STATE-DIR / stall-watch
# contract so the same auto-watch, health-check, and Phase 2.0 machinery ingest
# the review with only the expected-review-file name swapped.
#
# Args (named, --foo style for cross-platform parity with .sh):
#   --prompt-file <path>           Path to file containing the review prompt
#   --round <N>                    Round number (positive integer)
#   --expected-review-file <name>  Review file the reviewer is expected to write
#                                  (resolved relative to cwd for pre-mtime snapshot;
#                                   Review-GitHub-Copilot.md for the Copilot backend)
#
# Env:
#   COPILOT_BIN                    Copilot binary name or path (default: copilot)
#   GH_BIN                         gh binary name or path for the fallback path
#                                  (default: gh; used only when copilot is absent)
#   COPILOT_PREFLIGHT              auto (default), force, or off. auto runs a
#                                  live auth + git-shell probe for official
#                                  copilot / gh executables and skips wrappers.
#   COPILOT_PREFLIGHT_TIMEOUT_SECONDS
#                                  Live preflight timeout (default: 60)
#   TMPDIR / TEMP / TMP            Temp dir for state-dir (Windows uses TEMP by default)
#
# Stdout:
#   First (and only) machine-readable line: STATE-DIR <abs-path>
#
# Stderr:
#   Dispatch diagnostics + last 80 lines of copilot combined stdout+stderr
#
# Exit code:
#   Propagates copilot's exit code unchanged.
#   Returns 2 on usage errors (missing/invalid args).
#   Returns 70 when preflight/postflight rejects an unusable review, 124 when
#   preflight times out, and 127 when no Copilot executable is available.

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
    $reexecDir = Join-Path $reexecTmpBase "implement-review-dispatch-copilot-reexec-$PID"
    $reexecCopy = Join-Path $reexecDir 'dispatch-copilot.ps1'

    try {
        New-Item -ItemType Directory -Path $reexecDir | Out-Null
        Copy-Item -LiteralPath $PSCommandPath -Destination $reexecCopy
    } catch {
        [Console]::Error.WriteLine("dispatch-copilot: failed to create re-exec copy: $reexecCopy")
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
        [Console]::Error.WriteLine("dispatch-copilot: failed to launch re-exec copy: $reexecCopy")
    } finally {
        Remove-Item -LiteralPath $reexecCopy -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $reexecDir -Force -ErrorAction SilentlyContinue
    }
    exit $reexecExit
}

# Resolve an Application command to a runnable, extension-bearing path.
# Mirrors dispatch-codex.ps1's resolution: skip Microsoft Store App Execution
# Aliases under \WindowsApps\, and prefer the FIRST extension-bearing candidate
# in PATH order (Windows PowerShell 5.1 Sort-Object is not stable under custom
# keys, so a two-pass filter is used instead of Sort-Object).
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
            [Console]::Error.WriteLine("dispatch-copilot: unknown argument: $($args[$i])")
            [Console]::Error.WriteLine("Usage: dispatch-copilot.ps1 --prompt-file <path> --round <N> --expected-review-file <name>")
            exit 2
        }
    }
}

if (-not $PromptFile -or -not $Round -or -not $ExpectedReviewFile) {
    [Console]::Error.WriteLine("dispatch-copilot: missing required argument")
    [Console]::Error.WriteLine("Usage: dispatch-copilot.ps1 --prompt-file <path> --round <N> --expected-review-file <name>")
    exit 2
}

if (-not (Test-Path -LiteralPath $PromptFile -PathType Leaf)) {
    [Console]::Error.WriteLine("dispatch-copilot: prompt file not found: $PromptFile")
    exit 2
}

if (-not ($Round -match '^\d+$')) {
    [Console]::Error.WriteLine("dispatch-copilot: --round must be a positive integer, got: $Round")
    exit 2
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

$stateDirName = "implement-review-copilot-$repoHash-round$Round-$PID-$nonce"
$stateDir = Join-Path $tmpBase $stateDirName

try {
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
} catch {
    [Console]::Error.WriteLine("dispatch-copilot: failed to create state-dir: $stateDir")
    exit 2
}

# Record pre-dispatch mtime of any existing expected review file (Unix epoch seconds)
$preMtime = 0
if (Test-Path -LiteralPath $ExpectedReviewFile -PathType Leaf) {
    $utc = (Get-Item -LiteralPath $ExpectedReviewFile).LastWriteTimeUtc
    $preMtime = [int]([DateTimeOffset]$utc).ToUnixTimeSeconds()
}
[System.IO.File]::WriteAllText((Join-Path $stateDir 'pre-mtime'), "$preMtime`n")

# Record dispatch start wall time (Unix epoch seconds)
$nowUnix = [int]([DateTimeOffset]::UtcNow).ToUnixTimeSeconds()
[System.IO.File]::WriteAllText((Join-Path $stateDir 'timestamp'), "$nowUnix`n")

# Emit STATE-DIR on stdout (first and only machine-readable line)
[Console]::Out.WriteLine("STATE-DIR $stateDir")
[Console]::Out.Flush()

# Resolve copilot binary, with gh-copilot fallback ------------------------
# Standalone `copilot` is preferred; if it is not resolvable, fall back to the
# built-in `gh copilot` entry point (both verified equivalent in the probe).
# An explicit COPILOT_BIN that resolves wins; otherwise GH_BIN / `gh` is tried.
$copilotBin = if ($env:COPILOT_BIN) { $env:COPILOT_BIN } else { 'copilot' }
$useGh = $false
$exe = Resolve-AppPath $copilotBin
if (-not $exe) {
    $ghBin = if ($env:GH_BIN) { $env:GH_BIN } else { 'gh' }
    $ghResolved = Resolve-AppPath $ghBin
    if ($ghResolved) {
        $exe = $ghResolved
        $useGh = $true
    } else {
        $tailPath = Join-Path $stateDir 'tail'
        $missingMessage = @(
            "dispatch-copilot: no runnable Copilot CLI found (tried '$copilotBin' and '$ghBin').",
            'Install GitHub Copilot CLI or set COPILOT_BIN / GH_BIN to a runnable executable.'
        ) -join "`r`n"
        [System.IO.File]::WriteAllText($tailPath, "$missingMessage`r`n")
        [Console]::Error.WriteLine($missingMessage)
        exit 127
    }
}

$tailPath = Join-Path $stateDir 'tail'

$ghPrefix = if ($useGh) { 'copilot -- ' } else { '' }
$preflightMode = if ($env:COPILOT_PREFLIGHT) {
    $env:COPILOT_PREFLIGHT.ToLowerInvariant()
} else {
    'auto'
}
if ($preflightMode -notin @('auto', 'force', 'off')) {
    [Console]::Error.WriteLine("dispatch-copilot: COPILOT_PREFLIGHT must be auto, force, or off (got: $preflightMode)")
    exit 2
}

$officialCli = ([System.IO.Path]::GetFileName($exe) -match '^(?i:copilot|gh)(\.exe)?$')
$runPreflight = $officialCli
$strictChecks = $officialCli
if ($preflightMode -eq 'force') {
    $runPreflight = $true
    $strictChecks = $true
}
if ($preflightMode -eq 'off') { $runPreflight = $false }

$preflightTimeout = 60
if ($env:COPILOT_PREFLIGHT_TIMEOUT_SECONDS) {
    $parsedTimeout = 0
    if (-not [int]::TryParse($env:COPILOT_PREFLIGHT_TIMEOUT_SECONDS, [ref]$parsedTimeout) -or $parsedTimeout -lt 1) {
        [Console]::Error.WriteLine('dispatch-copilot: COPILOT_PREFLIGHT_TIMEOUT_SECONDS must be a positive integer')
        exit 2
    }
    $preflightTimeout = $parsedTimeout
}

# Paths and short prompt are escaped for cmd exactly as the full dispatch is.
$repo = (Get-Location).Path
$exeEsc = $exe -replace '%', '%%'
$repoEsc = $repo -replace '%', '%%'
$stateDirEsc = $stateDir -replace '%', '%%'
$promptFileEsc = $PromptFile -replace '%', '%%'
$tailPathEsc = $tailPath -replace '%', '%%'
$utf8NoBom = New-Object System.Text.UTF8Encoding $false

if ($runPreflight) {
    $preflightTail = Join-Path $stateDir 'preflight-tail'
    $preflightTailEsc = $preflightTail -replace '%', '%%'
    $preflightHelper = Join-Path $stateDir 'run-copilot-preflight.cmd'
    $preflightPrompt = 'Run git --version exactly once. If it exits 0, reply exactly COPILOT_PREFLIGHT_OK. Do not modify files.'
    $preflightBody = "@echo off`r`nchcp 65001 >NUL`r`nset GIT_PAGER=cat`r`n""$exeEsc"" ${ghPrefix}-C ""$stateDirEsc"" -p ""$preflightPrompt"" --add-dir ""$stateDirEsc"" --allow-tool=""shell(git:*)"" --no-ask-user --no-auto-update --no-custom-instructions --disable-builtin-mcps --stream on --output-format json --no-color > ""$preflightTailEsc"" 2>&1`r`n"
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
        [System.IO.File]::WriteAllText($preflightTail, "dispatch-copilot: could not launch preflight: $($_.Exception.Message)`r`n")
    } finally {
        Remove-Item -LiteralPath $preflightHelper -Force -ErrorAction SilentlyContinue
    }

    $preflightText = if (Test-Path -LiteralPath $preflightTail -PathType Leaf) {
        Get-Content -LiteralPath $preflightTail -Raw -ErrorAction SilentlyContinue
    } else {
        ''
    }
    $preflightToolOk = $preflightText -match '"type":"tool\.execution_complete".*"success":true'
    $preflightReplyOk = $preflightText -match '"type":"assistant\.message".*"content":"COPILOT_PREFLIGHT_OK"'

    if ($preflightExit -ne 0 -or -not $preflightToolOk -or -not $preflightReplyOk) {
        if (Test-Path -LiteralPath $preflightTail -PathType Leaf) {
            Copy-Item -LiteralPath $preflightTail -Destination $tailPath -Force -ErrorAction SilentlyContinue
        } else {
            [System.IO.File]::WriteAllText($tailPath, '')
        }

        if ($preflightExit -eq 124) {
            [Console]::Error.WriteLine("dispatch-copilot: preflight timed out after ${preflightTimeout}s; refusing to spend a review round.")
        } elseif ($preflightText -match '(?i)Authentication token found but could not be validated|Failed to fetch OAuth user login|authentication failed|not authenticated|unauthorized') {
            [Console]::Error.WriteLine('dispatch-copilot: preflight could not validate Copilot authentication; refusing to spend a review round.')
            [Console]::Error.WriteLine("Check network/proxy access first, then run 'copilot login' or provide COPILOT_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN.")
        } elseif ($preflightText -match "(?i)unknown option '--no-warnings'") {
            [Console]::Error.WriteLine("dispatch-copilot: Copilot's shell runner rejected its internal --no-warnings flag; refusing to start.")
            [Console]::Error.WriteLine("Run 'copilot update', start a fresh process, and retry. Dispatch auto-update is disabled with --no-auto-update.")
        } else {
            [Console]::Error.WriteLine("dispatch-copilot: preflight could not run a successful git shell command (exit $preflightExit); refusing to start.")
        }
        if (Test-Path -LiteralPath $preflightTail -PathType Leaf) {
            Get-Content -LiteralPath $preflightTail -Tail 40 -ErrorAction SilentlyContinue | ForEach-Object {
                [Console]::Error.WriteLine($_)
            }
        }
        if ($preflightExit -eq 0) { $preflightExit = 70 }
        exit $preflightExit
    }
}

# Start stall-watch only for the full review. The live JSON stream below makes
# tail growth a meaningful liveness signal instead of a completion-only write.
$scriptDir = if ($env:IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR) {
    $env:IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR
} else {
    $PSScriptRoot
}
$stallWatch = Join-Path $scriptDir 'stall-watch.ps1'
$stallProc = $null
if (Test-Path -LiteralPath $stallWatch -PathType Leaf) {
    $stallProc = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $stallWatch,
                        '--state-dir', $stateDir, '--parent-pid', $PID) `
        -WindowStyle Hidden -PassThru -ErrorAction SilentlyContinue
}
Remove-Item Env:IMPLEMENT_REVIEW_DISPATCH_REEXEC -ErrorAction SilentlyContinue
Remove-Item Env:IMPLEMENT_REVIEW_DISPATCH_SOURCE_DIR -ErrorAction SilentlyContinue

$ErrorActionPreference = 'Continue'

# Run copilot via a transient .cmd helper, for the same reasons dispatch-codex
# uses one (see that script for the long rationale):
#
# 1. Token preservation: cmd /c uses plain CreateProcess, so Copilot's own git
#    subprocesses inherit the logon-session token cleanly. PowerShell's
#    Start-Process with stream redirection routes through CreateProcessAsUserW,
#    which strips the token (child git then fails with Windows error 1312).
# 2. AV behavior: a plain .cmd + call operator avoids the process-injection
#    signature some AVs flag on the .NET StandardInput.BaseStream pattern.
# 3. Live tail growth: cmd's `> tail 2>&1` are OS-handle redirections that
#    append in real time, so stall-watch observes growth during the run.
#
# Copilot differs from Codex in HOW the prompt is delivered: `-p "@<prompt>"`
# references the prompt FILE (a long literal -p argument fails), so there is no
# stdin redirection. GIT_PAGER=cat keeps Copilot's own `git diff` from stalling
# on a pager. --allow-all gives Copilot the same unattended verification
# capability as the other reviewer backends; the review prompt keeps
# commit/push/publish/destructive operations out of scope. Copilot writes
# Review-GitHub-Copilot.md itself per the prompt's save contract. JSON
# streaming records reasoning and tool lifecycle events as they occur, so
# stall-watch can observe progress and postflight can reject hidden tool denial.
#
# Path handling mirrors dispatch-codex: escape every `%` to `%%` so cmd does not
# env-expand path values, and write the helper as UTF-8 (no BOM) with a
# `chcp 65001` prefix so non-ASCII paths survive cmd's codepage layer.
$cmdHelper = Join-Path $stateDir 'run-copilot.cmd'
$cmdBody = "@echo off`r`nchcp 65001 >NUL`r`nset GIT_PAGER=cat`r`n""$exeEsc"" ${ghPrefix}-C ""$repoEsc"" -p ""@$promptFileEsc"" --allow-all --no-ask-user --no-auto-update --stream on --output-format json --no-color > ""$tailPathEsc"" 2>&1`r`n"
[System.IO.File]::WriteAllText($cmdHelper, $cmdBody, $utf8NoBom)

& $cmdHelper
$copilotExit = $LASTEXITCODE

Remove-Item -LiteralPath $cmdHelper -Force -ErrorAction SilentlyContinue

# Ensure the tail file exists even if copilot emitted nothing -- Phase 2.0
# Health check 8 distinguishes "tail empty" from "tail missing".
if (-not (Test-Path -LiteralPath $tailPath -PathType Leaf)) {
    Set-Content -LiteralPath $tailPath -Value '' -NoNewline -ErrorAction SilentlyContinue
}

# Official CLI runs must not look complete when auth, the internal shell
# runner, permissions, or the review save contract failed quietly.
if ($strictChecks) {
    $tailText = Get-Content -LiteralPath $tailPath -Raw -ErrorAction SilentlyContinue
    $operationalTailText = (Get-Content -LiteralPath $tailPath -ErrorAction SilentlyContinue | Where-Object {
        $_ -notmatch '^\{' -or
        $_ -match '"type":"tool\.execution_complete".*"success":false' -or
        $_ -match '"type":"error"'
    }) -join "`n"
    if ($operationalTailText -match "(?i)unknown option '--no-warnings'") {
        [Console]::Error.WriteLine('dispatch-copilot: Copilot emitted the known --no-warnings shell-runner failure; review rejected.')
        $copilotExit = 70
    } elseif ($operationalTailText -match '(?i)Authentication token found but could not be validated|Failed to fetch OAuth user login|authentication failed|not authenticated|unauthorized') {
        [Console]::Error.WriteLine("dispatch-copilot: Copilot authentication failed during dispatch; review rejected. Run 'copilot login' after checking network/proxy access.")
        $copilotExit = 70
    } elseif ($tailText -match '(?i)("type":"tool\.execution_complete".*"success":false.*(permission|denied|not allowed|blocked)|(permission|denied|not allowed|blocked).*"type":"tool\.execution_complete".*"success":false)') {
        [Console]::Error.WriteLine("dispatch-copilot: a review tool was denied by Copilot's permission layer; review rejected instead of accepting unverified output.")
        $copilotExit = 70
    }

    if ($copilotExit -eq 0) {
        $reviewPath = if ([System.IO.Path]::IsPathRooted($ExpectedReviewFile)) {
            $ExpectedReviewFile
        } else {
            Join-Path $repo $ExpectedReviewFile
        }
        $reviewFailure = $null
        if (-not (Test-Path -LiteralPath $reviewPath -PathType Leaf)) {
            $reviewFailure = "expected review file was not created: $ExpectedReviewFile"
        } else {
            $reviewItem = Get-Item -LiteralPath $reviewPath
            $firstLine = Get-Content -LiteralPath $reviewPath -TotalCount 1 -ErrorAction SilentlyContinue
            $reviewMtime = ([DateTimeOffset]$reviewItem.LastWriteTimeUtc).ToUnixTimeSeconds()
            $freshFloor = [Math]::Max([long]$preMtime, [long]$nowUnix)
            if ($reviewItem.Length -lt 500) {
                $reviewFailure = "expected review file is only $($reviewItem.Length) bytes: $ExpectedReviewFile"
            } elseif ($firstLine.TrimEnd("`r") -ne "<!-- Round $Round -->") {
                $reviewFailure = "expected review file lacks the current round marker: $ExpectedReviewFile"
            } elseif ($reviewMtime -le $freshFloor) {
                $reviewFailure = "expected review file was not refreshed by this dispatch: $ExpectedReviewFile"
            }
        }
        if ($reviewFailure) {
            [Console]::Error.WriteLine("dispatch-copilot: $reviewFailure; review rejected.")
            $copilotExit = 70
        }
    }
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

# Do NOT force-kill stall-watch. It polls our PID via Get-Process and exits
# silently on its next interval after we die. Stopping it on the hot path can
# erase a stall period that crossed the threshold during the final poll window.

exit $copilotExit
