from __future__ import annotations

from fastapi import HTTPException
from pyrad.client import Client
from pyrad.dictionary import Dictionary
from pyrad.packet import AccessRequest
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.radius import RadiusServer


def _setting_value(db: Session, key: str) -> str | None:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.radius)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    if setting.value_text is not None:
        return setting.value_text
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _pick_radius_server(db: Session, server_id: str | None) -> RadiusServer:
    server_id = server_id or _setting_value(db, "auth_server_id")
    query = db.query(RadiusServer).filter(RadiusServer.is_active.is_(True))
    if server_id:
        server = query.filter(RadiusServer.id == server_id).first()
    else:
        server = query.order_by(RadiusServer.created_at.desc()).first()
    if not server:
        raise HTTPException(status_code=400, detail="Radius auth server not configured")
    return server


def authenticate(
    db: Session, username: str, password: str, server_id: str | None = None
) -> None:
    server = _pick_radius_server(db, server_id)
    secret = _setting_value(db, "auth_shared_secret")
    if not secret:
        raise HTTPException(status_code=400, detail="Radius auth secret not configured")
    dict_path = _setting_value(db, "auth_dictionary_path") or "/etc/raddb/dictionary"
    try:
        dictionary = Dictionary(dict_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Radius dictionary not available") from exc
    client = Client(
        server=server.host,
        secret=secret.encode("utf-8"),
        dict=dictionary,
        authport=server.auth_port,
    )
    client.retries = 1
    client.timeout = float(_setting_value(db, "auth_timeout_sec") or 3)
    req = client.CreateAuthPacket(code=AccessRequest, User_Name=username)
    req["User-Password"] = req.PwCrypt(password)
    try:
        reply = client.SendPacket(req)
    except TimeoutError as exc:
        raise HTTPException(status_code=502, detail="Radius auth timeout") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Radius auth failed") from exc
    if reply.code != reply.AccessAccept:
        raise HTTPException(status_code=401, detail="Invalid credentials")
