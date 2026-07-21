"""Provisioning convergence-triage page-data tests."""

from app.services import web_network_provisioning_triage as triage
from app.services.control_plane_intent import (
    ControlPlanePhase,
    phase_for_provisioning_run,
)


def test_triage_data_empty_shape(db_session):
    data = triage.provisioning_triage_data(db_session)
    assert data["items"] == []
    assert data["counts"] == {"total": 0, "runs": 0, "orders": 0, "tasks": 0}


def test_phase_for_provisioning_run_maps_all_run_statuses():
    # _project raises on an unmapped status, so every ProvisioningRunStatus must map.
    assert phase_for_provisioning_run("pending") is ControlPlanePhase.queued
    assert phase_for_provisioning_run("running") is ControlPlanePhase.applying
    assert phase_for_provisioning_run("success") is ControlPlanePhase.verified
    assert phase_for_provisioning_run("failed") is ControlPlanePhase.failed


def test_control_plane_phase_presentation_tone():
    from app.schemas.status_presentation import StatusTone
    from app.services.status_presentation import control_plane_phase_presentation

    assert control_plane_phase_presentation("failed").tone is StatusTone.negative
    assert control_plane_phase_presentation("drifted").tone is StatusTone.warning
    assert control_plane_phase_presentation("verified").tone is StatusTone.positive
    assert control_plane_phase_presentation("applying").tone is StatusTone.info
