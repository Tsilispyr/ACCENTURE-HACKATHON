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
| **B** | Infra & Delivery | 1 / 8 | B2 landed and verified |
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
- [ ] **B1 · Git** — root `.gitignore` first, then clone `dev-pipis`, copy tree, review
      `git status` **before** `git add`, commit, push. Verify `git ls-files "*.env"` is empty.
      Also delete the stray empty `HACKATHON_1/HACKATHON_1/readme.md`.
- [ ] **B3 · Machine-capability preflight + resource profiles** — `scripts/preflight.sh` reads
      `/proc/meminfo`, gates on **`MemTotal` not `MemAvailable`** (proposed X = 8 GB), selects
      `lean` / `full`, applied as a compose override layer. `lean` drops Grafana via a
      `profiles:` tag and staggers ClickHouse startup. Also: interactive credential prompt
      (the "window") that fills blanks in `.env` once.
- [ ] **B4 · Dockerfile hardening** — pin `uv:latest` to a version, add a non-root user.
      Keep the two-layer cache split exactly as it is.
- [ ] **B5 · Compose hardening** — pin `grafana-enterprise:latest` and the untagged MinIO image;
      repoint app healthcheck to `/health` once A3 lands; commented-out `frontend:` slot.
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
```

| Service | URL |
|---|---|
| App | http://localhost:8010 |
| Langfuse | http://localhost:3000 |
| Grafana | http://localhost:3001 |
| MinIO console | http://localhost:9091 |
