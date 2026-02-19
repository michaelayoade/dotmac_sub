from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.subscription_engine import SubscriptionEngine, SubscriptionEngineSetting
from app.schemas.subscription_engine import (
    SubscriptionEngineCreate,
    SubscriptionEngineSettingCreate,
    SubscriptionEngineSettingUpdate,
    SubscriptionEngineUpdate,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
)
from app.services.response import ListResponseMixin


class Engines(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SubscriptionEngineCreate):
        engine = SubscriptionEngine(**payload.model_dump())
        db.add(engine)
        db.commit()
        db.refresh(engine)
        return engine

    @staticmethod
    def get(db: Session, engine_id: str):
        engine = db.get(SubscriptionEngine, engine_id)
        if not engine:
            raise HTTPException(status_code=404, detail="Subscription engine not found")
        return engine

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SubscriptionEngine)
        if is_active is None:
            query = query.filter(SubscriptionEngine.is_active.is_(True))
        else:
            query = query.filter(SubscriptionEngine.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SubscriptionEngine.created_at, "name": SubscriptionEngine.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, engine_id: str, payload: SubscriptionEngineUpdate):
        engine = db.get(SubscriptionEngine, engine_id)
        if not engine:
            raise HTTPException(status_code=404, detail="Subscription engine not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(engine, key, value)
        db.commit()
        db.refresh(engine)
        return engine

    @staticmethod
    def delete(db: Session, engine_id: str):
        engine = db.get(SubscriptionEngine, engine_id)
        if not engine:
            raise HTTPException(status_code=404, detail="Subscription engine not found")
        engine.is_active = False
        db.commit()


class EngineSettings(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SubscriptionEngineSettingCreate):
        setting = SubscriptionEngineSetting(**payload.model_dump())
        db.add(setting)
        db.commit()
        db.refresh(setting)
        return setting

    @staticmethod
    def get(db: Session, setting_id: str):
        setting = db.get(SubscriptionEngineSetting, setting_id)
        if not setting:
            raise HTTPException(status_code=404, detail="Subscription engine setting not found")
        return setting

    @staticmethod
    def list(
        db: Session,
        engine_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SubscriptionEngineSetting)
        if engine_id:
            query = query.filter(SubscriptionEngineSetting.engine_id == engine_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SubscriptionEngineSetting.created_at, "key": SubscriptionEngineSetting.key},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, setting_id: str, payload: SubscriptionEngineSettingUpdate):
        setting = db.get(SubscriptionEngineSetting, setting_id)
        if not setting:
            raise HTTPException(status_code=404, detail="Subscription engine setting not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(setting, key, value)
        db.commit()
        db.refresh(setting)
        return setting

    @staticmethod
    def delete(db: Session, setting_id: str):
        setting = db.get(SubscriptionEngineSetting, setting_id)
        if not setting:
            raise HTTPException(status_code=404, detail="Subscription engine setting not found")
        db.delete(setting)
        db.commit()


engines = Engines()
engine_settings = EngineSettings()

subscription_engines = engines
subscription_engine_settings = engine_settings
