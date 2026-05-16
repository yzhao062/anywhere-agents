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

# Run codex exec with prompt via stdin (NOT positional arg; not codex exec review).
# Use Start-Process -RedirectStandardInput <filepath>: Windows' process-creation
# API reads the prompt file at byte level (no encoding conversion, no BOM, no
# CRLF translation), matching the .sh path's `< $PromptFile` byte semantics and
# preserving the byte-identical invariant declared in SKILL.md Phase 1c.
# Avoids both: (1) PowerShell text pipeline `Get-Content | & <bin>`, which
# re-encodes and can inject a UTF-8 BOM under PS 5.1; (2) System.Diagnostics
# raw stream writes, which AV products sometimes flag because the same pattern
# is used in process injection.
$codexBin = if ($env:CODEX_BIN) { $env:CODEX_BIN } else { 'codex' }
$tailPath = Join-Path $stateDir 'tail'

$ErrorActionPreference = 'Continue'

$stderrCap = Join-Path $stateDir 'tail.stderr-tmp'

# Stream codex stdout directly to <state-dir>/tail so stall-watch can observe
# real-time growth during the run (its Health check 9 signal depends on this).
# Stderr is captured separately and appended after exit; this loses some
# stream interleaving but preserves all diagnostic content. The earlier
# variant of this script wrote both streams to side files and concatenated
# only at exit, which silently broke stall-watch (tail did not exist during
# the codex run, so no growth comparison was possible).
$codexExit = 1
try {
    $proc = Start-Process -FilePath $codexBin `
        -ArgumentList @('exec', '-') `
        -RedirectStandardInput $PromptFile `
        -RedirectStandardOutput $tailPath `
        -RedirectStandardError $stderrCap `
        -NoNewWindow -Wait -PassThru -ErrorAction Stop
    $codexExit = $proc.ExitCode
} catch {
    [Console]::Error.WriteLine("dispatch-codex: failed to start codex: $_")
    Set-Content -LiteralPath $tailPath -Value "dispatch-codex: failed to start codex: $_" -ErrorAction SilentlyContinue
}

# Append captured stderr to tail.
if (Test-Path -LiteralPath $stderrCap -PathType Leaf) {
    try {
        $errText = Get-Content -Raw -LiteralPath $stderrCap -ErrorAction Stop
        if ($errText) {
            Add-Content -LiteralPath $tailPath -Value $errText -NoNewline -ErrorAction SilentlyContinue
        }
    } catch {
        # Best-effort.
    }
    Remove-Item -LiteralPath $stderrCap -Force -ErrorAction SilentlyContinue
}

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
