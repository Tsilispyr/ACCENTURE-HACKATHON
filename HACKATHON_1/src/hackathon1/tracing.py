"""Langfuse CallbackHandler wiring.

Underscore filename -- deliberately not "langfuse-tracing.py" like
day04/src/day04/langfuse-tracing.py, whose hyphenated name makes it
unimportable as a module (Python identifiers can't contain hyphens). This
one is importable.
"""

from __future__ import annotations

import os

from langfuse.langchain import CallbackHandler


def get_callback_handlers() -> list:
    """Return [CallbackHandler()] if Langfuse credentials are configured, else [].

    Guarded so unit tests never need live Langfuse/network access, and the
    app still starts and serves requests with tracing silently disabled if
    the infra stack isn't up (e.g. local `uv run uvicorn` dev without Docker).
    """
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return []
    return [CallbackHandler()]
