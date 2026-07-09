"""Service for managing subscription change requests."""

import logging
from datetime import UTC, date, datetime
from typing import cast

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.services.common import apply_ordering, apply_pagination, coerce_uuid
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)

SCHEDULED_CHANGE_TARGET_STATUSES = {
    SubscriptionStatus.pending,
    SubscriptionStatus.active,
    SubscriptionStatus.suspended,
}


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
            .filter(
                SubscriptionChangeRequest.status == SubscriptionChangeStatus.pending
            )
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
            requested_by_subscriber_id=coerce_uuid(requested_by_person_id)
            if requested_by_person_id
            else None,
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
    def schedule(
        db: Session,
        subscription_id: str,
        new_offer_id: str,
        effective_date: date,
        requested_by_person_id: str | None = None,
        notes: str | None = None,
    ) -> SubscriptionChangeRequest:
        """Schedule an admin-initiated plan change to apply at a future date.

        Records the change as an already-``approved`` request effective on
        ``effective_date`` (typically the subscription's next billing date).
        The periodic applier (``app.tasks.catalog.apply_due_subscription_changes``)
        swaps the offer once the effective date arrives — no mid-cycle proration
        is generated, so the customer simply moves to the new plan at the cycle
        boundary. Unlike :meth:`create` this needs no review: the admin has
        authority, so the row skips straight to ``approved``.

        Args:
            db: Database session
            subscription_id: The subscription to change
            new_offer_id: The new offer to switch to
            effective_date: When the change should take effect (next cycle)
            requested_by_person_id: Person scheduling the change
            notes: Optional notes

        Returns:
            The created (approved, not yet applied) change request
        """
        subscription = db.get(Subscription, coerce_uuid(subscription_id))
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")

        if not subscription.offer_id:
            raise HTTPException(
                status_code=400, detail="Subscription has no current offer"
            )

        from app.models.catalog import CatalogOffer

        new_offer = db.get(CatalogOffer, coerce_uuid(new_offer_id))
        if not new_offer:
            raise HTTPException(status_code=404, detail="Requested offer not found")

        if not new_offer.is_active:
            raise HTTPException(status_code=400, detail="Requested offer is not active")

        # Reject scheduling onto the current offer — that's a no-op change.
        if str(new_offer.id) == str(subscription.offer_id):
            raise HTTPException(
                status_code=400,
                detail="Subscription is already on the requested offer",
            )

        # One outstanding change per subscription: guard against both an
        # unreviewed customer request (pending) and an already-scheduled admin
        # change (approved, not yet applied).
        existing = (
            db.query(SubscriptionChangeRequest)
            .filter(SubscriptionChangeRequest.subscription_id == subscription.id)
            .filter(
                SubscriptionChangeRequest.status.in_(
                    [
                        SubscriptionChangeStatus.pending,
                        SubscriptionChangeStatus.approved,
                    ]
                )
            )
            .filter(SubscriptionChangeRequest.applied_at.is_(None))
            .filter(SubscriptionChangeRequest.is_active.is_(True))
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=400,
                detail="An outstanding plan change already exists for this subscription",
            )

        now = datetime.now(UTC)
        request = SubscriptionChangeRequest(
            subscription_id=subscription.id,
            current_offer_id=subscription.offer_id,
            requested_offer_id=new_offer.id,
            effective_date=effective_date,
            requested_by_subscriber_id=coerce_uuid(requested_by_person_id)
            if requested_by_person_id
            else None,
            reviewed_by_subscriber_id=coerce_uuid(requested_by_person_id)
            if requested_by_person_id
            else None,
            reviewed_at=now,
            notes=notes,
            status=SubscriptionChangeStatus.approved,
        )
        db.add(request)
        db.commit()
        db.refresh(request)

        logger.info(
            "Scheduled subscription change %s for subscription %s effective %s",
            request.id,
            subscription_id,
            effective_date,
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
    def get_scheduled_for_subscription(
        db: Session,
        subscription_id: str,
    ) -> SubscriptionChangeRequest | None:
        """Return the outstanding scheduled (approved, unapplied) change, if any."""
        return (
            db.query(SubscriptionChangeRequest)
            .filter(
                SubscriptionChangeRequest.subscription_id
                == coerce_uuid(subscription_id)
            )
            .filter(
                SubscriptionChangeRequest.status == SubscriptionChangeStatus.approved
            )
            .filter(SubscriptionChangeRequest.applied_at.is_(None))
            .filter(SubscriptionChangeRequest.is_active.is_(True))
            .order_by(SubscriptionChangeRequest.effective_date.asc())
            .first()
        )

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
                SubscriptionChangeRequest.subscription_id
                == coerce_uuid(subscription_id)
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
        return cast(
            list[SubscriptionChangeRequest],
            apply_pagination(query, limit, offset).all(),
        )

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

        now = datetime.now(UTC)
        request.status = SubscriptionChangeStatus.approved
        request.reviewed_at = now
        if reviewer_id:
            request.reviewed_by_subscriber_id = coerce_uuid(reviewer_id)

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

        now = datetime.now(UTC)
        request.status = SubscriptionChangeStatus.rejected
        request.reviewed_at = now
        request.rejection_reason = reason
        if reviewer_id:
            request.reviewed_by_subscriber_id = coerce_uuid(reviewer_id)

        db.commit()
        db.refresh(request)

        logger.info(f"Rejected subscription change request {request_id}")
        return request

    @staticmethod
    def apply(
        db: Session,
        request_id: str,
        *,
        skip_proration_artifacts: bool = False,
    ) -> SubscriptionChangeRequest:
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

        # Route all plan changes through the shared subscription update path so
        # validation, RADIUS refresh, events, and proration stay consistent.
        from app.schemas.catalog import SubscriptionUpdate
        from app.services import catalog as catalog_service

        catalog_service.subscriptions.update(
            db,
            str(subscription.id),
            SubscriptionUpdate(offer_id=request.requested_offer_id),
            skip_proration_artifacts=skip_proration_artifacts,
        )
        subscription = db.get(Subscription, request.subscription_id)
        if subscription is None:
            raise HTTPException(
                status_code=404, detail="Subscription not found after update"
            )

        now = datetime.now(UTC)
        request.status = SubscriptionChangeStatus.applied
        request.applied_at = now

        db.commit()
        db.refresh(request)
        try:
            from app.services.enforcement import update_subscription_sessions
            from app.services.radius import reconcile_subscription_connectivity

            reconcile_subscription_connectivity(db, str(subscription.id))
            if subscription.status == SubscriptionStatus.active:
                update_subscription_sessions(
                    db, str(subscription.id), reason="profile_change"
                )
        except Exception as exc:
            logger.warning(
                "Failed to refresh RADIUS state for subscription %s after change request: %s",
                subscription.id,
                exc,
            )

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

    @staticmethod
    def cancel_scheduled(
        db: Session,
        request_id: str,
        notes: str | None = None,
    ) -> SubscriptionChangeRequest:
        """Cancel an admin-scheduled (approved, not yet applied) change.

        The plain :meth:`cancel` only accepts ``pending`` rows (the customer
        request-review flow). Scheduled next-cycle changes live in ``approved``
        until the applier runs, so this cancels those before they take effect.
        """
        request = db.get(SubscriptionChangeRequest, coerce_uuid(request_id))
        if not request:
            raise HTTPException(status_code=404, detail="Change request not found")

        if (
            request.status != SubscriptionChangeStatus.approved
            or request.applied_at is not None
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot cancel scheduled change with status {request.status.value}",
            )

        request.status = SubscriptionChangeStatus.canceled
        if notes:
            request.notes = (request.notes + "\n" + notes) if request.notes else notes

        db.commit()
        db.refresh(request)

        logger.info(f"Canceled scheduled subscription change {request_id}")
        return request

    @classmethod
    def apply_due_changes(cls, db: Session) -> dict[str, object]:
        """Apply every scheduled change whose effective date has arrived.

        Processes ``approved`` (admin-scheduled next-cycle) changes with
        ``effective_date <= today`` and no ``applied_at`` yet, swapping the offer
        via :meth:`apply` with proration artifacts skipped (the change is aligned
        to the billing boundary, so there is nothing to prorate). Each request is
        applied in isolation; a failure on one does not abort the rest.

        Returns ``{applied, failed_ids}`` for observability.
        """
        today = datetime.now(UTC).date()
        due = (
            db.query(SubscriptionChangeRequest)
            .filter(
                SubscriptionChangeRequest.status == SubscriptionChangeStatus.approved
            )
            .filter(SubscriptionChangeRequest.applied_at.is_(None))
            .filter(SubscriptionChangeRequest.is_active.is_(True))
            .filter(SubscriptionChangeRequest.effective_date <= today)
            .order_by(SubscriptionChangeRequest.effective_date.asc())
            .all()
        )
        applied = 0
        canceled_ids: list[str] = []
        failed_ids: list[str] = []
        for request in due:
            try:
                subscription = db.get(Subscription, request.subscription_id)
                if subscription is None:
                    raise HTTPException(
                        status_code=404, detail="Subscription not found"
                    )
                if subscription.status not in SCHEDULED_CHANGE_TARGET_STATUSES:
                    request.status = SubscriptionChangeStatus.canceled
                    request.notes = _append_note(
                        request.notes,
                        "Auto-canceled: scheduled change target subscription is "
                        f"{subscription.status.value}.",
                    )
                    db.commit()
                    canceled_ids.append(str(request.id))
                    logger.info(
                        "Auto-canceled scheduled subscription change %s: "
                        "target subscription %s is %s",
                        request.id,
                        subscription.id,
                        subscription.status.value,
                    )
                    continue
                cls.apply(db, str(request.id), skip_proration_artifacts=True)
                applied += 1
            except Exception as exc:
                db.rollback()
                failed_ids.append(str(request.id))
                logger.error(
                    "Failed to apply scheduled subscription change %s: %s",
                    request.id,
                    exc,
                )
        return {
            "applied": applied,
            "canceled_ids": canceled_ids,
            "failed_ids": failed_ids,
        }


subscription_change_requests = SubscriptionChangeRequests()


def _append_note(existing: str | None, note: str) -> str:
    return f"{existing}\n{note}" if existing else note
