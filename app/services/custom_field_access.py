"""Object-aware native authorization for registered custom-field targets."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.work_order import WorkOrder
from app.services import custom_field_capabilities
from app.services.auth_dependencies import grant_scopes_for_permission


def target_access_allowed(
    db: Session,
    *,
    auth: dict,
    target_type: str,
    target_id: UUID,
    write: bool,
) -> bool:
    """Return whether the principal may reach this exact native record.

    Global/direct grants retain their normal behavior. Scoped grants fail closed
    unless the target has an explicit scope adapter; Work Orders currently map
    through their subscriber's reseller and region scopes.
    """

    try:
        target = custom_field_capabilities.target_capability(target_type)
    except custom_field_capabilities.CustomFieldCapabilityError:
        return False
    permission = target.write_permission if write else target.read_permission
    decision = grant_scopes_for_permission(auth, db, permission)
    if decision == "global":
        return True
    if not isinstance(decision, set) or target.key != "work_order":
        return False

    pair = (
        db.query(WorkOrder, Subscriber)
        .outerjoin(Subscriber, Subscriber.id == WorkOrder.subscriber_id)
        .filter(WorkOrder.id == target_id)
        .first()
    )
    if pair is None:
        return False
    _, subscriber = pair
    if subscriber is None:
        return False
    candidates: set[tuple[str, str]] = set()
    if subscriber.reseller_id is not None:
        candidates.add(("reseller", str(subscriber.reseller_id)))
    if subscriber.region:
        candidates.add(("region", subscriber.region))
    return bool(candidates.intersection(decision))


__all__ = ["target_access_allowed"]
