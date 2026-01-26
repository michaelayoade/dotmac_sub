from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.billing import CreditNoteStatus, Invoice
from app.models.catalog import SlaProfile, Subscription
from app.models.sla_credit import SlaCreditItem, SlaCreditReport, SlaCreditReportStatus
from app.models.tickets import Ticket, TicketSlaEvent
from app.schemas.billing import (
    CreditNoteApplyRequest,
    CreditNoteCreate,
    CreditNoteLineCreate,
)
from app.schemas.sla_credit import (
    SlaCreditApplyRequest,
    SlaCreditApplyResult,
    SlaCreditItemUpdate,
    SlaCreditReportCreate,
    SlaCreditReportUpdate,
)
from app.services import billing as billing_service
from app.services.common import coerce_uuid
from app.services.response import ListResponseMixin


def _round_percent(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _resolve_invoice_for_period(
    db: Session, account_id, period_start, period_end
) -> Invoice | None:
    invoice = (
        db.query(Invoice)
        .filter(Invoice.account_id == account_id)
        .filter(Invoice.billing_period_start >= period_start)
        .filter(Invoice.billing_period_end <= period_end)
        .order_by(Invoice.billing_period_start.desc(), Invoice.created_at.desc())
        .first()
    )
    if invoice:
        return invoice
    return (
        db.query(Invoice)
        .filter(Invoice.account_id == account_id)
        .filter(Invoice.created_at >= period_start)
        .filter(Invoice.created_at <= period_end)
        .order_by(Invoice.created_at.desc())
        .first()
    )


def _resolve_sla_profile(db: Session, subscription_id) -> SlaProfile | None:
    if not subscription_id:
        return None
    subscription = db.get(
        Subscription,
        subscription_id,
        options=[
            selectinload(Subscription.offer),
            selectinload(Subscription.offer_version),
        ],
    )
    if not subscription:
        return None
    if subscription.offer_version and subscription.offer_version.sla_profile:
        return subscription.offer_version.sla_profile
    if subscription.offer and subscription.offer.sla_profile:
        return subscription.offer.sla_profile
    return None


def _sla_event_stats(
    db: Session, period_start, period_end, account_id=None
) -> dict[tuple, dict[str, int]]:
    query = (
        db.query(TicketSlaEvent, Ticket)
        .join(Ticket, TicketSlaEvent.ticket_id == Ticket.id)
        .filter(Ticket.account_id.is_not(None))
        .filter(TicketSlaEvent.expected_at.is_not(None))
        .filter(TicketSlaEvent.expected_at >= period_start)
        .filter(TicketSlaEvent.expected_at <= period_end)
    )
    if account_id:
        query = query.filter(Ticket.account_id == account_id)
    stats: dict[tuple, dict[str, int]] = {}
    for event, ticket in query.all():
        key = (ticket.account_id, ticket.subscription_id)
        bucket = stats.setdefault(key, {"total": 0, "met": 0})
        bucket["total"] += 1
        if event.actual_at and event.expected_at and event.actual_at <= event.expected_at:
            bucket["met"] += 1
    return stats


class SlaCreditReports(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SlaCreditReportCreate) -> SlaCreditReport:
        if payload.period_end <= payload.period_start:
            raise HTTPException(status_code=400, detail="period_end must be after period_start")
        report = SlaCreditReport(
            period_start=payload.period_start,
            period_end=payload.period_end,
            status=payload.status,
            notes=payload.notes,
        )
        db.add(report)
        db.flush()
        stats = _sla_event_stats(
            db, payload.period_start, payload.period_end, payload.account_id
        )
        if payload.account_id and not stats:
            stats = {(payload.account_id, None): {"total": 0, "met": 0}}
        for (account_id, subscription_id), counts in stats.items():
            total = counts.get("total", 0)
            met = counts.get("met", 0)
            if total <= 0:
                actual_percent = Decimal("100.00")
            else:
                actual_percent = _round_percent(Decimal(met) * Decimal("100.00") / Decimal(total))
            sla_profile = _resolve_sla_profile(db, subscription_id)
            target_percent = (
                Decimal(str(sla_profile.uptime_percent))
                if sla_profile and sla_profile.uptime_percent is not None
                else Decimal("100.00")
            )
            credit_percent = (
                Decimal(str(sla_profile.credit_percent))
                if sla_profile and sla_profile.credit_percent is not None
                else Decimal("0.00")
            )
            target_percent = _round_percent(target_percent)
            credit_percent = _round_percent(credit_percent)
            invoice = _resolve_invoice_for_period(
                db, account_id, payload.period_start, payload.period_end
            )
            currency = invoice.currency if invoice else "NGN"
            credit_amount = Decimal("0.00")
            if invoice and credit_percent > 0 and actual_percent < target_percent:
                delta_ratio = (target_percent - actual_percent) / target_percent
                base = Decimal(str(invoice.total))
                credit_amount = _round_money(
                    base * (credit_percent / Decimal("100.00")) * delta_ratio
                )
            item = SlaCreditItem(
                report_id=report.id,
                account_id=account_id,
                subscription_id=subscription_id,
                invoice_id=invoice.id if invoice else None,
                sla_profile_id=sla_profile.id if sla_profile else None,
                target_percent=target_percent,
                actual_percent=actual_percent,
                credit_percent=credit_percent,
                credit_amount=credit_amount,
                currency=currency,
                approved=False,
            )
            db.add(item)
        db.commit()
        return SlaCreditReports.get(db, str(report.id))

    @staticmethod
    def get(db: Session, report_id: str) -> SlaCreditReport:
        report = db.get(
            SlaCreditReport,
            coerce_uuid(report_id),
            options=[selectinload(SlaCreditReport.items)],
        )
        if not report:
            raise HTTPException(status_code=404, detail="SLA credit report not found")
        return report

    @staticmethod
    def list(
        db: Session,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SlaCreditReport)
        if status:
            try:
                status_value = SlaCreditReportStatus(status)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid status") from exc
            query = query.filter(SlaCreditReport.status == status_value)
        if order_by not in {"created_at", "period_start", "period_end", "status"}:
            raise HTTPException(status_code=400, detail="Invalid order_by")
        column = getattr(SlaCreditReport, order_by)
        query = query.order_by(column.desc() if order_dir == "desc" else column.asc())
        return query.limit(limit).offset(offset).all()

    @staticmethod
    def update(db: Session, report_id: str, payload: SlaCreditReportUpdate):
        report = db.get(SlaCreditReport, coerce_uuid(report_id))
        if not report:
            raise HTTPException(status_code=404, detail="SLA credit report not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(report, key, value)
        db.commit()
        db.refresh(report)
        return report

    @staticmethod
    def apply(db: Session, report_id: str, payload: SlaCreditApplyRequest) -> SlaCreditApplyResult:
        report = db.get(
            SlaCreditReport,
            coerce_uuid(report_id),
            options=[selectinload(SlaCreditReport.items)],
        )
        if not report:
            raise HTTPException(status_code=404, detail="SLA credit report not found")
        if report.status == SlaCreditReportStatus.canceled:
            raise HTTPException(status_code=400, detail="Report is canceled")
        items = report.items
        if payload.item_ids:
            item_set = {coerce_uuid(item_id) for item_id in payload.item_ids}
            items = [item for item in items if item.id in item_set]
        elif payload.apply_all:
            items = [item for item in items if item.approved]
        else:
            items = []
        credit_notes_created = 0
        items_applied = 0
        for item in items:
            if item.credit_amount <= 0:
                continue
            note = billing_service.credit_notes.create(
                db,
                CreditNoteCreate(
                    account_id=item.account_id,
                    invoice_id=item.invoice_id,
                    status=CreditNoteStatus.issued,
                    currency=item.currency,
                    memo=item.memo or "SLA credit",
                ),
            )
            billing_service.credit_note_lines.create(
                db,
                CreditNoteLineCreate(
                    credit_note_id=note.id,
                    description="SLA credit",
                    quantity=Decimal("1"),
                    unit_price=item.credit_amount,
                ),
            )
            if payload.apply_to_invoices and item.invoice_id:
                billing_service.credit_notes.apply(
                    db,
                    str(note.id),
                    CreditNoteApplyRequest(
                        invoice_id=item.invoice_id, amount=item.credit_amount
                    ),
                )
            credit_notes_created += 1
            items_applied += 1
        if items_applied:
            report.status = SlaCreditReportStatus.applied
            db.commit()
        return SlaCreditApplyResult(
            report_id=report.id,
            credit_notes_created=credit_notes_created,
            items_applied=items_applied,
        )


class SlaCreditItems(ListResponseMixin):
    @staticmethod
    def get(db: Session, item_id: str) -> SlaCreditItem:
        item = db.get(SlaCreditItem, coerce_uuid(item_id))
        if not item:
            raise HTTPException(status_code=404, detail="SLA credit item not found")
        return item

    @staticmethod
    def list(
        db: Session,
        report_id: str | None,
        account_id: str | None,
        approved: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SlaCreditItem)
        if report_id:
            query = query.filter(SlaCreditItem.report_id == coerce_uuid(report_id))
        if account_id:
            query = query.filter(SlaCreditItem.account_id == coerce_uuid(account_id))
        if approved is not None:
            query = query.filter(SlaCreditItem.approved == approved)
        if order_by not in {"created_at", "credit_amount", "actual_percent", "target_percent"}:
            raise HTTPException(status_code=400, detail="Invalid order_by")
        column = getattr(SlaCreditItem, order_by)
        query = query.order_by(column.desc() if order_dir == "desc" else column.asc())
        return query.limit(limit).offset(offset).all()

    @staticmethod
    def update(db: Session, item_id: str, payload: SlaCreditItemUpdate):
        item = db.get(SlaCreditItem, coerce_uuid(item_id))
        if not item:
            raise HTTPException(status_code=404, detail="SLA credit item not found")
        data = payload.model_dump(exclude_unset=True)
        if "credit_amount" in data and data["credit_amount"] is not None:
            data["credit_amount"] = _round_money(Decimal(str(data["credit_amount"])))
        for key, value in data.items():
            setattr(item, key, value)
        db.commit()
        db.refresh(item)
        return item


sla_credit_reports = SlaCreditReports()
sla_credit_items = SlaCreditItems()
