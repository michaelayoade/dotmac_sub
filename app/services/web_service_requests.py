"""Service helpers for the reseller service-request admin queue web pages.

The reseller submits new-connection / install requests from the portal
(``reseller_service_requests``); staff work them here. List/detail context
builders + the allowed-transition view the detail page uses to render the
status control. Mutations go through ``reseller_service_requests.update_status``
(transition-guarded + reseller-notified).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.service_request import ResellerServiceRequest, ServiceRequestStatus
from app.services import reseller_service_requests
from app.services.common import coerce_uuid

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def list_data(
    db: Session,
    *,
    status: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    """Build template context for the service-request queue (newest first)."""
    query = db.query(ResellerServiceRequest)
    if status:
        try:
            query = query.filter(
                ResellerServiceRequest.status == ServiceRequestStatus(status)
            )
        except ValueError:
            status = None
    total = query.count()
    requests = (
        query.order_by(ResellerServiceRequest.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    new_count = (
        db.query(ResellerServiceRequest)
        .filter(ResellerServiceRequest.status == ServiceRequestStatus.new)
        .count()
    )
    total_pages = (total + per_page - 1) // per_page if total else 1

    return {
        "requests": requests,
        "statuses": [s.value for s in ServiceRequestStatus],
        "status_filter": status,
        "new_count": new_count,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def detail_data(db: Session, *, request_id: str) -> dict[str, object] | None:
    """Build template context for a single service request, or None."""
    req = db.get(ResellerServiceRequest, coerce_uuid(request_id))
    if not req:
        return None
    return {
        "req": req,
        "reseller": req.reseller,
        "allowed_next": [
            s.value for s in reseller_service_requests.allowed_next_statuses(req.status)
        ],
    }
