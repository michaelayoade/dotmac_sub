"""Bank-transfer payment proofs: customer/reseller submission + admin review."""

from datetime import datetime
from decimal import Decimal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.auth_dependencies import require_permission, require_user_auth

router = APIRouter(prefix="/payment-proofs", tags=["payment-proofs"])


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
    path = await payment_proofs.save_proof_file(file)
    return payment_proofs.submit_proof(
        db,
        str(principal["subscriber_id"]),
        submitted_by=str(principal["subscriber_id"]),
        amount=amount,
        bank_name=bank_name,
        reference=reference,
        paid_at=_parse_dt(paid_at),
        file_path=path,
    )


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
    path, media_type = payment_proofs.resolve_proof_file(proof)
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
    path = await payment_proofs.save_proof_file(file)
    return payment_proofs.submit_proof(
        db,
        account_id,
        submitted_by=str(principal["subscriber_id"]),
        amount=amount,
        bank_name=bank_name,
        reference=reference,
        paid_at=_parse_dt(paid_at),
        file_path=path,
    )


@router.get("/admin", dependencies=[Depends(require_permission("billing:read"))])
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
    dependencies=[Depends(require_permission("billing:read"))],
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
    path, media_type = payment_proofs.resolve_proof_file(proof)
    return Response(path.read_bytes(), media_type=media_type)


@router.post(
    "/admin/{proof_id}/verify",
    dependencies=[Depends(require_permission("billing:write"))],
)
def verify_payment_proof(
    proof_id: str,
    payload: ProofReview,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    """Confirm the transfer: creates a succeeded Payment for the reviewer-
    confirmed amount (auto-allocated to the oldest open invoices unless
    disabled) and notifies the customer."""
    from app.services import payment_proofs

    return payment_proofs.verify_proof(
        db,
        proof_id,
        verified_by=str(principal.get("principal_id")),
        amount=payload.amount,
        auto_allocate=payload.auto_allocate,
        review_notes=payload.review_notes,
        request=request,
    )


@router.post(
    "/admin/{proof_id}/reject",
    dependencies=[Depends(require_permission("billing:write"))],
)
def reject_payment_proof(
    proof_id: str,
    payload: ProofReview,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_user_auth),
) -> dict:
    from app.services import payment_proofs

    return payment_proofs.reject_proof(
        db,
        proof_id,
        verified_by=str(principal.get("principal_id")),
        review_notes=payload.review_notes or "",
        request=request,
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
