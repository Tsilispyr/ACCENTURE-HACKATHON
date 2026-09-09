# AI-Powered IT Incident Resolution Agent

A stateful, multi-pattern LangGraph service that triages, investigates, diagnoses, remediates and
verifies IT incidents against a simulated enterprise estate. Traced with Langfuse, reports archived
to MinIO, state persisted in Postgres, and runnable from a clean checkout with one command.

```
Incident ──► Triage ──► Investigation (parallel) ──► Diagnosis ──► Remediation plan + risk
                                                                            │
                                                          low/medium ◄──────┴──────► high
                                                               │                      │
                                                               │              Human approval
                                                               │                      │
                                                               └──────► Execute ◄─────┘
                                                                            │
                                                                         Verify
                                                                        ╱      ╲
                                                                  Resolved    Failed
                                                                       │         │
                                                                     Close    Replan (bounded)
```

## Quick start

```bash
# WSL / Linux / macOS, or Git Bash (which re-execs itself into WSL)
bash scripts/deploy.sh

# PowerShell or cmd
.\scripts\deploy.ps1
```

The only thing you supply is your four `AZURE_OPENAI_*` values — preflight asks once and writes
them to `.env`. Everything else ships pre-filled. Then open **http://localhost:8010/ui**.

| Service | URL | Credentials |
|---|---|---|
| Web UI | http://localhost:8010/ui | — |
| API docs | http://localhost:8010/docs | — |
| Langfuse traces | http://localhost:3000 | `user@example.com` / `12345678` |
| Grafana dashboards | http://localhost:3001 | `gtgh` / `grafanapassQWqw!@12` |
| MinIO console | http://localhost:9091 | `minio` / `miniopassQWqw!@12` |

## API

```
POST /incidents               submit an incident and run the workflow
GET  /incidents/{id}          retrieve its current state
POST /incidents/{id}/approve  approve or reject a high-risk remediation
GET  /health                  liveness plus which subsystems are actually live
GET  /ui                      operator web page
GET  /chat                    read-only investigation assistant
```

`POST /incidents` takes the five fields from the requirements, verbatim:

```json
{
  "Incident ID": "INC-1042",
  "Service": "payment-service",
  "Severity": "Unknown",
  "Description": "Customers report payment failures for approximately 15 minutes.",
  "Error": "Database connection timeout."
}
```

**`thread_id` is the incident id.** That single convention is what makes retrieval and
approval-resume work without a second store.

## How it is built

**Three layers, deliberately separated.**

| | Responsibility |
|---|---|
| `tools.py` + `world.py` | **Facts.** Eight tools over a simulated estate. No LLM. |
| `adapters.py` | **Judgement.** Triage, diagnosis and planning reason over the evidence tools produced. |
| `tools.effective_risk` | **Safety.** A risk floor the model may raise but never lower. |

`graph.py` talks to an `IncidentAdapter` protocol and imports no tools; `tools.py` knows nothing
about the graph. `adapters.LiveIncidentAdapter` binds them, and
`DeterministicIncidentAdapter` substitutes for it in tests with no I/O at all.

**Two properties worth defending:**

1. **The approval gate cannot be talked past.** Risk comes from a static policy floor derived from
   the action and its context. A model assessment can raise it and is ignored if it tries to lower
   it — because incident text is attacker-controllable in any real deployment, and a control the
   model can argue with is decorative. If the risk call fails outright, the policy floor stands.
2. **Remediation never reports success.** Execution returns an operator-style receipt of what it
   did; whether the incident is *fixed* is decided separately by observing service health. That is
   what gives the replan loop something real to react to, instead of a model declaring victory.

Approval is also **per plan revision** — approving one plan does not authorise a different one
produced by a later replan.

## Tools

| Tier | Tools | Risk |
|---|---|---|
| Investigation (read-only) | `search_logs`, `get_service_metrics`, `search_knowledge_base`, `get_incident_history` | none |
| Remediation | `scale_connection_pool` · `restart_service` · `rollback_change` | low · high (medium on allowlisted services) · always high |
| Verification | `check_service_health` | none |

The remediation tools deliberately span the risk scale. With a single high-risk action the approval
router would be a constant that always routes one way; the spread makes it a decision.

The estate is not uniform, which is what makes the workflow's behaviour meaningful:

| Service | Correct remediation |
|---|---|
| `payment-service` | scale the pool, **then** restart — scaling alone is partial |
| `identity-service` | roll back the change |
| `order-service` | restart |
| `catalog-service` | scale the pool — the running process reloads the new ceiling |
| `inventory-service` | scale the pool, **then** approve a restart — scaling alone is partial |
| `reporting-service` | nothing works — exercises the bounded-retry path |

`get_service_metrics` also fails its **first** call for a service, by design, so the tool-failure
path is exercised on a normal run rather than only under contrivance.

### UI scenario templates

The UI at `http://localhost:8010/ui` asks for five fields: **Incident ID**, **Service**,
**Severity**, **Description**, and **Error**. The following templates demonstrate three different
workflow outcomes. Submit each template as a separate incident.

#### 1. Immediate resolution: scale succeeds

Use `catalog-service`. The pool is saturated, but this service reloads the new pool ceiling while
running. `scale_connection_pool` is low risk, executes without approval, and verification should
finish the incident as **resolved**.

| Field | Value |
|---|---|
| Incident ID | `INC-CATALOG-001` |
| Service | `catalog-service` |
| Severity | `low` |
| Description | `Catalog requests are timing out because the database connection pool is exhausted. Increase the pool size.` |
| Error | `Database connection timeout` |

#### 2. First attempt fails, approved restart resolves it

Use `inventory-service`. Scaling lowers the symptoms but leaked connections remain held, so the
first verification is still degraded. The graph proposes `restart_service`; because this service
is not on the low-impact allowlist, the UI pauses at **Human approval required**. Choose **Approve**
to run the restart and finish as **resolved**.

| Field | Value |
|---|---|
| Incident ID | `INC-INVENTORY-001` |
| Service | `inventory-service` |
| Severity | `high` |
| Description | Inventory requests are timing out because connections are leaking from the stock reservation repository. Scaling the pool may reduce pressure, but a restart is needed to reclaim leaked connections.` |
| Error | `Database connection timeout; connection leak suspected` |

#### 3. Second attempt fails: escalate unresolved

Use `reporting-service`. The failure is in the upstream analytics warehouse, not the local
service. Local remediation actions do not change the breached metrics. The graph will retry its
plan within the bounded limit and eventually finish as **unresolved**, with escalation recorded in
the final report.

| Field | Value |
|---|---|
| Incident ID | `INC-REPORTING-001` |
| Service | `reporting-service` |
| Severity | `high` |
| Description | Scheduled reports are failing because the upstream analytics warehouse is degraded. Local CPU and memory are normal. |
| Error | `Warehouse query timed out; upstream data source unavailable` |

## Testing

```bash
uv run pytest -v                      # 96 tests, no network, no API costs
uv run pytest tests/live -v           # end-to-end against the deployed stack
```

| Suite | What it covers |
|---|---|
| `tests/test_tools.py` | The tools, weighted towards the risk policy the approval gate rests on |
| `tests/test_incident_workflow.py` | The compiled graph with a deterministic adapter |
| `tests/acceptance/` | Real graph, real tools, real evidence; only the LLM is scripted |
| `tests/api/` | The five-field request contract, parametrised over the supplied examples |
| `tests/live/` | The deployed container, including Langfuse trace correlation. Skips itself when the stack is not running |

The acceptance tests wrap each tool in a spy that records the call and then **delegates to the real
implementation**, so every assertion about a tool call is about a call that actually happened, and
the evidence reaching the diagnosis is the estate's real data.

## Deployment

Two Compose stacks joined by one network: the infrastructure (Postgres, ClickHouse, Redis, MinIO,
Langfuse, Grafana) and this app. `scripts/deploy.sh` orders them, since Compose's own `depends_on`
cannot span two files.

Deployment sizes itself to the machine: `scripts/preflight.sh` reads total RAM and picks a `lean` or
`full` resource profile. Grafana does not start on `lean` — it is a bonus service and lean exists to
free its ~256 MiB.

**Full reference — what every file does, how to drive `scripts/` and `compose/`, a cookbook and the
traps:** [`architecture/infrastructure.md`](architecture/infrastructure.md).

## Known limitations

- The `full` resource profile has been rendered and validated but never actually run; the
  development machine can only ever select `lean`.
- Grafana's ClickHouse plugin downloads at container start, so a first run on a new machine needs
  internet or the dashboards have no datasource.
- `stack-guide.md` describes the older Langfuse v2 stack and is kept only as history; sections 6 and
  9 are still accurate, the rest is not.
- `apiclient.py` is deliberately unhardened — no timeouts, no retries. It is a smoke test.
