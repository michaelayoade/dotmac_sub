"""Static boundary for Subscriptions' tenant-plane shadow composition."""

from __future__ import annotations

import ast
import configparser
import tomllib
from pathlib import Path

from dotmac_billing.manifest import module as billing_module
from dotmac_collections.manifest import module as collections_module
from dotmac_kernel.planes import (
    ModulePlane,
    supported_plane_sets,
    validate_module_plane_selections,
)
from dotmac_subscriptions.manifest import module as subscriptions_module

from app.migration_bindings import (
    ASSEMBLY_MODULE_PLANES,
    ASSEMBLY_PREREQUISITE_BINDINGS,
)
from app.module_release_contracts import SUBSCRIPTIONS_RELEASE
from app.shadow.cohort import SUBSCRIPTIONS_REVISION

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
ALEMBIC_INI = ROOT / "alembic.ini"
ALEMBIC_ENV = ROOT / "alembic" / "env.py"
TEST_CONFTEST = ROOT / "tests" / "conftest.py"
ADOPTION_LEDGER = ROOT / "docs/PLATFORM_ADOPTION_LEDGER.md"
SUBSCRIPTIONS_PEELED_COMMIT = "ad6c5824086f6f550447caeabe820e860cdfe23c"
STARTER_RELEASE_RECORD_COMMIT = "d6044d2dbfdf4692f4f88da7f308c7f106b01181"
SUBSCRIPTIONS_WHEEL_SHA256 = (
    "01fd4a2260a09e26a45cd105c474e1c90dd7f0aee23bd470790701c1677ac53d"
)
SUBSCRIPTIONS_SDIST_SHA256 = (
    "b6a2111cb4d80ce2916d833190d048c9eaf50cd6ea6f3246077e8eb7340adcd4"
)


def _pyproject() -> dict[str, object]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _module_imports_under_app() -> list[str]:
    offenders: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(
                name == "dotmac_subscriptions"
                or name.startswith("dotmac_subscriptions.")
                for name in names
            ):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    return offenders


def test_tagged_subscriptions_and_required_kernel_are_exact_pins() -> None:
    data = _pyproject()
    project_dependencies = data["project"]["dependencies"]  # type: ignore[index]
    assert "dotmac-kernel==0.1.0a94" in project_dependencies
    assert "dotmac-subscriptions==0.1.0a3" in project_dependencies

    poetry_dependencies = data["tool"]["poetry"]["dependencies"]  # type: ignore[index]
    assert poetry_dependencies["dotmac-kernel"] == {
        "version": "0.1.0a94",
        "source": "forgejo",
    }
    assert poetry_dependencies["dotmac-subscriptions"] == {
        "version": "0.1.0a3",
        "source": "forgejo",
    }
    assert subscriptions_module.version == "0.1.0a3"


def test_subscriptions_a3_adoption_carries_immutable_release_oracles() -> None:
    assert SUBSCRIPTIONS_REVISION == SUBSCRIPTIONS_PEELED_COMMIT
    assert SUBSCRIPTIONS_RELEASE.package == "dotmac-subscriptions"
    assert SUBSCRIPTIONS_RELEASE.version == subscriptions_module.version
    assert SUBSCRIPTIONS_RELEASE.revision == SUBSCRIPTIONS_PEELED_COMMIT
    evidence = ADOPTION_LEDGER.read_text(encoding="utf-8")
    assert "dotmac-subscriptions-v0.1.0a3" in evidence
    assert SUBSCRIPTIONS_PEELED_COMMIT in evidence
    assert STARTER_RELEASE_RECORD_COMMIT in evidence


def test_lock_carries_the_exact_tagged_subscriptions_artifacts() -> None:
    lock = tomllib.loads((ROOT / "poetry.lock").read_text(encoding="utf-8"))
    packages = [
        package
        for package in lock["package"]
        if package["name"] == "dotmac-subscriptions"
    ]
    assert len(packages) == 1
    package = packages[0]
    assert package["version"] == "0.1.0a3"
    assert package["source"] == {
        "type": "legacy",
        "url": "https://registry.dotmac.io/api/packages/dotmac/pypi/simple",
        "reference": "forgejo",
    }
    hashes = {entry["file"]: entry["hash"] for entry in package["files"]}
    assert hashes["dotmac_subscriptions-0.1.0a3-py3-none-any.whl"] == (
        f"sha256:{SUBSCRIPTIONS_WHEEL_SHA256}"
    )
    assert hashes["dotmac_subscriptions-0.1.0a3.tar.gz"] == (
        f"sha256:{SUBSCRIPTIONS_SDIST_SHA256}"
    )


def test_alembic_owns_all_installed_module_resources_before_env_runs() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ALEMBIC_INI)
    locations = parser["alembic"]["version_locations"].split()
    assert locations == [
        "%(here)s/alembic/versions",
        "dotmac_billing.migrations:versions",
        "dotmac_collections.migrations:versions",
        "dotmac_payments.migrations:versions",
        "dotmac_service_orders.migrations:versions",
        "dotmac_subscriptions.migrations:versions",
    ]

    source = ALEMBIC_ENV.read_text(encoding="utf-8")
    assert 'config.set_main_option("version_locations"' not in source
    assert "install_module_plane_selections(ASSEMBLY_MODULE_PLANES)" in source

    # The migration-preparation command and pytest run in separate processes.
    # Pytest's direct ScriptDirectory callers must therefore autoload the same
    # typed graph declarations instead of relying on env.py process state.
    test_bootstrap = TEST_CONFTEST.read_text(encoding="utf-8")
    assert "install_migration_graph_environment()" in test_bootstrap


def test_subscriptions_has_one_explicit_supported_tenant_selection() -> None:
    assert len(ASSEMBLY_MODULE_PLANES) == 3
    selections = {item.module: item for item in ASSEMBLY_MODULE_PLANES}
    selection = selections["subscriptions"]
    assert selection.module == "subscriptions"
    assert tuple(selection.planes) == (ModulePlane.TENANT,)
    assert (ModulePlane.TENANT,) in supported_plane_sets(subscriptions_module)
    assert (
        validate_module_plane_selections(
            (billing_module, collections_module, subscriptions_module),
            ASSEMBLY_MODULE_PLANES,
        )
        == ASSEMBLY_MODULE_PLANES
    )


def test_subscriptions_binds_only_effects_sub_already_supplies() -> None:
    supplied = {binding.prerequisite for binding in ASSEMBLY_PREREQUISITE_BINDINGS}
    assert supplied == {
        "tenant_scope_catalog.v1",
        "module_database_roles.v1",
        "idempotency_ledger.v1",
        "outbox_relay.v1",
    }
    assert set(subscriptions_module.requires) <= supplied
    assert set(subscriptions_module.tenant_requires) <= supplied


def test_shadow_composition_adds_no_subscriptions_runtime_import() -> None:
    assert _module_imports_under_app() == []

    # The runtime commercial seam deliberately remains empty. Selecting a
    # migration plane is storage intent, not provider or writer authority.
    from app.composition import DEDICATED_ISP_PROFILE

    assert DEDICATED_ISP_PROFILE.commercial_provider == "none"
