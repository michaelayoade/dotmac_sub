"""IP management services."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import (
    IPAssignment,
    IPVersion,
    IpBlock,
    IpPool,
    IPv4Address,
    IPv6Address,
)
from app.schemas.network import (
    IPAssignmentCreate,
    IPAssignmentUpdate,
    IpBlockCreate,
    IpBlockUpdate,
    IpPoolCreate,
    IpPoolUpdate,
    IPv4AddressCreate,
    IPv4AddressUpdate,
    IPv6AddressCreate,
    IPv6AddressUpdate,
)
from app.services import settings_spec
from app.services.common import coerce_uuid
from app.services.network._common import (
    _apply_ordering,
    _apply_pagination,
    _validate_enum,
)
from app.services.response import ListResponseMixin
from app.validators import network as network_validators


class IPAssignments(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: IPAssignmentCreate):
        network_validators.validate_ip_assignment_links(
            db,
            str(payload.account_id),
            str(payload.subscription_id) if payload.subscription_id else None,
            str(payload.subscription_add_on_id) if payload.subscription_add_on_id else None,
            str(payload.service_address_id) if payload.service_address_id else None,
        )
        assignment = IPAssignment(**payload.model_dump())
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        return assignment

    @staticmethod
    def get(db: Session, assignment_id: str):
        assignment = db.get(IPAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="IP assignment not found")
        return assignment

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        subscription_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(IPAssignment)
        if account_id:
            query = query.filter(IPAssignment.account_id == account_id)
        if subscription_id:
            query = query.filter(IPAssignment.subscription_id == subscription_id)
        if is_active is None:
            query = query.filter(IPAssignment.is_active.is_(True))
        else:
            query = query.filter(IPAssignment.is_active == is_active)
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
        account_id = str(data.get("account_id", assignment.account_id))
        subscription_id = data.get("subscription_id", assignment.subscription_id)
        subscription_add_on_id = data.get(
            "subscription_add_on_id", assignment.subscription_add_on_id
        )
        service_address_id = data.get(
            "service_address_id", assignment.service_address_id
        )
        network_validators.validate_ip_assignment_links(
            db,
            account_id,
            str(subscription_id) if subscription_id else None,
            str(subscription_add_on_id) if subscription_add_on_id else None,
            str(service_address_id) if service_address_id else None,
        )
        for key, value in data.items():
            setattr(assignment, key, value)
        db.commit()
        db.refresh(assignment)
        return assignment

    @staticmethod
    def delete(db: Session, assignment_id: str):
        assignment = db.get(IPAssignment, assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="IP assignment not found")
        assignment.is_active = False
        db.commit()


class IpPools(ListResponseMixin):
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

    @staticmethod
    def get(db: Session, pool_id: str):
        pool = db.get(IpPool, coerce_uuid(pool_id))
        if not pool:
            raise HTTPException(status_code=404, detail="IP pool not found")
        return pool

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
        if ip_version:
            query = query.filter(IpPool.ip_version == ip_version)
        if is_active is None:
            query = query.filter(IpPool.is_active.is_(True))
        else:
            query = query.filter(IpPool.is_active == is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IpPool.created_at, "name": IpPool.name},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, pool_id: str, payload: IpPoolUpdate):
        pool = db.get(IpPool, pool_id)
        if not pool:
            raise HTTPException(status_code=404, detail="IP pool not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(pool, key, value)
        db.commit()
        db.refresh(pool)
        return pool

    @staticmethod
    def delete(db: Session, pool_id: str):
        pool = db.get(IpPool, pool_id)
        if not pool:
            raise HTTPException(status_code=404, detail="IP pool not found")
        pool.is_active = False
        db.commit()


class IpBlocks(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: IpBlockCreate):
        data = payload.model_dump()
        block = IpBlock(**data)
        db.add(block)
        db.commit()
        db.refresh(block)
        return block

    @staticmethod
    def get(db: Session, block_id: str):
        block = db.get(IpBlock, block_id)
        if not block:
            raise HTTPException(status_code=404, detail="IP block not found")
        return block

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
        if pool_id:
            query = query.filter(IpBlock.pool_id == pool_id)
        if is_active is None:
            query = query.filter(IpBlock.is_active.is_(True))
        else:
            query = query.filter(IpBlock.is_active == is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IpBlock.created_at, "cidr": IpBlock.cidr},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, block_id: str, payload: IpBlockUpdate):
        block = db.get(IpBlock, block_id)
        if not block:
            raise HTTPException(status_code=404, detail="IP block not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(block, key, value)
        db.commit()
        db.refresh(block)
        return block

    @staticmethod
    def delete(db: Session, block_id: str):
        block = db.get(IpBlock, block_id)
        if not block:
            raise HTTPException(status_code=404, detail="IP block not found")
        block.is_active = False
        db.commit()


class IPv4Addresses(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: IPv4AddressCreate):
        address = IPv4Address(**payload.model_dump())
        db.add(address)
        db.commit()
        db.refresh(address)
        return address

    @staticmethod
    def get(db: Session, address_id: str):
        address = db.get(IPv4Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="IPv4 address not found")
        return address

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
        if pool_id:
            query = query.filter(IPv4Address.pool_id == pool_id)
        if is_reserved is not None:
            query = query.filter(IPv4Address.is_reserved == is_reserved)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IPv4Address.created_at, "address": IPv4Address.address},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, address_id: str, payload: IPv4AddressUpdate):
        address = db.get(IPv4Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="IPv4 address not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(address, key, value)
        db.commit()
        db.refresh(address)
        return address

    @staticmethod
    def delete(db: Session, address_id: str):
        address = db.get(IPv4Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="IPv4 address not found")
        db.delete(address)
        db.commit()


class IPv6Addresses(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: IPv6AddressCreate):
        address = IPv6Address(**payload.model_dump())
        db.add(address)
        db.commit()
        db.refresh(address)
        return address

    @staticmethod
    def get(db: Session, address_id: str):
        address = db.get(IPv6Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="IPv6 address not found")
        return address

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
        if pool_id:
            query = query.filter(IPv6Address.pool_id == pool_id)
        if is_reserved is not None:
            query = query.filter(IPv6Address.is_reserved == is_reserved)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": IPv6Address.created_at, "address": IPv6Address.address},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, address_id: str, payload: IPv6AddressUpdate):
        address = db.get(IPv6Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="IPv6 address not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(address, key, value)
        db.commit()
        db.refresh(address)
        return address

    @staticmethod
    def delete(db: Session, address_id: str):
        address = db.get(IPv6Address, address_id)
        if not address:
            raise HTTPException(status_code=404, detail="IPv6 address not found")
        db.delete(address)
        db.commit()


ip_assignments = IPAssignments()
ip_pools = IpPools()
ip_blocks = IpBlocks()
ipv4_addresses = IPv4Addresses()
ipv6_addresses = IPv6Addresses()
