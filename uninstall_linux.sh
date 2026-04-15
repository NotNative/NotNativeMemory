#!/usr/bin/env bash
# NotNativeMemory - Linux/macOS Uninstaller
# Run: bash uninstall_linux.sh [--full]
#
# Reads .install-manifest.json to determine what was installed and only
# removes those components. Safe to run multiple times.
#
# --full    Also remove Docker volume (destroys all stored memories)

set -e

NC='\033[0m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'

step()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[x]${NC} $1"; }
info()  { echo "    $1"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
MANIFEST_FILE=".install-manifest.json"
FULL_MODE=false

# Parse args
for arg in "$@"; do
    case "$arg" in
        --full) FULL_MODE=true ;;
    esac
done

echo ""
echo "+==========================================+"
echo "|  NotNativeMemory - Uninstaller           |"
echo "+==========================================+"
echo ""

# -----------------------------------------------------------------------
# 1. Load manifest
# -----------------------------------------------------------------------
if [ ! -f "$MANIFEST_FILE" ]; then
    err "No install manifest found ($MANIFEST_FILE)."
    info "Either this was never installed, or the manifest was deleted."
    info "You can manually clean up:"
    info "  - Remove hooks from ~/.claude/settings.json"
    info "  - Stop MCP server: python3 server.py --stop"
    info "  - Stop Docker: docker compose -f docker/docker-compose.yml down"
    exit 1
fi

# Parse manifest with python
INSTALL_MODE=$(python3 -c "import json; d=json.load(open('$MANIFEST_FILE')); print(d.get('install_mode',''))" 2>/dev/null || true)
COMPONENTS=$(python3 -c "import json; d=json.load(open('$MANIFEST_FILE')); print(' '.join(d.get('components',[])))" 2>/dev/null || true)

if [ -z "$INSTALL_MODE" ]; then
    err "Manifest is corrupt. Cannot determine what to uninstall."
    exit 1
fi

info "Install mode: $INSTALL_MODE"
info "Components: $COMPONENTS"
echo ""

# Confirm
read -rp "  Proceed with uninstall? [y/N]: " CONFIRM
case "$CONFIRM" in
    y|Y|yes|Yes) ;;
    *)
        info "Cancelled."
        exit 0
        ;;
esac
echo ""

# Helper to check if a component was installed
has_component() {
    echo "$COMPONENTS" | grep -qw "$1"
}

# -----------------------------------------------------------------------
# 2. Stop MCP server (if running)
# -----------------------------------------------------------------------
if has_component "server"; then
    if has_component "docker"; then
        info "MCP server runs in Docker (will be stopped with containers)"
    else
        step "Stopping MCP server..."
        if [ -f ".mcp-server.pid" ]; then
            python3 server.py --stop 2>&1 || warn "Server may not have been running"
        else
            info "No running server found"
        fi
    fi
fi

# -----------------------------------------------------------------------
# 3. Remove Claude Code hooks
# -----------------------------------------------------------------------
if has_component "hooks"; then
    step "Removing Claude Code hooks..."
    SETTINGS_FILE="$HOME/.claude/settings.json"

    if [ -f "$SETTINGS_FILE" ]; then
        # Use python to surgically remove our hooks from settings.json
        python3 -c "
import json, sys

settings_file = '$SETTINGS_FILE'
try:
    with open(settings_file, 'r') as f:
        settings = json.load(f)
except (json.JSONDecodeError, FileNotFoundError):
    print('  Could not parse settings.json. Manual cleanup may be needed.', file=sys.stderr)
    sys.exit(0)

changed = False
hooks = settings.get('hooks', {})

# Remove our PreToolUse entries
if 'PreToolUse' in hooks:
    before = len(hooks['PreToolUse'])
    hooks['PreToolUse'] = [
        g for g in hooks['PreToolUse']
        if not any('memory_inject.py' in h.get('command','') for h in g.get('hooks',[]))
    ]
    if len(hooks['PreToolUse']) < before:
        changed = True
        print('  Removed PreToolUse hook')
    if not hooks['PreToolUse']:
        del hooks['PreToolUse']

# Remove our PreCompact entries
if 'PreCompact' in hooks:
    before = len(hooks['PreCompact'])
    hooks['PreCompact'] = [
        g for g in hooks['PreCompact']
        if not any('compact_guard.py' in h.get('command','') for h in g.get('hooks',[]))
    ]
    if len(hooks['PreCompact']) < before:
        changed = True
        print('  Removed PreCompact hook')
    if not hooks['PreCompact']:
        del hooks['PreCompact']

# Clean up empty hooks object
if not hooks:
    settings.pop('hooks', None)

if changed:
    with open(settings_file, 'w') as f:
        json.dump(settings, f, indent=2)
    print(f'  Saved {settings_file}')
else:
    print('  No hooks to remove (already clean)')
" || warn "Could not clean up hooks. Manual cleanup may be needed."
    else
        info "No settings.json found"
    fi
fi

# -----------------------------------------------------------------------
# 4. Stop and remove Docker containers
# -----------------------------------------------------------------------
if has_component "docker"; then
    step "Stopping Docker containers..."
    # `--profile '*'` matches both the full and server profiles, so a single
    # `down` covers either install shape.
    docker compose -f docker/docker-compose.yml --profile '*' down 2>&1 || warn "Could not stop containers (Docker may not be running)"

    # Remove the built MCP image (harmless if missing / in use)
    docker rmi notnative-memory-mcp 2>&1 >/dev/null || true
fi

# Database volume handling is separate: only the full install owns the
# data directory. server+docker just points at a remote DB.
if has_component "database"; then
    if [ "$FULL_MODE" = true ]; then
        warn "Full mode: removing database data (ALL MEMORIES WILL BE DELETED)"
        read -rp "  Are you sure? This cannot be undone. [y/N]: " PURGE_CONFIRM
        case "$PURGE_CONFIRM" in
            y|Y|yes|Yes)
                if [ -d "docker/postgres" ]; then
                    rm -rf docker/postgres
                    info "Database data removed"
                else
                    info "No database data directory found"
                fi
                ;;
            *)
                info "Database data preserved"
                ;;
        esac
    else
        info "Database data preserved in docker/postgres/ (memories are safe)"
        info "Use --full flag to also delete the database data"
    fi
fi

# -----------------------------------------------------------------------
# 5. Remove manifest and setup guide
# -----------------------------------------------------------------------
step "Cleaning up..."
if [ -f "$MANIFEST_FILE" ]; then
    rm "$MANIFEST_FILE"
    info "Removed $MANIFEST_FILE"
fi
if [ -f "SETUP_COMPLETE.md" ]; then
    rm "SETUP_COMPLETE.md"
    info "Removed SETUP_COMPLETE.md"
fi

# -----------------------------------------------------------------------
# 6. Summary
# -----------------------------------------------------------------------
echo ""
echo "+==========================================+"
echo "|  Uninstall Complete                      |"
echo "+==========================================+"
echo ""
info "Removed: $COMPONENTS"
echo ""
info "NOT removed (manual cleanup if desired):"
info "  - This directory ($SCRIPT_DIR)"
info "  - .env file (contains database credentials)"
info "  - Python packages installed via pip"
if [ "$FULL_MODE" = false ] && has_component "database"; then
    info "  - Database data in docker/postgres/ (run with --full to delete)"
fi
echo ""
