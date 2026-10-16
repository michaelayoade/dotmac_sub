"""Private, flush-only archive participant. No caller or activation is installed.

Only the future registered runtime.billing_ledger_handoff root may invoke this
component. That whole owner MUST prohibit manually issued SQL SAVEPOINTs: the
PostgreSQL protocol cannot expose their existence to this participant.
"""

from __future__ import annotations

import hashlib
import json
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.orm import Session, SessionTransactionOrigin

from app.models.event_store import EventStore
from app.services.billing.legacy_archive_fence_contracts import (
    ArchiveFenceAuditPayload,
    ArchiveFenceRequest,
    ArchiveFenceStagedObservation,
    ArchiveRelation,
    ArchiveRelationObservation,
    CatalogDigest,
    DatabaseOid,
    FunctionOid,
    RelationOid,
    RoleOid,
    TransactionId,
    TriggerOid,
)
from app.services.domain_errors import DomainError
from app.services.events.owner_outputs import OwnerOutputEnvelope, stage_owner_output
from app.services.events.types import EventType
from app.services.owner_commands import (
    current_command_context,
    current_command_definition,
    current_owner_root_transaction,
    owner_command_active,
)
from app.services.sot_manifest import (
    OwnerRole,
    TransactionMode,
    contract_validation_errors,
)
from app.services.sot_registry.registry import all_services, service_relationship

_OWNER = "runtime.billing_ledger_handoff"
_FUNCTION = "dotmac_billing_archive_deny_v1"
_ROW = "dotmac_billing_archive_row_v1"
_STATEMENT = "dotmac_billing_archive_statement_v1"
_BODY = "BEGIN RAISE EXCEPTION 'legacy monetary archive is immutable' USING ERRCODE = '42501'; END;"
_RELATIONS = tuple(sorted(ArchiveRelation, key=lambda relation: relation.value))
_DML = frozenset({"INSERT", "UPDATE", "DELETE", "TRUNCATE"})


class ArchiveFenceRefused(DomainError):
    """The entire calling owner transaction must roll back on refusal."""


def _refuse(message: str) -> None:
    raise ArchiveFenceRefused(code="billing_archive_fence.refused", message=message)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _rows(connection: Connection, sql: str) -> list[RowMapping]:
    return list(connection.execute(text(sql)).mappings())


def _root(db: Session) -> Connection:
    if not owner_command_active(db, owner=_OWNER):
        _refuse("Archive staging requires the registered handoff owner.")
    definition = current_command_definition(db)
    if definition.owner != _OWNER:
        _refuse("Unexpected owner definition.")
    try:
        service = service_relationship(definition.owner)
    except KeyError:
        _refuse("Future handoff owner is not registered.")
        raise AssertionError("unreachable")
    if service.contract is None or contract_validation_errors(
        service, service_names={item.name for item in all_services()}
    ):
        _refuse("Future handoff owner lacks a valid typed manifest contract.")
    assert service.contract is not None
    if (
        service.contract.transaction.mode is not TransactionMode.COORDINATOR_MANAGED
        or not any(
            item.name == definition.concern
            and item.role is OwnerRole.APPLICATION_COORDINATOR
            for item in service.contract.concerns
        )
    ):
        _refuse("Future handoff concern is not a registered coordinator.")
    root = db.get_transaction()
    if (
        root is None
        or not root.is_active
        or root.parent is not None
        or root.origin is not SessionTransactionOrigin.BEGIN
        or db.in_nested_transaction()
        or db.new
        or db.dirty
        or db.deleted
        or getattr(db.get_bind, "__func__", None) is not Session.get_bind
        or getattr(db.connection, "__func__", None) is not Session.connection
    ):
        _refuse("Archive staging requires a clean, explicit active owner root.")
    bind = db.get_bind()
    # Connection-bound sessions can join external transactions/savepoints.
    if not isinstance(bind, Engine) or db.bind is not bind:
        _refuse("Archive staging requires one Engine-bound session.")
    if current_owner_root_transaction(db) is not root:
        _refuse("Archive staging requires the exact executor-owned root.")
    connection = db.connection()
    transaction = connection.get_transaction()
    if (
        connection.engine is not bind
        or connection.dialect.name != "postgresql"
        or transaction is None
        or not transaction.is_active
        or connection.in_nested_transaction()
        or bind.get_execution_options().get("schema_translate_map")
        or connection.get_execution_options().get("schema_translate_map")
    ):
        _refuse("Archive staging requires one untranslated PostgreSQL root.")
    for relation in _RELATIONS:
        clause = text(
            "SELECT 1 FROM public." + _quote(relation.value)  # noqa: S608 -- closed enum, quoted identifier
        )  # closed enum only
        if (
            db.get_bind(clause=clause) is not bind
            or db.connection(bind_arguments={"clause": clause}) is not connection
            or connection.get_transaction() is not transaction
        ):
            _refuse("Archive objects must share the exact owner connection.")
    if (
        db.get_bind(mapper=EventStore) is not bind
        or db.get_bind(clause=EventStore.__table__) is not bind
        or db.connection(bind_arguments={"mapper": EventStore}) is not connection
    ):
        _refuse("Component audit must use the archive owner root and connection.")
    return connection


def _posture(connection: Connection) -> RowMapping:
    row = _rows(
        connection,
        """
        SELECT d.oid AS database_oid, pg_catalog.pg_backend_pid() AS backend_pid,
               pg_catalog.pg_current_xact_id()::text AS transaction_id,
               o.oid AS owner_oid, r.oid AS runtime_oid,
               pg_catalog.current_setting('transaction_isolation') AS isolation,
               pg_catalog.current_setting('transaction_read_only') AS read_only,
               pg_catalog.current_setting('lock_timeout')::interval > interval '0' AND
               pg_catalog.current_setting('lock_timeout')::interval <= interval '60 seconds'
                 AS bounded_lock,
               pg_catalog.current_setting('statement_timeout')::interval > interval '0' AND
               pg_catalog.current_setting('statement_timeout')::interval <= interval '120 seconds'
                 AS bounded_statement,
               NOT o.rolsuper AND o.oid <> r.oid AND n.nspowner = o.oid
                 AS owner_valid,
               r.rolcanlogin AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR
                    r.rolcreatedb OR r.rolreplication) AND n.nspowner <> r.oid
                 AS runtime_valid,
               NOT (pg_catalog.pg_has_role(r.oid, o.oid, 'MEMBER') OR
                    pg_catalog.pg_has_role(r.oid, o.oid, 'USAGE') OR
                    pg_catalog.pg_has_role(r.oid, o.oid, 'SET')) AS separated
          FROM pg_catalog.pg_database d
          JOIN pg_catalog.pg_roles o ON o.rolname = current_user
          JOIN pg_catalog.pg_roles r ON r.rolname = 'app_user'
          JOIN pg_catalog.pg_namespace n ON n.nspname = 'public'
         WHERE d.datname = pg_catalog.current_database()
        """,
    )
    if len(row) != 1:
        _refuse("Missing database, public namespace or actual role identity.")
    identity = row[0]
    if (
        connection.dialect.server_version_info < (16,)
        or identity["isolation"] != "read committed"
        or identity["read_only"] != "off"
        or not all(
            identity[field]
            for field in (
                "bounded_lock",
                "bounded_statement",
                "owner_valid",
                "runtime_valid",
                "separated",
            )
        )
    ):
        _refuse("Actual owner, runtime separation, isolation or wait bounds refuse.")
    return identity


def _relations(connection: Connection, owner_oid: int) -> list[RowMapping]:
    result: list[RowMapping] = []
    for relation in _RELATIONS:
        rows = list(
            connection.execute(
                text(
                    """
                    SELECT c.oid, c.relowner, c.relkind, c.relispartition,
                           pg_catalog.to_regclass(:name)::oid = c.oid AS unshadowed,
                           NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i
                              WHERE i.inhrelid = c.oid OR i.inhparent = c.oid)
                              AS no_inheritance
                      FROM pg_catalog.pg_class c
                      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'public' AND c.relname = :name
                    """
                ),
                {"name": relation.value},
            ).mappings()
        )
        if len(rows) != 1:
            _refuse("An exact archived table is missing.")
        row = rows[0]
        if (
            row["relowner"] != owner_oid
            or row["relkind"] != "r"
            or row["relispartition"]
            or not row["unshadowed"]
            or not row["no_inheritance"]
        ):
            _refuse("Archive ownership, ordinary shape or namespace resolution drift.")
        result.append(row)
    return result


def _function(connection: Connection, owner_oid: int) -> int | None:
    rows = list(
        connection.execute(
            text(
                """
                SELECT p.oid, p.proowner, p.prosrc, p.prosecdef, p.proconfig,
                       p.pronargs, p.prorettype = 'pg_catalog.trigger'::regtype AS returns_trigger,
                       p.prokind, p.provolatile, p.proisstrict, p.proleakproof,
                       p.proparallel, l.lanname, p.proargtypes::text AS argument_types
                  FROM pg_catalog.pg_proc p
                  JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                  JOIN pg_catalog.pg_language l ON l.oid = p.prolang
                 WHERE n.nspname = 'public' AND p.proname = :name
                """
            ),
            {"name": _FUNCTION},
        ).mappings()
    )
    if not rows:
        return None
    if len(rows) != 1:
        _refuse("Archive function name collision.")
    row = rows[0]
    expected = {
        "proowner": owner_oid,
        "prosrc": _BODY,
        "prosecdef": False,
        "proconfig": ["search_path=pg_catalog"],
        "pronargs": 0,
        "returns_trigger": True,
        "prokind": "f",
        "provolatile": "v",
        "proisstrict": False,
        "proleakproof": False,
        "proparallel": "u",
        "lanname": "plpgsql",
        "argument_types": "",
    }
    if any(row[key] != value for key, value in expected.items()):
        _refuse("Archive function identity or body drift.")
    return cast(int, row["oid"])


def _triggers(
    connection: Connection, oid: int, function_oid: int | None
) -> tuple[int, int] | None:
    rows = list(
        connection.execute(
            text(
                """
                SELECT tgname, oid, tgfoid, tgtype, tgenabled, tgisinternal,
                       tgnargs, tgargs, tgattr::text AS attributes, tgqual,
                       tgconstraint, tgconstrrelid, tgdeferrable, tginitdeferred,
                       tgparentid, tgoldtable, tgnewtable
                  FROM pg_catalog.pg_trigger
                 WHERE tgrelid = :oid AND tgname IN (:row, :statement)
                 ORDER BY tgname
                """
            ),
            {"oid": oid, "row": _ROW, "statement": _STATEMENT},
        ).mappings()
    )
    if not rows:
        return None
    if function_oid is None or len(rows) != 2:
        _refuse("Archive trigger collision or incomplete installation.")
    by_name = {row["tgname"]: row for row in rows}
    for name, event_bits in ((_ROW, 31), (_STATEMENT, 62)):
        row = by_name.get(name)
        if row is None:
            _refuse("Archive trigger name drift.")
        assert row is not None
        expected = {
            "tgfoid": function_oid,
            "tgtype": event_bits,
            "tgenabled": "A",
            "tgisinternal": False,
            "tgnargs": 0,
            "attributes": "",
            "tgqual": None,
            "tgconstraint": 0,
            "tgconstrrelid": 0,
            "tgdeferrable": False,
            "tginitdeferred": False,
            "tgparentid": 0,
            "tgoldtable": None,
            "tgnewtable": None,
        }
        if (
            any(row[key] != value for key, value in expected.items())
            or bytes(row["tgargs"]) != b""
        ):
            _refuse(
                "Archive trigger function, events, arguments or enabled state drift."
            )
    return cast(int, by_name[_ROW]["oid"]), cast(int, by_name[_STATEMENT]["oid"])


def _grants(connection: Connection, oid: int, owner_oid: int) -> list[RowMapping]:
    return list(
        connection.execute(
            text(
                """
                SELECT a.grantee, r.rolname, a.privilege_type, a.is_grantable,
                       NULL::text AS column_name
                  FROM pg_catalog.pg_class c
                  CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(c.relacl, pg_catalog.acldefault('r', c.relowner))) a
                  LEFT JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
                 WHERE c.oid = :oid AND a.grantee <> :owner
                   AND a.privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
                UNION ALL
                SELECT a.grantee, r.rolname, a.privilege_type, a.is_grantable,
                       att.attname AS column_name
                  FROM pg_catalog.pg_attribute att
                  CROSS JOIN LATERAL pg_catalog.aclexplode(att.attacl) a
                  LEFT JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
                 WHERE att.attrelid = :oid AND att.attnum > 0 AND NOT att.attisdropped
                   AND a.grantee <> :owner AND a.privilege_type IN ('INSERT', 'UPDATE')
                 ORDER BY grantee, column_name, privilege_type, is_grantable
                """
            ),
            {"oid": oid, "owner": owner_oid},
        ).mappings()
    )


def _install_legacy_archive_fence(
    db: Session, *, request: ArchiveFenceRequest
) -> ArchiveFenceStagedObservation:
    """Stage permanent archive guards; caller alone completes the transaction.

    No helper/view/FK/privileged-function closure, retained-column contract,
    drain, seal, receipt or global activation is proved by this observation.
    """
    if not isinstance(request, ArchiveFenceRequest):
        _refuse("Archive staging requires its typed closed request.")
    connection = _root(db)
    root = db.get_transaction()
    transaction = connection.get_transaction()
    identity = _posture(connection)
    owner_oid = cast(int, identity["owner_oid"])
    before = _relations(connection, owner_oid)
    # One deterministic lock statement locks all 44 before any DDL or ACL change.
    connection.exec_driver_sql(
        "LOCK TABLE "
        + ", ".join("public." + _quote(r.value) for r in _RELATIONS)
        + " IN ACCESS EXCLUSIVE MODE"
    )
    after = _relations(connection, owner_oid)
    if [r["oid"] for r in before] != [r["oid"] for r in after]:
        _refuse("Archive relation identity changed while acquiring locks.")
    function_oid = _function(connection, owner_oid)
    guards = [
        _triggers(connection, cast(int, row["oid"]), function_oid) for row in after
    ]
    replay = function_oid is not None
    if replay != all(guard is not None for guard in guards) or (
        not replay and any(guards)
    ):
        _refuse("Partial archive installation is not repairable by this component.")
    grants = [_grants(connection, cast(int, row["oid"]), owner_oid) for row in after]
    if replay and any(grants):
        _refuse("Archive ACL drift refuses exact replay.")
    if not replay:
        connection.exec_driver_sql(
            f"CREATE FUNCTION public.{_quote(_FUNCTION)}() RETURNS trigger "
            f"LANGUAGE plpgsql SECURITY INVOKER SET search_path TO pg_catalog AS $archive${_BODY}$archive$"
        )
        for relation, observed in zip(_RELATIONS, grants, strict=True):
            table = "public." + _quote(relation.value)
            for name, events, level in (
                (_ROW, "INSERT OR UPDATE OR DELETE", "ROW"),
                (_STATEMENT, "INSERT OR UPDATE OR DELETE OR TRUNCATE", "STATEMENT"),
            ):
                connection.exec_driver_sql(
                    f"CREATE TRIGGER {_quote(name)} BEFORE {events} ON {table} "
                    f"FOR EACH {level} EXECUTE FUNCTION public.{_quote(_FUNCTION)}()"
                )
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {_quote(name)}"
                )
            # PUBLIC is included even when no explicit PUBLIC DML ACL was observed.
            grantees = {"PUBLIC"}
            for grant in observed:
                if grant["grantee"] != 0 and grant["rolname"] is None:
                    _refuse("Unknown archive ACL grantee.")
                grantees.add(
                    "PUBLIC"
                    if grant["grantee"] == 0
                    else _quote(cast(str, grant["rolname"]))
                )
            for grantee in sorted(grantees):
                connection.exec_driver_sql(
                    f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE {table} FROM {grantee} RESTRICT"
                )
            for grant in observed:
                column = cast(str | None, grant["column_name"])
                if column is not None:
                    privilege = cast(str, grant["privilege_type"])
                    if privilege not in _DML:
                        _refuse("Unknown archive mutation privilege.")
                    grantee = (
                        "PUBLIC"
                        if grant["grantee"] == 0
                        else _quote(cast(str, grant["rolname"]))
                    )
                    connection.exec_driver_sql(
                        f"REVOKE {privilege} ({_quote(column)}) ON TABLE {table} FROM {grantee} RESTRICT"
                    )
    # Re-observe actual catalog, never attest from emitted DDL or caller evidence.
    verified_identity = _posture(connection)
    verified = _relations(connection, owner_oid)
    verified_function = _function(connection, owner_oid)
    if verified_identity != identity or verified_function is None:
        _refuse("Archive transaction or role identity drift.")
    observations: list[ArchiveRelationObservation] = []
    for relation, row in zip(_RELATIONS, verified, strict=True):
        oid = cast(int, row["oid"])
        guard = _triggers(connection, oid, verified_function)
        if guard is None or _grants(connection, oid, owner_oid):
            _refuse("Archive catalog verification failed.")
        observations.append(
            ArchiveRelationObservation(
                relation, RelationOid(oid), TriggerOid(guard[0]), TriggerOid(guard[1])
            )
        )
    if (
        db.get_transaction() is not root
        or root is None
        or not root.is_active
        or db.in_nested_transaction()
        or connection.get_transaction() is not transaction
        or connection.in_nested_transaction()
        or _root(db) is not connection
    ):
        _refuse("Archive staging did not retain the exact owner root.")
    db.flush()
    digest_payload = [
        request.revision.value,
        identity["database_oid"],
        owner_oid,
        identity["runtime_oid"],
        verified_function,
        _BODY,
        [
            (r.relation.value, r.oid, r.row_guard_oid, r.statement_guard_oid)
            for r in observations
        ],
    ]
    digest = hashlib.sha256(
        b"dotmac.billing.archive-catalog.v1\0"
        + json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    context = current_command_context(db)
    if not replay:
        stage_owner_output(
            db,
            OwnerOutputEnvelope(
                event_type=EventType.billing_legacy_archive_fence_staged,
                producer_owner="runtime.billing_legacy_archive_fence",
                source_kind="archive_fence_component",
                source_id=context.command_id,
            ),
            ArchiveFenceAuditPayload(
                request.revision,
                len(observations),
                CatalogDigest(digest),
                context.command_id,
            ).serialize(),
            context=context,
        )
    return ArchiveFenceStagedObservation(
        revision=request.revision,
        command_id=current_command_context(db).command_id,
        database_oid=DatabaseOid(cast(int, identity["database_oid"])),
        backend_pid=cast(int, identity["backend_pid"]),
        transaction_id=TransactionId(cast(str, identity["transaction_id"])),
        owner_oid=RoleOid(owner_oid),
        runtime_oid=RoleOid(cast(int, identity["runtime_oid"])),
        function_oid=FunctionOid(verified_function),
        relations=tuple(observations),
        catalog_digest=CatalogDigest(digest),
        replay_observed=replay,
    )
