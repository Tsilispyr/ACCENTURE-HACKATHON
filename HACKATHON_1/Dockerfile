FROM python:3.12-slim

# Pinned. `uv sync --frozen` exists to guarantee the image cannot drift from
# the lockfile -- pulling the resolver itself from a moving :latest tag quietly
# undermines exactly that guarantee.
COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/

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

# ---------------------------------------------------------------------------
# Drop root. Nothing here needs it at runtime: the venv and source were built
# above as root and are only read afterwards, and the app binds 8000, not a
# privileged port. Created with a fixed uid so file ownership is predictable
# if a volume is ever mounted in.
#
# Done AFTER both uv sync layers so the dependency cache is unaffected --
# switching user earlier would invalidate it on every rebuild.
# ---------------------------------------------------------------------------
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "hackathon1.service:app", "--host", "0.0.0.0", "--port", "8000"]
