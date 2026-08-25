"""Sub prepares Billing adoption without creating a second live authority."""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

from scripts.architecture.billing_authority_retirement import (
    RetirementCategory,
    measure_authority,
)

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "adoption/dotmac_billing"
BASELINE = Path(__file__).with_name("billing_authority_retirement_baseline.json")
ADOPTION_ADR = ROOT / "docs/adr/0011-adopt-dotmac-billing-operational-receivables.md"
ADOPTION_DOSSIER = ROOT / "docs/adoption/dotmac-billing-candidate-dossier.md"
ADOPTION_RUNBOOK = ROOT / "docs/runbooks/DOTMAC_BILLING_TENANT_ADOPTION.md"


def test_adoption_harness_exact_pins_real_unpublished_candidates() -> None:
    with (HARNESS / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)

    assert project["project"]["dependencies"] == [
        "dotmac-kernel==0.1.0a69",
        "dotmac-billing==0.1.0a1",
    ]
    poetry = project["tool"]["poetry"]["dependencies"]
    assert poetry["dotmac-kernel"] == {
        "version": "0.1.0a69",
        "source": "forgejo",
    }
    assert poetry["dotmac-billing"] == {
        "version": "0.1.0a1",
        "source": "forgejo",
    }
    assert not (
        HARNESS / "poetry.lock"
    ).exists(), (
        "do not fabricate a registry lock before the exact artifacts are published"
    )


def test_candidate_is_not_composed_into_the_production_app_or_migrations() -> None:
    production_paths = (
        ROOT / "app/main.py",
        ROOT / "app/composition.py",
        ROOT / "alembic/env.py",
        ROOT / "pyproject.toml",
    )
    for path in production_paths:
        source = path.read_text(encoding="utf-8")
        assert "dotmac_billing" not in source
        assert "dotmac-billing" not in source


def test_adoption_sources_pin_the_timer_boundary_and_coupled_cutover() -> None:
    documents = tuple(
        path.read_text(encoding="utf-8")
        for path in (ADOPTION_ADR, ADOPTION_DOSSIER, ADOPTION_RUNBOOK)
    )
    joined = "\n".join(documents)
    normalized = " ".join(joined.split()).lower()

    assert "7e0543004864845f0035c9ec325e3f5064c281cc" in joined
    assert "4489ca1712f3c263d914f2af0ebfcf044aa70605" in joined
    assert "a9da920926a9d9212a8cf03a4744b48a1d4e14f2" in joined
    assert "outbox_relay.v1" in joined
    assert "three distinct" in normalized
    assert "coupled" in normalized
    assert "unknown/unverified" in normalized
    assert "production authority switch not authorized" in normalized
    assert "billing runtime dependency" in normalized


def test_harness_has_no_second_financial_or_transport_owner() -> None:
    source_files = tuple(sorted((HARNESS / "src").rglob("*.py")))
    assert source_files
    imports: set[str] = set()
    declared_classes: set[str] = set()
    for path in source_files:
        source = path.read_text(encoding="utf-8")
        assert "typing import Any" not in source
        assert "dict[str, Any]" not in source
        tree = ast.parse(source, filename=str(path))
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        declared_classes.update(
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        )

    assert not any(
        token in module.lower()
        for module in imports
        for token in ("provider", "paystack", "flutterwave", "general_ledger", "erp")
    )
    assert not declared_classes & {
        "AllocationCommand",
        "CoverageV1",
        "GeneralLedger",
        "Journal",
        "PaymentProviderClient",
    }


def test_all_six_retirement_categories_match_the_two_directional_baseline() -> None:
    current = measure_authority(ROOT)
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert expected["version"] == 1
    assert set(expected["categories"]) == {
        category.value for category in RetirementCategory
    }
    actual = json.loads(current.to_json())
    assert actual == expected, (
        "legacy Billing authority paths changed. A removal must lower the baseline "
        "in the same change; an addition is forbidden; a one-for-one replacement "
        "changes the digest and is also refused."
    )


def test_retirement_detector_has_a_sensitivity_proof(tmp_path: Path) -> None:
    app = tmp_path / "app"
    scripts = tmp_path / "scripts/tasks"
    app.mkdir(parents=True)
    scripts.mkdir(parents=True)
    (app / "offender.py").write_text(
        "from app.models.billing import Invoice, Payment, PaymentAllocation, TaxRate\n"
        "from app.services.billing.invoices import Invoices\n"
        "def mutate(invoice, payment):\n"
        "    invoice.balance_due = 0\n"
        "    invoice.tax_total = 0\n"
        "    Payments.update(payment)\n",
        encoding="utf-8",
    )
    (scripts / "provider_webhook.py").write_text(
        "def apply(payment):\n    return payment.refund()\n",
        encoding="utf-8",
    )

    report = measure_authority(tmp_path)

    for category in RetirementCategory:
        assert report.categories[category].count > 0, category
