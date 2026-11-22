"""Infrastructure form creation must not bypass duplicate evidence review."""

from __future__ import annotations

import inspect

from app.services import web_support_tickets


def test_infrastructure_duplicate_check_precedes_both_creation_paths() -> None:
    source = inspect.getsource(web_support_tickets.create_ticket_from_form)
    check = source.index("find_infrastructure_ticket_duplicates(")
    manual = source.index("create_infrastructure_manual(")
    asset = source.index("create_ticket_for_infrastructure_asset(")
    assert check < manual < asset
    assert "if not command.duplicate_override:" in source[check:manual]
    assert 'metadata["possible_duplicate_tickets"]' in source[check:manual]
