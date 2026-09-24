"""`.env.example` must document every variable the code reads.

An undocumented variable is a setting nobody knows exists: it works on the
machine where it was written and silently takes its default everywhere else.
Four had already drifted out of the example file before this test existed
(VECTOR_BACKEND, CHROMA_DIR, MCP_PORT, AZURE_OPENAI_JUDGE_DEPLOYMENT), and the
first three were exactly the ones needed to run without Postgres - the setup a
new person is most likely to need and least likely to guess.

Checked mechanically because documentation drift is invisible to review.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.workflow

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"

# Read by a library from the environment rather than by our own code, so they
# never appear in a getenv call here.
IMPLICIT = {
    "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_DEPLOYMENT_NAME", "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    "AZURE_EMBEDDING_ENDPOINT", "AZURE_EMBEDDING_API_KEY",
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD",
    "POSTGRES_DB", "VECTOR_COLLECTION",
    "APP_ENV", "APP_PORT", "LOG_LEVEL",
}

# Universal terminal conventions, not configuration for this application.
# `NO_COLOR` and `FORCE_COLOR` mean the same thing to every well behaved CLI
# on the machine (see no-color.org), and a user sets them for their shell
# rather than for us. Listing them in `.env.example` would imply they are
# deployment settings to be filled in, which is exactly the confusion that
# file exists to prevent.
CONVENTIONS = {"NO_COLOR", "FORCE_COLOR"}


def documented() -> set[str]:
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", EXAMPLE.read_text(encoding="utf-8"), re.M))


def read_by_code() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        names |= set(re.findall(r'getenv\(\s*"([A-Z][A-Z0-9_]*)"', text))
        names |= set(re.findall(r'environ\[\s*"([A-Z][A-Z0-9_]*)"\s*\]', text))
    return names


def test_every_variable_the_code_reads_is_documented():
    missing = read_by_code() - documented() - CONVENTIONS
    assert not missing, (
        f".env.example does not document {sorted(missing)}. "
        f"A variable nobody can see is a setting that only works on one machine."
    )


def test_the_example_holds_no_secrets():
    """It is committed, so anything filled in is public. Keys must ship blank."""
    text = EXAMPLE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not re.match(r"^[A-Z][A-Z0-9_]*=", line):
            continue
        name, _, value = line.partition("=")
        if any(word in name for word in ("KEY", "SECRET", "TOKEN")):
            assert value.strip().strip('"') == "", f"{name} must ship empty in .env.example"


def test_the_mcp_port_matches_the_url_it_is_serving():
    """Two places name the same port. They drift, and the failure is a timeout."""
    text = EXAMPLE.read_text(encoding="utf-8")
    port = re.search(r'^MCP_PORT="?(\d+)', text, re.M).group(1)
    url_port = re.search(r'^MCP_URL="[^"]*:(\d+)', text, re.M).group(1)
    assert port == url_port, f"MCP_PORT={port} but MCP_URL points at {url_port}"
