# NotNativeMemory - Windows Installer
# Run: powershell -ExecutionPolicy Bypass -File install_windows.ps1

param(
    [switch]$SkipModel
)

# Use "Continue" globally. Many tools (docker, python, pip) write warnings
# and progress to stderr, which PowerShell treats as terminating errors
# under "Stop". We check $LASTEXITCODE explicitly after each critical call.
$ErrorActionPreference = "Continue"

function Write-Step($msg) { Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg) { Write-Host "[x] $msg" -ForegroundColor Red }
function Write-Info($msg) { Write-Host "    $msg" }

# Detect which supported agent CLIs are on PATH and wire hooks + MCP
# registration for whichever is present. Returns the list of agents
# that were configured so the caller can tailor the summary output.
function Configure-Agents($installPath, $mcpUrl) {
    $configured = @()

    $claudeInstalled = Get-Command claude -ErrorAction SilentlyContinue
    if ($claudeInstalled) {
        Write-Step "Configuring Claude Code hooks..."
        python claude/hooks/merge_hooks.py "$installPath" "$mcpUrl" 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Claude Code hook configuration failed. You can run this manually later:"
            Write-Info "python claude/hooks/merge_hooks.py `"$installPath`" `"$mcpUrl`""
        } else {
            $configured += "claude"
        }

        Write-Step "Registering MCP memory server with Claude Code..."
        & claude mcp add --transport http memory --scope user "$mcpUrl" 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Info "MCP server registered with Claude Code"
        } else {
            Write-Warn "Claude Code MCP registration failed. Run manually:"
            Write-Info "claude mcp add --transport http memory --scope user `"$mcpUrl`""
        }
    }

    $nncInstalled = Get-Command nnc -ErrorAction SilentlyContinue
    if ($nncInstalled) {
        Write-Step "Configuring NotNativeCoder hooks..."
        python nnc/hooks/merge_hooks.py "$installPath" "$mcpUrl" 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "NotNativeCoder hook configuration failed. You can run this manually later:"
            Write-Info "python nnc/hooks/merge_hooks.py `"$installPath`" `"$mcpUrl`""
        } else {
            $configured += "nnc"
            Write-Info "memoryUrl and hooks written to ~/.nnc/settings.json"
        }
    }

    if ($configured.Count -eq 0) {
        Write-Info "No supported agent CLIs detected (claude, nnc)."
        Write-Info "To configure manually after installing one:"
        Write-Info "  python claude/hooks/merge_hooks.py `"$installPath`" `"$mcpUrl`"  # Claude Code"
        Write-Info "  python nnc/hooks/merge_hooks.py `"$installPath`" `"$mcpUrl`"     # NotNativeCoder"
    }

    return ,$configured
}

$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $SCRIPT_DIR
$INSTALL_PATH = (Get-Location).Path -replace '\\', '/'
$HOSTNAME = $env:COMPUTERNAME.ToLower()
$MCP_PORT = 9500
$MANIFEST_FILE = ".install-manifest.json"

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
Write-Host "      MCP server runs here, Postgres is on another machine. Requires Python."
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
    try {
        $testPayload = '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
        $response = Invoke-WebRequest -Uri $MCP_URL -Method POST -Body $testPayload -ContentType "application/json" -Headers @{"Accept"="application/json"} -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop
        if ($response.StatusCode -eq 200) {
            Write-Info "Connection successful"
        }
    } catch {
        Write-Warn "Could not reach $MCP_URL - server may not be running."
        Write-Info "Hooks will be configured anyway. Start the remote server before using."
    }

    # Configure hooks + MCP registration for whichever supported agent
    # CLI is installed on this machine (Claude Code, NotNativeCoder).
    $configuredAgents = Configure-Agents $INSTALL_PATH $MCP_URL

    # Write manifest
    @{
        installed    = (Get-Date -Format "yyyy-MM-dd")
        install_mode = "client"
        install_path = $INSTALL_PATH
        components   = @("hooks")
        mcp_url      = $MCP_URL
        db_host      = $null
        db_port      = $null
    } | ConvertTo-Json | Set-Content -Path $MANIFEST_FILE -Encoding UTF8

    Write-Host ""
    Write-Host "+==========================================+"
    Write-Host "|  Client Setup Complete!                  |"
    Write-Host "+==========================================+"
    Write-Host ""
    Write-Info "Hooks configured to use: $MCP_URL"
    Write-Info "Make sure the remote MCP server is running."
    Write-Host ""
    exit 0
}

# =======================================================================
# SERVER AND FULL INSTALL PATHS
# =======================================================================

# -----------------------------------------------------------------------
# 2. Check Python (required for server-only; optional for full install)
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
    if ($installMode -eq "full") {
        Write-Info "Python not found (not required for full Docker install)"
    } else {
        Write-Err "Python 3.11+ required for server-only install."
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

    # Check Docker is running
    try {
        docker info 2>&1 | Out-Null
    } catch {
        Write-Err "Docker is not running. Start Docker Desktop and try again."
        exit 1
    }

    # Reuse existing password from .env if present (prevents mismatch
    # with an already-initialized Postgres data volume).
    if (Test-Path ".env") {
        $existingPw = Select-String -Path ".env" -Pattern "^MEMORY_DB_PASSWORD=(.+)$" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($existingPw) {
            $DB_PASSWORD = $existingPw.Matches[0].Groups[1].Value
            Write-Info "Using existing password from .env"
        }
    }
    if (-not $DB_PASSWORD) {
        $DB_PASSWORD = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
        Write-Info "Generated new database password"
    }

    # Start Postgres container first (MCP depends on it, started after model download)
    Write-Step "Starting Postgres + pgvector container..."
    $env:MEMORY_DB_PASSWORD = $DB_PASSWORD
    $env:MEMORY_DB_PORT = $DB_PORT
    $env:MEMORY_DB_NAME = $DB_NAME
    $env:MEMORY_DB_USER = $DB_USER
    & docker compose -f docker/docker-compose.yml up -d postgres 2>&1 | Out-Null

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
}

# -----------------------------------------------------------------------
# 4. Write .env
# -----------------------------------------------------------------------
Write-Step "Writing .env file..."
@"
# NotNativeMemory - Generated by install script
MEMORY_DB_HOST=$DB_HOST
MEMORY_DB_PORT=$DB_PORT
MEMORY_DB_NAME=$DB_NAME
MEMORY_DB_USER=$DB_USER
MEMORY_DB_PASSWORD=$DB_PASSWORD
MEMORY_MODEL_PATH=models/gte-base-en-v1.5
MEMORY_DEFAULT_PROJECT=
"@ | Set-Content -Path ".env" -Encoding UTF8
Write-Info "Saved to .env"

if ($installMode -eq "full") {
    # -----------------------------------------------------------------------
    # 5. Build Docker image (full mode)
    # All Python deps live inside the container - no host pip install needed.
    # -----------------------------------------------------------------------
    Write-Step "Building MCP server Docker image..."
    & docker compose -f docker/docker-compose.yml build mcp 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Docker image build failed."
        exit 1
    }

    # -----------------------------------------------------------------------
    # 6. Download embedding model via container (full mode)
    # The container has sentence-transformers installed. Mount models/
    # read-write temporarily so the download persists on the host.
    # -----------------------------------------------------------------------
    if (-not $SkipModel) {
        Write-Step "Downloading embedding model (gte-base-en-v1.5, ~130MB)..."
        if (Test-Path "models/gte-base-en-v1.5") {
            Write-Info "Model already exists, skipping download"
        } else {
            if (-not (Test-Path "models")) { New-Item "models" -ItemType Directory | Out-Null }
            & docker compose -f docker/docker-compose.yml run --rm `
                -v "${PWD}/models:/app/models" `
                mcp python -c "
from sentence_transformers import SentenceTransformer
import os
os.makedirs('models/gte-base-en-v1.5', exist_ok=True)
print('Downloading gte-base-en-v1.5...')
model = SentenceTransformer('Alibaba-NLP/gte-base-en-v1.5', trust_remote_code=True)
model.save('models/gte-base-en-v1.5')
print('Model saved to models/gte-base-en-v1.5')
" 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Err "Model download failed. Check your internet connection."
                exit 1
            }
        }
    } else {
        Write-Warn "Skipping model download (--SkipModel)"
    }

    # -----------------------------------------------------------------------
    # 7. Start containers and wait for ready (full mode)
    # -----------------------------------------------------------------------
    Write-Step "Starting MCP server container..."
    & docker compose -f docker/docker-compose.yml up -d mcp 2>&1 | Out-Null

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

    # -----------------------------------------------------------------------
    # 6. Test database connection and run schema (server-only mode)
    # -----------------------------------------------------------------------
    Write-Step "Testing database connection..."
    python -c "
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
" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Cannot connect to database. Check your settings."
        exit 1
    }
    Write-Info "Connection successful"

    Write-Step "Creating database schema..."
    python -c "
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
" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Schema creation failed."
        exit 1
    }

    # -----------------------------------------------------------------------
    # 7. Download embedding model (server-only mode - on host)
    # -----------------------------------------------------------------------
    if (-not $SkipModel) {
        Write-Step "Downloading embedding model (gte-base-en-v1.5, ~130MB)..."
        if (Test-Path "models/gte-base-en-v1.5") {
            Write-Info "Model already exists, skipping download"
        } else {
            python -c "
from sentence_transformers import SentenceTransformer
import os
os.makedirs('models', exist_ok=True)
print('Downloading gte-base-en-v1.5...')
model = SentenceTransformer('Alibaba-NLP/gte-base-en-v1.5', trust_remote_code=True)
model.save('models/gte-base-en-v1.5')
print('Model saved to models/gte-base-en-v1.5')
" 2>&1
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
# 9. Self-test
# -----------------------------------------------------------------------
Write-Step "Running self-test..."
if ($installMode -eq "full") {
    # Run selftest inside the MCP container where deps and model are available
    & docker compose -f docker/docker-compose.yml exec mcp python selftest.py 2>&1
} else {
    python selftest.py 2>&1
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
} else {
    $components = @("hooks", "server", "python_deps", "embedding_model")
}

@{
    installed    = (Get-Date -Format "yyyy-MM-dd")
    install_mode = $installMode
    install_path = $INSTALL_PATH
    components   = $components
    mcp_url      = $MCP_URL
    mcp_port     = $MCP_PORT
    db_host      = $DB_HOST
    db_port      = $DB_PORT
    db_name      = $DB_NAME
    db_user      = $DB_USER
    hostname     = $HOSTNAME
} | ConvertTo-Json | Set-Content -Path $MANIFEST_FILE -Encoding UTF8

# -----------------------------------------------------------------------
# 11. Write setup guide and print summary
# -----------------------------------------------------------------------

# Generate SETUP_COMPLETE.md with actual values filled in
if ($installMode -eq "full") {
@"
# NotNativeMemory - Setup Complete

Your MCP memory server is installed and tested.

## Starting the Server

The server runs as a Docker container alongside Postgres:

``````
docker compose -f docker/docker-compose.yml up -d      # start
docker compose -f docker/docker-compose.yml down        # stop
docker compose -f docker/docker-compose.yml logs mcp    # view logs
``````

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

- Install mode: $installMode
- Install path: $INSTALL_PATH
- MCP endpoint: http://${HOSTNAME}:${MCP_PORT}/mcp
- Database: postgres (Docker internal) / localhost:${DB_PORT} (host)
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

- Install mode: $installMode
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
Write-Step "Mode: $installMode"
Write-Step "MCP endpoint: http://${HOSTNAME}:${MCP_PORT}/mcp"
if ($installMode -eq "full") {
    Write-Step "Database: localhost:${DB_PORT}/${DB_NAME} (Docker)"
} else {
    Write-Step "Database: ${DB_HOST}:${DB_PORT}/${DB_NAME} (remote)"
}
if ($configuredAgents -and $configuredAgents.Count -gt 0) {
    $agentLabels = @()
    foreach ($a in $configuredAgents) {
        if ($a -eq "claude") { $agentLabels += "Claude Code (~/.claude/settings.json)" }
        elseif ($a -eq "nnc") { $agentLabels += "NotNativeCoder (~/.nnc/settings.json)" }
    }
    Write-Step "Hooks: configured for $($agentLabels -join ', ')"
} else {
    Write-Step "Hooks: no supported agent CLI detected (run installer again after setup)"
}
Write-Host ""
if ($installMode -eq "full") {
    Write-Info "Server is running in Docker (auto-restarts on boot)"
    Write-Info "Manage:  docker compose -f docker/docker-compose.yml [up -d|down|logs mcp]"
} else {
    Write-Info "Start the server:  python server.py"
}
Write-Info "Full details:      SETUP_COMPLETE.md"
Write-Host ""
