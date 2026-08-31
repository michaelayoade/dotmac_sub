from __future__ import annotations

import uuid
from datetime import UTC
from types import MappingProxyType

import pytest

from app.models.audit import AuditEvent
from app.models.auth import (
    AuthenticationBinding,
    AuthenticationBindingIdentityError,
    AuthProvider,
    UserCredential,
)
from app.models.party import PartyType
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.services import authentication_mechanism_registry as mechanism_registry
from app.services import party as party_service
from app.services.credential_party_binding import (
    CredentialBindingError,
    CredentialPartyBinding,
    CredentialPrincipalKind,
    bind_credential_party,
    credential_convergence_report,
    resolve_binding_for_mechanism,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID, provision_operator_tenant
from app.services.owner_commands import CommandContext

SCOPE = "party:credential_authentication_projection"


def _context() -> CommandContext:
    return CommandContext.system(
        actor="pytest:credential-adoption",
        scope=SCOPE,
        reason="reviewed credential adoption test",
    )


def _binding(
    db_session,
    *,
    binding_key: str = "local.test",
    mechanism_code: str = "local",
) -> AuthenticationBinding:
    row = AuthenticationBinding(
        binding_key=binding_key,
        mechanism_code=mechanism_code,
        name=f"Test {mechanism_code}",
        is_active=True,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _staff_credential(db_session):
    provision_operator_tenant(db_session)
    party = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Private Staff Person",
    )
    staff = SystemUser(
        first_name="Private",
        last_name="Staff",
        email=f"staff-{uuid.uuid4().hex}@example.test",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(staff)
    db_session.flush()
    party_service.bind_system_user_principal(
        db_session,
        system_user_id=staff.id,
        person_party_id=party.id,
        source="reviewed_staff_worklist",
        reason="reviewed staff identity",
    )
    credential = UserCredential(
        system_user_id=staff.id,
        provider=AuthProvider.local,
        username=f"staff-{uuid.uuid4().hex}",
        password_hash="not-a-real-hash",
        is_active=True,
    )
    binding = _binding(db_session)
    db_session.add(credential)
    db_session.flush()
    credential_id = credential.id
    staff_id = staff.id
    party_id = party.id
    binding_id = binding.id
    db_session.commit()
    return credential_id, staff_id, party_id, binding_id


def _command(
    credential_id: uuid.UUID,
    expected_principal_id: uuid.UUID,
    party_id: uuid.UUID,
    binding_id: uuid.UUID,
    *,
    expected_principal_kind: CredentialPrincipalKind = (
        CredentialPrincipalKind.system_user
    ),
    reason: str = "reviewed exact credential projection",
) -> CredentialPartyBinding:
    return CredentialPartyBinding(
        context=_context(),
        credential_id=credential_id,
        expected_principal_kind=expected_principal_kind,
        expected_principal_id=expected_principal_id,
        party_id=party_id,
        authentication_binding_id=binding_id,
        tenant_id=OPERATOR_TENANT_ID,
        binding_source="reviewed_credential_worklist",
        binding_reason=reason,
    )


def test_complete_projection_commits_and_exact_retry_preserves_evidence(db_session):
    credential_id, staff_id, party_id, binding_id = _staff_credential(db_session)
    command = _command(credential_id, staff_id, party_id, binding_id)

    first = bind_credential_party(db_session, command)
    replay = bind_credential_party(db_session, command)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.bound_at == first.bound_at
    credential = db_session.get(UserCredential, credential_id)
    assert credential is not None
    assert (
        credential.party_id,
        credential.authentication_binding_id,
        credential.tenant_id,
    ) == (party_id, binding_id, OPERATOR_TENANT_ID)
    assert credential.party_bound_at is not None
    assert credential.party_bound_at.replace(tzinfo=UTC) == first.bound_at
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "credential.party_authentication_projected")
        .count()
        == 1
    )


def test_changed_evidence_is_not_an_exact_retry(db_session):
    credential_id, staff_id, party_id, binding_id = _staff_credential(db_session)
    bind_credential_party(
        db_session, _command(credential_id, staff_id, party_id, binding_id)
    )

    with pytest.raises(CredentialBindingError) as raised:
        bind_credential_party(
            db_session,
            _command(
                credential_id,
                staff_id,
                party_id,
                binding_id,
                reason="different review evidence",
            ),
        )

    assert raised.value.code.endswith(".repoint_refused")


def test_typed_expected_principal_must_match_the_locked_credential(db_session):
    credential_id, _staff_id, party_id, binding_id = _staff_credential(db_session)

    with pytest.raises(CredentialBindingError) as raised:
        bind_credential_party(
            db_session,
            _command(credential_id, uuid.uuid4(), party_id, binding_id),
        )

    assert raised.value.code.endswith(".principal_mismatch")
    assert raised.value.details == {
        "expected_principal_kind": "system_user",
        "actual_principal_kind": "system_user",
    }


def test_person_and_declared_matching_mechanism_are_required(db_session):
    credential_id, staff_id, party_id, _ = _staff_credential(db_session)
    organization = party_service.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="Private Organization",
    )
    radius = _binding(db_session, binding_key="radius.test", mechanism_code="radius")
    invented = _binding(db_session, binding_key="sso.test", mechanism_code="sso")
    organization_id = organization.id
    radius_id = radius.id
    invented_id = invented.id
    db_session.commit()

    with pytest.raises(CredentialBindingError) as non_person:
        bind_credential_party(
            db_session,
            _command(credential_id, staff_id, organization_id, radius_id),
        )
    assert non_person.value.code.endswith(".person_required")

    with pytest.raises(CredentialBindingError) as mismatch:
        bind_credential_party(
            db_session, _command(credential_id, staff_id, party_id, radius_id)
        )
    assert mismatch.value.code.endswith(".mechanism_mismatch")

    with pytest.raises(CredentialBindingError) as undeclared:
        bind_credential_party(
            db_session, _command(credential_id, staff_id, party_id, invented_id)
        )
    assert undeclared.value.code.endswith(".undeclared_mechanism")


def test_mechanism_resolution_refuses_ambiguity(db_session):
    _staff_credential(db_session)
    db_session.add(
        AuthenticationBinding(
            binding_key="local.second",
            mechanism_code="local",
            name="Second local verifier",
            is_active=True,
        )
    )
    db_session.commit()

    with pytest.raises(CredentialBindingError) as raised:
        resolve_binding_for_mechanism(db_session, "local")

    assert raised.value.code.endswith(".ambiguous_mechanism_binding")


def test_installed_binding_identity_is_immutable(db_session):
    _staff_credential(db_session)
    binding = db_session.query(AuthenticationBinding).one()

    binding.mechanism_code = "radius"
    with pytest.raises(AuthenticationBindingIdentityError):
        db_session.flush()


def test_report_separates_principal_readiness_from_projection(db_session):
    credential_id, staff_id, party_id, binding_id = _staff_credential(db_session)

    before = credential_convergence_report(db_session)
    db_session.commit()
    bind_credential_party(
        db_session, _command(credential_id, staff_id, party_id, binding_id)
    )
    after = credential_convergence_report(db_session)

    staff_before = next(
        item
        for item in before.principal_cohorts
        if item.principal_kind == "system_user"
    )
    assert staff_before.principal_party_ready == 1
    assert staff_before.remaining == 0
    assert before.projection.projected == 0
    assert before.projection.remaining == 1
    assert after.projection.projected == 1
    assert after.projection.remaining == 0
    assert after.projection.is_ready_for_enforcement is True


def test_second_local_credential_for_same_party_is_refused(db_session):
    from app.services.subscriber import _default_reseller_id

    credential_id, staff_id, party_id, binding_id = _staff_credential(db_session)
    subscriber = Subscriber(
        first_name="Private",
        last_name="Subscriber",
        email=f"subscriber-{uuid.uuid4().hex}@example.test",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(subscriber)
    db_session.flush()
    party_service.bind_subscriber_account(
        db_session,
        subscriber_id=subscriber.id,
        party_id=party_id,
        source="reviewed_subscriber_worklist",
        reason="same human, separate customer account",
    )
    second = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username=f"subscriber-{uuid.uuid4().hex}",
        password_hash="not-a-real-hash",
        is_active=True,
    )
    db_session.add(second)
    db_session.flush()
    second_id = second.id
    subscriber_id = subscriber.id
    db_session.commit()

    bind_credential_party(
        db_session, _command(credential_id, staff_id, party_id, binding_id)
    )
    with pytest.raises(CredentialBindingError) as raised:
        bind_credential_party(
            db_session,
            _command(
                second_id,
                subscriber_id,
                party_id,
                binding_id,
                expected_principal_kind=CredentialPrincipalKind.subscriber,
            ),
        )

    assert raised.value.code.endswith(".projection_collision")


# ---------------------------------------------------------------------------
# Mechanism vocabulary vs storage vocabulary
# ---------------------------------------------------------------------------


def _federated_staff_credential(db_session):
    """A staff credential stored as `sso` behind a binding declaring `oidc`.

    This is the shape an operator provisions for a federated field technician,
    and it is exactly the shape a literal `mechanism_code == provider`
    comparison can never accept.
    """

    provision_operator_tenant(db_session)
    party = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Field Technician",
    )
    staff = SystemUser(
        first_name="Field",
        last_name="Technician",
        email=f"tech-{uuid.uuid4().hex}@example.test",
        user_type=UserType.system_user,
        is_active=True,
    )
    db_session.add(staff)
    db_session.flush()
    party_service.bind_system_user_principal(
        db_session,
        system_user_id=staff.id,
        person_party_id=party.id,
        source="reviewed_staff_worklist",
        reason="reviewed staff identity",
    )
    credential = UserCredential(
        system_user_id=staff.id,
        provider=AuthProvider.sso,
        username=f"idp-subject-{uuid.uuid4().hex}",
        is_active=True,
    )
    binding = _binding(db_session, binding_key="oidc.field.test", mechanism_code="oidc")
    db_session.add(credential)
    db_session.flush()
    identifiers = (credential.id, staff.id, party.id, binding.id)
    db_session.commit()
    return identifiers


def test_a_federated_credential_projects_through_the_declared_storage_mapping(
    db_session,
):
    """The provisioning path that was impossible before the mapping existed.

    The binding declares `oidc`; the credential is persisted as `sso`. Those
    two strings differ, and comparing them literally refused every federated
    technician an operator could ever install — the seam worked only where no
    operator command ran.
    """

    credential_id, staff_id, party_id, binding_id = _federated_staff_credential(
        db_session
    )

    outcome = bind_credential_party(
        db_session, _command(credential_id, staff_id, party_id, binding_id)
    )

    assert outcome.replayed is False
    credential = db_session.get(UserCredential, credential_id)
    binding = db_session.get(AuthenticationBinding, binding_id)
    assert credential is not None and binding is not None
    assert credential.provider is AuthProvider.sso
    assert binding.mechanism_code == "oidc"
    assert credential.party_id == party_id
    assert credential.authentication_binding_id == binding_id


def test_a_provider_that_is_not_the_mechanisms_declared_storage_is_refused(db_session):
    """The other half: the mapping must still REFUSE the wrong storage.

    A mapping that only ever admits is indistinguishable from no check at all,
    so a local-password credential behind an OIDC binding — `oidc` maps to
    `sso`, not to `local` — has to be refused for that exact reason.
    """

    credential_id, staff_id, party_id, _ = _staff_credential(db_session)
    oidc = _binding(db_session, binding_key="oidc.mismatch", mechanism_code="oidc")
    oidc_id = oidc.id
    db_session.commit()

    with pytest.raises(CredentialBindingError) as raised:
        bind_credential_party(
            db_session, _command(credential_id, staff_id, party_id, oidc_id)
        )

    assert raised.value.code.endswith(".mechanism_mismatch")
    assert raised.value.details["credential_provider"] == "local"
    assert raised.value.details["expected_provider"] == "sso"


def test_a_mechanism_with_no_declared_storage_is_refused_by_the_writer(
    db_session, monkeypatch
):
    """Fail closed: an unmapped mechanism is refused, never identity-mapped.

    The declaration is what makes the projection provable. Without it the
    writer has nothing to compare the provider against, and the old shape's
    implicit answer — "the mechanism code IS the provider" — is precisely the
    assumption that must not come back.
    """

    credential_id, staff_id, party_id, binding_id = _federated_staff_credential(
        db_session
    )
    monkeypatch.setattr(
        mechanism_registry,
        "AUTHENTICATION_MECHANISM_STORAGE",
        MappingProxyType({"local": "local", "radius": "radius"}),
    )

    with pytest.raises(CredentialBindingError) as raised:
        bind_credential_party(
            db_session, _command(credential_id, staff_id, party_id, binding_id)
        )

    assert raised.value.code.endswith(".unmapped_mechanism_storage")
    assert raised.value.details == {"mechanism_code": "oidc"}
    credential = db_session.get(UserCredential, credential_id)
    assert credential is not None
    assert credential.party_id is None


def test_the_convergence_report_reads_the_writers_storage_declaration(
    db_session, monkeypatch
):
    """Two consumers, one declaration.

    If the report kept its own notion of "the mechanism matches the provider",
    it would report a federated deployment as permanently unconvergent while
    the writer happily projected it. Removing the declaration has to move both.
    """

    credential_id, staff_id, party_id, binding_id = _federated_staff_credential(
        db_session
    )
    bind_credential_party(
        db_session, _command(credential_id, staff_id, party_id, binding_id)
    )

    mapped = credential_convergence_report(db_session)
    assert mapped.projection.projected == 1
    assert mapped.projection.mechanism_mismatches == 0
    assert mapped.projection.is_ready_for_enforcement is True

    monkeypatch.setattr(
        mechanism_registry,
        "AUTHENTICATION_MECHANISM_STORAGE",
        MappingProxyType({"local": "local", "radius": "radius"}),
    )

    unmapped = credential_convergence_report(db_session)
    assert unmapped.projection.mechanism_mismatches == 1
    assert unmapped.projection.is_ready_for_enforcement is False
