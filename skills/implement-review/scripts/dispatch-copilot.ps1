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

$ErrorActionPreference = 'Stop'

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

# Launch stall-watch in background if present (shared with the Codex backend).
# Use $PSScriptRoot (auto-populated when invoked as a file) instead of
# Split-Path $PSCommandPath, which can fail under some invocation contexts.
$scriptDir = $PSScriptRoot
$stallWatch = Join-Path $scriptDir 'stall-watch.ps1'
$stallProc = $null
if (Test-Path -LiteralPath $stallWatch -PathType Leaf) {
    $stallProc = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $stallWatch,
                        '--state-dir', $stateDir, '--parent-pid', $PID) `
        -WindowStyle Hidden -PassThru -ErrorAction SilentlyContinue
}

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
        # Nothing resolvable; let the cmd invocation surface its own error to
        # the tail rather than guessing here.
        $exe = $copilotBin
    }
}

$tailPath = Join-Path $stateDir 'tail'

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
# on a pager. The narrow allow-list (read + write + git, scoped to the repo via
# --add-dir) is tighter than the Codex backend's danger-full-access; Copilot
# writes Review-GitHub-Copilot.md itself per the prompt's save contract.
#
# Path handling mirrors dispatch-codex: escape every `%` to `%%` so cmd does not
# env-expand path values, and write the helper as UTF-8 (no BOM) with a
# `chcp 65001` prefix so non-ASCII paths survive cmd's codepage layer.
$cmdHelper = Join-Path $stateDir 'run-copilot.cmd'
$repo = (Get-Location).Path
$exeEsc = $exe -replace '%', '%%'
$repoEsc = $repo -replace '%', '%%'
$promptFileEsc = $PromptFile -replace '%', '%%'
$tailPathEsc = $tailPath -replace '%', '%%'
$ghPrefix = if ($useGh) { 'copilot ' } else { '' }
$cmdBody = "@echo off`r`nchcp 65001 >NUL`r`nset GIT_PAGER=cat`r`n""$exeEsc"" ${ghPrefix}-C ""$repoEsc"" -p ""@$promptFileEsc"" --add-dir ""$repoEsc"" --allow-tool=read --allow-tool=write --allow-tool=""shell(git:*)"" --no-ask-user --silent --stream off --no-color > ""$tailPathEsc"" 2>&1`r`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($cmdHelper, $cmdBody, $utf8NoBom)

& $cmdHelper
$copilotExit = $LASTEXITCODE

Remove-Item -LiteralPath $cmdHelper -Force -ErrorAction SilentlyContinue

# Ensure the tail file exists even if copilot emitted nothing -- Phase 2.0
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

# Do NOT force-kill stall-watch. It polls our PID via Get-Process and exits
# silently on its next interval after we die. Stopping it on the hot path can
# erase a stall period that crossed the threshold during the final poll window.

exit $copilotExit
