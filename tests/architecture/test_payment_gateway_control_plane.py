"""Shrink-only guards for the payment-gateway control-plane cutover."""

from __future__ import annotations

from pathlib import Path

from app.services import sot_relationships
from app.services.sot_manifest import contract_validation_errors

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_payment_gateway_control_plane_has_named_owners() -> None:
    services = {item.name: item for item in sot_relationships.all_services()}

    for name, module in {
        "integration.installations": "app.services.integrations.installations",
        "financial.payment_routing": "app.services.payment_routing",
        "financial.payment_gateway_finance": ("app.services.payment_gateway_finance"),
        "financial.gateway_topup_intent_commands": (
            "app.services.gateway_topup_intents"
        ),
    }.items():
        service = services[name]
        assert service.module == module
        assert service.contract is not None
        assert (
            contract_validation_errors(
                service,
                service_names=set(services),
            )
            == ()
        )


def test_retired_gateway_routing_settings_do_not_return_to_runtime() -> None:
    retired_keys = {
        "payment_gateway_primary_provider",
        "payment_gateway_secondary_provider",
        "payment_gateway_failover_enabled",
    }
    runtime_paths = (
        ROOT / "app",
        ROOT / "templates",
    )

    matches = {
        path.relative_to(ROOT).as_posix()
        for root in runtime_paths
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".html", ".js"}
        and any(key in path.read_text(encoding="utf-8") for key in retired_keys)
    }

    assert matches == set()


def test_customer_checkout_templates_have_no_paystack_fallback() -> None:
    route_source = _source("app/web/customer/routes.py")
    template_source = "\n".join(
        (
            _source("templates/customer/billing/pay.html"),
            _source("templates/customer/billing/topup.html"),
        )
    )

    assert "selectedProvider || 'paystack'" not in template_source
    assert 'selectedProvider || "paystack"' not in template_source
    assert "default('paystack'" not in template_source
    assert 'default("paystack"' not in template_source
    assert '"provider_type": "paystack"' not in route_source
    assert "'provider_type': 'paystack'" not in route_source


def test_payment_provider_identity_is_read_only_outside_setup_owner() -> None:
    api_source = _source("app/api/billing.py")
    billing_source = _source("app/services/billing/providers.py")
    setup_source = _source("app/services/web_integrations_payment_gateways.py")

    assert '@router.post("/payment-providers"' not in api_source
    assert '@router.patch("/payment-providers/' not in api_source
    assert '@router.delete("/payment-providers/' not in api_source
    assert "def create(" not in billing_source
    assert "def update(" not in billing_source
    assert "def delete(" not in billing_source
    assert "payment_gateway_finance.ensure_gateway_identity(" in setup_source


def test_gateway_setup_accepts_references_not_secret_values() -> None:
    setup_source = _source("app/services/web_integrations_payment_gateways.py")
    registry_source = _source("app/services/integrations/registry.py")
    template_source = _source(
        "templates/admin/integrations/payment_gateways/config.html"
    )

    assert "is_secret_ref(" in setup_source
    assert "secret_refs=secret_refs" in setup_source
    assert "_DEFAULT_CONFIG" not in setup_source
    assert "_REQUIRED_REFS" not in setup_source
    assert "_CAPABILITIES" not in setup_source
    assert "_manifest_default_config(" in setup_source
    assert '"default": "https://api.paystack.co"' in registry_source
    assert '"default": "https://api.flutterwave.com/v3"' in registry_source
    assert 'value=""' in template_source
    assert "Secret values are managed under Settings" in template_source
