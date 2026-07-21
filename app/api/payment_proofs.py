"""Bank-transfer payment proofs: customer/reseller submission + admin review."""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.payment_proof_errors import payment_proof_http_error
from app.db import get_db
from app.services.auth_dependencies import require_permission, require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/payment-proofs", tags=["payment-proofs"])


def _command_context(
    principal: dict,
    *,
    scope: str,
    reason: str,
    idempotency_key: str | None = None,
) -> CommandContext:
    principal_id = str(
        principal.get("principal_id") or principal.get("subscriber_id") or ""
    ).strip()
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    actor_type = "api_key" if principal.get("principal_type") == "api_key" else "user"
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{actor_type}:{principal_id}",
        scope=scope,
        reason=reason,
        idempotency_key=idempotency_key,
    )


class ProofReview(BaseModel):
    review_notes: str | None = Field(default=None, max_length=2000)
    auto_allocate: bool = True
    # Reviewer-confirmed amount (from the bank statement). Defaults to the
    # customer-claimed amount when omitted.
    amount: Decimal | None = Field(default=None, gt=0)


@router.post("/me")
async def submit_my_payment_proof(
    amount: str = Form(...),
    bank_name: str | None = Form(default=None),
    reference: str | None = Form(default=None),
    paid_at: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Customer uploads their own transfer receipt."""
    from app.services import payment_proofs

    if principal.get("principal_type") != "subscriber":
        raise HTTPException(status_code=403, detail="Customer account required")
    try:
        path = await payment_proofs.save_proof_file(file)
        db_session_adapter.release_read_transaction(db)
        return payment_proofs.submit_proof(
            db,
            str(principal["subscriber_id"]),
            context=_command_context(
                principal,
                scope=payment_proofs.SUBMISSION_SCOPE,
                reason="Customer submitted bank-transfer evidence",
            ),
            submitted_by=str(principal["subscriber_id"]),
            amount=amount,
            bank_name=bank_name,
            reference=reference,
            paid_at=_parse_dt(paid_at),
            file_path=path,
        ).to_dict()
    except DomainError as exc:
        raise payment_proof_http_error(exc) from exc


@router.get("/me")
def my_payment_proofs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    from app.services import payment_proofs

    if principal.get("principal_type") != "subscriber":
        raise HTTPException(status_code=403, detail="Customer account required")
    return {
        "items": payment_proofs.list_for_account(
            db, str(principal["subscriber_id"]), limit, offset
        )
    }


@router.get("/me/{proof_id}/file")
def my_payment_proof_file(
    proof_id: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> Response:
    """Customer downloads their own receipt file."""
    from app.services import payment_proofs

    if principal.get("principal_type") != "subscriber":
        raise HTTPException(status_code=403, detail="Customer account required")
    proof = payment_proofs.get_proof(db, proof_id)
    if proof is None or str(proof.account_id) != str(principal["subscriber_id"]):
        raise HTTPException(status_code=404, detail="Payment proof not found")
    try:
        path, media_type = payment_proofs.resolve_proof_file(proof)
    except DomainError as exc:
        raise payment_proof_http_error(exc) from exc
    return Response(path.read_bytes(), media_type=media_type)


@router.post("/reseller/accounts/{account_id}")
async def submit_account_payment_proof(
    account_id: str,
    amount: str = Form(...),
    bank_name: str | None = Form(default=None),
    reference: str | None = Form(default=None),
    paid_at: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """A reseller uploads a transfer receipt for one of their accounts."""
    from app.api.reseller import _reseller_id
    from app.services import payment_proofs, reseller_portal

    reseller_id = _reseller_id(db, principal)
    detail = reseller_portal.get_account_detail(
        db, reseller_id=reseller_id, account_id=account_id
    )
    if not detail:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        path = await payment_proofs.save_proof_file(file)
        db_session_adapter.release_read_transaction(db)
        return payment_proofs.submit_proof(
            db,
            account_id,
            context=_command_context(
                principal,
                scope=payment_proofs.SUBMISSION_SCOPE,
                reason="Reseller submitted subscriber bank-transfer evidence",
            ),
            submitted_by=str(principal["subscriber_id"]),
            amount=amount,
            bank_name=bank_name,
            reference=reference,
            paid_at=_parse_dt(paid_at),
            file_path=path,
        ).to_dict()
    except DomainError as exc:
        raise payment_proof_http_error(exc) from exc


@router.post("/reseller/consolidated")
async def submit_reseller_consolidated_proof(
    amount: str = Form(...),
    gross_amount: str | None = Form(default=None),
    wht_rate: str | None = Form(default=None),
    bank_name: str | None = Form(default=None),
    reference: str | None = Form(default=None),
    paid_at: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """A reseller uploads a bulk bank-transfer receipt for their consolidated
    billing account, optionally net of withholding tax.

    ``amount`` is the net cash transferred; pass ``gross_amount`` (billed value)
    and/or ``wht_rate`` when tax was withheld at source.
    """
    from app.api.reseller import _reseller_id
    from app.services import billing as billing_service
    from app.services import payment_proofs

    reseller_id = _reseller_id(db, principal)
    ba = billing_service.billing_accounts.get_for_reseller(db, reseller_id)
    try:
        path = await payment_proofs.save_proof_file(file)
        currency = ba.currency
        billing_account_id = str(ba.id)
        db_session_adapter.release_read_transaction(db)
        return payment_proofs.submit_proof(
            db,
            None,
            context=_command_context(
                principal,
                scope=payment_proofs.SUBMISSION_SCOPE,
                reason="Reseller submitted consolidated bank-transfer evidence",
            ),
            submitted_by=principal.get("subscriber_id"),
            amount=amount,
            currency=currency,
            bank_name=bank_name,
            reference=reference,
            paid_at=_parse_dt(paid_at),
            file_path=path,
            billing_account_id=billing_account_id,
            gross_amount=gross_amount,
            wht_rate=wht_rate,
        ).to_dict()
    except DomainError as exc:
        raise payment_proof_http_error(exc) from exc


@router.get("/reseller/consolidated")
def reseller_consolidated_proofs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """A reseller's own consolidated bank-transfer receipts."""
    from app.api.reseller import _reseller_id
    from app.services import billing as billing_service
    from app.services import payment_proofs

    reseller_id = _reseller_id(db, principal)
    ba = billing_service.billing_accounts.get_for_reseller(db, reseller_id)
    return {
        "items": payment_proofs.list_for_billing_account(db, str(ba.id), limit, offset)
    }


@router.get("/admin", dependencies=[Depends(require_permission("billing:proof:read"))])
def list_payment_proofs(
    status: str | None = "submitted",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    from app.services import payment_proofs

    return {"items": payment_proofs.list_admin(db, status, limit, offset)}


@router.get(
    "/admin/{proof_id}/file",
    dependencies=[Depends(require_permission("billing:proof:read"))],
)
def payment_proof_file(
    proof_id: str,
    db: Session = Depends(get_db),
) -> Response:
    """Staff download/preview of any proof's receipt file."""
    from app.services import payment_proofs

    proof = payment_proofs.get_proof(db, proof_id)
    if proof is None:
        raise HTTPException(status_code=404, detail="Payment proof not found")
    try:
        path, media_type = payment_proofs.resolve_proof_file(proof)
    except DomainError as exc:
        raise payment_proof_http_error(exc) from exc
    return Response(path.read_bytes(), media_type=media_type)


@router.post(
    "/admin/{proof_id}/verify",
    dependencies=[Depends(require_permission("billing:proof:verify"))],
)
def verify_payment_proof(
    proof_id: str,
    payload: ProofReview,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Confirm the transfer: creates a succeeded Payment for the reviewer-
    confirmed amount (auto-allocated to the oldest open invoices unless
    disabled) and notifies the customer."""
    from app.services import payment_proofs

    try:
        db_session_adapter.release_read_transaction(db)
        return payment_proofs.verify_proof(
            db,
            proof_id,
            context=_command_context(
                principal,
                scope=payment_proofs.REVIEW_SCOPE,
                reason="Staff verified bank-transfer evidence",
                idempotency_key=f"payment-proof:verify:{proof_id}",
            ),
            verified_by=str(principal.get("principal_id")),
            amount=payload.amount,
            auto_allocate=payload.auto_allocate,
            review_notes=payload.review_notes,
        ).to_dict()
    except DomainError as exc:
        raise payment_proof_http_error(exc) from exc


@router.post(
    "/admin/{proof_id}/reject",
    dependencies=[Depends(require_permission("billing:proof:verify"))],
)
def reject_payment_proof(
    proof_id: str,
    payload: ProofReview,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    from app.services import payment_proofs

    try:
        db_session_adapter.release_read_transaction(db)
        return payment_proofs.reject_proof(
            db,
            proof_id,
            context=_command_context(
                principal,
                scope=payment_proofs.REVIEW_SCOPE,
                reason="Staff rejected bank-transfer evidence",
                idempotency_key=f"payment-proof:reject:{proof_id}",
            ),
            verified_by=str(principal.get("principal_id")),
            review_notes=payload.review_notes or "",
        ).to_dict()
    except DomainError as exc:
        raise payment_proof_http_error(exc) from exc


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
