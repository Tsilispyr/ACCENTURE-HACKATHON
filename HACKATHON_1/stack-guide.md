# Observability Stack - Setup, Architecture & Decision Log

The Day 4 local stack (Langfuse + Grafana + Postgres): what it is, how to run it, why it is built this way, and how it ended up here. Written 2026-09-03.

Companion documents:

| File | Purpose |
|---|---|
| [installation-guide.md](installation-guide.md) | The trainer's original walkthrough - connecting Grafana to Langfuse and building dashboards (Greek) |
| [troubleshooting-guide.md](troubleshooting-guide.md) | Error → cause → fix, searchable by the actual error text |
| **stack-guide.md** (this file) | The whole picture: architecture, decisions, and why the constraints are what they are |

---

## 1. Quick start

```bash
cd /mnt/d/accenture-train-program/project
docker compose up -d
bash scripts/stack-check.sh     # optional: health verdict
```

| Service | URL | Credentials |
|---|---|---|
| Langfuse | http://localhost:3000 | `user@example.com` / `12345678` |
| Grafana | http://localhost:3001 | `gtgh` / `ghgt#C0d3` |
| Postgres | `localhost:5432` | `gtgh` / `ghgt#C0d3`, database `langfuse` |

Grafana's "Langfuse PostgreSQL" data source is already configured on first boot - no manual setup needed.

---

## 2. The stack

Four services, defined in [docker-compose.yaml](docker-compose.yaml):

| Service | Image | Role |
|---|---|---|
| `db` | `postgres:16-alpine` | Single source of truth: Langfuse metadata **and** traces, plus Grafana's own state |
| `db-init` | `postgres:16-alpine` | One-shot, idempotent: creates the separate `grafana` database, then exits |
| `langfuse` | `langfuse/langfuse:2` | Tracing UI + API |
| `grafana` | `grafana/grafana-enterprise` | Dashboards over the Langfuse database |
| `log-collector` | `docker:cli` | ~30 MB sidecar that writes all logs to `./logs/` on the host |

Ports 3000 (Langfuse), 3001 (Grafana), 5432 (Postgres).

---

## 3. Which compose file runs, and why

Compose auto-discovers a file in the working directory in a fixed order, first match wins:

1. `compose.yaml`
2. `compose.yml`
3. **`docker-compose.yaml`** ← the active file
4. `docker-compose.yml`

`docker-compose.v3.yaml` is **not** in that list - a different filename is never auto-loaded. It is parked deliberately and only runs when named:

```bash
docker compose up -d                                # v2 light stack (default)
docker compose -f docker-compose.v3.yaml up -d      # heavy v3 stack
docker compose ls                                   # shows which file a running stack came from
```

**Both files share the project name `project`** (taken from the folder name), so they share containers, network and volume namespace. Consequences:

- `docker compose down` removes **every** container in the project, whichever file declared it. That is how switching from v3 back to v2 cleaned up ClickHouse/Redis/MinIO/worker.
- Starting one while the other's containers run produces a "Found orphan containers" warning; use `docker compose up -d --remove-orphans`.
- They cannot run simultaneously - ports 3000/3001/5432 would collide.

To pin the choice explicitly instead of relying on filename order, add `COMPOSE_FILE=docker-compose.yaml` to `project/.env`.

---

## 4. Persistence design - everything in Postgres

The design goal: **the setup must survive rebuilds, version switches and different machines, with no application volumes.** Only one named volume exists (`postgres_data`).

### 4.1 Langfuse - natively in Postgres

Langfuse stores users, organisations, projects and API keys in Postgres. On **v2** it stores traces there too (this matters - see §6).

To make that state reproducible rather than merely persistent, the stack **seeds itself** using Langfuse's headless bootstrap. Values live in `project/.env` (gitignored), documented by the committed [.env.example](.env.example):

```ini
LANGFUSE_INIT_ORG_ID / _ORG_NAME
LANGFUSE_INIT_PROJECT_ID / _PROJECT_NAME
LANGFUSE_INIT_USER_EMAIL / _USER_NAME / _USER_PASSWORD
LANGFUSE_INIT_PROJECT_PUBLIC_KEY / _SECRET_KEY   # must match day04/.env
```

On **any fresh database**, Langfuse recreates that exact org, project, login and - critically - those exact **API keys**. So `day04/.env` never needs updating after a rebuild. On an already-seeded database the variables are a no-op.

> This is why a sign-in failure earlier was not a bug: the v3 stack ran against a *different, empty* database (`postgres_data_v3`), where the account had never been created.

### 4.2 Grafana - moved off SQLite onto Postgres

By default Grafana keeps data sources, dashboards and users in **SQLite inside the container** (`/var/lib/grafana/grafana.db`). With no volume, every `docker compose up` that recreated the container destroyed it - login still worked (the admin user comes from env vars) but every configured data source vanished.

Fixed with a Postgres backend:

```yaml
GF_DATABASE_TYPE: postgres
GF_DATABASE_HOST: db:5432
GF_DATABASE_NAME: grafana
GF_DATABASE_USER: gtgh
GF_DATABASE_PASSWORD: "ghgt#C0d3"
GF_DATABASE_SSL_MODE: disable
```

Grafana needs its **own** database, and `POSTGRES_DB` only creates one, only on a first-time init of an empty data directory. Hence the `db-init` one-shot service, which creates `grafana` only if absent and is safe to re-run on every start.

### 4.3 Data source as code

[grafana/provisioning/datasources/langfuse.yaml](grafana/provisioning/datasources/langfuse.yaml) is bind-mounted read-only into Grafana, which reads it on every start. It replaces Part 1 of the trainer's installation guide and bakes in the two settings that are easy to get wrong:

- `sslmode: disable` - the `postgres:16-alpine` image has no server certificate and refuses TLS.
- `postgresVersion: 1600` - Grafana defaults to 9.3, which is wrong for a Postgres 16 server.

This is config, not state: the bind mount is not a persistence volume.

### 4.4 Volumes that exist

| Volume | Contents | Status |
|---|---|---|
| `project_postgres_data` | v2 database - the live one | **In use** |
| `project_postgres_data_v3` | v3 database from the upgrade attempt | Preserved, idle |
| `project_langfuse_clickhouse_data` / `_logs` | v3 traces | Preserved, idle |
| `project_langfuse_minio_data` | v3 event blobs | Preserved, idle |

Only `postgres_data` is declared in the active compose file. The others are deliberately left undeclared so **`docker compose down -v` cannot delete them** - rollback to v3 stays possible.

---

## 5. Logging

Two mechanisms, both automatic - no script to remember.

**Rotation on every service** (`json-file`, 5 × 10 MB), so `docker compose logs <service>` always has recent history and logs cannot fill the disk.

**The `log-collector` sidecar** runs `docker compose logs -f` against the project through a read-only Docker socket and appends everything to `logs/stack-YYYY-MM-DD.log` on the host, rolling the file at 50 MB. Because the file lives outside the containers it **survives `docker compose down`**, which otherwise deletes all container logs. It uses `--tail 0`, so it records from the moment it attaches and reconnects without duplicating history.

`logs/` is gitignored.

Three optional helpers remain in [scripts/](scripts/):

```bash
bash scripts/stack-check.sh     # status, exit/OOM codes, memory, endpoint probes, recent errors
bash scripts/stack-logs.sh      # per-service snapshot into logs/<timestamp>/ + summary.txt
bash scripts/stack-follow.sh    # manual follow (superseded by the collector)
```

**Reading exit codes**, which `stack-check.sh` prints:

| Code | Meaning |
|---|---|
| `0` | clean stop |
| `143` | SIGTERM - graceful shutdown |
| `137` + `oom=false` | SIGKILL after a stop exceeded the 10s grace period |
| `137` + `oom=true` | kernel killed it for memory |

---

## 6. The core constraint: three versions must agree

This is the single most important thing to understand about this stack. Three independent version axes interact, and only certain combinations work.

| Langfuse server | Python SDK | LangChain import | Works with LangChain 1.x? | Traces stored in |
|---|---|---|---|---|
| `langfuse:2` | `langfuse<3` | `from langfuse.callback import CallbackHandler` | **No** - needs LangChain 0.x | Postgres |
| `langfuse:3` / `:4` | `langfuse>=4` | `from langfuse.langchain import CallbackHandler` | Yes | ClickHouse |

**Why the v2 SDK cannot work here:** its LangChain integration does `from langchain.callbacks.base import ...` and `from langchain.schema.*` - the LangChain **0.x** layout. This project runs `langchain` **1.3.18**, whose submodules are `agents, chat_models, embeddings, messages, rate_limiters, tools`; there is no `langchain.callbacks` (callbacks moved to `langchain_core.callbacks`). langfuse 2.x declares only `langchain (>=0.0.309)`, an unbounded pin written before LangChain 1.x existed, so installation succeeds and the import then fails with a misleading `Please install langchain` message.

**Why the v4 SDK cannot target a v2 server:** it ships traces over OpenTelemetry to `POST /api/public/otel/v1/traces`, an endpoint introduced in server v3. Verified directly: that path returns `401` on v3 (exists, needs auth) and does not exist on v2. The failure is **silent** - the script runs, the model answers, and nothing appears in the UI.

**Why traces location matters:** Grafana's SQL dashboards read a `traces` table in Postgres. That table only exists on **v2**. On v3 the dashboards connect fine and return nothing.

---

## 7. How we got here - decision log

1. **Started on `langfuse:2`** (the trainer's stack) with Grafana reading the Postgres `traces` table.
2. **`docker-compose` v1 crashed** with `Not supported URL scheme http+docker` - the apt Python v1 (1.29.2) is incompatible with `requests` ≥ 2.32. Fixed by using `docker compose` (v2 plugin).
3. **Langfuse could not reach Postgres** - the password `ghgt#C0d3` was inlined raw into `DATABASE_URL`, where `#` starts a URL fragment, silently truncating the password to `ghgt`. Fixed by percent-encoding as `%23` (only inside the URL; the raw `#` stays everywhere else).
4. **Grafana data source failed** twice over: SSL mode `require` against a server with no certificate, and the percent-encoded password pasted into a plain form field. Fixed with `disable` and the raw password.
5. **Python tracing crashed** with `cannot import name 'RunTree' from 'langsmith'` - a local `langsmith.py` shadowed the installed package. Renamed.
6. **First attempt at the SDK mismatch was wrong.** Downgrading to `langfuse<3` to match the v2 server failed on the LangChain 0.x/1.x incompatibility in §6. The server, not the SDK, was the piece that had to move.
7. **Upgraded to Langfuse v3** - a 7-service stack (ClickHouse, Redis, MinIO, worker). It migrated and ran, but the machine could not sustain it: 30-second Redis stalls, Postgres `canceling authentication due to timeout`, and finally `dependency failed to start: container langfuse-clickhouse is unhealthy`. ClickHouse had not crashed (`status=running exit=0 oom=false restarts=0`) - it simply booted slower than its healthcheck window under memory pressure. **Host RAM 7.4 GB, WSL 3.6 GB, ~744 MiB free under load.**
8. **Reverted to the v2 light stack**, preserving v3 verbatim as `docker-compose.v3.yaml`, and reused the original `postgres_data` volume so the existing account and keys came back.
9. **Made state consistent** - Grafana onto Postgres, data source provisioned from the repo, Langfuse self-seeding via `LANGFUSE_INIT_*`.

---

## 8. What works today, and what does not

**Working:** Langfuse UI, Grafana with its provisioned data source, Postgres holding all state, automatic log capture, both stacks validating (`docker compose config`).

**Not working: Python tracing to the local server.** `day04` currently has `langfuse` **4.15.1** installed with `from langfuse.langchain import CallbackHandler`, which cannot ingest into a v2 server. Two ways forward - pick by what you need to demonstrate:

**A. Grafana dashboards with real data (fully local)**

```bash
uv add "langfuse<3"        # v2 SDK, matches the v2 server
```

Trace with the `@observe` decorator or the `langfuse.openai` drop-in rather than the LangChain callback - those paths live in `langfuse.decorators` and do not import langchain, so the LangChain 1.x conflict does not apply. Traces land in Postgres, so both the Langfuse UI **and** Grafana's `FROM traces` panels have data. You lose automatic LangChain/LangGraph span capture. [day04/src/day04/observe.py](day04/src/day04/observe.py) is presumably this exercise.

**B. LangChain/LangGraph auto-tracing (Langfuse Cloud)**

Keep `langfuse>=4` and the current import; change only the host and keys in `day04/.env`:

```ini
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Costs nothing locally. Traces appear in the cloud UI; Grafana's local dashboards stay empty.

Running the v3 stack locally is a third option, but §7 step 7 is the evidence against it on this hardware.

---

## 9. Environment notes

**Memory is the binding constraint.** Host 7.4 GB; WSL takes ~50% (3.6 GB) with no `.wslconfig`. The v2 stack needs a few hundred MB; v3 needs ~3-4.5 GB. To give WSL more headroom, create `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=5GB
swap=4GB
processors=4
```

then `wsl --shutdown`. Note this kills every container, and 5 GB of 7.4 GB leaves Windows tight. Stopping unrelated containers is the cheaper lever: `docker stop d_mytb_1 minio rabbitmq`.

**Windows vs WSL.** Docker runs in WSL; Python runs from PowerShell against a Windows venv (`.venv/Scripts/python.exe`), which **cannot** be used from WSL (which needs `.venv/bin/python`). `pyproject.toml` and `uv.lock` are cross-platform, so `uv sync` from WSL rebuilds it for Linux - but that replaces the Windows venv, so pick one side. Files under `D:\...` and `/mnt/d/...` are the same files.

**Ports already taken on this machine** by an unrelated project: 9000/9001 (another MinIO), 8080/1883/7070/5683-5688 (ThingsBoard). The v3 stack works around this by publishing MinIO on 9090/9091 and not publishing ClickHouse's native 9000 at all.

---

## 10. File inventory

```
project/
├─ docker-compose.yaml              # ACTIVE: v2 light stack
├─ docker-compose.v3.yaml           # parked: v3 heavy stack, same credentials/seeding
├─ .env                             # gitignored: LANGFUSE_INIT_* secrets
├─ .env.example                     # committed template
├─ .gitignore                       # logs/, .env, __pycache__, .venv
├─ grafana/provisioning/datasources/
│  └─ langfuse.yaml                 # data source as code
├─ scripts/
│  ├─ stack-check.sh                # health verdict
│  ├─ stack-logs.sh                 # log snapshot
│  └─ stack-follow.sh               # manual follow
├─ logs/                            # gitignored; written by log-collector
├─ installation-guide.md            # trainer's Grafana walkthrough
├─ troubleshooting-guide.md         # error → fix reference
└─ stack-guide.md                   # this file
```

---

## 11. Open items

- **Secrets in the repo.** `docker-compose.yaml`, `installation-guide.md` and this file contain the local Postgres/Grafana password, and `day02/.env` / `day03/.env` hold real Azure and Google keys with no `.gitignore` covering them historically. The root `.gitignore` now ignores `.env`, but **gitignore does not untrack already-tracked files** - check `git ls-files "*.env"` and use `git rm --cached` if anything shows up.
- **Grafana's data source user** is `gtgh`, which owns the Langfuse schema. Grafana warns that a careless dashboard query could drop tables. A read-only Postgres user is the safe version.
- **`ENCRYPTION_KEY` is 64 zeros** - fine locally, never acceptable beyond it.
- **Decide between option A and B** in §8 before the Day 4 exercises.
