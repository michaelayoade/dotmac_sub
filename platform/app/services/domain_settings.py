from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingCreate, DomainSettingUpdate


def _apply_ordering(query, order_by, order_dir, allowed_columns):
    if order_by not in allowed_columns:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid order_by. Allowed: {', '.join(sorted(allowed_columns))}",
        )
    column = allowed_columns[order_by]
    if order_dir == "desc":
        return query.order_by(column.desc())
    return query.order_by(column.asc())


def _apply_pagination(query, limit, offset):
    return query.limit(limit).offset(offset)


class DomainSettings:
    def __init__(self, domain: SettingDomain | None = None) -> None:
        self.domain = domain

    def _resolve_domain(self, payload_domain: SettingDomain | None) -> SettingDomain:
        if self.domain and payload_domain and payload_domain != self.domain:
            raise HTTPException(status_code=400, detail="Setting domain mismatch")
        if self.domain:
            return self.domain
        if payload_domain:
            return payload_domain
        raise HTTPException(status_code=400, detail="Setting domain is required")

    def create(self, db: Session, payload: DomainSettingCreate):
        data = payload.model_dump()
        data["domain"] = self._resolve_domain(payload.domain)
        setting = DomainSetting(**data)
        db.add(setting)
        db.commit()
        db.refresh(setting)
        return setting

    def get(self, db: Session, setting_id: str):
        setting = db.get(DomainSetting, setting_id)
        if not setting or (self.domain and setting.domain != self.domain):
            raise HTTPException(status_code=404, detail="Setting not found")
        return setting

    def list(
        self,
        db: Session,
        domain: SettingDomain | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(DomainSetting)
        effective_domain = self.domain or domain
        if effective_domain:
            query = query.filter(DomainSetting.domain == effective_domain)
        if is_active is None:
            query = query.filter(DomainSetting.is_active.is_(True))
        else:
            query = query.filter(DomainSetting.is_active == is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": DomainSetting.created_at, "key": DomainSetting.key},
        )
        return _apply_pagination(query, limit, offset).all()

    def update(self, db: Session, setting_id: str, payload: DomainSettingUpdate):
        setting = db.get(DomainSetting, setting_id)
        if not setting or (self.domain and setting.domain != self.domain):
            raise HTTPException(status_code=404, detail="Setting not found")
        data = payload.model_dump(exclude_unset=True)
        if "domain" in data and data["domain"] != setting.domain:
            raise HTTPException(status_code=400, detail="Setting domain mismatch")
        for key, value in data.items():
            setattr(setting, key, value)
        db.commit()
        db.refresh(setting)
        return setting

    def get_by_key(self, db: Session, key: str):
        if not self.domain:
            raise HTTPException(status_code=400, detail="Setting domain is required")
        setting = (
            db.query(DomainSetting)
            .filter(DomainSetting.domain == self.domain)
            .filter(DomainSetting.key == key)
            .first()
        )
        if not setting:
            raise HTTPException(status_code=404, detail="Setting not found")
        return setting

    def upsert_by_key(self, db: Session, key: str, payload: DomainSettingUpdate):
        if not self.domain:
            raise HTTPException(status_code=400, detail="Setting domain is required")
        setting = (
            db.query(DomainSetting)
            .filter(DomainSetting.domain == self.domain)
            .filter(DomainSetting.key == key)
            .first()
        )
        if setting:
            data = payload.model_dump(exclude_unset=True)
            data.pop("domain", None)
            data.pop("key", None)
            for field, value in data.items():
                setattr(setting, field, value)
            db.commit()
            db.refresh(setting)
            return setting
        create_payload = DomainSettingCreate(
            domain=self.domain,
            key=key,
            value_type=payload.value_type or SettingValueType.string,
            value_text=payload.value_text,
            value_json=payload.value_json,
            is_secret=payload.is_secret or False,
            is_active=True if payload.is_active is None else payload.is_active,
        )
        return self.create(db, create_payload)

    def ensure_by_key(
        self,
        db: Session,
        key: str,
        value_type: SettingValueType,
        value_text: str | None = None,
        value_json: dict | bool | int | None = None,
        is_secret: bool = False,
    ):
        if not self.domain:
            raise HTTPException(status_code=400, detail="Setting domain is required")
        existing = (
            db.query(DomainSetting)
            .filter(DomainSetting.domain == self.domain)
            .filter(DomainSetting.key == key)
            .first()
        )
        if existing:
            return existing
        payload = DomainSettingCreate(
            domain=self.domain,
            key=key,
            value_type=value_type,
            value_text=value_text,
            value_json=value_json,
            is_secret=is_secret,
            is_active=True,
        )
        return self.create(db, payload)

    def delete(self, db: Session, setting_id: str):
        setting = db.get(DomainSetting, setting_id)
        if not setting or (self.domain and setting.domain != self.domain):
            raise HTTPException(status_code=404, detail="Setting not found")
        setting.is_active = False
        db.commit()


settings = DomainSettings()
auth_settings = DomainSettings(SettingDomain.auth)
audit_settings = DomainSettings(SettingDomain.audit)
imports_settings = DomainSettings(SettingDomain.imports)
network_settings = DomainSettings(SettingDomain.network)
network_monitoring_settings = DomainSettings(SettingDomain.network_monitoring)
provisioning_settings = DomainSettings(SettingDomain.provisioning)
usage_settings = DomainSettings(SettingDomain.usage)
radius_settings = DomainSettings(SettingDomain.radius)
collections_settings = DomainSettings(SettingDomain.collections)
lifecycle_settings = DomainSettings(SettingDomain.lifecycle)
tr069_settings = DomainSettings(SettingDomain.tr069)
snmp_settings = DomainSettings(SettingDomain.snmp)
bandwidth_settings = DomainSettings(SettingDomain.bandwidth)
subscription_engine_settings = DomainSettings(SettingDomain.subscription_engine)
gis_settings = DomainSettings(SettingDomain.gis)
scheduler_settings = DomainSettings(SettingDomain.scheduler)
