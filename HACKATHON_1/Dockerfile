FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# ---------------------------------------------------------------------------
# Layer 1: dependencies only.
# Docker re-runs this ONLY when pyproject.toml or uv.lock change, so ordinary
# code edits reuse it and rebuilds stay fast.
# README.md is copied here because pyproject.toml declares readme = "README.md";
# without it the build fails with "failed to open file /app/README.md".
#
# --frozen installs exactly what uv.lock pins and FAILS if the lock is out of
# date with pyproject.toml, instead of silently resolving something else. That
# is what makes a plain "docker compose up --build" trustworthy: the image can
# never drift from your local environment.
# --no-install-project keeps the app itself out of this layer, so changing your
# own source does not invalidate the dependency cache.
# --no-dev skips the dev group (pytest, pytest-asyncio, httpx) -- not wanted
# in a runtime image.
# ---------------------------------------------------------------------------
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# ---------------------------------------------------------------------------
# Layer 2: application source. Rebuilt on any code change; deps stay cached.
# ---------------------------------------------------------------------------
COPY . .
RUN uv sync --frozen --no-dev

# uv sync installs into /app/.venv, so put it first on PATH -- this is what
# makes the bare "uvicorn" below resolve.
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "hackathon1.service:app", "--host", "0.0.0.0", "--port", "8000"]
