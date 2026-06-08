from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi import Request

from app.services import crm_portal


def _ticket(ticket_id: str, subscriber_id: str):
    return SimpleNamespace(
        id=ticket_id,
        number=f"T-{ticket_id}",
        title="Slow internet",
        description="Please investigate.",
        status="open",
        priority="normal",
        subscriber_id=subscriber_id,
        created_at=None,
        updated_at=None,
    )


def _comment(body: str, *, is_internal: bool = False, author_person_id=None):
    return SimpleNamespace(
        body=body,
        is_internal=is_internal,
        author_person_id=author_person_id,
        created_at=None,
    )


def test_tickets_list_context_skips_unfiltered_fetch_when_mapping_missing(
    monkeypatch,
) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    list_tickets = Mock(return_value=[])
    monkeypatch.setattr("app.services.support.Tickets.list", list_tickets)

    context = crm_portal.tickets_list_context(request, Mock(), customer, ["sub-1"])

    assert context["tickets"] == []
    assert context["crm_error"] is False
    assert context["priority_display"] == crm_portal.TICKET_PRIORITY_DISPLAY
    list_tickets.assert_called_once()


def test_cache_get_returns_none_when_redis_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("app.services.crm_portal.get_session_redis", lambda: None)

    assert crm_portal._cache_get("crm:key") is None


def test_cache_get_handles_redis_errors(monkeypatch) -> None:
    redis_client = Mock()
    redis_client.get.side_effect = RuntimeError("redis down")
    monkeypatch.setattr(
        "app.services.crm_portal.get_session_redis", lambda: redis_client
    )

    assert crm_portal._cache_get("crm:key") is None


def test_cache_set_handles_redis_errors(monkeypatch) -> None:
    redis_client = Mock()
    redis_client.setex.side_effect = RuntimeError("redis down")
    monkeypatch.setattr(
        "app.services.crm_portal.get_session_redis", lambda: redis_client
    )

    crm_portal._cache_set("crm:key", "value", 60)

    redis_client.setex.assert_called_once_with("crm:key", 60, "value")


def test_resolve_crm_subscriber_id_returns_cached_value(monkeypatch) -> None:
    monkeypatch.setattr("app.services.crm_portal._cache_get", lambda _key: "crm-sub-1")

    assert crm_portal.resolve_crm_subscriber_id(Mock(), "sub-1") == "crm-sub-1"


def test_resolve_crm_subscriber_id_returns_none_for_cached_miss(monkeypatch) -> None:
    monkeypatch.setattr("app.services.crm_portal._cache_get", lambda _key: "__none__")

    assert crm_portal.resolve_crm_subscriber_id(Mock(), "sub-1") is None


def test_resolve_crm_subscriber_id_caches_missing_subscriber(monkeypatch) -> None:
    db = Mock()
    db.get.return_value = None
    cache_sets: list[tuple[str, str, int]] = []
    subscriber_id = str(uuid4())

    monkeypatch.setattr("app.services.crm_portal._cache_get", lambda _key: None)
    monkeypatch.setattr(
        "app.services.crm_portal._cache_set",
        lambda key, value, ttl: cache_sets.append((key, value, ttl)),
    )

    assert crm_portal.resolve_crm_subscriber_id(db, subscriber_id) is None
    assert cache_sets == [
        (
            f"crm:sub_map:{subscriber_id}",
            "__none__",
            crm_portal._CACHE_SUBSCRIBER_MAP,
        )
    ]


def test_resolve_crm_subscriber_id_caches_crm_mapping(
    monkeypatch, db_session, subscriber
) -> None:
    subscriber.splynx_customer_id = 321
    db_session.commit()
    cache_sets: list[tuple[str, str, int]] = []

    client = Mock()
    client.resolve_subscriber_id.return_value = "crm-sub-321"

    monkeypatch.setattr("app.services.crm_portal._cache_get", lambda _key: None)
    monkeypatch.setattr(
        "app.services.crm_portal._cache_set",
        lambda key, value, ttl: cache_sets.append((key, value, ttl)),
    )
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    resolved = crm_portal.resolve_crm_subscriber_id(db_session, str(subscriber.id))

    assert resolved == "crm-sub-321"
    assert cache_sets == [
        (
            f"crm:sub_map:{subscriber.id}",
            "crm-sub-321",
            crm_portal._CACHE_SUBSCRIBER_MAP,
        )
    ]


def test_resolve_crm_subscriber_id_uses_short_ttl_for_lookup_miss(
    monkeypatch, db_session, subscriber
) -> None:
    subscriber.splynx_customer_id = 654
    db_session.commit()
    cache_sets: list[tuple[str, str, int]] = []

    client = Mock()
    client.resolve_subscriber_id.return_value = None

    monkeypatch.setattr("app.services.crm_portal._cache_get", lambda _key: None)
    monkeypatch.setattr(
        "app.services.crm_portal._cache_set",
        lambda key, value, ttl: cache_sets.append((key, value, ttl)),
    )
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    resolved = crm_portal.resolve_crm_subscriber_id(db_session, str(subscriber.id))

    assert resolved is None
    assert cache_sets == [(f"crm:sub_map:{subscriber.id}", "__none__", 300)]


def test_resolve_crm_subscriber_ids_deduplicates_and_skips_blanks(monkeypatch) -> None:
    mappings = {"sub-1": "crm-1", "sub-2": "crm-1", "sub-3": "crm-3"}
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda _db, subscriber_id: mappings.get(subscriber_id),
    )

    resolved = crm_portal.resolve_crm_subscriber_ids(
        Mock(),
        ["sub-1", " ", "sub-2", None, "sub-3"],
    )

    assert resolved == ["crm-1", "crm-3"]


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

    comments = Mock()
    monkeypatch.setattr(
        "app.services.support.Tickets.get",
        lambda *_args, **_kwargs: _ticket("ticket-1", "sub-2"),
    )
    monkeypatch.setattr("app.services.support.TicketComments.list", comments)

    context = crm_portal.ticket_detail_context(
        request, Mock(), customer, ["sub-1"], "ticket-1"
    )

    assert context["ticket"] is None
    assert context["crm_error"] is True
    assert context["crm_error_message"] == "Ticket not found."
    comments.assert_not_called()


def test_handle_ticket_comment_rejects_ticket_with_wrong_subscriber(
    monkeypatch,
) -> None:
    create_comment = Mock()
    monkeypatch.setattr(
        "app.services.support.Tickets.get",
        lambda *_args, **_kwargs: _ticket("ticket-1", "sub-2"),
    )
    monkeypatch.setattr("app.services.support.TicketComments.create", create_comment)

    result = crm_portal.handle_ticket_comment(
        Mock(),
        {"current_user": {"name": "Customer One"}},
        ["sub-1"],
        "ticket-1",
        "Please update me.",
    )

    assert result == {"success": False, "error": "Ticket not found."}
    create_comment.assert_not_called()


def test_ticket_detail_context_filters_internal_comments(monkeypatch) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    monkeypatch.setattr(
        "app.services.support.Tickets.get",
        lambda *_args, **_kwargs: _ticket("ticket-1", "sub-1"),
    )
    monkeypatch.setattr(
        "app.services.support.TicketComments.list",
        lambda *_args, **_kwargs: [
            _comment("Visible"),
            _comment("Hidden", is_internal=True),
        ],
    )

    context = crm_portal.ticket_detail_context(
        request, Mock(), customer, ["sub-1"], "ticket-1"
    )

    assert context["ticket"]["id"] == "ticket-1"
    assert [comment["body"] for comment in context["comments"]] == ["Visible"]
    assert context["status_display"] == crm_portal.TICKET_STATUS_DISPLAY


def test_tickets_list_context_returns_error_context_on_local_failure(
    monkeypatch,
) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    monkeypatch.setattr(
        "app.services.support.Tickets.list",
        Mock(side_effect=RuntimeError("unavailable")),
    )

    context = crm_portal.tickets_list_context(request, Mock(), customer, ["sub-1"])

    assert context["tickets"] == []
    assert context["crm_error"] is True
    assert context["status_display"] == crm_portal.TICKET_STATUS_DISPLAY


def test_ticket_detail_context_returns_error_context_on_local_failure(
    monkeypatch,
) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    monkeypatch.setattr(
        "app.services.support.Tickets.get",
        Mock(side_effect=RuntimeError("unavailable")),
    )

    context = crm_portal.ticket_detail_context(
        request, Mock(), customer, ["sub-1"], "ticket-1"
    )

    assert context["ticket"] is None
    assert context["crm_error"] is True
    assert context["crm_error_message"] == "Ticket not found."


def test_ticket_create_context_exposes_priority_choices() -> None:
    context = crm_portal.ticket_create_context(Mock(spec=Request), {"id": "cust-1"})

    assert context["active_page"] == "support"
    assert context["priorities"] == list(crm_portal.TICKET_PRIORITY_DISPLAY.keys())


def test_handle_ticket_create_normalizes_unknown_priority(monkeypatch) -> None:
    subscriber_id = str(uuid4())
    create_ticket = Mock(return_value=_ticket("ticket-1", subscriber_id))
    monkeypatch.setattr("app.services.support.Tickets.create", create_ticket)

    result = crm_portal.handle_ticket_create(
        Mock(),
        {"current_user": {"name": "Customer One"}},
        subscriber_id,
        "Slow internet",
        "Please investigate.",
        "not-a-priority",
    )

    assert result["success"] is True
    assert result["ticket"]["id"] == "ticket-1"
    payload = create_ticket.call_args.args[1]
    assert payload.priority == "normal"


def test_handle_ticket_create_returns_link_error_without_crm_mapping(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: None,
    )

    result = crm_portal.handle_ticket_create(
        Mock(),
        {"current_user": {"name": "Customer One"}},
        "sub-1",
        "Slow internet",
        "",
        "normal",
    )

    assert result == {
        "success": False,
        "error": "Unable to link your account to the support system.",
    }


def test_handle_ticket_create_returns_error_on_local_failure(monkeypatch) -> None:
    subscriber_id = str(uuid4())
    monkeypatch.setattr(
        "app.services.support.Tickets.create",
        Mock(side_effect=RuntimeError("down")),
    )
    db = Mock()

    result = crm_portal.handle_ticket_create(
        db,
        {"current_user": {"name": "Customer One"}},
        subscriber_id,
        "Slow internet",
        "",
        "normal",
    )

    assert result == {
        "success": False,
        "error": "Unable to create ticket. Please try again later.",
    }
    db.rollback.assert_called_once()


def test_handle_ticket_comment_returns_not_found_without_mapped_subscribers(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: None,
    )

    result = crm_portal.handle_ticket_comment(
        Mock(),
        {"current_user": {"name": "Customer One"}},
        ["sub-1"],
        "ticket-1",
        "Please update me.",
    )

    assert result == {"success": False, "error": "Ticket not found."}


def test_handle_ticket_comment_success_uses_default_author_name(monkeypatch) -> None:
    create_comment = Mock()
    db = Mock()
    monkeypatch.setattr(
        "app.services.support.Tickets.get",
        lambda *_args, **_kwargs: _ticket("ticket-1", "sub-1"),
    )
    monkeypatch.setattr("app.services.support.TicketComments.create", create_comment)

    result = crm_portal.handle_ticket_comment(
        db,
        {},
        ["sub-1"],
        "ticket-1",
        "Please update me.",
    )

    assert result == {"success": True}
    payload = create_comment.call_args.kwargs["payload"]
    assert payload.body == "Please update me."
    db.commit.assert_called_once()


def test_handle_ticket_comment_returns_error_on_local_failure(monkeypatch) -> None:
    db = Mock()
    monkeypatch.setattr(
        "app.services.support.Tickets.get",
        lambda *_args, **_kwargs: _ticket("ticket-1", "sub-1"),
    )
    monkeypatch.setattr(
        "app.services.support.TicketComments.create",
        Mock(side_effect=RuntimeError("down")),
    )

    result = crm_portal.handle_ticket_comment(
        db,
        {"current_user": {"name": "Customer One"}},
        ["sub-1"],
        "ticket-1",
        "Please update me.",
    )

    assert result == {
        "success": False,
        "error": "Unable to add comment. Please try again later.",
    }
    db.rollback.assert_called_once()


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

    list_tickets = Mock(
        side_effect=[
            [_ticket("t-1", "sub-1")],
            [_ticket("t-2", "sub-2")],
        ]
    )
    monkeypatch.setattr("app.services.support.Tickets.list", list_tickets)

    context = crm_portal.tickets_list_context(
        request, Mock(), customer, ["sub-1", "sub-2"]
    )

    assert [ticket["id"] for ticket in context["tickets"]] == ["t-1", "t-2"]


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


def test_work_orders_list_context_returns_error_context_on_crm_failure(
    monkeypatch,
) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: "crm-sub-1",
    )

    client = Mock()
    client.list_work_orders.side_effect = crm_portal.CRMClientError("unavailable")
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    context = crm_portal.work_orders_list_context(request, Mock(), customer, ["sub-1"])

    assert context["work_orders"] == []
    assert context["crm_error"] is True
    assert context["type_display"] == crm_portal.WORK_ORDER_TYPE_DISPLAY


def test_work_order_detail_context_returns_not_found_without_mapping(
    monkeypatch,
) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: None,
    )

    context = crm_portal.work_order_detail_context(
        request, Mock(), customer, ["sub-1"], "wo-1"
    )

    assert context["work_order"] is None
    assert context["crm_error_message"] == "Work order not found."


def test_work_order_detail_context_rejects_work_order_with_wrong_subscriber(
    monkeypatch,
) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: "crm-sub-1",
    )

    client = Mock()
    client.get_work_order.return_value = {"id": "wo-1", "subscriber_id": "crm-sub-2"}
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    context = crm_portal.work_order_detail_context(
        request, Mock(), customer, ["sub-1"], "wo-1"
    )

    assert context["work_order"] is None
    assert context["crm_error"] is True
    assert context["crm_error_message"] == "Work order not found."
    client.list_work_order_notes.assert_not_called()


def test_work_order_detail_context_returns_error_context_on_crm_failure(
    monkeypatch,
) -> None:
    request = Mock(spec=Request)
    customer = {"id": "cust-1"}

    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: "crm-sub-1",
    )

    client = Mock()
    client.get_work_order.side_effect = crm_portal.CRMClientError("down")
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    context = crm_portal.work_order_detail_context(
        request, Mock(), customer, ["sub-1"], "wo-1"
    )

    assert context["work_order"] is None
    assert context["crm_error"] is True


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


def test_reseller_account_tickets_context_returns_tickets(monkeypatch) -> None:
    request = Mock(spec=Request)

    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: "crm-sub-1",
    )

    client = Mock()
    client.list_tickets.return_value = [{"id": "ticket-1"}]
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    context = crm_portal.reseller_account_tickets_context(
        request,
        Mock(),
        "acct-1",
        current_user={"id": "user-1"},
        reseller={"id": "reseller-1"},
    )

    assert context["tickets"] == [{"id": "ticket-1"}]
    assert context["crm_error"] is False


def test_reseller_account_tickets_context_returns_error_context_on_crm_failure(
    monkeypatch,
) -> None:
    request = Mock(spec=Request)

    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: "crm-sub-1",
    )

    client = Mock()
    client.list_tickets.side_effect = crm_portal.CRMClientError("down")
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    context = crm_portal.reseller_account_tickets_context(
        request,
        Mock(),
        "acct-1",
        current_user={"id": "user-1"},
        reseller={"id": "reseller-1"},
    )

    assert context["tickets"] == []
    assert context["crm_error"] is True


def test_reseller_open_tickets_count_counts_only_open_statuses(monkeypatch) -> None:
    mappings = {"acct-1": "crm-1", "acct-2": "crm-2"}
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda _db, account_id: mappings.get(account_id),
    )

    client = Mock()
    client.list_tickets.side_effect = [
        [
            {"status": "open"},
            {"status": "closed"},
            {"status": "waiting_on_agent"},
        ],
        [
            {"status": "in_progress"},
            {"status": "resolved"},
        ],
    ]
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    count = crm_portal.reseller_open_tickets_count(
        Mock(), "reseller-1", ["acct-1", "acct-2"]
    )

    assert count == 3


def test_reseller_open_tickets_count_returns_zero_on_crm_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.crm_portal.resolve_crm_subscriber_id",
        lambda *_args, **_kwargs: "crm-1",
    )

    client = Mock()
    client.list_tickets.side_effect = crm_portal.CRMClientError("down")
    monkeypatch.setattr("app.services.crm_portal.get_crm_client", lambda: client)

    count = crm_portal.reseller_open_tickets_count(Mock(), "reseller-1", ["acct-1"])

    assert count == 0
