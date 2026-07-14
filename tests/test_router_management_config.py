import uuid
from unittest.mock import MagicMock

import pytest

from app.models.router_management import (
    Router,
    RouterConfigPush,
    RouterConfigPushStatus,
    RouterPushResultStatus,
    RouterSnapshotSource,
    RouterTemplateCategory,
)
from app.schemas.router_management import (
    RouterConfigPushCreate,
    RouterConfigTemplateCreate,
    RouterConfigTemplateUpdate,
)
from app.services.router_management.config import (
    RouterConfigService,
    RouterTemplateService,
)


@pytest.fixture(autouse=True)
def _rest_export_path(monkeypatch):
    """These tests mock the REST ``execute`` for config export. Snapshots now
    default to SSH, so pin this file to the REST path (the SSH path would try to
    load a real key and fail in CI)."""
    import types

    from app.services.router_management import config_export

    monkeypatch.setattr(
        config_export,
        "settings",
        types.SimpleNamespace(router_config_export_via_ssh=False),
    )


def _make_router(db_session, name: str) -> Router:
    r = Router(
        name=name,
        hostname=name,
        management_ip="10.0.0.1",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


def test_store_snapshot(db_session):
    router = _make_router(db_session, "snap-store-test")
    snap = RouterConfigService.store_snapshot(
        db_session,
        router_id=router.id,
        config_export="/ip address\nadd address=10.0.0.1/24 interface=ether1",
        source=RouterSnapshotSource.manual,
    )
    assert snap.router_id == router.id
    assert snap.config_hash is not None
    assert len(snap.config_hash) == 64


def test_list_snapshots(db_session):
    router = _make_router(db_session, "snap-list-test")
    for i in range(3):
        RouterConfigService.store_snapshot(
            db_session,
            router_id=router.id,
            config_export=f"config version {i}",
            source=RouterSnapshotSource.scheduled,
        )
    snaps = RouterConfigService.list_snapshots(db_session, router.id)
    assert len(snaps) == 3


def test_get_snapshot(db_session):
    router = _make_router(db_session, "snap-get-test")
    snap = RouterConfigService.store_snapshot(
        db_session,
        router_id=router.id,
        config_export="test config",
        source=RouterSnapshotSource.manual,
    )
    fetched = RouterConfigService.get_snapshot(db_session, snap.id)
    assert fetched.config_export == "test config"


def test_create_template(db_session):
    tmpl = RouterTemplateService.create(
        db_session,
        RouterConfigTemplateCreate(
            name="test-template",
            template_body="/queue simple set [find] queue={{ queue_type }}/{{ queue_type }}",
            category="queue",
            variables={"queue_type": {"type": "string", "default": "sfq"}},
        ),
    )
    assert tmpl.name == "test-template"
    assert tmpl.category == RouterTemplateCategory.queue


def test_update_template(db_session):
    tmpl = RouterTemplateService.create(
        db_session,
        RouterConfigTemplateCreate(
            name="update-tmpl",
            template_body="original body",
        ),
    )
    updated = RouterTemplateService.update(
        db_session, tmpl.id, RouterConfigTemplateUpdate(template_body="new body")
    )
    assert updated.template_body == "new body"


def test_list_templates(db_session):
    RouterTemplateService.create(
        db_session,
        RouterConfigTemplateCreate(name="list-tmpl-1", template_body="body1"),
    )
    templates = RouterTemplateService.list(db_session)
    assert len(templates) >= 1


def test_render_template():
    body = "/ip dns set servers={{ dns_servers }}"
    variables = {"dns_servers": "8.8.8.8,8.8.4.4"}
    result = RouterConfigService.render_template(body, variables)
    assert result == "/ip dns set servers=8.8.8.8,8.8.4.4"


def test_render_template_missing_var():
    body = "/ip dns set servers={{ dns_servers }}"
    with pytest.raises(ValueError, match="Template rendering failed"):
        RouterConfigService.render_template(body, {})


def test_manual_snapshot_uses_canonical_export_transport(db_session, monkeypatch):
    router = _make_router(db_session, "manual-snapshot-test")
    calls = []

    def fetch(router_arg):
        calls.append(router_arg.id)
        return "/system identity set name=manual-snapshot-test"

    monkeypatch.setattr(
        "app.services.router_management.config_export.fetch_config_export", fetch
    )
    snapshot = RouterConfigService.capture_from_router(db_session, router)

    assert calls == [router.id]
    assert snapshot.config_export.startswith("/system identity")
    assert len(snapshot.config_hash) == 64


def test_create_push_record(db_session):
    router = _make_router(db_session, "push-test")
    user_id = uuid.uuid4()

    push = RouterConfigService.create_push(
        db_session,
        commands=['/queue/simple/set {"numbers":"*1","queue":"sfq/sfq"}'],
        router_ids=[router.id],
        initiated_by=user_id,
        dry_run=True,
        failure_policy="abort",
    )
    assert push.status == RouterConfigPushStatus.pending
    assert push.dry_run is True
    assert push.failure_policy == "abort"
    assert push.allow_dangerous_commands is False
    assert len(push.results) == 1
    assert push.results[0].router_id == router.id
    assert push.operation_id is not None
    assert push.results[0].operation_id is not None


def test_create_push_dangerous_command(db_session):
    router = _make_router(db_session, "push-danger-test")
    with pytest.raises(ValueError, match="Dangerous command blocked"):
        RouterConfigService.create_push(
            db_session,
            commands=["/system/reset-configuration"],
            router_ids=[router.id],
            initiated_by=uuid.uuid4(),
        )


def test_create_push_dangerous_command_override(db_session):
    router = _make_router(db_session, "push-danger-override-test")
    with pytest.raises(ValueError, match="override is disabled"):
        RouterConfigService.create_push(
            db_session,
            commands=["/system/reset-configuration"],
            router_ids=[router.id],
            initiated_by=uuid.uuid4(),
            allow_dangerous_commands=True,
        )


def test_create_push_rejects_unknown_failure_policy(db_session):
    router = _make_router(db_session, "push-bad-policy-test")
    with pytest.raises(ValueError, match="Failure policy"):
        RouterConfigService.create_push(
            db_session,
            commands=["/ip address print"],
            router_ids=[router.id],
            initiated_by=uuid.uuid4(),
            failure_policy="rollback",
        )


def test_api_create_push_marks_results_failed_when_enqueue_fails(
    db_session, monkeypatch
):
    from fastapi import HTTPException

    from app.api.router_management import create_push

    router = _make_router(db_session, "push-enqueue-fail-test")
    user_id = uuid.uuid4()

    monkeypatch.setattr(
        "app.services.queue_adapter.enqueue_task",
        lambda *args, **kwargs: type(
            "Dispatch", (), {"queued": False, "error": "broker unavailable"}
        )(),
    )

    with pytest.raises(HTTPException) as exc_info:
        create_push(
            RouterConfigPushCreate(
                commands=['/system/ntp/client/set {"enabled":"yes"}'],
                router_ids=[router.id],
            ),
            auth={"principal_id": str(user_id)},
            db=db_session,
        )

    assert exc_info.value.status_code == 502
    push = db_session.query(RouterConfigPush).one()
    assert push.status == RouterConfigPushStatus.failed
    assert push.completed_at is not None
    assert len(push.results) == 1
    assert push.results[0].status == RouterPushResultStatus.failed
    assert "broker unavailable" in push.results[0].error_message


def test_api_create_push_rejects_unverifiable_command(db_session):
    from fastapi import HTTPException

    from app.api.router_management import create_push

    router = _make_router(db_session, "push-invalid-command-test")
    with pytest.raises(HTTPException) as exc_info:
        create_push(
            RouterConfigPushCreate(
                commands=['/system/reboot {"delay":"0s"}'],
                router_ids=[router.id],
            ),
            auth={"principal_id": str(uuid.uuid4())},
            db=db_session,
        )

    assert exc_info.value.status_code == 422
    assert db_session.query(RouterConfigPush).count() == 0


def test_execute_config_push_dry_run_captures_preview_without_posting(
    db_session, monkeypatch
):
    from app.tasks.router_sync import execute_config_push

    router = _make_router(db_session, "push-dry-run-test")
    push = RouterConfigService.create_push(
        db_session,
        commands=['/system/ntp/client/set {"enabled":"yes"}'],
        router_ids=[router.id],
        initiated_by=uuid.uuid4(),
        dry_run=True,
    )
    calls = []

    def fake_execute(router_arg, method, path, payload=None, **kwargs):
        calls.append((router_arg.name, method, path, payload))
        if path == "/export":  # config read (POST /rest/export), not a change
            return "/exported config"
        raise AssertionError("dry-run must not POST router changes")

    monkeypatch.setattr(
        "app.tasks.router_sync.db_session_adapter.create_session",
        lambda: db_session,
    )
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        "app.tasks.router_sync.RouterConnectionService.execute", fake_execute
    )

    result = execute_config_push.run(str(push.id))

    db_session.refresh(push)
    db_session.refresh(push.results[0])
    assert result["dry_run"] is True
    assert result["success"] == 1
    assert push.status == RouterConfigPushStatus.completed
    assert push.results[0].status == RouterPushResultStatus.success
    assert push.results[0].pre_snapshot_id is not None
    assert push.results[0].post_snapshot_id is None
    assert push.results[0].response_data["planned_commands"][0]["path"] == (
        "/system/ntp/client/set"
    )
    assert calls == [
        ("push-dry-run-test", "POST", "/export", None),
    ]


def test_execute_config_push_abort_policy_skips_remaining_after_failure(
    db_session, monkeypatch
):
    from app.tasks.router_sync import execute_config_push

    first = _make_router(db_session, "push-abort-a")
    second = _make_router(db_session, "push-abort-b")
    push = RouterConfigService.create_push(
        db_session,
        commands=['/system/ntp/client/set {"enabled":"yes"}'],
        router_ids=[first.id, second.id],
        initiated_by=uuid.uuid4(),
        failure_policy="abort",
    )
    calls = []

    def fake_execute(router_arg, method, path, payload=None, **kwargs):
        calls.append((router_arg.name, method, path, payload))
        if path == "/export":  # config read (POST /rest/export), not a change
            return "/exported config"
        if router_arg.name == "push-abort-a":
            raise RuntimeError("router rejected command")
        return {}

    monkeypatch.setattr(
        "app.tasks.router_sync.db_session_adapter.create_session",
        lambda: db_session,
    )
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        "app.tasks.router_sync.RouterConnectionService.execute", fake_execute
    )

    result = execute_config_push.run(str(push.id))

    db_session.refresh(push)
    for row in push.results:
        db_session.refresh(row)
    statuses = {row.router_id: row.status for row in push.results}
    errors = {row.router_id: row.error_message for row in push.results}
    assert result["failure_policy"] == "abort"
    assert result["failed"] == 1
    assert result["skipped"] == 1
    assert push.status == RouterConfigPushStatus.failed
    assert statuses[first.id] == RouterPushResultStatus.failed
    assert statuses[second.id] == RouterPushResultStatus.skipped
    assert "aborted" in (errors[second.id] or "")
    assert not any(call[0] == "push-abort-b" for call in calls)


def test_execute_config_push_requires_readback_and_snapshot(db_session, monkeypatch):
    from app.models.network_operation import NetworkOperationStatus
    from app.tasks.router_sync import execute_config_push

    router = _make_router(db_session, "push-verified-test")
    push = RouterConfigService.create_push(
        db_session,
        commands=['/system/ntp/client/set {"enabled":"yes"}'],
        router_ids=[router.id],
        initiated_by=uuid.uuid4(),
    )
    calls = []

    def fake_execute(router_arg, method, path, payload=None, **kwargs):
        calls.append((method, path, payload, kwargs.get("max_retries")))
        if path == "/export":
            return "/exported config"
        if method == "POST":
            return {}
        return {"enabled": "yes"}

    monkeypatch.setattr(
        "app.tasks.router_sync.db_session_adapter.create_session",
        lambda: db_session,
    )
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        "app.tasks.router_sync.RouterConnectionService.execute", fake_execute
    )

    outcome = execute_config_push.run(str(push.id))

    db_session.refresh(push)
    result = push.results[0]
    db_session.refresh(result)
    assert outcome["success"] == 1
    assert result.status == RouterPushResultStatus.success
    assert result.pre_snapshot_id is not None
    assert result.post_snapshot_id is not None
    assert result.response_data["verified"] is True
    assert calls[1] == (
        "POST",
        "/system/ntp/client/set",
        {"enabled": "yes"},
        1,
    )
    assert calls[2][0:2] == ("GET", "/system/ntp/client")
    assert result.operation.status == NetworkOperationStatus.succeeded
    assert push.operation.status == NetworkOperationStatus.succeeded


def test_ambiguous_write_waits_for_reconciliation(db_session, monkeypatch):
    from app.models.network_operation import NetworkOperationStatus
    from app.services.router_management.connection import RouterTransportError
    from app.tasks.router_sync import (
        execute_config_push,
        reconcile_config_push_readback,
    )

    router = _make_router(db_session, "push-pending-readback-test")
    push = RouterConfigService.create_push(
        db_session,
        commands=['/system/ntp/client/set {"enabled":"yes"}'],
        router_ids=[router.id],
        initiated_by=uuid.uuid4(),
    )

    def ambiguous_execute(router_arg, method, path, payload=None, **kwargs):
        if path == "/export":
            return "/exported config"
        if method == "POST":
            raise RouterTransportError("response timed out")
        raise AssertionError("readback must not run after ambiguous delivery")

    monkeypatch.setattr(
        "app.tasks.router_sync.db_session_adapter.create_session",
        lambda: db_session,
    )
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        "app.tasks.router_sync.RouterConnectionService.execute", ambiguous_execute
    )

    outcome = execute_config_push.run(str(push.id))
    db_session.refresh(push)
    result = push.results[0]
    db_session.refresh(result)
    assert outcome["pending_readback"] == 1
    assert push.status == RouterConfigPushStatus.pending_readback
    assert push.completed_at is None
    assert result.status == RouterPushResultStatus.pending_readback
    assert result.operation.status == NetworkOperationStatus.waiting

    def recovered_execute(router_arg, method, path, payload=None, **kwargs):
        if path == "/export":
            return "/reconciled config"
        assert method == "GET"
        return {"enabled": "yes"}

    monkeypatch.setattr(
        "app.tasks.router_sync.RouterConnectionService.execute", recovered_execute
    )
    stats = reconcile_config_push_readback.run()

    db_session.refresh(push)
    db_session.refresh(result)
    assert stats == {"checked": 1, "verified": 1, "drifted": 0, "pending": 0}
    assert result.status == RouterPushResultStatus.success
    assert result.post_snapshot_id is not None
    assert result.operation.status == NetworkOperationStatus.succeeded
    assert push.status == RouterConfigPushStatus.completed
    assert push.operation.status == NetworkOperationStatus.succeeded


def test_audit_persistence_recovery_marks_pending_readback(db_session, monkeypatch):
    from app.models.network_operation import NetworkOperationStatus
    from app.tasks.router_sync import _recover_pending_readback

    router = _make_router(db_session, "push-audit-recovery-test")
    push = RouterConfigService.create_push(
        db_session,
        commands=['/system/ntp/client/set {"enabled":"yes"}'],
        router_ids=[router.id],
        initiated_by=uuid.uuid4(),
    )
    result = push.results[0]
    monkeypatch.setattr(
        "app.tasks.router_sync.db_session_adapter.create_session",
        lambda: db_session,
    )
    monkeypatch.setattr(db_session, "close", lambda: None)
    failed_db = MagicMock()

    _recover_pending_readback(
        failed_db,
        result.id,
        "audit transaction failed",
        {"write_accepted": True, "verified": True},
    )
    failed_db.rollback.assert_called_once()

    db_session.refresh(push)
    db_session.refresh(result)
    assert push.status == RouterConfigPushStatus.pending_readback
    assert result.status == RouterPushResultStatus.pending_readback
    assert result.response_data["verified"] is True
    assert result.operation.status == NetworkOperationStatus.waiting
    assert push.operation.status == NetworkOperationStatus.waiting
