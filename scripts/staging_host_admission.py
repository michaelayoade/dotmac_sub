"""Fail-closed resource admission for a staging-host deployment.

The CLI is an infrastructure adapter.  It collects bounded host observations,
constructs the typed policy inputs below, and exits non-zero when the host is
not safe to mutate.  It never changes host or application state.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

GIB = 1024**3


class ContainerHealth(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    MISSING = "missing"
    UNKNOWN = "unknown"


class HeavyWorkKind(StrEnum):
    DATABASE_DUMP = "database_dump"
    DATABASE_RESTORE = "database_restore"
    STAGING_DATABASE_SYNC = "staging_database_sync"
    DATA_PIPELINE = "data_pipeline"


class AdmissionReason(StrEnum):
    DATABASE_UNHEALTHY = "database_unhealthy"
    HEAVY_WORK_ACTIVE = "heavy_work_active"
    HOST_LOAD_HIGH = "host_load_high"
    IO_PRESSURE_HIGH = "io_pressure_high"
    MEMORY_LOW = "memory_low"
    PROCESSES_BLOCKED = "processes_blocked"
    SWAP_USAGE_HIGH = "swap_usage_high"


class AdmissionConfigurationCode(StrEnum):
    INVALID_CONTAINER_NAME = "invalid_container_name"
    INVALID_FLOAT = "invalid_float"
    INVALID_INTEGER = "invalid_integer"
    INVALID_PERCENTAGE = "invalid_percentage"
    INVALID_POSITIVE_VALUE = "invalid_positive_value"


@dataclass(frozen=True, slots=True)
class StagingAdmissionConfigurationError(Exception):
    code: AdmissionConfigurationCode
    field: str
    supplied_value: str

    def __str__(self) -> str:
        return f"{self.code.value}: invalid {self.field} value"


@dataclass(frozen=True, slots=True)
class StagingAdmissionPolicy:
    minimum_available_memory_bytes: int
    maximum_load_per_cpu: float
    maximum_swap_used_percent: float
    maximum_blocked_processes: int
    maximum_io_pressure_avg10_percent: float
    required_database_container: str


@dataclass(frozen=True, slots=True)
class StagingHostSnapshot:
    cpu_count: int
    load1: float
    available_memory_bytes: int
    swap_total_bytes: int
    swap_free_bytes: int
    blocked_processes: int
    io_pressure_avg10_percent: float
    database_health: ContainerHealth
    active_heavy_work: tuple[HeavyWorkKind, ...]

    @property
    def swap_used_percent(self) -> float:
        if self.swap_total_bytes == 0:
            return 0.0
        used_bytes = self.swap_total_bytes - self.swap_free_bytes
        return 100.0 * used_bytes / self.swap_total_bytes

    @property
    def load_per_cpu(self) -> float:
        return self.load1 / self.cpu_count


@dataclass(frozen=True, slots=True)
class StagingAdmissionOutcome:
    allowed: bool
    reasons: tuple[AdmissionReason, ...]
    snapshot: StagingHostSnapshot


def evaluate_staging_admission(
    policy: StagingAdmissionPolicy,
    snapshot: StagingHostSnapshot,
) -> StagingAdmissionOutcome:
    reasons: list[AdmissionReason] = []
    if snapshot.database_health is not ContainerHealth.HEALTHY:
        reasons.append(AdmissionReason.DATABASE_UNHEALTHY)
    if snapshot.active_heavy_work:
        reasons.append(AdmissionReason.HEAVY_WORK_ACTIVE)
    if snapshot.load_per_cpu > policy.maximum_load_per_cpu:
        reasons.append(AdmissionReason.HOST_LOAD_HIGH)
    if snapshot.io_pressure_avg10_percent > policy.maximum_io_pressure_avg10_percent:
        reasons.append(AdmissionReason.IO_PRESSURE_HIGH)
    if snapshot.available_memory_bytes < policy.minimum_available_memory_bytes:
        reasons.append(AdmissionReason.MEMORY_LOW)
    if snapshot.blocked_processes > policy.maximum_blocked_processes:
        reasons.append(AdmissionReason.PROCESSES_BLOCKED)
    if snapshot.swap_used_percent > policy.maximum_swap_used_percent:
        reasons.append(AdmissionReason.SWAP_USAGE_HIGH)
    return StagingAdmissionOutcome(
        allowed=not reasons,
        reasons=tuple(reasons),
        snapshot=snapshot,
    )


def policy_from_environment(
    environment: Mapping[str, str],
) -> StagingAdmissionPolicy:
    minimum_memory_gib = _positive_float(
        environment,
        "STAGING_MIN_AVAILABLE_MEMORY_GIB",
        4.0,
    )
    maximum_load_per_cpu = _positive_float(
        environment,
        "STAGING_MAX_LOAD_PER_CPU",
        1.5,
    )
    maximum_swap_used_percent = _percentage(
        environment,
        "STAGING_MAX_SWAP_USED_PERCENT",
        50.0,
    )
    maximum_blocked_processes = _non_negative_integer(
        environment,
        "STAGING_MAX_BLOCKED_PROCESSES",
        2,
    )
    maximum_io_pressure = _percentage(
        environment,
        "STAGING_MAX_IO_PRESSURE_AVG10_PERCENT",
        20.0,
    )
    database_container = environment.get("STAGING_DB_CONTAINER", "dotmac_sub_db")
    if not database_container.strip():
        raise StagingAdmissionConfigurationError(
            AdmissionConfigurationCode.INVALID_CONTAINER_NAME,
            "STAGING_DB_CONTAINER",
            database_container,
        )
    return StagingAdmissionPolicy(
        minimum_available_memory_bytes=int(minimum_memory_gib * GIB),
        maximum_load_per_cpu=maximum_load_per_cpu,
        maximum_swap_used_percent=maximum_swap_used_percent,
        maximum_blocked_processes=maximum_blocked_processes,
        maximum_io_pressure_avg10_percent=maximum_io_pressure,
        required_database_container=database_container,
    )


def collect_staging_host_snapshot(
    policy: StagingAdmissionPolicy,
    *,
    proc_root: Path = Path("/proc"),
) -> StagingHostSnapshot:
    meminfo = _read_meminfo(proc_root / "meminfo")
    return StagingHostSnapshot(
        cpu_count=max(os.cpu_count() or 1, 1),
        load1=_read_load1(proc_root / "loadavg"),
        available_memory_bytes=meminfo["MemAvailable"] * 1024,
        swap_total_bytes=meminfo.get("SwapTotal", 0) * 1024,
        swap_free_bytes=meminfo.get("SwapFree", 0) * 1024,
        blocked_processes=_read_blocked_processes(proc_root / "stat"),
        io_pressure_avg10_percent=_read_io_pressure(proc_root / "pressure" / "io"),
        database_health=_read_container_health(policy.required_database_container),
        active_heavy_work=_read_active_heavy_work(proc_root),
    )


def render_outcome(outcome: StagingAdmissionOutcome) -> str:
    snapshot = outcome.snapshot
    state = "allowed" if outcome.allowed else "refused"
    reasons = ",".join(reason.value for reason in outcome.reasons) or "none"
    heavy_work = ",".join(item.value for item in snapshot.active_heavy_work) or "none"
    return (
        f"staging admission {state}: reasons={reasons} "
        f"load_per_cpu={snapshot.load_per_cpu:.2f} "
        f"memory_available_gib={snapshot.available_memory_bytes / GIB:.2f} "
        f"swap_used_percent={snapshot.swap_used_percent:.1f} "
        f"blocked_processes={snapshot.blocked_processes} "
        f"io_pressure_avg10_percent={snapshot.io_pressure_avg10_percent:.1f} "
        f"database_health={snapshot.database_health.value} "
        f"heavy_work={heavy_work}"
    )


def main() -> int:
    try:
        policy = policy_from_environment(os.environ)
        snapshot = collect_staging_host_snapshot(policy)
    except (
        KeyError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
        StagingAdmissionConfigurationError,
    ) as exc:
        print(f"staging admission refused: observation_error={exc}", file=sys.stderr)
        return 75

    outcome = evaluate_staging_admission(policy, snapshot)
    print(render_outcome(outcome), file=sys.stdout if outcome.allowed else sys.stderr)
    return 0 if outcome.allowed else 75


def _positive_float(
    environment: Mapping[str, str],
    key: str,
    default: float,
) -> float:
    raw = environment.get(key, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise StagingAdmissionConfigurationError(
            AdmissionConfigurationCode.INVALID_FLOAT,
            key,
            raw,
        ) from exc
    if value <= 0:
        raise StagingAdmissionConfigurationError(
            AdmissionConfigurationCode.INVALID_POSITIVE_VALUE,
            key,
            raw,
        )
    return value


def _percentage(
    environment: Mapping[str, str],
    key: str,
    default: float,
) -> float:
    value = _positive_float(environment, key, default)
    if value > 100:
        raise StagingAdmissionConfigurationError(
            AdmissionConfigurationCode.INVALID_PERCENTAGE,
            key,
            environment.get(key, str(default)),
        )
    return value


def _non_negative_integer(
    environment: Mapping[str, str],
    key: str,
    default: int,
) -> int:
    raw = environment.get(key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise StagingAdmissionConfigurationError(
            AdmissionConfigurationCode.INVALID_INTEGER,
            key,
            raw,
        ) from exc
    if value < 0:
        raise StagingAdmissionConfigurationError(
            AdmissionConfigurationCode.INVALID_INTEGER,
            key,
            raw,
        )
    return value


def _read_meminfo(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, raw = line.split(":", maxsplit=1)
        values[key] = int(raw.strip().split()[0])
    if "MemAvailable" not in values:
        raise ValueError("MemAvailable is absent from /proc/meminfo")
    return values


def _read_load1(path: Path) -> float:
    return float(path.read_text(encoding="utf-8").split()[0])


def _read_blocked_processes(path: Path) -> int:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("procs_blocked "):
            return int(line.split()[1])
    raise ValueError("procs_blocked is absent from /proc/stat")


def _read_io_pressure(path: Path) -> float:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("some "):
            continue
        for field in line.split()[1:]:
            key, value = field.split("=", maxsplit=1)
            if key == "avg10":
                return float(value)
    raise ValueError("some avg10 is absent from /proc/pressure/io")


def _read_container_health(container_name: str) -> ContainerHealth:
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            (
                "{{if .State.Health}}{{.State.Health.Status}}"
                "{{else}}{{.State.Status}}{{end}}"
            ),
            container_name,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return ContainerHealth.MISSING
    try:
        return ContainerHealth(result.stdout.strip())
    except ValueError:
        return ContainerHealth.UNKNOWN


def _read_active_heavy_work(
    proc_root: Path,
    *,
    current_pid: int | None = None,
) -> tuple[HeavyWorkKind, ...]:
    markers: tuple[tuple[bytes, HeavyWorkKind], ...] = (
        (b"pg_dump", HeavyWorkKind.DATABASE_DUMP),
        (b"pg_restore", HeavyWorkKind.DATABASE_RESTORE),
        (b"db_sync_to_staging", HeavyWorkKind.STAGING_DATABASE_SYNC),
        (b"dotmac_data", HeavyWorkKind.DATA_PIPELINE),
    )
    found: set[HeavyWorkKind] = set()
    excluded_pids = _ancestor_process_ids(
        proc_root,
        current_pid if current_pid is not None else os.getpid(),
    )
    for command_path in proc_root.glob("[0-9]*/cmdline"):
        if int(command_path.parent.name) in excluded_pids:
            continue
        try:
            command = command_path.read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for marker, work_kind in markers:
            if marker in command:
                found.add(work_kind)
    return tuple(sorted(found, key=lambda item: item.value))


def _ancestor_process_ids(proc_root: Path, starting_pid: int) -> set[int]:
    process_ids: set[int] = set()
    process_id = starting_pid
    while process_id > 1 and process_id not in process_ids:
        process_ids.add(process_id)
        try:
            stat = (proc_root / str(process_id) / "stat").read_text(encoding="utf-8")
            fields_after_name = stat.rsplit(")", maxsplit=1)[1].split()
            process_id = int(fields_after_name[1])
        except (FileNotFoundError, IndexError, PermissionError, ValueError):
            break
    return process_ids


if __name__ == "__main__":
    raise SystemExit(main())
