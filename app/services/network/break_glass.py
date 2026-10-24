"""Break-glass gate for direct ACS/OLT writes that bypass the ONT lifecycle.

The ONT service-configuration lifecycle is the single writer for ONT/OLT/ACS
state (2026-10-19 decision). The few remaining direct device writes are
operator-only escape hatches. Each must be admin-only, carry a written reason,
and leave an audit event before it runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services.audit_helpers import log_audit_event

BREAK_GLASS_ROLE = "admin"
BREAK_GLASS_LABEL = "Break-glass"
BREAK_GLASS_MIN_REASON = 10
BREAK_GLASS_MAX_REASON = 500
BREAK_GLASS_NOTICE = (
    "Break-glass: writes straight to the device and bypasses the ONT "
    "configuration lifecycle. Admin only; the reason is audited."
)


@dataclass(frozen=True, slots=True)
class BreakGlassDecision:
    allowed: bool
    message: str
    reason: str = ""


def _request_roles(request: object) -> set[str]:
    state = getattr(request, "state", None)
    auth = getattr(state, "auth", None) or {}
    return {str(role) for role in (auth.get("roles") or [])}


def authorize_break_glass(
    db: Session,
    request: object,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    reason: str | None,
    metadata: dict[str, object] | None = None,
) -> BreakGlassDecision:
    """Check role and reason, then persist the audit event. Fail closed.

    ``log_audit_event`` commits the event itself, so the record exists before
    the caller performs the device write.
    """
    if BREAK_GLASS_ROLE not in _request_roles(request):
        return BreakGlassDecision(False, "Break-glass actions require an admin.")
    text = " ".join((reason or "").split())
    if len(text) < BREAK_GLASS_MIN_REASON:
        return BreakGlassDecision(
            False,
            f"A break-glass reason of at least {BREAK_GLASS_MIN_REASON} "
            "characters is required.",
        )
    if len(text) > BREAK_GLASS_MAX_REASON:
        return BreakGlassDecision(False, "The break-glass reason is too long.")
    payload: dict[str, object] = dict(metadata or {})
    payload.update({"break_glass": True, "reason": text, "action": action})
    log_audit_event(
        db=db,
        request=request,
        action=f"network.break_glass.{action}",
        entity_type=resource_type,
        entity_id=resource_id,
        actor_id=None,
        metadata=payload,
    )
    return BreakGlassDecision(True, "ok", text)
