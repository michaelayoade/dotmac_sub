"""Pin the company-info page to owned, registered settings.

`app/services/web_system_company_info.py` used to write ~15 `company_*` keys
into `domain_settings` with raw ORM statements and no `SettingSpec`: a second,
unregistered settings surface beside the spec registry. The keys were never one
concern -- legal identity, tax registration, banking, a billing URL and
commission policy were being saved together and therefore governed together.

These tests fail if that surface comes back:

* the legal-identity keys must stay registered in the `comms` domain, where
  `customer.branding` (`app.services.brand_profiles`) already owns the other
  legacy convergence inputs -- not in `billing`, where they merely used to live;
* the module must not construct raw `DomainSetting` rows again;
* the six zero-consumer keys must not reappear anywhere in the codebase.

`company_vat_number` is deliberately absent from the registered set: its owner
is an open decision (see the module docstring), and this file asserts it stays
visible as such rather than being quietly assigned a domain.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.models.domain_settings import SettingDomain
from app.services import web_system_company_info
from app.services.settings_spec import get_spec

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
TEMPLATE_DIR = PROJECT_ROOT / "templates"
SCRIPT_DIR = PROJECT_ROOT / "scripts"
MODULE = APP_DIR / "services/web_system_company_info.py"

# Deleted with this slice. Every one of them had zero readers: the bank fields
# were already retired as an invoice fallback in favour of `collection_accounts`,
# per-partner commission lives on `Organization.commission_rate`, and nothing
# ever read the registration id or the billing URL.
RETIRED_ZERO_CONSUMER_KEYS = (
    "company_registration_id",
    "company_bank_name",
    "company_bank_account",
    "company_bank_branch",
    "billing_url",
    "partner_commission_pct",
)

# Retired-key sweep exemptions: files that name a retired key in order to keep
# it retired. The sweep matches quoted string literals only, so prose and
# unrelated identifiers (the `billing_url` Jinja macro parameter in
# `templates/components/portal/account_health.html`) do not register.
RETIRED_KEY_SWEEP_EXEMPT = {
    "tests/architecture/test_collection_account_ownership.py",
    "tests/architecture/test_company_identity_settings_ownership.py",
}


def _source_files() -> list[Path]:
    paths: list[Path] = []
    for root, pattern in (
        (APP_DIR, "**/*.py"),
        (TEMPLATE_DIR, "**/*.html"),
        (SCRIPT_DIR, "**/*.py"),
    ):
        if not root.exists():
            continue
        paths.extend(
            path
            for path in root.glob(pattern)
            if path.is_file() and "__pycache__" not in path.parts
        )
    return paths


def test_brand_identity_keys_are_registered_in_the_owning_domain() -> None:
    unregistered = [
        key
        for key in web_system_company_info.BRAND_IDENTITY_KEYS
        if get_spec(SettingDomain.comms, key) is None
    ]
    assert not unregistered, (
        "company legal-identity keys must be registered SettingSpecs in the "
        "comms domain, alongside the other customer.branding legacy "
        f"convergence inputs. Unregistered: {unregistered}"
    )


def test_brand_identity_keys_are_not_registered_in_billing() -> None:
    misfiled = [
        key
        for key in web_system_company_info.BRAND_IDENTITY_KEYS
        if get_spec(SettingDomain.billing, key) is not None
    ]
    assert not misfiled, (
        "company legal identity is owned by customer.branding, not billing. "
        f"Registering it in the billing domain recreates the mixed surface: {misfiled}"
    )


def test_company_info_writes_no_raw_setting_rows() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"), filename=str(MODULE))
    names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module in {"app.models", "app.models.domain_settings"}
        for alias in node.names
        if alias.name == "DomainSetting"
    }
    assert not names, (
        "web_system_company_info must persist through the domain-settings "
        "service, not by constructing DomainSetting rows itself"
    )


def test_retired_company_keys_have_no_readers_or_writers() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _source_files():
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative in RETIRED_KEY_SWEEP_EXEMPT:
            continue
        text = path.read_text(encoding="utf-8")
        for key in RETIRED_ZERO_CONSUMER_KEYS:
            if f'"{key}"' in text or f"'{key}'" in text:
                offenders.setdefault(key, []).append(relative)
    assert not offenders, (
        "retired zero-consumer company keys reappeared. Receiving accounts are "
        "owned by financial.collection_accounts and per-partner commission by "
        f"Organization.commission_rate: {offenders}"
    )


def test_the_unowned_tax_identity_key_stays_visible_as_an_open_decision() -> None:
    assert web_system_company_info.UNOWNED_TAX_IDENTITY_KEYS == ("company_vat_number",)
    for key in web_system_company_info.UNOWNED_TAX_IDENTITY_KEYS:
        assert get_spec(SettingDomain.comms, key) is None, (
            f"{key!r} was assigned to customer.branding without a decision; "
            "BrandProfile carries no tax-registration field"
        )
