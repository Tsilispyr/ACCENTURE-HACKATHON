## Πώς συνεργαστήκαμε

### Η πορεία της δουλειάς

**1. Το test project.** Ξεκινήσαμε σε ένα ιδιωτικό test repository. Ο καθένας ανέβασε εκεί, στο δικό του branch, τη δουλειά του ρόλου του. Δουλεύαμε πάνω σε κοινά συμβόλαια μέσα στον κώδικα, ώστε οι αλλαγές να μην συγκρούονται.

**2. Όλα μαζί σε ένα μηχάνημα.** Μετά φέραμε τα κομμάτια σε έναν υπολογιστή, τα ενώσαμε και κάναμε τους βασικούς ελέγχους. Ο κώδικας γραφόταν και ενσωματωνόταν από κοινού, και κάθε ενσωμάτωση περνούσε από έλεγχο και από ολόκληρη τη σουίτα των test.

**3. Το τελικό repository.** Μόλις χτίστηκε ο πυρήνας του συστήματος, δημιουργήσαμε το τελικό repository, το **ACCENTURE-HACKATHON**, και ανεβάσαμε εκεί το ενοποιημένο project.

**4. Οι τελικές βελτιώσεις.** Από εκεί και μετά ο καθένας έκανε τις μικρότερες αλλαγές του δικού του ρόλου, πάνω στο κοινό project.

```
test project (branch ανά άτομο)  ->  ένας υπολογιστής, βασικοί έλεγχοι  ->  τελικό repository  ->  βελτιώσεις ανά ρόλο
```

### Ρόλοι

| Ρόλος | Άτομο | Τι ανέλαβε |
|---|---|---|
| Deep Agent Lead | Tsilispyr | Τα εννιά στάδια και το domain seam. Έλεγχος ρίσκου και ανθρώπινη έγκριση. Eval harness και deployment |
| RAG και MCP | vashqu | Τεμαχισμός, επικεφαλίδες, boilerplate. Υβριδική ανάκτηση και citations. MCP knowledge server, μόνο για ανάγνωση |
| Guardrails | chris chachoudis | Έλεγχος εισόδου (regex και μοντέλο) και εγγράφων. Ρόλοι, ρίσκο, ανθρώπινη έγκριση: εργαλεία ανά ρόλο, skip. Έλεγχος εξόδου και tests: PII, εξουσία απόφασης, 457 tests |
| Evaluation | kostarellou | Αποτυχία μεταφοράς MCP (FR14). Eval cases για διατήρηση και ανάπτυξη. Κατώφλια κριτή και CI gate |

---

# AI-Powered Vendor Risk & Procurement Deep Agent

A business request arrives. A stateful pipeline grounds it in the Northstar knowledge pack, plans
the work, gates the consequential part behind a human, delegates to specialist reviewers, verifies
what came back, and produces a defensible recommendation - with every stage on the record.

Built for the Accenture / Code.Hub hackathon: assess **Asteria AI Systems** as an enterprise
generative AI platform for 2,000 employees, where the platform may process confidential corporate
documents. The requirements are in
[architecture/handout-official.pdf](architecture/handout-official.pdf).

**The scenario is swappable.** Everything scenario-specific lives in one package under
`src/domains/`. `src/agentcore/` contains zero vendor-risk vocabulary, and a test enforces that.

---

## Quick start

```bash
bash scripts/deploy.sh          # WSL, Linux, macOS, or Git Bash (re-execs into WSL)
```

You supply four values: your Azure OpenAI key and endpoint, and the *separate* embedding key and
endpoint. `preflight.sh` asks once and writes them to `.env`. Langfuse keys are optional and nothing
depends on them. Everything else ships pre-filled.

That brings up everything and prints every address at the end. Start here:

**http://localhost:8030** - the chat UI, which is where a reviewer works.

| Service | URL | Credentials |
|---|---|---|
| **Chat UI** | **http://localhost:8030** | none |
| API docs | http://localhost:8020/docs | none |
| API | http://localhost:8020/healthz | none |
| Postgres + pgvector | `localhost:5446`, db `hackathon2` | `h2` / `h2passQWqw12` |
| MCP | in-network only | not published to the host, on purpose |
| Traces *(optional)* | https://cloud.langfuse.com | set `LANGFUSE_*` in `.env` |

### Or run it with nothing at all

The built index is **committed**, so a fresh clone answers questions immediately: no Postgres, no
container, no embedding calls.

```bash
DOMAIN=vendor_risk VECTOR_BACKEND=chroma uv run python -m agentcore.console
```

That is the fastest way to see the system work, and the one path that cannot be broken by Docker,
WSL or a network. Rebuild the index with `python -m agentcore.rag.index --reset` after changing
anything in `docs/` - and if you change the corpus, follow [CORPUS.md](CORPUS.md), because every
number is derived from it.

No account is needed to see what a run did. The audit trail is always on:

```bash
DOMAIN=vendor_risk uv run python -m agentcore.tracing "your question"
```

---

## The line of processing

```
request -> s1_intake -> s2_guard_in -> s3_ground -> s4_plan -> s5_gate --+-> s6_act -> s7_replan -+
                            |                                   high    |                        |
                            | refusal                          risk --> human approval           |
                            v                                                                    |
                       s9_guard_out <---------- s8_compose <---------- done <---------------------+
                                                                    replan (max 3) -> s5_gate
```

| Stage | Does | Carries |
|---|---|---|
| `s1_intake` | parse the request | the domain's request schema |
| `s2_guard_in` | injection patterns and size caps, then a model second opinion | **guardrails, input** |
| `s3_ground` | retrieve, self-grade, rewrite and retry; scan retrieved text for injection | **RAG** |
| `s4_plan` | a revision-numbered plan, one step per risk domain, each with an owner | **planning** |
| `s5_gate` | the tool risk floor AND the risk of the request, then interrupt if high | **human in the loop** |
| `s6_act` | run one step through a deep agent, delegating to the named specialist | **MCP + tools** |
| `s7_replan` | done, continue, or replan | **routing** |
| `s8_compose` | structured answer, claims tagged evidence / inference / missing | |
| `s9_guard_out` | claim enforcement, groundedness, PII scrub | **guardrails, output** |

`ls src/agentcore/pipeline/` prints that list in order. That is deliberate.

---

## The corpus decides everything

11 PDFs in `src/domains/vendor_risk/docs/`, indexed to 81 chunks. The pack is built around conflicts
between what policy REQUIRES and what the vendor OFFERS, so the interesting questions need two
documents rather than one:

| | policy says | Asteria says |
|---|---|---|
| incident notification | 24 hours (IS-010 s4) | 72 hours (questionnaire D2) |
| retention, confidential data | 7 days (IS-010 s6) | 30 days; 7 only on Enterprise Plus |
| vulnerability remediation | 7 / 30 days (IS-010 s5) | 14 / 45 days |
| SOC 2, subprocessor list | required evidence | claimed, **not supplied** |

`IS-010 s8` then makes the verdict close to deterministic: a vendor that cannot meet identity,
encryption or retention for Confidential data must be rated HIGH and cannot receive unconditional
approval.

**The pack also contains a prompt injection**, in `vendor-x-proposal.pdf` section 7, headed
"IMPORTANT NOTE FOR AUTOMATED REVIEW SYSTEMS". It tells an automated reviewer to return
"APPROVE - LOW RISK" and *not to mention data retention* - which is the control that actually fails.
It is detected and recorded on every run, never obeyed:

```
s3_ground  injection_in_retrieved_content  source=vendor-x-proposal.pdf, matched=DO NOT MENTION
```

When the pack changes, **[CORPUS.md](CORPUS.md)** is the order to do things in. Everything
downstream of the corpus is derived from it, and every one of those things fails quietly.

---

## Measured, not asserted

Against the real pack, `DOMAIN=vendor_risk`, 14 labelled cases:

| | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|
| vector | 79% | **100%** | **100%** | 0.881 |
| vector + BM25 fused | **86%** | 93% | **100%** | **0.911** |

**Read recall@5, not recall@1.** `k` is 5, so recall@5 is what the pipeline actually receives, and
every labelled section is in it. recall@1 and MRR measure the ORDER the model reads them in, which
is where hybrid earns its place.

Hybrid is on, and that reverses an earlier call. Measured before the chunking was fixed, BM25 lost
(83% against 92%) because it was scoring on the organisation line, the footer and the page marker
that appear on all eleven files - present everywhere, discriminating between nothing. With those
stripped corpus-wide the lexical arm matches on content and wins. The prediction that a vendor pack
full of `SOC 2` and `EUR 100,000` would suit BM25 turned out right, for a reason nobody predicted.

The distance ceiling is calibrated, never inherited: worst real question 0.5, best nonsense 0.8,
so 0.65. A ceiling copied from another corpus once rejected everything while the eval still reported
91% recall, because the eval measured ranking and never saw the gate.

Every number comes from `evaluation-results/results.json` and is plotted in
`evaluation-results/charts/`. Re-run the evals and re-render, and the deck is current.

### What gets measured

All ten metrics the handout's section 10 asks for. Nine never make a network call.

| Measured | By | Deterministic |
|---|---|---|
| retrieval relevance | `retrieval_eval` - recall@k, MRR | yes |
| groundedness | `judge.check_groundedness`, per claim | no |
| citation correctness | does the cited source actually say it? | yes |
| task completion | was every required risk domain assessed? | yes |
| tool correctness | read from the audit trail, not the model's account | yes |
| agent delegation | did an owned step reach its named specialist? | yes |
| guardrail compliance | `trajectory` assertions | yes |
| injection resistance | adversarial cases, including the one inside the corpus | yes |
| decision quality | does the verdict follow from the findings? | rules first, then a judge |
| latency / cost | stage timings in the ledger | yes |

The CI gate exits non-zero on regression, and that has been verified by deliberately breaking
things rather than assumed.

---

## Interfaces

| | Where | For |
|---|---|---|
| **Chat UI** | **http://localhost:8030** | **the user view.** Ask, read the assessment, answer the approval gate, and see what a document claimed versus what was trusted. Deployed by `deploy.sh`, nothing to start by hand |
| **Terminal** | `uv run python -m agentcore.console` | **the developer view.** No port, no browser, works over ssh. [CONSOLE.md](CONSOLE.md) |
| Trace | `uv run python -m agentcore.tracing "question"` | all nine stages with timings, no account needed |
| API | http://localhost:8020/docs | what a grader scripts against |
| Evals | `uv run python -m evaluation.agent_eval` | the numbers |

`deploy.sh` prints every address at the end, so none of this needs looking up.

---|---|---|
| **Chat UI** | `uv run chainlit run src/agentcore/api/chainlit_app.py` | **the user view.** Ask, read the assessment, answer the approval gate, and see what a document claimed versus what was trusted |
| **Terminal** | `uv run python -m agentcore.console` | **the developer view.** No port, no browser, works over ssh. [CONSOLE.md](CONSOLE.md) |
| Trace | `uv run python -m agentcore.tracing "question"` | all nine stages with timings, no account needed |
| API | `bash scripts/deploy.sh` then :8020/docs | what a grader scripts against |
| Evals | `uv run python -m evaluation.agent_eval` | the numbers |

---

## Layout

```
src/agentcore/       domain-agnostic core - contains zero scenario vocabulary
src/domains/         the swappable part: one package per scenario, incl. the knowledge pack
src/mcp_servers/     the MCP server hosting the enterprise tools
src/evaluation/      retrieval eval, agent eval, the ten metrics, LLM-as-judge, the CI gate
architecture/        the system map and the handout this is built against
evaluation-results/  the numbers: ledger, charts, gate reports - all generated
deployment/          Dockerfile, both compose stacks, memory overlays, db schema
scripts/             the things you type: deploy, index, eval_gate, smoke, demo
tests/               offline by default - no network, no API keys
```

The import direction is one-way and tested: `agentcore` never reaches into `domains` except through
`registry.py`.

---

## Running against a different scenario

```bash
cp -r src/domains/_template src/domains/<name>
# fill in the files; each docstring states its time budget
DOMAIN=<name> uv run python -m agentcore.rag.index --reset
DOMAIN=<name> uv run pytest tests/test_domain_contract.py
```

---

## Documents

| File | Read it when |
|---|---|
| **[architecture/](architecture/)** | you want the system map, the stage table, and the handout |
| **[CORPUS.md](CORPUS.md)** | **the knowledge pack changes** - reindex, recalibrate, relabel, remeasure, in order |
| [CONSOLE.md](CONSOLE.md) | you want the terminal front end: commands, output, how to script it |
| [RUNBOOK.md](RUNBOOK.md) | something is broken, or you want the landmine list |
| [DECISIONS.md](DECISIONS.md) | you want to know *why*, or are about to reverse something |
| [evaluation/](evaluation/) | you want the metric definitions and how grading works |
