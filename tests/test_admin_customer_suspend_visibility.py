from pathlib import Path
from types import SimpleNamespace

from app.web.admin import customers


def _request_with_roles(*roles: str):
    return SimpleNamespace(state=SimpleNamespace(auth={"roles": list(roles)}))


def test_technical_support_role_gets_customer_dropdown_suspend_context(monkeypatch):
    monkeypatch.setattr(customers, "has_permission", lambda auth, db, key: False)

    context = customers._subscription_action_permission_context(
        _request_with_roles("Technical support"),
        db=None,
    )

    assert context["can_tech_support_suspend_subscriptions"] is True
    assert context["can_suspend_subscriptions"] is False


def test_subscription_suspend_permission_does_not_render_tech_support_button(
    monkeypatch,
):
    monkeypatch.setattr(
        customers,
        "has_permission",
        lambda auth, db, key: key == "subscription:suspend",
    )

    context = customers._subscription_action_permission_context(
        _request_with_roles("billing"),
        db=None,
    )

    assert context["can_suspend_subscriptions"] is True
    assert context["can_tech_support_suspend_subscriptions"] is False


def test_customer_table_suspend_button_uses_tech_support_visibility_flag():
    template = Path("templates/admin/customers/_table.html").read_text()

    assert "can_tech_support_suspend_subscriptions and customer.active_subscription_count" in template
    assert "can_suspend_subscriptions and customer.active_subscription_count" not in template
