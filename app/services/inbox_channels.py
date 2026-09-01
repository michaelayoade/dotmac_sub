"""Sub's conversation-channel declarations for the composed `dotmac-inbox`.

`dotmac_inbox.channels` ships an EMPTY registry on purpose. The module branches
on TRAITS and never on channel names, so the vocabulary belongs to the product:
`channel_spec()` raises `UnknownChannelError` until a product has declared its
channels. This module is Sub's declaration, and importing it is what makes
`dotmac_inbox.threading.thread_key` and `dedup_key` answerable for a Sub
channel at all.

Nothing under `app/` imports this module at runtime yet, and that is
deliberate. Sub's `public.inbox_*` tables remain the authority for every
conversation, message and read cursor; composing the module's schema and
declaring its traits are the two reversible halves of the adoption seam, and
the writer switch is a separate, separately-approved slice ruled by
`docs/adr/0013-inbox-conversation-authority.md`.

Every trait below is transcribed from behaviour Sub already has. Each comment
names the file the behaviour lives in, so the next reader can check the
transcription rather than trust it. Where the declaration CORRECTS Sub's
current behaviour rather than reproducing it — WhatsApp message identity — the
comment says so, because a silent correction is how a shadow comparison ends up
explaining a difference it should have predicted.

See `docs/designs/INBOX_MODULE_ADOPTION.md` § "Channel declarations".
"""

from __future__ import annotations

from typing import Final

from dotmac_inbox.channels import (
    AddressForm,
    ChannelSpec,
    MessageIdScope,
    ThreadIdentity,
    Transport,
    register_channels,
)

from app.models.team_inbox import InboxChannelType

_OWNER: Final = "dotmac-sub"

#: `InboxChannelType.field_job` is DELIBERATELY ABSENT — see
#: :data:`UNDECLARED_CHANNELS` below. Every other member of the enum is here.
SUB_CHANNELS: Final[tuple[ChannelSpec, ...]] = (
    ChannelSpec(
        code=InboxChannelType.email.value,
        owner=_OWNER,
        label="Email",
        # `normalize_channel_address` routes "email" to
        # `normalize_email_identifier`
        # (`app/services/customer_identity_normalization.py`).
        address_form=AddressForm.EMAIL,
        transport=Transport.EXTERNAL,
        # `team_inbox_receive._resolve_thread_conversation` threads on the
        # In-Reply-To / References headers, so the thread identity arrives with
        # the message rather than being derived from the sender.
        thread_identity=ThreadIdentity.PROVIDER,
        # An RFC 5322 Message-ID is generated to be globally unique, and Sub's
        # `uq_inbox_messages_inbound_external` partial index already treats it
        # that way. This is the one channel where the existing global rule and
        # the declared trait agree.
        message_id_scope=MessageIdScope.GLOBAL,
    ),
    ChannelSpec(
        code=InboxChannelType.whatsapp.value,
        owner=_OWNER,
        label="WhatsApp",
        # `PHONE_LIKE_HINTS` in
        # `app/services/customer_identity_normalization.py` includes whatsapp.
        address_form=AddressForm.PHONE,
        transport=Transport.EXTERNAL,
        # The Cloud API exposes no thread object: one business number talking
        # to one customer number is one conversation.
        thread_identity=ThreadIdentity.DERIVED,
        # CORRECTION, not transcription. A `wamid` is only meaningful inside
        # the business account it was delivered to, but Sub's
        # `uq_inbox_messages_inbound_external` is `(channel_type,
        # external_message_id)` with no account discriminator, so a second
        # connected number legitimately re-using an id is dropped today. The
        # account-scoped declaration admits those messages, which means a
        # shadow comparison MUST expect and count the delta rather than treat
        # it as drift.
        message_id_scope=MessageIdScope.ACCOUNT,
    ),
    ChannelSpec(
        code=InboxChannelType.website_fiber.value,
        owner=_OWNER,
        label="Website fibre enquiry",
        # `team_inbox_receive.receive_fiber_inquiry` sets
        # `contact_address=normalized_email`.
        address_form=AddressForm.EMAIL,
        # Not a third party, but a transport with a provider all the same:
        # `channel_health_contracts.SUPPORTED_EXTERNAL_CHANNELS` lists it and
        # `team_inbox_observations.InboxProvider.fiber_website` names it.
        transport=Transport.EXTERNAL,
        # The same receiver mints `external_thread_id=f"fiber:{delivery_id}"`,
        # one thread per submission. Derived `(channel, account, contact)`
        # would merge every enquiry one person ever files.
        thread_identity=ThreadIdentity.PROVIDER,
        # `external_message_id=delivery_id`, minted per submitting site
        # (`provider_account_scope=site_id`). Account scope is what the
        # evidence supports; a global claim would be stronger than the facts.
        message_id_scope=MessageIdScope.ACCOUNT,
    ),
    ChannelSpec(
        code=InboxChannelType.facebook_messenger.value,
        owner=_OWNER,
        label="Facebook Messenger",
        # In `_OPAQUE_CONTACT_CHANNELS`
        # (`app/services/team_inbox_channel_receive.py`): the contact is a
        # page-scoped id Sub stores verbatim, not an address anyone can write
        # to out of band.
        address_form=AddressForm.OPAQUE,
        transport=Transport.EXTERNAL,
        thread_identity=ThreadIdentity.PROVIDER,
        # A Messenger message id is scoped to the Page it was delivered to.
        message_id_scope=MessageIdScope.ACCOUNT,
    ),
    ChannelSpec(
        code=InboxChannelType.instagram_dm.value,
        owner=_OWNER,
        label="Instagram DM",
        address_form=AddressForm.OPAQUE,
        transport=Transport.EXTERNAL,
        thread_identity=ThreadIdentity.PROVIDER,
        message_id_scope=MessageIdScope.ACCOUNT,
    ),
    ChannelSpec(
        code=InboxChannelType.facebook_comment.value,
        owner=_OWNER,
        label="Facebook comment",
        address_form=AddressForm.OPAQUE,
        transport=Transport.EXTERNAL,
        # A comment thread is the post it hangs under, and the post id comes
        # from Meta.
        thread_identity=ThreadIdentity.PROVIDER,
        message_id_scope=MessageIdScope.ACCOUNT,
    ),
    ChannelSpec(
        code=InboxChannelType.instagram_comment.value,
        owner=_OWNER,
        label="Instagram comment",
        address_form=AddressForm.OPAQUE,
        transport=Transport.EXTERNAL,
        thread_identity=ThreadIdentity.PROVIDER,
        message_id_scope=MessageIdScope.ACCOUNT,
    ),
    ChannelSpec(
        code=InboxChannelType.chat_widget.value,
        owner=_OWNER,
        label="Website chat widget",
        # Also in `_OPAQUE_CONTACT_CHANNELS`: a visitor has no address until
        # they choose to give one.
        address_form=AddressForm.OPAQUE,
        # EXTERNAL rather than INTERNAL, from behaviour rather than from the
        # word "widget": Sub already classifies it as an external channel with
        # its own provider — `channel_health_contracts
        # .SUPPORTED_EXTERNAL_CHANNELS`, `team_inbox_observations
        # .InboxProvider.chat_widget`, and the `chat_widget -> {chat_widget}`
        # row of `team_inbox_integrator_envelope.PROVIDER_CHANNELS`. The trait
        # that actually depends on this is the next one.
        transport=Transport.EXTERNAL,
        # `team_inbox_widget._thread_id` mints
        # `chat_widget:{surface}:{entity_id}:{context}` and looks the
        # conversation up by it. That is richer than the module's derived
        # `(channel, account, contact)`, which would merge a visitor's
        # ticket-scoped chat with their general one — so the identity has to
        # travel with the message, which INTERNAL transport forbids.
        thread_identity=ThreadIdentity.PROVIDER,
        message_id_scope=MessageIdScope.ACCOUNT,
    ),
    ChannelSpec(
        code=InboxChannelType.note.value,
        owner=_OWNER,
        label="Internal note",
        address_form=AddressForm.OPAQUE,
        # Excluded from `channel_health_contracts.SUPPORTED_EXTERNAL_CHANNELS`
        # and unreachable from `PROVIDER_CHANNELS`: there is no provider and
        # nothing is ever delivered.
        transport=Transport.INTERNAL,
        # Forced by the trait validation, and correct here: a note thread has
        # no id of its own. (Today Sub writes internal notes as
        # `direction='internal'` messages on the HOST conversation's channel —
        # `team_inbox_operations.create_internal_note` — so no live writer
        # creates a `note` conversation. The declaration exists because the
        # vocabulary member does, and historical rows may.)
        thread_identity=ThreadIdentity.DERIVED,
        message_id_scope=MessageIdScope.NONE,
    ),
)

#: A vocabulary member Sub has and does NOT declare, with the premise that
#: makes the absence checkable rather than an oversight.
#:
#: `field_job` is an INTERNAL transport — the enum says so in its own comment
#: ("no external transport: delivery is the shared conversation websocket") and
#: `channel_health_contracts.SUPPORTED_EXTERNAL_CHANNELS` omits it. But its
#: thread identity is the work order: `team_inbox_field_job` sets
#: `external_thread_id = work_order.public_id` and finds the conversation by
#: it. `ChannelSpec` refuses `Transport.INTERNAL` together with
#: `ThreadIdentity.PROVIDER`, so the truthful declaration cannot be
#: constructed, and the only declaration that CAN be — `DERIVED` — would put
#: every work order for one subscriber in a single conversation.
#:
#: Declaring it wrongly would fail silently at cutover. Leaving it undeclared
#: fails loudly at the first call: `channel_spec("field_job")` raises
#: `UnknownChannelError` naming this module. That is the correct behaviour
#: until `dotmac-inbox` admits a thread identity on an internal transport, and
#: `tests/test_inbox_channel_declarations.py` proves the premise still holds
#: rather than trusting this comment.
UNDECLARED_CHANNELS: Final[dict[str, str]] = {
    InboxChannelType.field_job.value: (
        "an internal transport whose thread identity is the work order id; "
        "dotmac-inbox refuses Transport.INTERNAL with ThreadIdentity.PROVIDER"
    ),
}

#: Channels with no external transport. They never arrived at a connected
#: account, so anything deriving an account scope for them uses a declared
#: literal rather than inventing one.
INTERNAL_TRANSPORT_CHANNELS: Final[frozenset[str]] = frozenset(
    spec.code for spec in SUB_CHANNELS if spec.transport is Transport.INTERNAL
)

#: The declared absence of a connected account. Never parsed, only compared.
INTERNAL_ACCOUNT_SCOPE: Final = "sub:internal"

register_channels(SUB_CHANNELS)

__all__ = [
    "INTERNAL_ACCOUNT_SCOPE",
    "INTERNAL_TRANSPORT_CHANNELS",
    "SUB_CHANNELS",
    "UNDECLARED_CHANNELS",
]
