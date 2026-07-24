"""Customer 360 Communications: a customer-scoped read of the inbox.

`communications.team_inbox` stays the owner. The customer record projects its
conversations read-only; acting on one still means opening the workspace.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5, slice 3.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.models.team_inbox import InboxConversation, InboxConversationStatus
from app.services import team_inbox_read


def _conversation(db_session, *, subscriber_id, subject: str, channel="whatsapp"):
    conversation = InboxConversation(
        channel_type=channel,
        subject=subject,
        contact_address="+2348000000000",
        status=InboxConversationStatus.open.value,
        subscriber_id=subscriber_id,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.commit()
    return conversation.id


def test_list_conversations_scopes_to_subscriber(db_session):
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    kept = _conversation(db_session, subscriber_id=mine, subject="Mine")
    _conversation(db_session, subscriber_id=theirs, subject="Theirs")

    result = team_inbox_read.list_conversations(db_session, subscriber_id=mine)

    assert [row.id for row in result.rows] == [str(kept)]
    assert result.count == 1


def test_unscoped_read_is_unchanged(db_session):
    """The filter is additive — omitting it must not narrow the queue."""
    _conversation(db_session, subscriber_id=uuid.uuid4(), subject="A")
    _conversation(db_session, subscriber_id=uuid.uuid4(), subject="B")

    assert team_inbox_read.list_conversations(db_session).count == 2


def test_subscriber_filter_ignores_unlinked_conversations(db_session):
    """An unresolved conversation has no subscriber and must not leak."""
    _conversation(db_session, subscriber_id=None, subject="Unresolved")
    mine = uuid.uuid4()
    _conversation(db_session, subscriber_id=mine, subject="Mine")

    result = team_inbox_read.list_conversations(db_session, subscriber_id=mine)
    assert result.count == 1
    assert result.rows[0].subject == "Mine"


def test_customer_detail_renders_communications_behind_permission():
    detail = Path("templates/admin/customers/detail.html").read_text()
    assert "can_view_conversations" in detail
    assert "customer_conversations" in detail
    assert "Communications" in detail

    routes = Path("app/web/admin/customers.py").read_text()
    # Same gate as the inbox workspace itself.
    assert 'has_permission(\n        auth, db, "support:ticket:read"\n    )' in routes
    assert "subscriber_id=customer.id" in routes
