"""Explicit activation of Studio payment content and proved-pair composition.

The one persisted activation row gates both the settlement producer and email
handler. Merely installing the distribution or adopting content enables neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from dotmac_template_studio import service as studio
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel, NotificationTemplate
from app.models.payment_email import PaymentEmailCutover
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.operator_tenant import operator_tenant_id
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.payment_template_adoption import (
    PAYMENT_TEMPLATES,
    ParityStatus,
    ReviewedPaymentEmailTemplates,
    payment_email_parity_report,
    require_rls_runtime_role,
)

_COMMAND = OwnerCommandDefinition(
    owner="communications.payment_email_cutover",
    concern="reviewed payment email authority cutover",
    name="activate_payment_email_cutover",
)


class PaymentEmailSchemaState(StrEnum):
    """Closed classification; unknown evidence grants no recovery capability."""

    PREDECESSOR = "predecessor"
    INSTALLED = "installed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RollbackCatalogObservation:
    """Transaction-local catalog facts, independent of mutable activation rows."""

    legacy_table: bool
    legacy_columns: int
    cutover_relation: bool
    seal_column: bool
    episode_relation: bool
    part_relation: bool
    guard_triggers: int
    guard_functions: int
    installed_shape_complete: bool

    @property
    def legacy_shape_complete(self) -> bool:
        return (
            self.legacy_table is True
            and type(self.legacy_columns) is int
            and self.legacy_columns == 5
        )


def classify_payment_email_schema_floor(
    observation: RollbackCatalogObservation,
) -> PaymentEmailSchemaState:
    artifacts = (
        observation.cutover_relation,
        observation.seal_column,
        observation.episode_relation,
        observation.part_relation,
    )
    counts = (observation.guard_triggers, observation.guard_functions)
    if not observation.legacy_shape_complete or any(
        type(v) is not bool for v in artifacts
    ):
        return PaymentEmailSchemaState.UNKNOWN
    if any(type(v) is not int or v < 0 for v in counts):
        return PaymentEmailSchemaState.UNKNOWN
    if (
        not any(artifacts)
        and counts == (0, 0)
        and observation.installed_shape_complete is False
    ):
        return PaymentEmailSchemaState.PREDECESSOR
    if (
        all(artifacts)
        and counts == (1, 1)
        and observation.installed_shape_complete is True
    ):
        return PaymentEmailSchemaState.INSTALLED
    return PaymentEmailSchemaState.UNKNOWN


def legacy_image_rollback_allowed(observation: RollbackCatalogObservation) -> bool:
    """Only complete predecessor evidence admits an old application image."""
    return (
        classify_payment_email_schema_floor(observation)
        is PaymentEmailSchemaState.PREDECESSOR
    )


# Pinned to released revision 638's SQL, including whitespace in its function body.
_EMAIL_GUARD_DIGEST = "3fb1ea4f3fca6a78c845977fc31584fc"


_EMAIL_CATALOG = r"""
WITH relations AS (
 SELECT c.* FROM pg_catalog.pg_class c
 JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relname IN (
 'notification_templates','payment_email_episodes','payment_email_parts','payment_email_cutovers')
), columns AS (
 SELECT r.relname, a.*, pg_catalog.pg_get_expr(d.adbin,d.adrelid) AS default_expr
 FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid
 LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
 WHERE a.attnum>0 AND NOT a.attisdropped
), functions AS (
 SELECT p.* FROM pg_catalog.pg_proc p
 JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public' AND p.proname='prevent_sealed_payment_email_content'
), triggers AS (
 SELECT tr.* FROM pg_catalog.pg_trigger tr
 WHERE NOT tr.tgisinternal AND
 ((tr.tgname='guard_sealed_payment_email_content' AND tr.tgrelid IN (SELECT oid FROM relations))
 OR tr.tgfoid IN (SELECT oid FROM functions))
), expected_columns(table_name,column_name,type_name,required,default_expr) AS (
 VALUES
 ('payment_email_episodes','id','uuid',true,NULL),
 ('payment_email_episodes','tenant_id','uuid',true,NULL),
 ('payment_email_episodes','payment_id','uuid',true,NULL),
 ('payment_email_episodes','invoice_id','uuid',true,NULL),
 ('payment_email_episodes','subscriber_id','uuid',true,NULL),
 ('payment_email_episodes','recipient','character varying(255)',true,NULL),
 ('payment_email_episodes','notification_id','uuid',true,NULL),
 ('payment_email_episodes','deadline_at','timestamp with time zone',true,NULL),
 ('payment_email_episodes','created_at','timestamp with time zone',true,NULL),
 ('payment_email_parts','id','uuid',true,NULL),
 ('payment_email_parts','tenant_id','uuid',true,NULL),
 ('payment_email_parts','episode_id','uuid',true,NULL),
 ('payment_email_parts','source_event_id','uuid',true,NULL),
 ('payment_email_parts','recipient','character varying(255)',true,NULL),
 ('payment_email_parts','decision_id','uuid',true,NULL),
 ('payment_email_parts','kind','character varying(24)',true,NULL),
 ('payment_email_parts','content_template_id','uuid',true,NULL),
 ('payment_email_parts','content_version','integer',true,NULL),
 ('payment_email_parts','subject','character varying(200)',false,NULL),
 ('payment_email_parts','body','text',true,NULL),
 ('payment_email_parts','created_at','timestamp with time zone',true,NULL),
 ('payment_email_cutovers','composition_enabled','boolean',true,'true'),
 ('payment_email_cutovers','tenant_id','uuid',true,NULL),
 ('payment_email_cutovers','receipt_legacy_id','uuid',true,NULL),
 ('payment_email_cutovers','invoice_legacy_id','uuid',true,NULL),
 ('payment_email_cutovers','receipt_content_id','uuid',true,NULL),
 ('payment_email_cutovers','invoice_content_id','uuid',true,NULL),
 ('payment_email_cutovers','activated_at','timestamp with time zone',true,NULL),
 ('payment_email_cutovers','activated_by','character varying(200)',true,NULL)
), expected_constraints(table_name,kind,keys,target,target_keys,delete_action,expression) AS (
 VALUES
 ('payment_email_episodes','p',ARRAY['id']::text[],NULL::text,NULL::text[],NULL::text,NULL::text),
 ('payment_email_episodes','f',ARRAY['payment_id']::text[],'public.payments',ARRAY['id']::text[],'r',NULL::text),
 ('payment_email_episodes','f',ARRAY['invoice_id']::text[],'public.invoices',ARRAY['id']::text[],'r',NULL::text),
 ('payment_email_episodes','f',ARRAY['subscriber_id']::text[],'public.subscribers',ARRAY['id']::text[],'r',NULL::text),
 ('payment_email_episodes','f',ARRAY['notification_id']::text[],'public.notifications',ARRAY['id']::text[],'r',NULL::text),
 ('payment_email_episodes','u',ARRAY['tenant_id','payment_id','invoice_id','recipient']::text[],NULL::text,NULL::text[],NULL::text,NULL::text),
 ('payment_email_episodes','u',ARRAY['tenant_id','id']::text[],NULL::text,NULL::text[],NULL::text,NULL::text),
 ('payment_email_episodes','u',ARRAY['notification_id']::text[],NULL::text,NULL::text[],NULL::text,NULL::text),
 ('payment_email_episodes','c',ARRAY[]::text[],NULL::text,NULL::text[],NULL::text,'((deadline_at <= (created_at + ''00:01:00''::interval)) AND (deadline_at >= created_at))'),
 ('payment_email_episodes','f',ARRAY['tenant_id']::text[],'public.tenants',ARRAY['id']::text[],'c',NULL::text),
 ('payment_email_parts','p',ARRAY['id']::text[],NULL::text,NULL::text[],NULL::text,NULL::text),
 ('payment_email_parts','f',ARRAY['tenant_id','episode_id']::text[],'public.payment_email_episodes',ARRAY['tenant_id','id']::text[],'c',NULL::text),
 ('payment_email_parts','f',ARRAY['tenant_id','decision_id']::text[],'public.communication_intent_recipients',ARRAY['tenant_id','id']::text[],'r',NULL::text),
 ('payment_email_parts','u',ARRAY['tenant_id','source_event_id','recipient']::text[],NULL::text,NULL::text[],NULL::text,NULL::text),
 ('payment_email_parts','u',ARRAY['episode_id','kind']::text[],NULL::text,NULL::text[],NULL::text,NULL::text),
 ('payment_email_parts','u',ARRAY['decision_id']::text[],NULL::text,NULL::text[],NULL::text,NULL::text),
 ('payment_email_parts','c',ARRAY[]::text[],NULL::text,NULL::text[],NULL::text,'((kind)::text = ANY ((ARRAY[''receipt''::character varying, ''invoice_paid''::character varying])::text[]))'),
 ('payment_email_parts','c',ARRAY[]::text[],NULL::text,NULL::text[],NULL::text,'(length(TRIM(BOTH FROM body)) > 0)'),
 ('payment_email_parts','c',ARRAY[]::text[],NULL::text,NULL::text[],NULL::text,'(content_version > 0)'),
 ('payment_email_parts','f',ARRAY['tenant_id']::text[],'public.tenants',ARRAY['id']::text[],'c',NULL::text),
 ('payment_email_cutovers','p',ARRAY['tenant_id']::text[],NULL::text,NULL::text[],NULL::text,NULL::text),
 ('payment_email_cutovers','f',ARRAY['receipt_legacy_id']::text[],'public.notification_templates',ARRAY['id']::text[],'r',NULL::text),
 ('payment_email_cutovers','f',ARRAY['invoice_legacy_id']::text[],'public.notification_templates',ARRAY['id']::text[],'r',NULL::text),
 ('payment_email_cutovers','f',ARRAY['tenant_id']::text[],'public.tenants',ARRAY['id']::text[],'c',NULL::text)
), actual_constraints AS (
 SELECT r.relname AS table_name, c.*,
 ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY k(num,ord)
 JOIN pg_catalog.pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.num ORDER BY k.ord) AS keys,
 ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY k(num,ord)
 JOIN pg_catalog.pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.num ORDER BY k.ord) AS target_keys
 FROM relations r JOIN pg_catalog.pg_constraint c ON c.conrelid=r.oid
 WHERE r.relname <> 'notification_templates'
), installed AS (
 SELECT
 (SELECT count(*) FROM relations WHERE relname <> 'notification_templates' AND relkind='r' AND NOT relispartition)=3
 AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i
 WHERE i.inhrelid IN (SELECT oid FROM relations) OR i.inhparent IN (SELECT oid FROM relations))
 AND NOT EXISTS (
 SELECT 1 FROM expected_columns e FULL JOIN
 (SELECT * FROM columns WHERE relname <> 'notification_templates') a
 ON a.relname=e.table_name AND a.attname=e.column_name
 WHERE e.column_name IS NULL OR a.attname IS NULL
 OR pg_catalog.format_type(a.atttypid,a.atttypmod) <> e.type_name
 OR a.attnotnull <> e.required OR a.default_expr IS DISTINCT FROM e.default_expr
 OR a.attidentity <> '' OR a.attgenerated <> '')
 AND (SELECT count(*) FROM actual_constraints)=(SELECT count(*) FROM expected_constraints)
 AND NOT EXISTS (
 SELECT 1 FROM expected_constraints e WHERE NOT EXISTS (
 SELECT 1 FROM actual_constraints c WHERE c.table_name=e.table_name AND c.contype::text=e.kind
 AND c.convalidated AND NOT c.condeferrable AND NOT c.condeferred
 AND c.conislocal AND c.coninhcount=0 AND c.conparentid=0
 AND c.connoinherit=(e.kind<>'c')
 AND (e.kind='c' OR c.keys=e.keys)
 AND (e.kind NOT IN ('p','u') OR EXISTS (SELECT 1 FROM pg_catalog.pg_index i
 WHERE i.indexrelid=c.conindid AND i.indisvalid AND i.indisready AND i.indisunique))
 AND (e.kind<>'f' OR (c.confrelid=pg_catalog.to_regclass(e.target)
 AND c.target_keys=e.target_keys AND c.confdeltype::text=e.delete_action
 AND c.confupdtype='a' AND c.confmatchtype='s'
 AND (SELECT count(*) FROM pg_catalog.pg_trigger tr WHERE tr.tgconstraint=c.oid)=4
 AND (SELECT count(*) FROM pg_catalog.pg_trigger tr
 JOIN pg_catalog.pg_proc f ON f.oid=tr.tgfoid
 JOIN pg_catalog.pg_namespace n ON n.oid=f.pronamespace
 WHERE tr.tgconstraint=c.oid AND tr.tgisinternal AND tr.tgenabled='O'
 AND tr.tgnargs=0 AND octet_length(tr.tgargs)=0 AND tr.tgqual IS NULL
 AND tr.tgattr=''::int2vector AND NOT tr.tgdeferrable AND NOT tr.tginitdeferred
 AND n.nspname='pg_catalog' AND (
 (tr.tgrelid=c.conrelid AND tr.tgconstrrelid=c.confrelid AND (
   (tr.tgtype=5 AND f.proname='RI_FKey_check_ins') OR
   (tr.tgtype=17 AND f.proname='RI_FKey_check_upd')))
 OR (tr.tgrelid=c.confrelid AND tr.tgconstrrelid=c.conrelid AND (
   (tr.tgtype=9 AND f.proname=CASE e.delete_action WHEN 'c' THEN 'RI_FKey_cascade_del' ELSE 'RI_FKey_restrict_del' END) OR
   (tr.tgtype=17 AND f.proname='RI_FKey_noaction_upd')))))=4))
 AND (e.kind<>'c' OR pg_catalog.pg_get_expr(c.conbin,c.conrelid)=e.expression)))
 AND NOT EXISTS (SELECT 1 FROM relations r WHERE r.relname <> 'notification_templates'
 AND (NOT r.relrowsecurity OR NOT r.relforcerowsecurity
 OR (SELECT count(*) FROM pg_catalog.pg_policy p WHERE p.polrelid=r.oid)<>1
 OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policy p WHERE p.polrelid=r.oid
 AND p.polname=r.relname || '_tenant_isolation'
 AND p.polcmd='*' AND p.polpermissive AND p.polroles=ARRAY[0]::oid[]
 AND pg_catalog.pg_get_expr(p.polqual,p.polrelid)='(tenant_id = app_current_tenant_id())'
 AND pg_catalog.pg_get_expr(p.polwithcheck,p.polrelid)='(tenant_id = app_current_tenant_id())')))
 AND EXISTS (SELECT 1 FROM columns WHERE relname='notification_templates'
 AND attname='studio_content_sealed' AND atttypid='boolean'::regtype
 AND attnotnull AND default_expr='false' AND attidentity='' AND attgenerated='')
 AND (SELECT count(*) FROM functions)=1 AND (SELECT count(*) FROM triggers)=1
 AND EXISTS (SELECT 1 FROM triggers tr JOIN functions f ON f.oid=tr.tgfoid
 WHERE tr.tgrelid=pg_catalog.to_regclass('public.notification_templates')
 AND tr.tgname='guard_sealed_payment_email_content' AND tr.tgtype=31 AND tr.tgenabled='O'
 AND tr.tgnargs=0 AND octet_length(tr.tgargs)=0 AND tr.tgqual IS NULL
 AND tr.tgattr=''::int2vector AND NOT tr.tgdeferrable AND NOT tr.tginitdeferred
 AND f.pronargs=0 AND f.prorettype='trigger'::regtype AND NOT f.prosecdef
 AND f.proconfig IS NULL AND f.prokind='f'
 AND f.prolang=(SELECT oid FROM pg_catalog.pg_language WHERE lanname='plpgsql')
 AND md5(f.prosrc)=:guard_digest) AS complete
)
SELECT
 EXISTS (SELECT 1 FROM relations r WHERE relname='notification_templates' AND relkind='r' AND NOT relispartition
 AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conrelid=r.oid
 AND c.contype='p' AND c.convalidated AND NOT c.condeferrable
 AND c.conkey=ARRAY[(SELECT attnum FROM columns WHERE relname='notification_templates' AND attname='id')]::smallint[])),
 (SELECT count(*) FROM columns WHERE relname='notification_templates' AND
 ((attname='id' AND atttypid='uuid'::regtype AND attnotnull)
 OR (attname='code' AND atttypid='varchar'::regtype AND atttypmod=124 AND attnotnull)
 OR (attname='subject' AND atttypid='varchar'::regtype AND atttypmod=204 AND NOT attnotnull)
 OR (attname='channel' AND atttypid=pg_catalog.to_regtype('public.notificationchannel') AND attnotnull)
 OR (attname='body' AND atttypid='text'::regtype AND attnotnull))),
 EXISTS (SELECT 1 FROM relations WHERE relname='payment_email_cutovers'),
 EXISTS (SELECT 1 FROM columns WHERE relname='notification_templates' AND attname='studio_content_sealed'),
 EXISTS (SELECT 1 FROM relations WHERE relname='payment_email_episodes'),
 EXISTS (SELECT 1 FROM relations WHERE relname='payment_email_parts'),
 (SELECT count(*) FROM triggers), (SELECT count(*) FROM functions),
 (SELECT complete FROM installed)
"""


def read_legacy_image_rollback_floor(db: Session) -> RollbackCatalogObservation:
    """Read only catalog evidence under the actual nonprivileged runtime login."""
    refused = DomainError(
        code="payment_email_cutover.rollback_floor_unknown",
        message="Payment email rollback floor could not be proved",
        retryable=False,
    )
    if db.get_bind().dialect.name != "postgresql":
        raise refused
    try:
        identity = db.execute(
            text(
                "SELECT session_user, current_user, rolcanlogin, rolsuper, "
                "rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = session_user"
            )
        ).one_or_none()
        if identity is None or len(identity) != 5:
            raise refused
        session_user, current_user, can_login, is_superuser, bypass_rls = identity
        # The production runtime role is deployment-specific (currently
        # ``dotmac_app``). Prove the actual session is a stable, non-privileged
        # login instead of coupling the rollback floor to one role name.
        if (
            not isinstance(session_user, str)
            or not isinstance(current_user, str)
            or not session_user
            or session_user != current_user
            or any(
                type(value) is not bool
                for value in (can_login, is_superuser, bypass_rls)
            )
            or (can_login, is_superuser, bypass_rls) != (True, False, False)
        ):
            raise refused
        catalog = db.execute(
            text(_EMAIL_CATALOG), {"guard_digest": _EMAIL_GUARD_DIGEST}
        ).one_or_none()
    except SQLAlchemyError:
        raise refused from None
    if (
        catalog is None
        or len(catalog) != 9
        or any(type(catalog[i]) is not bool for i in (0, 2, 3, 4, 5, 8))
        or any(type(catalog[i]) is not int or catalog[i] < 0 for i in (1, 6, 7))
    ):
        raise refused
    return RollbackCatalogObservation(*catalog)


def active_cutover(db: Session) -> PaymentEmailCutover | None:
    return db.get(PaymentEmailCutover, operator_tenant_id())


def composition_enabled(db: Session) -> bool:
    # Producers/dispatchers retain a shared row lock until their source work
    # commits. Pause takes FOR UPDATE, so its completed response is the fence
    # after which no transaction can enter new collection under the old gate.
    cutover = db.scalar(
        select(PaymentEmailCutover)
        .where(PaymentEmailCutover.tenant_id == operator_tenant_id())
        .with_for_update(read=True, key_share=True)
        .execution_options(populate_existing=True)
    )
    return cutover is not None and cutover.composition_enabled


def pause_payment_email_composition(db: Session, *, context: CommandContext) -> None:
    """Stop new pair collection and its producer while retaining Studio authority.

    Already queued episodes finish from their immutable parts and coverage.
    This is deliberately a one-way rollback command; resumption requires a new
    reviewed activation contract rather than replaying the original activation.
    """
    tenant_id = operator_tenant_id()
    if context.scope != str(tenant_id):
        raise DomainError(
            code="payment_email_cutover.invalid_scope",
            message="Operator tenant scope does not match",
            retryable=False,
        )

    def operation() -> None:
        require_rls_runtime_role(db)
        cutover = db.scalar(
            select(PaymentEmailCutover)
            .where(PaymentEmailCutover.tenant_id == tenant_id)
            .with_for_update()
        )
        if cutover is None:
            raise DomainError(
                code="payment_email_cutover.not_active",
                message="Payment email authority has not been activated",
                retryable=False,
            )
        if not cutover.composition_enabled:
            return
        cutover.composition_enabled = False
        db.flush()
        emit_event(
            db,
            EventType.payment_email_composition_paused,
            {"schema_version": 1, "tenant_id": str(tenant_id)},
            actor=context.actor,
            defer_until_commit=True,
            dispatch_after_commit=False,
            record_only=True,
        )

    execute_owner_command(
        db,
        definition=OwnerCommandDefinition(
            owner=_COMMAND.owner,
            concern=_COMMAND.concern,
            name="pause_payment_email_composition",
        ),
        context=context,
        operation=operation,
    )


def activate_payment_email_cutover(
    db: Session,
    *,
    context: CommandContext,
    reviewed: ReviewedPaymentEmailTemplates,
) -> UUID:
    """Switch only after current locked content proves exact adoption parity."""
    tenant_id = operator_tenant_id()
    if context.scope != str(tenant_id):
        raise DomainError(
            code="payment_email_cutover.invalid_scope",
            message="Operator tenant scope does not match",
            retryable=False,
        )

    def operation() -> UUID:
        require_rls_runtime_role(db)
        if db.get_bind().dialect.name == "postgresql":
            # Refuse a concurrent alias insert/rebind during parity and sealing.
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            db.execute(
                text(
                    "LOCK TABLE public.notification_templates IN SHARE ROW EXCLUSIVE MODE"
                )
            )
        # Lock source identities in a deterministic order while proving parity.
        ids = (reviewed.payment_received_legacy_id, reviewed.invoice_paid_legacy_id)
        rows = db.scalars(
            select(NotificationTemplate)
            .where(NotificationTemplate.id.in_(ids))
            .order_by(NotificationTemplate.id)
            .with_for_update()
        ).all()
        if len(rows) != 2 or any(
            row.channel is not NotificationChannel.email for row in rows
        ):
            raise DomainError(
                code="payment_email_cutover.invalid_identity",
                message="Both reviewed legacy email identities are required",
                retryable=False,
            )
        content = tuple(
            studio.get_by_slug(db, tenant_id, item.slug, "email")
            for item in PAYMENT_TEMPLATES
        )
        for template in sorted(content, key=lambda item: str(item.id)):
            db.refresh(template, with_for_update=True)
        existing = active_cutover(db)
        if existing is not None:
            if (
                existing.receipt_legacy_id,
                existing.invoice_legacy_id,
                existing.receipt_content_id,
                existing.invoice_content_id,
            ) != (*ids, content[0].id, content[1].id):
                raise DomainError(
                    code="payment_email_cutover.identity_changed",
                    message="Activated payment email identity changed",
                    retryable=False,
                )
            return existing.tenant_id
        report = payment_email_parity_report(db)
        if (
            any(item.status is not ParityStatus.match for item in report.items)
            or tuple(item.legacy_template_id for item in report.items) != ids
        ):
            raise DomainError(
                code="payment_email_cutover.parity_failed",
                message="Current payment email adoption parity is required",
                retryable=False,
            )
        db.add(
            PaymentEmailCutover(
                tenant_id=tenant_id,
                receipt_legacy_id=ids[0],
                invoice_legacy_id=ids[1],
                receipt_content_id=content[0].id,
                invoice_content_id=content[1].id,
                activated_at=datetime.now(UTC),
                activated_by=context.actor,
            )
        )
        for row in rows:
            row.studio_content_sealed = True
        db.flush()
        emit_event(
            db,
            EventType.payment_email_cutover_activated,
            {
                "schema_version": 1,
                "receipt_legacy_id": str(ids[0]),
                "invoice_legacy_id": str(ids[1]),
                "receipt_content_id": str(content[0].id),
                "invoice_content_id": str(content[1].id),
            },
            actor=context.actor,
            defer_until_commit=True,
            dispatch_after_commit=False,
            record_only=True,
        )
        return tenant_id

    return execute_owner_command(
        db, definition=_COMMAND, context=context, operation=operation
    )


_PAYMENT_CODES = frozenset(
    {"payment_received", "payment_received_email", "invoice_paid", "invoice_paid_email"}
)


def legacy_content_is_sealed(
    db: Session, code: str, channel: NotificationChannel | str
) -> bool:
    value = channel.value if isinstance(channel, NotificationChannel) else channel
    if value != "email" or code not in _PAYMENT_CODES:
        return False
    # Legacy templates are global in this product; their immutable seal must
    # not disappear when a different tenant context hides the cutover row.
    return (
        db.scalar(
            select(NotificationTemplate.id)
            .where(NotificationTemplate.studio_content_sealed.is_(True))
            .limit(1)
        )
        is not None
    )
