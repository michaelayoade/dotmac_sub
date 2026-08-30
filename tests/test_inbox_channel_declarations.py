"""Sub's channel declarations mean what the adoption design says they mean.

`dotmac-inbox` decides two things on every message — which thread it belongs to
and whether we already have it — and it decides them ONLY from the four traits
a product declares. So the declaration is the contract, and these tests are
where it is stated in a form that fails when it changes.

The expected trait table below is deliberately a second copy of
`app/services/inbox_channels.py`. A trait change has to be made twice, which is
the point: the first copy is the behaviour and the second is the claim
`docs/designs/INBOX_MODULE_ADOPTION.md` makes about it.
"""

from __future__ import annotations

import pytest
from dotmac_inbox.channels import (
    AddressForm,
    ChannelSpec,
    MessageIdScope,
    ThreadIdentity,
    Transport,
    UnknownChannelError,
    channel_spec,
    register_channels,
)
from dotmac_inbox.threading import InboundIdentity, dedup_key, thread_key

from app.models.team_inbox import InboxChannelType
from app.services.inbox_channels import (
    INTERNAL_TRANSPORT_CHANNELS,
    SUB_CHANNELS,
    UNDECLARED_CHANNELS,
)

#: channel code -> (address form, transport, thread identity, message-id scope)
EXPECTED_TRAITS: dict[
    str, tuple[AddressForm, Transport, ThreadIdentity, MessageIdScope]
] = {
    "email": (
        AddressForm.EMAIL,
        Transport.EXTERNAL,
        ThreadIdentity.PROVIDER,
        MessageIdScope.GLOBAL,
    ),
    "whatsapp": (
        AddressForm.PHONE,
        Transport.EXTERNAL,
        ThreadIdentity.DERIVED,
        MessageIdScope.ACCOUNT,
    ),
    "website_fiber": (
        AddressForm.EMAIL,
        Transport.EXTERNAL,
        ThreadIdentity.PROVIDER,
        MessageIdScope.ACCOUNT,
    ),
    "facebook_messenger": (
        AddressForm.OPAQUE,
        Transport.EXTERNAL,
        ThreadIdentity.PROVIDER,
        MessageIdScope.ACCOUNT,
    ),
    "instagram_dm": (
        AddressForm.OPAQUE,
        Transport.EXTERNAL,
        ThreadIdentity.PROVIDER,
        MessageIdScope.ACCOUNT,
    ),
    "facebook_comment": (
        AddressForm.OPAQUE,
        Transport.EXTERNAL,
        ThreadIdentity.PROVIDER,
        MessageIdScope.ACCOUNT,
    ),
    "instagram_comment": (
        AddressForm.OPAQUE,
        Transport.EXTERNAL,
        ThreadIdentity.PROVIDER,
        MessageIdScope.ACCOUNT,
    ),
    "chat_widget": (
        AddressForm.OPAQUE,
        Transport.EXTERNAL,
        ThreadIdentity.PROVIDER,
        MessageIdScope.ACCOUNT,
    ),
    "note": (
        AddressForm.OPAQUE,
        Transport.INTERNAL,
        ThreadIdentity.DERIVED,
        MessageIdScope.NONE,
    ),
}


@pytest.fixture(autouse=True)
def _declarations_registered() -> None:
    """Keep the declarations present whatever order the suite runs in.

    The module registry is process-global and another test may reset it.
    `register_channels` is idempotent for identical specs, so re-asserting is
    free and removes the ordering dependency.
    """
    register_channels(SUB_CHANNELS)


def _identity(code: str | ChannelSpec, **overrides: object) -> InboundIdentity:
    fields: dict[str, object] = {
        "channel": code,
        "account_scope": "account-a",
        "contact": "contact-1",
    }
    fields.update(overrides)
    return InboundIdentity(**fields)  # type: ignore[arg-type]


def test_every_declared_channel_resolves_through_the_module_registry() -> None:
    for spec in SUB_CHANNELS:
        assert channel_spec(spec.code) is spec


def test_declared_traits_are_exactly_what_the_design_claims() -> None:
    observed = {
        spec.code: (
            spec.address_form,
            spec.transport,
            spec.thread_identity,
            spec.message_id_scope,
        )
        for spec in SUB_CHANNELS
    }
    assert observed == EXPECTED_TRAITS


def test_every_channel_in_subs_vocabulary_is_declared_or_named_undeclared() -> None:
    """No third state. A new enum member fails here until it is ruled on."""

    vocabulary = {member.value for member in InboxChannelType}
    declared = {spec.code for spec in SUB_CHANNELS}
    assert declared.isdisjoint(UNDECLARED_CHANNELS)
    assert declared | set(UNDECLARED_CHANNELS) == vocabulary


def test_an_undeclared_channel_fails_loudly_rather_than_threading_wrongly() -> None:
    for code in UNDECLARED_CHANNELS:
        with pytest.raises(UnknownChannelError):
            channel_spec(code)


def test_the_field_job_exclusion_states_a_premise_the_module_still_enforces() -> None:
    """The exclusion is caused, not remembered.

    `field_job` is absent because the truthful declaration — an internal
    transport whose thread identity is the work order — is one `ChannelSpec`
    refuses to construct. If the module ever admits it, this fails and the
    channel should be declared in the same change.
    """

    with pytest.raises(ValueError, match="internal but claims provider"):
        ChannelSpec(
            code=InboxChannelType.field_job.value,
            owner="dotmac-sub",
            address_form=AddressForm.OPAQUE,
            transport=Transport.INTERNAL,
            thread_identity=ThreadIdentity.PROVIDER,
            message_id_scope=MessageIdScope.NONE,
        )


def test_the_available_field_job_declaration_would_merge_two_work_orders() -> None:
    """Why the fallback declaration is refused rather than accepted as good enough.

    `DERIVED` is the only thread identity an internal transport may claim, and
    it keys on `(channel, account, contact)` — which is the same pair of values
    for every work order one subscriber ever has.
    """

    fallback = ChannelSpec(
        code=InboxChannelType.field_job.value,
        owner="dotmac-sub",
        address_form=AddressForm.OPAQUE,
        transport=Transport.INTERNAL,
        thread_identity=ThreadIdentity.DERIVED,
        message_id_scope=MessageIdScope.NONE,
    )
    # Passed as the spec itself rather than registered: `thread_key` accepts
    # either, and the process-global registry must not gain a channel Sub has
    # decided not to declare.
    first = thread_key(_identity(fallback, external_thread_id="work-order-1"))
    second = thread_key(_identity(fallback, external_thread_id="work-order-2"))
    assert first == second


def test_provider_channels_thread_on_the_supplied_thread_identity() -> None:
    provider_codes = [
        code
        for code, traits in EXPECTED_TRAITS.items()
        if traits[2] is ThreadIdentity.PROVIDER
    ]
    assert provider_codes, "no provider-threaded channel declared"
    for code in provider_codes:
        with_thread = thread_key(_identity(code, external_thread_id="thread-1"))
        other_thread = thread_key(_identity(code, external_thread_id="thread-2"))
        assert with_thread != other_thread
        # Same thread id, different contact, is still one thread.
        assert with_thread == thread_key(
            _identity(code, external_thread_id="thread-1", contact="contact-2")
        )


def test_derived_channels_thread_on_the_contact_at_one_account() -> None:
    derived_codes = [
        code
        for code, traits in EXPECTED_TRAITS.items()
        if traits[2] is ThreadIdentity.DERIVED
    ]
    assert derived_codes, "no derived-threaded channel declared"
    for code in derived_codes:
        base = thread_key(_identity(code))
        assert base == thread_key(_identity(code, external_thread_id="ignored"))
        assert base != thread_key(_identity(code, contact="contact-2"))
        assert base != thread_key(_identity(code, account_scope="account-b"))


def test_every_channel_threads_per_connected_account() -> None:
    """Two of our accounts talking to one contact stay two conversations."""

    for spec in SUB_CHANNELS:
        at_a = thread_key(_identity(spec.code, external_thread_id="t"))
        at_b = thread_key(
            _identity(spec.code, account_scope="account-b", external_thread_id="t")
        )
        assert at_a != at_b, spec.code


def test_email_message_identity_stays_global() -> None:
    """The one channel where the declaration reproduces Sub's current index."""

    at_a = dedup_key(_identity("email", external_message_id="<m1@example.test>"))
    at_b = dedup_key(
        _identity(
            "email",
            account_scope="account-b",
            external_message_id="<m1@example.test>",
        )
    )
    assert at_a == at_b
    assert not at_a.derived


def test_account_scoped_channels_admit_a_repeated_provider_id() -> None:
    """The declared CORRECTION, and the delta a shadow comparison must expect.

    Sub's `uq_inbox_messages_inbound_external` is global per channel, so the
    same provider id arriving at a second connected account is dropped today.
    Every account-scoped channel stops dropping it.
    """

    account_scoped = [
        code
        for code, traits in EXPECTED_TRAITS.items()
        if traits[3] is MessageIdScope.ACCOUNT
    ]
    assert "whatsapp" in account_scoped
    for code in account_scoped:
        at_a = dedup_key(_identity(code, external_message_id="wamid.1"))
        at_b = dedup_key(
            _identity(code, account_scope="account-b", external_message_id="wamid.1")
        )
        assert at_a != at_b, code
        assert not at_a.derived


def test_a_channel_without_a_provider_id_falls_back_to_a_flagged_fingerprint() -> None:
    for code in INTERNAL_TRANSPORT_CHANNELS:
        key = dedup_key(_identity(code, subject="s", body="b"))
        assert key.derived
        assert key == dedup_key(_identity(code, subject="s", body="b"))
        assert key != dedup_key(_identity(code, subject="s", body="different"))


def test_internal_transport_channels_carry_no_provider_identity() -> None:
    for code in INTERNAL_TRANSPORT_CHANNELS:
        spec = channel_spec(code)
        assert spec.thread_identity is ThreadIdentity.DERIVED
        assert spec.message_id_scope is MessageIdScope.NONE
