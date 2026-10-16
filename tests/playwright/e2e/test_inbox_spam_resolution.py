"""Real CSRF-protected spam resolution on desktop and phone widths."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

from app.models.team_inbox import InboxConversation, InboxStatusTransitionEvent
from app.services import team_inbox_customer_completion_policy
from tests.playwright.conftest import SessionLocal
from tests.playwright.helpers.config import E2ESettings

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("width", [1440, 390])
def test_unlinked_chat_resolves_as_spam_through_real_form(
    admin_page: Page, settings: E2ESettings, width: int
) -> None:
    with SessionLocal() as db:
        conversation = InboxConversation(
            channel_type="email",
            contact_address=f"spam-{uuid4().hex}@example.test",
            subject="Unsolicited message",
            status="open",
            is_active=True,
            first_message_at=datetime.now(UTC),
            last_message_at=datetime.now(UTC),
            customer_completion_policy_version_id=(
                team_inbox_customer_completion_policy.snapshot_active_policy_id(db)
            ),
        )
        db.add(conversation)
        db.commit()
        conversation_id = conversation.id

    admin_page.set_viewport_size({"width": width, "height": 900})
    admin_page.goto(f"{settings.base_url}/admin/inbox/{conversation_id}")
    thread = admin_page.locator(f'[data-conversation-thread="{conversation_id}"]')
    expect(thread).to_be_visible()
    thread.get_by_role("button", name="Status", exact=True).click()
    spam = thread.get_by_role("menuitem", name="Resolve as spam", exact=True)
    expect(spam).to_be_visible()
    form = spam.locator("..")
    assert form.locator('input[name="_csrf_token"]').input_value()
    spam.click()
    admin_page.wait_for_load_state("domcontentloaded")
    assert "Session Expired" not in admin_page.content()
    with SessionLocal() as db:
        resolved = db.get(InboxConversation, conversation_id)
        assert resolved is not None and resolved.status == "resolved"
        assert resolved.subscriber_id is None
        event = (
            db.query(InboxStatusTransitionEvent)
            .filter_by(
                conversation_id=conversation_id,
                status="resolved",
            )
            .one()
        )
        assert event.resolution_reason == "spam_irrelevant"
