# dispatch-codex.ps1 -- Auto-terminal channel dispatch for implement-review skill.
# See skills/implement-review/SKILL.md > Phase 1c Auto-terminal path for the contract.
#
# Args (named, --foo style for cross-platform parity with .sh):
#   --prompt-file <path>           Path to file containing the review prompt
#   --round <N>                    Round number (positive integer)
#   --expected-review-file <name>  Review file the reviewer is expected to write
#                                  (resolved relative to cwd for pre-mtime snapshot)
#
# Env:
#   CODEX_BIN                      Codex binary name or path (default: codex)
#   TMPDIR / TEMP / TMP            Temp dir for state-dir (Windows uses TEMP by default)
#
# Stdout:
#   First (and only) machine-readable line: STATE-DIR <abs-path>
#
# Stderr:
#   Dispatch diagnostics + last 80 lines of codex-exec combined stdout+stderr
#
# Exit code:
#   Propagates codex exec's exit code unchanged.
#   Returns 2 on usage errors (missing/invalid args).

$ErrorActionPreference = 'Stop'

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

# Emit STATE-DIR on stdout (first and only machine-readable line)
[Console]::Out.WriteLine("STATE-DIR $stateDir")
[Console]::Out.Flush()

# Launch stall-watch in background if present (B2 will land the script)
# Use $PSScriptRoot (auto-populated when this script is invoked as a file)
# instead of Split-Path $PSCommandPath, which can fail with "Parameter set
# cannot be resolved" if PowerShell sees $PSCommandPath as null under some
# invocation contexts.
$scriptDir = $PSScriptRoot
$stallWatch = Join-Path $scriptDir 'stall-watch.ps1'
$stallProc = $null
if (Test-Path -LiteralPath $stallWatch -PathType Leaf) {
    $stallProc = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $stallWatch,
                        '--state-dir', $stateDir, '--parent-pid', $PID) `
        -WindowStyle Hidden -PassThru -ErrorAction SilentlyContinue
}

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
$cmdBody = "@echo off`r`nchcp 65001 >NUL`r`n""$codexBinEsc"" exec --sandbox $sandboxModeEsc - > ""$tailPathEsc"" 2>&1 < ""$promptFileEsc""`r`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($cmdHelper, $cmdBody, $utf8NoBom)

& $cmdHelper
$codexExit = $LASTEXITCODE

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

# Do NOT force-kill stall-watch. It already polls our PID via Get-Process and
# will exit silently on its next interval after we die. Stopping it on the hot
# path can erase a stall period that crossed the threshold during the final
# poll window, leaving Phase 2.0 Health check 9 with no record.
# The cost is one extra polling interval of lingering observer; the benefit is
# preserving the Check 9 signal that justified stall-watch's existence.

exit $codexExit
