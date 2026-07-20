from datetime import UTC, datetime, timedelta

from app.services import radius_accounting_health


def test_accounting_freshness_classifies_fresh_stale_and_unavailable():
    now = datetime.now(UTC)

    fresh = radius_accounting_health.assess_freshness(
        now - timedelta(minutes=5),
        checked_at=now,
        stale_seconds=3600,
    )
    stale = radius_accounting_health.assess_freshness(
        now - timedelta(hours=2),
        checked_at=now,
        stale_seconds=3600,
    )
    unavailable = radius_accounting_health.assess_freshness(
        None,
        checked_at=now,
        stale_seconds=3600,
    )

    assert fresh.state == radius_accounting_health.RadiusAccountingSourceState.fresh
    assert fresh.fresh is True
    assert stale.state == radius_accounting_health.RadiusAccountingSourceState.stale
    assert stale.fresh is False
    assert unavailable.state == (
        radius_accounting_health.RadiusAccountingSourceState.unavailable
    )


def test_accounting_freshness_rejects_future_source_timestamp():
    now = datetime.now(UTC)

    result = radius_accounting_health.assess_freshness(
        now + timedelta(minutes=1),
        checked_at=now,
    )

    assert result.state == (
        radius_accounting_health.RadiusAccountingSourceState.clock_skew
    )
    assert result.fresh is False


def test_accounting_stale_threshold_uses_registered_setting(db_session, monkeypatch):
    monkeypatch.setattr(
        radius_accounting_health.settings_spec,
        "resolve_value",
        lambda *_args, **_kwargs: "7200",
    )

    assert radius_accounting_health.stale_after_seconds(db_session) == 7200
