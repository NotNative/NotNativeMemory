#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

HOST_NAME="$(hostname | tr '[:upper:]' '[:lower:]')"
PORT="9500"
ROTATE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --host)
            HOST_NAME="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --rotate)
            ROTATE="--rotate"
            shift
            ;;
        -h|--help)
            echo "Usage: scripts/claim-code.sh [--host HOST] [--port PORT] [--rotate]"
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

ARGS=(scripts/claim_code.py --host "$HOST_NAME" --port "$PORT")
if [ -n "$ROTATE" ]; then
    ARGS+=("$ROTATE")
fi

if docker container inspect MCP-server >/dev/null 2>&1; then
    docker compose --progress=plain -f docker/docker-compose.yml exec -T mcp python "${ARGS[@]}"
else
    echo "MCP-server container was not found; falling back to host Python."
    python3 "${ARGS[@]}"
fi
