"""Shared customer-link semantics for support tickets."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, false, or_
from sqlalchemy.sql.elements import ColumnElement

CUSTOMER_TICKET_LINK_FIELDS = (
    "subscriber_id",
    "customer_account_id",
    "customer_person_id",
)


def ticket_customer_link_filter(
    ticket_model, customer_id: object
) -> ColumnElement[bool]:
    """SQL predicate for tickets linked to a customer through any customer field."""
    if customer_id is None:
        return false()
    return or_(
        ticket_model.subscriber_id == customer_id,
        ticket_model.customer_account_id == customer_id,
        ticket_model.customer_person_id == customer_id,
    )


def ticket_customer_any_link_filter(
    ticket_model, customer_ids: Iterable[object | None]
) -> ColumnElement[bool]:
    """SQL predicate for tickets linked to any of the supplied customer IDs."""
    ids = _unique_non_null(customer_ids)
    if not ids:
        return false()
    if len(ids) == 1:
        return ticket_customer_link_filter(ticket_model, ids[0])
    return or_(*(ticket_customer_link_filter(ticket_model, item) for item in ids))


def ticket_unlinked_customer_filter(ticket_model) -> ColumnElement[bool]:
    """SQL predicate for tickets with no subscriber/customer link."""
    return and_(
        ticket_model.subscriber_id.is_(None),
        ticket_model.customer_account_id.is_(None),
        ticket_model.customer_person_id.is_(None),
    )


def ticket_customer_linked_ids(ticket) -> tuple[object, ...]:
    """Unique customer IDs referenced by a ticket, preserving field order."""
    return tuple(
        _unique_non_null(
            getattr(ticket, field, None) for field in CUSTOMER_TICKET_LINK_FIELDS
        )
    )


def ticket_primary_customer_id(ticket) -> object | None:
    ids = ticket_customer_linked_ids(ticket)
    return ids[0] if ids else None


def ticket_has_customer_link(ticket) -> bool:
    return bool(ticket_customer_linked_ids(ticket))


def ticket_links_customer(ticket, customer_id: object | None) -> bool:
    if customer_id is None:
        return False
    customer_key = str(customer_id)
    return any(
        str(linked_id) == customer_key
        for linked_id in ticket_customer_linked_ids(ticket)
    )


def _unique_non_null(values: Iterable[object | None]) -> list[object]:
    seen: set[str] = set()
    result: list[object] = []
    for value in values:
        if value is None:
            continue
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
