#!/usr/bin/env bash
# LOCAL MODE: services in Docker, application on the host.
#
# The other mode is scripts/deploy.sh, which containerises the app too. Both
# are supported on purpose, because they are good at different things:
#
#   local (this)    the app restarts in a second, breakpoints work, print()
#                   goes to your terminal, and the code you are editing is the
#                   code that runs. This is where you develop.
#   deploy.sh       the app is built into an image and runs on the compose
#                   network, exactly as a judge or a grader will run it. This
#                   is what you demo and what you submit.
#
# The ONE difference between them is hostnames: on the host, services are at
# localhost:<published port>; inside the network they are at <service>:<internal
# port>. `.env` holds the localhost form and deployment/docker-compose.yml overrides it --
# so neither file needs editing to switch modes, and there is no third
# configuration to drift.
#
#   bash scripts/local.sh              # start services, then run the API
#   bash scripts/local.sh --services   # services only, no API
#   bash scripts/local.sh --stop       # stop the services
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE="deployment/docker-compose-infra.yaml"
PROFILE_OVERLAY="deployment/compose/infra.lean.yaml"
DOMAIN="${DOMAIN:-sample_policy}"
PORT="${APP_PORT:-8020}"

# docker lives inside WSL on this machine, so every docker call goes through it
# when we are on the Windows side. Same delegation deploy.sh does.
docker_cmd() {
    if command -v docker >/dev/null 2>&1; then
        docker "$@"
    elif command -v wsl.exe >/dev/null 2>&1; then
        local here; here="$(wsl.exe wslpath -a "$(cygpath -m "$PWD")" 2>/dev/null | tr -d '\r')"
        wsl.exe -e bash -lc "cd '$here' && docker $*"
    else
        echo "ERROR: docker is not reachable and wsl.exe was not found." >&2
        exit 1
    fi
}

case "${1:-}" in
    --stop)
        echo "[local] stopping services..."
        docker_cmd compose -f "$COMPOSE" -f "$PROFILE_OVERLAY" down
        exit 0
        ;;
esac

echo "[local] starting services (postgres only - the lean profile)..."
docker_cmd compose -f "$COMPOSE" -f "$PROFILE_OVERLAY" up -d postgres

echo "[local] waiting for postgres..."
for _ in $(seq 1 30); do
    status="$(docker_cmd inspect -f '{{.State.Health.Status}}' h2-postgres 2>/dev/null | tr -d '\r')"
    [ "$status" = "healthy" ] && break
    sleep 2
done
[ "$status" = "healthy" ] || { echo "[local] postgres did not become healthy" >&2; exit 1; }

echo "[local] postgres healthy on localhost:5446"

if [ "${1:-}" = "--services" ]; then
    echo ""
    echo "Services are up. Run things against them directly:"
    echo "  DOMAIN=$DOMAIN uv run python -m agentcore.rag.index"
    echo "  DOMAIN=$DOMAIN uv run python -m evaluation.retrieval_eval"
    echo "  DOMAIN=$DOMAIN uv run python -m agentcore.inspect 'a question'"
    exit 0
fi

echo ""
echo "[local] starting the API on the HOST with reload"
echo "        http://localhost:$PORT/docs     domain=$DOMAIN"
echo ""
# MCP_MODE=stdio because there is no mcp-systems container in this mode: the
# server is spawned as a subprocess instead. Same code, different transport.
DOMAIN="$DOMAIN" MCP_MODE=stdio \
    uv run uvicorn agentcore.api.service:app --reload --port "$PORT"
