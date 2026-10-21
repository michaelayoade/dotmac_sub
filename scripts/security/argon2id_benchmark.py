"""Opt-in serial synthetic Argon2id measurements; never application policy."""

from __future__ import annotations

import argparse
import json
import math
import platform
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal, Protocol

Mode = Literal["plan", "microbench"]
Kind = Literal["verify", "hash", "rehash"]


@dataclass(frozen=True)
class Parameters:
    memory_mib: int
    time_cost: int
    parallelism: int = 1

    def __post_init__(self) -> None:
        if any(
            type(v) is not int
            for v in (self.memory_mib, self.time_cost, self.parallelism)
        ):
            raise ValueError("parameters must be integers")
        if (
            self.memory_mib not in (19, 32, 46, 64)
            or self.time_cost not in (2, 3)
            or self.parallelism not in (1, 2)
        ):
            raise ValueError("parameters outside the bounded grid")
        if self.parallelism == 2 and (self.memory_mib, self.time_cost) != (64, 2):
            raise ValueError("p=2 is only the explicit 64MiB/t2 control")


def planned_grid(*, include_p2_control: bool = False) -> tuple[Parameters, ...]:
    grid = tuple(Parameters(m, t) for m in (19, 32, 46, 64) for t in (2, 3))
    return grid + ((Parameters(64, 2, 2),) if include_p2_control else ())


@dataclass(frozen=True)
class Request:
    mode: Mode = "plan"
    execute: bool = False
    samples: int = 10
    warmups: int = 1
    max_seconds: int = 60
    include_p2_control: bool = False

    def __post_init__(self) -> None:
        if any(
            type(v) is not int for v in (self.samples, self.warmups, self.max_seconds)
        ):
            raise ValueError("resource limits must be integers")
        if type(self.execute) is not bool or type(self.include_p2_control) is not bool:
            raise ValueError("execution switches must be booleans")
        if self.mode not in ("plan", "microbench"):
            raise ValueError("unknown mode")
        if self.execute != (self.mode == "microbench"):
            raise ValueError("microbench requires --execute; plan forbids it")
        if not 1 <= self.samples <= 100 or not 0 <= self.warmups <= 5:
            raise ValueError("samples must be 1..100 and warmups 0..5")
        if not 1 <= self.max_seconds <= 120:
            raise ValueError("max-seconds must be 1..120")


@dataclass(frozen=True)
class Quantiles:
    count: int
    p50_ms: float
    p95_ms: float


def quantiles(samples_ms: Sequence[float]) -> Quantiles:
    """Nearest-rank percentiles; no interpolation or coordinated-omission claim."""
    if not samples_ms or any(not math.isfinite(x) or x < 0 for x in samples_ms):
        raise ValueError("timings must be finite nonnegative values")
    ordered = sorted(samples_ms)
    return Quantiles(
        len(ordered),
        ordered[math.ceil(len(ordered) * 0.5) - 1],
        ordered[math.ceil(len(ordered) * 0.95) - 1],
    )


@dataclass(frozen=True)
class OperationInterval:
    pid: int
    tid: int
    seq: int
    kind: Kind
    t_start_ns: int
    t_end_ns: int

    def __post_init__(self) -> None:
        values = (self.pid, self.tid, self.seq, self.t_start_ns, self.t_end_ns)
        if any(type(v) is not int for v in values):
            raise ValueError("interval fields must be integers")
        if (
            self.pid <= 0
            or self.tid <= 0
            or self.seq < 0
            or self.t_start_ns < 0
            or self.t_end_ns <= self.t_start_ns
        ):
            raise ValueError("interval must have positive duration and valid identity")
        if self.kind not in ("verify", "hash", "rehash"):
            raise ValueError("unknown operation kind")


@dataclass(frozen=True)
class Overlap:
    interval_count: int
    max_active_operations: int


def analyze_intervals(intervals: Sequence[OperationInterval]) -> Overlap:
    """Merge same-host monotonic-clock observations; half-open [start, end)."""
    if len(intervals) > 100_000:
        raise ValueError("at most 100000 intervals per analysis")
    identities: set[tuple[int, int, int]] = set()
    events: list[tuple[int, int]] = []
    for item in intervals:
        identity = (item.pid, item.tid, item.seq)
        if identity in identities:
            raise ValueError("duplicate (pid, tid, seq)")
        identities.add(identity)
        events.extend(((item.t_start_ns, 1), (item.t_end_ns, -1)))
    active = maximum = 0
    for _, delta in sorted(events):  # -1 (end) sorts before +1 (start) at ties.
        active += delta
        maximum = max(maximum, active)
    return Overlap(len(intervals), maximum)


@dataclass(frozen=True)
class CgroupSnapshot:
    memory_peak_bytes: int | None
    cpu_stat: dict[str, int] | None
    memory_status: Literal["available", "unavailable", "malformed"]
    cpu_status: Literal["available", "unavailable", "malformed"]


def read_cgroup(directory: Path) -> CgroupSnapshot:
    """Read only the selected cgroup v2 directory; never reset memory.peak."""
    memory: int | None = None
    cpu: dict[str, int] | None = None
    memory_status: Literal["available", "unavailable", "malformed"] = "unavailable"
    cpu_status: Literal["available", "unavailable", "malformed"] = "unavailable"
    try:
        with (directory / "memory.peak").open() as stream:
            raw = stream.read(4097)
        memory = int(raw)
        if len(raw) > 4096 or memory < 0:
            raise ValueError("invalid peak")
        memory_status = "available"
    except OSError:
        pass
    except ValueError:
        memory, memory_status = None, "malformed"
    try:
        with (directory / "cpu.stat").open() as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("oversized cpu.stat")
        cpu = {}
        for line in raw.splitlines():
            key, number = line.split()
            value = int(number)
            if key in cpu or value < 0:
                raise ValueError("invalid cpu.stat")
            cpu[key] = value
        if not {"nr_throttled", "throttled_usec"} <= cpu.keys():
            raise ValueError("missing throttling counters")
        cpu_status = "available"
    except OSError:
        pass
    except ValueError:
        cpu, cpu_status = None, "malformed"
    return CgroupSnapshot(memory, cpu, memory_status, cpu_status)


class Hasher(Protocol):
    def hash(self, password: str) -> str: ...
    def verify(self, hash: str, password: str) -> bool: ...


def argon2_hasher(parameters: Parameters) -> Hasher:
    from argon2 import PasswordHasher, Type

    return PasswordHasher(
        memory_cost=parameters.memory_mib * 1024,
        time_cost=parameters.time_cost,
        parallelism=parameters.parallelism,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


@dataclass(frozen=True)
class Measurement:
    parameters: Parameters
    hash: Quantiles
    verify: Quantiles


@dataclass(frozen=True)
class PlatformInfo:
    system: str
    release: str
    machine: str
    python: str
    argon2_cffi: str
    argon2_cffi_bindings: str


def platform_info() -> PlatformInfo:
    def library(name: str) -> str:
        try:
            return version(name)
        except PackageNotFoundError:
            return "unavailable"

    return PlatformInfo(
        platform.system(),
        platform.release(),
        platform.machine(),
        platform.python_version(),
        library("argon2-cffi"),
        library("argon2-cffi-bindings"),
    )


@dataclass(frozen=True)
class Report:
    schema_version: int
    request: Request
    platform: PlatformInfo
    grid: tuple[Parameters, ...]
    measurements: tuple[Measurement, ...]
    cgroup_before: CgroupSnapshot | None
    cgroup_after: CgroupSnapshot | None
    hash_length_bytes: int = 32
    salt_length_bytes: int = 16
    argon2_version: int = 19
    percentile_method: str = "nearest-rank"
    scope: str = "serial synthetic single-operation; no production recommendation"
    limitation: str = (
        "Mac data cannot substitute for production-class Linux/staging evidence"
    )


def run(
    request: Request,
    *,
    cgroup_directory: Path = Path("/sys/fs/cgroup"),
    factory: Callable[[Parameters], Hasher] = argon2_hasher,
    clock: Callable[[], float] = time.perf_counter,
) -> Report:
    grid = planned_grid(include_p2_control=request.include_p2_control)
    info = platform_info()
    if request.mode == "plan":
        return Report(1, request, info, grid, (), None, None)
    before = read_cgroup(cgroup_directory)
    deadline = clock() + request.max_seconds
    measurements: list[Measurement] = []
    # Generated synthetic input exists only in process memory; never reported.
    synthetic = secrets.token_urlsafe(32)
    for parameters in grid:
        hasher = factory(parameters)
        hashes: list[float] = []
        verifies: list[float] = []
        for index in range(request.warmups + request.samples):
            if clock() >= deadline:
                raise TimeoutError("benchmark budget expired; no complete artifact")
            start = clock()
            encoded = hasher.hash(synthetic)
            elapsed = (clock() - start) * 1000
            if index >= request.warmups:
                hashes.append(elapsed)
            if clock() >= deadline:
                raise TimeoutError("benchmark budget expired; no complete artifact")
            start = clock()
            if not hasher.verify(encoded, synthetic):
                raise RuntimeError("synthetic verification failed")
            elapsed = (clock() - start) * 1000
            if index >= request.warmups:
                verifies.append(elapsed)
        measurements.append(
            Measurement(parameters, quantiles(hashes), quantiles(verifies))
        )
    if clock() >= deadline:
        raise TimeoutError("benchmark budget expired; no complete artifact")
    return Report(
        1,
        request,
        info,
        grid,
        tuple(measurements),
        before,
        read_cgroup(cgroup_directory),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "microbench"), default="plan")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--max-seconds", type=int, default=60)
    parser.add_argument("--include-p2-control", action="store_true")
    parser.add_argument("--cgroup-directory", type=Path, default=Path("/sys/fs/cgroup"))
    args = parser.parse_args(argv)
    try:
        request = Request(
            mode=args.mode,
            execute=args.execute,
            samples=args.samples,
            warmups=args.warmups,
            max_seconds=args.max_seconds,
            include_p2_control=args.include_p2_control,
        )
        report = run(request, cgroup_directory=args.cgroup_directory)
    except Exception:
        # No exception payloads: a library error might include synthetic input.
        parser.exit(2, "benchmark refused or failed; no complete artifact\n")
    print(json.dumps(asdict(report), sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
