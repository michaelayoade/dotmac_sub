# Live chat — setup & cutover

Live chat bridges the customer (web + mobile) and reseller portals to the DotMac
Omni CRM's existing `chat_widget` channel. The sub authenticates the principal
and asserts identity to the CRM server-to-server; the client only ever holds an
opaque visitor token. Customer + reseller chats land in the **same general
support pool**.

Everything ships **flag-gated and inert** — no behaviour until the steps below.

## Components

| Side | What |
|------|------|
| CRM  | `POST /api/v1/widget/internal/session` trusted mint (branch `feat/chat-widget-internal-mint`) |
| CRM  | `message.outbound` webhook emitted on agent reply → mobile push |
| Sub  | `POST /api/v1/me/chat/session`, `POST /api/v1/reseller/chat/session` |
| Sub  | `POST /webhooks/crm/chat` (HMAC) → FCM push |
| Sub  | Web widget (both portal layouts) + Flutter chat screen |

## Environment

**Sub** (`.env`):

| Var | Meaning |
|-----|---------|
| `CHAT_LIVE_ENABLED` | `true` to enable the broker endpoints + render the web widget. Default off. |
| `CRM_CHAT_CONFIG_ID` | The `ChatWidgetConfig` UUID (printed by the CRM seed script). |
| `CRM_CHAT_WEBHOOK_SECRET` | HMAC secret for `/webhooks/crm/chat`. Must equal the CRM webhook endpoint's secret. |
| `CRM_CHAT_WS_URL` | Optional override; defaults to `wss://<crm host>/ws/widget`. |

Reuses existing `CRM_BASE_URL` / `CRM_USERNAME` / `CRM_PASSWORD` (the mint call)
and `FCM_CREDENTIALS_JSON` (push).

**CRM**:

| Var | Meaning |
|-----|---------|
| `CHAT_MINT_SERVICE_ACCOUNTS` | Comma list of account emails allowed to mint. Default `selfcare-sync@dotmac.io` (the sub's CRM login). |

## Cutover steps

1. **Deploy the CRM branch** `feat/chat-widget-internal-mint` (CI builds the
   ghcr image; bump `APP_IMAGE_TAG`; `docker compose up -d`). This adds the mint
   endpoint, the `message.outbound` event, and the `WebhookEventType` enum value.
2. **Seed the CRM** (after deploy, so the enum exists):
   ```
   docker exec -e CHAT_ALLOWED_DOMAINS="selfcare.dotmac.io,app.dotmac.io" \
     -e SUB_CHAT_WEBHOOK_URL="https://selfcare.dotmac.io/webhooks/crm/chat" \
     -e SUB_CHAT_WEBHOOK_SECRET="<pick a secret>" \
     dotmac_omni_app python scripts/seed_chat_widget.py
   ```
   Note the printed `CRM_CHAT_CONFIG_ID`. The portal domain(s) MUST be in
   `CHAT_ALLOWED_DOMAINS` or the browser's cross-origin REST/WS to the CRM is
   rejected.
3. **Configure the sub**: set `CRM_CHAT_CONFIG_ID`, `CRM_CHAT_WEBHOOK_SECRET`
   (same secret as step 2), then `CHAT_LIVE_ENABLED=true`. Deploy the sub branch
   `feat/live-chat-crm-bridge` and restart.
4. **Mobile**: ship the build; no extra env (uses the same authenticated API).
5. **Verify**: open the portal → chat bubble appears → send a message → it shows
   in the CRM omni-inbox → agent reply appears in the portal in real time and (if
   backgrounded) as a push on mobile.

## Notes / follow-ups

- The web client talks to the CRM directly (WS + REST); the mobile client polls
  while foregrounded and relies on FCM push when backgrounded.
- Tapping the mobile chat push to deep-link into `/chat` needs a navigatorKey
  (no `onMessageOpenedApp` handler is wired today).
- Conversation→ticket escalation (`resolved_to_ticket`) flows back to the sub
  through the existing CRM ticket bridge — nothing extra to wire.
