"""Keep the retired detail-GET writer from returning through a second helper."""

import ast
from pathlib import Path

from app.services.customer_legacy_address_display import CONCERN
from app.services.sot_manifest import OwnerRole, TransactionMode
from app.services.sot_registry.registry import owning_service_for


def test_legacy_display_owner_is_pure_and_exactly_registered() -> None:
    path = Path("app/services/customer_legacy_address_display.py")
    tree = ast.parse(path.read_text())
    imports = [
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ]
    assert set(imports) <= {"dataclasses", "enum", "math"}
    assert not any(isinstance(node, ast.Import) for node in ast.walk(tree))
    service = owning_service_for(CONCERN)
    assert service is not None
    assert service.name == "customer.legacy_address_display"
    assert service.module == "app.services.customer_legacy_address_display"
    assert service.owns == (CONCERN,)
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.NOT_APPLICABLE
    assert service.contract.concerns[0].role is OwnerRole.RESOLVER


def test_customer_detail_read_path_has_no_transaction_or_geocoding_calls() -> None:
    source = Path("app/services/web_customer_details.py").read_text()
    tree = ast.parse(source)
    assert "GeocodePreviewRequest" not in source
    assert "geocoding_service" not in source
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    read_path = {
        "_build_person_fallback_address",
        "_legacy_coordinate",
        "_build_map_payload",
    }
    for name in read_path:
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    "add",
                    "add_all",
                    "delete",
                    "commit",
                    "rollback",
                    "flush",
                    "geocode_preview_from_request",
                }, name
            if isinstance(node, ast.Assign):
                assert not any(
                    isinstance(target, (ast.Attribute, ast.Subscript))
                    and "metadata_" in ast.unparse(target)
                    for target in node.targets
                ), name
    fallback = functions["_build_person_fallback_address"]
    assert not fallback.args.args
    assert [arg.arg for arg in fallback.args.kwonlyargs] == ["customer"]

    # Bound this guard to the retired location path. Notification error handling
    # elsewhere in the page is independent legacy debt, not a location writer.
    composed = functions["build_customer_detail_snapshot"]
    fallback_calls = [
        node
        for node in ast.walk(composed)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_build_person_fallback_address"
    ]
    assert len(fallback_calls) == 1
    assert not fallback_calls[0].args
    assert [keyword.arg for keyword in fallback_calls[0].keywords] == ["customer"]
