"""IP management services."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import (
    IPAssignment,
    IpBlock,
    IpPool,
    IPv4Address,
    IPv6Address,
    IPVersion,
)
from app.schemas.network import (
    IPAssignmentCreate,
    IPAssignmentUpdate,
    IpBlockUpdate,
    IpPoolCreate,
    IpPoolUpdate,
    IPv4AddressUpdate,
    IPv6AddressUpdate,
)
from app.services import settings_spec
from app.services.common import coerce_uuid
from app.services.crud import CRUDManager
from app.services.network._common import (
    _apply_ordering,
    _apply_pagination,
    _validate_enum,
)
from app.services.query_builders import apply_active_state, apply_optional_equals
from app.validators import network as network_validators


class IPAssignments(CRUDManager[IPAssignment]):
    model = IPAssignment
    not_found_detail = "IP assignment not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(db: Session, payload: IPAssignmentCreate):
        network_validators.validate_ip_assignment_links(
            db,
            str(payload.subscriber_id),
            str(payload.subscription_id) if payload.subscription_id else None,
            str(payload.subscription_add_on_id) if payload.subscription_add_on_id else None,
            str(payload.service_address_id) if payload.service_address_id else None,
        )
        assignment = IPAssignment(**payload.model_dump())
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        return assignment

    @classmethod
    def get(cls, db: Session, assignment_id: str):
        return super().get(db, assignment_id)

    @staticmethod
    def list(
        db: Session,
        subscriber_id: str | None,
        subscription_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IPAssignment)
        query = apply_optional_equals(
            query,
            {
                IPAssignment.subscriber_id: subscriber_id,
                IPAssignment.subscription_id: subscription_id,
            },
        )
        query = apply_active_state(query, IPAssignment.is_active, is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IPAssignment.created_at, "ipv4_address_id": IPAssignment.ipv4_address_id},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, assignment_id: str, payload: IPAssignmentUpdate):
        assignment = db.get(IPAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="IP assignment not found")
        data = payload.model_dump(exclude_unset=True)
        subscriber_id = str(data.get("subscriber_id", assignment.subscriber_id))
        subscription_id = data.get("subscription_id", assignment.subscription_id)
        subscription_add_on_id = data.get(
            "subscription_add_on_id", assignment.subscription_add_on_id
        )
        service_address_id = data.get(
            "service_address_id", assignment.service_address_id
        )
        network_validators.validate_ip_assignment_links(
            db,
            subscriber_id,
            str(subscription_id) if subscription_id else None,
            str(subscription_add_on_id) if subscription_add_on_id else None,
            str(service_address_id) if service_address_id else None,
        )
        for key, value in data.items():
            setattr(assignment, key, value)
        db.commit()
        db.refresh(assignment)
        return assignment

    @classmethod
    def delete(cls, db: Session, assignment_id: str):
        return super().delete(db, assignment_id)


class IpPools(CRUDManager[IpPool]):
    model = IpPool
    not_found_detail = "IP pool not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(db: Session, payload: IpPoolCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "ip_version" not in fields_set:
            default_version = settings_spec.resolve_value(
                db, SettingDomain.network, "default_ip_version"
            )
            if default_version:
                data["ip_version"] = _validate_enum(
                    default_version, IPVersion, "ip_version"
                )
        pool = IpPool(**data)
        db.add(pool)
        db.commit()
        db.refresh(pool)
        return pool

    @classmethod
    def get(cls, db: Session, pool_id: str):
        return cls._get_or_404(db, coerce_uuid(pool_id))

    @staticmethod
    def list(
        db: Session,
        ip_version: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IpPool)
        query = apply_optional_equals(query, {IpPool.ip_version: ip_version})
        query = apply_active_state(query, IpPool.is_active, is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IpPool.created_at, "name": IpPool.name},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def update(cls, db: Session, pool_id: str, payload: IpPoolUpdate):
        return super().update(db, pool_id, payload)

    @classmethod
    def delete(cls, db: Session, pool_id: str):
        return super().delete(db, pool_id)


class IpBlocks(CRUDManager[IpBlock]):
    model = IpBlock
    not_found_detail = "IP block not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def list(
        db: Session,
        pool_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IpBlock)
        query = apply_optional_equals(query, {IpBlock.pool_id: pool_id})
        query = apply_active_state(query, IpBlock.is_active, is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IpBlock.created_at, "cidr": IpBlock.cidr},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, block_id: str):
        return super().get(db, block_id)

    @classmethod
    def update(cls, db: Session, block_id: str, payload: IpBlockUpdate):
        return super().update(db, block_id, payload)

    @classmethod
    def delete(cls, db: Session, block_id: str):
        return super().delete(db, block_id)


class IPv4Addresses(CRUDManager[IPv4Address]):
    model = IPv4Address
    not_found_detail = "IPv4 address not found"

    @staticmethod
    def list(
        db: Session,
        pool_id: str | None,
        is_reserved: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IPv4Address)
        query = apply_optional_equals(
            query,
            {
                IPv4Address.pool_id: pool_id,
                IPv4Address.is_reserved: is_reserved,
            },
        )
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IPv4Address.created_at, "address": IPv4Address.address},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, address_id: str):
        return super().get(db, address_id)

    @classmethod
    def update(cls, db: Session, address_id: str, payload: IPv4AddressUpdate):
        return super().update(db, address_id, payload)

    @classmethod
    def delete(cls, db: Session, address_id: str):
        return super().delete(db, address_id)


class IPv6Addresses(CRUDManager[IPv6Address]):
    model = IPv6Address
    not_found_detail = "IPv6 address not found"

    @staticmethod
    def list(
        db: Session,
        pool_id: str | None,
        is_reserved: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IPv6Address)
        query = apply_optional_equals(
            query,
            {
                IPv6Address.pool_id: pool_id,
                IPv6Address.is_reserved: is_reserved,
            },
        )
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IPv6Address.created_at, "address": IPv6Address.address},
        )
        return _apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, address_id: str):
        return super().get(db, address_id)

    @classmethod
    def update(cls, db: Session, address_id: str, payload: IPv6AddressUpdate):
        return super().update(db, address_id, payload)

    @classmethod
    def delete(cls, db: Session, address_id: str):
        return super().delete(db, address_id)


ip_assignments = IPAssignments()
ip_pools = IpPools()
ip_blocks = IpBlocks()
ipv4_addresses = IPv4Addresses()
ipv6_addresses = IPv6Addresses()
