from __future__ import annotations

from types import SimpleNamespace


def test_validate_olt_profile_dependencies_passes_with_valid_audit(monkeypatch) -> None:
    from app.services.network import olt_dependency_preflight

    monkeypatch.setattr(
        olt_dependency_preflight,
        "audit_olt_config_pack_live",
        lambda *_args, **_kwargs: SimpleNamespace(
            is_valid=True,
            errors=[],
            to_dict=lambda: {"is_valid": True},
        ),
    )

    result = olt_dependency_preflight.validate_olt_profile_dependencies(
        SimpleNamespace(),
        olt_id="olt-1",
        operation="authorization",
    )

    assert result.success is True
    assert result.message == "OLT profile dependencies are valid."
    assert result.audit == {"is_valid": True}


def test_validate_olt_profile_dependencies_fails_with_compact_errors(
    monkeypatch,
) -> None:
    from app.services.network import olt_dependency_preflight

    monkeypatch.setattr(
        olt_dependency_preflight,
        "audit_olt_config_pack_live",
        lambda *_args, **_kwargs: SimpleNamespace(
            is_valid=False,
            errors=["missing WAN config profile(s): 0", "missing DBA profile(s): 50"],
            to_dict=lambda: {"is_valid": False},
        ),
    )

    result = olt_dependency_preflight.validate_olt_profile_dependencies(
        SimpleNamespace(),
        olt_id="olt-1",
        operation="provisioning",
    )

    assert result.success is False
    assert result.audit == {"is_valid": False}
    assert result.errors == [
        "missing WAN config profile(s): 0",
        "missing DBA profile(s): 50",
    ]
    assert result.message == (
        "OLT provisioning dependency audit failed: "
        "missing WAN config profile(s): 0; missing DBA profile(s): 50"
    )
