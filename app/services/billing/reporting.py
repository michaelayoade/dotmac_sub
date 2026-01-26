"""Billing reporting services.

Provides statistics, summaries, and reports for billing data.
"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session


def _get_status_value(obj, attr_name: str) -> str:
    """Helper to get status value from enum or string."""
    status = getattr(obj, attr_name, "")
    return status.value if hasattr(status, "value") else str(status or "")


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
            status = _get_status_value(inv, "status")
            total = Decimal(str(getattr(inv, "total", 0) or 0))

            if status == "paid":
                total_revenue += total
                paid_count += 1
            elif status in ("pending", "sent"):
                pending_amount += total
                pending_count += 1
            elif status == "overdue":
                overdue_amount += total
                overdue_count += 1
            elif status == "draft":
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
            status = _get_status_value(account, "status") or "active"
            if status == "active":
                active_count += 1
            elif status == "suspended":
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

        today = datetime.now(timezone.utc).date()
        buckets = {
            "current": [],
            "1_30": [],
            "31_60": [],
            "61_90": [],
            "90_plus": [],
        }

        for invoice in all_invoices:
            status = _get_status_value(invoice, "status")
            if status in {"paid", "void"}:
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


billing_reporting = BillingReporting()
