# GUARDRAILS

The guardrails pillar (handout section 9, FR08 / FR09 / FR10 / FR12): what each of the six pieces does,
which file holds it, and how it is tested. Decisions and their alternatives are in `DECISIONS.md` (D49).
A longer walk-through in Greek is in `architecture/01-guardrail-regex-llm.pdf`.

The design is defence in depth. The strongest layers are structural (a tool that is not bound cannot be
called; the planner never sees raw retrieved text). Pattern matching and prompts discourage; they are
not relied on alone.

| # | Guardrail | Files | What it does |
|---|---|---|---|
| 1 | Input | `pipeline/s2_guard_in.py`, `safety/patterns.py`, `domains/vendor_risk/policy.py` | Screens the request before anything runs. A regex layer first, then a model second opinion for paraphrased attacks. |
| 2 | Retrieved content | `safety/untrusted.py` | Retrieved text is data, not instruction: wrapped in an UNTRUSTED envelope, and the planner receives only a 220 character summary. |
| 3 | Tool access | `tools/registry.py` | Each step receives only the tool it declared, and only if the caller's role may use it. |
| 4 | Risk gate / human approval | `safety/risk.py`, `pipeline/s5_gate.py` | Risk is `max(floor, assessed)`, never lower than the tool's floor. High risk pauses for a person. |
| 5 | Output | `pipeline/s9_guard_out.py` | Checks claims and citations, redacts PII, scores groundedness, and keeps a High risk vendor from being approved automatically. |
| 6 | Testing | `tests/test_safety_invariants.py`, `tests/test_role_authorization.py` | Prompt-injection tests (handout section 12 asks for at least one), plus negative controls. |

## 1. Input

Layer one is a regex screen: core patterns in `safety/patterns.py` and domain patterns in
`vendor_risk/policy.py`. Layer two (`model_screen`) asks the model whether the message tries to override
rules or authorization, and returns an `InjectionVerdict`. If the model call fails, the request continues
on the regex verdict alone (fail-open); the regex layer is never bypassed. The planted attack in
`vendor-x-proposal.pdf` section 7 is caught by the core patterns alone, with no false positive on the other
ten documents of the knowledge pack.

## 2. Retrieved content

`envelope()` labels each extract with its source and trust level and prepends a banner telling the model to
treat the block as data. `summarise()` is what the planner sees: provenance and 220 characters, never the
full text, so an instruction hidden in a document cannot reach the component that decides what to do.

## 3. Tool access

The per-step allowlist binds only the declared tool (plus read-only retrieval). On top of it, each role has
a ceiling on the low / medium / high scale the risk gate already uses (`ROLE_MAX_RISK`): `user`, `engineer`
and `procurement` up to medium, `admin` up to high, any unlisted role low. A tool above the ceiling is
replaced by a stand-in with the same name and arguments that returns "Denied: ..." and runs nothing. The role
comes from the actor bound for the run (`world.bound`), so the API, Chainlit and the evaluation all supply it.

## 4. Risk gate

`apply_floor` re-stamps every step through the floor table, so a model cannot lower a risk. A plan whose
maximum is High interrupts for approval (`approve`, `approve_with_conditions`, `reject`); a timeout counts as
a rejection, and an approval binds to the plan revision, so a replan clears it.

The gate also skips, rather than refuses, what the caller's role may not run. A step above the role becomes
`skipped` with a failed `StepResult` and a `role_skipped` audit event; the remaining steps run. The level is
recomputed over the steps that will run, so a plan whose only High step was skipped asks for no approval.
The whole request is refused only when no runnable step is left.

## 5. Output

`s9_guard_out.py` runs after the answer is composed:

- claims must rest on a citation or say why they do not, and unsupported claims are marked;
- an unconditional `approve` may not coexist with a missing-evidence claim or an unresolved gap;
- PII is redacted according to the domain's rules: email, phone, address, IBAN, IP address, credit card;
- groundedness is scored and a low score downgrades the answer;
- human authority (FR12): an unconditional `approve` on a plan with any High finding is **downgraded to
  `approve_with_conditions`**, and the unresolved High findings become the conditions attached to it. The rule
  it enforces says a High risk vendor cannot receive an *unconditional* approval - not that no decision may be
  reached - and the corpus agrees: vendor-alpha had this exact shape and was recorded as a conditional
  approval, not a decision deferred. A conditional approval nobody reviewed is labelled as a recommendation
  rather than a final approval. This follows AI-004 section 6 and PR-001 section 4;
- when a step was skipped for the caller's role, the summary ends with a "[Skipped for your role ...]" note.

## 6. Testing

`tests/test_safety_invariants.py` holds the planted-paragraph tests, the paraphrase and second-layer tests, the
PII tests and their negative controls, and the authority tests. `tests/test_role_authorization.py` covers role
ceilings, the denial stand-in, the gate's skip and refusal paths, and that the gate and the executor agree on
what a role may run.

## Not part of the six, but shipped with them

The Chainlit chat has a role picker for the demo. It is a demo device, not authentication: over the API the
role comes from the account that logged in.
