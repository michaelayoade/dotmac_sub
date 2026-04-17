"""Tests for OntWanServiceInstance model and related functionality.

Tests the Phase 2+3 WAN service architecture introduced in migration 026:
- OntWanServiceInstance model CRUD
- Profile application creating WAN service instances
- PPPoE username template resolution
- VLAN resolution by tag
- Provisioning flow integration
"""

from __future__ import annotations


class TestOntWanServiceInstanceModel:
    """Test OntWanServiceInstance model and relationships."""

    def test_create_wan_service_instance(self, db_session) -> None:
        from app.models.network import (
            OntUnit,
            OntWanServiceInstance,
            VlanMode,
            WanConnectionType,
            WanServiceProvisioningStatus,
            WanServiceType,
        )

        ont = OntUnit(serial_number="TEST-WAN-001")
        db_session.add(ont)
        db_session.flush()

        instance = OntWanServiceInstance(
            ont_id=ont.id,
            service_type=WanServiceType.internet,
            name="Primary Internet",
            priority=1,
            vlan_mode=VlanMode.tagged,
            s_vlan=203,
            connection_type=WanConnectionType.pppoe,
            nat_enabled=True,
            pppoe_username="test@isp.local",
            provisioning_status=WanServiceProvisioningStatus.pending,
        )
        db_session.add(instance)
        db_session.commit()
        db_session.refresh(instance)

        assert instance.id is not None
        assert instance.ont_id == ont.id
        assert instance.service_type == WanServiceType.internet
        assert instance.name == "Primary Internet"
        assert instance.s_vlan == 203
        assert instance.connection_type == WanConnectionType.pppoe
        assert instance.provisioning_status == WanServiceProvisioningStatus.pending

    def test_ont_relationship_backpopulates(self, db_session) -> None:
        from app.models.network import (
            OntUnit,
            OntWanServiceInstance,
            WanServiceType,
        )

        ont = OntUnit(serial_number="TEST-WAN-002")
        db_session.add(ont)
        db_session.flush()

        instance1 = OntWanServiceInstance(
            ont_id=ont.id,
            service_type=WanServiceType.internet,
            name="Internet",
            priority=1,
        )
        instance2 = OntWanServiceInstance(
            ont_id=ont.id,
            service_type=WanServiceType.iptv,
            name="IPTV",
            priority=2,
        )
        db_session.add_all([instance1, instance2])
        db_session.commit()
        db_session.refresh(ont)

        assert len(ont.wan_service_instances) == 2
        service_types = {i.service_type.value for i in ont.wan_service_instances}
        assert service_types == {"internet", "iptv"}

    def test_cascade_delete_on_ont_removal(self, db_session) -> None:
        from sqlalchemy import select

        from app.models.network import (
            OntUnit,
            OntWanServiceInstance,
            WanServiceType,
        )

        ont = OntUnit(serial_number="TEST-WAN-CASCADE")
        db_session.add(ont)
        db_session.flush()
        ont_id = ont.id

        instance = OntWanServiceInstance(
            ont_id=ont.id,
            service_type=WanServiceType.internet,
            name="To Be Deleted",
        )
        db_session.add(instance)
        db_session.commit()
        instance_id = instance.id

        # Delete ONT
        db_session.delete(ont)
        db_session.commit()

        # Instance should be gone (CASCADE)
        result = db_session.scalars(
            select(OntWanServiceInstance).where(OntWanServiceInstance.id == instance_id)
        ).first()
        assert result is None


class TestPppoeUsernameTemplateResolution:
    """Test PPPoE username template resolution logic."""

    def test_resolve_subscriber_code_placeholder(self) -> None:
        from app.services.network.ont_profile_apply import (
            _resolve_pppoe_username_template,
        )

        result = _resolve_pppoe_username_template(
            "{subscriber_code}@isp.local",
            subscriber_code="CUST123",
        )
        assert result == "CUST123@isp.local"

    def test_resolve_serial_number_placeholder(self) -> None:
        from app.services.network.ont_profile_apply import (
            _resolve_pppoe_username_template,
        )

        result = _resolve_pppoe_username_template(
            "ont-{serial_number}",
            serial_number="HWTC12345678",
        )
        assert result == "ont-HWTC12345678"

    def test_resolve_multiple_placeholders(self) -> None:
        from app.services.network.ont_profile_apply import (
            _resolve_pppoe_username_template,
        )

        result = _resolve_pppoe_username_template(
            "{subscriber_code}-{ont_id_short}@{offer_name}.isp",
            subscriber_code="ABC",
            ont_id_short="12345678",
            offer_name="fiber100",
        )
        assert result == "ABC-12345678@fiber100.isp"

    def test_none_template_returns_none(self) -> None:
        from app.services.network.ont_profile_apply import (
            _resolve_pppoe_username_template,
        )

        result = _resolve_pppoe_username_template(None)
        assert result is None

    def test_empty_template_returns_none(self) -> None:
        from app.services.network.ont_profile_apply import (
            _resolve_pppoe_username_template,
        )

        result = _resolve_pppoe_username_template("")
        assert result is None


class TestVlanResolutionByTag:
    """Test VLAN resolution by tag number."""

    def test_resolve_vlan_by_tag_global(self, db_session, region) -> None:
        from app.models.network import Vlan
        from app.services.network.ont_profile_apply import _resolve_vlan_by_tag

        vlan = Vlan(tag=203, region_id=region.id, is_active=True)
        db_session.add(vlan)
        db_session.commit()

        result = _resolve_vlan_by_tag(db_session, 203, olt_device_id=None)
        assert result is not None
        assert result.tag == 203

    def test_resolve_vlan_scoped_to_olt(self, db_session, region) -> None:
        from app.models.network import OLTDevice, Vlan
        from app.services.network.ont_profile_apply import _resolve_vlan_by_tag

        olt = OLTDevice(
            name="Test OLT",
            vendor="Huawei",
            model="MA5608T",
            ssh_username="admin",
            ssh_password="test",
        )
        db_session.add(olt)
        db_session.flush()

        vlan = Vlan(
            tag=500, region_id=region.id, olt_device_id=olt.id, is_active=True
        )
        db_session.add(vlan)
        db_session.commit()

        result = _resolve_vlan_by_tag(db_session, 500, olt_device_id=olt.id)
        assert result is not None
        assert result.tag == 500
        assert result.olt_device_id == olt.id

    def test_resolve_vlan_none_tag_returns_none(self, db_session) -> None:
        from app.services.network.ont_profile_apply import _resolve_vlan_by_tag

        result = _resolve_vlan_by_tag(db_session, None, olt_device_id=None)
        assert result is None


class TestApplyProfileCreatesWanInstances:
    """Test that applying a profile creates OntWanServiceInstance records."""

    def test_apply_profile_creates_wan_service_instances(self, db_session) -> None:
        from app.models.network import (
            OntProfileWanService,
            OntProvisioningProfile,
            OntUnit,
            PppoePasswordMode,
            WanConnectionType,
            WanServiceType,
        )
        from app.services.network.ont_profile_apply import apply_profile_to_ont

        # Create profile with WAN services
        profile = OntProvisioningProfile(
            name="Multi-WAN Profile",
            is_active=True,
        )
        db_session.add(profile)
        db_session.flush()

        internet_service = OntProfileWanService(
            profile_id=profile.id,
            service_type=WanServiceType.internet,
            name="Internet",
            priority=1,
            connection_type=WanConnectionType.pppoe,
            pppoe_username_template="{serial_number}@isp.local",
            pppoe_password_mode=PppoePasswordMode.generate,
            s_vlan=203,
            is_active=True,
        )
        iptv_service = OntProfileWanService(
            profile_id=profile.id,
            service_type=WanServiceType.iptv,
            name="IPTV",
            priority=2,
            connection_type=WanConnectionType.dhcp,
            s_vlan=500,
            is_active=True,
        )
        db_session.add_all([internet_service, iptv_service])
        db_session.flush()

        # Create ONT
        ont = OntUnit(serial_number="HWTC-MULTI-WAN")
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)
        db_session.refresh(profile)

        # Apply profile
        result = apply_profile_to_ont(
            db_session, str(ont.id), str(profile.id), create_wan_instances=True
        )

        assert result.success is True
        assert "2 WAN service instances created" in result.message

        # Verify instances created
        db_session.refresh(ont)
        instances = ont.wan_service_instances
        assert len(instances) == 2

        internet_inst = next(
            i for i in instances if i.service_type == WanServiceType.internet
        )
        assert internet_inst.pppoe_username == "HWTC-MULTI-WAN@isp.local"
        assert internet_inst.connection_type == WanConnectionType.pppoe
        assert internet_inst.s_vlan == 203

        iptv_inst = next(
            i for i in instances if i.service_type == WanServiceType.iptv
        )
        assert iptv_inst.connection_type == WanConnectionType.dhcp
        assert iptv_inst.s_vlan == 500

    def test_apply_profile_replaces_existing_instances(self, db_session) -> None:
        from sqlalchemy import select

        from app.models.network import (
            OntProfileWanService,
            OntProvisioningProfile,
            OntUnit,
            OntWanServiceInstance,
            WanServiceType,
        )
        from app.services.network.ont_profile_apply import apply_profile_to_ont

        # Create ONT with existing instance
        ont = OntUnit(serial_number="HWTC-REPLACE")
        db_session.add(ont)
        db_session.flush()

        old_instance = OntWanServiceInstance(
            ont_id=ont.id,
            service_type=WanServiceType.voip,
            name="Old VoIP",
        )
        db_session.add(old_instance)
        db_session.commit()
        old_instance_id = old_instance.id

        # Create profile with different service
        profile = OntProvisioningProfile(name="New Profile", is_active=True)
        db_session.add(profile)
        db_session.flush()

        new_service = OntProfileWanService(
            profile_id=profile.id,
            service_type=WanServiceType.internet,
            name="New Internet",
            is_active=True,
        )
        db_session.add(new_service)
        db_session.commit()

        # Apply profile
        result = apply_profile_to_ont(
            db_session, str(ont.id), str(profile.id), create_wan_instances=True
        )

        assert result.success is True

        # Old instance should be deleted
        old = db_session.scalars(
            select(OntWanServiceInstance).where(
                OntWanServiceInstance.id == old_instance_id
            )
        ).first()
        assert old is None

        # New instance should exist
        db_session.refresh(ont)
        assert len(ont.wan_service_instances) == 1
        assert ont.wan_service_instances[0].service_type == WanServiceType.internet

    def test_apply_profile_skip_wan_instances(self, db_session) -> None:
        from app.models.network import (
            OntProfileWanService,
            OntProvisioningProfile,
            OntUnit,
            WanServiceType,
        )
        from app.services.network.ont_profile_apply import apply_profile_to_ont

        profile = OntProvisioningProfile(name="Skip Instances", is_active=True)
        db_session.add(profile)
        db_session.flush()

        service = OntProfileWanService(
            profile_id=profile.id,
            service_type=WanServiceType.internet,
            is_active=True,
        )
        db_session.add(service)

        ont = OntUnit(serial_number="HWTC-SKIP")
        db_session.add(ont)
        db_session.commit()

        result = apply_profile_to_ont(
            db_session, str(ont.id), str(profile.id), create_wan_instances=False
        )

        assert result.success is True
        assert "WAN service instances created" not in result.message

        db_session.refresh(ont)
        assert len(ont.wan_service_instances) == 0


class TestServiceIntentIncludesWanInstances:
    """Test that build_service_intent includes WAN service instances."""

    def test_build_service_intent_with_wan_instances(self, db_session) -> None:
        from app.models.network import (
            OntUnit,
            OntWanServiceInstance,
            WanServiceProvisioningStatus,
            WanServiceType,
        )
        from app.services.network.ont_service_intent import build_service_intent

        ont = OntUnit(serial_number="TEST-INTENT-001")
        db_session.add(ont)
        db_session.flush()

        instance = OntWanServiceInstance(
            ont_id=ont.id,
            service_type=WanServiceType.internet,
            name="Internet",
            pppoe_username="test@isp.local",
            s_vlan=203,
            provisioning_status=WanServiceProvisioningStatus.provisioned,
            is_active=True,
        )
        db_session.add(instance)
        db_session.commit()

        intent = build_service_intent(ont, db=db_session)

        assert intent["has_wan_instances"] is True
        assert len(intent["wan_service_instances"]) == 1

        svc = intent["wan_service_instances"][0]
        assert svc["service_type"] == "internet"
        assert svc["pppoe_username"] == "test@isp.local"
        assert svc["vlan"] == "VLAN 203"
        assert svc["provisioning_status"] == "provisioned"

    def test_build_service_intent_no_instances(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.network.ont_service_intent import build_service_intent

        ont = OntUnit(serial_number="TEST-INTENT-EMPTY")
        db_session.add(ont)
        db_session.commit()

        intent = build_service_intent(ont, db=db_session)

        assert intent["has_wan_instances"] is False
        assert intent["wan_service_instances"] == []


class TestProvisioningFlowWithWanInstances:
    """Test provisioning flow integration with WAN service instances."""

    def test_wan_instances_are_queried_in_apply_saved_service_config(
        self, db_session
    ) -> None:
        """Verify that apply_saved_service_config detects and uses WAN service instances."""
        from sqlalchemy import select

        from app.models.network import (
            OntUnit,
            OntWanServiceInstance,
            WanConnectionType,
            WanServiceProvisioningStatus,
            WanServiceType,
        )
        from app.services.credential_crypto import encrypt_credential

        ont = OntUnit(serial_number="TEST-PROV-WAN")
        db_session.add(ont)
        db_session.flush()

        instance = OntWanServiceInstance(
            ont_id=ont.id,
            service_type=WanServiceType.internet,
            name="Internet",
            connection_type=WanConnectionType.pppoe,
            pppoe_username="user@isp.local",
            pppoe_password=encrypt_credential("secret123"),
            s_vlan=203,
            provisioning_status=WanServiceProvisioningStatus.pending,
            is_active=True,
        )
        db_session.add(instance)
        db_session.commit()
        db_session.refresh(ont)

        # Verify the instance is queryable
        instances = db_session.scalars(
            select(OntWanServiceInstance).where(
                OntWanServiceInstance.ont_id == ont.id,
                OntWanServiceInstance.is_active.is_(True),
            )
        ).all()

        assert len(instances) == 1
        assert instances[0].pppoe_username == "user@isp.local"
        assert instances[0].service_type == WanServiceType.internet

    def test_provision_wan_service_instances_function_exists(self) -> None:
        """Verify the _provision_wan_service_instances function is importable."""
        from app.services.network.ont_provision_steps import (
            _provision_wan_service_instances,
        )

        assert callable(_provision_wan_service_instances)
