"""Private retained-column participant of the existing legacy archive fence owner.

The future final owner must prelock the global union before final observation.
Manual SQL savepoints are forbidden by that owner; no activation is wired here.
"""

from __future__ import annotations

import hashlib
import json
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.orm import Session

from app.services.billing.legacy_archive_fence import _posture, _quote, _root
from app.services.billing.legacy_archive_fence_contracts import (
    CatalogDigest,
    DatabaseOid,
    FunctionOid,
    RelationOid,
    RoleOid,
    TransactionId,
    TriggerOid,
)
from app.services.billing.legacy_privilege_closure import (
    ClosurePopulation,
    _close_attachment_privileges,
    _lock_privilege_population,
    _observe_privilege_closure,
    _verify_attachment_denial,
    _verify_replay_digest,
)
from app.services.billing.legacy_retained_columns_contracts import (
    RetainedColumnsAuditPayload,
    RetainedColumnsRequest,
    RetainedColumnsStagedObservation,
    RetainedRelation,
    RetainedRelationObservation,
)
from app.services.domain_errors import DomainError
from app.services.events.owner_outputs import OwnerOutputEnvelope, stage_owner_output
from app.services.events.types import EventType
from app.services.owner_commands import current_command_context

_RELATIONS = tuple(sorted(RetainedRelation, key=lambda r: r.value))
_ROW = "dotmac_retained_final_row_v1"
_DELETE = "dotmac_retained_delete_v1"
_TRUNCATE = "dotmac_retained_truncate_v1"


class RetainedColumnsRefused(DomainError):
    """Must escape and roll back the complete calling owner transaction."""


def _refuse(message: str) -> None:
    raise RetainedColumnsRefused(
        code="billing_retained_columns.refused", message=message
    )


def _body(relation: RetainedRelation) -> str:
    column = "balance" if relation is RetainedRelation.BILLING_ACCOUNTS else "deposit"
    absent_new = (
        "NEW.balance IS NOT DISTINCT FROM 0"
        if column == "balance"
        else "NEW.deposit IS NULL"
    )
    absent_old = (
        "OLD.balance IS NOT DISTINCT FROM 0"
        if column == "balance"
        else "OLD.deposit IS NULL"
    )
    return (
        "BEGIN IF TG_OP = 'TRUNCATE' THEN "
        "RAISE EXCEPTION 'legacy retained monetary value is immutable' USING ERRCODE = '42501'; "
        f"ELSIF TG_OP = 'DELETE' THEN IF {absent_old} THEN RETURN OLD; END IF; "
        f"ELSIF TG_OP = 'INSERT' THEN IF {absent_new} THEN RETURN NEW; END IF; "
        f"ELSIF TG_OP = 'UPDATE' THEN IF NEW.id IS NOT DISTINCT FROM OLD.id AND NEW.{column} IS NOT DISTINCT FROM OLD.{column} THEN RETURN NEW; END IF; "
        "END IF; RAISE EXCEPTION 'legacy retained monetary value is immutable' USING ERRCODE = '42501'; END;"
    )


def _name(relation: RetainedRelation) -> str:
    return "dotmac_retained_" + relation.value + "_v1"


def _relations(connection: Connection, owner_oid: int) -> list[RowMapping]:
    result: list[RowMapping] = []
    for relation in _RELATIONS:
        column = (
            "balance" if relation is RetainedRelation.BILLING_ACCOUNTS else "deposit"
        )
        rows = list(
            connection.execute(
                text("""
            SELECT c.oid, c.relowner, c.relkind, c.relispartition,
                   pg_catalog.to_regclass(:name)::oid = c.oid AS unshadowed,
                   NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i WHERE i.inhrelid=c.oid OR i.inhparent=c.oid) AS ordinary,
                   a.atttypid='pg_catalog.numeric'::regtype AND a.atttypmod=786438 AND a.attnotnull=:notnull AS money_shape,
                   id.atttypid='pg_catalog.uuid'::regtype AND id.attnotnull AND
                     EXISTS (SELECT 1 FROM pg_catalog.pg_constraint p WHERE p.conrelid=c.oid AND p.contype='p' AND p.conkey=ARRAY[id.attnum]::smallint[]) AS id_shape
              FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
              JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attname=:column AND NOT a.attisdropped
              JOIN pg_catalog.pg_attribute id ON id.attrelid=c.oid AND id.attname='id' AND NOT id.attisdropped
             WHERE n.nspname='public' AND c.relname=:name
        """),
                {
                    "name": relation.value,
                    "column": column,
                    "notnull": column == "balance",
                },
            ).mappings()
        )
        if (
            len(rows) != 1
            or rows[0]["relowner"] != owner_oid
            or rows[0]["relkind"] != "r"
            or rows[0]["relispartition"]
            or not all(
                rows[0][key]
                for key in ("unshadowed", "ordinary", "money_shape", "id_shape")
            )
        ):
            _refuse("Retained table ownership, shape or namespace drift.")
        result.append(rows[0])
    return result


def _function(
    connection: Connection, relation: RetainedRelation, owner_oid: int
) -> int | None:
    rows = list(
        connection.execute(
            text("""
        SELECT p.oid, p.proowner, p.prosrc, p.prosecdef, p.proconfig, p.pronargs,
               p.prorettype='pg_catalog.trigger'::regtype AS returns_trigger,
               p.prokind, p.provolatile, p.proisstrict, p.proleakproof, p.proparallel,
               l.lanname, p.proargtypes::text AS argument_types
          FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
          JOIN pg_catalog.pg_language l ON l.oid=p.prolang
         WHERE n.nspname='public' AND p.proname=:name
    """),
            {"name": _name(relation)},
        ).mappings()
    )
    if not rows:
        return None
    expected = {
        "proowner": owner_oid,
        "prosrc": _body(relation),
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
    if len(rows) != 1 or any(rows[0][key] != value for key, value in expected.items()):
        _refuse("Retained function collision or catalog drift.")
    return cast(int, rows[0]["oid"])


def _guards(
    connection: Connection, oid: int, function_oid: int | None
) -> tuple[int, int, int] | None:
    rows = list(
        connection.execute(
            text("""
        SELECT oid, tgname, tgfoid, tgtype, tgenabled, tgisinternal, tgnargs, tgargs,
               tgattr::text AS attributes, tgqual, tgconstraint, tgconstrrelid,
               tgdeferrable, tginitdeferred, tgparentid, tgoldtable, tgnewtable
          FROM pg_catalog.pg_trigger WHERE tgrelid=:oid AND tgname IN (:row,:delete,:truncate)
    """),
            {"oid": oid, "row": _ROW, "delete": _DELETE, "truncate": _TRUNCATE},
        ).mappings()
    )
    if not rows:
        return None
    if function_oid is None or len(rows) != 3:
        _refuse("Retained guard collision or partial installation.")
    by_name = {row["tgname"]: row for row in rows}
    for name, bits in ((_ROW, 21), (_DELETE, 11), (_TRUNCATE, 34)):
        row = by_name.get(name)
        expected = {
            "tgfoid": function_oid,
            "tgtype": bits,
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
            row is None
            or any(row[key] != value for key, value in expected.items())
            or bytes(row["tgargs"]) != b""
        ):
            _refuse(
                "Retained guard timing, function, arguments or enabled state drift."
            )
    return (
        cast(int, by_name[_ROW]["oid"]),
        cast(int, by_name[_DELETE]["oid"]),
        cast(int, by_name[_TRUNCATE]["oid"]),
    )


def _truncate_grants(
    connection: Connection, oid: int, owner_oid: int
) -> list[RowMapping]:
    return list(
        connection.execute(
            text("""
        SELECT a.grantee, r.rolname, a.is_grantable FROM pg_catalog.pg_class c
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(c.relacl, pg_catalog.acldefault('r',c.relowner))) a
        LEFT JOIN pg_catalog.pg_roles r ON r.oid=a.grantee
        WHERE c.oid=:oid AND a.grantee<>:owner AND a.privilege_type='TRUNCATE'
        ORDER BY a.grantee, a.is_grantable
    """),
            {"oid": oid, "owner": owner_oid},
        ).mappings()
    )


def _install_legacy_retained_columns_fence(
    db: Session, *, request: RetainedColumnsRequest
) -> RetainedColumnsStagedObservation:
    """Stage only closed balance/deposit policy. Caller owns rollback and global closure."""
    if not isinstance(request, RetainedColumnsRequest):
        _refuse("Retained staging requires its typed closed request.")
    connection = _root(db)
    root, transaction = db.get_transaction(), connection.get_transaction()
    bind = db.get_bind()
    for relation in _RELATIONS:
        clause = text("SELECT 1 FROM public." + _quote(relation.value))  # noqa: S608 -- closed enum
        if (
            db.get_bind(clause=clause) is not bind
            or db.connection(bind_arguments={"clause": clause}) is not connection
        ):
            _refuse("Retained objects must share the exact audit and owner connection.")
    identity = _posture(connection)
    owner_oid = cast(int, identity["owner_oid"])
    before = _relations(connection, owner_oid)
    _lock_privilege_population(connection, population=ClosurePopulation.RETAINED)
    after = _relations(connection, owner_oid)
    if [row["oid"] for row in before] != [row["oid"] for row in after]:
        _refuse("Retained relation identity changed during lock acquisition.")
    functions = [_function(connection, relation, owner_oid) for relation in _RELATIONS]
    guards = [
        _guards(connection, cast(int, row["oid"]), function)
        for row, function in zip(after, functions, strict=True)
    ]
    replay = all(function is not None for function in functions)
    if (
        any(function is not None for function in functions) != replay
        or any(guard is not None for guard in guards) != replay
        or replay != all(guard is not None for guard in guards)
    ):
        _refuse("Partial retained installation refuses repair.")
    grants = [
        _truncate_grants(connection, cast(int, row["oid"]), owner_oid) for row in after
    ]
    closure_before = _observe_privilege_closure(
        connection, population=ClosurePopulation.RETAINED, owner_oid=owner_oid
    )
    if replay:
        _verify_attachment_denial(connection, observation=closure_before)
    if replay and any(grants):
        _refuse("Retained TRUNCATE ACL drift.")
    if not replay:
        _close_attachment_privileges(connection, observation=closure_before)
        for relation, observed in zip(_RELATIONS, grants, strict=True):
            table, function = (
                "public." + _quote(relation.value),
                "public." + _quote(_name(relation)),
            )
            connection.exec_driver_sql(
                f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path TO pg_catalog AS $retained${_body(relation)}$retained$"
            )
            for name, timing, events, level in (
                (_ROW, "AFTER", "INSERT OR UPDATE", "ROW"),
                (_DELETE, "BEFORE", "DELETE", "ROW"),
                (_TRUNCATE, "BEFORE", "TRUNCATE", "STATEMENT"),
            ):
                connection.exec_driver_sql(
                    f"CREATE TRIGGER {_quote(name)} {timing} {events} ON {table} FOR EACH {level} EXECUTE FUNCTION {function}()"
                )
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {_quote(name)}"
                )
            grantees = {"PUBLIC"}
            for grant in observed:
                if grant["grantee"] != 0 and grant["rolname"] is None:
                    _refuse("Unknown retained ACL grantee.")
                grantees.add(
                    "PUBLIC"
                    if grant["grantee"] == 0
                    else _quote(cast(str, grant["rolname"]))
                )
            for grantee in sorted(grantees):
                connection.exec_driver_sql(
                    f"REVOKE TRUNCATE ON TABLE {table} FROM {grantee} RESTRICT"
                )
    verified = _relations(connection, owner_oid)
    observations: list[RetainedRelationObservation] = []
    for relation, row in zip(_RELATIONS, verified, strict=True):
        oid = cast(int, row["oid"])
        function = _function(connection, relation, owner_oid)
        guard = _guards(connection, oid, function)
        if (
            function is None
            or guard is None
            or _truncate_grants(connection, oid, owner_oid)
        ):
            _refuse("Retained catalog verification failed.")
        observations.append(
            RetainedRelationObservation(
                relation,
                RelationOid(oid),
                FunctionOid(function),
                TriggerOid(guard[0]),
                TriggerOid(guard[1]),
                TriggerOid(guard[2]),
            )
        )
    if (
        _posture(connection) != identity
        or db.get_transaction() is not root
        or root is None
        or not root.is_active
        or connection.get_transaction() is not transaction
        or _root(db) is not connection
    ):
        _refuse("Retained component lost its exact owner root or identity.")
    closure_after = _observe_privilege_closure(
        connection, population=ClosurePopulation.RETAINED, owner_oid=owner_oid
    )
    _verify_attachment_denial(connection, observation=closure_after)
    if closure_after.catalog != closure_before.catalog:
        # ACL changes are expected only on first installation. The final digest
        # binds the observed post-closure catalog, never the emitted REVOKEs.
        if replay:
            _refuse("Privilege/helper catalog changed during exact replay.")
    db.flush()
    digest_payload = [
        request.revision.value,
        closure_after.catalog,
        identity["database_oid"],
        owner_oid,
        identity["runtime_oid"],
        [
            (
                r.relation.value,
                r.oid,
                r.function_oid,
                r.row_guard_oid,
                r.delete_guard_oid,
                r.truncate_guard_oid,
                _body(r.relation),
            )
            for r in observations
        ],
    ]
    digest = CatalogDigest(
        hashlib.sha256(
            b"dotmac.billing.retained-catalog.v1\0"
            + json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    if replay:
        _verify_replay_digest(
            connection, population=ClosurePopulation.RETAINED, digest=str(digest)
        )
    context = current_command_context(db)
    if not replay:
        stage_owner_output(
            db,
            OwnerOutputEnvelope(
                event_type=EventType.billing_legacy_retained_columns_staged,
                producer_owner="runtime.billing_legacy_archive_fence",
                source_kind="retained_columns_component",
                source_id=context.command_id,
            ),
            RetainedColumnsAuditPayload(
                request.revision, len(observations), digest, context.command_id
            ).serialize(),
            context=context,
        )
    return RetainedColumnsStagedObservation(
        request.revision,
        context.command_id,
        DatabaseOid(cast(int, identity["database_oid"])),
        cast(int, identity["backend_pid"]),
        TransactionId(cast(str, identity["transaction_id"])),
        RoleOid(owner_oid),
        RoleOid(cast(int, identity["runtime_oid"])),
        tuple(observations),
        digest,
        replay,
    )
