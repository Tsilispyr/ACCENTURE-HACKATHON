"""Who is asking, and what they are allowed to see.

A ContextVar, deliberately not a module global: LangGraph runs nodes
concurrently and FastAPI serves requests concurrently, so a global here would
leak one user's scope into another user's retrieval filter. That is the exact
bug the scope-isolation test exists to catch.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from agentcore.contracts import ANONYMOUS, Actor

_actor: ContextVar[Actor] = ContextVar("actor", default=ANONYMOUS)
_request_id: ContextVar[str] = ContextVar("request_id", default="")


def current_actor() -> Actor:
    return _actor.get()


def current_scope() -> str:
    """ANDed into every retrieval filter and every scoped tool."""
    return _actor.get().scope


def current_request_id() -> str:
    return _request_id.get()


@contextmanager
def bound(actor: Actor, request_id: str = "") -> Iterator[None]:
    actor_token = _actor.set(actor)
    request_token = _request_id.set(request_id)
    try:
        yield
    finally:
        _actor.reset(actor_token)
        _request_id.reset(request_token)
