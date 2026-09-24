# Evaluation

The code is at [`../src/evaluation/`](../src/evaluation/); the output is in
[`../evaluation-results/`](../evaluation-results/). This file is here because the deliverables list
names `evaluation/`, and because the split between the two is worth stating once.

The code stays under `src/` for a mechanical reason: `pyproject.toml` declares
`[tool.uv.build-backend] module-root = "src"` for all four packages, and there is no per-module
root. Moving one out breaks `uv sync` inside the Docker build, three output paths that resolve
`parents[2]`, and the `agentcore -> evaluation` import in `rag/index.py`.

## Running it

```bash
DOMAIN=vendor_risk uv run python -m evaluation.retrieval_eval   # recall@k, MRR
DOMAIN=vendor_risk uv run python -m evaluation.agent_eval       # the full pipeline per case
DOMAIN=vendor_risk uv run python -m evaluation.timing           # per-stage wall time
bash scripts/eval_gate.sh                                       # CI gate; EXITS NON-ZERO
uv run python -m evaluation.ledger                              # everything recorded so far
uv run python -m evaluation.charts                              # render the PNGs
```

## The ten metrics

All ten the handout's section 10 asks for. Nine never make a network call.

| Metric | Question it answers | Where | Deterministic |
|---|---|---|---|
| Retrieval relevance | did RAG retrieve the right passage? | `retrieval_eval.py` | yes |
| Groundedness | are conclusions supported by the corpus? | `judge.py` | no |
| Citation correctness | does the cited source actually support it? | `metrics.py` | yes |
| Task completion | were all required risk domains covered? | `metrics.py` | yes |
| Tool correctness | were the right tools selected and used? | `metrics.py` | yes |
| Agent delegation | was work handed to the right specialist? | `metrics.py` + `trajectory.py` | yes |
| Guardrail compliance | were policy and authority limits respected? | `trajectory.py` | yes |
| Injection resistance | was embedded instruction text ignored? | adversarial eval cases | yes |
| Decision quality | does the verdict follow from the findings? | `metrics.py` | hard rules, then a judge |
| Latency / cost | is execution operationally reasonable? | `timing.py` -> ledger | yes |

## How grading works

A case passes only when four independent layers agree, cheapest first:

1. **trajectory checks** - deterministic assertions about the PROCESS. Did grounding precede
   planning? Was high risk gated? Did an owned step reach its specialist?
2. **`must_not_say`** - hard string failures.
3. **the assessment metrics** - the table above.
4. **the LLM judge** - the rubric, plus per-claim groundedness.

The judge alone is too generous; the trajectory checks alone cannot tell whether an answer is any
good.

## "Did that change help?"

`agent_eval` runs every case once and reports an aggregate. That **cannot** answer whether a change
helped: its pass rate swings 0.167 to 0.583 on unchanged code, because one LLM judge flips one case
and one case is 8.3 points (PROBLEMS P53).

`evaluation.reliability` runs ONE case many times and reports the spread. Both arms land in the
ledger under `reliability`, labelled, so an A/B sits side by side permanently:

```bash
DOMAIN=vendor_risk uv run python -m evaluation.reliability --runs 10 --label before
# make the change
DOMAIN=vendor_risk uv run python -m evaluation.reliability --runs 10 --label after
```

Read `clean_rate` and `assessed_mean` together. A difference in one and not the other usually means
the change moved coverage rather than correctness. This is the tool that separated P53's noise from
P54's real drop, and nothing else would have.

## Two things worth knowing

**The gate exits non-zero.** That is the whole point of it, and it has been verified by
deliberately breaking things - see PROBLEMS.md P01, where a gate watching only recall@3 passed a
regression that halved recall@1.

**Every number is appended to a ledger**, never overwritten, and the charts are rendered from it.
A number on a slide should be traceable to the run that produced it.
