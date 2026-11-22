"""The CI architecture split cannot silently omit a test module."""

from scripts.ci.select_architecture_shard import (
    architecture_files,
    partition_architecture_files,
)


def test_architecture_shards_cover_each_module_once() -> None:
    groups = partition_architecture_files(shards=4)
    assert all(groups)
    selected = [path for group in groups for path in group]
    assert len(selected) == len(set(selected))
    assert set(selected) == set(architecture_files())
