# NotNativeMemory - Windows Uninstaller
# Run: powershell -ExecutionPolicy Bypass -File uninstall_windows.ps1
#
# Reads .install-manifest.json to determine what was installed and only
# removes those components. Safe to run multiple times.

param(
    [switch]$Full  # Also remove Docker volume (destroys all stored memories)
)

$ErrorActionPreference = "Continue"

# Match the installer's clean-output convention. See install_windows.ps1
# for the rationale (Starlette / docker compose stderr renders in red
# under the legacy console codepage when wrapped as ErrorRecord).
$env:BUILDKIT_PROGRESS = "plain"

function Write-Step($msg) { Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "[x] $msg" -ForegroundColor Red }
function Write-Info($msg) { Write-Host "    $msg" }

# Run a native command and render ALL output (stdout + stderr) as
# plain text in the default terminal color, with $LASTEXITCODE
# preserved. Same helper as the installer.
function Invoke-Native {
    if ($args.Count -lt 1) { return }
    $cmd = $args[0]
    $cmdArgs = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }
    & $cmd @cmdArgs 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) {
            Write-Host $_.Exception.Message
        } else {
            Write-Host $_
        }
    }
}

$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $SCRIPT_DIR
$MANIFEST_FILE = ".install-manifest.json"

Write-Host ""
Write-Host "+==========================================+"
Write-Host "|  NotNativeMemory - Uninstaller           |"
Write-Host "+==========================================+"
Write-Host ""

# -----------------------------------------------------------------------
# 1. Load manifest, or fall back to best-effort cleanup
# -----------------------------------------------------------------------
# The "no manifest" path is exactly when uninstall is most needed: an
# install crashed mid-flow before the manifest got written. Refusing
# to run there is unhelpful. Fall back to a conservative best-effort
# cleanup that handles the docker side (always safe, idempotent) and
# leaves anything we cannot positively identify (hooks in
# ~/.claude/settings.json, host-python server) alone.
$missingManifest = $false
$manifest = $null
if (Test-Path $MANIFEST_FILE) {
    try {
        $manifest = Get-Content $MANIFEST_FILE -Raw | ConvertFrom-Json
    } catch {
        Write-Warn "Manifest exists but is corrupt. Treating as missing."
        $missingManifest = $true
    }
} else {
    $missingManifest = $true
}

if ($missingManifest) {
    Write-Warn "No install manifest. Running best-effort cleanup based on what we can see on disk."
    Write-Info "Will:    stop docker containers, remove the built mcp image,"
    Write-Info "         optionally remove docker/postgres/ if -Full is passed."
    Write-Info "Will NOT touch hooks (~/.claude/settings.json), .env, or any host-python server."
    Write-Info "Manual cleanup may still be needed for those."
    # Synthesize the minimum component set so the docker-cleanup branch
    # below executes. install_mode stays "(unknown)" for the summary.
    $installMode = "(unknown)"
    $components = @("docker", "database")
} else {
    $installMode = $manifest.install_mode
    $components = $manifest.components
}

Write-Info "Install mode: $installMode"
Write-Info "Components: $($components -join ', ')"
Write-Host ""

# Confirm
$confirm = Read-Host "  Proceed with uninstall? [y/N]"
if ($confirm -notin @("y", "Y", "yes", "Yes")) {
    Write-Info "Cancelled."
    exit 0
}
Write-Host ""

# -----------------------------------------------------------------------
# 2. Stop MCP server (if running) - only when manifest tells us it
# was a host-python install. Without a manifest we cannot tell host
# from docker, so we skip and let the docker-down step below cover
# the docker case.
# -----------------------------------------------------------------------
if (-not $missingManifest -and $components -contains "server") {
    if ($components -contains "docker") {
        # Dockerized install - server is a container, stopped in step 4
        Write-Info "MCP server runs in Docker (will be stopped with containers)"
    } else {
        # Bare-metal install - server is a Python process
        Write-Step "Stopping MCP server..."
        if (Test-Path ".mcp-server.pid") {
            Invoke-Native python server.py --stop
            if ($LASTEXITCODE -eq 0) {
                Write-Info "Server stopped"
            } else {
                Write-Warn "Server may not have been running"
            }
        } else {
            Write-Info "No running server found"
        }
    }
}

# -----------------------------------------------------------------------
# 3. Remove Claude Code hooks - only when we have a manifest to
# anchor "ours" vs "someone else's". Without it, leave settings.json
# untouched rather than guessing.
# -----------------------------------------------------------------------
if (-not $missingManifest -and $components -contains "hooks") {
    Write-Step "Removing Claude Code hooks..."
    $settingsFile = Join-Path $env:USERPROFILE ".claude\settings.json"

    if (Test-Path $settingsFile) {
        try {
            $settings = Get-Content $settingsFile -Raw | ConvertFrom-Json

            $changed = $false

            # Remove our PreToolUse entries
            if ($settings.hooks -and $settings.hooks.PreToolUse) {
                $filtered = @()
                foreach ($group in $settings.hooks.PreToolUse) {
                    $isOurs = $false
                    foreach ($hook in $group.hooks) {
                        if ($hook.command -and $hook.command -match "memory_inject\.py") {
                            $isOurs = $true
                            break
                        }
                    }
                    if (-not $isOurs) { $filtered += $group }
                }
                if ($filtered.Count -ne @($settings.hooks.PreToolUse).Count) {
                    $settings.hooks.PreToolUse = $filtered
                    $changed = $true
                    Write-Info "Removed PreToolUse hook"
                }
            }

            # Remove our PreCompact entries
            if ($settings.hooks -and $settings.hooks.PreCompact) {
                $filtered = @()
                foreach ($group in $settings.hooks.PreCompact) {
                    $isOurs = $false
                    foreach ($hook in $group.hooks) {
                        if ($hook.command -and $hook.command -match "compact_guard\.py") {
                            $isOurs = $true
                            break
                        }
                    }
                    if (-not $isOurs) { $filtered += $group }
                }
                if ($filtered.Count -ne @($settings.hooks.PreCompact).Count) {
                    $settings.hooks.PreCompact = $filtered
                    $changed = $true
                    Write-Info "Removed PreCompact hook"
                }
            }

            # Clean up empty hook arrays and the hooks object itself
            if ($settings.hooks) {
                $emptyKeys = @()
                foreach ($key in @($settings.hooks.PSObject.Properties.Name)) {
                    if (@($settings.hooks.$key).Count -eq 0) {
                        $emptyKeys += $key
                    }
                }
                foreach ($key in $emptyKeys) {
                    $settings.hooks.PSObject.Properties.Remove($key)
                }
                if (@($settings.hooks.PSObject.Properties).Count -eq 0) {
                    $settings.PSObject.Properties.Remove("hooks")
                }
            }

            if ($changed) {
                $settings | ConvertTo-Json -Depth 10 | Set-Content -Path $settingsFile -Encoding UTF8
                Write-Info "Saved $settingsFile"
            } else {
                Write-Info "No hooks to remove (already clean)"
            }
        } catch {
            Write-Warn "Could not parse settings.json. Manual cleanup may be needed."
        }
    } else {
        Write-Info "No settings.json found"
    }
}

# -----------------------------------------------------------------------
# 4. Stop and remove Docker containers
# -----------------------------------------------------------------------
if ($components -contains "database" -or $components -contains "docker") {
    Write-Step "Stopping Docker containers..."
    # `--env-file .env` only when .env exists (a crashed install may
    # have left it absent and compose errors on a missing env file).
    # `--profile '*'` matches both full and server profiles so one
    # `down` covers either install shape.
    if (Test-Path ".env") {
        Invoke-Native docker compose --progress=plain --env-file .env -f docker/docker-compose.yml --profile '*' down
    } else {
        Invoke-Native docker compose --progress=plain -f docker/docker-compose.yml --profile '*' down
    }
    if ($LASTEXITCODE -eq 0) {
        Write-Info "Containers stopped"
    } else {
        Write-Warn "docker compose down exited $LASTEXITCODE; some containers may still be running."
    }

    # Remove the built MCP image. Harmless if missing or if another
    # container is using it (rmi just returns non-zero in that case).
    if ($components -contains "docker") {
        Invoke-Native docker rmi notnative-memory-mcp
        if ($LASTEXITCODE -eq 0) {
            Write-Info "MCP server image removed"
        } else {
            Write-Info "MCP server image not removed (already absent or in use)"
        }
    }

    if ($Full) {
        Write-Warn "Full mode: removing database data (ALL MEMORIES WILL BE DELETED)"
        $purgeConfirm = Read-Host "  Are you sure? This cannot be undone. [y/N]"
        if ($purgeConfirm -in @("y", "Y", "yes", "Yes")) {
            if (Test-Path "docker/postgres") {
                Remove-Item -Recurse -Force "docker/postgres"
                Write-Info "Database data removed"
            } else {
                Write-Info "No database data directory found"
            }
        } else {
            Write-Info "Database data preserved"
        }
    } else {
        Write-Info "Database data preserved in docker/postgres/ (memories are safe)"
        Write-Info "Use -Full flag to also delete the database data"
    }
}

# -----------------------------------------------------------------------
# 5. Remove manifest and setup guide
# -----------------------------------------------------------------------
Write-Step "Cleaning up..."
if (Test-Path $MANIFEST_FILE) {
    Remove-Item $MANIFEST_FILE
    Write-Info "Removed $MANIFEST_FILE"
}
if (Test-Path "SETUP_COMPLETE.md") {
    Remove-Item "SETUP_COMPLETE.md"
    Write-Info "Removed SETUP_COMPLETE.md"
}

# -----------------------------------------------------------------------
# 6. Summary
# -----------------------------------------------------------------------
Write-Host ""
Write-Host "+==========================================+"
Write-Host "|  Uninstall Complete                      |"
Write-Host "+==========================================+"
Write-Host ""
Write-Info "Removed: $($components -join ', ')"
Write-Host ""
Write-Info "NOT removed (manual cleanup if desired):"
Write-Info "  - This directory ($SCRIPT_DIR)"
Write-Info "  - .env file (contains database credentials)"
if ($components -contains "python_deps") {
    Write-Info "  - Python packages installed via pip"
}
if (-not $Full -and ($components -contains "database" -or $components -contains "docker")) {
    Write-Info "  - Database data in docker/postgres/ (run with --Full to delete)"
}
Write-Host ""
