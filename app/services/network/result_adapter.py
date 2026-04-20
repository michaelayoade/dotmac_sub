"""Unified result adapter for OLT/ONT operations.

Provides a consistent response format that can be rendered as:
- HTMX toast notification (for web UI)
- JSON response (for API)
- Redirect with flash message (for traditional forms)

Usage:
    result = OperationResult.success("Authorization queued", data={...})
    return result.to_response(request)  # Auto-detects HTMX vs API vs redirect
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from fastapi import Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.services.adapters.base import AdapterResult, AdapterStatus

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)


class ResultStatus(str, Enum):
    """Operation result status."""

    success = "success"
    error = "error"
    warning = "warning"
    queued = "queued"
    pending = "pending"


@dataclass
class OperationResult:
    """Unified operation result that adapts to different response formats."""

    status: ResultStatus
    message: str
    title: str | None = None
    data: dict | None = None
    redirect_url: str | None = None
    redirect_tab: str | None = None  # Preserve tab on redirect
    duration_ms: int = 8000  # Toast duration

    # Computed properties
    @property
    def success(self) -> bool:
        return self.status in (ResultStatus.success, ResultStatus.queued)

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        return {
            ResultStatus.success: "Success",
            ResultStatus.error: "Error",
            ResultStatus.warning: "Warning",
            ResultStatus.queued: "Queued",
            ResultStatus.pending: "Pending",
        }.get(self.status, "Result")

    def to_adapter_result(self) -> AdapterResult:
        """Convert UI/API operation result to the shared adapter result shape."""
        status_map = {
            ResultStatus.success: AdapterStatus.success,
            ResultStatus.error: AdapterStatus.error,
            ResultStatus.warning: AdapterStatus.warning,
            ResultStatus.queued: AdapterStatus.queued,
            ResultStatus.pending: AdapterStatus.queued,
        }
        return AdapterResult(
            success=self.success,
            message=self.message,
            data=self.data or {},
            status=status_map[self.status],
        )

    @classmethod
    def from_adapter_result(
        cls,
        result: AdapterResult,
        *,
        title: str | None = None,
        redirect_url: str | None = None,
    ) -> OperationResult:
        """Convert shared adapter results to the response-aware operation result."""
        status_map = {
            AdapterStatus.success: ResultStatus.success,
            AdapterStatus.error: ResultStatus.error,
            AdapterStatus.warning: ResultStatus.warning,
            AdapterStatus.queued: ResultStatus.queued,
            AdapterStatus.skipped: ResultStatus.warning,
        }
        return cls(
            status=status_map.get(result.status, ResultStatus.error),
            message=result.message,
            title=title,
            data=result.data,
            redirect_url=redirect_url,
        )

    # Factory methods
    @classmethod
    def ok(
        cls,
        message: str,
        *,
        title: str | None = None,
        data: dict | None = None,
        redirect_url: str | None = None,
    ) -> OperationResult:
        return cls(
            status=ResultStatus.success,
            message=message,
            title=title,
            data=data,
            redirect_url=redirect_url,
        )

    @classmethod
    def error(
        cls,
        message: str,
        *,
        title: str | None = None,
        data: dict | None = None,
        redirect_url: str | None = None,
    ) -> OperationResult:
        return cls(
            status=ResultStatus.error,
            message=message,
            title=title or "Error",
            data=data,
            redirect_url=redirect_url,
        )

    @classmethod
    def queued(
        cls,
        message: str,
        *,
        operation_id: str | None = None,
        data: dict | None = None,
    ) -> OperationResult:
        result_data = data or {}
        if operation_id:
            result_data["operation_id"] = operation_id
        return cls(
            status=ResultStatus.queued,
            message=message,
            title="Operation Queued",
            data=result_data,
        )

    @classmethod
    def warning(
        cls,
        message: str,
        *,
        title: str | None = None,
        data: dict | None = None,
    ) -> OperationResult:
        return cls(
            status=ResultStatus.warning,
            message=message,
            title=title or "Warning",
            data=data,
        )

    # Response adapters
    def to_response(
        self,
        request: Request | None = None,
        *,
        default_redirect: str | None = None,
    ) -> Response:
        """Auto-detect response format based on request headers."""
        if request is None:
            return self.to_json()

        # HTMX request -> toast
        if request.headers.get("HX-Request") == "true":
            return self.to_htmx_toast()

        # Accept: application/json -> JSON
        accept = request.headers.get("Accept", "")
        if "application/json" in accept:
            return self.to_json()

        # Default -> redirect with message
        return self.to_redirect(default_redirect)

    def to_htmx_toast(self) -> Response:
        """Return HTMX response with toast trigger."""
        trigger = {
            "showToast": {
                "type": self.status.value,
                "title": self.display_title,
                "message": self.message,
                "duration": self.duration_ms,
            }
        }
        headers = {"HX-Trigger": json.dumps(trigger)}

        # Optionally trigger a refresh
        if self.data and self.data.get("refresh"):
            headers["HX-Refresh"] = "true"

        return Response(status_code=200, headers=headers)

    def to_json(self) -> JSONResponse:
        """Return JSON API response."""
        body = {
            "success": self.success,
            "status": self.status.value,
            "message": self.message,
        }
        if self.data:
            body["data"] = self.data

        status_code = 200 if self.success else 400
        return JSONResponse(content=body, status_code=status_code)

    def to_redirect(self, default_url: str | None = None) -> RedirectResponse:
        """Return redirect with status/message in query params."""
        from urllib.parse import quote_plus, urlencode

        url = self.redirect_url or default_url or "/"

        # Add tab parameter if specified
        if self.redirect_tab and "?" not in url:
            url = f"{url}?tab={self.redirect_tab}"
        elif self.redirect_tab:
            url = f"{url}&tab={self.redirect_tab}"

        # Add status message params
        separator = "&" if "?" in url else "?"
        params = {
            "status": self.status.value,
            "message": self.message,
        }
        url = f"{url}{separator}{urlencode(params, quote_via=quote_plus)}"

        return RedirectResponse(url=url, status_code=303)


@dataclass
class AutofindResult:
    """Result from OLT autofind scan."""

    success: bool
    message: str
    entries: list[dict] = field(default_factory=list)
    olt_id: str | None = None
    olt_name: str | None = None
    scan_duration_ms: int | None = None

    def to_operation_result(self) -> OperationResult:
        """Convert to generic OperationResult for response."""
        if not self.success:
            return OperationResult.error(self.message)

        count = len(self.entries)
        if count == 0:
            return OperationResult.ok(
                "No unregistered ONTs found",
                title="Scan Complete",
                data={"count": 0, "olt_id": self.olt_id},
            )

        return OperationResult.ok(
            f"Found {count} unregistered ONT{'s' if count != 1 else ''}",
            title="Scan Complete",
            data={
                "count": count,
                "olt_id": self.olt_id,
                "entries": self.entries[:10],  # Limit for toast
            },
        )


@dataclass
class AuthorizationResult:
    """Result from ONT authorization."""

    success: bool
    message: str
    operation_id: str | None = None
    ont_id: str | None = None
    serial_number: str | None = None
    fsp: str | None = None
    queued: bool = False

    def to_operation_result(self) -> OperationResult:
        """Convert to generic OperationResult for response."""
        data = {}
        if self.operation_id:
            data["operation_id"] = self.operation_id
        if self.ont_id:
            data["ont_id"] = self.ont_id
        if self.serial_number:
            data["serial_number"] = self.serial_number
        if self.fsp:
            data["fsp"] = self.fsp

        if not self.success:
            return OperationResult.error(
                self.message,
                title="Authorization Failed",
                data=data or None,
            )

        if self.queued:
            return OperationResult.queued(
                self.message,
                operation_id=self.operation_id,
                data=data or None,
            )

        return OperationResult.ok(
            self.message,
            title="Authorization Complete",
            data=data or None,
        )
