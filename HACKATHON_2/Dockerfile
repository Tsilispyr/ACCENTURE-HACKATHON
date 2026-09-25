FROM python:3.12-slim

# Pinned, not :latest. `uv sync --frozen` exists to guarantee the image cannot
# drift from the lockfile - pulling the resolver itself from a moving tag
# quietly undermines exactly that guarantee.
COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/

WORKDIR /app

# Layer 1: dependencies only, so a code change does not re-resolve them.
# --frozen installs exactly what uv.lock pins and FAILS if the lock is stale.
#
# README.md is NOT copied here, even though pyproject.toml declares
# readme = "README.md". Copying it put the documentation in the cache key for
# the dependency layer, so editing a paragraph of prose invalidated it and
# every wheel was downloaded again. A stub satisfies the metadata read; the
# real file arrives with the source in layer 2 and overwrites it.
COPY pyproject.toml uv.lock ./
RUN touch README.md
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Layer 2: application source.
#
# The cache mount is what makes a rebuild cheap when this layer DOES change.
# Without it, any invalidation above means re-downloading every wheel from the
# network; with it, uv reuses what it already has on this machine and only
# fetches what genuinely changed. The cache lives in the builder, not in the
# image, so it costs nothing in the shipped layers.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# uv sync installs into /app/.venv, so put it first on PATH.
ENV PATH="/app/.venv/bin:$PATH"

# Drop root. AFTER both sync layers, so the dependency cache is unaffected.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Shell form on purpose, so ${PORT} expands. Azure Container Apps and App
# Service both inject PORT and expect the process to bind it; a hardcoded 8000
# means the platform's health probe reaches nothing and the revision never goes
# live. Compose sets no PORT, so the default keeps local behaviour identical.
CMD uvicorn agentcore.api.service:app --host 0.0.0.0 --port ${PORT:-8000}
