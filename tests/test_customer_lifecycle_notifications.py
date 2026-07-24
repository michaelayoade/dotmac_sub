"""Customer-facing lifecycle notifications: expiry warning + ticket movement."""

from app.models.domain_settings import SettingDomain
from app.models.notification import NotificationChannel
from app.models.support import TicketCommentAuthorType, TicketStatus
from app.services import scheduler_config, settings_spec
from app.services.events.handlers.notification import (
    EVENT_NOTIFICATION_SPECS,
    event_catalogue,
)
from app.services.events.types import EventType

# --- the expiry reminder is actually scheduled -----------------------------


def test_expiry_reminder_task_is_scheduled():
    """The task and its days-before setting existed; the beat entry did not.

    Without this entry subscriptions expire and suspend on schedule while the
    customer is never warned.
    """
    source = scheduler_config.__file__
    with open(source) as handle:
        body = handle.read()

    assert "app.tasks.catalog.send_expiry_reminders" in body
    assert "subscription_expiry_reminder_runner" in body


def test_expiry_reminder_interval_setting_is_registered():
    spec = settings_spec.get_spec(
        SettingDomain.catalog, "expiry_reminder_interval_seconds"
    )
    assert spec is not None
    assert spec.default == 86400
    assert spec.min_value == 3600


def test_expiry_reminder_runs_before_the_expiration_runner_can_cut_service():
    """Both run daily; the warning must not be rarer than the cut."""
    reminder = settings_spec.get_spec(
        SettingDomain.catalog, "expiry_reminder_interval_seconds"
    )
    expiration = settings_spec.get_spec(
        SettingDomain.catalog, "subscription_expiration_interval_seconds"
    )
    assert reminder is not None and expiration is not None
    assert reminder.default <= expiration.default


# --- ticket acknowledgement -------------------------------------------------


def test_customer_ticket_created_has_a_notification_spec():
    """The portal emitted this event; nothing consumed it, so raising a
    ticket produced silence."""
    spec = EVENT_NOTIFICATION_SPECS.get(EventType.customer_ticket_created)
    assert spec is not None
    assert spec.category == "support"
    assert "{ticket_number}" in spec.subject


def test_ticket_acknowledgement_is_channel_configurable():
    """It must appear in the catalogue the channel policy page renders."""
    codes = {entry.template_code for entry in event_catalogue()}
    assert "customer_ticket_created" in codes


def test_portal_ticket_create_supplies_ticket_number():
    """A template placeholder with no payload key ships an empty reference."""
    from pathlib import Path

    routes = Path(scheduler_config.__file__).parent.parent / "web/customer/routes.py"
    body = routes.read_text()
    marker = body.index('"customer_ticket_created"')
    assert "ticket_number" in body[marker : marker + 500]


# --- ticket movement --------------------------------------------------------


class _Comment:
    def __init__(self, *, is_internal: bool, author_type: str, body: str = "hello"):
        self.id = "11111111-1111-1111-1111-111111111111"
        self.is_internal = is_internal
        self.author_type = author_type
        self.body = body


class _Ticket:
    def __init__(self, status: str = TicketStatus.open.value):
        self.id = "22222222-2222-2222-2222-222222222222"
        self.number = "TKT-1"
        self.title = "No internet"
        self.status = status
        self.subscriber_id = None
        self.customer_account_id = None


def _capture(monkeypatch):
    from app.services import support

    sent: list[dict] = []

    def _fake(db, ticket, **kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(support.Tickets, "_queue_customer_ticket_update", _fake)
    return support, sent


def test_internal_note_never_reaches_the_customer(monkeypatch):
    support, sent = _capture(monkeypatch)
    support.Tickets._notify_customer_of_comment(
        None,
        _Ticket(),
        _Comment(is_internal=True, author_type=TicketCommentAuthorType.staff.value),
    )
    assert sent == []


def test_customers_own_comment_is_not_echoed_back(monkeypatch):
    support, sent = _capture(monkeypatch)
    support.Tickets._notify_customer_of_comment(
        None,
        _Ticket(),
        _Comment(is_internal=False, author_type=TicketCommentAuthorType.customer.value),
    )
    assert sent == []


def test_visible_staff_reply_notifies_the_customer(monkeypatch):
    support, sent = _capture(monkeypatch)
    support.Tickets._notify_customer_of_comment(
        None,
        _Ticket(),
        _Comment(
            is_internal=False,
            author_type=TicketCommentAuthorType.staff.value,
            body="  Engineer  dispatched  ",
        ),
    )
    assert len(sent) == 1
    assert sent[0]["event_type"] == "support_ticket_comment_added"
    assert "Engineer dispatched" in sent[0]["body"]
    assert sent[0]["dedupe_key"].startswith("ticket-comment:")


def test_status_change_notifies_only_for_customer_meaningful_states(monkeypatch):
    support, sent = _capture(monkeypatch)

    noisy = _Ticket(status=TicketStatus.pending.value)
    support.Tickets._notify_customer_of_status_change(
        None, noisy, TicketStatus.new.value
    )
    assert sent == []

    meaningful = _Ticket(status=TicketStatus.waiting_on_customer.value)
    support.Tickets._notify_customer_of_status_change(
        None, meaningful, TicketStatus.open.value
    )
    assert len(sent) == 1
    assert sent[0]["event_type"] == "support_ticket_status_changed"
    assert sent[0]["extra_metadata"]["from_status"] == TicketStatus.open.value


def test_unchanged_status_does_not_notify(monkeypatch):
    support, sent = _capture(monkeypatch)
    ticket = _Ticket(status=TicketStatus.open.value)
    support.Tickets._notify_customer_of_status_change(
        None, ticket, TicketStatus.open.value
    )
    assert sent == []


def test_status_notification_dedupes_a_transition_not_a_state(monkeypatch):
    """Keying on the state alone silences every later re-entry into it.

    Intent dedupe is permanent and global, so a ticket parked on_hold would be
    told "we'll let you know when it resumes" and then never hear the resume.
    """
    support, sent = _capture(monkeypatch)
    ticket = _Ticket(status=TicketStatus.closed.value)

    support.Tickets._notify_customer_of_status_change(
        None, ticket, TicketStatus.open.value
    )

    key = sent[0]["dedupe_key"]
    assert key.startswith(f"ticket-status:{ticket.id}:open->closed:")
    assert key != f"ticket-status:{ticket.id}:closed"


def test_re_entering_a_status_is_not_suppressed(monkeypatch):
    """open -> on_hold -> open must notify twice; the second is the resume."""
    support, sent = _capture(monkeypatch)
    ticket = _Ticket(status=TicketStatus.on_hold.value)

    support.Tickets._notify_customer_of_status_change(
        None, ticket, TicketStatus.open.value
    )
    ticket.status = TicketStatus.open.value
    support.Tickets._notify_customer_of_status_change(
        None, ticket, TicketStatus.on_hold.value
    )

    assert len(sent) == 2
    assert sent[0]["dedupe_key"] != sent[1]["dedupe_key"]


def test_the_same_transition_still_collapses(monkeypatch):
    """Idempotency is retained: a repeated identical transition dedupes."""
    support, sent = _capture(monkeypatch)
    ticket = _Ticket(status=TicketStatus.closed.value)

    support.Tickets._notify_customer_of_status_change(
        None, ticket, TicketStatus.open.value
    )
    support.Tickets._notify_customer_of_status_change(
        None, ticket, TicketStatus.open.value
    )

    assert sent[0]["dedupe_key"] == sent[1]["dedupe_key"]


def test_ticket_updates_do_not_hardcode_a_single_channel():
    """Defaults are a fallback; the channel policy owns the real decision."""
    import inspect

    from app.services import support

    source = inspect.getsource(support.Tickets._queue_customer_ticket_update)
    for channel in (
        NotificationChannel.email,
        NotificationChannel.whatsapp,
        NotificationChannel.push,
    ):
        assert channel.name in source
    assert "default_channels" in source
