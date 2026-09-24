"""agentcore must contain no scenario vocabulary.

This is the mechanical enforcement of the organisers' own ground rule - don't
hard-code the scenario - and of the claim this scaffold makes: on the day,
`git diff --name-only prep-freeze..HEAD` should list only src/domains/.

If this test fails, something scenario-specific has leaked into the core, and
the next scenario will require editing the core to remove it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentcore.registry import all_domain_names, load_domain

pytestmark = pytest.mark.workflow

CORE = Path(__file__).resolve().parents[1] / "src" / "agentcore"

# Words that are domain vocabulary, not architecture. Drawn from the reference
# domains; anything here appearing in agentcore/ means a leak.
FORBIDDEN = [
    "gdpr", "recital", "regulation 2016/679", "data subject", "supervisory authority",
    "payment-service", "identity-service", "order-service", "incident",
    "restart_service", "scale_connection_pool", "runbook",
]

# Real exceptions, each with a reason.
ALLOWED_IN = {
    # Docstrings cite the reference domains as EXAMPLES of how to use the seam.
    "domain.py": {"article", "clause"},
}


def _source_files() -> list[Path]:
    return [p for p in CORE.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("word", FORBIDDEN)
def test_core_is_free_of_domain_vocabulary(word):
    offenders = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8").lower()
        # Comments and docstrings may reference a domain as an example; code
        # may not. Strip nothing - if the word appears at all, look at it.
        if re.search(rf"\b{re.escape(word)}\b", text):
            allowed = ALLOWED_IN.get(path.name, set())
            if word not in allowed:
                offenders.append(str(path.relative_to(CORE)))
    assert not offenders, f"{word!r} leaked into agentcore: {offenders}"


def test_core_never_imports_a_specific_domain():
    """agentcore reaches domains ONLY through registry.py."""
    offenders = []
    for path in _source_files():
        if path.name == "registry.py":
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"(?:from|import)\s+domains\.(\w+)", text):
            offenders.append(f"{path.name} imports domains.{match.group(1)}")
    assert not offenders, offenders


def test_every_domain_keeps_its_vocabulary_to_itself():
    """A domain's own words should appear in its own package, nowhere else."""
    for name in all_domain_names():
        domain = load_domain(name)
        glossary = [w.strip() for w in domain.glossary().split(",") if len(w.strip()) > 6]
        for word in glossary[:4]:
            for path in _source_files():
                text = path.read_text(encoding="utf-8").lower()
                # Whole words only. 'service' must not match 'serviceable' --
                # the same substring trap the retrieval eval warns about.
                assert not re.search(rf"{re.escape(word.lower())}", text), (
                    f"{name} glossary term {word!r} appears in agentcore/{path.name}"
                )
