"""Structural guards for the field-mobile OIDC seam.

These pin the properties that a behaviour test cannot observe from outside: an
adapter that grew a query, a second session issuer, a refusal category invented
at a call site, or a log line that started carrying identity material. Each one
is a shape that would still pass every functional test in
``tests/test_oidc_mobile_federation.py`` while removing the guarantee.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = PROJECT_ROOT / "app" / "services" / "oidc_mobile_federation.py"
CONFIG = PROJECT_ROOT / "app" / "services" / "oidc_mobile_config.py"
JWKS = PROJECT_ROOT / "app" / "services" / "oidc_mobile_jwks.py"
ADAPTER = PROJECT_ROOT / "app" / "api" / "oidc_mobile.py"
SCHEMAS = PROJECT_ROOT / "app" / "schemas" / "oidc_mobile.py"
MODEL = PROJECT_ROOT / "app" / "models" / "oidc_mobile.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_adapter_issues_no_query_and_owns_no_decision() -> None:
    """A route that grows a query has taken a decision from the owner."""

    source = _source(ADAPTER)
    for forbidden in ("db.query", "db.execute", "db.scalars", "select(", "db.commit"):
        assert forbidden not in source, (
            f"{ADAPTER.name} contains {forbidden!r}; the redirect comparison, "
            "the replay refusal and the session issuance all belong behind "
            "app.services.oidc_mobile_federation"
        )


def test_the_owner_never_writes_a_second_session_issuer() -> None:
    """`AuthFlow` is the one issuance owner; this owner delegates to it.

    A second issuer would not fail any behaviour test — it would mint a
    perfectly good token — while quietly diverging on device supersession,
    refresh hashing, and the staff Party re-check.
    """

    source = _source(SERVICE)
    assert "issue_session_tokens" in source
    for forbidden in ("AuthSession(", "secrets.token_urlsafe(48)", "jwt.encode("):
        assert forbidden not in source, (
            f"{SERVICE.name} mints session material itself ({forbidden!r}); "
            "delegate to app.services.auth_flow.issue_session_tokens"
        )


def test_every_refusal_category_comes_from_the_closed_vocabulary() -> None:
    """A category invented at a call site is how a subject reaches a log line."""

    from app.services.oidc_mobile_federation import REFUSAL_REASONS

    used = set(re.findall(r'_refuse\(\s*"([^"]+)"', _source(SERVICE)))
    assert used, "the detector found no refusals at all"
    undeclared = sorted(used - REFUSAL_REASONS)
    assert not undeclared, f"undeclared refusal categories: {undeclared}"

    # Two-directional: a declared category with no refusal site is either dead
    # or a category someone expected to be emitted and is not.
    unused = sorted(REFUSAL_REASONS - used)
    assert not unused, f"declared refusal categories nothing emits: {unused}"


def test_an_undeclared_refusal_category_is_refused_at_construction() -> None:
    """The sensitivity proof for the test above: the guard must actually bite."""

    import pytest

    from app.services.oidc_mobile_federation import OidcFederationRefused

    with pytest.raises(ValueError):
        OidcFederationRefused("something_the_operator_should_never_see")


def test_no_identity_material_reaches_a_log_metric_or_event() -> None:
    """Not even truncated: a truncated identifier is still an identifier to
    whoever holds the other half."""

    forbidden = (
        "id_token",
        "nonce",
        "subject",
        "code_verifier",
        "access_token",
        "refresh_token",
        "kid",
    )
    for path in (SERVICE, JWKS, CONFIG):
        tree = ast.parse(_source(path), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = ast.unparse(node.func)
            is_log = target.startswith("logger.")
            is_metric = ".labels" in target or target.endswith(".inc")
            is_event = target == "emit_event" or target == "stage_audit_event"
            if not (is_log or is_metric or is_event):
                continue
            rendered = ast.unparse(node)
            for name in forbidden:
                assert f'"{name}"' not in rendered, (
                    f"{path.name}: {target} carries {name!r} — "
                    "identity material may never reach observability"
                )


def test_the_ceremony_row_has_nowhere_to_hold_a_verifier_or_a_nonce() -> None:
    """The guarantee is the schema. A column would eventually be filled."""

    source = _source(MODEL)
    assert "nonce_hash" in source
    assert "code_verifier" not in source
    assert "code_challenge" not in source
    assert "authorization_code" not in source
    assert re.search(r"^\s+nonce:\s*Mapped", source, re.MULTILINE) is None


def test_both_request_models_forbid_extra_fields() -> None:
    """This is what turns "Sub never receives the verifier" into a 422."""

    from app.schemas.oidc_mobile import (
        OidcMobileExchangeRequest,
        OidcMobileStartRequest,
    )

    for model in (OidcMobileStartRequest, OidcMobileExchangeRequest):
        assert model.model_config.get("extra") == "forbid", model.__name__
    assert 'extra="forbid"' in _source(SCHEMAS)


def test_the_algorithm_and_pkce_allowlists_are_code_rather_than_settings() -> None:
    """A configurable algorithm list is how `alg: none` reaches production."""

    from app.services.oidc_mobile_config import (
        ALLOWED_ID_TOKEN_ALGORITHMS,
        REQUIRED_CODE_CHALLENGE_METHOD,
    )

    assert ALLOWED_ID_TOKEN_ALGORITHMS == frozenset({"RS256"})
    assert REQUIRED_CODE_CHALLENGE_METHOD == "S256"

    from app.services.settings_spec import SETTINGS_SPECS

    keys = {spec.key for spec in SETTINGS_SPECS if spec.key.startswith("oidc_mobile_")}
    assert not any("algorithm" in key or "challenge" in key for key in keys), (
        "a security invariant became a knob: " + repr(sorted(keys))
    )


def test_every_federation_identifier_refuses_to_inherit() -> None:
    """A platform row must not answer "which issuer" for the operator tenant."""

    from app.models.domain_settings import SettingDomain
    from app.services.settings_spec import SETTINGS_SPECS

    identifiers = {
        "oidc_mobile_issuer",
        "oidc_mobile_client_id",
        "oidc_mobile_redirect_uri",
        "oidc_mobile_audience",
        "oidc_mobile_binding_key",
        "oidc_mobile_deployment_id",
        "oidc_mobile_jwks_uri",
    }
    seen = set()
    for spec in SETTINGS_SPECS:
        if spec.domain != SettingDomain.auth or spec.key not in identifiers:
            continue
        seen.add(spec.key)
        assert not spec.inherits, f"{spec.key} may not inherit across scopes"
        assert spec.default is None, f"{spec.key} must have no usable default"
    assert seen == identifiers, sorted(identifiers - seen)


def test_the_control_defaults_off_and_fails_closed() -> None:
    """Deploy dark is a declaration, not a deployment convention."""

    from app.services.control_registry import all_controls
    from app.services.oidc_mobile_config import OIDC_MOBILE_CONTROL

    control = next(item for item in all_controls() if item.key == OIDC_MOBILE_CONTROL)
    assert control.default is False
    assert control.on_missing is False
    # No owner_module: disabling a product module must not silently disable a
    # login path.
    assert control.owner_module is None


def test_the_control_is_actually_consumed_before_anything_happens() -> None:
    """A declared flag nothing reads is decoration."""

    config_source = _source(CONFIG)
    assert "control_registry.is_enabled(db, OIDC_MOBILE_CONTROL)" in config_source

    service_source = _source(SERVICE)
    assert service_source.count("federation_enabled(db)") >= 2, (
        "both endpoints must consult the control before doing anything"
    )


def test_the_boot_gate_refuses_an_enabled_but_unconfigured_deployment() -> None:
    main_source = _source(PROJECT_ROOT / "app" / "main.py")
    assert "_assert_oidc_federation_configured()" in main_source
    assert "verify_startup_configuration" in main_source


def test_the_jwks_refresh_is_bounded_in_the_source_as_well_as_in_behaviour() -> None:
    """The behaviour test counts calls; this pins the mechanism that bounds them.

    Stamping the attempt BEFORE the fetch is the load-bearing line: stamping on
    success instead would leave a permanently failing identity provider polled
    by every request, and no single-request test would notice.
    """

    source = _source(JWKS)
    stamp = source.index("entry.last_attempt_at = time.monotonic()")
    first_fetch = source.index("_jwks_uri(config", stamp)
    assert stamp < first_fetch, "the attempt stamp must precede the fetch"
    assert "jwks_min_refresh_seconds" in source
    assert "while " not in source, "a retry loop is not a bound"


def test_the_owner_is_registered_with_a_complete_typed_contract() -> None:
    from app.services.sot_manifest import contract_validation_errors
    from app.services.sot_registry.registry import all_services

    services = list(all_services())
    owner = next(
        item for item in services if item.name == "auth.oidc_mobile_federation"
    )
    assert owner.contract is not None
    assert not contract_validation_errors(
        owner, service_names={item.name for item in services}
    )
    # The concern strings the owner-command definitions use must be the exact
    # strings the manifest contracts; drift here is a runtime boundary failure,
    # not a documentation nit.
    from app.services.oidc_mobile_federation import (
        ADMISSION_CONCERN,
        CEREMONY_CONCERN,
    )

    assert set(owner.owns) == {CEREMONY_CONCERN, ADMISSION_CONCERN}
