from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.schemas.auth_flow import MeUpdateRequest
from app.services import user_profile as user_profile_service


def test_get_me_supports_system_user(db_session):
    user = SystemUser(
        first_name="Admin",
        last_name="Account",
        display_name="Admin Account",
        email="admin-account@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    result = user_profile_service.get_me(
        db_session,
        principal_id=user.id,
        principal_type="system_user",
        roles=["admin"],
        scopes=["network:read"],
    )

    assert result.id == user.id
    assert result.email == user.email
    assert result.roles == ["admin"]
    assert result.scopes == ["network:read"]


def test_update_me_supports_system_user(db_session):
    user = SystemUser(
        first_name="Ops",
        last_name="Lead",
        display_name="Ops Lead",
        email="ops-lead@example.com",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    result = user_profile_service.update_me(
        db_session,
        principal_id=user.id,
        principal_type="system_user",
        payload=MeUpdateRequest(first_name="Operations", phone="+2348000000000"),
        roles=["admin"],
        scopes=[],
    )

    assert result.first_name == "Operations"
    assert result.phone == "+2348000000000"
