from __future__ import annotations

import hmac
from datetime import UTC, datetime, time
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models.subscriber import SubscriberStatus
from app.services import crm_api

router = APIRouter(prefix="/crm", tags=["crm-api"])


def _error(
    status_code: int, message: str, errors: dict[str, list[str]] | None = None
) -> None:
    detail: dict[str, Any] = {"message": message}
    if errors:
        detail["errors"] = errors
    raise HTTPException(status_code=status_code, detail=detail)


def require_crm_bearer(authorization: str | None = Header(default=None)) -> None:
    expected = settings.selfcare_api_token
    if not expected:
        _error(status.HTTP_401_UNAUTHORIZED, "CRM API bearer token is not configured.")
    scheme, _, token = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        _error(status.HTTP_401_UNAUTHORIZED, "Missing bearer token.")
    if not hmac.compare_digest(token, expected):
        _error(status.HTTP_401_UNAUTHORIZED, "Invalid bearer token.")


def _envelope(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"data": data}
    if meta is not None:
        payload["meta"] = meta
    return payload


def _query_value(request: Request, name: str) -> str | None:
    value = request.query_params.get(name)
    return value if value not in ("", None) else None


def _pagination(request: Request) -> tuple[int, int, dict[str, Any]]:
    errors: dict[str, list[str]] = {}

    def parse_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
        raw = _query_value(request, name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            errors.setdefault(name, []).append("Must be an integer.")
            return default
        if value < min_value:
            errors.setdefault(name, []).append(
                f"Must be greater than or equal to {min_value}."
            )
        if value > max_value:
            errors.setdefault(name, []).append(
                f"Must be less than or equal to {max_value}."
            )
        return value

    page = parse_int("page", 1, min_value=1, max_value=1_000_000)
    per_page = parse_int("per_page", 100, min_value=1, max_value=500)
    if errors:
        _error(status.HTTP_400_BAD_REQUEST, "Invalid query parameters.", errors)
    return page, per_page, {"page": page, "per_page": per_page}


def _include_values(request: Request, allowed: set[str]) -> set[str]:
    raw = _query_value(request, "include")
    if raw is None:
        return set()
    values = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = values - allowed
    if unknown:
        _error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid query parameters.",
            {
                "include": [
                    f"Unsupported include value(s): {', '.join(sorted(unknown))}."
                ]
            },
        )
    return values


def _parse_date_filter(request: Request, name: str) -> datetime | None:
    raw = _query_value(request, name)
    if raw is None:
        return None
    try:
        if "T" in raw:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            boundary = time.max if name == "date_to" else time.min
            value = datetime.combine(datetime.fromisoformat(raw).date(), boundary)
    except ValueError:
        _error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid query parameters.",
            {name: ["Must be an ISO 8601 date or datetime."]},
        )
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_customer_filter(request: Request) -> Any:
    raw = _query_value(request, "customer_id")
    if raw is None:
        return None
    parsed = crm_api.coerce_subscriber_id(raw)
    if parsed is None:
        _error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid query parameters.",
            {"customer_id": ["Must be a valid subscriber UUID."]},
        )
    return parsed


def _subscriber_or_404(db: Session, subscriber_id: str):
    subscriber = crm_api.get_subscriber_or_none(db, subscriber_id)
    if subscriber is None:
        _error(status.HTTP_404_NOT_FOUND, "Subscriber not found.")
    return subscriber


@router.get("/ping", dependencies=[Depends(require_crm_bearer)])
def ping() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/subscribers", dependencies=[Depends(require_crm_bearer)])
def list_subscribers(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    page, per_page, meta = _pagination(request)
    includes = _include_values(
        request, {"services", "billing", "session_state", "last_seen"}
    )
    subscribers, total = crm_api.list_subscribers(db, page=page, per_page=per_page)
    subscriber_ids = [item.id for item in subscribers]
    services = (
        crm_api.services_by_subscriber(db, subscriber_ids)
        if "services" in includes
        else {}
    )
    billing = (
        crm_api.billing_by_subscriber(db, subscribers) if "billing" in includes else {}
    )
    sessions = crm_api.latest_session_by_subscriber(db, subscriber_ids)
    data = []
    for subscriber in subscribers:
        kwargs: dict[str, Any] = {}
        if "services" in includes:
            kwargs["services"] = services.get(subscriber.id, [])
        if "billing" in includes:
            kwargs["billing"] = billing.get(subscriber.id)
        data.append(
            crm_api.subscriber_payload(
                db,
                subscriber,
                session=sessions.get(subscriber.id),
                include_session_state="session_state" in includes,
                **kwargs,
            )
        )
    return _envelope(data, {**meta, "total": total})


@router.get("/subscribers/search", dependencies=[Depends(require_crm_bearer)])
def search_subscribers(
    request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    q = _query_value(request, "q")
    if not q:
        _error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid query parameters.",
            {"q": ["Search query is required."]},
        )
    assert q is not None
    page, per_page, meta = _pagination(request)
    subscribers, total = crm_api.search_subscribers(db, q, page=page, per_page=per_page)
    sessions = crm_api.latest_session_by_subscriber(
        db, [item.id for item in subscribers]
    )
    data = [
        crm_api.subscriber_payload(db, subscriber, session=sessions.get(subscriber.id))
        for subscriber in subscribers
    ]
    return _envelope(data, {**meta, "total": total})


@router.get("/subscribers/online", dependencies=[Depends(require_crm_bearer)])
def online_subscribers(db: Session = Depends(get_db)) -> dict[str, Any]:
    # Online state is inferred from open, fresh RADIUS accounting sessions.
    # It is not an authoritative real-time device poll; subscribers whose NAS
    # has not sent interim accounting inside ONLINE_FRESH_SECONDS are excluded.
    return _envelope(crm_api.online_subscribers(db))


@router.get("/subscribers/{subscriber_id}", dependencies=[Depends(require_crm_bearer)])
def subscriber_detail(
    subscriber_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    session = crm_api.latest_session_by_subscriber(db, [subscriber.id]).get(
        subscriber.id
    )
    return _envelope(
        crm_api.subscriber_payload(
            db,
            subscriber,
            services=crm_api.subscriber_services(db, subscriber.id),
            billing=crm_api.billing_summary(db, subscriber),
            session=session,
            include_session_state=True,
        )
    )


@router.get(
    "/subscribers/{subscriber_id}/services", dependencies=[Depends(require_crm_bearer)]
)
def subscriber_services(
    subscriber_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    return _envelope(crm_api.subscriber_services(db, subscriber.id))


@router.get(
    "/subscribers/{subscriber_id}/billing", dependencies=[Depends(require_crm_bearer)]
)
def subscriber_billing(
    subscriber_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    return _envelope(crm_api.billing_summary(db, subscriber))


@router.get(
    "/subscribers/{subscriber_id}/sessions", dependencies=[Depends(require_crm_bearer)]
)
def subscriber_sessions(
    subscriber_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    return _envelope(crm_api.session_rows(db, subscriber.id))


@router.get(
    "/subscribers/{subscriber_id}/statistics",
    dependencies=[Depends(require_crm_bearer)],
)
def subscriber_statistics(
    subscriber_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    return _envelope(crm_api.session_rows(db, subscriber.id))


@router.patch(
    "/subscribers/{subscriber_id}/status", dependencies=[Depends(require_crm_bearer)]
)
def update_subscriber_status(
    subscriber_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    x_crm_actor: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    requested_status = str(payload.get("status") or "").strip()
    reason = str(payload.get("reason") or "").strip() or None
    source = str(payload.get("source") or "").strip() or None
    errors: dict[str, list[str]] = {}
    if requested_status != "disabled":
        errors["status"] = ["Only 'disabled' is supported."]
    if not reason:
        errors["reason"] = ["Reason is required."]
    if source != "crm":
        errors["source"] = ["Source must be 'crm'."]
    if errors:
        _error(status.HTTP_400_BAD_REQUEST, "Invalid request body.", errors)

    current = subscriber.status
    current_value = current.value if current else None
    if current in {SubscriberStatus.disabled, SubscriberStatus.canceled}:
        crm_api.log_status_writeback(
            db,
            subscriber_id=subscriber.id,
            actor=x_crm_actor,
            source=source,
            reason=reason,
            requested_status=requested_status,
            previous_status=current_value,
            result="already_terminal",
            status_code=200,
        )
        db.commit()
        return _envelope({"id": str(subscriber.id), "status": current_value})

    if current not in {SubscriberStatus.blocked, SubscriberStatus.suspended}:
        crm_api.log_status_writeback(
            db,
            subscriber_id=subscriber.id,
            actor=x_crm_actor,
            source=source,
            reason=reason,
            requested_status=requested_status,
            previous_status=current_value,
            result="rejected_transition",
            status_code=status.HTTP_409_CONFLICT,
        )
        db.commit()
        _error(
            status.HTTP_409_CONFLICT,
            "Subscriber can only be disabled from blocked, suspended, or nonpayment_suspended state.",
        )

    return _envelope(
        crm_api.disable_subscriber_from_crm(
            db,
            subscriber,
            actor=x_crm_actor,
            source=source,
            reason=reason,
        )
    )


@router.get("/locations", dependencies=[Depends(require_crm_bearer)])
def locations(db: Session = Depends(get_db)) -> dict[str, Any]:
    return _envelope(crm_api.locations(db))


@router.get("/billing-risk-source", dependencies=[Depends(require_crm_bearer)])
def billing_risk_source(
    request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    page, per_page, meta = _pagination(request)
    rows, total = crm_api.billing_risk_rows(db, page=page, per_page=per_page)
    return _envelope(rows, {**meta, "total": total})


@router.get("/service-extensions", dependencies=[Depends(require_crm_bearer)])
def service_extensions(
    request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    page, per_page, meta = _pagination(request)
    rows, total = crm_api.service_extension_rows(
        db, page=page, per_page=per_page
    )
    return _envelope(rows, {**meta, "total": total})


@router.get(
    "/service-extensions/{extension_id}", dependencies=[Depends(require_crm_bearer)]
)
def service_extension_detail(
    extension_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    row = crm_api.service_extension_detail(db, extension_id)
    if row is None:
        _error(status.HTTP_404_NOT_FOUND, "Service extension not found.")
    return _envelope(row)


@router.get(
    "/subscribers/{subscriber_id}/service-extensions",
    dependencies=[Depends(require_crm_bearer)],
)
def subscriber_service_extensions(
    subscriber_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    return _envelope(crm_api.service_extensions_for_subscriber(db, subscriber.id))


@router.get("/finance/transactions", dependencies=[Depends(require_crm_bearer)])
def finance_transactions(
    request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    page, per_page, meta = _pagination(request)
    rows, total = crm_api.transaction_rows(
        db,
        customer_id=_parse_customer_filter(request),
        date_from=_parse_date_filter(request, "date_from"),
        date_to=_parse_date_filter(request, "date_to"),
        page=page,
        per_page=per_page,
    )
    return _envelope(rows, {**meta, "total": total})


@router.get("/finance/payments", dependencies=[Depends(require_crm_bearer)])
def finance_payments(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    page, per_page, meta = _pagination(request)
    rows, total = crm_api.payment_rows(
        db,
        customer_id=_parse_customer_filter(request),
        date_from=_parse_date_filter(request, "date_from"),
        date_to=_parse_date_filter(request, "date_to"),
        page=page,
        per_page=per_page,
    )
    return _envelope(rows, {**meta, "total": total})
