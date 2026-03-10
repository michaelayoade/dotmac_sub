"""Billing reporting services.

Provides statistics, summaries, and reports for billing data.
All aggregations are performed at the database level via SQL to avoid
loading large result sets into Python memory.
"""
from __future__ import annotations

import calendar
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import String, case, cast, func, select
from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentChannel,
    PaymentMethod,
    PaymentStatus,
)
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus

logger = logging.getLogger(__name__)


def _month_start(value: datetime) -> datetime:
    """Return the first instant of the month containing *value*."""
    return datetime(value.year, value.month, 1, tzinfo=UTC)


def _next_month_start(value: datetime) -> datetime:
    """Return the first instant of the month after *value*."""
    if value.month == 12:
        return datetime(value.year + 1, 1, 1, tzinfo=UTC)
    return datetime(value.year, value.month + 1, 1, tzinfo=UTC)


def _month_window(month_anchor: datetime) -> tuple[datetime, datetime]:
    start = _month_start(month_anchor)
    return start, _next_month_start(start)


def _last_6_months(now: datetime) -> list[tuple[str, int, int, datetime, datetime]]:
    """Return (label, year, month, start, end) for the last 6 months."""
    months: list[tuple[str, int, int, datetime, datetime]] = []
    for i in range(5, -1, -1):
        month = now.month - i
        year = now.year
        while month <= 0:
            month += 12
            year -= 1
        start = datetime(year, month, 1, tzinfo=UTC)
        end = _next_month_start(start)
        months.append((calendar.month_abbr[month], year, month, start, end))
    return months


class BillingReporting:
    """Service for billing reports and statistics."""

    @staticmethod
    def get_overview_stats(db: Session) -> dict[str, Any]:
        """Calculate billing overview statistics using SQL aggregation.

        Returns:
            Dictionary with keys:
            - total_revenue: Sum of paid invoice totals
            - pending_amount: Sum of pending/sent invoice totals
            - overdue_amount: Sum of overdue invoice totals
            - total_invoices: Total invoice count
            - paid_count: Number of paid invoices
            - pending_count: Number of pending/sent invoices
            - overdue_count: Number of overdue invoices
            - draft_count: Number of draft invoices
        """
        stmt = select(
            func.coalesce(
                func.sum(case((Invoice.status == InvoiceStatus.paid, Invoice.total), else_=Decimal("0"))),
                Decimal("0"),
            ).label("total_revenue"),
            func.coalesce(
                func.sum(case((Invoice.status == InvoiceStatus.issued, Invoice.total), else_=Decimal("0"))),
                Decimal("0"),
            ).label("pending_amount"),
            func.coalesce(
                func.sum(case((Invoice.status == InvoiceStatus.overdue, Invoice.total), else_=Decimal("0"))),
                Decimal("0"),
            ).label("overdue_amount"),
            func.count().label("total_invoices"),
            func.count(case((Invoice.status == InvoiceStatus.paid, 1))).label("paid_count"),
            func.count(case((Invoice.status == InvoiceStatus.issued, 1))).label("pending_count"),
            func.count(case((Invoice.status == InvoiceStatus.overdue, 1))).label("overdue_count"),
            func.count(case((Invoice.status == InvoiceStatus.draft, 1))).label("draft_count"),
        )
        row = db.execute(stmt).one()

        return {
            "total_revenue": float(row.total_revenue),
            "pending_amount": float(row.pending_amount),
            "overdue_amount": float(row.overdue_amount),
            "total_invoices": row.total_invoices,
            "paid_count": row.paid_count,
            "pending_count": row.pending_count,
            "overdue_count": row.overdue_count,
            "draft_count": row.draft_count,
        }

    @staticmethod
    def get_account_stats(db: Session) -> dict[str, Any]:
        """Calculate account-level statistics using SQL aggregation.

        Returns:
            Dictionary with keys:
            - total_balance: Sum of all account min_balance values
            - active_count: Number of active accounts
            - suspended_count: Number of suspended accounts
        """
        stmt = select(
            func.coalesce(func.sum(Subscriber.min_balance), Decimal("0")).label("total_balance"),
            func.count(case((Subscriber.status == SubscriberStatus.active, 1))).label("active_count"),
            func.count(case((Subscriber.status == SubscriberStatus.suspended, 1))).label("suspended_count"),
        )
        row = db.execute(stmt).one()

        return {
            "total_balance": float(row.total_balance),
            "active_count": row.active_count,
            "suspended_count": row.suspended_count,
        }

    @staticmethod
    def get_ar_aging_buckets(db: Session) -> dict[str, Any]:
        """Classify unpaid invoices into aging buckets.

        Returns:
            Dictionary with keys:
            - buckets: Dict with keys 'current', '1_30', '31_60', '61_90', '90_plus'
                      Each containing a list of invoices
            - totals: Dict with same keys containing sum of balance_due for each bucket
        """
        today = datetime.now(UTC).date()

        # Only fetch unpaid invoices — paid/void are excluded
        unpaid_statuses = [
            InvoiceStatus.draft,
            InvoiceStatus.issued,
            InvoiceStatus.overdue,
            InvoiceStatus.partially_paid,
        ]
        stmt = (
            select(Invoice)
            .where(Invoice.status.in_(unpaid_statuses))
            .order_by(Invoice.due_at.asc())
        )
        invoices = db.scalars(stmt).all()

        buckets: dict[str, list[Any]] = {
            "current": [],
            "1_30": [],
            "31_60": [],
            "61_90": [],
            "90_plus": [],
        }

        for invoice in invoices:
            due_at = invoice.due_at.date() if invoice.due_at else None
            if not due_at or due_at >= today:
                buckets["current"].append(invoice)
                continue

            days = (today - due_at).days
            if days <= 30:
                buckets["1_30"].append(invoice)
            elif days <= 60:
                buckets["31_60"].append(invoice)
            elif days <= 90:
                buckets["61_90"].append(invoice)
            else:
                buckets["90_plus"].append(invoice)

        totals = {
            key: sum(float(getattr(inv, "balance_due", 0) or 0) for inv in items)
            for key, items in buckets.items()
        }

        return {"buckets": buckets, "totals": totals}

    @staticmethod
    def get_dashboard_stats(
        db: Session,
        *,
        partner_id: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Build complete billing dashboard statistics.

        All heavy aggregations are performed at the SQL level.

        Returns:
            Dictionary with keys: stats, invoices, revenue_trend,
            chart_data, total_balance, active_count, suspended_count.
        """
        selected_partner_id = (partner_id or "").strip() or None
        selected_location = (location or "").strip() or None

        overview = BillingReporting.get_overview_stats(db)
        account_stats = BillingReporting.get_account_stats(db)

        # Collection rate
        total_billed = (
            overview["total_revenue"]
            + overview["pending_amount"]
            + overview["overdue_amount"]
        )
        collection_rate = (
            round(overview["total_revenue"] / total_billed * 100, 1)
            if total_billed > 0
            else 0.0
        )

        # --- Scoping helpers for SQL WHERE clauses ---
        def _scope_invoice_stmt(stmt: Any) -> Any:
            """Apply partner/location scope to an invoice query via JOIN."""
            if selected_partner_id or selected_location:
                stmt = stmt.join(Subscriber, Invoice.account_id == Subscriber.id)
                if selected_partner_id:
                    stmt = stmt.where(cast(Subscriber.reseller_id, String) == selected_partner_id)
                if selected_location:
                    loc = selected_location.lower()
                    stmt = stmt.where(
                        func.lower(func.coalesce(Subscriber.region, Subscriber.billing_region, Subscriber.city, "")) == loc
                    )
            return stmt

        def _scope_payment_stmt(stmt: Any) -> Any:
            """Apply partner/location scope to a payment query via JOIN."""
            if selected_partner_id or selected_location:
                stmt = stmt.join(Subscriber, Payment.account_id == Subscriber.id)
                if selected_partner_id:
                    stmt = stmt.where(cast(Subscriber.reseller_id, String) == selected_partner_id)
                if selected_location:
                    loc = selected_location.lower()
                    stmt = stmt.where(
                        func.lower(func.coalesce(Subscriber.region, Subscriber.billing_region, Subscriber.city, "")) == loc
                    )
            return stmt

        def _scope_credit_note_stmt(stmt: Any) -> Any:
            """Apply partner/location scope to a credit note query via JOIN."""
            if selected_partner_id or selected_location:
                stmt = stmt.join(Subscriber, CreditNote.account_id == Subscriber.id)
                if selected_partner_id:
                    stmt = stmt.where(cast(Subscriber.reseller_id, String) == selected_partner_id)
                if selected_location:
                    loc = selected_location.lower()
                    stmt = stmt.where(
                        func.lower(func.coalesce(Subscriber.region, Subscriber.billing_region, Subscriber.city, "")) == loc
                    )
            return stmt

        # --- Payments aggregate ---
        payments_stmt = select(
            func.count().label("count"),
            func.coalesce(func.sum(Payment.amount), Decimal("0")).label("total"),
        ).where(Payment.status == PaymentStatus.succeeded)
        payments_stmt = _scope_payment_stmt(payments_stmt)
        payments_row = db.execute(payments_stmt).one()

        # --- Unpaid invoices aggregate ---
        unpaid_statuses = [
            InvoiceStatus.issued,
            InvoiceStatus.overdue,
            InvoiceStatus.partially_paid,
        ]
        unpaid_stmt = select(
            func.count().label("count"),
            func.coalesce(func.sum(Invoice.balance_due), Decimal("0")).label("total"),
        ).where(Invoice.status.in_(unpaid_statuses))
        unpaid_stmt = _scope_invoice_stmt(unpaid_stmt)
        unpaid_row = db.execute(unpaid_stmt).one()

        # --- Credit notes aggregate ---
        cn_stmt = select(
            func.count().label("count"),
            func.coalesce(func.sum(CreditNote.total), Decimal("0")).label("total"),
        ).where(
            CreditNote.is_active.is_(True),
            CreditNote.status != CreditNoteStatus.void,
        )
        cn_stmt = _scope_credit_note_stmt(cn_stmt)
        cn_row = db.execute(cn_stmt).one()

        stats: dict[str, Any] = {
            **overview,
            "collection_rate": collection_rate,
            "payments_count": payments_row.count,
            "payments_amount": float(payments_row.total),
            "unpaid_invoices_count": unpaid_row.count,
            "unpaid_invoices_amount": float(unpaid_row.total),
            "credit_notes_count": cn_row.count,
            "credit_notes_total": float(cn_row.total),
        }

        # --- Revenue trend (last 6 months, SQL GROUP BY) ---
        now = datetime.now(UTC)
        six_months = _last_6_months(now)
        six_months_start = six_months[0][3]  # earliest start

        inv_trend_stmt = select(
            func.extract("year", Invoice.created_at).label("yr"),
            func.extract("month", Invoice.created_at).label("mo"),
            func.coalesce(func.sum(Invoice.total), Decimal("0")).label("billed"),
            func.coalesce(
                func.sum(case((Invoice.status == InvoiceStatus.paid, Invoice.total), else_=Decimal("0"))),
                Decimal("0"),
            ).label("collected"),
        ).where(Invoice.created_at >= six_months_start).group_by("yr", "mo")
        inv_trend_stmt = _scope_invoice_stmt(inv_trend_stmt)
        trend_rows = {(int(r.yr), int(r.mo)): r for r in db.execute(inv_trend_stmt).all()}

        labels: list[str] = []
        billed: list[float] = []
        collected: list[float] = []
        for label, year, month, _start, _end in six_months:
            labels.append(label)
            row = trend_rows.get((year, month))
            billed.append(float(row.billed) if row else 0.0)
            collected.append(float(row.collected) if row else 0.0)

        revenue_trend = {"labels": labels, "billed": billed, "collected": collected}

        # --- Period comparison (last / current / next month) ---
        current_month_start = _month_start(now)
        last_month_start = _month_start(current_month_start - timedelta(days=1))
        next_month_start_dt = _next_month_start(current_month_start)
        comparison_periods = [
            ("Last Month", *_month_window(last_month_start)),
            ("Current Month", *_month_window(current_month_start)),
            ("Next Month", *_month_window(next_month_start_dt)),
        ]

        period_comparison: list[dict[str, Any]] = []
        for period_label, period_start, period_end in comparison_periods:
            # Payments in period
            pp_stmt = select(
                func.count().label("count"),
                func.coalesce(func.sum(Payment.amount), Decimal("0")).label("total"),
            ).where(
                Payment.status == PaymentStatus.succeeded,
                func.coalesce(Payment.paid_at, Payment.created_at) >= period_start,
                func.coalesce(Payment.paid_at, Payment.created_at) < period_end,
            )
            pp_stmt = _scope_payment_stmt(pp_stmt)
            pp_row = db.execute(pp_stmt).one()

            # Paid invoices in period
            pi_stmt = select(func.count()).where(
                Invoice.status == InvoiceStatus.paid,
                func.coalesce(Invoice.paid_at, Invoice.created_at) >= period_start,
                func.coalesce(Invoice.paid_at, Invoice.created_at) < period_end,
            )
            pi_stmt = _scope_invoice_stmt(pi_stmt)
            paid_inv_count = db.execute(pi_stmt).scalar() or 0

            # Unpaid invoices created in period
            ui_stmt = select(func.count()).where(
                Invoice.status.in_(unpaid_statuses),
                Invoice.created_at >= period_start,
                Invoice.created_at < period_end,
            )
            ui_stmt = _scope_invoice_stmt(ui_stmt)
            unpaid_inv_count = db.execute(ui_stmt).scalar() or 0

            # Credit notes in period
            pcn_stmt = select(
                func.count().label("count"),
                func.coalesce(func.sum(CreditNote.total), Decimal("0")).label("total"),
            ).where(
                CreditNote.is_active.is_(True),
                CreditNote.status != CreditNoteStatus.void,
                CreditNote.created_at >= period_start,
                CreditNote.created_at < period_end,
            )
            pcn_stmt = _scope_credit_note_stmt(pcn_stmt)
            pcn_row = db.execute(pcn_stmt).one()

            period_comparison.append({
                "label": period_label,
                "payments_amount": float(pp_row.total),
                "payments_count": pp_row.count,
                "paid_invoices_count": paid_inv_count,
                "unpaid_invoices_count": unpaid_inv_count,
                "credit_notes_count": pcn_row.count,
                "credit_notes_amount": float(pcn_row.total),
                "total_income": float(pp_row.total),
            })

        # --- Payment method breakdown (SQL JOIN + GROUP BY) ---
        METHOD_LABELS = {
            "cash": "Cash",
            "card": "Card",
            "transfer": "Bank Transfer",
            "bank_account": "Bank Account",
            "check": "Check",
            "other": "Other",
            "bank_transfer": "Bank Transfer",
        }

        # Prefer PaymentMethod.method_type, fall back to PaymentChannel.channel_type
        pm_stmt = (
            select(
                func.coalesce(
                    cast(PaymentMethod.method_type, String),
                    cast(PaymentChannel.channel_type, String),
                    "other",
                ).label("method_key"),
                func.sum(Payment.amount).label("total"),
            )
            .outerjoin(PaymentMethod, Payment.payment_method_id == PaymentMethod.id)
            .outerjoin(PaymentChannel, Payment.payment_channel_id == PaymentChannel.id)
            .where(Payment.status == PaymentStatus.succeeded)
            .group_by("method_key")
        )
        if selected_partner_id or selected_location:
            pm_stmt = pm_stmt.join(Subscriber, Payment.account_id == Subscriber.id)
            if selected_partner_id:
                pm_stmt = pm_stmt.where(cast(Subscriber.reseller_id, String) == selected_partner_id)
            if selected_location:
                loc = selected_location.lower()
                pm_stmt = pm_stmt.where(
                    func.lower(func.coalesce(Subscriber.region, Subscriber.billing_region, Subscriber.city, "")) == loc
                )

        method_rows = db.execute(pm_stmt).all()
        method_totals: dict[str, float] = {}
        for mrow in method_rows:
            display_label = METHOD_LABELS.get(mrow.method_key, "Other")
            method_totals[display_label] = method_totals.get(display_label, 0.0) + float(mrow.total or 0)

        payment_method_breakdown = {
            "labels": list(method_totals.keys()),
            "values": list(method_totals.values()),
        }

        # --- Daily payments (current month, SQL GROUP BY day) ---
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
        month_end = _next_month_start(month_start)

        dp_stmt = select(
            func.extract("day", func.coalesce(Payment.paid_at, Payment.created_at)).label("day"),
            func.sum(Payment.amount).label("total"),
        ).where(
            Payment.status == PaymentStatus.succeeded,
            func.coalesce(Payment.paid_at, Payment.created_at) >= month_start,
            func.coalesce(Payment.paid_at, Payment.created_at) < month_end,
        ).group_by("day")
        dp_stmt = _scope_payment_stmt(dp_stmt)
        dp_rows = {int(r.day): float(r.total) for r in db.execute(dp_stmt).all()}

        daily_payments = {
            "labels": [str(day) for day in range(1, days_in_month + 1)],
            "values": [dp_rows.get(day, 0.0) for day in range(1, days_in_month + 1)],
        }

        # --- Invoicing period overlay (last 6 months, SQL) ---
        inv_overlay_stmt = select(
            func.extract("year", Invoice.created_at).label("yr"),
            func.extract("month", Invoice.created_at).label("mo"),
            func.coalesce(
                func.sum(case((Invoice.status == InvoiceStatus.paid, Invoice.total), else_=Decimal("0"))),
                Decimal("0"),
            ).label("paid"),
            func.coalesce(
                func.sum(case(
                    (Invoice.status.in_([InvoiceStatus.issued, InvoiceStatus.overdue, InvoiceStatus.partially_paid, InvoiceStatus.draft]), Invoice.total),
                    else_=Decimal("0"),
                )),
                Decimal("0"),
            ).label("unpaid"),
            # paid_on_time: paid AND (paid_at <= due_at OR due_at IS NULL)
            func.coalesce(
                func.sum(case(
                    (
                        (Invoice.status == InvoiceStatus.paid) & ((Invoice.paid_at <= Invoice.due_at) | (Invoice.due_at.is_(None))),
                        Invoice.total,
                    ),
                    else_=Decimal("0"),
                )),
                Decimal("0"),
            ).label("paid_on_time"),
            # paid_overdue: paid AND paid_at > due_at
            func.coalesce(
                func.sum(case(
                    (
                        (Invoice.status == InvoiceStatus.paid) & (Invoice.paid_at > Invoice.due_at),
                        Invoice.total,
                    ),
                    else_=Decimal("0"),
                )),
                Decimal("0"),
            ).label("paid_overdue"),
        ).where(Invoice.created_at >= six_months_start).group_by("yr", "mo")
        inv_overlay_stmt = _scope_invoice_stmt(inv_overlay_stmt)
        overlay_rows = {(int(r.yr), int(r.mo)): r for r in db.execute(inv_overlay_stmt).all()}

        invoicing_labels: list[str] = []
        invoicing_paid: list[float] = []
        invoicing_unpaid: list[float] = []
        invoicing_paid_on_time: list[float] = []
        invoicing_paid_overdue: list[float] = []
        for label, year, month, _start, _end in six_months:
            invoicing_labels.append(label)
            orow = overlay_rows.get((year, month))
            invoicing_paid.append(float(orow.paid) if orow else 0.0)
            invoicing_unpaid.append(float(orow.unpaid) if orow else 0.0)
            invoicing_paid_on_time.append(float(orow.paid_on_time) if orow else 0.0)
            invoicing_paid_overdue.append(float(orow.paid_overdue) if orow else 0.0)

        invoicing_period_overlay = {
            "labels": invoicing_labels,
            "paid": invoicing_paid,
            "unpaid": invoicing_unpaid,
            "paid_on_time": invoicing_paid_on_time,
            "paid_overdue": invoicing_paid_overdue,
        }

        # --- MRR / ARPU (SQL per-month aggregation on Subscription) ---
        mrr_labels: list[str] = []
        mrr_values: list[float] = []
        arpu_values: list[float] = []
        active_subscriber_counts: list[int] = []

        for label, year, month, m_start, m_end in six_months:
            mrr_labels.append(label)

            mrr_stmt = select(
                func.coalesce(func.sum(Subscription.unit_price), Decimal("0")).label("mrr"),
                func.count(func.distinct(Subscription.subscriber_id)).label("active_count"),
            ).where(
                Subscription.status == SubscriptionStatus.active,
                Subscription.next_billing_at >= m_start,
                Subscription.next_billing_at < m_end,
            )
            if selected_partner_id or selected_location:
                mrr_stmt = mrr_stmt.join(Subscriber, Subscription.subscriber_id == Subscriber.id)
                if selected_partner_id:
                    mrr_stmt = mrr_stmt.where(cast(Subscriber.reseller_id, String) == selected_partner_id)
                if selected_location:
                    loc = selected_location.lower()
                    mrr_stmt = mrr_stmt.where(
                        func.lower(func.coalesce(Subscriber.region, Subscriber.billing_region, Subscriber.city, "")) == loc
                    )

            mrr_row = db.execute(mrr_stmt).one()
            month_mrr = float(mrr_row.mrr)
            count = mrr_row.active_count or 1
            active_subscriber_counts.append(mrr_row.active_count)
            mrr_values.append(month_mrr)
            arpu_values.append(round(month_mrr / count, 2))

        mrr_growth_rate = 0.0
        if len(mrr_values) >= 2 and mrr_values[-2] > 0:
            mrr_growth_rate = round(((mrr_values[-1] - mrr_values[-2]) / mrr_values[-2]) * 100, 2)

        # --- Planned income (next month from active subscriptions) ---
        next_m_start = _next_month_start(_month_start(now))
        next_m_end = _next_month_start(next_m_start)
        planned_stmt = select(
            func.coalesce(func.sum(Subscription.unit_price), Decimal("0")),
        ).where(
            Subscription.status == SubscriptionStatus.active,
            Subscription.next_billing_at >= next_m_start,
            Subscription.next_billing_at < next_m_end,
        )
        if selected_partner_id or selected_location:
            planned_stmt = planned_stmt.join(Subscriber, Subscription.subscriber_id == Subscriber.id)
            if selected_partner_id:
                planned_stmt = planned_stmt.where(cast(Subscriber.reseller_id, String) == selected_partner_id)
            if selected_location:
                loc = selected_location.lower()
                planned_stmt = planned_stmt.where(
                    func.lower(func.coalesce(Subscriber.region, Subscriber.billing_region, Subscriber.city, "")) == loc
                )
        planned_income = float(db.execute(planned_stmt).scalar() or 0)

        # --- Net revenue retention (payment-based, SQL aggregation) ---
        current_start = _month_start(now)
        current_end = _next_month_start(current_start)
        prev_start = _month_start(current_start - timedelta(days=1))
        prev_end = current_start

        # Previous month totals by account
        prev_stmt = select(
            Payment.account_id,
            func.sum(Payment.amount).label("total"),
        ).where(
            Payment.status == PaymentStatus.succeeded,
            func.coalesce(Payment.paid_at, Payment.created_at) >= prev_start,
            func.coalesce(Payment.paid_at, Payment.created_at) < prev_end,
        ).group_by(Payment.account_id)
        prev_by_account = {str(r.account_id): float(r.total) for r in db.execute(prev_stmt).all()}

        # Current month totals by account (only for cohort accounts)
        cohort_ids = set(prev_by_account.keys())
        prev_total = sum(prev_by_account.values())

        if cohort_ids and prev_total > 0:
            cur_stmt = select(
                Payment.account_id,
                func.sum(Payment.amount).label("total"),
            ).where(
                Payment.status == PaymentStatus.succeeded,
                func.coalesce(Payment.paid_at, Payment.created_at) >= current_start,
                func.coalesce(Payment.paid_at, Payment.created_at) < current_end,
                cast(Payment.account_id, String).in_(cohort_ids),
            ).group_by(Payment.account_id)
            current_by_account = {str(r.account_id): float(r.total) for r in db.execute(cur_stmt).all()}
            current_total = sum(current_by_account.get(acc_id, 0.0) for acc_id in cohort_ids)
            net_revenue_retention = round((current_total / prev_total) * 100, 2)
        else:
            net_revenue_retention = 0.0

        # --- Top payers (current month, SQL JOIN + GROUP BY + ORDER BY) ---
        tp_stmt = (
            select(
                Payment.account_id,
                func.sum(Payment.amount).label("total"),
                func.max(Subscriber.display_name).label("display_name"),
                func.max(Subscriber.first_name).label("first_name"),
                func.max(Subscriber.last_name).label("last_name"),
                func.max(Subscriber.account_number).label("account_number"),
            )
            .join(Subscriber, Payment.account_id == Subscriber.id)
            .where(
                Payment.status == PaymentStatus.succeeded,
                func.coalesce(Payment.paid_at, Payment.created_at) >= current_start,
                func.coalesce(Payment.paid_at, Payment.created_at) < current_end,
            )
            .group_by(Payment.account_id)
            .order_by(func.sum(Payment.amount).desc())
            .limit(10)
        )
        tp_rows = db.execute(tp_stmt).all()
        top_payer_labels: list[str] = []
        top_payer_values: list[float] = []
        for tpr in tp_rows:
            name = (
                tpr.display_name
                or " ".join(part for part in [(tpr.first_name or "").strip(), (tpr.last_name or "").strip()] if part)
                or str(tpr.account_number or f"Account {str(tpr.account_id)[:8]}")
            )
            top_payer_labels.append(name)
            top_payer_values.append(float(tpr.total))

        top_payers = {"labels": top_payer_labels, "values": top_payer_values}

        # --- Invoice status chart data ---
        chart_data = {
            "labels": ["Paid", "Pending", "Overdue", "Draft"],
            "values": [
                overview["paid_count"],
                overview["pending_count"],
                overview["overdue_count"],
                overview["draft_count"],
            ],
            "colors": ["#10b981", "#3b82f6", "#f59e0b", "#94a3b8"],
        }

        # --- Recent invoices (last 10) ---
        recent_stmt = (
            select(Invoice)
            .order_by(Invoice.created_at.desc())
            .limit(10)
        )
        recent_invoices = db.scalars(recent_stmt).all()

        return {
            "stats": stats,
            "invoices": recent_invoices,
            "revenue_trend": revenue_trend,
            "chart_data": chart_data,
            "period_comparison": period_comparison,
            "payment_method_breakdown": payment_method_breakdown,
            "daily_payments": daily_payments,
            "invoicing_period_overlay": invoicing_period_overlay,
            "mrr_trend": {"labels": mrr_labels, "values": mrr_values},
            "arpu_trend": {"labels": mrr_labels, "values": arpu_values},
            "top_payers": top_payers,
            "mrr_growth_rate": mrr_growth_rate,
            "net_revenue_retention": net_revenue_retention,
            "planned_income": planned_income,
            "total_balance": account_stats["total_balance"],
            "active_count": account_stats["active_count"],
            "suspended_count": account_stats["suspended_count"],
        }


billing_reporting = BillingReporting()
