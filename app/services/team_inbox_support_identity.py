"""Bounded, trusted customer-identity projection for support consumers.

This is a Team Inbox read boundary.  Consumers (including future AI orchestration)
receive DTOs only; they cannot select a tenant, ORM model, or arbitrary filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.team_inbox import InboxConversation


class CustomerIdentifierKind(StrEnum):
    inbox_linked = "inbox_linked"
    phone = "phone"
    email = "email"
    account_number = "account_number"


class CustomerIdentityStatus(StrEnum):
    found = "found"
    not_found = "not_found"
    ambiguous = "ambiguous"
    unauthorized = "unauthorized"
    unavailable = "unavailable"


@dataclass(frozen=True, slots=True)
class SupportReadContext:
    """Application-authenticated scope; never constructed from model output."""

    conversation_id: UUID
    actor_person_id: UUID | None
    can_read_support_context: bool


@dataclass(frozen=True, slots=True)
class CustomerIdentityQuery:
    context: SupportReadContext
    identifier_kind: CustomerIdentifierKind
    identifier_value: str | None = None


@dataclass(frozen=True, slots=True)
class CustomerIdentityProjection:
    subscriber_id: UUID
    display_name: str | None
    account_number: str | None
    status: str


@dataclass(frozen=True, slots=True)
class CustomerIdentityResult:
    status: CustomerIdentityStatus
    customer: CustomerIdentityProjection | None = None


def _normalized(value: str | None) -> str | None:
    value = str(value or "").strip()
    return value.lower() if value else None


def resolve_customer_identity(
    db: Session, query: CustomerIdentityQuery
) -> CustomerIdentityResult:
    """Resolve only a conversation-linked customer or a verified exact identifier.

    The conversation is the trusted scope: an identifier may resolve only to the
    subscriber already linked to that conversation.  This deliberately avoids a
    cross-customer directory/search API while preserving exact verification.
    """
    if not query.context.can_read_support_context:
        return CustomerIdentityResult(CustomerIdentityStatus.unauthorized)
    conversation = db.get(InboxConversation, query.context.conversation_id)
    if conversation is None:
        return CustomerIdentityResult(CustomerIdentityStatus.unavailable)
    if conversation.subscriber_id is None:
        return CustomerIdentityResult(CustomerIdentityStatus.not_found)
    subscriber = db.get(Subscriber, conversation.subscriber_id)
    if subscriber is None:
        return CustomerIdentityResult(CustomerIdentityStatus.unavailable)
    if query.identifier_kind is CustomerIdentifierKind.inbox_linked:
        matched = True
    else:
        value = _normalized(query.identifier_value)
        if value is None:
            return CustomerIdentityResult(CustomerIdentityStatus.not_found)
        fields = {
            CustomerIdentifierKind.phone: _normalized(subscriber.phone),
            CustomerIdentifierKind.email: _normalized(subscriber.email),
            CustomerIdentifierKind.account_number: _normalized(
                subscriber.account_number
            ),
        }
        matched = fields.get(query.identifier_kind) == value
    if not matched:
        return CustomerIdentityResult(CustomerIdentityStatus.not_found)
    return CustomerIdentityResult(
        CustomerIdentityStatus.found,
        CustomerIdentityProjection(
            subscriber_id=subscriber.id,
            display_name=subscriber.display_name,
            account_number=subscriber.account_number,
            status=str(subscriber.status.value),
        ),
    )
