import uuid

from app.models.router_management import (
    JumpHost,
    Router,
    RouterAccessMethod,
    RouterConfigPush,
    RouterConfigPushResult,
    RouterConfigPushStatus,
    RouterConfigSnapshot,
    RouterConfigTemplate,
    RouterInterface,
    RouterPushResultStatus,
    RouterSnapshotSource,
    RouterStatus,
    RouterTemplateCategory,
)


def test_router_creation(db_session):
    router = Router(
        name="router-hq",
        hostname="hq-core",
        management_ip="10.0.0.1",
        rest_api_port=443,
        rest_api_username="admin",
        rest_api_password="enc:test",
        access_method=RouterAccessMethod.direct,
        status=RouterStatus.online,
    )
    db_session.add(router)
    db_session.commit()
    db_session.refresh(router)

    assert router.id is not None
    assert router.name == "router-hq"
    assert router.status == RouterStatus.online
    assert router.access_method == RouterAccessMethod.direct
    assert router.use_ssl is True
    assert router.verify_tls is False
    assert router.is_active is True


def test_jump_host_creation(db_session):
    jh = JumpHost(
        name="jump-dc1",
        hostname="jump.example.com",
        port=22,
        username="tunnel",
        ssh_key="enc:testkey",
    )
    db_session.add(jh)
    db_session.commit()
    db_session.refresh(jh)

    assert jh.id is not None
    assert jh.name == "jump-dc1"
    assert jh.is_active is True


def test_router_with_jump_host(db_session):
    jh = JumpHost(
        name="jump-dc2",
        hostname="jump2.example.com",
        username="tunnel",
    )
    db_session.add(jh)
    db_session.commit()
    db_session.refresh(jh)

    router = Router(
        name="router-remote",
        hostname="remote-1",
        management_ip="192.168.1.1",
        rest_api_username="admin",
        rest_api_password="enc:test",
        access_method=RouterAccessMethod.jump_host,
        jump_host_id=jh.id,
    )
    db_session.add(router)
    db_session.commit()
    db_session.refresh(router)

    assert router.jump_host_id == jh.id
    assert router.jump_host.name == "jump-dc2"


def test_router_interface(db_session):
    router = Router(
        name="router-iface-test",
        hostname="test-1",
        management_ip="10.0.0.2",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(router)
    db_session.commit()
    db_session.refresh(router)

    iface = RouterInterface(
        router_id=router.id,
        name="ether1",
        type="ether",
        mac_address="AA:BB:CC:DD:EE:FF",
        is_running=True,
        is_disabled=False,
    )
    db_session.add(iface)
    db_session.commit()
    db_session.refresh(iface)

    assert iface.router_id == router.id
    assert iface.name == "ether1"
    assert iface.is_running is True


def test_config_snapshot(db_session):
    router = Router(
        name="router-snap-test",
        hostname="snap-1",
        management_ip="10.0.0.3",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(router)
    db_session.commit()
    db_session.refresh(router)

    snap = RouterConfigSnapshot(
        router_id=router.id,
        config_export="/ip address\nadd address=10.0.0.1/24 interface=ether1",
        config_hash="abc123",
        source=RouterSnapshotSource.manual,
    )
    db_session.add(snap)
    db_session.commit()
    db_session.refresh(snap)

    assert snap.router_id == router.id
    assert snap.source == RouterSnapshotSource.manual


def test_config_template(db_session):
    tmpl = RouterConfigTemplate(
        name="sfq-queues",
        description="Set SFQ on all queues",
        template_body="/queue simple set [find] queue=sfq/sfq",
        category=RouterTemplateCategory.queue,
        variables={},
    )
    db_session.add(tmpl)
    db_session.commit()
    db_session.refresh(tmpl)

    assert tmpl.name == "sfq-queues"
    assert tmpl.category == RouterTemplateCategory.queue
    assert tmpl.is_active is True


def test_config_push_with_results(db_session):
    router = Router(
        name="router-push-test",
        hostname="push-1",
        management_ip="10.0.0.4",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(router)
    db_session.commit()
    db_session.refresh(router)

    push = RouterConfigPush(
        commands=["/queue simple set [find] queue=sfq/sfq"],
        initiated_by=uuid.uuid4(),
        status=RouterConfigPushStatus.pending,
    )
    db_session.add(push)
    db_session.commit()
    db_session.refresh(push)

    result = RouterConfigPushResult(
        push_id=push.id,
        router_id=router.id,
        status=RouterPushResultStatus.pending,
    )
    db_session.add(result)
    db_session.commit()
    db_session.refresh(result)

    assert result.push_id == push.id
    assert result.router_id == router.id
    assert result.status == RouterPushResultStatus.pending
