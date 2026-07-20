"""Shared network control-plane intent lifecycle contract."""

from __future__ import annotations

import pytest

from app.models.network import OntSyncStatus
from app.models.network_operation import NetworkOperationStatus
from app.models.router_management import (
    RouterConfigPushStatus,
    RouterPushResultStatus,
)
from app.models.uisp_control import UispIntentStatus
from app.services.control_plane_intent import (
    ControlPlaneContractError,
    ControlPlaneHeadConflict,
    ControlPlanePhase,
    ControlPlaneTarget,
    ControlPlaneTransitionError,
    assert_intent_head,
    assert_phase_transition,
    phase_for_huawei_sync,
    phase_for_network_operation,
    phase_for_router_push,
    phase_for_router_push_result,
    phase_for_uisp_intent,
)


def test_target_identity_is_revision_scoped() -> None:
    target = ControlPlaneTarget(
        provider=" UISP ",
        target_type=" CPE ",
        target_id=" device-42 ",
        desired_revision=7,
    )

    assert target.correlation_key == "uisp:cpe:device-42:revision:7"
    assert target.as_payload()["desired_revision"] == 7


@pytest.mark.parametrize("revision", [0, -1])
def test_target_rejects_invalid_revisions(revision: int) -> None:
    with pytest.raises(ControlPlaneContractError):
        ControlPlaneTarget("uisp", "cpe", "device-42", revision)


def test_revision_head_guard_rejects_superseded_work() -> None:
    assert_intent_head(expected_revision=4, current_revision=4)

    with pytest.raises(ControlPlaneHeadConflict, match="current revision is 5"):
        assert_intent_head(expected_revision=4, current_revision=5)


def test_lifecycle_accepts_delivery_and_retry_path() -> None:
    path = (
        ControlPlanePhase.desired,
        ControlPlanePhase.planned,
        ControlPlanePhase.queued,
        ControlPlanePhase.applying,
        ControlPlanePhase.readback_pending,
        ControlPlanePhase.applying,
        ControlPlanePhase.verified,
    )
    for current, destination in zip(path[:-1], path[1:], strict=True):
        assert_phase_transition(current, destination)


def test_lifecycle_rejects_verified_to_applying_without_new_intent() -> None:
    with pytest.raises(ControlPlaneTransitionError):
        assert_phase_transition(
            ControlPlanePhase.verified,
            ControlPlanePhase.applying,
        )


def test_every_network_operation_status_has_a_projection() -> None:
    phases = {phase_for_network_operation(status) for status in NetworkOperationStatus}

    assert phases == {
        ControlPlanePhase.queued,
        ControlPlanePhase.applying,
        ControlPlanePhase.readback_pending,
        ControlPlanePhase.verified,
        ControlPlanePhase.drifted,
        ControlPlanePhase.failed,
    }


def test_every_uisp_status_has_a_projection() -> None:
    assert all(phase_for_uisp_intent(status) for status in UispIntentStatus)


def test_every_huawei_sync_status_has_a_projection() -> None:
    assert all(phase_for_huawei_sync(status) for status in OntSyncStatus)


def test_every_router_push_status_has_a_projection() -> None:
    assert all(phase_for_router_push(status) for status in RouterConfigPushStatus)
    assert all(
        phase_for_router_push_result(status) for status in RouterPushResultStatus
    )


def test_unknown_native_status_fails_loudly() -> None:
    with pytest.raises(ControlPlaneContractError, match="Unknown UISP intent"):
        phase_for_uisp_intent("silently_ignored")
