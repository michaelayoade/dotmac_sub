from __future__ import annotations

from types import SimpleNamespace

from app.services.network import olt_api_operations


def test_api_authorize_ont_returns_workflow_result(monkeypatch):
    """API authorization returns the completed workflow result directly."""
    failed_result = SimpleNamespace(
        success=False,
        status="error",
        message="ONT authorization failed.",
        ont_unit_id="ont-1",
        ont_id_on_olt=7,
        completed_authorization=True,
        partial_success=True,
        duration_ms=123,
        steps=[
            SimpleNamespace(
                step=1,
                name="Activate ONT",
                success=True,
                message="authorized",
                duration_ms=10,
            )
        ],
    )

    monkeypatch.setattr(
        "app.services.network.olt_api_operations.authorize_ont_workflow",
        lambda *args, **kwargs: failed_result,
    )

    response = olt_api_operations.authorize_ont(
        object(),
        "olt-1",
        fsp="0/1/1",
        serial_number="HWTCWARNQUEUE",
    )

    assert response.success is False
    assert response.message == "ONT authorization failed."
    assert response.data == {
        "status": "error",
        "ont_unit_id": "ont-1",
        "ont_id_on_olt": 7,
        "completed_authorization": True,
        "partial_success": True,
        "duration_ms": 123,
        "steps": [
            {
                "step": 1,
                "name": "Activate ONT",
                "success": True,
                "message": "authorized",
                "duration_ms": 10,
            }
        ],
    }
