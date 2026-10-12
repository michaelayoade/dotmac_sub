"""Staff browser adapter for reviewed complaint-to-incident correlation."""

import secrets
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.csrf import CSRF_COOKIE_NAME, get_csrf_token, set_csrf_cookie
from app.db import get_db
from app.services.auth_dependencies import require_permission_admission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.form_contracts import FormConsequence, FormContract, register
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.topology import outage
from app.services.topology.outage_complaint_contracts import (
    MONITORING_READ,
    MONITORING_WRITE,
    SUPPORT_READ,
    SUPPORT_WRITE,
    ComplaintIncidentCandidatesQuery,
    ComplaintIncidentLinkCommand,
)
from app.web.request_parsing import parse_form_data_sync

router = APIRouter(prefix="/support/tickets", tags=["web-admin-outage-complaints"])
templates = Jinja2Templates(directory="templates")
FORM = register(
    FormContract(
        key="outage.complaint_link",
        title="Link reviewed incident",
        entity="support_ticket",
        command_owner="network.outage_lifecycle",
        consequences=(
            FormConsequence(
                "link",
                "Records this complaint against the exact reviewed incident audience.",
            ),
        ),
    )
)


def _status(error: DomainError) -> int:
    if "denied" in error.code:
        return 403
    if error.code.endswith("invalid_request"):
        return 400
    if error.code.endswith("not_found"):
        return 404
    return 409


def _render(
    request: Request,
    db: Session,
    *,
    ticket_id: UUID,
    support: PermissionAdmission,
    monitoring: PermissionAdmission,
    submitted: dict[str, str] | None = None,
    error: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    from app.web.admin import get_current_user, get_sidebar_stats

    try:
        page = outage.complaint_incident_candidates(
            db,
            query=ComplaintIncidentCandidatesQuery(
                ticket_id=ticket_id,
                support_access=support,
                monitoring_access=monitoring,
                after_incident_id=UUID(request.query_params["after"])
                if request.query_params.get("after")
                else None,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid incident pagination cursor"
        ) from exc
    except DomainError as exc:
        raise HTTPException(status_code=_status(exc), detail=exc.message) from exc
    token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="admin/network/outage_complaint_link.html",
        context={
            "request": request,
            "page": page,
            "submitted": submitted or {},
            "intent_key": (submitted or {}).get("intent_key", str(uuid4())),
            "error": error,
            "stale": status == 409,
            "form_contract": FORM,
            "csrf_token": token,
            "active_page": "support",
            "active_menu": "operations",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
        status_code=status,
    )
    set_csrf_cookie(response, token, request)
    return response


@router.get("/{ticket_id}/incident-link")
def complaint_link_page(
    request: Request,
    ticket_id: UUID,
    db: Session = Depends(get_db),
    support: PermissionAdmission = Depends(require_permission_admission(SUPPORT_READ)),
    monitoring: PermissionAdmission = Depends(
        require_permission_admission(MONITORING_READ)
    ),
) -> HTMLResponse:
    return _render(
        request, db, ticket_id=ticket_id, support=support, monitoring=monitoring
    )


@router.post("/{ticket_id}/incident-link", response_model=None)
def complaint_link_submit(
    request: Request,
    ticket_id: UUID,
    db: Session = Depends(get_db),
    support: PermissionAdmission = Depends(require_permission_admission(SUPPORT_WRITE)),
    monitoring: PermissionAdmission = Depends(
        require_permission_admission(MONITORING_WRITE)
    ),
    support_read: PermissionAdmission = Depends(
        require_permission_admission(SUPPORT_READ)
    ),
    monitoring_read: PermissionAdmission = Depends(
        require_permission_admission(MONITORING_READ)
    ),
) -> HTMLResponse | RedirectResponse:
    raw = parse_form_data_sync(request)
    csrf = raw.get("_csrf_token")
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if (
        not isinstance(csrf, str)
        or not cookie
        or not secrets.compare_digest(csrf, cookie)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    fields = (
        "incident_id",
        "subscription_id",
        "revision_id",
        "membership_token",
        "reason",
        "intent_key",
        "adopt_legacy",
    )
    submitted = {key: str(raw.get(key, "")) for key in fields}
    try:
        # Exact reviewed evidence is posted independently of the newly rendered candidate list.
        command = ComplaintIncidentLinkCommand(
            context=CommandContext.system(
                actor=str(support.principal_id),
                scope=SUPPORT_WRITE,
                reason=submitted["reason"],
                idempotency_key=submitted["intent_key"],
            ),
            support_access=support,
            monitoring_access=monitoring,
            ticket_id=ticket_id,
            incident_id=UUID(submitted["incident_id"]),
            subscription_id=UUID(submitted["subscription_id"]),
            expected_revision_id=UUID(submitted["revision_id"]),
            expected_membership_token=submitted["membership_token"],
            reason=submitted["reason"],
            adopt_legacy=submitted["adopt_legacy"] == "yes",
        )
        # Dependency permission reads are adapter observations, not the business transaction.
        db_session_adapter.release_read_transaction(db)
        outage.link_complaint_ticket(db, command=command)
    except ValueError:
        return _render(
            request,
            db,
            ticket_id=ticket_id,
            support=support_read,
            monitoring=monitoring_read,
            submitted=submitted,
            error="Choose an incident and service and provide a review reason.",
            status=400,
        )
    except DomainError as exc:
        if _status(exc) == 403:
            raise HTTPException(status_code=403, detail=exc.message) from exc
        return _render(
            request,
            db,
            ticket_id=ticket_id,
            support=support_read,
            monitoring=monitoring_read,
            submitted=submitted,
            error=exc.message,
            status=_status(exc),
        )
    return RedirectResponse(f"/admin/support/tickets/{ticket_id}", status_code=303)
