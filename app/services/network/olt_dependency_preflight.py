"""Fail-fast OLT profile dependency preflight checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.services.network.olt_config_pack_live_audit import audit_olt_config_pack_live


@dataclass
class OltDependencyPreflightResult:
    """Blocking result for OLT dependency validation."""

    success: bool
    message: str
    audit: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)


def format_dependency_audit_errors(errors: list[str]) -> str:
    """Return compact, UI-safe audit error text."""
    return "; ".join(error for error in errors if error)[:800]


def validate_olt_profile_dependencies(
    db: Session,
    *,
    olt_id: str,
    operation: str,
) -> OltDependencyPreflightResult:
    """Validate live OLT profile dependencies before any OLT write operation."""
    audit = audit_olt_config_pack_live(db, olt_id)
    if audit.is_valid:
        return OltDependencyPreflightResult(
            success=True,
            message="OLT profile dependencies are valid.",
            audit=audit.to_dict(),
        )

    errors = audit.errors or ["OLT profile dependency audit failed"]
    return OltDependencyPreflightResult(
        success=False,
        message=(
            f"OLT {operation} dependency audit failed: "
            + format_dependency_audit_errors(errors)
        ),
        audit=audit.to_dict(),
        errors=errors,
    )
