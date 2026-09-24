FROM python:3.12-slim

# Pinned, not :latest. `uv sync --frozen` exists to guarantee the image cannot
# drift from the lockfile - pulling the resolver itself from a moving tag
# quietly undermines exactly that guarantee.
COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/

WORKDIR /app

# Layer 1: dependencies only, so a code change does not re-resolve them.
# README.md is copied because pyproject.toml declares readme = "README.md".
# --frozen installs exactly what uv.lock pins and FAILS if the lock is stale.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# Layer 2: application source.
COPY . .
RUN uv sync --frozen --no-dev

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
