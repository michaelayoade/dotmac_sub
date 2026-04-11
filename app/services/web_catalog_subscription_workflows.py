"""Route-facing workflow helpers for admin catalog subscriptions."""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import SubscriberCategory
from app.services import catalog as catalog_service
from app.services import subscriber as subscriber_service
from app.services import web_catalog_subscriptions as core
from app.services.audit_helpers import build_audit_activities

logger = logging.getLogger(__name__)


def get_subscription_or_none(
    db: Session,
    subscription_id: str,
) -> Subscription | None:
    """Return a subscription for route-level 404 handling."""
    try:
        return catalog_service.subscriptions.get(
            db=db,
            subscription_id=subscription_id,
        )
    except Exception:
        return None


def subscription_edit_form_context(
    db: Session,
    subscription_id: str,
) -> dict[str, object] | None:
    """Build subscription edit form context, or None when missing."""
    subscription_obj = get_subscription_or_none(db, subscription_id)
    if subscription_obj is None:
        return None
    subscription = core.edit_form_data(db, subscription_obj)
    context = core.subscription_form_context(db, subscription)
    context["activities"] = build_audit_activities(
        db, "subscription", str(subscription_id)
    )
    context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
    return context


def subscription_detail_page_context(
    db: Session,
    subscription_id: str,
) -> dict[str, object] | None:
    """Build subscription detail page context, or None when missing."""
    subscription = get_subscription_or_none(db, subscription_id)
    if subscription is None:
        return None
    context: dict[str, object] = {
        "subscription": subscription,
        "activities": build_audit_activities(db, "subscription", str(subscription_id)),
    }
    context.update(core.subscription_detail_context(db, subscription))
    return context


def customer_detail_url_for_subscriber_id(db: Session, subscriber_id: str) -> str:
    """Return the admin customer services URL for a subscriber."""
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
    if subscriber.category == SubscriberCategory.business:
        return f"/admin/customers/business/{subscriber.id}#subscriptions"
    return f"/admin/customers/person/{subscriber.id}#subscriptions"


def _selected_ipv4_values_from_form(form: FormData) -> tuple[list[str], list[str]]:
    block_ids = [
        str(value).strip()
        for value in form.getlist("ipv4_block_ids")
        if str(value).strip()
    ]
    addresses = [
        str(value).strip()
        for value in form.getlist("ipv4_addresses")
        if str(value).strip()
    ]
    return block_ids, addresses


def handle_subscription_create_form(
    db: Session,
    *,
    form: FormData,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Validate and create a subscription from the admin form."""
    subscription = core.parse_subscription_form(form)
    error = core.resolve_account_id(db, subscription)
    if not error:
        error = core.validate_subscription_form(subscription, for_create=True)
    if not error:
        try:
            block_ids, addresses = _selected_ipv4_values_from_form(form)
            core.ensure_ipv4_blocks_allocatable(db, block_ids, addresses)
        except Exception as exc:
            error = core.error_message(exc)
    if error:
        return {
            "form_context": core.subscription_form_context(db, subscription, error),
        }

    subscriber_id = str(
        subscription.get("subscriber_id") or subscription.get("account_id") or ""
    )
    try:
        core.create_subscription_with_audit(
            db,
            core.build_payload_data(subscription),
            form,
            request,
            actor_id,
        )
        redirect_url = (
            customer_detail_url_for_subscriber_id(db, subscriber_id)
            if subscriber_id
            else "/admin/catalog/subscriptions"
        )
        return {"redirect_url": redirect_url}
    except ValidationError as exc:
        db.rollback()
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        db.rollback()
        error = core.error_message(exc)

    return {
        "form_context": core.subscription_form_context(
            db,
            subscription,
            error or "Please correct the highlighted fields.",
        ),
    }


def handle_subscription_update_form(
    db: Session,
    *,
    subscription_id: str,
    form: FormData,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Validate and update a subscription from the admin form."""
    subscription = core.parse_subscription_form(form, subscription_id=subscription_id)
    error = core.validate_subscription_form(subscription, for_create=False)
    if error:
        context = core.subscription_form_context(db, subscription, error)
        context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
        return {"form_context": context}

    try:
        block_ids, addresses = _selected_ipv4_values_from_form(form)
        updated = core.update_subscription_with_audit(
            db,
            subscription_id,
            core.build_payload_data(subscription),
            str(subscription.get("service_password") or ""),
            block_ids,
            addresses,
            request,
            actor_id,
        )
        subscriber_id = getattr(updated, "subscriber_id", None)
        redirect_url = (
            customer_detail_url_for_subscriber_id(db, str(subscriber_id))
            if subscriber_id
            else "/admin/catalog/subscriptions"
        )
        return {"redirect_url": redirect_url}
    except ValidationError as exc:
        db.rollback()
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        db.rollback()
        error = core.error_message(exc)

    context = core.subscription_form_context(
        db,
        subscription,
        error or "Please correct the highlighted fields.",
    )
    context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
    return {"form_context": context}


def send_subscription_credentials_redirect(
    db: Session,
    *,
    subscription_id: str,
) -> str:
    """Send service credentials and return the edit-page redirect URL."""
    try:
        result = core.send_subscription_credentials(
            db,
            subscription_id=subscription_id,
        )
        notice = (
            f"Sent credentials to {result['email_sent']} email target(s) "
            f"and {result['sms_sent']} SMS target(s)."
        )
        query = f"notice={quote_plus(notice)}"
    except Exception as exc:
        logger.error(
            "Failed to send credentials for subscription %s: %s",
            subscription_id,
            exc,
        )
        query = f"error={quote_plus(str(exc))}"
    return f"/admin/catalog/subscriptions/{subscription_id}/edit?{query}"


def bulk_activate_response(
    db: Session,
    *,
    subscription_ids: str,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Activate eligible subscriptions and return API response payload."""
    count = core.bulk_update_status(
        db,
        subscription_ids,
        target_status=SubscriptionStatus.active,
        allowed_from=[SubscriptionStatus.pending, SubscriptionStatus.suspended],
        request=request,
        actor_id=actor_id,
    )
    return {"message": f"Activated {count} subscriptions", "count": count}


def bulk_suspend_response(
    db: Session,
    *,
    subscription_ids: str,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Suspend eligible subscriptions and return API response payload."""
    count = core.bulk_update_status(
        db,
        subscription_ids,
        target_status=SubscriptionStatus.suspended,
        allowed_from=[SubscriptionStatus.active],
        request=request,
        actor_id=actor_id,
    )
    return {"message": f"Suspended {count} subscriptions", "count": count}


def bulk_cancel_response(
    db: Session,
    *,
    subscription_ids: str,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Cancel eligible subscriptions and return API response payload."""
    count = core.bulk_update_status(
        db,
        subscription_ids,
        target_status=SubscriptionStatus.canceled,
        allowed_from=[
            SubscriptionStatus.active,
            SubscriptionStatus.pending,
            SubscriptionStatus.suspended,
        ],
        request=request,
        actor_id=actor_id,
    )
    return {"message": f"Canceled {count} subscriptions", "count": count}


def bulk_change_plan_response(
    db: Session,
    *,
    subscription_ids: str,
    target_offer_id: str,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Bulk change subscription plans and return API response payload."""
    count = core.bulk_change_plan(
        db,
        subscription_ids,
        target_offer_id,
        request=request,
        actor_id=actor_id,
    )
    return {"message": f"Changed plan for {count} subscriptions", "count": count}
