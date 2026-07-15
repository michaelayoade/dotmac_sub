from __future__ import annotations

from app.services.network import olt_api_operations
from app.services.network.ont_provisioning_commands import ProvisioningCommandResult


def test_api_authorize_ont_returns_durable_command_result(monkeypatch):
    monkeypatch.setattr(
        "app.services.network.olt_api_operations.request_ont_authorization",
        lambda *args, **kwargs: ProvisioningCommandResult(
            True,
            True,
            "ONT authorization accepted.",
            operation_id="operation-1",
            dispatch_id="dispatch-1",
        ),
    )

    response = olt_api_operations.authorize_ont(
        object(),
        "olt-1",
        fsp="0/1/1",
        serial_number="HWTCWARNQUEUE",
    )

    assert response.success is True
    assert response.message == "ONT authorization accepted."
    assert response.data == {
        "operation_id": "operation-1",
        "dispatch_id": "dispatch-1",
        "waiting": True,
        "duplicate": False,
    }
