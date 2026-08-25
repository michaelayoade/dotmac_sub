"""Sub's ten conversation channels, declared once with their behaviour traits.

`dotmac_inbox.channels` ships an EMPTY registry on purpose: the module branches
on traits, never on channel names, so the vocabulary is the product's. This is
Sub's declaration, and importing this module is what makes
`dotmac_inbox.threading.thread_key` and `dedup_key` answerable at all —
`channel_spec()` raises `UnknownChannelError` until it has run.

Every trait below is transcribed from behaviour Sub already has, not chosen
fresh. Where the source is a set literal in a receive service, it is named in
the comment so the next reader can check the transcription rather than trust it.

See `docs/adr/0013-inbox-authority-cutover.md` § "Sub declares its channels".
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

#: The channel declarations, in `InboxChannelType` order.
#:
#: `address_form=OPAQUE` reproduces `_OPAQUE_CONTACT_CHANNELS` in
#: `app/services/team_inbox_channel_receive.py`: those five channels carry a
#: provider-scoped id that Sub stores verbatim, and every other channel carries
#: an address `normalize_channel_address` can put in a canonical form.
SUB_CHANNELS: Final[tuple[ChannelSpec, ...]] = (
    ChannelSpec(
        code=InboxChannelType.email.value,
        owner=_OWNER,
        label="Email",
        address_form=AddressForm.EMAIL,
        transport=Transport.EXTERNAL,
        # Sub threads email on In-Reply-To/References
        # (`_resolve_thread_conversation`, `team_inbox_receive`), which is the
        # provider's thread identity even though the provider is SMTP.
        thread_identity=ThreadIdentity.PROVIDER,
        # An RFC 5322 Message-ID is generated to be globally unique, and Sub's
        # `uq_inbox_messages_inbound_external` already treats it that way.
        message_id_scope=MessageIdScope.GLOBAL,
    ),
    ChannelSpec(
        code=InboxChannelType.whatsapp.value,
        owner=_OWNER,
        label="WhatsApp",
        address_form=AddressForm.PHONE,
        transport=Transport.EXTERNAL,
        # WhatsApp has no thread object: one business number talking to one
        # customer number is one conversation.
        thread_identity=ThreadIdentity.DERIVED,
        # A `wamid` is only meaningful inside the business account it was
        # delivered to. Two connected numbers can legitimately see the same id.
        message_id_scope=MessageIdScope.ACCOUNT,
    ),
    ChannelSpec(
        code=InboxChannelType.website_fiber.value,
        owner=_OWNER,
        label="Website fiber enquiry",
        address_form=AddressForm.EMAIL,
        transport=Transport.EXTERNAL,
        # `team_inbox_fiber_receive` mints `fiber:{delivery_id}` as the thread
        # id, so the thread identity is supplied rather than derived.
        thread_identity=ThreadIdentity.PROVIDER,
        message_id_scope=MessageIdScope.ACCOUNT,
    ),
    ChannelSpec(
        code=InboxChannelType.facebook_messenger.value,
        owner=_OWNER,
        label="Facebook Messenger",
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
        # A comment thread is the post it hangs under.
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
        address_form=AddressForm.OPAQUE,
        transport=Transport.EXTERNAL,
        # `team_inbox_widget` supplies its own session thread id.
        thread_identity=ThreadIdentity.PROVIDER,
        message_id_scope=MessageIdScope.ACCOUNT,
    ),
    ChannelSpec(
        code=InboxChannelType.note.value,
        owner=_OWNER,
        label="Internal note",
        address_form=AddressForm.OPAQUE,
        transport=Transport.INTERNAL,
        # INTERNAL transport forbids PROVIDER identity and any id scope but
        # NONE — the module rejects the declaration otherwise, which is the
        # trait validation doing its job.
        thread_identity=ThreadIdentity.DERIVED,
        message_id_scope=MessageIdScope.NONE,
    ),
    ChannelSpec(
        code=InboxChannelType.field_job.value,
        owner=_OWNER,
        label="Field job chat",
        address_form=AddressForm.OPAQUE,
        transport=Transport.INTERNAL,
        thread_identity=ThreadIdentity.DERIVED,
        message_id_scope=MessageIdScope.NONE,
    ),
)

#: Channels with no external transport. They never arrived at a connected
#: account, so the backfill's `account_scope` ladder gives them a declared
#: literal rather than inventing one — ADR-0013 § 5, rung 3.
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
]
