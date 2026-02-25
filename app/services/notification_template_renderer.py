"""Template rendering helpers for notification channels."""

from __future__ import annotations

import re
from collections.abc import Mapping

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}|\{\s*([a-zA-Z0-9_]+)\s*\}")


def render_template_text(text: str | None, variables: Mapping[str, object] | None = None) -> str:
    """Render variable placeholders in text.

    Supports both ``{{variable}}`` and ``{variable}`` tokens.
    Unknown placeholders are left unchanged.
    """
    if not text:
        return ""
    values = {str(key): "" if value is None else str(value) for key, value in (variables or {}).items()}

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2)
        if key in values:
            return values[key]
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, text)


def default_preview_variables() -> dict[str, str]:
    """Sample values for template previews/tests."""
    return {
        "customer_name": "Jane Doe",
        "account_number": "AC-1024",
        "invoice_number": "INV-2026-0001",
        "amount_due": "12500.00",
        "due_date": "2026-03-01",
        "technician_name": "NOC Team",
        "eta_time": "14:30",
    }
