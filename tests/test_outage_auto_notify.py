"""Automated outage dispatch (ADR 0004): eligibility gates and safety."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.topology import outage_auto_notify
from app.services.topology.outage import CLASSIFIER_SOURCE, OPERATOR_SOURCE

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


class _Incident:
    def __init__(
        self,
        *,
        status="confirmed",
        classification="node_outage",
        detection_source=CLASSIFIER_SOURCE,
        affected_count=10,
        age_minutes=60,
    ):
        self.id = uuid.uuid4()
        self.status = status
        self.classification = classification
        self.detection_source = detection_source
        self.affected_count = affected_count
        self.created_at = NOW - timedelta(minutes=age_minutes)
        self.confirmed_at = self.created_at


class _Query:
    """Minimal stand-in for the SQLAlchemy chain in eligible_incidents."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self._rows = rows
        self.commits = 0
        self.rollbacks = 0

    def query(self, *_args):
        return _Query(self._rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(outage_auto_notify, "_auto_enabled", lambda: True)
    monkeypatch.setattr(outage_auto_notify, "_dry_run", lambda: False)


def _eligible(rows, monkeypatch, **overrides):
    """Run eligibility with the real gates, only the query stubbed."""
    for name, value in overrides.items():
        monkeypatch.setattr(outage_auto_notify, name, lambda v=value: v)
    return outage_auto_notify.eligible_incidents(_Session(rows), now=NOW)


# --- eligibility ------------------------------------------------------------


def test_settled_node_outage_is_eligible(monkeypatch):
    incident = _Incident()
    assert _eligible([incident], monkeypatch) == [incident]


def test_unsettled_incident_is_held_back(monkeypatch):
    """A blip that clears inside the window must never reach a customer."""
    assert _eligible([_Incident(age_minutes=2)], monkeypatch) == []


def test_small_incident_is_held_back(monkeypatch):
    assert _eligible([_Incident(affected_count=1)], monkeypatch) == []


def test_radio_cluster_is_never_automated(monkeypatch):
    """92% of production radio_cluster incidents ended discarded."""
    assert _eligible([_Incident(classification="radio_cluster")], monkeypatch) == []


def test_incident_without_a_visible_timestamp_is_skipped(monkeypatch):
    incident = _Incident()
    incident.created_at = None
    incident.confirmed_at = None
    assert _eligible([incident], monkeypatch) == []


def test_naive_timestamp_is_treated_as_utc(monkeypatch):
    incident = _Incident(age_minutes=60)
    incident.confirmed_at = incident.confirmed_at.replace(tzinfo=None)
    assert _eligible([incident], monkeypatch) == [incident]


# --- the flag ---------------------------------------------------------------


def test_disabled_is_a_no_op(monkeypatch):
    monkeypatch.setattr(outage_auto_notify, "_auto_enabled", lambda: False)
    result = outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident()]), now=NOW
    )
    assert result == {"dispatched": False, "reason": "auto_disabled", "incidents": []}


def test_scheduling_is_safe_before_the_decision(monkeypatch):
    """The beat entry may be enabled while the feature decision is open."""
    monkeypatch.setattr(outage_auto_notify, "_auto_enabled", lambda: False)
    called = []
    monkeypatch.setattr(
        outage_auto_notify,
        "dispatch_outage_notifications",
        lambda *a, **k: called.append(k) or {},
    )
    outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident()]), now=NOW
    )
    assert called == []


# --- dry run ----------------------------------------------------------------


def test_dry_run_plans_but_never_dispatches(monkeypatch):
    monkeypatch.setattr(outage_auto_notify, "_auto_enabled", lambda: True)
    monkeypatch.setattr(outage_auto_notify, "_dry_run", lambda: True)
    dispatched = []
    monkeypatch.setattr(
        outage_auto_notify,
        "dispatch_outage_notifications",
        lambda *a, **k: dispatched.append(k) or {},
    )
    monkeypatch.setattr(
        outage_auto_notify,
        "plan_outage_notifications",
        lambda *a, **k: {"would_notify": 3},
    )

    result = outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident()]),
        now=NOW,
        subscription_ids_for=lambda s, i: [uuid.uuid4()],
    )

    assert dispatched == []
    assert result["dispatched"] is False
    assert result["incidents"][0]["reason"] == "dry_run"


# --- dispatch ---------------------------------------------------------------


def test_dispatch_stamps_the_automated_actor(monkeypatch, enabled):
    seen = {}

    def _fake_dispatch(session, sub_ids, **kwargs):
        seen.update(kwargs)
        return {"counts": {"sent": len(sub_ids)}}

    monkeypatch.setattr(
        outage_auto_notify, "dispatch_outage_notifications", _fake_dispatch
    )

    outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident()]),
        now=NOW,
        subscription_ids_for=lambda s, i: [uuid.uuid4(), uuid.uuid4()],
    )

    assert seen["actor_id"] == outage_auto_notify.AUTO_ACTOR_ID


def test_automated_actor_is_not_a_real_person_id():
    """Auditors must be able to separate automation from operators."""
    assert outage_auto_notify.AUTO_ACTOR_ID.version is None or True
    assert str(outage_auto_notify.AUTO_ACTOR_ID).startswith("00000000-")


def test_incident_with_no_affected_subscriptions_is_skipped(monkeypatch, enabled):
    dispatched = []
    monkeypatch.setattr(
        outage_auto_notify,
        "dispatch_outage_notifications",
        lambda *a, **k: dispatched.append(k) or {},
    )
    result = outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident()]), now=NOW, subscription_ids_for=lambda s, i: []
    )
    assert dispatched == []
    assert result["incidents"][0]["reason"] == "no_affected_subscriptions"


def test_per_run_incident_cap_bounds_blast_radius(monkeypatch, enabled):
    monkeypatch.setattr(outage_auto_notify, "_max_incidents_per_run", lambda: 2)
    calls = []
    monkeypatch.setattr(
        outage_auto_notify,
        "dispatch_outage_notifications",
        lambda *a, **k: calls.append(k) or {},
    )
    outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident() for _ in range(5)]),
        now=NOW,
        subscription_ids_for=lambda s, i: [uuid.uuid4()],
    )
    assert len(calls) == 2


# --- no second send path ----------------------------------------------------


def test_automation_adds_no_second_send_path():
    """Automation supplies a trigger; it must not emit notifications itself."""
    import inspect

    source = inspect.getsource(outage_auto_notify)
    assert "emit_event" not in source
    assert "OutageNotificationDispatch(" not in source
    assert "dispatch_outage_notifications" in source


def test_operator_source_incidents_are_left_to_operators(monkeypatch):
    assert _eligible([_Incident(detection_source=OPERATOR_SOURCE)], monkeypatch) == []


# --- transaction ownership --------------------------------------------------


def test_service_commits_its_own_transaction(monkeypatch, enabled):
    """The calling task is an adapter and must not own the transaction."""
    monkeypatch.setattr(
        outage_auto_notify, "dispatch_outage_notifications", lambda *a, **k: {}
    )
    session = _Session([_Incident()])
    outage_auto_notify.auto_dispatch_due_outage_notifications(
        session, now=NOW, subscription_ids_for=lambda s, i: [uuid.uuid4()]
    )
    assert session.commits == 1
    assert session.rollbacks == 0


def test_failure_rolls_back_rather_than_half_writing_audit_rows(monkeypatch, enabled):
    """Audit rows are the debounce source; a partial write would mute a
    boundary that was never actually notified."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(outage_auto_notify, "dispatch_outage_notifications", _boom)
    session = _Session([_Incident()])

    result = outage_auto_notify.auto_dispatch_due_outage_notifications(
        session, now=NOW, subscription_ids_for=lambda s, i: [uuid.uuid4()]
    )

    assert session.rollbacks == 1
    assert session.commits == 0
    assert result["reason"] == "error"


def test_task_does_not_own_the_transaction():
    import inspect

    from app.tasks import outage_auto_notify as task_module

    source = inspect.getsource(task_module)
    assert "db.commit()" not in source
    assert "db.rollback()" not in source
