"""Application-facing adapter for IPAM resource scoping and OLT assignments."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session


class IpamAdapter:
    """Keep OLT/UI flows from depending directly on IPAM query details."""

    def olt_scope_context(self, db: Session, *, olt: object) -> dict[str, object]:
        from app.models.network import IpPool, Vlan
        from app.services.network.olt_web_resources import ip_pool_usage_summary

        olt_id = getattr(olt, "id", None)
        olt_vlans = list(
            db.scalars(
                select(Vlan).where(Vlan.olt_device_id == olt_id).order_by(Vlan.tag.asc())
            ).all()
        )
        olt_ip_pools = list(
            db.scalars(
                select(IpPool)
                .where(IpPool.olt_device_id == olt_id)
                .order_by(IpPool.name.asc())
            ).all()
        )
        available_vlans = self.available_vlans_for_olt(db, olt_id=str(olt_id))
        available_ip_pools = list(
            db.scalars(
                select(IpPool)
                .outerjoin(Vlan, IpPool.vlan_id == Vlan.id)
                .where(IpPool.olt_device_id.is_(None))
                .where(IpPool.is_active.is_(True))
                .where(or_(IpPool.vlan_id.is_(None), Vlan.olt_device_id == olt_id))
                .order_by(IpPool.name.asc())
            ).all()
        )
        return {
            "olt_vlans": olt_vlans,
            "olt_ip_pools": olt_ip_pools,
            "olt_ip_pool_usage": ip_pool_usage_summary(db, olt_ip_pools),
            "available_vlans": available_vlans,
            "available_ip_pools": available_ip_pools,
        }

    def available_vlans_for_olt(self, db: Session, olt_id: str) -> list[object]:
        from app.services.network.olt_web_resources import available_vlans_for_olt

        return available_vlans_for_olt(db, olt_id)

    def available_ip_pools_for_olt(self, db: Session, olt_id: str) -> list[object]:
        from app.services.network.olt_web_resources import available_ip_pools_for_olt

        return available_ip_pools_for_olt(db, olt_id)

    def assign_vlan_to_olt(
        self, db: Session, olt_id: str, vlan_id: str
    ) -> tuple[bool, str]:
        from app.services.network.olt_web_resources import assign_vlan_to_olt

        return assign_vlan_to_olt(db, olt_id, vlan_id)

    def unassign_vlan_from_olt(
        self, db: Session, olt_id: str, vlan_id: str
    ) -> tuple[bool, str]:
        from app.services.network.olt_web_resources import unassign_vlan_from_olt

        return unassign_vlan_from_olt(db, olt_id, vlan_id)

    def assign_ip_pool_to_olt(
        self,
        db: Session,
        olt_id: str,
        pool_id: str,
        vlan_id: str | None = None,
    ) -> tuple[bool, str]:
        from app.services.network.olt_web_resources import assign_ip_pool_to_olt

        return assign_ip_pool_to_olt(db, olt_id, pool_id, vlan_id)

    def unassign_ip_pool_from_olt(
        self, db: Session, olt_id: str, pool_id: str
    ) -> tuple[bool, str]:
        from app.services.network.olt_web_resources import unassign_ip_pool_from_olt

        return unassign_ip_pool_from_olt(db, olt_id, pool_id)


ipam_adapter = IpamAdapter()
