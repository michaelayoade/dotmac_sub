"""CPE-detail WiFi actions delegate to the ONT service-configuration owner.

Covers the cutover away from ``cpe_action_wifi.py`` writing to GenieACS
directly (bypassing ``OntServiceConfigurationRevision``/durable dispatch) and
away from ``resolve_genieacs_for_cpe_with_reason``'s ambiguous fallback
resolution (serial-number match across all active TR-069 rows, or a default
ACS server) for this specific admin mutation path.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.catalog import SubscriptionStatus
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    OntAssignment,
    OntAuthorizationStatus,
    OntUnit,
    PonPort,
)
from app.models.network_operation import NetworkOperation, NetworkOperationDispatch
from app.models.ont_service_configuration import OntServiceConfigurationRevision
from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
from app.services.domain_errors import DomainError
from app.services.network import cpe_action_wifi
from app.services.network.ont_service_configuration import (
    resolve_cpe_wifi_admission_scope,
)
from app.services.owner_commands import CommandContext


def _acs_server(db_session) -> Tr069AcsServer:
    server = Tr069AcsServer(name="Test ACS", base_url="http://acs.test.local")
    db_session.add(server)
    db_session.flush()
    return server


def _cpe_device(db_session, subscriber) -> CPEDevice:
    cpe = CPEDevice(
        subscriber_id=subscriber.id,
        status=DeviceStatus.active,
        serial_number=f"CPE-{uuid.uuid4().hex[:10]}",
    )
    db_session.add(cpe)
    db_session.flush()
    return cpe


def _eligible_ont_with_assignment(
    db_session, *, olt_device, subscription, subscriber
) -> tuple[OntUnit, OntAssignment]:
    """Build the full admission-eligible ONT/PON/assignment/subscription chain.

    Mirrors ``tests/test_ont_service_configuration.py::_admission_scope`` --
    the same shape ``_load_admission_scope`` requires -- since delegating to
    ``configure_ont_service`` re-verifies this chain under row locks.
    """
    subscription.status = SubscriptionStatus.active
    olt_device.is_active = True
    pon = PonPort(
        olt_id=olt_device.id,
        name=f"0/1/{uuid.uuid4().int % 100000}",
        is_active=True,
    )
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"CPE-WIFI-{uuid.uuid4().hex[:10]}",
        is_active=True,
        authorization_status=OntAuthorizationStatus.authorized,
        olt_device_id=olt_device.id,
        pon_port_id=pon.id,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        pon_port_id=pon.id,
        active=True,
    )
    db_session.add(assignment)
    db_session.commit()
    return ont, assignment


def _tr069_device(
    db_session, *, server: Tr069AcsServer, cpe: CPEDevice, ont: OntUnit | None
) -> Tr069CpeDevice:
    device = Tr069CpeDevice(
        acs_server_id=server.id,
        cpe_device_id=cpe.id,
        ont_unit_id=ont.id if ont is not None else None,
        serial_number=cpe.serial_number,
        is_active=True,
    )
    db_session.add(device)
    db_session.commit()
    return device


def _command_context(*, idempotency_key: str) -> CommandContext:
    command_id = uuid.uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="test:admin",
        scope="network:ont:write",
        reason="CPE-detail WiFi cutover test",
        idempotency_key=idempotency_key,
    )


def _stub_effective_wifi(monkeypatch, **overrides) -> None:
    values = {
        "wifi_enabled": None,
        "wifi_ssid": None,
        "wifi_channel": None,
        "wifi_security_mode": None,
        **overrides,
    }
    monkeypatch.setattr(
        "app.services.network.cpe_action_wifi.resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "config_pack": {"id": "test-pack"},
            "values": values,
        },
    )


# ── resolve_cpe_wifi_admission_scope: the eligibility chain ─────────────


def test_resolves_exactly_one_active_cpe_to_its_ont(
    db_session, olt_device, subscription, subscriber
):
    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    ont, assignment = _eligible_ont_with_assignment(
        db_session,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    _tr069_device(db_session, server=server, cpe=cpe, ont=ont)

    scope = resolve_cpe_wifi_admission_scope(db_session, cpe.id)

    assert scope.ont_unit_id == ont.id
    assert scope.assignment_id == assignment.id


def test_cpe_device_not_found_refuses_when_no_active_tr069_row_matches(
    db_session, subscriber
):
    cpe = _cpe_device(db_session, subscriber)
    # No Tr069CpeDevice row at all references this cpe.id.

    with pytest.raises(DomainError) as exc_info:
        resolve_cpe_wifi_admission_scope(db_session, cpe.id)

    assert exc_info.value.code.endswith("cpe_device_not_found")


def test_cpe_device_ambiguous_refuses_when_multiple_active_rows_match(
    db_session, subscriber
):
    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    _tr069_device(db_session, server=server, cpe=cpe, ont=None)
    _tr069_device(db_session, server=server, cpe=cpe, ont=None)

    with pytest.raises(DomainError) as exc_info:
        resolve_cpe_wifi_admission_scope(db_session, cpe.id)

    assert exc_info.value.code.endswith("cpe_device_ambiguous")
    assert exc_info.value.details["candidate_count"] == 2


def test_cpe_ont_not_linked_refuses_when_tr069_row_has_no_ont(db_session, subscriber):
    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    _tr069_device(db_session, server=server, cpe=cpe, ont=None)

    with pytest.raises(DomainError) as exc_info:
        resolve_cpe_wifi_admission_scope(db_session, cpe.id)

    assert exc_info.value.code.endswith("cpe_ont_not_linked")


def test_cpe_ont_missing_refuses_when_linked_ont_row_cannot_be_found(
    db_session, olt_device, subscriber, monkeypatch
):
    """A dangling ``ont_unit_id`` whose ``OntUnit`` row is gone is distinct
    from having no link at all. ``ont_unit_id`` carries a real foreign key
    (``tr069_cpe_devices.ont_unit_id -> ont_units.id``), so the deployed
    schema prevents manufacturing this drift through ordinary writes --
    simulate the authoritative ONT lookup missing after a genuinely linked
    row is found, mirroring
    ``test_ont_service_configuration.py``'s identical
    ``customer_assigned_ont_missing`` test for the exact same reason.
    """
    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    pon = PonPort(olt_id=olt_device.id, name=f"0/1/{uuid.uuid4().int % 100000}")
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"MISSING-{uuid.uuid4().hex[:8]}",
        is_active=True,
        olt_device_id=olt_device.id,
        pon_port_id=pon.id,
    )
    db_session.add(ont)
    db_session.flush()
    _tr069_device(db_session, server=server, cpe=cpe, ont=ont)
    monkeypatch.setattr(db_session, "scalar", lambda *_args, **_kwargs: None)

    with pytest.raises(DomainError) as exc_info:
        resolve_cpe_wifi_admission_scope(db_session, cpe.id)

    assert exc_info.value.code.endswith("cpe_ont_missing")


def test_cpe_assignment_inactive_refuses_when_ont_has_no_active_assignment(
    db_session, olt_device, subscriber
):
    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    pon = PonPort(olt_id=olt_device.id, name=f"0/1/{uuid.uuid4().int % 100000}")
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"NOASSIGN-{uuid.uuid4().hex[:8]}",
        is_active=True,
        olt_device_id=olt_device.id,
        pon_port_id=pon.id,
    )
    db_session.add(ont)
    db_session.flush()
    _tr069_device(db_session, server=server, cpe=cpe, ont=ont)

    with pytest.raises(DomainError) as exc_info:
        resolve_cpe_wifi_admission_scope(db_session, cpe.id)

    assert exc_info.value.code.endswith("cpe_assignment_inactive")


# NOTE: there is deliberately no DB-fixture-backed test for
# ``cpe_assignment_ambiguous`` (more than one active ``OntAssignment`` for the
# SAME ``ont_unit_id``). ``ix_ont_assignments_active_unit`` is a real
# Postgres partial unique index that makes that exact row shape impossible to
# construct via valid writes -- the same reason the pre-existing
# ``_load_admission_scope``/``ambiguous_assignment`` branch it mirrors has no
# such test either. The code path is kept for the same defense-in-depth
# reason the owner keeps its own (a future relaxation of that index, or a
# race before it commits, would otherwise silently pick one row via
# ``.limit(1)``-style resolution instead of refusing) -- flagged here rather
# than silently left untested.


# ── cpe_action_wifi.set_wifi_ssid / set_wifi_password: full delegation ──


def test_set_wifi_ssid_delegates_to_the_ont_service_configuration_owner(
    db_session, monkeypatch, olt_device, subscription, subscriber
):
    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    ont, assignment = _eligible_ont_with_assignment(
        db_session,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    _tr069_device(db_session, server=server, cpe=cpe, ont=ont)
    _stub_effective_wifi(monkeypatch, wifi_channel="6", wifi_security_mode="wpa2")

    result = cpe_action_wifi.set_wifi_ssid(
        db_session,
        str(cpe.id),
        "NewHomeWifi",
        context=_command_context(idempotency_key=f"ssid-{uuid.uuid4()}"),
        permission_granted=True,
    )

    assert result.success is True
    assert result.data is not None
    operation_id = uuid.UUID(str(result.data["operation_id"]))
    operation = db_session.get(NetworkOperation, operation_id)
    assert operation is not None
    revision = db_session.scalar(
        select(OntServiceConfigurationRevision).where(
            OntServiceConfigurationRevision.operation_id == operation_id
        )
    )
    assert revision is not None
    dispatch = db_session.scalar(
        select(NetworkOperationDispatch).where(
            NetworkOperationDispatch.operation_id == operation_id
        )
    )
    assert dispatch is not None
    db_session.refresh(ont)
    assert ont.desired_config["wifi"]["ssid"] == "NewHomeWifi"
    # Fields not part of this change are carried forward, not cleared.
    assert ont.desired_config["wifi"]["channel"] == "6"
    assert ont.desired_config["wifi"]["security_mode"] == "wpa2"


def test_set_wifi_password_delegates_to_the_ont_service_configuration_owner(
    db_session, monkeypatch, olt_device, subscription, subscriber
):
    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    ont, assignment = _eligible_ont_with_assignment(
        db_session,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    _tr069_device(db_session, server=server, cpe=cpe, ont=ont)
    _stub_effective_wifi(monkeypatch, wifi_ssid="ExistingSSID")

    result = cpe_action_wifi.set_wifi_password(
        db_session,
        str(cpe.id),
        "correct-horse-battery",
        context=_command_context(idempotency_key=f"pwd-{uuid.uuid4()}"),
        permission_granted=True,
    )

    assert result.success is True
    db_session.refresh(ont)
    assert ont.desired_config["wifi"]["ssid"] == "ExistingSSID"
    assert ont.desired_config["wifi"]["password"] is not None
    assert ont.desired_config["wifi"]["password"] != "correct-horse-battery"


def test_set_wifi_ssid_refuses_without_owner_permission(
    db_session, monkeypatch, olt_device, subscription, subscriber
):
    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    ont, _assignment = _eligible_ont_with_assignment(
        db_session,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    _tr069_device(db_session, server=server, cpe=cpe, ont=ont)
    _stub_effective_wifi(monkeypatch)

    result = cpe_action_wifi.set_wifi_ssid(
        db_session,
        str(cpe.id),
        "NewHomeWifi",
        context=_command_context(idempotency_key=f"denied-{uuid.uuid4()}"),
        permission_granted=False,
    )

    assert result.success is False
    assert result.error_code is not None
    assert result.error_code.endswith("permission_denied")


@pytest.mark.parametrize(
    "cause",
    ["not_found", "ambiguous", "ont_not_linked", "assignment_inactive"],
)
def test_typed_blockers_refuse_before_any_delegation(
    db_session, monkeypatch, olt_device, subscription, subscriber, cause
):
    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)

    if cause == "not_found":
        pass
    elif cause == "ambiguous":
        _tr069_device(db_session, server=server, cpe=cpe, ont=None)
        _tr069_device(db_session, server=server, cpe=cpe, ont=None)
    elif cause == "ont_not_linked":
        _tr069_device(db_session, server=server, cpe=cpe, ont=None)
    elif cause == "assignment_inactive":
        pon = PonPort(olt_id=olt_device.id, name=f"0/1/{uuid.uuid4().int % 100000}")
        db_session.add(pon)
        db_session.flush()
        ont = OntUnit(
            serial_number=f"BLOCK-{uuid.uuid4().hex[:8]}",
            is_active=True,
            olt_device_id=olt_device.id,
            pon_port_id=pon.id,
        )
        db_session.add(ont)
        db_session.flush()
        _tr069_device(db_session, server=server, cpe=cpe, ont=ont)

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError(f"configure_ont_service must not be called ({cause})")

    monkeypatch.setattr(
        "app.services.network.cpe_action_wifi.configure_ont_service", _fail_if_called
    )

    result = cpe_action_wifi.set_wifi_ssid(
        db_session,
        str(cpe.id),
        "NewHomeWifi",
        context=_command_context(idempotency_key=f"{cause}-{uuid.uuid4()}"),
        permission_granted=True,
    )

    assert result.success is False
    assert result.error_code is not None
    assert result.error_code != ""


# ── Structural proof: no direct GenieACS write on this path ─────────────


def test_no_direct_genieacs_write_on_successful_cpe_wifi_path(
    db_session, monkeypatch, olt_device, subscription, subscriber
):
    """Structural write-incapability proof for the CPE-detail WiFi path.

    Patches the exact GenieACS write function (``set_and_verify``, still used
    by the untouched ``toggle_lan_port`` action in the same module) and the
    GenieACS client constructor to blow up if either is ever invoked, then
    runs the full eligible SSID and password change paths end to end. Both
    must succeed via the durable owner without ever reaching ACS -- proving
    ``configure_ont_service``'s admission+staging is genuinely write-free,
    not merely "happens to not fail" in this test.
    """
    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    ont, _assignment = _eligible_ont_with_assignment(
        db_session,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    _tr069_device(db_session, server=server, cpe=cpe, ont=ont)
    _stub_effective_wifi(monkeypatch)

    def _spy_set_and_verify(*_args, **_kwargs):
        raise AssertionError("set_and_verify must never be called on this path")

    def _spy_create_client(*_args, **_kwargs):
        raise AssertionError("create_genieacs_client must never be called on this path")

    monkeypatch.setattr(
        "app.services.network.cpe_action_wifi.set_and_verify", _spy_set_and_verify
    )
    monkeypatch.setattr(
        "app.services.genieacs_client.create_genieacs_client", _spy_create_client
    )

    ssid_result = cpe_action_wifi.set_wifi_ssid(
        db_session,
        str(cpe.id),
        "ProvenNoDirectWrite",
        context=_command_context(idempotency_key=f"proof-ssid-{uuid.uuid4()}"),
        permission_granted=True,
    )
    password_result = cpe_action_wifi.set_wifi_password(
        db_session,
        str(cpe.id),
        "structurally-safe-password",
        context=_command_context(idempotency_key=f"proof-pwd-{uuid.uuid4()}"),
        permission_granted=True,
    )

    assert ssid_result.success is True
    assert password_result.success is True


def test_resolve_genieacs_for_cpe_with_reason_is_never_called_on_this_path(
    db_session, monkeypatch, olt_device, subscription, subscriber
):
    """Regression: the ambiguous fuzzy-fallback resolver must never be
    reachable from the CPE-detail WiFi admin path. This is the sole path this
    task changes; ``resolve_genieacs_for_cpe_with_reason`` itself (tier 2
    serial-number match, tier 3 default ACS server) is out of scope and
    remains unreliable by design until a later slice -- this path simply must
    never call into it, or any equivalent fallback resolver, at all.
    """
    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    ont, _assignment = _eligible_ont_with_assignment(
        db_session,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    _tr069_device(db_session, server=server, cpe=cpe, ont=ont)
    _stub_effective_wifi(monkeypatch)

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError(
            "resolve_genieacs_for_cpe_with_reason must never be called from the "
            "CPE-detail WiFi admin path"
        )

    monkeypatch.setattr(
        "app.services.network._resolve.resolve_genieacs_for_cpe_with_reason",
        _fail_if_called,
    )
    monkeypatch.setattr(
        "app.services.network.ont_action_common.resolve_genieacs_for_cpe_with_reason",
        _fail_if_called,
    )

    result = cpe_action_wifi.set_wifi_ssid(
        db_session,
        str(cpe.id),
        "NoFuzzyFallback",
        context=_command_context(idempotency_key=f"no-fallback-{uuid.uuid4()}"),
        permission_granted=True,
    )

    assert result.success is True


# ── Web layer: each typed blocker is a distinct refusal, not a 500 ──────


class _StubUser:
    def __init__(self, principal_id: str) -> None:
        self.id = principal_id
        self.first_name = "Test"
        self.last_name = "Admin"
        self.email = "admin@example.test"
        self.person_id = None


def _admin_request(*, roles: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            auth={
                "principal_id": "admin-1",
                "principal_type": "user",
                "roles": roles if roles is not None else ["admin"],
            },
            user=_StubUser("admin-1"),
            request_id="",
        ),
        headers={},
        # `persist_audit_event`'s `request.client.host if request and
        # request.client else None` null-checks the VALUE but still requires
        # the ATTRIBUTE to exist -- a bare SimpleNamespace with no `client=`
        # raises AttributeError rather than reading as falsy. Matches the
        # established fake-request pattern elsewhere (e.g.
        # tests/test_admin_material_requests.py).
        client=None,
    )


def test_web_route_surfaces_cpe_device_not_found_as_distinct_json_refusal(
    db_session, subscriber
):
    from app.web.admin.network_cpes import cpe_wifi_ssid

    cpe = _cpe_device(db_session, subscriber)

    response = cpe_wifi_ssid(
        _admin_request(), str(cpe.id), ssid="WhateverSSID", db=db_session
    )

    assert response.status_code == 502
    import json as _json

    body = _json.loads(response.body)
    assert body["success"] is False
    assert body["error_code"].endswith("cpe_device_not_found")
    assert "not linked" in body["message"] or "No active" in body["message"]


def test_web_route_surfaces_cpe_ont_not_linked_as_distinct_json_refusal(
    db_session, subscriber
):
    from app.web.admin.network_cpes import cpe_wifi_ssid

    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    _tr069_device(db_session, server=server, cpe=cpe, ont=None)

    response = cpe_wifi_ssid(
        _admin_request(), str(cpe.id), ssid="WhateverSSID", db=db_session
    )

    import json as _json

    body = _json.loads(response.body)
    assert response.status_code == 502
    assert body["error_code"].endswith("cpe_ont_not_linked")


def test_web_route_surfaces_cpe_assignment_inactive_as_distinct_json_refusal(
    db_session, olt_device, subscriber
):
    from app.web.admin.network_cpes import cpe_wifi_ssid

    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    pon = PonPort(olt_id=olt_device.id, name=f"0/1/{uuid.uuid4().int % 100000}")
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"WEB-BLOCK-{uuid.uuid4().hex[:8]}",
        is_active=True,
        olt_device_id=olt_device.id,
        pon_port_id=pon.id,
    )
    db_session.add(ont)
    db_session.flush()
    _tr069_device(db_session, server=server, cpe=cpe, ont=ont)

    response = cpe_wifi_ssid(
        _admin_request(), str(cpe.id), ssid="WhateverSSID", db=db_session
    )

    import json as _json

    body = _json.loads(response.body)
    assert response.status_code == 502
    assert body["error_code"].endswith("cpe_assignment_inactive")


def test_web_route_wifi_ssid_succeeds_end_to_end_for_eligible_cpe(
    db_session, monkeypatch, olt_device, subscription, subscriber
):
    from app.web.admin.network_cpes import cpe_wifi_ssid

    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    ont, _assignment = _eligible_ont_with_assignment(
        db_session,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    _tr069_device(db_session, server=server, cpe=cpe, ont=ont)
    _stub_effective_wifi(monkeypatch)

    response = cpe_wifi_ssid(
        _admin_request(), str(cpe.id), ssid="WebLayerSSID", db=db_session
    )

    import json as _json

    body = _json.loads(response.body)
    assert response.status_code == 200
    assert body["success"] is True
    db_session.refresh(ont)
    assert ont.desired_config["wifi"]["ssid"] == "WebLayerSSID"


def test_web_route_wifi_password_refuses_without_network_ont_write_permission(
    db_session, monkeypatch, olt_device, subscription, subscriber
):
    """Without ``network:ont:write`` (e.g. an admin who only holds
    ``network:cpe:write``), the owner refuses with ``permission_denied`` --
    the FastAPI-level ``Depends(require_permission("network:ont:write"))``
    on the real route would normally 403 first, but this proves the owner's
    OWN internal enforcement (``permission_granted``) independently refuses
    too, rather than trusting the caller.
    """
    import app.web.admin.network_cpes as network_cpes_module
    from app.web.admin.network_cpes import cpe_wifi_password

    server = _acs_server(db_session)
    cpe = _cpe_device(db_session, subscriber)
    ont, _assignment = _eligible_ont_with_assignment(
        db_session,
        olt_device=olt_device,
        subscription=subscription,
        subscriber=subscriber,
    )
    _tr069_device(db_session, server=server, cpe=cpe, ont=ont)
    _stub_effective_wifi(monkeypatch)
    # `parse_form_data_sync` bridges into an anyio worker-thread portal that
    # only exists inside a real ASGI request; stub it so this plain-function
    # call can exercise the route's permission/refusal logic directly.
    monkeypatch.setattr(
        network_cpes_module,
        "parse_form_data_sync",
        lambda _request: {"password": "correct-horse-battery"},
    )

    non_ont_writer_request = SimpleNamespace(
        state=SimpleNamespace(
            auth={
                "principal_id": "cpe-only-admin",
                "principal_type": "user",
                "roles": ["network_cpe_write_only"],
            },
            user=_StubUser("cpe-only-admin"),
            request_id="",
        ),
        headers={},
        client=None,
    )

    response = cpe_wifi_password(non_ont_writer_request, str(cpe.id), db=db_session)

    import json as _json

    body = _json.loads(response.body)
    assert response.status_code == 502
    assert body["error_code"].endswith("permission_denied")
