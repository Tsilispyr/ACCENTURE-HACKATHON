"""Build the index for a domain's corpus.

    DOMAIN=sample_policy uv run python -m agentcore.rag.index
    DOMAIN=sample_policy uv run python -m agentcore.rag.index --reset

Separate from the app on purpose: indexing is slow, costs embedding calls, and
should happen once - not on every API boot.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from agentcore.rag.chunking import chunk_corpus
from agentcore.rag.vector import index_chunks, is_empty, open_store
from agentcore.registry import load_domain


def build(domain_name: str | None = None, *, reset: bool = False) -> int:
    domain = load_domain(domain_name)
    corpus = domain.corpus()

    paths = [Path(p) for p in corpus.paths]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise SystemExit(f"Corpus files not found: {[str(p) for p in missing]}")

    print(f"Indexing domain '{domain.name}' -> collection '{corpus.collection}'")
    chunks = chunk_corpus(
        paths,
        source=paths[0].name if len(paths) == 1 else domain.name,
        section_finder=domain.section_finder(),
        is_heading=domain.is_heading,
        chunk_size=corpus.chunk_size,
        chunk_overlap=corpus.chunk_overlap,
        cache_dir=Path(".cache"),
    )

    from evaluation.ledger import record

    sections = domain.section_finder()
    record(
        domain=domain.name, experiment="chunking", arm="corpus",
        metrics={"chunks": len(chunks),
                 "pages": len({c.metadata.get("page") for c in chunks}),
                 "sections": len({c.metadata.get("section") for c in chunks}),
                 "avg_chunk_chars": sum(len(c.page_content) for c in chunks) / max(len(chunks), 1)},
        n=len(chunks),
        note=f"size={corpus.chunk_size} overlap={corpus.chunk_overlap}",
    )

    store = open_store(corpus.collection, reset=reset)
    if not reset and not is_empty(store):
        print(f"  collection '{corpus.collection}' already populated; --reset to rebuild")
        return 0

    return index_chunks(store, chunks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default=None, help="overrides the DOMAIN env var")
    parser.add_argument("--reset", action="store_true", help="drop and rebuild the collection")
    args = parser.parse_args()
    count = build(args.domain, reset=args.reset)
    print(f"done: {count} chunks")
