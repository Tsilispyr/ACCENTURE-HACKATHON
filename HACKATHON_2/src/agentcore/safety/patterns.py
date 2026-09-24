"""Deterministic input screening. No LLM, no latency, no cost.

This runs before anything else touches the request. It is not clever and it is
not meant to be - a model-based check is a second opinion, not a first line.
See 51-guardrails-theory for the regex-vs-model comparison this is drawn from.

Known limit, stated so nobody mistakes this for sufficiency: a determined
paraphrase gets through. The defences that actually hold are structural - the
planner never sees raw untrusted text, and the risk floor cannot be argued
down. This layer exists to make the obvious attempts cheap to refuse.
"""

from __future__ import annotations

import re

# Prompt injection and instruction override.
# A bounded word gap (\w+\s+){0,3} rather than a fixed phrase: real attempts
# stack qualifiers - "disregard your prior rules", "ignore all the previous
# system instructions" - and an exact-phrase list misses every variant.
BASE_PATTERNS = [
    r"ignore\s+(?:\w+\s+){0,3}?(instructions|rules|prompt|directives)",
    r"disregard\s+(?:\w+\s+){0,3}?(instructions|rules|prompt|directives)",
    r"forget\s+(?:\w+\s+){0,3}?(instructions|rules|prompt|everything above)",
    r"(reveal|show|print|repeat|output)\s+(?:\w+\s+){0,3}?(system prompt|instructions)",
    r"you are now (a|an|in) ",
    r"pretend (you are|to be)",
    r"developer mode|jailbreak|DAN mode",
    r"(disable|bypass|turn off) (your )?(safety|security|guardrails|filters)",
    r"(show|give|print|reveal) (me )?(your )?(api[ _-]?key|secret|password|credentials|token)",
    r"</?(system|assistant)>",          # fake role markers
    r"^\s*system\s*:",                  # a message pretending to be the system turn
    # The qualified form. The patterns above allow three words and a short
    # vocabulary, and the real hackathon corpus was written past both:
    # "IGNORE ALL PREVIOUS POLICIES AND INSTRUCTIONS" has four words in between
    # and names "policies". Requiring previous/prior/above keeps this from
    # firing on an ordinary question like "can we ignore the retention policy".
    r"(ignore|disregard|forget|override)\s+(?:(?:all|any|your|the|these|those|every)\s+)?"
    r"(?:previous|prior|above|earlier|preceding|existing)\s+(?:\w+\s+){0,4}?"
    r"(instructions|rules|prompts?|directives|polic(?:y|ies)|guidelines|constraints|controls)",
    # A note addressed to the machine reader rather than the human one.
    r"(note|message|instructions?)\s+(for|to)\s+(automated|ai|llm|machine)\s+(\w+\s+){0,2}"
    r"(systems?|agents?|reviewers?|assistants?|models?)",
]

MAX_REQUEST_CHARS = 20_000


class Refusal(Exception):
    """Raised when input screening rejects a request."""

    def __init__(self, reason: str, matched: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.matched = matched


def compile_patterns(extra: list[str] | None = None) -> list[re.Pattern[str]]:
    return [re.compile(p, re.I) for p in BASE_PATTERNS + list(extra or [])]


def screen(text: str, patterns: list[re.Pattern[str]]) -> None:
    """Raise Refusal if the text should not be processed."""
    if len(text) > MAX_REQUEST_CHARS:
        raise Refusal(
            f"Request is {len(text):,} characters; the limit is {MAX_REQUEST_CHARS:,}.",
            matched="length",
        )
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            raise Refusal(
                "This request appears to be an attempt to override my instructions, "
                "so I will not act on it.",
                matched=found.group(0)[:80],
            )


def scan(text: str, patterns: list[re.Pattern[str]]) -> str | None:
    """Non-raising form: returns the matched fragment, or None.

    Used where a hit should be RECORDED rather than fatal - notably when
    screening text that came back from a tool, where refusing the whole request
    would let any document shut the system down.
    """
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return found.group(0)[:80]
    return None
