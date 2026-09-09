#!/usr/bin/env bash
# Preflight: make a clean checkout runnable, and size the deployment to the
# machine it is running on.
#
# Two jobs, both of which must happen before any container starts:
#
#   1. CREDENTIALS -- create .env from .env.example if missing and fill any
#      blank required key. Interactively when there is a terminal (asked once,
#      then written to .env and never asked again); otherwise fail loudly
#      naming exactly which keys are missing. Without this a fresh clone dies
#      on `env_file: .env` with a Compose error that says nothing useful.
#
#   2. CAPABILITY -- pick a resource profile from the machine's total RAM.
#      Gate on MemTotal, NOT MemAvailable: free memory swings minute to minute
#      (a browser, a build), total does not. A big machine with a temporarily
#      low ceiling should still get the full profile.
#
# Usable two ways:
#   bash scripts/preflight.sh          # standalone: prints the verdict
#   source scripts/preflight.sh        # sets DEPLOY_PROFILE for deploy.sh

# POSIX-only guard: must parse under sh/dash, since that is the case it catches.
# No `exec` here -- deploy.sh *sources* this file, and exec would replace the
# calling shell. A sourced run is already under bash, so this only ever fires
# for a direct `sh scripts/preflight.sh`.
if [ -z "${BASH_VERSION:-}" ]; then
    echo "ERROR: run this with bash, not sh -- it uses pipefail, arrays and read -s." >&2
    echo "       bash scripts/preflight.sh" >&2
    exit 1
fi

set -euo pipefail

PREFLIGHT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$PREFLIGHT_DIR/.." && pwd)"

ENV_FILE="$APP_DIR/.env"
ENV_EXAMPLE="$APP_DIR/.env.example"

# Keys with no sensible shared default -- only the user can supply these.
# Everything else in .env.example ships pre-filled (local-only demo values).
REQUIRED_KEYS=(
    AZURE_OPENAI_API_KEY
    AZURE_OPENAI_ENDPOINT
    OPENAI_API_VERSION
    AZURE_OPENAI_DEPLOYMENT_NAME
)

# Below this much TOTAL RAM the machine gets the lean profile.
MEM_THRESHOLD_GB="${MEM_THRESHOLD_GB:-8}"

# ---------------------------------------------------------------------------
# 1. Credentials
# ---------------------------------------------------------------------------

env_value() {
    # Read KEY from .env, stripping surrounding quotes. Empty if absent/blank.
    local key="$1"
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n1 | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

set_env_value() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        # Delimiter is | and the value is escaped, because these are API keys:
        # they routinely contain / and & , which sed would otherwise eat.
        local escaped
        escaped="$(printf '%s' "$value" | sed -e 's/[\|&]/\&/g')"
        sed -i "s|^${key}=.*|${key}=\"${escaped}\"|" "$ENV_FILE"
    else
        printf '%s="%s"\n' "$key" "$value" >> "$ENV_FILE"
    fi
}

preflight_credentials() {
    if [ ! -f "$ENV_FILE" ]; then
        if [ ! -f "$ENV_EXAMPLE" ]; then
            echo "[preflight] ERROR: neither .env nor .env.example exists in $APP_DIR" >&2
            return 1
        fi
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        echo "[preflight] created .env from .env.example"
    fi

    local missing=()
    for key in "${REQUIRED_KEYS[@]}"; do
        [ -z "$(env_value "$key")" ] && missing+=("$key")
    done

    if [ ${#missing[@]} -eq 0 ]; then
        echo "[preflight] credentials OK (${#REQUIRED_KEYS[@]} required keys present)"
        return 0
    fi

    # No terminal (CI, a pipe, a hook): do not hang waiting on stdin.
    if [ ! -t 0 ]; then
        echo "[preflight] ERROR: .env is missing values for:" >&2
        printf '  - %s\n' "${missing[@]}" >&2
        echo "[preflight] fill them in $ENV_FILE, or run this from a terminal to be prompted." >&2
        return 1
    fi

    echo ""
    echo "  Setup -- ${#missing[@]} value(s) needed. Asked once, then saved to .env."
    echo "  Everything else ships pre-filled; only your Azure OpenAI details are personal."
    echo ""
    for key in "${missing[@]}"; do
        local value=""
        while [ -z "$value" ]; do
            if [ "$key" = "AZURE_OPENAI_API_KEY" ]; then
                read -r -s -p "  $key (hidden): " value; echo ""
            else
                read -r -p "  $key: " value
            fi
            [ -z "$value" ] && echo "    (required -- cannot be empty)"
        done
        set_env_value "$key" "$value"
    done
    echo ""
    echo "[preflight] credentials saved to .env"
}

# ---------------------------------------------------------------------------
# 2. Machine capability
# ---------------------------------------------------------------------------

preflight_profile() {
    local mem_total_kb mem_total_gb mem_avail_gb cpus
    mem_total_kb="$(awk '/^MemTotal:/{print $2}' /proc/meminfo)"
    mem_total_gb=$(( mem_total_kb / 1024 / 1024 ))
    mem_avail_gb=$(( $(awk '/^MemAvailable:/{print $2}' /proc/meminfo) / 1024 / 1024 ))
    cpus="$(nproc)"

    if [ "$mem_total_gb" -ge "$MEM_THRESHOLD_GB" ]; then
        DEPLOY_PROFILE="full"
    else
        DEPLOY_PROFILE="lean"
    fi

    echo "[preflight] machine: ${mem_total_gb}GB total RAM (${mem_avail_gb}GB free), ${cpus} CPUs"
    echo "[preflight] profile: ${DEPLOY_PROFILE}  (threshold ${MEM_THRESHOLD_GB}GB total; free RAM is deliberately not used to decide)"

    if [ "$DEPLOY_PROFILE" = "lean" ]; then
        echo "[preflight]   -> memory ceilings applied; Grafana not started (optional, see section 7)"
        # A big Windows host with a small WSL slice is a config problem, not a
        # small machine -- say so instead of silently degrading it.
        if grep -qiE "microsoft|wsl" /proc/version 2>/dev/null; then
            echo "[preflight]   NOTE: WSL detected. If this host has more RAM than ${mem_total_gb}GB,"
            echo "[preflight]         raise the WSL allocation in C:\Users\<you>\.wslconfig:"
            echo "[preflight]           [wsl2]"
            echo "[preflight]           memory=8GB"
            echo "[preflight]         then run: wsl --shutdown   (this stops all containers)"
        fi
    fi
}

preflight_credentials
preflight_profile

export DEPLOY_PROFILE
