"""Transport-neutral errors for the native vendor operations domain."""

from __future__ import annotations

from typing import Literal

VendorPortalErrorKind = Literal["invalid", "forbidden", "not_found", "conflict"]


class VendorPortalOperationError(ValueError):
    """A vendor-domain command was rejected before any transport mapping."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        kind: VendorPortalErrorKind = "conflict",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.kind = kind


class VendorProjectLifecycleError(VendorPortalOperationError):
    """A project lifecycle transition was rejected by its named owner."""
