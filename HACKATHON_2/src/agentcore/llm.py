"""The only place load_dotenv() is called, and the only place clients are built.

Why here and nowhere else: AzureChatOpenAI validates credentials EAGERLY at
construction, so if .env is loaded by whichever module happens to import first,
import order decides whether startup works. Loading it beside the constructors
removes that coupling.

Note the embedding client uses a SEPARATE endpoint and key from the chat client
on this subscription. Reusing the chat credentials is the single most common
setup failure here.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings

# Explicit path, not a bare load_dotenv(): that walks up from the CALLER and
# stops at the first .env it finds, so a stray .env in a subdirectory would
# silently shadow this one.
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Run `bash scripts/preflight.sh` to fill .env, "
            f"or set it in the environment."
        )
    return value


@lru_cache(maxsize=1)
def chat_model() -> AzureChatOpenAI:
    """The reasoning model. temperature=0 so runs are comparable across evals."""
    return AzureChatOpenAI(
        azure_endpoint=_require("AZURE_OPENAI_ENDPOINT"),
        api_key=_require("AZURE_OPENAI_API_KEY"),
        azure_deployment=_require("AZURE_OPENAI_DEPLOYMENT_NAME"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
        temperature=0,
    )


@lru_cache(maxsize=1)
def judge_model() -> AzureChatOpenAI:
    """Separate handle for evaluation.

    Same deployment today, but keeping it distinct means swapping the judge for
    a different model later is a one-line change - and a judge that shares a
    cached client with the thing it judges is easy to confuse.
    """
    return AzureChatOpenAI(
        azure_endpoint=_require("AZURE_OPENAI_ENDPOINT"),
        api_key=_require("AZURE_OPENAI_API_KEY"),
        azure_deployment=os.getenv("AZURE_OPENAI_JUDGE_DEPLOYMENT")
        or _require("AZURE_OPENAI_DEPLOYMENT_NAME"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
        temperature=0,
    )


@lru_cache(maxsize=1)
def embedding_model() -> AzureOpenAIEmbeddings:
    """SEPARATE endpoint and key - not the chat ones. See module docstring."""
    return AzureOpenAIEmbeddings(
        azure_endpoint=_require("AZURE_EMBEDDING_ENDPOINT"),
        api_key=_require("AZURE_EMBEDDING_API_KEY"),
        azure_deployment=_require("AZURE_OPENAI_EMBEDDING_DEPLOYMENT"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
    )


@lru_cache(maxsize=1)
def langfuse_handler():
    """The tracing callback, or None if Langfuse is not configured.

    Team decision, and the course's guide 04 is the Langfuse guide: Langfuse
    rather than LangSmith. Bound once in `pipeline/graph.py`, so no call site
    has to thread a callback through.

    Returns None when unconfigured, and every call site already tolerates that.
    A missing key is an ordinary state, not a failure: the always-on layer is
    the LangGraph audit trail, which needs no account at all.
    """
    if not os.getenv("LANGFUSE_PUBLIC_KEY", "").strip():
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception:  # noqa: BLE001 - tracing must never break a run
        return None


def database_url(*, driver: str = "postgresql+psycopg") -> str:
    """Assemble the Postgres URL with proper quoting.

    Never hand-write this. Passwords routinely contain characters that change
    the meaning of a URL: a '#' silently truncated one on a previous project,
    and an unencoded '@' was rejected by a driver on another.
    """
    from urllib.parse import quote

    user = quote(os.getenv("POSTGRES_USER", "h2"), safe="")
    password = quote(os.getenv("POSTGRES_PASSWORD", ""), safe="")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5446")
    database = os.getenv("POSTGRES_DB", "hackathon2")
    return f"{driver}://{user}:{password}@{host}:{port}/{database}"
