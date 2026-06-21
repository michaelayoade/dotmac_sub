"""Audited clear helpers for ONT status (SM-gap #42).

reset/decommission/autofind paths now route their authorization_status /
provisioning_status writes through ont_status helpers instead of direct
assignment, so every write is observable + (for transitions) validated.
"""

from app.models.network import OntAuthorizationStatus, OntProvisioningStatus, OntUnit
from app.services.network.ont_status import (
    clear_authorization_status,
    clear_provisioning_status,
    set_authorization_status,
)


def _ont(**kw) -> OntUnit:
    return OntUnit(serial_number="TESTONT0001", **kw)


def test_clear_authorization_status_sets_none():
    ont = _ont(authorization_status=OntAuthorizationStatus.authorized)
    clear_authorization_status(ont, reason="decommission")
    assert ont.authorization_status is None


def test_clear_provisioning_status_sets_none():
    ont = _ont(provisioning_status=OntProvisioningStatus.provisioned)
    clear_provisioning_status(ont, reason="decommission")
    assert ont.provisioning_status is None


def test_clear_is_idempotent_when_already_none():
    ont = _ont()
    clear_authorization_status(ont, reason="inventory_reset")
    clear_provisioning_status(ont, reason="inventory_reset")
    assert ont.authorization_status is None
    assert ont.provisioning_status is None


def test_autofind_reauthorize_from_deauthorized_is_allowed_and_audited():
    # deauthorized -> authorized is a legal transition; routing autofind through
    # the guarded setter makes it observable instead of a silent direct write.
    ont = _ont(authorization_status=OntAuthorizationStatus.deauthorized)
    set_authorization_status(ont, OntAuthorizationStatus.authorized, strict=False)
    assert ont.authorization_status == OntAuthorizationStatus.authorized
