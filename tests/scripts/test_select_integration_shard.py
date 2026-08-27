"""Contract for duration-balanced integration sharding.

The single property that matters more than balance: **scheduling must never
decide execution.** Whatever the duration history contains -- nothing, garbage,
NaN, a negative number, a hostile key -- every integration file must still land
in exactly one shard. A sharding bug that drops a file is invisible: CI stays
green because the tests that would have failed were never collected.

So most of this module is the same assertion under adversarial inputs.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from scripts.ci.select_integration_shard import (
    REPOSITORY_ROOT,
    UNMEASURED_FILE_SECONDS,
    estimate_file_seconds,
    integration_files,
    load_durations,
    partition_integration_files,
    render_audit,
    select_integration_shard,
    unmeasured_estimate,
)


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "durations.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _relative(path: Path) -> str:
    return path.relative_to(REPOSITORY_ROOT).as_posix()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_discovery_is_recursive() -> None:
    """A subdirectory must not silently vanish from every shard.

    The previous selector used a non-recursive glob. Nothing under
    `tests/integration/` is nested today, so the bug was latent rather than
    active -- which is exactly why it needs a test rather than an observation.
    """

    root = REPOSITORY_ROOT / "tests" / "integration"
    expected = sorted(root.rglob("test_*.py"))
    assert integration_files() == expected
    assert len(expected) > 20, "the inventory is too small to be meaningful"


def test_discovery_would_find_a_nested_file(tmp_path: Path) -> None:
    """Sensitivity proof for the test above, on a synthetic tree.

    Asserting `rglob == rglob` cannot fail while the tree stays flat. This one
    fails if the implementation reverts to a non-recursive glob.
    """

    from scripts.ci import select_integration_shard as module

    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / "test_top.py").write_text("", encoding="utf-8")
    (nested / "test_deep.py").write_text("", encoding="utf-8")

    original = module.INTEGRATION_ROOT
    module.INTEGRATION_ROOT = tmp_path
    try:
        found = {path.name for path in module.integration_files()}
    finally:
        module.INTEGRATION_ROOT = original
    assert found == {"test_top.py", "test_deep.py"}


# --------------------------------------------------------------------------
# Completeness -- every file, exactly once
# --------------------------------------------------------------------------


@pytest.mark.parametrize("shards", [1, 2, 3, 4, 5, 8, 13])
def test_every_file_appears_exactly_once(shards: int) -> None:
    groups = partition_integration_files(shards=shards, durations_path=None)
    flattened = [path for group in groups for path in group]
    expected = integration_files()

    assert len(groups) == shards
    assert sorted(flattened) == expected, "a file was dropped or duplicated"
    assert len(flattened) == len(set(flattened)) == len(expected)


@pytest.mark.parametrize("shards", [1, 4, 7])
def test_selecting_each_shard_reproduces_the_whole_inventory(shards: int) -> None:
    """The CLI path, not just the partition function."""

    collected: list[Path] = []
    for shard in range(1, shards + 1):
        collected.extend(
            select_integration_shard(shard=shard, shards=shards, durations_path=None)
        )
    assert sorted(collected) == integration_files()
    assert len(collected) == len(set(collected))


def test_more_shards_than_files_still_covers_everything() -> None:
    """An over-provisioned matrix must produce empty shards, not lost files."""

    count = len(integration_files())
    groups = partition_integration_files(shards=count + 3, durations_path=None)
    flattened = [path for group in groups for path in group]
    assert sorted(flattened) == integration_files()
    assert sum(1 for group in groups if not group) == 3


@pytest.mark.parametrize("shards", [0, -1])
def test_a_non_positive_shard_count_is_refused(shards: int) -> None:
    with pytest.raises(ValueError):
        partition_integration_files(shards=shards, durations_path=None)


@pytest.mark.parametrize("shard", [0, 5, -1])
def test_an_out_of_range_shard_is_refused(shard: int) -> None:
    with pytest.raises(ValueError):
        select_integration_shard(shard=shard, shards=4, durations_path=None)


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_the_partition_is_stable_across_calls(tmp_path: Path) -> None:
    """Every runner must compute the same answer from the same inputs."""

    files = integration_files()
    durations = _write(
        tmp_path,
        {
            "schema_version": 1,
            "durations": {_relative(path): 1.0 for path in files},
        },
    )
    first = partition_integration_files(shards=4, durations_path=durations)
    for _ in range(5):
        assert partition_integration_files(shards=4, durations_path=durations) == first


def test_equal_durations_break_ties_on_path(tmp_path: Path) -> None:
    """With every duration identical, only a total order gives one answer."""

    files = integration_files()
    durations = _write(
        tmp_path,
        {"schema_version": 1, "durations": {_relative(p): 7.0 for p in files}},
    )
    groups = partition_integration_files(shards=4, durations_path=durations)

    # With equal weights, heaviest-first degenerates to path order and the
    # placement rule (lightest shard, lowest index) becomes round-robin. So the
    # assignment is fully determined: file i goes to shard i % 4.
    for index, path in enumerate(sorted(files)):
        assert path in groups[index % 4], (path, index)
    # And the counts can differ by at most one.
    counts = [len(group) for group in groups]
    assert max(counts) - min(counts) <= 1, counts


# --------------------------------------------------------------------------
# Balance comes from duration, not from size
# --------------------------------------------------------------------------


def test_a_slow_small_file_outweighs_a_fast_large_one(tmp_path: Path) -> None:
    """The defect being fixed, stated directly.

    Byte size and runtime are uncorrelated here: the old selector produced
    110/110/111/110 KB and a 22/6/5/4-minute spread. So the heaviest file by
    DURATION must be scheduled first regardless of how small its source is.
    """

    files = integration_files()
    smallest = min(files, key=lambda path: path.stat().st_size)
    durations = _write(
        tmp_path,
        {
            "schema_version": 1,
            "durations": {
                **{_relative(path): 1.0 for path in files},
                _relative(smallest): 10_000.0,
            },
        },
    )
    groups = partition_integration_files(shards=4, durations_path=durations)
    owning = [group for group in groups if smallest in group]
    assert len(owning) == 1
    # It is the single heaviest item, so it is placed first and its shard then
    # stays the fullest -- nothing else should join it.
    assert len(owning[0]) == 1


def test_balancing_actually_evens_out_a_lopsided_inventory(tmp_path: Path) -> None:
    """Non-vacuity: a partitioner that ignored durations would fail this."""

    files = integration_files()
    weights = {path: (200.0 if index < 4 else 1.0) for index, path in enumerate(files)}
    durations = _write(
        tmp_path,
        {
            "schema_version": 1,
            "durations": {_relative(p): w for p, w in weights.items()},
        },
    )
    groups = partition_integration_files(shards=4, durations_path=durations)
    totals = [sum(weights[path] for path in group) for group in groups]
    assert max(totals) - min(totals) < 200.0, totals
    # Each heavy file must be on a different shard.
    heavy = set(files[:4])
    assert all(len(heavy & set(group)) == 1 for group in groups)


# --------------------------------------------------------------------------
# Unmeasured files are assumed expensive -- never sized
# --------------------------------------------------------------------------


def test_an_unmeasured_file_is_estimated_conservatively(tmp_path: Path) -> None:
    files = integration_files()
    measured = {_relative(path): 5.0 for path in files[1:]}
    measured[_relative(files[2])] = 900.0
    durations = load_durations(
        _write(tmp_path, {"schema_version": 1, "durations": measured})
    )
    estimates = estimate_file_seconds(files, durations)
    assert estimates[files[0]] == 900.0, "an unknown file must assume the worst case"
    assert estimates[files[0]] >= UNMEASURED_FILE_SECONDS


def test_the_estimate_never_drops_below_the_floor() -> None:
    assert unmeasured_estimate({}) == UNMEASURED_FILE_SECONDS
    assert unmeasured_estimate({"a": 0.1}) == UNMEASURED_FILE_SECONDS
    assert unmeasured_estimate({"a": 999.0}) == 999.0


def test_source_size_is_not_consulted(tmp_path: Path) -> None:
    """With no history at all, every file must weigh the same.

    If byte size leaked back in as a fallback, the largest file would outweigh
    the smallest and this would fail.
    """

    files = integration_files()
    estimates = estimate_file_seconds(files, {})
    assert len(set(estimates.values())) == 1
    assert set(estimates.values()) == {UNMEASURED_FILE_SECONDS}


# --------------------------------------------------------------------------
# Corrupt history degrades scheduling, never execution
# --------------------------------------------------------------------------


CORRUPT_PAYLOADS = {
    "not_a_mapping": [1, 2, 3],
    "wrong_schema": {"schema_version": 99, "durations": {"a": 1.0}},
    "durations_not_a_mapping": {"schema_version": 1, "durations": "nonsense"},
    "empty": {},
}


@pytest.mark.parametrize("case", sorted(CORRUPT_PAYLOADS))
def test_corrupt_payloads_yield_no_durations(tmp_path: Path, case: str) -> None:
    assert load_durations(_write(tmp_path, CORRUPT_PAYLOADS[case])) == {}


def test_unparseable_json_yields_no_durations(tmp_path: Path) -> None:
    path = tmp_path / "durations.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert load_durations(path) == {}


def test_undecodable_bytes_yield_no_durations(tmp_path: Path) -> None:
    path = tmp_path / "durations.json"
    path.write_bytes(b"\xff\xfe\x00 not utf-8")
    assert load_durations(path) == {}


def test_a_missing_file_yields_no_durations(tmp_path: Path) -> None:
    assert load_durations(tmp_path / "absent.json") == {}
    assert load_durations(None) == {}


def test_hostile_entries_are_dropped_individually(tmp_path: Path) -> None:
    """One bad entry must not discard the whole history.

    NaN would poison every comparison in the sort; +inf would monopolise a
    shard and starve the rest; a JSON `true` is an int in Python but not a
    duration.
    """

    payload = {
        "schema_version": 1,
        "durations": {
            "tests/integration/good.py": 12.5,
            "tests/integration/nan.py": float("nan"),
            "tests/integration/pos_inf.py": float("inf"),
            "tests/integration/neg_inf.py": float("-inf"),
            "tests/integration/negative.py": -3.0,
            "tests/integration/boolean.py": True,
            "tests/integration/text.py": "40",
            "tests/integration/null.py": None,
        },
    }
    path = tmp_path / "durations.json"
    # json.dumps emits bare NaN/Infinity, which json.loads accepts by default.
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_durations(path)
    assert loaded == {"tests/integration/good.py": 12.5}
    assert all(math.isfinite(value) for value in loaded.values())


@pytest.mark.parametrize("case", [*sorted(CORRUPT_PAYLOADS), "unparseable", "missing"])
def test_every_file_still_runs_whatever_the_history_says(
    tmp_path: Path, case: str
) -> None:
    """THE load-bearing property: bad data changes balance, never coverage."""

    path = tmp_path / "durations.json"
    if case == "unparseable":
        path.write_text("{[}", encoding="utf-8")
    elif case == "missing":
        path = tmp_path / "absent.json"
    else:
        path.write_text(json.dumps(CORRUPT_PAYLOADS[case]), encoding="utf-8")

    groups = partition_integration_files(shards=4, durations_path=path)
    flattened = [item for group in groups for item in group]
    assert sorted(flattened) == integration_files()
    assert len(flattened) == len(set(flattened))


def test_history_naming_files_that_no_longer_exist_is_harmless(
    tmp_path: Path,
) -> None:
    """A renamed or deleted test must not unbalance or break the partition."""

    durations = _write(
        tmp_path,
        {
            "schema_version": 1,
            "durations": {
                "tests/integration/test_deleted_last_year.py": 5000.0,
                "tests/integration/../../etc/passwd": 1.0,
                _relative(integration_files()[0]): 3.0,
            },
        },
    )
    groups = partition_integration_files(shards=4, durations_path=durations)
    flattened = [item for group in groups for item in group]
    assert sorted(flattened) == integration_files()


# --------------------------------------------------------------------------
# Auditability
# --------------------------------------------------------------------------


def test_the_audit_names_every_shard_and_the_selected_files(tmp_path: Path) -> None:
    files = integration_files()
    durations = _write(
        tmp_path,
        {"schema_version": 1, "durations": {_relative(p): 2.0 for p in files}},
    )
    audit = render_audit(shard=2, shards=4, durations_path=durations)
    selected = select_integration_shard(shard=2, shards=4, durations_path=durations)

    assert "integration shard 2/4" in audit
    for index in range(1, 5):
        assert f"shard {index}:" in audit
    for path in selected:
        assert _relative(path) in audit
    assert "measured" in audit


def test_the_audit_admits_when_it_is_guessing(tmp_path: Path) -> None:
    """A reviewer must be able to tell a measured shard from an estimated one."""

    audit = render_audit(shard=1, shards=4, durations_path=tmp_path / "absent.json")
    assert "absent or unusable" in audit
    assert "estimated" in audit
    assert f"unmeasured assumed {UNMEASURED_FILE_SECONDS:.0f}s" in audit
