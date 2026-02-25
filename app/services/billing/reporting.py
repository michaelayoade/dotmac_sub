"""Billing reporting services.

Provides statistics, summaries, and reports for billing data.
"""
from __future__ import annotations

import calendar
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNoteStatus,
    InvoiceStatus,
    PaymentStatus,
)
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import SubscriberStatus

logger = logging.getLogger(__name__)


class BillingReporting:
    """Service for billing reports and statistics."""

    @staticmethod
    def get_overview_stats(db: Session) -> dict:
        """Calculate billing overview statistics.

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
        from app.services.billing import invoices as invoices_service

        all_invoices = invoices_service.list(
            db=db,
            account_id=None,
            status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=10000,
            offset=0,
        )

        total_revenue = Decimal("0")
        pending_amount = Decimal("0")
        overdue_amount = Decimal("0")
        paid_count = 0
        pending_count = 0
        overdue_count = 0
        draft_count = 0

        for inv in all_invoices:
            total = Decimal(str(getattr(inv, "total", 0) or 0))

            if inv.status == InvoiceStatus.paid:
                total_revenue += total
                paid_count += 1
            elif inv.status == InvoiceStatus.issued:
                pending_amount += total
                pending_count += 1
            elif inv.status == InvoiceStatus.overdue:
                overdue_amount += total
                overdue_count += 1
            elif inv.status == InvoiceStatus.draft:
                draft_count += 1

        return {
            "total_revenue": float(total_revenue),
            "pending_amount": float(pending_amount),
            "overdue_amount": float(overdue_amount),
            "total_invoices": len(all_invoices),
            "paid_count": paid_count,
            "pending_count": pending_count,
            "overdue_count": overdue_count,
            "draft_count": draft_count,
        }

    @staticmethod
    def get_account_stats(db: Session) -> dict:
        """Calculate account-level statistics.

        Returns:
            Dictionary with keys:
            - total_balance: Sum of all account balances
            - active_count: Number of active accounts
            - suspended_count: Number of suspended accounts
        """
        from app.services import subscriber as subscriber_service

        accounts = subscriber_service.accounts.list(
            db=db,
            subscriber_id=None,
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=10000,
            offset=0,
        )

        total_balance = Decimal("0")
        active_count = 0
        suspended_count = 0

        for account in accounts:
            total_balance += Decimal(str(getattr(account, "balance", 0) or 0))
            if account.status == SubscriberStatus.active:
                active_count += 1
            elif account.status == SubscriberStatus.suspended:
                suspended_count += 1

        return {
            "total_balance": float(total_balance),
            "active_count": active_count,
            "suspended_count": suspended_count,
        }

    @staticmethod
    def get_ar_aging_buckets(db: Session) -> dict:
        """Classify invoices into aging buckets.

        Returns:
            Dictionary with keys:
            - buckets: Dict with keys 'current', '1_30', '31_60', '61_90', '90_plus'
                      Each containing a list of invoices
            - totals: Dict with same keys containing sum of balance_due for each bucket
        """
        from app.services.billing import invoices as invoices_service

        all_invoices = invoices_service.list(
            db=db,
            account_id=None,
            status=None,
            is_active=None,
            order_by="due_at",
            order_dir="asc",
            limit=10000,
            offset=0,
        )

        today = datetime.now(UTC).date()
        buckets: dict[str, list[Any]] = {
            "current": [],
            "1_30": [],
            "31_60": [],
            "61_90": [],
            "90_plus": [],
        }

        for invoice in all_invoices:
            if invoice.status in {InvoiceStatus.paid, InvoiceStatus.void}:
                continue

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

        Combines overview stats, account stats, collection rate,
        revenue trend, chart data, and recent invoices into a
        single dict suitable for both web and API consumption.

        Returns:
            Dictionary with keys: stats, invoices, revenue_trend,
            chart_data, total_balance, active_count, suspended_count.
        """
        from app.services.billing import credit_notes as credit_notes_service
        from app.services.billing import invoices as invoices_service
        from app.services.billing import payments as payments_service

        def _month_start(value: datetime) -> datetime:
            return datetime(value.year, value.month, 1, tzinfo=UTC)

        def _next_month_start(value: datetime) -> datetime:
            if value.month == 12:
                return datetime(value.year + 1, 1, 1, tzinfo=UTC)
            return datetime(value.year, value.month + 1, 1, tzinfo=UTC)

        def _month_window(month_anchor: datetime) -> tuple[datetime, datetime]:
            start = _month_start(month_anchor)
            return start, _next_month_start(start)

        def _as_utc_aware(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)

        def _in_window(value: datetime | None, start: datetime, end: datetime) -> bool:
            comparable = _as_utc_aware(value)
            return bool(comparable and start <= comparable < end)

        def _matches_scope(account, *, selected_partner_id: str | None, selected_location: str | None) -> bool:
            if selected_partner_id:
                if str(getattr(account, "reseller_id", "") or "") != selected_partner_id:
                    return False
            if selected_location:
                account_location = (
                    str(getattr(account, "region", "") or "")
                    or str(getattr(account, "billing_region", "") or "")
                    or str(getattr(account, "city", "") or "")
                )
                if account_location.lower() != selected_location.lower():
                    return False
            return True

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

        all_invoices = invoices_service.list(
            db=db,
            account_id=None,
            status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=10000,
            offset=0,
        )
        if selected_partner_id or selected_location:
            all_invoices = [
                inv
                for inv in all_invoices
                if _matches_scope(getattr(inv, "account", None), selected_partner_id=selected_partner_id, selected_location=selected_location)
            ]

        all_payments = payments_service.list(
            db=db,
            account_id=None,
            invoice_id=None,
            status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=10000,
            offset=0,
        )
        if selected_partner_id or selected_location:
            all_payments = [
                p
                for p in all_payments
                if _matches_scope(getattr(p, "account", None), selected_partner_id=selected_partner_id, selected_location=selected_location)
            ]
        succeeded_payments = [
            payment
            for payment in all_payments
            if getattr(payment, "status", None) == PaymentStatus.succeeded
        ]
        total_payments_amount = sum(
            Decimal(str(getattr(payment, "amount", 0) or 0)) for payment in succeeded_payments
        )

        unpaid_statuses = {
            InvoiceStatus.issued,
            InvoiceStatus.overdue,
            InvoiceStatus.partially_paid,
        }
        unpaid_invoices = [
            inv
            for inv in all_invoices
            if getattr(inv, "status", None) in unpaid_statuses
        ]
        unpaid_amount = sum(
            Decimal(str(getattr(inv, "balance_due", 0) or 0)) for inv in unpaid_invoices
        )

        all_credit_notes = credit_notes_service.list(
            db=db,
            account_id=None,
            invoice_id=None,
            status=None,
            is_active=True,
            order_by="created_at",
            order_dir="desc",
            limit=10000,
            offset=0,
        )
        if selected_partner_id or selected_location:
            all_credit_notes = [
                n
                for n in all_credit_notes
                if _matches_scope(getattr(n, "account", None), selected_partner_id=selected_partner_id, selected_location=selected_location)
            ]
        active_credit_notes = [
            note
            for note in all_credit_notes
            if getattr(note, "status", None) != CreditNoteStatus.void
        ]
        credit_note_total = sum(
            Decimal(str(getattr(note, "total", 0) or 0)) for note in active_credit_notes
        )

        stats = {
            **overview,
            "collection_rate": collection_rate,
            "payments_count": len(succeeded_payments),
            "payments_amount": float(total_payments_amount),
            "unpaid_invoices_count": len(unpaid_invoices),
            "unpaid_invoices_amount": float(unpaid_amount),
            "credit_notes_count": len(active_credit_notes),
            "credit_notes_total": float(credit_note_total),
        }

        # Revenue trend — last 6 months of billed vs collected
        now = datetime.now(UTC)
        labels: list[str] = []
        billed: list[float] = []
        collected: list[float] = []

        for i in range(5, -1, -1):
            month = now.month - i
            year = now.year
            while month <= 0:
                month += 12
                year -= 1
            label = calendar.month_abbr[month]
            labels.append(label)

            month_billed = Decimal("0")
            month_collected = Decimal("0")
            for inv in all_invoices:
                inv_date = inv.created_at
                if inv_date and inv_date.year == year and inv_date.month == month:
                    total = Decimal(str(getattr(inv, "total", 0) or 0))
                    month_billed += total
                    if inv.status == InvoiceStatus.paid:
                        month_collected += total

            billed.append(float(month_billed))
            collected.append(float(month_collected))

        revenue_trend = {
            "labels": labels,
            "billed": billed,
            "collected": collected,
        }

        # Period comparison (last/current/next month)
        current_month_start = _month_start(now)
        last_month_start = _month_start(current_month_start - timedelta(days=1))
        next_month_start = _next_month_start(current_month_start)
        comparison_periods = [
            ("Last Month", * _month_window(last_month_start)),
            ("Current Month", * _month_window(current_month_start)),
            ("Next Month", * _month_window(next_month_start)),
        ]
        period_comparison: list[dict[str, Any]] = []
        for label, start, end in comparison_periods:
            period_payments = [
                payment
                for payment in succeeded_payments
                if _in_window(getattr(payment, "paid_at", None) or getattr(payment, "created_at", None), start, end)
            ]
            payments_amount = sum(Decimal(str(getattr(p, "amount", 0) or 0)) for p in period_payments)
            paid_invoices = [
                inv
                for inv in all_invoices
                if getattr(inv, "status", None) == InvoiceStatus.paid
                and _in_window(getattr(inv, "paid_at", None) or getattr(inv, "created_at", None), start, end)
            ]
            unpaid_invoices_period = [
                inv
                for inv in all_invoices
                if getattr(inv, "status", None) in unpaid_statuses
                and _in_window(getattr(inv, "created_at", None), start, end)
            ]
            period_credit_notes = [
                note
                for note in active_credit_notes
                if _in_window(getattr(note, "created_at", None), start, end)
            ]
            credit_note_amount = sum(Decimal(str(getattr(n, "total", 0) or 0)) for n in period_credit_notes)
            period_comparison.append(
                {
                    "label": label,
                    "payments_amount": float(payments_amount),
                    "payments_count": len(period_payments),
                    "paid_invoices_count": len(paid_invoices),
                    "unpaid_invoices_count": len(unpaid_invoices_period),
                    "credit_notes_count": len(period_credit_notes),
                    "credit_notes_amount": float(credit_note_amount),
                    "total_income": float(payments_amount),
                }
            )

        # Payment method breakdown
        method_labels = {
            "cash": "Cash",
            "card": "Card",
            "transfer": "Bank Transfer",
            "bank_account": "Bank Account",
            "check": "Check",
            "other": "Other",
            "bank_transfer": "Bank Transfer",
        }
        method_totals: dict[str, Decimal] = {}
        for payment in succeeded_payments:
            method_key = "other"
            if getattr(payment, "payment_method", None) and getattr(payment.payment_method, "method_type", None):
                raw = payment.payment_method.method_type
                method_key = raw.value if hasattr(raw, "value") else str(raw)
            elif getattr(payment, "payment_channel", None) and getattr(payment.payment_channel, "channel_type", None):
                raw = payment.payment_channel.channel_type
                method_key = raw.value if hasattr(raw, "value") else str(raw)
            label = method_labels.get(method_key, "Other")
            method_totals[label] = method_totals.get(label, Decimal("0")) + Decimal(
                str(getattr(payment, "amount", 0) or 0)
            )
        payment_method_breakdown = {
            "labels": list(method_totals.keys()),
            "values": [float(value) for value in method_totals.values()],
        }

        # Daily payments (current month)
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        daily_totals: dict[int, Decimal] = {day: Decimal("0") for day in range(1, days_in_month + 1)}
        month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
        month_end = _next_month_start(month_start)
        for payment in succeeded_payments:
            payment_at = getattr(payment, "paid_at", None) or getattr(payment, "created_at", None)
            if not _in_window(payment_at, month_start, month_end):
                continue
            if payment_at:
                daily_totals[payment_at.day] += Decimal(str(getattr(payment, "amount", 0) or 0))
        daily_payments = {
            "labels": [str(day) for day in range(1, days_in_month + 1)],
            "values": [float(daily_totals[day]) for day in range(1, days_in_month + 1)],
        }

        # Invoicing for period (status overlay) - month granularity for last 6 months.
        invoicing_labels: list[str] = []
        invoicing_paid: list[float] = []
        invoicing_unpaid: list[float] = []
        invoicing_paid_on_time: list[float] = []
        invoicing_paid_overdue: list[float] = []
        for i in range(5, -1, -1):
            month = now.month - i
            year = now.year
            while month <= 0:
                month += 12
                year -= 1
            invoicing_labels.append(calendar.month_abbr[month])
            paid = Decimal("0")
            unpaid = Decimal("0")
            paid_on_time = Decimal("0")
            paid_overdue = Decimal("0")
            for inv in all_invoices:
                inv_date = getattr(inv, "created_at", None)
                if not inv_date or inv_date.year != year or inv_date.month != month:
                    continue
                total = Decimal(str(getattr(inv, "total", 0) or 0))
                status = getattr(inv, "status", None)
                if status == InvoiceStatus.paid:
                    paid += total
                    due_at = getattr(inv, "due_at", None)
                    paid_at = getattr(inv, "paid_at", None)
                    if due_at and paid_at and paid_at > due_at:
                        paid_overdue += total
                    else:
                        paid_on_time += total
                elif status in {InvoiceStatus.issued, InvoiceStatus.overdue, InvoiceStatus.partially_paid, InvoiceStatus.draft}:
                    unpaid += total
            invoicing_paid.append(float(paid))
            invoicing_unpaid.append(float(unpaid))
            invoicing_paid_on_time.append(float(paid_on_time))
            invoicing_paid_overdue.append(float(paid_overdue))
        invoicing_period_overlay = {
            "labels": invoicing_labels,
            "paid": invoicing_paid,
            "unpaid": invoicing_unpaid,
            "paid_on_time": invoicing_paid_on_time,
            "paid_overdue": invoicing_paid_overdue,
        }

        # MRR/ARPU + top payers.
        mrr_labels: list[str] = []
        mrr_values: list[float] = []
        arpu_values: list[float] = []
        active_subscriber_counts: list[int] = []
        for i in range(5, -1, -1):
            month = now.month - i
            year = now.year
            while month <= 0:
                month += 12
                year -= 1
            month_start = datetime(year, month, 1, tzinfo=UTC)
            month_end = _next_month_start(month_start)
            mrr_labels.append(calendar.month_abbr[month])
            month_mrr = Decimal("0")
            active_accounts: set[str] = set()
            for sub in db.query(Subscription).filter(Subscription.status == SubscriptionStatus.active).all():
                sub_account = getattr(sub, "subscriber", None)
                if selected_partner_id or selected_location:
                    if not _matches_scope(sub_account, selected_partner_id=selected_partner_id, selected_location=selected_location):
                        continue
                next_billing_at = getattr(sub, "next_billing_at", None)
                if next_billing_at and _in_window(next_billing_at, month_start, month_end):
                    month_mrr += Decimal(str(getattr(sub, "unit_price", 0) or 0))
                    if getattr(sub, "subscriber_id", None):
                        active_accounts.add(str(sub.subscriber_id))
            count = len(active_accounts) or 1
            active_subscriber_counts.append(len(active_accounts))
            mrr_values.append(float(month_mrr))
            arpu_values.append(float(month_mrr / Decimal(count)))
        mrr_growth_rate = 0.0
        if len(mrr_values) >= 2 and mrr_values[-2] > 0:
            mrr_growth_rate = round(((mrr_values[-1] - mrr_values[-2]) / mrr_values[-2]) * 100, 2)

        # Planned income (next billing period from active subscriptions).
        next_month_start = _next_month_start(_month_start(now))
        next_month_end = _next_month_start(next_month_start)
        planned_income = Decimal("0")
        for sub in db.query(Subscription).filter(Subscription.status == SubscriptionStatus.active).all():
            sub_account = getattr(sub, "subscriber", None)
            if selected_partner_id or selected_location:
                if not _matches_scope(sub_account, selected_partner_id=selected_partner_id, selected_location=selected_location):
                    continue
            next_billing_at = getattr(sub, "next_billing_at", None)
            if next_billing_at and _in_window(next_billing_at, next_month_start, next_month_end):
                planned_income += Decimal(str(getattr(sub, "unit_price", 0) or 0))

        # Net revenue retention (payment-based approximation).
        current_start = _month_start(now)
        current_end = _next_month_start(current_start)
        prev_start = _month_start(current_start - timedelta(days=1))
        prev_end = current_start
        prev_by_account: dict[str, Decimal] = {}
        current_by_account: dict[str, Decimal] = {}
        for payment in succeeded_payments:
            paid_at = getattr(payment, "paid_at", None) or getattr(payment, "created_at", None)
            account_id = str(getattr(payment, "account_id", "") or "")
            if not account_id:
                continue
            amount = Decimal(str(getattr(payment, "amount", 0) or 0))
            if _in_window(paid_at, prev_start, prev_end):
                prev_by_account[account_id] = prev_by_account.get(account_id, Decimal("0")) + amount
            if _in_window(paid_at, current_start, current_end):
                current_by_account[account_id] = current_by_account.get(account_id, Decimal("0")) + amount
        cohort_ids = set(prev_by_account.keys())
        prev_total = sum(prev_by_account.values())
        current_total = sum(current_by_account.get(acc_id, Decimal("0")) for acc_id in cohort_ids)
        net_revenue_retention = round((float(current_total / prev_total) * 100), 2) if prev_total > 0 else 0.0

        payer_totals: dict[str, Decimal] = {}
        payer_labels: dict[str, str] = {}
        period_start = _month_start(now)
        period_end = _next_month_start(period_start)
        for payment in succeeded_payments:
            paid_at = getattr(payment, "paid_at", None) or getattr(payment, "created_at", None)
            if not _in_window(paid_at, period_start, period_end):
                continue
            payer_account = getattr(payment, "account", None)
            account_id = str(getattr(payment, "account_id", "") or "")
            if not account_id:
                continue
            amount = Decimal(str(getattr(payment, "amount", 0) or 0))
            payer_totals[account_id] = payer_totals.get(account_id, Decimal("0")) + amount
            if payer_account:
                payer_labels[account_id] = (
                    getattr(payer_account, "display_name", None)
                    or " ".join(
                        part for part in [
                            (getattr(payer_account, "first_name", "") or "").strip(),
                            (getattr(payer_account, "last_name", "") or "").strip(),
                        ] if part
                    )
                    or str(getattr(payer_account, "account_number", None) or f"Account {account_id[:8]}")
                )
            else:
                payer_labels[account_id] = f"Account {account_id[:8]}"
        top_payers_sorted = sorted(payer_totals.items(), key=lambda item: item[1], reverse=True)[:10]
        top_payers = {
            "labels": [payer_labels.get(account_id, account_id) for account_id, _ in top_payers_sorted],
            "values": [float(amount) for _, amount in top_payers_sorted],
        }

        # Invoice status chart data
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

        # Recent invoices (last 10)
        recent_invoices = invoices_service.list(
            db=db,
            account_id=None,
            status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )

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
            "planned_income": float(planned_income),
            "total_balance": account_stats["total_balance"],
            "active_count": account_stats["active_count"],
            "suspended_count": account_stats["suspended_count"],
        }


billing_reporting = BillingReporting()
