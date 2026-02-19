from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.sequence import DocumentSequence
from app.services import settings_spec


def _format_number(prefix: str | None, padding: int | None, value: int) -> str:
    prefix_value = prefix or ""
    pad = max(int(padding or 0), 0)
    if pad > 0:
        return f"{prefix_value}{value:0{pad}d}"
    return f"{prefix_value}{value}"


def _next_sequence_value(db: Session, key: str, start_value: int) -> int:
    sequence = (
        db.query(DocumentSequence)
        .filter(DocumentSequence.key == key)
        .with_for_update()
        .first()
    )
    if not sequence:
        sequence = DocumentSequence(key=key, next_value=start_value)
        db.add(sequence)
        db.flush()
    value = sequence.next_value
    sequence.next_value = value + 1
    db.flush()
    return value


def _resolve_setting(db: Session, domain: SettingDomain, key: str):
    return settings_spec.resolve_value(db, domain, key)


def generate_number(
    db: Session,
    domain: SettingDomain,
    sequence_key: str,
    enabled_key: str,
    prefix_key: str,
    padding_key: str,
    start_key: str,
) -> str | None:
    enabled = _resolve_setting(db, domain, enabled_key)
    if enabled is False:
        return None
    prefix = _resolve_setting(db, domain, prefix_key)
    padding = _resolve_setting(db, domain, padding_key)
    start_value = _resolve_setting(db, domain, start_key)
    try:
        start_value_int = int(start_value) if start_value is not None else 1
    except (TypeError, ValueError):
        start_value_int = 1
    value = _next_sequence_value(db, sequence_key, start_value_int)
    return _format_number(prefix, padding, value)
