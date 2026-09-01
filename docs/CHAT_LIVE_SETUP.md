# Live chat setup

Live chat has exactly ONE authority: Sub's native Team Inbox. There is no
selector, and adding one back is a build failure -- see "One authority" below.
`CHAT_LIVE_ENABLED` is the whole availability gate.

## The native transport

Authenticated customer and reseller sessions use Sub's native Team Inbox:

- session broker: `/api/v1/me/chat/session`, `/api/v1/reseller/chat/session`,
  and `/portal/chat/session`;
- visitor REST: `/widget`;
- real time: `/ws/inbox`;
- authoritative conversation/message state: `InboxConversation` and
  `InboxMessage`.

The public fiber website uses the same native transport through
`POST /widget/fiber/session`. Its adapter accepts only the exact
`FIBER_CHAT_ALLOWED_ORIGIN` (default `https://fiber.dotmac.ng`), applies a
five-start/fifteen-minute per-address throttle and a hidden honeypot/timing
gate, then issues the same bounded visitor token used by `/widget` and
`/ws/inbox`. Subsequent messages are limited to 30 per minute per session and
address.

Public fiber chat is stored as `channel_type=chat_widget` with
`surface=fiber_website`; this keeps the established reply-capable chat
transport separate from one-way `website_fiber` inquiry conversations. Exact
email/phone matches link an active Subscriber. Unmatched visitors create or
reuse a Party-backed prospect Lead through `sales.capture`; ambiguous matches
create the conversation without a Subscriber or automatic Lead and carry
`identity_review_required` for human review.

The fiber WordPress site loads `/static/js/fiber-chat-widget.js` from the Sub
base URL configured outside Git as `DOTMAC_FIBER_CHAT_API_URL`. That public base
URL is not a secret. The signed inquiry endpoint and its HMAC secret must never
be exposed to the browser.

## One authority

Between 2026-07-27 and 2026-08-30 a `comms.chat_session_authority` setting
selected between this native transport and an external CRM one (ADR 0006). The
CRM was decommissioned on 2026-08-29 and the selector was removed rather than
re-pointed: a live-chat surface with two possible writers loses operator
visibility the moment they disagree, and reconciling the two afterwards is
unbounded work. The retired setting, the `crm.chat_session.v1` capability, the
CRM broker, `CRMClient.create_widget_session` and the inbound
`POST /webhooks/crm/chat` receiver are all gone.

`tests/architecture/test_single_chat_authority.py` fails the build if any of
them returns: a second destination in the broker seam, a chat-authority
setting, one of the deleted module paths, or an external chat-transport
capability in a current connector manifest. The guard carries its own
sensitivity proof, so it cannot pass by finding nothing to check.

Retirement record: `docs/adr/0006-temporary-crm-chat-authority.md` and
`docs/runbooks/TEMPORARY_CRM_CHAT_AUTHORITY.md`. The second of those also
carries the production queries for the one question the repository cannot
answer -- whether the cut to CRM was ever actually executed, and therefore
whether any conversation was lost with the CRM.
