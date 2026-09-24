"""The ONE place agentcore is allowed to know that domains exist.

Everything else imports the active Domain from here. That keeps the dependency
one-way - agentcore -> registry -> domains - and makes
tests/test_no_domain_leakage.py a meaningful check rather than a formality.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from functools import lru_cache

from agentcore.domain import Domain

DEFAULT_DOMAIN = "sample_policy"


@lru_cache(maxsize=None)
def load_domain(name: str | None = None) -> Domain:
    """Import domains.<name> and return its DOMAIN singleton."""
    name = name or os.getenv("DOMAIN", DEFAULT_DOMAIN)
    try:
        module = importlib.import_module(f"domains.{name}")
    except ModuleNotFoundError as error:
        available = ", ".join(all_domain_names()) or "(none)"
        raise RuntimeError(
            f"No domain package 'domains.{name}'. Available: {available}.\n"
            f"Set DOMAIN=<name>, or create it with: "
            f"cp -r src/domains/_template src/domains/{name}"
        ) from error

    domain = getattr(module, "DOMAIN", None)
    if domain is None:
        raise RuntimeError(f"domains.{name} defines no DOMAIN singleton.")
    return domain


def all_domain_names() -> list[str]:
    """Every importable domain except the template.

    test_domain_contract parametrises over this, so a domain added on the day
    is checked by the existing test without anyone editing the test.
    """
    import domains

    return sorted(
        m.name
        for m in pkgutil.iter_modules(domains.__path__)
        if not m.name.startswith("_")
    )


def all_domains() -> list[Domain]:
    return [load_domain(n) for n in all_domain_names()]
