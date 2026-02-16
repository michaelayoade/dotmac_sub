"""Service for managing subscription change requests."""

import logging
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscription_change import SubscriptionChangeRequest, SubscriptionChangeStatus
from app.services.common import apply_ordering, apply_pagination, coerce_uuid
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


class SubscriptionChangeRequests(ListResponseMixin):
    """Service for subscription change request CRUD operations."""

    @staticmethod
    def create(
        db: Session,
        subscription_id: str,
        new_offer_id: str,
        effective_date: date,
        requested_by_person_id: str | None = None,
        notes: str | None = None,
    ) -> SubscriptionChangeRequest:
        """Create a new subscription change request.

        Args:
            db: Database session
            subscription_id: The subscription to change
            new_offer_id: The new offer to switch to
            effective_date: When the change should take effect
            requested_by_person_id: Person making the request
            notes: Optional notes

        Returns:
            The created change request
        """
        subscription = db.get(Subscription, coerce_uuid(subscription_id))
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")

        if not subscription.offer_id:
            raise HTTPException(
                status_code=400, detail="Subscription has no current offer"
            )

        # Validate new offer exists
        from app.models.catalog import CatalogOffer

        new_offer = db.get(CatalogOffer, coerce_uuid(new_offer_id))
        if not new_offer:
            raise HTTPException(status_code=404, detail="Requested offer not found")

        if not new_offer.is_active:
            raise HTTPException(status_code=400, detail="Requested offer is not active")

        # Check for existing pending request
        existing = (
            db.query(SubscriptionChangeRequest)
            .filter(SubscriptionChangeRequest.subscription_id == subscription.id)
            .filter(SubscriptionChangeRequest.status == SubscriptionChangeStatus.pending)
            .filter(SubscriptionChangeRequest.is_active.is_(True))
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=400,
                detail="A pending change request already exists for this subscription",
            )

        request = SubscriptionChangeRequest(
            subscription_id=subscription.id,
            current_offer_id=subscription.offer_id,
            requested_offer_id=new_offer.id,
            effective_date=effective_date,
            requested_by_person_id=coerce_uuid(requested_by_person_id) if requested_by_person_id else None,
            notes=notes,
            status=SubscriptionChangeStatus.pending,
        )
        db.add(request)
        db.commit()
        db.refresh(request)

        logger.info(
            f"Created subscription change request {request.id} for subscription {subscription_id}"
        )
        return request

    @staticmethod
    def get(db: Session, request_id: str) -> SubscriptionChangeRequest:
        """Get a subscription change request by ID."""
        request = db.get(SubscriptionChangeRequest, coerce_uuid(request_id))
        if not request:
            raise HTTPException(status_code=404, detail="Change request not found")
        return request

    @staticmethod
    def list(
        db: Session,
        subscription_id: str | None,
        account_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ) -> list[SubscriptionChangeRequest]:
        """List subscription change requests with filters."""
        query = db.query(SubscriptionChangeRequest).filter(
            SubscriptionChangeRequest.is_active.is_(True)
        )

        if subscription_id:
            query = query.filter(
                SubscriptionChangeRequest.subscription_id == coerce_uuid(subscription_id)
            )

        if account_id:
            # Join to get requests for all subscriptions under this account
            query = query.join(Subscription).filter(
                Subscription.subscriber_id == coerce_uuid(account_id)
            )

        if status:
            try:
                query = query.filter(
                    SubscriptionChangeRequest.status == SubscriptionChangeStatus(status)
                )
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": SubscriptionChangeRequest.created_at,
                "effective_date": SubscriptionChangeRequest.effective_date,
                "status": SubscriptionChangeRequest.status,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def approve(
        db: Session,
        request_id: str,
        reviewer_id: str | None = None,
    ) -> SubscriptionChangeRequest:
        """Approve a subscription change request.

        Args:
            db: Database session
            request_id: The change request ID
            reviewer_id: Person approving the request

        Returns:
            The updated change request
        """
        request = db.get(SubscriptionChangeRequest, coerce_uuid(request_id))
        if not request:
            raise HTTPException(status_code=404, detail="Change request not found")

        if request.status != SubscriptionChangeStatus.pending:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve request with status {request.status.value}",
            )

        now = datetime.now(timezone.utc)
        request.status = SubscriptionChangeStatus.approved
        request.reviewed_at = now
        if reviewer_id:
            request.reviewed_by_person_id = coerce_uuid(reviewer_id)

        db.commit()
        db.refresh(request)

        logger.info(f"Approved subscription change request {request_id}")
        return request

    @staticmethod
    def reject(
        db: Session,
        request_id: str,
        reviewer_id: str | None = None,
        reason: str | None = None,
    ) -> SubscriptionChangeRequest:
        """Reject a subscription change request.

        Args:
            db: Database session
            request_id: The change request ID
            reviewer_id: Person rejecting the request
            reason: Rejection reason

        Returns:
            The updated change request
        """
        request = db.get(SubscriptionChangeRequest, coerce_uuid(request_id))
        if not request:
            raise HTTPException(status_code=404, detail="Change request not found")

        if request.status != SubscriptionChangeStatus.pending:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot reject request with status {request.status.value}",
            )

        now = datetime.now(timezone.utc)
        request.status = SubscriptionChangeStatus.rejected
        request.reviewed_at = now
        request.rejection_reason = reason
        if reviewer_id:
            request.reviewed_by_person_id = coerce_uuid(reviewer_id)

        db.commit()
        db.refresh(request)

        logger.info(f"Rejected subscription change request {request_id}")
        return request

    @staticmethod
    def apply(db: Session, request_id: str) -> SubscriptionChangeRequest:
        """Apply an approved subscription change request.

        Updates the subscription to the new offer.

        Args:
            db: Database session
            request_id: The change request ID

        Returns:
            The updated change request
        """
        request = db.get(SubscriptionChangeRequest, coerce_uuid(request_id))
        if not request:
            raise HTTPException(status_code=404, detail="Change request not found")

        if request.status != SubscriptionChangeStatus.approved:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot apply request with status {request.status.value}",
            )

        subscription = db.get(Subscription, request.subscription_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")

        # Update subscription to new offer
        subscription.offer_id = request.requested_offer_id

        # Get new offer for pricing info
        from app.models.catalog import CatalogOffer

        new_offer = db.get(CatalogOffer, request.requested_offer_id)
        if new_offer:
            subscription.monthly_price = new_offer.price

        now = datetime.now(timezone.utc)
        request.status = SubscriptionChangeStatus.applied
        request.applied_at = now

        db.commit()
        db.refresh(request)

        logger.info(
            f"Applied subscription change request {request_id}, "
            f"subscription {subscription.id} now on offer {request.requested_offer_id}"
        )
        return request

    @staticmethod
    def cancel(
        db: Session,
        request_id: str,
        notes: str | None = None,
    ) -> SubscriptionChangeRequest:
        """Cancel a pending subscription change request.

        Args:
            db: Database session
            request_id: The change request ID
            notes: Cancellation notes

        Returns:
            The updated change request
        """
        request = db.get(SubscriptionChangeRequest, coerce_uuid(request_id))
        if not request:
            raise HTTPException(status_code=404, detail="Change request not found")

        if request.status != SubscriptionChangeStatus.pending:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot cancel request with status {request.status.value}",
            )

        request.status = SubscriptionChangeStatus.canceled
        if notes:
            request.notes = (request.notes + "\n" + notes) if request.notes else notes

        db.commit()
        db.refresh(request)

        logger.info(f"Canceled subscription change request {request_id}")
        return request


subscription_change_requests = SubscriptionChangeRequests()
