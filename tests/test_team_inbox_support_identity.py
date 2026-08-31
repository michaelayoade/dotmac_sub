from types import SimpleNamespace
from uuid import uuid4

from app.services.team_inbox_support_identity import (
    CustomerIdentifierKind,
    CustomerIdentityQuery,
    CustomerIdentityStatus,
    SupportReadContext,
    resolve_customer_identity,
)


class _Db:
    def __init__(self, conversation, subscriber):
        self.conversation = conversation
        self.subscriber = subscriber

    def get(self, model, identifier):
        if identifier == self.conversation.id:
            return self.conversation
        if identifier == self.subscriber.id:
            return self.subscriber
        return None


def test_identity_requires_trusted_permission_and_exact_linked_identifier():
    subscriber_id = uuid4()
    conversation = SimpleNamespace(id=uuid4(), subscriber_id=subscriber_id)
    subscriber = SimpleNamespace(
        id=subscriber_id,
        display_name="Safe customer",
        account_number="PORTAL-1",
        phone="2348012345678",
        email="customer@example.test",
        status=SimpleNamespace(value="active"),
    )
    db = _Db(conversation, subscriber)
    context = SupportReadContext(conversation.id, None, True)
    found = resolve_customer_identity(
        db,
        CustomerIdentityQuery(
            context, CustomerIdentifierKind.email, "CUSTOMER@example.test"
        ),
    )
    assert found.status is CustomerIdentityStatus.found
    assert found.customer is not None
    denied = resolve_customer_identity(
        db,
        CustomerIdentityQuery(
            SupportReadContext(conversation.id, None, False),
            CustomerIdentifierKind.inbox_linked,
        ),
    )
    assert denied.status is CustomerIdentityStatus.unauthorized


def test_identity_confirms_only_the_linked_subscriber_identifiers():
    subscriber_id = uuid4()
    conversation = SimpleNamespace(id=uuid4(), subscriber_id=subscriber_id)
    subscriber = SimpleNamespace(
        id=subscriber_id,
        display_name="Safe",
        account_number="PORTAL-1",
        phone="2348012345678",
        email="customer@example.test",
        status=SimpleNamespace(value="active"),
    )
    db = _Db(conversation, subscriber)
    context = SupportReadContext(conversation.id, None, True)
    for kind, value in (
        (CustomerIdentifierKind.phone, "2348012345678"),
        (CustomerIdentifierKind.email, "customer@example.test"),
        (CustomerIdentifierKind.account_number, "PORTAL-1"),
        (CustomerIdentifierKind.inbox_linked, None),
    ):
        assert (
            resolve_customer_identity(
                db, CustomerIdentityQuery(context, kind, value)
            ).status
            is CustomerIdentityStatus.found
        )
    # The fake DB deliberately has no search API: an unmatched value cannot
    # reach a second customer.
    assert (
        resolve_customer_identity(
            db,
            CustomerIdentityQuery(
                context, CustomerIdentifierKind.email, "other@example.test"
            ),
        ).status
        is CustomerIdentityStatus.not_found
    )


def test_identity_missing_conversation_is_unavailable_and_output_is_bounded():
    subscriber = SimpleNamespace(id=uuid4())
    conversation = SimpleNamespace(id=uuid4(), subscriber_id=None)
    db = _Db(conversation, subscriber)
    result = resolve_customer_identity(
        db,
        CustomerIdentityQuery(
            SupportReadContext(uuid4(), None, True), CustomerIdentifierKind.inbox_linked
        ),
    )
    assert result.status is CustomerIdentityStatus.unavailable
    assert result.customer is None
