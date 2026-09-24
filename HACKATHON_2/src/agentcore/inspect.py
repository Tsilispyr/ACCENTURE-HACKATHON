"""See exactly what goes to the model, and what came back from retrieval.

    DOMAIN=sample_policy uv run python -m agentcore.inspect "your question"

WHY THIS EXISTS. "The answer was wrong" is not one problem, it is four, and
from the outside they look identical:

    1. the right chunk was never retrieved          -> a retrieval problem
    2. it was retrieved but ranked below the cutoff -> a threshold problem
    3. it reached the prompt and was ignored        -> a prompt problem
    4. it reached the prompt and was misread        -> a model problem

Only the first two are visible without instrumentation, and only by looking.
Every hour spent rewriting a prompt for a problem of type 1 is an hour wasted,
and that is the single most common way an afternoon disappears.

So this prints, for one question: what each retrieval arm returned, which
chunks survived the distance gate, the exact prompt text assembled from them,
and its token cost. No LLM call is made unless you ask for one.
"""

from __future__ import annotations

import argparse

from agentcore.contracts import Actor
from agentcore.rag.hybrid import retrieve as hybrid_retrieve
from agentcore.rag.vector import open_store, parse_locator
from agentcore.registry import load_domain
from agentcore.safety.patterns import compile_patterns, scan
from agentcore.safety.untrusted import envelope, summarise
from agentcore.world import bound

RULE = "=" * 78


def estimate_tokens(text: str) -> int:
    """Rough, and labelled as rough. ~4 characters per token for English."""
    return len(text) // 4


def inspect(question: str, *, scope: str = "public", show_full: bool = False) -> None:
    domain = load_domain()
    policy = domain.retrieval_policy()
    collection = domain.corpus().collection
    actor = Actor(id="inspector", role="admin", scope=scope)

    print(f"\n{RULE}\nQUESTION  {question!r}")
    print(f"DOMAIN    {domain.name}   collection={collection}   actor scope={scope!r}")
    print(RULE)

    # --- 0. what the router will do ---------------------------------------
    locator = parse_locator(question, policy.locators)
    print("\n[0] ROUTING")
    if locator:
        print(f"    LOCATOR MATCH {locator} - this bypasses similarity search entirely")
    else:
        print(f"    no locator ({policy.locators or 'none declared'}); similarity search")

    # --- 1. input screening ------------------------------------------------
    patterns = compile_patterns(domain.blocked_patterns())
    hit = scan(question, patterns)
    print("\n[1] INPUT GUARD")
    print(f"    {'REFUSED - matched ' + repr(hit) if hit else 'passed'}")
    if hit:
        print("\n    Nothing further would run. Retrieval and planning are skipped.")
        return

    # --- 2. retrieval, arm by arm -----------------------------------------
    store = open_store(collection)
    with bound(actor, "inspect"):
        evidence, trace = hybrid_retrieve(store, question, policy, collection)

    print("\n[2] RETRIEVAL")
    print(f"    arms      : {' + '.join(trace.arms)}")
    print(f"    filter    : {trace.filtered_by}")
    print(f"    ceiling   : max_distance={policy.max_distance} margin={policy.relative_margin}")
    print(f"\n    vector arm returned {len(trace.vector)}:")
    for line in trace.vector:
        print(f"       {line[:92]}")
    if "bm25" in trace.arms:
        print(f"\n    lexical arm returned {len(trace.lexical)}:")
        for line in trace.lexical or ["(nothing - no shared terms)"]:
            print(f"       {line[:92]}")
        print(f"\n    after fusion ({len(trace.fused)}):")
        for line in trace.fused:
            print(f"       {line[:92]}")

    if not evidence:
        print("\n    NOTHING SURVIVED THE GATE.")
        print("    Either the corpus does not cover this, or max_distance is too tight.")
        print("    Check which with:  uv run python -m evaluation.calibrate")
        return

    # --- 3. injection screening of what came back --------------------------
    print("\n[3] RETRIEVED-CONTENT SCREENING")
    flagged = [(e.cite(), scan(e.text, patterns)) for e in evidence]
    flagged = [(cite, m) for cite, m in flagged if m]
    if flagged:
        for cite, matched in flagged:
            print(f"    FLAGGED {cite[:60]} - {matched!r}")
        print("    (recorded, never obeyed - and never fatal)")
    else:
        print("    clean")

    # --- 4. what the PLANNER sees ------------------------------------------
    planner_view = summarise(evidence)
    print("\n[4] WHAT THE PLANNER SEES  (summaries only - never raw document text)")
    print(f"    {estimate_tokens(planner_view)} tokens approx")
    for line in planner_view.splitlines():
        print(f"    {line[:92]}")

    # --- 5. what the ANSWERING model sees ----------------------------------
    body = envelope(evidence)
    prompt = f"{domain.persona()}\n\nREQUEST:\n{question}\n\n{body}"
    print("\n[5] WHAT THE ANSWERING MODEL SEES")
    print(f"    persona  {estimate_tokens(domain.persona())} tokens approx")
    print(f"    evidence {estimate_tokens(body)} tokens approx ({len(evidence)} extracts)")
    print(f"    TOTAL    {estimate_tokens(prompt)} tokens approx")

    print(f"\n{RULE}")
    print(prompt if show_full else prompt[:1800] + ("\n... [--full for all]" if len(prompt) > 1800 else ""))
    print(RULE)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question", nargs="+")
    parser.add_argument("--domain", default=None)
    parser.add_argument("--scope", default="public", help="inspect as an actor with this scope")
    parser.add_argument("--full", action="store_true", help="print the entire prompt")
    args = parser.parse_args()

    if args.domain:
        import os

        os.environ["DOMAIN"] = args.domain
    inspect(" ".join(args.question), scope=args.scope, show_full=args.full)
