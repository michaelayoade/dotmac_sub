from types import SimpleNamespace

import app.web.admin as admin_root
from app.models.system_user import SystemUser, SystemUserType
from app.services import tr069_web_audit, web_network_cpe_audit, web_network_ont_actions
from app.services import web_admin as web_admin_service
from app.services.network import olt_web_audit, ont_web_forms
from app.web.admin import catalog as admin_catalog
from app.web.admin import provisioning as admin_provisioning
from app.web.admin import support_tickets as admin_support_tickets
from app.web.admin import wireguard as admin_wireguard


def _request_with_user(user, principal_type: str = "system_user"):
    return SimpleNamespace(
        state=SimpleNamespace(
            user=user,
            auth={"principal_type": principal_type},
        )
    )


def test_get_current_user_exposes_stable_actor_id_for_system_user():
    system_user = SimpleNamespace(
        id="system-user-1",
        person_id=None,
        first_name="Admin",
        last_name="User",
        email="admin@example.com",
    )

    current_user = web_admin_service.get_current_user(_request_with_user(system_user))

    assert current_user["id"] == "system-user-1"
    assert current_user["actor_id"] == "system-user-1"
    assert current_user["principal_id"] == "system-user-1"
    assert current_user["subscriber_id"] == ""
    assert current_user["person_id"] == ""


def test_admin_audit_actor_helpers_prefer_stable_principal_id(monkeypatch):
    current_user = {
        "id": "system-user-1",
        "actor_id": "system-user-1",
        "subscriber_id": "legacy-subscriber-id",
        "principal_type": "system_user",
    }
    request = SimpleNamespace()

    monkeypatch.setattr(admin_root, "get_current_user", lambda _request: current_user)
    monkeypatch.setattr(
        web_admin_service, "get_current_user", lambda _request: current_user
    )

    assert admin_catalog._get_actor_id(request) == "system-user-1"
    assert admin_provisioning._actor_id(request) == "system-user-1"
    assert admin_support_tickets._actor_id(request) == "system-user-1"
    assert admin_wireguard._get_actor_id(request) == "system-user-1"
    assert web_network_cpe_audit.actor_id_from_request(request) == "system-user-1"
    assert tr069_web_audit.actor_id_from_request(request) == "system-user-1"
    assert olt_web_audit.actor_id_from_request(request) == "system-user-1"
    assert web_network_ont_actions._actor_id_from_request(request) == "system-user-1"
    assert ont_web_forms._actor_id_from_request(request) == "system-user-1"


def test_ont_action_actor_context_uses_cached_plain_values(monkeypatch):
    calls = {"count": 0}
    request = SimpleNamespace(state=SimpleNamespace())

    def fake_get_current_user(_request):
        calls["count"] += 1
        if calls["count"] > 1:
            raise AssertionError("request user should not be reloaded")
        return {
            "id": "system-user-1",
            "actor_id": "system-user-1",
            "subscriber_id": "",
            "name": "Admin User",
            "email": "admin@example.com",
        }

    monkeypatch.setattr(admin_root, "get_current_user", fake_get_current_user)

    assert web_network_ont_actions.cache_current_user_context(request) == {
        "id": "system-user-1",
        "actor_id": "system-user-1",
        "subscriber_id": "",
        "name": "Admin User",
        "email": "admin@example.com",
    }
    assert web_network_ont_actions._actor_id_from_request(request) == "system-user-1"
    assert web_network_ont_actions.actor_name_from_request(request) == "Admin User"
    assert calls["count"] == 1


def test_ont_action_audit_failure_does_not_raise(monkeypatch):
    request = SimpleNamespace(
        state=SimpleNamespace(
            _dotmac_cached_user_context={
                "actor_id": "system-user-1",
                "subscriber_id": "",
                "name": "Admin User",
                "email": "admin@example.com",
            }
        )
    )
    db = SimpleNamespace(rollback=lambda: None)

    def fail_audit(**_kwargs):
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(
        "app.services.web_network_ont_actions._common.log_audit_event",
        fail_audit,
    )

    web_network_ont_actions._log_action_audit(
        db,
        request=request,
        action="set_wifi_config",
        ont_id="ont-1",
        metadata={"success": True},
    )


def test_get_uploaded_by_subscriber_id_returns_none_for_system_user_without_subscriber(
    db_session,
):
    system_user = SystemUser(
        first_name="Admin",
        last_name="Only",
        email="admin-only@example.com",
        user_type=SystemUserType.system_user,
        is_active=True,
    )
    db_session.add(system_user)
    db_session.commit()

    request = _request_with_user(system_user)

    assert web_admin_service.get_uploaded_by_subscriber_id(request, db_session) is None
