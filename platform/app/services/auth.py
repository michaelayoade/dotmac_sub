import hashlib
import secrets
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.auth import (
    ApiKey,
    AuthProvider,
    MFAMethod,
    MFAMethodType,
    Session as AuthSession,
    SessionStatus,
    UserCredential,
)
from app.models.person import Person
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyGenerateRequest,
    ApiKeyUpdate,
    MFAMethodCreate,
    MFAMethodUpdate,
    SessionCreate,
    SessionUpdate,
    UserCredentialCreate,
    UserCredentialUpdate,
)


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


def hash_api_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_enum(value, enum_cls, label):
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {label}") from exc


def _ensure_person(db: Session, person_id: str):
    person = db.get(Person, person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")


class UserCredentials:
    @staticmethod
    def create(db: Session, payload: UserCredentialCreate):
        _ensure_person(db, str(payload.person_id))
        credential = UserCredential(**payload.model_dump())
        db.add(credential)
        db.commit()
        db.refresh(credential)
        return credential

    @staticmethod
    def get(db: Session, credential_id: str):
        credential = db.get(UserCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="User credential not found")
        return credential

    @staticmethod
    def list(
        db: Session,
        person_id: str | None,
        provider: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(UserCredential)
        if person_id:
            query = query.filter(UserCredential.person_id == person_id)
        if provider:
            query = query.filter(
                UserCredential.provider
                == _validate_enum(provider, AuthProvider, "provider")
            )
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": UserCredential.created_at,
                "username": UserCredential.username,
                "last_login_at": UserCredential.last_login_at,
            },
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, credential_id: str, payload: UserCredentialUpdate):
        credential = db.get(UserCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="User credential not found")
        data = payload.model_dump(exclude_unset=True)
        if "person_id" in data:
            _ensure_person(db, str(data["person_id"]))
        for key, value in data.items():
            setattr(credential, key, value)
        db.commit()
        db.refresh(credential)
        return credential

    @staticmethod
    def delete(db: Session, credential_id: str):
        credential = db.get(UserCredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="User credential not found")
        db.delete(credential)
        db.commit()


class MFAMethods:
    @staticmethod
    def create(db: Session, payload: MFAMethodCreate):
        _ensure_person(db, str(payload.person_id))
        if payload.is_primary:
            db.query(MFAMethod).filter(
                MFAMethod.person_id == payload.person_id,
                MFAMethod.is_primary.is_(True),
            ).update({"is_primary": False})
        method = MFAMethod(**payload.model_dump())
        db.add(method)
        db.commit()
        db.refresh(method)
        return method

    @staticmethod
    def get(db: Session, method_id: str):
        method = db.get(MFAMethod, method_id)
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        return method

    @staticmethod
    def list(
        db: Session,
        person_id: str | None,
        method_type: str | None,
        is_primary: bool | None,
        enabled: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(MFAMethod)
        if person_id:
            query = query.filter(MFAMethod.person_id == person_id)
        if method_type:
            query = query.filter(
                MFAMethod.method_type
                == _validate_enum(method_type, MFAMethodType, "method_type")
            )
        if is_primary is not None:
            query = query.filter(MFAMethod.is_primary == is_primary)
        if enabled is not None:
            query = query.filter(MFAMethod.enabled == enabled)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": MFAMethod.created_at,
                "method_type": MFAMethod.method_type,
                "is_primary": MFAMethod.is_primary,
            },
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, method_id: str, payload: MFAMethodUpdate):
        method = db.get(MFAMethod, method_id)
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        data = payload.model_dump(exclude_unset=True)
        if "person_id" in data:
            _ensure_person(db, str(data["person_id"]))
        if data.get("is_primary"):
            person_id = data.get("person_id", method.person_id)
            db.query(MFAMethod).filter(
                MFAMethod.person_id == person_id,
                MFAMethod.id != method.id,
                MFAMethod.is_primary.is_(True),
            ).update({"is_primary": False})
        for key, value in data.items():
            setattr(method, key, value)
        db.commit()
        db.refresh(method)
        return method

    @staticmethod
    def delete(db: Session, method_id: str):
        method = db.get(MFAMethod, method_id)
        if not method:
            raise HTTPException(status_code=404, detail="MFA method not found")
        db.delete(method)
        db.commit()


class Sessions:
    @staticmethod
    def create(db: Session, payload: SessionCreate):
        _ensure_person(db, str(payload.person_id))
        session = AuthSession(**payload.model_dump())
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def get(db: Session, session_id: str):
        session = db.get(AuthSession, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    @staticmethod
    def list(
        db: Session,
        person_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AuthSession)
        if person_id:
            query = query.filter(AuthSession.person_id == person_id)
        if status:
            query = query.filter(
                AuthSession.status
                == _validate_enum(status, SessionStatus, "status")
            )
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": AuthSession.created_at,
                "last_seen_at": AuthSession.last_seen_at,
                "status": AuthSession.status,
            },
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, session_id: str, payload: SessionUpdate):
        session = db.get(AuthSession, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        data = payload.model_dump(exclude_unset=True)
        if "person_id" in data:
            _ensure_person(db, str(data["person_id"]))
        for key, value in data.items():
            setattr(session, key, value)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def delete(db: Session, session_id: str):
        session = db.get(AuthSession, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        db.delete(session)
        db.commit()


class ApiKeys:
    @staticmethod
    def generate(db: Session, payload: ApiKeyGenerateRequest):
        raw_key = secrets.token_urlsafe(32)
        data = payload.model_dump()
        data["key_hash"] = hash_api_key(raw_key)
        data.setdefault("is_active", True)
        if data.get("person_id"):
            _ensure_person(db, str(data["person_id"]))
        api_key = ApiKey(**data)
        db.add(api_key)
        db.commit()
        db.refresh(api_key)
        return api_key, raw_key

    @staticmethod
    def create(db: Session, payload: ApiKeyCreate):
        if payload.person_id:
            _ensure_person(db, str(payload.person_id))
        data = payload.model_dump()
        data["key_hash"] = hash_api_key(data["key_hash"])
        api_key = ApiKey(**data)
        db.add(api_key)
        db.commit()
        db.refresh(api_key)
        return api_key

    @staticmethod
    def get(db: Session, key_id: str):
        api_key = db.get(ApiKey, key_id)
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")
        return api_key

    @staticmethod
    def list(
        db: Session,
        person_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(ApiKey)
        if person_id:
            query = query.filter(ApiKey.person_id == person_id)
        if is_active is not None:
            query = query.filter(ApiKey.is_active == is_active)
        query = _apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": ApiKey.created_at, "label": ApiKey.label},
        )
        return _apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, key_id: str, payload: ApiKeyUpdate):
        api_key = db.get(ApiKey, key_id)
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")
        data = payload.model_dump(exclude_unset=True)
        if "person_id" in data and data["person_id"] is not None:
            _ensure_person(db, str(data["person_id"]))
        if "key_hash" in data and data["key_hash"]:
            data["key_hash"] = hash_api_key(data["key_hash"])
        for key, value in data.items():
            setattr(api_key, key, value)
        db.commit()
        db.refresh(api_key)
        return api_key

    @staticmethod
    def delete(db: Session, key_id: str):
        api_key = db.get(ApiKey, key_id)
        if not api_key:
            raise HTTPException(status_code=404, detail="API key not found")
        api_key.is_active = False
        api_key.revoked_at = datetime.utcnow()
        db.commit()

    @staticmethod
    def revoke(db: Session, key_id: str):
        ApiKeys.delete(db, key_id)


user_credentials = UserCredentials()
mfa_methods = MFAMethods()
sessions = Sessions()
api_keys = ApiKeys()
