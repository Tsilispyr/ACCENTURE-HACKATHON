# Roles 1 + 5 — Task Board

Integration / Docker / API / Deployment. Living checklist — tick boxes as they land.
Full reasoning lives in the approved plan; this file is the status view.

**Branch:** `dev-pipis` (we commit and push here only; other branches are pulled to
compare and take from selectively, never merged into by us).

**Split:** **A = Runtime & API** (inside the container, `src/hackathon1/`) ·
**B = Infra & Delivery** (outside it). Zero file overlap.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done & verified · `[!]` blocked

---

## Status at a glance

| | Workstream | Done | Notes |
|---|---|---|---|
| **A** | Runtime & API | 0 / 7 | not started |
| **B** | Infra & Delivery | 3 / 8 | B1, B2, B3 landed and verified |
| **C** | Integration | 0 / 1 | waits on roles 2–4 |

---

## B — Infra & Delivery

- [x] **B2 · One `.env`, filled once + the missing credentials** ← *biggest scoring win, done*
  - Added `LANGFUSE_PUBLIC_KEY` / `_SECRET_KEY` / `MINIO_ACCESS_KEY` / `_SECRET_KEY` to `.env`.
    They were **absent**, so `tracing.py` returned `[]` and `storage.py` returned `None` —
    Langfuse tracing and MinIO upload were **silently dead**. Both now work (see Verified below).
  - Wrote committed `HACKATHON_1/.env.example`: only the 4 `AZURE_*` keys are blank.
  - Changed `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `_SECRET_KEY` in `docker-compose-langfuse.yaml`
    to match `.env`, so a **fresh database seeds the same keys** and every machine behaves
    identically. Rendered-config diff showed exactly those 2 lines and nothing else.
  - Dropped unused `GOOGLE_API_KEY` (nothing in the codebase reads it).
- [x] **B1 · Git**
  - Repo was already initialised on `dev-pipis` tracking origin. Added the missing **root**
    `.gitignore` (`.env`, `*.env`, `.env.*`, `!.env.example`, `logs/`, ...) so a stray env file
    outside `HACKATHON_1/` cannot leak a key. Verified `git ls-files "*.env"` is empty.
  - Note: your global git email is blocked by GitHub's email-privacy setting, so this repo has a
    **local** `user.email` matching the identity already in its history
    (`Tsilispyr <spyrostsl456@gmail.com>`). Local only — your global config is untouched.
  - The handout PDF is deliberately left **untracked** — say if you want it committed.
- [x] **B3 · Machine-capability preflight + resource profiles**
  - `scripts/preflight.sh` — two jobs. (1) **Credentials**: creates `.env` from `.env.example`,
    prompts once for any blank required key (hidden input for the API key), writes it back;
    non-interactive runs fail loudly naming the missing keys. (2) **Capability**: gates on
    **`MemTotal`, not `MemAvailable`** (threshold `MEM_THRESHOLD_GB`, default 8) and picks
    `lean` / `full`. Detects WSL and tells you to raise `.wslconfig` rather than silently
    degrading a big host with a small slice.
  - `compose/{infra,app}.{lean,full}.yaml` — memory ceilings + relaxed health `start_period`,
    applied as **override layers**; the base compose file is not modified.
  - `scripts/deploy.sh` — sources preflight, layers the profile, stops a stale Grafana on lean,
    and now **converges every run** (an earlier version skipped `up` when the stack was already
    healthy, which printed the profile and applied nothing).
  - **Fail-fast health gate** replaces the old fixed wait: polls real state and aborts the moment
    a service is OOM-killed, exits, or restart-loops, instead of sitting out the whole timeout.
    Timeout is profile-aware (lean 420s / full 180s) because Langfuse v4 needs ~2 min to boot here.
  - Verified: full deploy run, 7/7 healthy, 0 restarts, 0 OOM; forced `full` profile renders and
    validates; all three credential paths tested (missing `.env`, blank keys, complete).
- [ ] **B4 · Dockerfile hardening** — pin `uv:latest` to a version, add a non-root user.
      Keep the two-layer cache split exactly as it is.
- [ ] **B5 · Compose hardening** — pin `grafana-enterprise:latest` and the untagged MinIO image;
      repoint app healthcheck to `/health` once A3 lands; commented-out `frontend:` slot.
      (Memory limits already live in B3's profile overrides, not inline.)
- [ ] **B6 · Grafana dashboard (§7 bonus)** — provisioning-as-code, datasource → **ClickHouse**.
      ⚠ **Query `events_core` / `events_full`, not `traces`** (see Verified below).
      Needs `GF_INSTALL_PLUGINS=grafana-clickhouse-datasource`. Do last; skipped on `lean`.
- [ ] **B7 · `scripts/verify.sh`** — the demo script. Domain-agnostic: payload from
      `scripts/scenarios/*.json`, expected field names in a list at the top.
      ⚠ Cannot use `/api/public/traces` (404 in v4 events_only) — assert via ClickHouse.
- [ ] **B8 · Docs** — `architecture/` (required by §13, missing); README rewrite;
      mark `stack-guide.md` historical (it still describes the old v2 stack).

## A — Runtime & API

- [ ] **A4 · `schemas.py`** ← *do this first.* §2's mandated output fields, every one populated
      via `state.get(key, default)` in **one** mapping function. The shock absorber for roles
      2–4's renames.
- [ ] **A1 · `config.py`** — centralize env reads. Plain `os.getenv`; do **not** add
      `pydantic-settings`.
- [ ] **A7 · Make degradation visible** — one WARNING per disabled subsystem at startup +
      the same booleans in `/health`. This is what would have caught B2's outage on day one.
- [ ] **A5 · CORS** — one-liner, origins from env; the frontend is coming.
- [ ] **A2 · Durable checkpointing** — `AsyncPostgresSaver` on the **existing** `hackathon1`
      database (`app-db-init` already creates it). No new container. Falls back to
      `MemorySaver` with a loud warning.
- [ ] **A3 · API surface (§8)** — `GET /health`, `POST /incidents`, `GET /incidents/{id}`,
      `POST /incidents/{id}/approve`. Keep `GET /` as-is so the healthcheck keeps passing.
      **Convention: `thread_id == incident_id`.**
- [ ] **A6 · Trace naming** — `run_name=f"incident-{id}"` + metadata so a judge can find one
      incident's trace.

## C — Integration (after roles 2–4 land)

- [ ] **C1 · Integration pass** — fetch their branch, diff, take selectively; reconcile A4's
      mapping with the final state keys; wire A3's approve to the real `interrupt()`; then
      update the **five places a rename leaks into together**: `.env.example`, `architecture/`,
      `README.md`, `apiclient.py`'s payload, `verify.sh`'s scenario JSON + field list.

---

## Verified facts (re-check if the stack changes)

- 8 containers healthy; app on `:8010`; deploys run **from WSL** (`docker` is not on the
  Git-Bash PATH). WSL: 3.6 GB total, ~550 MB free — the constraint behind B3.
- **Langfuse v4.32.0 runs in `events_only` mode.** Consequences:
  - `GET /api/public/traces` returns **404** — do not build B7 on it.
  - ClickHouse `traces` / `observations` / `analytics_*` are **all 0 rows**.
    Ingested data lands in **`events_core` / `events_full`** — that is what B6 must query.
- Both Langfuse key pairs (yours and the previously-seeded one) authenticate to the same
  project `hackathon1` / org `GTGH`. `.env` and the compose INIT vars now agree.
- MinIO needs no setup: keys are `minio` / `miniopassQWqw!@12` (already in the infra compose),
  and `storage.py` auto-creates the bucket on first upload.
- **Putting a Node service under `mem_limit` silently caps its V8 heap.** Adding
  `mem_limit: 896m` to `langfuse-web` put it in a restart loop: V8 derives its heap ceiling from
  the *cgroup*, capped old-space at ~450MB, and died with
  `FATAL ERROR: Reached heap limit` — while reporting **`OOMKilled=false`, `ExitCode=0`**, so it
  looked nothing like a memory problem. Fixed by setting `NODE_OPTIONS=--max-old-space-size`
  explicitly so the heap is decoupled from the container limit. This is why the deploy health
  gate checks `RestartCount` and not just `OOMKilled`.
- **Measured usage beats the README's estimates.** `docker stats` on a live stack:
  clickhouse 834Mi · langfuse-web 633Mi · langfuse-worker 438Mi · grafana 256Mi · minio 118Mi ·
  postgres 61Mi · redis 14Mi · app 168Mi. The README's proposed `langfuse-web 512m` and
  `grafana 256m` are **below** those readings and would be OOM-killed. Every limit in
  `compose/` sits above measured usage.
- **Do not set Redis `--maxmemory`.** The base file uses `--maxmemory-policy noeviction`, so a cap
  would make Langfuse's queue writes *fail* rather than evict. Redis uses ~15Mi; the container
  ceiling is enough.

## Do not touch

- Service credentials and the service / port / volume / healthcheck **topology** of the infra
  compose — the `LANGFUSE_INIT_PROJECT_*` keys in B2 were the one justified exception.
- Named volumes. **Never `docker compose down -v`** — the seeded org, project and API keys
  live in `langfuse_postgres_data`, and `.env` depends on them.
- The Dockerfile's two-layer cache split.
- `apiclient.py`'s deliberate lack of timeout/retry handling (only its payload changes, in C1).
- `graph.py` / `tools.py` internals — roles 2–4's.

## How to check

```bash
# from WSL
cd /mnt/c/projects/ACCENTURE-HACKATHON-main/HACKATHON_1

docker ps                                    # 8 healthy
bash scripts/deploy.sh                       # ordered infra -> app, numbered log in logs/
uv run pytest -v                             # 15 tests

# tracing landed? (events_only mode -- not the traces table)
docker exec langfuse-clickhouse clickhouse-client --user clickhouse \
  --password 'clickpassQWqw!@12' --query "SELECT count() FROM default.events_core"

# report uploaded?
docker exec langfuse-minio ls /data/hackathon1-reports

# before ANY compose edit, capture and diff the rendered config
docker compose -f docker-compose-langfuse.yaml config > /tmp/before.yaml

# force the other resource profile (this machine only ever picks lean)
MEM_THRESHOLD_GB=1 bash scripts/preflight.sh      # -> full
docker compose -f docker-compose-langfuse.yaml -f compose/infra.full.yaml config

# restart Grafana after a lean deploy stopped it
docker start grafana-app
```

| Service | URL |
|---|---|
| App | http://localhost:8010 |
| Langfuse | http://localhost:3000 |
| Grafana | http://localhost:3001 |
| MinIO console | http://localhost:9091 |
