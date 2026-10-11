"""Pure harness checks; no native hashing, DB, network, or load generation."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from scripts.security import argon2id_benchmark as bench


class FakeHasher:
    def hash(self, password: str) -> str:
        return "synthetic-encoded-value"

    def verify(self, hash: str, password: str) -> bool:
        return hash == "synthetic-encoded-value"


def test_default_plan_never_constructs_hasher(tmp_path: Path) -> None:
    def forbidden(parameters: bench.Parameters) -> bench.Hasher:
        pytest.fail("default plan attempted hashing")

    report = bench.run(bench.Request(), factory=forbidden, cgroup_directory=tmp_path)
    assert len(report.grid) == 8
    assert not report.measurements
    assert report.cgroup_before is None
    assert {(p.memory_mib, p.time_cost, p.parallelism) for p in report.grid} == {
        (m, t, 1) for m in (19, 32, 46, 64) for t in (2, 3)
    }
    assert bench.planned_grid(include_p2_control=True)[-1] == bench.Parameters(64, 2, 2)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "microbench"},
        {"execute": True},
        {"mode": "unknown"},
        {"samples": 0},
        {"samples": 101},
        {"warmups": -1},
        {"warmups": 6},
        {"max_seconds": 0},
        {"max_seconds": 121},
    ],
)
def test_request_guards(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        bench.Request(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("values", [(18, 2, 1), (64, 1, 1), (64, 3, 2), (19, 2, 3)])
def test_parameter_guards(values: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError):
        bench.Parameters(*values)


def test_quantiles_use_nearest_rank() -> None:
    assert bench.quantiles([4, 1, 3, 2]) == bench.Quantiles(4, 2, 4)
    assert bench.quantiles([7]) == bench.Quantiles(1, 7, 7)
    assert bench.quantiles(list(range(1, 101))) == bench.Quantiles(100, 50, 95)


@pytest.mark.parametrize("values", [[], [-1], [float("nan")], [float("inf")]])
def test_quantiles_reject_invalid(values: list[float]) -> None:
    with pytest.raises(ValueError):
        bench.quantiles(values)


def test_overlap_merges_processes_and_ends_before_starts() -> None:
    intervals = [
        bench.OperationInterval(1, 1, 0, "verify", 0, 10),
        bench.OperationInterval(1, 1, 1, "rehash", 10, 20),
        bench.OperationInterval(2, 1, 0, "hash", 5, 15),
        bench.OperationInterval(3, 1, 0, "hash", 20, 21),
    ]
    assert bench.analyze_intervals(intervals[::-1]) == bench.Overlap(4, 2)
    assert bench.analyze_intervals([]) == bench.Overlap(0, 0)
    with pytest.raises(ValueError, match="duplicate"):
        bench.analyze_intervals([intervals[0], intervals[0]])


@pytest.mark.parametrize(
    "values",
    [
        (0, 1, 0, "hash", 0, 1),
        (1, 0, 0, "hash", 0, 1),
        (1, 1, -1, "hash", 0, 1),
        (1, 1, 0, "hash", 1, 1),
        (1, 1, 0, "hash", 2, 1),
        (1, 1, 0, "hash", -1, 1),
        (1, 1, 0, "unknown", 0, 1),
        (True, 1, 0, "hash", 0, 1),
    ],
)
def test_malformed_intervals_rejected(values: tuple[object, ...]) -> None:
    with pytest.raises(ValueError):
        bench.OperationInterval(*values)  # type: ignore[arg-type]


def test_cgroup_distinguishes_zero_unavailable_and_malformed(tmp_path: Path) -> None:
    empty = bench.read_cgroup(tmp_path)
    assert empty.memory_peak_bytes is None and empty.cpu_stat is None
    assert empty.memory_status == empty.cpu_status == "unavailable"
    (tmp_path / "memory.peak").write_text("0\n")
    (tmp_path / "cpu.stat").write_text("nr_throttled 0\nthrottled_usec 0\n")
    zero = bench.read_cgroup(tmp_path)
    assert zero.memory_peak_bytes == 0
    assert zero.cpu_stat == {"nr_throttled": 0, "throttled_usec": 0}
    assert zero.memory_status == zero.cpu_status == "available"
    (tmp_path / "memory.peak").write_text("-1")
    (tmp_path / "cpu.stat").write_text("nr_throttled 0\nnr_throttled 1\n")
    bad = bench.read_cgroup(tmp_path)
    assert bad.memory_peak_bytes is None and bad.cpu_stat is None
    assert bad.memory_status == bad.cpu_status == "malformed"


def test_microbench_mocked_timings_and_secret_free_artifact(tmp_path: Path) -> None:
    calls: list[str] = []

    class RecordingHasher(FakeHasher):
        def hash(self, password: str) -> str:
            calls.append(password)
            return super().hash(password)

    ticks = iter(x / 1000 for x in range(1000))
    report = bench.run(
        bench.Request(mode="microbench", execute=True, samples=2, warmups=1),
        cgroup_directory=tmp_path,
        factory=lambda _: RecordingHasher(),
        clock=lambda: next(ticks),
    )
    assert len(calls) == 24 and len(report.measurements) == 8
    assert all(row.hash.count == row.verify.count == 2 for row in report.measurements)
    assert report.measurements[0].hash.p50_ms == pytest.approx(1)
    artifact = json.dumps(asdict(report))
    assert calls[0] not in artifact and "synthetic-encoded-value" not in artifact
    assert "pid" not in artifact and "hostname" not in artifact


def test_deadline_prevents_starting_operations(tmp_path: Path) -> None:
    ticks = iter((0.0, 2.0))
    with pytest.raises(TimeoutError):
        bench.run(
            bench.Request(mode="microbench", execute=True, max_seconds=1),
            cgroup_directory=tmp_path,
            factory=lambda _: FakeHasher(),
            clock=lambda: next(ticks),
        )


def test_cli_default_is_plan(capsys: pytest.CaptureFixture[str]) -> None:
    assert bench.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["request"]["mode"] == "plan"
    assert result["measurements"] == []


def test_cli_refuses_implicit_execution(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        bench.main(["--mode", "microbench"])
    assert error.value.code == 2
    assert capsys.readouterr().out == ""


def test_cli_sanitizes_library_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(request: bench.Request, *, cgroup_directory: Path) -> bench.Report:
        raise Exception("sensitive synthetic input")

    monkeypatch.setattr(bench, "run", fail)
    with pytest.raises(SystemExit) as error:
        bench.main(["--mode", "microbench", "--execute"])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "sensitive synthetic input" not in captured.err


def test_typed_resource_limits_reject_bool_and_float() -> None:
    with pytest.raises(ValueError):
        bench.Request(samples=True)
    with pytest.raises(ValueError):
        bench.Parameters(64.0, 2)  # type: ignore[arg-type]
