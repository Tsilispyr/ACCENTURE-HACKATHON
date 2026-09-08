#!/usr/bin/env bash
# Dependency-ordered deploy: bring up the Langfuse/Postgres/ClickHouse/Redis/
# MinIO/Grafana infra stack first (if not already healthy), then the
# hackathon1 app. This is the ONLY place cross-compose-file ordering can
# live -- Compose's own depends_on/health-condition mechanism only works
# within one compose invocation, not across two independently-managed files
# (./docker-compose-langfuse.yaml and ./docker-compose.yml).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

INFRA_COMPOSE="$APP_DIR/docker-compose-langfuse.yaml"
APP_COMPOSE="$APP_DIR/docker-compose.yml"

# Grafana is intentionally not in this list -- the app doesn't talk to it,
# and docker-compose-langfuse.yaml itself only marks it "OPTIONAL".
INFRA_CONTAINERS=(langfuse-postgres langfuse-clickhouse langfuse-redis langfuse-minio langfuse-worker langfuse-web)
TIMEOUT_SECONDS=120

# ---------------------------------------------------------------------------
# Run logging: every invocation gets its own numbered, timestamped log file
# under logs/ -- nothing is ever overwritten or deleted (same convention as
# ../../scripts/stack-logs.sh's numbered/timestamped snapshots). A counter
# file tracks the next run number so it keeps incrementing even if earlier
# logs are archived/moved elsewhere later.
# ---------------------------------------------------------------------------
LOG_DIR="$APP_DIR/logs"
mkdir -p "$LOG_DIR"
COUNTER_FILE="$LOG_DIR/.run_counter"

if [ -f "$COUNTER_FILE" ]; then
    run_number=$(( $(cat "$COUNTER_FILE") + 1 ))
else
    run_number=1
fi
echo "$run_number" > "$COUNTER_FILE"
run_number_padded="$(printf "%04d" "$run_number")"
timestamp="$(date +%Y-%m-%dT%H-%M-%S)"
LOG_FILE="$LOG_DIR/run-${run_number_padded}-${timestamp}.log"

# Mirror everything from here on to both the terminal and the log file.
exec > >(tee -a "$LOG_FILE") 2>&1

echo "== Hackathon 1 deploy -- run #${run_number_padded} ($(date -u +%FT%TZ)) =="
echo "   logging to: $LOG_FILE"

infra_healthy() {
    for name in "${INFRA_CONTAINERS[@]}"; do
        status="$(docker inspect --format='{{.State.Health.Status}}' "$name" 2>/dev/null || echo "missing")"
        if [ "$status" != "healthy" ]; then
            return 1
        fi
    done
    return 0
}

if infra_healthy; then
    echo "[deploy] infra stack already up and healthy -- skipping"
else
    echo "[deploy] bringing up infra stack ($INFRA_COMPOSE)..."
    docker compose -f "$INFRA_COMPOSE" up -d

    echo "[deploy] waiting up to ${TIMEOUT_SECONDS}s for infra to become healthy..."
    elapsed=0
    until infra_healthy; do
        if [ "$elapsed" -ge "$TIMEOUT_SECONDS" ]; then
            echo "[deploy] ERROR: infra stack did not become healthy within ${TIMEOUT_SECONDS}s." >&2
            echo "[deploy] check with: docker compose -f \"$INFRA_COMPOSE\" ps" >&2
            exit 1
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    echo "[deploy] infra stack healthy."
fi

echo "[deploy] bringing up hackathon1 app ($APP_COMPOSE)..."
docker compose -f "$APP_COMPOSE" --project-directory "$APP_DIR" up -d --build

echo ""
echo "== Deployed (run #${run_number_padded}) =="
echo "  Langfuse : http://localhost:3000 (user@example.com / 12345678)"
echo "  Grafana  : http://localhost:3001 (gtgh / grafanapassQWqw!@12)"
echo "  MinIO    : http://localhost:9091 (minio / miniopassQWqw!@12)"
echo "  App      : http://localhost:8010"
echo "  App DB   : localhost:5432 db: hackathon1 (gtgh / postgrepassQWqw!@12)"
echo "  Log      : $LOG_FILE"
echo ""
echo "Smoke test: uv run python -m hackathon1.apiclient   (from $APP_DIR)"
