from types import SimpleNamespace

import app.web.admin as admin_root
from app.models.system_user import SystemUser, SystemUserType
from app.services import web_admin as web_admin_service
from app.web.admin import billing_accounts as admin_billing_accounts
from app.web.admin import billing_dunning as admin_billing_dunning
from app.web.admin import billing_invoices as admin_billing_invoices
from app.web.admin import billing_payments as admin_billing_payments
from app.web.admin import catalog as admin_catalog
from app.web.admin import customers as admin_customers
from app.web.admin import nas as admin_nas
from app.web.admin import network_core_devices as admin_network_core_devices
from app.web.admin import network_cpes as admin_network_cpes
from app.web.admin import network_dns_threats as admin_network_dns_threats
from app.web.admin import network_fiber_plant as admin_network_fiber_plant
from app.web.admin import network_ip_management as admin_network_ip_management
from app.web.admin import network_olts_profiles as admin_network_olts_profiles
from app.web.admin import network_onts_inventory as admin_network_onts_inventory
from app.web.admin import network_pop_sites as admin_network_pop_sites
from app.web.admin import network_radius as admin_network_radius
from app.web.admin import network_site_survey as admin_network_site_survey
from app.web.admin import network_speedtests as admin_network_speedtests
from app.web.admin import network_tr069 as admin_network_tr069
from app.web.admin import provisioning as admin_provisioning
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

    current_user = web_admin_service.get_current_user(
        _request_with_user(system_user)
    )

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
    assert admin_billing_accounts._actor_id(request) == "system-user-1"
    assert admin_billing_dunning._actor_id(request) == "system-user-1"
    assert admin_billing_invoices._actor_id(request) == "system-user-1"
    assert admin_billing_payments._actor_id(request) == "system-user-1"
    assert admin_customers._actor_id(request) == "system-user-1"
    assert admin_nas._actor_id(request) == "system-user-1"
    assert admin_network_core_devices._actor_id(request) == "system-user-1"
    assert admin_network_cpes._actor_id(request) == "system-user-1"
    assert admin_network_dns_threats._actor_id(request) == "system-user-1"
    assert admin_network_fiber_plant._actor_id(request) == "system-user-1"
    assert admin_network_fiber_plant._actor_type(request) == "system_user"
    assert admin_network_ip_management._actor_id(request) == "system-user-1"
    assert admin_network_olts_profiles._actor_id(request) == "system-user-1"
    assert admin_network_onts_inventory._actor_id(request) == "system-user-1"
    assert admin_network_pop_sites._actor_id(request) == "system-user-1"
    assert admin_network_radius._actor_id(request) == "system-user-1"
    assert admin_network_speedtests._actor_id(request) == "system-user-1"
    assert admin_network_site_survey._actor_id(request) == "system-user-1"
    assert admin_network_tr069._actor_id(request) == "system-user-1"
    assert admin_provisioning._actor_id(request) == "system-user-1"
    assert admin_provisioning._actor_subscriber_id(request) == "legacy-subscriber-id"
    assert admin_wireguard._get_actor_id(request) == "system-user-1"


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
