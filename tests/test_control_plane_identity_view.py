from __future__ import annotations

from types import SimpleNamespace

from app.models.network import OLTDevice
from app.models.router_management import Router, RouterAccessMethod
from app.services.control_plane_identity_view import (
    olt_identity_view,
    router_identity_view,
)
from app.services.device_adapter_binding import attach_adapter_binding
from app.services.router_management.write_adapter import routeros_adapter_binding


def _router(*, version: str) -> Router:
    return Router(
        name="garki-core",
        hostname="garki-core",
        management_ip="172.16.1.1",
        rest_api_username="admin",
        rest_api_password="secret",
        routeros_version=version,
        board_name="CCR2116-12G-4S+",
        architecture="arm64",
        access_method=RouterAccessMethod.direct,
        is_active=True,
    )


def test_router_identity_view_is_ready_only_for_mapped_routeros() -> None:
    ready = router_identity_view(_router(version="7.18.2"))
    blocked = router_identity_view(_router(version="6.49.17"))

    assert ready.write_allowed is True
    assert ready.binding is not None
    assert ready.binding.adapter_name == "mikrotik-routeros-rest-v7"
    assert blocked.write_allowed is False
    assert blocked.readiness == "unmapped"
    assert "require a mapped v7 profile" in blocked.write_reason


def test_router_identity_view_flags_operation_binding_drift() -> None:
    router = _router(version="7.18.2")
    planned = routeros_adapter_binding(router)
    router.routeros_version = "7.19.1"
    result = SimpleNamespace(
        operation=SimpleNamespace(
            input_payload=attach_adapter_binding({}, planned),
        )
    )

    view = router_identity_view(router, result=result)

    assert view.binding_changed is True
    assert view.readiness == "identity_changed"
    assert view.write_allowed is False


def test_olt_identity_view_fails_closed_for_generic_firmware() -> None:
    mapped = OLTDevice(
        name="Karsana OLT",
        vendor="Huawei",
        model="MA5608T",
        firmware_version="V800R015C10",
    )
    generic = OLTDevice(
        name="Unknown OLT",
        vendor="Huawei",
        model="MA5608T",
        firmware_version="V800R099C00",
    )

    mapped_view = olt_identity_view(mapped)
    generic_view = olt_identity_view(generic)

    assert mapped_view.write_allowed is True
    assert mapped_view.binding is not None
    assert mapped_view.binding.adapter_name == "huawei-ma5608t-v800r015"
    assert generic_view.write_allowed is False
    assert generic_view.readiness == "unmapped"
    assert generic_view.binding is not None
    assert generic_view.binding.adapter_name == "huawei-generic"
