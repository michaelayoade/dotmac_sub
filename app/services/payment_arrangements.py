"""Service for managing payment arrangements."""

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session
from typing import cast

from app.models.payment_arrangement import (
    ArrangementStatus,
    InstallmentStatus,
    PaymentArrangement,
    PaymentArrangementInstallment,
    PaymentFrequency,
)
from app.services.common import apply_ordering, apply_pagination, coerce_uuid
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


def _calculate_end_date(
    start_date: date, frequency: PaymentFrequency, installments: int
) -> date:
    """Calculate the end date based on frequency and number of installments."""
    if frequency == PaymentFrequency.weekly:
        return start_date + timedelta(weeks=installments - 1)
    elif frequency == PaymentFrequency.biweekly:
        return start_date + timedelta(weeks=(installments - 1) * 2)
    else:  # monthly
        # Add months
        end_date = start_date
        for _ in range(installments - 1):
            # Move to next month
            if end_date.month == 12:
                end_date = end_date.replace(year=end_date.year + 1, month=1)
            else:
                end_date = end_date.replace(month=end_date.month + 1)
        return end_date


def _calculate_next_due_date(
    current_date: date, frequency: PaymentFrequency
) -> date:
    """Calculate the next due date based on frequency."""
    if frequency == PaymentFrequency.weekly:
        return current_date + timedelta(weeks=1)
    elif frequency == PaymentFrequency.biweekly:
        return current_date + timedelta(weeks=2)
    else:  # monthly
        if current_date.month == 12:
            return current_date.replace(year=current_date.year + 1, month=1)
        else:
            return current_date.replace(month=current_date.month + 1)


class PaymentArrangements(ListResponseMixin):
    """Service for payment arrangement CRUD operations."""

    @staticmethod
    def create(
        db: Session,
        account_id: str,
        total_amount: Decimal,
        installments: int,
        frequency: str,
        start_date: date,
        invoice_id: str | None = None,
        requested_by_person_id: str | None = None,
        notes: str | None = None,
    ) -> PaymentArrangement:
        """Create a new payment arrangement with installments.

        Args:
            db: Database session
            account_id: The account requesting the arrangement
            total_amount: Total amount to be paid
            installments: Number of installments
            frequency: Payment frequency (weekly, biweekly, monthly)
            start_date: First payment date
            invoice_id: Optional specific invoice
            requested_by_person_id: Person making the request
            notes: Optional notes

        Returns:
            The created payment arrangement
        """
        # Validate account
        from app.models.subscriber import Subscriber

        account = db.get(Subscriber, coerce_uuid(account_id))
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")

        # Validate frequency
        try:
            freq = PaymentFrequency(frequency)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid frequency. Must be one of: {[f.value for f in PaymentFrequency]}",
            )

        # Validate invoice if provided
        if invoice_id:
            from app.models.billing import Invoice

            invoice = db.get(Invoice, coerce_uuid(invoice_id))
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if str(invoice.account_id) != account_id:
                raise HTTPException(
                    status_code=400, detail="Invoice does not belong to this account"
                )

        # Validate installments
        if installments < 2:
            raise HTTPException(
                status_code=400, detail="Minimum 2 installments required"
            )
        if installments > 24:
            raise HTTPException(
                status_code=400, detail="Maximum 24 installments allowed"
            )

        # Calculate installment amount
        installment_amount = (total_amount / Decimal(installments)).quantize(
            Decimal("0.01")
        )

        # Adjust last installment for rounding
        total_from_installments = installment_amount * installments
        rounding_diff = total_amount - total_from_installments

        # Calculate end date
        end_date = _calculate_end_date(start_date, freq, installments)

        arrangement = PaymentArrangement(
            account_id=coerce_uuid(account_id),
            invoice_id=coerce_uuid(invoice_id) if invoice_id else None,
            total_amount=total_amount,
            installment_amount=installment_amount,
            frequency=freq,
            installments_total=installments,
            installments_paid=0,
            start_date=start_date,
            end_date=end_date,
            next_due_date=start_date,
            status=ArrangementStatus.pending,
            requested_by_person_id=coerce_uuid(requested_by_person_id) if requested_by_person_id else None,
            notes=notes,
        )
        db.add(arrangement)
        db.flush()

        # Create installments
        current_date = start_date
        for i in range(installments):
            amount = installment_amount
            # Add rounding difference to last installment
            if i == installments - 1:
                amount += rounding_diff

            installment = PaymentArrangementInstallment(
                arrangement_id=arrangement.id,
                installment_number=i + 1,
                amount=amount,
                due_date=current_date,
                status=InstallmentStatus.pending,
            )
            db.add(installment)

            current_date = _calculate_next_due_date(current_date, freq)

        db.commit()
        db.refresh(arrangement)

        logger.info(
            f"Created payment arrangement {arrangement.id} for account {account_id}, "
            f"{installments} installments of {installment_amount}"
        )
        return arrangement

    @staticmethod
    def get(db: Session, arrangement_id: str) -> PaymentArrangement:
        """Get a payment arrangement by ID."""
        arrangement = db.get(PaymentArrangement, coerce_uuid(arrangement_id))
        if not arrangement:
            raise HTTPException(status_code=404, detail="Payment arrangement not found")
        return arrangement

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ) -> list[PaymentArrangement]:
        """List payment arrangements with filters."""
        query = db.query(PaymentArrangement).filter(
            PaymentArrangement.is_active.is_(True)
        )

        if account_id:
            query = query.filter(
                PaymentArrangement.subscriber_id == coerce_uuid(account_id)
            )

        if status:
            try:
                query = query.filter(
                    PaymentArrangement.status == ArrangementStatus(status)
                )
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": PaymentArrangement.created_at,
                "start_date": PaymentArrangement.start_date,
                "status": PaymentArrangement.status,
            },
        )
        return cast(list[PaymentArrangement], apply_pagination(query, limit, offset).all())

    @staticmethod
    def approve(
        db: Session,
        arrangement_id: str,
        approver_id: str | None = None,
    ) -> PaymentArrangement:
        """Approve a payment arrangement.

        Args:
            db: Database session
            arrangement_id: The arrangement ID
            approver_id: Person approving the arrangement

        Returns:
            The updated arrangement
        """
        arrangement = db.get(PaymentArrangement, coerce_uuid(arrangement_id))
        if not arrangement:
            raise HTTPException(status_code=404, detail="Payment arrangement not found")

        if arrangement.status != ArrangementStatus.pending:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve arrangement with status {arrangement.status.value}",
            )

        now = datetime.now(timezone.utc)
        arrangement.status = ArrangementStatus.active
        arrangement.approved_at = now
        if approver_id:
            arrangement.approved_by_subscriber_id = coerce_uuid(approver_id)

        # Mark first installment as due
        first_installment = (
            db.query(PaymentArrangementInstallment)
            .filter(PaymentArrangementInstallment.arrangement_id == arrangement.id)
            .filter(PaymentArrangementInstallment.installment_number == 1)
            .first()
        )
        if first_installment:
            first_installment.status = InstallmentStatus.due

        db.commit()
        db.refresh(arrangement)

        logger.info(f"Approved payment arrangement {arrangement_id}")
        return arrangement

    @staticmethod
    def record_installment_payment(
        db: Session,
        installment_id: str,
        payment_id: str | None = None,
    ) -> PaymentArrangementInstallment:
        """Record payment for an installment.

        Args:
            db: Database session
            installment_id: The installment ID
            payment_id: Optional payment record ID

        Returns:
            The updated installment
        """
        installment = db.get(PaymentArrangementInstallment, coerce_uuid(installment_id))
        if not installment:
            raise HTTPException(status_code=404, detail="Installment not found")

        if installment.status == InstallmentStatus.paid:
            raise HTTPException(
                status_code=400, detail="Installment already paid"
            )

        now = datetime.now(timezone.utc)
        installment.status = InstallmentStatus.paid
        installment.paid_at = now
        if payment_id:
            installment.payment_id = coerce_uuid(payment_id)

        # Update arrangement
        arrangement = installment.arrangement
        arrangement.installments_paid += 1

        # Mark next installment as due
        next_installment = (
            db.query(PaymentArrangementInstallment)
            .filter(PaymentArrangementInstallment.arrangement_id == arrangement.id)
            .filter(PaymentArrangementInstallment.installment_number == installment.installment_number + 1)
            .first()
        )
        if next_installment:
            next_installment.status = InstallmentStatus.due
            arrangement.next_due_date = next_installment.due_date
        else:
            arrangement.next_due_date = None

        # Check if all installments are paid
        if arrangement.installments_paid >= arrangement.installments_total:
            arrangement.status = ArrangementStatus.completed
            logger.info(f"Payment arrangement {arrangement.id} completed")

        db.commit()
        db.refresh(installment)

        logger.info(
            f"Recorded payment for installment {installment_id}, "
            f"arrangement {arrangement.id} now has {arrangement.installments_paid}/{arrangement.installments_total} paid"
        )
        return installment

    @staticmethod
    def check_overdue_installments(db: Session) -> int:
        """Check for overdue installments and update their status.

        Returns:
            Number of installments marked as overdue
        """
        today = date.today()

        # Find due installments that are past their due date
        overdue = (
            db.query(PaymentArrangementInstallment)
            .filter(PaymentArrangementInstallment.status == InstallmentStatus.due)
            .filter(PaymentArrangementInstallment.due_date < today)
            .filter(PaymentArrangementInstallment.is_active.is_(True))
            .all()
        )

        for installment in overdue:
            installment.status = InstallmentStatus.overdue

            # Check if arrangement should be marked as defaulted
            # (e.g., if 2+ installments are overdue)
            arrangement = installment.arrangement
            overdue_count = (
                db.query(PaymentArrangementInstallment)
                .filter(PaymentArrangementInstallment.arrangement_id == arrangement.id)
                .filter(PaymentArrangementInstallment.status == InstallmentStatus.overdue)
                .count()
            )
            if overdue_count >= 2 and arrangement.status == ArrangementStatus.active:
                arrangement.status = ArrangementStatus.defaulted
                logger.warning(f"Payment arrangement {arrangement.id} defaulted")

        if overdue:
            db.commit()
            logger.info(f"Marked {len(overdue)} installments as overdue")

        return len(overdue)

    @staticmethod
    def cancel(
        db: Session,
        arrangement_id: str,
        notes: str | None = None,
    ) -> PaymentArrangement:
        """Cancel a payment arrangement.

        Args:
            db: Database session
            arrangement_id: The arrangement ID
            notes: Cancellation notes

        Returns:
            The updated arrangement
        """
        arrangement = db.get(PaymentArrangement, coerce_uuid(arrangement_id))
        if not arrangement:
            raise HTTPException(status_code=404, detail="Payment arrangement not found")

        if arrangement.status in (ArrangementStatus.completed, ArrangementStatus.canceled):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot cancel arrangement with status {arrangement.status.value}",
            )

        arrangement.status = ArrangementStatus.canceled
        if notes:
            arrangement.notes = (arrangement.notes + "\n" + notes) if arrangement.notes else notes

        db.commit()
        db.refresh(arrangement)

        logger.info(f"Canceled payment arrangement {arrangement_id}")
        return arrangement


class PaymentArrangementInstallments(ListResponseMixin):
    """Service for installment operations."""

    @staticmethod
    def list(
        db: Session,
        arrangement_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ) -> list[PaymentArrangementInstallment]:
        """List installments with filters."""
        query = db.query(PaymentArrangementInstallment).filter(
            PaymentArrangementInstallment.is_active.is_(True)
        )

        if arrangement_id:
            query = query.filter(
                PaymentArrangementInstallment.arrangement_id == coerce_uuid(arrangement_id)
            )

        if status:
            try:
                query = query.filter(
                    PaymentArrangementInstallment.status == InstallmentStatus(status)
                )
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "due_date": PaymentArrangementInstallment.due_date,
                "installment_number": PaymentArrangementInstallment.installment_number,
                "status": PaymentArrangementInstallment.status,
            },
        )
        return cast(
            list[PaymentArrangementInstallment],
            apply_pagination(query, limit, offset).all(),
        )


payment_arrangements = PaymentArrangements()
installments = PaymentArrangementInstallments()
