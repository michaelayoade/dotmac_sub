from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_coordinator_runs_post_registration_phases_through_saga(monkeypatch) -> None:
    from app.services.network.provisioning_coordinator import ProvisioningCoordinator

    db = MagicMock()
    coordinator = ProvisioningCoordinator(db, initiated_by="unit-test")
    phase_calls: list[str] = []
    executed_sagas: list[object] = []

    def registration(*_args, **_kwargs):
        assert coordinator._result is not None
        coordinator._result.ont_id = "ont-1"
        coordinator._result.ont_id_on_olt = 5
        return True

    class RecordingSagaExecutor:
        def __init__(self, saga, context):
            self.saga = saga
            self.context = context
            executed_sagas.append(saga)

        def execute(self):
            for step in self.saga.steps:
                step.action(self.context)
            return SimpleNamespace(success=True, compensation_failures=[])

    monkeypatch.setattr(coordinator, "_execute_olt_registration", registration)
    monkeypatch.setattr(coordinator, "_get_ont", lambda _ont_id: SimpleNamespace())
    monkeypatch.setattr(coordinator, "_get_olt", lambda _olt_id: SimpleNamespace())
    monkeypatch.setattr(
        coordinator,
        "_execute_service_port_creation",
        lambda _olt_id: phase_calls.append("service_ports") or True,
    )
    monkeypatch.setattr(
        coordinator,
        "_execute_management_ip_config",
        lambda _olt_id: phase_calls.append("management_ip") or True,
    )
    monkeypatch.setattr(
        coordinator,
        "_execute_tr069_binding",
        lambda _olt_id: phase_calls.append("tr069") or True,
    )
    monkeypatch.setattr(
        "app.services.network.ont_provisioning.saga.SagaExecutor",
        RecordingSagaExecutor,
    )

    result = coordinator.provision_ont(
        "olt-1",
        "0/1/3",
        "ONT-SERIAL",
        skip_acs_config=True,
    )

    assert result.success is True
    assert phase_calls == ["service_ports", "management_ip", "tr069"]
    assert len(executed_sagas) == 1
    assert executed_sagas[0].name == "coordinated_post_registration"
