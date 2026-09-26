# ZeptoMail Delivery Tracking

## Purpose

The application treats a successful SMTP response from ZeptoMail as
`submitted`, not `delivered`. ZeptoMail's later result becomes the final status:

- `delivered` when ZeptoMail confirms delivery;
- `bounced` for a hard or soft bounce;
- `failed` for a provider processing failure, including `relaying-issues` and
  `Mail sending blocked`.

The notification record is the source of truth. Inbox messages display the
status projected from that record.

## ZeptoMail setup

1. Create a ZeptoMail OAuth client with the `Zeptomail.email.READ` scope.
2. Store the client ID, client secret, and refresh token in the approved secret
   store. Do not put their values in Git.
3. Create a ZeptoMail webhook for delivered, soft-bounce, and hard-bounce
   events. Its application URL is:

   `/api/v1/webhooks/zeptomail/delivery`

4. Set a webhook authentication key in ZeptoMail and store the same value in
   the approved secret store.
5. Configure these application settings through the normal settings process:

   - `ZEPTOMAIL_OAUTH_CLIENT_ID`
   - `ZEPTOMAIL_OAUTH_CLIENT_SECRET`
   - `ZEPTOMAIL_OAUTH_REFRESH_TOKEN`
   - `ZEPTOMAIL_WEBHOOK_AUTHENTICATION_KEY`
   - `ZEPTOMAIL_DELIVERY_TRACKING_ENABLED=true`

The default API URLs and 30-second check interval normally do not need to be
changed.

## How it works

Each SMTP message includes its notification ID as ZeptoMail's client reference.
A signed webhook normally provides the final result. A scheduled check also
reads ZeptoMail's logs every 30 seconds so missed webhooks and `Process failed`
results are still found. Only messages from the last 24 hours that remain
`submitted` are checked, with at most 50 checked per run.

## Verification

1. Send a test email from the Team Inbox.
2. Confirm the app first shows `Submitted`.
3. Confirm the status changes to `Delivered`, `Bounced`, or `Failed` after the
   ZeptoMail result appears.
4. For a blocked message, confirm the app shows the provider's reason and does
   not show `Delivered`.
5. Confirm the ZeptoMail log client reference equals the application's
   notification ID.

If messages remain `Submitted`, check that delivery tracking is enabled, the
OAuth client has read access, the webhook authentication keys match, and the
notifications worker and scheduler are running.
