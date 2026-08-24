"""PostgreSQL evidence for the merged-source status backfill."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.stored_file import StoredFile
from app.models.support import (
    AutomationActionType,
    AutomationTrigger,
    Ticket,
    TicketAutomationRule,
    TicketMerge,
)
from app.services import support_ticket_settings

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic/versions/552_cancel_merged_ticket_sources.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "cancel_merged_ticket_sources", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_preserves_merge_relation_and_is_idempotent(
    db_session, subscriber, monkeypatch
) -> None:
    migration = _load_migration()
    assert migration.down_revision == "551_machine_credentials"

    opened_at = datetime.now(UTC) - timedelta(days=3)
    updated_at = datetime.now(UTC) - timedelta(days=1)
    target = Ticket(
        subscriber_id=subscriber.id,
        title="Canonical merge target",
        status="open",
        priority="normal",
    )
    source = Ticket(
        subscriber_id=subscriber.id,
        title="Legacy merged source",
        description="Preserve source evidence",
        status="merged",
        priority="high",
        tags=["duplicate"],
        metadata_={"legacy": True},
        attachments=[{"file_name": "evidence.txt", "storage_key": "kept"}],
        created_at=opened_at,
        updated_at=updated_at,
    )
    db_session.add_all([target, source])
    db_session.flush()
    merge = TicketMerge(
        source_ticket_id=source.id,
        target_ticket_id=target.id,
        reason="duplicate",
        created_at=opened_at,
    )
    rule = TicketAutomationRule(
        name="Retired merged rule",
        trigger=AutomationTrigger.status_changed,
        conditions={"status": "merged"},
        action_type=AutomationActionType.set_status,
        action_value={"status": "merged"},
        is_active=True,
    )
    db_session.add_all([merge, rule])
    stored_attachment = StoredFile(
        entity_type="support_ticket_attachment",
        entity_id=str(source.id),
        original_filename="evidence.txt",
        storage_key_or_relative_path="kept",
        file_size=4,
        content_type="text/plain",
        storage_provider="s3",
    )
    db_session.add(stored_attachment)
    target.attachments = list(source.attachments or [])

    configured = db_session.scalar(
        select(DomainSetting).where(
            DomainSetting.key == support_ticket_settings.STATUS_OPTIONS_KEY
        )
    )
    if configured is None:
        configured = DomainSetting(
            domain=SettingDomain.workflow,
            key=support_ticket_settings.STATUS_OPTIONS_KEY,
            value_type=SettingValueType.json,
            value_json=["open", "merged", "canceled", "closed"],
        )
        db_session.add(configured)
    else:
        configured.value_json = ["open", "merged", "canceled", "closed"]
    db_session.flush()

    context = MigrationContext.configure(db_session.connection())
    monkeypatch.setattr(migration, "op", Operations(context))

    migration.upgrade()
    migration.upgrade()
    db_session.expire_all()

    repaired = db_session.get(Ticket, source.id)
    assert repaired is not None
    assert repaired.status == "canceled"
    assert repaired.display_status == "merged"
    assert repaired.merged_into_ticket_id == target.id
    assert repaired.title == "Legacy merged source"
    assert repaired.description == "Preserve source evidence"
    assert repaired.priority == "high"
    assert repaired.tags == ["duplicate"]
    assert repaired.metadata_ == {"legacy": True}
    assert repaired.attachments == []
    assert repaired.created_at == opened_at
    assert repaired.updated_at == updated_at
    repaired_target = db_session.get(Ticket, target.id)
    assert repaired_target is not None
    assert repaired_target.attachments == [
        {"file_name": "evidence.txt", "storage_key": "kept"}
    ]

    repaired_configuration = db_session.get(DomainSetting, configured.id)
    assert repaired_configuration is not None
    assert repaired_configuration.value_json == ["open", "canceled", "closed"]
    repaired_rule = db_session.get(TicketAutomationRule, rule.id)
    assert repaired_rule is not None
    assert repaired_rule.is_active is False
    repaired_file = db_session.get(StoredFile, stored_attachment.id)
    assert repaired_file is not None
    assert repaired_file.entity_id == str(target.id)
