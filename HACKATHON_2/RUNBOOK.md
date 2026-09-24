# RUNBOOK

Operating the stack, and what to do when it breaks.

---

## Profiles

`preflight.sh` picks one from **total** RAM (not available RAM - free memory swings minute to
minute, total does not). Threshold: 6 GB.

| Profile | Starts | Roughly | When |
|---|---|---|---|
| `lean` | postgres, api, mcp-systems | ~1.2 GB | **this machine** (WSL has 3.6 GB) - the demo profile |
| `full` | + neo4j | ~2.0 GB | a machine with ≥6 GB, or after raising the WSL allocation |

Force one: `MEM_THRESHOLD_GB=1 bash scripts/deploy.sh` gives `full`, `MEM_THRESHOLD_GB=99` gives
`lean`.

**Rehearse the demo on `lean` at least twice.** The worst version of a RAM problem is a stack that
only ever ran on `full`.

### Raising the WSL allocation

There is no `.wslconfig` on this machine, so WSL took ~50% of a 7.4 GB host. To give it more, create
`C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=5GB
```

then `wsl --shutdown` - **this stops every container on the machine**, including hackathon 1's.

---

## Before you start: the other stack

This machine cannot run hackathon 1 and hackathon 2 simultaneously - 2.4 GB + 1.2 GB against a
3.6 GB ceiling. `deploy.sh` checks and refuses with the exact command to fix it.

```bash
STOP_HACKATHON1=1 bash scripts/deploy.sh     # stop them and continue
docker start langfuse-postgres langfuse-clickhouse ...   # bring hackathon 1 back later
```

---

## Everyday commands

```bash
bash scripts/deploy.sh                    # converge everything; idempotent
docker compose -f deployment/docker-compose.yml --project-directory . up -d --build api   # rebuild app only
docker logs -f h2-api
docker stats --no-stream                  # who is actually using the memory
uv run pytest -m workflow                 # offline tests, no network, no keys
```

Run logs are numbered and never overwritten: `logs/run-0001-<timestamp>.log`.

---

## Symptom → cause → fix

| Symptom | Cause | Fix |
|---|---|---|
| `bash: /usr/bin/env: 'bash\r': No such file or directory` | The file was checked out with CRLF | `.gitattributes` should prevent it. `dos2unix scripts/*.sh`, and check `git config core.autocrlf` |
| `illegal option: pipefail` | The script ran under `sh`, not `bash` | Run `bash scripts/deploy.sh`. The POSIX prologue normally re-execs for you |
| `RuntimeError: Event loop is closed`, from inside a tool call | Somebody reintroduced a per-call `asyncio.run`. The tool is fine; the bridge under it is dead | Route the call through `agentcore.aio.run`. See PROBLEMS P40 |
| Every step reports `step_failed` and the answer still looks plausible | The executor is crashing and the pipeline is replanning around it. This hid a real bug for the whole build | Read the `error` field in the `s6_act` audit events. A `TypeError` there is a programming error, not a tool failure. PROBLEMS P39 |
| `Could not compose an answer` with zero findings | A structured-output validation error. One bad enum value invalidates the whole draft | Check the `s8_compose` audit event's `error`. PROBLEMS P41 |
| `docker: command not found` in Git Bash | Docker lives inside WSL; there is no Docker Desktop | `deploy.sh` re-execs into WSL automatically. To do it by hand: `wsl -e bash -lc "cd /mnt/c/projects/hackathon2 && bash scripts/deploy.sh"` |
| A container restarts forever, `OOMKilled=false`, `ExitCode=0` | **A JVM or Node process sized its heap from the cgroup limit and exhausted it.** `mem_limit` alone does not prevent this | Neo4j's heap and pagecache are capped explicitly in `deployment/docker-compose-infra.yaml`. If you add any JVM/Node service, cap its heap too - never rely on `mem_limit` |
| `deploy.sh` exits fast naming a container | The health gate saw `OOMKilled`, `exited`, or a restart loop | Believe it. Those three do not recover by waiting. `docker logs <name>` |
| Compose error about `env_file: ../.env` | `.env` does not exist | `bash scripts/preflight.sh` creates it from `.env.example`. The path is `../.env` because the compose files live in `deployment/` and `.env` stays at the repo root |
| Preflight exits 1 listing keys, without asking | No TTY - piped, CI, or a hook | By design. Fill `.env` by hand, or run from a real terminal |
| No traces in LangSmith | `LANGSMITH_API_KEY` is blank, so `tracing.configure()` forces `LANGSMITH_TRACING=false` | Optional, and not a fault. Set the key in `.env` to turn it on. The local trace works either way: `uv run python -m agentcore.tracing "question"` |
| Want to see what a run did, with no account | hosted tracing is off | `uv run python -m agentcore.tracing "question"` prints all nine stages with per-stage timings, straight from the audit trail. DECISIONS D47 |
| `h2-neo4j` still running after a deploy | it was created before Neo4j was removed, and compose no longer declares it, so `down` cannot stop it | `deploy.sh` sweeps it on every run. By hand: `docker stop h2-neo4j`. DECISIONS D48 |
| A new corpus was just indexed | Everything derived from the corpus is now stale: ceiling, labels, numbers, charts | **[CORPUS.md](CORPUS.md)**, in order |
| Retrieval returns nothing for sensible questions | The distance ceiling is calibrated to a different corpus | Recalibrate - see DECISIONS.md D14. This is a 20-minute scheduled task, not a bug |
| Retrieval returns confident nonsense | Same cause, other direction | Same fix |
| `Expected a Python module at: src\hackathon2\__init__.py` | `uv_build` wants a package named after the project | Already solved by `[tool.uv.build-backend] module-name = [...]`. Do not "fix" it by renaming packages |
| MCP client cannot reach the server | Transport mismatch | `MCP_MODE=stdio` spawns a subprocess; `http` needs the `mcp-systems` container. Compose overrides `MCP_MODE=http` for the `api` service |

---

## Landmines carried forward

Learned the hard way on the previous project. They still apply wherever the pattern recurs.

- **The stack lives in `deployment/`.** Every `docker compose` command needs `-f deployment/...`. The scripts already do this; a command copied from an older note will not.
- **Never `docker compose down -v`.** It deletes volumes. Here that is the vector index and the
  checkpoints; there it also destroyed seeded API keys. `down` without `-v` is fine.
- **Percent-encode passwords in any database URL.** `@` must be `%40`. Build URLs with
  `urllib.parse.quote`, never by hand - a `#` silently truncated a password once, and Prisma
  rejected an unencoded `@` another time.
- **The heap-from-cgroup trap** (above). It cost a day on hackathon 1 because the container reported
  `OOMKilled=false` and exit code 0, so it did not look like a memory problem at all.
- **Do not cap Redis memory** when its eviction policy is `noeviction` - a cap makes writes *fail*
  rather than evict. (No Redis here, but the pattern recurs.)
- **A silent integration is worse than a broken one.** Tracing and storage failures tend to return
  empty rather than raise. `/healthz` reports the booleans so a dead integration is visible.

---

## Things that are deliberate, not bugs

- `api` and `mcp-systems` build the **same image**. One Dockerfile, two entrypoints.
- `.env` says `localhost`; compose overrides it with in-network DNS names. One file, two deployment
  modes - do not "fix" the localhost values.
- Stateful services bind to `127.0.0.1` on purpose.
- `deploy.sh` converges every run even when everything is healthy. Memory limits only apply at
  container **creation**, so skipping `up` would print a profile and change nothing.

---

## The two modes

```bash
bash scripts/local.sh              # services in Docker, API on the host with reload
bash scripts/local.sh --services   # services only - then run CLIs against them
bash scripts/local.sh --stop       # stop the services
bash scripts/deploy.sh             # everything containerised, as a grader runs it
```

Develop in local mode, demo in deploy mode. The only difference is hostnames, and neither `.env`
nor any compose file is edited to switch.

## The sync/async bridge

Everything async in this system runs on **one event loop per process**, started on first use and
open for the life of the process (`src/agentcore/aio.py`). Nothing should call `asyncio.run`
directly.

If you see **`RuntimeError: Event loop is closed`** from inside a tool call, that is the symptom of
somebody reintroducing a per-call loop. The tool is fine; the bridge under it is dead. MCP tools
carry a session and a stdio subprocess transport bound to the loop they were fetched on, so
fetching them under one loop and invoking them under another cannot work.

On Windows the loop owns overlapped I/O handles for that subprocess, and interpreter exit races it.
The shutdown is deliberately silent about that - see PROBLEMS P43.

## Inspecting what the model actually receives

```bash
DOMAIN=sample_policy uv run python -m agentcore.inspect "how fast must we report a breach"
DOMAIN=sample_policy uv run python -m agentcore.inspect --full --scope payments "..."
```

Prints routing, each retrieval arm, what survived the distance gate, what the planner sees, the
full assembled prompt and its token cost. Makes no LLM call.

**Use it before touching a prompt.** "The answer was wrong" is four different problems and only
this tells you which one you have.

## Retrieval troubleshooting, in order

| Symptom | First check | Then |
|---|---|---|
| Refuses everything | `uv run python -m evaluation.calibrate` | the ceiling is too tight for this corpus |
| Confident nonsense | same | the ceiling is too loose |
| Right answer, wrong citation | `agentcore.inspect` | the chunk was retrieved but ranked below a better-scoring wrong one |
| Nothing found for an exact ID | is `hybrid=True`? | embeddings flatten identifiers; BM25 is what finds them |
| Hybrid made things worse | `evaluation.retrieval_eval` compares both arms | normal for paraphrased prose - set `hybrid=False` |
| Graph tool says "unavailable" | is Neo4j running? `docker ps` | the arm is optional; it degrades to `search_corpus` |

## MCP

```bash
# stdio (local dev, tests) - the server is a subprocess, nothing to start
MCP_MODE=stdio uv run python -m agentcore.inspect "..."

# http - start the server first
DOMAIN=sample_ops MCP_TRANSPORT=streamable-http uv run python -m mcp_servers --server systems
MCP_MODE=http MCP_URL=http://localhost:8100/mcp DOMAIN=sample_ops uv run ...
```

Two spellings, both correct: the **server** takes `streamable-http` (hyphen), the **client** takes
`streamable_http` (underscore).

Two traps, both already handled but worth knowing if you touch this code:

- **MCP tools are async-only.** `func` is None and `coroutine` is set, so a synchronous `.invoke()`
  raises *"StructuredTool does not support sync invocation"*. `s6_act` always uses `ainvoke`.
- **The stdio server needs `--domain` passed explicitly.** It loads its own domain in a subprocess;
  relying on an inherited `DOMAIN` gives you a server for the default domain, which usually has no
  `systems()` and so returns **zero tools with no error**.

---

## Vector store backends

```bash
# default: pgvector, needs the postgres container
DOMAIN=sample_policy uv run python -m agentcore.rag.index

# chroma: in process, persists to .chroma/, no container at all
DOMAIN=sample_policy VECTOR_BACKEND=chroma uv run python -m agentcore.rag.index --reset
DOMAIN=sample_policy VECTOR_BACKEND=chroma uv run python -m evaluation.retrieval_eval
```

Measured identical on the same corpus and questions. Use Chroma when Docker is unavailable or when
you want a fast reindex loop; use pgvector for the demo, since it is what the compose stack runs.

Each backend keeps its own index. Switching backends means re-indexing.

## The terminal console

Full guide: [CONSOLE.md](CONSOLE.md). The short version:

```bash
DOMAIN=vendor_risk uv run python -m agentcore.console          # interactive
DOMAIN=vendor_risk uv run python -m agentcore.console --demo   # the hardest eval case
uv run python -m agentcore.console "a question"                # one shot
uv run python -m agentcore.console --quiet "a question"        # answer only
```

It starts in under a second, needs no port, and survives over ssh. It streams
the stages as they finish, so on a 112 second assessment you can see where the
time is going rather than watching a blank prompt.

Commands inside it: `:help`, `:stages`, `:plan`, `:audit`, `:evidence`,
`:metrics`, `:domain <name>`, `:domains`, `:quiet`, `:loud`, `:quit`.
`:metrics` scores the last run against the same metrics the eval suite uses,
which makes "did that change help" a two second question.

The approval prompt offers three answers and **fails closed**: a piped stdin
that runs out, a Ctrl-C, or an unparseable answer all mean reject. That is the
same contract the API and the Chainlit dialog follow.

One shot mode exits **non zero** when the answer was refused or missing, so it
composes into a shell pipeline like any other command.

Styling respects `NO_COLOR` and turns itself off when stdout is redirected, so
piping to a file or a screenshot gives clean ASCII.

## The frontend

**It is deployed. Nothing to start by hand:**

```
http://localhost:8030
```

`deploy.sh` brings it up with everything else and prints the address at the end. Run it standalone
only when you want auto-reload while editing the UI:

```bash
DOMAIN=vendor_risk uv run chainlit run src/agentcore/api/chainlit_app.py -w --port 8030
```

That binds 8000 without `--port`, which collides with the course units, so pass it. It shows which stages ran, the
plan with per step risk, and an approval dialog for anything high risk. Dismissing that dialog or
letting it time out counts as a **rejection**, the same rule the API and the CLI follow.

The approval dialog offers three answers, not two: **Approve**, **Approve with conditions** and
**Reject**. Choosing the middle one prompts for the conditions, one per line, and they are carried
into the report. Give none and it degrades to a plain approval - the UI says so rather than
letting you believe you attached something you did not.

For a domain that declares risk domains, the answer arrives with side panels: the risk findings one
row per domain (including the ones marked **not assessed**, which is a result rather than an
omission), the claims grouped by what each one rests on, any contradictions, and the conditions.
The headline names **who decided** - a model recommendation and a human sign off must never look
alike.

Run `uv run pytest tests/test_chainlit_view.py` to exercise the renderer without a browser. It
stubs `chainlit`, so it catches the class of bug that a UI otherwise only reveals when a person
clicks.

## Recording results and making charts

```bash
DOMAIN=sample_policy uv run python -m evaluation.retrieval_eval --force-hybrid
DOMAIN=sample_policy uv run python -m evaluation.calibrate
uv run python -m evaluation.ledger        # print everything recorded so far
uv run python -m evaluation.charts        # render the PNGs for slides
```

The ledger is append only. Charts read the most recent value per arm, so re-running an experiment
updates the picture rather than adding a second bar beside the first.

Everything generated lands in **`evaluation-results/`** at the repo root - `results.json` and
`results.csv` for the ledger, `charts/*.png` for the deck, `reports/` for one JSON per gate run.
Nothing generated is written inside `src/`.

`uv run python -m evaluation.agent_eval` records the ten metrics alongside the pass rate. The gate
(`bash scripts/eval_gate.sh`) thresholds `citation_correctness` at 0.90 and `task_completion` at
1.00 and **exits 1** when either slips.

Stage timings are a command too:

```bash
DOMAIN=vendor_risk uv run python -m evaluation.timing
```

It streams the graph, times each node, and records the result. It exists because the previous
timings were hand-measured once and one of them was timing a stage that crashed on every call. A
number that only one person can reproduce is not a measurement. Current figure for the full
assessment: about **86 seconds**, with `s6_act` at 51s across 5 steps. Worth knowing before
promising a live demo.

## Where the code lives, and how it reaches git

**One directory, one repo.** This project IS its own repository:

| | |
|---|---|
| Here | `C:/projects/hackathon2` - `.venv`, `.env` and the Chroma index live here |
| Remote | `https://github.com/Tsilispyr/hack2tests`, branches `main` and `dev-pipis` |

### The branch rule

**`dev-pipis` is the integration branch. Everything lands there first: teammates' branches, your
own work, all of it. `main` only ever fast forwards from `dev-pipis` once it is green.**

```
teammate branch  ---                     >--- dev-pipis --- (verify) --- main
your work        ---/
```

```bash
# your own work
git add -A && git commit && git push origin dev-pipis

# a teammate's branch
git fetch origin
git checkout dev-pipis
git merge origin/<their-branch>

# then verify, and only then
uv run pytest -q
git checkout main && git merge --ff-only dev-pipis && git push origin main
git checkout dev-pipis
```

`--ff-only` on the main merge is deliberate: if it refuses, main has drifted and you want to know
before overwriting anything.

### What "verify" means before main

Not a feeling. These, in this order, because each is cheaper than the next:

```bash
uv run pytest -q                                      # must be green
bash -n scripts/*.sh                                  # a merged script that cannot parse
docker compose config --quiet                         # a merged compose that cannot start
DOMAIN=vendor_risk uv run python -m evaluation.retrieval_eval   # both arms AND the gated row
bash scripts/preflight.sh < /dev/null                 # a fresh clone must be able to start
```

The preflight one is there because it has already been broken by a merge: a branch renamed a
required key and left the old name in `REQUIRED_KEYS`, so no fresh clone could satisfy it and
`deploy.sh`, which sources preflight, was blocked. Nothing else in the list catches that.

For a change that could plausibly move answer quality, add
`uv run python -m evaluation.reliability --runs 10 --label <what-changed>` and compare against the
previous arm in the ledger. A single agent eval run cannot tell you: its pass rate swings 0.167 to
0.583 on unchanged code (PROBLEMS P53).

### The history worth knowing

This used to live as a `hackathon2/` folder inside the ACCENTURE-HACKATHON repo beside
`HACKATHON_1/`, synced across by `scripts/sync_to_repo.sh`. Two copies of a tree that must agree is
how a stale one gets committed, and the script existed to manage exactly that hazard.

On 2026-09-23 the eight hackathon2 commits were removed from ACCENTURE-HACKATHON (both `main` and
`dev-pipis` force-pushed back to their pre-hackathon2 tips) and the project moved here, to its own
repo. That repo is now hackathon 1 only, with a clean history.

`scripts/sync_to_repo.sh` is therefore **obsolete** and kept only because it still works if anyone
ever wants to vendor this into another tree. Nothing in the normal workflow calls it.

**Commit identity matters here.** GitHub blocks pushes that would expose a private email. Use the
account's public address:

```bash
git config user.email spyrostsl456@gmail.com
```

A push rejected with *"email privacy restrictions"* is this, and nothing else.

## Stopping everything and freeing ports

```bash
cd /mnt/c/projects/hackathon2 && docker compose -f deployment/docker-compose-infra.yaml down
cd /mnt/c/projects/ACCENTURE-HACKATHON-main/HACKATHON_1 && docker compose -f docker-compose-langfuse.yaml down
```

Never add `-v`. It deletes the volumes, which here means the vector index and the checkpoints, and
in hackathon 1 means seeded API keys that cannot be recreated.
