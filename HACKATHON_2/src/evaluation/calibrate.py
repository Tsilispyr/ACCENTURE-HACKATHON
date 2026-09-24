"""Find the right distance ceiling for a corpus.

    DOMAIN=<name> uv run python -m evaluation.calibrate

Why this exists as a command rather than a paragraph of advice: the numbers in
a RetrievalPolicy are calibrated to ONE corpus. Copied to another they are
wrong, and wrong in a way that presents as a prompt problem - the system
either refuses everything or grounds on noise, and the afternoon goes into
rewriting prompts that were never the issue.

It uses the domain's own eval cases as the "real questions", so the only thing
to supply is nonsense.
"""

from __future__ import annotations

import argparse

from agentcore.rag.vector import open_store, retrieve
from agentcore.registry import load_domain

NONSENSE = [
    "how do I bake sourdough bread",
    "what is the offside rule in football",
    "asdfgh qwerty zxcvbn",
    "best hiking trails near Innsbruck",
]


def run(domain_name: str | None = None, *, samples: int | None = None) -> dict[str, float]:
    domain = load_domain(domain_name)
    policy = domain.retrieval_policy().model_copy()
    policy.max_distance = None      # measure the raw distribution
    policy.relative_margin = None
    store = open_store(domain.corpus().collection)

    # ALL non-adversarial cases by default, not a sample. Measured on this
    # corpus: 4 questions suggested a ceiling of 0.58, which would have
    # rejected the hardest real question at 0.658. The ceiling is set by the
    # WORST real question, so a sample that misses it sets it too tight.
    real = [c.question for c in domain.eval_cases() if not c.adversarial]
    if samples:
        real = real[:samples]
    if not real:
        raise SystemExit("This domain has no non-adversarial eval cases to calibrate against.")

    def best(question: str) -> float | None:
        found = retrieve(store, question, policy, k=1)
        return found[0].score if found and found[0].score is not None else None

    print(f"Calibrating '{domain.name}'\n")
    print("REAL questions (these must pass):")
    real_scores = []
    for question in real:
        score = best(question)
        if score is not None:
            real_scores.append(score)
            print(f"   {score:.3f}  {question[:66]}")

    print("\nNONSENSE (these must be rejected):")
    junk_scores = []
    for question in NONSENSE:
        score = best(question)
        if score is not None:
            junk_scores.append(score)
            print(f"   {score:.3f}  {question[:66]}")

    if not real_scores or not junk_scores:
        raise SystemExit("Not enough hits to calibrate. Is the collection indexed?")

    worst_real, best_junk = max(real_scores), min(junk_scores)
    print(f"\nworst real question : {worst_real:.3f}")
    print(f"best nonsense       : {best_junk:.3f}")

    if best_junk <= worst_real:
        print(
            "\nNO GAP. Nonsense scores as well as a real question, so no single "
            "ceiling separates them.\nThat usually means the corpus does not "
            "actually cover the eval questions - check the index before\n"
            "tuning anything else."
        )
        return {"max_distance": round(worst_real + 0.02, 2), "gap": 0.0}

    suggested = round((worst_real + best_junk) / 2, 2)

    from evaluation.ledger import record

    record(
        domain=domain.name, experiment="calibration", arm="distances",
        metrics={"worst_real": worst_real, "best_nonsense": best_junk,
                 "suggested_ceiling": suggested,
                 "configured_ceiling": domain.retrieval_policy().max_distance or 0,
                 "gap": best_junk - worst_real},
        n=len(real_scores),
        detail={"real": [round(s, 3) for s in real_scores],
                "nonsense": [round(s, 3) for s in junk_scores]},
    )
    print(f"\nSUGGESTED max_distance = {suggested}   (midpoint of a {best_junk - worst_real:.3f} gap)")
    print(f"Put it in {domain.name}'s retrieval_policy(). A wider gap is a healthier corpus.")
    return {"max_distance": suggested, "gap": round(best_junk - worst_real, 3)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default=None)
    parser.add_argument("--samples", type=int, default=None,
                        help="limit real questions (default: all - see the note in run())")
    args = parser.parse_args()
    run(args.domain, samples=args.samples)
