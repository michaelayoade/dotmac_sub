from types import SimpleNamespace

from fastapi import HTTPException
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
        **kwargs,
    ):
        captured["payload"] = payload
        captured["block_ids"] = block_ids
        captured["addresses"] = addresses
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
    assert captured["block_ids"] == []
    assert captured["addresses"] == []


def test_ipv4_replacement_action_calls_only_dedicated_owner_adapter(
    db_session,
    subscription,
    monkeypatch,
):
    captured: dict[str, object] = {}

    def fake_replace(
        db,
        *,
        subscription_id,
        selector,
        requested_ip,
        actor_id,
    ):
        captured.update(
            {
                "subscription_id": subscription_id,
                "selector": selector,
                "requested_ip": requested_ip,
                "actor_id": actor_id,
            }
        )
        return requested_ip

    monkeypatch.setattr(
        web_catalog_subscriptions_service,
        "replace_subscription_ipv4_with_owner",
        fake_replace,
    )
    form = FormData(
        [
            ("ipv4_block_ids", "block-1"),
            ("ipv4_addresses", "160.119.125.5"),
            ("ip_addon_id", "must-not-be-read"),
            ("billing_mode", "must-not-be-read"),
        ]
    )

    redirect_url = workflow_service.handle_subscription_ipv4_replacement(
        db_session,
        subscription_id=str(subscription.id),
        form=form,
        actor_id="secondary-admin",
    )

    assert captured == {
        "subscription_id": str(subscription.id),
        "selector": "block-1",
        "requested_ip": "160.119.125.5",
        "actor_id": "secondary-admin",
    }
    assert "/edit?notice=" in redirect_url
    assert "billing+unchanged" in redirect_url


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


def test_generate_invoice_from_subscription_error_renders_invoice_list_contract(
    monkeypatch,
):
    captured: dict[str, object] = {}
    request = SimpleNamespace(state=SimpleNamespace(auth={"permissions": []}))
    list_state = {
        "list_query": SimpleNamespace(page=1),
        "invoices": [],
        "page_meta": SimpleNamespace(),
        "invoice_status_presentations": {},
    }
    bulk_contract = {"selection_enabled": False, "actions": []}

    monkeypatch.setattr(
        billing_invoices_web.web_billing_invoices_service,
        "generate_invoice_from_subscription_web",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTTPException(status_code=400, detail="Prepaid subscriptions use review.")
        ),
    )
    monkeypatch.setattr(
        billing_invoices_web,
        "_actor_id",
        lambda _request: "staff-1",
    )
    monkeypatch.setattr(
        billing_invoices_web.web_billing_overview_service,
        "build_invoice_list_query",
        lambda **_kwargs: list_state["list_query"],
    )
    monkeypatch.setattr(
        billing_invoices_web.web_billing_overview_service,
        "build_invoices_list_data",
        lambda _db, *, list_query: list_state,
    )
    monkeypatch.setattr(
        billing_invoices_web.web_billing_invoice_bulk_actions_service,
        "build_invoice_bulk_action_contract",
        lambda _db, *, auth, invoices: bulk_contract,
    )
    monkeypatch.setattr(
        "app.web.admin.get_current_user",
        lambda _request: {"actor_id": "staff-1"},
    )
    monkeypatch.setattr("app.web.admin.get_sidebar_stats", lambda _db: {})

    def capture_template(name, context, *, status_code=200):
        captured["name"] = name
        captured["context"] = context
        captured["status_code"] = status_code
        return captured

    monkeypatch.setattr(
        billing_invoices_web.templates,
        "TemplateResponse",
        capture_template,
    )

    result = billing_invoices_web.invoice_generate_from_subscription(
        request,
        subscriber_id="subscriber-1",
        subscription_id="subscription-1",
        db=object(),
    )

    context = result["context"]
    assert result["name"] == "admin/billing/invoices.html"
    assert result["status_code"] == 400
    assert context["error"] == "Prepaid subscriptions use review."
    assert context["invoice_bulk_action_contract"] == bulk_contract
    assert context["invoices"] == []
