"""Sales orders — CRM port (Phase 3 §1.5 / §2.1 / §2.3).

Faithful port of ``dotmac_crm/app/services/sales_orders.py`` onto sub's
native models, with the Phase 3 deltas applied:

* Customer party: CRM ``person_id`` becomes ``subscriber_id`` — the CRM's
  person-mediated SO → sub identity chain (SO → project → person →
  ``selfcare_id``) collapses to the first-class column (§1.5/§2.3).
* The crm#233 ``account_id`` slot fix is ported as the FIXED shape: the
  legacy ``account_id``/``invoice_id`` schema fields are gone and the list
  API passes filters by keyword, so nothing can land in the wrong slot.
* ``order_number`` continues the CRM ``SO-%06d`` sequence via sub's
  ``document_sequences`` (key ``sales_order_number``, ``with_for_update``).
* **Financial side-effects are rewired native (§2.3):** the CRM's HTTP
  pushes to sub become direct in-process calls —

  - ``push_sales_order_subscription_to_selfcare`` →
    :func:`app.services.crm_api.create_subscription` per offer-tagged line,
    ``external_ref="sales_order:{id}:subscription:{line_id}"`` (unchanged);
  - ``push_sales_order_payment_to_selfcare`` →
    :func:`app.services.crm_api.record_external_payment`,
    ``external_ref="sales_order:{id}:payment"`` (unchanged);
  - ``ensure_installation_invoice_for_sales_order`` →
    :func:`app.services.crm_api.create_installation_invoice`,
    ``external_ref="project:{project_id}"`` (unchanged), still row-locking
    the project for the invoice-dedup metadata write.

  Every ``external_ref`` idempotency key is byte-identical to the HTTP era,
  so re-runs and historical rows stay deduped (risk #12 analogue).
* ``_accrue_reseller_commission`` is a stub — ``reseller_commission.py``
  ports with the referrals PR of the Phase 3 series (§2.3 module map).
* Install-project creation for manual (quote-less) sales orders is deferred
  to the projects service port (see ``_ensure_project_for_manual_sales_order``).
* Statuses are stored as plain strings (sub convention, §1.7).
* Native services emit sub events from day one (risk #13):
  ``sales_order.paid``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.models.project import Project
from app.models.sales import (
    Quote,
    QuoteLineItem,
    SalesOrder,
    SalesOrderLine,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.sequence import DocumentSequence
from app.models.subscriber import Subscriber
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    get_by_id,
    round_money,
    validate_enum,
)
from app.services.events import EventType, emit_event
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)

_PAID = SalesOrderPaymentStatus.paid.value
_PARTIAL = SalesOrderPaymentStatus.partial.value
_PENDING = SalesOrderPaymentStatus.pending.value
_WAIVED = SalesOrderPaymentStatus.waived.value


def _enum_str(value, enum_cls, label: str) -> str | None:
    member = validate_enum(value, enum_cls, label)
    return member.value if member is not None else None


def _ensure_subscriber(db: Session, subscriber_id) -> Subscriber:
    subscriber = get_by_id(db, Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    return subscriber


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="Invalid decimal value") from None


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid datetime value") from exc


def _next_sequence_value(db: Session, key: str, start_value: int = 1) -> int:
    sequence = (
        db.query(DocumentSequence)
        .filter(DocumentSequence.key == key)
        .with_for_update()
        .first()
    )
    if not sequence:
        sequence = DocumentSequence(key=key, next_value=start_value)
        db.add(sequence)
        db.flush()
    value = sequence.next_value
    sequence.next_value = value + 1
    db.flush()
    return value


def _generate_order_number(db: Session) -> str:
    # Continues the CRM sequence: the backfill imports the CRM row's
    # next_value under the same key (§1.5, risk #10).
    value = _next_sequence_value(db, "sales_order_number", 1)
    return f"SO-{value:06d}"


# ---------------------------------------------------------------------------
# §2.3 — sales-order financial side-effects, rewired native.
# CRM source: app/services/events/handlers/selfcare_customer.py (the
# push_sales_order_* + ensure_installation_invoice_for_sales_order pushers).
# ---------------------------------------------------------------------------


def _resolve_project_for_sales_order(db: Session, sales_order_id: object):
    """The active project a sales order spawned (metadata.sales_order_id).
    Shared by the installation-invoice and payment paths."""
    if not sales_order_id:
        return None
    existing = (
        db.query(Project)
        .filter(Project.is_active.is_(True))
        .filter(Project.metadata_["sales_order_id"].as_string() == str(sales_order_id))
        .order_by(Project.created_at.desc())
        .first()
    )
    if existing:
        return existing

    # SQLite JSON path comparisons are not reliable across SQLAlchemy/SQLite
    # builds. Fall back to an in-Python metadata check for tests/dev.
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        rows = (
            db.query(Project)
            .filter(Project.is_active.is_(True))
            .order_by(Project.created_at.desc())
            .all()
        )
        for row in rows:
            metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
            if str(metadata.get("sales_order_id")) == str(sales_order_id):
                return row
    return None


def _has_existing_installation_invoice(project: Project) -> bool:
    metadata = project.metadata_ if isinstance(project.metadata_, dict) else {}
    return bool(str(metadata.get("selfcare_installation_invoice_id") or "").strip())


def _find_existing_related_installation_invoice(
    db: Session, project: Project
) -> tuple[str, Decimal | None] | None:
    metadata = project.metadata_ if isinstance(project.metadata_, dict) else {}
    sales_order_id = metadata.get("sales_order_id")
    quote_id = metadata.get("quote_id")
    if not sales_order_id and not quote_id:
        return None

    filters = []
    if sales_order_id:
        filters.append(
            Project.metadata_["sales_order_id"].as_string() == str(sales_order_id)
        )
    if quote_id:
        filters.append(Project.metadata_["quote_id"].as_string() == str(quote_id))

    rows = (
        db.query(Project)
        .filter(Project.id != project.id)
        .filter(or_(*filters))
        .order_by(Project.created_at.desc())
        .all()
    )
    for row in rows:
        row_meta = row.metadata_ if isinstance(row.metadata_, dict) else {}
        invoice_id = str(row_meta.get("selfcare_installation_invoice_id") or "").strip()
        if invoice_id:
            return invoice_id, _parse_invoice_amount(
                row_meta.get("selfcare_installation_invoice_amount")
            )

    # SQLite JSON-path fallback (idempotency in tests/dev).
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        for row in db.query(Project).filter(Project.id != project.id).all():
            row_meta = row.metadata_ if isinstance(row.metadata_, dict) else {}
            same_sales_order = sales_order_id and str(
                row_meta.get("sales_order_id")
            ) == str(sales_order_id)
            same_quote = quote_id and str(row_meta.get("quote_id")) == str(quote_id)
            if not (same_sales_order or same_quote):
                continue
            invoice_id = str(
                row_meta.get("selfcare_installation_invoice_id") or ""
            ).strip()
            if invoice_id:
                return invoice_id, _parse_invoice_amount(
                    row_meta.get("selfcare_installation_invoice_amount")
                )
    return None


def _store_invoice_metadata(
    project: Project, invoice_id: str, amount: Decimal | None
) -> None:
    # Metadata keys keep their historical names — they are local Fact now
    # (§1.5): the ids point at sub's own invoice rows.
    metadata = dict(project.metadata_ or {})
    metadata["selfcare_installation_invoice_id"] = str(invoice_id)
    if amount is not None:
        metadata["selfcare_installation_invoice_amount"] = str(amount)
    metadata.pop("selfcare_installation_invoice_error", None)
    project.metadata_ = metadata


def _record_invoice_failure(project: Project, detail: str) -> None:
    metadata = dict(project.metadata_ or {})
    metadata["selfcare_installation_invoice_error"] = {
        "detail": detail[:500],
        "at": datetime.now(UTC).isoformat(),
    }
    project.metadata_ = metadata


def _parse_invoice_amount(value: object) -> Decimal | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _sum_installation_lines(lines) -> Decimal:
    total = Decimal("0.00")
    for line in lines:
        description = str(getattr(line, "description", "") or "").lower()
        if "installation" not in description:
            continue
        amount = Decimal(getattr(line, "amount", 0) or 0)
        if amount > 0:
            total += amount
    return total


def _installation_amount_from_sales_order(db: Session, sales_order_id) -> Decimal:
    if not sales_order_id:
        return Decimal("0.00")
    lines = (
        db.query(SalesOrderLine)
        .filter(SalesOrderLine.sales_order_id == coerce_uuid(str(sales_order_id)))
        .filter(SalesOrderLine.is_active.is_(True))
        .all()
    )
    return _sum_installation_lines(lines)


def _installation_amount_from_quote(db: Session, quote_id) -> Decimal:
    if not quote_id:
        return Decimal("0.00")
    lines = (
        db.query(QuoteLineItem)
        .filter(QuoteLineItem.quote_id == coerce_uuid(str(quote_id)))
        .all()
    )
    return _sum_installation_lines(lines)


def _resolve_installation_amount(db: Session, project: Project) -> Decimal:
    metadata = project.metadata_ if isinstance(project.metadata_, dict) else {}
    amount = _installation_amount_from_sales_order(db, metadata.get("sales_order_id"))
    if amount > 0:
        return amount
    return _installation_amount_from_quote(db, metadata.get("quote_id"))


def ensure_installation_invoice_for_sales_order(db: Session, sales_order_id) -> None:
    """Create the installation invoice for a sales order's project (§2.3).

    Native rewire of the CRM's ``ensure_installation_invoice_for_sales_order``:
    ``selfcare.create_installation_invoice`` (HTTP → ``POST /crm/invoices``)
    becomes an in-process :func:`app.services.crm_api.create_installation_invoice`
    call. The ``external_ref="project:{project_id}"`` idempotency key is
    unchanged, and the project row-lock still serializes concurrent triggers
    of the read-then-create-then-store sequence.
    """
    if not sales_order_id:
        return
    sales_order = db.get(SalesOrder, coerce_uuid(str(sales_order_id)))
    if not sales_order:
        return

    project = _resolve_project_for_sales_order(db, sales_order_id)
    if not project:
        return

    locked = (
        db.query(Project)
        .filter(Project.id == project.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if locked is None:
        return
    project = locked
    if _has_existing_installation_invoice(project):
        return

    related_invoice = _find_existing_related_installation_invoice(db, project)
    if related_invoice:
        invoice_id, amount = related_invoice
        _store_invoice_metadata(project, invoice_id, amount)
        db.add(project)
        db.commit()
        db.refresh(project)
        logger.info(
            "installation_invoice_reused project_id=%s invoice_id=%s",
            project.id,
            invoice_id,
        )
        return

    amount = _resolve_installation_amount(db, project)
    if amount <= 0:
        logger.info("invoice_skip_no_installation_cost project_id=%s", project.id)
        return

    subscriber_id = sales_order.subscriber_id or project.subscriber_id
    if not subscriber_id:
        return

    from app.services import crm_api

    try:
        invoice = crm_api.create_installation_invoice(
            db,
            subscriber_id=str(subscriber_id),
            amount=amount,
            description="Installation cost",
            external_ref=f"project:{project.id}",
            currency=sales_order.currency or "NGN",
        )
    except LookupError as exc:
        # Record the failure so it surfaces and a later trigger (or operator)
        # can retry — the external_ref dedup makes the retry safe.
        _record_invoice_failure(project, str(exc))
        db.add(project)
        db.commit()
        logger.error(
            "installation_invoice_failed project_id=%s error=%s", project.id, exc
        )
        return
    if not invoice:
        return

    _store_invoice_metadata(project, str(invoice.id), amount)
    db.add(project)
    db.commit()
    db.refresh(project)
    logger.info(
        "installation_invoice_created project_id=%s subscriber_id=%s "
        "invoice_id=%s amount=%s",
        project.id,
        subscriber_id,
        invoice.id,
        amount,
    )


def _line_offer_ref(line: object) -> str | None:
    """The sub CatalogOffer id/code a sales-order line was tagged with at
    quote time (metadata.sub_offer_id), identifying a recurring subscription
    charge vs a one-off installation line."""
    meta = getattr(line, "metadata_", None)
    if not isinstance(meta, dict):
        return None
    for key in ("sub_offer_id", "sub_offer_code", "offer_id", "offer_code"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return None


def _active_sales_order_lines(db: Session, sales_order_id) -> list[SalesOrderLine]:
    return (
        db.query(SalesOrderLine)
        .filter(SalesOrderLine.sales_order_id == sales_order_id)
        .filter(SalesOrderLine.is_active.is_(True))
        .all()
    )


def _push_sales_order_subscriptions(db: Session, sales_order: SalesOrder) -> None:
    """Create a subscription (plus its first invoice) for each sales-order
    line tagged with a sub offer (§2.3).

    Native rewire of ``push_sales_order_subscription_to_selfcare``:
    ``selfcare.create_subscription`` (HTTP → ``POST /crm/subscriptions``)
    becomes an in-process :func:`app.services.crm_api.create_subscription`
    call keyed on the unchanged
    ``external_ref="sales_order:{id}:subscription:{line_id}"``. Best-effort:
    a billing hiccup must never break the sale; the resolved ids are stored
    on the line metadata so repeated calls are safe.
    """
    from app.services import crm_api

    try:
        sales_order_id = sales_order.id
        if not sales_order_id:
            return
        if not sales_order.subscriber_id:
            return

        lines = _active_sales_order_lines(db, sales_order_id)
        offer_lines = [(line, ref) for line in lines if (ref := _line_offer_ref(line))]
        if not offer_lines:
            return

        for line, offer_ref in offer_lines:
            meta = line.metadata_ if isinstance(line.metadata_, dict) else {}
            if str(meta.get("selfcare_subscription_id") or "").strip():
                continue  # already synced

            try:
                result = crm_api.create_subscription(
                    db,
                    subscriber_id=str(sales_order.subscriber_id),
                    offer_ref=offer_ref,
                    external_ref=f"sales_order:{sales_order_id}:subscription:{line.id}",
                    unit_price=line.unit_price,
                )
            except LookupError:
                logger.warning(
                    "sales_order_subscription_offer_unresolved "
                    "sales_order_id=%s line_id=%s offer_ref=%s",
                    sales_order_id,
                    line.id,
                    offer_ref,
                )
                continue
            subscription = result.get("subscription") if result else None
            if subscription is None:
                continue
            invoice = result.get("invoice")
            new_meta = dict(line.metadata_ or {})
            new_meta["selfcare_subscription_id"] = str(subscription.id)
            if invoice is not None:
                new_meta["selfcare_subscription_invoice_id"] = str(invoice.id)
            line.metadata_ = new_meta
            db.add(line)
            logger.info(
                "sales_order_subscription_created sales_order_id=%s line_id=%s "
                "subscription_id=%s",
                sales_order_id,
                line.id,
                subscription.id,
            )
        db.commit()
    except Exception:
        logger.warning(
            "sales_order_subscription_sync_failed sales_order_id=%s",
            getattr(sales_order, "id", None),
            exc_info=True,
        )
        db.rollback()


def _record_sales_order_payment(db: Session, sales_order: SalesOrder) -> None:
    """Record the customer's payment against their account (§2.3).

    Native rewire of ``push_sales_order_payment_to_selfcare``:
    ``selfcare.record_payment`` (HTTP → ``POST /crm/payments``) becomes an
    in-process :func:`app.services.crm_api.record_external_payment` call
    keyed on the unchanged ``external_ref="sales_order:{id}:payment"``.

    The payment is charged to the account, not pinned to one invoice — the
    ledger auto-allocates it across open invoices (installation + the
    subscription's first invoice) oldest/soonest-due first, so a single
    upfront payment settles whatever the sale covered. Best-effort and
    idempotent (the external_ref dedups in the ledger).
    """
    from app.services import crm_api

    try:
        amount_paid = sales_order.amount_paid
        if amount_paid is None or Decimal(str(amount_paid)) <= 0:
            return
        sales_order_id = sales_order.id
        if not sales_order_id or not sales_order.subscriber_id:
            return

        # Ensure the installation invoice exists so the payment has something
        # to settle. The subscription's first invoice is created by the
        # subscription push, which runs before this, so a single payment can
        # settle both.
        ensure_installation_invoice_for_sales_order(db, sales_order_id)

        crm_api.record_external_payment(
            db,
            subscriber_id=str(sales_order.subscriber_id),
            amount=amount_paid,
            external_ref=f"sales_order:{sales_order_id}:payment",
            paid_at=sales_order.paid_at,
            memo=f"Sales order {sales_order.order_number or sales_order_id}",
            currency=sales_order.currency or "NGN",
        )
    except Exception:
        logger.warning(
            "sales_order_payment_record_failed sales_order_id=%s",
            getattr(sales_order, "id", None),
            exc_info=True,
        )
        db.rollback()


def _sync_sales_order_financials(db: Session, sales_order: SalesOrder) -> None:
    """Apply the paid-order financial side-effects natively (§2.3).

    Replaces the CRM's ``_sync_sales_order_payment_to_sub`` HTTP fan-out.
    Only fires on a paid/partial order; every step is idempotent, so
    repeated calls are safe.
    """
    if sales_order.payment_status not in {_PAID, _PARTIAL}:
        return
    # Create the subscription (and its first invoice) BEFORE recording the
    # payment, so the account-level payment can settle both the installation
    # and subscription invoices in one go.
    _push_sales_order_subscriptions(db, sales_order)
    _record_sales_order_payment(db, sales_order)


def _emit_sales_order_paid(
    db: Session, sales_order: SalesOrder, previous_payment_status: str | None
) -> None:
    if sales_order.payment_status != _PAID or previous_payment_status == _PAID:
        return
    try:
        emit_event(
            db,
            EventType.sales_order_paid,
            {
                "sales_order_id": str(sales_order.id),
                "order_number": sales_order.order_number,
                "total": str(sales_order.total or 0),
                "amount_paid": str(sales_order.amount_paid or 0),
                "currency": sales_order.currency,
            },
            subscriber_id=sales_order.subscriber_id,
        )
    except Exception:
        logger.warning(
            "sales_order_paid_event_failed sales_order_id=%s",
            sales_order.id,
            exc_info=True,
        )


def _apply_payment_fields(sales_order: SalesOrder, data: dict) -> None:
    if "amount_paid" in data or "total" in data:
        total = Decimal(data.get("total") or sales_order.total or 0)
        amount_paid = Decimal(data.get("amount_paid") or sales_order.amount_paid or 0)
        balance_due = round_money(total - amount_paid)
        sales_order.total = round_money(total)
        sales_order.amount_paid = round_money(amount_paid)
        sales_order.balance_due = balance_due
        if total > 0 and balance_due <= 0:
            sales_order.payment_status = _PAID
            if not sales_order.paid_at:
                sales_order.paid_at = datetime.now(UTC)
        elif amount_paid > 0:
            sales_order.payment_status = _PARTIAL
        else:
            sales_order.payment_status = _PENDING
    if sales_order.payment_status == _PAID:
        if sales_order.status in {
            SalesOrderStatus.draft.value,
            SalesOrderStatus.confirmed.value,
        }:
            sales_order.status = SalesOrderStatus.paid.value
    elif (
        sales_order.payment_status == _WAIVED
        and sales_order.status == SalesOrderStatus.draft.value
    ):
        sales_order.status = SalesOrderStatus.confirmed.value


def _recalculate_order_totals(db: Session, sales_order_id: str) -> None:
    sales_order = db.get(SalesOrder, coerce_uuid(sales_order_id))
    if not sales_order:
        return
    totals = (
        db.query(func.coalesce(func.sum(SalesOrderLine.amount), 0))
        .filter(SalesOrderLine.sales_order_id == sales_order.id)
        .filter(SalesOrderLine.is_active.is_(True))
        .scalar()
    )
    subtotal = round_money(Decimal(totals or 0))
    sales_order.subtotal = subtotal
    sales_order.total = round_money(subtotal + Decimal(sales_order.tax_total or 0))
    _apply_payment_fields(sales_order, {"total": sales_order.total})
    db.flush()


def _ensure_fulfillment(db: Session, sales_order: SalesOrder) -> None:
    """Placeholder for fulfillment actions once implemented (§2.1: stays a
    no-op through the port)."""
    return None


def _accrue_reseller_commission(db: Session, sales_order: SalesOrder | None) -> None:
    """Stub — ``reseller_commission.py`` ports with the referrals PR of the
    Phase 3 series (§2.3 module map: COPY with referrals). The call sites are
    kept so the referrals PR only has to fill this in; a commission hiccup
    must never break sales-order processing either way.
    """
    logger.debug(
        "reseller_commission_accrual_deferred sales_order_id=%s (referrals PR pending)",
        getattr(sales_order, "id", None),
    )
    return None


def _ensure_project_for_manual_sales_order(db: Session, sales_order: SalesOrder):
    """Deferred to the projects service port (Phase 3 PR 6).

    The CRM creates an install project for manual (quote-less) sales orders
    (idempotent on ``Project.metadata_["sales_order_id"]``, template by
    ``metadata.project_type``). Sub's projects service has not been ported
    yet — the projects PR rewires this hook onto it.
    """
    if sales_order.quote_id:
        # Quote-driven flow already creates projects on quote acceptance.
        return None
    logger.info(
        "sales_order_project_deferred sales_order_id=%s "
        "(projects service port pending)",
        sales_order.id,
    )
    return None


class SalesOrders(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload):
        data = payload.model_dump()
        if data.get("status"):
            data["status"] = _enum_str(data["status"], SalesOrderStatus, "status")
        if data.get("payment_status"):
            data["payment_status"] = _enum_str(
                data["payment_status"], SalesOrderPaymentStatus, "payment_status"
            )
        total_value = Decimal(data.get("total") or 0)
        amount_paid_value = Decimal(data.get("amount_paid") or 0)

        _ensure_subscriber(db, data.get("subscriber_id"))
        if data.get("quote_id"):
            quote = db.get(
                Quote, coerce_uuid(data["quote_id"]), options=[selectinload(Quote.lead)]
            )
            if not quote:
                raise HTTPException(status_code=404, detail="Quote not found")
            existing = (
                db.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).first()
            )
            if existing:
                raise HTTPException(
                    status_code=400, detail="Sales order already exists for this quote"
                )
            if quote.lead:
                data["owner_agent_id"] = (
                    data.get("owner_agent_id") or quote.lead.owner_agent_id
                )
                data["source"] = data.get("source") or quote.lead.lead_source

        if not data.get("order_number"):
            data["order_number"] = _generate_order_number(db)

        if data.get("total") is not None and data.get("balance_due") is None:
            data["amount_paid"] = round_money(amount_paid_value)
            data["balance_due"] = round_money(total_value - amount_paid_value)

        sales_order = SalesOrder(**data)
        _apply_payment_fields(sales_order, data)
        db.add(sales_order)
        db.commit()
        db.refresh(sales_order)
        _ensure_fulfillment(db, sales_order)
        _ensure_project_for_manual_sales_order(db, sales_order)
        db.commit()
        db.refresh(sales_order)
        _accrue_reseller_commission(db, sales_order)
        _sync_sales_order_financials(db, sales_order)
        _emit_sales_order_paid(db, sales_order, previous_payment_status=None)
        return sales_order

    @staticmethod
    def create_from_quote(db: Session, quote_id: str) -> SalesOrder:
        quote = db.get(
            Quote,
            coerce_uuid(quote_id),
            options=[selectinload(Quote.line_items), selectinload(Quote.lead)],
        )
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found")
        existing = db.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).first()
        if existing:
            return existing

        order_number = _generate_order_number(db)
        sales_order = SalesOrder(
            quote_id=quote.id,
            subscriber_id=quote.subscriber_id,
            owner_agent_id=quote.lead.owner_agent_id if quote.lead else None,
            source=quote.lead.lead_source if quote.lead else None,
            order_number=order_number,
            status=SalesOrderStatus.confirmed.value,
            payment_status=SalesOrderPaymentStatus.pending.value,
            currency=quote.currency,
            subtotal=quote.subtotal,
            tax_total=quote.tax_total,
            total=quote.total,
            amount_paid=Decimal("0.00"),
            balance_due=quote.total,
        )
        db.add(sales_order)
        db.flush()

        for item in quote.line_items:
            amount = item.amount
            if amount is None:
                amount = Decimal(item.quantity or 0) * Decimal(item.unit_price or 0)
            line = SalesOrderLine(
                sales_order_id=sales_order.id,
                inventory_item_id=item.inventory_item_id,
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                amount=amount,
                metadata_=dict(item.metadata_) if item.metadata_ else None,
            )
            db.add(line)

        db.commit()
        db.refresh(sales_order)
        return sales_order

    @staticmethod
    def get(db: Session, sales_order_id: str):
        sales_order = db.get(
            SalesOrder,
            coerce_uuid(sales_order_id),
            options=[selectinload(SalesOrder.lines)],
        )
        if not sales_order:
            raise HTTPException(status_code=404, detail="Sales order not found")
        return sales_order

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None = None,
        quote_id: str | None = None,
        status: str | None = None,
        payment_status: str | None = None,
        is_active: bool | None = None,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ):
        query = db.query(SalesOrder)
        if subscriber_id:
            query = query.filter(SalesOrder.subscriber_id == coerce_uuid(subscriber_id))
        if quote_id:
            query = query.filter(SalesOrder.quote_id == coerce_uuid(quote_id))
        if status:
            query = query.filter(
                SalesOrder.status == _enum_str(status, SalesOrderStatus, "status")
            )
        if payment_status:
            query = query.filter(
                SalesOrder.payment_status
                == _enum_str(payment_status, SalesOrderPaymentStatus, "payment_status")
            )
        if is_active is None:
            query = query.filter(SalesOrder.is_active.is_(True))
        else:
            query = query.filter(SalesOrder.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SalesOrder.created_at, "updated_at": SalesOrder.updated_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, sales_order_id: str, payload):
        sales_order = db.get(SalesOrder, coerce_uuid(sales_order_id))
        if not sales_order:
            raise HTTPException(status_code=404, detail="Sales order not found")
        previous_payment_status = sales_order.payment_status
        data = payload.model_dump(exclude_unset=True)
        if "status" in data:
            data["status"] = _enum_str(data["status"], SalesOrderStatus, "status")
        if "payment_status" in data:
            data["payment_status"] = _enum_str(
                data["payment_status"], SalesOrderPaymentStatus, "payment_status"
            )
        if data.get("subscriber_id"):
            _ensure_subscriber(db, data["subscriber_id"])
        if data.get("quote_id"):
            quote = db.get(
                Quote, coerce_uuid(data["quote_id"]), options=[selectinload(Quote.lead)]
            )
            if not quote:
                raise HTTPException(status_code=404, detail="Quote not found")
            existing = (
                db.query(SalesOrder)
                .filter(
                    SalesOrder.quote_id == quote.id, SalesOrder.id != sales_order.id
                )
                .first()
            )
            if existing:
                raise HTTPException(
                    status_code=400, detail="Sales order already exists for this quote"
                )
            if quote.lead:
                data["owner_agent_id"] = (
                    data.get("owner_agent_id")
                    or sales_order.owner_agent_id
                    or quote.lead.owner_agent_id
                )
                data["source"] = (
                    data.get("source") or sales_order.source or quote.lead.lead_source
                )

        if data.get("payment_status") == _PAID:
            resolved_total = Decimal(data.get("total") or sales_order.total or 0)
            resolved_amount_paid = Decimal(
                data.get("amount_paid") or sales_order.amount_paid or 0
            )
            if resolved_amount_paid < resolved_total:
                data["amount_paid"] = round_money(resolved_total)
            data["balance_due"] = Decimal("0.00")
            if "paid_at" not in data or data.get("paid_at") is None:
                data["paid_at"] = datetime.now(UTC)
            if "status" not in data and sales_order.status in {
                SalesOrderStatus.draft.value,
                SalesOrderStatus.confirmed.value,
            }:
                data["status"] = SalesOrderStatus.paid.value

        for key, value in data.items():
            setattr(sales_order, key, value)

        _apply_payment_fields(sales_order, data)
        _ensure_fulfillment(db, sales_order)
        db.commit()
        db.refresh(sales_order)
        # Accrue on any transition into paid (idempotent). Covers
        # update_from_input too.
        _accrue_reseller_commission(db, sales_order)
        _sync_sales_order_financials(db, sales_order)
        _emit_sales_order_paid(
            db, sales_order, previous_payment_status=previous_payment_status
        )
        return sales_order

    @staticmethod
    def update_from_input(
        db: Session,
        sales_order_id: str,
        *,
        status: str | None = None,
        payment_status: str | None = None,
        total: str | None = None,
        amount_paid: str | None = None,
        paid_at: str | None = None,
        notes: str | None = None,
        owner_agent_id: str | None = None,
        source: str | None = None,
    ):
        """Update a sales order using raw string inputs (e.g. web forms)."""
        update_data: dict[str, Any] = {}
        if status:
            update_data["status"] = validate_enum(status, SalesOrderStatus, "status")
        if payment_status:
            update_data["payment_status"] = validate_enum(
                payment_status, SalesOrderPaymentStatus, "payment_status"
            )

        total_value = _parse_decimal(total)
        if total_value is not None:
            update_data["total"] = total_value

        amount_paid_value = _parse_decimal(amount_paid)
        if amount_paid_value is not None:
            update_data["amount_paid"] = amount_paid_value

        paid_at_value = _parse_datetime(paid_at)
        if paid_at is not None:
            update_data["paid_at"] = paid_at_value

        if notes is not None:
            update_data["notes"] = notes.strip() or None
        if owner_agent_id is not None:
            update_data["owner_agent_id"] = (
                coerce_uuid(owner_agent_id) if owner_agent_id.strip() else None
            )
        if source is not None:
            update_data["source"] = source.strip() or None

        # If payment status is paid and paid_at is missing, set it now to
        # satisfy the schema validation.
        if (
            update_data.get("payment_status") == SalesOrderPaymentStatus.paid
            and update_data.get("paid_at") is None
        ):
            update_data["paid_at"] = datetime.now(UTC)

        from app.schemas.sales_order import SalesOrderUpdate

        payload = SalesOrderUpdate(**update_data)
        return SalesOrders.update(db, sales_order_id, payload)

    @staticmethod
    def delete(db: Session, sales_order_id: str):
        sales_order = db.get(SalesOrder, coerce_uuid(sales_order_id))
        if not sales_order:
            raise HTTPException(status_code=404, detail="Sales order not found")
        sales_order.is_active = False
        db.commit()


class SalesOrderLines(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload):
        sales_order = db.get(SalesOrder, payload.sales_order_id)
        if not sales_order:
            raise HTTPException(status_code=404, detail="Sales order not found")
        data = payload.model_dump()
        if not data.get("amount"):
            data["amount"] = Decimal(data.get("quantity") or 0) * Decimal(
                data.get("unit_price") or 0
            )
        line = SalesOrderLine(**data)
        db.add(line)
        db.flush()
        _recalculate_order_totals(db, str(sales_order.id))
        db.commit()
        db.refresh(line)
        ensure_installation_invoice_for_sales_order(db, sales_order.id)
        db.refresh(sales_order)
        _accrue_reseller_commission(db, sales_order)
        return line

    @staticmethod
    def update(db: Session, line_id: str, payload):
        line = db.get(SalesOrderLine, coerce_uuid(line_id))
        if not line:
            raise HTTPException(status_code=404, detail="Sales order line not found")
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(line, key, value)
        if "quantity" in data or "unit_price" in data:
            line.amount = Decimal(line.quantity or 0) * Decimal(line.unit_price or 0)
        db.flush()
        _recalculate_order_totals(db, str(line.sales_order_id))
        db.commit()
        db.refresh(line)
        ensure_installation_invoice_for_sales_order(db, line.sales_order_id)
        sales_order = db.get(SalesOrder, line.sales_order_id)
        _accrue_reseller_commission(db, sales_order)
        return line

    @staticmethod
    def list(
        db: Session,
        sales_order_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SalesOrderLine)
        if sales_order_id:
            query = query.filter(
                SalesOrderLine.sales_order_id == coerce_uuid(sales_order_id)
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SalesOrderLine.created_at},
        )
        return apply_pagination(query, limit, offset).all()


sales_orders = SalesOrders()
sales_order_lines = SalesOrderLines()
