# Hackathon 1 -- CodeHub Ticket Triage

A LangGraph service that triages and resolves CodeHub support tickets, combining the patterns `project/HACKATHON1.md` names explicitly: **Orchestrator-Workers**, **Multi-Agent Supervisor**, and **Async LangGraph**, plus both of Day3's routing mechanisms (`add_conditional_edges` and `Command`). No real business case has been assigned yet -- this uses the CodeHub support-ticket domain that runs through the whole curriculum as a placeholder, easy to swap once one lands. Traced via Langfuse, reports uploaded to MinIO.

## Architecture

Two independent, connected Docker Compose stacks -- per `HACKATHON1.md`'s own request for "2 docker compose files ... depending on each other":

```
project/
├── docker-compose-langfuse.yaml   infra: Postgres, ClickHouse, Redis, MinIO, Langfuse v4, Grafana
│                                   -> network "langfuse-infra" (declared here, external:true below)
└── HACKATHON_1/
    ├── docker-compose.yml         app: hackathon1-app, joins "langfuse-infra" as external
    ├── Dockerfile
    └── src/hackathon1/            this service
```

The app reaches Postgres/ClickHouse/Redis/MinIO/Langfuse purely via that shared Docker network and their Compose service DNS names (`langfuse-web`, `minio`, ...) -- every backend port in the infra file is bound to `127.0.0.1` on the *host*, so this is the only way a separate container can reach them (no nginx/load balancer needed).

### The graph

```
START -> classify_ticket
           -> route_after_classify
                -> "orchestrator" (complex tickets):
                     orchestrator -> dispatch_sections (Send fan-out)
                       -> investigate_section x N (parallel, async)
                            -> synthesize_incident_report (fan-in, uploads report to MinIO) -> END
                -> "supervisor" (simple tickets):
                     supervisor (Command) -> billing_agent / tech_agent / account_agent
                       -> Command(goto=<other specialist, at most once> | END)
```

Every node is `async def`, invoked only via `await app_graph.ainvoke(...)`.

## Setup

```bash
cp .env.example .env   # fill in Azure OpenAI / Langfuse / MinIO values
uv sync
uv run pytest -v
```

## Local dev (no Docker)

```bash
uv run uvicorn hackathon1.service:app --reload
```
With no infra stack running, `tracing.py` and `storage.py` both degrade gracefully (tracing silently disabled, report upload skipped with a `WARNING` log) rather than failing requests.

## Docker deploy

From `project/`:
```bash
bash HACKATHON_1/scripts/deploy.sh
```
Brings up the infra stack first (if not already healthy), then this app -- see `scripts/deploy.sh` for why that ordering can't just be `depends_on` in one compose file.

Every run writes its own numbered, timestamped log to `logs/run-NNNN-<timestamp>.log` (mirrored to the terminal at the same time via `tee`) -- nothing is ever overwritten or deleted, so past runs stay available for comparison. `logs/` is gitignored, same convention as the project-root `logs/` folder.

## Smoke test

```bash
uv run python -m hackathon1.apiclient
```
Adapted as-is from `day21/src/day21/apiclient.py` (only the port changed) per the explicit "use as is, don't define as a pytest test" instruction -- no timeout/retry/error handling, deliberately.

## URLs

| Service | URL |
|---|---|
| App | http://localhost:8010 |
| Langfuse (traces) | http://localhost:3000 |
| Grafana (dashboards) | http://localhost:3001 |

See `stack-guide.md` and `troubleshooting-guide.md` for infra-level setup/errors.

## Known limitations / deferred work

Per the approved plan, everything below is intentional and deferred, not accidentally missing:

- **No Docker resource limits yet** -- sized recommendations exist (`postgres` 256m, `clickhouse` 1g, `redis` 128m + `maxmemory`, `minio` 256m, `langfuse-worker`/`langfuse-web` 512m each, `grafana` 256m, `hackathon1-app` 512m) but aren't applied; validate with `docker stats` first.
- **No machine-capability-aware deployment** (laptop vs. dev-machine resource profile) yet.
- **Dockerfile runs as root** -- add a non-root user before anything production-facing.
- `grafana/grafana-enterprise:latest` and MinIO's untagged image in the infra file aren't pinned yet.
- **`MemorySaver` checkpointing** (in-memory, not durable across restarts) -- fine for this skeleton phase, a `PostgresSaver` swap is a later concern.
- Not doing: full `.yaml`->`.yml` normalization of the existing infra file lineage, retiring `docker-compose.v3.yaml`, fixing the unrelated broken root-level `docker-compose.yml` stub, or any CI/CD pipeline (`HACKATHON1.md` itself: "no need for more actions").
- `apiclient.py` stays unhardened (no timeout/retry/exception handling) -- intentional, per the "use as is" instruction.
