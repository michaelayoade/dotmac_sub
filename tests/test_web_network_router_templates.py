from starlette.datastructures import FormData

from app.services import web_network_routers


def test_router_template_form_helpers_use_canonical_service(db_session):
    created = web_network_routers.create_template(
        db_session,
        FormData(
            {
                "name": "RouterOS NTP",
                "description": "Configure NTP",
                "category": "ntp",
                "template_body": "/system ntp client set enabled=yes",
            }
        ),
    )

    assert created.is_active is False
    assert created.category.value == "ntp"

    web_network_routers.update_template(
        db_session,
        created.id,
        FormData(
            {
                "name": "RouterOS NTP Updated",
                "description": "Configure NTP safely",
                "category": "system",
                "template_body": "/system ntp client set enabled=yes mode=unicast",
                "is_active": "1",
            }
        ),
    )
    db_session.refresh(created)

    assert created.name == "RouterOS NTP Updated"
    assert created.is_active is True
    assert created.category.value == "system"
