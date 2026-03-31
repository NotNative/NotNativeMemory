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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
INSTALL_PATH="$SCRIPT_DIR"
HOSTNAME_VAL=$(hostname | tr '[:upper:]' '[:lower:]')
MCP_PORT=9500
MANIFEST_FILE=".install-manifest.json"

echo ""
echo "+==========================================+"
echo "|  NotNativeMemory - MCP Memory Server     |"
echo "|  Persistent memory for Claude Code / LMS |"
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
echo "      MCP server runs here, Postgres is on another machine. Requires Python."
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

    # Configure hooks and register MCP server (if Claude Code is installed)
    if command -v claude &> /dev/null; then
        step "Configuring Claude Code hooks..."
        python3 claude/hooks/merge_hooks.py "$INSTALL_PATH" "$MCP_URL" || {
            warn "Hook configuration failed. You can run this manually later:"
            info "python3 claude/hooks/merge_hooks.py \"$INSTALL_PATH\" \"$MCP_URL\""
        }

        step "Registering MCP memory server with Claude Code..."
        if claude mcp add --transport http memory --scope user "$MCP_URL" 2>&1; then
            info "MCP server registered (tools: memory_store, memory_search, memory_forget, memory_list)"
        else
            warn "MCP registration failed. You can run this manually:"
            info "claude mcp add --transport http memory --scope user \"$MCP_URL\""
        fi
    else
        info "Claude Code not detected - skipping hook and MCP registration"
        info "To configure manually after installing Claude Code:"
        info "  python3 claude/hooks/merge_hooks.py \"$INSTALL_PATH\" \"$MCP_URL\""
        info "  claude mcp add --transport http memory --scope user \"$MCP_URL\""
    fi

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
# 2. Check Python (required for server-only; optional for full install)
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
    if [ "$INSTALL_MODE" = "full" ]; then
        info "Python not found (not required for full Docker install)"
    else
        err "Python 3.11+ required for server-only install."
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

    # Reuse existing password from .env if present
    if [ -f ".env" ]; then
        EXISTING_PW=$(grep -oP '^MEMORY_DB_PASSWORD=\K.+' .env 2>/dev/null || true)
        if [ -n "$EXISTING_PW" ]; then
            DB_PASSWORD="$EXISTING_PW"
            info "Using existing password from .env"
        fi
    fi
    if [ -z "$DB_PASSWORD" ]; then
        DB_PASSWORD=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24)))")
        info "Generated new database password"
    fi

    # Start Postgres container first (MCP depends on it, started after model download)
    step "Starting Postgres + pgvector container..."
    MEMORY_DB_PASSWORD="$DB_PASSWORD" \
    MEMORY_DB_PORT="$DB_PORT" \
    MEMORY_DB_NAME="$DB_NAME" \
    MEMORY_DB_USER="$DB_USER" \
    docker compose -f docker/docker-compose.yml up -d postgres 2>&1

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
fi

# -----------------------------------------------------------------------
# 4. Write .env
# -----------------------------------------------------------------------
step "Writing .env file..."
cat > .env << EOF
# NotNativeMemory - Generated by install script
MEMORY_DB_HOST=$DB_HOST
MEMORY_DB_PORT=$DB_PORT
MEMORY_DB_NAME=$DB_NAME
MEMORY_DB_USER=$DB_USER
MEMORY_DB_PASSWORD=$DB_PASSWORD
MEMORY_MODEL_PATH=models/gte-base-en-v1.5
MEMORY_DEFAULT_PROJECT=
EOF
info "Saved to .env"

if [ "$INSTALL_MODE" = "full" ]; then
    # -----------------------------------------------------------------------
    # 5. Build Docker image (full mode)
    # All Python deps live inside the container - no host pip install needed.
    # -----------------------------------------------------------------------
    step "Building MCP server Docker image..."
    docker compose -f docker/docker-compose.yml build mcp 2>&1
    if [ $? -ne 0 ]; then
        err "Docker image build failed."
        exit 1
    fi

    # -----------------------------------------------------------------------
    # 6. Download embedding model via container (full mode)
    # The container has sentence-transformers installed. Mount models/
    # read-write temporarily so the download persists on the host.
    # -----------------------------------------------------------------------
    if [ "${SKIP_MODEL:-}" != "1" ]; then
        step "Downloading embedding model (gte-base-en-v1.5, ~130MB)..."
        if [ -d "models/gte-base-en-v1.5" ]; then
            info "Model already exists, skipping download"
        else
            mkdir -p models
            MEMORY_DB_PASSWORD="$DB_PASSWORD" \
            MEMORY_DB_PORT="$DB_PORT" \
            MEMORY_DB_NAME="$DB_NAME" \
            MEMORY_DB_USER="$DB_USER" \
            docker compose -f docker/docker-compose.yml run --rm \
                -v "$(pwd)/models:/app/models" \
                mcp python -c "
from sentence_transformers import SentenceTransformer
import os
os.makedirs('models/gte-base-en-v1.5', exist_ok=True)
print('Downloading gte-base-en-v1.5...')
model = SentenceTransformer('Alibaba-NLP/gte-base-en-v1.5', trust_remote_code=True)
model.save('models/gte-base-en-v1.5')
print('Model saved to models/gte-base-en-v1.5')
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
    # 7. Start containers and wait for ready (full mode)
    # -----------------------------------------------------------------------
    step "Starting MCP server container..."
    MEMORY_DB_PASSWORD="$DB_PASSWORD" \
    MEMORY_DB_PORT="$DB_PORT" \
    MEMORY_DB_NAME="$DB_NAME" \
    MEMORY_DB_USER="$DB_USER" \
    docker compose -f docker/docker-compose.yml up -d mcp 2>&1

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
    # 5. Install Python dependencies (server-only mode - runs on host)
    # -----------------------------------------------------------------------
    step "Installing Python dependencies..."
    pip install -r requirements.txt --quiet 2>&1

    # -----------------------------------------------------------------------
    # 6. Test database connection and run schema (server-only mode)
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

    # -----------------------------------------------------------------------
    # 7. Download embedding model (server-only mode - on host)
    # -----------------------------------------------------------------------
    if [ "${SKIP_MODEL:-}" != "1" ]; then
        step "Downloading embedding model (gte-base-en-v1.5, ~130MB)..."
        if [ -d "models/gte-base-en-v1.5" ]; then
            info "Model already exists, skipping download"
        else
            python3 -c "
from sentence_transformers import SentenceTransformer
import os
os.makedirs('models', exist_ok=True)
print('Downloading gte-base-en-v1.5...')
model = SentenceTransformer('Alibaba-NLP/gte-base-en-v1.5', trust_remote_code=True)
model.save('models/gte-base-en-v1.5')
print('Model saved to models/gte-base-en-v1.5')
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
# 9. Self-test
# -----------------------------------------------------------------------
step "Running self-test..."
if [ "$INSTALL_MODE" = "full" ]; then
    # Run selftest inside the MCP container where deps and model are available
    docker compose -f docker/docker-compose.yml exec mcp python selftest.py
else
    python3 selftest.py
fi
if [ $? -ne 0 ]; then
    err "Self-test failed. Check the output above for details."
    exit 1
fi

# -----------------------------------------------------------------------
# 10. Configure Claude Code hooks (if Claude Code is installed)
# -----------------------------------------------------------------------
MCP_URL="http://localhost:$MCP_PORT/mcp"
if command -v claude &> /dev/null; then
    step "Configuring Claude Code hooks..."
    python3 claude/hooks/merge_hooks.py "$INSTALL_PATH" "$MCP_URL" || {
        warn "Hook configuration failed. You can run this manually later:"
        info "python3 claude/hooks/merge_hooks.py \"$INSTALL_PATH\" \"$MCP_URL\""
    }

    step "Registering MCP memory server with Claude Code..."
    if claude mcp add --transport http memory --scope user "$MCP_URL" 2>&1; then
        info "MCP server registered (tools: memory_store, memory_search, memory_forget, memory_list)"
    else
        warn "MCP registration failed. You can run this manually:"
        info "claude mcp add --transport http memory --scope user \"$MCP_URL\""
    fi
else
    info "Claude Code not detected - skipping hook and MCP registration"
    info "Install hooks on client machines using option 3 (client only)"
fi

# -----------------------------------------------------------------------
# 11. Write manifest
# -----------------------------------------------------------------------
if [ "$INSTALL_MODE" = "full" ]; then
    COMPONENTS='["hooks", "server", "docker", "embedding_model", "database"]'
else
    COMPONENTS='["hooks", "server", "python_deps", "embedding_model"]'
fi

python3 -c "
import json
manifest = {
    'installed': '$(date +%Y-%m-%d)',
    'install_mode': '$INSTALL_MODE',
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
if [ "$INSTALL_MODE" = "full" ]; then
cat > SETUP_COMPLETE.md << EOF
# NotNativeMemory - Setup Complete

Your MCP memory server is installed and tested.

## Starting the Server

The server runs as a Docker container alongside Postgres:

\`\`\`
docker compose -f docker/docker-compose.yml up -d      # start
docker compose -f docker/docker-compose.yml down        # stop
docker compose -f docker/docker-compose.yml logs mcp    # view logs
\`\`\`

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

- Install mode: $INSTALL_MODE
- Install path: $INSTALL_PATH
- MCP endpoint: http://${HOSTNAME_VAL}:${MCP_PORT}/mcp
- Database: postgres (Docker internal) / localhost:${DB_PORT} (host)
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

- Install mode: $INSTALL_MODE
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
step "Mode: $INSTALL_MODE"
step "MCP endpoint: http://${HOSTNAME_VAL}:${MCP_PORT}/mcp"
if [ "$INSTALL_MODE" = "full" ]; then
    step "Database: localhost:${DB_PORT}/${DB_NAME} (Docker)"
else
    step "Database: ${DB_HOST}:${DB_PORT}/${DB_NAME} (remote)"
fi
step "Hooks: configured in ~/.claude/settings.json"
echo ""
if [ "$INSTALL_MODE" = "full" ]; then
    info "Server is running in Docker (auto-restarts on boot)"
    info "Manage:  docker compose -f docker/docker-compose.yml [up -d|down|logs mcp]"
else
    info "Start the server:  python3 server.py"
fi
info "Full details:      SETUP_COMPLETE.md"
echo ""
