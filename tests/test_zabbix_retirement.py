from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env.example",
        "docker-compose.yml",
        "scripts/deploy_shared_architecture.sh",
        "scripts/setup/deploy_reconcile.py",
    ],
)
def test_deployment_surface_does_not_reference_zabbix(relative_path: str) -> None:
    content = (ROOT / relative_path).read_text(encoding="utf-8")
    assert "zabbix" not in content.lower()


@pytest.mark.parametrize(
    "relative_path",
    [
        "app/api/zabbix.py",
        "app/services/zabbix.py",
        "app/tasks/zabbix_ingestion.py",
        "app/tasks/zabbix_sync.py",
        "scripts/one_off/topology_reconcile_dryrun.py",
    ],
)
def test_retired_zabbix_runtime_modules_are_absent(relative_path: str) -> None:
    assert not (ROOT / relative_path).exists()
