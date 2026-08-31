"""Keep Sub's live ERP wire contract provider- and predecessor-neutral."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ERP_TRANSPORT_ROOT = PROJECT_ROOT / "app/services/dotmac_erp"
EXPLICIT_WIRE_SOURCES = (
    PROJECT_ROOT / "app/api/erp_material_webhooks.py",
    PROJECT_ROOT / "app/schemas/erp_material_webhook.py",
    PROJECT_ROOT / "app/services/integrations/connectors/dotmac_erp.py",
    PROJECT_ROOT / "app/services/integrations/erp_capability.py",
)
LEGACY_WIRE_NAMES = frozenset(
    {
        "crm_id",
        "crm_invoice_id",
        "customer_crm_id",
        "omni_id",
        "omni_project_id",
        "omni_quote_id",
        "omni_work_order_id",
        "project_crm_id",
        "ticket_crm_id",
    }
)


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _legacy_wire_names(source: str) -> set[tuple[int, str, str]]:
    """Find executable declarations, mapping keys, and compatibility aliases.

    Attribute reads such as ``row.crm_ticket_id`` are intentionally not findings:
    those are retained historical provenance being translated into a neutral wire
    role. Comments and docstrings are also inert. The boundary rejects only names
    that a live caller can emit, accept, or invoke.
    """

    findings: set[tuple[int, str, str]] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.arg in LEGACY_WIRE_NAMES:
            findings.add((node.lineno, "argument", node.arg))
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id in LEGACY_WIRE_NAMES
            ):
                findings.add((node.lineno, "declared field", node.target.id))
        elif isinstance(node, ast.keyword):
            if node.arg in LEGACY_WIRE_NAMES:
                findings.add((node.lineno, "constructor keyword", node.arg))
            elif (
                node.arg in {"alias", "serialization_alias", "validation_alias"}
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and node.value.value in LEGACY_WIRE_NAMES
            ):
                findings.add((node.value.lineno, "Pydantic alias", node.value.value))
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value in LEGACY_WIRE_NAMES
                ):
                    findings.add((key.lineno, "mapping key", key.value))
        elif isinstance(node, ast.Subscript):
            key = node.slice
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in LEGACY_WIRE_NAMES
            ):
                findings.add((key.lineno, "mapping access", key.value))
        elif isinstance(node, ast.Call) and _called_name(node) in {
            "AliasChoices",
            "get",
        }:
            for argument in node.args:
                if (
                    isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                    and argument.value in LEGACY_WIRE_NAMES
                ):
                    findings.add((argument.lineno, "accepted key", argument.value))
    return findings


def _wire_sources() -> tuple[Path, ...]:
    assert ERP_TRANSPORT_ROOT.is_dir(), "the ERP transport family is missing"
    transport_sources = tuple(sorted(ERP_TRANSPORT_ROOT.rglob("*.py")))
    assert transport_sources, "the ERP transport-family scan is vacuous"
    sources = (*transport_sources, *EXPLICIT_WIRE_SOURCES)
    for path in sources:
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        assert path.is_file(), f"declared ERP wire source is missing: {relative_path}"
    return sources


def _legacy_crm_paths(source: str) -> set[tuple[int, str]]:
    findings: set[tuple[int, str]] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "/sync/crm" in node.value
        ):
            findings.add((node.lineno, node.value))
    return findings


def test_live_sub_to_erp_contract_has_no_crm_or_omni_compatibility_names() -> None:
    findings: list[str] = []
    for path in _wire_sources():
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        assert path.is_file(), f"declared ERP wire source is missing: {relative_path}"
        for line, kind, name in sorted(
            _legacy_wire_names(path.read_text(encoding="utf-8"))
        ):
            findings.append(f"{relative_path}:{line}: {kind} {name!r}")

    assert not findings, (
        "live Sub -> ERP DTOs and serializers must use neutral source roles; "
        "historical CRM/Omni provenance may be read but never emitted or accepted:\n  "
        + "\n  ".join(findings)
    )


def test_live_sub_to_erp_contract_has_no_direct_crm_route() -> None:
    findings: list[str] = []
    for path in _wire_sources():
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        for line, route in sorted(_legacy_crm_paths(path.read_text(encoding="utf-8"))):
            findings.append(f"{relative_path}:{line}: {route!r}")

    assert not findings, (
        "live Sub -> ERP transport must use the neutral /sync/sub namespace:\n  "
        + "\n  ".join(findings)
    )


def test_the_direct_crm_route_guard_bites() -> None:
    planted = """
def fetch(client):
    return client.get("/api/v1/sync/crm/example")
"""

    assert _legacy_crm_paths(planted) == {
        (3, "/api/v1/sync/crm/example"),
    }


def test_the_neutral_wire_guard_bites_on_each_executable_alias_shape() -> None:
    planted = """
from pydantic import AliasChoices, BaseModel, Field

class Payload(BaseModel):
    customer_crm_id: str | None = None
    source_id: str = Field(validation_alias=AliasChoices("crm_id", "source_id"))
    source_invoice_id: str = Field(serialization_alias="crm_invoice_id")

def lookup(omni_id: str, payload: dict[str, str]) -> dict[str, str]:
    emitted = Payload(
        customer_crm_id="customer-1",
        source_id="source-1",
        source_invoice_id="invoice-1",
    )
    return emitted.model_dump() | {
        "project_crm_id": payload["ticket_crm_id"],
        "source_invoice_id": payload.get("crm_invoice_id", ""),
        "omni_work_order_id": omni_id,
    }
"""

    findings = _legacy_wire_names(planted)

    assert {name for _line, _kind, name in findings} == {
        "crm_id",
        "crm_invoice_id",
        "customer_crm_id",
        "omni_id",
        "omni_work_order_id",
        "project_crm_id",
        "ticket_crm_id",
    }


def test_historical_provenance_reads_are_not_misclassified_as_wire_aliases() -> None:
    assert (
        _legacy_wire_names(
            '''
def project_reference(row):
    """Translate a retained CRM provenance value into a neutral role."""
    # crm_id and omni_id in prose are not transport fields.
    return row.crm_project_id
'''
        )
        == set()
    )
