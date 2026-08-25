from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from scripts.staging_host_admission import (
    AdmissionConfigurationCode,
    AdmissionReason,
    ContainerHealth,
    HeavyWorkKind,
    StagingAdmissionConfigurationError,
    StagingAdmissionPolicy,
    StagingHostSnapshot,
    _read_active_heavy_work,
    evaluate_staging_admission,
    policy_from_environment,
    render_outcome,
)

GIB = 1024**3


@pytest.fixture
def policy() -> StagingAdmissionPolicy:
    return StagingAdmissionPolicy(
        minimum_available_memory_bytes=4 * GIB,
        maximum_load_per_cpu=1.5,
        maximum_swap_used_percent=50.0,
        maximum_blocked_processes=2,
        maximum_io_pressure_avg10_percent=20.0,
        required_database_container="dotmac_sub_db",
    )


@pytest.fixture
def healthy_snapshot() -> StagingHostSnapshot:
    return StagingHostSnapshot(
        cpu_count=6,
        load1=3.0,
        available_memory_bytes=6 * GIB,
        swap_total_bytes=12 * GIB,
        swap_free_bytes=9 * GIB,
        blocked_processes=0,
        io_pressure_avg10_percent=2.0,
        database_health=ContainerHealth.HEALTHY,
        active_heavy_work=(),
    )


def test_healthy_staging_host_is_admitted(
    policy: StagingAdmissionPolicy,
    healthy_snapshot: StagingHostSnapshot,
) -> None:
    outcome = evaluate_staging_admission(policy, healthy_snapshot)

    assert outcome.allowed is True
    assert outcome.reasons == ()
    assert "staging admission allowed" in render_outcome(outcome)


@pytest.mark.parametrize(
    ("snapshot_changes", "reason"),
    [
        (
            {"database_health": ContainerHealth.UNHEALTHY},
            AdmissionReason.DATABASE_UNHEALTHY,
        ),
        (
            {"active_heavy_work": (HeavyWorkKind.DATABASE_RESTORE,)},
            AdmissionReason.HEAVY_WORK_ACTIVE,
        ),
        ({"load1": 9.1}, AdmissionReason.HOST_LOAD_HIGH),
        ({"io_pressure_avg10_percent": 20.1}, AdmissionReason.IO_PRESSURE_HIGH),
        ({"available_memory_bytes": 4 * GIB - 1}, AdmissionReason.MEMORY_LOW),
        ({"blocked_processes": 3}, AdmissionReason.PROCESSES_BLOCKED),
        (
            {"swap_total_bytes": 12 * GIB, "swap_free_bytes": 6 * GIB - 1},
            AdmissionReason.SWAP_USAGE_HIGH,
        ),
    ],
)
def test_each_unsafe_observation_fails_closed(
    policy: StagingAdmissionPolicy,
    healthy_snapshot: StagingHostSnapshot,
    snapshot_changes: dict[str, object],
    reason: AdmissionReason,
) -> None:
    snapshot = replace(healthy_snapshot, **snapshot_changes)

    outcome = evaluate_staging_admission(policy, snapshot)

    assert outcome.allowed is False
    assert reason in outcome.reasons
    assert reason.value in render_outcome(outcome)


def test_swapless_host_does_not_divide_by_zero(
    policy: StagingAdmissionPolicy,
    healthy_snapshot: StagingHostSnapshot,
) -> None:
    snapshot = replace(
        healthy_snapshot,
        swap_total_bytes=0,
        swap_free_bytes=0,
    )

    outcome = evaluate_staging_admission(policy, snapshot)

    assert outcome.allowed is True
    assert snapshot.swap_used_percent == 0.0


def test_environment_values_construct_the_typed_policy() -> None:
    policy = policy_from_environment(
        {
            "STAGING_MIN_AVAILABLE_MEMORY_GIB": "5",
            "STAGING_MAX_LOAD_PER_CPU": "1.25",
            "STAGING_MAX_SWAP_USED_PERCENT": "40",
            "STAGING_MAX_BLOCKED_PROCESSES": "1",
            "STAGING_MAX_IO_PRESSURE_AVG10_PERCENT": "12.5",
            "STAGING_DB_CONTAINER": "custom-db",
        }
    )

    assert policy == StagingAdmissionPolicy(
        minimum_available_memory_bytes=5 * GIB,
        maximum_load_per_cpu=1.25,
        maximum_swap_used_percent=40.0,
        maximum_blocked_processes=1,
        maximum_io_pressure_avg10_percent=12.5,
        required_database_container="custom-db",
    )


def test_invalid_environment_value_raises_typed_configuration_error() -> None:
    with pytest.raises(StagingAdmissionConfigurationError) as exc_info:
        policy_from_environment({"STAGING_MAX_SWAP_USED_PERCENT": "101"})

    assert exc_info.value.code is AdmissionConfigurationCode.INVALID_PERCENTAGE
    assert exc_info.value.field == "STAGING_MAX_SWAP_USED_PERCENT"


def test_heavy_work_observation_excludes_the_current_process_tree(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    for process_id, parent_id, command in (
        (100, 50, b"python3\0staging_host_admission.py\0"),
        (50, 1, b"bash\0db_sync_to_staging.sh\0"),
        (200, 1, b"pg_restore\0--single-transaction\0"),
    ):
        process_root = proc_root / str(process_id)
        process_root.mkdir(parents=True)
        (process_root / "stat").write_text(
            f"{process_id} (process) S {parent_id} 0 0 0\n",
            encoding="utf-8",
        )
        (process_root / "cmdline").write_bytes(command)

    active_work = _read_active_heavy_work(proc_root, current_pid=100)

    assert active_work == (HeavyWorkKind.DATABASE_RESTORE,)
