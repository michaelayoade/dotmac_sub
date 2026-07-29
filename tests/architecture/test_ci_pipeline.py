from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
SHARD_SCRIPT = ROOT / "scripts/ci/select_test_shard.py"


def _load_shard_module():
    spec = importlib.util.spec_from_file_location("select_test_shard", SHARD_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unit_shards_partition_all_unit_test_files_once() -> None:
    module = _load_shard_module()
    expected = set(module._test_files())
    groups = [
        set(module.select_shard(shard=shard, shards=4)) for shard in range(1, 5)
    ]

    assert set().union(*groups) == expected
    assert sum(len(group) for group in groups) == len(expected)


def test_ci_uses_one_named_application_image_cache_and_no_duplicate_publisher() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    e2e_workflow = (ROOT / ".github/workflows/e2e.yml").read_text(encoding="utf-8")

    assert workflow.count("uses: docker/build-push-action@v6") == 1
    assert "scope=dotmac-sub-application" in workflow
    assert "Publish the health-checked image" in workflow
    assert "docker/build-push-action" not in e2e_workflow
    assert 'image_tag="sha-${GITHUB_SHA::7}"' in e2e_workflow
    assert not (ROOT / ".github/workflows/ghcr.yml").exists()


def test_ci_retains_pre_merge_and_promotion_postgresql_gate() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "branches: [main, develop, dev]" in workflow
    assert "make test-integration" in workflow
    assert "poetry run alembic upgrade head" in workflow
