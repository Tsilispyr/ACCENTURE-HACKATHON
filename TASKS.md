# Roles 1 + 5 — Task Board

Integration / Docker / API / Deployment. Living checklist — tick boxes as they land.
Full reasoning lives in the approved plan; this file is the status view.

**Branch:** `dev-pipis` (we commit and push here only; other branches are pulled to
compare and take from selectively, never merged into by us).

**Split:** **A = Runtime & API** (inside the container, `src/hackathon1/`) ·
**B = Infra & Delivery** (outside it). Zero file overlap.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done & verified · `[!]` blocked

> **Running it on Windows:** use `.\scripts\deploy.ps1` from PowerShell or cmd, or
> `bash scripts/deploy.sh` from WSL. Do *not* run the bash script from PowerShell — Docker lives
> in WSL and is not on the Windows PATH, and a `sh` invocation fails with a misleading `pipefail`
> error.

**Start here:** [`HACKATHON_1/architecture/infrastructure.md`](HACKATHON_1/architecture/infrastructure.md)
— what every file does, how to use `scripts/` and `compose/`, and the traps.

### Which document is which

| Document | Where | Role |
|---|---|---|
| **`TASKS.md`** (this file) | in the repo | **Canonical.** Live status, shared with the team, versioned with the code. Update this one. nn|
| `architecture/infrastructure.md` | in the repo | Reference manual — what each file does, how to run things. |
| `~/.claude/plans/this-project-folder-...md` | **local only, not in the repo** | The original approved design and its reasoning. A frozen snapshot; your coworker cannot see it. |

If the two ever disagree, this file wins — it is the one that gets committed.

---

## Do I need to fill in `.env`?

**On this machine: no.** `HACKATHON_1/.env` already exists with everything filled.

**For your coworker, or any fresh clone: yes, but only four values.** `.env` is gitignored, so it
is *not* in the repo — a clone has only `.env.example`. `scripts/preflight.sh` copies the example
to `.env` automatically and prompts once for the four `AZURE_*` values (API-key input is hidden),
then never asks again. Everything else — Langfuse keys, MinIO keys, endpoints — ships pre-filled
in `.env.example` and needs no input.

Note the file lives at **`HACKATHON_1/.env`**, not the repo root. There is a root `.gitignore`
covering stray `.env` files elsewhere, but nothing reads one from the root.

Verified by cloning the repo fresh: no `.env` and no Azure key present, preflight created `.env`
and named exactly the four missing keys.

---

## How the resource profile behaves on another machine

`scripts/preflight.sh` reads `MemTotal` and picks one profile. Nothing is per-container-adaptive;
it is one binary choice, which is what makes it predictable.

| Machine total RAM | Profile | What happens |
|---|---|---|
| **< 8 GB** (this one, 3.6 GB in WSL) | `lean` | Tight ceilings, bounded Node heaps, **Grafana not started** |
| **>= 8 GB** | `full` | Generous ceilings, everything starts including Grafana |

Change the cut-off with `MEM_THRESHOLD_GB=16 bash scripts/deploy.sh`.

**Nobody needs to set `MEM_THRESHOLD_GB`.** It defaults to 8 and the profile is chosen
automatically — a coworker just runs `bash scripts/deploy.sh`. The variable exists only to
*override* the cut-off, e.g. to force-test the `full` path on a small machine.

**On Windows, the script sees the WSL slice, not the physical machine.** WSL2 defaults to about
half the host's RAM (measured here: 7.4 GB host -> 3.58 GB in WSL, 49%). So in practice:

| Windows laptop | WSL sees | Profile |
|---|---|---|
| 8 GB | ~4 GB | `lean` |
| 12 GB | ~6 GB | `lean` |
| **16 GB** | ~8 GB | **`full`** |

That is correct rather than a bug — the ceilings govern what Docker can actually use, which *is*
the WSL slice — but it means `full` effectively needs a ~16 GB Windows machine. On native Linux
there is no halving, so 8 GB of real RAM gets `full`. A coworker who wants `full` on a smaller
Windows box should raise the WSL allocation in `.wslconfig` (preflight prints the exact snippet)
rather than lower the threshold.

It gates on **total**, not free, RAM on purpose: free memory swings with whatever else is open, so
a capable machine that happens to be busy would otherwise get crippled. On WSL it also warns when
a large Windows host has been given a small slice, pointing at `.wslconfig` rather than silently
degrading it.

> **Caveat, and it is a real one:** the `full` profile has only ever been *rendered and validated*,
> never actually run — this machine can only ever select `lean`. Before relying on it, force it on
> a bigger box: `MEM_THRESHOLD_GB=1 bash scripts/deploy.sh`.

### What lean costs you

**Only Grafana**, and today that is nothing at all — Grafana has no provisioned datasource yet, so
it would start blank, and it is a bonus item rather than a mandatory requirement. Verified working
on `lean`: Langfuse tracing (events reach ClickHouse), MinIO report storage, the app and API, and
all six other infra services healthy.

Get it back on demand when the dashboards exist: `docker start grafana-app` (~256 MiB),
`docker stop grafana-app` to release it again.

The two other differences are not functional: smaller Node heaps (marginally more GC under heavy
ingestion) and a longer health `start_period` (Docker waits longer before calling a probe failed —
it does not slow anything down). `lean` does **not** disable tracing, drop spans, or degrade the
workflow. Full detail: `architecture/infrastructure.md` section 5.

---

## Status at a glance

| | Workstream | Done | Notes |
|---|---|---|---|
| **A** | Runtime & API | 0 / 7 | not started |
| **B** | Infra & Delivery | 3.5 / 8 | B1, B2, B3 done; B8 docs part-written |
| | | | *plus: LF line endings, pip exports, infrastructure guide* |
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
- [~] **B8 · Docs** — `architecture/infrastructure.md` **written**: file-by-file reference, how to
      use `scripts/` and `compose/`, a cookbook, and the gotchas (Node heap trap, events_only
      mode, never `down -v`). Still to do: README rewrite for the incident domain, and marking
      `stack-guide.md` historical in its own front matter.
- [x] **Packaging: pip fallback** — `requirements.txt` / `requirements-dev.txt` exported from
      `uv.lock` (with hashes). `uv.lock` stays the source of truth; these are a convenience so
      pip users are not blocked. Note the Docker path never needed `uv` installed — the
      Dockerfile pulls the binary from a registry image, so `deploy.sh` works with only Docker.
      Regenerate with the `uv export` commands in `architecture/infrastructure.md` section 3.

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

## Integration status (branches merged into dev-pipis)

| Branch | Brought in | State |
|---|---|---|
| `tools-features` | `tools.py` (8 tools, 3 risk tiers), `world.py`, `models.py`, `data/scenarios.json` | merged, tested |
| `feature/langgraph-workflow` | incident graph with `interrupt()` HITL, `IncidentAdapter` Protocol, workflow tests | merged, tested |
| `testing-suite` | top-level `tests/`, API contract suite, workflow tests, fixtures, `data/simulated/` request examples | merged, placeholders fixed |
| `maria/containerization` | `GET /health`, `GET /incidents/{id}`, postgres host port 5433, `DATABASE_URL` fix | merged, conflict resolved |

**Written here to close the gap between them:** `adapters.py` — `LiveIncidentAdapter`, the binding
neither branch could write alone. `graph.py` talks to a Protocol and imports no tools; `tools.py`
knows nothing about the graph. Tools produce facts, the LLM supplies judgement, and policy
(`tools.effective_risk`) owns safety — the model may raise the risk level, never lower it.

### Testing suite: placeholders resolved

**70 passed, 10 skipped, 0 failed.** What was fixed, and why each mattered:

| Placeholder | Reality | Effect |
|---|---|---|
| `conftest.mock_llm` patched `graph.get_llm` | no such function; modules do `from hackathon1.llm import llm` | autouse fixture → **all 71 tests errored at setup** |
| offline guard blocked every `socket.connect` | Windows asyncio builds its self-pipe on 127.0.0.1 | **64 errors**; now blocks non-loopback only |
| `scenarios` fixture did `[Scenario(**i) for i in raw]` | `incidents.json` is a mapping; iterating yields keys | `**` on a str; now returns the dict, still validated |
| `service.get_chat_agent` expected | agent was built at import time | now `lru_cache`d and lazy — importing `service` no longer builds an agent |
| `tool_interfaces.py` guessed the tool API | wrong: async (they are sync), `get_service_metrics` returns a mapping not a list, no single `execute_remediation` | rewritten; **all 8 signatures machine-verified against `tools.py`** |
| five-field request `xfail(strict=True)` | API took the old 3 lowercase fields | implemented `Incident ID / Service / Severity / Description / Error`; scaffolding removed per its own instruction |

`data/simulated/` (API request examples) and `src/hackathon1/data/scenarios.json` (world simulation
ground truth) are **not** duplicates — different purposes, both needed.

### Still open

- [ ] **`POST /incidents` still runs the OLD CodeHub graph.** `service.py` calls `app_graph`; the
      incident workflow and `LiveIncidentAdapter` exist but nothing wires them to the API yet.
- [ ] **`POST /incidents/{id}/approve` does not exist.** The graph implements `interrupt()`, so HITL
      is ready on the graph side and has no endpoint.
- [ ] **Two in-memory stores, neither durable.** `service.py`'s `_incidents_store` dict and the
      graph's `InMemorySaver`. Both are fine for a single-process demo and both lose everything on
      restart — which also means an approval cannot survive one.
- [ ] **Duplicate model definitions.** `RemediationPlan`, `VerificationResult` and `IncidentReport`
      are defined in **both** `graph.py` and `models.py`, one per branch. They are not the same
      shape. Worth collapsing before anyone imports the wrong one.

### From `maria/containerization`, worth knowing

- **Postgres host port is now 5433**, not 5432 — avoids clashing with a locally installed Postgres.
  Inside the docker network it is still `postgres:5432`.
- **`DATABASE_URL` now URL-encodes the `@`** in the password (`!%4012`). This was flagged as a
  latent bug in the plan: the password contains `!@` and the URL had two `@`, so it worked only
  because the parser took the *last* one as the host separator. Prisma did not.
- `HACKATHON_1/.gitattributes` was folded into the root one. Two `.gitattributes` files meant the
  nested one silently won for everything under `HACKATHON_1/`, which had already overridden the
  `*.ps1 → CRLF` rule. Their stricter `* text=auto eol=lf` default was kept.

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
