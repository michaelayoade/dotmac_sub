"""Ticket deflection: inform the customer, never block the report."""

import uuid

import pytest

from app.services import portal_ticket_deflection as deflection


class _Assessment:
    def __init__(self, headline="Area outage", message="We're on it", advice=None):
        self.headline = headline
        self.message = message
        self.advice = advice


class _State:
    def __init__(
        self,
        *,
        connection_state,
        area_outage=False,
        open_infrastructure_ticket_id=None,
        active_outage_id=None,
    ):
        self.connection_state = connection_state
        self.area_outage = area_outage
        self.open_infrastructure_ticket_id = open_infrastructure_ticket_id
        self.active_outage_id = active_outage_id


_UNSET = object()


class _Subscription:
    def __init__(self, subscriber_id=None):
        self.subscriber_id = subscriber_id or uuid.uuid4()


class _Ticket:
    """Only the field the portal detail route authorises on."""

    def __init__(self, subscriber_id):
        self.subscriber_id = subscriber_id


class _Db:
    """Enough Session surface for the portal-visibility guard."""

    def __init__(self, ticket=None):
        self._ticket = ticket

    def get(self, _model, _pk):
        return self._ticket


@pytest.fixture
def wired(monkeypatch):
    """Stub the owners this module reads; it decides none of them itself."""

    def _install(*, state, subscription=_UNSET, assessment=None):
        # `subscription=None` must stay meaningful (the unprovisioned case), so
        # the default is a sentinel rather than None.
        if subscription is _UNSET:
            subscription = _Subscription()
        monkeypatch.setattr(
            "app.services.customer_portal_context.resolve_customer_subscription",
            lambda db, s: subscription,
        )
        monkeypatch.setattr(
            "app.services.customer_service_state.get_customer_service_state",
            lambda db, sub: state,
        )
        monkeypatch.setattr(
            "app.services.topology.connection_status.assess",
            lambda db, sub, **kw: assessment or _Assessment(),
        )

    return _install


def test_connected_customer_sees_the_plain_form(wired):
    wired(state=_State(connection_state="connected"))
    result = deflection.assess_ticket_deflection(None, {})
    assert result.known_issue is False
    assert result.headline == ""


def test_area_outage_is_surfaced_with_its_incident(wired):
    incident = uuid.uuid4()
    wired(
        state=_State(
            connection_state="outage", area_outage=True, active_outage_id=incident
        ),
        assessment=_Assessment(
            headline="Outage in your area",
            message="Engineers are working on it",
            advice="No action needed",
        ),
    )
    result = deflection.assess_ticket_deflection(None, {})

    assert result.known_issue is True
    assert result.scope == "outage"
    assert result.headline == "Outage in your area"
    assert result.advice == "No action needed"
    assert result.incident_id == str(incident)


def test_last_mile_trouble_is_scoped_to_the_customer(wired):
    wired(state=_State(connection_state="trouble"))
    result = deflection.assess_ticket_deflection(None, {})
    assert result.known_issue is True
    assert result.scope == "trouble"


def test_existing_infrastructure_ticket_is_offered(wired):
    ticket_id = uuid.uuid4()
    subscription = _Subscription()
    wired(
        state=_State(
            connection_state="outage",
            area_outage=True,
            open_infrastructure_ticket_id=ticket_id,
        ),
        subscription=subscription,
    )
    db = _Db(_Ticket(subscriber_id=subscription.subscriber_id))

    result = deflection.assess_ticket_deflection(db, {})

    assert result.existing_ticket_id == str(ticket_id)


def test_ticket_the_portal_would_reject_is_not_offered(wired):
    """The banner promises "updates land there"; a dead link breaks that.

    open_infrastructure_down_ticket matches through any customer field, but the
    portal detail route authorises on subscriber_id alone.
    """
    ticket_id = uuid.uuid4()
    subscription = _Subscription()
    wired(
        state=_State(
            connection_state="outage",
            area_outage=True,
            open_infrastructure_ticket_id=ticket_id,
        ),
        subscription=subscription,
    )
    # Linked by account/person only — subscriber_id never populated.
    db = _Db(_Ticket(subscriber_id=None))

    result = deflection.assess_ticket_deflection(db, {})

    assert result.known_issue is True
    assert result.existing_ticket_id is None


def test_another_subscribers_ticket_is_never_offered(wired):
    ticket_id = uuid.uuid4()
    wired(
        state=_State(
            connection_state="outage",
            area_outage=True,
            open_infrastructure_ticket_id=ticket_id,
        ),
        subscription=_Subscription(),
    )
    db = _Db(_Ticket(subscriber_id=uuid.uuid4()))

    assert deflection.assess_ticket_deflection(db, {}).existing_ticket_id is None


def test_a_vanished_ticket_degrades_to_no_offer(wired):
    wired(
        state=_State(
            connection_state="outage",
            area_outage=True,
            open_infrastructure_ticket_id=uuid.uuid4(),
        )
    )

    assert deflection.assess_ticket_deflection(_Db(None), {}).existing_ticket_id is None


def test_a_customer_with_no_subscription_gets_the_plain_form(wired):
    wired(state=_State(connection_state="outage"), subscription=None)
    assert deflection.assess_ticket_deflection(None, {}).known_issue is False


def test_a_broken_diagnostic_never_blocks_the_form(monkeypatch):
    """A customer who cannot report a problem is worse off than one who sees
    no banner."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("topology exploded")

    monkeypatch.setattr(
        "app.services.customer_portal_context.resolve_customer_subscription", _boom
    )
    result = deflection.assess_ticket_deflection(None, {})
    assert result.known_issue is False


def test_deflection_suggests_a_triageable_subject(wired):
    wired(state=_State(connection_state="outage", area_outage=True))
    assert "outage" in deflection.assess_ticket_deflection(None, {}).suggested_title

    wired(state=_State(connection_state="trouble"))
    assert "connection" in deflection.assess_ticket_deflection(None, {}).suggested_title


def test_context_exposes_the_flag_the_template_branches_on(wired):
    wired(state=_State(connection_state="outage", area_outage=True))
    context = deflection.assess_ticket_deflection(None, {}).as_context()
    assert context["deflection_known_issue"] is True
    assert context["deflection"].scope == "outage"


def test_deflection_decides_no_outage_truth_of_its_own():
    """Connection state and outage truth belong to their owners."""
    import inspect

    source = inspect.getsource(deflection)
    assert "OutageIncident" not in source
    assert "affected_customers" not in source
    assert "get_customer_service_state" in source


def test_the_form_still_accepts_a_report_during_an_outage():
    """The banner is informational; the template must not disable the form."""
    from pathlib import Path

    body = (
        Path(__file__).resolve().parents[1] / "templates/customer/support/new.html"
    ).read_text()
    banner_start = body.index("deflection_known_issue")
    banner = body[banner_start : body.index("{% if crm_error %}")]
    assert "disabled" not in banner
    assert "<form" not in banner
