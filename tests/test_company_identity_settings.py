"""Behaviour for the company-info page after the settings-registry cutover.

Legal identity, contact and address now resolve through the registered `comms`
specs owned by `customer.branding`; the six zero-consumer keys (banking,
registration id, billing URL, commission policy) can no longer be written.
"""

from __future__ import annotations

from app.models.domain_settings import DomainSetting, SettingDomain
from app.services import settings_spec, web_system_company_info
from app.services.brand_profiles import resolve_brand


def _rows(db_session, domain: SettingDomain) -> dict[str, str]:
    return {
        row.key: row.value_text or ""
        for row in db_session.query(DomainSetting)
        .filter(DomainSetting.domain == domain)
        .all()
    }


def test_saved_identity_resolves_through_the_spec_registry(db_session):
    web_system_company_info.save_company_info(
        db_session,
        {
            "company_name": "Dotmac Technologies Ltd",
            "company_email": "billing@dotmac.ng",
            "company_phone": "+234 801 234 5678",
            "company_address_street1": "12 Aminu Kano Crescent",
            "company_address_city": "Abuja",
            "company_address_country": "Nigeria",
            "company_vat_number": "VAT-99",
        },
    )

    assert (
        settings_spec.resolve_value(db_session, SettingDomain.comms, "company_name")
        == "Dotmac Technologies Ltd"
    )
    assert (
        settings_spec.resolve_value(
            db_session, SettingDomain.comms, "company_address_city"
        )
        == "Abuja"
    )
    info = web_system_company_info.get_company_info(db_session)
    assert info["company_phone"] == "+234 801 234 5678"
    assert info["company_vat_number"] == "VAT-99"


def test_identity_is_stored_in_the_owning_domain_not_billing(db_session):
    web_system_company_info.save_company_info(
        db_session, {"company_name": "Owned By Branding"}
    )

    comms_rows = _rows(db_session, SettingDomain.comms)
    billing_rows = _rows(db_session, SettingDomain.billing)

    assert comms_rows["company_name"] == "Owned By Branding"
    assert "company_name" not in billing_rows
    assert "company_vat_number" in billing_rows


def test_saved_identity_reaches_the_named_owner(db_session):
    web_system_company_info.save_company_info(
        db_session,
        {
            "company_name": "Brand Owner Ltd",
            "company_email": "support@dotmac.ng",
            "company_address_city": "Lagos",
        },
    )

    brand = resolve_brand(db_session)
    assert brand.legal_name == "Brand Owner Ltd"
    assert brand.support_email == "support@dotmac.ng"
    assert brand.legal_address["city"] == "Lagos"


def test_zero_consumer_keys_can_no_longer_be_written(db_session):
    web_system_company_info.save_company_info(
        db_session,
        {
            "company_name": "Dotmac Technologies Ltd",
            "company_bank_name": "Zenith Bank",
            "company_bank_account": "1234567890",
            "company_bank_branch": "Wuse II",
            "company_registration_id": "RC-123456",
            "billing_url": "https://billing.example.com",
            "partner_commission_pct": "12.5",
        },
    )

    written = set(_rows(db_session, SettingDomain.comms)) | set(
        _rows(db_session, SettingDomain.billing)
    )
    assert not written & {
        "company_bank_name",
        "company_bank_account",
        "company_bank_branch",
        "company_registration_id",
        "billing_url",
        "partner_commission_pct",
    }
    assert not set(web_system_company_info.get_company_info(db_session)) & {
        "company_bank_name",
        "billing_url",
        "partner_commission_pct",
    }
