"""Pin payment-proof review alerts to the staff-notification owner."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import sot_relationships

ROOT = Path(__file__).resolve().parents[2]


def _calls(path: Path, owner_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == owner_name
    }


def test_payment_proof_owner_requests_and_resolves_staff_review_notifications() -> None:
    path = ROOT / "app/services/payment_proofs.py"
    calls = _calls(path, "staff_notifications")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    referenced_names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }

    assert "queue_permission_review_request" in calls
    assert "resolve_permission_review_request" in calls
    assert "AdminAlert" not in referenced_names
    assert "AdminNotification" not in referenced_names


def test_staff_notification_owner_is_registered_for_permission_targeting() -> None:
    owner = sot_relationships.owning_service_for(
        "permission-targeted staff notification audience resolution"
    )

    assert owner is not None
    assert owner.name == "communications.staff_notifications"
    assert owner.module == "app.services.staff_notifications"


def test_admin_inbox_links_use_the_user_scoped_open_boundary() -> None:
    template = (ROOT / "templates/admin/partials/notifications_menu.html").read_text(
        encoding="utf-8"
    )

    assert "/admin/notifications/inbox/{{ notification.id }}/open" in template
    assert "/admin/alerts/notifications/{{ notification.id }}/open" not in template


def test_payment_proof_escalation_is_keyed_to_the_generic_sla_owner() -> None:
    payment_source = (ROOT / "app/services/payment_proofs.py").read_text(
        encoding="utf-8"
    )
    staff_source = (ROOT / "app/services/staff_notifications.py").read_text(
        encoding="utf-8"
    )
    sla_ui_source = (ROOT / "app/services/web_notifications_sla_policies.py").read_text(
        encoding="utf-8"
    )

    assert 'sla_trigger="payment_proof.review_requested"' in payment_source
    assert "operational_escalation.matching_policies" in staff_source
    assert (
        "NotificationChannel.email"
        not in staff_source.split("def queue_permission_review_request", 1)[1].split(
            "def resolve_permission_review_request", 1
        )[0]
    )
    assert "OPERATIONAL_ENTITY_TYPES" in sla_ui_source
