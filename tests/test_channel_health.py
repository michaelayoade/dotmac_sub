from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY
from starlette.exceptions import HTTPException

from app.api.webhook_observation import webhook_observation
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import InboxChannelType
from app.services import channel_health, team_inbox_channel_receive


def _team(db_session) -> ServiceTeam:
    team = ServiceTeam(name="Support", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _receive(db_session, *, channel: str, wamid: str, when: datetime, team) -> object:
    return team_inbox_channel_receive.receive_inbound_channel(
        db_session,
        team_inbox_channel_receive.InboundChannelPayload(
            channel_type=channel,
            contact_address=f"{channel}:+2348035550{wamid[-3:]}",
            body="hello",
            external_message_id=wamid,
            fallback_service_team_id=team.id,
            received_at=when,
        ),
    )


def _counter(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_channel_ingestion_reports_freshness_and_recent_count(db_session):
    team = _team(db_session)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    _receive(
        db_session,
        channel=InboxChannelType.whatsapp.value,
        wamid="wamid-101",
        when=now - timedelta(minutes=5),
        team=team,
    )
    _receive(
        db_session,
        channel=InboxChannelType.whatsapp.value,
        wamid="wamid-102",
        when=now - timedelta(minutes=30),
        team=team,
    )
    _receive(
        db_session,
        channel=InboxChannelType.email.value,
        wamid="mid-201",
        when=now - timedelta(minutes=60),
        team=team,
    )
    db_session.commit()

    observations = channel_health.collect_channel_ingestion_observations(
        db_session, now=now
    )
    by_key = {(o.signal, o.scope): o.value for o in observations}

    # Freshness is measured from the newest inbound row per channel.
    assert by_key[("seconds_since_last_inbound", "whatsapp")] == pytest.approx(300)
    assert by_key[("seconds_since_last_inbound", "email")] == pytest.approx(3600)
    # Only the 5-min-old whatsapp row falls inside the 15-minute window.
    assert by_key[("inbound_count_15m", "whatsapp")] == 1
    assert by_key[("inbound_count_15m", "email")] == 0


def test_duplicate_inbound_increments_suppression_counter(db_session):
    team = _team(db_session)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    labels = {"channel": InboxChannelType.whatsapp.value}
    before = _counter("sub_inbound_dedup_suppressed_total", labels)

    first = _receive(
        db_session,
        channel=InboxChannelType.whatsapp.value,
        wamid="wamid-dup",
        when=now,
        team=team,
    )
    db_session.commit()
    assert first.kind == "received"

    second = _receive(
        db_session,
        channel=InboxChannelType.whatsapp.value,
        wamid="wamid-dup",
        when=now,
        team=team,
    )
    db_session.commit()

    assert second.kind == "duplicate"
    after = _counter("sub_inbound_dedup_suppressed_total", labels)
    assert after == before + 1


def test_publish_survives_broker_failure_without_dropping_freshness(
    db_session, monkeypatch
):
    _team(db_session)
    published: list[tuple[str, str]] = []

    def _capture(domain, observations, *, status="ok", now=None):
        published.append((domain, status))
        return True

    def _broker_down():
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr(channel_health, "publish_state_snapshot", _capture)
    monkeypatch.setattr(
        channel_health, "collect_celery_queue_observations", _broker_down
    )

    summary = channel_health.publish_channel_health(db_session)

    domains = {domain: status for domain, status in published}
    # Freshness still publishes; the queue snapshot degrades rather than raising.
    assert domains["channel_ingestion"] == "ok"
    assert domains["celery_queues"] == "degraded"
    assert summary["queue_status"] == "degraded"


def test_webhook_observation_records_each_outcome():
    labels = {"provider": "obs_test", "event": "unit"}

    def outcome(name: str) -> float:
        return _counter("sub_webhook_events_total", {**labels, "outcome": name})

    accepted_before = outcome("accepted")
    with webhook_observation(provider="obs_test", event="unit"):
        pass
    assert outcome("accepted") == accepted_before + 1

    rejected_before = outcome("rejected")
    with pytest.raises(HTTPException):
        with webhook_observation(provider="obs_test", event="unit"):
            raise HTTPException(status_code=401)
    assert outcome("rejected") == rejected_before + 1

    error_before = outcome("error")
    with pytest.raises(ValueError):
        with webhook_observation(provider="obs_test", event="unit"):
            raise ValueError("boom")
    assert outcome("error") == error_before + 1
