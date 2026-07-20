from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"


def _attribute_name(node: ast.expr) -> str | None:
    return node.attr if isinstance(node, ast.Attribute) else None


def test_lifecycle_status_assignments_are_owned_by_account_lifecycle():
    violations: list[str] = []
    for path in APP.rglob("*.py"):
        if path == APP / "services" / "account_lifecycle.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if not isinstance(value, ast.Attribute):
                continue
            owner = value.value
            if not isinstance(owner, ast.Name) or owner.id not in {
                "SubscriberStatus",
                "SubscriptionStatus",
            }:
                continue
            if any(_attribute_name(target) == "status" for target in targets):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_campaign_and_inbox_write_only_to_communication_outbox():
    prohibited = (
        "email_service.send_email",
        "email_service.send_email_with_config",
        "sms_service.send_sms",
        "push_service.send_push",
        "whatsapp_connector.send_text_message",
        "whatsapp_connector.send_template_message",
        "team_inbox_outbound.send_inbox_reply",
    )
    for relative in (
        "app/services/comms_campaigns.py",
        "app/services/team_inbox_outbound.py",
    ):
        source = (ROOT / relative).read_text()
        found = [name for name in prohibited if name in source]
        assert found == [], f"{relative} bypasses the outbox: {found}"


def test_campaign_requests_canonical_delivery_without_owning_smtp_credentials():
    source = (APP / "services" / "comms_campaigns.py").read_text()
    model_source = (APP / "models" / "comms_campaign.py").read_text()
    assert "CommunicationIntent(" in source
    assert "submit(" in source
    assert "CampaignSmtpConfig" not in model_source


def test_notification_owner_wraps_unowned_customer_deliveries_in_intents():
    source = (APP / "services" / "notification.py").read_text()
    assert "if payload.communication_intent_id is not None" in source
    assert "CommunicationIntent(" in source
    assert "submit(" in source


def test_crm_customer_status_is_observed_not_authoritative():
    source = (APP / "services" / "crm_customers.py").read_text()
    assert 'merged["crm_reported_status"]' in source
    assert "subscriber.status = status_value" not in source
