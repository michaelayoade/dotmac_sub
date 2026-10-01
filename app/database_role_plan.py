"""Pure, fail-closed review plan for Sub database object authority.

The caller supplies an explicitly complete catalog and an object-by-object
policy. This module does no catalog reads, role changes, or SQL execution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal, cast

PLAN_VERSION = 1
MAX_OBJECTS = 5000
MAX_ROLES = 128
MAX_GRANTS_PER_OBJECT = 128
MAX_STATEMENTS = 10000

ObjectKind = Literal["schema", "relation", "type", "routine"]
Classification = Literal["named", "none"]

_RELATION_KINDS = frozenset("rpSvmf")
_TYPE_KINDS = frozenset("de")
_ROUTINE_KINDS = frozenset("fpaw")
_COLUMN_RELATION_KINDS = frozenset({"r", "p", "f", "v"})
_COLUMN_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE", "REFERENCES"})
_PRIVILEGES = {
    "schema": frozenset({"USAGE", "CREATE"}),
    "table": frozenset(
        {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
    ),
    "sequence": frozenset({"USAGE", "SELECT", "UPDATE"}),
    "materialized_view": frozenset({"SELECT"}),
    "type": frozenset({"USAGE"}),
    "routine": frozenset({"EXECUTE"}),
}
_RELATION_SQL = {
    "r": ("TABLE", 40),
    "p": ("TABLE", 40),
    "f": ("FOREIGN TABLE", 40),
    "S": ("SEQUENCE", 50),
    "v": ("VIEW", 60),
    "m": ("MATERIALIZED VIEW", 60),
}
_ROUTINE_SQL = {"f": "FUNCTION", "w": "FUNCTION", "p": "PROCEDURE", "a": "AGGREGATE"}


class PlanInputError(ValueError):
    """Malformed supplied evidence; never a database or executor error."""


@dataclass(frozen=True)
class EvidenceCompleteness:
    objects_complete: bool
    relation_columns_complete: bool
    acl_complete: bool
    grant_options_complete: bool
    column_acl_complete: bool
    effective_privileges_complete: bool
    membership_complete: bool
    default_acl_complete: bool
    database_privileges_complete: bool
    database_acl_complete: bool


@dataclass(frozen=True)
class RolePosture:
    name: str
    can_login: bool
    inherits: bool
    bypass_rls: bool
    superuser: bool
    can_create_database: bool
    can_create_role: bool
    database_create: bool


@dataclass(frozen=True)
class Membership:
    member: str
    role: str
    inherits: bool
    set_option: bool
    admin_option: bool


@dataclass(frozen=True)
class Grant:
    grantee: str
    privileges: tuple[str, ...]
    grant_options: tuple[str, ...]


@dataclass(frozen=True)
class ColumnGrant:
    column: str
    grantee: str
    privileges: tuple[str, ...]
    grant_options: tuple[str, ...]


@dataclass(frozen=True)
class DefaultAcl:
    owner: str
    schema: str | None
    object_class: str
    grantee: str
    privileges: tuple[str, ...]


@dataclass(frozen=True)
class TypeName:
    schema: str
    name: str


@dataclass(frozen=True)
class CatalogObject:
    kind: ObjectKind
    schema: str | None
    name: str
    subkind: str | None
    argument_types: tuple[TypeName, ...]
    owner: str
    extension_owned: bool
    columns: tuple[str, ...]
    acl_grants: tuple[Grant, ...]
    column_grants: tuple[ColumnGrant, ...]
    effective_privileges: tuple[Grant, ...]


@dataclass(frozen=True)
class CatalogSnapshot:
    schema_version: int
    database: str
    database_owner: str
    database_acl_grants: tuple[Grant, ...]
    scope_schemas: tuple[str, ...]
    evidence: EvidenceCompleteness
    roles: tuple[RolePosture, ...]
    memberships: tuple[Membership, ...]
    default_acls: tuple[DefaultAcl, ...]
    objects: tuple[CatalogObject, ...]


@dataclass(frozen=True)
class NamedGrant:
    role: str
    privileges: tuple[str, ...]
    justification: str | None


@dataclass(frozen=True)
class NamedColumnGrant:
    column: str
    role: str
    privileges: tuple[str, ...]
    justification: str | None


@dataclass(frozen=True)
class ObjectPolicy:
    identity: str
    classification: Classification
    grants: tuple[NamedGrant, ...]
    column_grants: tuple[NamedColumnGrant, ...]


@dataclass(frozen=True)
class SourceAdminPrincipal:
    name: str
    justification: str


@dataclass(frozen=True)
class OperationalBootstrapMembership:
    member: str
    role: str
    justification: str


@dataclass(frozen=True)
class AuthorityPolicy:
    schema_version: int
    database: str
    target_owner: str
    runtime_role: str
    expected_database_owner: str
    scope_schemas: tuple[str, ...]
    approved_source_owners: tuple[str, ...]
    source_admin_principals: tuple[SourceAdminPrincipal, ...]
    operational_bootstrap_memberships: tuple[OperationalBootstrapMembership, ...]
    objects: tuple[ObjectPolicy, ...]


@dataclass(frozen=True)
class AuthorityPlan:
    schema_version: int
    status: Literal["blocked", "ready_for_review"]
    database: str
    target_owner: str
    blocked_reasons: tuple[str, ...]
    statements: tuple[str, ...]
    catalog_sha256: str
    policy_sha256: str
    plan_sha256: str


def _record(value: object, fields: frozenset[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PlanInputError(f"{label} must be an object")
    row = cast(dict[str, object], value)
    if set(row) != fields:
        raise PlanInputError(f"{label} has missing or unknown fields")
    return row


def _list(value: object, label: str, limit: int) -> list[object]:
    if not isinstance(value, list) or len(value) > limit:
        raise PlanInputError(f"{label} must be a bounded array")
    return cast(list[object], value)


def _name(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > 63
    ):
        raise PlanInputError(f"{label} must be a PostgreSQL-sized identifier")
    return value


def _text(value: object, label: str, *, max_length: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise PlanInputError(f"{label} must be bounded nonempty text")
    return value


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise PlanInputError(f"{label} must be a boolean")
    return cast(bool, value)


def _strings(value: object, label: str, limit: int) -> tuple[str, ...]:
    result = tuple(_name(item, label) for item in _list(value, label, limit))
    if len(result) != len(set(result)):
        raise PlanInputError(f"{label} contains duplicates")
    return tuple(sorted(result))


def _privileges(value: object, label: str) -> tuple[str, ...]:
    values = _strings(value, label, 16)
    if any(item != item.upper() for item in values):
        raise PlanInputError(f"{label} must use uppercase PostgreSQL privileges")
    return values


def _grant(value: object, label: str) -> Grant:
    row = _record(value, frozenset(Grant.__dataclass_fields__), label)
    privileges = _privileges(row["privileges"], label)
    grant_options = _privileges(row["grant_options"], f"{label}.grant_options")
    if not set(grant_options) <= set(privileges):
        raise PlanInputError(f"{label} grant options exceed privileges")
    return Grant(_name(row["grantee"], f"{label}.grantee"), privileges, grant_options)


def _type_name(value: object) -> TypeName:
    row = _record(value, frozenset({"schema", "name"}), "argument type")
    return TypeName(
        _name(row["schema"], "type schema"), _name(row["name"], "type name")
    )


def _catalog_object(value: object) -> CatalogObject:
    row = _record(
        value,
        frozenset(
            {
                "kind",
                "schema",
                "name",
                "subkind",
                "argument_types",
                "owner",
                "extension_owned",
                "columns",
                "acl_grants",
                "column_grants",
                "effective_privileges",
            }
        ),
        "catalog object",
    )
    kind = row["kind"]
    if kind not in ("schema", "relation", "type", "routine"):
        raise PlanInputError("unknown catalog object kind")
    schema = None if row["schema"] is None else _name(row["schema"], "object schema")
    subkind = row["subkind"]
    if subkind is not None and not isinstance(subkind, str):
        raise PlanInputError("object subkind must be text or null")
    args = tuple(
        _type_name(item) for item in _list(row["argument_types"], "argument_types", 128)
    )
    relation_columns = _strings(row["columns"], "relation columns", MAX_OBJECTS)
    if kind == "schema":
        valid = schema is None and subkind is None and not args
    elif kind == "relation":
        valid = schema is not None and subkind in _RELATION_KINDS and not args
    elif kind == "type":
        valid = schema is not None and subkind in _TYPE_KINDS and not args
    else:
        valid = schema is not None and subkind in _ROUTINE_KINDS
    if not valid or (kind != "relation" and relation_columns):
        raise PlanInputError("catalog object kind, subkind, or arguments disagree")
    acl = tuple(
        _grant(item, "acl grant")
        for item in _list(row["acl_grants"], "acl_grants", MAX_GRANTS_PER_OBJECT)
    )
    effective = tuple(
        _grant(item, "effective grant")
        for item in _list(
            row["effective_privileges"], "effective_privileges", MAX_GRANTS_PER_OBJECT
        )
    )
    column_grants = []
    for item in _list(row["column_grants"], "column_grants", MAX_GRANTS_PER_OBJECT):
        grant = _record(
            item, frozenset(ColumnGrant.__dataclass_fields__), "column grant"
        )
        privileges = _privileges(grant["privileges"], "column privileges")
        grant_options = _privileges(grant["grant_options"], "column grant options")
        if not set(grant_options) <= set(privileges):
            raise PlanInputError("column grant options exceed privileges")
        column_grants.append(
            ColumnGrant(
                _name(grant["column"], "column name"),
                _name(grant["grantee"], "column grantee"),
                privileges,
                grant_options,
            )
        )
    return CatalogObject(
        cast(ObjectKind, kind),
        schema,
        _name(row["name"], "object name"),
        cast(str | None, subkind),
        args,
        _name(row["owner"], "object owner"),
        _bool(row["extension_owned"], "extension_owned"),
        relation_columns,
        acl,
        tuple(column_grants),
        effective,
    )


def decode_catalog(value: object) -> CatalogSnapshot:
    """Decode exact JSON v1; omission is blocked rather than treated as empty."""
    row = _record(
        value,
        frozenset(
            {
                "schema_version",
                "database",
                "database_owner",
                "database_acl_grants",
                "scope_schemas",
                "evidence",
                "roles",
                "memberships",
                "default_acls",
                "objects",
            }
        ),
        "catalog",
    )
    if type(row["schema_version"]) is not int or row["schema_version"] != PLAN_VERSION:
        raise PlanInputError("unsupported catalog schema_version")
    evidence_row = _record(
        row["evidence"],
        frozenset(EvidenceCompleteness.__dataclass_fields__),
        "evidence",
    )
    evidence = EvidenceCompleteness(
        *(
            _bool(evidence_row[field], field)
            for field in EvidenceCompleteness.__dataclass_fields__
        )
    )
    roles = []
    for item in _list(row["roles"], "roles", MAX_ROLES):
        role = _record(item, frozenset(RolePosture.__dataclass_fields__), "role")
        roles.append(
            RolePosture(
                _name(role["name"], "role name"),
                _bool(role["can_login"], "can_login"),
                _bool(role["inherits"], "inherits"),
                _bool(role["bypass_rls"], "bypass_rls"),
                _bool(role["superuser"], "superuser"),
                _bool(role["can_create_database"], "can_create_database"),
                _bool(role["can_create_role"], "can_create_role"),
                _bool(role["database_create"], "database_create"),
            )
        )
    memberships = []
    for item in _list(row["memberships"], "memberships", MAX_ROLES * MAX_ROLES):
        member = _record(item, frozenset(Membership.__dataclass_fields__), "membership")
        memberships.append(
            Membership(
                _name(member["member"], "membership member"),
                _name(member["role"], "membership role"),
                _bool(member["inherits"], "membership inherits"),
                _bool(member["set_option"], "membership set_option"),
                _bool(member["admin_option"], "membership admin_option"),
            )
        )
    defaults = []
    for item in _list(row["default_acls"], "default_acls", MAX_OBJECTS):
        default = _record(
            item, frozenset(DefaultAcl.__dataclass_fields__), "default ACL"
        )
        defaults.append(
            DefaultAcl(
                _name(default["owner"], "default ACL owner"),
                None
                if default["schema"] is None
                else _name(default["schema"], "default ACL schema"),
                _name(default["object_class"], "default ACL object class"),
                _name(default["grantee"], "default ACL grantee"),
                _privileges(default["privileges"], "default ACL privileges"),
            )
        )
    objects = tuple(
        _catalog_object(item) for item in _list(row["objects"], "objects", MAX_OBJECTS)
    )
    return CatalogSnapshot(
        PLAN_VERSION,
        _name(row["database"], "database"),
        _name(row["database_owner"], "database owner"),
        tuple(
            _grant(item, "database ACL grant")
            for item in _list(
                row["database_acl_grants"],
                "database_acl_grants",
                MAX_GRANTS_PER_OBJECT,
            )
        ),
        _strings(row["scope_schemas"], "scope_schemas", MAX_OBJECTS),
        evidence,
        tuple(roles),
        tuple(memberships),
        tuple(defaults),
        objects,
    )


def decode_policy(value: object) -> AuthorityPolicy:
    row = _record(
        value,
        frozenset(AuthorityPolicy.__dataclass_fields__),
        "policy",
    )
    if type(row["schema_version"]) is not int or row["schema_version"] != PLAN_VERSION:
        raise PlanInputError("unsupported policy schema_version")
    source_admins = []
    for item in _list(
        row["source_admin_principals"], "source_admin_principals", MAX_ROLES
    ):
        entry = _record(
            item, frozenset(SourceAdminPrincipal.__dataclass_fields__), "source admin"
        )
        source_admins.append(
            SourceAdminPrincipal(
                _name(entry["name"], "source admin name"),
                _text(entry["justification"], "source admin justification"),
            )
        )
    bootstraps = []
    for item in _list(
        row["operational_bootstrap_memberships"],
        "operational_bootstrap_memberships",
        MAX_ROLES,
    ):
        entry = _record(
            item,
            frozenset(OperationalBootstrapMembership.__dataclass_fields__),
            "operational bootstrap membership",
        )
        bootstraps.append(
            OperationalBootstrapMembership(
                _name(entry["member"], "bootstrap member"),
                _name(entry["role"], "bootstrap role"),
                _text(entry["justification"], "bootstrap justification"),
            )
        )
    objects = []
    for item in _list(row["objects"], "policy objects", MAX_OBJECTS):
        entry = _record(
            item, frozenset(ObjectPolicy.__dataclass_fields__), "object policy"
        )
        classification = entry["classification"]
        if classification not in ("named", "none"):
            raise PlanInputError("unknown object policy classification")
        grants = []
        for grant_value in _list(
            entry["grants"], "policy grants", MAX_GRANTS_PER_OBJECT
        ):
            grant = _record(
                grant_value, frozenset(NamedGrant.__dataclass_fields__), "named grant"
            )
            justification = grant["justification"]
            if justification is not None:
                justification = _text(justification, "grant justification")
            grants.append(
                NamedGrant(
                    _name(grant["role"], "grant role"),
                    _privileges(grant["privileges"], "grant privileges"),
                    cast(str | None, justification),
                )
            )
        column_grants = []
        for grant_value in _list(
            entry["column_grants"], "policy column grants", MAX_GRANTS_PER_OBJECT
        ):
            grant = _record(
                grant_value,
                frozenset(NamedColumnGrant.__dataclass_fields__),
                "named column grant",
            )
            justification = grant["justification"]
            if justification is not None:
                justification = _text(justification, "column grant justification")
            column_grants.append(
                NamedColumnGrant(
                    _name(grant["column"], "policy column name"),
                    _name(grant["role"], "column grant role"),
                    _privileges(grant["privileges"], "column grant privileges"),
                    cast(str | None, justification),
                )
            )
        if (classification == "none" and (grants or column_grants)) or (
            classification == "named" and not (grants or column_grants)
        ):
            raise PlanInputError("object classification and grants disagree")
        objects.append(
            ObjectPolicy(
                _text(entry["identity"], "object identity", max_length=1000),
                cast(Classification, classification),
                tuple(grants),
                tuple(column_grants),
            )
        )
    return AuthorityPolicy(
        PLAN_VERSION,
        _name(row["database"], "policy database"),
        _name(row["target_owner"], "target owner"),
        _name(row["runtime_role"], "runtime role"),
        _name(row["expected_database_owner"], "expected database owner"),
        _strings(row["scope_schemas"], "policy scope_schemas", MAX_OBJECTS),
        _strings(row["approved_source_owners"], "approved_source_owners", MAX_ROLES),
        tuple(source_admins),
        tuple(bootstraps),
        tuple(objects),
    )


def object_identity(item: CatalogObject) -> str:
    """Stable identity includes overload argument types and object subkind."""
    return json.dumps(
        [
            item.kind,
            item.schema,
            item.name,
            item.subkind,
            [[arg.schema, arg.name] for arg in item.argument_types],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _object_sql(item: CatalogObject) -> tuple[str, str, int]:
    if item.kind == "schema":
        return "SCHEMA", _quoted(item.name), 20
    identifier = f"{_quoted(item.schema or '')}.{_quoted(item.name)}"
    if item.kind == "relation":
        keyword, order = _RELATION_SQL[item.subkind or ""]
        return keyword, identifier, order
    if item.kind == "type":
        return ("DOMAIN" if item.subkind == "d" else "TYPE"), identifier, 30
    args = ", ".join(
        f"{_quoted(arg.schema)}.{_quoted(arg.name)}" for arg in item.argument_types
    )
    return _ROUTINE_SQL[item.subkind or ""], f"{identifier}({args})", 70


def _allowed_privileges(item: CatalogObject) -> frozenset[str]:
    if item.kind == "relation":
        category = (
            "sequence"
            if item.subkind == "S"
            else "materialized_view"
            if item.subkind == "m"
            else "table"
        )
        return _PRIVILEGES[category]
    return _PRIVILEGES[item.kind]


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_catalog(catalog: CatalogSnapshot) -> dict[str, object]:
    document = asdict(catalog)
    document["database_acl_grants"] = sorted(
        document["database_acl_grants"], key=lambda grant: grant["grantee"]
    )
    for grant in document["database_acl_grants"]:
        grant["privileges"] = sorted(grant["privileges"])
        grant["grant_options"] = sorted(grant["grant_options"])
    document["roles"] = sorted(document["roles"], key=lambda row: row["name"])
    document["memberships"] = sorted(
        document["memberships"], key=lambda row: (row["member"], row["role"])
    )
    document["default_acls"] = sorted(
        document["default_acls"],
        key=lambda row: (
            row["owner"],
            row["schema"] or "",
            row["object_class"],
            row["grantee"],
        ),
    )
    document["objects"] = sorted(
        document["objects"],
        key=lambda row: (
            row["kind"],
            row["schema"] or "",
            row["name"],
            row["subkind"] or "",
            str(row["argument_types"]),
        ),
    )
    for row in document["objects"]:
        row["columns"] = sorted(row["columns"])
        for field in ("acl_grants", "effective_privileges"):
            row[field] = sorted(row[field], key=lambda grant: grant["grantee"])
            for grant in row[field]:
                grant["privileges"] = sorted(grant["privileges"])
                grant["grant_options"] = sorted(grant["grant_options"])
        row["column_grants"] = sorted(
            row["column_grants"], key=lambda grant: (grant["column"], grant["grantee"])
        )
        for grant in row["column_grants"]:
            grant["privileges"] = sorted(grant["privileges"])
            grant["grant_options"] = sorted(grant["grant_options"])
    for row in document["default_acls"]:
        row["privileges"] = sorted(row["privileges"])
    return document


def _normalized_policy(policy: AuthorityPolicy) -> dict[str, object]:
    document = asdict(policy)
    document["source_admin_principals"] = sorted(
        document["source_admin_principals"], key=lambda row: row["name"]
    )
    document["operational_bootstrap_memberships"] = sorted(
        document["operational_bootstrap_memberships"],
        key=lambda row: (row["member"], row["role"]),
    )
    document["objects"] = sorted(document["objects"], key=lambda row: row["identity"])
    for row in document["objects"]:
        row["grants"] = sorted(row["grants"], key=lambda grant: grant["role"])
        for grant in row["grants"]:
            grant["privileges"] = sorted(grant["privileges"])
        row["column_grants"] = sorted(
            row["column_grants"],
            key=lambda grant: (grant["column"], grant["role"]),
        )
        for grant in row["column_grants"]:
            grant["privileges"] = sorted(grant["privileges"])
    return document


def compile_plan(catalog: CatalogSnapshot, policy: AuthorityPolicy) -> AuthorityPlan:
    """Join a complete supplied catalog to every explicit object decision."""
    reasons: list[str] = []
    statements: list[tuple[int, str, int, str]] = []
    if catalog.schema_version != PLAN_VERSION or policy.schema_version != PLAN_VERSION:
        reasons.append("schema version mismatch")
    if catalog.database != policy.database:
        reasons.append("catalog and policy database differ")
    if policy.target_owner != "app_admin" or policy.runtime_role != "app_user":
        reasons.append("target owner or runtime role differs from Sub contract")
    if catalog.database_owner != policy.expected_database_owner:
        reasons.append("database owner differs from explicit policy expectation")
    if catalog.database_owner in {policy.target_owner, policy.runtime_role}:
        reasons.append(
            "migration or runtime role owns database and implicitly holds CREATE"
        )
    if (
        set(catalog.scope_schemas) != set(policy.scope_schemas)
        or not catalog.scope_schemas
        or len(catalog.scope_schemas) != len(set(catalog.scope_schemas))
        or len(policy.scope_schemas) != len(set(policy.scope_schemas))
    ):
        reasons.append("catalog and policy app-scope schemas differ or are empty")
    if any(
        name.startswith("pg_") or name == "information_schema"
        for name in catalog.scope_schemas
    ):
        reasons.append("system schema included in app scope")
    for field, complete in asdict(catalog.evidence).items():
        if not complete:
            reasons.append(f"{field} evidence is incomplete")
    roles = {role.name: role for role in catalog.roles}
    if len(roles) != len(catalog.roles):
        reasons.append("duplicate role posture")
    source_admins = {item.name: item for item in policy.source_admin_principals}
    if len(source_admins) != len(policy.source_admin_principals):
        reasons.append("duplicate source admin declaration")
    posture_valid_source_admins: set[str] = set()
    for name, declaration in source_admins.items():
        posture = roles.get(name)
        if not declaration.justification.strip():
            reasons.append(f"source admin {name} lacks justification")
        if name in {policy.runtime_role, "platform_api", "PUBLIC", "dotmac_app"}:
            reasons.append(f"source admin {name} is a forbidden principal")
        elif (
            posture is None
            or not posture.can_login
            or not (
                (name == policy.target_owner and posture.bypass_rls)
                or posture.superuser
            )
        ):
            reasons.append(f"source admin {name} lacks migration or DBA posture")
        else:
            posture_valid_source_admins.add(name)
    for name, expected in (
        ("app_admin", (True, True, False)),
        ("app_user", (True, False, False)),
        ("platform_api", (True, False, False)),
    ):
        role = roles.get(name)
        if (
            role is None
            or (role.can_login, role.bypass_rls, role.superuser) != expected
        ):
            reasons.append(f"{name} role posture is missing or invalid")
        elif role.can_create_database or role.can_create_role:
            reasons.append(f"{name} holds cluster role or database creation authority")
        if role is not None and role.database_create:
            reasons.append(f"{name} effectively holds database CREATE")
    bootstrap_name = "dotmac_schema_bootstrap"
    expected_bootstrap = (bootstrap_name, policy.target_owner)
    declarations = policy.operational_bootstrap_memberships
    if (
        len(declarations) != 1
        or (
            declarations[0].member,
            declarations[0].role,
        )
        != expected_bootstrap
    ):
        reasons.append("exact operational bootstrap membership declaration is required")
    elif not declarations[0].justification.strip():
        reasons.append("operational bootstrap membership lacks justification")
    bootstrap_role = roles.get(bootstrap_name)
    if bootstrap_role is None or (
        bootstrap_role.can_login,
        bootstrap_role.inherits,
        bootstrap_role.bypass_rls,
        bootstrap_role.superuser,
        bootstrap_role.can_create_database,
        bootstrap_role.can_create_role,
        bootstrap_role.database_create,
    ) != (True, False, False, False, False, False, True):
        reasons.append("operational bootstrap role posture is missing or invalid")
    database_acl = {grant.grantee: grant for grant in catalog.database_acl_grants}
    if len(database_acl) != len(catalog.database_acl_grants):
        reasons.append("duplicate database ACL grantee")
    for grant in catalog.database_acl_grants:
        database_rights = set(grant.privileges)
        options = set(grant.grant_options)
        if grant.grantee not in {"PUBLIC", catalog.database_owner} | set(roles):
            reasons.append("database ACL has unknown grantee")
        if not database_rights <= {"CONNECT", "CREATE", "TEMPORARY"}:
            reasons.append("database ACL has unknown privilege")
        if not options <= database_rights:
            reasons.append("database ACL grant options exceed privileges")
        if grant.grantee == bootstrap_name:
            if "CREATE" not in database_rights or options:
                reasons.append(
                    "bootstrap needs direct database CREATE without grant option"
                )
        elif "CREATE" in database_rights and grant.grantee != catalog.database_owner:
            reasons.append("unreviewed direct database CREATE grant")
        if options and grant.grantee != catalog.database_owner:
            reasons.append("non-owner database grant options require review")
    if bootstrap_name not in database_acl or (
        "CREATE" not in database_acl[bootstrap_name].privileges
    ):
        reasons.append("bootstrap needs direct database CREATE without grant option")
    if len(catalog.objects) > MAX_OBJECTS or len(policy.objects) > MAX_OBJECTS:
        reasons.append("object limit exceeded")
    if len(catalog.roles) > MAX_ROLES:
        reasons.append("role limit exceeded")
    if (
        len(policy.source_admin_principals) > MAX_ROLES
        or len(policy.operational_bootstrap_memberships) > MAX_ROLES
        or len(catalog.database_acl_grants) > MAX_GRANTS_PER_OBJECT
        or len(catalog.memberships) > MAX_ROLES * MAX_ROLES
    ):
        reasons.append("authority evidence or declaration limit exceeded")
    protected = {policy.target_owner, policy.runtime_role, "platform_api"}
    protected.update(grant.role for item in policy.objects for grant in item.grants)
    protected.update(
        grant.role for item in policy.objects for grant in item.column_grants
    )
    protected.update(source_admins)
    bootstrap_membership_seen = False
    if any(
        member.member in protected or member.role in protected
        for member in catalog.memberships
        if (member.member, member.role) != expected_bootstrap
    ):
        reasons.append("protected role membership requires separate authority review")
    for member in catalog.memberships:
        if member.member not in roles or member.role not in roles:
            reasons.append("membership names unknown role")
        if (member.member, member.role) == expected_bootstrap:
            bootstrap_membership_seen = True
            if (member.inherits, member.set_option, member.admin_option) != (
                False,
                True,
                False,
            ):
                reasons.append("operational bootstrap membership options are invalid")
        elif member.member == bootstrap_name:
            reasons.append("unexpected operational bootstrap membership")
    if not bootstrap_membership_seen:
        reasons.append("operational bootstrap membership is absent")
    if len(set(catalog.memberships)) != len(catalog.memberships):
        reasons.append("duplicate membership evidence")
    if catalog.default_acls:
        reasons.append("default ACLs require separate future-object policy review")
    objects: dict[str, CatalogObject] = {}
    for item in catalog.objects:
        scope = item.name if item.kind == "schema" else item.schema
        if scope is None:
            reasons.append("catalog object has no schema")
            continue
        if (
            scope.startswith("pg_")
            or scope == "information_schema"
            or item.extension_owned
        ):
            continue
        if scope not in catalog.scope_schemas:
            reasons.append("catalog object is outside declared app scope")
            continue
        identity = object_identity(item)
        if identity in objects:
            reasons.append(f"duplicate catalog object {identity}")
        objects[identity] = item
    policies: dict[str, ObjectPolicy] = {}
    for rule in policy.objects:
        if rule.identity in policies:
            reasons.append(f"duplicate policy object {rule.identity}")
        policies[rule.identity] = rule
    for scope in catalog.scope_schemas:
        if not any(
            item.kind == "schema" and item.name == scope for item in objects.values()
        ):
            reasons.append(f"app-scope schema {scope!r} is absent from catalog")
    for identity in sorted(set(objects) - set(policies)):
        reasons.append(f"unclassified app object {identity}")
    for identity in sorted(set(policies) - set(objects)):
        reasons.append(f"policy names unknown or excluded object {identity}")
    for identity in sorted(set(objects) & set(policies)):
        item = objects[identity]
        decision = policies[identity]
        allowed = _allowed_privileges(item)
        if item.owner not in roles and item.owner != "pg_database_owner":
            reasons.append(f"owner posture missing for {identity}")
        if item.owner in {policy.runtime_role, "platform_api"}:
            reasons.append(
                f"runtime role owns {identity}; implicit privileges cannot be inferred"
            )
        if item.owner != policy.target_owner:
            if item.owner not in policy.approved_source_owners:
                reasons.append(f"unexpected source owner for {identity}")
            else:
                keyword, sql_name, order = _object_sql(item)
                statements.append(
                    (
                        order,
                        identity,
                        0,
                        f"ALTER {keyword} {sql_name} OWNER TO {_quoted(policy.target_owner)};",
                    )
                )
        if item.kind != "relation" and item.columns:
            reasons.append(f"non-relation declares columns on {identity}")
        if len(item.columns) != len(set(item.columns)):
            reasons.append(f"duplicate relation column on {identity}")
        if (item.column_grants or decision.column_grants) and (
            item.kind != "relation" or item.subkind not in _COLUMN_RELATION_KINDS
        ):
            reasons.append(f"unsupported column-grant relation kind on {identity}")
        observed_columns: dict[tuple[str, str], set[str]] = {}
        for observed_column in item.column_grants:
            key = (observed_column.column, observed_column.grantee)
            if key in observed_columns:
                reasons.append(f"duplicate column ACL on {identity}")
            observed_columns[key] = set(observed_column.privileges)
            if observed_column.column not in item.columns:
                reasons.append(f"column ACL names unknown column on {identity}")
            if (
                observed_column.grantee != "PUBLIC"
                and observed_column.grantee not in roles
            ):
                reasons.append(f"column ACL has unknown grantee on {identity}")
            if not set(observed_column.privileges) <= _COLUMN_PRIVILEGES:
                reasons.append(f"column ACL has unknown privilege on {identity}")
            if not set(observed_column.grant_options) <= set(
                observed_column.privileges
            ):
                reasons.append(
                    f"column ACL grant options exceed privileges on {identity}"
                )
            if observed_column.grant_options:
                reasons.append(
                    f"column ACL grant options require separate review on {identity}"
                )
        desired_columns: dict[tuple[str, str], set[str]] = {}
        for requested_column in decision.column_grants:
            key = (requested_column.column, requested_column.role)
            if key in desired_columns:
                reasons.append(f"duplicate policy column grant on {identity}")
            desired_columns[key] = set(requested_column.privileges)
            if requested_column.column not in item.columns:
                reasons.append(
                    f"policy column grant names unknown column on {identity}"
                )
            if (
                not requested_column.privileges
                or not set(requested_column.privileges) <= _COLUMN_PRIVILEGES
            ):
                reasons.append(
                    f"policy column grant has unknown privilege on {identity}"
                )
            if requested_column.role == "PUBLIC":
                if not requested_column.justification:
                    reasons.append(
                        f"PUBLIC column grant lacks justification on {identity}"
                    )
            elif requested_column.role == policy.target_owner:
                reasons.append(f"owner column grant is implicit on {identity}")
            elif requested_column.role not in roles:
                reasons.append(f"unknown column grant role on {identity}")
            else:
                role_posture = roles[requested_column.role]
                if any(
                    (
                        role_posture.superuser,
                        role_posture.bypass_rls,
                        role_posture.can_create_database,
                        role_posture.can_create_role,
                        role_posture.database_create,
                    )
                ):
                    reasons.append(
                        f"column grant role has unsafe posture on {identity}"
                    )
        for key, observed in observed_columns.items():
            if not observed <= desired_columns.get(key, set()):
                reasons.append(f"excess or unexplained column ACL on {identity}")
        direct_grants = {grant.grantee: grant for grant in item.acl_grants}
        direct = {
            grantee: set(grant.privileges) for grantee, grant in direct_grants.items()
        }
        effective = {
            grant.grantee: set(grant.privileges) for grant in item.effective_privileges
        }
        if len(direct) != len(item.acl_grants) or len(effective) != len(
            item.effective_privileges
        ):
            reasons.append(f"duplicate ACL grantee on {identity}")
        desired = {grant.role: set(grant.privileges) for grant in decision.grants}
        if len(desired) != len(decision.grants):
            reasons.append(f"duplicate policy grantee on {identity}")
        if (
            decision.classification == "none"
            and (decision.grants or decision.column_grants)
        ) or (
            decision.classification == "named"
            and not (decision.grants or decision.column_grants)
        ):
            reasons.append(f"invalid policy classification on {identity}")
        for requested in decision.grants:
            if requested.role != "PUBLIC" and requested.role not in roles:
                reasons.append(f"unknown grant role on {identity}")
            elif requested.role != "PUBLIC" and requested.role != policy.target_owner:
                role_posture = roles[requested.role]
                if role_posture.superuser or role_posture.bypass_rls:
                    reasons.append(f"grant role has unsafe posture on {identity}")
            if requested.role == "PUBLIC" and not requested.justification:
                reasons.append(f"PUBLIC grant lacks justification on {identity}")
            if requested.role == policy.target_owner:
                reasons.append(f"owner grant is implicit on {identity}")
        for source, grants in (
            ("catalog ACL", item.acl_grants),
            ("effective ACL", item.effective_privileges),
        ):
            for observed_grant in grants:
                if (
                    observed_grant.grantee != "PUBLIC"
                    and observed_grant.grantee not in roles
                    and observed_grant.grantee != item.owner
                ):
                    reasons.append(f"{source} has unknown grantee on {identity}")
                if not set(observed_grant.privileges) <= allowed:
                    reasons.append(f"{source} has unknown privilege on {identity}")
                if not set(observed_grant.grant_options) <= set(
                    observed_grant.privileges
                ):
                    reasons.append(
                        f"{source} grant options exceed privileges on {identity}"
                    )
                if source == "effective ACL" and observed_grant.grant_options:
                    reasons.append(
                        f"effective ACL grant options are not direct evidence on {identity}"
                    )
        if any(not set(grant.privileges) <= allowed for grant in decision.grants):
            reasons.append(f"policy has unknown privilege on {identity}")
        for grantee in {"PUBLIC", policy.runtime_role, "platform_api", *desired}:
            if grantee not in effective:
                reasons.append(
                    f"effective ACL evidence for {grantee} missing on {identity}"
                )
        # Extra direct, effective, or PUBLIC rights are never assumed removable
        # by an ownership change. An exact REVOKE plan needs a separate review.
        for grantee, observed in direct.items():
            # A raw ACL entry for the current owner is owner authority. ALTER
            # OWNER transfers it. It is not a separate grant to retain.
            owner_entry = grantee == item.owner and (
                item.owner == policy.target_owner
                or item.owner in policy.approved_source_owners
            )
            observed_grant = direct_grants[grantee]
            if observed_grant.grant_options and not owner_entry:
                reasons.append(f"non-owner direct grant options on {identity}")
            if not owner_entry and not observed <= desired.get(grantee, set()):
                reasons.append(f"excess direct ACL for {grantee} on {identity}")
        for grantee, observed in effective.items():
            permitted_effective = desired.get(grantee, set()) | desired.get(
                "PUBLIC", set()
            )
            if (
                grantee
                not in {
                    item.owner,
                    policy.target_owner,
                    *posture_valid_source_admins,
                }
                and not observed <= permitted_effective
            ):
                reasons.append(f"excess effective ACL for {grantee} on {identity}")
        keyword, sql_name, order = _object_sql(item)
        grant_class = (
            "ROUTINE"
            if item.kind == "routine"
            else (
                "TYPE"
                if item.kind == "type"
                else (
                    "SEQUENCE"
                    if item.kind == "relation" and item.subkind == "S"
                    else "TABLE"
                    if item.kind == "relation"
                    else keyword
                )
            )
        )
        for grantee in sorted(desired):
            missing = desired[grantee] - direct.get(grantee, set())
            if missing:
                ordered = ", ".join(sorted(missing))
                target = "PUBLIC" if grantee == "PUBLIC" else _quoted(grantee)
                statements.append(
                    (
                        order,
                        identity,
                        1,
                        f"GRANT {ordered} ON {grant_class} {sql_name} TO {target};",
                    )
                )
        if item.kind == "relation" and item.subkind in _COLUMN_RELATION_KINDS:
            for column, grantee in sorted(desired_columns):
                missing = desired_columns[(column, grantee)] - observed_columns.get(
                    (column, grantee), set()
                )
                if missing:
                    rights = ", ".join(
                        f"{privilege} ({_quoted(column)})"
                        for privilege in sorted(missing)
                    )
                    target = "PUBLIC" if grantee == "PUBLIC" else _quoted(grantee)
                    statements.append(
                        (
                            order,
                            identity,
                            2,
                            f"GRANT {rights} ON TABLE {sql_name} TO {target};",
                        )
                    )
    if len(statements) > MAX_STATEMENTS:
        reasons.append("statement limit exceeded")
    ordered_statements = (
        tuple(entry[3] for entry in sorted(statements)) if not reasons else ()
    )
    catalog_sha = _digest(_normalized_catalog(catalog))
    policy_sha = _digest(_normalized_policy(policy))
    plan_sha = _digest(
        {
            "schema_version": PLAN_VERSION,
            "database": catalog.database,
            "target_owner": policy.target_owner,
            "catalog_sha256": catalog_sha,
            "policy_sha256": policy_sha,
            "statements": ordered_statements,
        }
    )
    return AuthorityPlan(
        PLAN_VERSION,
        "blocked" if reasons else "ready_for_review",
        catalog.database,
        policy.target_owner,
        tuple(sorted(set(reasons))),
        ordered_statements,
        catalog_sha,
        policy_sha,
        plan_sha,
    )
