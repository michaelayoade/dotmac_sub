from types import SimpleNamespace

import pytest

from app.models.network import OntProvisioningStatus
from app.services.network.ont_status import set_provisioning_status


def test_set_provisioning_status_allows_pending_transitions() -> None:
    ont = SimpleNamespace(
        id="ont-pending-ok",
        provisioning_status=OntProvisioningStatus.partial,
    )

    set_provisioning_status(ont, OntProvisioningStatus.pending_acs_registration)
    assert ont.provisioning_status == OntProvisioningStatus.pending_acs_registration

    set_provisioning_status(ont, "pending_service_config")
    assert ont.provisioning_status == OntProvisioningStatus.pending_service_config

    set_provisioning_status(ont, OntProvisioningStatus.provisioned)
    assert ont.provisioning_status == OntProvisioningStatus.provisioned


def test_set_provisioning_status_rejects_illegal_pending_regression() -> None:
    ont = SimpleNamespace(
        id="ont-pending-illegal",
        provisioning_status=OntProvisioningStatus.provisioned,
    )

    with pytest.raises(
        ValueError,
        match="Illegal ONT provisioning status transition: provisioned -> pending_acs_registration",
    ):
        set_provisioning_status(ont, OntProvisioningStatus.pending_acs_registration)
