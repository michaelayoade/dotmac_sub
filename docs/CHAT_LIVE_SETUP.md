# Live chat setup

Live chat is owned by Sub's native Team Inbox. Customer and reseller sessions
are authenticated and minted by Sub; browsers and mobile clients receive an
opaque visitor token that is valid only for the native widget API and inbox
WebSocket. CRM is not the chat transport or session authority.

## Components

| Owner | Surface |
| --- | --- |
| Sub | `POST /api/v1/me/chat/session` and `POST /api/v1/reseller/chat/session` |
| Sub | `POST /portal/chat/session` for the browser-authenticated customer portal |
| Sub | `/widget` REST API and `/ws/inbox` real-time channel |
| Sub | Team Inbox conversation/message state and agent workspace |
| CRM integration | Optional verified `message.outbound` observation at `POST /webhooks/crm/chat` for push notification only |

## Configuration

Set `CHAT_LIVE_ENABLED=true` to enable session minting and render the portal
widget. No CRM chat URL, widget ID, username, password, or direct WebSocket
setting is read by Sub.

If CRM `message.outbound` notifications are used, configure a `dotmac.crm`
installation through the integration admin surface:

- configuration: `base_url`, optional `timeout_seconds`, and optional
  `public_portal_api_base`;
- secret references: required `service_credentials` and optional
  `webhook_signing_secret`;
- enabled binding: `crm.events.receive.v1`.

The signing secret remains in the approved secret store. Configure the CRM
webhook sender with the matching secret reference value and the Sub endpoint;
do not place the value in this file or another tracked configuration file.

## Verification

1. Enable live chat and open a customer or reseller portal.
2. Start a chat and verify the returned URLs are `/widget` and `/ws/inbox`.
3. Send a message and confirm it appears once in the native Team Inbox.
4. Reply from the agent workspace and confirm real-time delivery to the portal.
5. If CRM push observations are enabled, send a signed `message.outbound` event
   twice with the same delivery ID and confirm one inbox consequence and an
   idempotent replay response.

Ticket and project context is accepted only after Sub verifies that the current
subscriber or reseller owns the referenced record.
