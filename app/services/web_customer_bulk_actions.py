"""Server-owned bulk interaction projection for the admin customer list."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.auth_dependencies import has_permission
from app.services.bulk_actions import (
    BulkActionDefinition,
    BulkResourceDefinition,
)

CUSTOMER_BULK_ACTION_DEFINITION = BulkResourceDefinition(
    key="customers",
    filtered_selection_supported=True,
    actions=(
        BulkActionDefinition(
            key="update",
            label="Update",
            description="Modify supported fields for the selected customer scope.",
            permission="customer:write",
            tone="warning",
        ),
        BulkActionDefinition(
            key="send_message",
            label="Send message",
            description="Queue a template-based notification for the selected scope.",
            permission="customer:write",
            tone="info",
            execution_mode="queued",
            result_reference="notification_ids",
        ),
    ),
)


def build_customer_bulk_action_contract(
    db: Session, *, auth: dict
) -> dict[str, object]:
    """Project only actions the current principal may execute."""

    declared_permissions = {
        action.permission for action in CUSTOMER_BULK_ACTION_DEFINITION.actions
    }
    authorized_permissions = {
        permission
        for permission in declared_permissions
        if auth and has_permission(auth, db, permission)
    }
    return CUSTOMER_BULK_ACTION_DEFINITION.project(
        authorized_permissions=authorized_permissions
    ).as_dict()
