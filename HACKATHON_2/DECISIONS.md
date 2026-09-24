# DECISIONS

Why the code is the way it is. One entry per non-obvious choice: **the decision, the alternatives,
why this one, what it costs.** Each entry names the file it governs.

New decisions get appended. When one is reversed, the entry stays and gains a **Superseded** note -
a decision record that only shows the current answer cannot stop anyone re-litigating a settled
question.

---

## D1 · LangSmith only. No Langfuse, ClickHouse, Redis, MinIO or Grafana
`deployment/docker-compose-infra.yaml`

**Alternatives:** keep the proven 7-service Langfuse stack from hackathon 1; or run both.

**Why:** measured, not assumed. WSL 2 on this machine has **3,663 MB total** (no `.wslconfig`, so it
took ~50% of a 7.4 GB host), and the hackathon 1 stack alone consumes **~2.4 GB** of it - ClickHouse
796 MiB, langfuse-web 679 MiB, langfuse-worker 416 MiB, Grafana 246 MiB. Adding Neo4j (~768 MB) and
an app to that does not fit. LangSmith is SaaS: an API key, zero containers, zero RAM.

**Cost:** needs outbound internet and an account, so it dies in a venue that blocks egress. Also
loses the Grafana-dashboard bonus points hackathon 1 earned. Accepted because a stack that does not
start scores zero on every line, not just observability.

**Consequence:** two whole classes of landmine disappear with it - the Node/V8 heap trap
(`mem_limit` alone makes the process die with `OOMKilled=false, ExitCode=0`), and Langfuse's
`events_only` mode where `/api/public/traces` returns 404.

---

## D2 · `mcp` pinned to `>=1.24,<2`
`pyproject.toml`

**Alternatives:** keep `mcp>=2.2.0` as the course project has, and hand-write the LangChain↔MCP bridge.

**Why:** forced. `langchain-mcp-adapters` 0.3.2 declares `mcp<2.0.0,>=1.24.0`, so the adapter and
mcp 2.x cannot coexist. Separately, mcp 2.x **renamed `FastMCP` to `MCPServer`** and moved it to
`mcp.server.mcpserver`, which invalidates all four MCP reference implementations in the course
material (units 56-59). Pinning `<2` keeps the adapter *and* makes those units run verbatim.

**Verified after pinning:** `mcp` 1.30.0, `from mcp.server.fastmcp import FastMCP` imports,
`MultiServerMCPClient` imports, transports are `stdio | sse | streamable-http`.

**Cost:** pinned to a major version that will eventually be legacy. Irrelevant on a one-day horizon.

**Note:** this corrects a belief held earlier in planning. `src/day11/.../server.py` in the course
repo uses `from mcp.server import MCPServer`, which is *closer* to the mcp 2.x API than the class
examples are - but on the pinned version it is still wrong. Use unit 56.

---

## D3 · Four top-level packages under `src/`, not one named after the project
`pyproject.toml` → `[tool.uv.build-backend]`

**Why:** `agentcore` / `domains` / `evaluation` / `mcp_servers` makes the architecture legible from
`ls src/`, and lets a test assert that `agentcore` never imports `domains` except through
`registry.py`.

**The trap this hit:** `uv_build` expects `src/<project-name>/__init__.py` and fails the build with
`Expected a Python module at: src\hackathon2\__init__.py` - exactly the failure documented in the
course repo's `DEPENDENCIES.md`. Fixed with `module-root = "src"` and `module-name` as a **list**.

**Cost:** slightly unusual config that a reader may not recognise. The comment in `pyproject.toml`
explains it in place.

---

## D4 · LangGraph `StateGraph` is the spine; `create_deep_agent` sits at exactly one node
`src/agentcore/pipeline/graph.py`, `src/agentcore/executor/deep_agent.py`

**Alternatives:** a deep agent as the whole system; or a bare LangGraph with no deep agent.

**Why:**
1. Hackathon 1's rubric gave **20/100 to graph and state architecture** and 10 to routing and
   replanning. Nine named nodes show a judge what one `create_deep_agent(...)` line cannot.
2. **Testability.** The graph can run end-to-end against a `ScriptedLLM` with no network. An
   assertion like "retrieval ran before planning" is impossible to make about an agent that owns
   its own control flow.
3. A deep agent still earns its place at `s6_act`: `todos`, `files`, `subagents`, skills with
   progressive disclosure, and `interrupt_on` are all free there, and all demo-able.

**The boundary, precisely:** `s6_act` receives **one** `PlanStep` and an allowlist of the tools that
step declared. The deep agent never sees the plan, the other steps, or unapproved tools.

**Cost:** two control-flow idioms in one codebase. Mitigated by confining the deep agent to a single
node.

---

## D5 · Agentic RAG over pgvector; Neo4j is an optional second arm
`src/agentcore/rag/`

**Alternatives:** naive cosine similarity; or graph RAG on Neo4j as the centrepiece.

**Why not naive:** the organisers name three options; picking the cheapest reads as picking the
cheapest.

**Why not graph-first:** `GraphCypherQAChain.from_llm(..., allow_dangerous_requests=True)` generates
Cypher against a schema built that morning from an ontology learned at 10:30. That is the
highest-variance component available, and it fails *in front of judges* rather than in tests.

**Why agentic:** it is **additive over code that already works and has measured numbers**.
`search_GDPR.py` is already ~80% of it - two tools plus a calibrated distance gate. Agentic RAG adds
three things in ~80 lines: route (tool choice), self-grade (`Sufficiency(enough, missing)`), and
rewrite-and-retry capped at 2.

**The graph arm stays, safely:** `domain.graph_queries()` returns **named, parameterised Cypher
written by a human** - never generated. A domain that returns `{}` disables the arm entirely and the
router cannot offer it.

**Cost:** 1-3 extra LLM calls per request, and a second store to keep in sync at index time.

---

## D6 · Domains are Python packages, not YAML
`src/domains/`

**Alternatives:** YAML or JSON domain descriptors.

**Why:** pydantic validation, IDE completion, and real `@tool` functions. A YAML syntax error at
16:30 on one shared machine, discovered through a stack trace nobody can read, is worse than five
extra minutes of typing.

**Cost:** slower to fill in, and needs someone who can write Python. Mitigated by `_template/`
carrying a time budget in every file's docstring, and two reference domains to copy from.

---

## D7 · The risk floor the model cannot lower
`src/agentcore/safety/risk.py`

**The rule:** `effective_risk(action, ...) = max(floor_from_table, model_assessment)`. The model can
raise a risk level. It can never lower one. If the risk call raises, `assessed=None` and the floor
stands.

**Why:** in any real deployment the request text is attacker-controllable, and retrieved documents
are attacker-controllable too. **A gate the model can argue with is decorative.** Carried directly
from hackathon 1, where it was the standout piece of the safety design.

**Cost:** occasionally over-cautious - a genuinely low-risk instance of a high-risk action still
needs approval. Correct trade for a demo of controlled autonomy.

---

## D8 · Approval is bound to a plan revision, not to a request
`src/agentcore/pipeline/{s5_gate,s7_replan}.py`

**The rule:** replanning sets `revision += 1` and `approved_plan_revision = None`. Both the router
and the executor re-check `approved_plan_revision == plan.revision`.

**Why:** without it, approving one plan silently authorises whatever the agent replans afterwards -
which is the most plausible way an HITL gate becomes theatre.

**Cost:** a human may be asked to approve twice in one request. That is the correct behaviour.

---

## D9 · MCP hosts the mutating tools; retrieval tools stay in-process
`src/mcp_servers/systems_server.py`, `src/agentcore/tools/`

**Why:** MCP is a process boundary, so put it where a real deployment would: between the agent and
the systems it **changes**. Retrieval tools need live process-local handles (`PGVector`,
`Neo4jGraph`) and the actor scope from a `ContextVar`; serialising those over MCP buys nothing and
costs a hop.

**The payoff:** the risk gate and the approval step land exactly on the process boundary, which is a
clean thing to explain and a real thing to show.

**Hosting:** one server file, transport chosen by env var - `stdio` for tests and local dev (no
container, no port, no ordering), `streamable-http` as its own compose service when deployed.

**Cost:** ~128 MB for the extra container in the deployed profile.

---

## D10 · One Postgres serves both the vector store and the checkpoints
`deployment/docker-compose-infra.yaml`

**Why:** the `pgvector/pgvector:pg17` image is plain Postgres with the extension preinstalled. A
second Postgres for LangGraph checkpoints would cost ~250 MB and buy nothing at this scale. The
one-shot `app-db-init` container runs `CREATE EXTENSION IF NOT EXISTS vector` and exits.

**Cost:** vector search and checkpoint writes contend for the same instance. Irrelevant at one
concurrent request.

---

## D11 · `deploy.sh` refuses to run while the hackathon 1 stack is up
`scripts/deploy.sh` → `check_conflicting_stack()`

**Why:** the two stacks need ~4 GB combined and WSL has 3.6 GB. Without the check, the failure mode
is containers dying of OOM partway through a deploy - which looks like a broken compose file.
`STOP_HACKATHON1=1` stops them and says how to restart them.

**Cost:** one more thing that can refuse to run. It prints the exact command to fix it.

---

## D12 · Ports deliberately chosen to collide with nothing
`.env.example`, both compose files

Already in use on this machine: `3000 3001 5432 5433 5442 5444 5445 6379 7474 7687 8000 8010 8123
9000 9090 9091`.

This project therefore uses **5446** (Postgres), **7475/7688** (Neo4j), **8020** (API), **8100**
(MCP). Stateful services bind to `127.0.0.1` - nothing here should be reachable from the network.

---

## D13 · Measured facts worth citing to judges
`src/evaluation/`

From the course project's `RESULTS.md`, to be **re-measured on the day's own corpus**, not quoted:

- A metadata filter (articles only) took **recall@1 from 55% to 91%** and MRR from 0.710 to 0.932.
- Contextual retrieval - 459 LLM calls at index time - helped only the tail (recall@3 86→95%) and
  was *worse* combined with the metadata filter.
- **"A metadata filter beat 459 LLM calls. Structure first, then the model."**

The honest framing matters: 22 questions means one question is 4.5%, and baseline recall@3 was
observed at both 86% and 91% across runs. The articles-only result is 8 questions, well outside
that noise. Report the noise floor alongside the number.

---

## D14 · The distance gate must be recalibrated for every corpus
`src/agentcore/rag/vector.py`, `domain.retrieval_policy()`

`MAX_DISTANCE = 0.70` and `RELATIVE_MARGIN = 0.12` are calibrated **to the GDPR corpus**: real
questions land 0.43-0.60, nonsense 0.78-0.86, corpus median 0.716. **These numbers will be wrong for
a different corpus.**

Procedure, 20 minutes, scheduled - not optional: run three real and two nonsense questions with
retrieval only, read the distances, put the ceiling in the gap. Skipping it makes the system either
refuse everything or ground on noise, and it presents as a prompt bug.

---

## D15 · Verified library surface - do not re-litigate
Checked by `inspect` against the installed versions, 2026-09-22:

| Claim | Reality |
|---|---|
| `create_deep_agent(middleware=...)` | **Accepted.** Full params include `middleware`, `subagents`, `skills`, `memory`, `permissions`, `backend`, `interrupt_on`, `checkpointer`, `store` |
| `PIIMiddleware` | **Importable** from `langchain.agents.middleware` |
| `mcp.server.fastmcp.FastMCP` | Exists on `mcp` 1.30.0. **Does not exist** on 2.x - renamed `MCPServer` |
| MCP transports | `stdio`, `sse`, `streamable-http` |

Guardrails still live in graph nodes and the tool factory rather than relying solely on
`middleware=`, for the testability reason in D4 - but that is now a *choice*, not a workaround for
an unsupported parameter.

---

## D16 · The seam grew from 13 methods to 17
`src/agentcore/domain.py`

Four were added during implementation, each because a real scenario axis could not be expressed
without it:

| Method | Why the original 13 could not cover it |
|---|---|
| `section_finder()` | The chunker's *mechanism* is generic, but "what counts as a section" is document vocabulary. Hardcoding Articles and Recitals would put one scenario in the core. |
| `is_heading()` | Boilerplate removal must protect structure. If it disagrees with `section_finder()`, headings get deleted as repeated furniture and the document silently collapses. |
| `systems()` | MCP-served mutating operations are not `local_tools()` - they are plain callables the server registers, and they need a different lifecycle. |
| `store_factory()` | Without it the CI eval gate could not measure retrieval without Postgres and an embedding key, which made the gate hollow on a fork. |

**Cost:** four more methods to understand. Mitigated by `BaseDomain` defaulting every one, so a
minimal domain still overrides only `name`, `persona()`, `corpus()` and `eval_cases()`.

---

## D17 · The CI gate thresholds watch recall@1 and MRR, not just recall@3
`src/evaluation/gate.py`

**What happened:** a deliberate corpus regression was introduced to check the gate fires. It did
not. recall@1 halved from 100% to 50% while recall@3 stayed at 100%, and the gate passed.

**Why:** recall@3 is insensitive - a result sliding from rank 1 to rank 2 does not move it at all.
But rank 1 is what a user experiences.

**Now:** thresholds on recall@1 (0.70), recall@3 (0.80) and MRR (0.75). Re-verified by gutting the
corpus: the gate exits 1 naming all three breaches. A mild regression (one question of five)
correctly still passes - 80% is above the floor, and a gate that fires on noise gets disabled.

---

## D18 · `calibrate` uses every real question, not a sample
`src/evaluation/calibrate.py`

The ceiling is set by the WORST real question, so a sample that misses it sets the ceiling too
tight and the system starts refusing legitimate questions.

**Measured on the GDPR corpus:** 4 sampled questions suggested `max_distance = 0.58`. All 22
suggested `0.72`, because the hardest real question scores 0.658 - which 0.58 would have rejected.
The configured 0.70 sits correctly inside the real gap of 0.658 to 0.780.

---

## D19 · Locator vocabulary belongs to the domain
`src/agentcore/rag/vector.py`

`vector.py` originally hardcoded `page|chunk|section|article|recital|clause|step`. `article` and
`recital` are one document's vocabulary, so that was a genuine leak of scenario knowledge into the
core - **caught by `tests/test_no_domain_leakage.py`, not by review.**

Now only `page` and `chunk` are universal (every corpus has them); everything else comes from
`domain.retrieval_policy().locators`. A side effect worth having: "particle 33" no longer false-
matches as `article 33`, because the pattern is built with word boundaries from a known list.

---

## D20 · Guardrails live in graph nodes even though `middleware=` works
`src/agentcore/pipeline/s2_guard_in.py`, `s9_guard_out.py`

`create_deep_agent` **does** accept `middleware=` - verified, not assumed. Guardrails still live in
graph nodes, now as a choice rather than a workaround:

- A node can be tested offline with no LLM. Middleware inside an agent cannot.
- Input screening at `s2` runs before retrieval and planning, so a hostile request costs nothing.
  Middleware on the executor would screen it after both.
- The strongest tool-level guardrail is not middleware at all: it is the per-step allowlist in
  `tools/registry.py`. A tool that is not bound cannot be called, however persuasive the text.

**Cost:** the deep agent at `s6_act` runs without guardrail middleware. Acceptable because it only
ever receives one approved step and an allowlist, and a nested interrupt is treated as a tripwire.

---

## D21 · BM25 alongside embeddings - and measured OFF where it hurts
`src/agentcore/rag/lexical.py`, `hybrid.py`

**The question this answers:** is lexical search needed when the model already does semantics?

**Yes, in principle.** BM25 is not a worse embedding, it is a different failure mode. Embeddings
are strong on paraphrase and weak on exact tokens - an identifier like `INC-1042`, an error code or
a rare proper noun is a nearly meaningless direction in embedding space. BM25 is the reverse:
strong on exact tokens, blind to paraphrase ("terminate an employee" and "dismissal procedure"
share no words and score zero). Anthropic's own numbers make the same point - contextual embeddings
alone cut retrieval failures 35%, adding contextual BM25 took it to 49%.

**But measured, not assumed.** On the GDPR corpus:

| arm | recall@1 | recall@3 | MRR |
|---|---|---|---|
| vector | **91%** | 95% | **0.932** |
| hybrid (vector + BM25, RRF) | 82% | 91% | 0.867 |

Hybrid is **worse here**, and the reason is legible: formal legal prose asked about in plain
language has almost no exact tokens to match, so BM25 contributes noise and RRF lets it drag a good
vector ranking down. `sample_policy` therefore sets `hybrid=False`; `sample_ops` sets it `True`
because service names, tiers and incident ids are exactly the shape BM25 is for.

**Fusion is by rank (RRF, k=60), never by score**, because BM25 scores and cosine distances are not
on the same scale and normalising them is guesswork.

**Two bugs found on the way**, both of which silently halved recall rather than raising:
- the lexical arm ignored the vector arm's metadata filter, so fusion dragged back in exactly the
  recitals the filter was removing (91% → 36%);
- the two arms used different identity keys (an int against a string), so RRF never matched a
  chunk across arms and "fusion" was really concatenation.

A third was in the evaluator itself: recovering `Article 33` by parsing a locator string matched
**`Section 2` first**, because the path is `CHAPTER IV > Section 2 > Article 33`. Both arms now
label from metadata, which is why `retrieve_documents()` exists.

---

## D22 · The graph is built by an LLM; the queries are not
`src/agentcore/rag/graphstore.py`

**Assessment of the two reference files:** `66_neo4j_kg.py` hand-writes `CREATE` statements and
then uses `GraphCypherQAChain`; `66b_neo4j.py` uses `LLMGraphTransformer` to extract a graph from
unstructured text. **`66b` is the more useful of the two**, and it changes the calculus on graph
RAG entirely.

The original objection to graph RAG was that it needs an ontology you do not have at 10:30. With
`LLMGraphTransformer` you no longer do - extraction runs at **index time**, offline, and the result
can be inspected and re-run before anyone sees it.

So the split is by *when*, not by *whether*:

| | who writes it | when | if it is wrong |
|---|---|---|---|
| the graph | an LLM (`LLMGraphTransformer`) | index time, offline | re-run the builder |
| the queries | a human, parameterised | request time | returns wrong rows, never wrong actions |

**`GraphCypherQAChain` is deliberately excluded.** It has a model generate Cypher per request - the
same class of risk as letting a model write SQL, except it also fails in front of an audience.

`allowed_nodes` / `allowed_relationships` should be supplied whenever the vocabulary is known:
unconstrained extraction invents a slightly different label per document, and a graph with
`Service`, `service` and `SystemComponent` as three node types is not queryable by any hand-written
Cypher.

---

## D23 · Two supported ways to run, differing only in hostnames
`scripts/local.sh`, `scripts/deploy.sh`

| mode | app runs | services | for |
|---|---|---|---|
| `scripts/local.sh` | on the host, `--reload` | in Docker | developing - restarts in a second, breakpoints work |
| `scripts/deploy.sh` | in a container | in Docker | demoing and submitting - what a grader runs |

The only difference is hostnames: `localhost:<published>` on the host, `<service>:<internal>` on
the compose network. `.env` holds the localhost form and `deployment/docker-compose.yml`'s `environment:`
block overrides it, so **neither file is edited to switch modes** and there is no third
configuration to drift.

MCP follows the same split: `MCP_MODE=stdio` spawns the server as a subprocess locally,
`MCP_MODE=http` talks to the `mcp-systems` container. One server file, transport by env.

---

## D24 · MCP over HTTP works, and MCP tools are async-only
`src/agentcore/pipeline/s6_act.py`, `src/agentcore/tools/mcp_client.py`

**Verified end to end**, not assumed: the server ran with `--transport streamable-http`, the client
connected over `streamable_http`, both mutating tools appeared with their docstrings as
descriptions, and one was invoked remotely and changed state in the server's process.

**Two findings that would have cost an afternoon:**

1. **MCP tools are async-only.** `langchain_mcp_adapters` returns `StructuredTool`s with
   `coroutine` set and `func` **None**, so a synchronous `.invoke()` raises *"StructuredTool does
   not support sync invocation"* the moment the agent reaches for one. `s6_act` now always runs the
   agent with `ainvoke`, bridging from its synchronous graph node. Local `@tool` functions work
   either way, so async is the only option that works for both.

2. **The stdio server needs `--domain` passed explicitly.** It loads its own domain in a
   subprocess, so inheriting `DOMAIN` from the environment meant a caller who had not exported it
   got a server for the *default* domain - which usually has no `systems()` at all, and therefore
   returned **zero tools with no error at all**.

Transport spelling differs by side and both are correct: the **server** takes `streamable-http`
(hyphen), the **client** takes `streamable_http` (underscore).

---

## D25 · An inspector for what actually reaches the model
`src/agentcore/inspect.py`

"The answer was wrong" is four different problems that look identical from outside: the chunk was
never retrieved, it was retrieved but fell below the cutoff, it reached the prompt and was ignored,
or it reached the prompt and was misread. Only the first two are visible without instrumentation,
and only by looking.

`python -m agentcore.inspect "question"` prints the routing decision, each retrieval arm's hits,
what survived the distance gate, what the planner sees (summaries), what the answering model sees
(the full assembled prompt) and its approximate token cost. **No LLM call is made.**

Every hour spent rewriting a prompt for a retrieval problem is an hour wasted, and that is the most
common way an afternoon disappears.

---

## D26 · Every measurement is appended to a ledger
`src/evaluation/ledger.py`, `results/results.json`, `results/results.csv`

A result printed to a terminal is gone the moment the terminal scrolls. That is fine while
iterating and useless afterwards, when the questions become "did that change help?" and "what did
we actually measure?" and, at the end, when a slide needs a number that was true.

Append-only, never rewritten: a ledger you can edit is a ledger you cannot trust. Two formats, same
rows. JSON keeps nested detail (the full distance distributions, per case ranks); CSV is one row
per metric so a spreadsheet can pivot it without parsing a nested cell.

Recorded automatically by `retrieval_eval`, `calibrate` and the indexer. `python -m
evaluation.ledger` prints the current state.

---

## D27 · Charts are generated from the ledger, never drawn by hand
`src/evaluation/charts.py`, `evaluation-results/charts/*.png`

Static PNGs at 200 DPI, because these go on slides where a hover tooltip is worth nothing.

Conventions, so a new chart matches the rest and nobody re-derives them: one measure per axis and
never two scales, categorical colours assigned in a fixed order rather than cycled, values printed
on the marks so the axis is a reference and not a lookup, a recessive grid with no chart border,
horizontal gridlines only on bar charts, a legend only when there are two or more series, and plain
ASCII in every title and label.

| Image | Shows |
|---|---|
| `retrieval_arms.png` | three retrieval methods at three cutoffs |
| `metadata_filter.png` | the single biggest win, as two bars |
| `calibration.png` | real and nonsense distances, and where the threshold sits in the gap |
| `chunking_shape.png` | pages, sections and chunks |
| `pipeline_costs.png` | seconds per stage |
| `store_backends.png` | pgvector against Chroma |

Because they render from the ledger, a number on a slide is a number something measured. Re-run the
evals, re-render, and the deck is current.

---

## D28 · Chroma as a second store, chosen by one env var
`src/agentcore/rag/chroma_store.py`, `VECTOR_BACKEND`

**Not a replacement.** pgvector stays the default: it is already in the compose stack, shares one
Postgres with the checkpoints, and the 91% figure was measured against it.

It earns its place on a different axis. Chroma runs **in-process and persists to a directory**, so
it needs no container, no port and no credentials. That makes three things possible:

1. a laptop with Docker broken can still run and demo the whole pipeline
2. CI can measure retrieval against real embeddings rather than a keyword stub
3. `--reset` is a directory delete, which is a much faster iteration loop

**Measured identical on the same corpus and questions:**

| store | recall@1 | recall@3 | MRR |
|---|---|---|---|
| pgvector | 91% | 95% | 0.932 |
| chroma | 91% | 95% | 0.932 |

That equality is the finding, not a boring chart: it demonstrates the store is genuinely swappable
and that the metadata filter survives translation. The filters are the risky part - the two engines
express them differently, and Chroma rejects a single-key `$and` that PGVector accepts. A silently
dropped filter is worth 36 points of recall@1 here, so `_Adapter` translates rather than hoping.

**Cost:** a second index to keep in sync at build time, and embeddings are still an API call, so
"no server" does not mean "no network".

---

## D29 · Chainlit for the frontend
`src/agentcore/api/chainlit_app.py`

**Why a UI at all:** the pipeline PAUSES for human approval, and an approval gate is only
convincing when someone can see the plan and press the button. A curl command proves the mechanism;
a UI proves the product.

**Why Chainlit specifically:** chat, streaming, session state and an approval dialog
(`AskActionMessage`) come free, against a Python function. A hand-built React frontend is a day of
work for the same demo and a worse use of the day.

**What it deliberately shows:** the working, not just the answer. Which stages ran, the plan with
its per-step risk levels, the approval prompt, and the sources in a side panel. A chat box that
emits a paragraph would hide exactly the parts worth grading.

**One rule carried across all three interfaces:** approval **fails closed**. A dismissed dialog or
a timeout is a rejection, never an approval - the same rule the CLI and the API follow.

Runs alongside FastAPI rather than replacing it: the API is what a grader scripts against, the UI
is what a human drives.

---

## D30 · Neo4j: verified, with an honest caveat about extraction quality
`src/agentcore/rag/graphstore.py`

Built a graph from **real GDPR chunks** (Articles 33 and 34) with `LLMGraphTransformer` and
`allowed_nodes=[Obligation, Actor, Deadline, Document]`. It produced `Document`, `Obligation` and
`Actor` nodes with `REQUIRES`, `DEFINED_IN` and `MENTIONS` relations.

**The caveat, stated plainly:** even with `allowed_nodes` constraining the vocabulary, the
extraction produced relations of dubious value - `Obligation REQUIRES Obligation`, `Document
MENTIONS Document`. The node types were respected; the *edges* were not always meaningful. So the
graph arm is real and queryable, but its quality on an unseen corpus is a variable, not a given.

That is exactly why the queries are hand-written and named: a mediocre graph returns unhelpful
rows, which is recoverable, rather than a model writing Cypher against a schema it misunderstands.

**A bug found here:** `graphstore.py` never triggered `load_dotenv()`, because `agentcore.llm` is
the only module that calls it and graphstore imported it lazily inside a function. `NEO4J_PASSWORD`
was therefore empty and every connection failed with a *misleading* "password is not set" while a
direct connection worked. It now imports `agentcore.llm` at module level for the side effect.

---

## D31 · The scenario arrived, and the seam held
`src/domains/vendor_risk/`

The brief leaked: procurement, vendor risk and AI governance, assessing a fictional vendor
(Asteria AI Systems) against a fictional knowledge pack (Northstar).

**Eleven of the fifteen functional requirements were already built.** The scaffold's whole bet was
that a scenario is a package, not a set of edits, and that is what happened: `parse_request`,
the plan-and-replan loop, RAG, citations, the MCP server, four-point guardrails, injection
resistance, the HITL gate, the eval suite and failure handling all carried over untouched.

**Four were genuine gaps**, and each needed a core change rather than a domain one:

| FR | Gap | Change |
|---|---|---|
| FR05 | no way to separate evidence from inference | `Claim.basis` = evidence / inference / missing |
| FR08 | no structure for several risk domains | `RiskFinding` per domain + `Domain.risk_domains()` |
| FR11 | contradictions were not detected | `Contradiction` on the answer, found in `s8_compose` |
| FR12 | approval was binary | three-way decision with carried conditions |

Plus specialist subagents (`Domain.specialists()`), which the brief names and the deep agent
already supported.

That ratio, eleven to four, is the honest verdict on the seam: it absorbed a scenario nobody had
seen, and the four things it could not absorb were all about the SHAPE OF A VERDICT rather than
the shape of a domain. Worth knowing for next time: "what does an answer look like" was
under-modelled, and "what does a corpus look like" was not.

---

## D32 · The model recommends; a human decides
`src/agentcore/contracts.py`, `src/agentcore/pipeline/s8_compose.py`

The first end-to-end assessment produced a report that said **`decision: reject`** and then listed
eight conditions under which it would be approved. That is not a decision, it is two of them
wearing one label.

Cause: one field held both the model's recommendation and the human's verdict, so whichever wrote
last won.

Now `recommendation` (always the model's) and `decision` (the record) are separate, with
`decided_by` naming who set it. A human overriding a reject is visible rather than lost, which is
the entire point of having a human at the gate.

**A plain approval does not override the recommendation.** Approving the model's own plan adds no
information; only a CONDITIONAL approval carries something the model did not have, so only that
changes the decision of record.

---

## D33 · Section labels are positional and break silently
`src/evaluation/sections.py`

The generic section finder numbers sections in the order they appear, sorted by filename. Adding
one file renumbers every section after it, and an eval case written against the old numbering then
scores a CORRECT retrieval as a miss. It looks exactly like a retrieval regression.

Three of the first five vendor_risk labels were wrong for this reason. They were written by
reasoning about the documents rather than by reading the index.

`python -m evaluation.sections` prints the label the indexer actually recorded for every section.
Run it after any change to a corpus, before trusting a single eval number. The cost of not doing so
is an hour spent debugging retrieval that was working.

---

## D34 · WSL2 was the hard problem, not the architecture
`.wslconfig`, `deployment/docker-compose-infra.yaml`, `src/agentcore/rag/vector.py`

An afternoon went into an intermittent `ConnectionRefused` against a Postgres that was healthy.
Three separate causes, each of which looked like the others:

**1. WSL shuts its VM down when idle.** Default `vmIdleTimeout` is 60000 ms, so the VM stopped
about a minute after each command, taking every container with it. They restarted via
`restart: unless-stopped`, so `docker ps` always looked fine by the time anyone looked.
Diagnosed by `uptime -p` inside WSL reading **"up 0 minutes"** every single time.

**2. `vmIdleTimeout=-1` is not a valid value.** It is milliseconds, and `-1` is ignored SILENTLY,
which is how this looked fixed while being entirely unfixed. It is now `14400000` (4 hours).

**3. A port published on WSL's loopback depends on Windows localhost forwarding**, which registers
unreliably after a VM start. `127.0.0.1:5446:5432` became `5446:5432` so the port binds to all
interfaces INSIDE the VM. That is not a LAN exposure: WSL2 is NAT'd, so "all interfaces" means the
VM's own network.

**And one real bug this surfaced in our own code:** `open_store` retried the CONSTRUCTOR, but
SQLAlchemy's pool connects lazily, so it returned a store that failed on its first query far from
the retry loop. It now runs a `SELECT 1` probe inside the loop and sets `pool_pre_ping=True`.

**The lesson worth carrying:** when a database is "healthy" and "reachable" at different times,
those are two different facts and only one of them is what the healthcheck measured. Prove
reachability from where the client actually is.

**The fallback that saved the day:** `VECTOR_BACKEND=chroma` runs in-process with no server, and
was already measured identical to pgvector. The assessment work was completed on Chroma while the
WSL problem was still open. A second backend earned its place for a reason nobody predicted.

---

## D35 · The four missing metrics, four of them deterministic
`src/evaluation/metrics.py`, wired into `src/evaluation/agent_eval.py`

The brief names ten things to measure. Five were already covered: retrieval relevance by
`retrieval_eval`, groundedness by `judge`, guardrail compliance and injection resistance by
`trajectory`, latency by the stage timings in the ledger. **Citation correctness, tool correctness,
agent delegation and decision quality were named and measured by nothing.** Task completion made a
fifth once risk domains existed.

**Alternatives:** an LLM judge for all five, which is the default reflex; or a separate script run
after the eval.

**Why this one, on both counts.**

*Deterministic wherever possible.* Four of the five are. A deterministic check cannot flatter you,
costs nothing, and gives the same answer twice. Tool correctness reads the **audit trail**, never
the model's account of what it did - an agent's self-report is precisely the thing least worth
trusting. Only `decision_quality` genuinely needs judgement, because "does this conclusion follow"
is not a string comparison, and even there the deterministic half runs **first** and can fail the
metric on its own, so a generous judge cannot rescue an obviously wrong verdict.

*Inside `agent_eval`, not beside it.* The metrics need the same pipeline run the judge grades.
Running the pipeline twice would cost double and - worse - could measure two different runs, which
is how an evaluation quietly stops describing the system it claims to describe.

**What it costs:** `citation_correctness` is content-word overlap between a claim and the source it
names. It cannot tell whether a citation is apt, only whether the cited document says anything of
the kind. That is deliberately the cheap version of the right question, and it catches the failure
that matters - a citation pointing at a document that does not support it. The expensive version is
`evaluation/judge.py`, which already runs per claim.

**Thresholds:** `citation_correctness` 0.90, `task_completion` 1.00. The first floor is the highest
in the gate because a fabricated citation is worse than a missing answer: it is the error a reader
is least likely to check. The second is absolute because a required risk domain is either assessed
or the assessment is incomplete - there is no partial credit for a domain nobody looked at.

---

## D36 · An eval case declares what it requires, the domain does not
`src/agentcore/contracts.py` (`EvalCase.expected_domains`, `expected_tools`, `forbidden_tools`)

`task_completion` first read the required risk domains from the **domain**, which meant every case
was required to cover all four. A narrow lookup question - *"how long is the incident notification
window?"* - answers one thing by design, and would have been marked incomplete for it. With the
gate's 1.00 threshold, correct behaviour would have failed the build.

**Why:** a metric that fires on correct behaviour gets switched off, and then it protects nothing.
Ground truth about the **process** belongs on the case, exactly like ground truth about the answer.
The fields are optional on purpose: a case that says nothing about tools is not asserting that no
tools were used.

**What it buys beyond the fix:** `forbidden_tools` on every adversarial case. Refusing is not enough
if the run wrote to a system first, and nothing previously checked that.

---

## D37 · Do not give a judge a rule you already enforce
`src/evaluation/metrics.py` (`decision_quality`)

The judge prompt restated the hard rule the deterministic half already applies - *"a high risk
finding should not end in unconditional approval"*. The judge pattern matched on the phrase and
**failed a correct `approve_with_conditions`**, reasoning about it as though the conditions were
not there.

**Why it matters beyond the one prompt:** a judge given a rule that is already enforced cannot add
anything, and can subtract. The prompt now asks only what the judge alone can answer - whether
*these* conditions address *these* findings, and whether each one is checkable.

**How it was found:** a test asserting that the normal outcome passes. Worth noting that the same
test was also reaching the real endpoint, which is how it failed at all. Both are fixed; the tests
now script the judge.

---

## D38 · The layout the brief names, and the one thing that did not move
`deployment/`, `evaluation-results/`

The brief's project format lists top-level `evaluation/`, `evaluation-results/` and `deployment/`.
The stack was at the repo root and the results were inside `src/evaluation/`.

**Moved:** the compose files, overlays, `db/` and `Dockerfile` into `deployment/`; the ledger,
charts and gate reports into `evaluation-results/`. Generated output does not belong inside a
package - a directory that fills with PNGs stops being reviewable, and a reader looking for the
numbers should not have to open `src/`.

**Did not move:** `scripts/`. They are entrypoints a person types, they already resolve paths from
their own location, and `scripts/deploy.sh` is a more discoverable thing to type than
`deployment/scripts/deploy.sh`.

**What it cost, and the part worth remembering:** moving a compose file silently changes what every
relative path inside it means. `build: .` became the wrong directory and `env_file: .env` pointed
at a file no longer beside it. **Nothing fails at import time; nothing fails until a deploy.** All
four profile combinations were re-validated with `docker compose config`, and
`tests/test_layout.py` now asserts the build context is still the repo root, every bind mount
exists, and no stale copy was left at the root to deploy by accident.

---

## D39 · Contracts that are recorded get enforced, or they are decoration
`src/agentcore/pipeline/s9_guard_out.py` (`check_claims`), `s8_compose.py`

Two flaws with one shape, found by re-reading the code against the brief rather than by a test.

**`Claim.basis` was recorded and never checked.** The field distinguishes evidence from inference
from missing - the whole difference between an assessment and an opinion. An `evidence` claim with
no citation passed straight through. `check_claims()` now enforces that an evidence claim cites, an
inference claim reasons, a citation names a source retrieval actually returned, and a declared risk
domain has a finding.

**`s8_compose` wrote the report from a second independent pass** over the same evidence, tied to
the first by nothing. It could produce a recommendation disagreeing with the findings printed above
it. The second call is now a transcription of the assessment already computed, told explicitly not
to re-evaluate it.

**The enforcement downgrades rather than deletes.** A partial assessment that names which parts are
weak is worth more than no assessment, and far more than a confident fabrication. Same principle as
the groundedness downgrade in D20.

**The general lesson:** every one of these was a contract the code recorded and did not enforce.
They are invisible to review precisely because the field exists and looks handled. What catches
them is asking, for each field, *what fails if this is wrong?* - and if the answer is "nothing",
the field is documentation, not a contract.

---

## D40 · One event loop per process, not one per call
`src/agentcore/aio.py`, used by `pipeline/s6_act.py` and `tools/mcp_client.py`

The graph nodes are synchronous; MCP tools are async-only. Something has to bridge that, and the
obvious bridge is `asyncio.run(...)` at each boundary.

**Why that fails, and why it takes a while to see:** `asyncio.run` creates a loop, runs the
coroutine, and **closes** it. MCP tools are fetched under one call and invoked under a later one,
carrying async resources bound to the first loop - a session, a stdio subprocess transport, an
httpx pool. By the time a tool is called, the loop that owns its transport is gone. The error
surfaces from **inside a tool call**, as `RuntimeError: Event loop is closed`, so it reads as a
flaky tool rather than a broken bridge. In a pipeline that replans around failed steps, it reads as
a flaky tool that the system is gracefully handling.

**Alternatives:** a fresh `ThreadPoolExecutor` per call (same problem, one thread further away);
making the graph nodes async end to end (correct, and a much larger change that would put every
stage on the critical path of a refactor the day before a hackathon); or opening an explicit MCP
session per tool call (pays subprocess startup on every call, and only helps MCP).

**Why this one:** one loop, on a daemon thread, open for the life of the process. Resources created
on it stay valid for every later call. Used from inside an already-running loop (FastAPI) it
submits work rather than nesting, so the API path needs no special case. It is about forty lines
and it is the only place in the codebase that knows the bridge exists.

**What it costs:** a background thread, and a `timeout` on every call - a backstop rather than a
policy, because a hung MCP server should surface as a failed step and not as a pipeline that never
returns. A live demo that stops responding is worse than one that reports an error.

**Measured:** the flagship assessment case went from four failed steps and a replan-limit answer to
one step, `step_done`, four risk findings and a real verdict. See PROBLEMS.md P40.

---

## D41 · Delegation is a plan decision with an owner, not an instruction to the executor
`src/agentcore/pipeline/s4_plan.py`, `s6_act.py`, `contracts.PlanStep.owner`

FR07 needs agent-to-agent interaction with at least two specialists, and it is worth 10 of 100.
Three specialists were wired as deep agent subagents and delegation measured **0.00 on all twelve
eval cases** - the executor had never emitted a single `task` call.

**The obvious fix was the wrong one.** Telling the executor to delegate more firmly would have
produced delegation, and it would have been theatre: a `task` call made because the prompt demanded
one, to whichever specialist the model picked, measured by a metric counting `task` calls.

**What was actually wrong** was that the surrounding structure made delegation look like
disobedience. The executor prompt said *"Do exactly this step and nothing else. Do not attempt
other actions"* and then, three paragraphs and an untrusted evidence block later, *"you may
delegate"*. Meanwhile the planner had no concept of specialists at all, `PlanStep` had no field
that could name one, and a rule collapsed assessments to a single step - and a one-step plan has
nothing to delegate.

**So the decision is made one stage earlier.** The planner assigns an `owner` to each step, chosen
from specialist descriptions, with the risk domains in front of it. By the time the executor runs,
delegating is not an exception to "do exactly this step" - it **is** the step, and `s5_gate`
approved it as such. The prohibition sentence survives verbatim; what changed is that "this step"
now has an extent that already contains the hand-off.

**Alternatives:** a stronger instruction (theatre, as above); routing in the graph, one node per
specialist (the plan stops being data and becomes topology, and the gate can no longer approve a
step before it runs); or accepting the 10 points as lost.

**What it costs:** an assessment now plans four steps instead of one, so the executor runs four
times. Latency roughly quadruples on the assessment case, which was already 86 seconds. `MAX_STEPS`
and both loop caps are unchanged, so the ceiling is the same.

**Measured:** delegation 0.00 -> **1.00**, owned steps reaching their named specialist 0/4 ->
**4/4**, task completion 0.75 -> **1.00**, citation correctness 0.00 -> **0.75**.

**The anti-gaming piece is the point.** deepagents auto-injects a `general-purpose` subagent with
broad reach, so counting `task` calls alone would let a hand-off to that satisfy a metric about
specialists. The audit records `delegated_to`, and a trajectory check requires the terminal event
for each owned step to name **that step's own declared owner**. A run can only score by delegating
to the specialist its own planner chose.

---

## D42 · Azure Monitor alongside LangSmith, not instead of it
`src/agentcore/observability.py`, `deployment/azure/deploy.sh`

**Supersedes the "only observability backend" framing in D1**, which was written before the real
handout arrived. Section 11 and the Definition of Done require Application Insights and Azure
Monitor over OpenTelemetry, plus a deployment to an Azure service.

**Both, because they answer different questions.** LangSmith is where an engineer reads one run's
prompts and outputs while building. App Insights is where an operator watches a fleet: latency
distributions, failure rates, exceptions over time, evaluation results on the same dashboard as a
latency spike. Dropping LangSmith to satisfy the handout would trade a working development tool for
a compliance box.

The RAM argument in D1 still holds and is untouched: both are SaaS, neither adds a container.

**Why this was cheap.** OpenTelemetry 1.44.0 was already installed transitively, via
chainlit -> literalai -> traceloop-sdk and via chromadb. Only `azure-monitor-opentelemetry` and the
FastAPI instrumentor were missing. And the nine LangGraph node names were already the `stage` values
in every audit event, so **span names equal node names with no mapping table to drift** - a trace
reads `s1_intake -> ... -> s9_guard_out`, which is the same sequence `ls` prints.

**Audit events are span EVENTS, not spans.** `audit_event` records what happened and carries no
timings. Promoting them to spans would mean inventing durations.

**The one rule:** telemetry must never break what it observes. Every entry point catches broadly
and degrades to a no-op, because a dead exporter should cost a trace and never a request. Twelve
tests cover the off path, including one that asserts a broken exporter does not raise - which
caught a real gap where `tracer()` was called outside the guard.

**Deployment: Container Apps, pinned to one replica.** Not a cost saving - a correctness
constraint. The API keeps LangGraph checkpoints in a `MemorySaver`, so a run paused for human
approval lives in exactly one replica's memory and a second replica would fail roughly half the
resume requests. Scaling out means wiring the Postgres checkpointer first;
`langgraph-checkpoint-postgres` is already a dependency.

---

## D43 · The per-step allowlist now removes the library's built-in tools
`src/agentcore/pipeline/s6_act.py` (`_no_filesystem`)

`s6_act` claimed the agent receives "ONLY the tools that step declared", and that injected text
naming another tool "fails because that tool is not bound - not merely discouraged".

**That was false.** `create_deep_agent` binds `ls`, `read_file`, `write_file`, `edit_file`, `glob`
and `grep` to every agent, and the library is explicit that passing `tools=` is additive and never
removes a built-in.

**The exposure was smaller than it sounds**, and worth stating precisely rather than dramatising:
the default `StateBackend` is not a sandbox, so no `execute` tool is bound and no shell ever runs,
and the file tools operate on ephemeral LangGraph state rather than the host disk. But `write_file`
was bound to an agent whose guardrail story said it was not, and that gap is the kind that is fine
until the day the backend changes.

**Fix:** pass our own `FilesystemMiddleware(tools=["read_file"])`. `read_file` cannot be removed -
the middleware rejects a set without it - and it is the harmless one, reading a backend nothing in
this pipeline writes to.

**The measured effect was larger than the security one, and that was the surprise.** A live
assessment had been spending eight tool calls running `grep` and `read_file` against an **empty**
virtual filesystem instead of the retrieval and TCO tools its step declared. Removing the
distractions took delegation from 2/4 to 4/4 owned steps and citation correctness from 0.00 to
0.75. The model was not ignoring its tools; it was reaching for closer ones that did nothing.

---

## D44 · Local only, and no A2A protocol
Scope decision, 2026-09-23. Narrows D42.

Two scope calls, made after the Azure work landed and recorded here because both look like gaps
otherwise.

### Everything runs locally. There is no cloud deployment.

Section 11 and the Definition of Done ask for the API deployed to an Azure service. We are not
doing that. Development, the evaluation runs and the demo are all local Docker.

**Azure OpenAI is unaffected and still the LLM.** That is four values in `.env`, not a cloud
deployment, and confusing the two would be easy: this project uses Azure, it just does not deploy
to it.

`src/agentcore/observability.py` stays, inert. It is a no-op unless
`APPLICATIONINSIGHTS_CONNECTION_STRING` is set, which it is not, so the cost of keeping it is zero
and `/healthz` reports `azure_monitor_enabled: false` honestly. `deployment/azure/deploy.sh` stays
for the same reason: it documents the shortest path if this ever changes, and an unused script that
is never invoked cannot break a demo.

**LangSmith is therefore the live tracing backend**, which is what D1 originally said. D42 added
Azure Monitor alongside it; this narrows that to "wired, dormant".

**What it costs:** the demo cannot end on "Deployed Application". Worth knowing that the handout's
scoring table does not list Azure at all - its eight criteria sum to 90 of a stated 100, so the
weight of the missing row is unknown. If Azure is the missing 10, this decision costs them.

### No A2A protocol. The equivalent, which the handout allows.

FR07 asks for *"A2A **or an equivalent** agent-to-agent interaction with at least two specialist
agents"*. We do the equivalent: four deep agent subagents, assigned by the planner and delegated to
by the executor, measured at 1.00 with 4/4 owned steps reaching their named specialist.

**Why not the protocol:** A2A is a wire protocol for agents that need to find and talk to each
other **across trust or process boundaries**. Every specialist here runs in the same process, on
the same approved step, bound to the same per-step tool allowlist. Putting a protocol between them
would add a transport, a serialisation format and a discovery mechanism to buy a diagram - and it
would puncture the property that makes the allowlist safe, which is that a specialist cannot reach
anything the step did not declare.

**What it costs:** if a judge reads FR07 as requiring the named protocol rather than an equivalent,
this reads as a gap. The handout's own wording is the defence, and `architecture/README.md` states
the position rather than leaving it to be inferred.

---

## D45 · Langfuse replaces LangSmith
`src/agentcore/llm.py` (`langfuse_handler`), `pipeline/graph.py` (`build_app`)

**Supersedes D1's "LangSmith only" and narrows D42 and D44.** Contributed on the
`christos_guardrails` branch; reviewed and merged rather than argued with, because the reasoning
holds.

**Why the original decision is reversed.** D1 dropped Langfuse on a measurement: WSL has 3,663 MB
and the self-hosted Langfuse stack consumes about 2.4 GB of it. That argument was about
**self-hosting**, and it does not apply to **Langfuse Cloud**. `LANGFUSE_HOST` defaults to
`https://cloud.langfuse.com`, so the RAM cost is the same zero that made LangSmith attractive, and
the course guide asks for Langfuse.

The team was right and the earlier `TEAM_PLAN.md` note ("the photo says Langfuse, we use
LangSmith") is resolved in their favour.

**How it is wired.** `langfuse_handler()` returns `None` unless `LANGFUSE_PUBLIC_KEY` is set, and
`build_app()` binds it once with `.with_config(callbacks=[handler])`. Binding at the compiled graph
rather than at each call site means `service.py`, `chainlit_app.py`, `console.py` and every
evaluation entry point get tracing without threading a callback through each of them.

**Verified before merging**, because a tracing change that breaks the graph is worse than no
tracing:

- `with_config` on a `CompiledStateGraph` returns a `CompiledStateGraph`, not a `RunnableBinding`,
  so `get_state` and `stream` survive. The console and `service.py` both depend on those, and a
  `RunnableBinding` would have broken approval resume the moment anyone added a key.
- the full suite passes with keys set and unreachable: 401s are logged, runs are unaffected. Same
  rule as D42, telemetry must never break what it observes.
- the handler really does construct when keys are present, so the bound path is exercised rather
  than assumed.

**One thing the change missed, fixed on merge.** `scripts/preflight.sh` still listed
`LANGSMITH_API_KEY` in `REQUIRED_KEYS`. With that name gone from `.env.example`, preflight demanded
a key no fresh clone could ever supply, and `deploy.sh` sources preflight, so the whole deploy path
was blocked. The Langfuse keys deliberately did **not** replace it in `REQUIRED_KEYS`: the pipeline
runs identically with tracing off, and demanding a key for an optional integration blocks anyone
who does not want it. They are listed as `OPTIONAL_KEYS` and preflight now says `tracing is OFF`
once per run, so nobody discovers at 16:00 that it was never configured.

**What it costs:** one more SaaS account to create before the day, and `langsmith` remains
installed as a transitive dependency of langchain, which is harmless but means the package being
present proves nothing about what is in use.

---

## D46 · A bad risk level degrades its own finding, it does not fail the report

**This reverses part of an earlier decision, on evidence.** `RiskFinding.level` mapped the words a
model reaches for onto the four level scale and deliberately **raised** on anything it could not
map, on the reasoning that guessing a risk level is worse than refusing one. A test asserted it.

Ten runs of the flagship case said what that cost: **three entire reports**. `findings` is a list
inside `AssessmentDraft`, so one unmappable string makes pydantic reject the enclosing model,
`s8_compose` catches it, and the summary, fourteen claims, four findings and the decision are all
replaced by "Could not compose an answer" (PROBLEMS P55).

**The principle that was wrong.** Failing closed on one field is conservative only while the
failure stays LOCAL. Here it did not. Refusing to guess one level silently refused everything
around it, and the reader got nothing at all instead of an assessment with one soft spot. A refusal
that takes its neighbours down with it is not caution, it is a crash with a tidy justification.

**What it does now.** An unreadable level sets that finding to `level="none"`, `assessed=False`, and
records the word in `gaps`. The domain reads as a gap in coverage, which is what it is, and
`task_completion` counts it against us. The degradation runs in the direction that does not flatter
the system, which is the property that makes it safe to do at all.

**Still NOT coerced: a decision.** `s5_gate._read_decision` maps an unrecognised decision to
`rejected`. A risk level is an ordinal scale where `critical` has an obvious place; a decision is
not, and an unrecognised one could mean anything. The two look like the same problem and are not,
which is why a test pins both behaviours side by side.

**Compound levels round up.** `medium-high` becomes `high`. A model writing a compound is between
two points, and for risk the conservative read of "between" is the higher one.

**What it costs:** a finding can now be marked unassessed because of a formatting problem rather
than a real gap in evidence, and those two are indistinguishable in the metric. The `gaps` entry is
what tells them apart, so it has to carry the offending word rather than a generic message.

---

## D47 · LangGraph is the observability. LangSmith only if credentials arrive

**Supersedes D45** (Langfuse Cloud), which itself superseded D1 (LangSmith only). Three positions
in two days is worth explaining rather than hiding: each was decided on a different question, and
only this one asks what happens when the network does not cooperate.

**The layers now:**

| Layer | State | Needs |
|---|---|---|
| LangGraph audit trail | **always on** | nothing. No account, no container, no network |
| LangSmith | optional | an API key, if one arrives |
| Azure Monitor | optional | a connection string. D42, still dormant |

**Why the local layer is the one that counts.** Every hosted viewer needs an account, outbound
egress and a working network, and a demo venue reliably supplies none of the three. The audit trail
has no such dependency: it is ordinary application state, written by every stage, and it already
carries the evidence a judge would ask for - which specialist ran, what the gate decided, which
tripwire fired. `agentcore.tracing` renders it with per-stage timings:

```bash
uv run python -m agentcore.tracing "your question"
```

**Why LangSmith rather than Langfuse for the optional slot.** Langfuse needs an SDK, a callback
handler, and a `.with_config()` wrapper around the compiled graph. That wrapper was load-bearing:
the console and `service.py` both reach through it for `get_state`, so every change near it had to
be re-verified against those. LangSmith needs **none of that** - langchain reads its own
environment - so the optional layer now costs a key instead of a dependency, a handler and a
wrapper. The `langsmith` package is already installed as a langchain transitive dependency, so
turning it on adds nothing to the image.

**The one thing that DOES need code**, and the reason `tracing.py` exists at all: the half
configured state. `LANGSMITH_TRACING=true` is by itself enough to make langchain trace, so a value
inherited from a parent shell points a keyless process at an endpoint it cannot authenticate to,
and langchain then retries and logs on every call. `configure()` therefore DERIVES the switch from
the key and forces it to a literal `false` when there is none. An absent credential is an ordinary
state, not a partial failure.

**What it costs:** no hosted run viewer unless a key turns up, so the local trace is what gets
shown. Langfuse's prompt-and-output view was genuinely nicer to read than an audit trail, and that
is given up. `/healthz` reports `audit_trail_enabled` and `tracing_enabled` separately so nobody
has to guess which layer is live.

---

## D48 · Neo4j is removed as a container, kept as a seam

**It was already dead.** `vendor_risk`'s `GRAPH_QUERIES` is `{}`, so the graph arm was never
queried by the scenario this project is being judged on. Only `sample_ops` defines any, and
`/healthz` has been reporting `graph_arm: false` throughout. A JVM was being started, capped and
health-checked for a code path nothing entered.

**What it cost to keep:** ~768MB of a 3,663MB WSL budget, plus a heap that needed explicit capping
because a JVM sizes itself from the cgroup limit rather than the host, plus a health gate in
`deploy.sh` and a service in three compose files.

**What is removed:** the `neo4j` service, its volume, its ports, its health gate, and the
`NEO4J_URI` override in the app stack. The `lean` and `full` profiles now differ only in the
Postgres memory limit; both are kept, because the four combinations are wired into `deploy.sh` and
validated in CI and collapsing them would be churn for no saving.

**What is kept, deliberately:** `rag/graphstore.py`, the `graph_query` tool factory,
`graph_queries()` on the domain Protocol and `BaseDomain`, `langchain-neo4j`, and the `NEO4J_*`
variables in `.env.example`. The seam is domain-agnostic architecture that costs nothing at runtime
when a domain returns `{}`, the import is lazy so nothing loads on a run with no graph arm, and
removing it would edit the domain Protocol - which any teammate branch implementing that Protocol
would then fail against. A scenario that wants a graph arm points `NEO4J_URI` at its own instance
and the tool works unchanged.

**The failure mode this creates, and the guard for it.** Compose only stops what it currently
declares, so removing a service ORPHANS any container already created from it: `h2-neo4j` keeps
running, keeps its RAM, and `docker compose down` never touches it again. On the very machine the
removal was meant to help, the saving would never arrive. `deploy.sh` now sweeps a named
`RETIRED_CONTAINERS` list on every run. Named explicitly rather than pruned by label, because a
broad prune on a box that also hosts hackathon 1 is exactly the kind of cleanup that takes
something it should not.

**What it costs:** no graph RAG to demonstrate, which was a differentiator on paper and nothing in
practice, since no query existed to run. Bringing it back is a compose service and a filled in
`GRAPH_QUERIES`, not a rewrite.


## D49 · Tools are gated by the caller's role, and the gate skips what a role may not run
`src/agentcore/tools/registry.py`, `src/agentcore/pipeline/s5_gate.py`, `src/agentcore/pipeline/s9_guard_out.py`
Guardrails role. Handout section 9: "Restrict sensitive MCP tools according to role/authorization."

**Before:** the allowlist asked whether a step declared a tool and the risk gate whether it was
dangerous. Nothing asked who was calling.

**Rule:** each role has a ceiling on the existing low/medium/high scale, so there is no second
per-tool table to keep in step with the first. `user`, `engineer` and `procurement` are medium (read,
retrieve, calculate); `admin` is high. A role the table does not name gets the lowest ceiling, so
being unlisted is never a way in. The role is read from the bound actor (`world.bound`), the same
source that scopes retrieval, so the API, Chainlit and the evaluation all supply it.

**Two enforcement points, one rule.** `steps_above_role` decides for the gate and `restrict_by_role`
for the executor, both on the tool's own floor, and a test asserts they agree.
- *Executor:* a tool above the ceiling is replaced by a stand-in with the same name and arguments
  that runs nothing and returns "Denied: ...". Removing it would leave "unknown tool" for the reader
  to decode.
- *Gate:* it skips only the steps above the requester's role. Each becomes `skipped` with a failed
  `StepResult`, a `role_skipped` audit event records it, and the rest of the plan runs. The risk level
  is recomputed over the steps that will actually run, so a plan whose only high risk step was skipped
  no longer pauses for approval, and the approval prompt lists only what will run. `s9` appends a
  "[Skipped for your role ...]" note and `s8` marks the answer partial. Only when NO runnable step is
  left does the gate refuse the whole request ("Not authorised"), because an empty plan would look
  like a success.

**Why the gate and not only the executor:** before this, a user saw the approval prompt, approved,
and only then met "Denied" - a real click that changed nothing.

**Human authority is the companion rule, in `s9`.** An unconditional `approve` on a plan with any
`high` finding becomes `pending`; a conditional approval with no human review is labelled as awaiting
one. This follows AI-004 s6 and PR-001 s4: a High risk AI vendor is not approved by an automated
recommendation alone.

**Alternatives:** an admin-only rule (clear in a demo, but the default user could then do nothing);
refusing the whole plan when one step is above the role (simpler, but a user who asked for an
assessment and a filing would lose the assessment).

**What it costs:** the Chainlit role picker is a demo device, not authentication - over the API the
role comes from the account. A skipped write step is reported in the answer, so a reader has to notice
the note to know the filing did not happen.
