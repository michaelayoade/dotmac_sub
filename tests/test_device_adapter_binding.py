from __future__ import annotations

import pytest

from app.services.device_adapter_binding import (
    AdapterBinding,
    AdapterIdentityChanged,
    DeviceIdentity,
    assert_adapter_binding,
    attach_adapter_binding,
)


def _binding(*, firmware: str = "8.7.19", revision: str = "profile-1"):
    return AdapterBinding(
        adapter_name="uisp-airos",
        adapter_revision="1",
        identity=DeviceIdentity(
            vendor="Ubiquiti",
            model="LBE-5AC-Gen2",
            firmware_version=firmware,
        ),
        capability_id="capability-1",
        capability_revision=revision,
    )


def test_binding_is_stable_across_identity_case_and_whitespace():
    planned = _binding()
    current = AdapterBinding(
        adapter_name="uisp-airos",
        adapter_revision="1",
        identity=DeviceIdentity(
            vendor=" ubiquiti ",
            model="lbe-5ac-gen2",
            firmware_version="8.7.19",
        ),
        capability_id="capability-1",
        capability_revision="profile-1",
    )

    payload = attach_adapter_binding({"desired": "state"}, planned)
    assert_adapter_binding(payload, current)


@pytest.mark.parametrize(
    "current",
    [
        _binding(firmware="8.7.20"),
        _binding(revision="profile-2"),
        AdapterBinding(
            adapter_name="uisp-onu",
            adapter_revision="1",
            identity=_binding().identity,
            capability_id="capability-1",
            capability_revision="profile-1",
        ),
    ],
)
def test_binding_revalidation_rejects_identity_or_profile_changes(current):
    payload = attach_adapter_binding({}, _binding())

    with pytest.raises(AdapterIdentityChanged, match="changed after planning"):
        assert_adapter_binding(payload, current)
