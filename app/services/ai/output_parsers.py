"""Parse and validate an LLM's textual output into a structured object.

A model returns prose; a persona needs a dict. Failures here are the model's
fault, not the caller's, so they surface as ``AIClientError`` — the same
error class as a transport failure, because from the caller's point of view
"the provider gave us nothing usable" is one condition.

Ported verbatim from dotmac_crm.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.ai.client import AIClientError


def parse_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise AIClientError("Empty AI response")

    # Strip common code-fence wrappers (only first/last fence lines).
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        data = json.loads(raw)
    except Exception as exc:
        raise AIClientError("AI output was not valid JSON") from exc
    if not isinstance(data, dict):
        raise AIClientError("AI output must be a JSON object")
    return data


def require_keys(data: dict[str, Any], keys: list[str]) -> None:
    missing = [
        k for k in keys if k not in data or data.get(k) is None or data.get(k) == ""
    ]
    if missing:
        raise AIClientError(f"AI output missing required keys: {', '.join(missing)}")
