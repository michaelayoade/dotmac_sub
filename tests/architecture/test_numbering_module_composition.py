"""Pin released numbering and permit only the reviewed invoice participant."""

from __future__ import annotations

import ast
import configparser
import re
import tomllib
from pathlib import Path

import pytest
from dotmac_kernel.planes import ModulePlane, supported_plane_sets
from dotmac_numbering.manifest import module

from app.migration_bindings import (
    ASSEMBLY_MODULE_PLANES,
    ASSEMBLY_PREREQUISITE_BINDINGS,
)

ROOT = Path(__file__).resolve().parents[2]
RELEASE_REVISION = "62ecb6b0e198ba08bef9a096037194a9dfca4da0"
READINESS_OWNER = "app/services/billing_numbering_readiness.py"
READ_ONLY_IMPORTS = {
    "dotmac_numbering.models": frozenset(
        {"AllocationReceipt", "NumberSeries", "SeriesCounter"}
    ),
    "dotmac_numbering.service": frozenset(
        {
            "format_number",
            "period_for",
            "SeriesConfiguration",
            "MAX_DIGITS",
            "MAX_VALUE",
        }
    ),
}
READ_ONLY_PATH_IMPORTS = {
    READINESS_OWNER: READ_ONLY_IMPORTS,
    "app/services/billing_numbering_activation_proof.py": {
        "dotmac_numbering": frozenset(
            {
                "MAX_DIGITS",
                "MAX_VALUE",
                "AllocationReceipt",
                "NumberSeries",
                "SeriesConfiguration",
                "SeriesCounter",
                "format_number",
                "preview",
            }
        ),
    },
    "scripts/migration/billing_numbering_activation_proof.py": {
        "dotmac_numbering": frozenset({"SeriesConfiguration"}),
    },
}
INVOICE_PARTICIPANT = "app/services/billing/invoices.py"
INVOICE_IMPORTS = {
    "dotmac_numbering.models": frozenset({"NumberSeries", "SeriesCounter"}),
    "dotmac_numbering.service": frozenset({"MAX_DIGITS", "MAX_VALUE", "allocate"}),
}
ARTIFACT_HASHES = {
    "dotmac_numbering-0.1.0a3-py3-none-any.whl": (
        "sha256:03e9ac4de8b2d4501b6d3a0daf11131ec4d85c2913cd9e0d91a31937cdc9032c"
    ),
    "dotmac_numbering-0.1.0a3.tar.gz": (
        "sha256:4b609712451b804e44cef496b8f46dec86fda5c9491e3a21ed94f5187f0e8573"
    ),
}


def _activation_errors(root: Path) -> tuple[str, ...]:
    """Only exact canonical imports may reach the invoice participant."""
    errors: list[str] = []
    paths = [
        path
        for directory in (root / "app", root / "scripts")
        for path in directory.rglob("*.py")
    ]
    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        dynamic_import_names = {"__import__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "importlib":
                dynamic_import_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )
        for node in ast.walk(tree):
            names: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                allowed_imports = READ_ONLY_PATH_IMPORTS.get(
                    path.relative_to(root).as_posix(), {}
                )
                if node.module in allowed_imports and all(
                    alias.name in allowed_imports[node.module] for alias in node.names
                ):
                    continue
                if (
                    path.relative_to(root).as_posix() == INVOICE_PARTICIPANT
                    and node.module in INVOICE_IMPORTS
                    and all(
                        alias.name in INVOICE_IMPORTS[node.module]
                        for alias in node.names
                    )
                ):
                    continue
                names = (node.module or "",)
            elif isinstance(node, ast.Call):
                function = node.func
                dynamic_import = (
                    isinstance(function, ast.Name)
                    and function.id in dynamic_import_names
                ) or (
                    isinstance(function, ast.Attribute)
                    and function.attr == "import_module"
                )
                if dynamic_import and node.args:
                    target = node.args[0]
                    if isinstance(target, ast.Constant) and isinstance(
                        target.value, str
                    ):
                        names = (target.value,)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value.lower()
                if "mod_numbering" in value and re.search(
                    r"\b(?:insert\s+into|update\s+|delete\s+from)\b", value
                ):
                    names = ("dotmac_numbering",)
            if any(name.split(".")[0] == "dotmac_numbering" for name in names):
                errors.append(f"{path.relative_to(root)}:{node.lineno}")
    return tuple(errors)


def test_numbering_release_and_kernel_are_exact_pins() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]
    assert "dotmac-numbering==0.1.0a3" in dependencies
    assert "dotmac-kernel==0.1.0a105" in dependencies
    enriched = project["tool"]["poetry"]["dependencies"]
    assert enriched["dotmac-numbering"] == {"version": "0.1.0a3", "source": "forgejo"}
    assert module.version == "0.1.0a3"
    lock = tomllib.loads((ROOT / "poetry.lock").read_text())
    packages = [p for p in lock["package"] if p["name"] == "dotmac-numbering"]
    assert len(packages) == 1
    assert packages[0]["version"] == "0.1.0a3"
    assert packages[0]["source"] == {
        "type": "legacy",
        "url": "https://registry.dotmac.io/api/packages/dotmac/pypi/simple",
        "reference": "forgejo",
    }
    assert {
        entry["file"]: entry["hash"] for entry in packages[0]["files"]
    } == ARTIFACT_HASHES
    ledger = (ROOT / "docs/PLATFORM_ADOPTION_LEDGER.md").read_text()
    assert RELEASE_REVISION in ledger
    assert "39010380319" in ledger


def test_numbering_installed_lineage_has_one_supported_tenant_selection() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "alembic.ini")
    locations = parser["alembic"]["version_locations"].split()
    assert locations.count("dotmac_numbering.migrations:versions") == 1
    selections = [item for item in ASSEMBLY_MODULE_PLANES if item.module == "numbering"]
    assert len(selections) == 1
    assert selections[0].planes == (ModulePlane.TENANT,)
    assert (ModulePlane.TENANT,) in supported_plane_sets(module)
    bindings = {item.prerequisite: item for item in ASSEMBLY_PREREQUISITE_BINDINGS}
    expected = {
        "tenant_scope_catalog.v1": "545_tenant_scope_catalog_prereq",
        "module_database_roles.v1": "546_module_db_roles_prereq",
        "idempotency_ledger.v1": "556_idempotency_ledger_prereq",
    }
    assert set(module.requires) | set(module.tenant_requires) == set(expected)
    for prerequisite, revision in expected.items():
        assert bindings[prerequisite].provider_revision == revision
        assert bindings[prerequisite].provider_owner == "sub"


def test_numbering_composition_has_only_reviewed_invoice_participation() -> None:
    from app.composition import DEDICATED_ISP_PROFILE

    assert _activation_errors(ROOT) == ()
    assert DEDICATED_ISP_PROFILE.commercial_provider == "none"


@pytest.mark.parametrize(
    "defect",
    (
        "from dotmac_numbering.service import allocate\n",
        "import dotmac_numbering.service as numbering\n",
        'import importlib\nimportlib.import_module("dotmac_numbering.service")\n',
        'sql = "INSERT INTO mod_numbering.number_series VALUES (1)"\n',
        "from importlib import import_module\n"
        'import_module("dotmac_numbering.service").allocate()\n',
        "from importlib import import_module as load\n"
        'load("dotmac_numbering.service").allocate()\n',
    ),
)
def test_activation_guard_rejects_planted_defects(tmp_path: Path, defect: str) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "accidental_writer.py").write_text(defect)
    assert _activation_errors(tmp_path)


def test_readiness_owner_may_import_only_released_pure_read_contracts(
    tmp_path: Path,
) -> None:
    owner = tmp_path / READINESS_OWNER
    owner.parent.mkdir(parents=True)
    owner.write_text(
        "from dotmac_numbering.models import AllocationReceipt, NumberSeries, SeriesCounter\n"
        "from dotmac_numbering.service import (\n"
        "    format_number, period_for, SeriesConfiguration, MAX_DIGITS, MAX_VALUE\n"
        ")\n"
    )
    assert _activation_errors(tmp_path) == ()
    owner.write_text("from dotmac_numbering.service import allocate\n")
    assert _activation_errors(tmp_path)


@pytest.mark.parametrize("alias", ("import_module", "load"))
def test_readiness_owner_cannot_activate_a_dynamically_imported_writer(
    tmp_path: Path,
    alias: str,
) -> None:
    owner = tmp_path / READINESS_OWNER
    owner.parent.mkdir(parents=True)
    owner.write_text(
        f"from importlib import import_module as {alias}\n"
        f'{alias}("dotmac_numbering.service").allocate()\n'
    )
    assert _activation_errors(tmp_path)


@pytest.mark.parametrize(
    "path",
    (
        "app/services/billing_numbering_activation_proof.py",
        "scripts/migration/billing_numbering_activation_proof.py",
    ),
)
def test_v2_exact_paths_may_import_only_declared_public_reads(
    tmp_path: Path, path: str
) -> None:
    owner = tmp_path / path
    owner.parent.mkdir(parents=True)
    exports = READ_ONLY_PATH_IMPORTS[path]["dotmac_numbering"]
    owner.write_text(f"from dotmac_numbering import {', '.join(sorted(exports))}\n")
    assert _activation_errors(tmp_path) == ()
    # A neighboring path does not inherit the delegated read allowance.
    neighbor = owner.with_name("unregistered_numbering_reader.py")
    neighbor.write_text(owner.read_text())
    assert _activation_errors(tmp_path)


@pytest.mark.parametrize(
    "path",
    (
        "app/services/billing_numbering_activation_proof.py",
        "scripts/migration/billing_numbering_activation_proof.py",
    ),
)
@pytest.mark.parametrize(
    "defect",
    (
        "from dotmac_numbering import allocate\n",
        "from dotmac_numbering import configure_series\n",
        "from dotmac_numbering import advance_to_at_least\n",
        "from dotmac_numbering.service import allocate\n",
        "import dotmac_numbering as numbering\n",
        'import importlib\nimportlib.import_module("dotmac_numbering")\n',
        "from importlib import import_module as load\n"
        'load("dotmac_numbering.service").allocate()\n',
        'sql = "UPDATE mod_numbering.series_counters SET next_value=1"\n',
    ),
)
def test_v2_read_delegation_cannot_activate_or_seed(
    tmp_path: Path, path: str, defect: str
) -> None:
    owner = tmp_path / path
    owner.parent.mkdir(parents=True)
    owner.write_text(defect)
    assert _activation_errors(tmp_path)


def test_invoice_participant_cannot_import_configuration_or_counter_repair(
    tmp_path: Path,
) -> None:
    owner = tmp_path / INVOICE_PARTICIPANT
    owner.parent.mkdir(parents=True)
    owner.write_text(
        "from dotmac_numbering.models import NumberSeries, SeriesCounter\n"
        "from dotmac_numbering.service import MAX_DIGITS, MAX_VALUE, allocate\n"
    )
    assert _activation_errors(tmp_path) == ()
    for writer in ("configure_series", "advance_to_at_least"):
        owner.write_text(f"from dotmac_numbering.service import {writer}\n")
        assert _activation_errors(tmp_path)
