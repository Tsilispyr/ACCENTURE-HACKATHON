"""pgvector store: open, index, retrieve.

Ported from the course project's search_GDPR.py. Two things carried over that
are easy to omit and expensive to rediscover:

  * a CONNECT TIMEOUT - psycopg has no default, so a missing server hangs
    forever instead of reaching the error message that says how to start it.
  * TWO-STAGE distance gating - an absolute ceiling rejects nonsense, and a
    relative margin from the best hit prunes a weak tail. One hardcoded number
    cannot do both: 0.60 rejected a real question by 0.002, and 0.90 admitted
    'how do I bake sourdough bread'.

Scores are COSINE DISTANCE: lower is closer, range 0..2 in theory but ~0.4..0.9
in practice. Calibration is per corpus - see DECISIONS.md D14.
"""

from __future__ import annotations

import os
import re
from typing import Any

from langchain_core.documents import Document
from langchain_postgres import PGVector
from sqlalchemy import text as sa_text

from agentcore.contracts import Evidence, RetrievalPolicy
from agentcore.llm import database_url, embedding_model

CONNECT_TIMEOUT = 5
CONNECT_RETRIES = 12      # ~30s total, see open_store
BATCH = 100

# A reader who already knows where they are should be able to say so instead of
# describing it and hoping similarity agrees.
#
# The KINDS come from the domain, never from a list here. 'page' and 'chunk'
# are structural and exist for every corpus; anything else is one document's
# vocabulary, and hardcoding it would put scenario knowledge in the core.
UNIVERSAL_LOCATORS = ("page", "chunk")


def open_store(collection: str, *, reset: bool = False) -> PGVector:
    """Connect, failing with an explanation rather than a driver traceback.

    A domain may supply its own store via store_factory() - that is how the
    deterministic domain runs the whole pipeline, and the CI eval gate, with no
    database at all.
    """
    from agentcore.registry import load_domain

    factory = load_domain().store_factory()
    if factory is not None:
        return factory(collection, reset=reset)

    # VECTOR_BACKEND=chroma runs in-process with no server. Same pipeline, same
    # filters; see rag/chroma_store.py for why a second store exists at all.
    if os.getenv("VECTOR_BACKEND", "pgvector").lower() == "chroma":
        from agentcore.rag.chroma_store import open_chroma

        return open_chroma(collection, reset=reset)

    # Retry before giving up, because on WSL "the database is up" and "the
    # database is reachable from Windows" are different events several seconds
    # apart. The container healthcheck passes INSIDE the VM while Windows'
    # localhost forwarding has not yet registered the published port, and a
    # connection in that window is REFUSED - instantly, so a short retry loop
    # burns through it without waiting. Measured on this machine: forwarding
    # takes up to ~20s to appear after a cold WSL start.
    #
    # The failure this prevents is the worst kind: intermittent, indistinguishable
    # from a network fault, and it disappears while you are investigating it.
    import time

    last_error: Exception | None = None
    for attempt in range(CONNECT_RETRIES):
        try:
            store = PGVector(
                embeddings=embedding_model(),
                connection=database_url(),
                collection_name=collection,
                use_jsonb=True,  # required for metadata filtering
                pre_delete_collection=reset,
                engine_args={
                    "connect_args": {"connect_timeout": CONNECT_TIMEOUT},
                    # Validate a pooled connection before handing it out. Without
                    # this, a connection opened while the port proxy was up and
                    # reused after it blinked fails at QUERY time, far from
                    # anything that looks like a connection problem.
                    "pool_pre_ping": True,
                },
            )
            # PROVE it is usable. SQLAlchemy's pool connects lazily, so
            # constructing PGVector succeeds against a database that cannot be
            # reached - and the failure then surfaces at the first query,
            # nowhere near this retry loop. One cheap round trip closes that gap.
            with store._make_sync_session() as probe:  # noqa: SLF001
                probe.execute(sa_text("SELECT 1"))
            return store
        except Exception as error:  # noqa: BLE001
            last_error = error
            if attempt < CONNECT_RETRIES - 1:
                if attempt == 2:
                    print("  waiting for postgres to become reachable...")
                time.sleep(2.5)

    try:
        raise last_error  # type: ignore[misc]
    except Exception as error:  # noqa: BLE001 - re-raised with guidance
        host = f"{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5446')}"
        raise RuntimeError(
            f"Could not connect to Postgres at {host}\n"
            f"  {type(error).__name__}: {error}\n\n"
            f"Start it with:  bash scripts/deploy.sh"
        ) from error


def index_chunks(store: PGVector, chunks: list[Document]) -> int:
    """Embed and store, in batches so progress is visible."""
    for start in range(0, len(chunks), BATCH):
        store.add_documents(chunks[start : start + BATCH])
        print(f"  indexed {min(start + BATCH, len(chunks))}/{len(chunks)}")
    return len(chunks)


def is_empty(store: PGVector) -> bool:
    """One cheap probe: an empty collection returns nothing for any query."""
    try:
        return not store.similarity_search("test", k=1)
    except Exception:  # noqa: BLE001 - an unreadable collection is an empty one
        return True


def scoped_filter(policy: RetrievalPolicy, extra: dict[str, Any] | None = None) -> dict | None:
    """Combine the domain's filter, the caller's, and the actor's scope.

    The scope term is what makes actor A unable to retrieve actor B's rows. It
    is ANDed in here, at the one place every retrieval passes through, rather
    than being left to each call site to remember.
    """
    from agentcore.world import current_scope

    terms = [t for t in (policy.metadata_filter, extra) if t]

    scope = current_scope()
    if scope and scope != "public":
        terms.append({"$or": [{"scope": {"$eq": scope}}, {"scope": {"$eq": "public"}}]})

    if not terms:
        return None
    return terms[0] if len(terms) == 1 else {"$and": terms}


def retrieve(
    store: PGVector,
    query: str,
    policy: RetrievalPolicy,
    *,
    k: int | None = None,
    extra_filter: dict[str, Any] | None = None,
) -> list[Evidence]:
    """Semantic search with two-stage distance gating."""
    k = k or policy.k
    found = store.similarity_search_with_score(query, k=k, filter=scoped_filter(policy, extra_filter))

    ceiling = policy.max_distance
    hits = [(d, s) for d, s in found if ceiling is None or s <= ceiling]

    if hits and policy.relative_margin is not None:
        cutoff = hits[0][1] + policy.relative_margin
        hits = [(d, s) for d, s in hits if s <= cutoff]

    return [to_evidence(doc, score) for doc, score in hits]


def fetch_locator(
    store: PGVector,
    kind: str,
    number: int,
    policy: RetrievalPolicy,
    *,
    limit: int = 30,
) -> list[Evidence]:
    """A locator is a METADATA lookup, not a similarity question.

    Distance is meaningless here so no threshold applies. The one embedding
    spent on the dummy query is the price of reusing similarity_search.
    """
    if kind in {"page", "chunk"}:
        where: dict[str, Any] = {"page" if kind == "page" else "index": {"$eq": number}}
    else:
        where = {"$and": [{"kind": {"$eq": kind}}, {"number": {"$eq": number}}]}

    docs = store.similarity_search(
        "reference", k=limit, filter=scoped_filter(policy, where)
    )
    docs.sort(key=lambda d: d.metadata.get("index", 0))
    return [to_evidence(d, None) for d in docs]


def parse_locator(query: str, kinds: list[str] | None = None) -> tuple[str, int] | None:
    """'what does article 33 say' -> ('article', 33). None if not a locator.

    `kinds` is the domain's own locator vocabulary; only those are recognised.
    """
    allowed = list(UNIVERSAL_LOCATORS) + [k for k in (kinds or []) if k.isalpha()]
    names = "|".join(sorted(set(allowed)))
    pattern = re.compile(rf"\b({names})\s+(\d{{1,4}})\b", re.I)
    match = pattern.search(query)
    return (match.group(1).lower(), int(match.group(2))) if match else None


def to_evidence(doc: Document, score: float | None) -> Evidence:
    meta = doc.metadata
    page = meta.get("page")
    return Evidence(
        text=doc.page_content,
        source=str(meta.get("source", "corpus")),
        locator=f"{meta.get('section', '')}{f' | page {page}' if page else ''}".strip(" |"),
        score=score,
        # Everything retrieved is attacker-influenced in a real deployment.
        trusted=False,
    )
