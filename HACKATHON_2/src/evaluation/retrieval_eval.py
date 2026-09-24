"""Does retrieval actually return the right section?

    DOMAIN=sample_policy uv run python -m evaluation.retrieval_eval

This is the eval that runs first and runs constantly. It costs no LLM calls,
finishes in seconds, and it is the one that tells you whether the day's corpus
is indexed correctly - before any amount of prompt tuning can mislead you.

Labels are compared WHOLE, never as substrings: 'Article 3' is a prefix of
'Article 33', and substring matching would score wrong hits as correct.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

from langchain_core.documents import Document

from agentcore.contracts import EvalCase
from agentcore.rag.vector import open_store
from agentcore.registry import load_domain

TOP_KS = (1, 3, 5)


def label_of(doc: Document) -> str:
    """'Article 33' from metadata, not from parsing the section string.

    The indexer already recorded kind and number, so this needs no regex and no
    knowledge of any particular document's vocabulary.
    """
    meta = doc.metadata
    kind, number = meta.get("kind", ""), meta.get("number", 0)
    if kind and number:
        return f"{str(kind).title()} {number}"
    return str(meta.get("section", ""))


@dataclass
class Result:
    name: str
    recall: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    ranks: dict[str, int | None] = field(default_factory=dict)
    top1: dict[str, str] = field(default_factory=dict)
    scored: int = 0
    # Questions that returned NOTHING at all. Only the gated row sets this,
    # and it is the first number to read: a mis-set ceiling shows up here as
    # "every question empty" while recall above it still looks perfect.
    empty: int = 0


def score(store, cases: list[EvalCase], name: str, *, metadata_filter=None,
          hybrid_policy=None, collection: str = "") -> Result:
    """recall@k and mean reciprocal rank over every case with a known label.

    Passing hybrid_policy routes through the fused retriever instead of raw
    similarity search, so the two can be compared on identical ground truth.
    """
    graded = [c for c in cases if c.expected_labels]
    hits = {k: 0 for k in TOP_KS}
    reciprocal = 0.0
    result = Result(name=name, scored=len(graded))

    for case in graded:
        if hybrid_policy is not None:
            from agentcore.rag.hybrid import retrieve_documents

            docs = retrieve_documents(store, case.question, hybrid_policy,
                                      collection, k=max(TOP_KS))
        else:
            docs = store.similarity_search(case.question, k=max(TOP_KS), filter=metadata_filter)
        # One labelling path for both arms, straight from metadata.
        found = [label_of(d) for d in docs]
        rank = next((i + 1 for i, label in enumerate(found) if label in case.expected_labels), None)

        result.ranks[case.question] = rank
        result.top1[case.question] = found[0] if found else "(nothing)"
        for k in TOP_KS:
            if rank is not None and rank <= k:
                hits[k] += 1
        if rank:
            reciprocal += 1 / rank

    if graded:
        result.recall = {k: hits[k] / len(graded) for k in TOP_KS}
        result.mrr = reciprocal / len(graded)
    return result


def gated_yield(store, cases: list[EvalCase], policy) -> Result:
    """recall@k through `retrieve`, so the distance ceiling is included.

    This is the only row that reflects what `s3_ground` actually receives.
    Everything else here measures ranking and stops short of the gate.

    `scored` counts questions that returned ANY evidence at all, which is the
    number to look at first: a domain whose ceiling is mis-set scores zero
    here while scoring perfectly above.
    """
    graded = [c for c in cases if c.expected_labels]
    hits = {k: 0 for k in TOP_KS}
    reciprocal = 0.0
    result = Result(name="through the gate", scored=len(graded))
    empty = 0

    for case in graded:
        scored = store.similarity_search_with_score(
            case.question, k=max(TOP_KS), filter=policy.metadata_filter
        )
        # The same two stages `vector.retrieve` applies. Mirrored rather than
        # called, so the ONE labelling path (`label_of`, straight from
        # metadata) is preserved - a second labeller is what produced a wrong
        # number once already, see PROBLEMS P02. Keep these four lines in step
        # with `vector.retrieve` if that gate ever changes.
        kept = [
            (doc, s) for doc, s in scored
            if policy.max_distance is None or s <= policy.max_distance
        ]
        if kept and policy.relative_margin is not None:
            cutoff = kept[0][1] + policy.relative_margin
            kept = [(doc, s) for doc, s in kept if s <= cutoff]

        if not kept:
            empty += 1
        found = [label_of(doc) for doc, _ in kept]
        rank = next((i + 1 for i, label in enumerate(found) if label in case.expected_labels), None)

        result.ranks[case.question] = rank
        result.top1[case.question] = found[0] if found else "(nothing)"
        for k in TOP_KS:
            if rank is not None and rank <= k:
                hits[k] += 1
        if rank:
            reciprocal += 1 / rank

    if graded:
        result.recall = {k: hits[k] / len(graded) for k in TOP_KS}
        result.mrr = reciprocal / len(graded)
    result.empty = empty
    return result


def report(results: list[Result]) -> None:
    print(f"\n{'':<22}{'cases':>6}  " + "".join(f"recall@{k:<5}" for k in TOP_KS) + "MRR")
    for r in results:
        cells = "".join(f"{r.recall.get(k, 0):<12.0%}" for k in TOP_KS)
        note = f"   {r.empty}/{r.scored} returned NOTHING" if r.empty else ""
        print(f"{r.name:<22}{r.scored:>6}  {cells}{r.mrr:.3f}{note}")

    # An aggregate over ~20 questions hides which ones moved. One question is
    # ~4.5%, so per-question ranks are the honest view.
    primary = results[0]
    misses = [(q, r) for q, r in primary.ranks.items() if r != 1]
    if misses:
        print(f"\nNot at rank 1 ({len(misses)}/{primary.scored}) in '{primary.name}':")
        for question, rank in misses:
            print(f"   rank {rank or 'miss':<5} got {primary.top1[question]:<14} {question[:52]}")


def run(domain_name: str | None = None, *, compare: bool = True,
        always_hybrid: bool = False) -> Result:
    domain = load_domain(domain_name)
    cases = domain.eval_cases()
    policy = domain.retrieval_policy()
    store = open_store(domain.corpus().collection)

    results = [score(store, cases, "vector", metadata_filter=policy.metadata_filter)]

    # WHAT SURVIVES THE DISTANCE GATE, which every arm above ignores.
    #
    # `score` calls `similarity_search` directly, so it measures RANKING: is
    # the right chunk in the top k. That is the correct question for recall@k,
    # and it is blind to the ceiling the pipeline actually applies.
    #
    # The two came apart badly once. `vendor_risk` inherited max_distance=0.70
    # from another corpus, its correct top hit scored 0.7007, and the vector
    # arm therefore returned NOTHING for any question - while this eval
    # reported 100% recall@1. BM25 carried the entire system and the failure
    # was invisible from here.
    #
    # So this row reports the pipeline's real yield. A large gap between it and
    # `vector` means the CEILING is wrong, not the ranking.
    results.append(gated_yield(store, cases, policy))

    if compare and always_hybrid:
        # Force the comparison even when the domain has hybrid off, so the
        # decision stays evidence-backed rather than inherited.
        forced = policy.model_copy(update={"hybrid": True})
        results.append(
            score(store, cases, "hybrid", hybrid_policy=forced,
                  collection=domain.corpus().collection)
        )
    elif compare and policy.hybrid:
        # The comparison IS the finding. Hybrid is a claim until measured on
        # this corpus, and a corpus of pure paraphrased prose may not need it.
        results.append(
            score(store, cases, "hybrid (v+bm25)", hybrid_policy=policy,
                  collection=domain.corpus().collection)
        )

    if compare and policy.metadata_filter:
        # The comparison IS the finding: it is what shows the metadata filter
        # earning its place rather than being an unexamined default.
        results.append(score(store, cases, "unfiltered", metadata_filter=None))

    report(results)

    # Record every arm. A number that only ever reached a terminal is a number
    # nobody can cite later.
    from evaluation.ledger import record

    for result in results:
        if not result.scored:
            continue
        record(
            domain=domain.name,
            experiment="retrieval",
            arm=result.name,
            metrics={f"recall@{k}": v for k, v in result.recall.items()} | {"mrr": result.mrr},
            n=result.scored,
            note=f"filter={policy.metadata_filter} hybrid={policy.hybrid}",
        )
    return results[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default=None)
    parser.add_argument("--no-compare", action="store_true")
    parser.add_argument("--force-hybrid", action="store_true",
                        help="measure the hybrid arm even if the domain disables it")
    args = parser.parse_args()
    run(args.domain, compare=not args.no_compare, always_hybrid=args.force_hybrid)
