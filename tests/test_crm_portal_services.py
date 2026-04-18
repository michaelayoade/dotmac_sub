from __future__ import annotations

from unittest.mock import Mock

from fastapi import Request

from app.services import crm_portal


def test_tickets_list_context_skips_unfiltered_fetch_when_mapping_missing(
    monkeypatch,
) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: None,
    )

    client = Mock()
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    context = crm_portal.tickets_list_context(request, Mock(), customer, ["sub-1"])

    assert context["tickets"] == []
    assert context["crm_error"] is False
    assert context["priority_display"] == crm_portal.TICKET_PRIORITY_DISPLAY
    client.list_tickets.assert_not_called()


def test_ticket_detail_context_rejects_access_when_mapping_missing(monkeypatch) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: None,
    )

    client = Mock()
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    context = crm_portal.ticket_detail_context(
        request, Mock(), customer, ["sub-1"], "ticket-1"
    )

    assert context["ticket"] is None
    assert context["crm_error"] is True
    assert context["crm_error_message"] == "Ticket not found."
    client.get_ticket.assert_not_called()


def test_ticket_detail_context_rejects_ticket_with_wrong_subscriber(
    monkeypatch,
) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: "crm-sub-1",
    )

    client = Mock()
    client.get_ticket.return_value = {"id": "ticket-1", "subscriber_id": "crm-sub-2"}
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    context = crm_portal.ticket_detail_context(
        request, Mock(), customer, ["sub-1"], "ticket-1"
    )

    assert context["ticket"] is None
    assert context["crm_error"] is True
    assert context["crm_error_message"] == "Ticket not found."
    client.list_ticket_comments.assert_not_called()


def test_handle_ticket_comment_rejects_ticket_with_wrong_subscriber(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: "crm-sub-1",
    )

    client = Mock()
    client.get_ticket.return_value = {"id": "ticket-1", "subscriber_id": "crm-sub-2"}
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    result = crm_portal.handle_ticket_comment(
        Mock(),
        {"current_user": {"name": "Customer One"}},
        ["sub-1"],
        "ticket-1",
        "Please update me.",
    )

    assert result == {"success": False, "error": "Ticket not found."}
    client.create_ticket_comment.assert_not_called()


def test_work_orders_list_context_skips_unfiltered_fetch_when_mapping_missing(
    monkeypatch,
) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: None,
    )

    client = Mock()
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    context = crm_portal.work_orders_list_context(request, Mock(), customer, ["sub-1"])

    assert context["work_orders"] == []
    assert context["crm_error"] is False
    client.list_work_orders.assert_not_called()


def test_tickets_list_context_merges_multiple_allowed_accounts(monkeypatch) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    mappings = {"sub-1": "crm-1", "sub-2": "crm-2"}
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda _db, subscriber_id: mappings.get(subscriber_id),
    )

    client = Mock()
    client.list_tickets.side_effect = [
        [{"id": "t-1", "updated_at": "2026-03-18T10:00:00"}],
        [{"id": "t-2", "updated_at": "2026-03-18T11:00:00"}],
    ]
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    context = crm_portal.tickets_list_context(
        request, Mock(), customer, ["sub-1", "sub-2"]
    )

    assert [ticket["id"] for ticket in context["tickets"]] == ["t-2", "t-1"]


def test_work_orders_list_context_merges_multiple_allowed_accounts(monkeypatch) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    mappings = {"sub-1": "crm-1", "sub-2": "crm-2"}
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda _db, subscriber_id: mappings.get(subscriber_id),
    )

    client = Mock()
    client.list_work_orders.side_effect = [
        [{"id": "wo-1", "updated_at": "2026-03-18T10:00:00"}],
        [{"id": "wo-2", "updated_at": "2026-03-18T11:00:00"}],
    ]
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    context = crm_portal.work_orders_list_context(
        request, Mock(), customer, ["sub-1", "sub-2"]
    )

    assert [work_order["id"] for work_order in context["work_orders"]] == [
        "wo-2",
        "wo-1",
    ]


def test_reseller_account_tickets_context_skips_unfiltered_fetch_when_mapping_missing(
    monkeypatch,
) -> None:
    request = Mock(spec=Request)

    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: None,
    )

    client = Mock()
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    context = crm_portal.reseller_account_tickets_context(
        request,
        Mock(),
        "acct-1",
        current_user={"id": "user-1"},
        reseller={"id": "reseller-1"},
    )

    assert context["tickets"] == []
    assert context["crm_error"] is False
    client.list_tickets.assert_not_called()
