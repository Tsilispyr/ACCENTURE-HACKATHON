"""The Neo4j graph arm: build a graph from documents, query it safely.

TWO HALVES WITH VERY DIFFERENT RISK PROFILES, and the distinction is the whole
design:

  BUILD (index time)   `LLMGraphTransformer` extracts entities and relations
                       from unstructured text. An LLM writes the GRAPH. This is
                       fine: it runs once, offline, and the result can be
                       inspected, corrected and re-run before anyone sees it.

  QUERY (request time) named, parameterised Cypher written by a human, chosen
                       by name. An LLM picks WHICH query and supplies
                       parameters; it never writes Cypher.

What is deliberately NOT here is `GraphCypherQAChain`, which has an LLM
generate Cypher per request against a schema. That is the same class of risk as
letting a model write SQL against your database, except it also fails in front
of an audience - a malformed query at 17:05 is unrecoverable, whereas a bad
extraction at 12:00 is just another run of the builder.

This makes the graph arm practical on a corpus nobody has seen before, which
was the objection to graph RAG in the first place: you no longer need to know
the ontology in advance.

Credentials come from the environment. Nothing here is hardcoded.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from langchain_core.documents import Document

# Imported for its side effect: agentcore.llm is the ONE place load_dotenv()
# runs, and without it os.getenv here returns empty strings and every
# connection fails with a misleading "password is not set".
import agentcore.llm  # noqa: F401
from agentcore.contracts import Evidence, GraphQuery


def neo4j_settings() -> dict[str, str]:
    """Connection details, from env only."""
    return {
        "url": os.getenv("NEO4J_URI", "bolt://localhost:7688"),
        "username": os.getenv("NEO4J_USER", "neo4j"),
        "password": os.getenv("NEO4J_PASSWORD", ""),
    }


@lru_cache(maxsize=1)
def open_graph():
    """Connect, or explain how to start it.

    The graph arm is OPTIONAL everywhere: a domain with no graph_queries()
    never calls this, and a caller that catches the RuntimeError degrades to
    vector-only rather than failing the request.
    """
    from langchain_neo4j import Neo4jGraph

    settings = neo4j_settings()
    if not settings["password"]:
        raise RuntimeError(
            "NEO4J_PASSWORD is not set. Run `bash scripts/preflight.sh`, or "
            "disable the graph arm by returning {} from graph_queries()."
        )
    try:
        return Neo4jGraph(**settings)
    except Exception as error:  # noqa: BLE001
        raise RuntimeError(
            f"Could not reach Neo4j at {settings['url']}\n"
            f"  {type(error).__name__}: {error}\n\n"
            f"Start it with:  MEM_THRESHOLD_GB=1 bash scripts/deploy.sh   (the 'full' profile)"
        ) from error


def available() -> bool:
    """Is the graph arm usable right now? Never raises."""
    try:
        open_graph().query("RETURN 1 AS ok")
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ build ---


def build_from_documents(
    documents: list[Document],
    *,
    wipe: bool = False,
    allowed_nodes: list[str] | None = None,
    allowed_relationships: list[str] | None = None,
) -> dict[str, int]:
    """Extract a knowledge graph from unstructured text with an LLM.

    `allowed_nodes` / `allowed_relationships` constrain the extraction to a
    vocabulary. Worth supplying whenever you know it: unconstrained extraction
    invents a slightly different label for the same concept on every document,
    and a graph with `Service`, `service` and `SystemComponent` as three node
    types is not queryable by any hand-written Cypher.
    """
    from langchain_neo4j import LLMGraphTransformer

    from agentcore.llm import chat_model

    graph = open_graph()
    if wipe:
        # Explicit, never implicit: this deletes everything in the database.
        graph.query("MATCH (n) DETACH DELETE n")

    transformer = LLMGraphTransformer(
        llm=chat_model(),
        allowed_nodes=allowed_nodes or [],
        allowed_relationships=allowed_relationships or [],
    )
    graph_documents = transformer.convert_to_graph_documents(documents)

    nodes = sum(len(d.nodes) for d in graph_documents)
    relationships = sum(len(d.relationships) for d in graph_documents)

    graph.add_graph_documents(
        graph_documents,
        include_source=True,   # keeps a link back to the chunk it came from
        baseEntityLabel=True,  # one shared label, so generic lookups work
    )
    graph.refresh_schema()
    return {"documents": len(graph_documents), "nodes": nodes, "relationships": relationships}


def schema() -> str:
    """What the graph actually contains. Read this before writing any Cypher."""
    graph = open_graph()
    graph.refresh_schema()
    return graph.schema


# ------------------------------------------------------------------ query ---


def run_query(query: GraphQuery, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute ONE named, human-written query with bound parameters.

    Parameters are bound by the driver, never interpolated into the string --
    the Cypher equivalent of using placeholders instead of formatting SQL.
    """
    missing = [p for p in query.params if p not in params]
    if missing:
        raise ValueError(f"missing parameters for graph query: {missing}")
    return open_graph().query(query.cypher, params)


def as_evidence(rows: list[dict[str, Any]], *, name: str, description: str) -> list[Evidence]:
    """Graph rows rendered as evidence, so they join the same envelope."""
    if not rows:
        return []
    lines = "\n".join(
        "; ".join(f"{key}={value}" for key, value in row.items()) for row in rows[:20]
    )
    return [
        Evidence(
            text=f"{description}\n{lines}",
            source="knowledge graph",
            locator=f"query {name}",
            trusted=False,  # still model-extracted; still not gospel
        )
    ]
