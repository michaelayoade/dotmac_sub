"""Native sales orders ported from CRM.

Faithful port of ``dotmac_crm/app/services/sales_orders.py`` onto sub's
native models, with Sub ownership deltas applied:

* Customer party: CRM ``person_id`` becomes ``subscriber_id`` — the CRM's
  person-mediated SO → sub identity chain (SO → project → person →
  ``selfcare_id``) collapses to the first-class column.
* The crm#233 ``account_id`` slot fix is ported as the FIXED shape: the
  legacy ``account_id``/``invoice_id`` schema fields are gone and the list
  API passes filters by keyword, so nothing can land in the wrong slot.
* ``order_number`` continues the CRM ``SO-%06d`` sequence via sub's
  ``document_sequences`` (key ``sales_order_number``, ``with_for_update``).
* **Financial side-effects are rewired native:** the CRM's HTTP
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
* ``_accrue_reseller_commission`` is a stub until the native referral and
  reseller-commission capability owns that side effect.
* Install-project creation for manual (quote-less) sales orders is deferred
  to the projects service port (see ``_ensure_project_for_manual_sales_order``).
* Statuses are stored as plain strings (sub convention, ).
* Native services emit sub events from day one (risk #13):
  ``sales_order.paid``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import Subscription
from app.models.project import Project, ProjectTask
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
    net_line_amount,
    round_money,
    validate_enum,
)
from app.services.events import EventType, emit_event
from app.services.response import ListResponseMixin
from app.services.sales import lifecycle as lead_lifecycle

logger = logging.getLogger(__name__)

_PAID = SalesOrderPaymentStatus.paid.value
_PARTIAL = SalesOrderPaymentStatus.partial.value
_PENDING = SalesOrderPaymentStatus.pending.value
_WAIVED = SalesOrderPaymentStatus.waived.value


class SalesOrderLifecycleError(ValueError):
    def __init__(self, code: str, message: str, *, kind: str = "conflict") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


def fulfill_from_customer_experience(
    db: Session,
    *,
    sales_order_id: UUID,
    handoff_id: UUID,
    actor_id: str,
) -> bool:
    """Apply accepted CX evidence through the SalesOrder lifecycle owner."""

    actor = str(actor_id or "").strip()
    if not actor:
        raise SalesOrderLifecycleError(
            "actor_required", "Sales-order fulfilment actor is required", kind="invalid"
        )
    order = db.scalars(
        select(SalesOrder)
        .where(SalesOrder.id == coerce_uuid(sales_order_id))
        .with_for_update()
    ).one_or_none()
    if order is None or not order.is_active:
        raise SalesOrderLifecycleError(
            "sales_order_not_found", "Sales order not found", kind="not_found"
        )
    metadata = dict(order.metadata_ or {})
    recorded_handoff_id = metadata.get("cx_handoff_id")
    if order.status == SalesOrderStatus.fulfilled.value:
        if recorded_handoff_id not in {None, str(handoff_id)}:
            raise SalesOrderLifecycleError(
                "handoff_evidence_conflict",
                "Sales order was fulfilled by different CX evidence",
            )
        return False
    # A waived order is settled too — nothing is owed on it — so it must be
    # able to reach fulfilled. Gating on status == paid stranded every waived
    # order at confirmed for good.
    if not funding_is_settled(order):
        raise SalesOrderLifecycleError(
            "sales_order_not_settled",
            "Only a fully paid or waived sales order can be fulfilled",
        )
    previous_status = order.status
    assert_legal_sales_order_transition(
        previous_status, SalesOrderStatus.fulfilled.value
    )
    order.status = SalesOrderStatus.fulfilled.value
    metadata["cx_handoff_id"] = str(handoff_id)
    order.metadata_ = metadata
    emit_event(
        db,
        EventType.sales_order_fulfilled,
        {
            "sales_order_id": str(order.id),
            "cx_handoff_id": str(handoff_id),
            "from_status": previous_status,
            "to_status": SalesOrderStatus.fulfilled.value,
        },
        actor=actor,
        subscriber_id=order.subscriber_id,
    )
    db.flush()
    return True


#: Legal edges of the SalesOrder's own lifecycle.
#:
#: This is the Sale pipeline's state machine, declared rather than scattered.
#: Until now the edges lived as loose ``if`` guards across several functions,
#: which is how a waived order came to be permanently stranded at ``confirmed``:
#: no one could see the whole machine at once.
#:
#: ``payment_status`` deliberately has no table here. It is a Money-pipeline
#: fact being migrated out to the ledger — giving it a Sale-owned state machine
#: now would entrench the duplication. See SALE_TO_MONEY_HANDOFF_SOT.md.
#:
#: ``paid``/``fulfilled`` -> ``cancelled`` are deliberately absent. Cancelling
#: an order that has taken money or delivered work strands a refund obligation
#: and possibly a live subscription, and cancellation currently has NO owning
#: command anywhere in the codebase — nothing assigns ``cancelled``; it is only
#: ever read as a guard. Until that owner exists, the generic update may not
#: perform it. Same reasoning as the deactivation guard in ``delete``.
ALLOWED_SALES_ORDER_TRANSITIONS: dict[str, frozenset[str]] = {
    SalesOrderStatus.draft.value: frozenset(
        {
            SalesOrderStatus.confirmed.value,
            SalesOrderStatus.paid.value,
            SalesOrderStatus.cancelled.value,
        }
    ),
    SalesOrderStatus.confirmed.value: frozenset(
        {
            SalesOrderStatus.paid.value,
            # A waived order rests at confirmed and is fulfilled from there.
            SalesOrderStatus.fulfilled.value,
            SalesOrderStatus.cancelled.value,
        }
    ),
    SalesOrderStatus.paid.value: frozenset({SalesOrderStatus.fulfilled.value}),
    SalesOrderStatus.fulfilled.value: frozenset(),
    SalesOrderStatus.cancelled.value: frozenset(),
}


def assert_legal_sales_order_transition(
    from_status: str | None, to_status: str | None
) -> None:
    """Reject an illegal SalesOrder lifecycle transition."""
    if not to_status or not from_status or from_status == to_status:
        return
    if to_status not in ALLOWED_SALES_ORDER_TRANSITIONS.get(from_status, frozenset()):
        detail = f"Illegal sales order transition {from_status} → {to_status}"
        if to_status == SalesOrderStatus.cancelled.value:
            detail += (
                ". Cancelling an order that has taken money or delivered work "
                "strands a refund obligation and has no owning command; it "
                "cannot be done by setting a status"
            )
        raise HTTPException(status_code=409, detail=detail)


#: Nothing further is owed on the order: either it was paid in full, or the
#: charge was explicitly waived. Both authorize delivery; they differ only in
#: whether money changed hands, which is what ``payment_status`` records.
_FUNDING_SETTLED = frozenset({_PAID, _WAIVED})


def funding_is_settled(sales_order: SalesOrder) -> bool:
    """Whether the sale is settled enough to create the service contract."""
    return sales_order.payment_status in _FUNDING_SETTLED


def _enum_str(value, enum_cls, label: str) -> str | None:
    member = validate_enum(value, enum_cls, label)
    return member.value if member is not None else None


def _ensure_subscriber(db: Session, subscriber_id) -> Subscriber:
    subscriber = get_by_id(db, Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    return subscriber


def _validate_quote_subscriber_context(
    db: Session,
    *,
    quote: Quote,
    subscriber: Subscriber,
) -> None:
    if quote.subscriber_id != subscriber.id:
        raise HTTPException(
            status_code=409,
            detail="Sales order Subscriber does not match the Quote Subscriber",
        )
    if quote.lead is None:
        return
    try:
        lead_lifecycle.validate_lead_subscriber_alignment(
            db,
            lead=quote.lead,
            subscriber=subscriber,
        )
    except lead_lifecycle.LeadLifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    # next_value under the same key.
    value = _next_sequence_value(db, "sales_order_number", 1)
    return f"SO-{value:06d}"


# ---------------------------------------------------------------------------
# — sales-order financial side-effects, rewired native.
# CRM source: app/services/events/handlers/selfcare_customer.py (the
# push_sales_order_* + ensure_installation_invoice_for_sales_order pushers).
# ---------------------------------------------------------------------------


def _resolve_project_for_sales_order(db: Session, sales_order_id: object):
    """The active project a sales order spawned.

    The structural foreign key is authoritative.  Metadata lookup remains a
    bounded compatibility fallback for rows predating migration 389.
    """
    if not sales_order_id:
        return None
    existing = (
        db.query(Project)
        .filter(Project.sales_order_id == coerce_uuid(str(sales_order_id)))
        .filter(Project.is_active.is_(True))
        .one_or_none()
    )
    if existing:
        return existing
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
    #: the ids point at sub's own invoice rows.
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
    amount = _installation_amount_from_sales_order(
        db, project.sales_order_id or metadata.get("sales_order_id")
    )
    if amount > 0:
        return amount
    return _installation_amount_from_quote(
        db, project.quote_id or metadata.get("quote_id")
    )


def ensure_installation_invoice_for_sales_order(db: Session, sales_order_id) -> None:
    """Create the installation invoice for a sales order's project.

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

    if sales_order.payment_status == _WAIVED:
        # A waived order still gets its accounting document, at zero, so every
        # order has one. It carries no debt: collections selects on
        # ``balance_due > 0`` (see ``invoice_collectibility``), so a zero
        # invoice is never chased regardless of the status it rests in.
        amount = Decimal("0.00")
        description = "Installation cost (waived)"
    else:
        amount = _resolve_installation_amount(db, project)
        description = "Installation cost"
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
            description=description,
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


def _line_billing_cycle(line: object) -> str | None:
    """The contracted billing cadence captured on a sales-order line at quote
    time (metadata.billing_cycle). SOT: the contract line is the fact-of-record
    for cadence; absent => the subscription inherits the offer price cadence."""
    meta = getattr(line, "metadata_", None)
    if not isinstance(meta, dict):
        return None
    for key in ("billing_cycle", "sub_billing_cycle", "billing_period"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return None


def _line_add_on_ref(line: object) -> str | None:
    meta = getattr(line, "metadata_", None)
    if not isinstance(meta, dict):
        return None
    for key in ("sub_add_on_id", "add_on_id", "addon_id"):
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


def _resolve_activation_task_for_project(
    db: Session, project: Project | None
) -> ProjectTask | None:
    """Resolve the single named activation gate from the native project graph."""
    if project is None:
        return None
    from app.services import projects

    return projects.resolve_activation_gate_task(db, project.id)


def _ensure_provisioning_order_for_sales_line(
    db: Session,
    *,
    sales_order: SalesOrder,
    line: SalesOrderLine,
    subscription: Subscription,
) -> None:
    """Stage one idempotent provisioning order for a closed sale line."""
    # Widened, not narrowed: the order lifecycle statuses that already meant
    # "closed" still qualify, and a waived order now qualifies too.
    closed = sales_order.status in {
        SalesOrderStatus.paid.value,
        SalesOrderStatus.fulfilled.value,
    }
    if not closed and not funding_is_settled(sales_order):
        return

    from app.models.provisioning import (
        ServiceOrder,
        ServiceOrderStatus,
        ServiceOrderType,
    )
    from app.schemas.provisioning import ServiceOrderCreate
    from app.services.provisioning_managers import service_orders

    subscription_id = getattr(subscription, "id", None)
    persisted_subscription = (
        db.get(Subscription, subscription_id) if subscription_id else None
    )
    if persisted_subscription is None:
        return
    from app.services import sales_fulfillment

    scope = sales_fulfillment.ensure_implementation_scope(
        db,
        sales_order_id=sales_order.id,
        actor_id="sales.orders",
        commit=False,
    )
    activation_task = _resolve_activation_task_for_project(db, scope.project)
    idempotency_key = f"sales-order-line:{line.id}:new_install"
    existing = (
        db.query(ServiceOrder)
        .filter(
            (ServiceOrder.sales_order_line_id == line.id)
            | (ServiceOrder.idempotency_key == idempotency_key)
        )
        .first()
    )
    if existing is not None:
        if existing.project_id is None:
            existing.project_id = scope.project.id
        if existing.installation_project_id is None:
            existing.installation_project_id = scope.installation_project.id
        if existing.activation_project_task_id is None and activation_task is not None:
            existing.activation_project_task_id = activation_task.id
        db.flush()
        return
    execution_context = _build_staged_device_intent(
        db,
        sales_order=sales_order,
        line=line,
        subscription=persisted_subscription,
    )
    order = service_orders.create(
        db,
        ServiceOrderCreate(
            subscriber_id=sales_order.subscriber_id,
            subscription_id=persisted_subscription.id,
            sales_order_id=sales_order.id,
            sales_order_line_id=line.id,
            project_id=scope.project.id,
            installation_project_id=scope.installation_project.id,
            idempotency_key=idempotency_key,
            activation_project_task_id=(
                activation_task.id if activation_task is not None else None
            ),
            # Sales-linked work cannot enter provisioning until implementation
            # verification releases it through service_order_lifecycle.
            status=ServiceOrderStatus.draft,
            order_type=ServiceOrderType.new_install,
            notes=f"Provisioning for {sales_order.order_number or sales_order.id}",
            execution_context=execution_context,
        ),
        actor_id="sales.orders",
        commit=False,
    )
    metadata = dict(line.metadata_ or {})
    metadata["service_order_id"] = str(order.id)
    line.metadata_ = metadata
    db.add(line)
    db.flush()


def _build_staged_device_intent(
    db: Session,
    *,
    sales_order: SalesOrder,
    line: SalesOrderLine,
    subscription: Subscription,
) -> dict[str, object]:
    """Stage separate ONT and BNG intent without touching either control plane."""
    from app.models.catalog import AccessCredential, ConnectionType, SubscriptionAddOn
    from app.services.connection_type_provisioning import resolve_connection_type
    from app.services.ipv6_pd import pd_enabled, resolve_pd_pool
    from app.services.pppoe_credentials import auto_generate_pppoe_credential

    subscription_id = subscription.id
    subscriber_id = subscription.subscriber_id
    credential = (
        db.query(AccessCredential)
        .filter(
            AccessCredential.subscription_id == subscription_id,
            AccessCredential.is_active.is_(True),
        )
        .first()
    )
    if credential is None:
        credential = auto_generate_pppoe_credential(
            db,
            str(subscriber_id),
            radius_profile_id=(
                str(subscription.radius_profile_id)
                if getattr(subscription, "radius_profile_id", None)
                else None
            ),
            subscription_id=str(subscription_id),
        )
    if credential is not None and not str(getattr(subscription, "login", "") or ""):
        subscription.login = credential.username

    nas = getattr(subscription, "provisioning_nas_device", None)
    connection_type = resolve_connection_type(db, subscription, nas)
    wan_mode = {
        ConnectionType.pppoe: "pppoe",
        ConnectionType.dhcp: "dhcp",
        ConnectionType.ipoe: "dhcp",
        ConnectionType.static: "static_ip",
        ConnectionType.hotspot: "dhcp",
    }[connection_type]
    pd_pool = resolve_pd_pool(db, subscription) if pd_enabled() else None
    ip_protocol = "dual_stack" if pd_pool is not None else "ipv4"
    desired_config: dict[str, object] = {
        "wan.mode": wan_mode,
        "wan.ip_protocol": ip_protocol,
    }
    if connection_type == ConnectionType.pppoe and credential is not None:
        desired_config.update(
            {
                "wan.pppoe_username": credential.username,
                "wan.pppoe_password": credential.secret_hash,
            }
        )

    add_ons = []
    for link in (
        db.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == subscription_id)
        .all()
    ):
        add_on_type = getattr(getattr(link, "add_on", None), "addon_type", None)
        add_ons.append(
            {
                "subscription_add_on_id": str(link.id),
                "add_on_id": str(link.add_on_id),
                "quantity": int(link.quantity or 1),
                "type": add_on_type.value if add_on_type is not None else None,
            }
        )
    return {
        "source": "closed_sales_order",
        "sales_order_id": str(sales_order.id),
        "sales_order_line_id": str(line.id),
        "subscription_id": str(subscription_id),
        "subscriber_id": str(subscriber_id),
        "service_address_id": str(getattr(subscription, "service_address_id", "") or "")
        or None,
        "catalog_offer_id": str(subscription.offer_id),
        "device_intent": {
            "version": 1,
            "connection_type": connection_type.value,
            "desired_config": desired_config,
            "add_ons": add_ons,
        },
        # Subscriber addresses are BNG/RADIUS policy. The ONT intent only
        # enables the access method and, for dual stack, the DHCPv6-PD client.
        # It must never carry Framed-IP, delegated prefixes, or routed add-ons.
        "bng_intent": {
            "version": 1,
            "subscription_id": str(subscription_id),
            "connection_type": connection_type.value,
            "radius_username": credential.username if credential is not None else None,
            "ipv4": {
                "source": "ipam",
                "assignment_scope": "subscription",
                "nat_policy": "pool_defined",
            },
            "ipv6": {
                "source": "ipam",
                "assignment_scope": "subscription",
                "pd_enabled": pd_pool is not None,
                "pd_pool_id": str(pd_pool.id) if pd_pool is not None else None,
            },
            "additional_routes": {
                "source": "subscription_add_ons",
                "radius_attribute": "Framed-Route",
                "nat_policy": "no_nat",
            },
        },
    }


def _sync_sales_order_add_ons(
    db: Session,
    *,
    lines: list[SalesOrderLine],
    subscriptions: list[Subscription],
    commit: bool = True,
) -> None:
    """Attach explicitly sold add-ons to an unambiguous subscription."""
    from app.models.catalog import AddOn, SubscriptionAddOn
    from app.schemas.catalog import SubscriptionAddOnCreate
    from app.services.catalog import subscription_add_ons
    from app.services.web_catalog_subscriptions import (
        _route_range_options_for_ipam,
        normalize_additional_routes,
        sync_additional_routes_for_subscription,
    )

    persisted = {
        str(subscription.id): subscription
        for candidate in subscriptions
        if (subscription := db.get(Subscription, getattr(candidate, "id", None)))
        is not None
    }
    for line in lines:
        add_on_ref = _line_add_on_ref(line)
        if not add_on_ref:
            continue
        meta = line.metadata_ if isinstance(line.metadata_, dict) else {}
        target_id = str(meta.get("subscription_id") or "").strip()
        if not target_id and len(persisted) == 1:
            target_id = next(iter(persisted))
        subscription = persisted.get(target_id)
        try:
            add_on = db.get(AddOn, coerce_uuid(add_on_ref))
        except (TypeError, ValueError):
            add_on = None
        if subscription is None or add_on is None or not add_on.is_active:
            logger.warning(
                "sales_order_addon_unresolved line_id=%s addon=%s subscription=%s",
                line.id,
                add_on_ref,
                target_id or "ambiguous",
            )
            continue
        quantity = max(1, int(meta.get("quantity") or line.quantity or 1))
        existing = (
            db.query(SubscriptionAddOn)
            .filter(
                SubscriptionAddOn.subscription_id == subscription.id,
                SubscriptionAddOn.add_on_id == add_on.id,
                SubscriptionAddOn.end_at.is_(None),
            )
            .first()
        )
        if existing is None:
            existing = subscription_add_ons.create(
                db,
                SubscriptionAddOnCreate(
                    subscription_id=subscription.id,
                    add_on_id=add_on.id,
                    quantity=quantity,
                    start_at=datetime.now(UTC),
                ),
            )
        else:
            existing.quantity = quantity

        if add_on.ip_is_public and add_on.ip_prefix_length:
            raw_cidrs = meta.get("additional_route_cidrs") or meta.get("cidrs") or []
            if isinstance(raw_cidrs, str):
                raw_cidrs = [raw_cidrs]
            cidrs = [str(value) for value in raw_cidrs if str(value).strip()]
            if not cidrs:
                options = _route_range_options_for_ipam(db)
                for option in options:
                    groups = option.get("children_by_prefix")
                    if not isinstance(groups, dict):
                        continue
                    children = groups.get(str(add_on.ip_prefix_length), [])
                    if not isinstance(children, list):
                        continue
                    for child in children:
                        cidrs.append(str(child["cidr"]))
                        if len(cidrs) >= quantity:
                            break
                    if len(cidrs) >= quantity:
                        break
            if len(cidrs) < quantity:
                raise ValueError(
                    f"No available /{add_on.ip_prefix_length} routed block for add-on"
                )
            normalized = normalize_additional_routes(cidrs[:quantity])
            sync_additional_routes_for_subscription(
                db,
                subscription_obj=subscription,
                cidrs=[item[0] for item in normalized],
                add_on_id=str(add_on.id),
                quantity=quantity,
            )

        next_meta = dict(line.metadata_ or {})
        next_meta["subscription_id"] = str(subscription.id)
        next_meta["subscription_add_on_id"] = str(existing.id)
        line.metadata_ = next_meta
        db.add(line)
    if commit:
        db.commit()
    else:
        db.flush()


@dataclass(frozen=True)
class SalesOrderServiceSyncResult:
    """What one funding-consequence pass actually achieved.

    ``unresolved_offer_lines`` is the honest part: a line whose offer cannot be
    resolved is skipped, not provisioned, and the reconciler reports it instead
    of claiming a clean repair.
    """

    subscriptions_created: int = 0
    unresolved_offer_lines: int = 0


def _push_sales_order_subscriptions(db: Session, sales_order: SalesOrder) -> None:
    """Best-effort wrapper: a billing hiccup must never break the sale.

    Swallowing the error here is deliberate, but it is only safe because the
    drift it can leave behind — a fully funded order with no Subscription and
    no ServiceOrder — is now detected and repaired by
    ``sales_lifecycle_reconciliation``. Do not widen this except-clause without
    a matching reconciler check.
    """
    try:
        push_sales_order_subscriptions(db, sales_order, commit=True)
    except Exception:
        logger.warning(
            "sales_order_subscription_sync_failed sales_order_id=%s",
            getattr(sales_order, "id", None),
            exc_info=True,
        )
        db.rollback()


def push_sales_order_subscriptions(
    db: Session, sales_order: SalesOrder, *, commit: bool = True
) -> SalesOrderServiceSyncResult:
    """Create a subscription (plus its first invoice) for each sales-order
    line tagged with a sub offer.

    Native rewire of ``push_sales_order_subscription_to_selfcare``:
    ``selfcare.create_subscription`` (HTTP → ``POST /crm/subscriptions``)
    becomes an in-process :func:`app.services.crm_api.create_subscription`
    call keyed on the unchanged
    ``external_ref="sales_order:{id}:subscription:{line_id}"``. The resolved
    ids are stored on the line metadata so repeated calls are safe.

    Errors propagate. ``_push_sales_order_subscriptions`` is the best-effort
    caller on the live sale path; the reconciler calls this directly so a
    repair that cannot complete is reported rather than logged and dropped.
    """
    from app.services import crm_api

    sales_order_id = sales_order.id
    if not sales_order_id or not sales_order.subscriber_id:
        return SalesOrderServiceSyncResult()

    lines = _active_sales_order_lines(db, sales_order_id)
    offer_lines = [(line, ref) for line in lines if (ref := _line_offer_ref(line))]
    if not offer_lines:
        return SalesOrderServiceSyncResult()

    unresolved = 0
    staged_subscriptions: list[tuple[SalesOrderLine, Subscription]] = []
    for line, offer_ref in offer_lines:
        meta = line.metadata_ if isinstance(line.metadata_, dict) else {}
        existing_subscription_id = str(
            meta.get("selfcare_subscription_id") or ""
        ).strip()
        subscription = None
        invoice = None
        if existing_subscription_id:
            from app.models.catalog import Subscription

            subscription = db.get(Subscription, coerce_uuid(existing_subscription_id))
        else:
            try:
                result = crm_api.create_subscription(
                    db,
                    subscriber_id=str(sales_order.subscriber_id),
                    offer_ref=offer_ref,
                    external_ref=f"sales_order:{sales_order_id}:subscription:{line.id}",
                    unit_price=line.unit_price,
                    service_address_id=meta.get("service_address_id"),
                    billing_cycle=_line_billing_cycle(line),
                )
            except LookupError:
                # The offer no longer resolves. Skipping keeps the rest of the
                # order moving, but the line stays unprovisioned, so it is
                # counted and surfaced rather than silently dropped.
                unresolved += 1
                logger.warning(
                    "sales_order_subscription_offer_unresolved "
                    "sales_order_id=%s line_id=%s offer_ref=%s",
                    sales_order_id,
                    line.id,
                    offer_ref,
                )
                continue
            subscription = result.get("subscription") if result else None
            invoice = result.get("invoice") if result else None
        if subscription is None:
            unresolved += 1
            continue
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
        staged_subscriptions.append((line, subscription))
    if commit:
        db.commit()
    else:
        db.flush()
    _sync_sales_order_add_ons(
        db,
        lines=lines,
        subscriptions=[item[1] for item in staged_subscriptions],
        commit=commit,
    )
    for line, subscription in staged_subscriptions:
        _ensure_provisioning_order_for_sales_line(
            db,
            sales_order=sales_order,
            line=line,
            subscription=subscription,
        )
    if commit:
        db.commit()
    else:
        db.flush()
    return SalesOrderServiceSyncResult(
        subscriptions_created=len(staged_subscriptions),
        unresolved_offer_lines=unresolved,
    )


#: How the recorded ``amount_paid`` was arrived at. ``observed`` means a caller
#: supplied the figure from an actual receipt; ``inferred_from_total`` means
#: staff flipped the order to paid and the amount was back-filled from the
#: order total. Both post to the ledger today, but only the first is evidence.
AMOUNT_OBSERVED = "observed"
AMOUNT_INFERRED = "inferred_from_total"


def _payment_provenance(sales_order: SalesOrder) -> dict[str, str]:
    metadata = sales_order.metadata_ if isinstance(sales_order.metadata_, dict) else {}
    return {
        "amount_source": str(metadata.get("payment_amount_source") or AMOUNT_OBSERVED),
        "confirmed_by": str(metadata.get("payment_confirmed_by") or ""),
    }


def _stamp_payment_provenance(
    sales_order: SalesOrder, *, amount_source: str, actor_id: str | None
) -> None:
    """Record who settled the order and whether the amount was evidenced.

    A staff-confirmed settlement creates ledger money, so it must be
    attributable. Without this the ledger row carries only the order number and
    no actor, and an inferred amount is indistinguishable from a real receipt.
    """
    metadata = dict(sales_order.metadata_ or {})
    metadata["payment_amount_source"] = amount_source
    metadata["payment_confirmed_by"] = str(actor_id or "").strip() or "unattributed"
    metadata["payment_confirmed_at"] = datetime.now(UTC).isoformat()
    sales_order.metadata_ = metadata


def unprovisioned_service_lines(
    db: Session, sales_order: SalesOrder
) -> list[SalesOrderLine]:
    """Offer-tagged lines on a fully funded order that never got a Subscription.

    This is the drift ``_push_sales_order_subscriptions`` can leave behind when
    its best-effort guard swallows a failure, or when an offer failed to
    resolve. Gate 4 of the sales-to-service contract says full funding creates
    one pending Subscription per service line, so a non-empty result here is a
    contract violation the reconciler must repair.
    """
    if not funding_is_settled(sales_order) or not sales_order.is_active:
        return []
    if sales_order.status == SalesOrderStatus.cancelled.value:
        return []
    missing: list[SalesOrderLine] = []
    for line in _active_sales_order_lines(db, sales_order.id):
        if not _line_offer_ref(line):
            continue
        meta = line.metadata_ if isinstance(line.metadata_, dict) else {}
        if str(meta.get("selfcare_subscription_id") or "").strip():
            continue
        missing.append(line)
    return missing


def _record_sales_order_payment(db: Session, sales_order: SalesOrder) -> None:
    """Record the customer's payment against their account.

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

        provenance = _payment_provenance(sales_order)
        memo = f"Sales order {sales_order.order_number or sales_order_id}"
        if provenance["amount_source"] == AMOUNT_INFERRED:
            # Make an unevidenced settlement legible in the ledger rather than
            # letting it read like a received payment.
            memo += (
                f" — settled by {provenance['confirmed_by'] or 'unattributed'}"
                " (amount inferred from order total, no receipt reference)"
            )
        elif provenance["confirmed_by"]:
            memo += f" — recorded by {provenance['confirmed_by']}"
        crm_api.record_external_payment(
            db,
            subscriber_id=str(sales_order.subscriber_id),
            amount=amount_paid,
            external_ref=f"sales_order:{sales_order_id}:payment",
            paid_at=sales_order.paid_at,
            memo=memo,
            currency=sales_order.currency or "NGN",
        )
    except Exception:
        logger.warning(
            "sales_order_payment_record_failed sales_order_id=%s",
            getattr(sales_order, "id", None),
            exc_info=True,
        )
        db.rollback()


def _sync_sales_order_financials(
    db: Session, sales_order: SalesOrder, *, record_ledger_payment: bool = True
) -> None:
    """Apply the paid-order financial side-effects natively.

    Replaces the CRM's ``_sync_sales_order_payment_to_sub`` HTTP fan-out.
    Only fires on a paid/partial order; every step is idempotent, so
    repeated calls are safe.

    ``record_ledger_payment=False`` is for money that is already in the ledger
    by another owner's hand — the self-serve deposit is posted by
    ``payments.verify_and_record_payment`` before the sales order ever hears
    about it, so re-posting here would double-count it. The service
    consequences of full funding still run.
    """
    if sales_order.payment_status not in {_PAID, _PARTIAL, _WAIVED}:
        return
    # A partial receipt is financial evidence only.  It must not create a
    # service contract or provisioning order before the sale is settled.
    if funding_is_settled(sales_order):
        _push_sales_order_subscriptions(db, sales_order)
    if record_ledger_payment:
        # A waiver moves no money, so there is nothing to post; the helper's
        # own amount_paid <= 0 guard makes that a no-op, but saying so here
        # keeps the intent legible.
        _record_sales_order_payment(db, sales_order)


def _emit_sales_order_paid(
    db: Session,
    sales_order: SalesOrder,
    previous_payment_status: str | None,
    *,
    actor_id: str | None = None,
) -> None:
    if sales_order.payment_status != _PAID or previous_payment_status == _PAID:
        return
    provenance = _payment_provenance(sales_order)
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
                # Downstream consumers must be able to tell a receipted
                # settlement from a staff-asserted one.
                "amount_source": provenance["amount_source"],
                "confirmed_by": provenance["confirmed_by"] or None,
            },
            actor=str(actor_id or "").strip() or provenance["confirmed_by"] or None,
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
        # A waiver is an explicit financial decision, so only an explicit
        # payment_status may revoke it. Without this guard every totals
        # recalculation (a line add/edit routes through
        # ``_recalculate_order_totals``) reads a waived order as
        # amount_paid == 0 and silently downgrades it to pending, and the
        # waived -> confirmed promotion below can never fire again because
        # the status it tests for has just been overwritten.
        derive_status = (
            "payment_status" in data or sales_order.payment_status != _WAIVED
        )
        if not derive_status:
            pass
        elif total > 0 and balance_due <= 0:
            sales_order.payment_status = _PAID
            if not sales_order.paid_at:
                sales_order.paid_at = datetime.now(UTC)
        elif amount_paid > 0:
            sales_order.payment_status = _PARTIAL
        else:
            # A zero-total order stays pending on purpose: a freshly created
            # order has total == 0 and amount_paid == 0, and promoting that to
            # paid would fire the funding consequences against an order with no
            # lines. Genuinely free work is modelled as ``waived``.
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
    """Ensure one structural implementation scope for the SalesOrder."""
    if (
        not sales_order.is_active
        or sales_order.status == SalesOrderStatus.cancelled.value
    ):
        return
    from app.services import sales_fulfillment

    sales_fulfillment.ensure_implementation_scope(
        db,
        sales_order_id=sales_order.id,
        actor_id="sales.orders",
        commit=False,
    )


def _accrue_reseller_commission(db: Session, sales_order: SalesOrder | None) -> None:
    """Stub until the native referral capability owns reseller commissions.

    The call sites remain so the owner can implement the side effect without
    changing sales-order orchestration; a commission hiccup
    must never break sales-order processing either way.
    """
    logger.debug(
        "reseller_commission_accrual_deferred sales_order_id=%s",
        getattr(sales_order, "id", None),
    )
    return None


def record_deposit_receipt(
    db: Session,
    *,
    sales_order_id: UUID | str,
    amount: Decimal | str,
    reference: str,
    actor_id: str,
    provider: str | None = None,
    ledger_already_recorded: bool = False,
) -> SalesOrder:
    """Record one verified deposit receipt against its SalesOrder.

    Owns its transaction: the receipt, the derived financial state and the
    ``sales_order.paid`` outbox row commit together, and the funding
    consequences run after. There is deliberately no ``commit=False`` mode —
    the consequences commit internally, so a caller-managed transaction could
    not span them anyway.

    ``sales.orders`` owns SalesOrder financial status. Callers that verified
    the money elsewhere hand the receipt here rather than writing
    ``payment_status``/``amount_paid`` themselves, so the funding consequences
    the sales-to-service contract promises — one pending Subscription and one
    idempotent ServiceOrder per service line once the sale is fully funded —
    actually fire.

    ``ledger_already_recorded=True`` says the money is already in the billing
    ledger by another owner's hand: the self-serve portal settles the deposit
    invoice through ``payments.verify_and_record_payment`` before the sales
    order hears about it, so re-posting here would double-count it.

    Idempotent on ``reference``: an exact replay is a no-op, and the same
    reference arriving with a different amount is a conflict rather than a
    silent rewrite of the order's financial state.
    """
    actor = str(actor_id or "").strip()
    if not actor:
        raise SalesOrderLifecycleError(
            "actor_required", "Deposit actor is required", kind="invalid"
        )
    normalized_reference = str(reference or "").strip()
    if not normalized_reference:
        raise SalesOrderLifecycleError(
            "reference_required", "Deposit reference is required", kind="invalid"
        )
    try:
        receipt_amount = round_money(Decimal(str(amount)))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SalesOrderLifecycleError(
            "invalid_amount", "Deposit amount must be a number", kind="invalid"
        ) from exc
    if receipt_amount <= 0:
        raise SalesOrderLifecycleError(
            "invalid_amount",
            "Deposit amount must be greater than zero",
            kind="invalid",
        )

    order = db.scalars(
        select(SalesOrder)
        .where(SalesOrder.id == coerce_uuid(str(sales_order_id)))
        .with_for_update()
    ).one_or_none()
    if order is None or not order.is_active:
        raise SalesOrderLifecycleError(
            "sales_order_not_found", "Sales order not found", kind="not_found"
        )
    if order.status == SalesOrderStatus.cancelled.value:
        raise SalesOrderLifecycleError(
            "sales_order_canceled", "Cancelled order cannot accept a deposit"
        )

    metadata = dict(order.metadata_ or {})
    receipts = dict(metadata.get("deposit_receipts") or {})
    already = receipts.get(normalized_reference)
    if isinstance(already, dict):
        recorded = round_money(Decimal(str(already.get("amount") or 0)))
        if recorded != receipt_amount:
            raise SalesOrderLifecycleError(
                "deposit_receipt_conflict",
                f"Deposit reference '{normalized_reference}' was already recorded "
                f"as {recorded}; refusing to rewrite it as {receipt_amount}",
            )
        return order

    previous_payment_status = order.payment_status
    receipts[normalized_reference] = {
        "amount": str(receipt_amount),
        "provider": provider,
        "recorded_at": datetime.now(UTC).isoformat(),
        "recorded_by": actor,
    }
    metadata["deposit_receipts"] = receipts
    order.metadata_ = metadata
    order.deposit_required = True
    order.deposit_paid = True
    # Receipts accumulate. Assigning amount_paid would let a second deposit
    # erase the first.
    accumulated = round_money(Decimal(str(order.amount_paid or 0)) + receipt_amount)
    _stamp_payment_provenance(order, amount_source=AMOUNT_OBSERVED, actor_id=actor)
    _apply_payment_fields(order, {"total": order.total, "amount_paid": accumulated})
    # Emit inside the same transaction as the state change it describes, so the
    # outbox row cannot survive a rolled-back settlement.
    _emit_sales_order_paid(
        db, order, previous_payment_status=previous_payment_status, actor_id=actor
    )
    db.flush()
    db.commit()
    db.refresh(order)
    _sync_sales_order_financials(
        db, order, record_ledger_payment=not ledger_already_recorded
    )
    return order


def record_waiver(
    db: Session,
    *,
    sales_order_id: UUID | str,
    actor_id: str,
    reason: str,
) -> SalesOrder:
    """Waive the charge on a sales order and authorize delivery.

    A waiver is a decision to give the work away, so it is evidenced the same
    way every other consequential transition in this chain is — actor, reason,
    timestamp — rather than being a bare status string anyone can set.

    Distinct from a discount: a discount reduces what is owed and the customer
    still pays the remainder through the normal funding path. A waiver says
    nothing is owed at all, and is what authorizes provisioning in place of a
    payment.

    Refuses an order that already carries money. Reversing a real receipt is a
    refund, which belongs to billing, not to a status flip here.
    """
    actor = str(actor_id or "").strip()
    if not actor:
        raise SalesOrderLifecycleError(
            "actor_required", "Waiver actor is required", kind="invalid"
        )
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise SalesOrderLifecycleError(
            "reason_required", "Waiver reason is required", kind="invalid"
        )

    order = db.scalars(
        select(SalesOrder)
        .where(SalesOrder.id == coerce_uuid(str(sales_order_id)))
        .with_for_update()
    ).one_or_none()
    if order is None or not order.is_active:
        raise SalesOrderLifecycleError(
            "sales_order_not_found", "Sales order not found", kind="not_found"
        )
    if order.status == SalesOrderStatus.cancelled.value:
        raise SalesOrderLifecycleError(
            "sales_order_canceled", "Cancelled order cannot be waived"
        )

    metadata = dict(order.metadata_ or {})
    existing = metadata.get("waiver")
    if order.payment_status == _WAIVED and isinstance(existing, dict):
        return order
    if Decimal(str(order.amount_paid or 0)) > 0:
        raise SalesOrderLifecycleError(
            "sales_order_already_funded",
            "Sales order has recorded payments; refund it through billing "
            "rather than waiving it",
        )

    metadata["waiver"] = {
        "reason": normalized_reason,
        "waived_by": actor,
        "waived_at": datetime.now(UTC).isoformat(),
        "waived_total": str(order.total or 0),
    }
    order.metadata_ = metadata
    order.payment_status = _WAIVED
    order.balance_due = Decimal("0.00")
    if order.status == SalesOrderStatus.draft.value:
        order.status = SalesOrderStatus.confirmed.value
    emit_event(
        db,
        EventType.sales_order_waived,
        {
            "sales_order_id": str(order.id),
            "order_number": order.order_number,
            "waived_total": str(order.total or 0),
            "currency": order.currency,
            "reason": normalized_reason,
        },
        actor=actor,
        subscriber_id=order.subscriber_id,
    )
    db.flush()
    db.commit()
    db.refresh(order)
    # Settled funding authorizes the service contract, exactly as a full
    # payment does. No ledger payment is posted — no money moved.
    _sync_sales_order_financials(db, order, record_ledger_payment=False)
    ensure_installation_invoice_for_sales_order(db, order.id)
    return order


def _ensure_project_for_manual_sales_order(db: Session, sales_order: SalesOrder):
    """Compatibility wrapper around the canonical fulfillment coordinator."""
    if sales_order.quote_id:
        return sales_order.project
    _ensure_fulfillment(db, sales_order)
    return sales_order.project


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

        subscriber = _ensure_subscriber(db, data.get("subscriber_id"))
        if data.get("quote_id"):
            quote = db.get(
                Quote, coerce_uuid(data["quote_id"]), options=[selectinload(Quote.lead)]
            )
            if not quote:
                raise HTTPException(status_code=404, detail="Quote not found")
            _validate_quote_subscriber_context(db, quote=quote, subscriber=subscriber)
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
    def create_from_quote(
        db: Session, quote_id: str, *, commit: bool = True
    ) -> SalesOrder:
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

        subscriber = _ensure_subscriber(db, quote.subscriber_id)
        _validate_quote_subscriber_context(db, quote=quote, subscriber=subscriber)

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
            discount_percent = item.discount_percent or Decimal("0.00")
            amount = item.amount
            if amount is None:
                amount = net_line_amount(
                    item.quantity, item.unit_price, discount_percent
                )
            line = SalesOrderLine(
                sales_order_id=sales_order.id,
                inventory_item_id=item.inventory_item_id,
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                # Carry the discount, not just the net amount it produced —
                # otherwise the order cannot explain its own price and the
                # next line edit recomputes it back to gross.
                discount_percent=discount_percent,
                amount=amount,
                metadata_=dict(item.metadata_) if item.metadata_ else None,
            )
            db.add(line)

        if commit:
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
    def update(
        db: Session, sales_order_id: str, payload, *, actor_id: str | None = None
    ):
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
        prospective_subscriber_id = (
            data["subscriber_id"]
            if "subscriber_id" in data
            else sales_order.subscriber_id
        )
        if prospective_subscriber_id is None:
            raise HTTPException(status_code=400, detail="subscriber_id is required")
        subscriber = _ensure_subscriber(db, prospective_subscriber_id)
        prospective_quote_id = (
            data["quote_id"] if "quote_id" in data else sales_order.quote_id
        )
        quote = None
        if prospective_quote_id is not None:
            quote = db.get(
                Quote,
                coerce_uuid(prospective_quote_id),
                options=[selectinload(Quote.lead)],
            )
            if not quote:
                raise HTTPException(status_code=404, detail="Quote not found")
            _validate_quote_subscriber_context(db, quote=quote, subscriber=subscriber)
        if "quote_id" in data and quote is not None:
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

        # The declared machine governs every externally requested status
        # change. Derived promotions below (a full payment moving the order to
        # paid) are legal edges of the same table.
        if "status" in data:
            assert_legal_sales_order_transition(sales_order.status, data["status"])
        if data.get("payment_status") == _WAIVED and (
            sales_order.payment_status != _WAIVED
        ):
            # Waiving is a decision to give away revenue, so it goes through
            # the command that demands an actor and a reason. Allowing it here
            # too would leave the evidence trivially bypassable.
            raise HTTPException(
                status_code=409,
                detail=(
                    "Waive the order through the waiver command so the actor "
                    "and reason are recorded"
                ),
            )
        amount_source = AMOUNT_OBSERVED if "amount_paid" in data else None
        if data.get("payment_status") == _PAID:
            resolved_total = Decimal(data.get("total") or sales_order.total or 0)
            resolved_amount_paid = Decimal(
                data.get("amount_paid") or sales_order.amount_paid or 0
            )
            if resolved_amount_paid < resolved_total:
                # Staff asserted the order is settled without supplying a
                # figure, so the amount is back-filled from the total. This
                # still posts to the ledger, so record that the money was
                # asserted rather than received — see AMOUNT_INFERRED.
                data["amount_paid"] = round_money(resolved_total)
                amount_source = AMOUNT_INFERRED
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

        if amount_source is not None:
            _stamp_payment_provenance(
                sales_order, amount_source=amount_source, actor_id=actor_id
            )
        _apply_payment_fields(sales_order, data)
        _ensure_fulfillment(db, sales_order)
        db.commit()
        db.refresh(sales_order)
        # Accrue on any transition into paid (idempotent). Covers
        # update_from_input too.
        _accrue_reseller_commission(db, sales_order)
        _sync_sales_order_financials(db, sales_order)
        _emit_sales_order_paid(
            db,
            sales_order,
            previous_payment_status=previous_payment_status,
            actor_id=actor_id,
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
        actor_id: str | None = None,
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
        return SalesOrders.update(db, sales_order_id, payload, actor_id=actor_id)

    @staticmethod
    def delete(db: Session, sales_order_id: str):
        """Deactivate a sales order that has not yet taken money or work.

        Deactivation is not a cancellation: the reconciler only scans
        ``is_active`` orders, so flipping the flag on a funded order hides it
        from drift repair while its Project, Subscription and ServiceOrder keep
        running. Anything past that point has to be cancelled through the
        lifecycle instead.
        """
        sales_order = db.get(SalesOrder, coerce_uuid(sales_order_id))
        if not sales_order:
            raise HTTPException(status_code=404, detail="Sales order not found")
        if not sales_order.is_active:
            return
        blockers: list[str] = []
        if sales_order.status in {
            SalesOrderStatus.paid.value,
            SalesOrderStatus.fulfilled.value,
        }:
            blockers.append(f"status is {sales_order.status}")
        if sales_order.payment_status in {_PAID, _PARTIAL}:
            blockers.append(f"payment_status is {sales_order.payment_status}")
        if sales_order.project is not None:
            blockers.append("it has an implementation project")
        from app.models.provisioning import ServiceOrder

        service_order_count = (
            db.query(func.count(ServiceOrder.id))
            .filter(ServiceOrder.sales_order_id == sales_order.id)
            .scalar()
            or 0
        )
        if service_order_count:
            blockers.append(f"it has {service_order_count} provisioning order(s)")
        if blockers:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Sales order cannot be deactivated because "
                    + "; ".join(blockers)
                    + ". Cancel it through the sales-order lifecycle instead."
                ),
            )
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
            data["amount"] = net_line_amount(
                data.get("quantity"),
                data.get("unit_price"),
                data.get("discount_percent"),
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
        # Recompute from every input that shapes the amount. Omitting the
        # discount here is what used to restore a negotiated line to full price
        # the first time anyone touched its quantity.
        if {"quantity", "unit_price", "discount_percent"} & set(data):
            line.amount = net_line_amount(
                line.quantity, line.unit_price, line.discount_percent
            )
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
