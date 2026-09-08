# Troubleshooting & Version-Mismatch Guide

Problems hit while bringing up the Day4 observability stack (Langfuse + Grafana + Postgres) and the `day04` Python project, what caused them, and the exact fix for each. Separate from [installation-guide.md](installation-guide.md), which covers the happy path of wiring Grafana to Langfuse.

Recorded 2026-09-03. Search this file by the **error text** - each section quotes the real message.

## Summary

| # | Symptom | Root cause | Status |
|---|---|---|---|
| 1 | `Not supported URL scheme http+docker` | Ran `docker-compose` (v1) instead of `docker compose` (v2) | Fixed - change the command |
| 2 | Langfuse can't reach Postgres | `#` in password broke the `DATABASE_URL` | Fixed in `docker-compose.yaml` |
| 3 | Grafana `password authentication failed` + `server refused TLS` | SSL mode `require` + percent-encoded password pasted into a non-URL field | Fixed in Grafana UI |
| 4 | `cannot import name 'RunTree' from 'langsmith'` | Local `langsmith.py` shadowed the installed package | Fixed - file renamed |
| 5 | `No module named 'langchain.callbacks'` / traces never appear | Langfuse SDK ↔ server ↔ LangChain three-way version conflict | Fixed - server upgraded to v3 |
| 6 | `NameError: name 'os' is not defined` | Missing `import os` | Fixed |
| 7 | - | Docker stack rewritten for Langfuse v3 | Superseded by #9 |
| 8 | Logs lost on `down`, terminal buffer scrolled away | Container logs are deleted with the container | Fixed - collector sidecar + rotation |
| 9 | `dependency failed to start: container langfuse-clickhouse is unhealthy` | Langfuse v3's 7-service stack exceeds this machine's RAM | Fixed - reverted to the v2 light stack |

---

## 1. `docker-compose` v1 is broken - use `docker compose` v2

**Error**

```
urllib3.exceptions.URLSchemeUnknown: Not supported URL scheme http+docker
...
File "/usr/bin/docker-compose", line 33, in <module>
    sys.exit(load_entry_point('docker-compose==1.29.2', ...
docker.errors.DockerException: Error while fetching server API version: Not supported URL scheme http+docker
```

**Cause.** `/usr/bin/docker-compose` is the old Python v1 (1.29.2) from apt. It depends on `docker-py`, which registered a custom `http+docker://` URL scheme that `requests` ≥ 2.32 no longer resolves. Nothing to do with your YAML - it dies before the file is ever parsed.

**How to tell which one you ran.** A Python traceback can only come from v1; the v2 plugin is a Go binary with no Python in it.

**Fix - run the space form:**

```bash
docker compose up          # v2 plugin -- correct
docker-compose up          # v1 script -- broken, do not use
```

Verify v2 is present:

```bash
docker compose version     # expect v2.x or newer
```

If missing, install the plugin (pick by how Docker was installed):

```bash
sudo apt update && sudo apt install docker-compose-v2       # Ubuntu 24.04 / apt docker.io
sudo apt update && sudo apt install docker-compose-plugin   # Docker's official apt repo
```

With Docker Desktop on Windows, enable Settings → Resources → WSL Integration for the distro instead.

**Stop the mistake recurring:**

```bash
echo "alias docker-compose='docker compose'" >> ~/.bashrc && source ~/.bashrc
# or remove the broken v1 entirely
sudo apt remove docker-compose
```

**Also:** `--build` does nothing here. All three services use prebuilt images; none has a `build:` section.

---

## 2. `#` in a password breaks `DATABASE_URL`

**Where:** [docker-compose.yaml](docker-compose.yaml), `langfuse` service.

**Cause.** The Postgres password is `ghgt#C0d3`. Inlined raw into a connection URL, `#` starts the URL *fragment*, so everything after it is discarded and the password silently becomes `ghgt` → auth failure against `db`. YAML quoting does not help; this is URL parsing, not YAML parsing.

**Fix - percent-encode `#` as `%23`:**

```yaml
# WRONG
DATABASE_URL: "postgresql://gtgh:ghgt#C0d3@db:5432/langfuse"
# RIGHT
DATABASE_URL: "postgresql://gtgh:ghgt%23C0d3@db:5432/langfuse"
```

`POSTGRES_PASSWORD` on the `db` service stays **raw** (`ghgt#C0d3`) - it is not a URL.

**Rule of thumb.** Any of `# / : @ ? & %` in a password must be percent-encoded inside a URL, and only inside a URL.

---

## 3. Grafana → Postgres data source fails

**Error**

```
failed to connect to `user=gtgh database=langfuse`:
    db:5432 (db): tls error: server refused TLS connection
    db:5432 (db): failed SASL auth: FATAL: password authentication failed for user "gtgh" (SQLSTATE 28P01)
```

Two independent faults, one per connection attempt.

**Fix A - TLS/SSL Mode: `require` → `disable`.** The `postgres:16-alpine` image ships with no server certificate and TLS off, so it refuses any handshake. Grafana reaches Postgres over the private compose network, never the host, so unencrypted is acceptable here.

**Fix B - password must be the RAW value:**

```
ghgt#C0d3      <- correct, Grafana's password box is a plain form field
ghgt%23C0d3    <- WRONG, the %23 from issue #2 belongs only inside DATABASE_URL
```

This is the easy trap right after fixing issue #2. Percent-encoding is a URL rule; a discrete form field takes the literal password.

**Fix C -** set PostgreSQL Version under *Additional settings* from `9.3` to `16`, matching the actual server.

`Host URL: db:5432` and `Database: langfuse` are correct - a SASL error already proves DNS and networking resolved to the right container.

**Optional hardening.** Grafana warns that `gtgh` owns the Langfuse schema, so a careless dashboard query could `DROP` Langfuse tables. For production use a read-only user instead.

---

## 4. Local file shadowing an installed package

**Error**

```
ImportError: cannot import name 'RunTree' from 'langsmith'
    (d:\...\project\day04\src\day04\langsmith.py)
```

Read the path in the parentheses - Python is telling you exactly which file it loaded.

**Cause.** Running a script directly puts *that script's own folder* first on `sys.path`. A file named `langsmith.py` therefore wins over the installed `langsmith` package for every import in the process - including imports made deep inside `langchain_core`. The traceback starts at `langchain_openai` only because that is where the import chain began; that package is not the problem.

**Fix - rename the file and delete its stale bytecode:**

```bash
mv src/day04/langsmith.py src/day04/langsmith_tracing.py
rm -f src/day04/__pycache__/langsmith.cpython-312.pyc
```

**Never name a file after a package you import.** Watch out for: `langsmith.py`, `logging.py`, `json.py`, `types.py`, `email.py`, `queue.py`.

> Known instance still in the repo: `day03/src/day03/logging.py` shadows the **stdlib** `logging` module. It has not been renamed - flagged, not fixed.

---

## 5. Langfuse SDK version must match the Langfuse server version

The biggest trap of the day, and it fails **silently**: the script runs, prints its answer, and no trace ever reaches the UI.

**Cause.** `docker-compose.yaml` pins the server to `langfuse/langfuse:2`. `uv add langfuse` installs the newest SDK (4.x), which ships traces over OpenTelemetry to `POST /api/public/otel/v1/traces` - an endpoint that only exists from Langfuse server v3 onward. A v2 server has no such route.

**It is a three-way constraint, not two.** The first attempt - downgrading to `langfuse<3` to match the v2 server - **fails**, because the v2 SDK's LangChain integration was written against LangChain **0.x**:

```
File ".../langfuse/callback/langchain.py", line 31, in <module>
    from langchain.callbacks.base import (
ModuleNotFoundError: No module named 'langchain.callbacks'
...
ModuleNotFoundError: Please install langchain to use the Langfuse langchain integration: 'pip install langchain'
```

That last message is misleading - `langchain` **is** installed. It's version 1.3.18, where `langchain.callbacks` no longer exists (callbacks moved to `langchain_core.callbacks`). langfuse 2.x declares only `langchain (>=0.0.309)`, an unbounded pin predating LangChain 1.x, so the install succeeds and the import then fails. The import block is a single `try` with no fallback, so no 2.x release fixes it.

**Compatibility - pick a row, all four columns must agree:**

| Server image | SDK | LangChain import | Works with LangChain 1.x? |
|---|---|---|---|
| `langfuse/langfuse:2` | `langfuse<3` | `from langfuse.callback import CallbackHandler` | **No** - needs LangChain 0.x |
| `langfuse/langfuse:3` / `:4` | `langfuse>=4` | `from langfuse.langchain import CallbackHandler` | Yes |

Since this course requires LangChain 1.x (`create_agent`, middleware), only the second row is viable - so **the server had to be upgraded**, not the SDK downgraded.

**Fix applied:**

```powershell
uv add "langfuse>=4"
```

```python
from langfuse.langchain import CallbackHandler   # v4 path, works on LangChain 1.x
```

plus rewriting `docker-compose.yaml` from `langfuse/langfuse:2` to the v3 stack (see section 7 below).

**Env var naming.** The v4 SDK reads `LANGFUSE_HOST` *or* `LANGFUSE_BASE_URL`; the v2 SDK reads only `LANGFUSE_HOST`. Setting only `LANGFUSE_BASE_URL` on a v2 SDK means it silently defaults to `https://cloud.langfuse.com` and rejects your local keys. `day04/.env` carries both, which is correct for either:

```ini
LANGFUSE_BASE_URL=http://localhost:3000
LANGFUSE_HOST=http://localhost:3000
```

**How to verify what an installed SDK actually expects** - inspect it instead of guessing:

```bash
ls .venv/Lib/site-packages/langfuse/                                    # v2 has callback/, v4 has langchain/
grep -rho "LANGFUSE_[A-Z_]*" --include="*.py" .venv/Lib/site-packages/langfuse | sort -u
```

---

## 6. `NameError: name 'os' is not defined`

`src/day04/langfuse-tracing.py` called `os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")` with no `import os`. Added. Worth knowing that this one was hidden behind issue #4 - fixing one error just exposes the next.

---

## 7. Upgrading the Docker stack from Langfuse v2 to v3

Consequence of section 5. Langfuse v3 splits storage across four backends, so `docker-compose.yaml` went from 3 services to 7.

| Service | Role | Host port |
|---|---|---|
| `db` (Postgres) | metadata: projects, users, API keys | 5432 |
| `clickhouse` | **traces, observations, scores** | 8123 (HTTP only) |
| `redis` | ingestion queues | not published |
| `minio` | S3 blobs for raw events | 9090 / 9091 (console) |
| `langfuse-worker` | async ingestion pipeline (new, required in v3) | not published |
| `langfuse` (web) | UI + API + OTel endpoint | 3000 |
| `grafana` | dashboards | 3001 |

**Port choices worth knowing.** ClickHouse's native port 9000 is deliberately **not** published, and MinIO uses 9090/9091 instead of 9000/9001, because an unrelated MinIO container already owns 9000-9001 on this machine. Check yours before assuming: `docker ps -a`.

**Your v2 data is not deleted.** The new Postgres uses a fresh volume, `postgres_data_v3`. The old `project_postgres_data` volume is intentionally left undeclared so `docker compose down -v` cannot remove it.

**Bring it up:**

```bash
docker compose up -d
docker compose ps          # all services should be healthy/running
docker compose logs -f langfuse    # watch migrations on first boot
```

First boot runs both Postgres and ClickHouse migrations, so give it a couple of minutes.

**You need new API keys.** The fresh database has no account. Either sign up again at `http://localhost:3000` and paste the new keys into `day04/.env`, or uncomment the `LANGFUSE_INIT_*` block in `docker-compose.yaml` and seed your existing keys before first boot.

**Rollback**, if v3 causes trouble:

```bash
docker compose down
git checkout docker-compose.yaml    # back to the v2 file
docker compose up -d                # old postgres_data volume is still intact
uv add "langfuse<3"                 # and revert the import (but see section 5 -- the callback won't work)
```

### Grafana dashboards break on v3 - expected

The SQL in [installation-guide.md](installation-guide.md) queries `FROM traces` on Postgres. In v3 that table is gone from Postgres; traces live in ClickHouse. Options:

- Install the **ClickHouse data source plugin** in Grafana and point it at `http://clickhouse:8123` (user `clickhouse`, password `clickhouse`), then rewrite the panel SQL against ClickHouse's `traces` table.
- Or keep the Postgres data source for what v3 still stores there (projects, users, sessions) and use Langfuse's own UI for trace analytics.

The Postgres data source itself still connects fine - same `db:5432`, same credentials, unchanged.

---

## 8. Keeping logs and checking the stack

Docker keeps container logs only until the container is removed - `docker compose down` destroys them. Three scripts in [scripts/](scripts/) exist so a failure leaves evidence behind. Run them from WSL:

```bash
cd /mnt/d/accenture-train-program/project

bash scripts/stack-check.sh     # read-only health snapshot: status, exit/OOM codes, memory, endpoints, recent errors
bash scripts/stack-logs.sh      # save every service's logs to logs/<timestamp>/ + summary.txt
bash scripts/stack-follow.sh    # append live logs to logs/stack-follow.log
```

**Run the stack detached and follow separately:**

```bash
docker compose up -d
bash scripts/stack-follow.sh
```

`Ctrl+C` while following stops only the following. `Ctrl+C` on a foreground `docker compose up` sends SIGTERM to every service and takes the whole stack down - that is what an all-services `exit=137` means.

**Reading exit codes** (`stack-check.sh` prints these):

| Code | Meaning |
|---|---|
| `0` | clean stop |
| `143` | SIGTERM - graceful shutdown requested |
| `137` + `oom=false` | SIGKILL after a stop that exceeded the 10s grace period |
| `137` + `oom=true` | the kernel killed it for memory |

`logs/` is gitignored.

---

## 9. Note on Langfuse versions (v2 vs v3/v4)

In earlier days of this training program, the stack was reverted to Langfuse v2 to save RAM (omitting ClickHouse, MinIO, and Redis). 
However, for `HACKATHON_1`, we are explicitly running the **full v3/v4 stack** (which includes ClickHouse, MinIO, Redis, and Langfuse-Worker).
This is fully configured and optimized in `docker-compose-langfuse.yaml`. 

If you experience RAM constraints on your local WSL environment, make sure to close other heavy IDEs or increase your `.wslconfig` memory limit. Do **not** revert to v2 for this project.

## Environment notes

**Windows venv vs WSL.** `uv` built `day04/.venv` in Windows layout (`.venv/Scripts/python.exe`). That venv **cannot** be used from inside WSL, which needs `.venv/bin/python`. Current working setup is Docker in WSL, Python from PowerShell. `pyproject.toml` and `uv.lock` are cross-platform, so `uv sync` from WSL rebuilds the venv for Linux - but it replaces the Windows one, so pick one side and stay there.

Files under `D:\...` and `/mnt/d/...` are the same files, so edits from either side persist for both.

**day04 dependency baseline** - the full `uv add` set carried over from Day1-Day3 is in [../notes/06-SETUP-CHEATSHEET.md](../notes/06-SETUP-CHEATSHEET.md).

---

## Known issues, not fixed

Flagged during review, left alone deliberately - each needs a decision:

| Where | Issue |
|---|---|
| `day03/src/day03/tests/test_ticket_service.py:5` | Imports `from gtgh.logging_testing.ticket_service import ...`, but the installed package is `day03`. No `gtgh` module exists → `pytest` cannot collect the file at all. |
| `day03/src/day03/logging.py` | Shadows the stdlib `logging` module (same class of bug as issue #4). |
| `day02/src/day02/ex03.py:8` | `status: str = field(default_factory=list)` - a `str` field defaulting to `[]`; `is_urgent()` then does `Priorities[self.status]` → `KeyError`. |
| `day02/src/day02/"ex09 copy.py":99` | `ToolMessage(..., tool_name=..., tool_args=...)` - `ToolMessage` requires `tool_call_id`. |
| `day04/src/day04/langfuse-tracing.py` | Hyphenated filename cannot be imported or wired to `[project.scripts]`; rename to `langfuse_tracing.py` if an entry point is wanted. |
| repo-wide | Only `day01` and `day04` have a `.gitignore`, and `day01`'s omits `.env`. `day02/.env` and `day03/.env` hold real Azure/Google keys with nothing ignoring them. `docker-compose.yaml` and `installation-guide.md` also carry plaintext passwords. Check with `git ls-files "*.env"` before any push. |
