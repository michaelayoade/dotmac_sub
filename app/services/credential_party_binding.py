"""Bind one credential to the Party it authenticates, and to how it proves it.

The only native writer of `user_credentials.party_id` /
`authentication_binding_id` (R1 of the Party/principal adoption slice). Routes,
imports, jobs and future backfills call this owner; they do not write those
columns.

## The person-only rule — and what it does NOT say

The rule is scoped to **this reference only**:

| reference | required Party type |
|---|---|
| `UserCredential.party_id` | **person only** |
| `Subscriber.party_id` | person **or** organization, on evidence |
| organization-account login | a person administrator, plus membership/account authorization |

**A credential may only bind to a `person` Party.** An organization cannot log
in — the kernel says so outright ("organization parties have no login of their
own"), and the archetype agrees: authentication proves *which human*, while an
organization reaches the system through a person holding a membership.

**This is emphatically not "all subscribers must be people."** An
organization-owned subscriber account must *stay* an Organization Party; that is
the correct modelling of who holds the service. What moves is the credential: it
binds to a reviewed human administrator of that organization. Forcing the
account owner into a Person Party to satisfy a login would corrupt the ownership
record to work around an authentication constraint.

That is why `party.bind_subscriber_account` deliberately does **not** constrain
`party_type`, and why the guard belongs here instead — at the one reference that
genuinely requires a person. `credential.subscriber_id -> subscribers.party_id`
can legitimately resolve to an organization Party, and this command refuses it
rather than letting a login attach to something nothing can authenticate.

Measured on production 2026-08-12: all 2,229 parties are `person`, so the
hazard is currently zero. That is exactly why the rule belongs here now — it
costs nothing while the count is zero, and the first organization Party created
during the subscriber campaign would otherwise hit it silently, mid-batch.
Classifying which subscriber accounts are organization-owned is a separate
measurement, and it may enlarge the credential campaign: those accounts need an
administrator Person Party through a different path.

## What this command will not do

It binds; it does not merge, repoint, create a Party, assign a role, or change
authentication state. An exact retry is idempotent. A *different* target is
refused: repointing an identity is a reviewed workflow, never a force flag here
— the same contract `party.bind_subscriber_account` holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import AuthenticationBinding, UserCredential
from app.models.party import Party, PartyIdentityStatus, PartyType


class CredentialBindingError(ValueError):
    """A binding was refused. The message names the exact reason."""


@dataclass(frozen=True, slots=True)
class CredentialPartyBinding:
    """One reviewed binding request. Immutable — see the typed-contract rule."""

    credential_id: UUID
    party_id: UUID
    binding_source: str
    binding_reason: str
    authentication_binding_id: UUID | None = None


def _required_text(value: str | None, field: str) -> str:
    text = (value or "").strip()
    if not text:
        raise CredentialBindingError(f"{field} is required")
    return text


def _credential(db: Session, credential_id: UUID) -> UserCredential:
    credential = db.get(UserCredential, credential_id)
    if credential is None:
        raise CredentialBindingError(f"Credential '{credential_id}' was not found")
    return credential


def _person_party(db: Session, party_id: UUID) -> Party:
    party = db.get(Party, party_id)
    if party is None:
        raise CredentialBindingError(f"Party '{party_id}' was not found")

    # THE PERSON-ONLY RULE.
    if party.party_type != PartyType.person.value:
        raise CredentialBindingError(
            f"Party '{party_id}' is a {party.party_type}; a credential may only "
            "bind to a person Party. An organization reaches the system through "
            "a person holding a membership, not through a login of its own."
        )

    if party.status not in {
        PartyIdentityStatus.active.value,
        PartyIdentityStatus.quarantined.value,
    }:
        raise CredentialBindingError(
            f"Party '{party_id}' is '{party.status}' and cannot hold a credential"
        )
    return party


def _binding(db: Session, binding_id: UUID) -> AuthenticationBinding:
    binding = db.get(AuthenticationBinding, binding_id)
    if binding is None:
        raise CredentialBindingError(
            f"Authentication binding '{binding_id}' was not found"
        )
    if not binding.is_active:
        raise CredentialBindingError(
            f"Authentication binding '{binding_id}' ({binding.mechanism_code}) "
            "is inactive"
        )
    return binding


def resolve_binding_for_mechanism(
    db: Session, mechanism_code: str
) -> AuthenticationBinding:
    """The active binding for a mechanism code, when exactly one exists.

    Production has one binding per mechanism today, so this resolves cleanly.
    It refuses rather than guessing when a second is installed — that is the
    moment the code stops being a sufficient key, and a caller that guessed
    would silently attach credentials to an arbitrary verifier.
    """
    code = _required_text(mechanism_code, "mechanism_code")
    matches = list(
        db.scalars(
            select(AuthenticationBinding)
            .where(AuthenticationBinding.mechanism_code == code)
            .where(AuthenticationBinding.is_active.is_(True))
        )
    )
    if not matches:
        raise CredentialBindingError(
            f"No active authentication binding is installed for '{code}'"
        )
    if len(matches) > 1:
        raise CredentialBindingError(
            f"{len(matches)} active bindings exist for '{code}'; name the exact "
            "binding. A mechanism code stops identifying one verifier as soon "
            "as a second is installed."
        )
    return matches[0]


def bind_credential_party(
    db: Session, command: CredentialPartyBinding
) -> UserCredential:
    """Bind one credential to its person Party. Idempotent for an exact retry."""
    credential = _credential(db, command.credential_id)
    party = _person_party(db, command.party_id)
    source = _required_text(command.binding_source, "binding_source")
    reason = _required_text(command.binding_reason, "binding_reason")

    binding = (
        _binding(db, command.authentication_binding_id)
        if command.authentication_binding_id is not None
        else None
    )

    if credential.party_id is not None:
        if credential.party_id != party.id:
            raise CredentialBindingError(
                f"Credential '{command.credential_id}' is already bound to Party "
                f"'{credential.party_id}'; repointing is a reviewed workflow, "
                "not a rebind"
            )
        # Exact retry. Attach the mechanism binding if this call supplies one
        # and the row does not yet carry it; refuse a conflicting change.
        if binding is not None:
            if (
                credential.authentication_binding_id is not None
                and credential.authentication_binding_id != binding.id
            ):
                raise CredentialBindingError(
                    f"Credential '{command.credential_id}' is already bound to "
                    f"authentication binding '{credential.authentication_binding_id}'"
                )
            credential.authentication_binding_id = binding.id
        return credential

    credential.party_id = party.id
    credential.party_bound_at = datetime.now(UTC)
    credential.party_binding_source = source
    credential.party_binding_reason = reason
    if binding is not None:
        credential.authentication_binding_id = binding.id
    db.flush()
    return credential


@dataclass(frozen=True, slots=True)
class CredentialCohort:
    """PII-free convergence report for one principal kind."""

    principal_kind: str
    credentials: int
    party_bound: int
    organization_party_blockers: int

    @property
    def remaining(self) -> int:
        return self.credentials - self.party_bound


@dataclass(frozen=True, slots=True)
class CredentialConvergenceReport:
    """What credential convergence still needs. Counts only — no identifiers."""

    cohorts: tuple[CredentialCohort, ...]
    party_collisions: int

    @property
    def credentials(self) -> int:
        return sum(c.credentials for c in self.cohorts)

    @property
    def remaining(self) -> int:
        return sum(c.remaining for c in self.cohorts)

    @property
    def organization_party_blockers(self) -> int:
        return sum(c.organization_party_blockers for c in self.cohorts)

    @property
    def is_convergeable(self) -> bool:
        """True when nothing structural stands in the way of a uniqueness key.

        Collisions and organization-Party blockers are the two conditions that
        R3's constraint cannot be added over. Both are zero on production as of
        2026-08-12; this recomputes rather than assuming, because the subscriber
        campaign is what will introduce the first of either.
        """
        return self.party_collisions == 0 and self.organization_party_blockers == 0


def credential_convergence_report(db: Session) -> CredentialConvergenceReport:
    """Measure the population credential convergence still has to bind.

    Read-only. Safe to run against production, and meant to be: this is the
    number that decides whether a batch is ready, and it must come from the
    database rather than from a document that was true last week.
    """
    from app.models.subscriber import ResellerUser, Subscriber
    from app.models.system_user import SystemUser

    cohorts: list[CredentialCohort] = []
    for kind, principal_column, party_column, model in (
        ("subscriber", UserCredential.subscriber_id, Subscriber.party_id, Subscriber),
        (
            "system_user",
            UserCredential.system_user_id,
            SystemUser.person_party_id,
            SystemUser,
        ),
        (
            "reseller_user",
            UserCredential.reseller_user_id,
            ResellerUser.person_party_id,
            ResellerUser,
        ),
    ):
        rows = list(
            db.execute(
                select(UserCredential.id, party_column, Party.party_type)
                .select_from(UserCredential)
                .join(model, model.id == principal_column)
                .outerjoin(Party, Party.id == party_column)
                .where(principal_column.is_not(None))
            )
        )
        bound = [row for row in rows if row[1] is not None]
        cohorts.append(
            CredentialCohort(
                principal_kind=kind,
                credentials=len(rows),
                party_bound=len(bound),
                organization_party_blockers=sum(
                    1 for row in bound if row[2] == PartyType.organization.value
                ),
            )
        )

    bound_party_ids = list(
        db.scalars(
            select(UserCredential.party_id).where(UserCredential.party_id.is_not(None))
        )
    )
    collisions = len(bound_party_ids) - len(set(bound_party_ids))

    return CredentialConvergenceReport(
        cohorts=tuple(cohorts), party_collisions=collisions
    )
