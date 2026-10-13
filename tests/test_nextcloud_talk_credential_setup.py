"""Fast logic checks; database fixtures are not PostgreSQL concurrency evidence."""

from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.integration_platform import IntegrationConfigRevision
from app.monitoring import _scrub_event_credentials
from app.services import secrets, web_system_secrets
from app.services.integrations import installations
from app.services.integrations import nextcloud_talk_installation as owner
from app.services.integrations.runtime import ValidationResult
from app.services.owner_commands import CommandContext

PASSWORD = "test-only-transient-sentinel-credential"


def _context():
    return CommandContext.system(
        actor="test.talk",
        scope=owner.TALK_CREDENTIAL_SCOPE,
        reason="Test credential setup",
        idempotency_key=str(uuid4()),
    )


def _installation(db):
    installation = installations.create_draft(
        db, connector_key="nextcloud.talk", name="Talk", environment="test"
    )
    installations.create_config_revision(
        db,
        installation_id=installation.id,
        config={
            "url": "https://next.example.test",
            "notifier_username": "old",
            "timeout_seconds": 12,
        },
        secret_refs={"app_password": "bao://secret/integrations/existing#app_password"},
    )
    installations.bind_capability(
        db,
        installation_id=installation.id,
        capability_id="collaboration.message.send.v1",
        scope={"staff": "approved"},
        policy={"approved_egress_hosts": ["next.example.test"], "default": True},
    )
    db.commit()
    command = owner.ConfigureTalkCredential(
        context=_context(),
        installation_id=installation.id,
        expected_revision_id=installation.current_config_revision_id,
        expected_pin=installations.ManifestPin(
            installation.connector_version, installation.manifest_digest
        ),
        operation_id=uuid4(),
        username="notifier",
        app_password=SecretStr(PASSWORD),
    )
    identity = installation.id
    db.rollback()
    return identity, command


def _transport(monkeypatch, *, valid=True):
    values = {}

    def create(command):
        reference = secrets.talk_credential_reference(command)
        if reference in values and values[reference] != (
            command.password.get_secret_value(),
            command.configuration_digest,
        ):
            raise secrets._immutable_error("conflict")
        replayed = reference in values
        values[reference] = (
            command.password.get_secret_value(),
            command.configuration_digest,
        )
        return secrets.ImmutableCredentialResult(reference, replayed)

    monkeypatch.setattr(owner, "create_talk_credential", create)
    monkeypatch.setattr(
        owner,
        "validate_installation_connection",
        lambda *args, **kwargs: ValidationResult(valid=valid),
    )
    return values


def test_setup_preserves_manifest_configuration_scope_policy_and_replays(
    db_session, monkeypatch
):
    identity, command = _installation(db_session)
    values = _transport(monkeypatch)
    result = owner.configure_talk_credential(db_session, command)
    assert result.status is owner.TalkCredentialStatus.enabled
    row = installations.get_installation(db_session, identity)
    assert row.current_config_revision.config_json == {
        "url": "https://next.example.test",
        "notifier_username": "notifier",
        "timeout_seconds": 12,
    }
    assert row.capability_bindings[0].scope_json == {"staff": "approved"}
    assert row.capability_bindings[0].policy_json["approved_egress_hosts"] == [
        "next.example.test"
    ]
    assert len(values) == 1
    assert PASSWORD not in repr(command)
    assert PASSWORD not in repr(
        owner.get_talk_credential_projection(db_session, installation_id=identity)
    )
    assert PASSWORD not in repr(
        [r.details for r in db_session.scalars(select(AuditEvent)).all()]
    )
    db_session.rollback()
    repeated = owner.configure_talk_credential(db_session, command)
    assert repeated.replayed and repeated.revision_id == result.revision_id
    assert len(values) == 1


def test_stale_and_pin_reject_before_store(db_session, monkeypatch):
    _, command = _installation(db_session)
    monkeypatch.setattr(
        owner, "create_talk_credential", lambda _: pytest.fail("must not write")
    )
    with pytest.raises(owner.TalkCredentialError, match="no longer valid"):
        owner.configure_talk_credential(
            db_session, replace(command, expected_revision_id=uuid4())
        )
    with pytest.raises(owner.TalkCredentialError):
        owner.configure_talk_credential(
            db_session,
            replace(command, expected_pin=installations.ManifestPin("0.0.0", "x")),
        )


def test_validation_failure_commits_disabled_candidate_then_new_operation_recovers(
    db_session, monkeypatch
):
    identity, command = _installation(db_session)
    _transport(monkeypatch, valid=False)
    failed = owner.configure_talk_credential(db_session, command)
    assert failed.status is owner.TalkCredentialStatus.validation_failed
    row = installations.get_installation(db_session, identity)
    assert row.state == "disabled" and row.capability_bindings[0].state == "disabled"
    assert failed.revision_id != command.expected_revision_id
    db_session.rollback()
    _transport(monkeypatch, valid=True)
    corrected = replace(
        command,
        operation_id=uuid4(),
        expected_revision_id=failed.revision_id,
        app_password=SecretStr("corrected-test-only"),
    )
    assert (
        owner.configure_talk_credential(db_session, corrected).status
        is owner.TalkCredentialStatus.enabled
    )


def test_old_operation_never_resurrects_and_same_operation_material_conflicts(
    db_session, monkeypatch
):
    identity, command = _installation(db_session)
    _transport(monkeypatch)
    first = owner.configure_talk_credential(db_session, command)
    with pytest.raises(owner.TalkCredentialError):
        owner.configure_talk_credential(db_session, replace(command, username="other"))
    with pytest.raises(owner.TalkCredentialError):
        owner.configure_talk_credential(
            db_session, replace(command, app_password=SecretStr("different-test-only"))
        )
    second = owner.configure_talk_credential(
        db_session,
        replace(command, operation_id=uuid4(), expected_revision_id=first.revision_id),
    )
    with pytest.raises(owner.TalkCredentialError):
        owner.configure_talk_credential(db_session, command)
    assert (
        installations.get_installation(db_session, identity).current_config_revision_id
        == second.revision_id
    )


def test_denied_store_commits_safe_failure_without_config_change(
    db_session, monkeypatch
):
    identity, command = _installation(db_session)

    def denied(_):
        raise secrets._immutable_error("denied")

    monkeypatch.setattr(owner, "create_talk_credential", denied)
    result = owner.configure_talk_credential(db_session, command)
    assert result.status is owner.TalkCredentialStatus.storage_unavailable
    assert (
        installations.get_installation(db_session, identity).current_config_revision_id
        == command.expected_revision_id
    )
    assert (
        db_session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "integration.nextcloud_talk.credentials.configure"
            )
        ).is_success
        is False
    )


def test_shared_lock_preserves_participant_pending_fields(db_session):
    identity, _ = _installation(db_session)
    row = installations.lock_installation(db_session, identity)
    row.capability_bindings[0].policy_json = {
        "approved_egress_hosts": ["next.example.test"],
        "changed": True,
    }
    installations.validate_static(db_session, installation_id=identity)
    assert row.capability_bindings[0].policy_json["changed"] is True


@pytest.mark.parametrize(
    "path",
    [
        "integrations/nextcloud-talk-credentials/a",
        "/integrations//nextcloud-talk-credentials/a",
        "x/../integrations/nextcloud-talk-credentials/a",
        "integrations/%6eextcloud-talk-credentials/a",
        "integrations/nextcloud-talk-credentials?ignored",
    ],
)
def test_generic_secret_mutations_cannot_change_managed_credentials(monkeypatch, path):
    monkeypatch.setattr(
        secrets, "_openbao_config", lambda: pytest.fail("must reject before network")
    )
    assert not secrets.write_secret(path, {"app_password": PASSWORD})
    assert not secrets.delete_secret(path)
    assert "status=error" in web_system_secrets.delete_secret_path(path)


def _store(
    monkeypatch,
    *,
    post_status=200,
    read_password=PASSWORD,
    malformed=False,
    ambiguous=False,
):
    calls = []
    monkeypatch.setattr(
        secrets,
        "_openbao_config",
        lambda: ("https://bao.example.test", "test-token", "unit-test", "2"),
    )

    def post(url, **kwargs):
        calls.append(kwargs)
        if ambiguous:
            raise httpx.ReadTimeout("test timeout")
        return httpx.Response(post_status)

    monkeypatch.setattr(secrets.httpx, "post", post)
    monkeypatch.setattr(
        secrets.httpx,
        "get",
        lambda *args, **kwargs: httpx.Response(
            200,
            json={
                "data": {
                    "data": []
                    if malformed
                    else {
                        "app_password": read_password,
                        "configuration_digest": "f" * 64,
                    }
                }
            },
        ),
    )
    return calls


@pytest.mark.parametrize(
    "post_status,ambiguous", [(200, False), (400, False), (409, False), (200, True)]
)
def test_immutable_store_uses_cas0_and_exact_bounded_readback(
    monkeypatch, post_status, ambiguous
):
    calls = _store(monkeypatch, post_status=post_status, ambiguous=ambiguous)
    command = secrets.CreateTalkCredential(
        uuid4(), uuid4(), "test", SecretStr(PASSWORD), "f" * 64
    )
    result = secrets.create_talk_credential(command)
    assert calls[0]["json"]["options"] == {"cas": 0}
    assert result.reference.endswith(
        f"/{command.installation_id}/{command.operation_id}#app_password"
    )
    assert PASSWORD not in repr(command) and PASSWORD not in repr(result)


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"post_status": 403}, "denied"),
        ({"read_password": "different"}, "conflict"),
        ({"malformed": True}, "unavailable"),
    ],
)
def test_immutable_store_failures_are_safe(monkeypatch, kwargs, code):
    _store(monkeypatch, **kwargs)
    with pytest.raises(secrets.ImmutableSecretError) as error:
        secrets.create_talk_credential(
            secrets.CreateTalkCredential(
                uuid4(), uuid4(), "test", SecretStr(PASSWORD), "f" * 64
            )
        )
    assert error.value.code.endswith(code) and PASSWORD not in str(error.value)


def test_credential_route_telemetry_retains_no_payload_or_exception():
    event = {
        "event_id": "safe-id",
        "request": {
            "url": "https://sub.example/admin/integrations/nextcloud-talk/abc",
            "data": {"app_password": PASSWORD},
        },
        "exception": {"values": [{"value": PASSWORD}]},
        "extra": {"credential": PASSWORD},
        "breadcrumbs": [{"message": PASSWORD}],
    }
    assert _scrub_event_credentials(event, None) == {"event_id": "safe-id"}


def test_real_runtime_validation_uses_only_room_get_and_exact_password(
    db_session, monkeypatch
):
    import base64

    from app.services.integrations.connectors import nextcloud_talk as runner

    identity, command = _installation(db_session)
    values = _transport(monkeypatch)
    monkeypatch.setattr(
        owner,
        "validate_installation_connection",
        __import__(
            "app.services.integrations.nextcloud_talk_capability",
            fromlist=["validate_installation_connection"],
        ).validate_installation_connection,
    )
    monkeypatch.setattr(runner, "_validate_public_host", lambda *args: None)
    requests = []

    class Client:
        def __init__(self, **kwargs):
            self.auth = kwargs["auth"]
            assert kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            requests.append((method, url, kwargs, self.auth))
            return httpx.Response(
                200, json={"ocs": {"meta": {"statuscode": 200}, "data": []}}
            )

    monkeypatch.setattr(runner.httpx, "Client", Client)
    # Resolve only the in-memory store written by this test, never OpenBao.
    from app.services.integrations import nextcloud_talk_capability as capability

    original = capability.validate_installation_connection
    monkeypatch.setattr(
        owner,
        "validate_installation_connection",
        lambda db, **kwargs: original(
            db, **kwargs, secret_resolver=lambda ref: values[ref][0]
        ),
    )
    command = replace(command, app_password=SecretStr("  test-only-exact-password  "))
    assert (
        owner.configure_talk_credential(db_session, command).status
        is owner.TalkCredentialStatus.enabled
    )
    assert len(requests) == 1
    method, url, kwargs, auth = requests[0]
    assert method == "GET" and url.endswith("/ocs/v2.php/apps/spreed/api/v4/room")
    request = httpx.Request("GET", url)
    authenticated = next(auth.auth_flow(request))
    assert (
        base64.b64decode(authenticated.headers["authorization"].split()[1]).decode()
        == "notifier:  test-only-exact-password  "
    )
    assert not kwargs["json"] and not kwargs["data"]


def test_sql_failure_leaves_only_immutable_orphan_and_exact_retry_recovers(
    db_session, monkeypatch
):
    identity, command = _installation(db_session)
    values = _transport(monkeypatch)
    from app.services.integrations import nextcloud_talk_installation as module

    original_evidence = module._evidence

    def fail(*args, **kwargs):
        raise RuntimeError("simulated SQL failure before commit")

    monkeypatch.setattr(module, "_evidence", fail)
    with pytest.raises(RuntimeError):
        owner.configure_talk_credential(db_session, command)
    assert len(values) == 1
    assert (
        installations.get_installation(db_session, identity).current_config_revision_id
        == command.expected_revision_id
    )
    db_session.rollback()
    monkeypatch.setattr(module, "_evidence", original_evidence)
    recovered = owner.configure_talk_credential(db_session, command)
    assert recovered.status is owner.TalkCredentialStatus.enabled and len(values) == 1


def _ui_client(db_session, monkeypatch, permissions, *, admin=False):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import csrf_middleware
    from app.services import auth_dependencies
    from app.web.admin import integrations as routes

    app = FastAPI()
    app.middleware("http")(csrf_middleware)
    app.include_router(routes.router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: db_session

    principal_id = str(uuid4())

    async def auth(request: Request):
        principal = {
            "principal_id": principal_id,
            "roles": ["admin"] if admin else [],
            "scopes": permissions,
            "permission_keys": frozenset({"*"} if admin else permissions),
        }
        request.state.auth = principal
        return principal

    app.dependency_overrides[auth_dependencies.require_user_auth] = auth
    monkeypatch.setattr(
        auth_dependencies, "_enforce_staff_leave_write_guard", lambda **kwargs: None
    )
    monkeypatch.setattr(
        routes,
        "_base_context",
        lambda request, db, **kwargs: {
            "request": request,
            "current_user": {},
            "sidebar_stats": {},
        },
    )
    monkeypatch.setattr(routes, "recent_activity_for_paths", lambda *args: [])
    return TestClient(app)


def _form(response):
    import re

    return dict(re.findall(r'name="([^"]+)" value="([^"]*)"', response.text))


@pytest.mark.parametrize("admin", [False, True])
def test_composed_editor_permissions_csrf_redaction_and_retry(
    db_session, monkeypatch, admin
):
    identity, _ = _installation(db_session)
    _transport(monkeypatch, valid=False)
    permissions = [
        "system:settings:read",
        "system:settings:write",
        "system:secrets:write",
    ]
    client = _ui_client(db_session, monkeypatch, permissions, admin=admin)
    url = f"/admin/integrations/nextcloud-talk/{identity}"
    page = client.get(url)
    assert page.status_code == 200
    assert 'type="password" name="app_password"' in page.text
    assert "bao://" not in page.text and PASSWORD not in page.text
    fields = _form(page)
    original_operation = fields["operation_id"]
    fields.update(username="notifier", app_password=PASSWORD)
    blocked = client.post(url, data={**fields, "_csrf_token": "wrong"})
    assert blocked.status_code == 403 and PASSWORD not in blocked.text
    failed = client.post(url, data=fields)
    assert failed.status_code == 400 and PASSWORD not in failed.text
    assert "Talk remains disabled" in failed.text
    assert _form(failed)["operation_id"] != original_operation
    malformed = client.post(
        url, data={**_form(failed), "revision_id": PASSWORD, "app_password": PASSWORD}
    )
    assert malformed.status_code == 400 and PASSWORD not in malformed.text


@pytest.mark.parametrize("write", ["system:settings:write", "system:secrets:write"])
def test_each_write_permission_alone_cannot_submit_or_see_form(
    db_session, monkeypatch, write
):
    identity, _ = _installation(db_session)
    client = _ui_client(db_session, monkeypatch, ["system:settings:read", write])
    url = f"/admin/integrations/nextcloud-talk/{identity}"
    page = client.get(url)
    assert page.status_code == 200 and 'name="app_password"' not in page.text
    response = client.post(
        url,
        data={"_csrf_token": client.cookies["csrf_token"], "app_password": PASSWORD},
    )
    assert response.status_code == 403 and PASSWORD not in response.text


def test_orphan_operation_rejects_changed_config_and_global_setting_remains(
    db_session, monkeypatch
):
    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.models.event_store import EventStore

    identity, command = _installation(db_session)
    flag = DomainSetting(
        domain=SettingDomain.notification,
        key="nextcloud_talk_staff_notifications_enabled",
        value_text="false",
        is_active=True,
    )
    db_session.add(flag)
    db_session.commit()
    values = _transport(monkeypatch)
    original = owner._evidence

    def sql_failure(*args, **kwargs):
        raise RuntimeError("test SQL failure")

    monkeypatch.setattr(owner, "_evidence", sql_failure)
    with pytest.raises(RuntimeError):
        owner.configure_talk_credential(db_session, command)
    monkeypatch.setattr(owner, "_evidence", original)
    with pytest.raises(owner.TalkCredentialError) as error:
        owner.configure_talk_credential(
            db_session, replace(command, username="changed-notifier")
        )
    assert error.value.code.endswith("talk_credential_conflict")
    result = owner.configure_talk_credential(db_session, command)
    assert result.status is owner.TalkCredentialStatus.enabled and len(values) == 1
    db_session.refresh(flag)
    assert flag.value_text == "false"
    events = list(
        db_session.scalars(
            select(EventStore).where(
                EventStore.event_type
                == "integration.installation.talk_credential_configured"
            )
        ).all()
    )
    assert events
    assert PASSWORD not in repr([row.payload for row in events])
    assert PASSWORD not in repr(
        [
            (row.config_json, row.secret_refs)
            for row in db_session.scalars(select(IntegrationConfigRevision)).all()
        ]
    )


def test_managed_generic_editor_rejects_before_reading_password(monkeypatch):
    monkeypatch.setattr(
        secrets,
        "read_secret_fields",
        lambda _: pytest.fail("must not read managed credential for edit"),
    )
    path = f"{secrets.IMMUTABLE_TALK_PREFIX}/unit-test"
    assert (
        "managed by its integration"
        in web_system_secrets.save_secret(path, {})["error"]
    )
    assert (
        "managed by its integration"
        in web_system_secrets.create_secret({"path": path})["error"]
    )


def test_storage_retry_keeps_operation_and_conflicting_password_is_safe(
    db_session, monkeypatch
):
    identity, _ = _installation(db_session)
    client = _ui_client(
        db_session,
        monkeypatch,
        ["system:settings:read", "system:settings:write", "system:secrets:write"],
    )
    url = f"/admin/integrations/nextcloud-talk/{identity}"
    fields = _form(client.get(url))
    fields.update(username="notifier", app_password=PASSWORD)

    def unavailable(_):
        raise secrets._immutable_error("unavailable")

    monkeypatch.setattr(owner, "create_talk_credential", unavailable)
    response = client.post(url, data=fields)
    assert response.status_code == 503 and PASSWORD not in response.text
    assert _form(response)["operation_id"] == fields["operation_id"]
    _transport(monkeypatch)
    success = client.post(url, data=fields)
    assert success.status_code == 200
    conflict = client.post(url, data={**fields, "app_password": "other-test-only"})
    assert conflict.status_code == 400 and "already stored different" in conflict.text
    assert PASSWORD not in conflict.text and "other-test-only" not in conflict.text


def test_unknown_and_wrong_connector_editor_targets_fail_safely(
    db_session, monkeypatch
):
    other = installations.create_draft(
        db_session, connector_key="whatsapp", name="Wrong target"
    )
    identity = other.id
    db_session.commit()
    client = _ui_client(db_session, monkeypatch, [], admin=True)
    for target in (uuid4(), identity):
        url = f"/admin/integrations/nextcloud-talk/{target}"
        assert client.get(url).status_code == 404
        response = client.post(
            url,
            data={
                "_csrf_token": client.cookies["csrf_token"],
                "app_password": PASSWORD,
                "operation_id": str(uuid4()),
                "revision_id": str(uuid4()),
            },
        )
        assert response.status_code == 404 and PASSWORD not in response.text
