from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.billing import (
    BankAccountCreate,
    BankAccountRead,
    BankAccountUpdate,
    BillingRunRead,
    CollectionAccountCreate,
    CollectionAccountRead,
    CollectionAccountUpdate,
    CreditNoteApplicationRead,
    CreditNoteApplyRequest,
    CreditNoteCreate,
    CreditNoteLineCreate,
    CreditNoteLineRead,
    CreditNoteLineUpdate,
    CreditNoteRead,
    CreditNoteUpdate,
    InvoiceBulkActionResponse,
    InvoiceBulkVoidRequest,
    InvoiceBulkWriteOffRequest,
    InvoiceCreate,
    InvoiceLineCreate,
    InvoiceLineRead,
    InvoiceLineUpdate,
    InvoiceRead,
    InvoiceRunRequest,
    InvoiceRunResponse,
    InvoiceUpdate,
    InvoiceWriteOffRequest,
    LedgerEntryCreate,
    LedgerEntryRead,
    LedgerEntryUpdate,
    PaymentAllocationCreate,
    PaymentAllocationRead,
    PaymentChannelAccountCreate,
    PaymentChannelAccountRead,
    PaymentChannelAccountUpdate,
    PaymentChannelCreate,
    PaymentChannelRead,
    PaymentChannelUpdate,
    PaymentCreate,
    PaymentMethodCreate,
    PaymentMethodRead,
    PaymentMethodUpdate,
    PaymentProviderCreate,
    PaymentProviderEventIngest,
    PaymentProviderEventRead,
    PaymentProviderRead,
    PaymentProviderUpdate,
    PaymentRead,
    PaymentUpdate,
    TaxRateCreate,
    TaxRateRead,
    TaxRateUpdate,
)
from app.schemas.common import ListResponse
from app.services import api_billing_webhooks as api_billing_webhooks_service
from app.services import billing as billing_service
from app.services import billing_automation as billing_automation_service
from app.services.auth_dependencies import require_permission

router = APIRouter()


# --- Dashboard ---


@router.get(
    "/dashboard",
    tags=["billing"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def billing_dashboard(db: Session = Depends(get_db)) -> dict:
    """Billing dashboard stats for external consumers."""
    from app.services.billing.reporting import billing_reporting

    return billing_reporting.get_dashboard_stats(db)


# --- Invoices ---


@router.post(
    "/invoices",
    response_model=InvoiceRead,
    status_code=status.HTTP_201_CREATED,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_invoice(payload: InvoiceCreate, db: Session = Depends(get_db)):
    return billing_service.invoices.create(db, payload)


@router.get(
    "/invoices/{invoice_id}",
    response_model=InvoiceRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_invoice(invoice_id: str, db: Session = Depends(get_db)):
    return billing_service.invoices.get(db, invoice_id)


@router.get(
    "/invoices",
    response_model=ListResponse[InvoiceRead],
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_invoices(
    account_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.invoices.list_response(
        db, account_id, status, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/invoices/{invoice_id}",
    response_model=InvoiceRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_invoice(
    invoice_id: str, payload: InvoiceUpdate, db: Session = Depends(get_db)
):
    return billing_service.invoices.update(db, invoice_id, payload)


@router.post(
    "/invoices/{invoice_id}/write-off",
    response_model=InvoiceRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def write_off_invoice(
    invoice_id: str, payload: InvoiceWriteOffRequest, db: Session = Depends(get_db)
):
    return billing_service.invoices.write_off(db, invoice_id, payload.memo)


@router.post(
    "/invoices/bulk-write-off",
    response_model=InvoiceBulkActionResponse,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def bulk_write_off_invoices(
    payload: InvoiceBulkWriteOffRequest, db: Session = Depends(get_db)
):
    response = billing_service.invoices.bulk_write_off_response(db, payload)
    return InvoiceBulkActionResponse(**response)


@router.post(
    "/invoices/bulk-void",
    response_model=InvoiceBulkActionResponse,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def bulk_void_invoices(payload: InvoiceBulkVoidRequest, db: Session = Depends(get_db)):
    response = billing_service.invoices.bulk_void_response(db, payload)
    return InvoiceBulkActionResponse(**response)


@router.post(
    "/invoice-runs",
    response_model=InvoiceRunResponse,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def run_invoice_cycle(payload: InvoiceRunRequest, db: Session = Depends(get_db)):
    return billing_automation_service.run_invoice_cycle(
        db,
        run_at=payload.run_at,
        billing_cycle=payload.billing_cycle,
        dry_run=payload.dry_run,
    )


@router.get(
    "/billing-runs",
    response_model=ListResponse[BillingRunRead],
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_billing_runs(
    status: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.billing_runs.list_response(
        db, status, order_by, order_dir, limit, offset
    )


@router.get(
    "/billing-runs/{run_id}",
    response_model=BillingRunRead,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_billing_run(run_id: str, db: Session = Depends(get_db)):
    return billing_service.billing_runs.get(db, run_id)


@router.delete(
    "/invoices/{invoice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["invoices"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_invoice(invoice_id: str, db: Session = Depends(get_db)):
    billing_service.invoices.delete(db, invoice_id)


# --- Credit Notes ---


@router.post(
    "/credit-notes",
    response_model=CreditNoteRead,
    status_code=status.HTTP_201_CREATED,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_credit_note(payload: CreditNoteCreate, db: Session = Depends(get_db)):
    return billing_service.credit_notes.create(db, payload)


@router.get(
    "/credit-notes/{credit_note_id}",
    response_model=CreditNoteRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_credit_note(credit_note_id: str, db: Session = Depends(get_db)):
    return billing_service.credit_notes.get(db, credit_note_id)


@router.get(
    "/credit-notes",
    response_model=ListResponse[CreditNoteRead],
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_credit_notes(
    account_id: str | None = None,
    invoice_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.credit_notes.list_response(
        db,
        account_id,
        invoice_id,
        status,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/credit-notes/{credit_note_id}",
    response_model=CreditNoteRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_credit_note(
    credit_note_id: str, payload: CreditNoteUpdate, db: Session = Depends(get_db)
):
    return billing_service.credit_notes.update(db, credit_note_id, payload)


@router.delete(
    "/credit-notes/{credit_note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_credit_note(credit_note_id: str, db: Session = Depends(get_db)):
    billing_service.credit_notes.delete(db, credit_note_id)


@router.post(
    "/credit-notes/{credit_note_id}/void",
    response_model=CreditNoteRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def void_credit_note(credit_note_id: str, db: Session = Depends(get_db)):
    return billing_service.credit_notes.void(db, credit_note_id)


@router.post(
    "/credit-notes/{credit_note_id}/apply",
    response_model=CreditNoteApplicationRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def apply_credit_note(
    credit_note_id: str, payload: CreditNoteApplyRequest, db: Session = Depends(get_db)
):
    return billing_service.credit_notes.apply(db, credit_note_id, payload)


# --- Collection Accounts ---


@router.post(
    "/collection-accounts",
    response_model=CollectionAccountRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payment-accounts"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_collection_account(
    payload: CollectionAccountCreate, db: Session = Depends(get_db)
):
    return billing_service.collection_accounts.create(db, payload)


@router.get(
    "/collection-accounts/{account_id}",
    response_model=CollectionAccountRead,
    tags=["payment-accounts"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_collection_account(account_id: str, db: Session = Depends(get_db)):
    return billing_service.collection_accounts.get(db, account_id)


@router.get(
    "/collection-accounts",
    response_model=ListResponse[CollectionAccountRead],
    tags=["payment-accounts"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_collection_accounts(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.collection_accounts.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/collection-accounts/{account_id}",
    response_model=CollectionAccountRead,
    tags=["payment-accounts"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_collection_account(
    account_id: str, payload: CollectionAccountUpdate, db: Session = Depends(get_db)
):
    return billing_service.collection_accounts.update(db, account_id, payload)


@router.delete(
    "/collection-accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["payment-accounts"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_collection_account(account_id: str, db: Session = Depends(get_db)):
    billing_service.collection_accounts.delete(db, account_id)


# --- Payment Channels ---


@router.post(
    "/payment-channels",
    response_model=PaymentChannelRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_payment_channel(
    payload: PaymentChannelCreate, db: Session = Depends(get_db)
):
    return billing_service.payment_channels.create(db, payload)


@router.get(
    "/payment-channels/{channel_id}",
    response_model=PaymentChannelRead,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_payment_channel(channel_id: str, db: Session = Depends(get_db)):
    return billing_service.payment_channels.get(db, channel_id)


@router.get(
    "/payment-channels",
    response_model=ListResponse[PaymentChannelRead],
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_payment_channels(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.payment_channels.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/payment-channels/{channel_id}",
    response_model=PaymentChannelRead,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_payment_channel(
    channel_id: str, payload: PaymentChannelUpdate, db: Session = Depends(get_db)
):
    return billing_service.payment_channels.update(db, channel_id, payload)


@router.delete(
    "/payment-channels/{channel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_payment_channel(channel_id: str, db: Session = Depends(get_db)):
    billing_service.payment_channels.delete(db, channel_id)


# --- Payment Channel Accounts ---


@router.post(
    "/payment-channel-accounts",
    response_model=PaymentChannelAccountRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_payment_channel_account(
    payload: PaymentChannelAccountCreate, db: Session = Depends(get_db)
):
    return billing_service.payment_channel_accounts.create(db, payload)


@router.get(
    "/payment-channel-accounts/{mapping_id}",
    response_model=PaymentChannelAccountRead,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_payment_channel_account(mapping_id: str, db: Session = Depends(get_db)):
    return billing_service.payment_channel_accounts.get(db, mapping_id)


@router.get(
    "/payment-channel-accounts",
    response_model=ListResponse[PaymentChannelAccountRead],
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_payment_channel_accounts(
    channel_id: str | None = None,
    collection_account_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.payment_channel_accounts.list_response(
        db,
        channel_id,
        collection_account_id,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/payment-channel-accounts/{mapping_id}",
    response_model=PaymentChannelAccountRead,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_payment_channel_account(
    mapping_id: str, payload: PaymentChannelAccountUpdate, db: Session = Depends(get_db)
):
    return billing_service.payment_channel_accounts.update(db, mapping_id, payload)


@router.delete(
    "/payment-channel-accounts/{mapping_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["payment-channels"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_payment_channel_account(mapping_id: str, db: Session = Depends(get_db)):
    billing_service.payment_channel_accounts.delete(db, mapping_id)


# --- Payment Allocations ---


@router.post(
    "/payment-allocations",
    response_model=PaymentAllocationRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_payment_allocation(
    payload: PaymentAllocationCreate, db: Session = Depends(get_db)
):
    return billing_service.payment_allocations.create(db, payload)


@router.get(
    "/payment-allocations",
    response_model=ListResponse[PaymentAllocationRead],
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_payment_allocations(
    payment_id: str | None = None,
    invoice_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.payment_allocations.list_response(
        db, payment_id, invoice_id, order_by, order_dir, limit, offset
    )


@router.delete(
    "/payment-allocations/{allocation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_payment_allocation(allocation_id: str, db: Session = Depends(get_db)):
    billing_service.payment_allocations.delete(db, allocation_id)


# --- Credit Note Lines ---


@router.post(
    "/credit-note-lines",
    response_model=CreditNoteLineRead,
    status_code=status.HTTP_201_CREATED,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_credit_note_line(
    payload: CreditNoteLineCreate, db: Session = Depends(get_db)
):
    return billing_service.credit_note_lines.create(db, payload)


@router.get(
    "/credit-note-lines/{line_id}",
    response_model=CreditNoteLineRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_credit_note_line(line_id: str, db: Session = Depends(get_db)):
    return billing_service.credit_note_lines.get(db, line_id)


@router.get(
    "/credit-note-lines",
    response_model=ListResponse[CreditNoteLineRead],
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_credit_note_lines(
    credit_note_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.credit_note_lines.list_response(
        db, credit_note_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/credit-note-lines/{line_id}",
    response_model=CreditNoteLineRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_credit_note_line(
    line_id: str, payload: CreditNoteLineUpdate, db: Session = Depends(get_db)
):
    return billing_service.credit_note_lines.update(db, line_id, payload)


@router.delete(
    "/credit-note-lines/{line_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_credit_note_line(line_id: str, db: Session = Depends(get_db)):
    billing_service.credit_note_lines.delete(db, line_id)


# --- Credit Note Applications ---


@router.get(
    "/credit-note-applications",
    response_model=ListResponse[CreditNoteApplicationRead],
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_credit_note_applications(
    credit_note_id: str | None = None,
    invoice_id: str | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.credit_note_applications.list_response(
        db, credit_note_id, invoice_id, order_by, order_dir, limit, offset
    )


@router.get(
    "/credit-note-applications/{application_id}",
    response_model=CreditNoteApplicationRead,
    tags=["credit-notes"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_credit_note_application(application_id: str, db: Session = Depends(get_db)):
    return billing_service.credit_note_applications.get(db, application_id)


# --- Invoice Lines ---


@router.post(
    "/invoice-lines",
    response_model=InvoiceLineRead,
    status_code=status.HTTP_201_CREATED,
    tags=["invoice-lines"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_invoice_line(payload: InvoiceLineCreate, db: Session = Depends(get_db)):
    return billing_service.invoice_lines.create(db, payload)


@router.get(
    "/invoice-lines/{line_id}",
    response_model=InvoiceLineRead,
    tags=["invoice-lines"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_invoice_line(line_id: str, db: Session = Depends(get_db)):
    return billing_service.invoice_lines.get(db, line_id)


@router.get(
    "/invoice-lines",
    response_model=ListResponse[InvoiceLineRead],
    tags=["invoice-lines"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_invoice_lines(
    invoice_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.invoice_lines.list_response(
        db, invoice_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/invoice-lines/{line_id}",
    response_model=InvoiceLineRead,
    tags=["invoice-lines"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_invoice_line(
    line_id: str, payload: InvoiceLineUpdate, db: Session = Depends(get_db)
):
    return billing_service.invoice_lines.update(db, line_id, payload)


@router.delete(
    "/invoice-lines/{line_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["invoice-lines"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_invoice_line(line_id: str, db: Session = Depends(get_db)):
    billing_service.invoice_lines.delete(db, line_id)


# --- Payment Methods ---


@router.post(
    "/payment-methods",
    response_model=PaymentMethodRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payment-methods"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_payment_method(payload: PaymentMethodCreate, db: Session = Depends(get_db)):
    return billing_service.payment_methods.create(db, payload)


@router.get(
    "/payment-methods/{method_id}",
    response_model=PaymentMethodRead,
    tags=["payment-methods"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_payment_method(method_id: str, db: Session = Depends(get_db)):
    return billing_service.payment_methods.get(db, method_id)


@router.get(
    "/payment-methods",
    response_model=ListResponse[PaymentMethodRead],
    tags=["payment-methods"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_payment_methods(
    account_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.payment_methods.list_response(
        db, account_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/payment-methods/{method_id}",
    response_model=PaymentMethodRead,
    tags=["payment-methods"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_payment_method(
    method_id: str, payload: PaymentMethodUpdate, db: Session = Depends(get_db)
):
    return billing_service.payment_methods.update(db, method_id, payload)


@router.delete(
    "/payment-methods/{method_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["payment-methods"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_payment_method(method_id: str, db: Session = Depends(get_db)):
    billing_service.payment_methods.delete(db, method_id)


# --- Payment Providers ---


@router.post(
    "/payment-providers",
    response_model=PaymentProviderRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payment-providers"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_payment_provider(
    payload: PaymentProviderCreate, db: Session = Depends(get_db)
):
    return billing_service.payment_providers.create(db, payload)


@router.get(
    "/payment-providers/{provider_id}",
    response_model=PaymentProviderRead,
    tags=["payment-providers"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_payment_provider(provider_id: str, db: Session = Depends(get_db)):
    return billing_service.payment_providers.get(db, provider_id)


@router.get(
    "/payment-providers",
    response_model=ListResponse[PaymentProviderRead],
    tags=["payment-providers"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_payment_providers(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.payment_providers.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/payment-providers/{provider_id}",
    response_model=PaymentProviderRead,
    tags=["payment-providers"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_payment_provider(
    provider_id: str, payload: PaymentProviderUpdate, db: Session = Depends(get_db)
):
    return billing_service.payment_providers.update(db, provider_id, payload)


@router.delete(
    "/payment-providers/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["payment-providers"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_payment_provider(provider_id: str, db: Session = Depends(get_db)):
    billing_service.payment_providers.delete(db, provider_id)


# --- Payment Events ---


@router.post(
    "/payment-events/ingest",
    response_model=PaymentProviderEventRead,
    tags=["payment-events"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def ingest_payment_event(
    payload: PaymentProviderEventIngest, db: Session = Depends(get_db)
):
    return billing_service.payment_provider_events.ingest(db, payload)


@router.get(
    "/payment-events/{event_id}",
    response_model=PaymentProviderEventRead,
    tags=["payment-events"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_payment_event(event_id: str, db: Session = Depends(get_db)):
    return billing_service.payment_provider_events.get(db, event_id)


@router.get(
    "/payment-events",
    response_model=ListResponse[PaymentProviderEventRead],
    tags=["payment-events"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_payment_events(
    provider_id: str | None = None,
    payment_id: str | None = None,
    invoice_id: str | None = None,
    status: str | None = None,
    order_by: str = Query(default="received_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.payment_provider_events.list_response(
        db,
        provider_id,
        payment_id,
        invoice_id,
        status,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.post(
    "/payment-events/paystack",
    tags=["payment-events"],
)
async def paystack_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("X-Paystack-Signature", "")
    return api_billing_webhooks_service.process_paystack_webhook(
        db=db,
        body=body,
        signature=signature,
    )


@router.post(
    "/payment-events/flutterwave",
    tags=["payment-events"],
)
async def flutterwave_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("verif-hash", "")
    return api_billing_webhooks_service.process_flutterwave_webhook(
        db=db,
        body=body,
        signature=signature,
    )


# --- Bank Accounts ---


@router.post(
    "/bank-accounts",
    response_model=BankAccountRead,
    status_code=status.HTTP_201_CREATED,
    tags=["bank-accounts"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_bank_account(payload: BankAccountCreate, db: Session = Depends(get_db)):
    return billing_service.bank_accounts.create(db, payload)


@router.get(
    "/bank-accounts/{bank_account_id}",
    response_model=BankAccountRead,
    tags=["bank-accounts"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_bank_account(bank_account_id: str, db: Session = Depends(get_db)):
    return billing_service.bank_accounts.get(db, bank_account_id)


@router.get(
    "/bank-accounts",
    response_model=ListResponse[BankAccountRead],
    tags=["bank-accounts"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_bank_accounts(
    account_id: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.bank_accounts.list_response(
        db, account_id, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/bank-accounts/{bank_account_id}",
    response_model=BankAccountRead,
    tags=["bank-accounts"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_bank_account(
    bank_account_id: str, payload: BankAccountUpdate, db: Session = Depends(get_db)
):
    return billing_service.bank_accounts.update(db, bank_account_id, payload)


@router.delete(
    "/bank-accounts/{bank_account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["bank-accounts"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_bank_account(bank_account_id: str, db: Session = Depends(get_db)):
    billing_service.bank_accounts.delete(db, bank_account_id)


# --- Payments ---


@router.post(
    "/payments",
    response_model=PaymentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_payment(payload: PaymentCreate, db: Session = Depends(get_db)):
    return billing_service.payments.create(db, payload)


@router.get(
    "/payments/{payment_id}",
    response_model=PaymentRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_payment(payment_id: str, db: Session = Depends(get_db)):
    return billing_service.payments.get(db, payment_id)


@router.get(
    "/payments",
    response_model=ListResponse[PaymentRead],
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_payments(
    account_id: str | None = None,
    invoice_id: str | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.payments.list_response(
        db,
        account_id,
        invoice_id,
        status,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/payments/{payment_id}",
    response_model=PaymentRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_payment(
    payment_id: str, payload: PaymentUpdate, db: Session = Depends(get_db)
):
    return billing_service.payments.update(db, payment_id, payload)


@router.post(
    "/payments/{payment_id}/mark-succeeded",
    response_model=PaymentRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def mark_payment_succeeded(payment_id: str, db: Session = Depends(get_db)):
    return billing_service.payments.mark_status(db, payment_id, status="succeeded")


@router.post(
    "/payments/{payment_id}/mark-failed",
    response_model=PaymentRead,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def mark_payment_failed(payment_id: str, db: Session = Depends(get_db)):
    return billing_service.payments.mark_status(db, payment_id, status="failed")


@router.delete(
    "/payments/{payment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["payments"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_payment(payment_id: str, db: Session = Depends(get_db)):
    billing_service.payments.delete(db, payment_id)


# --- Ledger Entries ---


@router.post(
    "/ledger-entries",
    response_model=LedgerEntryRead,
    status_code=status.HTTP_201_CREATED,
    tags=["ledger-entries"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_ledger_entry(payload: LedgerEntryCreate, db: Session = Depends(get_db)):
    return billing_service.ledger_entries.create(db, payload)


@router.get(
    "/ledger-entries/{entry_id}",
    response_model=LedgerEntryRead,
    tags=["ledger-entries"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_ledger_entry(entry_id: str, db: Session = Depends(get_db)):
    return billing_service.ledger_entries.get(db, entry_id)


@router.get(
    "/ledger-entries",
    response_model=ListResponse[LedgerEntryRead],
    tags=["ledger-entries"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_ledger_entries(
    account_id: str | None = None,
    entry_type: str | None = None,
    source: str | None = None,
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.ledger_entries.list_response(
        db,
        account_id,
        entry_type,
        source,
        is_active,
        order_by,
        order_dir,
        limit,
        offset,
    )


@router.patch(
    "/ledger-entries/{entry_id}",
    response_model=LedgerEntryRead,
    tags=["ledger-entries"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_ledger_entry(
    entry_id: str, payload: LedgerEntryUpdate, db: Session = Depends(get_db)
):
    return billing_service.ledger_entries.update(db, entry_id, payload)


@router.delete(
    "/ledger-entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["ledger-entries"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_ledger_entry(entry_id: str, db: Session = Depends(get_db)):
    billing_service.ledger_entries.delete(db, entry_id)


# --- Tax Rates ---


@router.post(
    "/tax-rates",
    response_model=TaxRateRead,
    status_code=status.HTTP_201_CREATED,
    tags=["tax-rates"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def create_tax_rate(payload: TaxRateCreate, db: Session = Depends(get_db)):
    return billing_service.tax_rates.create(db, payload)


@router.get(
    "/tax-rates/{rate_id}",
    response_model=TaxRateRead,
    tags=["tax-rates"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def get_tax_rate(rate_id: str, db: Session = Depends(get_db)):
    return billing_service.tax_rates.get(db, rate_id)


@router.get(
    "/tax-rates",
    response_model=ListResponse[TaxRateRead],
    tags=["tax-rates"],
    dependencies=[Depends(require_permission("billing:read"))],
)
def list_tax_rates(
    is_active: bool | None = None,
    order_by: str = Query(default="created_at"),
    order_dir: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return billing_service.tax_rates.list_response(
        db, is_active, order_by, order_dir, limit, offset
    )


@router.patch(
    "/tax-rates/{rate_id}",
    response_model=TaxRateRead,
    tags=["tax-rates"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def update_tax_rate(
    rate_id: str, payload: TaxRateUpdate, db: Session = Depends(get_db)
):
    return billing_service.tax_rates.update(db, rate_id, payload)


@router.delete(
    "/tax-rates/{rate_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["tax-rates"],
    dependencies=[Depends(require_permission("billing:write"))],
)
def delete_tax_rate(rate_id: str, db: Session = Depends(get_db)):
    billing_service.tax_rates.delete(db, rate_id)
