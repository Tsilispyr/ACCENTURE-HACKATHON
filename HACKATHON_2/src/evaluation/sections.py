"""Print the section labels the indexer actually recorded.

    DOMAIN=vendor_risk uv run python -m evaluation.sections

Run this after ANY change to a corpus, before trusting an eval result.

The generic section finder numbers sections positionally, in filename order, so
adding one file renumbers every section after it. An eval case whose
`expected_labels` were written against the old numbering then scores a correct
retrieval as a miss - which looks like a retrieval regression and is not one.

This is the cheapest possible way to not spend an hour on that.
"""

from __future__ import annotations

import argparse

from agentcore.rag.vector import open_store
from agentcore.registry import load_domain


def run(domain_name: str | None = None) -> None:
    domain = load_domain(domain_name)
    collection = domain.corpus().collection
    store = open_store(collection)

    # No "list everything" on the store interface, so ask for far more than any
    # corpus here holds and de-duplicate by section.
    docs = store.similarity_search("document", k=2000)
    seen: dict[str, dict] = {}
    for doc in docs:
        seen.setdefault(doc.metadata.get("section", "?"), doc.metadata)

    ordered = sorted(seen.items(), key=lambda pair: pair[1].get("index", 0))
    print(f"\n{domain.name}: {len(ordered)} section(s) in collection '{collection}'\n")
    print(f"{'label':<16} {'source':<34} section")
    print("-" * 96)
    for section, meta in ordered:
        kind, number = meta.get("kind", ""), meta.get("number", 0)
        label = f"{str(kind).title()} {number}" if kind and number else "(none)"
        print(f"{label:<16} {str(meta.get('source', ''))[:32]:<34} {section[:44]}")

    print("\nUse the label column verbatim in EvalCase.expected_labels.")
    print("They are compared as WHOLE labels: 'Section 1' never matches 'Section 11'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default=None)
    run(parser.parse_args().domain)
