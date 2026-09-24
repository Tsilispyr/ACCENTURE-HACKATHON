"""The data contracts every stage of the pipeline passes around.

These are domain-agnostic on purpose: a Request holds whatever entities the
domain parsed out of the raw text, not a fixed set of fields. Adding a scenario
must never require editing this file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

RiskLevel = Literal["none", "low", "medium", "high"]
StepStatus = Literal["pending", "done", "failed", "skipped"]

# What a statement in the final report is STANDING ON. Keeping these apart is
# the difference between an assessment and an opinion: a reader must be able to
# see which conclusions are carried by a document and which the model reasoned
# its way to.
ClaimBasis = Literal["evidence", "inference", "missing"]

# A decision can be conditional. Binary approve/reject forces a reviewer to
# either accept unmitigated risk or block the whole thing, which is not how any
# real approval works.
Decision = Literal["approve", "approve_with_conditions", "reject", "pending"]

# Ordering for comparisons. The risk floor in safety/risk.py depends on this
# being a total order - see DECISIONS.md D7.
RISK_ORDER: dict[str, int] = {"none": 0, "low": 1, "medium": 2, "high": 3}

# The words a model reaches for instead of the four it was given. Each has an
# unambiguous place on the scale; see normalise_level for why mapping is right
# for a level and wrong for a decision.
_LEVEL_ALIASES: dict[str, str] = {
    "critical": "high", "severe": "high", "major": "high", "red": "high",
    "elevated": "medium", "moderate": "medium", "mid": "medium",
    "amber": "medium", "yellow": "medium",
    "minor": "low", "minimal": "low", "green": "low", "negligible": "low",
    "informational": "low", "info": "low",
    "": "none", "n a": "none", "not applicable": "none", "unknown": "none",
    "not assessed": "none", "none identified": "none", "no": "none",
    "tbd": "none", "not evaluated": "none", "undetermined": "none",
}


def normalise_level(value: str) -> str | None:
    """Map a model's word onto one of the four levels. None if it is not one.

    Mapping is right HERE and would be wrong for a decision: a risk level is an
    ORDINAL scale, where "critical" unambiguously means the top of it. An
    unrecognised decision could mean anything, so s5_gate fails it closed
    instead. The two must not be confused.

    COMPOUND levels round UP. A model writing "medium-high" is between two
    points on the scale, and for risk the conservative read of "between" is the
    higher one. This is the case the first version missed: it replaced the
    hyphen and then looked up "medium high" whole, found nothing, and the
    unmapped value took an entire assessment down with it.
    """
    word = value.strip().lower()
    for separator in ("-", "_", "/"):
        word = word.replace(separator, " ")
    for filler in (" risk", " severity", " level"):
        word = word.replace(filler, "")
    word = word.strip()
    if word in RISK_ORDER:
        return word
    if word in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[word]
    named = []
    for part in word.split():
        mapped = part if part in RISK_ORDER else _LEVEL_ALIASES.get(part, "")
        if mapped in RISK_ORDER:
            named.append(mapped)
    if named:
        return max(named, key=lambda level: RISK_ORDER[level])
    return None


class Actor(BaseModel):
    """Who is asking. `scope` is ANDed into every retrieval filter and tool."""

    id: str
    email: str = ""
    role: str = "user"
    scope: str = "public"


ANONYMOUS = Actor(id="anonymous", role="anonymous", scope="public")


class Request(BaseModel):
    id: str
    raw_text: str
    actor: Actor = ANONYMOUS
    entities: dict[str, str] = Field(default_factory=dict)
    priority: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Evidence(BaseModel):
    """One retrieved passage.

    `trusted` is load-bearing. Anything that came from a corpus, a tool or an
    MCP server is attacker-influenced in a real deployment, so it defaults to
    False and safety/untrusted.py renders it inside an explicit envelope before
    it reaches a prompt.
    """

    text: str
    source: str
    locator: str = ""
    score: float | None = None
    trusted: bool = False

    def cite(self) -> str:
        return f"{self.source}{f' | {self.locator}' if self.locator else ''}"


def _normalise_citation(citation: str) -> str:
    return " ".join(citation.lower().split())


def match_citation(citation: str, known: dict[str, str]) -> str | None:
    """Find the key in `known` that a citation refers to, or None.

    WHY THIS IS NOT `citation.split("|")[0]`, which is what two call sites used
    to do: `cite()` is `source | locator`, and `source` is the COLLECTION name.
    It is identical for every chunk in a corpus, so splitting on the pipe and
    comparing the first field matched every citation against the first piece of
    evidence. One caller scored correct citations as unsupported; the other --
    the guard against fabricated citations - passed everything.

    The locator is the part that discriminates, so the whole string is compared.
    Containment either way tolerates a model that quotes a citation slightly
    short or slightly long, which is the normal failure, without tolerating one
    that names a different document, which is the one being guarded against.
    """
    wanted = _normalise_citation(citation)
    if not wanted:
        return None

    normalised = {key: _normalise_citation(key) for key in known}
    for key, value in normalised.items():
        if value == wanted:
            return key
    # Longest match first, so a citation naming a section does not attach to a
    # shorter key that merely shares a prefix.
    for key, value in sorted(normalised.items(), key=lambda kv: -len(kv[1])):
        if wanted in value or value in wanted:
            return key
    return None


class PlanStep(BaseModel):
    id: str
    description: str
    tool_hint: str | None = None
    risk: RiskLevel = "low"
    status: StepStatus = "pending"

    # WHO executes this step, if a specialist should. A name from
    # domain.specialists(), or None for "the executor does it itself".
    #
    # This is deliberately invisible to safety/risk.py. Risk is a property of
    # the TOOL, not of who holds it, so `apply_floor` and `effective_risk` stay
    # keyed on `tool_hint` alone. An owner can never widen what a step may do.
    owner: str | None = None


class Plan(BaseModel):
    """A to-do list with a revision number.

    The revision is what approval binds to. Replanning bumps it and clears the
    approval, so approving one plan never authorises a later one - DECISIONS.md D8.
    """

    revision: int = 1
    steps: list[PlanStep] = Field(default_factory=list)

    @property
    def pending(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status == "pending"]

    @property
    def max_risk(self) -> RiskLevel:
        if not self.steps:
            return "none"
        return max((s.risk for s in self.steps), key=lambda r: RISK_ORDER[r])


class Claim(BaseModel):
    """One statement in the report, and what it rests on.

    `basis` is the FR05 distinction:
      evidence   a retrieved document says this; `citations` point at it
      inference  the model reasoned it from evidence; the reasoning is stated
      missing    this should have been checkable and the corpus does not cover it

    A "missing" claim is not a failure to report - it is the most useful thing
    an assessment can say, because it names what nobody has verified.
    """

    statement: str
    basis: ClaimBasis = "evidence"
    citations: list[str] = Field(default_factory=list)
    reasoning: str = Field(
        default="", description="Required when basis is inference: how it follows."
    )
    risk_domain: str = ""
    confidence: float = 0.0

    @property
    def is_supported(self) -> bool:
        """Evidence claims need a citation. Inference claims need reasoning."""
        if self.basis == "evidence":
            return bool(self.citations)
        if self.basis == "inference":
            return bool(self.reasoning)
        return True  # a "missing" claim is self-justifying


class Contradiction(BaseModel):
    """Two sources that cannot both be right.

    Worth surfacing rather than silently picking one: in an assessment, a
    contradiction between a vendor's claim and a policy is often the finding.
    """

    statement_a: str
    statement_b: str
    source_a: str = ""
    source_b: str = ""
    note: str = ""


class RiskFinding(BaseModel):
    """The verdict for ONE risk domain.

    FR07 requires several domains assessed. Keeping them as separate findings
    rather than one blob means coverage is checkable: a domain with no findings
    is visibly unassessed instead of quietly absent.
    """

    domain: str
    level: RiskLevel = "none"
    summary: str = ""
    claims: list[Claim] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    assessed: bool = False

    @field_validator("level", mode="before")
    @classmethod
    def _coerce_level(cls, value):
        """Map the word onto the scale. Anything unreadable is left to the salvage."""
        if not isinstance(value, str):
            return value
        return normalise_level(value) or value

    @model_validator(mode="before")
    @classmethod
    def _salvage_an_unreadable_level(cls, data):
        """An unreadable level costs THIS finding, never the whole assessment.

        The earlier version let an unmapped word raise, reasoning that guessing
        is worse than failing. Measured over ten runs of the flagship case, that
        reasoning cost three entire reports: pydantic rejects the whole
        AssessmentDraft, s8_compose catches it, and fourteen claims, four
        findings and the decision are all replaced by "Could not compose an
        answer" (PROBLEMS P55).

        Failing closed on one field is only conservative while the failure stays
        LOCAL. Here it did not, so the finding degrades instead: the domain is
        marked not assessed and the word is recorded in `gaps`. That reads as a
        gap in coverage, which is exactly what it is, and task_completion counts
        it against us rather than scoring it clean.
        """
        if not isinstance(data, dict):
            return data
        raw = data.get("level")
        if not isinstance(raw, str) or normalise_level(raw) is not None:
            return data
        salvaged = dict(data)
        salvaged["level"] = "none"
        salvaged["assessed"] = False
        salvaged["gaps"] = [
            *(data.get("gaps") or []),
            f"unreadable risk level {raw!r}; treated as not assessed",
        ]
        return salvaged


class StepResult(BaseModel):
    step_id: str
    output: str = ""
    tool_calls: list[str] = Field(default_factory=list)
    ok: bool = True
    error: str | None = None
    # DID NOT RUN, as opposed to RAN AND FAILED. Both carry ok=False, and
    # collapsing them marked an answer `partial` whenever a step was skipped
    # for the caller's role - so a user asking something entirely within their
    # authority got a result labelled provisional, with citations, for a step
    # that was never meant to run. A deliberate omission is not a failure.
    skipped: bool = False
    # Carried into s8_compose's "WHAT WAS DONE" block, so a finding can be
    # attributed to the specialist that reached it. This is what makes
    # delegation improve the ANSWER rather than only a metric.
    owner: str = ""


class Answer(BaseModel):
    """The final response. `body` is the domain's own report schema instance."""

    body: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    citations: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    partial: bool = False
    refused: bool = False
    refusal_reason: str | None = None

    # --- assessment structure (FR05, FR07, FR10, FR11) --------------------
    claims: list[Claim] = Field(default_factory=list)
    findings: list[RiskFinding] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    # WHO decided what, kept apart. The model RECOMMENDS; a human DECIDES.
    # Collapsing them into one field produced a report that said "reject" while
    # listing the conditions under which it would be approved - which is not a
    # decision, it is two of them wearing one label.
    recommendation: Decision = "pending"
    decision: Decision = "pending"
    decided_by: str = Field(
        default="", description="'model' until a human answers the gate, then the actor id."
    )
    conditions: list[str] = Field(
        default_factory=list,
        description="What must be true for a conditional approval to stand.",
    )

    @property
    def unsupported_claims(self) -> list[Claim]:
        """Evidence claims with no citation, inference claims with no reasoning."""
        return [c for c in self.claims if not c.is_supported]

    @property
    def assessed_domains(self) -> list[str]:
        return [f.domain for f in self.findings if f.assessed]


class EvalCase(BaseModel):
    """One row of ground truth.

    `expected_labels` drives the retrieval eval and is compared as a WHOLE
    label, never a substring - 'Article 3' is a prefix of 'Article 33'.
    """

    question: str
    rubric: str = "The answer is correct, grounded in the corpus, and cites its sources."
    expected_labels: list[str] = Field(default_factory=list)
    must_not_say: list[str] = Field(default_factory=list)
    adversarial: bool = False

    # Ground truth for the process, not just the answer. These drive the tool
    # and delegation metrics, and they are OPTIONAL on purpose: a case that
    # says nothing about tools is not asserting that no tools were used.
    expected_tools: list[str] = Field(
        default_factory=list,
        description="Tools this case should use. Empty means no expectation.",
    )
    forbidden_tools: list[str] = Field(
        default_factory=list,
        description="Tools this case must NOT use, whatever else it does.",
    )
    expect_delegation: bool = False
    expected_domains: list[str] = Field(
        default_factory=list,
        description=(
            "Risk domains this case must cover. Only an assessment case declares "
            "these; a narrow lookup question covers one thing by design and must "
            "not be marked incomplete for it."
        ),
    )


class Corpus(BaseModel):
    """Where the documents are and how to cut them up."""

    paths: list[str] = Field(default_factory=list)
    collection: str = "corpus"
    chunk_size: int = 1200
    chunk_overlap: int = 150


class RetrievalPolicy(BaseModel):
    """How retrieval behaves for this domain.

    max_distance/relative_margin are COSINE DISTANCE (lower is closer) and must
    be recalibrated per corpus - DECISIONS.md D14.
    """

    k: int = 4
    # Run BM25 alongside the vector arm and fuse by rank. Worth it whenever the
    # corpus contains identifiers, codes or section numbers that embeddings
    # flatten; unnecessary for pure prose that is always paraphrased.
    hybrid: bool = True
    max_distance: float | None = 0.70
    relative_margin: float | None = 0.12
    metadata_filter: dict[str, Any] | None = None
    locators: list[str] = Field(default_factory=list)


class GraphQuery(BaseModel):
    """A named, parameterised Cypher query written by a human.

    Never generated. See DECISIONS.md D5 for why the graph arm is not driven by
    an LLM writing Cypher.
    """

    description: str
    cypher: str
    params: list[str] = Field(default_factory=list)


class RunCritique(BaseModel):
    """What the agentic evaluator returns about a whole run."""

    rubric_scores: dict[str, int] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    passed: bool = True
