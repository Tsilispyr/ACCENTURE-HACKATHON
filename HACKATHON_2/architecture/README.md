# Architecture

How the system is put together, and why it is shaped this way. The reasoning behind each choice,
with the alternatives and what they cost, is in [DECISIONS.md](../DECISIONS.md); this file is the
map.

## The shape of it

A business request arrives and moves through nine named stages. The stages are the architecture:
`ls src/agentcore/pipeline/` prints them in execution order, which is deliberate.

```
                                                     high risk
request -> s1_intake -> s2_guard_in -> s3_ground -> s4_plan -> s5_gate --------> human approval
                             |                                    |                    |
                             | refusal                            | approved           | rejected
                             |                                    v                    |
                             |                              s6_act -> s7_replan        |
                             |                                    ^        |           |
                             |                     steps remain   +--------+           |
                             |                                             | done      |
                             v                                             v           v
                        s9_guard_out <------------------------------- s8_compose <-----+
                             |
                             v
                          answer
```

| Stage | Does | Carries |
|---|---|---|
| `s1_intake` | parse the request into the domain's schema | FR01 |
| `s2_guard_in` | injection patterns, PII, size caps. No LLM | FR09 |
| `s3_ground` | retrieve, self grade, rewrite and retry (max 2) | FR03, FR04 |
| `s4_plan` | build a revision numbered plan | FR02 |
| `s5_gate` | risk floor, then interrupt if high | FR12, FR13 |
| `s6_act` | run ONE step through a deep agent, with an allowlist | FR06, FR07 |
| `s7_replan` | done, continue, or replan (max 3) | FR02 |
| `s8_compose` | claims tagged by basis, one finding per risk domain | FR05, FR08, FR11 |
| `s9_guard_out` | claim contract, groundedness, PII scrub | FR09, FR11 |

Two hard caps are asserted in tests: `replan_count >= 3` forces composition with `partial=True`,
and retrieval rewriting stops after 2. Unbounded loops are the classic live demo death.

## Why a graph, with the deep agent at one node

`create_deep_agent` sits at exactly one stage, `s6_act`, and executes a single approved step.

An agent that owns its own control flow cannot be asked "did retrieval run before planning?" A
`StateGraph` can, and the assertion is a test rather than a hope. It also means the whole pipeline
runs offline against a scripted LLM, which is why 281 tests need no network and no API key.

The cost is that the agent is less free. That is the point: `s5_gate` has already approved exactly
what the step may do, so an interrupt surfacing from INSIDE the agent means it reached for
something outside the approved step. That auto-rejects and writes a safety event.

## The domain seam

`src/agentcore/domain.py` is a `Protocol` with 19 methods and a `BaseDomain` supplying a working
default for each. `agentcore` never imports `domains` except through `registry.py`, and a test
(`test_no_domain_leakage.py`) asserts the core contains no scenario vocabulary.

```
persona() glossary() parse_request()              identity and intake
corpus() retrieval_policy() section_finder()      knowledge
local_tools() mcp_servers() action_risk()         action, and the risk floor
blocked_patterns() pii_rules()                    safety
risk_domains() specialists()                      the assessment
report_schema() eval_cases()                      output and ground truth
```

Swapping scenario means writing one package under `src/domains/`, not editing the core.
`vendor_risk` is the hackathon scenario; `sample_policy` (GDPR) and `sample_ops` are reference
domains; `deterministic` does zero I/O and exists so the CI gate can measure retrieval with no
database.

## Where the requirements live

| Concern | Where | Note |
|---|---|---|
| **RAG** | `src/agentcore/rag/` | pgvector or Chroma by one env var; BM25 and RRF available, measured, off by default because it scored worse |
| **MCP** | `src/mcp_servers/systems_server.py` | everything that MUTATES is behind MCP, so the approval gate sits on the process boundary |
| **Specialist agents** | `Domain.specialists()` -> deep agent subagents | four reviewers, the handout's section 8 suggestion. Each is a different STANDARD OF JUDGEMENT, not a slice of work. Delegation measures **1.00** |
| **Guardrails** | `src/agentcore/safety/` + `s2`, `s9` | four surfaces: input, retrieved content, tool access, output |
| **Evaluation** | `src/evaluation/` | ten metrics; output to `evaluation-results/` |
| **Deployment** | `Dockerfile`, `docker-compose.yml`, `deployment/` | two compose stacks joined by an external network, memory overlays. **Local only**, by decision |

## The three ideas worth arguing about

**The risk floor the model cannot lower.** `action_risk()` sets a floor per tool; the model may
raise a level and never lower one. A gate the model can argue its way past is decoration.

**Untrusted by default.** Every piece of retrieved evidence carries `trusted=False` and reaches a
prompt inside an explicit envelope. The planner sees summaries, never raw retrieved text, so an
injection in a document cannot rewrite the plan.

**The model recommends; a human decides.** `recommendation`, `decision` and `decided_by` are three
separate fields. Collapsing them erases the disagreement between what the model concluded and what
a reviewer signed, which is the most interesting line in an assessment.

## Reading order

| File | For |
|---|---|
| [STATUS.md](../STATUS.md) | where it actually is, and what remains. **Open this first** |
| [DECISIONS.md](../DECISIONS.md) | why it is built this way, 44 entries |
| [PROBLEMS.md](../PROBLEMS.md) | every defect found and how it was found |
| [RUNBOOK.md](../RUNBOOK.md) | running it, and what to do when it breaks |
| [PLAN.md](../PLAN.md) | what was intended, and the cut list |
| [hackathon2-handout.pdf](hackathon2-handout.pdf) | the requirements this is built against |
