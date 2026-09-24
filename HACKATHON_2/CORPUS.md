# CORPUS

What to do when the real knowledge pack arrives. Follow it in order.

Everything downstream of the corpus is **derived** from it: the distance ceiling, the eval labels,
the retrieval numbers, the charts, the reliability baseline. A new corpus invalidates all of them
at once, and every one of them fails quietly rather than loudly. This file exists so that is a
checklist rather than a memory.

Budget about **40 minutes**, most of it waiting.

---

## What we have now, and what changes

| | stand in (now) | the real pack |
|---|---|---|
| files | 2 markdown | **11 PDFs**, 5 policies + 3 `vendor-x-*` + 3 under `historical-vendor-assessments/` |
| chunks | 12 | unknown, expect a few hundred |
| eval labels | `Section 3` to `Section 11`, positional | **will all be wrong** |
| distance ceiling | 0.70, calibrated on this corpus | **will be wrong** |
| every retrieval number | provisional | the real ones |

**Nothing about the stand in numbers survives.** Treat them as proof the pipeline works, never as
results.

---

## The procedure

### 1. Drop the files in

```bash
knowledge-base/knowledge/
```

That is where `vendor_risk/corpus.py` reads from (the old `src/domains/vendor_risk/docs/` stand in
is no longer read). A hidden vendor's files go in the same folder. Subfolders are handled: `corpus.py` uses `rglob`, so `historical-vendor-assessments/` is picked up
without anyone editing a path. Dotfiles are skipped, which is what stops a macOS `.DS_Store` being
indexed as a document.

### 2. Reindex, with `--reset`

```bash
DOMAIN=vendor_risk VECTOR_BACKEND=chroma uv run python -m agentcore.rag.index --reset
```

**`--reset` is not optional.** Chunk metadata is baked in at index time: the per file `source`, the
section numbers, the page numbers. Indexing on top of an existing collection leaves the old rows
there and retrieval silently mixes two corpora.

Read the output. It prints one line per file and a total:

```
  procurement-policy.pdf: 14 pages -> 38,201 chars -> 22 sections
  ...
  11 file(s) -> 81 chunks
```

**If a file shows far fewer sections than it has headings, stop.** That is PROBLEMS P03: boilerplate
stripping once deleted 99 of 106 headings and the corpus collapsed from 272 sections to 7. There is
a `RuntimeError` guard now, but it catches the severe case, not a subtle one.

### 3. Print the REAL section index

```bash
DOMAIN=vendor_risk uv run python -m evaluation.sections
```

**Do not skip this and do not guess labels.** Section numbers are positional and files load in path
order, so "Section 9" means the ninth section across the whole pack, which is almost never the
ninth section of the document you are thinking of. Three of five labels were wrong the first time
for exactly this reason (PROBLEMS P05).

### 4. Rewrite the eval labels

In `src/domains/vendor_risk/evalset.py`, set every `expected_labels` from step 3's output. Labels
are compared **whole**, never as substrings, because `Article 3` is a prefix of `Article 33`.

While you are there: the flagship case still invents per seat pricing. The handout's request is
*"an enterprise GenAI platform for 2,000 employees"* that *"may process confidential corporate
documents"*. Align it.

### 5. Recalibrate the distance ceiling

```bash
DOMAIN=vendor_risk VECTOR_BACKEND=chroma uv run python -m evaluation.calibrate
```

Put the suggested value in `POLICY.max_distance` in `src/domains/vendor_risk/corpus.py`.

**This is the step that has already cost the most.** The ceiling belongs to a corpus, an embedding
model AND a distance metric, and is portable across none of them. A ceiling inherited from another
corpus once rejected **everything**: `sample_policy` reported 91% recall@1 while delivering 32% to
the pipeline, and `vendor_risk` delivered nothing at all while BM25 quietly carried every query
(PROBLEMS P52).

A wider gap between worst real question and best nonsense is a healthier corpus. If the gap is
narrow, the chunking is probably wrong before the ceiling is.

### 6. Re-run retrieval, and read the gated row

```bash
DOMAIN=vendor_risk VECTOR_BACKEND=chroma uv run python -m evaluation.retrieval_eval
```

```
                       cases  recall@1    recall@3    recall@5    MRR
vector                    22  91%         95%         95%         0.932
through the gate          22  91%         95%         95%         0.932
```

**`through the gate` is the row that matters.** The rows above it measure ranking and never touch
the distance ceiling; that row is what `s3_ground` actually receives. A large gap between them
means the ceiling is wrong, not the ranking. If it reports questions returning NOTHING, go back to
step 5.

### 7. Re-measure hybrid, do not assume it

BM25 is currently ON for `vendor_risk` and OFF for `sample_policy`, both on measurement. The real
pack has identifiers an embedding flattens: SOC 2, ISO 27001, clause numbers, euro thresholds,
vendor names. That is the shape BM25 exists for, so it may finally earn its place, or may not.

```bash
DOMAIN=vendor_risk VECTOR_BACKEND=chroma uv run python -m evaluation.retrieval_eval --force-hybrid
```

Set `POLICY.hybrid` from what that prints. It was measured **worse** on GDPR prose (0.82 against
0.91) and that is a finding, not a failure.

### 8. Re-baseline reliability

```bash
DOMAIN=vendor_risk VECTOR_BACKEND=chroma \
  uv run python -m evaluation.reliability --runs 10 --label real-pack
```

The existing baselines are against a 12 chunk stand in and mean nothing afterwards. This is also
the moment to settle PROBLEMS P54, since the chunking question is much more interesting on eleven
files than on two.

### 9. Run the whole eval and the gate

```bash
DOMAIN=vendor_risk VECTOR_BACKEND=chroma uv run python -m evaluation.agent_eval
bash scripts/eval_gate.sh
```

Expect the gate's `agent_pass_rate` threshold of 0.70 to fail: no run has ever reached it
(PROBLEMS P53). Decide then whether to lower it or gate on the metric means, which are stable.

### 10. Re-render the charts and update the numbers

```bash
uv run python -m evaluation.charts
```

Then update the figures quoted in [HANDOVER.md](HANDOVER.md), [STATUS.md](STATUS.md) and
[TEAM_PLAN.md](TEAM_PLAN.md). Every number in those files is currently from the stand in corpus and
will be wrong.

---

## The traps, in one place

| Trap | What it looks like | Guard |
|---|---|---|
| Indexing without `--reset` | answers mixing two corpora, no error | nothing catches this. Always `--reset` |
| Guessing eval labels | recall collapses, retrieval looks broken | `evaluation.sections` (P05) |
| Inheriting the distance ceiling | perfect recall@1, pipeline receives nothing | the `through the gate` row (P52) |
| Boilerplate eating headings | section count far below heading count | `RuntimeError` on a severe drop (P03) |
| Reading one agent eval run | a "regression" that is noise | `evaluation.reliability` (P53) |
| Switching vector backend | ceiling wrong by a factor of two | Chroma is pinned to cosine, same as pgvector (P52) |

---

## If the pack is large

The stand in is 12 chunks; the real one may be several hundred. Two things change:

**Indexing costs embedding calls.** 459 chunks took a few minutes on the GDPR corpus. Reindex
deliberately, not casually, and never in the middle of an experiment.

**`k` may need raising.** `POLICY.k` is 5, chosen for a 12 chunk corpus. With eleven documents, a
question that spans several of them may need more. Raise it and re-measure rather than guessing,
and watch the latency: `s3_ground` is currently 4 to 14 seconds and grows with `k`.
