"""Disposable-fixture Kernel entry participant; never an operational handoff.

Caller owns the PostgreSQL root transaction and must roll it back. No CLI,
connection factory, import policy or commit authority exists here.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import distribution
from pathlib import Path

from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.runtime.migration import MigrationContext, RevisionStep
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Connection

KERNEL_VERSION = "0.1.0a105"
KERNEL_SOURCE_COMMIT = "2f5abe9101c9aef48f23977cad81a3e499672287"
KERNEL_HEAD = "0028_machine_attribution"
KERNEL_VERSION_TABLE = "dotmac_kernel_alembic_version"
ESCROW_SCHEMA = "sub_kernel_entry_fixture_escrow"

# Git blob identities from the released a105 tag, not a mutable checkout.
# Source identities do not substitute for the locked wheel digest.
KERNEL_MIGRATION_BLOBS: tuple[tuple[str, str], ...] = (
    (
        "20260504_0001_initial_tenant_schema.py",
        "80144d19cb5eaa86d40c3bbb27c1321d2cd8113b",
    ),
    ("20260717_0002_settings_table.py", "65c5e9e0ecb739eebfd8139ad6b952cadcd1145f"),
    ("20260717_0003_party_identity.py", "68f5431183b374773f10431271df155e769642e9"),
    ("20260717_0004_custom_fields.py", "387883dd5389d74cf510fd06951026708eae3b03"),
    (
        "20260718_0005_single_email_authority.py",
        "f31414d291f3db09cfc79996eb26d87421bb3fdb",
    ),
    (
        "20260718_0006_display_setting_domain.py",
        "063151fbf9d2b6ad5703c08c535c25d65fa7b9a4",
    ),
    ("20260730_0007_platform_identity.py", "20f629de2d56271b797b24e06372cae25f8da9e6"),
    ("20260730_0008_outbox_inbox.py", "2b702b1275944f1fe8142f59c872d1bfd1e654e8"),
    (
        "20260730_0009_platform_audit_inbox.py",
        "5b527d329331e484a71dcfab13d5d651dea695bd",
    ),
    (
        "20260731_0010_tenant_entitlements.py",
        "01a27f4e4dc062c9a41016020195f982099accac",
    ),
    (
        "20260731_0011_outbox_relay_leasing.py",
        "b576b4d8cbde7deae933e6a3afb218605608d086",
    ),
    ("20260731_0012_platform_outbox.py", "f078f7003ac6d125e835429115bdc8fd5b9d10a0"),
    (
        "20260807_0013_feature_flag_overrides.py",
        "94358ca774d0fbaee9094d4daf187697455722fe",
    ),
    (
        "20260807_0014_open_setting_domains.py",
        "6ef5803b940de1fa036f39e1d20bd295e0281188",
    ),
    ("20260808_0015_open_value_types.py", "781e4febae4dd0698fe0ed244074de52fb3aad39"),
    (
        "20260808_0016_setting_scope_depth.py",
        "48887a8a6c3504111002133ae287338b32d97928",
    ),
    ("20260808_0017_history_actor.py", "f292818e746139d23f3043437cffc5aa479df389"),
    (
        "20260810_0018_idempotency_one_owner.py",
        "47af082867dcc92a8926cf1499c7bdb85acc2fbe",
    ),
    (
        "20260810_0019_communication_consent.py",
        "6cff78773b6fad3495e315800d44a5003fd8d688",
    ),
    ("20260810_0020_delivery_receipts.py", "889315111402a06a2a39ce8c9e3f6f982e08a6fa"),
    (
        "20260811_0021_setting_scope_alignment.py",
        "e192e0a12becbc8cf59988b780e32203c8dd884b",
    ),
    ("20260812_0022_party_role_grants.py", "8c5454c3b11c5546336c6f2ef0a0b8e258f799fb"),
    (
        "20260812_0023_audit_actor_and_forensics.py",
        "7566608045ed1936ca80fdbc0cec1de8f9335560",
    ),
    (
        "20260814_0024_external_identity_bindings.py",
        "04ceed31ca6dbdbff47d7f5bf362144336f039d3",
    ),
    ("20260816_0025_session_provenance.py", "d7998677eca4199b1d31aabeb1db507ba0279ace"),
    ("20260816_0026_platform_audit_log.py", "832ac204e57da03180f6381a296f372b8e5a0325"),
    ("20260822_0027_machine_credential.py", "5c2a281886e8ceacee47d96ef704ced98e1f83a8"),
    (
        "20260824_0028_machine_attribution.py",
        "e9bfde751fc684a0313f39a21151f608ecf0a355",
    ),
)

# Includes transient 0003 party_roles, renamed by Kernel 0022.
COLLISION_TABLES = (
    "audit_events",
    "communication_suppressions",
    "domain_setting_history",
    "domain_settings",
    "idempotency_records",
    "machine_credentials",
    "outbox_events",
    "parties",
    "party_roles",
    "platform_idempotency_records",
    "platform_outbox_events",
    "roles",
    "tenant_domains",
    "tenants",
    "user_credentials",
)
RELAY_SIGNATURES = (
    "claim_outbox_batch(text,integer,integer)",
    "settle_outbox_event(uuid,text,text,timestamp with time zone,integer,text)",
    "claim_platform_outbox_batch(text,integer,integer)",
    "settle_platform_outbox_event(uuid,text,text,timestamp with time zone,integer,text)",
)


class EntryPhase(StrEnum):
    LOCKED = "locked"
    ESCROWED = "escrowed"
    KERNEL_APPLIED = "kernel_applied"


class KernelEntryRefused(RuntimeError):
    """Fixture, artifact, transaction or catalog differs from the entry plan."""


class InjectedEntryFailure(RuntimeError):
    """Hosted proof fault at an explicit phase boundary."""


@dataclass(frozen=True, slots=True)
class RelationIdentity:
    name: str
    oid: int
    row_type_oid: int


@dataclass(frozen=True, slots=True)
class RelayIdentity:
    signature: str
    oid: int
    result_type_oid: int


@dataclass(frozen=True, slots=True)
class FixtureCatalog:
    database: str
    sub_heads: tuple[str, ...]
    relations: tuple[RelationIdentity, ...]
    relays: tuple[RelayIdentity, ...]
    # Definitions/ACLs stay memory-only, never in diagnostic repr.
    facts: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class KernelEntryFixturePlan:
    catalog: FixtureCatalog
    kernel_version: str = KERNEL_VERSION
    kernel_source_commit: str = KERNEL_SOURCE_COMMIT


@dataclass(frozen=True, slots=True)
class KernelEntryObserved:
    """Uncommitted observations, NOT adoption or an acceptance receipt."""

    kernel_heads: tuple[str, ...]
    sub_heads: tuple[str, ...]
    predecessor_relations: tuple[RelationIdentity, ...]
    canonical_relations: tuple[RelationIdentity, ...]
    predecessor_relays: tuple[RelayIdentity, ...]


def _require_fixture_transaction(connection: Connection) -> str:
    if (
        connection.dialect.name != "postgresql"
        or not connection.in_transaction()
        or connection.in_nested_transaction()
        or connection.get_isolation_level() == "AUTOCOMMIT"
        or connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT"
        or getattr(connection.connection.driver_connection, "autocommit", False)
    ):
        raise KernelEntryRefused(
            "A caller-owned PostgreSQL root transaction is required"
        )
    database = str(connection.scalar(text("SELECT current_database()")))
    if not re.search(
        r"(?:^|_)(?:test|pytest|ci|e2e|migration|rehearsal)(?:_|$)", database
    ):
        raise KernelEntryRefused(
            "Entry prototype accepts only disposable fixture databases"
        )
    if connection.scalar(text("SELECT current_schema()")) != "public":
        raise KernelEntryRefused(
            "Kernel entry requires the reviewed public compatibility namespace"
        )
    return database


def _installed_versions() -> Path:
    installed = distribution("dotmac-kernel")
    if installed.version != KERNEL_VERSION:
        raise KernelEntryRefused(
            "Installed Kernel differs from Sub's reviewed a105 pin"
        )
    versions = Path(installed.locate_file("dotmac_kernel/migrations/versions"))
    if {p.name for p in versions.glob("*.py")} != {
        n for n, _ in KERNEL_MIGRATION_BLOBS
    }:
        raise KernelEntryRefused(
            "Installed Kernel migration file set differs from a105"
        )
    for name, expected in KERNEL_MIGRATION_BLOBS:
        body = (versions / name).read_bytes()
        blob = hashlib.sha1(
            b"blob " + str(len(body)).encode("ascii") + b"\0" + body,
            usedforsecurity=False,
        ).hexdigest()
        if blob != expected:
            raise KernelEntryRefused(
                "Installed Kernel migration bytes differ from released source"
            )
    return versions


def _created_table_names(versions: Path) -> frozenset[str]:
    """Inventory verified literal DDL; Alembic executes the real revisions."""
    names: set[str] = set()
    for path in versions.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name) and isinstance(
                        node.value.value, str
                    ):
                        constants[target.id] = node.value.value
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "op"
                and node.func.attr == "create_table"
                and node.args
            ):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
            elif isinstance(first, ast.Name) and first.id in constants:
                names.add(constants[first.id])
            else:
                raise KernelEntryRefused("Unreviewed dynamic Kernel table creation")
    # Verified 0018 renames its two inbox ledgers through fixed SQL.
    names.update(("idempotency_records", "platform_idempotency_records"))
    return frozenset(names)


def _require_dependency_scope(connection: Connection) -> None:
    """Refuse an unaccounted callable/view path to the predecessor objects.

    Incoming FKs retain their exact old OIDs; this entry probe never transfers
    them. Views and catalog-known callable paths need their own disposition,
    including parsed scalar SQL bodies and transitive relay wrappers. A newly
    observed plan cannot silently approve an additional callable dependency.
    """
    names = list(COLLISION_TABLES)
    if connection.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_depend d "
            "JOIN pg_class target ON target.oid=d.refobjid "
            "JOIN pg_namespace n ON n.oid=target.relnamespace "
            "JOIN pg_rewrite rule ON d.classid='pg_rewrite'::regclass AND rule.oid=d.objid "
            "JOIN pg_class dependent ON dependent.oid=rule.ev_class "
            "WHERE d.refclassid='pg_class'::regclass AND n.nspname='public' "
            "AND target.relname=ANY(:names) AND dependent.oid<>target.oid)"
        ),
        {"names": names},
    ):
        raise KernelEntryRefused(
            "An incoming view/rule needs an explicit escrow disposition"
        )
    permitted = [f"public.{signature}" for signature in RELAY_SIGNATURES]
    if connection.scalar(
        text(
            "WITH RECURSIVE "
            "allowed(oid) AS (SELECT to_regprocedure(v)::oid "
            "FROM unnest(CAST(:allowed AS text[])) v), "
            "relations(oid,row_type_oid) AS (SELECT c.oid,c.reltype "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relname=ANY(:names)), "
            "roots(classid,objid) AS ("
            "SELECT 'pg_class'::regclass::oid,oid FROM relations UNION "
            "SELECT 'pg_type'::regclass::oid,row_type_oid FROM relations UNION "
            "SELECT 'pg_proc'::regclass::oid,oid FROM allowed), "
            "callable_paths(classid,objid) AS (SELECT classid,objid FROM roots UNION "
            "SELECT d.classid,d.objid FROM pg_depend d "
            "JOIN callable_paths parent "
            "ON parent.classid=d.refclassid AND parent.objid=d.refobjid "
            "WHERE d.classid IN ('pg_proc'::regclass,'pg_type'::regclass)) "
            "SELECT EXISTS (SELECT 1 FROM callable_paths path "
            "WHERE path.classid='pg_proc'::regclass "
            "AND NOT EXISTS (SELECT 1 FROM allowed WHERE allowed.oid=path.objid))"
        ),
        {"names": names, "allowed": permitted},
    ):
        raise KernelEntryRefused(
            "An unaccounted catalog callable path needs an escrow disposition"
        )


def _relations(connection: Connection, schema: str) -> tuple[RelationIdentity, ...]:
    return tuple(
        RelationIdentity(str(r[0]), int(r[1]), int(r[2]))
        for r in connection.execute(
            text(
                "SELECT c.relname,c.oid,c.reltype FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=:schema AND c.relkind IN ('r','p') ORDER BY c.relname"
            ),
            {"schema": schema},
        )
    )


def _relays(connection: Connection, schema: str) -> tuple[RelayIdentity, ...]:
    result: list[RelayIdentity] = []
    for signature in RELAY_SIGNATURES:
        row = connection.execute(
            text("SELECT oid,prorettype FROM pg_proc WHERE oid=to_regprocedure(:name)"),
            {"name": f"{schema}.{signature}"},
        ).one_or_none()
        if row is None:
            raise KernelEntryRefused("A reviewed relay signature is missing")
        result.append(RelayIdentity(signature, int(row[0]), int(row[1])))
    return tuple(result)


def _catalog_facts(connection: Connection) -> tuple[str, ...]:
    """Independent catalog inventory, never proposed-move-list enumeration.

    Includes SQL bodies (pg_depend cannot see string references), object/column
    grants, policies, rules, triggers, defaults and role posture. No data rows,
    password catalog or runtime credentials are read.
    """
    queries = (
        "SELECT 'relation:' || row_to_json(x)::text FROM ("
        "SELECT c.oid,n.nspname,c.relname,c.relkind,c.reltype,c.relowner,c.relacl,"
        "c.relrowsecurity,c.relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' ORDER BY c.oid) x",
        "SELECT 'column:' || row_to_json(x)::text FROM ("
        "SELECT a.attrelid,a.attnum,a.attname,a.atttypid,a.atttypmod,a.attnotnull,"
        "a.attacl,a.attidentity,a.attgenerated FROM pg_attribute a "
        "JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE a.attnum>0 AND NOT a.attisdropped AND n.nspname !~ '^pg_' "
        "AND n.nspname <> 'information_schema' ORDER BY a.attrelid,a.attnum) x",
        "SELECT 'constraint:' || row_to_json(x)::text FROM ("
        "SELECT oid,conrelid,confrelid,conname,pg_get_constraintdef(oid) AS body "
        "FROM pg_constraint ORDER BY oid) x",
        "SELECT 'index:' || row_to_json(x)::text FROM ("
        "SELECT indexrelid,indrelid,pg_get_indexdef(indexrelid) AS body "
        "FROM pg_index ORDER BY indexrelid) x",
        "SELECT 'dependency:' || row_to_json(x)::text FROM ("
        "SELECT * FROM pg_depend "
        "ORDER BY classid,objid,objsubid,refclassid,refobjid,refobjsubid,deptype) x",
        "SELECT 'function:' || row_to_json(x)::text FROM ("
        "SELECT p.oid,p.proowner,p.proacl,p.prosecdef,p.proconfig,pg_get_functiondef(p.oid) AS body "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
        "AND p.prokind IN ('f','p') ORDER BY p.oid) x",
        "SELECT 'policy:' || row_to_json(x)::text FROM ("
        "SELECT oid,polrelid,polname,polcmd,polpermissive,polroles,"
        "pg_get_expr(polqual,polrelid) AS qual,pg_get_expr(polwithcheck,polrelid) AS check_expr "
        "FROM pg_policy ORDER BY oid) x",
        "SELECT 'rule:' || row_to_json(x)::text FROM ("
        "SELECT oid,ev_class,pg_get_ruledef(oid) AS body FROM pg_rewrite ORDER BY oid) x",
        "SELECT 'trigger:' || row_to_json(x)::text FROM ("
        "SELECT oid,tgrelid,tgfoid,tgenabled,pg_get_triggerdef(oid) AS body "
        "FROM pg_trigger ORDER BY oid) x",
        "SELECT 'event_trigger:' || row_to_json(x)::text FROM ("
        "SELECT oid,evtname,evtevent,evtowner,evtfoid,evtenabled,evttags "
        "FROM pg_event_trigger ORDER BY oid) x",
        "SELECT 'default:' || row_to_json(x)::text FROM ("
        "SELECT oid,adrelid,adnum,pg_get_expr(adbin,adrelid) AS body "
        "FROM pg_attrdef ORDER BY oid) x",
        "SELECT 'role:' || row_to_json(x)::text FROM ("
        "SELECT oid,rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,"
        "rolreplication,rolbypassrls,rolconfig FROM pg_roles ORDER BY oid) x",
        "SELECT 'membership:' || row_to_json(x)::text FROM ("
        "SELECT roleid,member,grantor,admin_option FROM pg_auth_members "
        "ORDER BY roleid,member,grantor) x",
    )
    return tuple(str(v) for query in queries for v in connection.scalars(text(query)))


def observe_fixture_plan(connection: Connection) -> KernelEntryFixturePlan:
    """Observe a complete disposable fixture; grants no operational authority."""
    database = _require_fixture_transaction(connection)
    versions = _installed_versions()
    if connection.scalar(
        text("SELECT to_regnamespace(:name)"), {"name": ESCROW_SCHEMA}
    ):
        raise KernelEntryRefused("Escrow exists: no replay or partial-entry repair")
    if connection.scalar(
        text("SELECT to_regclass(:name)"), {"name": f"public.{KERNEL_VERSION_TABLE}"}
    ):
        raise KernelEntryRefused("Kernel history exists: no replay or stamping")
    all_relations = _relations(connection, "public")
    collisions = _created_table_names(versions) & {r.name for r in all_relations}
    if collisions != set(COLLISION_TABLES):
        raise KernelEntryRefused(
            "Actual installed-chain/source collision closure differs"
        )
    relations = tuple(r for r in all_relations if r.name in collisions)
    _require_dependency_scope(connection)
    heads = tuple(
        str(v)
        for v in connection.scalars(
            text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
        )
    )
    if "698_credential_deactivation_reason" not in heads:
        raise KernelEntryRefused("Fixture lacks the reviewed actual Sub source head")
    return KernelEntryFixturePlan(
        FixtureCatalog(
            database,
            heads,
            relations,
            _relays(connection, "public"),
            _catalog_facts(connection),
        )
    )


def _fault(phase: EntryPhase, requested: EntryPhase | None) -> None:
    if phase is requested:
        raise InjectedEntryFailure(
            f"Injected entry fixture failure after {phase.value}"
        )


def _escrow(connection: Connection) -> None:
    quote = connection.dialect.identifier_preparer.quote
    schema = quote(ESCROW_SCHEMA)
    connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
    connection.exec_driver_sql(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC")
    # Both claim return types AND both hardcoded SECURITY DEFINER settle writers.
    for signature in RELAY_SIGNATURES:
        connection.exec_driver_sql(
            f"ALTER FUNCTION public.{signature} SET SCHEMA {schema}"
        )
        grantees = tuple(
            str(v)
            for v in connection.scalars(
                text(
                    "SELECT DISTINCT r.rolname FROM pg_proc p "
                    "CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a "
                    "JOIN pg_roles r ON r.oid=a.grantee "
                    "WHERE p.oid=to_regprocedure(:signature)"
                ),
                {"signature": f"{ESCROW_SCHEMA}.{signature}"},
            )
        )
        connection.exec_driver_sql(
            f"REVOKE ALL ON FUNCTION {schema}.{signature} FROM PUBLIC"
        )
        for grantee in grantees:
            connection.exec_driver_sql(
                f"REVOKE ALL ON FUNCTION {schema}.{signature} FROM {quote(grantee)}"
            )
    for name in COLLISION_TABLES:
        connection.exec_driver_sql(
            f"ALTER TABLE public.{quote(name)} SET SCHEMA {schema}"
        )
    # Fresh owner-only schema also fences retained column grants and implicit
    # function-owner EXECUTE. Check usable ordinary LOGIN actors, including
    # inherited privileges and SET ROLE paths to any privileged target. A
    # NOLOGIN capability alone is not an actor. Direct superuser/current-owner
    # custody containment remains a separate gate.
    accessible = connection.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_roles actor "
            "WHERE actor.rolcanlogin AND NOT actor.rolsuper "
            "AND actor.rolname <> current_user AND ("
            "has_schema_privilege(actor.oid,:schema,'USAGE') OR EXISTS ("
            "SELECT 1 FROM pg_roles target "
            "WHERE pg_has_role(actor.oid,target.oid,'SET') "
            "AND has_schema_privilege(target.oid,:schema,'USAGE'))))"
        ),
        {"schema": ESCROW_SCHEMA},
    )
    if accessible:
        raise KernelEntryRefused(
            "A non-owner ordinary role retains an escrow call path"
        )


def _apply_installed_kernel(connection: Connection, versions: Path) -> tuple[str, ...]:
    config = Config()
    config.set_main_option("script_location", str(versions.parent))
    config.set_main_option("version_locations", str(versions))
    script = ScriptDirectory.from_config(config)
    if script.get_heads() != [KERNEL_HEAD]:
        raise KernelEntryRefused("Installed Kernel has an unreviewed lineage head")

    def upgrade(
        revision: str | tuple[str, ...], _context: MigrationContext
    ) -> list[RevisionStep]:
        return script._upgrade_revs("heads", revision)

    with EnvironmentContext(
        config, script, fn=upgrade, destination_rev="heads"
    ) as environment:
        environment.configure(
            connection=connection,
            version_table=KERNEL_VERSION_TABLE,
            version_table_schema="public",
            transactional_ddl=True,
            transaction_per_migration=False,
        )
        # No begin_transaction: the supplied Connection already owns the root.
        # Direct environment installs ordinary Operations, never Sub env.py's
        # idempotent wrappers or a migration-DDL interceptor.
        environment.run_migrations()
    return tuple(
        str(v)
        for v in connection.scalars(
            text(
                f"SELECT version_num FROM public.{KERNEL_VERSION_TABLE} ORDER BY version_num"
            )
        )
    )


def rehearse_kernel_entry(
    connection: Connection,
    *,
    plan: KernelEntryFixturePlan,
    fail_after: EntryPhase | None = None,
) -> KernelEntryObserved:
    """Enter unchanged Kernel; caller retains root rollback authority.

    Stops before canonical imports/extensions. Must never be committed as a
    product handoff. Only hosted disposable fixtures call this participant.
    """
    _require_fixture_transaction(connection)
    if not isinstance(plan, KernelEntryFixturePlan) or (
        plan.kernel_version != KERNEL_VERSION
        or plan.kernel_source_commit != KERNEL_SOURCE_COMMIT
    ):
        raise KernelEntryRefused("A reviewed typed fixture entry plan is required")
    if observe_fixture_plan(connection) != plan:
        raise KernelEntryRefused("Fixture catalog changed since observation")
    connection.execute(text("SELECT set_config('lock_timeout','5s',true)"))
    quote = connection.dialect.identifier_preparer.quote
    # Lock actual relation dependencies, including original version history;
    # that history is NEVER part of the relocation list.
    lock_names = tuple(
        connection.execute(
            text(
                "SELECT n.nspname,c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
                "AND c.relkind IN ('r','p') ORDER BY n.nspname,c.relname"
            )
        )
    )
    for schema, name in lock_names:
        connection.exec_driver_sql(
            f"LOCK TABLE {quote(schema)}.{quote(name)} IN ACCESS EXCLUSIVE MODE"
        )
    if observe_fixture_plan(connection) != plan:
        raise KernelEntryRefused("Fixture catalog changed while acquiring entry locks")
    _fault(EntryPhase.LOCKED, fail_after)
    _escrow(connection)
    _fault(EntryPhase.ESCROWED, fail_after)
    heads = _apply_installed_kernel(connection, _installed_versions())
    _fault(EntryPhase.KERNEL_APPLIED, fail_after)
    predecessor = _relations(connection, ESCROW_SCHEMA)
    if (
        predecessor != plan.catalog.relations
        or _relays(connection, ESCROW_SCHEMA) != plan.catalog.relays
    ):
        raise KernelEntryRefused("Escrow lost predecessor relation or relay identity")
    canonical = tuple(
        r for r in _relations(connection, "public") if r.name in COLLISION_TABLES
    )
    if {r.name for r in canonical} != set(COLLISION_TABLES) - {"party_roles"}:
        raise KernelEntryRefused(
            "Canonical Kernel destination relations are incomplete"
        )
    if any(r.oid in {p.oid for p in predecessor} for r in canonical) or heads != (
        KERNEL_HEAD,
    ):
        raise KernelEntryRefused(
            "Canonical destinations or genuine Kernel history differ"
        )
    source_heads = tuple(
        str(v)
        for v in connection.scalars(
            text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
        )
    )
    if source_heads != plan.catalog.sub_heads:
        raise KernelEntryRefused("Kernel entry altered genuine Sub history")
    return KernelEntryObserved(
        heads, source_heads, predecessor, canonical, plan.catalog.relays
    )
