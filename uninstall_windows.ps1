# NotNativeMemory - Windows Uninstaller
# Run: powershell -ExecutionPolicy Bypass -File uninstall_windows.ps1
#
# Reads .install-manifest.json to determine what was installed and only
# removes those components. Safe to run multiple times.

param(
    [switch]$Full  # Also remove Docker volume (destroys all stored memories)
)

$ErrorActionPreference = "Continue"

function Write-Step($msg) { Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "[x] $msg" -ForegroundColor Red }
function Write-Info($msg) { Write-Host "    $msg" }

$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $SCRIPT_DIR
$MANIFEST_FILE = ".install-manifest.json"

Write-Host ""
Write-Host "+==========================================+"
Write-Host "|  NotNativeMemory - Uninstaller           |"
Write-Host "+==========================================+"
Write-Host ""

# -----------------------------------------------------------------------
# 1. Load manifest
# -----------------------------------------------------------------------
if (-not (Test-Path $MANIFEST_FILE)) {
    Write-Err "No install manifest found ($MANIFEST_FILE)."
    Write-Info "Either this was never installed, or the manifest was deleted."
    Write-Info "You can manually clean up:"
    Write-Info "  - Remove hooks from ~/.claude/settings.json"
    Write-Info "  - Stop MCP server: python server.py --stop"
    Write-Info "  - Stop Docker: docker compose -f docker/docker-compose.yml down"
    exit 1
}

try {
    $manifest = Get-Content $MANIFEST_FILE -Raw | ConvertFrom-Json
} catch {
    Write-Err "Manifest is corrupt. Cannot determine what to uninstall."
    exit 1
}

$installMode = $manifest.install_mode
$components = $manifest.components

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
# 2. Stop MCP server (if running)
# -----------------------------------------------------------------------
if ($components -contains "server") {
    if ($components -contains "docker") {
        # Dockerized install - server is a container, stopped in step 4
        Write-Info "MCP server runs in Docker (will be stopped with containers)"
    } else {
        # Bare-metal install - server is a Python process
        Write-Step "Stopping MCP server..."
        if (Test-Path ".mcp-server.pid") {
            python server.py --stop 2>&1
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
# 3. Remove Claude Code hooks
# -----------------------------------------------------------------------
if ($components -contains "hooks") {
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
    try {
        # `--profile '*'` matches both full and server profiles so a single
        # `down` covers either install shape.
        & docker compose --env-file .env -f docker/docker-compose.yml --profile '*' down 2>&1 | Out-Null
        Write-Info "Containers stopped"
    } catch {
        Write-Warn "Could not stop containers (Docker may not be running)"
    }

    # Remove the Docker image if this was a containerized install
    if ($components -contains "docker") {
        try {
            & docker rmi notnative-memory-mcp 2>&1 | Out-Null
            Write-Info "MCP server image removed"
        } catch {
            Write-Warn "Could not remove MCP server image"
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
        Write-Info "Use --Full flag to also delete the database data"
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
