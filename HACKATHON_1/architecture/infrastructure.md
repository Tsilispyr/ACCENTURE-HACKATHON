# Infrastructure Guide

What every file does, and how to actually use `scripts/` and `compose/`.

Scope: the deployment and runtime layer (roles 1 + 5). The LangGraph workflow itself is
documented separately by roles 2-4.

---

## 1. Quick start

**From WSL:**

```bash
cd /mnt/c/projects/ACCENTURE-HACKATHON-main/HACKATHON_1
bash scripts/deploy.sh
```

**From PowerShell or cmd:**

```powershell
cd C:\projects\ACCENTURE-HACKATHON-main\HACKATHON_1
.\scripts\deploy.ps1
```

Either entry point figures out the environment for itself:

| Shell | Command | What happens |
|---|---|---|
| WSL / Linux | `bash scripts/deploy.sh` | runs directly — docker is on PATH |
| Git Bash / MSYS | `bash scripts/deploy.sh` | detects no docker, **re-execs inside WSL** |
| PowerShell / cmd | `.\scripts\deploy.ps1` | translates the path, runs it in WSL |
| wrong shell | `sh scripts/deploy.sh` | re-execs under bash (pipefail is bash-only) |

The only thing that cannot be automatic is PowerShell running a `.sh` file — Windows has no way to
execute one, which is why `deploy.ps1` exists at all.

Do **not** expect `bash scripts/deploy.sh` to work from *PowerShell*. Docker for this project lives
inside WSL and is not on the Windows PATH, so a Windows-side bash (Git Bash / MSYS) cannot reach
it — and if the script gets picked up by `sh` rather than `bash` you get a misleading error about
`pipefail`, which is a bash-only option. `deploy.ps1` is the supported entry point: it translates
this folder to its WSL path and runs the same `deploy.sh` in there, so there is one implementation
rather than two that drift.

That single command does everything: creates `.env` if missing, prompts once for your Azure
credentials, measures the machine, picks a resource profile, brings the infra stack up in
dependency order, waits for real health, then builds and starts the app.

| Service | URL | Credentials |
|---|---|---|
| App | http://localhost:8010 | - |
| Langfuse (traces) | http://localhost:3000 | `user@example.com` / `12345678` |
| Grafana | http://localhost:3001 | `gtgh` / `grafanapassQWqw!@12` — *not started on the lean profile* |
| MinIO console | http://localhost:9091 | `minio` / `miniopassQWqw!@12` |
| Postgres | `localhost:5433` (host-side; 5432 inside the network) | `gtgh` / `postgrepassQWqw!@12` |

---

## 1a. Running it from each terminal

Every entry point works out which environment it is in. Pick the row for the terminal you are
actually sitting in.

### WSL / Linux / macOS

```bash
cd /mnt/c/projects/ACCENTURE-HACKATHON-main/HACKATHON_1
bash scripts/deploy.sh
```

Runs directly — docker is on PATH here. This is the native path; everything else routes to it.

### PowerShell

```powershell
cd C:\projects\ACCENTURE-HACKATHON-main\HACKATHON_1
.\scripts\deploy.ps1
```

If execution policy blocks it:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\deploy.ps1
```

Useful switches: `-DryRun` (validate and print the command, start nothing), `-Full` (force the
full resource profile), `-MemThresholdGb 16`.

### cmd

```bat
cd C:\projects\ACCENTURE-HACKATHON-main\HACKATHON_1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy.ps1
```

cmd has no way to run either a `.sh` or a `.ps1` directly, so it goes through `powershell -File`.
Verified working.

### Git Bash / MSYS

```bash
cd /c/projects/ACCENTURE-HACKATHON-main/HACKATHON_1
bash scripts/deploy.sh
```

Docker is not on the PATH of Git Bash, so the script detects that, translates its own directory
with `cygpath`/`wslpath`, and **re-execs itself inside WSL**. You will see:

```
[deploy] docker is not reachable from this Windows shell -- re-running inside WSL.
```

That line is the delegation working, not a warning.

### What does not work, and why

| Attempt | Result |
|---|---|
| `bash scripts/deploy.sh` **from PowerShell** | PowerShell cannot execute a `.sh`. Use `.\scripts\deploy.ps1`. |
| `sh scripts/deploy.sh` | Re-execs itself under bash. `set -o pipefail` is bash-only, and the raw error names `pipefail` while explaining nothing. |
| Docker Desktop not running | Every path fails at the docker check with a message saying so. Start it first. |

## 2. How the stack fits together

Two independently-managed Compose stacks, joined by one shared Docker network:

```
                docker-compose-langfuse.yaml            (infra stack)
                +-----------------------------------------------+
                |  postgres   clickhouse   redis   minio         |
                |  langfuse-worker    langfuse-web    grafana    |
                +-----------------------------------------------+
                          network: "langfuse-infra"
                                    ^
                                    |  joins as external
                +-----------------------------------------------+
                |  hackathon1-app          app-db-init           |
                +-----------------------------------------------+
                docker-compose.yml                      (app stack)
```

Why two files: the app must be redeployable without restarting the infra. They cannot be one
file's `depends_on`, because that only works within a single Compose invocation — so the
cross-stack ordering lives in `scripts/deploy.sh` instead.

**Every infra port is bound to `127.0.0.1` on the host**, so the app reaches Postgres, MinIO and
Langfuse only over the shared network, by Compose service DNS name (`langfuse-web`, `minio`,
`postgres`). That is why `docker-compose.yml` overrides `LANGFUSE_HOST` and `MINIO_ENDPOINT`:
`.env` holds `localhost` values, which are correct for running the app *outside* Docker.

**`app-db-init`** is a one-shot container that creates a `hackathon1` database inside the infra
Postgres and then exits. It exists so we do not run a second Postgres server just for app state.

---

## 3. File-by-file

### Configuration

| File | What it is | Notes |
|---|---|---|
| `.env.example` | **Committed** template; the single source of truth for env keys | Only the four `AZURE_*` values are blank. Langfuse and MinIO values ship pre-filled — local-only demo credentials, identical on every machine by design. |
| `.env` | **Gitignored.** Your real values | Created automatically by `preflight.sh`. Never commit it. |
| `.gitignore` | Ignores `.env`, `logs/`, `.venv`, caches | A second one at the repo root is a safety net for stray files outside this folder. |
| `.gitattributes` (repo root) | Forces LF for `*.sh`, `*.yaml`, `Dockerfile` | Without it, `core.autocrlf=true` checks scripts out with CRLF and they die in WSL with `bash: /usr/bin/env: bash\r: No such file or directory`. |
| `.dockerignore` | Keeps `.env`, `.venv`, `.git` out of the build context | Baking `.env` into an image layer publishes your keys to anyone who can pull it, and deleting the file in a later layer does **not** remove it from history. `README.md` must stay *included* — `pyproject.toml` declares it. |

### Python packaging

| File | What it is |
|---|---|
| `pyproject.toml` | Project metadata and dependencies. Build backend is `uv_build`. |
| `uv.lock` | **The lockfile of record** — exact pinned resolution, with hashes, cross-platform. |
| `requirements.txt` | *Generated* from `uv.lock` (runtime deps, with hashes). A convenience for pip users; not the source of truth. |
| `requirements-dev.txt` | *Generated* — the dev group (pytest, pytest-asyncio, httpx). |

**Do you need `uv`?** Not to run the stack. The `Dockerfile` copies the `uv` binary out of a
registry image, so `bash scripts/deploy.sh` works on a machine that has only Docker installed.
`uv` is needed only for local non-Docker work (`uv run pytest`, `uv run uvicorn`).

Regenerate the pip exports after changing dependencies:

```bash
uv export --format requirements-txt --no-dev   --no-emit-project -o requirements.txt
uv export --format requirements-txt --only-dev --no-emit-project -o requirements-dev.txt
```

The pip route needs one extra step, because `--no-emit-project` omits the project itself:

```bash
pip install -r requirements.txt
pip install -e .
```

### Containers

| File | What it is |
|---|---|
| `Dockerfile` | Two-layer build: dependencies first (`--frozen --no-install-project --no-dev`), source second. Ordinary code edits reuse the dependency layer, so rebuilds stay fast. `--frozen` fails if `uv.lock` has drifted from `pyproject.toml`, which is what makes `up --build` trustworthy. |
| `docker-compose-langfuse.yaml` | The seven-service infra stack. Declares the `langfuse-infra` network. |
| `docker-compose.yml` | The app stack: `hackathon1-app` + `app-db-init`. Joins `langfuse-infra` as `external`. |
| `compose/*.yaml` | Resource profiles — see section 5. |

### Scripts

| File | What it is |
|---|---|
| `scripts/deploy.sh` | The entry point. See section 4. |
| `scripts/preflight.sh` | Credentials + machine capability. See section 4. |
| `scripts/deploy.ps1` | PowerShell/cmd entry point. Delegates into WSL — see section 4. |
| `logs/run-NNNN-<timestamp>.log` | One numbered log per deploy run, never overwritten. Gitignored. |

### Application (`src/hackathon1/`)

| File | What it is |
|---|---|
| `service.py` | FastAPI app. `GET /` (health, and the container healthcheck target), `GET /chat`, `POST /incidents`. |
| `graph.py` | The LangGraph workflow. Exports `build_graph()` and `app_graph`. |
| `llm.py` | The shared `AzureChatOpenAI` client. Calls `load_dotenv()` itself, deliberately — the client validates credentials eagerly at construction, so it must not depend on import order. |
| `tools.py` | Domain tools bound to the chat agent. |
| `tracing.py` | Returns `[CallbackHandler()]` **only if** both Langfuse keys are set, otherwise `[]`. |
| `storage.py` | Uploads reports to MinIO, creating the bucket on first use. Best-effort: any failure is logged at WARNING, never raised. |
| `apiclient.py` | Smoke-test script, deliberately unhardened (no timeout or retry). |
| `tests/` | 15 tests. Every LLM-touching test mocks, so the suite makes no live API calls. |

### Documentation

| File | Status |
|---|---|
| `architecture/infrastructure.md` | This file. |
| `README.md` | Project overview and setup. |
| `TASKS.md` (repo root) | Live task board for roles 1 + 5. |
| `hackathon 1- handout.docx.pdf` (repo root) | **The requirements of record.** Committed so the spec is versioned with the implementation and every section reference in these docs resolves to a fixed document. |
| `troubleshooting-guide.md` | Error → cause → fix, searchable by the actual error text. |
| `stack-guide.md` | **Historical.** Describes the older Langfuse **v2** stack (Postgres-backed traces, `/mnt/d/...` paths). Sections 6 and 9 are still worth reading; the rest no longer matches what runs. |

---

## 4. Using `scripts/`

### `scripts/deploy.sh` — the one command

```bash
bash scripts/deploy.sh
```

In order, it:

1. **Sources `preflight.sh`** — ensures `.env` is complete, sets `DEPLOY_PROFILE`.
2. **Starts a numbered log** (`logs/run-0007-...log`), mirrored to your terminal. Nothing is ever
   overwritten, so past runs stay comparable.
3. **Converges the infra stack** with the profile override layered on. This runs *every* time:
   `up -d` is idempotent and recreates only containers whose config actually changed. (It used to
   skip this entirely when the stack was already healthy — which printed the chosen profile and
   then applied none of it, because memory limits are set at container *creation*.)
4. **On lean, stops a stale Grafana** left running by an earlier full-profile run.
5. **Waits for real health** — see the gate below.
6. **Builds and starts the app** with its own profile override.
7. **Prints the URL summary**, showing Grafana as "not started" on lean.

**The health gate** reads actual container state and stops as soon as the answer is known,
instead of sitting out the timeout. It aborts immediately on three signals, none of which
improve by waiting:

| Signal | Meaning |
|---|---|
| `OOMKilled=true` | the ceiling in `compose/infra.<profile>.yaml` is too low |
| `exited` / `dead` | fatal |
| `RestartCount` rising by 3+ | restart loop — check `docker logs <name>` |

A couple of restarts during boot are normal (`langfuse-web` legitimately exits 0 partway through
its init), which is why the loop threshold is a *rise of 3*, not any restart at all. The timeout
is profile-aware — **420s on lean, 180s on full** — because Langfuse v4 needs roughly two minutes
to boot on constrained hardware.

### `scripts/deploy.ps1` — the PowerShell entry point

A thin wrapper, deliberately: all the logic stays in `deploy.sh`. It checks WSL is present,
translates this folder to its `/mnt/...` path, checks Docker is reachable *inside* WSL, and then
runs `bash scripts/deploy.sh` there.

```powershell
.\scripts\deploy.ps1              # normal use
.\scripts\deploy.ps1 -Full        # force the full profile (= MEM_THRESHOLD_GB 1)
.\scripts\deploy.ps1 -DryRun      # validate and print the command, start nothing
.\scripts\deploy.ps1 -MemThresholdGb 16
```

Two details that are easy to get wrong if you rewrite this:

- It calls `wsl.exe -e bash -lc <command>` directly rather than wrapping it further, so **stdin
  stays attached to your console**. `preflight.sh` tests `[ -t 0 ]` to decide whether it may prompt
  for credentials; break that and a fresh checkout fails with "missing values" instead of asking.
- It hands `wslpath` **forward slashes**. `wsl.exe` consumes backslashes in its arguments as
  escapes, so passing a native Windows path turns `C:\projects\...` into `C:projects...` and
  `wslpath` rejects it.

### `scripts/preflight.sh` — credentials and capability

Run it standalone to see the verdict without deploying anything:

```bash
bash scripts/preflight.sh
```

**Job 1 — credentials.** Creates `.env` from `.env.example` if missing, then checks the four
required keys. With a terminal it prompts once (API-key input is hidden) and writes them back.
Without one it exits 1 naming exactly what is missing, rather than letting Compose fail with an
unhelpful `env_file: .env` error.

**Job 2 — capability.** Picks the profile from **`MemTotal`, deliberately not `MemAvailable`** —
free RAM swings minute to minute with whatever else is open; total does not. A big machine with a
temporarily low ceiling should still get the full profile.

```bash
MEM_THRESHOLD_GB=16 bash scripts/preflight.sh    # raise the bar
MEM_THRESHOLD_GB=1  bash scripts/preflight.sh    # force full on a small machine
```

It also detects WSL and, on lean, explains how to raise the allocation in `.wslconfig` — a 32 GB
Windows host giving WSL 3 GB is a configuration problem, not a small machine, and should not be
silently degraded.

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

---

## 5. Using `compose/`

Four override files. They are **layers, never edits** — the base compose files stay untouched, so
you can always tell what is stock and what is ours.

```
compose/
├── infra.lean.yaml    memory ceilings + relaxed health start_period, constrained machine
├── infra.full.yaml    generous ceilings, capable machine
├── app.lean.yaml      app container ceiling, constrained
└── app.full.yaml      app container ceiling, capable
```

`deploy.sh` applies them for you. To do it by hand — the later `-f` wins per key:

```bash
docker compose -f docker-compose-langfuse.yaml -f compose/infra.lean.yaml up -d
```

**Always check what you are about to apply.** `config` renders the merged result:

```bash
docker compose -f docker-compose-langfuse.yaml -f compose/infra.lean.yaml config
```

**Before editing any compose file**, capture a baseline so you can prove the change did only what
you intended:

```bash
docker compose -f docker-compose-langfuse.yaml config > /tmp/before.yaml
# ...edit...
docker compose -f docker-compose-langfuse.yaml config > /tmp/after.yaml
diff /tmp/before.yaml /tmp/after.yaml     # expect ONLY the lines you meant to change
```

### What `lean` actually does

| | lean | full |
|---|---|---|
| Grafana | **not started** (−256 MiB) | started |
| clickhouse | 1280m | 3g |
| langfuse-web | 1g + heap 640 MB | 2g + heap 1536 MB |
| langfuse-worker | 768m + heap 512 MB | 1536m + heap 1024 MB |
| postgres / redis / minio | 256m / 128m / 256m | 1g / 512m / 1g |
| app | 512m | 1g |
| health `start_period` | 180s (clickhouse 60s) | 60s (clickhouse 30s) |

A limit is a **ceiling, not a reservation** — it stops one runaway container from taking the host,
it does not lower baseline usage. On lean the real saving comes from not starting Grafana and from
bounding the Node heaps.

### What you actually lose on lean

Measured on this machine, not estimated. Everything below was re-checked while running `lean`:

| Capability | On lean | Evidence |
|---|---|---|
| Langfuse tracing | **works** | events land in ClickHouse `events_core` |
| MinIO report storage | **works** | objects written to `hackathon1-reports` |
| App, API and the graph | **works** | `GET /` returns 200; incidents run end to end |
| postgres · clickhouse · redis · minio · langfuse-web · langfuse-worker | **all running and healthy** | `docker ps` |
| **Grafana** | **not started** | the only thing actually withheld |

So the single functional difference is Grafana — and today that costs **nothing**, because Grafana
has no provisioned datasource yet (its only mount is its own data volume), so it would start blank.
It is also a **bonus** item in the requirements, not a mandatory one, so `lean` forfeits no marks.

This begins to matter once the Grafana dashboards land. At that point a lean machine simply starts
it on demand:

```bash
docker start grafana-app          # ~256 MiB, then http://localhost:3001
docker stop grafana-app           # give the memory back
```

Two further differences, neither of them functional:

- **Smaller Node heaps** (640/512 MB vs 1536/1024 MB). V8 collects more often, so there is
  marginally more GC CPU under heavy trace ingestion. Not observable at the volumes this project
  produces.
- **Longer health `start_period`** (180s vs 60s). Purely how long Docker waits before counting a
  probe as a failure — it does not slow the service down, it stops a normal boot being reported
  as unhealthy.

What `lean` is *not*: it does not disable tracing, drop spans, reduce retention, or degrade the
workflow in any way. It stops one optional container and bounds two Node heaps.

### Changing a limit

1. Measure first: `docker stats --no-stream`.
2. Set the new value **above** observed usage in the relevant `compose/infra.<profile>.yaml`.
3. Re-run `bash scripts/deploy.sh`. Only the changed containers are recreated.
4. Confirm: `docker stats --no-stream`, and `docker inspect -f '{{.State.OOMKilled}}' <name>`.

Every current limit was sized from real `docker stats` readings, not estimates. The figures
originally proposed in the README (`langfuse-web 512m`, `grafana 256m`) sit *below* measured usage
and would have been OOM-killed on first boot.

---

## 6. Cookbook

```bash
# deploy / redeploy everything
bash scripts/deploy.sh

# rebuild just the app after a code change
docker compose -f docker-compose.yml -f compose/app.lean.yaml --project-directory . up -d --build

# run the tests (no live API calls -- every LLM test mocks)
uv run pytest -v

# run the app locally without Docker (uses .env's localhost values)
uv run uvicorn hackathon1.service:app --reload

# smoke test
uv run python -m hackathon1.apiclient

# start Grafana after a lean deploy stopped it
docker start grafana-app

# did a trace land? (see section 7 -- NOT the traces table)
docker exec langfuse-clickhouse clickhouse-client --user clickhouse \
  --password 'clickpassQWqw!@12' --query "SELECT count() FROM default.events_core"

# did a report upload?
docker exec langfuse-minio ls /data/hackathon1-reports

# why is a container unhappy?
docker inspect <name> --format 'Restarts={{.RestartCount}} OOM={{.State.OOMKilled}} Exit={{.State.ExitCode}}'
docker logs <name> | tail -50
```

---

## 7. Gotchas

These all cost real debugging time. They are written down so they cost it once.

**Never `docker compose down -v`.** The Langfuse-seeded organisation, project and **API keys** live
in the `langfuse_postgres_data` volume. Deleting it invalidates the keys in `.env`. `down` without
`-v` is fine.

**Langfuse v4 runs in `events_only` mode here.** The ClickHouse `traces`, `observations` and
`analytics_*` tables stay permanently **empty**, and `GET /api/public/traces` returns **404**.
Ingested data lands in **`events_core` / `events_full`**. Any Grafana dashboard or verification
script must query those.

**A `mem_limit` on a Node service silently caps its V8 heap.** The nastiest one here. Adding
`mem_limit: 896m` to `langfuse-web` put it into a restart loop: V8 derives its heap ceiling from
the *cgroup*, capped old-space at roughly 450 MB, and died with
`FATAL ERROR: Reached heap limit - JavaScript heap out of memory` — while Docker reported
**`OOMKilled=false` and `ExitCode=0`**, so it looked nothing like a memory problem. Unlimited,
Node saw the whole host and picked a large heap, which is exactly why the service ran fine for an
hour and broke the moment a limit was added.

Fix: set the heap explicitly with `NODE_OPTIONS=--max-old-space-size=<MB>` so it is decoupled from
the container limit. Tuning it *down* also saves real memory — V8 grows toward whatever ceiling it
is given, so `langfuse-web` went from 880 MiB RSS at heap 896 to 436 MiB at heap 640.

**Do not give Redis `--maxmemory`.** The base file sets `--maxmemory-policy noeviction`, so a cap
would make Langfuse's queue writes *fail* rather than evict. Redis uses about 15 MiB; the container
ceiling is protection enough.

**`.env` and the compose `LANGFUSE_INIT_PROJECT_*` keys must agree.** Only the `LANGFUSE_INIT_*`
pair is re-seeded on a fresh database. If they diverge, tracing works on the machine where the keys
were created and silently does nothing everywhere else.

**Tracing and storage fail silently by design.** `tracing.py` returns `[]` and `storage.py` returns
`None` when credentials are missing — the app keeps serving requests and logs nothing alarming.
That is good for local dev without Docker, but it once hid the fact that neither subsystem was
running at all. If traces are missing, check the container's environment first:

```bash
docker inspect hackathon1-app --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'LANGFUSE|MINIO'
```

**`docker` is not on the Git-Bash PATH.** Deploys run from WSL, or from PowerShell via
`.\scripts\deploy.ps1`.

**A `pipefail` error means the script was run by `sh`, not `bash`.** `set -o pipefail` is bash-only,
so `sh scripts/deploy.sh` dies on the first executable line with an error naming pipefail and
nothing about the real cause. Both scripts now carry a POSIX-only prologue that re-execs under bash
(`deploy.sh`) or exits with a clear message (`preflight.sh`, which is *sourced* and so must never
`exec`). Note the prologue has to parse under `dash`, which is why it uses no arrays or `[[ ]]`
before the guard — and why `dash -n scripts/deploy.sh` still reports a syntax error: `-n` forces a
full parse of the bash-only code further down, while a real run never reaches it.
