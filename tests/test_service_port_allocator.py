from __future__ import annotations

from types import SimpleNamespace


def _create_olt_and_ont(db_session):
    from app.models.network import OLTDevice, OntUnit

    olt = OLTDevice(
        name="Allocator Test OLT",
        vendor="Huawei",
        model="MA5608T",
        ssh_username="admin",
        ssh_password="test",
    )
    ont = OntUnit(serial_number="ALLOC-ONT-001")
    db_session.add_all([olt, ont])
    db_session.commit()
    db_session.refresh(olt)
    db_session.refresh(ont)
    return olt, ont


def test_allocate_service_port_rejects_exhausted_reserved_pool(db_session) -> None:
    from app.models.network import OltServicePortPool
    from app.services.network.service_port_allocator import (
        AllocationError,
        allocate_service_port,
    )

    olt, ont = _create_olt_and_ont(db_session)
    pool = OltServicePortPool(
        olt_device_id=olt.id,
        min_index=100,
        max_index=100,
        next_available_index=100,
        reserved_indices=[100],
        is_active=True,
    )
    db_session.add(pool)
    db_session.commit()

    try:
        allocate_service_port(db_session, olt.id, ont.id, vlan_id=203, gem_index=1)
    except AllocationError as exc:
        assert "No available service-port indices" in str(exc)
    else:
        raise AssertionError("Expected AllocationError for exhausted pool")


def test_with_allocated_service_port_releases_failed_write(db_session) -> None:
    from app.models.network import ServicePortAllocation
    from app.services.network.service_port_allocator import with_allocated_service_port

    olt, ont = _create_olt_and_ont(db_session)

    result = with_allocated_service_port(
        db_session,
        olt.id,
        ont.id,
        lambda allocation: SimpleNamespace(success=False, port_index=allocation.port_index),
        vlan_id=203,
        gem_index=1,
        provisioned=lambda write_result: bool(write_result.success),
    )

    allocation = db_session.query(ServicePortAllocation).one()
    assert result.success is False
    assert allocation.port_index == result.port_index
    assert allocation.is_active is False
    assert allocation.released_at is not None


def test_with_allocated_service_port_replays_cached_result_by_correlation_key(
    db_session,
) -> None:
    from app.services.network.service_port_allocator import (
        build_service_port_correlation_key,
        with_allocated_service_port,
    )

    olt, ont = _create_olt_and_ont(db_session)
    calls: list[int] = []
    correlation_key = build_service_port_correlation_key(
        "alloc:test:1",
        ont_id=ont.id,
        vlan_id=203,
        gem_index=1,
        tag_transform="translate",
    )

    def _provision(allocation):
        calls.append(allocation.port_index)
        return SimpleNamespace(success=True, port_index=allocation.port_index)

    serializer = lambda result: {
        "success": bool(result.success),
        "port_index": int(result.port_index),
    }
    deserializer = lambda payload: SimpleNamespace(
        success=bool(payload["success"]),
        port_index=int(payload["port_index"]),
    )

    first = with_allocated_service_port(
        db_session,
        olt.id,
        ont.id,
        _provision,
        vlan_id=203,
        gem_index=1,
        correlation_key=correlation_key,
        provisioned=lambda result: bool(result.success),
        serialize_result=serializer,
        deserialize_result=deserializer,
    )
    second = with_allocated_service_port(
        db_session,
        olt.id,
        ont.id,
        _provision,
        vlan_id=203,
        gem_index=1,
        correlation_key=correlation_key,
        provisioned=lambda result: bool(result.success),
        serialize_result=serializer,
        deserialize_result=deserializer,
    )

    assert first.success is True
    assert second.success is True
    assert first.port_index == second.port_index
    assert calls == [first.port_index]


def test_with_allocated_service_port_does_not_cache_failed_result(
    db_session,
) -> None:
    from app.models.network import ServicePortAllocation
    from app.services.network.service_port_allocator import (
        build_service_port_correlation_key,
        with_allocated_service_port,
    )

    olt, ont = _create_olt_and_ont(db_session)
    calls: list[int] = []
    correlation_key = build_service_port_correlation_key(
        "alloc:test:retry",
        ont_id=ont.id,
        vlan_id=203,
        gem_index=1,
        tag_transform="translate",
    )

    def _provision(allocation):
        calls.append(allocation.port_index)
        if len(calls) == 1:
            return SimpleNamespace(success=False, port_index=allocation.port_index)
        return SimpleNamespace(success=True, port_index=allocation.port_index)

    serializer = lambda result: {
        "success": bool(result.success),
        "port_index": int(result.port_index),
    }
    deserializer = lambda payload: SimpleNamespace(
        success=bool(payload["success"]),
        port_index=int(payload["port_index"]),
    )

    first = with_allocated_service_port(
        db_session,
        olt.id,
        ont.id,
        _provision,
        vlan_id=203,
        gem_index=1,
        correlation_key=correlation_key,
        provisioned=lambda result: bool(result.success),
        serialize_result=serializer,
        deserialize_result=deserializer,
    )
    second = with_allocated_service_port(
        db_session,
        olt.id,
        ont.id,
        _provision,
        vlan_id=203,
        gem_index=1,
        correlation_key=correlation_key,
        provisioned=lambda result: bool(result.success),
        serialize_result=serializer,
        deserialize_result=deserializer,
    )

    allocations = db_session.query(ServicePortAllocation).all()
    released = next(allocation for allocation in allocations if allocation.is_active is False)

    assert first.success is False
    assert second.success is True
    assert calls == [first.port_index, second.port_index]
    assert len(allocations) == 2
    assert released.correlation_key is None
    assert released.result_payload is None
