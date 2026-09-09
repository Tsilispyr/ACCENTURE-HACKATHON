#!/usr/bin/env bash
# Dependency-ordered deploy: bring up the Langfuse/Postgres/ClickHouse/Redis/
# MinIO/Grafana infra stack first (if not already healthy), then the
# hackathon1 app. This is the ONLY place cross-compose-file ordering can
# live -- Compose's own depends_on/health-condition mechanism only works
# within one compose invocation, not across two independently-managed files
# (./docker-compose-langfuse.yaml and ./docker-compose.yml).

# ---------------------------------------------------------------------------
# POSIX-only prologue. Everything down to the `set` line must parse under
# dash/sh as well as bash, because this block exists precisely for the case
# where the script was NOT started by bash.
#
# `set -o pipefail` is a bash feature. Run this file with sh/dash -- which is
# what happens with `sh scripts/deploy.sh`, and on some Windows shells -- and
# the very first executable line dies with an "illegal option" error naming
# pipefail. That error says nothing about the real problem, so re-exec under a
# real bash instead of letting it happen.
# ---------------------------------------------------------------------------
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash >/dev/null 2>&1; then
        exec bash "$0" "$@"
    fi
    echo "ERROR: this script needs bash (it uses pipefail and associative arrays)." >&2
    echo "       No bash found on PATH." >&2
    echo "       On Windows, run it from WSL, or use PowerShell:  .\scripts\deploy.ps1" >&2
    exit 1
fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# Docker for this project lives inside WSL. It is NOT on the PATH of Git Bash /
# MSYS, so running here would otherwise die halfway through a deploy with a
# bare "docker: command not found".
#
# Rather than just explaining that, hand the job to WSL and get on with it --
# so `bash scripts/deploy.sh` does the right thing from any shell, and the only
# thing that differs between environments is who ends up executing it:
#
#   WSL / Linux    run directly (docker is on PATH, nothing to do)
#   Git Bash/MSYS  re-exec inside WSL, below
#   PowerShell/cmd scripts/deploy.ps1, which does the same translation
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    case "${OSTYPE:-}" in
        msys*|cygwin*|win32*)
            if command -v wsl.exe >/dev/null 2>&1; then
                echo "[deploy] docker is not reachable from this Windows shell, re-running inside WSL."
                # cygpath -m gives C:/path/with/forward/slashes. Forward slashes
                # matter: wsl.exe consumes backslashes in its arguments as
                # escapes, so a native path arrives mangled and wslpath rejects
                # it. tr strips the CR that wsl.exe appends to its output.
                _win_dir="$(cygpath -m "$APP_DIR" 2>/dev/null || printf '%s' "$APP_DIR")"
                _wsl_dir="$(wsl.exe wslpath -a "$_win_dir" 2>/dev/null | tr -d '\r')"
                if [ -z "$_wsl_dir" ]; then
                    echo "ERROR: could not translate '$APP_DIR' to a WSL path." >&2
                    echo "       Is the WSL default distribution available?  wsl -l -v" >&2
                    exit 1
                fi
                # Forward the profile override if the caller set one.
                _fwd=""
                if [ -n "${MEM_THRESHOLD_GB:-}" ]; then
                    _fwd="MEM_THRESHOLD_GB=${MEM_THRESHOLD_GB} "
                fi
                # exec, not a subshell: WSL inherits this terminal, so
                # preflight.sh's `[ -t 0 ]` test still sees a TTY and can prompt
                # for credentials on a fresh checkout.
                exec wsl.exe -e bash -lc "cd '$_wsl_dir' && ${_fwd}bash scripts/deploy.sh"
            fi
            echo "ERROR: 'docker' is not on PATH and wsl.exe was not found." >&2
            echo "       Docker for this project runs inside WSL. Install it with:  wsl --install" >&2
            exit 1
            ;;
        *)
            echo "ERROR: 'docker' is not on PATH in this shell." >&2
            echo "       Start Docker, or install the Docker CLI, then retry." >&2
            exit 1
            ;;
    esac
fi

INFRA_COMPOSE="$APP_DIR/docker-compose-langfuse.yaml"
APP_COMPOSE="$APP_DIR/docker-compose.yml"

# Grafana is intentionally not in this list -- the app doesn't talk to it,
# and docker-compose-langfuse.yaml itself only marks it "OPTIONAL".
INFRA_CONTAINERS=(langfuse-postgres langfuse-clickhouse langfuse-redis langfuse-minio langfuse-worker langfuse-web)

# ---------------------------------------------------------------------------
# Preflight: ensure .env is complete (prompting once if a key is blank) and
# pick a resource profile from this machine's total RAM. Sourced BEFORE the
# logging redirection below, because an interactive prompt sent through `tee`
# can sit in a buffer while `read` is already blocking on stdin.
# Sets DEPLOY_PROFILE (lean|full).
# ---------------------------------------------------------------------------
# shellcheck source=scripts/preflight.sh
source "$SCRIPT_DIR/preflight.sh"

INFRA_LIMITS="$APP_DIR/compose/infra.${DEPLOY_PROFILE}.yaml"
APP_LIMITS="$APP_DIR/compose/app.${DEPLOY_PROFILE}.yaml"

# On lean the services are named explicitly so Grafana never starts -- it is
# marked OPTIONAL in the infra file and is bonus-only per section 7. Naming
# them here keeps the base compose file untouched, which a `profiles:` tag
# would not.
INFRA_SERVICES_LEAN=(postgres clickhouse redis minio langfuse-worker langfuse-web)

# How long to wait for infra health on a COLD start. Lean machines need much
# longer: Langfuse v4 takes ~2 minutes to boot here (see the start_period notes
# in compose/infra.lean.yaml), and the old flat 120s gave up on a stack that
# was starting normally.
if [ "$DEPLOY_PROFILE" = "lean" ]; then
    TIMEOUT_SECONDS=420
else
    TIMEOUT_SECONDS=180
fi

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
echo "   profile   : $DEPLOY_PROFILE (limits: $(basename "$INFRA_LIMITS"))"

infra_healthy() {
    for name in "${INFRA_CONTAINERS[@]}"; do
        status="$(docker inspect --format='{{.State.Health.Status}}' "$name" 2>/dev/null || echo "missing")"
        if [ "$status" != "healthy" ]; then
            return 1
        fi
    done
    return 0
}

# Wait on ACTUAL container state, and stop the moment the answer is known --
# either all healthy, or a definitively broken service. The previous version
# polled only "is everything healthy yet" and so sat out the entire timeout
# even when a container had already died, turning a 5-second diagnosis into a
# multi-minute one. The timeout is now an upper bound, not the normal cost of
# a failure.
#
# Three signals are treated as terminal, because none of them recover by
# waiting longer:
#   - OOMKilled        the ceiling in compose/infra.$DEPLOY_PROFILE.yaml is too low
#   - exited / dead    with restart:always this is rare, but it is fatal
#   - restart looping   RestartCount climbing well past its starting value
#
# A couple of restarts during boot are NORMAL (langfuse-web legitimately exits
# 0 and comes back partway through its init), so the loop threshold is a rise
# of 3 from the baseline captured before waiting, not any restart at all.
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
                fatal="$name was OOM-killed -- raise its mem_limit in compose/infra.${DEPLOY_PROFILE}.yaml"
            elif [ "$state" = "exited" ] || [ "$state" = "dead" ]; then
                fatal="$name $state (exit $(docker inspect -f '{{.State.ExitCode}}' "$name" 2>/dev/null))"
            elif [ "$restarts" -ge $(( ${baseline_restarts[$name]:-0} + 3 )) ]; then
                fatal="$name is restart-looping ($restarts restarts) -- see: docker logs $name"
            fi
            [ "$health" = "healthy" ] || all_healthy=false
        done

        if [ -n "$fatal" ]; then
            echo "[deploy] ERROR: $fatal" >&2
            echo "[deploy] stopping early -- this does not recover by waiting." >&2
            return 1
        fi
        if [ "$all_healthy" = true ]; then
            return 0
        fi
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "[deploy] ERROR: infra did not become healthy within ${TIMEOUT_SECONDS}s." >&2
            echo "[deploy] current state:" >&2
            for name in "${INFRA_CONTAINERS[@]}"; do
                printf '  %-22s %s
' "$name"                     "$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$name" 2>/dev/null || echo missing)" >&2
            done
            return 1
        fi
        sleep 2
    done
}

# Converge the infra stack every run, then wait for health only if it was not
# already healthy. `up -d` is idempotent -- Compose recreates only containers
# whose config actually changed -- and running it unconditionally is what makes
# a profile switch take effect. Memory limits are set at container creation, so
# skipping `up` entirely (as an earlier version of this script did when the
# stack was already healthy) would print the profile and apply nothing.
if infra_healthy; then
    echo "[deploy] infra stack already up and healthy"
fi

echo "[deploy] converging infra stack ($INFRA_COMPOSE, profile $DEPLOY_PROFILE)..."
if [ "$DEPLOY_PROFILE" = "lean" ]; then
    docker compose -f "$INFRA_COMPOSE" -f "$INFRA_LIMITS" up -d "${INFRA_SERVICES_LEAN[@]}"
    # `up -d <services>` starts what is named but never stops what is not, so a
    # Grafana left running by an earlier full-profile run would keep its ~256Mi
    # on a machine that just asked for lean. Free it, loudly.
    if [ "$(docker inspect --format='{{.State.Running}}' grafana-app 2>/dev/null)" = "true" ]; then
        echo "[deploy] lean profile: stopping Grafana to free ~256Mi (restart with: docker start grafana-app)"
        docker stop grafana-app >/dev/null
    fi
else
    docker compose -f "$INFRA_COMPOSE" -f "$INFRA_LIMITS" up -d
fi

# Wait unconditionally, even when the stack was healthy a moment ago. The
# converge step above recreates any container whose config changed, so "healthy
# before" says nothing about "healthy now" -- and starting the app against a
# still-booting langfuse-web loses its first spans to
# "Failed to export span batch ... Read timed out", which reads like a tracing
# bug rather than the startup race it is. When everything really is healthy this
# returns on the first poll and costs nothing.
echo "[deploy] waiting for infra health (up to ${TIMEOUT_SECONDS}s, profile $DEPLOY_PROFILE)..."
wait_for_infra || {
    echo "[deploy] check with: docker compose -f \"$INFRA_COMPOSE\" ps" >&2
    exit 1
}
echo "[deploy] infra stack healthy."

echo "[deploy] bringing up hackathon1 app ($APP_COMPOSE)..."
docker compose -f "$APP_COMPOSE" -f "$APP_LIMITS" --project-directory "$APP_DIR" up -d --build

echo ""
echo "== Deployed (run #${run_number_padded}) =="
echo "  WEB UI   : http://localhost:8010/ui          <-- open this"
echo "  API docs : http://localhost:8010/docs"
echo "  Langfuse : http://localhost:3000 (user@example.com / 12345678)"
if [ "$DEPLOY_PROFILE" = "lean" ]; then
    echo "  Grafana  : not started (lean profile, optional bonus service)"
else
    echo "  Grafana  : http://localhost:3001 (gtgh / grafanapassQWqw!@12)"
fi
echo "  MinIO    : http://localhost:9091 (minio / miniopassQWqw!@12)"
echo "  API      : http://localhost:8010  (GET / is the healthcheck, returns JSON)"
echo "  App DB   : localhost:5433 db: hackathon1 (gtgh / postgrepassQWqw!@12)"
echo "  Log      : $LOG_FILE"
echo ""
echo "Smoke test: uv run python -m hackathon1.apiclient   (from $APP_DIR)"
