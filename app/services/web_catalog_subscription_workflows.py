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
        "offer_options": core.active_offer_options(db),
        "scheduled_plan_change": _scheduled_plan_change_context(db, subscription_id),
    }
    context.update(core.subscription_detail_context(db, subscription))
    return context


def _scheduled_plan_change_context(
    db: Session,
    subscription_id: str,
) -> dict[str, object] | None:
    """Summarize the outstanding scheduled (next-cycle) plan change, if any."""
    from app.models.catalog import CatalogOffer
    from app.services.subscription_changes import subscription_change_requests

    scheduled = subscription_change_requests.get_scheduled_for_subscription(
        db, subscription_id
    )
    if scheduled is None:
        return None
    target_offer = db.get(CatalogOffer, scheduled.requested_offer_id)
    return {
        "id": str(scheduled.id),
        "offer_name": target_offer.name if target_offer else "New plan",
        "effective_date": scheduled.effective_date,
    }


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
    ][:1]
    addresses = [
        str(value).strip()
        for value in form.getlist("ipv4_addresses")
        if str(value).strip()
    ][:1]
    return block_ids, addresses


def _has_ipv4_assignment_edit(block_ids: list[str], addresses: list[str]) -> bool:
    return bool(block_ids or addresses)


def _selected_additional_route_values_from_form(
    form: FormData,
) -> tuple[list[str], list[str]]:
    cidrs = [str(value).strip() for value in form.getlist("additional_route_cidrs")]
    metrics = [str(value).strip() for value in form.getlist("additional_route_metrics")]
    return cidrs, metrics


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
            if block_ids or addresses:
                core.ensure_ipv4_blocks_allocatable(db, block_ids, addresses)
            route_cidrs, route_metrics = _selected_additional_route_values_from_form(
                form
            )
            core.normalize_additional_routes(route_cidrs, route_metrics)
            core.validate_additional_route_billing(
                db, cidrs=route_cidrs, metrics=route_metrics
            )
            core.validate_public_ip_addon_selection(
                db,
                add_on_id=str(form.get("ip_addon_id") or ""),
                quantity=str(form.get("ip_addon_quantity") or "1"),
            )
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
    error = core.resolve_account_id(db, subscription)
    if not error:
        error = core.validate_subscription_form(subscription, for_create=False)
    if not error:
        try:
            route_cidrs, route_metrics = _selected_additional_route_values_from_form(
                form
            )
            core.normalize_additional_routes(route_cidrs, route_metrics)
            core.validate_additional_route_billing(
                db, cidrs=route_cidrs, metrics=route_metrics
            )
            core.validate_public_ip_addon_selection(
                db,
                add_on_id=str(form.get("ip_addon_id") or ""),
                quantity=str(form.get("ip_addon_quantity") or "1"),
            )
        except Exception as exc:
            error = core.error_message(exc)
    if error:
        context = core.subscription_form_context(db, subscription, error)
        context["action_url"] = f"/admin/catalog/subscriptions/{subscription_id}/edit"
        return {"form_context": context}

    try:
        block_ids, addresses = _selected_ipv4_values_from_form(form)
        ipv4_assignment_submitted = _has_ipv4_assignment_edit(block_ids, addresses)
        updated = core.update_subscription_with_audit(
            db,
            subscription_id,
            core.build_payload_data(subscription),
            str(subscription.get("service_password") or ""),
            block_ids,
            addresses,
            request,
            actor_id,
            additional_route_cidrs=route_cidrs,
            additional_route_metrics=route_metrics,
            ip_addon_id=str(form.get("ip_addon_id") or ""),
            ip_addon_quantity=str(form.get("ip_addon_quantity") or "1"),
            ipv4_assignment_submitted=ipv4_assignment_submitted,
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


def admin_resume_vacation_hold_redirect(
    db: Session,
    *,
    subscription_id: str,
    actor_id: str | None,
) -> str:
    """Admin action to resume a customer vacation hold and return redirect URL."""
    from app.models.enforcement_lock import EnforcementReason
    from app.services.account_lifecycle import restore_subscription

    admin_ref = f"admin:{actor_id or 'unknown'}"
    try:
        restore_subscription(
            db,
            subscription_id,
            trigger=admin_ref,
            resolved_by=admin_ref,
            reason=EnforcementReason.customer_hold,
        )
        db.commit()
        notice = "Vacation hold has been cleared. Service resumed successfully."
        query = f"notice={quote_plus(notice)}"
    except Exception as exc:
        db.rollback()
        logger.error(
            "Failed to resume vacation hold for subscription %s: %s",
            subscription_id,
            exc,
            exc_info=True,
        )
        query = f"error={quote_plus(str(exc))}"
    return f"/admin/catalog/subscriptions/{subscription_id}?{query}"


def _bulk_result_payload(verb: str, result: dict) -> dict[str, object]:
    """Standard partial-success payload: message + changed/skipped/failed detail."""
    changed = result.get("changed", 0)
    skipped = result.get("skipped_ids", [])
    failed = result.get("failed_ids", [])
    parts = [f"{verb} {changed} subscription{'s' if changed != 1 else ''}"]
    if skipped:
        parts.append(f"{len(skipped)} skipped (not eligible)")
    if failed:
        parts.append(f"{len(failed)} FAILED")
    return {
        "message": "; ".join(parts),
        "count": changed,
        "changed": changed,
        "skipped_ids": skipped,
        "failed_ids": failed,
    }


def bulk_activate_response(
    db: Session,
    *,
    subscription_ids: str,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Activate eligible subscriptions and return API response payload."""
    result = core.bulk_update_status(
        db,
        subscription_ids,
        target_status=SubscriptionStatus.active,
        allowed_from=[SubscriptionStatus.pending, SubscriptionStatus.suspended],
        request=request,
        actor_id=actor_id,
    )
    return _bulk_result_payload("Activated", result)


def bulk_suspend_response(
    db: Session,
    *,
    subscription_ids: str,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Suspend eligible subscriptions and return API response payload."""
    result = core.bulk_update_status(
        db,
        subscription_ids,
        target_status=SubscriptionStatus.suspended,
        allowed_from=[SubscriptionStatus.active],
        request=request,
        actor_id=actor_id,
    )
    return _bulk_result_payload("Suspended", result)


def bulk_cancel_response(
    db: Session,
    *,
    subscription_ids: str,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Cancel eligible subscriptions and return API response payload."""
    result = core.bulk_update_status(
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
    return _bulk_result_payload("Canceled", result)


def bulk_change_plan_response(
    db: Session,
    *,
    subscription_ids: str,
    target_offer_id: str,
    request: object,
    actor_id: str | None,
    effective_timing: str = "instant",
    include_suspended: bool = False,
) -> dict[str, object]:
    """Bulk change subscription plans and return API response payload.

    ``effective_timing`` is ``instant`` (swap now, prorate) or ``next_cycle``
    (schedule the swap for each subscription's next billing date).
    ``include_suspended`` also changes suspended subscriptions, not just active.
    """
    result = core.bulk_change_plan(
        db,
        subscription_ids,
        target_offer_id,
        request=request,
        actor_id=actor_id,
        effective_timing=effective_timing,
        include_suspended=include_suspended,
    )
    verb = (
        "Scheduled next-cycle plan change for"
        if effective_timing == "next_cycle"
        else "Changed plan for"
    )
    return _bulk_result_payload(verb, result)


def cancel_scheduled_plan_change_redirect(
    db: Session,
    *,
    subscription_id: str,
    request_id: str,
    actor_id: str | None,
) -> str:
    """Cancel a scheduled next-cycle plan change; return a redirect URL."""
    from app.services.audit_adapter import record_audit_event
    from app.services.subscription_changes import subscription_change_requests

    base = f"/admin/catalog/subscriptions/{subscription_id}"
    try:
        subscription_change_requests.cancel_scheduled(
            db,
            request_id=request_id,
            notes="Canceled via admin subscription detail",
        )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return f"{base}?error={quote_plus(str(detail))}"
    record_audit_event(
        db,
        action="cancel_scheduled_plan_change",
        entity_type="subscription",
        entity_id=subscription_id,
        actor_id=actor_id,
        metadata={"change_request_id": request_id},
    )
    return f"{base}?notice={quote_plus('Scheduled plan change canceled.')}"


def change_plan_quote_response(
    db: Session,
    *,
    subscription_id: str,
    target_offer_id: str,
) -> dict[str, object]:
    """Proration quote for an admin change-plan preview.

    Reuses the customer-portal quote builder so the admin modal shows the same
    credit/charge/net numbers the change will actually produce.
    """
    from fastapi import HTTPException

    from app.models.catalog import CatalogOffer
    from app.services.common import coerce_uuid
    from app.services.customer_portal_flow_changes import (
        _build_plan_change_quote,
        _serialize_plan_change_quote,
    )

    subscription = catalog_service.subscriptions.get(db, subscription_id)
    target = db.get(CatalogOffer, coerce_uuid(target_offer_id))
    if not target:
        raise HTTPException(status_code=404, detail="Target offer not found")
    quote = _build_plan_change_quote(db, subscription, target)
    return {
        "quote": _serialize_plan_change_quote(quote),
        "target_offer_name": target.name,
    }
