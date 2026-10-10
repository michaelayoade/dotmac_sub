"""Field-plane principal guards (transport layer).

These are FastAPI dependencies, so they live with the API adapters: the
service layer raises typed domain errors and never imports transport
exceptions.

Routes under ``/field`` are self-scoped: the caller's authority comes from
being a technician or a vendor member, not from staff RBAC. ``require_user_auth``
alone only proves *someone* is logged in, which is not enough for a route that
mutates shared plant. These dependencies resolve the caller to the field
identity the route actually requires and refuse otherwise.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.field.execution import field_system_user_id
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.services.auth_dependencies import require_user_auth
from app.services.field.vendor_auth import vendor_context
from app.services.field.work_order_access import FieldAccessError


def require_field_principal(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> dict:
    """Resolve an active technician or vendor member behind this request.

    Plant coordinates are shared operational state, so a merely authenticated
    principal (a subscriber or reseller session, for example) must not reach a
    write. Technicians resolve through their profile; vendor members resolve
    through their active vendor membership.
    """

    from app.services.field.jobs import _profile_from_principal

    field_system_user_id(auth)
    try:
        profile: TechnicianProfile | None = _profile_from_principal(db, auth)
    except FieldAccessError:
        profile = None
    if profile is not None:
        return {**auth, "technician_profile": profile, "field_actor": "technician"}

    try:
        vendor = vendor_context(db, auth)
    except HTTPException:
        vendor = None
    if vendor is not None:
        return {**vendor, "field_actor": "vendor"}

    raise HTTPException(
        status_code=403,
        detail="Field technician or vendor access required",
    )
