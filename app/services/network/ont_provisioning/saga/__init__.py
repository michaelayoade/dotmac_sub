"""Saga pattern for ONT provisioning with automatic rollback.

This package provides a saga-based orchestration framework for multi-step
ONT provisioning operations. When a critical step fails, completed steps
are automatically rolled back using compensation actions.

Key Components:
- SagaExecutor: Orchestrates step execution and compensation
- SagaDefinition: Defines a sequence of steps with compensations
- SagaContext: Execution context shared between steps
- SagaResult: Outcome with execution and compensation history

Pre-built Sagas:
- FULL_PROVISIONING_SAGA: Complete OLT + ACS provisioning
- WIFI_SETUP_SAGA: WiFi-only configuration
- ACS_CONFIG_SAGA: PPPoE, WiFi, and LAN via TR-069

Usage:
    from app.services.network.ont_provisioning.saga import (
        execute_saga,
        FULL_PROVISIONING_SAGA,
    )

    result = execute_saga(
        db,
        FULL_PROVISIONING_SAGA,
        ont_id,
        step_data={"internet_vlan_id": 100},
    )

    if result.success:
        print(f"Provisioned in {result.duration_ms}ms")
    else:
        print(f"Failed: {result.message}")
        for step_name, error in result.compensation_failures:
            print(f"  Manual cleanup needed: {step_name}")
"""

from app.services.network.ont_provisioning.saga.executor import (
    SagaExecutor,
    execute_saga,
)
from app.services.network.ont_provisioning.saga.persistence import (
    SagaExecutionRepository,
    saga_executions,
)
from app.services.network.ont_provisioning.saga.types import (
    CompensationRecord,
    SagaContext,
    SagaDefinition,
    SagaExecutionStatus,
    SagaResult,
    SagaStep,
    StepExecutionRecord,
    generate_saga_execution_id,
)
from app.services.network.ont_provisioning.saga.workflows import (
    ACS_CONFIG_SAGA,
    FULL_PROVISIONING_SAGA,
    SAGA_REGISTRY,
    WIFI_SETUP_SAGA,
    build_internet_provisioning_saga,
    get_saga_by_name,
    list_available_sagas,
)

__all__ = [
    # Types
    "SagaStep",
    "SagaDefinition",
    "SagaContext",
    "SagaResult",
    "SagaExecutionStatus",
    "StepExecutionRecord",
    "CompensationRecord",
    "generate_saga_execution_id",
    # Executor
    "SagaExecutor",
    "execute_saga",
    # Persistence
    "SagaExecutionRepository",
    "saga_executions",
    # Pre-built Sagas
    "FULL_PROVISIONING_SAGA",
    "WIFI_SETUP_SAGA",
    "ACS_CONFIG_SAGA",
    "SAGA_REGISTRY",
    # Builders
    "build_internet_provisioning_saga",
    "get_saga_by_name",
    "list_available_sagas",
]
