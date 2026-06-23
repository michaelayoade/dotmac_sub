from app.models.system_user import SystemUser


def test_system_user_has_device_login_fields():
    cols = SystemUser.__table__.columns.keys()
    assert "device_login_enabled" in cols
    assert "device_login_secret" in cols
    assert "device_login_secret_set_at" in cols
    assert "device_login_revoked_at" in cols
    assert SystemUser.__table__.c.device_login_enabled.default.arg is False
