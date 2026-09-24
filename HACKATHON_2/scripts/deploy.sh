#!/usr/bin/env bash
# Dependency-ordered deploy: bring up the infra stack (Postgres/pgvector),
# then the app stack.
#
# This is the ONLY place cross-compose-file ordering can live - Compose's own
# depends_on/health-condition mechanism works within one invocation, never
# across two independently-managed files.

# ---------------------------------------------------------------------------
# POSIX-only prologue. Everything down to the `set` line must parse under
# dash/sh too, because this block exists precisely for the case where the
# script was NOT started by bash. `set -o pipefail` is a bash feature: run this
# with sh and the first executable line dies with "illegal option: pipefail",
# which says nothing about the real problem.
# ---------------------------------------------------------------------------
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash >/dev/null 2>&1; then
        exec bash "$0" "$@"
    fi
    echo "ERROR: this script needs bash (pipefail, associative arrays)." >&2
    echo "       On Windows run it from WSL, or use:  .\\scripts\\deploy.ps1" >&2
    exit 1
fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# Docker for this project lives INSIDE WSL - there is no Docker Desktop on
# this machine and no docker.exe on the Windows PATH. Rather than explaining
# that, hand the job to WSL so `bash scripts/deploy.sh` works from any shell.
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    case "${OSTYPE:-}" in
        msys*|cygwin*|win32*)
            if command -v wsl.exe >/dev/null 2>&1; then
                echo "[deploy] docker is not reachable from this Windows shell, re-running inside WSL."
                # cygpath -m gives forward slashes, which matter: wsl.exe eats
                # backslashes in its arguments as escapes. tr strips the CR
                # wsl.exe appends to its output.
                _win_dir="$(cygpath -m "$APP_DIR" 2>/dev/null || printf '%s' "$APP_DIR")"
                _wsl_dir="$(wsl.exe wslpath -a "$_win_dir" 2>/dev/null | tr -d '\r')"
                if [ -z "$_wsl_dir" ]; then
                    echo "ERROR: could not translate '$APP_DIR' to a WSL path." >&2
                    echo "       Is the WSL default distribution available?  wsl -l -v" >&2
                    exit 1
                fi
                _fwd=""
                [ -n "${MEM_THRESHOLD_GB:-}" ] && _fwd="MEM_THRESHOLD_GB=${MEM_THRESHOLD_GB} "
                [ -n "${STOP_HACKATHON1:-}" ] && _fwd="${_fwd}STOP_HACKATHON1=${STOP_HACKATHON1} "
                # exec, not a subshell: WSL inherits this terminal, so
                # preflight.sh's `[ -t 0 ]` test still sees a TTY and can prompt.
                exec wsl.exe -e bash -lc "cd '$_wsl_dir' && ${_fwd}bash scripts/deploy.sh"
            fi
            echo "ERROR: 'docker' is not on PATH and wsl.exe was not found." >&2
            exit 1
            ;;
        *)
            echo "ERROR: 'docker' is not on PATH in this shell." >&2
            exit 1
            ;;
    esac
fi

INFRA_COMPOSE="$APP_DIR/deployment/docker-compose-infra.yaml"
APP_COMPOSE="$APP_DIR/deployment/docker-compose.yml"

# ---------------------------------------------------------------------------
# Preflight: complete .env (prompting once) and pick a profile from total RAM.
# Sourced BEFORE the logging redirection below, because an interactive prompt
# sent through `tee` can sit in a buffer while `read` already blocks on stdin.
# Sets DEPLOY_PROFILE (lean|full).
# ---------------------------------------------------------------------------
# shellcheck source=scripts/preflight.sh
source "$SCRIPT_DIR/preflight.sh"

INFRA_LIMITS="$APP_DIR/deployment/compose/infra.${DEPLOY_PROFILE}.yaml"
APP_LIMITS="$APP_DIR/deployment/compose/app.${DEPLOY_PROFILE}.yaml"

# Which containers must be healthy before the app starts. One, since Neo4j was
# removed (DECISIONS D48); the profiles now differ only in memory limits.
INFRA_CONTAINERS=(h2-postgres)
INFRA_SERVICES_LEAN=(postgres)

TIMEOUT_SECONDS=180

# ---------------------------------------------------------------------------
# This machine cannot run both hackathon stacks at once: WSL has ~3.6GB and the
# hackathon1 stack alone uses ~2.4GB of it. Refuse early with a specific
# instruction rather than letting containers die of OOM halfway through.
# ---------------------------------------------------------------------------
H1_CONTAINERS=(langfuse-web langfuse-worker langfuse-clickhouse langfuse-postgres langfuse-redis langfuse-minio grafana-app hackathon1-app)

check_conflicting_stack() {
    local running=()
    local name
    for name in "${H1_CONTAINERS[@]}"; do
        if [ "$(docker inspect --format='{{.State.Running}}' "$name" 2>/dev/null)" = "true" ]; then
            running+=("$name")
        fi
    done
    [ ${#running[@]} -eq 0 ] && return 0

    echo "[deploy] the hackathon1 stack is running (${#running[@]} containers, ~2.4GB)."
    if [ "${STOP_HACKATHON1:-}" = "1" ]; then
        echo "[deploy] STOP_HACKATHON1=1 - stopping them."
        docker stop "${running[@]}" >/dev/null
        echo "[deploy] stopped. Restart later with: docker start ${running[*]}"
        return 0
    fi
    echo "[deploy] ERROR: not enough RAM for both stacks on this machine." >&2
    echo "[deploy] stop them first:" >&2
    echo "           docker stop ${running[*]}" >&2
    echo "[deploy] or re-run as:  STOP_HACKATHON1=1 bash scripts/deploy.sh" >&2
    return 1
}

# ---------------------------------------------------------------------------
# Run logging: every invocation gets its own numbered, timestamped log file.
# Nothing is ever overwritten.
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
LOG_FILE="$LOG_DIR/run-${run_number_padded}-$(date +%Y-%m-%dT%H-%M-%S).log"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "== hackathon2 deploy - run #${run_number_padded} ($(date -u +%FT%TZ)) =="
echo "   logging to: $LOG_FILE"
echo "   profile   : $DEPLOY_PROFILE (limits: $(basename "$INFRA_LIMITS"))"

check_conflicting_stack || exit 1

# Wait on ACTUAL container state and stop the moment the answer is known --
# either all healthy, or a definitively broken service. Three signals are
# terminal because none recover by waiting:
#   OOMKilled       the ceiling in deployment/compose/infra.$DEPLOY_PROFILE.yaml is too low
#   exited / dead   fatal even with restart:unless-stopped
#   restart loop    RestartCount climbing past its starting value
# A couple of restarts during boot can be normal, so the threshold is a rise of
# 3 from the baseline, not any restart at all. This is what catches a JVM or
# Node process that died of heap exhaustion with OOMKilled=false and exit 0.
wait_for_infra() {
    local deadline=$((SECONDS + TIMEOUT_SECONDS))
    local -A baseline_restarts=()
    local name
    for name in "${INFRA_CONTAINERS[@]}"; do
        baseline_restarts["$name"]="$(docker inspect -f '{{.RestartCount}}' "$name" 2>/dev/null || echo 0)"
    done

    while true; do
        local all_healthy=true fatal=""
        for name in "${INFRA_CONTAINERS[@]}"; do
            local state health restarts oom
            state="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)"
            health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null || echo missing)"
            restarts="$(docker inspect -f '{{.RestartCount}}' "$name" 2>/dev/null || echo 0)"
            oom="$(docker inspect -f '{{.State.OOMKilled}}' "$name" 2>/dev/null || echo false)"

            if [ "$oom" = "true" ]; then
                fatal="$name was OOM-killed - raise its mem_limit in deployment/compose/infra.${DEPLOY_PROFILE}.yaml"
            elif [ "$state" = "exited" ] || [ "$state" = "dead" ]; then
                fatal="$name $state (exit $(docker inspect -f '{{.State.ExitCode}}' "$name" 2>/dev/null))"
            elif [ "$restarts" -ge $(( ${baseline_restarts[$name]:-0} + 3 )) ]; then
                fatal="$name is restart-looping ($restarts restarts) - see: docker logs $name"
            fi
            [ "$health" = "healthy" ] || all_healthy=false
        done

        if [ -n "$fatal" ]; then
            echo "[deploy] ERROR: $fatal" >&2
            echo "[deploy] stopping early - this does not recover by waiting." >&2
            return 1
        fi
        [ "$all_healthy" = true ] && return 0
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "[deploy] ERROR: infra did not become healthy within ${TIMEOUT_SECONDS}s." >&2
            for name in "${INFRA_CONTAINERS[@]}"; do
                printf '  %-16s %s\n' "$name" \
                    "$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$name" 2>/dev/null || echo missing)" >&2
            done
            return 1
        fi
        sleep 2
    done
}

# Containers this project USED to define. Compose only stops what it currently
# declares, so removing a service from the file orphans any container already
# created from it: it keeps running, keeps its RAM, and `docker compose down`
# never touches it again. On the very machine the removal was meant to help,
# the saving would simply never arrive.
#
# Named explicitly rather than pruned by label, because a broad prune on a box
# that also hosts hackathon1 is exactly the kind of cleanup that takes
# something it should not.
RETIRED_CONTAINERS=(h2-neo4j)

stop_retired_containers() {
    local name
    for name in "${RETIRED_CONTAINERS[@]}"; do
        if [ "$(docker inspect --format='{{.State.Running}}' "$name" 2>/dev/null)" = "true" ]; then
            echo "[deploy] stopping retired container $name (frees ~768Mi; DECISIONS D48)"
            docker stop "$name" >/dev/null
        fi
    done
}

# Converge every run. `up -d` is idempotent - Compose recreates only containers
# whose config changed - and running it unconditionally is what makes a profile
# switch take effect, because memory limits are set at container CREATION.
echo "[deploy] converging infra stack (profile $DEPLOY_PROFILE)..."
stop_retired_containers
if [ "$DEPLOY_PROFILE" = "lean" ]; then
    docker compose -f "$INFRA_COMPOSE" -f "$INFRA_LIMITS" up -d "${INFRA_SERVICES_LEAN[@]}"
else
    docker compose -f "$INFRA_COMPOSE" -f "$INFRA_LIMITS" up -d
fi

# Wait unconditionally, even if it was healthy a moment ago: the converge step
# recreates any container whose config changed, so "healthy before" says
# nothing about "healthy now". When it really is healthy this returns on the
# first poll and costs nothing.
echo "[deploy] waiting for infra health (up to ${TIMEOUT_SECONDS}s)..."
wait_for_infra || {
    echo "[deploy] check with: docker compose -f \"$INFRA_COMPOSE\" ps" >&2
    exit 1
}
echo "[deploy] infra healthy."

echo "[deploy] bringing up the app stack..."
# --project-directory is required because the -f files are absolute paths, and
# it must be deployment/, NOT the repo root. Compose resolves every relative
# path in a compose file against the PROJECT DIRECTORY, not against the file
# that contains it, so pointing it at the repo root made both relative paths
# climb one level too far:
#
#   env_file: ../.env   ->  /mnt/c/projects/.env        (above the repo)
#   context:  ..        ->  /mnt/c/projects             (above the repo)
#
# The first failed loudly. The second would have built the image from the
# wrong tree. deployment/ is also what a bare `docker compose -f
# deployment/docker-compose.yml` infers on its own, so both invocations now
# resolve identically.
docker compose -f "$APP_COMPOSE" -f "$APP_LIMITS"     --project-directory "$APP_DIR/deployment" up -d --build

echo ""
echo "== Deployed (run #${run_number_padded}) =="
echo "  API      : http://localhost:8020        (GET /healthz)"
echo "  API docs : http://localhost:8020/docs"
echo "  Postgres : localhost:5446  db hackathon2  (h2 / h2passQWqw12)"
if [ -n "${LANGFUSE_PUBLIC_KEY:-}" ]; then
    echo "  Traces   : ${LANGFUSE_HOST:-https://cloud.langfuse.com}"
else
    echo "  Traces   : local only - uv run python -m agentcore.tracing \"question\""
fi
echo "  Log      : $LOG_FILE"
echo ""
echo "Smoke test: bash scripts/smoke.sh"
