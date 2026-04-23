# Line endings are handled by this repo's .gitattributes. Bootstrap intentionally
# avoids changing user-level Git configuration.

param(
    [Parameter(Position=0)][string]$Upstream,
    [string]$RulePacks,
    [switch]$NoCache,
    [Alias("h")][switch]$Help
)

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
      # ref: v0.3.2

After committing agent-config.yaml, run:

  .\bootstrap.ps1
"@
    Write-Output $snippet
    exit 0
}

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

function Merge-Json($base, $over) {
  foreach ($p in $over.PSObject.Properties) {
    $b = $base.PSObject.Properties[$p.Name]
    if ($b -and $b.Value -is [PSCustomObject] -and $p.Value -is [PSCustomObject]) {
      Merge-Json $b.Value $p.Value
    } elseif ($b -and $b.Value -is [Array] -and $p.Value -is [Array]) {
      # Arrays of objects (e.g., hooks): replace. Arrays of strings: dedup.
      $hasObj = $false; foreach ($el in $p.Value) { if ($el -is [PSCustomObject]) { $hasObj = $true; break } }
      if ($hasObj) {
        $base | Add-Member -NotePropertyName $p.Name -NotePropertyValue $p.Value -Force
      } else {
        $s = [System.Collections.Generic.HashSet[string]]::new()
        $m = @(); foreach ($i in $b.Value) { if ($s.Add($i)) { $m += $i } }
        foreach ($i in $p.Value) { if ($s.Add($i)) { $m += $i } }
        $base | Add-Member -NotePropertyName $p.Name -NotePropertyValue $m -Force
      }
    } else {
      $base | Add-Member -NotePropertyName $p.Name -NotePropertyValue $p.Value -Force
    }
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

# Compose root AGENTS.md. Default-on: every aa consumer gets the agent-style
# writing rule pack unless they explicitly opt out via `rule_packs: []` in
# agent-config.yaml. Composition requires Python 3 + PyYAML; when PyYAML is
# missing we attempt a best-effort `pip install --user pyyaml`. If Python or
# PyYAML still aren't available, we fall back to the verbatim upstream
# AGENTS.md and print a one-line tip unless the consumer has explicitly
# referenced rule_packs themselves.
# Verify a candidate python actually executes before trusting it. On default
# Windows installs, `Get-Command python` can resolve to the WindowsApps shim
# at C:\...\WindowsApps\...\python.exe which fails at launch without setting
# $LASTEXITCODE the way this script would otherwise rely on. Probe every
# candidate with an import-and-exit test, and gate subsequent checks on both
# $? (native launch success) AND $LASTEXITCODE (the program's own exit).
function Test-PythonRuns([string]$PythonPath) {
    $global:LASTEXITCODE = $null
    & $PythonPath -c "import sys; raise SystemExit(0 if sys.version_info[0] >= 3 else 1)" 2>$null
    return ($? -and $LASTEXITCODE -eq 0)
}

function Test-PythonHasYaml([string]$PythonPath) {
    $global:LASTEXITCODE = $null
    & $PythonPath -c "import yaml" 2>$null
    return ($? -and $LASTEXITCODE -eq 0)
}

$pyCmd = $null
foreach ($name in @("python", "python3")) {
    $candidate = Get-Command $name -ErrorAction SilentlyContinue
    if ($candidate -and (Test-PythonRuns $candidate.Path)) {
        $pyCmd = $candidate
        break
    }
}

$composeOk = $false
if ($pyCmd) {
    if (-not (Test-PythonHasYaml $pyCmd.Path)) {
        [Console]::Error.WriteLine("installing PyYAML (enables agent-style rule-pack composition)...")
        $global:LASTEXITCODE = $null
        & $pyCmd.Path -m pip install --user --quiet pyyaml 2>$null
    }
    if (Test-PythonHasYaml $pyCmd.Path) {
        $composeOk = $true
    }
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
    $global:LASTEXITCODE = $null
    & $pyCmd.Path $composer @composeArgs
    if (-not $? -or $LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine("error: pack composition failed; AGENTS.md not updated")
        exit 1
    }
} else {
    Copy-Item .agent-config/AGENTS.md AGENTS.md -Force
    $rpAware = $false
    if ((Test-Path agent-config.yaml) -and (Select-String -Quiet -Pattern '^rule_packs:' agent-config.yaml)) {
        $rpAware = $true
    } elseif ((Test-Path agent-config.local.yaml) -and (Select-String -Quiet -Pattern '^rule_packs:' agent-config.local.yaml)) {
        $rpAware = $true
    } elseif ($env:AGENT_CONFIG_RULE_PACKS) {
        $rpAware = $true
    }
    if (-not $rpAware) {
        [Console]::Error.WriteLine("")
        [Console]::Error.WriteLine("tip: anywhere-agents ships with agent-style writing rules enabled by default,")
        [Console]::Error.WriteLine("     but this run skipped them (Python 3 with PyYAML unavailable).")
        [Console]::Error.WriteLine("     install Python + PyYAML to enable, or silence with 'rule_packs: []' in agent-config.yaml.")
    }
}
# Generate per-agent config files (CLAUDE.md, agents/codex.md) from AGENTS.md.
# Generator preserves hand-authored files (no GENERATED header) and warns loudly.
if (Test-Path .agent-config/repo/scripts/generate_agent_configs.py) {
  $genPy = Get-Command python -ErrorAction SilentlyContinue
  if (-not $genPy) { $genPy = Get-Command python3 -ErrorAction SilentlyContinue }
  if ($genPy) {
    & $genPy.Path .agent-config/repo/scripts/generate_agent_configs.py --root . --quiet
  }
}
if (Test-Path .agent-config/repo/.claude/commands) {
  Copy-Item .agent-config/repo/.claude/commands/*.md .claude/commands/ -Force
}
if (Test-Path .agent-config/repo/.claude/settings.json) {
  if (Test-Path .claude/settings.json) {
    $shared = Get-Content .agent-config/repo/.claude/settings.json -Raw | ConvertFrom-Json
    $project = Get-Content .claude/settings.json -Raw | ConvertFrom-Json
    Merge-Json $project $shared
    $project | ConvertTo-Json -Depth 10 | Set-Content .claude/settings.json
  } else {
    Copy-Item .agent-config/repo/.claude/settings.json .claude/settings.json -Force
  }
}
# --- User-level setup: hooks and settings ---
# This section modifies ~/.claude/ (user-level, not project-level).
# It deploys a PreToolUse hook guard and merges shared permission settings.
# Remove this section if you do not want bootstrap to modify user-level config.
$userClaude = Join-Path $env:USERPROFILE '.claude'
if (Test-Path .agent-config/repo/scripts/guard.py) {
  $hooksDir = Join-Path $userClaude 'hooks'
  New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null
  Copy-Item .agent-config/repo/scripts/guard.py (Join-Path $hooksDir 'guard.py') -Force
}
if (Test-Path .agent-config/repo/scripts/session_bootstrap.py) {
  $hooksDir = Join-Path $userClaude 'hooks'
  New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null
  Copy-Item .agent-config/repo/scripts/session_bootstrap.py (Join-Path $hooksDir 'session_bootstrap.py') -Force
}
if (Test-Path .agent-config/repo/user/settings.json) {
  New-Item -ItemType Directory -Force -Path $userClaude | Out-Null
  $userSettings = Join-Path $userClaude 'settings.json'
  if (Test-Path $userSettings) {
    $shared = Get-Content .agent-config/repo/user/settings.json -Raw | ConvertFrom-Json
    $existing = Get-Content $userSettings -Raw | ConvertFrom-Json
    Merge-Json $existing $shared
    $existing | ConvertTo-Json -Depth 10 | Set-Content $userSettings
  } else {
    Copy-Item .agent-config/repo/user/settings.json $userSettings -Force
  }
}
# Heal legacy autoUpdates: false in ~/.claude.json. When the flag was already
# false at Claude Code native-install launch, the updater daemon never spawns
# (autoUpdatesProtectedForNative does not actually neutralize it in that path).
# To genuinely disable auto-updates, use DISABLE_AUTOUPDATER=1 via the env
# block in ~/.claude/settings.json; that takes precedence regardless.
$claudeJson = Join-Path $env:USERPROFILE '.claude.json'
if (Test-Path $claudeJson) {
  try {
    $claudeState = Get-Content $claudeJson -Raw | ConvertFrom-Json
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
      $claudeState | ConvertTo-Json -Depth 20 | Set-Content $tmp
      Move-Item -Force $tmp $claudeJson
    }
  } catch {
    # ~/.claude.json is runtime-managed by Claude Code; skip on any read/parse error.
    if ($tmp -and (Test-Path $tmp)) { Remove-Item -Force $tmp }
  }
}

if (-not (Test-Path .gitignore) -or -not (Select-String -Quiet -Pattern '^\/?\.agent-config/' .gitignore)) {
  Add-Content -Path .gitignore -Value "`n.agent-config/"
}
# Rule-pack opt-in writes agent-config.local.yaml as a machine-local override
# that must not be committed. Auto-ignore it idempotently alongside .agent-config/.
if (-not (Test-Path .gitignore) -or -not (Select-String -Quiet -Pattern '^\/?agent-config\.local\.yaml$' .gitignore)) {
  Add-Content -Path .gitignore -Value "`nagent-config.local.yaml"
}
# Self-update: copy the latest bootstrap script from the sparse clone over this
# one. Without this, a consumer that initially fetched an older bootstrap.ps1
# stays on that version forever; future bootstrap improvements added upstream
# (e.g. the 2026-04-16 generator step) would never reach them automatically.
if (Test-Path .agent-config/repo/bootstrap/bootstrap.ps1) {
  try {
    Copy-Item .agent-config/repo/bootstrap/bootstrap.ps1 .agent-config/bootstrap.ps1 -Force -ErrorAction Stop
  } catch {
    Write-Warning "Could not self-update .agent-config/bootstrap.ps1: $($_.Exception.Message)"
  }
}
