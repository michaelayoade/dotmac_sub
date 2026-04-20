"""Tests for ONT firmware download saga registration."""

from __future__ import annotations


def test_firmware_download_saga_is_registered() -> None:
    from app.services.network.ont_provisioning.saga.workflows import (
        get_saga_by_name,
        list_available_sagas,
    )

    saga = get_saga_by_name("firmware_download")

    assert saga is not None
    assert saga.name == "firmware_download"
    assert [step.name for step in saga.steps] == ["download_firmware"]
    assert saga.steps[0].critical is True
    assert any(item["name"] == "firmware_download" for item in list_available_sagas())
