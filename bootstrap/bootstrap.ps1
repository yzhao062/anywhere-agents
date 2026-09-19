# Line endings are handled by this repo's .gitattributes. Bootstrap intentionally
# avoids changing user-level Git configuration.

param(
    [Parameter(Position=0)][string]$Upstream,
    [string]$RulePacks,
    [switch]$NoCache,
    [Alias("h")][switch]$Help
)

# Verify a candidate python actually executes before trusting it. Exit status
# alone is not proof that Python ran, so require the interpreter to echo a
# sentinel produced by the -c program.
#
# The -c argument must contain no double quote. Windows PowerShell 5.1 rebuilds
# a single command-line string for a native executable and does not escape a
# double quote inside the argument, so Python receives a truncated program and
# fails to parse it. Quote the argument with double quotes and use single quotes
# inside; keep `$` out of it, because the outer double quotes interpolate.
function Test-PythonRuns([string]$PythonPath) {
  try {
    $global:LASTEXITCODE = $null
    $probeOutput = & $PythonPath -c "import sys; sys.stdout.write('__ANYWHERE_AGENTS_PY3__' if sys.version_info[0] >= 3 else '')" 2>$null
    $launched = $?
    return ($launched -and $LASTEXITCODE -eq 0 -and ([string]$probeOutput).Trim() -eq '__ANYWHERE_AGENTS_PY3__')
  } catch {
    return $false
  }
}

function Test-PythonHasYaml([string]$PythonPath) {
  $global:LASTEXITCODE = $null
  & $PythonPath -c "import yaml" 2>$null
  return ($? -and $LASTEXITCODE -eq 0)
}

# Find a real Python interpreter, avoiding the Windows Store App Execution
# Alias shim under %LOCALAPPDATA%\Microsoft\WindowsApps\ that prints
# "Python was not found; install from Store" and exits non-zero on call.
# See https://github.com/yzhao062/anywhere-agents/issues/2.
function Find-RealPython {
  if ($env:ANYWHERE_AGENTS_PYTHON) {
    $override = Resolve-Path -LiteralPath $env:ANYWHERE_AGENTS_PYTHON -ErrorAction SilentlyContinue
    if ($override) {
      $cmd = Get-Command $override.ProviderPath -ErrorAction SilentlyContinue
      if ($cmd -and (Test-PythonRuns $cmd.Path)) { return $cmd }
    }
    [Console]::Error.WriteLine("[anywhere-agents] ANYWHERE_AGENTS_PYTHON did not execute Python 3 successfully: $($env:ANYWHERE_AGENTS_PYTHON); trying automatic discovery.")
  }
  $candidates = @()
  $candidates += Get-Command python3 -All -ErrorAction SilentlyContinue
  $candidates += Get-Command python -All -ErrorAction SilentlyContinue
  foreach ($c in $candidates) {
    if (-not $c) { continue }
    if ($c.Source -and ($c.Source -notmatch 'WindowsApps') -and (Test-PythonRuns $c.Path)) {
      return $c
    }
  }
  return $null
}

# Read the passive-pack selection from one config layer without Python or
# PyYAML. The return value is one of: none, empty, nonempty.
#
# `packs:` is the canonical key and `rule_packs:` the deprecated alias, and
# within one file the resolver prefers the canonical one. A pre-parser that
# knew only the alias read a consumer on the canonical key as having no
# selection at all, which is the answer that both deletes composed pack blocks
# and freezes opted-out ones. See scripts/packs/config.py.
function Get-RulePacksConfigState([string]$ConfigPath) {
  # The empty-string check comes first on purpose. Get-UserConfigPath returns
  # one when no user-config home resolves. `Test-Path -LiteralPath ''` answers
  # $false on PowerShell 7, but on Windows PowerShell 5.1 it fails to bind,
  # writes a non-terminating error, and returns nothing, and the surrounding
  # `if (-not ...)` then takes the branch for a path that exists.
  if (-not $ConfigPath) { return 'none' }
  if (-not (Test-Path -LiteralPath $ConfigPath)) { return 'none' }
  $canonical = Get-RulePacksKeyState $ConfigPath 'packs'
  if ($canonical -ne 'none') { return $canonical }
  return (Get-RulePacksKeyState $ConfigPath 'rule_packs')
}

# Return the value that follows a top-level `<key>:` on this line, or $null
# when the line is not that key. See the bash half: matching only the bare
# spelling answered `none` for `"packs": [agent-style]` and
# `packs : [agent-style]`, which the resolver reads as a selection. $null and
# the empty string are different answers here, so callers compare against $null
# rather than testing truthiness.
function Get-RulePacksKeyTail([string]$Line, [string]$Key) {
  if ($Line -match '^\s') { return $null }
  $colon = $Line.IndexOf(':')
  if ($colon -lt 0) { return $null }
  $head = ($Line.Substring(0, $colon) -replace '\s', '')
  if ($head.Length -ge 2) {
    $first = $head[0]
    $last = $head[$head.Length - 1]
    if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
      $head = $head.Substring(1, $head.Length - 2)
    }
  }
  # -cne, not -ne: PowerShell compares case-insensitively by default, so
  # `Rule_Packs:` read as `empty` here and as `none` in bash, and the
  # PowerShell answer routes to the silent bare-copy path.
  if ($head -cne $Key) { return $null }
  return $Line.Substring($colon + 1)
}

# The single-key scanner behind Get-RulePacksConfigState.
function Get-RulePacksKeyState([string]$ConfigPath, [string]$Key) {
  if (-not $ConfigPath) { return 'none' }
  if (-not (Test-Path -LiteralPath $ConfigPath)) { return 'none' }
  $found = $false
  $inList = $false
  foreach ($line in [System.IO.File]::ReadAllLines((Resolve-Path -LiteralPath $ConfigPath))) {
    $keyTail = Get-RulePacksKeyTail $line $Key
    if ($null -ne $keyTail) {
      $found = $true
      $inList = $true
      $tail = (($keyTail -replace '#.*$', '') -replace '\s', '').ToLowerInvariant()
      if ($tail -and $tail -notin @('[]', 'null', '~')) { return 'nonempty' }
      continue
    }
    if ($inList) {
      if ([string]::IsNullOrWhiteSpace($line) -or $line -match '^\s*#') { continue }
      # A block sequence may sit at the same indentation as its key. That is
      # what PyYAML's safe_dump emits, and anywhere-agents writes this file
      # with safe_dump, so the zero-indent shape is the common one rather than
      # an edge case. Requiring indentation here read every such file as an
      # empty list, which is the explicit opt-out.
      if ($line -match '^\s*-') { return 'nonempty' }
      # The three spellings of an empty value; see the bash half.
      if ((($line -replace '\s', '').ToLowerInvariant()) -in @('[]', 'null', '~')) { continue }
      # An indented node belongs to the key, and anything that is not a proven
      # empty list counts as a selection. See the bash half: an indented flow
      # sequence read as the explicit opt-out and deleted every pack block.
      if ($line -match '^\s+') { return 'nonempty' }
      break
    }
  }
  if ($found) { return 'empty' }
  return 'none'
}

# Report whether the AGENTS.md already on disk is a composed artifact. The
# composer stamps `<!-- rule-pack:<name>:begin ... -->` above each pack block,
# so that marker is the one signal available without re-running composition.
# The skipped-composition marker reads `rule-pack composition skipped`, with a
# space rather than a colon after `rule-pack`, so it does not match here.
function Test-AgentsMdIsComposed {
  if (-not (Test-Path -LiteralPath 'AGENTS.md')) { return $false }
  try {
    $text = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath 'AGENTS.md'))
  } catch {
    return $false
  }
  # A complete marker line. See the bash half for why a prefix match both
  # discarded authentic artifacts and accepted fakes, and for why the version
  # field is any non-whitespace run rather than a narrow character class.
  # -cmatch, not -match: the default is case-insensitive, so an uppercase fake
  # passed here while bash rejected it. The trailing class tolerates CRLF and
  # trailing blanks, matching the bash half's [[:space:]]*; \s is not usable
  # here because it also matches the newline and would run the match past the
  # line, and the version class excludes \n for the same reason.
  return ($text -cmatch '(?m)^<!-- rule-pack:.+:begin version=[^ \t\r\n]+ sha256=[0-9A-Fa-f]{64} -->[ \t\r]*$')
}

# Append one entry to .gitignore, once. The previous inline form prefixed the
# value with a newline escape, which emitted a blank line whenever the file
# already ended in one, and a leading blank line when it created the file.
# Probe the last byte instead, and write without the BOM Add-Content adds on
# some editions.
function Add-GitignoreLine([string]$Line) {
  $path = Join-Path (Get-Location).Path '.gitignore'
  $prefix = ''
  if (Test-Path -LiteralPath $path) {
    $bytes = [System.IO.File]::ReadAllBytes($path)
    if ($bytes.Length -gt 0 -and $bytes[$bytes.Length - 1] -ne 0x0A) { $prefix = "`n" }
  }
  $encoding = New-Object System.Text.UTF8Encoding $false
  [byte[]]$existing = @()
  if (Test-Path -LiteralPath $path) {
    $existing = [System.IO.File]::ReadAllBytes($path)
  }
  $addition = $encoding.GetBytes($prefix + $Line + "`n")
  $combined = New-Object byte[] ($existing.Length + $addition.Length)
  [System.Array]::Copy($existing, 0, $combined, 0, $existing.Length)
  [System.Array]::Copy($addition, 0, $combined, $existing.Length, $addition.Length)
  [System.IO.File]::WriteAllBytes($path, $combined)
}

function Add-GitignoreEntry([string]$Pattern, [string]$Line) {
  # -CaseSensitive, because Select-String is case-insensitive by default while
  # the bash half's `grep -qE` is not. With an existing `/AGENTS.MD`, the two
  # entry points disagreed: PowerShell called the rule present and Bash
  # appended the lowercase one. On a case-sensitive checkout `/AGENTS.MD` does
  # not cover `AGENTS.md`, so the generated file becomes visible again.
  if ((Test-Path .gitignore) -and (Select-String -Quiet -CaseSensitive -Pattern $Pattern .gitignore)) { return }
  $path = Join-Path (Get-Location).Path '.gitignore'
  $prefix = ''
  if (Test-Path -LiteralPath $path) {
    $bytes = [System.IO.File]::ReadAllBytes($path)
    if ($bytes.Length -gt 0 -and $bytes[$bytes.Length - 1] -ne 0x0A) { $prefix = "`n" }
  }
  $encoding = New-Object System.Text.UTF8Encoding $false
  [byte[]]$existing = @()
  if (Test-Path -LiteralPath $path) {
    $existing = [System.IO.File]::ReadAllBytes($path)
  }
  $addition = $encoding.GetBytes($prefix + $Line + "`n")
  $combined = New-Object byte[] ($existing.Length + $addition.Length)
  [System.Array]::Copy($existing, 0, $combined, 0, $existing.Length)
  [System.Array]::Copy($addition, 0, $combined, $existing.Length, $addition.Length)
  [System.IO.File]::WriteAllBytes($path, $combined)
}

# Report whether git already tracks a path in this repo.
function Test-GitTracks([string]$RelativePath) {
  try {
    $global:LASTEXITCODE = $null
    & git ls-files --error-unmatch -- $RelativePath 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

# Where the user-level config layer lives, mirroring config.user_config_home.
# That function branches on the platform rather than cascading: Windows reads
# %APPDATA% and stops, POSIX reads $XDG_CONFIG_HOME then $HOME/.config and
# never looks at %APPDATA%. See the bash half for the shapes a cascade got
# wrong. Returns an empty string when nothing resolves, which the caller
# treats as no layer.
function Get-UserConfigPath {
  # $IsWindows exists from PowerShell 6 on. Windows PowerShell 5.1 leaves it
  # undefined and only ever runs on Windows, so a null value means Windows.
  if ($null -eq $IsWindows -or $IsWindows) {
    if ($env:APPDATA) { return (Join-Path (Join-Path $env:APPDATA 'anywhere-agents') 'config.yaml') }
    return ''
  }
  if ($env:XDG_CONFIG_HOME) { return (Join-Path (Join-Path $env:XDG_CONFIG_HOME 'anywhere-agents') 'config.yaml') }
  if ($env:HOME) { return (Join-Path (Join-Path (Join-Path $env:HOME '.config') 'anywhere-agents') 'config.yaml') }
  return ''
}

# True when the environment overlay names at least one pack to ADD. The overlay
# grammar is additive with a `-name` subtract form, so a value made only of
# subtractions adds nothing and must not be read as a selection. See the bash
# half for the recorded difference from config.parse_env_var on whitespace.
function Test-EnvPackSelectionAdds {
  $raw = [string]$env:AGENT_CONFIG_PACKS
  if (-not $raw) { $raw = [string]$env:AGENT_CONFIG_RULE_PACKS }
  if (-not $raw) { return $false }
  foreach ($entry in ($raw -split '[\s,]+')) {
    if (-not $entry) { continue }
    if ($entry.StartsWith('-')) {
      # An entry the resolver rejects outright, or a name carrying anything a
      # pack name is not made of; see the bash half for why this is a whitelist.
      $name = $entry.Substring(1)
      if (-not $name -or $entry -match '[/@:]' -or $name -notmatch '^[A-Za-z0-9._-]+$') {
        return $true
      }
      continue
    }
    return $true
  }
  return $false
}

# The four layers, in the resolver's precedence order: user-level, tracked,
# project-local, environment overlay. Within the three file layers an explicit
# empty list clears everything earlier, and a nonempty list selects; a file
# with no key at all leaves the running answer alone. The overlay is additive
# and so can only turn the answer on. See the bash half for the seed rationale
# and for the five documented gaps, each of which preserves a file rather than
# deleting one.
# True when a line in this file is one the scanner above cannot classify; see
# the bash half for the four readable shapes and for why file length is not the
# same question.
function Test-FileHasUnreadableLine([string]$Path) {
  if (-not $Path) { return $false }
  if (-not (Test-Path -LiteralPath $Path)) { return $false }
  $seenTopLevel = $false
  foreach ($line in [System.IO.File]::ReadAllLines((Resolve-Path -LiteralPath $Path))) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    if ($line -match '^\s*#') { continue }
    # A continuation needs a line above it; see the bash half.
    if ($line -match '^\s') {
      if ($seenTopLevel) { continue }
      return $true
    }
    $seenTopLevel = $true
    if ($line -match '^\s*-') { continue }
    $colon = $line.IndexOf(':')
    if ($colon -lt 0) { return $true }
    $head = ($line.Substring(0, $colon) -replace '\s', '')
    if (-not $head -or $head -notmatch '^[A-Za-z0-9_.''"-]+$') { return $true }
  }
  return $false
}

function Test-PassiveRulePackConfigured {
  $configured = $true
  foreach ($layer in @((Get-UserConfigPath), 'agent-config.yaml', 'agent-config.local.yaml')) {
    if (-not $layer) { continue }
    switch (Get-RulePacksConfigState $layer) {
      'nonempty' { $configured = $true }
      'empty' { $configured = $false }
      'none' {
        # A nonempty later layer this scanner cannot read is uncertainty rather
        # than absence; see the bash half.
        if (-not $configured -and (Test-FileHasUnreadableLine $layer)) {
          $configured = $true
        }
      }
    }
  }
  if (Test-EnvPackSelectionAdds) { $configured = $true }
  return $configured
}

# Stage beside the destination, then rename over it. Readers therefore see a
# complete old helper or a complete new helper, never a truncated copy.
function Copy-HelperAtomic([string]$Source, [string]$Destination) {
  $destinationDirectory = Split-Path -Parent $Destination
  $destinationName = Split-Path -Leaf $Destination
  $tempPath = $null
  $backupPath = $null
  try {
    New-Item -ItemType Directory -Force -Path $destinationDirectory -ErrorAction Stop | Out-Null
    # Same no-op skip as the Bash helper (#44). A replacement onto a file a live
    # session holds open is refused, and taking that refusal over identical bytes
    # fails the phase for nothing. There is no executable bit to preserve here.
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
      try {
        $sourceHash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256 -ErrorAction Stop).Hash
        $destinationHash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256 -ErrorAction Stop).Hash
        if ($sourceHash -eq $destinationHash) { return $true }
      } catch {
        # Unreadable on either side: fall through to the replacement and let its
        # own error path report, rather than turning a read failure into a skip.
      }
    }
    $tempPath = Join-Path $destinationDirectory ('.{0}.{1}.tmp' -f $destinationName, [guid]::NewGuid().ToString('N'))
    Copy-Item -LiteralPath $Source -Destination $tempPath -Force -ErrorAction Stop
    if (Test-Path -LiteralPath $Destination) {
      $backupPath = Join-Path $destinationDirectory ('.{0}.{1}.bak' -f $destinationName, [guid]::NewGuid().ToString('N'))
      [System.IO.File]::Replace($tempPath, $Destination, $backupPath)
    } else {
      try {
        [System.IO.File]::Move($tempPath, $Destination)
      } catch {
        # Another bootstrap may have created the target after Test-Path.
        if (Test-Path -LiteralPath $Destination) {
          $backupPath = Join-Path $destinationDirectory ('.{0}.{1}.bak' -f $destinationName, [guid]::NewGuid().ToString('N'))
          [System.IO.File]::Replace($tempPath, $Destination, $backupPath)
        } else {
          throw
        }
      }
    }
    return $true
  } catch {
    throw "Could not atomically deploy '$Source' to '$Destination': $($_.Exception.Message)"
  } finally {
    if ($tempPath -and (Test-Path -LiteralPath $tempPath)) {
      Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
    if ($backupPath -and (Test-Path -LiteralPath $backupPath)) {
      Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
    }
  }
}

function Write-Ledger([string]$LastPhase, [bool]$Completed) {
  try {
    $ledgerDir = Join-Path (Get-Location).Path '.agent-config'
    New-Item -ItemType Directory -Force -Path $ledgerDir -ErrorAction Stop | Out-Null
    $path = Join-Path $ledgerDir 'last-run.json'
    $tempPath = Join-Path $ledgerDir 'last-run.json.tmp'
    $document = [ordered]@{
      schema = 1
      emitted_by = 'bootstrap.ps1'
      run_id = $script:LedgerRunId
      started_at = $script:LedgerStarted
      upstream = $script:LedgerUpstream
      completed = $Completed
      last_phase = $LastPhase
      steps = @($script:LedgerSteps)
    }
    $json = $document | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText(
      $tempPath,
      $json + [Environment]::NewLine,
      (New-Object System.Text.UTF8Encoding $false)
    )
    Move-Item -LiteralPath $tempPath -Destination $path -Force -ErrorAction Stop
  } catch {
    # The ledger must never change bootstrap's outcome.
  }
}

function Initialize-Ledger {
  try {
    $script:LedgerSteps = @()
    $script:LedgerTargets = @()
    $script:LedgerIncomplete = $false
    $script:LedgerRunId = "$PID-" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $script:LedgerStarted = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    # Coalesce to a string. bootstrap.sh emits "" for an unset upstream, and
    # ConvertTo-Json renders $null as JSON null, so leaving it unset would give
    # the two entry points different types for the same field.
    $script:LedgerUpstream = if ($Upstream) { $Upstream } elseif ($env:AGENT_CONFIG_UPSTREAM) { $env:AGENT_CONFIG_UPSTREAM } else { '' }
    Write-Ledger 'start' $false
  } catch {}
}

function Add-LedgerTarget([string]$TargetPath) {
  try {
    $script:LedgerTargets += $TargetPath
  } catch {}
}

function Add-LedgerStep {
  param(
    [string]$Phase,
    [string]$Scope,
    [string]$Status,
    [Nullable[int]]$Rc = $null,
    [string]$Reason
  )
  try {
    $stepRc = if ($PSBoundParameters.ContainsKey('Rc')) { [int]$Rc } else { $null }
    $step = [ordered]@{
      phase = $Phase
      scope = $Scope
      status = $Status
      rc = $stepRc
      targets = @($script:LedgerTargets)
    }
    if ($PSBoundParameters.ContainsKey('Reason')) {
      $step['reason'] = $Reason
    }
    $script:LedgerSteps += [pscustomobject]$step
    $script:LedgerTargets = @()
    Write-Ledger $Phase $false
  } catch {}
}

$script:GeneratorStatus = 'skipped'
$script:GeneratorRc = $null

function Invoke-AgentConfigGenerator($PythonCommand) {
  try {
    if (-not $PythonCommand -or -not (Test-Path .agent-config/repo/scripts/generate_agent_configs.py)) {
      return
    }
    $global:LASTEXITCODE = $null
    & $PythonCommand.Path .agent-config/repo/scripts/generate_agent_configs.py --root . --quiet
    $rc = if ($LASTEXITCODE -ne $null) { [int]$LASTEXITCODE } elseif (-not $?) { 1 } else { 0 }
    $script:GeneratorRc = $rc
    $script:GeneratorStatus = if ($rc -eq 0) { 'ok' } else { 'failed' }
  } catch {
    $script:GeneratorRc = 1
    $script:GeneratorStatus = 'failed'
  }
}

function Add-GeneratorLedgerStep {
  try {
    if ($script:GeneratorStatus -eq 'ok') {
      Add-LedgerTarget 'CLAUDE.md'
      Add-LedgerTarget 'agents/codex.md'
      Add-LedgerStep 'generate' 'repo' 'ok'
    } elseif ($script:GeneratorStatus -eq 'failed') {
      Add-LedgerStep 'generate' 'repo' 'failed' $script:GeneratorRc
    } else {
      Add-LedgerStep 'generate' 'repo' 'skipped'
    }
  } catch {}
}

# Detect git binary and reject pre-2.25 versions before any git invocation
# below. Sparse clone uses `git clone --filter=blob:none --sparse`; `--sparse`
# is the Git 2.25 floor (2020-01-13), while `--filter=blob:none` is the older
# partial-clone option (Git 2.19+). On parse failure default-pass with a
# stderr warning so unexpected version strings (alpha builds, distro
# suffixes like `2.30.1.windows.1`) do not block already-modern systems.
function Invoke-GitPreflight {
  if ($env:AGENT_CONFIG_SKIP_GIT_PREFLIGHT) { return }
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine("[anywhere-agents] git is not installed or not on PATH; bootstrap needs git >= 2.25 for sparse clone.")
    [Console]::Error.WriteLine("[anywhere-agents] install: https://git-scm.com/download/win")
    Exit-Bootstrap 1
  }
  $verLine = $null
  try {
    $verLine = (& git --version 2>$null | Select-Object -First 1)
  } catch {
    $verLine = $null
  }
  if (-not $verLine) {
    [Console]::Error.WriteLine("[anywhere-agents] could not run git --version; assuming OK")
    return
  }
  $verStr = $verLine -replace '^git version ', ''
  if ($verStr -match '^(\d+)\.(\d+)') {
    $major = [int]$matches[1]
    $minor = [int]$matches[2]
  } else {
    [Console]::Error.WriteLine("[anywhere-agents] could not parse git version from '$verLine'; assuming OK")
    return
  }
  if (($major -lt 2) -or (($major -eq 2) -and ($minor -lt 25))) {
    [Console]::Error.WriteLine("[anywhere-agents] git $major.$minor is too old; bootstrap needs git >= 2.25 for sparse clone.")
    [Console]::Error.WriteLine("[anywhere-agents] install: https://git-scm.com/download/win")
    Exit-Bootstrap 1
  }
}

if ($Help) {
    Write-Output "Usage: .\bootstrap.ps1 [UPSTREAM] [-RulePacks PACK] [-NoCache]"
    Write-Output "  UPSTREAM      user/repo form; overrides AGENT_CONFIG_UPSTREAM env and persisted file"
    Write-Output "  -RulePacks P  print agent-config.yaml snippet for pack P and exit (dry helper)"
    Write-Output "  -NoCache      force refetch of rule-pack content on this run"
    exit 0
}

# Dry helper: -RulePacks prints a YAML snippet and exits without running
# bootstrap. Flag wins when both -RulePacks and AGENT_CONFIG_RULE_PACKS
# are set simultaneously.
if ($PSBoundParameters.ContainsKey('RulePacks')) {
    if ([string]::IsNullOrEmpty($RulePacks)) {
        [Console]::Error.WriteLine("error: -RulePacks requires a pack name")
        exit 1
    }
    if ($env:AGENT_CONFIG_RULE_PACKS) {
        [Console]::Error.WriteLine("notice: -RulePacks is a dry helper; AGENT_CONFIG_RULE_PACKS env var is ignored in this mode")
    }
    $snippet = @"
Add the following to agent-config.yaml at your project root, then run bootstrap again to apply:

  rule_packs:
    - name: $RulePacks
      # Optional: pin to a specific ref (defaults to manifest's default-ref)
      # ref: v0.3.5

After committing agent-config.yaml, run:

  .\bootstrap.ps1
"@
    Write-Output $snippet
    exit 0
}

try { Initialize-Ledger } catch {}

# Publish the session banner report when this run ends, whatever ended it.
# The renderer reads the ledger this run wrote, so a failed or degraded
# refresh publishes a report naming the phase it stopped at instead of
# leaving an older all-clear report in place. Called at the end of the run
# and before each explicit exit after the ledger exists (Exit-Bootstrap);
# an uncaught exception ends the run without a report, and the agent's
# freshness check then rejects any earlier report. A sparse clone without
# the renderer (a first install whose fetch failed) publishes nothing for
# the same reason. Best effort: the renderer's own failure never changes
# this run's result. Not a try/finally around the body: inside a try block
# a .NET exception at script scope stops the script instead of printing and
# continuing, which is not the error semantics this entry point has.
function Invoke-BannerRender([int]$BootstrapRc = 0) {
  try {
    if (-not (Test-Path -LiteralPath .agent-config/repo/scripts/render_banner.py)) { return }
    $renderPy = $script:pyCmd
    if (-not $renderPy) { $renderPy = Find-RealPython }
    if (-not $renderPy) { return }
    & $renderPy.Path .agent-config/repo/scripts/render_banner.py --root . --bootstrap-rc $BootstrapRc 2>$null | Out-Null
  } catch {}
}

function Exit-Bootstrap([int]$Code) {
  Invoke-BannerRender $Code
  exit $Code
}

# Deploy one user-level helper, or end the run. Copy-HelperAtomic throws when
# the replacement is refused (a live session holding the destination open is
# the common case), and an uncaught throw would end the run with no report.
# The Bash entry point exits 1 on the same failure, so this does too, through
# Exit-Bootstrap so the report names the phase it stopped at.
function Deploy-UserHelper([string]$Source, [string]$Destination, [string]$LedgerName) {
  if (-not (Test-Path $Source)) { return }
  try {
    if (Copy-HelperAtomic $Source $Destination) {
      try { Add-LedgerTarget $LedgerName } catch {}
    }
  } catch {
    [Console]::Error.WriteLine("error: could not atomically deploy ${LedgerName}: $($_.Exception.Message)")
    Exit-Bootstrap 1
  }
}

Invoke-GitPreflight
if ($env:AGENT_CONFIG_PREFLIGHT_TEST) { exit 0 }

# Legacy AC -> AA migration for direct PowerShell-bootstrap runs. If the
# persisted upstream or cached repo origin still points at agent-config
# and the caller did not pass an explicit upstream, delete the old cache
# so the normal clone path below re-clones anywhere-agents.
$explicitUpstream = $PSBoundParameters.ContainsKey('Upstream') -or $env:AGENT_CONFIG_UPSTREAM
$legacyAC = $false
if (-not $explicitUpstream) {
  if (Test-Path .agent-config/upstream) {
    $persisted = (Get-Content .agent-config/upstream -Raw).Replace("`r", "").Replace("`n", "").Trim()
    if ($persisted -eq 'yzhao062/agent-config') { $legacyAC = $true }
  }
  if (-not $legacyAC -and (Test-Path .agent-config/repo/.git/config)) {
    try {
      $originUrl = git -C .agent-config/repo remote get-url origin 2>$null
      if ($originUrl -match '(^|[:/])yzhao062/agent-config(\.git)?/?$') { $legacyAC = $true }
    } catch {}
  }
}
if ($legacyAC) {
  [Console]::Error.WriteLine('[anywhere-agents] Migrating from agent-config bootstrap to anywhere-agents...')
  Remove-Item -LiteralPath .agent-config/repo -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath .agent-config/upstream, .agent-config/bootstrap.sh, .agent-config/bootstrap.ps1 -Force -ErrorAction SilentlyContinue
}

# Resolve Python once before the network-backed fetch and this run's user-level
# helper deployment. Every later Python-backed phase reuses this snapshot.
$pyCmd = Find-RealPython

# Upstream cascade: argv > env var > persisted file > hardcoded default.
# Forkers can persist a different default in their fork; consumers can pass
# upstream via `.\.agent-config\bootstrap.ps1 <user>/<repo>` or the
# $env:AGENT_CONFIG_UPSTREAM environment variable.
if (-not $Upstream) { $Upstream = $env:AGENT_CONFIG_UPSTREAM }
if (-not $Upstream -and (Test-Path .agent-config/upstream)) {
  $Upstream = (Get-Content .agent-config/upstream -Raw).Trim()
}
if (-not $Upstream) { $Upstream = 'yzhao062/anywhere-agents' }
New-Item -ItemType Directory -Force -Path .agent-config | Out-Null
Set-Content -Path .agent-config/upstream -Value $Upstream -NoNewline
try {
  $script:LedgerUpstream = $Upstream
  Add-LedgerTarget '.agent-config/upstream'
  Add-LedgerStep 'preflight' 'repo' 'ok'
} catch {}

# JSON depth for settings documents. -Depth silently truncates on Windows
# PowerShell 5.1 and only warns on 7, so the number is generous rather than
# tuned to the deepest document seen so far.
$script:JsonDepth = 64

# Read a JSON file as UTF-8 regardless of the machine's ANSI codepage.
# Get-Content without -Encoding uses that codepage on Windows PowerShell 5.1,
# which is cp1252 on a default install, so a UTF-8 settings.json round-tripped
# through it loses every character outside the codepage. The loss is silent:
# the result is still valid JSON. A leading BOM is skipped, which heals a copy
# some earlier `Set-Content -Encoding UTF8` wrote.
function Read-JsonFileUtf8([string]$Path) {
  $bytes = [System.IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $Path))
  $offset = 0
  if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    $offset = 3
  }
  # Strict decoding rather than [System.Text.Encoding]::UTF8, whose shared
  # instance replaces an invalid byte with U+FFFD and says nothing. The Bash
  # half decodes with `utf-8-sig`, which raises, so the two entry points
  # disagreed on exactly the input this release is about: on a file already
  # damaged by the cp1252 round trip, the `~/.claude.json` heal read the
  # damage, substituted its own, and wrote the whole file back. That path has
  # no Python helper in front of it and no other guard.
  $strict = New-Object System.Text.UTF8Encoding $false, $true
  $text = $strict.GetString($bytes, $offset, $bytes.Length - $offset)
  # An unparseable target used to come back as one newline under Windows
  # PowerShell 5.1 and as `null` under PowerShell 7, with the run reporting
  # success. A strict decoder does not catch that, because an opening brace on
  # its own is valid UTF-8. What let it through was a call site with no
  # try/catch: ConvertFrom-Json raises a statement-terminating error, which
  # ends the assignment and leaves the variable null while the script carries
  # on to merge and write. The catch at the call site is the fix; measured on
  # both editions, the error terminates with or without -ErrorAction Stop. The
  # switch stays because it also forces the throw when a caller has set
  # $ErrorActionPreference to SilentlyContinue, where the default Continue
  # would otherwise swallow it.
  $data = $text | ConvertFrom-Json -ErrorAction Stop
  # The helper requires both files to hold a JSON object. Without the same
  # check, a bare array or string root reaches Merge-Json, which reads its
  # properties and writes the result back over an object it never merged.
  #
  # The full type name, not the [pscustomobject] accelerator, which resolves to
  # PSObject. Every value is wrapped in one of those, so `'hello' -is
  # [pscustomobject]` is true on both editions and `[1,2] -is [pscustomobject]`
  # is true on 5.1. Written that way the check passed everything through, and a
  # string root came back rewritten.
  if ($null -eq $data -or $data -isnot [System.Management.Automation.PSCustomObject]) {
    throw "the JSON root in $Path is not an object"
  }
  return $data
}

# Serialize to the one on-disk form both entry points agree on: LF line endings
# and a trailing newline. ConvertTo-Json emits CRLF on both editions, so
# writing its output unchanged would rewrite every line of a file the bash path
# writes with LF, and the two machines would take turns reformatting it.
function ConvertTo-CanonicalJson($Object) {
  $json = $Object | ConvertTo-Json -Depth $script:JsonDepth
  return (($json -replace "`r`n", "`n") + "`n")
}

# Write UTF-8 without a BOM. `Set-Content -Encoding UTF8` writes a BOM on
# Windows PowerShell 5.1 and none on 7, so it cannot be the answer here.
function Write-JsonFileUtf8([string]$Path, [string]$Text) {
  [System.IO.File]::WriteAllBytes($Path, (New-Object System.Text.UTF8Encoding $false).GetBytes($Text))
}

# Publish by rename instead of writing over the destination. WriteAllBytes
# truncates first, so an interrupted write leaves an empty settings file, and
# one of those cost a maintainer six user-only keys (#32). The temp file is a
# sibling because a rename is atomic only within one filesystem.
function Write-JsonFileAtomic([string]$Path, [string]$Text) {
  $directory = Split-Path -Parent $Path
  $name = Split-Path -Leaf $Path
  $temp = Join-Path $directory ('.{0}.{1}.tmp' -f $name, [guid]::NewGuid().ToString('N'))
  $backup = $null
  $published = $false
  try {
    Write-JsonFileUtf8 $temp $Text
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
      $backup = Join-Path $directory ('.{0}.{1}.bak' -f $name, [guid]::NewGuid().ToString('N'))
      [System.IO.File]::Replace($temp, $Path, $backup)
    } else {
      [System.IO.File]::Move($temp, $Path)
    }
    $published = $true
  } catch {
    # ReplaceFile can fail with the original already moved to the backup name
    # and the replacement still under the temp name (Windows error 1177), so a
    # blanket cleanup here is what would destroy the only surviving copy.
    $failure = $_
    if ($backup -and (Test-Path -LiteralPath $backup -PathType Leaf) -and
        -not (Test-Path -LiteralPath $Path)) {
      try {
        [System.IO.File]::Move($backup, $Path)
        $backup = $null
      } catch {
        [Console]::Error.WriteLine("warning: could not restore ${Path}; previous content is at $backup")
        throw $failure
      }
    }
    throw $failure
  } finally {
    if ($published) {
      foreach ($stale in @($temp, $backup)) {
        if ($stale -and (Test-Path -LiteralPath $stale -PathType Leaf)) {
          Remove-Item -LiteralPath $stale -Force -ErrorAction SilentlyContinue
        }
      }
    }
  }
}

# Take the same lock the merge helper takes, so the no-Python fallback and the
# first install are not the paths that race. Both sides lock one byte at offset
# 0 of the same file: FileStream.Lock and Python's msvcrt.locking are the same
# Windows byte-range lock. Returns the stream to release, or $null when the lock
# could not be taken, which the caller reports rather than writing anyway.
function Open-SettingsLock([string]$TargetPath, [int]$TimeoutSeconds = 60) {
  $userSettings = Join-Path (Join-Path $HOME '.claude') 'settings.json'
  $lockPath = if ($TargetPath -eq $userSettings) {
    Join-Path (Split-Path -Parent $userSettings) '.pack-lock.lock'
  } else {
    $TargetPath + '.lock'
  }
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ($true) {
    try {
      $stream = [System.IO.File]::Open(
        $lockPath, [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::ReadWrite)
      try {
        $stream.Lock(0, 1)
        return $stream
      } catch {
        $stream.Dispose()
        if ((Get-Date) -ge $deadline) { return $null }
      }
    } catch {
      if ((Get-Date) -ge $deadline) { return $null }
    }
    Start-Sleep -Milliseconds 100
  }
}

function Close-SettingsLock($Stream) {
  if (-not $Stream) { return }
  try { $Stream.Unlock(0, 1) } catch {}
  try { $Stream.Dispose() } catch {}
}

# Keep a short history of the previous content beside the file. The merge helper
# does the same, so whichever path ran, the recovery material looks alike. The
# loss this guards against went unnoticed for 78 days, so the history is bounded
# by count rather than by age.
# True when the file already holds exactly the bytes a publish would write.
function Test-SettingsUnchanged([string]$Path, [string]$Text) {
  try {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $current = [System.IO.File]::ReadAllBytes($Path)
    $next = (New-Object System.Text.UTF8Encoding $false).GetBytes($Text)
    if ($current.Length -ne $next.Length) { return $false }
    for ($i = 0; $i -lt $current.Length; $i++) {
      if ($current[$i] -ne $next[$i]) { return $false }
    }
    return $true
  } catch {
    return $false
  }
}

# Publish a merge result, unless the file already holds those bytes. The merge
# helper applies the same rule: a run that changes nothing writes nothing and
# copies nothing aside, because 27 consumer bootstraps otherwise leave 27
# identical backups and push the recovery history out of the capped window
# (anywhere-agents#58). Returns whether the file was written.
function Publish-SettingsIfChanged([string]$Path, [string]$Text, [switch]$KeepBackup) {
  if (Test-SettingsUnchanged $Path $Text) { return $false }
  if ($KeepBackup) { Backup-SettingsFile $Path }
  Write-JsonFileAtomic $Path $Text
  return $true
}

function Backup-SettingsFile([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
  try {
    $directory = Split-Path -Parent $Path
    $name = Split-Path -Leaf $Path
    # Same fixed-width fractional component as the Python helper, so copies
    # written by either path sort into one chronological history.
    $stamp = (Get-Date).ToUniversalTime().ToString(
      'yyyyMMdd-HHmmss-ffffff', [System.Globalization.CultureInfo]::InvariantCulture)
    $copy = Join-Path $directory ('{0}.bak-{1}-{2}' -f $name, $stamp, $PID)
    Copy-Item -LiteralPath $Path -Destination $copy -Force -ErrorAction Stop
    $keep = 10
    $existing = @(Get-ChildItem -LiteralPath $directory -Filter ($name + '.bak-*') -File -ErrorAction SilentlyContinue |
      Sort-Object Name)
    if ($existing.Count -gt $keep) {
      foreach ($stale in $existing[0..($existing.Count - $keep - 1)]) {
        Remove-Item -LiteralPath $stale.FullName -Force -ErrorAction SilentlyContinue
      }
    }
  } catch {
    # A recovery aid that cannot be written must not become a new way to fail;
    # the publish below is atomic either way.
  }
}

# Merge through the shared Python helper, so both entry points run the same
# code and produce the same bytes. Returns false when the helper cannot run, in
# which case the caller falls back to the in-shell merge below. The fallback is
# byte-compatible in content but not in formatting: ConvertTo-Json indents with
# four spaces and aligns nested objects on Windows PowerShell 5.1, and with two
# spaces on PowerShell 7, so neither matches json.dumps. That divergence is the
# reason the helper exists.
#
# Paths are passed as separate arguments. A Windows path cannot contain a
# double quote and PowerShell quotes embedded spaces correctly, so this is the
# one invocation shape that is safe under the 5.1 native command-line rebuild
# that caused anywhere-agents#34.
# $false means the helper could not run, which is the only state the in-shell
# fallback is for. A nonzero exit is the helper refusing the input after
# reading it, and running a second implementation over the same bytes then
# destroyed them: a target holding `{` came back as one newline under Windows
# PowerShell 5.1 and as `null` under PowerShell 7, and a target holding an
# invalid UTF-8 byte came back with U+FFFD, both with the run reporting
# success. The Bash entry point does not read the exit code at all, so leaving
# the file alone is also what makes the two agree.
function Invoke-SettingsMerge([string]$TargetPath, [string]$SharedPath) {
  $helper = '.agent-config/repo/scripts/merge_settings.py'
  if (-not $pyCmd) { return $false }
  if (-not (Test-Path -LiteralPath $helper)) { return $false }
  try {
    $global:LASTEXITCODE = $null
    & $pyCmd.Path $helper $TargetPath $SharedPath
    if ($LASTEXITCODE -ne 0) {
      $reason = if ($LASTEXITCODE -eq 3) {
        "another writer held the lock for ${TargetPath}; target left unchanged"
      } else {
        "settings merge helper exited $LASTEXITCODE for ${TargetPath}; target left unchanged"
      }
      $script:SettingsMergeReason = $reason
      [Console]::Error.WriteLine("warning: $reason")
    }
    return $true
  } catch {
    return $false
  }
}

# Classify a JSON number the way Python does, for the comparison below.
# Returns @($isExactInteger, $bigIntegerValue, $doubleValue), or $null for
# anything that is not a number.
#
# The two editions do not even agree on the type: `ConvertFrom-Json` reads
# 9007199254740992.0 as a Double under PowerShell 7 and as a Decimal under
# Windows PowerShell 5.1, and 1e20 as a Double under both. The invariant string
# settles the integer case for every one of those, and Truncate settles the
# rest, so neither the type list nor the edition has to be enumerated.
function Get-PythonNumericKey($Value) {
  try {
    $text = $Value.ToString([System.Globalization.CultureInfo]::InvariantCulture)
    if ($text -match '^-?\d+$') {
      return @($true, [System.Numerics.BigInteger]::Parse($text), [double]$Value)
    }
    $asDouble = [double]$Value
    if ([double]::IsNaN($asDouble) -or [double]::IsInfinity($asDouble)) {
      return @($false, [System.Numerics.BigInteger]::Zero, $asDouble)
    }
    if ($asDouble -eq [System.Math]::Truncate($asDouble)) {
      return @($true, [System.Numerics.BigInteger]$asDouble, $asDouble)
    }
    return @($false, [System.Numerics.BigInteger]::Zero, $asDouble)
  } catch {
    return $null
  }
}

# Scalar equality as Python's dict keys see it, for the array dedup below.
# Python keys on the value with its type for strings but treats 1, 1.0 and
# True as one key; .NET boxed equality keeps those three apart.
function Test-PythonScalarEqual($Left, $Right) {
  if ($null -eq $Left -or $null -eq $Right) {
    return ($null -eq $Left -and $null -eq $Right)
  }
  if ($Left -is [string] -or $Right -is [string]) {
    return ($Left -is [string] -and $Right -is [string] -and $Left -ceq $Right)
  }
  # Python compares a bool as the integer 1 or 0, so True is 1 and 1.0 but not
  # 2. LanguagePrimitives converts toward the left operand's type instead, so
  # `Equals($true, 2)` converts 2 to a bool and answers true while
  # `Equals(2, $true)` compares numbers and answers false. The result depended
  # on which side of the merge a value arrived from, and in the order a real
  # settings file produces it dropped a value the helper keeps.
  if ($Left -is [bool]) { $Left = [int]$Left }
  if ($Right -is [bool]) { $Right = [int]$Right }
  # Python compares an integer and a float exactly. .NET widens the integer to
  # a double first, so 9007199254740993 equalled 9007199254740992.0 under
  # PowerShell 7, and 9223372036854775807 equalled its rounded double under
  # both editions, while dict.fromkeys keeps each pair apart. Compare exact
  # integers as integers and everything else as doubles.
  $leftKey = Get-PythonNumericKey $Left
  $rightKey = Get-PythonNumericKey $Right
  if ($null -eq $leftKey -or $null -eq $rightKey) {
    return [System.Management.Automation.LanguagePrimitives]::Equals($Left, $Right)
  }
  if ($leftKey[0] -ne $rightKey[0]) { return $false }
  if ($leftKey[0]) { return ($leftKey[1] -eq $rightKey[1]) }
  return ($leftKey[2] -eq $rightKey[2])
}

function Merge-Json($base, $over) {
  foreach ($p in $over.PSObject.Properties) {
    # The PSObject.Properties indexer matches case-insensitively on both
    # editions, so asking for `env` returns a property named `Env` and the
    # merge then writes the shared value under the existing spelling. The
    # Python path on the bash side keys on the exact string. Scan for a
    # case-exact match so the two agree.
    $b = $null
    foreach ($candidate in $base.PSObject.Properties) {
      if ($candidate.Name -ceq $p.Name) { $b = $candidate; break }
    }
    if ($b -and $b.Value -is [PSCustomObject] -and $p.Value -is [PSCustomObject]) {
      Merge-Json $b.Value $p.Value
    } elseif ($b -and $b.Value -is [Array] -and $p.Value -is [Array]) {
      # Arrays of objects (e.g., hooks): replace. Arrays of scalars: dedup.
      # `merge_settings.py` decides on the incoming list's FIRST element alone,
      # so a mixed list like ["a", {...}] takes the dedup path there. Scanning
      # every element for an object sent the same input down the replace path
      # here, and the two entry points then wrote different files.
      $firstIsObj = ($p.Value.Count -gt 0 -and $p.Value[0] -is [PSCustomObject])
      if ($firstIsObj) {
        $b.Value = $p.Value
      } else {
        # Deduplicate the way `dict.fromkeys` does in merge_settings.py.
        # Neither hash set works here. HashSet[string] stringifies, so 1 and
        # "1" collapse and $true becomes "True". HashSet[object] keeps 1, 1.0
        # and $true apart because their boxed .NET types differ, while Python
        # treats all three as one key. Compare pairwise instead: a string only
        # equals a string, case-sensitively as Python does, and everything else
        # goes through PowerShell's numeric conversion, which agrees with
        # Python on 1 == 1.0 == True.
        $m = @()
        foreach ($i in (@($b.Value) + @($p.Value))) {
          $duplicate = $false
          foreach ($seen in $m) {
            if (Test-PythonScalarEqual $seen $i) { $duplicate = $true; break }
          }
          if (-not $duplicate) { $m += ,$i }
        }
        $b.Value = $m
      }
    } elseif ($b) {
      # Assign in place rather than through Add-Member -Force, which removes
      # the property and appends it again. That moved an updated key to the end
      # of the object on every Windows run while the bash path left it where it
      # was, so the two entry points reordered the file against each other.
      $b.Value = $p.Value
    } else {
      # No case-exact match, but a case-different one may still exist, and
      # `Add-Member -Force` deletes it: base `Env` plus shared `env` would leave
      # `env` alone, silently dropping whatever `Env` held. A PSCustomObject
      # cannot carry both spellings at once, so keep the existing key and merge
      # into it. That loses no data, unlike -Force, though the Python path would
      # have kept the two apart. Say so, because the spelling then depends on
      # which entry point ran.
      $clash = $null
      foreach ($candidate in $base.PSObject.Properties) {
        if ($candidate.Name -eq $p.Name) { $clash = $candidate; break }
      }
      if ($clash) {
        Write-Warning ("settings merge: '{0}' and '{1}' differ only in case and PowerShell cannot hold both; merging into '{0}'. Install Python to keep them separate." -f $clash.Name, $p.Name)
        if ($clash.Value -is [PSCustomObject] -and $p.Value -is [PSCustomObject]) {
          Merge-Json $clash.Value $p.Value
        } else {
          $clash.Value = $p.Value
        }
      } else {
        $base | Add-Member -NotePropertyName $p.Name -NotePropertyValue $p.Value
      }
    }
  }
}

function Test-DisabledValue([string]$Value) {
  if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
  $normalized = $Value.Trim().ToLowerInvariant()
  return @('off', '0', 'disabled', 'false', 'no').Contains($normalized)
}

function Invoke-CodexAutoUpdate {
  if (Test-DisabledValue $env:ANYWHERE_AGENTS_CODEX_AUTO_UPDATE) { return }
  $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
  if (-not $npmCmd) { return }
  try {
    $npm = $npmCmd.Source
    $globalPrefix = (& $npm prefix -g 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($globalPrefix)) { return }
    $pkgJson = Join-Path (Join-Path (Join-Path $globalPrefix 'node_modules') '@openai') 'codex'
    $pkgJson = Join-Path $pkgJson 'package.json'
    if (-not (Test-Path $pkgJson)) { return }

    $outdatedRaw = (& $npm outdated -g '@openai/codex' --json 2>$null | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($outdatedRaw) -or $outdatedRaw -eq '{}') { return }
    try {
      $outdated = $outdatedRaw | ConvertFrom-Json
    } catch {
      return
    }
    $entry = $outdated.PSObject.Properties['@openai/codex']
    if (-not $entry) { return }
    $current = [string]$entry.Value.current
    $latest = [string]$entry.Value.latest
    if ([string]::IsNullOrWhiteSpace($latest) -or $latest -eq $current) { return }

    [Console]::Error.WriteLine("[anywhere-agents] updating Codex CLI @openai/codex $current -> $latest")
    & $npm install -g '@openai/codex@latest' --silent
    if ($LASTEXITCODE -ne 0) {
      [Console]::Error.WriteLine("[anywhere-agents] Codex CLI auto-update failed; run ``npm install -g @openai/codex@latest``")
    }
  } catch {
    # Do not let a local npm or registry problem block project bootstrap.
  }
}
New-Item -ItemType Directory -Force -Path .agent-config, .claude, .claude/commands | Out-Null
Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/$Upstream/main/AGENTS.md" -OutFile .agent-config/AGENTS.md

# Sparse clone moved up (before composing the root AGENTS.md): the rule-pack
# manifest and composer helper live inside .agent-config/repo/ and must be
# present before we branch on compose vs verbatim fallback.
$RepoUrl = "https://github.com/$Upstream.git"
if (Test-Path .agent-config/repo/.git) {
  git -C .agent-config/repo remote set-url origin $RepoUrl
  git -C .agent-config/repo pull --ff-only
} else {
  git clone --depth 1 --filter=blob:none --sparse $RepoUrl .agent-config/repo
}
git -C .agent-config/repo sparse-checkout set skills .claude scripts user bootstrap
try {
  Add-LedgerTarget '.agent-config/AGENTS.md'
  Add-LedgerTarget '.agent-config/repo'
  Add-LedgerStep 'fetch' 'repo' 'ok'
} catch {}

# Compose root AGENTS.md. Default-on: every aa consumer gets the agent-style
# writing rule pack unless they explicitly opt out via `rule_packs: []` in
# agent-config.yaml. Composition requires Python 3 + PyYAML; when PyYAML is
# missing we attempt a best-effort `pip install --user pyyaml`. If Python or
# PyYAML still are not available, we fall back to a marked upstream
# AGENTS.md and print a one-line tip unless the consumer has explicitly
# referenced rule_packs themselves.
$composeOk = $false
$composeSkipReason = ''
if (-not $pyCmd) {
    $composeSkipReason = 'no Python 3 interpreter found'
} else {
    if (-not (Test-PythonHasYaml $pyCmd.Path)) {
        [Console]::Error.WriteLine("installing PyYAML (enables agent-style rule-pack composition)...")
        $global:LASTEXITCODE = $null
        & $pyCmd.Path -m pip install --user --quiet pyyaml 2>$null
    }
    if (Test-PythonHasYaml $pyCmd.Path) {
        $composeOk = $true
    } else {
        $composeSkipReason = 'Python 3 interpreter has no PyYAML after install attempt'
    }
}

if ($composeOk -and -not (Test-Path .agent-config/repo/scripts/compose_packs.py) -and -not (Test-Path .agent-config/repo/scripts/compose_rule_packs.py)) {
    # Upstream sparse clone has no composer script (e.g. the ac source repo
    # itself, which intentionally ships only generate_agent_configs.py and
    # not the v0.4.0 unified composer). Fall through to the marked-AGENTS.md
    # path instead of crashing on a non-existent Python file.
    [Console]::Error.WriteLine("[anywhere-agents] no composer script in .agent-config/repo/scripts/; falling back to verbatim AGENTS.md")
    $composeOk = $false
    $composeSkipReason = 'no composer script in sparse clone'
}

if ($composeOk) {
    $composeArgs = @("--root", ".")
    if ($NoCache) { $composeArgs += "--no-cache" }
    # Prefer the v0.4.0 unified composer. Fall back to the v0.3.x rule-pack
    # composer on pre-v0.4.0 sparse clones that predate compose_packs.py.
    if (Test-Path .agent-config/repo/scripts/compose_packs.py) {
        $composer = ".agent-config/repo/scripts/compose_packs.py"
    } else {
        $composer = ".agent-config/repo/scripts/compose_rule_packs.py"
    }
    # v0.5.8: capture composer rc and always run generator so CLAUDE.md stays
    # coherent even when composition aborts (e.g. DriftAbort, OSError).
    $global:LASTEXITCODE = $null
    & $pyCmd.Path $composer @composeArgs
    $composerRc = if ($LASTEXITCODE -ne $null) { [int]$LASTEXITCODE } elseif (-not $?) { 1 } else { 0 }
    Invoke-AgentConfigGenerator $pyCmd
    if ($composerRc -ne 0) {
        [Console]::Error.WriteLine("[anywhere-agents] pack composition did not complete (rc=$composerRc); generated files (CLAUDE.md, agents/codex.md) refreshed from current AGENTS.md. Re-run ``anywhere-agents`` after addressing the failure.")
        try {
          Add-LedgerTarget 'AGENTS.md'
          Add-LedgerStep 'compose' 'repo' 'failed' $composerRc
          Add-GeneratorLedgerStep
        } catch {}
        Exit-Bootstrap $composerRc
    }
    try {
      Add-LedgerTarget 'AGENTS.md'
      Add-LedgerStep 'compose' 'repo' 'ok'
    } catch {}
} else {
    $composePreserved = $false
    $passiveRulePackConfigured = Test-PassiveRulePackConfigured
    # Gated on a configured selection; see the bash half. An explicit
    # `rule_packs: []` must still restore the upstream file.
    if ($passiveRulePackConfigured -and (Test-AgentsMdIsComposed)) {
      # Composition cannot run, and the AGENTS.md on disk is a composed
      # artifact. Replacing it with the un-composed upstream copy deletes every
      # pack block, and where the file is tracked git then records that
      # deletion as intent. Keep the last good artifact. This check reads the
      # file rather than the configuration, so it still holds when the
      # configuration is misread, which is how the pack blocks were lost in the
      # first place.
      $composePreserved = $true
    } elseif ($passiveRulePackConfigured) {
      $fallbackMarker = "<!-- rule-pack composition skipped: $composeSkipReason; run anywhere-agents to compose -->"
      $upstreamAgents = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath .agent-config/AGENTS.md))
      [System.IO.File]::WriteAllText(
        (Join-Path (Get-Location).Path 'AGENTS.md'),
        $fallbackMarker + "`n" + $upstreamAgents,
        (New-Object System.Text.UTF8Encoding $false)
      )
      # The marker goes on every skip, because the artifact must never be
      # mistakable for a composed one. completed:false is narrower: it means
      # the run did not do its job and someone can act. Missing Python or
      # PyYAML is actionable. An upstream that ships no composer is a property
      # of that upstream, and agent-config deliberately ships only the
      # generator, so flagging it would mark every bootstrap from an ac-shaped
      # remote as incomplete forever with nothing to fix. pack verify still
      # reports those packs as registered rather than composed.
      if ($composeSkipReason -ne 'no composer script in sparse clone') {
        $script:LedgerIncomplete = $true
      }
    } else {
      Copy-Item .agent-config/AGENTS.md AGENTS.md -Force
    }
    if ($composePreserved) {
      [Console]::Error.WriteLine("")
      [Console]::Error.WriteLine("warning: composition was skipped ($composeSkipReason) and the AGENTS.md on disk is a")
      [Console]::Error.WriteLine("         composed artifact, so this run left it untouched rather than")
      [Console]::Error.WriteLine("         replacing it with the un-composed upstream copy.")
      [Console]::Error.WriteLine("         Its pack blocks are whatever the last successful composition")
      [Console]::Error.WriteLine("         produced; upstream changes reach this file only once")
      [Console]::Error.WriteLine("         composition runs again.")
      if ($composeSkipReason -ne 'no composer script in sparse clone') {
        $script:LedgerIncomplete = $true
      }
    }
    # Awareness is a different question from selection: `packs: []` is an
    # opt-out and still means the operator knows about packs. It reads the same
    # four layers the selection gate does, so a consumer whose only mention is
    # user-level, or who uses the canonical env var, is not told that packs
    # were skipped when they were never asked for.
    $trackedState = Get-RulePacksConfigState 'agent-config.yaml'
    $localState = Get-RulePacksConfigState 'agent-config.local.yaml'
    $userState = Get-RulePacksConfigState (Get-UserConfigPath)
    $rpAware = ($trackedState -ne 'none' -or $localState -ne 'none' -or $userState -ne 'none' -or [bool]$env:AGENT_CONFIG_PACKS -or [bool]$env:AGENT_CONFIG_RULE_PACKS)
    # The tip tells the operator the writing rules are absent. When the composed
    # artifact was preserved they are present, so the tip would be wrong.
    if ($composePreserved) { $rpAware = $true }
    if (-not $rpAware) {
        [Console]::Error.WriteLine("")
        [Console]::Error.WriteLine("tip: anywhere-agents ships with agent-style writing rules enabled by default,")
        [Console]::Error.WriteLine("     but this run skipped them ($composeSkipReason).")
        [Console]::Error.WriteLine("     install Python + PyYAML to enable, or silence with 'rule_packs: []' in agent-config.yaml.")
    }
    try {
      Add-LedgerTarget 'AGENTS.md'
      if ($composePreserved) {
        Add-LedgerStep 'compose' 'repo' 'skipped' -Reason "$composeSkipReason; existing composed AGENTS.md preserved"
      } else {
        Add-LedgerStep 'compose' 'repo' 'skipped' -Reason $composeSkipReason
      }
    } catch {}
}
# Generate per-agent config files (CLAUDE.md, agents/codex.md) from AGENTS.md.
# Generator preserves hand-authored files (no GENERATED header) and warns loudly.
# v0.5.8: in the composeOk path the generator already ran above. Only run here
# for the fallback path (Python/PyYAML unavailable) where $composeOk is false.
if (-not $composeOk) {
  Invoke-AgentConfigGenerator $pyCmd
}
Add-GeneratorLedgerStep
if (Test-Path .agent-config/repo/.claude/commands) {
  Copy-Item .agent-config/repo/.claude/commands/*.md .claude/commands/ -Force
  try { Add-LedgerTarget '.claude/commands' } catch {}
}
if (Test-Path .agent-config/repo/.claude/settings.json) {
  # The project phase owns this variable from here to its ledger step below.
  $script:SettingsMergeReason = $null
  if (Test-Path .claude/settings.json) {
    if (-not (Invoke-SettingsMerge '.claude/settings.json' '.agent-config/repo/.claude/settings.json')) {
      # Unreadable input leaves the file alone, which is what the helper does
      # with the same bytes. Without this the throw would abort the run partway
      # through, on a machine that has no Python to fall back from.
      try {
        $shared = Read-JsonFileUtf8 .agent-config/repo/.claude/settings.json
        $project = Read-JsonFileUtf8 .claude/settings.json
        Merge-Json $project $shared
        # The helper's rule, so both entry points refuse the same result.
        if (@($project.PSObject.Properties).Count -eq 0) {
          throw 'refusing to write an empty object'
        }
        [void](Publish-SettingsIfChanged (Join-Path (Get-Location).Path '.claude/settings.json') (ConvertTo-CanonicalJson $project))
      } catch {
        $script:SettingsMergeReason = "could not merge .claude/settings.json: $($_.Exception.Message)"
        [Console]::Error.WriteLine("warning: $script:SettingsMergeReason")
      }
    }
  } else {
    Copy-Item .agent-config/repo/.claude/settings.json .claude/settings.json -Force
  }
  try { Add-LedgerTarget '.claude/settings.json' } catch {}
}
try {
  if ($script:SettingsMergeReason) {
    Add-LedgerStep 'project_files' 'repo' 'failed' $null $script:SettingsMergeReason
  } else {
    Add-LedgerStep 'project_files' 'repo' 'ok'
  }
} catch {}
# --- User-level setup: hooks and settings ---
# This section modifies ~/.claude/ (user-level, not project-level).
# It deploys a PreToolUse hook guard and merges shared permission settings.
# Remove this section if you do not want bootstrap to modify user-level config.
$userClaude = Join-Path $env:USERPROFILE '.claude'
$hooksDir = Join-Path $userClaude 'hooks'
Deploy-UserHelper .agent-config/repo/scripts/_python (Join-Path $hooksDir '_python') '~/.claude/hooks/_python'
Deploy-UserHelper .agent-config/repo/scripts/guard.py (Join-Path $hooksDir 'guard.py') '~/.claude/hooks/guard.py'
Deploy-UserHelper .agent-config/repo/scripts/session_bootstrap.py (Join-Path $hooksDir 'session_bootstrap.py') '~/.claude/hooks/session_bootstrap.py'
Deploy-UserHelper .agent-config/repo/scripts/statusline.py (Join-Path $userClaude 'statusline.py') '~/.claude/statusline.py'
Deploy-UserHelper .agent-config/repo/scripts/agent-quota.py (Join-Path $userClaude 'agent-quota.py') '~/.claude/agent-quota.py'
if (Test-Path .agent-config/repo/user/settings.json) {
  New-Item -ItemType Directory -Force -Path $userClaude | Out-Null
  # The project phase has already recorded its own outcome, so the user phase
  # starts clean rather than inheriting a project failure.
  $script:SettingsMergeReason = $null
  $userSettings = Join-Path $userClaude 'settings.json'
  if (Invoke-SettingsMerge $userSettings '.agent-config/repo/user/settings.json') {
    # The helper ran and took the lock itself; its exit code already decided
    # what the ledger records.
  } else {
    # No Python, so this shell does the merge. The existence check belongs
    # inside the lock with the write: deciding absence outside it is how an
    # installer overwrites a peer's finished merge.
    $lock = Open-SettingsLock $userSettings
    if (-not $lock) {
      $script:SettingsMergeReason = "could not take the settings lock for ${userSettings} (a peer may hold it); target left unchanged"
      [Console]::Error.WriteLine("warning: $script:SettingsMergeReason")
    } else {
      try {
        if (Test-Path $userSettings) {
          try {
            $shared = Read-JsonFileUtf8 .agent-config/repo/user/settings.json
            $existing = Read-JsonFileUtf8 $userSettings
            Merge-Json $existing $shared
            if (@($existing.PSObject.Properties).Count -eq 0) {
              throw 'refusing to write an empty object'
            }
            [void](Publish-SettingsIfChanged $userSettings (ConvertTo-CanonicalJson $existing) -KeepBackup)
          } catch {
            $script:SettingsMergeReason = "could not merge ${userSettings}: $($_.Exception.Message)"
            [Console]::Error.WriteLine("warning: $script:SettingsMergeReason")
          }
        } else {
          # First install publishes by rename too. Copy-Item -Force truncates
          # the destination before writing. Copy-HelperAtomic signals failure by
          # throwing, so the ledger only hears about it if this catches.
          try {
            [void](Copy-HelperAtomic .agent-config/repo/user/settings.json $userSettings)
          } catch {
            $script:SettingsMergeReason = "could not install ${userSettings}: $($_.Exception.Message)"
            [Console]::Error.WriteLine("warning: $script:SettingsMergeReason")
          }
        }
      } finally {
        Close-SettingsLock $lock
      }
    }
  }
  try { Add-LedgerTarget '~/.claude/settings.json' } catch {}
}
# Heal legacy autoUpdates: false in ~/.claude.json. When the flag was already
# false at Claude Code native-install launch, the updater daemon never spawns
# (autoUpdatesProtectedForNative does not actually neutralize it in that path).
# To genuinely disable auto-updates, use DISABLE_AUTOUPDATER=1 via the env
# block in ~/.claude/settings.json; that takes precedence regardless.
$claudeJson = Join-Path $env:USERPROFILE '.claude.json'
if (Test-Path $claudeJson) {
  try {
    $claudeState = Read-JsonFileUtf8 $claudeJson
    if ($claudeState.PSObject.Properties['autoUpdates'] -and $claudeState.autoUpdates -eq $false) {
      $claudeState.autoUpdates = $true
      # Best-effort heal. Atomic replace (staged temp + Move-Item -Force) prevents
      # a truncated config if this process is interrupted mid-write. It is NOT a
      # cross-process lock: a concurrent Claude Code write that lands between our
      # read and replace will still be clobbered by our older snapshot. The
      # healed flag persists on the next session if Claude Code re-wrote with
      # the stale value. Key ordering may change during the round trip; Claude
      # Code reads by key so this is acceptable. Unique GUID suffix avoids
      # concurrent-bootstrap temp-path collisions.
      $tmp = Join-Path (Split-Path $claudeJson) (".claude.json.{0}.tmp" -f [guid]::NewGuid().ToString("N"))
      Write-JsonFileUtf8 $tmp (ConvertTo-CanonicalJson $claudeState)
      Move-Item -Force $tmp $claudeJson
    }
  } catch {
    # ~/.claude.json is runtime-managed by Claude Code; skip on any read/parse error.
    if ($tmp -and (Test-Path $tmp)) { Remove-Item -Force $tmp }
  }
  try { Add-LedgerTarget '~/.claude.json' } catch {}
}
try {
  if ($script:SettingsMergeReason) {
    Add-LedgerStep 'user_files' 'user' 'failed' $null $script:SettingsMergeReason
  } else {
    Add-LedgerStep 'user_files' 'user' 'ok'
  }
} catch {}

# Codex CLI has no native updater like Claude Code. If Codex is installed as
# the global npm package that this config recommends, keep it current during
# bootstrap. Set ANYWHERE_AGENTS_CODEX_AUTO_UPDATE=off to disable.
Invoke-CodexAutoUpdate
# No target here: Invoke-CodexAutoUpdate no-ops when Codex is not the global
# npm install or when ANYWHERE_AGENTS_CODEX_AUTO_UPDATE=off, so naming the
# package would imply an update that may not have happened.
try {
  Add-LedgerStep 'external' 'external' 'ok'
} catch {}

Add-GitignoreEntry '^\/?\.agent-config/' '.agent-config/'
# Rule-pack opt-in writes agent-config.local.yaml as a machine-local override
# that must not be committed. Auto-ignore it idempotently alongside .agent-config/.
Add-GitignoreEntry '^\/?agent-config\.local\.yaml$' 'agent-config.local.yaml'
# See the matching block in bootstrap.sh for why the three generated files are
# ignored, why an already-tracked one is left alone, and why the entries are
# anchored and name codex.md rather than the agents/ directory.
if (-not $env:AGENT_CONFIG_TRACK_GENERATED) {
  foreach ($generated in @('AGENTS.md', 'CLAUDE.md', 'agents/codex.md')) {
    if (-not (Test-GitTracks $generated)) {
      Add-GitignoreEntry ("^/?" + [regex]::Escape($generated) + "$") ("/" + $generated)
    }
  }
}
# The todo/ drop box. Same contract as the Bash entry point: seed when absent,
# never rewrite an existing README, and pair `todo/*` with the negation so the
# folder survives a fresh clone. See the block above the matching code in
# bootstrap.sh for why the directory form of the pattern cannot be used.
if (-not $env:AGENT_CONFIG_NO_TODO_DROPBOX) {
  $todoRoot = Join-Path (Get-Location).Path 'todo'
  $todoReadme = Join-Path $todoRoot 'README.md'
  $todoSource = '.agent-config/repo/bootstrap/todo-readme.md'
  # A linked todo belongs to whoever linked it; see the matching block in
  # bootstrap.sh. Get-Item needs -Force because a link can carry Hidden, and
  # ReparsePoint is the attribute both PowerShell editions report for a
  # symbolic link and for a directory junction.
  $todoItem = Get-Item -LiteralPath $todoRoot -Force -ErrorAction SilentlyContinue
  $todoIsLink = ($null -ne $todoItem -and
    (($todoItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0))
  # -PathType Leaf and -ErrorAction Stop both matter. Without the first, a
  # directory named like the template would pass the probe; without the second
  # these cmdlets report a non-terminating error, skip the catch, and let the
  # run continue as though the seed had worked.
  if ((-not $todoIsLink) -and
      (-not (Test-Path -LiteralPath $todoReadme)) -and
      (Test-Path -LiteralPath $todoSource -PathType Leaf)) {
    try {
      New-Item -ItemType Directory -Force -Path 'todo' -ErrorAction Stop | Out-Null
      Copy-Item -LiteralPath $todoSource -Destination $todoReadme -Force -ErrorAction Stop
    } catch {
      [Console]::Error.WriteLine('warning: could not seed todo/README.md')
    }
  }
  # The README, not the directory: `Test-Path 'todo'` is true for a plain file
  # of that name, where the Bash half's `[ -d todo ]` is false, and the two
  # entry points then disagreed about whether to write ignore rules and a
  # ledger target for a README that does not exist.
  if ((-not $todoIsLink) -and
      (Test-Path -LiteralPath $todoReadme -PathType Leaf)) {
    Add-GitignoreEntry '^/?todo/\*$' 'todo/*'
    Add-GitignoreEntry '^!/?todo/README\.md$' '!todo/README.md'
    # git applies the last matching rule, so a negation above its exclusion
    # does nothing. See the matching block in bootstrap.sh.
    $gi = Join-Path (Get-Location).Path '.gitignore'
    if (Test-Path -LiteralPath $gi) {
      $giLines = [System.IO.File]::ReadAllLines($gi)
      $lastExclude = -1
      $lastNegate = -1
      for ($i = 0; $i -lt $giLines.Length; $i++) {
        if ($giLines[$i] -cmatch '^/?todo/\*$') { $lastExclude = $i }
        if ($giLines[$i] -cmatch '^!/?todo/README\.md$') { $lastNegate = $i }
      }
      if ($lastExclude -ge 0 -and $lastNegate -ge 0 -and $lastNegate -lt $lastExclude) {
        Add-GitignoreLine '!todo/README.md'
      }
    }
    Add-LedgerTarget 'todo/README.md'
  }
}

# Self-update: copy the latest bootstrap script from the sparse clone over this
# one. Without this, a consumer that initially fetched an older bootstrap.ps1
# stays on that version forever; future bootstrap improvements added upstream
# (e.g. the 2026-04-16 generator step) would never reach them automatically.
#
# v0.5.2 cross-OS fix: copy BOTH bootstrap.ps1 AND bootstrap.sh. Previously
# the .ps1 entry only refreshed itself, so a developer who later switched
# to Git Bash / WSL on the same project would hit "No such file or
# directory" on .agent-config/bootstrap.sh. Symmetric in bootstrap.sh.
if (Test-Path .agent-config/repo/bootstrap/bootstrap.ps1) {
  try {
    Copy-Item .agent-config/repo/bootstrap/bootstrap.ps1 .agent-config/bootstrap.ps1 -Force -ErrorAction Stop
  } catch {
    Write-Warning "Could not self-update .agent-config/bootstrap.ps1: $($_.Exception.Message)"
  }
}
if (Test-Path .agent-config/repo/bootstrap/bootstrap.sh) {
  try {
    Copy-Item .agent-config/repo/bootstrap/bootstrap.sh .agent-config/bootstrap.sh -Force -ErrorAction Stop
  } catch {
    Write-Warning "Could not copy .agent-config/bootstrap.sh: $($_.Exception.Message)"
  }
}
try {
  Add-LedgerTarget '.gitignore'
  Add-LedgerTarget '.agent-config/bootstrap.sh'
  Add-LedgerTarget '.agent-config/bootstrap.ps1'
  Add-LedgerStep 'finalize' 'repo' 'ok'
  Write-Ledger 'finalize' (-not $script:LedgerIncomplete)
} catch {}
Invoke-BannerRender 0
