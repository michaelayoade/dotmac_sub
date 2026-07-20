"""Transport-neutral errors for authoritative work-order commands."""

from __future__ import annotations

from typing import Literal

WorkOrderErrorKind = Literal["invalid", "forbidden", "not_found", "conflict"]


class WorkOrderCommandError(ValueError):
    """A work-order decision was rejected before transport mapping."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        kind: WorkOrderErrorKind = "conflict",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.kind = kind
