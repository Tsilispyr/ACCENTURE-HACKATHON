"""Durable checkpointing for the incident workflow.

The workflow pauses at the approval gate and waits for a human. With an
in-memory checkpointer that wait survives only as long as the process: restart
the container and every incident awaiting approval is gone, with no way to
resume and no record that it was ever asked. That is the difference between a
demo and something an operator could rely on, so state lives in Postgres.

No new database server: `app-db-init` in docker-compose.yml already creates a
`hackathon1` database inside the infra Postgres, which is what this connects to.

Degrades deliberately. If Postgres is unreachable the app still starts, still
serves requests, and still runs workflows -- on an in-memory checkpointer, with
a loud warning saying exactly what was lost. `uv run uvicorn` with no Docker at
all has to keep working, and an incident-resolution service that refuses to
start because its archive is down would be a poor advertisement for one.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

logger = logging.getLogger(__name__)

#: Set when a Postgres checkpointer is live, so /health can report the truth
#: rather than assuming. Degradation that nothing surfaces is how this project
#: previously ran for a week with tracing silently switched off.
BACKEND: str = "memory"


def build_database_url() -> str | None:
    """Connection URL for the checkpoint database, or None if not configured.

    Assembled from parts rather than read as one string, because the password
    contains `!@` and a hand-written URL would need it percent-encoded. That
    exact mistake is on record twice in this project: once with a `#` silently
    truncating the password, and once with Prisma rejecting an unencoded `@`.
    `quote` removes the opportunity.
    """
    explicit = os.getenv("CHECKPOINT_DATABASE_URL")
    if explicit:
        return explicit

    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    user = os.getenv("POSTGRES_USER", "gtgh")
    password = os.getenv("POSTGRES_PASSWORD")
    database = os.getenv("CHECKPOINT_DATABASE", "hackathon1")
    if not password:
        return None
    return f"postgresql://{quote(user)}:{quote(password)}@{host}:{port}/{database}"


async def open_checkpointer():
    """Return an (checkpointer, closer) pair. Never raises.

    The caller owns the lifetime: `closer()` releases the pool on shutdown.
    """
    global BACKEND

    url = build_database_url()
    if not url:
        logger.warning(
            "CHECKPOINTING DEGRADED: no Postgres credentials configured, using in-memory state. "
            "Incidents awaiting approval will NOT survive a restart."
        )
        BACKEND = "memory"
        return None, _noop

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        manager = AsyncPostgresSaver.from_conn_string(url)
        checkpointer = await manager.__aenter__()
        # Idempotent: creates the checkpoint tables on first run, no-op after.
        await checkpointer.setup()
    except Exception as exc:
        logger.warning(
            "CHECKPOINTING DEGRADED: could not reach Postgres (%s: %s), using in-memory state. "
            "Incidents awaiting approval will NOT survive a restart.",
            type(exc).__name__,
            exc,
        )
        BACKEND = "memory"
        return None, _noop

    async def close() -> None:
        await manager.__aexit__(None, None, None)

    logger.info("Checkpointing to Postgres database %r", os.getenv("CHECKPOINT_DATABASE", "hackathon1"))
    BACKEND = "postgres"
    return checkpointer, close


async def _noop() -> None:
    return None
