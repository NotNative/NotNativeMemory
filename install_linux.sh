#!/usr/bin/env bash
# NotNativeMemory - Linux/macOS Installer
# Run: bash install_linux.sh

set -e

NC='\033[0m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'

step()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[x]${NC} $1"; }
info()  { echo "    $1"; }

embedding_model_complete() {
    local path="$1"
    [ -d "$path" ] || return 1

    for file in \
        config.json \
        modules.json \
        config_sentence_transformers.json \
        tokenizer.json
    do
        [ -f "$path/$file" ] || return 1
    done
    grep -q '"model_type"' "$path/config.json" || return 1

    [ -f "$path/model.safetensors" ] || [ -f "$path/pytorch_model.bin" ]
}

# Configure hooks + MCP registration for whichever supported agent CLIs
# are installed. Sets CONFIGURED_AGENTS (bash array) so the caller can
# tailor the summary output. Usage: configure_agents <install_path> <mcp_url>
CONFIGURED_AGENTS=()
configure_agents() {
    local install_path="$1"
    local mcp_url="$2"
    CONFIGURED_AGENTS=()

    if command -v claude &> /dev/null; then
        step "Configuring Claude Code hooks..."
        if python3 hook_bundles/claude/notnative-memory/merge_hooks.py "$install_path" "$mcp_url"; then
            CONFIGURED_AGENTS+=("claude")
        else
            warn "Claude Code hook configuration failed. You can run this manually later:"
            info "python3 hook_bundles/claude/notnative-memory/merge_hooks.py \"$install_path\" \"$mcp_url\""
        fi

        step "Registering MCP memory server with Claude Code..."
        if claude mcp add --transport http memory --scope user "$mcp_url" 2>&1; then
            info "MCP server registered with Claude Code"
        else
            warn "Claude Code MCP registration failed. Run manually:"
            info "claude mcp add --transport http memory --scope user \"$mcp_url\""
        fi
    fi

    if command -v codex &> /dev/null || [ -d "$HOME/.codex" ]; then
        step "Configuring Codex hooks..."
        if python3 hook_bundles/codex/notnative-memory/merge_hooks.py "$install_path" "$mcp_url"; then
            CONFIGURED_AGENTS+=("codex")
            info "Codex hooks merged into ~/.codex/hooks.json"
            info "Codex may ask you to trust these hooks with /hooks before they run."
        else
            warn "Codex hook configuration failed. You can run this manually later:"
            info "python3 hook_bundles/codex/notnative-memory/merge_hooks.py \"$install_path\" \"$mcp_url\""
        fi
    fi

    if [ ${#CONFIGURED_AGENTS[@]} -eq 0 ]; then
        info "No supported agent CLIs detected (claude, codex)."
        info "To configure manually after installing one:"
        info "  python3 hook_bundles/claude/notnative-memory/merge_hooks.py \"$install_path\" \"$mcp_url\"  # Claude Code"
        info "  python3 hook_bundles/codex/notnative-memory/merge_hooks.py \"$install_path\" \"$mcp_url\"   # Codex"
    fi
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
INSTALL_PATH="$SCRIPT_DIR"
HOSTNAME_VAL=$(hostname | tr '[:upper:]' '[:lower:]')
MCP_PORT=9500
MANIFEST_FILE=".install-manifest.json"

echo ""
echo "+==========================================+"
echo "|  NotNativeMemory - MCP Memory Server     |"
echo "|  Persistent memory for MCP-compatible    |"
echo "|  agents (Claude Code, NNC, LM Studio...) |"
echo "+==========================================+"
echo ""

# -----------------------------------------------------------------------
# Load existing manifest if present (for idempotent re-runs)
# -----------------------------------------------------------------------
EXISTING_MODE=""
EXISTING_MCP_URL=""
EXISTING_DB_HOST=""
EXISTING_DB_PORT=""
if [ -f "$MANIFEST_FILE" ]; then
    # Parse JSON with python (available since we need it anyway)
    EXISTING_MODE=$(python3 -c "import json; d=json.load(open('$MANIFEST_FILE')); print(d.get('install_mode',''))" 2>/dev/null || true)
    EXISTING_MCP_URL=$(python3 -c "import json; d=json.load(open('$MANIFEST_FILE')); print(d.get('mcp_url',''))" 2>/dev/null || true)
    EXISTING_DB_HOST=$(python3 -c "import json; d=json.load(open('$MANIFEST_FILE')); print(d.get('db_host',''))" 2>/dev/null || true)
    EXISTING_DB_PORT=$(python3 -c "import json; d=json.load(open('$MANIFEST_FILE')); print(d.get('db_port',''))" 2>/dev/null || true)
    if [ -n "$EXISTING_MODE" ]; then
        info "Found existing installation: $EXISTING_MODE"
        info "Re-running will update the existing configuration."
        echo ""
    fi
fi

# -----------------------------------------------------------------------
# 1. What are we installing?
# -----------------------------------------------------------------------
step "Installation Mode"
echo ""
echo "  What are you setting up on this machine?"
echo ""
echo -e "  ${CYAN}1${NC} - Full install (database + server, all in Docker)"
echo "      Everything runs here. No Python required."
echo ""
echo -e "  ${CYAN}2${NC} - Server only (server + hooks, database is remote)"
echo "      MCP server runs here, Postgres is on another machine."
echo "      Docker container (auto-restarts) or host Python process."
echo ""
echo -e "  ${CYAN}3${NC} - Client only (hooks only, server is remote)"
echo "      Just configure Claude Code to use a remote MCP server."
echo "      No Python dependencies, no model download, no database."
echo ""

# Show previous mode as default
DEFAULT_MODE=""
case "$EXISTING_MODE" in
    full)   DEFAULT_MODE="1" ;;
    server) DEFAULT_MODE="2" ;;
    client) DEFAULT_MODE="3" ;;
esac
if [ -n "$DEFAULT_MODE" ]; then
    info "Previous install was mode $DEFAULT_MODE ($EXISTING_MODE)"
fi

read -rp "  Choice [1/2/3]: " MODE_CHOICE
MODE_CHOICE="${MODE_CHOICE:-$DEFAULT_MODE}"

case "$MODE_CHOICE" in
    1) INSTALL_MODE="full" ;;
    2) INSTALL_MODE="server" ;;
    3) INSTALL_MODE="client" ;;
    *)
        err "Invalid choice. Run the installer again."
        exit 1
        ;;
esac

echo ""
step "Installing: $INSTALL_MODE"

# =======================================================================
# CLIENT-ONLY PATH
# =======================================================================
if [ "$INSTALL_MODE" = "client" ]; then
    echo ""
    step "Remote MCP Server Configuration"
    echo ""

    # Reuse existing values if re-running
    DEFAULT_HOST=""
    DEFAULT_PORT="9500"
    if [ -n "$EXISTING_MCP_URL" ]; then
        # Parse host and port from existing URL
        DEFAULT_HOST=$(echo "$EXISTING_MCP_URL" | sed -n 's|http://\([^:/]*\).*|\1|p')
        PARSED_PORT=$(echo "$EXISTING_MCP_URL" | sed -n 's|http://[^:]*:\([0-9]*\).*|\1|p')
        if [ -n "$PARSED_PORT" ]; then DEFAULT_PORT="$PARSED_PORT"; fi
        info "Current: $DEFAULT_HOST:$DEFAULT_PORT"
    fi

    read -rp "  MCP server hostname (e.g. myserver, 192.168.1.10): " MCP_HOST
    MCP_HOST="${MCP_HOST:-$DEFAULT_HOST}"
    if [ -z "$MCP_HOST" ]; then
        err "Hostname is required for client-only install."
        exit 1
    fi

    read -rp "  MCP server port (default: $DEFAULT_PORT): " MCP_PORT_INPUT
    MCP_PORT_INPUT="${MCP_PORT_INPUT:-$DEFAULT_PORT}"

    MCP_URL="http://${MCP_HOST}:${MCP_PORT_INPUT}/mcp"
    info "URL: $MCP_URL"

    # Test connectivity
    step "Testing MCP server connection..."
    TEST_PAYLOAD='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
    if curl -s -o /dev/null -w "%{http_code}" -X POST "$MCP_URL" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json" \
        -d "$TEST_PAYLOAD" --connect-timeout 10 2>/dev/null | grep -q "200"; then
        info "Connection successful"
    else
        warn "Could not reach $MCP_URL - server may not be running."
        info "Hooks will be configured anyway. Start the remote server before using."
    fi

    # Configure hooks + MCP registration for whichever supported agent
    # CLI is installed on this machine (Claude Code, NotNativeCoder).
    configure_agents "$INSTALL_PATH" "$MCP_URL"

    # Write manifest
    python3 -c "
import json
manifest = {
    'installed': '$(date +%Y-%m-%d)',
    'install_mode': 'client',
    'install_path': '$INSTALL_PATH',
    'components': ['hooks'],
    'mcp_url': '$MCP_URL',
    'db_host': None,
    'db_port': None,
}
with open('$MANIFEST_FILE', 'w') as f:
    json.dump(manifest, f, indent=2)
"

    echo ""
    echo "+==========================================+"
    echo "|  Client Setup Complete!                  |"
    echo "+==========================================+"
    echo ""
    info "Hooks configured to use: $MCP_URL"
    info "Make sure the remote MCP server is running."
    echo ""
    exit 0
fi

# =======================================================================
# SERVER AND FULL INSTALL PATHS
# =======================================================================

# -----------------------------------------------------------------------
# 1b. Server backend sub-choice (server mode only)
# -----------------------------------------------------------------------
# Full mode is always Docker. For server mode we offer either a Docker
# container (auto-restarts, consistent with full mode) or a host Python
# process. If Docker isn't available, silently fall through to Python.
SERVER_BACKEND=""  # docker | python; only meaningful in server mode
if [ "$INSTALL_MODE" = "server" ]; then
    DOCKER_AVAILABLE=false
    if command -v docker &> /dev/null && docker info &> /dev/null; then
        DOCKER_AVAILABLE=true
    fi

    if [ "$DOCKER_AVAILABLE" = true ]; then
        step "Server Backend"
        echo ""
        echo "  How should the MCP server run on this machine?"
        echo ""
        echo -e "  ${CYAN}d${NC} - Docker container (recommended)"
        echo "      Auto-restarts on reboot. No Python needed on host."
        echo ""
        echo -e "  ${CYAN}p${NC} - Host Python process"
        echo "      Run server.py directly. Requires Python 3.11+."
        echo ""

        DEFAULT_BACKEND="d"
        if [ -n "$EXISTING_MODE" ] && [ "$EXISTING_MODE" = "server" ]; then
            EXISTING_BACKEND=$(python3 -c "import json; d=json.load(open('$MANIFEST_FILE')); print(d.get('server_backend',''))" 2>/dev/null || true)
            case "$EXISTING_BACKEND" in
                docker) DEFAULT_BACKEND="d" ;;
                python) DEFAULT_BACKEND="p" ;;
            esac
            info "Previous backend: $EXISTING_BACKEND"
        fi

        read -rp "  Choice [d/p] (default: $DEFAULT_BACKEND): " BACKEND_CHOICE
        BACKEND_CHOICE="${BACKEND_CHOICE:-$DEFAULT_BACKEND}"
        case "$BACKEND_CHOICE" in
            d|D) SERVER_BACKEND="docker" ;;
            p|P) SERVER_BACKEND="python" ;;
            *)
                err "Invalid choice. Run the installer again."
                exit 1
                ;;
        esac
    else
        info "Docker not available; using host Python process for the server."
        SERVER_BACKEND="python"
    fi
fi

# USE_DOCKER drives the install path from here on. Full mode is always
# Docker; server mode depends on the sub-choice above.
if [ "$INSTALL_MODE" = "full" ] || [ "$SERVER_BACKEND" = "docker" ]; then
    USE_DOCKER=true
    COMPOSE_PROFILE="server"
    [ "$INSTALL_MODE" = "full" ] && COMPOSE_PROFILE="full"
else
    USE_DOCKER=false
    COMPOSE_PROFILE=""
fi

# -----------------------------------------------------------------------
# 2. Check Python (required when the server runs as a host process)
# -----------------------------------------------------------------------
PYTHON_AVAILABLE=false
if command -v python3 &> /dev/null; then
    PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 11 ]; then
        PYTHON_AVAILABLE=true
        info "Found Python $PY_VERSION"
    fi
fi

if [ "$PYTHON_AVAILABLE" = false ]; then
    if [ "$USE_DOCKER" = true ]; then
        info "Python not found (not required for Docker backend)"
    else
        err "Python 3.11+ required for the host Python server backend."
        exit 1
    fi
fi

# -----------------------------------------------------------------------
# 3. Database configuration
# -----------------------------------------------------------------------
DB_HOST="localhost"
DB_PORT="5433"
DB_NAME="notnative_memory"
DB_USER="memory"
DB_PASSWORD=""
APP_DB_USER="memory_app"
APP_DB_PASSWORD=""

if [ "$INSTALL_MODE" = "full" ]; then
    # Local Docker database
    step "Checking Docker..."
    if ! command -v docker &> /dev/null; then
        err "Docker not found. Install Docker and try again."
        exit 1
    fi
    info "Found $(docker --version)"

    if ! docker info &> /dev/null; then
        err "Docker is not running. Start Docker and try again."
        exit 1
    fi

    # Reuse existing passwords from .env if present (idempotent install)
    if [ -f ".env" ]; then
        EXISTING_PW=$(grep -oP '^MEMORY_DB_PASSWORD=\K.+' .env 2>/dev/null || true)
        if [ -n "$EXISTING_PW" ]; then
            DB_PASSWORD="$EXISTING_PW"
            info "Using existing MEMORY_DB_PASSWORD from .env"
        fi
        EXISTING_APP_PW=$(grep -oP '^MEMORY_APP_DB_PASSWORD=\K.+' .env 2>/dev/null || true)
        if [ -n "$EXISTING_APP_PW" ]; then
            APP_DB_PASSWORD="$EXISTING_APP_PW"
            info "Using existing MEMORY_APP_DB_PASSWORD from .env"
        fi
    fi
    if [ -z "$DB_PASSWORD" ]; then
        DB_PASSWORD=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24)))")
        info "Generated new database password"
    fi
    if [ -z "$APP_DB_PASSWORD" ]; then
        # url-safe characters only: no single-quote risk when the
        # password is interpolated into docker init SQL literals.
        APP_DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
        info "Generated memory_app role password (RLS enforcement enabled)"
    fi

    # Start Postgres container first (MCP depends on it, started after model download)
    # Use an explicit `if !` check so a failure here produces a
    # context-specific error instead of a bare `set -e` abort with the
    # raw compose output as the only signal.
    step "Starting Postgres + pgvector container..."
    if ! MEMORY_DB_PASSWORD="$DB_PASSWORD" \
         MEMORY_DB_PORT="$DB_PORT" \
         MEMORY_DB_NAME="$DB_NAME" \
         MEMORY_DB_USER="$DB_USER" \
         MEMORY_APP_DB_USER="$APP_DB_USER" \
         MEMORY_APP_DB_PASSWORD="$APP_DB_PASSWORD" \
         docker compose --progress=plain -f docker/docker-compose.yml --profile full up -d postgres 2>&1; then
        err "Failed to start Postgres container. See the compose output above."
        exit 1
    fi

    # Wait for healthy
    step "Waiting for Postgres to be ready..."
    MAX_WAIT=30
    WAITED=0
    while [ $WAITED -lt $MAX_WAIT ]; do
        HEALTH=$(docker inspect --format='{{.State.Health.Status}}' MCP-postgres 2>/dev/null || echo "starting")
        if [ "$HEALTH" = "healthy" ]; then break; fi
        sleep 2
        WAITED=$((WAITED + 2))
        info "Waiting... ($WAITED/$MAX_WAIT seconds)"
    done
    if [ $WAITED -ge $MAX_WAIT ]; then
        err "Postgres did not become healthy within $MAX_WAIT seconds."
        exit 1
    fi
    info "Postgres is ready"

elif [ "$INSTALL_MODE" = "server" ]; then
    # Remote database
    step "Remote Database Configuration"
    echo ""

    if [ -n "$EXISTING_DB_HOST" ]; then
        info "Current: $EXISTING_DB_HOST:$EXISTING_DB_PORT"
    fi

    read -rp "  Postgres host (e.g. myserver, 192.168.1.10): " INPUT
    DB_HOST="${INPUT:-${EXISTING_DB_HOST:-localhost}}"

    read -rp "  Postgres port (default: 5433): " INPUT
    DB_PORT="${INPUT:-${EXISTING_DB_PORT:-5433}}"

    read -rp "  Database name (default: notnative_memory): " INPUT
    DB_NAME="${INPUT:-notnative_memory}"

    read -rp "  Database user (default: memory): " INPUT
    DB_USER="${INPUT:-memory}"

    read -rsp "  Database password: " DB_PASSWORD
    echo ""
    # Fall back to existing .env password if user just hits enter
    if [ -z "$DB_PASSWORD" ] && [ -f ".env" ]; then
        EXISTING_PW=$(grep -oP '^MEMORY_DB_PASSWORD=\K.+' .env 2>/dev/null || true)
        if [ -n "$EXISTING_PW" ]; then
            DB_PASSWORD="$EXISTING_PW"
            info "Using existing password from .env"
        fi
    fi
    if [ -z "$DB_PASSWORD" ]; then
        err "Password is required for remote database."
        exit 1
    fi

    # Remote-DB path: generate the app-role password if not already
    # set. The installer will CREATE ROLE later via asyncpg (needs
    # MEMORY_DB_USER to have CREATE ROLE privilege on the remote).
    if [ -f ".env" ]; then
        EXISTING_APP_PW=$(grep -oP '^MEMORY_APP_DB_PASSWORD=\K.+' .env 2>/dev/null || true)
        if [ -n "$EXISTING_APP_PW" ]; then
            APP_DB_PASSWORD="$EXISTING_APP_PW"
            info "Using existing MEMORY_APP_DB_PASSWORD from .env"
        fi
    fi
    if [ -z "$APP_DB_PASSWORD" ]; then
        APP_DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
        info "Generated memory_app role password (RLS enforcement enabled)"
    fi
fi

# -----------------------------------------------------------------------
# 4. Write .env
# -----------------------------------------------------------------------
# The values in .env are what the running server (container or host
# python) will use to connect. For full mode the server is a container
# on the internal Docker network, so it reaches Postgres by service
# name. For remote-DB modes (server+docker, server+python) it uses the
# host/port the user entered.
if [ "$INSTALL_MODE" = "full" ]; then
    ENV_DB_HOST="postgres"
    ENV_DB_PORT="5432"
else
    ENV_DB_HOST="$DB_HOST"
    ENV_DB_PORT="$DB_PORT"
fi

step "Writing .env file..."
cat > .env << EOF
# NotNativeMemory - Generated by install script
MEMORY_DB_HOST=$ENV_DB_HOST
MEMORY_DB_PORT=$ENV_DB_PORT
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
EOF
info "Saved to .env"

if [ "$USE_DOCKER" = true ]; then
    # -----------------------------------------------------------------------
    # 5. Build Docker image
    # All Python deps live inside the container - no host pip install needed.
    # -----------------------------------------------------------------------
    step "Building MCP server Docker image..."
    docker compose --progress=plain -f docker/docker-compose.yml --profile "$COMPOSE_PROFILE" build mcp 2>&1
    if [ $? -ne 0 ]; then
        err "Docker image build failed."
        exit 1
    fi

    # -----------------------------------------------------------------------
    # 6. Download embedding model via container
    # The container has sentence-transformers installed. Mount models/
    # read-write temporarily so the download persists on the host.
    # -----------------------------------------------------------------------
    if [ "${SKIP_MODEL:-}" != "1" ]; then
        step "Downloading embedding model (gte-large-en-v1.5, ~870MB)..."
        if embedding_model_complete "models/gte-large-en-v1.5"; then
            info "Model already exists, skipping download"
        else
            if [ -d "models/gte-large-en-v1.5" ]; then
                warn "Existing model directory is incomplete; re-downloading it."
            fi
            mkdir -p models
            docker compose --progress=plain -f docker/docker-compose.yml --profile "$COMPOSE_PROFILE" run --rm \
                -v "$(pwd)/models:/app/models" \
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
            if [ $? -ne 0 ]; then
                err "Model download failed. Check your internet connection."
                exit 1
            fi
        fi
    else
        warn "Skipping model download (SKIP_MODEL=1)"
    fi

    # -----------------------------------------------------------------------
    # 6b. Remote schema apply (server+docker only)
    # Full mode's Postgres container applies config/schema.sql via its
    # init-script mount, so we only need to do this when the DB is remote.
    # The server's migration bootstrap runs on first tool call, but the
    # base schema must exist first.
    # -----------------------------------------------------------------------
    if [ "$INSTALL_MODE" = "server" ]; then
        step "Testing remote database connection from container..."
        docker compose --progress=plain -f docker/docker-compose.yml --profile "$COMPOSE_PROFILE" run --rm mcp python -c "
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
        if [ $? -ne 0 ]; then
            err "Cannot connect to remote database from container. Check your settings."
            exit 1
        fi
        info "Connection successful"

        step "Applying schema to remote database..."
        docker compose --progress=plain -f docker/docker-compose.yml --profile "$COMPOSE_PROFILE" run --rm mcp python -c "
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
        if [ $? -ne 0 ]; then
            err "Schema apply failed."
            exit 1
        fi
    fi

    # -----------------------------------------------------------------------
    # 6c. Ensure memory_app role exists (all Docker modes)
    # For full mode, the postgres container's 02-roles.sh handled this at
    # first init; running again is idempotent and heals cases where the
    # DB volume predates the addition of the init script.
    # For server mode, this is the only path that creates the role.
    # -----------------------------------------------------------------------
    step "Ensuring memory_app role (RLS enforcement)..."
    docker compose --progress=plain -f docker/docker-compose.yml --profile "$COMPOSE_PROFILE" run --rm mcp python docker/init/ensure_app_role.py
    if [ $? -ne 0 ]; then
        err "Role provisioning failed. MCP would fail to authenticate as $APP_DB_USER."
        info "If this is an existing Docker volume and .env was regenerated, restore the old .env password or reset the Docker Postgres volume."
        info "To opt out of RLS enforcement, blank MEMORY_APP_DB_USER and MEMORY_APP_DB_PASSWORD in .env, then re-run."
        exit 1
    fi

    # -----------------------------------------------------------------------
    # 7. Start containers and wait for ready
    # -----------------------------------------------------------------------
    step "Starting MCP server container..."
    if ! docker compose --progress=plain -f docker/docker-compose.yml --profile "$COMPOSE_PROFILE" up -d --force-recreate mcp 2>&1; then
        err "Failed to start MCP server container. See the compose output above."
        info "Check logs: docker compose -f docker/docker-compose.yml logs mcp"
        exit 1
    fi

    # Wait for MCP server to be ready (model loading takes 10-30s on first start)
    step "Waiting for MCP server to be ready..."
    MAX_WAIT=60
    WAITED=0
    while [ $WAITED -lt $MAX_WAIT ]; do
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://localhost:${MCP_PORT}/mcp" \
            -H "Content-Type: application/json" \
            -H "Accept: application/json" \
            -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
            --connect-timeout 3 2>/dev/null || echo "000")
        if [ "$HTTP_CODE" = "200" ]; then break; fi
        sleep 3
        WAITED=$((WAITED + 3))
        info "Waiting... ($WAITED/$MAX_WAIT seconds)"
    done
    if [ $WAITED -ge $MAX_WAIT ]; then
        err "MCP server did not become ready within $MAX_WAIT seconds."
        info "Check logs: docker compose -f docker/docker-compose.yml logs mcp"
        exit 1
    fi
    info "MCP server is ready"

else
    # -----------------------------------------------------------------------
    # 5. Install Python dependencies (host python server)
    # -----------------------------------------------------------------------
    step "Installing Python dependencies..."
    if ! pip install -r requirements.txt --quiet 2>&1; then
        err "pip install failed. See output above."
        exit 1
    fi

    # -----------------------------------------------------------------------
    # 6. Test database connection and run schema (host python server)
    # -----------------------------------------------------------------------
    step "Testing database connection..."
    python3 -c "
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
    if [ $? -ne 0 ]; then
        err "Cannot connect to database. Check your settings."
        exit 1
    fi
    info "Connection successful"

    step "Creating database schema..."
    python3 -c "
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
    if [ $? -ne 0 ]; then
        err "Schema creation failed."
        exit 1
    fi

    step "Ensuring memory_app role (RLS enforcement)..."
    # ensure_app_role.py loads .env itself via python-dotenv, so no
    # pre-sourcing needed here.
    if ! python3 docker/init/ensure_app_role.py; then
        err "Role provisioning failed. MCP would fail to authenticate as $APP_DB_USER."
        info "To opt out of RLS enforcement, blank MEMORY_APP_DB_USER and MEMORY_APP_DB_PASSWORD in .env, then re-run."
        exit 1
    fi

    # -----------------------------------------------------------------------
    # 7. Download embedding model (host python server)
    # -----------------------------------------------------------------------
    if [ "${SKIP_MODEL:-}" != "1" ]; then
        step "Downloading embedding model (gte-large-en-v1.5, ~870MB)..."
        if embedding_model_complete "models/gte-large-en-v1.5"; then
            info "Model already exists, skipping download"
        else
            if [ -d "models/gte-large-en-v1.5" ]; then
                warn "Existing model directory is incomplete; re-downloading it."
            fi
            python3 -c "
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
            if [ $? -ne 0 ]; then
                err "Model download failed. Check your internet connection."
                exit 1
            fi
        fi
    else
        warn "Skipping model download (SKIP_MODEL=1)"
    fi
fi

# -----------------------------------------------------------------------
# 8b. Firewall rule for inbound MCP port (Linux: ufw / firewall-cmd)
# Server modes only (client-only mode exits earlier). The .env we wrote
# pins MEMORY_BIND_HOST=0.0.0.0; without a firewall hole the port is
# usually unreachable from the LAN. Opt-in via prompt, idempotent on
# re-runs (skips when the rule already exists).
# -----------------------------------------------------------------------
# shellcheck source=install_lib/firewall.sh
. "$SCRIPT_DIR/install_lib/firewall.sh"
FW_TOOL=$(nnm_firewall_detect_tool)
if [ "$FW_TOOL" = "none" ]; then
    info "No active host firewall detected (ufw/firewall-cmd); skipping firewall configuration."
elif nnm_firewall_rule_exists "$FW_TOOL" "$MCP_PORT"; then
    info "Firewall rule for port $MCP_PORT already present in $FW_TOOL; skipping."
else
    step "Host firewall ($FW_TOOL)"
    echo ""
    read -r -p "  Open inbound TCP $MCP_PORT in $FW_TOOL? [Y/n] " fw_ans
    if [ -z "$fw_ans" ] || [ "$fw_ans" = "y" ] || [ "$fw_ans" = "Y" ]; then
        FW_RESULT=$(nnm_firewall_apply "$FW_TOOL" "$MCP_PORT" "0")
        case "$FW_RESULT" in
            created)
                info "Firewall rule added via $FW_TOOL for port $MCP_PORT."
                ;;
            skipped-exists)
                info "Firewall rule already present, skipping."
                ;;
            failed)
                FW_CMD=$(nnm_firewall_build_cmd "$FW_TOOL" "$MCP_PORT")
                warn "Could not add firewall rule (likely missing privilege)."
                info "Run this manually as root:"
                info "  $FW_CMD"
                ;;
        esac
    else
        info "Firewall left unchanged. Remote clients will not be able to reach $MCP_PORT until you open it."
    fi
fi

# -----------------------------------------------------------------------
# 9. Self-test
# -----------------------------------------------------------------------
step "Running self-test..."
if [ "$USE_DOCKER" = true ]; then
    # Run selftest inside the MCP container where deps and model are available
    docker compose --progress=plain -f docker/docker-compose.yml exec mcp python scripts/selftest.py
else
    python3 scripts/selftest.py
fi
if [ $? -ne 0 ]; then
    err "Self-test failed. Check the output above for details."
    exit 1
fi

# -----------------------------------------------------------------------
# 10. Configure hooks + MCP registration for any installed agent CLIs
# -----------------------------------------------------------------------
MCP_URL="http://localhost:$MCP_PORT/mcp"
configure_agents "$INSTALL_PATH" "$MCP_URL"
if [ ${#CONFIGURED_AGENTS[@]} -eq 0 ]; then
    info "Install hooks on client machines using option 3 (client only) once you have Claude Code or NotNativeCoder set up."
fi

# -----------------------------------------------------------------------
# 11. Write manifest
# -----------------------------------------------------------------------
if [ "$INSTALL_MODE" = "full" ]; then
    COMPONENTS='["hooks", "server", "docker", "embedding_model", "database"]'
    SERVER_BACKEND_OUT="docker"
elif [ "$SERVER_BACKEND" = "docker" ]; then
    COMPONENTS='["hooks", "server", "docker", "embedding_model"]'
    SERVER_BACKEND_OUT="docker"
else
    COMPONENTS='["hooks", "server", "python_deps", "embedding_model"]'
    SERVER_BACKEND_OUT="python"
fi

python3 -c "
import json
manifest = {
    'installed': '$(date +%Y-%m-%d)',
    'install_mode': '$INSTALL_MODE',
    'server_backend': '$SERVER_BACKEND_OUT',
    'install_path': '$INSTALL_PATH',
    'components': $COMPONENTS,
    'mcp_url': '$MCP_URL',
    'mcp_port': $MCP_PORT,
    'db_host': '$DB_HOST',
    'db_port': '$DB_PORT',
    'db_name': '$DB_NAME',
    'db_user': '$DB_USER',
    'hostname': '$HOSTNAME_VAL',
}
with open('$MANIFEST_FILE', 'w') as f:
    json.dump(manifest, f, indent=2)
"

# -----------------------------------------------------------------------
# 12. Write setup guide and print summary
# -----------------------------------------------------------------------
if [ "$USE_DOCKER" = true ]; then
    if [ "$INSTALL_MODE" = "full" ]; then
        SERVER_MGMT_BLURB="The server runs as a Docker container alongside Postgres:

\`\`\`
docker compose -f docker/docker-compose.yml --profile full up -d    # start
docker compose -f docker/docker-compose.yml --profile full down     # stop
docker compose -f docker/docker-compose.yml logs mcp                 # view logs
\`\`\`"
        DB_LINE="Database: postgres (Docker internal)"
    else
        SERVER_MGMT_BLURB="The server runs as a Docker container against your remote Postgres:

\`\`\`
docker compose -f docker/docker-compose.yml --profile server up -d mcp   # start
docker compose -f docker/docker-compose.yml --profile server down        # stop
docker compose -f docker/docker-compose.yml logs mcp                      # view logs
\`\`\`"
        DB_LINE="Database: ${DB_HOST}:${DB_PORT}/${DB_NAME} (remote)"
    fi

cat > SETUP_COMPLETE.md << EOF
# NotNativeMemory - Setup Complete

Your MCP memory server is installed and tested.

## Starting the Server

$SERVER_MGMT_BLURB

The server auto-starts on boot (\`restart: unless-stopped\`).

## Claude Code Configuration

### This machine (already configured by installer)

Hooks have been added to \`~/.claude/settings.json\`.

### Remote machines (client-only)

On other machines, run the installer and choose option 3 (client only):
\`\`\`
bash install_linux.sh
\`\`\`

Or manually register with Claude Code:
\`\`\`
claude mcp add --transport http memory --scope user http://${HOSTNAME_VAL}:${MCP_PORT}/mcp
\`\`\`

## LM Studio Configuration

Add to \`~/.lmstudio/mcp.json\`:

\`\`\`json
{
  "memory": {
    "type": "http",
    "url": "http://${HOSTNAME_VAL}:${MCP_PORT}/mcp"
  }
}
\`\`\`

## Add Memory Instructions to Your Project

- **New project (no CLAUDE.md):** Copy \`claude/CLAUDE.md\` from this directory to your project root
- **Existing project:** Append the contents of \`claude/memory-instructions.md\` to your existing CLAUDE.md

## Test It

Open Claude Code and try:

1. "Store a test memory: the sky is blue"
2. "Search your memories for sky"

You should see the stored memory come back with a similarity score.

## Installation Details

- Install mode: $INSTALL_MODE ($SERVER_BACKEND_OUT backend)
- Install path: $INSTALL_PATH
- MCP endpoint: http://${HOSTNAME_VAL}:${MCP_PORT}/mcp
- $DB_LINE
- Manifest: $INSTALL_PATH/$MANIFEST_FILE
EOF
else
cat > SETUP_COMPLETE.md << EOF
# NotNativeMemory - Setup Complete

Your MCP memory server is installed and tested.

## Starting the Server

**HTTP mode (recommended):**
\`\`\`
python3 server.py
\`\`\`
Starts on http://0.0.0.0:$MCP_PORT. Other machines connect to http://${HOSTNAME_VAL}:${MCP_PORT}/mcp.

**stdio mode (launched automatically by Claude Code / LM Studio on THIS machine):**
No manual start needed. The MCP client launches the server as a child process.

## Claude Code Configuration

### This machine (already configured by installer)

Hooks have been added to \`~/.claude/settings.json\`. The MCP server should
also be registered. If not, run:
\`\`\`
claude mcp add --transport stdio memory --scope user -- python3 $INSTALL_PATH/server.py --mcp
\`\`\`

### Remote machines (client-only)

On other machines, run the installer and choose option 3 (client only):
\`\`\`
bash install_linux.sh
\`\`\`

Or manually register with Claude Code:
\`\`\`
claude mcp add --transport http memory --scope user http://${HOSTNAME_VAL}:${MCP_PORT}/mcp
\`\`\`

## LM Studio Configuration

Add to \`~/.lmstudio/mcp.json\`:

\`\`\`json
{
  "memory": {
    "type": "http",
    "url": "http://${HOSTNAME_VAL}:${MCP_PORT}/mcp"
  }
}
\`\`\`

## Add Memory Instructions to Your Project

- **New project (no CLAUDE.md):** Copy \`claude/CLAUDE.md\` from this directory to your project root
- **Existing project:** Append the contents of \`claude/memory-instructions.md\` to your existing CLAUDE.md

## Test It

Open Claude Code and try:

1. "Store a test memory: the sky is blue"
2. "Search your memories for sky"

You should see the stored memory come back with a similarity score.

## Installation Details

- Install mode: $INSTALL_MODE (python backend)
- Install path: $INSTALL_PATH
- MCP endpoint: http://${HOSTNAME_VAL}:${MCP_PORT}/mcp
- Database: ${DB_HOST}:${DB_PORT}/${DB_NAME}
- Manifest: $INSTALL_PATH/$MANIFEST_FILE
EOF
fi

echo ""
echo "+==========================================+"
echo "|  Setup Complete!                         |"
echo "+==========================================+"
echo ""
step "Mode: $INSTALL_MODE ($SERVER_BACKEND_OUT backend)"
step "MCP endpoint: http://${HOSTNAME_VAL}:${MCP_PORT}/mcp"
if [ "$INSTALL_MODE" = "full" ]; then
    step "Database: Docker internal (pgvector)"
else
    step "Database: ${DB_HOST}:${DB_PORT}/${DB_NAME} (remote)"
fi
if [ ${#CONFIGURED_AGENTS[@]} -gt 0 ]; then
    agent_summary=""
    for a in "${CONFIGURED_AGENTS[@]}"; do
        case "$a" in
            claude) label="Claude Code (~/.claude/settings.json)" ;;
            codex)  label="Codex (~/.codex/hooks.json)" ;;
            *)      label="$a" ;;
        esac
        agent_summary="${agent_summary:+$agent_summary, }$label"
    done
    step "Hooks: configured for $agent_summary"
else
    step "Hooks: no supported agent CLI detected (run installer again after setup)"
fi
echo ""
if [ "$USE_DOCKER" = true ]; then
    info "Server is running in Docker (auto-restarts on boot)"
    info "Manage:  docker compose -f docker/docker-compose.yml --profile $COMPOSE_PROFILE [up -d|down|logs mcp]"
else
    info "Start the server:  python3 server.py"
fi
info "Full details:      SETUP_COMPLETE.md"
echo ""
