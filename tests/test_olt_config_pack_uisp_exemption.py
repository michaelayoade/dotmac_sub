"""UISP-managed OLTs are exempt from the TR069 config-pack requirement.

Ubiquiti UF-OLTs run on the UISP control plane, so ``validate_config_pack_required``
must not demand a TR069 config pack for them (mirrors the
``ck_olt_devices_config_pack_required`` DB constraint).
"""

from __future__ import annotations

from app.models.network import OLTDevice
from app.services.network.olt_config_pack import validate_config_pack_required


def test_uisp_olt_exempt_without_config_pack():
    olt = OLTDevice(
        name="GPON-GUDU-1",
        uisp_device_id="uf-olt-abc123",
        config_pack=None,
        tr069_acs_server_id=None,
        mgmt_ip_pool_id=None,
    )
    assert validate_config_pack_required(olt, raise_on_error=False) == []


def test_tr069_olt_still_requires_config_pack():
    olt = OLTDevice(
        name="Gudu Huawei OLT",
        uisp_device_id=None,
        config_pack=None,
        tr069_acs_server_id=None,
        mgmt_ip_pool_id=None,
    )
    assert validate_config_pack_required(olt, raise_on_error=False) != []
