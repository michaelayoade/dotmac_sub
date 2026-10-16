"""Read-only operational schema floor; owns no Payments financial decisions.

Catalog facts come from the actual runtime login, independent of tenant-filtered
business rows. Any installed, partial or unrecognized evidence refuses an
incumbent image. Installation is permanent, including before the first intent.
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.services.domain_errors import DomainError

_GUARD_DIGEST = "5b7afe019c8a585241d7c423e060d912"


@dataclass(frozen=True)
class PaymentsIntentSchemaObservation:
    """Transaction-local pg_catalog facts, never a cached activation flag."""

    legacy_shape_complete: bool
    correlation_columns: int
    correlation_constraints: int
    correlation_triggers: int
    correlation_functions: int
    installed_shape_complete: bool


class PaymentsIntentSchemaState(StrEnum):
    """Closed catalog classification for normal forward admission and rollback."""

    PREDECESSOR = "predecessor"
    INSTALLED = "installed"
    UNKNOWN = "unknown"


def classify_payments_intent_schema_floor(
    observation: PaymentsIntentSchemaObservation,
) -> PaymentsIntentSchemaState:
    """Classify only complete predecessor or complete supported installed shape."""
    counts = (
        observation.correlation_columns,
        observation.correlation_constraints,
        observation.correlation_triggers,
        observation.correlation_functions,
    )
    if (
        observation.legacy_shape_complete is True
        and observation.installed_shape_complete is False
        and all(type(value) is int and value == 0 for value in counts)
    ):
        return PaymentsIntentSchemaState.PREDECESSOR
    if (
        observation.legacy_shape_complete is True
        and observation.installed_shape_complete is True
        and all(type(value) is int for value in counts)
        and counts == (2, 2, 1, 1)
    ):
        return PaymentsIntentSchemaState.INSTALLED
    return PaymentsIntentSchemaState.UNKNOWN


def legacy_image_rollback_allowed(observation: PaymentsIntentSchemaObservation) -> bool:
    """A complete predecessor with zero installation evidence admits legacy."""
    return (
        classify_payments_intent_schema_floor(observation)
        is PaymentsIntentSchemaState.PREDECESSOR
    )


_CATALOG = """
WITH local_table AS (
 SELECT c.oid FROM pg_catalog.pg_class c
 JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='public' AND c.relname='topup_intents' AND c.relkind IN ('r','p')
), columns AS (
 SELECT a.* FROM pg_catalog.pg_attribute a JOIN local_table t ON t.oid=a.attrelid
 WHERE a.attnum>0 AND NOT a.attisdropped
), constraints AS (
 SELECT c.* FROM pg_catalog.pg_constraint c JOIN local_table t ON t.oid=c.conrelid
 WHERE c.conname IN ('ck_topup_module_intent_pair','fk_topup_module_intent_scope')
 OR EXISTS (SELECT 1 FROM columns a WHERE a.attname LIKE 'module_intent%'
            AND a.attnum=ANY(c.conkey))
), functions AS (
 SELECT p.* FROM pg_catalog.pg_proc p
 JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public' AND p.proname='guard_payments_invoice_intent'
), triggers AS (
 SELECT tr.* FROM pg_catalog.pg_trigger tr JOIN local_table t ON t.oid=tr.tgrelid
 WHERE NOT tr.tgisinternal AND (tr.tgname='guard_payments_invoice_intent'
 OR tr.tgfoid IN (SELECT oid FROM functions))
)
SELECT
 (SELECT count(*) FROM columns WHERE
  (attname IN ('id','account_id','invoice_id','provider_id','capability_binding_id') AND atttypid='uuid'::regtype)
  OR (attname IN ('reference','provider_type','currency') AND atttypid='varchar'::regtype)
  OR (attname='requested_amount' AND atttypid='numeric'::regtype)
  OR (attname='metadata' AND atttypid='jsonb'::regtype)) = 10
 AND EXISTS (SELECT 1 FROM pg_catalog.pg_constraint c JOIN local_table t ON t.oid=c.conrelid
    WHERE c.contype='p' AND c.convalidated
    AND c.conkey=ARRAY[(SELECT attnum FROM columns WHERE attname='id')]::smallint[])
 AND EXISTS (SELECT 1 FROM columns WHERE attname='id' AND attnotnull),
 (SELECT count(*) FROM columns WHERE attname LIKE 'module_intent%'),
 (SELECT count(*) FROM constraints),
 (SELECT count(*) FROM triggers),
 (SELECT count(*) FROM functions),
 (
  (SELECT count(*) FROM columns WHERE attname LIKE 'module_intent%') = 2
  AND (SELECT count(*) FROM constraints) = 2
  AND (SELECT count(*) FROM triggers) = 1
  AND (SELECT count(*) FROM functions) = 1
  AND
  (SELECT count(*) FROM columns WHERE attname IN ('module_intent_id','module_intent_tenant_id')
     AND atttypid='uuid'::regtype AND NOT attnotnull AND NOT atthasdef) = 2
  AND EXISTS (SELECT 1 FROM constraints c WHERE c.conname='fk_topup_module_intent_scope'
    AND c.contype='f' AND c.convalidated AND NOT c.condeferrable
    AND c.confdeltype='r' AND c.confupdtype='a' AND c.confmatchtype='s'
    AND c.confrelid=pg_catalog.to_regclass('mod_payments.payment_intents')
    AND c.conkey=ARRAY[(SELECT attnum FROM columns WHERE attname='module_intent_tenant_id'),
                      (SELECT attnum FROM columns WHERE attname='module_intent_id')]::smallint[]
    AND c.confkey=ARRAY[
      (SELECT attnum FROM pg_catalog.pg_attribute WHERE attrelid=c.confrelid AND attname='tenant_id'),
      (SELECT attnum FROM pg_catalog.pg_attribute WHERE attrelid=c.confrelid AND attname='id')]::smallint[])
  AND EXISTS (SELECT 1 FROM constraints c WHERE c.conname='ck_topup_module_intent_pair'
    AND c.contype='c' AND c.convalidated
    AND pg_catalog.pg_get_expr(c.conbin,c.conrelid) =
       '((module_intent_id IS NULL) = (module_intent_tenant_id IS NULL))')
  AND EXISTS (SELECT 1 FROM triggers tr JOIN functions f ON f.oid=tr.tgfoid
    WHERE tr.tgname='guard_payments_invoice_intent' AND tr.tgtype=31
    AND tr.tgenabled='O' AND tr.tgnargs=0 AND tr.tgqual IS NULL
    AND NOT tr.tgdeferrable AND NOT tr.tginitdeferred
    AND f.pronargs=0 AND f.prorettype='trigger'::regtype AND NOT f.prosecdef
    AND f.proconfig=ARRAY['search_path=pg_catalog']::text[]
    AND f.prolang=(SELECT oid FROM pg_catalog.pg_language WHERE lanname='plpgsql')
    AND md5(f.prosrc)=:guard_digest)
 )
"""


def read_payments_intent_schema_floor(db: Session) -> PaymentsIntentSchemaObservation:
    """Prove catalog shape under an actual nonprivileged runtime login.

    Errors and malformed observations refuse recovery. This owner performs
    SELECT only and never opens/completes a transaction or reads business rows.
    """
    refused = DomainError(
        code="payments_intent_schema_floor.unknown",
        message="Payments invoice intent schema floor could not be proved",
        retryable=False,
    )
    if db.get_bind().dialect.name != "postgresql":
        raise refused
    try:
        identity = db.execute(
            text(
                "SELECT session_user, current_user, rolcanlogin, rolsuper, rolbypassrls "
                "FROM pg_catalog.pg_roles WHERE rolname=session_user"
            )
        ).one_or_none()
        if identity is None or len(identity) != 5:
            raise refused
        login, current, can_login, superuser, bypass = identity
        if (
            type(login) is not str
            or not login
            or login != current
            or type(can_login) is not bool
            or type(superuser) is not bool
            or type(bypass) is not bool
            or (can_login, superuser, bypass) != (True, False, False)
        ):
            raise refused
        catalog = db.execute(
            text(_CATALOG), {"guard_digest": _GUARD_DIGEST}
        ).one_or_none()
    except SQLAlchemyError:
        raise refused from None
    if (
        catalog is None
        or len(catalog) != 6
        or any(type(catalog[i]) is not bool for i in (0, 5))
        or any(type(catalog[i]) is not int or catalog[i] < 0 for i in (1, 2, 3, 4))
    ):
        raise refused
    return PaymentsIntentSchemaObservation(
        legacy_shape_complete=catalog[0],
        correlation_columns=catalog[1],
        correlation_constraints=catalog[2],
        correlation_triggers=catalog[3],
        correlation_functions=catalog[4],
        installed_shape_complete=catalog[5],
    )
