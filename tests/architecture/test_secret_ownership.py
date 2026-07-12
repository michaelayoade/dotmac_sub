"""Architecture checks for secret and credential ownership."""

from app.services import credential_key_rotation
from app.services.credential_crypto import ENCRYPTED_MODEL_FIELDS


def test_every_declared_encrypted_model_is_covered_by_key_rotation() -> None:
    assert set(credential_key_rotation._MODEL_BY_NAME) == set(ENCRYPTED_MODEL_FIELDS)


def test_security_task_remains_a_thin_service_wrapper() -> None:
    from pathlib import Path

    import app.tasks.security as security_task

    source = Path(security_task.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "SessionLocal",
        "db_session_adapter",
        "sqlalchemy",
        ".commit(",
        ".rollback(",
        ".execute(",
    ):
        assert forbidden not in source
