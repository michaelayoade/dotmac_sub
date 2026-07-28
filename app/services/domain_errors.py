"""Transport-neutral errors returned by public domain service boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class DomainError(Exception):
    """Stable, safe error contract for adapters to translate.

    ``code`` is the machine contract. ``message`` is safe to expose to an
    operator or client. ``details`` must contain structured, non-secret
    decision evidence only; transport status codes do not belong here.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if not code.strip():
            raise ValueError("domain error code cannot be empty")
        if not message.strip():
            raise ValueError("domain error message cannot be empty")
        self.code = code
        self.message = message
        self.details = dict(details or {})
        super().__init__(message)
