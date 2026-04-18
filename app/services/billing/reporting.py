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

from sqlalchemy import String, and_, case, cast, func, select
from sqlalchemy.orm import Session, joinedload

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


PERIOD_CHOICES = ("this_month", "last_month", "this_quarter", "this_year", "all")


def _period_bounds(
    period: str, now: datetime
) -> tuple[datetime | None, datetime | None]:
    """Return (start, end) datetimes for a named period.

    Returns (None, None) for ``"all"`` (no date filter).
    """
    if period == "this_month":
        start = _month_start(now)
        return start, _next_month_start(start)
    if period == "last_month":
        start = _month_start(_month_start(now) - timedelta(days=1))
        return start, _month_start(now)
    if period == "this_quarter":
        q_month = ((now.month - 1) // 3) * 3 + 1
        start = datetime(now.year, q_month, 1, tzinfo=UTC)
        end_month = q_month + 3
        end_year = now.year
        if end_month > 12:
            end_month -= 12
            end_year += 1
        return start, datetime(end_year, end_month, 1, tzinfo=UTC)
    if period == "this_year":
        return datetime(now.year, 1, 1, tzinfo=UTC), datetime(
            now.year + 1, 1, 1, tzinfo=UTC
        )
    # "all" — no bounds
    return None, None


def _daily_chart_window(
    period_start: datetime | None,
    period_end: datetime | None,
    now: datetime,
) -> tuple[datetime, datetime, str]:
    """Return chart bounds and grouping granularity for the dashboard payment chart."""
    if period_start is None or period_end is None:
        chart_end = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(
            days=1
        )
        chart_start = chart_end - timedelta(days=30)
    else:
        chart_start = period_start
        chart_end = period_end

    total_days = max((chart_end - chart_start).days, 1)
    granularity = "month" if total_days > 92 else "day"
    return chart_start, chart_end, granularity


def _subscriber_location_expr():
    return func.lower(
        func.coalesce(Subscriber.region, Subscriber.billing_region, Subscriber.city, "")
    )


class BillingReporting:
    """Service for billing reports and statistics."""

    @staticmethod
    def get_overview_stats(
        db: Session,
        *,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        partner_id: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Calculate billing overview statistics using SQL aggregation.

        When *period_start*/*period_end* are provided, invoices are filtered
        by ``created_at`` within the window.  Payments and credit notes are
        filtered similarly by their respective date columns.

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
                func.sum(
                    case(
                        (Invoice.status == InvoiceStatus.paid, Invoice.total),
                        else_=Decimal("0"),
                    )
                ),
                Decimal("0"),
            ).label("total_revenue"),
            func.coalesce(
                func.sum(
                    case(
                        (Invoice.status == InvoiceStatus.issued, Invoice.total),
                        else_=Decimal("0"),
                    )
                ),
                Decimal("0"),
            ).label("pending_amount"),
            func.coalesce(
                func.sum(
                    case(
                        (Invoice.status == InvoiceStatus.overdue, Invoice.total),
                        else_=Decimal("0"),
                    )
                ),
                Decimal("0"),
            ).label("overdue_amount"),
            func.count().label("total_invoices"),
            func.count(case((Invoice.status == InvoiceStatus.paid, 1))).label(
                "paid_count"
            ),
            func.count(case((Invoice.status == InvoiceStatus.issued, 1))).label(
                "pending_count"
            ),
            func.count(case((Invoice.status == InvoiceStatus.overdue, 1))).label(
                "overdue_count"
            ),
            func.count(case((Invoice.status == InvoiceStatus.draft, 1))).label(
                "draft_count"
            ),
        )
        if partner_id or location:
            stmt = stmt.join(Subscriber, Invoice.account_id == Subscriber.id)
            if partner_id:
                stmt = stmt.where(cast(Subscriber.reseller_id, String) == partner_id)
            if location:
                stmt = stmt.where(_subscriber_location_expr() == location.lower())
        if period_start is not None:
            stmt = stmt.where(Invoice.created_at >= period_start)
        if period_end is not None:
            stmt = stmt.where(Invoice.created_at < period_end)
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
    def get_account_stats(
        db: Session,
        *,
        partner_id: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Calculate account-level statistics using SQL aggregation.

        Returns:
            Dictionary with keys:
            - total_balance: Sum of all account min_balance values
            - active_count: Number of active accounts
            - suspended_count: Number of suspended accounts
        """
        stmt = select(
            func.coalesce(func.sum(Subscriber.min_balance), Decimal("0")).label(
                "total_balance"
            ),
            func.count(case((Subscriber.status == SubscriberStatus.active, 1))).label(
                "active_count"
            ),
            func.count(
                case((Subscriber.status == SubscriberStatus.suspended, 1))
            ).label("suspended_count"),
        )
        if partner_id:
            stmt = stmt.where(cast(Subscriber.reseller_id, String) == partner_id)
        if location:
            stmt = stmt.where(_subscriber_location_expr() == location.lower())
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
        period: str = "this_month",
    ) -> dict[str, Any]:
        """Build complete billing dashboard statistics.

        All heavy aggregations are performed at the SQL level.
        The *period* parameter controls the date window for the top-level
        KPI cards (payments, revenue, unpaid, credit notes).  Charts that
        have their own time axis (6-month trends, MRR) are unaffected.

        Returns:
            Dictionary with keys: stats, invoices, revenue_trend,
            chart_data, total_balance, active_count, suspended_count,
            selected_period.
        """
        selected_partner_id = (partner_id or "").strip() or None
        selected_location = (location or "").strip() or None
        selected_period = period if period in PERIOD_CHOICES else "this_month"

        now = datetime.now(UTC)
        period_start, period_end = _period_bounds(selected_period, now)

        overview = BillingReporting.get_overview_stats(
            db,
            period_start=period_start,
            period_end=period_end,
            partner_id=selected_partner_id,
            location=selected_location,
        )
        account_stats = BillingReporting.get_account_stats(
            db,
            partner_id=selected_partner_id,
            location=selected_location,
        )

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
                    stmt = stmt.where(
                        cast(Subscriber.reseller_id, String) == selected_partner_id
                    )
                if selected_location:
                    stmt = stmt.where(
                        _subscriber_location_expr() == selected_location.lower()
                    )
            return stmt

        def _scope_payment_stmt(stmt: Any) -> Any:
            """Apply partner/location scope to a payment query via JOIN."""
            if selected_partner_id or selected_location:
                stmt = stmt.join(Subscriber, Payment.account_id == Subscriber.id)
                if selected_partner_id:
                    stmt = stmt.where(
                        cast(Subscriber.reseller_id, String) == selected_partner_id
                    )
                if selected_location:
                    stmt = stmt.where(
                        _subscriber_location_expr() == selected_location.lower()
                    )
            return stmt

        def _scope_credit_note_stmt(stmt: Any) -> Any:
            """Apply partner/location scope to a credit note query via JOIN."""
            if selected_partner_id or selected_location:
                stmt = stmt.join(Subscriber, CreditNote.account_id == Subscriber.id)
                if selected_partner_id:
                    stmt = stmt.where(
                        cast(Subscriber.reseller_id, String) == selected_partner_id
                    )
                if selected_location:
                    stmt = stmt.where(
                        _subscriber_location_expr() == selected_location.lower()
                    )
            return stmt

        # --- Payments aggregate (period-scoped) ---
        payments_stmt = select(
            func.count().label("count"),
            func.coalesce(func.sum(Payment.amount), Decimal("0")).label("total"),
        ).where(Payment.status == PaymentStatus.succeeded)
        if period_start is not None:
            payments_stmt = payments_stmt.where(
                func.coalesce(Payment.paid_at, Payment.created_at) >= period_start
            )
        if period_end is not None:
            payments_stmt = payments_stmt.where(
                func.coalesce(Payment.paid_at, Payment.created_at) < period_end
            )
        payments_stmt = _scope_payment_stmt(payments_stmt)
        payments_row = db.execute(payments_stmt).one()

        # --- Unpaid invoices aggregate (period-scoped) ---
        unpaid_statuses = [
            InvoiceStatus.issued,
            InvoiceStatus.overdue,
            InvoiceStatus.partially_paid,
        ]
        unpaid_stmt = select(
            func.count().label("count"),
            func.coalesce(func.sum(Invoice.balance_due), Decimal("0")).label("total"),
        ).where(Invoice.status.in_(unpaid_statuses))
        if period_start is not None:
            unpaid_stmt = unpaid_stmt.where(Invoice.created_at >= period_start)
        if period_end is not None:
            unpaid_stmt = unpaid_stmt.where(Invoice.created_at < period_end)
        unpaid_stmt = _scope_invoice_stmt(unpaid_stmt)
        unpaid_row = db.execute(unpaid_stmt).one()

        # --- Credit notes aggregate (period-scoped) ---
        cn_stmt = select(
            func.count().label("count"),
            func.coalesce(func.sum(CreditNote.total), Decimal("0")).label("total"),
        ).where(
            CreditNote.is_active.is_(True),
            CreditNote.status != CreditNoteStatus.void,
        )
        if period_start is not None:
            cn_stmt = cn_stmt.where(CreditNote.created_at >= period_start)
        if period_end is not None:
            cn_stmt = cn_stmt.where(CreditNote.created_at < period_end)
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

        inv_trend_stmt = (
            select(
                func.extract("year", Invoice.created_at).label("yr"),
                func.extract("month", Invoice.created_at).label("mo"),
                func.coalesce(func.sum(Invoice.total), Decimal("0")).label("billed"),
                func.coalesce(
                    func.sum(
                        case(
                            (Invoice.status == InvoiceStatus.paid, Invoice.total),
                            else_=Decimal("0"),
                        )
                    ),
                    Decimal("0"),
                ).label("collected"),
            )
            .where(Invoice.created_at >= six_months_start)
            .group_by("yr", "mo")
        )
        inv_trend_stmt = _scope_invoice_stmt(inv_trend_stmt)
        trend_rows = {
            (int(r.yr), int(r.mo)): r for r in db.execute(inv_trend_stmt).all()
        }

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
        # Consolidated into 3 queries instead of 12 using CASE expressions
        current_month_start = _month_start(now)
        last_month_start = _month_start(current_month_start - timedelta(days=1))
        last_month_end = current_month_start
        current_month_end = _next_month_start(current_month_start)
        next_month_start_dt = current_month_end
        next_month_end = _next_month_start(next_month_start_dt)

        # Helper to create period CASE expression
        def _period_case(
            date_col, last_start, last_end, curr_start, curr_end, next_start, next_end
        ):
            return case(
                (and_(date_col >= last_start, date_col < last_end), "last"),
                (and_(date_col >= curr_start, date_col < curr_end), "current"),
                (and_(date_col >= next_start, date_col < next_end), "next"),
                else_=None,
            )

        # Combined payments query for all periods
        payment_date = func.coalesce(Payment.paid_at, Payment.created_at)
        payment_period = _period_case(
            payment_date,
            last_month_start,
            last_month_end,
            current_month_start,
            current_month_end,
            next_month_start_dt,
            next_month_end,
        )
        pp_stmt = (
            select(
                payment_period.label("period"),
                func.count().label("count"),
                func.coalesce(func.sum(Payment.amount), Decimal("0")).label("total"),
            )
            .where(
                Payment.status == PaymentStatus.succeeded,
                payment_date >= last_month_start,
                payment_date < next_month_end,
            )
            .group_by(payment_period)
        )
        pp_stmt = _scope_payment_stmt(pp_stmt)
        payment_rows = {r.period: r for r in db.execute(pp_stmt).all()}

        # Combined paid invoices query for all periods
        invoice_date = func.coalesce(Invoice.paid_at, Invoice.created_at)
        inv_period = _period_case(
            invoice_date,
            last_month_start,
            last_month_end,
            current_month_start,
            current_month_end,
            next_month_start_dt,
            next_month_end,
        )
        pi_stmt = (
            select(
                inv_period.label("period"),
                func.count().label("paid_count"),
            )
            .where(
                Invoice.status == InvoiceStatus.paid,
                invoice_date >= last_month_start,
                invoice_date < next_month_end,
            )
            .group_by(inv_period)
        )
        pi_stmt = _scope_invoice_stmt(pi_stmt)
        paid_inv_rows = {r.period: r.paid_count for r in db.execute(pi_stmt).all()}

        # Combined unpaid invoices query for all periods
        unpaid_period = _period_case(
            Invoice.created_at,
            last_month_start,
            last_month_end,
            current_month_start,
            current_month_end,
            next_month_start_dt,
            next_month_end,
        )
        ui_stmt = (
            select(
                unpaid_period.label("period"),
                func.count().label("unpaid_count"),
            )
            .where(
                Invoice.status.in_(unpaid_statuses),
                Invoice.created_at >= last_month_start,
                Invoice.created_at < next_month_end,
            )
            .group_by(unpaid_period)
        )
        ui_stmt = _scope_invoice_stmt(ui_stmt)
        unpaid_inv_rows = {r.period: r.unpaid_count for r in db.execute(ui_stmt).all()}

        # Combined credit notes query for all periods
        cn_period = _period_case(
            CreditNote.created_at,
            last_month_start,
            last_month_end,
            current_month_start,
            current_month_end,
            next_month_start_dt,
            next_month_end,
        )
        pcn_stmt = (
            select(
                cn_period.label("period"),
                func.count().label("count"),
                func.coalesce(func.sum(CreditNote.total), Decimal("0")).label("total"),
            )
            .where(
                CreditNote.is_active.is_(True),
                CreditNote.status != CreditNoteStatus.void,
                CreditNote.created_at >= last_month_start,
                CreditNote.created_at < next_month_end,
            )
            .group_by(cn_period)
        )
        pcn_stmt = _scope_credit_note_stmt(pcn_stmt)
        cn_rows = {r.period: r for r in db.execute(pcn_stmt).all()}

        # Build period comparison from consolidated results
        period_comparison: list[dict[str, Any]] = []
        for label, period_key in [
            ("Last Month", "last"),
            ("Current Month", "current"),
            ("Next Month", "next"),
        ]:
            pp = payment_rows.get(period_key)
            cn = cn_rows.get(period_key)
            payments_amount = float(pp.total) if pp else 0.0
            period_comparison.append(
                {
                    "label": label,
                    "payments_amount": payments_amount,
                    "payments_count": pp.count if pp else 0,
                    "paid_invoices_count": paid_inv_rows.get(period_key, 0),
                    "unpaid_invoices_count": unpaid_inv_rows.get(period_key, 0),
                    "credit_notes_count": cn.count if cn else 0,
                    "credit_notes_amount": float(cn.total) if cn else 0.0,
                    "total_income": payments_amount,
                }
            )

        # --- Payment method breakdown (period-scoped, SQL JOIN + GROUP BY) ---
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
        if period_start is not None:
            pm_stmt = pm_stmt.where(
                func.coalesce(Payment.paid_at, Payment.created_at) >= period_start
            )
        if period_end is not None:
            pm_stmt = pm_stmt.where(
                func.coalesce(Payment.paid_at, Payment.created_at) < period_end
            )
        if selected_partner_id or selected_location:
            pm_stmt = pm_stmt.join(Subscriber, Payment.account_id == Subscriber.id)
            if selected_partner_id:
                pm_stmt = pm_stmt.where(
                    cast(Subscriber.reseller_id, String) == selected_partner_id
                )
            if selected_location:
                pm_stmt = pm_stmt.where(
                    _subscriber_location_expr() == selected_location.lower()
                )

        method_rows = db.execute(pm_stmt).all()
        method_totals: dict[str, float] = {}
        for mrow in method_rows:
            display_label = METHOD_LABELS.get(mrow.method_key, "Other")
            method_totals[display_label] = method_totals.get(
                display_label, 0.0
            ) + float(mrow.total or 0)

        payment_method_breakdown = {
            "labels": list(method_totals.keys()),
            "values": list(method_totals.values()),
        }

        # --- Daily/monthly payments (period-scoped, adaptive aggregation) ---
        chart_start, chart_end, chart_granularity = _daily_chart_window(
            period_start, period_end, now
        )
        payment_ts = func.coalesce(Payment.paid_at, Payment.created_at)
        if chart_granularity == "month":
            dp_stmt = (
                select(
                    func.extract("year", payment_ts).label("yr"),
                    func.extract("month", payment_ts).label("mo"),
                    func.sum(Payment.amount).label("total"),
                )
                .where(
                    Payment.status == PaymentStatus.succeeded,
                    payment_ts >= chart_start,
                    payment_ts < chart_end,
                )
                .group_by("yr", "mo")
            )
            dp_stmt = _scope_payment_stmt(dp_stmt)
            dp_rows = {
                (int(r.yr), int(r.mo)): float(r.total or 0)
                for r in db.execute(dp_stmt).all()
            }

            month_labels: list[str] = []
            month_values: list[float] = []
            cursor = datetime(chart_start.year, chart_start.month, 1, tzinfo=UTC)
            end_cursor = datetime(chart_end.year, chart_end.month, 1, tzinfo=UTC)
            while cursor < end_cursor:
                month_labels.append(cursor.strftime("%b %Y"))
                month_values.append(dp_rows.get((cursor.year, cursor.month), 0.0))
                cursor = _next_month_start(cursor)
            daily_payments = {"labels": month_labels, "values": month_values}
        else:
            daily_stmt = (
                select(
                    cast(func.date(payment_ts), String).label("day_key"),
                    func.sum(Payment.amount).label("total"),
                )
                .where(
                    Payment.status == PaymentStatus.succeeded,
                    payment_ts >= chart_start,
                    payment_ts < chart_end,
                )
                .group_by("day_key")
            )
            daily_stmt = _scope_payment_stmt(daily_stmt)
            daily_rows = {
                str(r.day_key): float(r.total or 0)
                for r in db.execute(daily_stmt).all()
            }

            day_labels: list[str] = []
            day_values: list[float] = []
            cursor = chart_start
            while cursor < chart_end:
                day_key = cursor.date().isoformat()
                day_labels.append(cursor.strftime("%b %d"))
                day_values.append(daily_rows.get(day_key, 0.0))
                cursor += timedelta(days=1)
            daily_payments = {"labels": day_labels, "values": day_values}

        # --- Invoicing period overlay (last 6 months, SQL) ---
        inv_overlay_stmt = (
            select(
                func.extract("year", Invoice.created_at).label("yr"),
                func.extract("month", Invoice.created_at).label("mo"),
                func.coalesce(
                    func.sum(
                        case(
                            (Invoice.status == InvoiceStatus.paid, Invoice.total),
                            else_=Decimal("0"),
                        )
                    ),
                    Decimal("0"),
                ).label("paid"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                Invoice.status.in_(
                                    [
                                        InvoiceStatus.issued,
                                        InvoiceStatus.overdue,
                                        InvoiceStatus.partially_paid,
                                        InvoiceStatus.draft,
                                    ]
                                ),
                                Invoice.total,
                            ),
                            else_=Decimal("0"),
                        )
                    ),
                    Decimal("0"),
                ).label("unpaid"),
                # paid_on_time: paid AND (paid_at <= due_at OR due_at IS NULL)
                func.coalesce(
                    func.sum(
                        case(
                            (
                                (Invoice.status == InvoiceStatus.paid)
                                & (
                                    (Invoice.paid_at <= Invoice.due_at)
                                    | (Invoice.due_at.is_(None))
                                ),
                                Invoice.total,
                            ),
                            else_=Decimal("0"),
                        )
                    ),
                    Decimal("0"),
                ).label("paid_on_time"),
                # paid_overdue: paid AND paid_at > due_at
                func.coalesce(
                    func.sum(
                        case(
                            (
                                (Invoice.status == InvoiceStatus.paid)
                                & (Invoice.paid_at > Invoice.due_at),
                                Invoice.total,
                            ),
                            else_=Decimal("0"),
                        )
                    ),
                    Decimal("0"),
                ).label("paid_overdue"),
            )
            .where(Invoice.created_at >= six_months_start)
            .group_by("yr", "mo")
        )
        inv_overlay_stmt = _scope_invoice_stmt(inv_overlay_stmt)
        overlay_rows = {
            (int(r.yr), int(r.mo)): r for r in db.execute(inv_overlay_stmt).all()
        }

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
                func.coalesce(func.sum(Subscription.unit_price), Decimal("0")).label(
                    "mrr"
                ),
                func.count(func.distinct(Subscription.subscriber_id)).label(
                    "active_count"
                ),
            ).where(
                Subscription.status == SubscriptionStatus.active,
                Subscription.next_billing_at >= m_start,
                Subscription.next_billing_at < m_end,
            )
            if selected_partner_id or selected_location:
                mrr_stmt = mrr_stmt.join(
                    Subscriber, Subscription.subscriber_id == Subscriber.id
                )
                if selected_partner_id:
                    mrr_stmt = mrr_stmt.where(
                        cast(Subscriber.reseller_id, String) == selected_partner_id
                    )
                if selected_location:
                    mrr_stmt = mrr_stmt.where(
                        _subscriber_location_expr() == selected_location.lower()
                    )

            mrr_row = db.execute(mrr_stmt).one()
            month_mrr = float(mrr_row.mrr)
            count = mrr_row.active_count or 1
            active_subscriber_counts.append(mrr_row.active_count)
            mrr_values.append(month_mrr)
            arpu_values.append(round(month_mrr / count, 2))

        mrr_growth_rate = 0.0
        if len(mrr_values) >= 2 and mrr_values[-2] > 0:
            mrr_growth_rate = round(
                ((mrr_values[-1] - mrr_values[-2]) / mrr_values[-2]) * 100, 2
            )

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
            planned_stmt = planned_stmt.join(
                Subscriber, Subscription.subscriber_id == Subscriber.id
            )
            if selected_partner_id:
                planned_stmt = planned_stmt.where(
                    cast(Subscriber.reseller_id, String) == selected_partner_id
                )
            if selected_location:
                planned_stmt = planned_stmt.where(
                    _subscriber_location_expr() == selected_location.lower()
                )
        planned_income = float(db.execute(planned_stmt).scalar() or 0)

        # --- Net revenue retention (payment-based, SQL aggregation) ---
        current_start = _month_start(now)
        current_end = _next_month_start(current_start)
        prev_start = _month_start(current_start - timedelta(days=1))
        prev_end = current_start

        # Previous month totals by account
        prev_stmt = (
            select(
                Payment.account_id,
                func.sum(Payment.amount).label("total"),
            )
            .where(
                Payment.status == PaymentStatus.succeeded,
                func.coalesce(Payment.paid_at, Payment.created_at) >= prev_start,
                func.coalesce(Payment.paid_at, Payment.created_at) < prev_end,
            )
            .group_by(Payment.account_id)
        )
        prev_stmt = _scope_payment_stmt(prev_stmt)
        prev_by_account = {
            str(r.account_id): float(r.total) for r in db.execute(prev_stmt).all()
        }

        # Current month totals by account (only for cohort accounts)
        cohort_ids = set(prev_by_account.keys())
        prev_total = sum(prev_by_account.values())

        if cohort_ids and prev_total > 0:
            cur_stmt = (
                select(
                    Payment.account_id,
                    func.sum(Payment.amount).label("total"),
                )
                .where(
                    Payment.status == PaymentStatus.succeeded,
                    func.coalesce(Payment.paid_at, Payment.created_at) >= current_start,
                    func.coalesce(Payment.paid_at, Payment.created_at) < current_end,
                    cast(Payment.account_id, String).in_(cohort_ids),
                )
                .group_by(Payment.account_id)
            )
            cur_stmt = _scope_payment_stmt(cur_stmt)
            current_by_account = {
                str(r.account_id): float(r.total) for r in db.execute(cur_stmt).all()
            }
            current_total = sum(
                current_by_account.get(acc_id, 0.0) for acc_id in cohort_ids
            )
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
        )
        tp_stmt = _scope_payment_stmt(tp_stmt)
        tp_stmt = (
            tp_stmt.group_by(Payment.account_id)
            .order_by(func.sum(Payment.amount).desc())
            .limit(10)
        )
        tp_rows = db.execute(tp_stmt).all()
        top_payer_labels: list[str] = []
        top_payer_values: list[float] = []
        for tpr in tp_rows:
            name = (
                tpr.display_name
                or " ".join(
                    part
                    for part in [
                        (tpr.first_name or "").strip(),
                        (tpr.last_name or "").strip(),
                    ]
                    if part
                )
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
        recent_stmt = select(Invoice).options(joinedload(Invoice.account))
        if period_start is not None:
            recent_stmt = recent_stmt.where(Invoice.created_at >= period_start)
        if period_end is not None:
            recent_stmt = recent_stmt.where(Invoice.created_at < period_end)
        recent_stmt = _scope_invoice_stmt(recent_stmt)
        recent_stmt = recent_stmt.order_by(Invoice.created_at.desc()).limit(10)
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
            "selected_period": selected_period,
        }


billing_reporting = BillingReporting()
