import importlib.util
from pathlib import Path


def test_sales_lifecycle_migration_extends_pr_1508_head_once() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "389_sales_to_service_lifecycle.py"
    )
    spec = importlib.util.spec_from_file_location("sales_lifecycle_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = path.read_text(encoding="utf-8")

    assert module.revision == "389_sales_to_service_lifecycle"
    assert module.down_revision == "388_device_projection_class_facts"
    assert 'op.add_column(\n        "work_order"' not in source
    assert 'op.drop_column("project_tasks", "work_order_id")' not in source
