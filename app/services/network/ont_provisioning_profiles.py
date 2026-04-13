"""ONT provisioning profile catalog management services."""

from __future__ import annotations

import logging
import re

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.network import (
    ConfigMethod,
    IpProtocol,
    MgmtIpMode,
    OntProfileType,
    OntProfileWanService,
    OntProvisioningProfile,
    OnuMode,
    PppoePasswordMode,
    Vlan,
    VlanMode,
    WanConnectionType,
    WanServiceType,
)
from app.services.common import apply_ordering, coerce_uuid
from app.services.credential_crypto import encrypt_credential

logger = logging.getLogger(__name__)

# Allowed template variables for wifi_ssid_template and pppoe_username_template
TEMPLATE_VAR_PATTERN = re.compile(
    r"\{(subscriber_code|subscriber_name|serial_number|offer_name|ont_id_short)\}"
)
_ALLOWED_VARS = {
    "subscriber_code",
    "subscriber_name",
    "serial_number",
    "offer_name",
    "ont_id_short",
}


def validate_template_string(template: str, field_name: str) -> None:
    """Validate that a template string only contains allowed variables."""
    all_vars = re.findall(r"\{(\w+)\}", template)
    invalid = set(all_vars) - _ALLOWED_VARS
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid template variables in {field_name}: {invalid}. "
            f"Allowed: {_ALLOWED_VARS}",
        )


class OntProvisioningProfiles:
    """CRUD operations for ONT provisioning profile catalog."""

    @staticmethod
    def _validate_profile_vlan_scope(
        db: Session,
        *,
        olt_device_id: str | None,
        mgmt_vlan_tag: int | None = None,
        pppoe_omci_vlan: int | None = None,
        profile_id: str | None = None,
    ) -> None:
        tags = {
            "Management VLAN": mgmt_vlan_tag,
            "PPPoE OMCI VLAN": pppoe_omci_vlan,
        }
        tags = {label: tag for label, tag in tags.items() if tag is not None}
        service_tags: set[int] = set()
        if profile_id:
            services = WanServices.list_for_profile(db, profile_id)
            service_tags = {
                service.s_vlan for service in services if service.s_vlan is not None
            }
        if not tags and not service_tags:
            return
        if not olt_device_id:
            raise HTTPException(
                status_code=400,
                detail="Provisioning profile must be scoped to an OLT before VLANs can be assigned.",
            )

        olt_uuid = coerce_uuid(olt_device_id)
        tags_to_lookup = set(tags.values()) | service_tags
        available_tags = set(
            db.scalars(
                select(Vlan.tag).where(
                    Vlan.olt_device_id == olt_uuid,
                    Vlan.is_active.is_(True),
                    Vlan.tag.in_(tags_to_lookup),
                )
            ).all()
        )
        for label, tag in tags.items():
            if tag not in available_tags:
                raise HTTPException(
                    status_code=400,
                    detail=f"{label} {tag} is not defined on this profile's OLT.",
                )

        if service_tags:
            missing_service_vlans = sorted(
                service_tag
                for service_tag in service_tags
                if service_tag not in available_tags
            )
            if missing_service_vlans:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Existing WAN service VLANs are not defined on the selected OLT: "
                        + ", ".join(str(tag) for tag in missing_service_vlans)
                    ),
                )

    @staticmethod
    def _validate_authorization_profile_pair(
        *,
        authorization_line_profile_id: int | None,
        authorization_service_profile_id: int | None,
    ) -> None:
        if (authorization_line_profile_id is None) == (
            authorization_service_profile_id is None
        ):
            return
        raise HTTPException(
            status_code=400,
            detail="Both OLT authorization line and service profile IDs are required.",
        )

    @staticmethod
    def list(
        db: Session,
        *,
        owner_subscriber_id: str | None = None,
        profile_type: str | None = None,
        config_method: str | None = None,
        onu_mode: str | None = None,
        olt_device_id: str | None = None,
        include_global: bool = False,
        is_active: bool | None = None,
        search: str | None = None,
        order_by: str = "name",
        order_dir: str = "asc",
        limit: int = 200,
        offset: int = 0,
    ) -> list[OntProvisioningProfile]:
        """List provisioning profiles with optional filtering."""
        stmt = select(OntProvisioningProfile).options(
            selectinload(OntProvisioningProfile.wan_services),
            selectinload(OntProvisioningProfile.olt_device),
            selectinload(OntProvisioningProfile.download_speed_profile),
            selectinload(OntProvisioningProfile.upload_speed_profile),
        )
        if owner_subscriber_id:
            stmt = stmt.where(
                OntProvisioningProfile.owner_subscriber_id
                == coerce_uuid(owner_subscriber_id)
            )
        if olt_device_id:
            olt_uuid = coerce_uuid(olt_device_id)
            if include_global:
                stmt = stmt.where(
                    or_(
                        OntProvisioningProfile.olt_device_id == olt_uuid,
                        OntProvisioningProfile.olt_device_id.is_(None),
                    )
                )
            else:
                stmt = stmt.where(OntProvisioningProfile.olt_device_id == olt_uuid)
        if is_active is not None:
            stmt = stmt.where(OntProvisioningProfile.is_active.is_(is_active))
        if profile_type:
            try:
                pt = OntProfileType(profile_type)
                stmt = stmt.where(OntProvisioningProfile.profile_type == pt)
            except ValueError:
                logger.warning("Invalid profile_type filter: %s", profile_type)
        if config_method:
            try:
                cm = ConfigMethod(config_method)
                stmt = stmt.where(OntProvisioningProfile.config_method == cm)
            except ValueError:
                logger.warning("Invalid config_method filter: %s", config_method)
        if onu_mode:
            try:
                om = OnuMode(onu_mode)
                stmt = stmt.where(OntProvisioningProfile.onu_mode == om)
            except ValueError:
                logger.warning("Invalid onu_mode filter: %s", onu_mode)
        if search:
            stmt = stmt.where(OntProvisioningProfile.name.ilike(f"%{search}%"))

        allowed_columns = {
            "name": OntProvisioningProfile.name,
            "olt": OntProvisioningProfile.olt_device_id,
            "profile_type": OntProvisioningProfile.profile_type,
            "config_method": OntProvisioningProfile.config_method,
            "created_at": OntProvisioningProfile.created_at,
        }
        stmt = apply_ordering(stmt, order_by, order_dir, allowed_columns)
        stmt = stmt.limit(limit).offset(offset)
        return list(db.scalars(stmt).all())

    @staticmethod
    def get(db: Session, profile_id: str) -> OntProvisioningProfile:
        """Get a provisioning profile by ID or raise 404."""
        stmt = (
            select(OntProvisioningProfile)
            .options(
                selectinload(OntProvisioningProfile.wan_services),
                selectinload(OntProvisioningProfile.olt_device),
                selectinload(OntProvisioningProfile.download_speed_profile),
                selectinload(OntProvisioningProfile.upload_speed_profile),
            )
            .where(OntProvisioningProfile.id == coerce_uuid(profile_id))
        )
        profile = db.scalars(stmt).first()
        if not profile:
            raise HTTPException(
                status_code=404, detail="Provisioning profile not found"
            )
        return profile

    @staticmethod
    def create(
        db: Session,
        *,
        owner_subscriber_id: str,
        name: str,
        profile_type: OntProfileType = OntProfileType.residential,
        description: str | None = None,
        config_method: ConfigMethod | None = None,
        onu_mode: OnuMode | None = None,
        ip_protocol: IpProtocol | None = None,
        download_speed_profile_id: str | None = None,
        upload_speed_profile_id: str | None = None,
        olt_device_id: str | None = None,
        mgmt_ip_mode: MgmtIpMode | None = None,
        mgmt_vlan_tag: int | None = None,
        mgmt_remote_access: bool = False,
        wifi_enabled: bool = True,
        wifi_ssid_template: str | None = None,
        wifi_security_mode: str | None = None,
        wifi_channel: str | None = None,
        wifi_band: str | None = None,
        voip_enabled: bool = False,
        authorization_line_profile_id: int | None = None,
        authorization_service_profile_id: int | None = None,
        internet_config_ip_index: int | None = None,
        wan_config_profile_id: int | None = None,
        pppoe_omci_vlan: int | None = None,
        cr_username: str | None = None,
        cr_password: str | None = None,
        is_default: bool = False,
        notes: str | None = None,
    ) -> OntProvisioningProfile:
        """Create a new provisioning profile."""
        if wifi_ssid_template:
            validate_template_string(wifi_ssid_template, "wifi_ssid_template")
        OntProvisioningProfiles._validate_authorization_profile_pair(
            authorization_line_profile_id=authorization_line_profile_id,
            authorization_service_profile_id=authorization_service_profile_id,
        )
        OntProvisioningProfiles._validate_profile_vlan_scope(
            db,
            olt_device_id=olt_device_id,
            mgmt_vlan_tag=mgmt_vlan_tag,
            pppoe_omci_vlan=pppoe_omci_vlan,
        )

        profile = OntProvisioningProfile(
            owner_subscriber_id=coerce_uuid(owner_subscriber_id),
            name=name,
            profile_type=profile_type,
            description=description,
            config_method=config_method,
            onu_mode=onu_mode,
            ip_protocol=ip_protocol,
            download_speed_profile_id=coerce_uuid(download_speed_profile_id),
            upload_speed_profile_id=coerce_uuid(upload_speed_profile_id),
            olt_device_id=coerce_uuid(olt_device_id),
            mgmt_ip_mode=mgmt_ip_mode,
            mgmt_vlan_tag=mgmt_vlan_tag,
            mgmt_remote_access=mgmt_remote_access,
            wifi_enabled=wifi_enabled,
            wifi_ssid_template=wifi_ssid_template,
            wifi_security_mode=wifi_security_mode,
            wifi_channel=wifi_channel,
            wifi_band=wifi_band,
            voip_enabled=voip_enabled,
            authorization_line_profile_id=authorization_line_profile_id,
            authorization_service_profile_id=authorization_service_profile_id,
            internet_config_ip_index=internet_config_ip_index,
            wan_config_profile_id=wan_config_profile_id,
            pppoe_omci_vlan=pppoe_omci_vlan,
            cr_username=cr_username,
            cr_password=cr_password,
            is_default=is_default,
            notes=notes,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)
        logger.info("Created provisioning profile %s: %s", profile.id, profile.name)
        return profile

    @staticmethod
    def update(
        db: Session, profile_id: str, **kwargs: object
    ) -> OntProvisioningProfile:
        """Update an existing provisioning profile."""
        profile = db.get(OntProvisioningProfile, coerce_uuid(profile_id))
        if not profile:
            raise HTTPException(
                status_code=404, detail="Provisioning profile not found"
            )

        wifi_ssid = kwargs.get("wifi_ssid_template")
        if wifi_ssid and isinstance(wifi_ssid, str):
            validate_template_string(wifi_ssid, "wifi_ssid_template")
        merged_line_profile_id = kwargs.get(
            "authorization_line_profile_id", profile.authorization_line_profile_id
        )
        merged_service_profile_id = kwargs.get(
            "authorization_service_profile_id",
            profile.authorization_service_profile_id,
        )
        OntProvisioningProfiles._validate_authorization_profile_pair(
            authorization_line_profile_id=(
                int(str(merged_line_profile_id))
                if merged_line_profile_id is not None
                else None
            ),
            authorization_service_profile_id=(
                int(str(merged_service_profile_id))
                if merged_service_profile_id is not None
                else None
            ),
        )
        merged_olt_device_id = kwargs.get("olt_device_id", profile.olt_device_id)
        OntProvisioningProfiles._validate_profile_vlan_scope(
            db,
            olt_device_id=str(merged_olt_device_id) if merged_olt_device_id else None,
            mgmt_vlan_tag=(
                int(str(kwargs.get("mgmt_vlan_tag", profile.mgmt_vlan_tag)))
                if kwargs.get("mgmt_vlan_tag", profile.mgmt_vlan_tag) is not None
                else None
            ),
            pppoe_omci_vlan=(
                int(str(kwargs.get("pppoe_omci_vlan", profile.pppoe_omci_vlan)))
                if kwargs.get("pppoe_omci_vlan", profile.pppoe_omci_vlan) is not None
                else None
            ),
            profile_id=profile_id,
        )

        for key, value in kwargs.items():
            if hasattr(profile, key):
                setattr(profile, key, value)
        db.commit()
        db.refresh(profile)
        logger.info("Updated provisioning profile %s: %s", profile.id, profile.name)
        return profile

    @staticmethod
    def delete(db: Session, profile_id: str) -> None:
        """Soft-delete a provisioning profile by setting is_active=False."""
        profile = db.get(OntProvisioningProfile, coerce_uuid(profile_id))
        if not profile:
            raise HTTPException(
                status_code=404, detail="Provisioning profile not found"
            )
        profile.is_active = False
        db.commit()
        logger.info("Soft-deleted provisioning profile %s", profile_id)

    @staticmethod
    def count(db: Session, *, is_active: bool | None = None) -> int:
        """Count provisioning profiles."""
        stmt = select(func.count()).select_from(OntProvisioningProfile)
        if is_active is not None:
            stmt = stmt.where(OntProvisioningProfile.is_active.is_(is_active))
        return db.scalar(stmt) or 0


class WanServices:
    """CRUD operations for WAN services within a provisioning profile."""

    @staticmethod
    def _validate_vlan_scope(
        db: Session,
        *,
        profile_id: str,
        s_vlan: int | None,
    ) -> None:
        if s_vlan is None:
            return
        profile = db.get(OntProvisioningProfile, coerce_uuid(profile_id))
        if not profile:
            raise HTTPException(
                status_code=404, detail="Provisioning profile not found"
            )
        if not profile.olt_device_id:
            raise HTTPException(
                status_code=400,
                detail="Provisioning profile must be scoped to an OLT before VLAN services can be added.",
            )
        vlan = db.scalars(
            select(Vlan).where(
                Vlan.olt_device_id == profile.olt_device_id,
                Vlan.tag == int(s_vlan),
                Vlan.is_active.is_(True),
            )
        ).first()
        if not vlan:
            raise HTTPException(
                status_code=400,
                detail=f"VLAN {s_vlan} is not defined on this profile's OLT.",
            )

    @staticmethod
    def list_for_profile(db: Session, profile_id: str) -> list[OntProfileWanService]:
        """List all WAN services for a profile."""
        stmt = (
            select(OntProfileWanService)
            .where(OntProfileWanService.profile_id == coerce_uuid(profile_id))
            .order_by(OntProfileWanService.priority)
        )
        return list(db.scalars(stmt).all())

    @staticmethod
    def get(db: Session, service_id: str) -> OntProfileWanService:
        """Get a WAN service by ID or raise 404."""
        service = db.get(OntProfileWanService, coerce_uuid(service_id))
        if not service:
            raise HTTPException(status_code=404, detail="WAN service not found")
        return service

    @staticmethod
    def create(
        db: Session,
        *,
        profile_id: str,
        service_type: WanServiceType = WanServiceType.internet,
        name: str | None = None,
        priority: int = 1,
        vlan_mode: VlanMode = VlanMode.tagged,
        s_vlan: int | None = None,
        c_vlan: int | None = None,
        cos_priority: int | None = None,
        mtu: int = 1500,
        connection_type: WanConnectionType = WanConnectionType.pppoe,
        nat_enabled: bool = True,
        ip_mode: IpProtocol | None = None,
        pppoe_username_template: str | None = None,
        pppoe_password_mode: PppoePasswordMode | None = None,
        pppoe_static_password: str | None = None,
        static_ip_source: str | None = None,
        bind_lan_ports: list[int] | None = None,
        bind_ssid_index: int | None = None,
        gem_port_id: int | None = None,
        t_cont_profile: str | None = None,
        notes: str | None = None,
    ) -> OntProfileWanService:
        """Create a new WAN service for a profile."""
        if pppoe_username_template:
            validate_template_string(pppoe_username_template, "pppoe_username_template")
        WanServices._validate_vlan_scope(db, profile_id=profile_id, s_vlan=s_vlan)

        # Encrypt static PPPoE password if provided
        encrypted_password = None
        if pppoe_static_password:
            encrypted_password = encrypt_credential(pppoe_static_password)

        service = OntProfileWanService(
            profile_id=coerce_uuid(profile_id),
            service_type=service_type,
            name=name,
            priority=priority,
            vlan_mode=vlan_mode,
            s_vlan=s_vlan,
            c_vlan=c_vlan,
            cos_priority=cos_priority,
            mtu=mtu,
            connection_type=connection_type,
            nat_enabled=nat_enabled,
            ip_mode=ip_mode,
            pppoe_username_template=pppoe_username_template,
            pppoe_password_mode=pppoe_password_mode,
            pppoe_static_password=encrypted_password,
            static_ip_source=static_ip_source,
            bind_lan_ports=bind_lan_ports,
            bind_ssid_index=bind_ssid_index,
            gem_port_id=gem_port_id,
            t_cont_profile=t_cont_profile,
            notes=notes,
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        logger.info("Created WAN service %s for profile %s", service.id, profile_id)
        return service

    @staticmethod
    def update(db: Session, service_id: str, **kwargs: object) -> OntProfileWanService:
        """Update an existing WAN service."""
        service = db.get(OntProfileWanService, coerce_uuid(service_id))
        if not service:
            raise HTTPException(status_code=404, detail="WAN service not found")

        pppoe_tmpl = kwargs.get("pppoe_username_template")
        if pppoe_tmpl and isinstance(pppoe_tmpl, str):
            validate_template_string(pppoe_tmpl, "pppoe_username_template")
        profile_id = str(service.profile_id)
        s_vlan = kwargs.get("s_vlan", service.s_vlan)
        WanServices._validate_vlan_scope(
            db,
            profile_id=profile_id,
            s_vlan=int(str(s_vlan)) if s_vlan is not None else None,
        )

        # Encrypt static password if being updated
        if "pppoe_static_password" in kwargs and kwargs["pppoe_static_password"]:
            kwargs["pppoe_static_password"] = encrypt_credential(
                str(kwargs["pppoe_static_password"])
            )

        for key, value in kwargs.items():
            if hasattr(service, key):
                setattr(service, key, value)
        db.commit()
        db.refresh(service)
        logger.info("Updated WAN service %s", service.id)
        return service

    @staticmethod
    def delete(db: Session, service_id: str) -> None:
        """Hard-delete a WAN service (cascade from profile handles soft)."""
        service = db.get(OntProfileWanService, coerce_uuid(service_id))
        if not service:
            raise HTTPException(status_code=404, detail="WAN service not found")
        db.delete(service)
        db.commit()
        logger.info("Deleted WAN service %s", service_id)


ont_provisioning_profiles = OntProvisioningProfiles()
wan_services = WanServices()
