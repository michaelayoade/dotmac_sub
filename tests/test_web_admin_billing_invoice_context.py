from starlette.datastructures import FormData

from app.services import web_catalog_subscription_workflows as workflow_service
from app.services import web_catalog_subscriptions as web_catalog_subscriptions_service
from app.web.admin import billing_invoices as billing_invoices_web


def test_handle_subscription_update_form_resolves_account_from_subscriber(
    db_session,
    subscription,
    monkeypatch,
):
    captured: dict[str, object] = {}

    def fake_update_subscription_with_audit(
        db,
        subscription_id,
        payload,
        service_password,
        block_ids,
        addresses,
        request,
        actor_id,
    ):
        captured["payload"] = payload
        return subscription

    monkeypatch.setattr(
        web_catalog_subscriptions_service,
        "update_subscription_with_audit",
        fake_update_subscription_with_audit,
    )

    form = FormData(
        {
            "subscriber_id": str(subscription.subscriber_id),
            "offer_id": str(subscription.offer_id),
            "status": subscription.status.value,
            "billing_mode": subscription.billing_mode.value,
            "contract_term": subscription.contract_term.value,
        }
    )

    result = workflow_service.handle_subscription_update_form(
        db_session,
        subscription_id=str(subscription.id),
        form=form,
        request=None,
        actor_id=None,
    )

    assert result["redirect_url"].endswith("#subscriptions")
    assert captured["payload"]["account_id"] == str(subscription.subscriber_id)


def test_invoice_new_resolves_single_customer_account_from_legacy_query_params(
    db_session,
    subscriber,
):
    resolved = billing_invoices_web._resolve_invoice_new_account_id(
        db_session,
        account_id=None,
        account=None,
        customer_id=str(subscriber.id),
        customer_type="person",
    )

    assert resolved == str(subscriber.id)
