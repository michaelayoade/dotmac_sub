"""Service helpers for admin system API key form/list mutations."""

from __future__ import annotations


def get_api_key_new_form_context() -> dict[str, str | None]:
    """Return default context fragment for API key creation form."""
    return {"error": None}


def parse_scopes(raw: str | None) -> list[str]:
    """Parse the comma/space/newline-separated scopes field into a clean list."""
    if not raw:
        return []
    tokens = raw.replace(",", " ").split()
    seen: list[str] = []
    for token in tokens:
        token = token.strip()
        if token and token not in seen:
            seen.append(token)
    return seen
