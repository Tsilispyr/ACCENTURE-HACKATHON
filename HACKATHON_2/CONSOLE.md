# CONSOLE

The terminal front end. The whole pipeline, no browser, no port, no localhost.

```bash
uv run python -m agentcore.console
```

Works as written in PowerShell, Git Bash, WSL, Linux and macOS. `DOMAIN` and `VECTOR_BACKEND` come
from `.env`, so no prefix is needed and none should be added: `DOMAIN=vendor_risk uv run ...` is
bash syntax, and PowerShell reads it as a command name and fails with *"The term
'DOMAIN=vendor_risk' is not recognized"*.

To point at a different domain, the flag works in every shell and needs no export:

```bash
uv run python -m agentcore.console --domain deterministic
```

That is the whole setup. It starts in under a second.

**File:** [src/agentcore/console.py](src/agentcore/console.py)

---

## Why this exists alongside the web UI

Chainlit is the thing to **show a room**: it renders the plan, the approval dialog and the side
panels, and it looks like a product.

This is the thing to **use**. It needs no port, survives over ssh, works inside tmux, and prints
the stages as they happen rather than after. For anyone testing a change, the round trip of edit,
run, read the audit trail is the entire job, and a browser is in the way of it.

There is no second code path. It drives the same `StateGraph` the API runs, so if the console
works, the product works.

---

## Running it

| Command | What it does |
|---|---|
| `uv run python -m agentcore.console` | interactive session |
| `uv run python -m agentcore.console "your question"` | ask once, print, exit |
| `uv run python -m agentcore.console --demo` | run that domain's hardest eval case |
| `uv run python -m agentcore.console --quiet "..."` | answer only, no stage timings |
| `uv run python -m agentcore.console --domain vendor_risk` | pick a domain without editing `.env` |

**Mind the domain.** `.env` ships with `DOMAIN="sample_policy"`, which is the GDPR reference
corpus. The hackathon scenario is `vendor_risk`, so either export it per run or change that line.

```bash
uv run python -m agentcore.console --demo
```

`python -m` rather than a script in `scripts/` because it is a Python entry point, not a shell
wrapper. Same pattern as `python -m evaluation.agent_eval`.

---

## Commands inside a session

Type a question to ask it. Anything starting with a colon is a command.

| Command | Shows |
|---|---|
| `:help` | this list |
| `:stages` | the nine stages, in execution order |
| `:plan` | the last plan, with risk, status, tool and specialist owner per step |
| `:audit` | the full audit trail of the last run, every event |
| `:evidence` | what retrieval actually returned, with citations |
| `:metrics` | score the last run against the assessment metrics |
| `:domain <name>` | switch domains without restarting |
| `:domains` | list what is available |
| `:history` | the conversation so far, and which turns are still being carried |
| `:new` | forget the conversation and start fresh |
| `:quiet` / `:loud` | hide or show per stage timings |
| `:quit` | leave (Ctrl-D and Ctrl-C also work) |

### It is a conversation, not a series of questions

Ask something, read the answer, then ask a follow up. The last few turns are
carried as context, so `it`, `that` and `the commercial side` resolve against
what you just asked:

```
> Above what contract value do we need procurement committee approval?
  ... EUR 250,000 total cost of ownership over the term ...

> And what about the security certification?
  (carrying 1 earlier turn(s))
  ... ISO 27001 or a SOC 2 Type II issued within twelve months ...
```

`:history` shows the conversation and marks which turns are still in range.
`:new` clears it. Switching domain with `:domain` clears it too, because a new
domain means a new corpus and a follow up would resolve against evidence that
no longer exists.

**Each question is still its own graph run**, with its own thread and its own
approval gate. The pipeline is request shaped, and the gate binds to a plan
revision within ONE run, so treating a conversation as a single run would break
it. What carries across turns is context, not state.

**Three turns, 400 characters of each answer.** Deliberately small: this text is
prepended to every request, so an unbounded history would grow the prompt until
retrieval quality and cost both suffered, and the fourth question back is rarely
what `it` refers to.

**A refusal is never remembered.** One bad turn must not become the context the
next question is resolved against.

**On safety:** this feeds model output back in as input, so prior answers are
labelled as context and explicitly not as instructions, and the composed text
still passes through `s2_guard_in` on every turn. An injection that reached an
answer is screened again on its way back in, rather than arriving pre trusted
because we wrote it.

`:metrics` is the one worth knowing. It runs the same metrics the evaluation suite reports against
the run you just did, which turns *"did that change help?"* into a two second question instead of a
fifteen minute eval.

---

## Reading the output

### Stage timings

```
PIPELINE
------------------------------------------------------------------------------
  s1_intake        0.01s  parse the request
  s2_guard_in      0.01s  input guardrails
  s3_ground        6.24s  retrieve evidence
  s4_plan          1.24s  build the plan
  s5_gate          0.00s  risk floor and approval
  s6_act          17.01s  execute a step
  s7_replan        0.00s  done, continue or replan
  s8_compose       8.91s  compose the answer
  s9_guard_out     0.01s  output guardrails
------------------------------------------------------------------------------
  total           33.44s
```

Printed as each stage finishes, not at the end. On a 112 second assessment that is the difference
between watching progress and wondering whether it has hung.

Stage names are shown verbatim rather than prettified, so anything you see here can be grepped for
in `src/agentcore/pipeline/`.

**Typical totals:** a policy lookup is about **33s**, a full four domain assessment about **112s**.
`s6_act` dominates, because it runs once per plan step and each step may delegate to a specialist.

### The answer

For a domain that declares risk domains, the answer comes with:

- **DECISION**, and who made it. A model recommendation and a human sign off are never printed the
  same way, and when they disagree it says so.
- **RISK FINDINGS**, one row per required domain. A domain with nothing found prints
  `NOT ASSESSED` in bold rather than being quietly absent, because silence about a domain reads as
  "nothing to report" and usually means "never looked".
- **CLAIMS**, grouped by what each one rests on: `evidence`, `inference`, `missing`. A claim whose
  stated basis is not honoured is flagged `UNSUPPORTED`. A `missing` claim is **not** flagged,
  because reporting an absence is its content rather than a defect in it.
- **CONTRADICTIONS**, both sides with their sources.
- **SOURCES**, every citation.

---

## The approval gate

High risk steps pause and ask. The prompt shows the plan, the risk of each step, the tool it will
use, and the specialist that owns it:

```
==============================================================================
  APPROVAL NEEDED   Plan revision 1 contains high-risk steps.
------------------------------------------------------------------------------
  s5  high             via record_assessment
      Compile the findings and record an assessment for Asteria.
------------------------------------------------------------------------------
  a = approve    c = approve with conditions    r = reject
  >
```

Three answers, not two. Binary approve or reject forces a reviewer to either accept unmitigated
risk or block everything, and most real sign offs are "yes, provided". Choosing `c` prompts for
conditions, one per line, blank line to finish, and they are carried into the report.

**It fails closed.** A piped stdin that runs out, a Ctrl-C, or an answer it cannot parse all mean
**reject**. Nobody answering must never mean yes. That is the same contract the API and the
Chainlit dialog follow, and it has seven tests of its own in
[tests/test_console.py](tests/test_console.py).

Give `c` with no conditions and it says so, rather than letting you believe you attached something:
the gate degrades that to a plain approval.

---

## Scripting it

One shot mode exits **non zero** when the answer was refused or missing, so it composes like any
other command:

```bash
uv run python -m agentcore.console --quiet "what is the notification window?" \
  && echo "answered" || echo "refused or failed"
```

Useful for a smoke check in CI or a pre demo sanity run.

---

## Styling

Plain ASCII throughout. No box drawing, no emoji, no unicode dashes, because this has to stay
legible over a serial console, inside tmux, through `less`, and in a screenshot pasted into a chat.

Colour is used sparingly for risk levels and decisions, and turns itself off when:

- `NO_COLOR` is set (the [no-color.org](https://no-color.org) convention), or
- stdout is not a terminal, so piping to a file gives clean text

`FORCE_COLOR=1` overrides both if you are piping into something that renders escapes.

---

## When something looks wrong

| What you see | What it means |
|---|---|
| `s3_ground` fast and the answer is vague | Retrieval returned little. Run `:evidence` to see what it actually got, then `evaluation.calibrate` if the corpus changed |
| Every step says `step_failed` in `:audit` | The executor is crashing and the pipeline is replanning around it. Read the `error` field. A `TypeError` there is a programming error, not a tool failure |
| `Could not compose an answer` | A structured output validation error. Check the `s8_compose` audit event |
| Approval prompt appears then immediately rejects | No interactive stdin. That is the fail closed path working. Run it from a real terminal |
| Answer arrives but `:metrics` shows `citation_correctness 0.00` | Claims are citing passages that are not in `state["evidence"]`. See PROBLEMS P52 and the open item in [HANDOVER.md](HANDOVER.md) |
| Nothing retrieved for a sensible question | The distance ceiling may be wrong for this corpus or backend. `uv run python -m evaluation.calibrate` |

More symptoms in [RUNBOOK.md](RUNBOOK.md).

---

## Testing

[tests/test_console.py](tests/test_console.py) covers it without a terminal: 30 tests, no TTY, no
network. The fail closed behaviour and the field rendering get the most attention, because both
are the kind of thing only a person clicking would otherwise catch, and the Chainlit view already
shipped once reading a field that did not exist.

```bash
uv run pytest tests/test_console.py -q
```
