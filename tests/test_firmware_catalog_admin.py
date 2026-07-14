"""Firmware artifact catalog policy and admin UI tests."""

from __future__ import annotations

import uuid

import pytest
from starlette.datastructures import FormData
from starlette.requests import Request

from app.models.network import OltFirmwareImage, OntFirmwareImage
from app.models.network_operation import (
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services import web_network_firmware_catalog
from app.services.network import firmware_catalog
from app.services.network_operations import network_operations
from app.web.admin.network_firmware_catalog import router
from app.web.templates import templates


def _values(kind: str = "ont", *, version: str = "V1R2") -> dict[str, object]:
    return {
        "kind": kind,
        "vendor": "Huawei",
        "model": "EG8145V5" if kind == "ont" else "MA5800-X7",
        "version": version,
        "file_url": (
            f"https://firmware.example/{version}.bin"
            if kind == "ont"
            else f"sftp://firmware.example/{version}.bin"
        ),
        "filename": f"{version}.bin",
        "checksum": "a" * 64,
        "file_size_bytes": "4096",
        "release_notes": "Verified release",
        "notes": "Operator note",
        "is_active": True,
    }


def _record_rollout(db_session, image, *, active: bool):
    kind = "olt" if isinstance(image, OltFirmwareImage) else "ont"
    operation = network_operations.start(
        db_session,
        (
            NetworkOperationType.olt_firmware_upgrade
            if kind == "olt"
            else NetworkOperationType.ont_firmware_upgrade
        ),
        (
            NetworkOperationTargetType.olt
            if kind == "olt"
            else NetworkOperationTargetType.ont
        ),
        str(uuid.uuid4()),
        correlation_key=f"firmware-catalog-test:{uuid.uuid4()}",
        input_payload={"firmware_image_id": str(image.id)},
    )
    if not active:
        network_operations.mark_succeeded(db_session, str(operation.id))
    db_session.commit()
    return operation


def test_create_normalizes_verified_olt_and_ont_images(db_session) -> None:
    olt = firmware_catalog.create_image(db_session, _values("olt"))
    ont = firmware_catalog.create_image(db_session, _values("ont"))

    assert isinstance(olt, OltFirmwareImage)
    assert olt.upgrade_method == "sftp"
    assert olt.checksum == f"sha256:{'a' * 64}"
    assert olt.release_notes == "Verified release"
    assert isinstance(ont, OntFirmwareImage)
    assert ont.notes == "Operator note"
    assert ont.checksum == f"sha256:{'a' * 64}"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("checksum", "abc", "full SHA-256"),
        ("file_url", "file:///tmp/image.bin", "must use"),
        (
            "file_url",
            "https://user:secret@firmware.example/image.bin",
            "must not contain credentials",
        ),
        ("file_url", "https://firmware.example/image.bin;reboot", "unsafe"),
    ],
)
def test_create_rejects_unverified_or_unsafe_artifacts(
    db_session, field: str, value: str, message: str
) -> None:
    values = _values()
    values[field] = value

    with pytest.raises(firmware_catalog.FirmwareCatalogError, match=message):
        firmware_catalog.create_image(db_session, values)


def test_completed_rollout_locks_artifact_identity_but_not_catalog_metadata(
    db_session,
) -> None:
    image = firmware_catalog.create_image(db_session, _values("olt"))
    _record_rollout(db_session, image, active=False)
    changed = _values("olt")
    changed["version"] = "V1R3"

    with pytest.raises(firmware_catalog.FirmwareCatalogError, match="immutable"):
        firmware_catalog.update_image(db_session, "olt", str(image.id), changed)

    metadata = _values("olt")
    metadata["release_notes"] = "Superseded after verified rollout"
    metadata["notes"] = "Retained for rollback"
    metadata["is_active"] = False
    updated = firmware_catalog.update_image(db_session, "olt", str(image.id), metadata)

    assert updated.release_notes == "Superseded after verified rollout"
    assert updated.notes == "Retained for rollback"
    assert updated.is_active is False


def test_active_rollout_blocks_update_and_deactivation(db_session) -> None:
    image = firmware_catalog.create_image(db_session, _values())
    _record_rollout(db_session, image, active=True)

    with pytest.raises(firmware_catalog.FirmwareCatalogError, match="active upgrade"):
        firmware_catalog.update_image(db_session, "ont", str(image.id), _values())
    with pytest.raises(firmware_catalog.FirmwareCatalogError, match="active upgrade"):
        firmware_catalog.deactivate_image(db_session, "ont", str(image.id))


def test_list_filters_and_paginates_across_firmware_types(db_session) -> None:
    for number in range(12):
        firmware_catalog.create_image(
            db_session, _values("ont", version=f"ONT-{number:02d}")
        )
    firmware_catalog.create_image(db_session, _values("olt", version="OLT-01"))

    first = firmware_catalog.list_images(
        db_session, kind="ont", vendor="huawei", page=1, per_page=10
    )
    second = firmware_catalog.list_images(
        db_session, kind="ont", vendor="Huawei", page=2, per_page=10
    )
    searched = firmware_catalog.list_images(db_session, search="OLT-01")

    assert first["pagination"]["total"] == 12
    assert len(first["rows"]) == 10
    assert len(second["rows"]) == 2
    assert len(searched["rows"]) == 1
    assert searched["rows"][0].kind == "olt"


def test_form_checkbox_uses_last_submitted_value() -> None:
    form = FormData([("is_active", "false"), ("is_active", "true")])

    assert web_network_firmware_catalog.parse_form(form)["is_active"] is True


def test_admin_routes_and_templates_are_registered() -> None:
    paths = {route.path for route in router.routes}

    assert "/network/firmware-images" in paths
    assert "/network/firmware-images/create" in paths
    assert "/network/firmware-images/{kind}/{image_id}/edit" in paths
    templates.env.get_template("admin/network/firmware/index.html")
    templates.env.get_template("admin/network/firmware/form.html")


def test_page_contract_hides_commands_without_write_permission(
    db_session, monkeypatch
) -> None:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.auth = {"user_id": "read-only"}
    monkeypatch.setattr(
        web_network_firmware_catalog.web_admin,
        "get_current_user",
        lambda _request: None,
    )
    monkeypatch.setattr(
        web_network_firmware_catalog.web_admin, "get_sidebar_stats", lambda _db: {}
    )
    monkeypatch.setattr(
        web_network_firmware_catalog, "has_permission", lambda *_args: False
    )

    context = web_network_firmware_catalog._base_context(request, db_session)

    assert context["can_manage_firmware"] is False


def test_firmware_list_has_one_primary_and_one_row_action() -> None:
    source, _, _ = templates.env.loader.get_source(
        templates.env, "admin/network/firmware/index.html"
    )

    assert (
        source.count(
            'href="/admin/network/firmware-images/create" class="inline-flex items-center gap-2 rounded-lg bg-emerald-600'
        )
        == 1
    )
    assert source.count('title="Edit image"') == 1
    assert 'title="Deactivate image"' not in source
    assert '<tr><td colspan="7"' not in source
