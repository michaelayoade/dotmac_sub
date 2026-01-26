from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.connector import ConnectorAuthType, ConnectorConfig, ConnectorType
from app.services.common import apply_ordering, apply_pagination, coerce_uuid, validate_enum
from app.schemas.connector import ConnectorConfigCreate, ConnectorConfigUpdate
from app.services.response import ListResponseMixin


class ConnectorConfigs(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: ConnectorConfigCreate):
        config = ConnectorConfig(**payload.model_dump())
        db.add(config)
        db.commit()
        db.refresh(config)
        return config

    @staticmethod
    def get(db: Session, config_id: str):
        config = db.get(ConnectorConfig, config_id)
        if not config:
            raise HTTPException(status_code=404, detail="Connector config not found")
        return config

    @staticmethod
    def list(
        db: Session,
        connector_type: str | None,
        auth_type: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ConnectorConfig)
        if connector_type:
            query = query.filter(
                ConnectorConfig.connector_type
                == validate_enum(connector_type, ConnectorType, "connector_type")
            )
        if auth_type:
            query = query.filter(
                ConnectorConfig.auth_type
                == validate_enum(auth_type, ConnectorAuthType, "auth_type")
            )
        if is_active is None:
            query = query.filter(ConnectorConfig.is_active.is_(True))
        else:
            query = query.filter(ConnectorConfig.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ConnectorConfig.created_at, "name": ConnectorConfig.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_all(
        db: Session,
        connector_type: str | None,
        auth_type: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ConnectorConfig)
        if connector_type:
            query = query.filter(
                ConnectorConfig.connector_type
                == validate_enum(connector_type, ConnectorType, "connector_type")
            )
        if auth_type:
            query = query.filter(
                ConnectorConfig.auth_type
                == validate_enum(auth_type, ConnectorAuthType, "auth_type")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ConnectorConfig.created_at, "name": ConnectorConfig.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, config_id: str, payload: ConnectorConfigUpdate):
        config = db.get(ConnectorConfig, config_id)
        if not config:
            raise HTTPException(status_code=404, detail="Connector config not found")
        data = payload.model_dump(exclude_unset=True)
        if "auth_config" in data and data["auth_config"]:
            merged = dict(config.auth_config or {})
            merged.update(data["auth_config"])
            data["auth_config"] = merged
        for key, value in data.items():
            setattr(config, key, value)
        db.commit()
        db.refresh(config)
        return config

    @staticmethod
    def delete(db: Session, config_id: str):
        config = db.get(ConnectorConfig, config_id)
        if not config:
            raise HTTPException(status_code=404, detail="Connector config not found")
        config.is_active = False
        db.commit()


connector_configs = ConnectorConfigs()
