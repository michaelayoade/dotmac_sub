import uuid

import pytest
from fastapi import HTTPException

from app.models.router_management import (
    Router,
    RouterConfigPush,
    RouterConfigPushResult,
    RouterConfigPushStatus,
    RouterConfigSnapshot,
    RouterConfigTemplate,
    RouterPushResultStatus,
    RouterSnapshotSource,
    RouterTemplateCategory,
)
from app.schemas.router_management import (
    RouterConfigPushCreate,
    RouterConfigTemplateCreate,
    RouterConfigTemplateUpdate,
)
from app.services.router_management.config import (
    RouterConfigService,
    RouterTemplateService,
)


def _make_router(db_session, name: str) -> Router:
    r = Router(
        name=name,
        hostname=name,
        management_ip="10.0.0.1",
        rest_api_username="admin",
        rest_api_password="enc:test",
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(r)
    return r


def test_store_snapshot(db_session):
    router = _make_router(db_session, "snap-store-test")
    snap = RouterConfigService.store_snapshot(
        db_session,
        router_id=router.id,
        config_export="/ip address\nadd address=10.0.0.1/24 interface=ether1",
        source=RouterSnapshotSource.manual,
    )
    assert snap.router_id == router.id
    assert snap.config_hash is not None
    assert len(snap.config_hash) == 64


def test_list_snapshots(db_session):
    router = _make_router(db_session, "snap-list-test")
    for i in range(3):
        RouterConfigService.store_snapshot(
            db_session,
            router_id=router.id,
            config_export=f"config version {i}",
            source=RouterSnapshotSource.scheduled,
        )
    snaps = RouterConfigService.list_snapshots(db_session, router.id)
    assert len(snaps) == 3


def test_get_snapshot(db_session):
    router = _make_router(db_session, "snap-get-test")
    snap = RouterConfigService.store_snapshot(
        db_session,
        router_id=router.id,
        config_export="test config",
        source=RouterSnapshotSource.manual,
    )
    fetched = RouterConfigService.get_snapshot(db_session, snap.id)
    assert fetched.config_export == "test config"


def test_create_template(db_session):
    tmpl = RouterTemplateService.create(
        db_session,
        RouterConfigTemplateCreate(
            name="test-template",
            template_body="/queue simple set [find] queue={{ queue_type }}/{{ queue_type }}",
            category="queue",
            variables={"queue_type": {"type": "string", "default": "sfq"}},
        ),
    )
    assert tmpl.name == "test-template"
    assert tmpl.category == RouterTemplateCategory.queue


def test_update_template(db_session):
    tmpl = RouterTemplateService.create(
        db_session,
        RouterConfigTemplateCreate(
            name="update-tmpl",
            template_body="original body",
        ),
    )
    updated = RouterTemplateService.update(
        db_session, tmpl.id, RouterConfigTemplateUpdate(template_body="new body")
    )
    assert updated.template_body == "new body"


def test_list_templates(db_session):
    RouterTemplateService.create(
        db_session,
        RouterConfigTemplateCreate(name="list-tmpl-1", template_body="body1"),
    )
    templates = RouterTemplateService.list(db_session)
    assert len(templates) >= 1


def test_render_template():
    body = "/ip dns set servers={{ dns_servers }}"
    variables = {"dns_servers": "8.8.8.8,8.8.4.4"}
    result = RouterConfigService.render_template(body, variables)
    assert result == "/ip dns set servers=8.8.8.8,8.8.4.4"


def test_render_template_missing_var():
    body = "/ip dns set servers={{ dns_servers }}"
    with pytest.raises(ValueError, match="Template rendering failed"):
        RouterConfigService.render_template(body, {})


def test_create_push_record(db_session):
    router = _make_router(db_session, "push-test")
    user_id = uuid.uuid4()

    push = RouterConfigService.create_push(
        db_session,
        commands=["/queue simple set [find] queue=sfq/sfq"],
        router_ids=[router.id],
        initiated_by=user_id,
    )
    assert push.status == RouterConfigPushStatus.pending
    assert len(push.results) == 1
    assert push.results[0].router_id == router.id


def test_create_push_dangerous_command(db_session):
    router = _make_router(db_session, "push-danger-test")
    with pytest.raises(ValueError, match="Dangerous command blocked"):
        RouterConfigService.create_push(
            db_session,
            commands=["/system/reset-configuration"],
            router_ids=[router.id],
            initiated_by=uuid.uuid4(),
        )
