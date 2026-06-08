# NotNativeMemory - Windows Installer
# Run: powershell -ExecutionPolicy Bypass -File install_windows.ps1

param(
    [switch]$SkipModel
)

# Use "Continue" globally. Many tools (docker, python, pip) write warnings
# and progress to stderr, which PowerShell treats as terminating errors
# under "Stop". We check $LASTEXITCODE explicitly after each critical call.
$ErrorActionPreference = "Continue"

# Force docker buildx and docker compose to use line-based progress
# output instead of the default fancy renderer with unicode block
# characters. The legacy Windows console codepage cannot render those
# glyphs and they show up as garbage like `Gu_` / `GaO`. Plain mode
# is one log line per step, ASCII-only, much easier to read and to
# pipe through Invoke-Native without losing context.
$env:BUILDKIT_PROGRESS = "plain"

function Write-Step($msg) { Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "[x] $msg" -ForegroundColor Red }
function Write-Info($msg) { Write-Host "    $msg" }

# Run a native command and render ALL output (stdout + stderr) as plain
# text in the default terminal color, with $LASTEXITCODE preserved.
#
# Why: in Windows PowerShell 5.1, `2>&1` wraps every stderr line as a
# NativeCommandError record, which the host renders in red even when
# the underlying tool is writing benign progress (docker compose,
# pip, etc.) to stderr. $ErrorActionPreference only governs whether
# errors halt execution, not how they render. This wrapper unwraps
# the records so informational stderr no longer looks like a failure.
#
# Usage: Invoke-Native docker compose --progress=plain -f docker/docker-compose.yml ...
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

function Test-EmbeddingModelComplete {
    param([string]$Path)

    if (-not (Test-Path $Path -PathType Container)) {
        return $false
    }

    $requiredFiles = @(
        "config.json",
        "modules.json",
        "config_sentence_transformers.json",
        "tokenizer.json"
    )
    foreach ($file in $requiredFiles) {
        if (-not (Test-Path (Join-Path $Path $file) -PathType Leaf)) {
            return $false
        }
    }
    try {
        $config = Get-Content (Join-Path $Path "config.json") -Raw | ConvertFrom-Json
        if (-not $config.model_type) {
            return $false
        }
    } catch {
        return $false
    }

    $checkpointFiles = @("model.safetensors", "pytorch_model.bin")
    foreach ($file in $checkpointFiles) {
        if (Test-Path (Join-Path $Path $file) -PathType Leaf) {
            return $true
        }
    }
    return $false
}

# Detect which supported agent CLIs are on PATH and wire hooks + MCP
# registration for whichever is present. Returns the list of agents
# that were configured so the caller can tailor the summary output.
function Configure-Agents($installPath, $mcpUrl) {
    $configured = @()

    $claudeInstalled = Get-Command claude -ErrorAction SilentlyContinue
    if ($claudeInstalled) {
        Write-Step "Configuring Claude Code hooks..."
        Invoke-Native python hook_bundles/claude/notnative-memory/merge_hooks.py "$installPath" "$mcpUrl"
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Claude Code hook configuration failed. You can run this manually later:"
            Write-Info "python hook_bundles/claude/notnative-memory/merge_hooks.py `"$installPath`" `"$mcpUrl`""
        } else {
            $configured += "claude"
        }

        Write-Step "Registering MCP memory server with Claude Code..."
        Invoke-Native claude mcp add --transport http memory --scope user "$mcpUrl"
        if ($LASTEXITCODE -eq 0) {
            Write-Info "MCP server registered with Claude Code"
        } else {
            Write-Warn "Claude Code MCP registration failed. Run manually:"
            Write-Info "claude mcp add --transport http memory --scope user `"$mcpUrl`""
        }
    }

    $nnaInstalled = Get-Command nna -ErrorAction SilentlyContinue
    if ($nnaInstalled) {
        Write-Step "Configuring NotNativeAgent memory..."
        Invoke-Native python hook_bundles/nna/notnative-memory/merge_hooks.py "$installPath" "$mcpUrl"
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "NotNativeAgent configuration failed. You can run this manually later:"
            Write-Info "python hook_bundles/nna/notnative-memory/merge_hooks.py `"$installPath`" `"$mcpUrl`""
        } else {
            $configured += "nna"
            Write-Info "memoryUrl written to ~/.nna/config.json"
        }
    }

    $codexInstalled = (Get-Command codex -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $env:USERPROFILE ".codex"))
    if ($codexInstalled) {
        Write-Step "Configuring Codex hooks..."
        Invoke-Native python hook_bundles/codex/notnative-memory/merge_hooks.py "$installPath" "$mcpUrl"
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Codex hook configuration failed. You can run this manually later:"
            Write-Info "python hook_bundles/codex/notnative-memory/merge_hooks.py `"$installPath`" `"$mcpUrl`""
        } else {
            $configured += "codex"
            Write-Info "hooks merged into ~/.codex/hooks.json"
            Write-Info "Codex may ask you to trust these hooks with /hooks before they run."
        }
    }

    if ($configured.Count -eq 0) {
        Write-Info "No supported agent CLIs detected (claude, nna, codex)."
        Write-Info "To configure manually after installing one:"
        Write-Info "  python hook_bundles/claude/notnative-memory/merge_hooks.py `"$installPath`" `"$mcpUrl`"  # Claude Code"
        Write-Info "  python hook_bundles/nna/notnative-memory/merge_hooks.py `"$installPath`" `"$mcpUrl`"     # NotNativeAgent"
        Write-Info "  python hook_bundles/codex/notnative-memory/merge_hooks.py `"$installPath`" `"$mcpUrl`"   # Codex"
    }

    return ,$configured
}

$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $SCRIPT_DIR
$INSTALL_PATH = (Get-Location).Path -replace '\\', '/'
$HOSTNAME = $env:COMPUTERNAME.ToLower()
$MCP_PORT = 9500
$MANIFEST_FILE = ".install-manifest.json"

. "$SCRIPT_DIR\install_lib\manifest_merge.ps1"

Write-Host ""
Write-Host "+==========================================+"
Write-Host "|  NotNativeMemory - MCP Memory Server     |"
Write-Host "|  Persistent memory for MCP-compatible    |"
Write-Host "|  agents (Claude Code, NNC, LM Studio...) |"
Write-Host "+==========================================+"
Write-Host ""

# -----------------------------------------------------------------------
# Load existing manifest if present (for idempotent re-runs)
# -----------------------------------------------------------------------
$existingManifest = $null
if (Test-Path $MANIFEST_FILE) {
    try {
        $existingManifest = Get-Content $MANIFEST_FILE -Raw | ConvertFrom-Json
        Write-Info "Found existing installation: $($existingManifest.install_mode)"
        Write-Info "Re-running will update the existing configuration."
        Write-Host ""
    } catch {
        Write-Warn "Existing manifest is corrupt. Starting fresh."
    }
}

# -----------------------------------------------------------------------
# 1. What are we installing?
# -----------------------------------------------------------------------
Write-Step "Installation Mode"
Write-Host ""
Write-Host "  What are you setting up on this machine?"
Write-Host ""
Write-Host "  1 - Full install (database + server, all in Docker)"
Write-Host "      Everything runs here. No Python required."
Write-Host ""
Write-Host "  2 - Server only (server + hooks, database is remote)"
Write-Host "      MCP server runs here, Postgres is on another machine."
Write-Host "      Docker container (auto-restarts) or host Python process."
Write-Host ""
Write-Host "  3 - Client only (hooks only, server is remote)"
Write-Host "      Just configure Claude Code to use a remote MCP server."
Write-Host "      No Python dependencies, no model download, no database."
Write-Host ""

# Default to existing mode if re-running
$defaultMode = ""
if ($existingManifest) {
    $modeMap = @{ "full" = "1"; "server" = "2"; "client" = "3" }
    $defaultMode = $modeMap[$existingManifest.install_mode]
    Write-Info "Previous install was mode $defaultMode ($($existingManifest.install_mode))"
}
$modeChoice = Read-Host "  Choice [1/2/3]"
if (-not $modeChoice -and $defaultMode) { $modeChoice = $defaultMode }

# Validate
if ($modeChoice -notin @("1", "2", "3")) {
    Write-Err "Invalid choice. Run the installer again."
    exit 1
}

$installMode = switch ($modeChoice) {
    "1" { "full" }
    "2" { "server" }
    "3" { "client" }
}
$needsDatabase = $installMode -in @("full")
$needsServer = $installMode -in @("full", "server")
$needsHooks = $true  # all modes get hooks

Write-Host ""
Write-Step "Installing: $installMode"

# =======================================================================
# CLIENT-ONLY PATH
# =======================================================================
if ($installMode -eq "client") {
    Write-Host ""
    Write-Step "Remote MCP Server Configuration"
    Write-Host ""

    # Reuse existing values if re-running
    $defaultHost = ""
    $defaultPort = "9500"
    if ($existingManifest -and $existingManifest.mcp_url) {
        # Parse host and port from existing URL
        if ($existingManifest.mcp_url -match "http://([^:/]+):?(\d+)?") {
            $defaultHost = $Matches[1]
            if ($Matches[2]) { $defaultPort = $Matches[2] }
        }
        Write-Info "Current: $defaultHost`:$defaultPort"
    }

    $mcpHost = Read-Host "  MCP server hostname (e.g. myserver, 192.168.1.10)"
    if (-not $mcpHost -and $defaultHost) { $mcpHost = $defaultHost }
    if (-not $mcpHost) {
        Write-Err "Hostname is required for client-only install."
        exit 1
    }

    $mcpPort = Read-Host "  MCP server port (default: $defaultPort)"
    if (-not $mcpPort) { $mcpPort = $defaultPort }

    $MCP_URL = "http://${mcpHost}:${mcpPort}/mcp"
    Write-Info "URL: $MCP_URL"

    # Test connectivity
    Write-Step "Testing MCP server connection..."
    $connectionFailed = $false
    try {
        $testPayload = '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
        $response = Invoke-WebRequest -Uri $MCP_URL -Method POST -Body $testPayload -ContentType "application/json" -Headers @{"Accept"="application/json"} -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop
        if ($response.StatusCode -eq 200) {
            Write-Info "Connection successful"
        }
    } catch {
        $connectionFailed = $true
        Write-Warn "Could not reach $MCP_URL - server may not be running."
        Write-Info "Hooks will be configured anyway. Start the remote server before using."
    }

    # Configure hooks + MCP registration for whichever supported agent
    # CLI is installed on this machine (Claude Code, NotNativeCoder).
    $configuredAgents = Configure-Agents $INSTALL_PATH $MCP_URL

    # Write manifest. Merge with any existing manifest so running
    # client-only on a host that already has a heavier install does
    # not strand docker/database in the uninstall record.
    $merged = Merge-InstallManifest -Existing $existingManifest -NewMode "client" -NewComponents @("hooks")
    $dbHost = if ($existingManifest) { $existingManifest.db_host } else { $null }
    $dbPort = if ($existingManifest) { $existingManifest.db_port } else { $null }
    @{
        installed    = (Get-Date -Format "yyyy-MM-dd")
        install_mode = $merged.mode
        install_path = $INSTALL_PATH
        components   = $merged.components
        mcp_url      = $MCP_URL
        db_host      = $dbHost
        db_port      = $dbPort
    } | ConvertTo-Json | Set-Content -Path $MANIFEST_FILE -Encoding UTF8

    Write-Host ""
    Write-Host "+==========================================+"
    Write-Host "|  Client Setup Complete!                  |"
    Write-Host "+==========================================+"
    Write-Host ""
    Write-Info "Hooks configured to use: $MCP_URL"
    Write-Info "Make sure the remote MCP server is running."

    # When the MCP URL is loopback AND the connection test just failed,
    # the most likely culprit on Windows is the IPv6 prefix policy that
    # resolves 'localhost' to ::1 before 127.0.0.1. If the server only
    # binds to IPv4 the same-host client times out. Surface a one-line
    # netsh fix the user can run in an elevated shell. No auto-apply,
    # and no advice on a successful connection or a routable URL.
    if ($connectionFailed) {
        . "$SCRIPT_DIR\install_lib\loopback_advice.ps1"
        Show-LoopbackIPv6AdviceIfNeeded -Url $MCP_URL | Out-Null
    }

    Write-Host ""
    exit 0
}

# =======================================================================
# SERVER AND FULL INSTALL PATHS
# =======================================================================

# -----------------------------------------------------------------------
# 1b. Server backend sub-choice (server mode only)
# -----------------------------------------------------------------------
# Full mode is always Docker. For server mode offer Docker container vs
# host Python process. If Docker isn't running/installed, fall through
# to Python silently.
$serverBackend = ""  # "docker" | "python"; only meaningful for server mode
if ($installMode -eq "server") {
    $dockerAvailable = $false
    try {
        docker info 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $dockerAvailable = $true }
    } catch { }

    if ($dockerAvailable) {
        Write-Step "Server Backend"
        Write-Host ""
        Write-Host "  How should the MCP server run on this machine?"
        Write-Host ""
        Write-Host "  d - Docker container (recommended)"
        Write-Host "      Auto-restarts on reboot. No Python needed on host."
        Write-Host ""
        Write-Host "  p - Host Python process"
        Write-Host "      Run server.py directly. Requires Python 3.11+."
        Write-Host ""

        $defaultBackend = "d"
        if ($existingManifest -and $existingManifest.install_mode -eq "server") {
            switch ($existingManifest.server_backend) {
                "docker" { $defaultBackend = "d"; Write-Info "Previous backend: docker" }
                "python" { $defaultBackend = "p"; Write-Info "Previous backend: python" }
            }
        }

        $backendChoice = Read-Host "  Choice [d/p] (default: $defaultBackend)"
        if (-not $backendChoice) { $backendChoice = $defaultBackend }
        switch ($backendChoice.ToLower()) {
            "d" { $serverBackend = "docker" }
            "p" { $serverBackend = "python" }
            default {
                Write-Err "Invalid choice. Run the installer again."
                exit 1
            }
        }
    } else {
        Write-Info "Docker not available; using host Python process for the server."
        $serverBackend = "python"
    }
}

# USE_DOCKER drives the install path from here on.
if ($installMode -eq "full" -or $serverBackend -eq "docker") {
    $useDocker = $true
    if ($installMode -eq "full") { $composeProfile = "full" } else { $composeProfile = "server" }
} else {
    $useDocker = $false
    $composeProfile = ""
}

# -----------------------------------------------------------------------
# 2. Check Python (required when the server runs as a host process)
# -----------------------------------------------------------------------
$pythonAvailable = $false
try {
    $pyVersion = python --version 2>&1
    if ($pyVersion -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -ge 3 -and $minor -ge 11) {
            $pythonAvailable = $true
            Write-Info "Found $pyVersion"
        }
    }
} catch { }

if (-not $pythonAvailable) {
    if ($useDocker) {
        Write-Info "Python not found (not required for Docker backend)"
    } else {
        Write-Err "Python 3.11+ required for the host Python server backend."
        exit 1
    }
}

# -----------------------------------------------------------------------
# 3. Database configuration
# -----------------------------------------------------------------------
$DB_HOST = "localhost"
$DB_PORT = "5433"
$DB_NAME = "notnative_memory"
$DB_USER = "memory"
$DB_PASSWORD = ""
$APP_DB_USER = "memory_app"
$APP_DB_PASSWORD = ""

if ($installMode -eq "full") {
    # Local Docker database
    Write-Step "Checking Docker..."
    try {
        $dockerVersion = docker --version 2>&1
        Write-Info "Found $dockerVersion"
    } catch {
        Write-Err "Docker not found. Install Docker Desktop and try again."
        exit 1
    }

    # Check Docker daemon is reachable. Native commands do not throw on
    # non-zero exit under $ErrorActionPreference = "Continue", so the
    # previous try/catch here was a no-op. Check $LASTEXITCODE explicitly.
    docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Docker is not running (or daemon unreachable). Start Docker Desktop and try again."
        exit 1
    }

    # Reuse existing passwords from .env if present (prevents mismatch
    # with an already-initialized Postgres data volume).
    if (Test-Path ".env") {
        $existingPw = Select-String -Path ".env" -Pattern "^MEMORY_DB_PASSWORD=(.+)$" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($existingPw) {
            $DB_PASSWORD = $existingPw.Matches[0].Groups[1].Value
            Write-Info "Using existing MEMORY_DB_PASSWORD from .env"
        }
        $existingAppPw = Select-String -Path ".env" -Pattern "^MEMORY_APP_DB_PASSWORD=(.+)$" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($existingAppPw) {
            $APP_DB_PASSWORD = $existingAppPw.Matches[0].Groups[1].Value
            Write-Info "Using existing MEMORY_APP_DB_PASSWORD from .env"
        }
    }
    if (-not $DB_PASSWORD) {
        $DB_PASSWORD = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
        Write-Info "Generated new database password"
    }
    if (-not $APP_DB_PASSWORD) {
        # url-safe chars only: no single-quote risk in the init SQL.
        $APP_DB_PASSWORD = python -c "import secrets; print(secrets.token_urlsafe(24))"
        Write-Info "Generated memory_app role password (RLS enforcement enabled)"
    }

    # Start Postgres container first (MCP depends on it, started after model download)
    Write-Step "Starting Postgres + pgvector container..."
    $env:MEMORY_DB_PASSWORD = $DB_PASSWORD
    $env:MEMORY_DB_PORT = $DB_PORT
    $env:MEMORY_DB_NAME = $DB_NAME
    $env:MEMORY_DB_USER = $DB_USER
    $env:MEMORY_APP_DB_USER = $APP_DB_USER
    $env:MEMORY_APP_DB_PASSWORD = $APP_DB_PASSWORD
    # Do NOT swallow compose output. An env-interpolation or image-pull
    # failure here would otherwise be invisible and the health check
    # below would burn 30 seconds polling a container that never started.
    # Invoke-Native unwraps stderr so compose's progress lines render as
    # plain text instead of red NativeCommandError records.
    Invoke-Native docker compose --progress=plain -f docker/docker-compose.yml --profile full up -d postgres
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to start Postgres container (docker compose up exited $LASTEXITCODE)."
        Write-Info "Check the output above for the specific error."
        exit 1
    }

    # Wait for healthy
    Write-Step "Waiting for Postgres to be ready..."
    $maxWait = 30
    $waited = 0
    while ($waited -lt $maxWait) {
        $health = & docker inspect --format "{{.State.Health.Status}}" MCP-postgres 2>$null
        $health = "$health".Trim()
        if ($health -eq "healthy") { break }
        Start-Sleep -Seconds 2
        $waited += 2
        Write-Info "Waiting... ($waited/$maxWait seconds)"
    }
    if ($waited -ge $maxWait) {
        Write-Err "Postgres did not become healthy within $maxWait seconds."
        exit 1
    }
    Write-Info "Postgres is ready"

} elseif ($installMode -eq "server") {
    # Remote database
    Write-Step "Remote Database Configuration"
    Write-Host ""

    # Reuse existing values if re-running
    if ($existingManifest -and $existingManifest.db_host) {
        Write-Info "Current: $($existingManifest.db_host):$($existingManifest.db_port)"
    }

    $input = Read-Host "  Postgres host (e.g. myserver, 192.168.1.10)"
    if ($input) { $DB_HOST = $input }
    elseif ($existingManifest -and $existingManifest.db_host) { $DB_HOST = $existingManifest.db_host }

    $input = Read-Host "  Postgres port (default: 5433)"
    if ($input) { $DB_PORT = $input }
    elseif ($existingManifest -and $existingManifest.db_port) { $DB_PORT = $existingManifest.db_port }

    $input = Read-Host "  Database name (default: notnative_memory)"
    if ($input) { $DB_NAME = $input }

    $input = Read-Host "  Database user (default: memory)"
    if ($input) { $DB_USER = $input }

    $DB_PASSWORD = Read-Host "  Database password"
    # Fall back to existing .env password if user just hits enter
    if (-not $DB_PASSWORD -and (Test-Path ".env")) {
        $existingPw = Select-String -Path ".env" -Pattern "^MEMORY_DB_PASSWORD=(.+)$" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($existingPw) {
            $DB_PASSWORD = $existingPw.Matches[0].Groups[1].Value
            Write-Info "Using existing password from .env"
        }
    }
    if (-not $DB_PASSWORD) {
        Write-Err "Password is required for remote database."
        exit 1
    }

    # Remote-DB path: generate the memory_app password if not set.
    # The installer will CREATE ROLE later via asyncpg.
    if (Test-Path ".env") {
        $existingAppPw = Select-String -Path ".env" -Pattern "^MEMORY_APP_DB_PASSWORD=(.+)$" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($existingAppPw) {
            $APP_DB_PASSWORD = $existingAppPw.Matches[0].Groups[1].Value
            Write-Info "Using existing MEMORY_APP_DB_PASSWORD from .env"
        }
    }
    if (-not $APP_DB_PASSWORD) {
        $APP_DB_PASSWORD = python -c "import secrets; print(secrets.token_urlsafe(24))"
        Write-Info "Generated memory_app role password (RLS enforcement enabled)"
    }
}

# -----------------------------------------------------------------------
# 4. Write .env
# -----------------------------------------------------------------------
# .env values are what the running server (container or host python) will
# use to connect. Full mode uses the internal Docker service name for
# Postgres; remote-DB modes use what the user entered.
if ($installMode -eq "full") {
    $envDbHost = "postgres"
    $envDbPort = "5432"
} else {
    $envDbHost = $DB_HOST
    $envDbPort = $DB_PORT
}

Write-Step "Writing .env file..."
@"
# NotNativeMemory - Generated by install script
MEMORY_DB_HOST=$envDbHost
MEMORY_DB_PORT=$envDbPort
MEMORY_DB_NAME=$DB_NAME
MEMORY_DB_USER=$DB_USER
MEMORY_DB_PASSWORD=$DB_PASSWORD
MEMORY_MODEL_PATH=models/gte-large-en-v1.5
MEMORY_DEFAULT_PROJECT=

# -- Network exposure -----------------------------------------------------
# MEMORY_BIND_HOST: which interface the HTTP server listens on.
#   127.0.0.1 = loopback only (this machine only; safest default)
#   0.0.0.0   = all interfaces (LAN / Docker / public)
# When you use a non-loopback value you SHOULD also run behind a
# TLS-terminating reverse proxy and set MEMORY_COOKIE_SECURE=1 so
# session cookies never travel in plaintext.
MEMORY_BIND_HOST=0.0.0.0

# MEMORY_COOKIE_SECURE: set to 1 when behind TLS so session and CSRF
# cookies carry the Secure attribute. Leave unset on plain HTTP dev.
MEMORY_COOKIE_SECURE=

# -- Postgres Row-Level Security (default on) -----------------------------
# The installer creates a non-superuser memory_app role at DB init
# time and points the app at it, so RLS policies enforce from the
# first request. Migrations still use MEMORY_DB_USER (needs superuser
# for DDL). To disable RLS enforcement, blank these two values and
# restart the server. The app will fall back to MEMORY_DB_USER.
MEMORY_APP_DB_USER=$APP_DB_USER
MEMORY_APP_DB_PASSWORD=$APP_DB_PASSWORD
"@ | Set-Content -Path ".env" -Encoding UTF8
Write-Info "Saved to .env"

if ($useDocker) {
    # -----------------------------------------------------------------------
    # 5. Build Docker image
    # All Python deps live inside the container - no host pip install needed.
    # -----------------------------------------------------------------------
    Write-Step "Building MCP server Docker image..."
    Invoke-Native docker compose --progress=plain -f docker/docker-compose.yml --profile $composeProfile build mcp
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Docker image build failed."
        exit 1
    }

    # -----------------------------------------------------------------------
    # 6. Download embedding model via container
    # The container has sentence-transformers installed. Mount models/
    # read-write temporarily so the download persists on the host.
    # -----------------------------------------------------------------------
    if (-not $SkipModel) {
        Write-Step "Downloading embedding model (gte-large-en-v1.5, ~870MB)..."
        if (Test-EmbeddingModelComplete "models/gte-large-en-v1.5") {
            Write-Info "Model already exists, skipping download"
        } else {
            if (Test-Path "models/gte-large-en-v1.5") {
                Write-Warn "Existing model directory is incomplete; re-downloading it."
            }
            if (-not (Test-Path "models")) { New-Item "models" -ItemType Directory | Out-Null }
            Invoke-Native docker compose --progress=plain -f docker/docker-compose.yml --profile $composeProfile run --rm `
                -v "${PWD}/models:/app/models" `
                mcp python -c "
from sentence_transformers import SentenceTransformer
import os
os.makedirs('models/gte-large-en-v1.5', exist_ok=True)
print('Downloading gte-large-en-v1.5...')
model = SentenceTransformer('Alibaba-NLP/gte-large-en-v1.5', trust_remote_code=True)
# fp16 cast halves both disk and RAM footprint. Quality loss is
# negligible for cosine-similarity retrieval.
model = model.half()
model.save('models/gte-large-en-v1.5')
print('Model saved to models/gte-large-en-v1.5 (fp16)')
"
            if ($LASTEXITCODE -ne 0) {
                Write-Err "Model download failed. Check your internet connection."
                exit 1
            }
        }
    } else {
        Write-Warn "Skipping model download (--SkipModel)"
    }

    # -----------------------------------------------------------------------
    # 6b. Remote schema apply (server+docker only)
    # Full mode's Postgres container runs config/schema.sql as an init
    # script. For remote DBs we apply it via a one-shot mcp container.
    # -----------------------------------------------------------------------
    if ($installMode -eq "server") {
        Write-Step "Testing remote database connection from container..."
        Invoke-Native docker compose --progress=plain -f docker/docker-compose.yml --profile $composeProfile run --rm mcp python -c "
import asyncio, asyncpg, os, sys
async def test():
    try:
        conn = await asyncpg.connect(
            host=os.environ['MEMORY_DB_HOST'],
            port=int(os.environ['MEMORY_DB_PORT']),
            database=os.environ['MEMORY_DB_NAME'],
            user=os.environ['MEMORY_DB_USER'],
            password=os.environ['MEMORY_DB_PASSWORD'],
            timeout=10,
        )
        await conn.close()
        print('OK')
    except Exception as e:
        print(f'FAIL: {e}', file=sys.stderr)
        sys.exit(1)
asyncio.run(test())
"
        if ($LASTEXITCODE -ne 0) {
            Write-Err "Cannot connect to remote database from container. Check your settings."
            exit 1
        }
        Write-Info "Connection successful"

        Write-Step "Applying schema to remote database..."
        Invoke-Native docker compose --progress=plain -f docker/docker-compose.yml --profile $composeProfile run --rm mcp python -c "
import asyncio, asyncpg, os
async def run_schema():
    conn = await asyncpg.connect(
        host=os.environ['MEMORY_DB_HOST'],
        port=int(os.environ['MEMORY_DB_PORT']),
        database=os.environ['MEMORY_DB_NAME'],
        user=os.environ['MEMORY_DB_USER'],
        password=os.environ['MEMORY_DB_PASSWORD'],
    )
    with open('config/schema.sql', 'r') as f:
        sql = f.read()
    await conn.execute(sql)
    await conn.close()
    print('Schema applied')
asyncio.run(run_schema())
"
        if ($LASTEXITCODE -ne 0) {
            Write-Err "Schema apply failed."
            exit 1
        }
    }

    # -----------------------------------------------------------------------
    # 6c. Ensure memory_app role exists (all Docker modes)
    # Idempotent: safe to re-run. For full mode this heals DB volumes
    # that predate 02-roles.sh; for server mode it's the only role-creator.
    # -----------------------------------------------------------------------
    Write-Step "Ensuring memory_app role (RLS enforcement)..."
    Invoke-Native docker compose --progress=plain -f docker/docker-compose.yml --profile $composeProfile run --rm mcp python docker/init/ensure_app_role.py
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Role provisioning failed. MCP would fail to authenticate as $APP_DB_USER."
        Write-Info "If this is an existing Docker volume and .env was regenerated, restore the old .env password or reset the Docker Postgres volume."
        Write-Info "To opt out of RLS enforcement, blank MEMORY_APP_DB_USER and MEMORY_APP_DB_PASSWORD in .env, then re-run."
        exit 1
    }

    # -----------------------------------------------------------------------
    # 7. Start containers and wait for ready
    # -----------------------------------------------------------------------
    Write-Step "Starting MCP server container..."
    Invoke-Native docker compose --progress=plain -f docker/docker-compose.yml --profile $composeProfile up -d --force-recreate mcp
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to start MCP server container (docker compose up exited $LASTEXITCODE)."
        Write-Info "Check the output above and try: docker compose -f docker/docker-compose.yml logs mcp"
        exit 1
    }

    # Wait for MCP server to be ready (model loading takes 10-30s on first start)
    Write-Step "Waiting for MCP server to be ready..."
    $maxWait = 60
    $waited = 0
    while ($waited -lt $maxWait) {
        try {
            $testPayload = '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
            $response = Invoke-WebRequest -Uri "http://localhost:$MCP_PORT/mcp" -Method POST `
                -Body $testPayload -ContentType "application/json" `
                -Headers @{"Accept"="application/json"} -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
            if ($response.StatusCode -eq 200) { break }
        } catch { }
        Start-Sleep -Seconds 3
        $waited += 3
        Write-Info "Waiting... ($waited/$maxWait seconds)"
    }
    if ($waited -ge $maxWait) {
        Write-Err "MCP server did not become ready within $maxWait seconds."
        Write-Info "Check logs: docker compose -f docker/docker-compose.yml logs mcp"
        exit 1
    }
    Write-Info "MCP server is ready"

} else {
    # -----------------------------------------------------------------------
    # 5. Install Python dependencies (server-only mode - runs on host)
    # -----------------------------------------------------------------------
    Write-Step "Installing Python dependencies..."
    pip install -r requirements.txt --quiet 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Err "pip install failed (exit $LASTEXITCODE). See output above."
        exit 1
    }

    # -----------------------------------------------------------------------
    # 6. Test database connection and run schema (server-only mode)
    # -----------------------------------------------------------------------
    Write-Step "Testing database connection..."
    Invoke-Native python -c "
import asyncio, asyncpg, os, sys
from dotenv import load_dotenv
load_dotenv()

async def test():
    try:
        conn = await asyncpg.connect(
            host=os.environ['MEMORY_DB_HOST'],
            port=int(os.environ['MEMORY_DB_PORT']),
            database=os.environ['MEMORY_DB_NAME'],
            user=os.environ['MEMORY_DB_USER'],
            password=os.environ['MEMORY_DB_PASSWORD'],
            timeout=10,
        )
        await conn.close()
        print('OK')
    except Exception as e:
        print(f'FAIL: {e}', file=sys.stderr)
        sys.exit(1)

asyncio.run(test())
"
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Cannot connect to database. Check your settings."
        exit 1
    }
    Write-Info "Connection successful"

    Write-Step "Creating database schema..."
    Invoke-Native python -c "
import asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()

async def run_schema():
    conn = await asyncpg.connect(
        host=os.environ['MEMORY_DB_HOST'],
        port=int(os.environ['MEMORY_DB_PORT']),
        database=os.environ['MEMORY_DB_NAME'],
        user=os.environ['MEMORY_DB_USER'],
        password=os.environ['MEMORY_DB_PASSWORD'],
    )
    with open('config/schema.sql', 'r') as f:
        sql = f.read()
    await conn.execute(sql)
    await conn.close()
    print('Schema created successfully')

asyncio.run(run_schema())
"
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Schema creation failed."
        exit 1
    }

    Write-Step "Ensuring memory_app role (RLS enforcement)..."
    # .env values are loaded by python-dotenv inside the script.
    Invoke-Native python docker/init/ensure_app_role.py
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Role provisioning failed. MCP would fail to authenticate as $APP_DB_USER."
        Write-Info "To opt out of RLS enforcement, blank MEMORY_APP_DB_USER and MEMORY_APP_DB_PASSWORD in .env, then re-run."
        exit 1
    }

    # -----------------------------------------------------------------------
    # 7. Download embedding model (server-only mode - on host)
    # -----------------------------------------------------------------------
    if (-not $SkipModel) {
        Write-Step "Downloading embedding model (gte-large-en-v1.5, ~870MB)..."
        if (Test-EmbeddingModelComplete "models/gte-large-en-v1.5") {
            Write-Info "Model already exists, skipping download"
        } else {
            if (Test-Path "models/gte-large-en-v1.5") {
                Write-Warn "Existing model directory is incomplete; re-downloading it."
            }
            Invoke-Native python -c "
from sentence_transformers import SentenceTransformer
import os
os.makedirs('models', exist_ok=True)
print('Downloading gte-large-en-v1.5...')
model = SentenceTransformer('Alibaba-NLP/gte-large-en-v1.5', trust_remote_code=True)
# fp16 cast halves both disk and RAM footprint. Quality loss is
# negligible for cosine-similarity retrieval.
model = model.half()
model.save('models/gte-large-en-v1.5')
print('Model saved to models/gte-large-en-v1.5 (fp16)')
"
            if ($LASTEXITCODE -ne 0) {
                Write-Err "Model download failed. Check your internet connection."
                exit 1
            }
        }
    } else {
        Write-Warn "Skipping model download (--SkipModel)"
    }
}

# -----------------------------------------------------------------------
# 8b. Windows Firewall rule for inbound MCP port.
# Server modes only (client-only mode exits earlier). The .env we just
# wrote pins MEMORY_BIND_HOST=0.0.0.0, so the server listens on every
# interface and a firewall rule is the actual gate. The rule is opt-in
# via prompt; the helper functions are idempotent so re-running the
# installer with an existing rule is a no-op.
# -----------------------------------------------------------------------
. "$SCRIPT_DIR\install_lib\firewall.ps1"
$ruleName = Get-NnmFirewallRuleName -Port $MCP_PORT
$existingRule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existingRule) {
    Write-Info "Firewall rule '$ruleName' already exists, skipping."
} else {
    Write-Step "Windows Firewall"
    Write-Host ""
    Write-Host "  Current network profiles on this host:"
    Get-NetConnectionProfile | ForEach-Object {
        Write-Host ("    {0,-20} {1}" -f $_.InterfaceAlias, $_.NetworkCategory)
    }
    Write-Host ""
    $openAns = Read-Host "  Open inbound TCP $MCP_PORT in Windows Firewall? [Y/n]"
    if (-not $openAns -or $openAns -match '^[Yy]') {
        $pubAns = Read-Host "  Include Public networks? Recommended only for trusted-LAN servers. [y/N]"
        $includePublic = ($pubAns -match '^[Yy]')
        $fwProfiles = Get-FirewallProfileList -IncludePublic $includePublic
        $fwResult = Add-NnmFirewallRuleIfMissing -Port $MCP_PORT -Profiles $fwProfiles
        switch ($fwResult.action) {
            "created"        { Write-Info "Firewall rule '$($fwResult.rule)' added (profile: $($fwResult.profile))" }
            "skipped-exists" { Write-Info "Firewall rule '$($fwResult.rule)' already exists, skipping." }
            "failed" {
                Write-Warn "Could not add firewall rule: $($fwResult.error)"
                Write-Info "Run this manually in an admin PowerShell:"
                Write-Info "  New-NetFirewallRule -DisplayName '$($fwResult.rule)' -Direction Inbound -Protocol TCP -LocalPort $MCP_PORT -Action Allow -Profile $fwProfiles"
            }
        }
    } else {
        Write-Info "Firewall left unchanged. Remote clients will not be able to reach $MCP_PORT until you open it."
    }
}

# -----------------------------------------------------------------------
# 9. Self-test
# -----------------------------------------------------------------------
Write-Step "Running self-test..."
if ($useDocker) {
    # Run selftest inside the MCP container where deps and model are available
    Invoke-Native docker compose --progress=plain -f docker/docker-compose.yml exec mcp python scripts/selftest.py
} else {
    Invoke-Native python scripts/selftest.py
}
if ($LASTEXITCODE -ne 0) {
    Write-Err "Self-test failed. Check the output above for details."
    exit 1
}

# -----------------------------------------------------------------------
# 10. Configure hooks + MCP registration for any installed agent CLIs
# -----------------------------------------------------------------------
$MCP_URL = "http://localhost:$MCP_PORT/mcp"
$configuredAgents = Configure-Agents $INSTALL_PATH $MCP_URL
if ($configuredAgents.Count -eq 0) {
    Write-Info "Install hooks on client machines using option 3 (client only) once you have Claude Code or NotNativeCoder set up."
}

# -----------------------------------------------------------------------
# 10. Write manifest
# -----------------------------------------------------------------------
if ($installMode -eq "full") {
    $components = @("hooks", "server", "docker", "embedding_model", "database")
    $serverBackendOut = "docker"
} elseif ($serverBackend -eq "docker") {
    $components = @("hooks", "server", "docker", "embedding_model")
    $serverBackendOut = "docker"
} else {
    $components = @("hooks", "server", "python_deps", "embedding_model")
    $serverBackendOut = "python"
}

# Merge with any existing manifest. server-on-full or full-on-server
# should keep the broader install_mode and union of components so the
# uninstaller still sees everything.
$merged = Merge-InstallManifest -Existing $existingManifest -NewMode $installMode -NewComponents $components

@{
    installed      = (Get-Date -Format "yyyy-MM-dd")
    install_mode   = $merged.mode
    server_backend = $serverBackendOut
    install_path   = $INSTALL_PATH
    components     = $merged.components
    mcp_url        = $MCP_URL
    mcp_port       = $MCP_PORT
    db_host        = $DB_HOST
    db_port        = $DB_PORT
    db_name        = $DB_NAME
    db_user        = $DB_USER
    hostname       = $HOSTNAME
} | ConvertTo-Json | Set-Content -Path $MANIFEST_FILE -Encoding UTF8

# -----------------------------------------------------------------------
# 11. Write setup guide and print summary
# -----------------------------------------------------------------------

# Generate SETUP_COMPLETE.md with actual values filled in
if ($useDocker) {
    if ($installMode -eq "full") {
        $serverMgmtBlurb = @"
The server runs as a Docker container alongside Postgres:

``````
docker compose -f docker/docker-compose.yml --profile full up -d    # start
docker compose -f docker/docker-compose.yml --profile full down     # stop
docker compose -f docker/docker-compose.yml logs mcp                 # view logs
``````
"@
        $dbLine = "Database: postgres (Docker internal)"
    } else {
        $serverMgmtBlurb = @"
The server runs as a Docker container against your remote Postgres:

``````
docker compose -f docker/docker-compose.yml --profile server up -d mcp   # start
docker compose -f docker/docker-compose.yml --profile server down        # stop
docker compose -f docker/docker-compose.yml logs mcp                      # view logs
``````
"@
        $dbLine = "Database: ${DB_HOST}:${DB_PORT}/${DB_NAME} (remote)"
    }

@"
# NotNativeMemory - Setup Complete

Your MCP memory server is installed and tested.

## Starting the Server

$serverMgmtBlurb

The server auto-starts on boot (``restart: unless-stopped``).

## Claude Code Configuration

### This machine (already configured by installer)

Hooks have been added to ``~/.claude/settings.json``.

### Remote machines (client-only)

On other machines, run the installer and choose option 3 (client only):
``````
powershell -ExecutionPolicy Bypass -File install_windows.ps1
``````

Or manually register with Claude Code:
``````
claude mcp add --transport http memory --scope user http://${HOSTNAME}:${MCP_PORT}/mcp
``````

## LM Studio Configuration

Add to ``~/.lmstudio/mcp.json``:

``````json
{
  "memory": {
    "type": "http",
    "url": "http://${HOSTNAME}:${MCP_PORT}/mcp"
  }
}
``````

## Add Memory Instructions to Your Project

- **New project (no CLAUDE.md):** Copy ``claude/CLAUDE.md`` from this directory to your project root
- **Existing project:** Append the contents of ``claude/memory-instructions.md`` to your existing CLAUDE.md

## Test It

Open Claude Code and try:

1. "Store a test memory: the sky is blue"
2. "Search your memories for sky"

You should see the stored memory come back with a similarity score.

## Installation Details

- Install mode: $installMode ($serverBackendOut backend)
- Install path: $INSTALL_PATH
- MCP endpoint: http://${HOSTNAME}:${MCP_PORT}/mcp
- $dbLine
- Manifest: $INSTALL_PATH/$MANIFEST_FILE
"@ | Set-Content -Path "SETUP_COMPLETE.md" -Encoding UTF8
} else {
@"
# NotNativeMemory - Setup Complete

Your MCP memory server is installed and tested.

## Starting the Server

**HTTP mode (recommended):**
``````
python server.py
``````
Starts on http://0.0.0.0:$MCP_PORT. Other machines connect to http://${HOSTNAME}:${MCP_PORT}/mcp.

**stdio mode (launched automatically by Claude Code / LM Studio on THIS machine):**
No manual start needed. The MCP client launches the server as a child process.

## Claude Code Configuration

### This machine (already configured by installer)

Hooks have been added to ``~/.claude/settings.json``. The MCP server should
also be registered. If not, run:
``````
claude mcp add --transport stdio memory --scope user -- python $INSTALL_PATH/server.py --mcp
``````

### Remote machines (client-only)

On other machines, run the installer and choose option 3 (client only):
``````
powershell -ExecutionPolicy Bypass -File install_windows.ps1
``````

Or manually register with Claude Code:
``````
claude mcp add --transport http memory --scope user http://${HOSTNAME}:${MCP_PORT}/mcp
``````

## LM Studio Configuration

Add to ``~/.lmstudio/mcp.json``:

``````json
{
  "memory": {
    "type": "http",
    "url": "http://${HOSTNAME}:${MCP_PORT}/mcp"
  }
}
``````

## Add Memory Instructions to Your Project

- **New project (no CLAUDE.md):** Copy ``claude/CLAUDE.md`` from this directory to your project root
- **Existing project:** Append the contents of ``claude/memory-instructions.md`` to your existing CLAUDE.md

## Test It

Open Claude Code and try:

1. "Store a test memory: the sky is blue"
2. "Search your memories for sky"

You should see the stored memory come back with a similarity score.

## Installation Details

- Install mode: $installMode (python backend)
- Install path: $INSTALL_PATH
- MCP endpoint: http://${HOSTNAME}:${MCP_PORT}/mcp
- Database: ${DB_HOST}:${DB_PORT}/${DB_NAME}
- Manifest: $INSTALL_PATH/$MANIFEST_FILE
"@ | Set-Content -Path "SETUP_COMPLETE.md" -Encoding UTF8
}

Write-Host ""
Write-Host "+==========================================+"
Write-Host "|  Setup Complete!                         |"
Write-Host "+==========================================+"
Write-Host ""
Write-Step "Mode: $installMode ($serverBackendOut backend)"
Write-Step "MCP endpoint: http://${HOSTNAME}:${MCP_PORT}/mcp"
if ($installMode -eq "full") {
    Write-Step "Database: Docker internal (pgvector)"
} else {
    Write-Step "Database: ${DB_HOST}:${DB_PORT}/${DB_NAME} (remote)"
}
if ($configuredAgents -and $configuredAgents.Count -gt 0) {
    $agentLabels = @()
    foreach ($a in $configuredAgents) {
        if ($a -eq "claude") { $agentLabels += "Claude Code (~/.claude/settings.json)" }
        elseif ($a -eq "nna") { $agentLabels += "NotNativeAgent (~/.nna/config.json)" }
        elseif ($a -eq "codex") { $agentLabels += "Codex (~/.codex/hooks.json)" }
    }
    Write-Step "Hooks: configured for $($agentLabels -join ', ')"
} else {
    Write-Step "Hooks: no supported agent CLI detected (run installer again after setup)"
}
Write-Host ""
if ($useDocker) {
    Write-Info "Server is running in Docker (auto-restarts on boot)"
    Write-Info "Manage:  docker compose -f docker/docker-compose.yml --profile $composeProfile [up -d|down|logs mcp]"
} else {
    Write-Info "Start the server:  python server.py"
}
Write-Info "Full details:      SETUP_COMPLETE.md"
Write-Host ""
