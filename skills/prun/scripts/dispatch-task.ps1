# dispatch-task.ps1 -- prun generic task dispatch (Codex worker, PowerShell variant).
# Generalized from implement-review/scripts/dispatch-codex.ps1. Runs codex from a
# per-unit scratch cwd; see dispatch-task.sh for the full contract.
#
# Args (named, --foo style for parity with .sh):
#   --prompt-file <path>   Task prompt (fed to codex on stdin)
#   --result-file <path>   File the unit will write its result to (mtime snapshot)
#   --unit-id <id>         Label (alnum/dash/underscore; names the state-dir)
#
# Env: CODEX_BIN, TMPDIR/TEMP/TMP, CODEX_DISPATCH_SANDBOX (default danger-full-access),
#      CODEX_DISPATCH_ISOLATE_MCP (off disables isolation), CODEX_DISPATCH_REASONING
#      (default xhigh), PRUN_SCRATCH_CWD (default <state-dir>\work),
#      PRUN_STALL_THRESHOLD (default 600), CODEX_DISPATCH_TIMEOUT (default 0,
#      disabled)
#
# Stdout: STATE-DIR <abs-path> (first and only machine-readable line)
# Exit:   propagates codex exec's exit code; 124 when dispatch reaps the worker;
#         2 on usage error.

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

if ($UnitId -notmatch '^[A-Za-z0-9_-]+$') {
    [Console]::Error.WriteLine("dispatch-task: --unit-id must be alphanumeric/dash/underscore, got: $UnitId")
    exit 2
}

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

# Run codex via a transient .cmd helper. cmd's `< > 2>&1` are byte-level OS-handle
# redirections (no BOM/CRLF drift) and `cmd /c` uses plain CreateProcess, so codex's
# own git/browser subprocesses inherit the logon token (avoids Windows error 1312).
# The helper `cd /d`s into the per-unit scratch dir first. Escape % to %% so cmd does
# not env-expand path values; write UTF-8 no-BOM with chcp 65001 for non-ASCII paths.
$sandboxMode = if ($env:CODEX_DISPATCH_SANDBOX) { $env:CODEX_DISPATCH_SANDBOX } else { 'danger-full-access' }
$reasoning = if ($env:CODEX_DISPATCH_REASONING) { $env:CODEX_DISPATCH_REASONING } else { 'xhigh' }
$isolateArg = if ($env:CODEX_DISPATCH_ISOLATE_MCP -eq 'off') { '' } else { "--ignore-user-config -c model_reasoning_effort=$reasoning " }

$codexBinEsc   = $codexBin   -replace '%', '%%'
$tailPathEsc   = $tailPath   -replace '%', '%%'
$promptFileEsc = $PromptFile -replace '%', '%%'
$sandboxModeEsc = $sandboxMode -replace '%', '%%'
$scratchEsc    = $scratchCwd -replace '%', '%%'

# --skip-git-repo-check is required because the scratch cwd is intentionally
# NOT a git repo (and --ignore-user-config drops the trusted-projects list);
# without it codex refuses with "Not inside a trusted directory".
$cmdHelper = Join-Path $stateDir 'run-task.cmd'
# `|| exit /b 1` aborts if the cd fails, so codex never runs from the inherited
# (repo) cwd and the scratch-isolation invariant holds (parity with the .sh `&&`).
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
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $reapWatch,
                        '--state-dir', $stateDir, '--worker-pid', $worker.Id, '--tail', $tailPath) `
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
