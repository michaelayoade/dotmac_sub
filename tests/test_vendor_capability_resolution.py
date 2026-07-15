from __future__ import annotations

from app.models.network import VendorModelCapability
from app.services.network.vendor_capabilities import VendorCapabilities


def _capability(db_session, *, pattern: str | None):
    capability = VendorModelCapability(
        vendor="Ubiquiti",
        model="LBE-5AC-Gen2",
        firmware_pattern=pattern,
        supported_features={},
    )
    db_session.add(capability)
    db_session.flush()
    return capability


def test_resolver_selects_longest_matching_firmware_prefix(db_session):
    _capability(db_session, pattern=None)
    _capability(db_session, pattern="8")
    expected = _capability(db_session, pattern="8.7")

    resolved = VendorCapabilities.resolve_capability(
        db_session,
        vendor="ubiquiti",
        model="lbe-5ac-gen2",
        firmware="8.7.19",
    )

    assert resolved == expected


def test_resolver_does_not_guess_version_profile_without_observed_firmware(db_session):
    _capability(db_session, pattern="8.7")

    resolved = VendorCapabilities.resolve_capability(
        db_session,
        vendor="Ubiquiti",
        model="LBE-5AC-Gen2",
        firmware=None,
    )

    assert resolved is None


def test_resolver_uses_explicit_generic_when_firmware_is_unmapped(db_session):
    generic = _capability(db_session, pattern=None)
    _capability(db_session, pattern="8.7")

    resolved = VendorCapabilities.resolve_capability(
        db_session,
        vendor="Ubiquiti",
        model="LBE-5AC-Gen2",
        firmware="9.0.0",
    )

    assert resolved == generic
