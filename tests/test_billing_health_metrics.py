from datetime import UTC, datetime


def test_billing_health_collector_reads_snapshot_without_database(monkeypatch):
    from app import metrics
    from app.services import observability
    from app.services.db_session_adapter import db_session_adapter

    snapshot = {
        "domain": "billing_health",
        "status": "degraded",
        "observed_at": datetime.now(UTC).isoformat(),
        "observations": [
            {
                "signal": "negative_prepaid_balance_accounts",
                "scope": "all",
                "value": 7,
            },
            {
                "signal": "negative_prepaid_balance_total",
                "scope": "all",
                "value": 1250,
            },
            {
                "signal": "runner_heartbeat_stale",
                "scope": "app.tasks.billing.run_invoice_cycle",
                "value": 1,
            },
            {
                "signal": "unbilled_active_subscriptions",
                "scope": "no_billing_path",
                "value": 3,
            },
        ],
    }
    monkeypatch.setattr(
        observability,
        "load_state_snapshot",
        lambda domain: snapshot if domain == "billing_health" else None,
    )

    def database_access_is_a_failure(*_args, **_kwargs):
        raise AssertionError("scrape-time billing health must not access the database")

    monkeypatch.setattr(db_session_adapter, "session", database_access_is_a_failure)

    families = list(metrics._BillingHealthCollector().collect())
    by_name = {family.name: family for family in families}

    assert by_name["billing_health_snapshot_available"].samples[0].value == 1.0
    assert by_name["billing_negative_prepaid_balance_accounts"].samples[0].value == 7.0
    assert by_name["billing_negative_prepaid_balance_total"].samples[0].value == 1250
    assert any(
        sample.labels == {"task": "app.tasks.billing.run_invoice_cycle"}
        and sample.value == 1.0
        for sample in by_name["billing_runner_heartbeat_stale"].samples
    )
    assert any(
        sample.labels == {"reason": "no_billing_path"} and sample.value == 3.0
        for sample in by_name["billing_unbilled_active_subscriptions"].samples
    )


def test_billing_health_collector_fails_fast_when_snapshot_missing(monkeypatch):
    from app import metrics
    from app.services import observability

    monkeypatch.setattr(observability, "load_state_snapshot", lambda _domain: None)

    families = list(metrics._BillingHealthCollector().collect())

    assert [family.name for family in families] == ["billing_health_snapshot_available"]
    assert families[0].samples[0].value == 0.0
