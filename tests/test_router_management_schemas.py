import uuid

import pytest
from pydantic import ValidationError

from app.schemas.router_management import (
    JumpHostCreate,
    JumpHostUpdate,
    RouterConfigPushCreate,
    RouterConfigTemplateCreate,
    RouterCreate,
    RouterUpdate,
)


def test_router_create_minimal():
    schema = RouterCreate(
        name="router-1",
        hostname="r1",
        management_ip="10.0.0.1",
        rest_api_username="admin",
        rest_api_password="secret",
    )
    assert schema.name == "router-1"
    assert schema.rest_api_port == 443
    assert schema.use_ssl is True
    assert schema.access_method == "direct"


def test_router_create_with_jump_host():
    jh_id = uuid.uuid4()
    schema = RouterCreate(
        name="router-2",
        hostname="r2",
        management_ip="10.0.0.2",
        rest_api_username="admin",
        rest_api_password="secret",
        access_method="jump_host",
        jump_host_id=jh_id,
    )
    assert schema.access_method == "jump_host"
    assert schema.jump_host_id == jh_id


def test_router_create_name_too_short():
    with pytest.raises(ValidationError):
        RouterCreate(
            name="",
            hostname="r1",
            management_ip="10.0.0.1",
            rest_api_username="admin",
            rest_api_password="secret",
        )


def test_router_update_partial():
    schema = RouterUpdate(name="new-name")
    data = schema.model_dump(exclude_unset=True)
    assert data == {"name": "new-name"}


def test_jump_host_create():
    schema = JumpHostCreate(
        name="jump-1",
        hostname="jump.example.com",
        username="tunnel",
        ssh_key="-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n-----END OPENSSH PRIVATE KEY-----",
    )
    assert schema.port == 22
    assert schema.ssh_key is not None


def test_jump_host_update_partial():
    schema = JumpHostUpdate(hostname="new-jump.example.com")
    data = schema.model_dump(exclude_unset=True)
    assert data == {"hostname": "new-jump.example.com"}


def test_config_template_create():
    schema = RouterConfigTemplateCreate(
        name="sfq-queues",
        template_body="/queue simple set [find] queue=sfq/sfq",
        category="queue",
        variables={"queue_type": {"type": "string", "default": "sfq"}},
    )
    assert schema.category == "queue"


def test_config_push_create():
    router_ids = [uuid.uuid4(), uuid.uuid4()]
    schema = RouterConfigPushCreate(
        commands=["/queue simple set [find] queue=sfq/sfq"],
        router_ids=router_ids,
    )
    assert len(schema.router_ids) == 2
    assert len(schema.commands) == 1


def test_config_push_create_empty_commands():
    with pytest.raises(ValidationError):
        RouterConfigPushCreate(
            commands=[],
            router_ids=[uuid.uuid4()],
        )


def test_config_push_create_empty_routers():
    with pytest.raises(ValidationError):
        RouterConfigPushCreate(
            commands=["/ip address print"],
            router_ids=[],
        )
