"""Tests for strict OLT write readback verification."""

from __future__ import annotations

from types import SimpleNamespace

from app.models.network import OLTDevice


def _olt() -> OLTDevice:
    return OLTDevice(name="Recon OLT", vendor="Huawei", model="MA5608T")


class TestVerifyOntAuthorized:
    def test_uses_direct_status_query_when_ont_id_is_known(self, monkeypatch) -> None:
        from app.services.network.olt_write_reconciliation import verify_ont_authorized

        monkeypatch.setattr(
            "app.services.network.olt_ssh_ont.get_ont_status",
            lambda *_args, **_kwargs: (
                True,
                "ok",
                SimpleNamespace(
                    serial_number="HWTC-ABCD1234",
                    run_state="online",
                    config_state="normal",
                    match_state="match",
                ),
            ),
        )

        result = verify_ont_authorized(
            _olt(),
            fsp="0/2/1",
            ont_id=7,
            serial_number="HWTC-ABCD1234",
        )

        assert result.success is True
        assert "Verified ONT" in result.message

    def test_falls_back_to_serial_lookup_when_direct_status_query_fails(
        self, monkeypatch
    ) -> None:
        from app.services.network.olt_write_reconciliation import verify_ont_authorized

        monkeypatch.setattr(
            "app.services.network.olt_ssh_ont.get_ont_status",
            lambda *_args, **_kwargs: (
                False,
                "OLT error: display ont info 0/165 % Parameter error",
                None,
            ),
        )
        monkeypatch.setattr(
            "app.services.network.olt_ssh_ont.find_ont_by_serial",
            lambda *_args, **_kwargs: (
                True,
                "found",
                SimpleNamespace(
                    fsp="0/1/6",
                    onu_id=5,
                    real_serial="4857544328201B9A",
                    run_state="online",
                ),
            ),
        )

        result = verify_ont_authorized(
            _olt(),
            fsp="0/1/6",
            ont_id=5,
            serial_number="4857544328201B9A",
        )

        assert result.success is True
        assert result.details["ont_id"] == 5
        assert "serial readback" in result.message

    def test_falls_back_to_registered_serial_scan(self, monkeypatch) -> None:
        from app.services.network.olt_write_reconciliation import verify_ont_authorized

        monkeypatch.setattr(
            "app.services.network.olt_ssh_ont.get_registered_ont_serials",
            lambda *_args, **_kwargs: (
                True,
                "ok",
                [
                    SimpleNamespace(
                        fsp="0/2/1",
                        onu_id=9,
                        real_serial="HWTCABCD1234",
                        run_state="online",
                    )
                ],
            ),
        )

        result = verify_ont_authorized(
            _olt(),
            fsp="0/2/1",
            ont_id=None,
            serial_number="HWTC-ABCD1234",
        )

        assert result.success is True
        assert result.details["ont_id"] == 9


class TestVerifyOntAbsent:
    def test_detects_still_present_registration(self, monkeypatch) -> None:
        from app.services.network.olt_write_reconciliation import verify_ont_absent

        monkeypatch.setattr(
            "app.services.network.olt_ssh_ont.get_registered_ont_serials",
            lambda *_args, **_kwargs: (
                True,
                "ok",
                [
                    SimpleNamespace(
                        fsp="0/2/1",
                        onu_id=7,
                        real_serial="HWTCABCD1234",
                        run_state="offline",
                    )
                ],
            ),
        )

        result = verify_ont_absent(
            _olt(),
            fsp="0/2/1",
            ont_id=7,
            serial_number="HWTC-ABCD1234",
        )

        assert result.success is False
        assert "still appears" in result.message


class TestVerifyServicePortPresent:
    def test_detects_missing_vlan_after_write(self, monkeypatch) -> None:
        from app.services.network.olt_write_reconciliation import (
            verify_service_port_present,
        )

        monkeypatch.setattr(
            "app.services.network.olt_ssh_service_ports.get_service_ports_for_ont",
            lambda *_args, **_kwargs: (
                True,
                "ok",
                [
                    SimpleNamespace(
                        index=11, vlan_id=201, ont_id=7, gem_index=1, state="up"
                    )
                ],
            ),
        )

        result = verify_service_port_present(
            _olt(),
            fsp="0/2/1",
            ont_id=7,
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is False
        assert "not present" in result.message


class TestVerifyIphostConfig:
    def test_detects_vlan_mismatch(self, monkeypatch) -> None:
        from app.services.network.olt_write_reconciliation import verify_iphost_config

        monkeypatch.setattr(
            "app.services.network.olt_ssh_ont.get_ont_iphost_config",
            lambda *_args, **_kwargs: (
                True,
                "ok",
                {"VLAN": "450", "IP mode": "DHCP"},
            ),
        )

        result = verify_iphost_config(
            _olt(),
            fsp="0/2/1",
            ont_id=7,
            vlan_id=203,
            ip_mode="dhcp",
        )

        assert result.success is False
        assert "different VLAN" in result.message
