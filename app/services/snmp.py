from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.snmp import SnmpCredential, SnmpOid, SnmpPoller, SnmpReading, SnmpTarget
from app.services.common import apply_ordering, apply_pagination, coerce_uuid, validate_enum
from app.schemas.snmp import (
    SnmpCredentialCreate,
    SnmpCredentialUpdate,
    SnmpOidCreate,
    SnmpOidUpdate,
    SnmpPollerCreate,
    SnmpPollerUpdate,
    SnmpReadingCreate,
    SnmpReadingUpdate,
    SnmpTargetCreate,
    SnmpTargetUpdate,
)
from app.services.response import ListResponseMixin


class Credentials(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SnmpCredentialCreate):
        credential = SnmpCredential(**payload.model_dump())
        db.add(credential)
        db.commit()
        db.refresh(credential)
        return credential

    @staticmethod
    def get(db: Session, credential_id: str):
        credential = db.get(SnmpCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="SNMP credential not found")
        return credential

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SnmpCredential)
        if is_active is None:
            query = query.filter(SnmpCredential.is_active.is_(True))
        else:
            query = query.filter(SnmpCredential.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SnmpCredential.created_at, "name": SnmpCredential.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, credential_id: str, payload: SnmpCredentialUpdate):
        credential = db.get(SnmpCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="SNMP credential not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(credential, key, value)
        db.commit()
        db.refresh(credential)
        return credential

    @staticmethod
    def delete(db: Session, credential_id: str):
        credential = db.get(SnmpCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="SNMP credential not found")
        credential.is_active = False
        db.commit()


class Targets(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SnmpTargetCreate):
        target = SnmpTarget(**payload.model_dump())
        db.add(target)
        db.commit()
        db.refresh(target)
        return target

    @staticmethod
    def get(db: Session, target_id: str):
        target = db.get(SnmpTarget, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="SNMP target not found")
        return target

    @staticmethod
    def list(
        db: Session,
        device_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SnmpTarget)
        if device_id:
            query = query.filter(SnmpTarget.device_id == device_id)
        if is_active is None:
            query = query.filter(SnmpTarget.is_active.is_(True))
        else:
            query = query.filter(SnmpTarget.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SnmpTarget.created_at, "hostname": SnmpTarget.hostname},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, target_id: str, payload: SnmpTargetUpdate):
        target = db.get(SnmpTarget, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="SNMP target not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(target, key, value)
        db.commit()
        db.refresh(target)
        return target

    @staticmethod
    def delete(db: Session, target_id: str):
        target = db.get(SnmpTarget, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="SNMP target not found")
        target.is_active = False
        db.commit()


class Oids(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SnmpOidCreate):
        oid = SnmpOid(**payload.model_dump())
        db.add(oid)
        db.commit()
        db.refresh(oid)
        return oid

    @staticmethod
    def get(db: Session, oid_id: str):
        oid = db.get(SnmpOid, oid_id)
        if not oid:
            raise HTTPException(status_code=404, detail="SNMP OID not found")
        return oid

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SnmpOid)
        if is_active is None:
            query = query.filter(SnmpOid.is_active.is_(True))
        else:
            query = query.filter(SnmpOid.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SnmpOid.created_at, "name": SnmpOid.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, oid_id: str, payload: SnmpOidUpdate):
        oid = db.get(SnmpOid, oid_id)
        if not oid:
            raise HTTPException(status_code=404, detail="SNMP OID not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(oid, key, value)
        db.commit()
        db.refresh(oid)
        return oid

    @staticmethod
    def delete(db: Session, oid_id: str):
        oid = db.get(SnmpOid, oid_id)
        if not oid:
            raise HTTPException(status_code=404, detail="SNMP OID not found")
        oid.is_active = False
        db.commit()


class Pollers(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SnmpPollerCreate):
        poller = SnmpPoller(**payload.model_dump())
        db.add(poller)
        db.commit()
        db.refresh(poller)
        return poller

    @staticmethod
    def get(db: Session, poller_id: str):
        poller = db.get(SnmpPoller, poller_id)
        if not poller:
            raise HTTPException(status_code=404, detail="SNMP poller not found")
        return poller

    @staticmethod
    def list(
        db: Session,
        target_id: str | None,
        oid_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SnmpPoller)
        if target_id:
            query = query.filter(SnmpPoller.target_id == target_id)
        if oid_id:
            query = query.filter(SnmpPoller.oid_id == oid_id)
        if is_active is None:
            query = query.filter(SnmpPoller.is_active.is_(True))
        else:
            query = query.filter(SnmpPoller.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SnmpPoller.created_at, "poll_interval_sec": SnmpPoller.poll_interval_sec},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, poller_id: str, payload: SnmpPollerUpdate):
        poller = db.get(SnmpPoller, poller_id)
        if not poller:
            raise HTTPException(status_code=404, detail="SNMP poller not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(poller, key, value)
        db.commit()
        db.refresh(poller)
        return poller

    @staticmethod
    def delete(db: Session, poller_id: str):
        poller = db.get(SnmpPoller, poller_id)
        if not poller:
            raise HTTPException(status_code=404, detail="SNMP poller not found")
        poller.is_active = False
        db.commit()


class Readings(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SnmpReadingCreate):
        reading = SnmpReading(**payload.model_dump())
        db.add(reading)
        db.commit()
        db.refresh(reading)
        return reading

    @staticmethod
    def get(db: Session, reading_id: str):
        reading = db.get(SnmpReading, reading_id)
        if not reading:
            raise HTTPException(status_code=404, detail="SNMP reading not found")
        return reading

    @staticmethod
    def list(
        db: Session,
        poller_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SnmpReading)
        if poller_id:
            query = query.filter(SnmpReading.poller_id == poller_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SnmpReading.created_at, "recorded_at": SnmpReading.recorded_at},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, reading_id: str, payload: SnmpReadingUpdate):
        reading = db.get(SnmpReading, reading_id)
        if not reading:
            raise HTTPException(status_code=404, detail="SNMP reading not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(reading, key, value)
        db.commit()
        db.refresh(reading)
        return reading

    @staticmethod
    def delete(db: Session, reading_id: str):
        reading = db.get(SnmpReading, reading_id)
        if not reading:
            raise HTTPException(status_code=404, detail="SNMP reading not found")
        db.delete(reading)
        db.commit()


credentials = Credentials()
targets = Targets()
oids = Oids()
pollers = Pollers()
readings = Readings()

snmp_credentials = credentials
snmp_targets = targets
snmp_oids = oids
snmp_pollers = pollers
snmp_readings = readings