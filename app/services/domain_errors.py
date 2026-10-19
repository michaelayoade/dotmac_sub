"""Transport-neutral errors returned by public domain service boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class DomainError(Exception):
    """Stable, safe error contract for adapters to translate.

    ``code`` is the machine contract. ``message`` is safe to expose to an
    operator or client. ``details`` must contain structured, non-secret
    decision evidence only; transport status codes do not belong here.

    ``retryable`` declares whether a durable event-handler caller (see
    ``app.services.events.dispatcher.EventDispatcher.dispatch``) should
    treat this failure as transient (worth a later automatic retry) or
    permanent (a genuine, reviewable failure that will not resolve itself on
    replay). Defaults to ``True`` so every existing raise site keeps its
    current transient/retry-eligible behavior with no code change required;
    a caller that knows a failure is terminal sets ``retryable=False``
    explicitly.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        retryable: bool = True,
    ) -> None:
        if not code.strip():
            raise ValueError("domain error code cannot be empty")
        if not message.strip():
            raise ValueError("domain error message cannot be empty")
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = retryable
        super().__init__(message)

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        """Preserve the keyword-only error contract across worker processes."""

        return (
            _restore_domain_error,
            (type(self), self.code, self.message, self.details, self.retryable),
        )


def _restore_domain_error(
    error_type: type[DomainError],
    code: str,
    message: str,
    details: Mapping[str, Any],
    retryable: bool,
) -> DomainError:
    """Rebuild a domain error without depending on a subclass constructor."""

    restored = error_type.__new__(error_type)
    DomainError.__init__(
        restored,
        code=code,
        message=message,
        details=details,
        retryable=retryable,
    )
    return restored
