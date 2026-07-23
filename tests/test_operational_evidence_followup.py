from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.poller.mikrotik_poller import DevicePool, MikroTikConnection
from app.services import operational_checks
from app.services.db_error_observability import statement_fingerprint
from app.services.payment_reconciliation import TopupReconciliationBacklog
from app.services.web_network_ont_actions import device_actions


def test_running_config_releases_read_transaction_before_ssh(monkeypatch):
    transaction_open = True
    olt_id = uuid4()
    ont_id = uuid4()
    olt = SimpleNamespace(
        id=olt_id,
        name="Garki OLT",
        hostname="garki-olt",
        mgmt_ip="192.0.2.10",
        vendor="Huawei",
        model="MA5800",
        firmware_version="V1",
        software_version="V1",
        ssh_username="operator",
        ssh_password="encrypted",
        ssh_port=22,
        rate_limit_ops_per_minute=10,
    )
    ont = SimpleNamespace(
        id=ont_id,
        serial_number="HWTC12345678",
        external_id="1",
        olt_device=olt,
    )
    pon = SimpleNamespace(name="0/1/0", olt=olt)
    assignment = SimpleNamespace(pon_port=pon)

    class _Db:
        def get(self, _model, _id):
            return ont

    def finish_read(_db):
        nonlocal transaction_open
        transaction_open = False

    def run_cli(_target, _command):
        assert transaction_open is False
        return True, "ok", "output"

    monkeypatch.setattr(device_actions, "finish_read_transaction", finish_read)
    monkeypatch.setattr(
        "app.services.web_network_ont_assignments.active_assignment_for_ont_id",
        lambda *_args: assignment,
    )
    monkeypatch.setattr("app.services.network.olt_ssh.run_cli_command", run_cli)
    monkeypatch.setattr(
        "app.services.network.huawei_command_profiles.get_huawei_command_profile",
        lambda _olt: SimpleNamespace(
            display_ont_info=lambda fsp, onu_id: f"display ont info {fsp} {onu_id}"
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_read_cache.olt_cache.get",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.network.olt_read_cache.olt_cache.set",
        lambda *_args, **_kwargs: None,
    )

    result = device_actions.fetch_olt_running_config(_Db(), str(ont_id))

    assert result.error is None
    assert result.ont is not ont
    assert result.olt is not olt
    assert result.ont.serial_number == "HWTC12345678"


def test_poller_failure_snapshot_explains_attempt_impact_and_retry():
    now = datetime.now(UTC)
    device_id = uuid4()
    subscription_id = uuid4()
    connection = MikroTikConnection(
        device_id=device_id,
        display_name="Garki Core",
        host="192.0.2.20",
        username="operator",
        password="test-ciphertext",
    )
    connection._consecutive_failures = 9
    connection._last_attempt = now
    connection._last_successful_poll = now - timedelta(hours=1)
    connection._last_error_category = "no_route_to_host"
    connection._last_error = "No route to host"
    pool = DevicePool()
    pool._connections[device_id] = connection
    pool._queue_mappings[device_id] = {"customer": subscription_id}

    snapshot = pool.health_snapshot()

    assert snapshot["devices_failing"] == 1
    row = snapshot["device_failures"][0]
    assert row["name"] == "Garki Core"
    assert row["error_category"] == "no_route_to_host"
    assert row["services_without_live_bandwidth"] == 1
    assert row["next_attempt_at"] is not None


def test_database_statement_correlation_is_stable_and_redacted():
    first = statement_fingerprint(
        "SELECT subscribers.id FROM subscribers WHERE subscribers.email = %(email)s"
    )
    second = statement_fingerprint(
        " select  subscribers.id from subscribers where subscribers.email = %(email)s "
    )

    assert first == second
    assert first is not None
    assert "email" not in first


def test_paystack_operational_check_exposes_webhook_and_reconciliation_gap(
    db_session, monkeypatch
):
    now = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(
        operational_checks,
        "_paystack_binding_evidence",
        lambda _db: ("Capabilities are enabled.", True, True),
    )
    monkeypatch.setattr(
        operational_checks,
        "_task_row",
        lambda _db, _task_name: SimpleNamespace(
            enabled=True,
            interval_seconds=1800,
        ),
    )
    monkeypatch.setattr(
        operational_checks,
        "_task_result",
        lambda _task_name: (
            {
                "status": "partial",
                "detail": {"checked": 8, "recovered": 0, "errors": 3},
            },
            now - timedelta(minutes=5),
        ),
    )
    monkeypatch.setattr(
        operational_checks.job_heartbeat,
        "get_last_success",
        lambda _task_name: now - timedelta(minutes=5),
    )
    monkeypatch.setattr(
        operational_checks,
        "topup_reconciliation_backlog",
        lambda _db, observed_at: TopupReconciliationBacklog(
            pending=3,
            eligible=3,
            outside_window=0,
            oldest_pending_at=observed_at - timedelta(hours=1),
            stale_before=observed_at - timedelta(minutes=15),
            oldest_eligible_at=observed_at - timedelta(days=7),
        ),
    )
    monkeypatch.setattr(
        operational_checks,
        "_latest_paystack_webhook_at",
        lambda _db: None,
    )

    check = operational_checks.paystack_payment_check(db_session, now=now)

    assert check.needs_attention is True
    assert check.last_result == (
        "Reconciliation checked 8, recovered 0, and rejected 3; "
        "3 stale intent(s) remain."
    )
    assert operational_checks.PAYSTACK_WEBHOOK_PATH in check.expected
    assert "Set the Paystack live webhook URL" in check.next_step
