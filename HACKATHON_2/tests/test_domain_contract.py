"""Every registered domain must satisfy the seam.

Parametrised over registry.all_domain_names(), so a domain written on the day is
checked by this existing test without anyone editing it. At 16:15 the machine
tells you what you forgot, instead of a stack trace doing it at 16:50.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel

from agentcore.contracts import Actor, Corpus, EvalCase, GraphQuery, Request, RetrievalPolicy
from agentcore.domain import Domain
from agentcore.registry import all_domain_names, load_domain

pytestmark = pytest.mark.workflow

DOMAINS = all_domain_names()


@pytest.fixture(params=DOMAINS)
def any_domain(request):
    return load_domain(request.param)


def test_at_least_two_reference_domains_exist():
    """One reference domain proves nothing about generalisation."""
    assert len(DOMAINS) >= 2, f"only found {DOMAINS}"


def test_satisfies_the_protocol(any_domain):
    assert isinstance(any_domain, Domain)


def test_has_a_name_that_matches_its_package(any_domain):
    assert any_domain.name in DOMAINS


def test_persona_is_substantial(any_domain):
    """A one-line persona produces a one-line assistant."""
    assert len(any_domain.persona()) > 80


def test_corpus_is_well_formed(any_domain):
    corpus = any_domain.corpus()
    assert isinstance(corpus, Corpus)
    assert corpus.collection
    assert corpus.chunk_size > corpus.chunk_overlap, "overlap must be smaller than the chunk"


def test_corpus_files_exist_if_declared(any_domain):
    from pathlib import Path

    for path in any_domain.corpus().paths:
        assert Path(path).exists(), f"{any_domain.name} declares a missing corpus file: {path}"


def test_retrieval_policy_is_sane(any_domain):
    policy = any_domain.retrieval_policy()
    assert isinstance(policy, RetrievalPolicy)
    assert policy.k >= 1
    if policy.max_distance is not None:
        # Cosine distance. Above ~1.0 the ceiling stops filtering anything.
        assert 0 < policy.max_distance <= 1.2, "a ceiling this high filters nothing"


def test_every_tool_has_a_description(any_domain):
    """The docstring IS the schema the model reads. A missing one is a bug."""
    for tool in any_domain.local_tools():
        assert tool.description and len(tool.description) > 20, f"{tool.name} has no usable description"


def test_every_system_function_has_a_docstring(any_domain):
    for function in any_domain.systems():
        assert function.__doc__, f"{function.__name__} has no docstring"
        assert inspect.signature(function).parameters, f"{function.__name__} takes no arguments"


def test_mutating_operations_are_in_the_risk_table(any_domain):
    """A mutating tool missing from action_risk() defaults to 'low' - a real gap."""
    floors = any_domain.action_risk()
    for function in any_domain.systems():
        assert function.__name__ in floors, (
            f"{function.__name__} mutates state but has no risk floor. "
            f"Add it to {any_domain.name}.action_risk()."
        )


def test_the_planner_can_name_every_real_tool(any_domain):
    vocabulary = set(any_domain.tool_vocabulary())
    for tool in any_domain.local_tools():
        assert tool.name in vocabulary
    for function in any_domain.systems():
        assert function.__name__ in vocabulary


def test_parse_request_handles_both_shapes(any_domain):
    actor = Actor(id="t")
    from_text = any_domain.parse_request("a plain question", actor)
    from_record = any_domain.parse_request({"id": "X-1", "description": "a record"}, actor)
    assert isinstance(from_text, Request) and from_text.raw_text
    assert isinstance(from_record, Request) and from_record.id == "X-1"


def test_report_schema_is_a_pydantic_model(any_domain):
    schema = any_domain.report_schema()
    assert issubclass(schema, BaseModel)
    assert schema.model_fields, "an answer schema with no fields shapes nothing"


def test_eval_cases_are_usable(any_domain):
    cases = any_domain.eval_cases()
    assert cases, f"{any_domain.name} has no eval cases - nothing can be measured"
    for case in cases:
        assert isinstance(case, EvalCase)
        assert case.question.strip()
        # Either it is gradeable by retrieval, or it is gradeable by rubric.
        assert case.expected_labels or case.rubric or case.adversarial


def test_has_at_least_one_adversarial_case(any_domain):
    """If nothing is expected to be refused, refusal is never tested."""
    assert any(c.adversarial for c in any_domain.eval_cases()), (
        f"{any_domain.name} has no adversarial eval case"
    )


def test_graph_queries_are_parameterised_not_generated(any_domain):
    """Named Cypher written by a human. Never a model writing queries."""
    for name, query in any_domain.graph_queries().items():
        assert isinstance(query, GraphQuery)
        assert query.cypher.strip()
        for param in query.params:
            assert f"${param}" in query.cypher, f"{name} declares ${param} but never uses it"


def test_the_two_reference_domains_are_genuinely_different_shapes():
    """The claim the scaffold rests on: the seam carries more than one shape."""
    if not {"sample_policy", "sample_ops"} <= set(DOMAINS):
        pytest.skip("reference domains not both present")

    policy, ops = load_domain("sample_policy"), load_domain("sample_ops")
    assert len(policy.corpus().paths) >= 1 and not policy.systems(), "policy should be corpus-only"
    assert ops.systems(), "ops should have mutating operations"
    assert ops.graph_queries() and not policy.graph_queries(), "only ops should use the graph arm"


# ------------------------------------------------------------ specialists ---
#
# At least two specialist agents. This was FR07 before the official handout
# deleted that row; the check stays because the capability does. Mechanical checks that
# a domain's specialists are usable, parametrised over every domain so a new
# one cannot ship a subtly broken set.


def test_specialists_are_well_formed(any_domain):
    """Catches the `prompt` vs `system_prompt` key error, which fails SILENTLY.

    The library reads the prompt with `spec.get("system_prompt", "")`, so a
    domain using the wrong key gets a specialist with an empty prompt, no
    error, and reviews indistinguishable from the main agent's.
    """
    specialists = any_domain.specialists()
    names = [s["name"] for s in specialists]
    assert len(names) == len(set(names)), f"duplicate specialist names: {names}"

    for spec in specialists:
        assert spec["name"].strip(), "a specialist with no name cannot be assigned"
        assert len(spec["description"]) > 40, f"{spec['name']}: describe WHEN to pick it"
        assert len(spec.get("system_prompt", "")) > 40, (
            f"{spec['name']}: no system_prompt. Check the key is not `prompt`"
        )


def test_the_planner_can_name_every_specialist(any_domain):
    """Mirrors the tool vocabulary check: a name the planner emits must validate."""
    domain = any_domain
    vocabulary = domain.specialist_vocabulary()
    assert vocabulary == [s["name"] for s in domain.specialists()]


def test_the_risk_table_names_no_tool_that_does_not_exist():
    """The reverse of the check above, and it caught a real one.

    `test_every_tool_has_a_risk_floor` runs tools -> table, so a tool nobody
    listed is caught. Nothing ran table -> tools, so `submit_for_signoff` sat
    in ACTION_RISK as a high risk tool for as long as it existed, and it was
    never implemented.

    Harmless on its own: a floor for a tool nobody can call never applies. But
    it is a claim the code cannot back, and it appeared in every discussion of
    which tools a role may use, including one where a phantom tool looked
    permitted because no tool of that name was there to deny.
    """
    from agentcore.registry import load_domain
    from agentcore.tools.registry import _all_tools

    for name in ("vendor_risk", "sample_policy", "sample_ops"):
        domain = load_domain(name)
        real = {tool.name for tool in _all_tools(name)}
        phantom = sorted(set(domain.action_risk()) - real)
        assert not phantom, (
            f"{name}'s action_risk() lists {phantom}, which are not tools. Either implement "
            f"them or drop the entry: a risk floor for a tool nobody can call is a claim "
            f"the code cannot back."
        )
