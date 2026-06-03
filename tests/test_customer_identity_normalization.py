from __future__ import annotations

from app.services.customer_identity_normalization import (
    normalize_channel_address,
    normalize_email_identifier,
    normalize_phone_identifier,
)


def test_normalize_email_identifier_lowercases_and_trims():
    assert normalize_email_identifier("  Mixed.Case@Example.COM  ") == (
        "mixed.case@example.com"
    )


def test_normalize_phone_identifier_handles_common_local_and_prefixed_forms():
    assert normalize_phone_identifier("(0801) 234-5678") == "+2348012345678"
    assert normalize_phone_identifier("whatsapp: 0808 111 2222") == "+2348081112222"
    assert normalize_phone_identifier("+1 (415) 555-0100") == "+14155550100"
    assert normalize_phone_identifier("2348012345678") == "+2348012345678"


def test_normalize_channel_address_uses_channel_type_hints():
    assert normalize_channel_address("email", " Person@Example.com ") == (
        "person@example.com"
    )
    assert normalize_channel_address("sms", "08012345678") == "+2348012345678"
    assert normalize_channel_address("whatsapp", "whatsapp:08081112222") == (
        "+2348081112222"
    )
