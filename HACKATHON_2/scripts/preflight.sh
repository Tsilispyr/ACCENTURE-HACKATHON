#!/usr/bin/env bash
# Preflight: make a clean checkout runnable, and size the deployment to the
# machine it runs on.
#
# Three jobs, all before any container starts:
#
#   1. CREDENTIALS - create .env from .env.example and fill any blank required
#      key. Interactively when there is a terminal (asked once, then saved);
#      otherwise fail loudly naming exactly which keys are missing. Without
#      this a fresh clone dies on `env_file: .env` with a Compose error that
#      says nothing useful.
#
#   2. SECRETS - generate APP_SECRET if blank. It signs session tokens; there
#      is no reason to make a human invent one.
#
#   3. CAPABILITY - pick a resource profile from total RAM. Gate on MemTotal,
#      NOT MemAvailable: free memory swings minute to minute, total does not.
#
# Usable two ways:
#   bash scripts/preflight.sh      # standalone: prints the verdict
#   source scripts/preflight.sh    # sets DEPLOY_PROFILE for deploy.sh

# POSIX-only guard: must parse under sh/dash, since that is the case it catches.
# No `exec` here - deploy.sh SOURCES this file, and exec would replace the
# calling shell.
if [ -z "${BASH_VERSION:-}" ]; then
    echo "ERROR: run this with bash, not sh - it uses pipefail, arrays and read -s." >&2
    echo "       bash scripts/preflight.sh" >&2
    exit 1
fi

set -euo pipefail

PREFLIGHT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$PREFLIGHT_DIR/.." && pwd)"

ENV_FILE="$APP_DIR/.env"
ENV_EXAMPLE="$APP_DIR/.env.example"

# Keys with no sensible shared default AND no working system without them.
# Everything else in .env.example ships pre-filled with local-demo-only values.
#
# NO TRACING KEY IS REQUIRED, deliberately. The pipeline runs identically with
# tracing off, because the always-on layer is the LangGraph audit trail and
# that needs no credentials at all. Demanding a key for an optional hosted
# viewer blocks anyone who does not have one.
#
# This has already gone wrong once: a rename left LANGSMITH_API_KEY listed here
# while it no longer existed in .env.example, so preflight demanded a key no
# fresh clone could supply and stopped deploy.sh with it, since deploy sources
# this file. Anything added below must be genuinely unable to run without.
REQUIRED_KEYS=(
    AZURE_OPENAI_API_KEY
    AZURE_OPENAI_ENDPOINT
    AZURE_EMBEDDING_API_KEY
    AZURE_EMBEDDING_ENDPOINT
)

# Optional, and worth MENTIONING rather than demanding. Hosted tracing that is
# off is a normal state; hosted tracing that is off because nobody knew it
# existed is not. /healthz reports `tracing_enabled` so a dead one stays
# visible, and the local trace reader works either way:
#
#     uv run python -m agentcore.tracing "your question"
OPTIONAL_KEYS=(LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY)

# Secret but machine-generatable - never prompt for these.
GENERATED_KEYS=(APP_SECRET)

# Needed only by the optional graph arm, which no longer ships a container
# (DECISIONS D48). Still generated rather than prompted for: a scenario that
# brings its own Neo4j needs a value here, and prompting would block everyone
# who does not.
OPTIONAL_GENERATED=(NEO4J_PASSWORD)

# Below this much TOTAL RAM the machine gets the lean profile.
MEM_THRESHOLD_GB="${MEM_THRESHOLD_GB:-6}"

# ---------------------------------------------------------------------------
# 1 + 2. Credentials
# ---------------------------------------------------------------------------

env_value() {
    local key="$1"
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n1 | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

set_env_value() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        # Delimiter is | and the value is escaped, because these are API keys:
        # they routinely contain / and &, which sed would otherwise eat.
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

    # Generated secrets first - silent, no prompt.
    local key
    for key in "${GENERATED_KEYS[@]}"; do
        if [ -z "$(env_value "$key")" ]; then
            set_env_value "$key" "$(head -c 32 /dev/urandom | base64 | tr -d '\n=/+')"
            echo "[preflight] generated $key"
        fi
    done

    for key in "${OPTIONAL_GENERATED[@]}"; do
        if [ -z "$(env_value "$key")" ]; then
            set_env_value "$key" "$(head -c 18 /dev/urandom | base64 | tr -d '
=/+')"
            echo "[preflight] generated $key (graph arm)"
        fi
    done

    local missing=()
    for key in "${REQUIRED_KEYS[@]}"; do
        [ -z "$(env_value "$key")" ] && missing+=("$key")
    done

    # Say once, quietly, when an optional integration is off. Not an error and
    # not a prompt: the point is that nobody discovers at 16:00 that tracing
    # was never configured.
    local unset_optional=()
    for key in "${OPTIONAL_KEYS[@]}"; do
        [ -z "$(env_value "$key")" ] && unset_optional+=("$key")
    done
    if [ ${#unset_optional[@]} -gt 0 ]; then
        echo "[preflight] hosted tracing is OFF (${unset_optional[*]} not set). Optional."
        echo "[preflight]   local traces still work: uv run python -m agentcore.tracing \"question\""
    fi

    if [ ${#missing[@]} -eq 0 ]; then
        echo "[preflight] credentials OK (${#REQUIRED_KEYS[@]} required keys present)"
        return 0
    fi

    # No terminal (CI, a pipe, a hook): do not hang waiting on stdin.
    if [ ! -t 0 ]; then
        echo "[preflight] ERROR: .env is missing values for:" >&2
        printf '  - %s\n' "${missing[@]}" >&2
        echo "[preflight] fill them in $ENV_FILE, or run from a terminal to be prompted." >&2
        return 1
    fi

    echo ""
    echo "  Setup - ${#missing[@]} value(s) needed. Asked once, then saved to .env."
    echo "  Everything else ships pre-filled; only your Azure details are personal."
    echo ""
    for key in "${missing[@]}"; do
        local value=""
        while [ -z "$value" ]; do
            case "$key" in
                *API_KEY) read -r -s -p "  $key (hidden): " value; echo "" ;;
                *)        read -r -p "  $key: " value ;;
            esac
            [ -z "$value" ] && echo "    (required - cannot be empty)"
        done
        set_env_value "$key" "$value"
    done
    echo ""
    echo "[preflight] credentials saved to .env"
}

# ---------------------------------------------------------------------------
# 3. Machine capability
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
    echo "[preflight] profile: ${DEPLOY_PROFILE}  (threshold ${MEM_THRESHOLD_GB}GB total; free RAM deliberately not used to decide)"

    if [ "$DEPLOY_PROFILE" = "lean" ]; then
        echo "[preflight]   -> Neo4j not started. The graph arm is optional:"
        echo "[preflight]      a domain whose graph_queries() returns {} never offers it."
        # A big Windows host with a small WSL slice is a config problem, not a
        # small machine - say so instead of silently degrading it.
        if grep -qiE "microsoft|wsl" /proc/version 2>/dev/null; then
            echo "[preflight]   NOTE: WSL detected. If this host has more RAM than ${mem_total_gb}GB,"
            echo "[preflight]         raise the allocation in C:\Users\<you>\.wslconfig:"
            echo "[preflight]           [wsl2]"
            echo "[preflight]           memory=5GB"
            echo "[preflight]         then run: wsl --shutdown   (this stops all containers)"
        fi
    fi
}

preflight_credentials
preflight_profile

export DEPLOY_PROFILE
