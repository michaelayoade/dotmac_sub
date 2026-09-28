from __future__ import annotations

from app.services.automation_contracts import AutomationCatalogState
from app.services.web_automation_center import build_automation_center_data


def test_support_communications_catalog_explains_center_readiness() -> None:
    data = build_automation_center_data(
        None,  # type: ignore[arg-type]
        can_read_rules=False,
        can_read_runs=False,
        can_create_rules=False,
        can_update_rules=False,
        can_publish_rules=False,
        can_operate_rules=False,
        permission_keys=frozenset(),
    )
    rows = {row.item.key: row for row in data["catalog_items"]}

    assert rows["support.ticket.center_rules"].state == "available"
    assert rows["support.ticket.assignment_rules"].state == "managed_elsewhere"
    assert rows["communications.inbox_automation_rules"].state == "unavailable"
    assert rows["communications.retired_stale_auto_resolution"].state == "retired"
    assert all(row.explanation for row in rows.values())
    assert rows["support.ticket.center_rules"].item.state is (
        AutomationCatalogState.available
    )
