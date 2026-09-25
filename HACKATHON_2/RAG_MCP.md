# RAG & MCP: what changed, and how it works

**For:** the Deep Agent Lead, the Guardrails Engineer and the Evaluation Engineer.
**From:** the RAG & MCP Engineer. **Branch:** `mcp-rag`. **Date:** 2026-09-24.

**Status:** MERGED. Written against branch `69d9d9c`; the numbers and the corpus path below were
updated on merge, where they are marked. **457 tests pass offline**, and the pipeline has since
been run against the live LLM and embedding model.

---

## TL;DR

- The **real knowledge pack is indexed**: `src/domains/vendor_risk/docs/`, 11 PDFs → **81 chunks**,
  one per policy clause. Each chunk cites **its own file, section and page**.
  *(Merged to `docs/` rather than a root folder: the root copy was removed when the pack was
  deduplicated, because two copies of a corpus drift and only one gets indexed. One copy, beside
  the domain that reads it.)*
- Retrieval on the real pack, **re-measured after merge on 14 labelled cases**: vector
  **79% / 100% / 100%** at recall@1/3/5, MRR **0.881**; hybrid **86% / 93% / 100%**, MRR **0.911**.
  Hybrid is ON, and this branch was right about that. The ceiling is **0.64**. `k` is 5, so
  recall@5 is what the pipeline receives and it is 100% either way; the arms differ only in order.
  Off-topic questions return **nothing**.
- The MCP mock enterprise data **no longer contradicts the pack**. The invented Asteria incident
  and prior approval are gone, and the approval threshold is **€100k** as the procurement policy
  (PR-001 §2.3) says.
- New **read-only MCP knowledge server**:
  - `search_policy`, `retrieve_document` and `corpus://documents` resources;
  - **stdio only, enforced**;
  - **not wired into the agent**, so the pipeline behaves as before.
- **No contract you depend on changed.** See [Contracts that did not change](#contracts-that-did-not-change).
- **Three things need you:** proposals P1 (Lead), P2 (Eval) and P3 (Guardrails), [below](#what-i-need-from-each-of-you).

---

## 1. Code changes, file by file

| File | What changed | Does it affect you? |
|---|---|---|
| `src/agentcore/rag/chunking.py` | Chunking runs **per file** (each chunk's `source` is its file name). Heading paths lose markdown bold. Boilerplate (footers) is stripped **corpus-wide**, then per file. | Citations now read `information-security-policy.pdf \| Information Security Policy > 4. Logging and incident response \| page 1`. GDPR and sample_ops chunks are **byte-identical** to before. |
| `src/agentcore/rag/index.py` | The ledger's `pages`/`sections` counts are keyed per `(source, …)`. | Eval: the chunking row now says 11 pages, not 1. |
| `src/domains/vendor_risk/corpus.py` | `DOCS` stays `src/domains/vendor_risk/docs/` (recursive; skips dotfiles) and now holds the real pack. `max_distance` **0.64** on merge. `hybrid=True`, `k=5` (both measured). | One copy of the corpus, beside the domain that reads it. |
| `src/domains/vendor_risk/systems.py` | **Data only** (function names, signatures, output format unchanged). Asteria: first engagement, ISO 27001 / SOC 2 "claimed, report not supplied", no invented incidents or prior approval. Added Vendor Alpha/Beta/Gamma prior assessments mirroring the historical PDFs. `ai-platform` threshold €250k → **€100k**. `get_prior_assessments` prints a record's `reason`. | Agent outputs about Asteria's history change: they now match the documents. `FAIL_NEXT_HISTORY` (the MCP failure test hook) is untouched. |
| `src/mcp_servers/knowledge_server.py` | **New.** Read-only FastMCP server: `search_policy`, `retrieve_document`, `corpus://documents` resources. Stdio only, loopback host. | No domain declares it in `mcp_servers()`, so **the agent doesn't load it**. |
| `src/mcp_servers/__main__.py` | Registers `--server knowledge`. Refuses a network transport for it before building anything. | The **systems server is unchanged**, including its `streamable-http` container mode. |
| `CORPUS.md` | 11 PDFs (not 12), the real example output, and step 1 names `src/domains/vendor_risk/docs/`. | Checklist now matches reality. |
| `evaluation-results/results.{csv,json}` | One chunking row from the official `index --reset` (81 chunks, 11 pages). | Eval: a genuine real-pack measurement. My trial retrieval numbers were **not** written here. |
| `tests/test_chunking.py` | +2 tests: bold headings give clean paths; a footer on one-page files is stripped. | – |
| `tests/test_knowledge_server.py` | **New**, 13 tests. Cover: tools and resources, untrusted banner on tools and resources, deleted-file handling, hybrid retriever used, network transport refused (API and CLI), systems server unaffected. | – |

## 2. Knowledge files I created (outside the repo, in `mcp_rag_cheatsheet/`)

| File | What it is |
|---|---|
| `DECISIONS_2.md` | The change log for my track: R1–R12 (what and why for every change), the backlog B1–B15, proposals P1–P3, and a run-sheet. |
| `Why.md` | The rationale for a non-technical reader: the business scenario, the cross-document facts retrieval must connect, each design choice, and 12 prepared questions and answers. |
| `Notes.pdf` | Presentation notes: the problem, architecture diagrams, what was built, alternatives, the best-practice mapping, a demo script, and manager Q&A. |
| `RAG_MCP.md` | This file. |
| `MCP_RAG_Best_Practices.md` | The course cheatsheet every choice was checked against (not mine; the reference). |

---

## 3. How it works

### 3.1 Indexing (run once per corpus change)

```
src/domains/vendor_risk/docs/**.pdf
  -> pymupdf4llm: PDF to markdown, cached in .cache/ by file hash
  -> strip_boilerplate: corpus-wide pass (footers repeated across files), then per file
  -> sections by markdown heading, with clean paths ("Policy > 6. Data retention")
  -> RecursiveCharacterTextSplitter inside each section (900 / 120; structure is what binds here)
  -> metadata per chunk: source (file name), page, section, kind, number, index
  -> Azure text-embedding-3-small -> pgvector (default) or Chroma (VECTOR_BACKEND=chroma), cosine
```

Section **numbers are positional across the corpus** (Section 1 … Section 81, in path order).
That's what eval labels and `get_by_locator("section", N)` use. Run `evaluation.sections` after
any change to the pack; new files named `vendor-y-*` sort last and won't shift existing labels.

### 3.2 Retrieval (the same code for the pipeline and for MCP)

```
question -> vector arm (cosine, top-k) --+
         -> BM25 arm (same metadata filter) --+--> RRF (k=60) -> distance gate -> Evidence(trusted=False)
```

- **Distance gate** (`vendor_risk/corpus.py`): absolute ceiling **0.64** plus a relative margin
  of **0.15** from the best hit. It was calibrated on the real pack: the worst real question
  scores 0.500 and the best nonsense 0.803.
- **Nothing passes the gate:** the caller gets "No sufficiently relevant passage found. Say so
  rather than guessing."
- **Everything returned is untrusted:** it's wrapped in the UNTRUSTED CONTENT banner from
  `safety/untrusted.py`.
- **Consumers:**
  - `s3_ground` (grade, rewrite, at most 2 retries);
  - the executor's in-process tools `search_corpus` / `get_by_locator`;
  - MCP `search_policy`.

### 3.3 MCP servers

| | Systems server (`--server systems`) | Knowledge server (`--server knowledge`) |
|---|---|---|
| Purpose | Enterprise systems: what the agent **changes** or looks up in a system of record | What the corpus **says**, for any MCP client |
| Tools | `get_vendor_history`, `get_prior_assessments`, `get_budget`, `calculate_tco`, `record_assessment`, `raise_exception` | `search_policy(query)`, `retrieve_document(name)` |
| Resources / prompts | prompt `house_style` | `corpus://documents` (list), `corpus://documents/{name}` (full text, banner-wrapped) |
| Transport | stdio (dev/tests), `streamable-http` in compose (internal network, no published port). **Unchanged.** | **stdio only.** HTTP/SSE raise `ValueError`; the host is `127.0.0.1` |
| Used by the agent? | Yes, per-step allowlist; writes gated by the risk floor and human approval | No. It's for other MCP clients and the demo (D9 holds) |
| Failure behaviour | `FAIL_NEXT_HISTORY` drives the MCP failure test | Dead index → "knowledge index is unavailable"; deleted file → "can no longer be read" |

---

## 4. How to run it

```bash
cd <project root>
uv sync && uv run pytest -q                    # offline, no keys: 378 passed
bash scripts/preflight.sh                      # once: writes .env (Azure embedding key needed)

# Chroma needs no Docker. Drop VECTOR_BACKEND=chroma to use pgvector after `bash scripts/deploy.sh`.
DOMAIN=vendor_risk VECTOR_BACKEND=chroma uv run python -m agentcore.rag.index --reset
#   expect: one line per PDF, then "11 file(s) -> 81 chunks"
DOMAIN=vendor_risk VECTOR_BACKEND=chroma uv run python -m evaluation.sections       # real labels
DOMAIN=vendor_risk VECTOR_BACKEND=chroma uv run python -m evaluation.retrieval_eval

uv run python -m mcp_servers --server systems --domain vendor_risk                  # 6 tools + prompt
VECTOR_BACKEND=chroma uv run python -m mcp_servers --server knowledge --domain vendor_risk
#   VECTOR_BACKEND must match the index command, or search_policy looks in the wrong store
```

**Hidden vendor case:** put the new vendor's PDFs in `src/domains/vendor_risk/docs/`, run the index
command with `--reset`, then `evaluation.sections` and `evaluation.calibrate`. No code change.

---

## 5. Contracts that did not change

- `Evidence`, `RetrievalPolicy`, `Corpus` and the `Domain` seam (`agentcore/contracts.py`,
  `agentcore/domain.py`).
- In-process tool names and behaviour: `search_corpus`, `get_by_locator` (and the graph tool,
  still off for vendor_risk).
- Systems server tool **names and signatures**, its `streamable-http` container mode, and
  `mcp_client.py`.
- The pipeline stages (`s1`–`s9`), the per-step tool allowlist, the risk floor and the approval
  gate. My track touched none of them.
- `vendor_risk.mcp_servers()` still declares only `systems`, so the agent loads exactly the same
  MCP tools as before.

---

## 6. What I need from each of you

**Deep Agent Lead: P1, tool-retrieved evidence** (`pipeline/s6_act.py`). Passages the executor finds
through its own `search_corpus` calls never reach `state["evidence"]`, so `s9` flags correct
citations as `citation_not_in_evidence`. My proposal: the two retrieval tools append their
`Evidence` to a per-request `ContextVar`, and `s6_act` returns it as `"evidence": [...]`. That
field already uses an `operator.add` reducer, so it appends. It's about 10 lines and lifts
`citation_correctness`. I can pair on it.

**Evaluation Engineer: P2, retrieval cases** (`domains/vendor_risk/evalset.py`).
- The 5 current retrieval cases test **stand-in** rules (a "tier 1 supplier certification", a
  "€250,000 TCO", a "72-hour AI incident") that don't exist in the real pack, so their labels
  and rubrics are wrong.
- Replace them with these 16. Each label comes from `evaluation.sections` and was measured
  (recall@3 100%):

```python
("How quickly must a critical vendor tell us about a security incident?", ["Section 31"]),
("Can a generative AI provider keep our confidential prompts, and for how long?", ["Section 33", "Section 16"]),
("Who has to sign off a technology purchase worth more than 100,000 euros?", ["Section 40"]),
("How many competing quotes do we need for a large purchase?", ["Section 43"]),
("How should we treat evidence the vendor did not provide?", ["Section 52", "Section 45"]),
("Can the AI system give final approval to a high risk vendor by itself?", ["Section 9", "Section 53", "Section 42"]),
("How fast must critical vulnerabilities be patched?", ["Section 32"]),
("Is multi-factor authentication required for admin accounts?", ["Section 29"]),
("What kind of data must never be sent to an external generative AI service?", ["Section 16", "Section 15"]),
("How long does Asteria take to notify customers of an incident?", ["Section 77"]),
("What is Asteria's default retention period for prompts and outputs?", ["Section 78", "Section 67"]),
("How much does Asteria cost per year for 2,000 users?", ["Section 56", "Section 60"]),
("Has Asteria supplied its SOC 2 and ISO 27001 reports?", ["Section 80"]),
("Does Asteria train its models on our documents?", ["Section 66", "Section 78"]),
("Has Asteria given us its list of subprocessors?", ["Section 79"]),
("Why was a previous AI assistant vendor rejected?", ["Section 22", "Section 23"]),
```

- Also: `evalset.py:73` calls it "Assess Asteria AI Systems **for renewal**". The pack treats
  Asteria as a **first-time proposal**. Suggest the handout's request instead: 2,000 employees,
  Confidential documents, APPROVE / CONDITIONAL APPROVAL / REJECT.
- Once adopted, run `retrieval_eval` and `calibrate` so the numbers land in the ledger.

**Guardrails Engineer: P3, role check at the MCP boundary** (handout §9, optional).
`record_assessment` and `raise_exception` are gated today by the risk floor (`high`) and human
approval before the agent can call them. If you want defence in depth at the server too, the
smallest version is a required `actor_role` argument checked against an allowlist before the
write. Say the word and I'll implement the server side against the contract you choose.

---

## 7. Known limits (so nobody is surprised in the demo)

- **Vendor-framed questions retrieve the vendor's side first.** "Does Asteria meet our 24-hour
  requirement?" returns questionnaire D and the Alpha precedent before the NFS clause. The
  pipeline's rewrite loop and multi-step plan cover it. A query containing `IS-010` already
  returns both. Query expansion is backlog B15, to measure before adding.
- **The retrieval numbers come from Chroma.** Both stores use cosine and measured identically
  before. Re-run once on pgvector during rehearsal.
- **The Chroma index is local and gitignored.** Everyone rebuilds it with the index command above
  (a few seconds; 81 chunks to embed, sent in one batch).
- ~~The old stand-in `src/domains/vendor_risk/docs/` is unused.~~ **Reversed on merge:** `docs/`
  holds the real pack and is the only copy. The stand-in markdown files were deleted; delete nothing
  if nobody objects.


