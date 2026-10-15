"""Path-bound commercial shadow imports and released Payments read observation."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from dotmac_billing.manifest import module as billing_module
from dotmac_collections.manifest import module as collections_module
from dotmac_kernel.planes import ModulePlane

from app.migration_bindings import ASSEMBLY_MODULE_PLANES

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"


_SHADOW_IMPORTS = {
    "app/services/payment_intent_module_shadow.py": {
        ("dotmac_payments", "GetPaymentIntentV1"),
        ("dotmac_payments", "PaymentPurpose"),
        ("dotmac_payments", "get_payment_intent_v1"),
    },
    "app/services/dotmac_erp/billing_adapter.py": {
        ("dotmac_billing", "AccountingFactV1"),
    },
    "app/services/dotmac_erp/billing_accounting_fact.py": {
        ("dotmac_billing", "AccountingAllocationEffectV1"),
        ("dotmac_billing", "AccountingEffectV1"),
        ("dotmac_billing", "AccountingFactV1"),
        ("dotmac_billing", "AppliedFxSnapshotV1"),
        ("dotmac_billing", "AppliedTaxSnapshotV1"),
        ("dotmac_billing.contracts", "ACCOUNTING_FACT_CONTRACT"),
    },
    "app/services/collections_module_shadow.py": {
        ("dotmac_collections", "ReceivableObservationV1"),
    },
}


def _external_commercial_imports_under_app(root: Path = ROOT) -> list[str]:
    offenders: list[str] = []
    prefixes = (
        "dotmac_billing",
        "dotmac_collections",
        "dotmac_subscriptions",
        "dotmac_payments",
    )
    for path in (root / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        dynamic_names = {"__import__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "importlib":
                dynamic_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args:
                function = node.func
                dynamic = (
                    isinstance(function, ast.Name) and function.id in dynamic_names
                ) or (
                    isinstance(function, ast.Attribute)
                    and function.attr == "import_module"
                )
                target = node.args[0]
                if (
                    dynamic
                    and isinstance(target, ast.Constant)
                    and isinstance(target.value, str)
                ):
                    if any(
                        target.value == prefix or target.value.startswith(f"{prefix}.")
                        for prefix in prefixes
                    ):
                        offenders.append(
                            f"{path.relative_to(root)}:{node.lineno}:{target.value}:dynamic"
                        )
            if isinstance(node, ast.Import):
                imports = [(alias.name, "*") for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [(node.module or "", alias.name) for alias in node.names]
            else:
                continue
            rel = path.relative_to(root).as_posix()
            allowed = _SHADOW_IMPORTS.get(rel, set())
            for module, name in imports:
                if not any(
                    module == prefix or module.startswith(f"{prefix}.")
                    for prefix in prefixes
                ):
                    continue
                if (module, name) not in allowed:
                    offenders.append(f"{rel}:{node.lineno}:{module}:{name}")
    return offenders


def test_exact_tagged_commercial_dependencies_are_supply_pins() -> None:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project_dependencies = data["project"]["dependencies"]
    assert "dotmac-billing==0.1.0a1" in project_dependencies
    assert "dotmac-collections==0.1.0a1" in project_dependencies
    poetry_dependencies = data["tool"]["poetry"]["dependencies"]
    assert poetry_dependencies["dotmac-billing"] == {
        "version": "0.1.0a1",
        "source": "forgejo",
    }
    assert poetry_dependencies["dotmac-collections"] == {
        "version": "0.1.0a1",
        "source": "forgejo",
    }
    assert billing_module.version == "0.1.0a1"
    assert collections_module.version == "0.1.0a1"


def test_lock_carries_the_exact_billing_and_collections_artifacts() -> None:
    lock = tomllib.loads((ROOT / "poetry.lock").read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    expected = {
        "dotmac-billing": {
            "dotmac_billing-0.1.0a1-py3-none-any.whl": (
                "sha256:ec1f50c2e30b29c4f9e2427fe6c11d0fe98c1042920825efa50bf204c01dd50b"
            ),
            "dotmac_billing-0.1.0a1.tar.gz": (
                "sha256:c0ff6e1257a00ba09e62b832e471cffe3945504d1c2ad59a799baae77c0d0eef"
            ),
        },
        "dotmac-collections": {
            "dotmac_collections-0.1.0a1-py3-none-any.whl": (
                "sha256:f1ef5a38f70557a29e310f62e576983c3b971ce3ece5d778a702c619536e766b"
            ),
            "dotmac_collections-0.1.0a1.tar.gz": (
                "sha256:f3f298fbf7fa5ef05e5fa52369ac5d151a42b065d6be1eb81b644358be561aa1"
            ),
        },
    }
    for name, expected_hashes in expected.items():
        package = packages[name]
        assert package["version"] == "0.1.0a1"
        assert package["source"] == {
            "type": "legacy",
            "url": "https://registry.dotmac.io/api/packages/dotmac/pypi/simple",
            "reference": "forgejo",
        }
        assert {entry["file"]: entry["hash"] for entry in package["files"]} == (
            expected_hashes
        )


def test_both_dual_plane_modules_select_tenant_only() -> None:
    selections = {selection.module: selection for selection in ASSEMBLY_MODULE_PLANES}
    assert tuple(selections["billing"].planes) == (ModulePlane.TENANT,)
    assert tuple(selections["collections"].planes) == (ModulePlane.TENANT,)


def test_shadow_runtime_imports_are_path_bound_without_provider_switch() -> None:
    assert _external_commercial_imports_under_app() == []

    from app.composition import DEDICATED_ISP_PROFILE

    assert DEDICATED_ISP_PROFILE.commercial_provider == "none"


def test_shadow_runtime_import_boundary_is_sensitive(tmp_path: Path) -> None:
    app = tmp_path / "app" / "services"
    app.mkdir(parents=True)
    (app / "collections_module_shadow.py").write_text(
        "from dotmac_collections import ReceivableObservationV1, CollectionCaseService\n",
        encoding="utf-8",
    )
    (app / "other.py").write_text(
        "from dotmac_billing import ReceivableExposureV1\n"
        "from dotmac_subscriptions import SubscriptionService\n",
        encoding="utf-8",
    )

    violations = _external_commercial_imports_under_app(tmp_path)
    assert len(violations) == 3
    assert any("CollectionCaseService" in item for item in violations)
    assert any("dotmac_billing" in item for item in violations)
    assert any("dotmac_subscriptions" in item for item in violations)


def test_shadow_adapter_has_no_write_or_stateful_reader_surface() -> None:
    source = (ROOT / "app/services/collections_module_shadow.py").read_text(
        encoding="utf-8"
    )
    assert "CollectionCaseService" not in source
    assert "dotmac_collections.service" not in source
    assert "db.add(" not in source
    assert "db.flush(" not in source
    assert "process_once" not in source
    assert "source_version=1" in source
    assert "report-local" in source
    assert "blocker_pairs" in source
    assert "observe_at" in source


def test_accounting_transport_dto_allowlist_is_exact_and_path_bound(
    tmp_path: Path,
) -> None:
    owner = tmp_path / "app/services/dotmac_erp/billing_accounting_fact.py"
    owner.parent.mkdir(parents=True)
    owner.write_text("from dotmac_billing import AccountingFactV1 as ReleasedFact\n")
    assert _external_commercial_imports_under_app(tmp_path) == []
    owner.write_text("from dotmac_billing.service import accept_settlement\n")
    assert _external_commercial_imports_under_app(tmp_path)
    owner.write_text(
        'from importlib import import_module as load\nload("dotmac_billing.service")\n'
    )
    assert _external_commercial_imports_under_app(tmp_path)
    owner.unlink()
    (owner.parent / "wrong_owner.py").write_text(
        "from dotmac_billing import AccountingFactV1\n"
    )
    assert _external_commercial_imports_under_app(tmp_path)


def _committed_fact_runtime_activation(root: Path) -> tuple[str, ...]:
    offenders: list[str] = []
    for directory in (root / "app", root / "scripts"):
        for path in directory.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and any(
                    alias.name == "stage_committed_billing_fact" for alias in node.names
                ):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
                elif isinstance(node, ast.Call):
                    function = node.func
                    if (
                        isinstance(function, ast.Attribute)
                        and function.attr == "stage_committed_billing_fact"
                    ) or (
                        isinstance(function, ast.Name)
                        and function.id == "stage_committed_billing_fact"
                    ):
                        offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    return tuple(offenders)


def test_committed_billing_fact_transport_has_no_live_consumer() -> None:
    assert _committed_fact_runtime_activation(ROOT) == ()


def test_pending_consumer_guard_rejects_planted_activation(tmp_path: Path) -> None:
    consumer = tmp_path / "app/services/accidental_fact_consumer.py"
    consumer.parent.mkdir(parents=True)
    consumer.write_text(
        "from app.services.dotmac_erp.billing_adapter import stage_committed_billing_fact as stage\n"
        "stage(db, command, context=context)\n"
    )
    assert _committed_fact_runtime_activation(tmp_path)


def test_payments_getter_allowlist_is_exact_and_path_bound(tmp_path: Path) -> None:
    owner = tmp_path / "app/services/payment_intent_module_shadow.py"
    owner.parent.mkdir(parents=True)
    owner.write_text(
        "from dotmac_payments import GetPaymentIntentV1, get_payment_intent_v1\n"
    )
    assert _external_commercial_imports_under_app(tmp_path) == []
    owner.write_text("from dotmac_payments import record_confirmation\n")
    assert _external_commercial_imports_under_app(tmp_path)
    owner.write_text(
        'from importlib import import_module\nimport_module("dotmac_payments.service")\n'
    )
    assert _external_commercial_imports_under_app(tmp_path)
    owner.unlink()
    (owner.parent / "wrong_owner.py").write_text(
        "from dotmac_payments import get_payment_intent_v1\n"
    )
    assert _external_commercial_imports_under_app(tmp_path)


def test_payments_released_getter_supply_pin() -> None:
    project = tomllib.loads(PYPROJECT.read_text())
    assert "dotmac-payments==0.1.0a2" in project["project"]["dependencies"]
    assert project["tool"]["poetry"]["dependencies"]["dotmac-payments"] == {
        "version": "0.1.0a2",
        "source": "forgejo",
    }
    lock = tomllib.loads((ROOT / "poetry.lock").read_text())
    package = next(
        item for item in lock["package"] if item["name"] == "dotmac-payments"
    )
    assert package["version"] == "0.1.0a2"
    assert package["files"] == [
        {
            "file": "dotmac_payments-0.1.0a2-py3-none-any.whl",
            "hash": "sha256:da2877bc5365fc0d6cf93868a6235a608413edbb809199cd5c919f78390cf8ed",
        }
    ]
