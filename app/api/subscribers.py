import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.subscriber import (
    NINVerificationStatus,
    Subscriber,
    SubscriberNINVerification,
)
from app.schemas.common import ListResponse
from app.schemas.subscriber import (
    AddressCreate,
    AddressRead,
    AddressUpdate,
    ResellerCreate,
    ResellerRead,
    ResellerUpdate,
    SubscriberCreate,
    SubscriberCustomFieldCreate,
    SubscriberCustomFieldRead,
    SubscriberCustomFieldUpdate,
    SubscriberRead,
    SubscriberUpdate,
)
from app.services import subscriber as subscriber_service
from app.services.auth_dependencies import require_permission
from app.services.nin_matching import normalize_nin, validate_nin
from app.services.queue_adapter import enqueue_task
from app.tasks.nin_tasks import verify_nin_task

router = APIRouter()


def _nin_verification_payload(
    verification: SubscriberNINVerification | None,
) -> dict[str, object | None]:
    if verification is None:
        return {
            "status": "not_found",
            "is_match": None,
            "match_score": None,
            "failure_reason": None,
            "verified_at": None,
            "created_at": None,
        }
    return {
        "status": verification.status.value if verification.status else None,
        "is_match": verification.is_match,
        "match_score": verification.match_score,
        "failure_reason": verification.failure_reason,
        "verified_at": (
            verification.verified_at.isoformat() if verification.verified_at else None
        ),
        "created_at": verification.created_at.isoformat()
        if verification.created_at
        else None,
    }


def _latest_nin_verification(
    db: Session,
    subscriber_id: uuid.UUID,
) -> SubscriberNINVerification | None:
    return (
        db.query(SubscriberNINVerification)
        .filter(SubscriberNINVerification.subscriber_id == subscriber_id)
        .order_by(SubscriberNINVerification.created_at.desc())
        .first()
    )


def _get_or_create_pending_nin_verification(
    db: Session,
    subscriber_id: uuid.UUID,
    nin: str,
) -> SubscriberNINVerification:
    verification = (
        db.query(SubscriberNINVerification)
        .filter(
            SubscriberNINVerification.subscriber_id == subscriber_id,
            SubscriberNINVerification.nin == nin,
            SubscriberNINVerification.status == NINVerificationStatus.pending,
        )
        .order_by(SubscriberNINVerification.created_at.desc())
        .first()
    )
    if verification is not None:
        return verification

    verification = SubscriberNINVerification(
        subscriber_id=subscriber_id,
        nin=nin,
        status=NINVerificationStatus.pending,
    )
    db.add(verification)
    db.flush()
    return verification


@router.post(
    "/resellers",
    response_model=ResellerRead,
    status_code=status.HTTP_201_CREATED,
    tags=["resellers"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
def create_reseller(payload: ResellerCreate, db: Session = Depends(get_db)):
    return subscriber_service.resellers.create(db, payload)


@router.get(
    "/resellers/{reseller_id}",
    response_model=ResellerRead,
    tags=["resellers"],
    dependencies=[Depends(require_permission("subscriber:read"))],
)
def get_reseller(reseller_id: str, db: Session = Depends(get_db)):
    return subscriber_service.resellers.get(db, reseller_id)


@router.get(
    "/resellers",
    response_model=ListResponse[ResellerRead],
    tags=["resellers"],
    dependencies=[Depends(require_permission("subscriber:read"))],
)
def list_resellers(
    is_active: bool | None = None,
    order_by: str = Query(default="name"),
    order_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscriber_service.resellers.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/resellers/{reseller_id}",
    response_model=ResellerRead,
    tags=["resellers"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
def update_reseller(
    reseller_id: str, payload: ResellerUpdate, db: Session = Depends(get_db)
):
    return subscriber_service.resellers.update(db, reseller_id, payload)


@router.delete(
    "/resellers/{reseller_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["resellers"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
def delete_reseller(reseller_id: str, db: Session = Depends(get_db)):
    subscriber_service.resellers.delete(db, reseller_id)


@router.post(
    "/subscribers",
    response_model=SubscriberRead,
    status_code=status.HTTP_201_CREATED,
    tags=["subscribers"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
def create_subscriber(payload: SubscriberCreate, db: Session = Depends(get_db)):
    return subscriber_service.subscribers.create(db, payload)


@router.get(
    "/subscribers/{subscriber_id}",
    response_model=SubscriberRead,
    tags=["subscribers"],
    dependencies=[Depends(require_permission("subscriber:read"))],
)
def get_subscriber(subscriber_id: str, db: Session = Depends(get_db)):
    return subscriber_service.subscribers.get(db, subscriber_id)


@router.get(
    "/subscribers",
    response_model=ListResponse[SubscriberRead],
    tags=["subscribers"],
    dependencies=[Depends(require_permission("subscriber:read"))],
)
def list_subscribers(
    subscriber_type: str | None = None,
    person_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscriber_service.subscribers.list_response(
        db,
        person_id,
        None,
        subscriber_type,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/subscribers/{subscriber_id}",
    response_model=SubscriberRead,
    tags=["subscribers"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
def update_subscriber(
    subscriber_id: str, payload: SubscriberUpdate, db: Session = Depends(get_db)
):
    return subscriber_service.subscribers.update(db, subscriber_id, payload)


@router.delete(
    "/subscribers/{subscriber_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["subscribers"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
def delete_subscriber(subscriber_id: str, db: Session = Depends(get_db)):
    subscriber_service.subscribers.delete(db, subscriber_id)


@router.post(
    "/subscribers/{subscriber_id}/verify-nin",
    tags=["subscribers"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
async def verify_subscriber_nin(
    subscriber_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    nin: str | None = None
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid NIN") from exc
        if isinstance(payload, dict):
            nin = str(payload.get("nin") or "")
    else:
        form = await request.form()
        nin = str(form.get("nin") or "")

    normalized_nin = normalize_nin(nin or "")
    if not validate_nin(normalized_nin):
        raise HTTPException(status_code=400, detail="Invalid NIN")

    try:
        subscriber_uuid = uuid.UUID(str(subscriber_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Subscriber not found") from exc

    subscriber = db.get(Subscriber, subscriber_uuid)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    verification = _get_or_create_pending_nin_verification(
        db,
        subscriber_uuid,
        normalized_nin,
    )
    db.commit()

    dispatch = enqueue_task(
        verify_nin_task,
        args=[str(subscriber.id), normalized_nin],
        queue="nin",
        source="subscriber_nin_verification",
    )
    if not dispatch.queued:
        verification.status = NINVerificationStatus.failed
        verification.is_match = False
        verification.match_score = 0
        verification.failure_reason = dispatch.error or "NIN verification could not be queued"
        db.commit()
        raise HTTPException(
            status_code=503,
            detail=dispatch.error or "NIN verification could not be queued",
        )
    return {"status": "queued", "task_id": dispatch.task_id or ""}


@router.get(
    "/subscribers/{subscriber_id}/nin-verification",
    tags=["subscribers"],
    dependencies=[Depends(require_permission("subscriber:read"))],
)
def get_subscriber_nin_verification(
    subscriber_id: str,
    db: Session = Depends(get_db),
) -> dict[str, object | None]:
    try:
        subscriber_uuid = uuid.UUID(str(subscriber_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Subscriber not found") from exc

    subscriber = db.get(Subscriber, subscriber_uuid)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    return _nin_verification_payload(_latest_nin_verification(db, subscriber_uuid))


@router.post(
    "/addresses",
    response_model=AddressRead,
    status_code=status.HTTP_201_CREATED,
    tags=["addresses"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
def create_address(payload: AddressCreate, db: Session = Depends(get_db)):
    return subscriber_service.addresses.create(db, payload)


@router.get(
    "/addresses/{address_id}",
    response_model=AddressRead,
    tags=["addresses"],
    dependencies=[Depends(require_permission("subscriber:read"))],
)
def get_address(address_id: str, db: Session = Depends(get_db)):
    return subscriber_service.addresses.get(db, address_id)


@router.get(
    "/addresses",
    response_model=ListResponse[AddressRead],
    tags=["addresses"],
    dependencies=[Depends(require_permission("subscriber:read"))],
)
def list_addresses(
    subscriber_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscriber_service.addresses.list_response(
        db, subscriber_id, order_by, order_dir, limit, offset
    )


@router.patch(
    "/addresses/{address_id}",
    response_model=AddressRead,
    tags=["addresses"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
def update_address(
    address_id: str, payload: AddressUpdate, db: Session = Depends(get_db)
):
    return subscriber_service.addresses.update(db, address_id, payload)


@router.delete(
    "/addresses/{address_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["addresses"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
def delete_address(address_id: str, db: Session = Depends(get_db)):
    subscriber_service.addresses.delete(db, address_id)


@router.post(
    "/subscriber-custom-fields",
    response_model=SubscriberCustomFieldRead,
    status_code=status.HTTP_201_CREATED,
    tags=["subscriber-custom-fields"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
def create_subscriber_custom_field(
    payload: SubscriberCustomFieldCreate, db: Session = Depends(get_db)
):
    return subscriber_service.subscriber_custom_fields.create(db, payload)


@router.get(
    "/subscriber-custom-fields/{custom_field_id}",
    response_model=SubscriberCustomFieldRead,
    tags=["subscriber-custom-fields"],
    dependencies=[Depends(require_permission("subscriber:read"))],
)
def get_subscriber_custom_field(custom_field_id: str, db: Session = Depends(get_db)):
    return subscriber_service.subscriber_custom_fields.get(db, custom_field_id)


@router.get(
    "/subscriber-custom-fields",
    response_model=ListResponse[SubscriberCustomFieldRead],
    tags=["subscriber-custom-fields"],
    dependencies=[Depends(require_permission("subscriber:read"))],
)
def list_subscriber_custom_fields(
    subscriber_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return subscriber_service.subscriber_custom_fields.list_response(
        db, subscriber_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/subscriber-custom-fields/{custom_field_id}",
    response_model=SubscriberCustomFieldRead,
    tags=["subscriber-custom-fields"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
def update_subscriber_custom_field(
    custom_field_id: str,
    payload: SubscriberCustomFieldUpdate,
    db: Session = Depends(get_db),
):
    return subscriber_service.subscriber_custom_fields.update(
        db, custom_field_id, payload
    )


@router.delete(
    "/subscriber-custom-fields/{custom_field_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["subscriber-custom-fields"],
    dependencies=[Depends(require_permission("subscriber:write"))],
)
def delete_subscriber_custom_field(custom_field_id: str, db: Session = Depends(get_db)):
    subscriber_service.subscriber_custom_fields.delete(db, custom_field_id)
