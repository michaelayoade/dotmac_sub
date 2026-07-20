"""The /api/v1 contract surface matches the committed manifest.

Phase 1 of the platform adoption ledger: pin the external JSON/mobile/field
API contract so refactors (and later kernel adoption) cannot change it
silently. See ``openapi_contract_lib`` for what the manifest covers and why
the full OpenAPI document is not snapshotted wholesale.

On intentional contract changes, regenerate and review the manifest diff:

    python scripts/update_openapi_contract.py

The manifest lives next to this test (``openapi_contract_surface.json``).
"""

from __future__ import annotations

from tests.architecture import openapi_contract_lib as lib

_MAX_REPORTED = 40


def test_api_v1_contract_surface_matches_manifest() -> None:
    current = lib.compute_surface(lib.build_full_app())
    pinned = lib.load_manifest()
    drift = lib.diff_surfaces(pinned, current)
    shown = drift[:_MAX_REPORTED]
    if len(drift) > _MAX_REPORTED:
        shown.append(f"... and {len(drift) - _MAX_REPORTED} more")
    assert not drift, (
        "API contract surface drifted from the committed manifest. If the "
        "change is intentional, run `python scripts/update_openapi_contract.py` "
        "and review the manifest diff in this commit:\n  " + "\n  ".join(shown)
    )


def test_manifest_is_normalized() -> None:
    """The committed file is exactly what write_manifest produces — so
    regeneration never introduces formatting-only churn."""
    import json

    pinned_text = lib.MANIFEST_PATH.read_text(encoding="utf-8")
    normalized = json.dumps(json.loads(pinned_text), sort_keys=True, indent=1) + "\n"
    assert pinned_text == normalized, (
        "openapi_contract_surface.json is not normalized — regenerate it with "
        "scripts/update_openapi_contract.py instead of editing by hand"
    )


def test_openapi_operation_ids_are_unique() -> None:
    """One operation id must identify exactly one HTTP method/path operation."""
    schema = lib.build_full_app().openapi()
    locations: dict[str, list[str]] = {}
    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            operation_id = operation["operationId"]
            locations.setdefault(operation_id, []).append(f"{method.upper()} {path}")

    duplicates = {
        operation_id: routes
        for operation_id, routes in locations.items()
        if len(routes) > 1
    }
    assert duplicates == {}
