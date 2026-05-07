from __future__ import annotations

from types import SimpleNamespace

from app.models.network import OLTDevice, OltServicePort, OntUnit, ServicePortAllocation
from app.services.network.olt_protocol_adapters import OltOperationResult
from app.services.network.olt_ssh import ServicePortEntry


def _service_port(
    *,
    index: int,
    ont_id: int = 5,
    vlan_id: int = 203,
    gem_index: int = 1,
) -> ServicePortEntry:
    return ServicePortEntry(
        index=index,
        vlan_id=vlan_id,
        ont_id=ont_id,
        gem_index=gem_index,
        flow_type="vlan",
        flow_para=str(vlan_id),
        state="up",
        fsp="0/2/1",
        tag_transform="translate",
    )


def _create_olt_ont(db_session):
    olt = OLTDevice(
        name="Service Port Test OLT",
        hostname="service-port-test-olt.local",
        vendor="Huawei",
        model="MA5608T",
        ssh_username="admin",
        ssh_password="secret",
    )
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="SP-ONT-001",
        olt_device_id=olt.id,
        board="0/2",
        port="1",
        external_id="5",
    )
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(olt)
    db_session.refresh(ont)
    return olt, ont


class _FakeAdapter:
    def __init__(self, ports: list[ServicePortEntry] | None = None):
        self.ports = list(ports or [])
        self.deleted: list[int] = []
        self.created: list[dict[str, object]] = []

    def get_service_ports_for_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
        return OltOperationResult(
            success=True,
            message="ok",
            data={"service_ports": [p for p in self.ports if p.ont_id == ont_id]},
        )

    def create_service_port(
        self,
        fsp: str,
        ont_id: int,
        *,
        gem_index: int,
        vlan_id: int,
        user_vlan: int | str | None = None,
        tag_transform: str = "translate",
        port_index: int | None = None,
    ) -> OltOperationResult:
        assert port_index is not None
        self.created.append(
            {
                "port_index": port_index,
                "ont_id": ont_id,
                "vlan_id": vlan_id,
                "gem_index": gem_index,
                "user_vlan": user_vlan,
                "tag_transform": tag_transform,
            }
        )
        self.ports.append(
            _service_port(
                index=port_index,
                ont_id=ont_id,
                vlan_id=vlan_id,
                gem_index=gem_index,
            )
        )
        return OltOperationResult(
            success=True,
            message="created",
            data={"port_index": port_index},
            service_port_index=port_index,
        )

    def delete_service_port(self, index: int) -> OltOperationResult:
        self.deleted.append(index)
        self.ports = [p for p in self.ports if p.index != index]
        return OltOperationResult(success=True, message="deleted")


def _patch_context_and_adapter(monkeypatch, olt, ont, adapter):
    from app.services import web_network_service_ports as service

    monkeypatch.setattr(
        service,
        "_resolve_ont_olt_context",
        lambda db, ont_id: (ont, olt, "0/2/1", 5),
    )
    monkeypatch.setattr(service, "get_protocol_adapter", lambda resolved_olt: adapter)
    return service


def test_handle_delete_rejects_index_not_owned_by_target_ont(
    db_session, monkeypatch
) -> None:
    olt, ont = _create_olt_ont(db_session)
    adapter = _FakeAdapter([_service_port(index=100, ont_id=9)])
    service = _patch_context_and_adapter(monkeypatch, olt, ont, adapter)

    ok, message = service.handle_delete(db_session, str(ont.id), 100)

    assert ok is False
    assert "does not belong to this ONT" in message
    assert adapter.deleted == []


def test_handle_delete_releases_only_matching_allocation(db_session, monkeypatch) -> None:
    olt, ont = _create_olt_ont(db_session)
    from app.services.network.service_port_allocator import allocate_service_port

    allocation = allocate_service_port(
        db_session,
        olt.id,
        ont.id,
        vlan_id=203,
        gem_index=1,
    )
    db_session.commit()
    adapter = _FakeAdapter([_service_port(index=allocation.port_index, ont_id=5)])
    service = _patch_context_and_adapter(monkeypatch, olt, ont, adapter)

    ok, message = service.handle_delete(db_session, str(ont.id), allocation.port_index)

    db_session.refresh(allocation)
    assert ok is True
    assert "deleted" in message
    assert adapter.deleted == [allocation.port_index]
    assert allocation.is_active is False
    assert allocation.released_at is not None


def test_handle_delete_keeps_allocation_reserved_when_readback_fails(
    db_session, monkeypatch
) -> None:
    olt, ont = _create_olt_ont(db_session)
    from app.services.network.service_port_allocator import allocate_service_port

    allocation = allocate_service_port(
        db_session,
        olt.id,
        ont.id,
        vlan_id=203,
        gem_index=1,
    )
    db_session.commit()

    class ReadbackFailsAfterDeleteAdapter(_FakeAdapter):
        def get_service_ports_for_ont(self, fsp: str, ont_id: int) -> OltOperationResult:
            if self.deleted:
                return OltOperationResult(success=False, message="readback timeout")
            return super().get_service_ports_for_ont(fsp, ont_id)

    adapter = ReadbackFailsAfterDeleteAdapter(
        [_service_port(index=allocation.port_index, ont_id=5)]
    )
    service = _patch_context_and_adapter(monkeypatch, olt, ont, adapter)

    ok, message = service.handle_delete(db_session, str(ont.id), allocation.port_index)

    db_session.refresh(allocation)
    assert ok is False
    assert "Keeping service-port allocation reserved" in message
    assert adapter.deleted == [allocation.port_index]
    assert allocation.is_active is True
    assert allocation.released_at is None


def test_handle_create_keeps_allocation_reserved_when_readback_misses(
    db_session, monkeypatch
) -> None:
    olt, ont = _create_olt_ont(db_session)

    class NoReadbackAdapter(_FakeAdapter):
        def create_service_port(self, *args, **kwargs) -> OltOperationResult:
            port_index = kwargs["port_index"]
            self.created.append({"port_index": port_index})
            return OltOperationResult(
                success=True,
                message="accepted",
                data={"port_index": port_index},
                service_port_index=port_index,
            )

    adapter = NoReadbackAdapter([])
    service = _patch_context_and_adapter(monkeypatch, olt, ont, adapter)
    monkeypatch.setattr(
        "app.services.network.config_validator_adapter.validate_service_port_config",
        lambda *args, **kwargs: SimpleNamespace(
            is_valid=True,
            errors=[],
            warnings=[],
        ),
    )

    ok, message = service.handle_create(db_session, str(ont.id), 203, 1)

    allocation = db_session.query(ServicePortAllocation).one()
    assert ok is False
    assert "readback did not show" in message
    assert allocation.is_active is True
    assert allocation.provisioned_at is not None


def test_list_context_reads_imported_service_ports_without_live_olt(
    db_session, monkeypatch
) -> None:
    olt, ont = _create_olt_ont(db_session)
    imported = OltServicePort(
        olt_device_id=olt.id,
        ont_unit_id=ont.id,
        port_index=401,
        fsp="0/2/1",
        ont_id_on_olt=5,
        vlan_id=203,
        gem_index=2,
        flow_type="vlan",
        flow_para="203",
        state="up",
        source="running_config",
    )
    db_session.add(imported)
    db_session.commit()

    from app.services import web_network_service_ports as service

    monkeypatch.setattr(
        service,
        "_resolve_ont_olt_context",
        lambda db, ont_id: (ont, olt, "0/2/1", 5),
    )

    def _fail_live_adapter(_olt):
        raise AssertionError("list_context should use imported DB state")

    monkeypatch.setattr(service, "get_protocol_adapter", _fail_live_adapter)

    context = service.list_context(db_session, str(ont.id))

    assert context["error"] is None
    assert context["service_ports_source"] == "imported"
    ports = context["service_ports"]
    assert len(ports) == 1
    assert ports[0].index == 401
    assert ports[0].vlan_id == 203
    assert ports[0].gem_index == 2


def test_list_context_fails_when_service_port_state_was_never_imported(
    db_session, monkeypatch
) -> None:
    olt, ont = _create_olt_ont(db_session)
    from app.services import web_network_service_ports as service

    monkeypatch.setattr(
        service,
        "_resolve_ont_olt_context",
        lambda db, ont_id: (ont, olt, "0/2/1", 5),
    )

    context = service.list_context(db_session, str(ont.id))

    assert "No imported service-port state" in context["error"]
    assert context["service_ports"] == []


def test_handle_clone_uses_allocator_indices(db_session, monkeypatch) -> None:
    olt, ont = _create_olt_ont(db_session)
    ref_ont = OntUnit(
        serial_number="SP-ONT-REF",
        olt_device_id=olt.id,
        board="0/2",
        port="1",
        external_id="7",
    )
    db_session.add(ref_ont)
    db_session.flush()
    db_session.add(
        OltServicePort(
            olt_device_id=olt.id,
            ont_unit_id=ref_ont.id,
            port_index=900,
            fsp="0/2/1",
            ont_id_on_olt=7,
            vlan_id=203,
            gem_index=1,
            flow_type="vlan",
            flow_para="203",
            state="up",
            tag_transform="translate",
            source="running_config",
        )
    )
    db_session.commit()
    db_session.refresh(ref_ont)

    adapter = _FakeAdapter([_service_port(index=900, ont_id=7, vlan_id=203)])
    from app.services import web_network_service_ports as service

    def _context(db, ont_id):
        if str(ont_id) == str(ref_ont.id):
            return ref_ont, olt, "0/2/1", 7
        return ont, olt, "0/2/1", 5

    monkeypatch.setattr(service, "_resolve_ont_olt_context", _context)
    monkeypatch.setattr(service, "get_protocol_adapter", lambda resolved_olt: adapter)

    ok, message = service.handle_clone(db_session, str(ont.id), str(ref_ont.id))

    allocation = db_session.query(ServicePortAllocation).one()
    assert ok is True
    assert "Created 1 service-port" in message
    assert adapter.created == [
        {
            "port_index": allocation.port_index,
            "ont_id": 5,
            "vlan_id": 203,
            "gem_index": 1,
            "user_vlan": 203,
            "tag_transform": "translate",
        }
    ]
    assert allocation.is_active is True
    assert allocation.provisioned_at is not None
