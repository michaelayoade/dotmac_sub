"""Tax storage composition and the exact gated native invoice adapter surface."""

from __future__ import annotations

import ast
import configparser
import re
import tomllib
from pathlib import Path

import pytest
from dotmac_kernel.planes import ModulePlane, supported_plane_sets
from dotmac_tax.manifest import module

from app.migration_bindings import (
    ASSEMBLY_MODULE_PLANES,
    ASSEMBLY_PREREQUISITE_BINDINGS,
)

ROOT = Path(__file__).resolve().parents[2]
RELEASE_REVISION = "fb7fb252c6fba8e15d7c39ed18f8057057adfbab"
ARTIFACT_HASHES = {
    "dotmac_tax-0.1.0a4-py3-none-any.whl": (
        "sha256:032908e1ca862b03ff9e649c1415bfcc067d3cd426ed1843ee4bf4693b0bd95e"
    ),
    "dotmac_tax-0.1.0a4.tar.gz": (
        "sha256:def1de45e6562a09dd400af52c11077892dad70f08294c59fee2757808e8498c"
    ),
}


_NATIVE_TAX_IMPORTS = {
    "app/services/billing/starter_tax_evidence.py": {
        ("dotmac_tax", "TaxCommercialSourceV1"),
        ("dotmac_tax", "TaxConflict"),
        ("dotmac_tax", "TaxDeterminationSetV2"),
        ("dotmac_tax", "TaxFact"),
        ("dotmac_tax", "TaxFactV2"),
        ("dotmac_tax", "TaxNotFound"),
        ("dotmac_tax", "TaxPriorResultReferenceV1"),
        ("dotmac_tax", "TaxRuleViolation"),
        ("dotmac_tax", "determine_tax_set_v2"),
    },
    "app/services/tax_policy_authoring.py": {
        ("dotmac_tax", "TaxAuthorityInput"),
        ("dotmac_tax", "TaxCodeV1"),
        ("dotmac_tax", "TaxConflict"),
        ("dotmac_tax", "TaxFactV2"),
        ("dotmac_tax", "TaxJurisdictionInput"),
        ("dotmac_tax", "TaxJurisdictionV1"),
        ("dotmac_tax", "TaxNotFound"),
        ("dotmac_tax", "TaxPolicyPreviewV1"),
        ("dotmac_tax", "TaxPriorResultReferenceV1"),
        ("dotmac_tax", "TaxRuleSupersessionInputV2"),
        ("dotmac_tax", "TaxRuleViolation"),
        ("dotmac_tax", "ensure_tax_authority_in_transaction"),
        ("dotmac_tax", "ensure_tax_code_in_transaction"),
        ("dotmac_tax", "ensure_tax_jurisdiction_in_transaction"),
        ("dotmac_tax", "ensure_tax_rule_v2_in_transaction"),
        ("dotmac_tax", "get_tax_code"),
        ("dotmac_tax", "get_tax_determination_set_v2"),
        ("dotmac_tax", "get_tax_jurisdiction"),
        ("dotmac_tax", "preview_tax_policy"),
        ("dotmac_tax", "preview_tax_policy_v2"),
        ("dotmac_tax", "supersede_tax_rule_v2"),
    },
    "app/services/tax_policy_authoring_contracts.py": {
        ("dotmac_tax", "TaxBaseDependencyPolicyV1"),
        ("dotmac_tax", "TaxCommercialSourceV1"),
        ("dotmac_tax", "TaxFact"),
        ("dotmac_tax", "TaxFactSignatureV1"),
        ("dotmac_tax", "TaxPolicyPreviewV1"),
        ("dotmac_tax", "TaxPriorResultReferenceV1"),
        ("dotmac_tax", "TaxRuleBandInput"),
        ("dotmac_tax", "TaxRuleInputV2"),
    },
    "app/services/tax_policy_authoring_workbench.py": {
        ("dotmac_tax", "ListTaxAuthoritiesV1"),
        ("dotmac_tax", "ListTaxCodesV1"),
        ("dotmac_tax", "ListTaxJurisdictionsV1"),
        ("dotmac_tax", "ListTaxRuleHistoryV2"),
        ("dotmac_tax", "TaxRuleHistoryPageV2"),
        ("dotmac_tax", "get_tax_code"),
        ("dotmac_tax", "get_tax_jurisdiction"),
        ("dotmac_tax", "get_tax_rule"),
        ("dotmac_tax", "list_tax_authorities"),
        ("dotmac_tax", "list_tax_codes"),
        ("dotmac_tax", "list_tax_jurisdictions"),
        ("dotmac_tax", "list_tax_rule_history_v2"),
    },
    "app/web/admin/billing_tax_policy.py": {
        ("dotmac_tax", "TaxConflict"),
        ("dotmac_tax", "TaxNotFound"),
        ("dotmac_tax", "TaxRuleViolation"),
    },
}


def _activation_errors(root: Path) -> tuple[str, ...]:
    """Only the named public source-fact adapter may import Tax; no SQL/internal owner."""
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
                if "mod_tax" in value and re.search(
                    r"\b(?:insert\s+into|update\s+|delete\s+from)\b", value
                ):
                    names = ("dotmac_tax",)
            if any(name.split(".")[0] == "dotmac_tax" for name in names):
                if isinstance(node, ast.ImportFrom):
                    imports = {(node.module or "", alias.name) for alias in node.names}
                    allowed = _NATIVE_TAX_IMPORTS.get(
                        path.relative_to(root).as_posix(), set()
                    )
                    if imports <= allowed:
                        continue
                errors.append(f"{path.relative_to(root)}:{node.lineno}")
    return tuple(errors)


def test_tax_release_and_kernel_are_exact_pins() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]
    assert "dotmac-tax==0.1.0a4" in dependencies
    assert "dotmac-kernel==0.1.0a105" in dependencies
    enriched = project["tool"]["poetry"]["dependencies"]
    assert enriched["dotmac-tax"] == {"version": "0.1.0a4", "source": "forgejo"}
    assert module.version == "0.1.0a4"
    lock = tomllib.loads((ROOT / "poetry.lock").read_text())
    packages = [p for p in lock["package"] if p["name"] == "dotmac-tax"]
    assert len(packages) == 1
    assert packages[0]["version"] == "0.1.0a4"
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
    assert "34711205539" in ledger


def test_tax_installed_lineage_has_one_supported_tenant_selection() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "alembic.ini")
    locations = parser["alembic"]["version_locations"].split()
    assert locations.count("dotmac_tax.migrations:versions") == 1
    selections = [item for item in ASSEMBLY_MODULE_PLANES if item.module == "tax"]
    assert selections == []  # Released Tax is atomic tenant-only, not selectable.
    assert (ModulePlane.TENANT,) in supported_plane_sets(module)
    bindings = {item.prerequisite: item for item in ASSEMBLY_PREREQUISITE_BINDINGS}
    expected = {
        "tenant_scope_catalog.v1": "545_tenant_scope_catalog_prereq",
        "module_database_roles.v1": "546_module_db_roles_prereq",
    }
    assert set(module.requires) | set(module.tenant_requires) == set(expected)
    for prerequisite, revision in expected.items():
        assert bindings[prerequisite].provider_revision == revision
        assert bindings[prerequisite].provider_owner == "sub"


def test_tax_composition_only_admits_the_native_invoice_adapter() -> None:
    from app.composition import DEDICATED_ISP_PROFILE

    assert _activation_errors(ROOT) == ()
    assert DEDICATED_ISP_PROFILE.commercial_provider == "none"


@pytest.mark.parametrize(
    "defect",
    (
        "from dotmac_tax.service import determine_tax_set\n",
        "import dotmac_tax.service as tax\n",
        'import importlib\nimportlib.import_module("dotmac_tax.service")\n',
        'sql = "INSERT INTO mod_tax.tax_authorities VALUES (1)"\n',
        "from importlib import import_module\n"
        'import_module("dotmac_tax.service").determine_tax_set()\n',
        "from importlib import import_module as load\n"
        'load("dotmac_tax.service").determine_tax_set()\n',
    ),
)
def test_activation_guard_rejects_planted_defects(tmp_path: Path, defect: str) -> None:
    app = tmp_path / "app"
    app.mkdir()
    (app / "accidental_writer.py").write_text(defect)
    assert _activation_errors(tmp_path)


@pytest.mark.parametrize(
    "defect",
    (
        "from dotmac_tax.service import determine_tax_set as determine\n",
        "from dotmac_tax.models import TaxRule\n",
        "from dotmac_tax import ensure_tax_rule as publish\n",
        "import dotmac_tax as tax\n",
        'sql = "UPDATE mod_tax.tax_rules SET status=1"\n',
    ),
)
def test_native_adapter_path_is_not_a_blanket_tax_import_exception(
    tmp_path: Path, defect: str
) -> None:
    adapter = tmp_path / "app/services/billing/starter_tax_evidence.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(defect)
    assert _activation_errors(tmp_path)


def test_native_issuance_retains_gate_before_each_runtime_family() -> None:
    tree = ast.parse((ROOT / "app/services/billing/starter_issuance.py").read_text())
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    for name, eligibility in (
        ("issue_admin_invoice_via_starter", "eligibility_reason"),
        ("issue_recurring_invoice_via_starter", "recurring_eligibility_reason"),
        ("issue_prepaid_invoice_via_starter", "prepaid_eligibility_reason"),
    ):
        calls = [
            node.func.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        assert eligibility in calls and "_issue_via_starter" in calls
        ordered_calls = {
            node.func.id: node.lineno
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {eligibility, "_issue_via_starter"}
        }
        assert ordered_calls[eligibility] < ordered_calls["_issue_via_starter"]
    gate_calls = [
        node.func.id
        for node in ast.walk(functions["eligibility_reason"])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "starter_ledger_live_enabled" in gate_calls
    # No migrated native issuance path may re-adopt legacy numeric tax inputs.
    forbidden = {
        "tax_rate_percent_snapshot",
        "tax_rate_code_snapshot",
        "withholding_tax_enabled",
    }
    assert not any(
        isinstance(node, ast.Attribute) and node.attr in forbidden
        for node in ast.walk(tree)
    )


def test_installed_tax_public_api_really_requires_the_binding_kernel_floor() -> None:
    """Read artifacts, not a copied declaration or an unused assembly import."""
    import importlib.metadata
    import inspect

    import dotmac_tax.service
    from packaging.requirements import Requirement
    from packaging.version import Version

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    composed = project["tool"]["poetry"]["dependencies"]
    floors: dict[str, Version] = {}
    for name, declaration in composed.items():
        if declaration.get("source") != "forgejo" or name == "dotmac-kernel":
            continue
        distribution = importlib.metadata.distribution(name)
        assert distribution.version == declaration["version"], name
        for text in distribution.requires or ():
            dependency = Requirement(text)
            if dependency.name.replace("_", "-") != "dotmac-kernel":
                continue
            bounds = [
                Version(item.version)
                for item in dependency.specifier
                if item.operator in {">=", "=="}
            ]
            assert bounds, f"{name} has no unambiguous installed kernel lower bound"
            floors[name] = max(bounds)
    assert floors["dotmac-tax"] == Version("0.1.0a98")
    # Billing 0.1.0a8 (2026-10-17 money-pack pin) is now the binding floor:
    # it requires the exact pinned kernel, above tax's a98 floor.
    assert floors["dotmac-billing"] == Version("0.1.0a105")
    assert max(floors.values()) == Version("0.1.0a105")
    assert importlib.metadata.version("dotmac-kernel") == "0.1.0a105"
    assert "site-packages" in Path(dotmac_tax.service.__file__).parts
    tree = ast.parse(inspect.getsource(dotmac_tax.service))
    for name in (
        "ensure_tax_authority",
        "ensure_tax_jurisdiction",
        "ensure_tax_code",
        "ensure_tax_rule",
    ):
        owner = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        assert any(
            isinstance(node, ast.ImportFrom)
            and node.module == "dotmac_kernel.transactions"
            and any(alias.name == "conflict_savepoint" for alias in node.names)
            for node in ast.walk(owner)
        ), name


def test_installed_tax_lineage_is_the_released_three_revision_chain() -> None:
    import hashlib
    import importlib.metadata

    distribution = importlib.metadata.distribution("dotmac-tax")
    assert distribution.version == "0.1.0a4"
    expected = {
        "tx_0001_tax": (
            None,
            "bf3091556eb5eac401e64cfe342a2d59c17b7d511c0c772aef034340b07012ab",
        ),
        "tx_0002_multi_tax": (
            "tx_0001_tax",
            "9b78094519fe8d0785735f3a4e3a37dacdb9901b88eda25c90cdb167474abde0",
        ),
        "tx_0003_result_fingerprint": (
            "tx_0002_multi_tax",
            "dd9751218ce7e27922a6ae4a869f6b25061dfa4bb93e0db7e152ea834579d4b3",
        ),
    }
    for revision, (predecessor, digest) in expected.items():
        path = Path(
            distribution.locate_file(f"dotmac_tax/migrations/versions/{revision}.py")
        )
        assert "site-packages" in path.parts
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        tree = ast.parse(path.read_text())
        assignments = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"revision", "down_revision"}
        }
        assert assignments == {"revision": revision, "down_revision": predecessor}
