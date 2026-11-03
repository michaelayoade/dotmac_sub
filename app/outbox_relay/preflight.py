"""Fail-closed identity check: the relay may only be the EXECUTE-only dispatcher.

The dispatcher role is a LOGIN that holds EXECUTE on the tenant claim/settle
pair and nothing else. If the supplied credential is a different role (for
example the application's ``DATABASE_URL`` pasted into the wrong slot), or the
role has any table/column privilege, or can run the platform-plane functions,
the relay refuses to start rather than running with more authority than its
design allows.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.outbox_dispatcher_roles import RELAY_DISPATCHER_CONTRACT

TENANT_ROLE = "outbox_dispatcher"
TENANT_CLAIM_SIG = "public.claim_outbox_batch(text,integer,integer)"
TENANT_SETTLE_SIG = (
    "public.settle_outbox_event(uuid,text,text,timestamp with time zone,integer,text)"
)
#: The only SECURITY DEFINER functions the dispatcher may be able to execute.
ALLOWED_DEFINER_FUNCTIONS = frozenset(
    {"public.claim_outbox_batch", "public.settle_outbox_event"}
)


@dataclass(frozen=True, slots=True)
class DispatcherObservation:
    current_user: str
    can_login: bool
    bypass_rls: bool
    superuser: bool
    #: Relations in EVERY non-system schema on which the role holds any table
    #: or column privilege (not just ``public``).
    relations_with_privilege: int
    #: ``pg_auth_members`` rows where the role is the member.
    role_memberships: int
    can_claim: bool
    can_settle: bool
    #: ``schema.name`` of every SECURITY DEFINER function in a non-system
    #: schema the role can EXECUTE (PUBLIC holds EXECUTE by default).
    executable_definers: frozenset[str]


def dispatcher_identity_violations(obs: DispatcherObservation) -> tuple[str, ...]:
    expected_login, expected_bypass, expected_super = RELAY_DISPATCHER_CONTRACT[
        TENANT_ROLE
    ]
    problems: list[str] = []
    if obs.current_user != TENANT_ROLE:
        problems.append(f"connected as {obs.current_user!r}, not {TENANT_ROLE!r}")
    if obs.can_login != expected_login:
        problems.append("role LOGIN posture differs from the contract")
    if obs.bypass_rls != expected_bypass:
        problems.append("role BYPASSRLS posture differs from the contract")
    if obs.superuser != expected_super:
        problems.append("role SUPERUSER posture differs from the contract")
    if obs.relations_with_privilege:
        problems.append(
            f"role holds table/column privileges on {obs.relations_with_privilege} "
            "relation(s); the dispatcher must hold none"
        )
    if obs.role_memberships:
        problems.append(
            f"role is a member of {obs.role_memberships} other role(s); the "
            "dispatcher must inherit nothing"
        )
    if not obs.can_claim:
        problems.append("role lacks EXECUTE on claim_outbox_batch")
    if not obs.can_settle:
        problems.append("role lacks EXECUTE on settle_outbox_event")
    extra = obs.executable_definers - ALLOWED_DEFINER_FUNCTIONS
    if extra:
        problems.append(
            "role can execute SECURITY DEFINER function(s) beyond the tenant "
            "pair: " + ", ".join(sorted(extra))
        )
    return tuple(problems)


_OBSERVE = text(
    """
    SELECT
      current_user::text AS current_user_name,
      r.rolcanlogin, r.rolbypassrls, r.rolsuper,
      (SELECT count(*) FROM pg_class c
         JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg\\_toast%' AND n.nspname NOT LIKE 'pg\\_temp%'
          AND c.relkind IN ('r','p','v','m','f')
          AND (has_table_privilege(current_user, c.oid,
                 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
               OR has_any_column_privilege(current_user, c.oid,
                 'SELECT,INSERT,UPDATE,REFERENCES'))) AS relations_with_privilege,
      (SELECT count(*) FROM pg_auth_members m
        WHERE m.member = r.oid) AS role_memberships,
      has_function_privilege(current_user, :claim, 'EXECUTE') AS can_claim,
      has_function_privilege(current_user, :settle, 'EXECUTE') AS can_settle,
      ARRAY(SELECT n.nspname || '.' || p.proname
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog','information_schema')
               AND has_function_privilege(current_user, p.oid, 'EXECUTE')
          ) AS executable_definers
    FROM pg_roles r WHERE r.rolname = current_user
    """
)


def observe_dispatcher(db: Session) -> DispatcherObservation:
    row = (
        db.execute(_OBSERVE, {"claim": TENANT_CLAIM_SIG, "settle": TENANT_SETTLE_SIG})
        .mappings()
        .one()
    )
    return DispatcherObservation(
        current_user=str(row["current_user_name"]),
        can_login=bool(row["rolcanlogin"]),
        bypass_rls=bool(row["rolbypassrls"]),
        superuser=bool(row["rolsuper"]),
        relations_with_privilege=int(row["relations_with_privilege"]),
        role_memberships=int(row["role_memberships"]),
        can_claim=bool(row["can_claim"]),
        can_settle=bool(row["can_settle"]),
        executable_definers=frozenset(str(n) for n in row["executable_definers"]),
    )
