from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.connector import ConnectorAuthType, ConnectorConfig, ConnectorType
from app.schemas.connector import ConnectorConfigCreate, ConnectorConfigUpdate


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


def _validate_enum(value, enum_cls, label):
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {label}") from exc


class ConnectorConfigs:
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
                == _validate_enum(connector_type, ConnectorType, "connector_type")
            )
        if auth_type:
            query = query.filter(
                ConnectorConfig.auth_type
                == _validate_enum(auth_type, ConnectorAuthType, "auth_type")
            )
        if is_active is None:
            query = query.filter(ConnectorConfig.is_active.is_(True))
        else:
            query = query.filter(ConnectorConfig.is_active == is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ConnectorConfig.created_at, "name": ConnectorConfig.name},
        )
        return _apply_pagination(query, limit, offset).all()

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
