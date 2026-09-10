from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.field.work_order_compat import resolve_work_order_id
from app.models.system_user import SystemUser
from app.schemas.common import ListResponse
from app.schemas.field import (
    FieldAttachmentRead,
    FieldExpenseApproverRead,
    FieldExpenseBankRead,
    FieldExpenseCategoryRead,
    FieldExpenseDestinationRead,
    FieldExpenseDestinationVerify,
    FieldExpenseFormContextRead,
    FieldExpenseProfileDestinationRead,
    FieldExpenseRequestCreate,
    FieldExpenseRequestRead,
    FieldExpenseRequestSubmit,
    FieldExpenseVendorRead,
)
from app.services.auth_dependencies import require_user_auth
from app.services.db_session_adapter import db_session_adapter
from app.services.dotmac_erp.client import DotMacERPError, DotMacERPTransientError
from app.services.dotmac_erp.expense_form_contracts import (
    ExpenseDestinationMode,
    InspectExpenseDestination,
    VerifyExpenseDestination,
)
from app.services.field.attachments import field_attachments
from app.services.field.expense_categories import (
    ExpenseCategoryQueryError,
    ListExpenseCategories,
    list_expense_categories,
)
from app.services.field.expense_requests import (
    ExpenseRequestLineInput,
    FieldExpenseRequestError,
    ListFieldExpenseVendors,
    SelectedExpenseApprover,
    SubmitFieldExpenseRequest,
    VerifiedExpenseDestinationInput,
    field_expense_requests,
    list_expense_vendors,
    submit_field_expense_request_command,
)
from app.services.integrations.erp_capability import capability_client
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/expense-requests", tags=["field-expense-requests"])


def _command_context(auth: dict, *, request_id: UUID, reason: str) -> CommandContext:
    principal_id = UUID(str(auth["principal_id"]))
    return CommandContext(
        command_id=request_id,
        correlation_id=request_id,
        actor=f"user:{principal_id}",
        scope="field:expense_requests:write",
        reason=reason,
        idempotency_key=str(request_id),
    )


def _expense_command_error(exc: FieldExpenseRequestError) -> HTTPException:
    if exc.code.endswith("work_order_not_found") or exc.code.endswith(
        "requester_not_found"
    ):
        status_code = 404
    elif exc.code.endswith("invalid_request"):
        status_code = 422
    else:
        status_code = 409
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )


@router.get("/categories", response_model=ListResponse[FieldExpenseCategoryRead])
def list_field_expense_categories(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    del auth
    try:
        items = list_expense_categories(db, ListExpenseCategories())
    except ExpenseCategoryQueryError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return {"items": items, "count": len(items), "limit": len(items), "offset": 0}


@router.get("/vendors", response_model=ListResponse[FieldExpenseVendorRead])
def list_field_expense_vendors(
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    del auth
    items = list_expense_vendors(
        db=db,
        query=ListFieldExpenseVendors(search=q, limit=limit, offset=offset),
    )
    return {
        "items": [
            FieldExpenseVendorRead(id=item.id, label=item.label) for item in items
        ],
        "count": len(items),
        "limit": limit,
        "offset": offset,
    }


def _requesting_user(db: Session, auth: dict) -> SystemUser:
    user = db.get(SystemUser, UUID(str(auth["principal_id"])))
    if user is None or not user.is_active or not user.email.strip():
        raise HTTPException(status_code=422, detail="Staff email is required in Sub")
    return user


def _selected_approver(
    db: Session,
    *,
    requester: SystemUser,
    erp_employee_id: UUID,
) -> SelectedExpenseApprover:
    try:
        options = capability_client(db).get_expense_approvers(
            requested_by_email=requester.email
        )
    except DotMacERPError as exc:
        raise HTTPException(
            status_code=503, detail="Expense approvers are unavailable from ERP"
        ) from exc
    option = next(
        (item for item in options if item.employee_id == erp_employee_id), None
    )
    if option is None:
        raise HTTPException(
            status_code=422, detail="Select an eligible expense approver"
        )
    local_user = (
        db.query(SystemUser)
        .filter(
            SystemUser.is_active.is_(True),
            func.lower(SystemUser.email) == option.email.strip().lower(),
        )
        .one_or_none()
    )
    if local_user is None:
        raise HTTPException(
            status_code=422,
            detail="The selected ERP approver has no matching active Sub user",
        )
    return SelectedExpenseApprover(
        erp_employee_id=option.employee_id,
        system_user_id=local_user.id,
        display_name=option.display_name,
        email=option.email,
    )


@router.get("/form-context", response_model=FieldExpenseFormContextRead)
def get_field_expense_form_context(
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> FieldExpenseFormContextRead:
    requester = _requesting_user(db, auth)
    client = capability_client(db)
    try:
        approvers = client.get_expense_approvers(requested_by_email=requester.email)
        banks = client.get_expense_banks()
        profile = client.get_expense_profile_destination(
            requested_by_email=requester.email
        )
    except DotMacERPError as exc:
        raise HTTPException(
            status_code=503,
            detail="Expense approvers and payment details are unavailable from ERP",
        ) from exc
    emails = {option.email.strip().lower() for option in approvers}
    local_users = {
        user.email.strip().lower(): user
        for user in db.query(SystemUser)
        .filter(
            SystemUser.is_active.is_(True), func.lower(SystemUser.email).in_(emails)
        )
        .all()
    }
    return FieldExpenseFormContextRead(
        approvers=[
            FieldExpenseApproverRead(
                erp_employee_id=option.employee_id,
                system_user_id=local_users[option.email.strip().lower()].id,
                display_name=option.display_name,
                email=option.email,
            )
            for option in approvers
            if option.email.strip().lower() in local_users
        ],
        banks=[FieldExpenseBankRead(**bank.model_dump()) for bank in banks],
        profile_destination=FieldExpenseProfileDestinationRead(**profile.model_dump()),
    )


@router.post("/payment-destination/verify", response_model=FieldExpenseDestinationRead)
def verify_field_expense_payment_destination(
    payload: FieldExpenseDestinationVerify,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> FieldExpenseDestinationRead:
    requester = _requesting_user(db, auth)
    try:
        result = capability_client(db).verify_expense_destination(
            VerifyExpenseDestination(
                requested_by_email=requester.email,
                source_claim_id=payload.source_claim_id,
                mode=ExpenseDestinationMode(payload.mode),
                bank_code=payload.bank_code,
                account_number=payload.account_number,
                beneficiary_name=payload.beneficiary_name,
            )
        )
    except DotMacERPTransientError as exc:
        raise HTTPException(
            status_code=503,
            detail="Bank account verification is temporarily unavailable",
        ) from exc
    except DotMacERPError as exc:
        raise HTTPException(
            status_code=422, detail="ERP could not verify the payment details"
        ) from exc
    return FieldExpenseDestinationRead(**result.model_dump())


@router.post("/receipts", response_model=FieldAttachmentRead, status_code=201)
def upload_field_expense_receipt(
    file: UploadFile = File(...),
    work_order_id: str | None = Form(default=None),
    crm_work_order_id: str | None = Form(default=None, deprecated=True),
    client_ref: UUID | None = Form(default=None),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    resolved_work_order_id = resolve_work_order_id(
        work_order_id=work_order_id, crm_work_order_id=crm_work_order_id
    )
    if resolved_work_order_id is None:
        raise HTTPException(status_code=422, detail="work_order_id is required")
    return field_attachments.create(
        db,
        auth,
        kind="document",
        file_name=file.filename or "receipt",
        mime_type=file.content_type,
        content=file.file.read(),
        client_ref=client_ref,
        crm_work_order_id=resolved_work_order_id,
    )


@router.get("", response_model=ListResponse[FieldExpenseRequestRead])
def list_field_expense_requests(
    work_order_id: str | None = None,
    crm_work_order_id: str | None = Query(default=None, deprecated=True),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    resolved_work_order_id = resolve_work_order_id(
        work_order_id=work_order_id, crm_work_order_id=crm_work_order_id
    )
    items = field_expense_requests.list_mine(
        db,
        auth,
        crm_work_order_id=resolved_work_order_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.post(
    "",
    response_model=FieldExpenseRequestRead,
    status_code=status.HTTP_201_CREATED,
)
def create_field_expense_request(
    payload: FieldExpenseRequestCreate,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_expense_requests.create(
        db,
        auth,
        crm_work_order_id=payload.work_order_id,
        purpose=payload.purpose,
        expense_date=payload.expense_date,
        currency=payload.currency,
        notes=payload.notes,
        client_ref=payload.client_ref,
        items=[item.model_dump() for item in payload.items],
    )


@router.post(
    "/submit",
    response_model=FieldExpenseRequestRead,
    status_code=status.HTTP_201_CREATED,
)
def create_and_submit_field_expense_request(
    payload: FieldExpenseRequestSubmit,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    try:
        requester = _requesting_user(db, auth)
        selected_approver = _selected_approver(
            db,
            requester=requester,
            erp_employee_id=payload.selected_approver.erp_employee_id,
        )
        try:
            verified_destination = capability_client(db).inspect_expense_destination(
                InspectExpenseDestination(
                    requested_by_email=requester.email,
                    source_claim_id=payload.client_ref,
                    destination_token=payload.payment_destination.destination_token,
                )
            )
        except DotMacERPTransientError as exc:
            raise HTTPException(
                status_code=503,
                detail="Payment details cannot be checked with ERP right now",
            ) from exc
        except DotMacERPError as exc:
            raise HTTPException(
                status_code=422,
                detail="Payment details expired or do not belong to this expense",
            ) from exc
        db_session_adapter.release_read_transaction(db)
        return submit_field_expense_request_command(
            db,
            SubmitFieldExpenseRequest(
                context=_command_context(
                    auth,
                    request_id=payload.client_ref,
                    reason="field_expense_request_submission",
                ),
                requester_person_id=UUID(str(auth["principal_id"])),
                work_order_public_id=payload.work_order_id,
                request_id=payload.client_ref,
                purpose=payload.purpose,
                expense_date=payload.expense_date,
                currency=payload.currency,
                notes=payload.notes,
                items=tuple(
                    ExpenseRequestLineInput(**item.model_dump())
                    for item in payload.items
                ),
                selected_approver=selected_approver,
                payment_destination=VerifiedExpenseDestinationInput(
                    mode=verified_destination.mode.value,
                    destination_token=verified_destination.destination_token,
                    bank_code=verified_destination.bank_code,
                    bank_name=verified_destination.bank_name,
                    masked_account_number=verified_destination.masked_account_number,
                    verified_beneficiary_name=(
                        verified_destination.verified_beneficiary_name
                    ),
                    verified_at=verified_destination.verified_at,
                    expires_at=verified_destination.expires_at,
                ),
            ),
        )
    except FieldExpenseRequestError as exc:
        raise _expense_command_error(exc) from exc


@router.get("/{expense_request_id}", response_model=FieldExpenseRequestRead)
def get_field_expense_request(
    expense_request_id: UUID,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_expense_requests.get(db, auth, expense_request_id)


@router.post("/{expense_request_id}/submit", response_model=FieldExpenseRequestRead)
def submit_field_expense_request(
    expense_request_id: UUID,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_expense_requests.submit(db, auth, expense_request_id)


@router.post("/{expense_request_id}/cancel", response_model=FieldExpenseRequestRead)
def cancel_field_expense_request(
    expense_request_id: UUID,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    return field_expense_requests.cancel(db, auth, expense_request_id)
