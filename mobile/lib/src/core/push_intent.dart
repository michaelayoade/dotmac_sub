import 'package:flutter/foundation.dart' show immutable;

/// Push-notification navigation intents.
///
/// SECURITY CONTRACT — read before adding anything here.
///
/// An FCM payload is **not** a trust boundary. Anyone able to send to the
/// Firebase project (a leaked server key, a misconfigured or compromised
/// sender, a mis-targeted campaign tool) fully controls every byte of
/// `message.data`, and the OS hands it to us unauthenticated. Therefore:
///
///  * the payload NAMES AN INTENT; this app — not the payload — decides the
///    route. There is no `route`/`path`/`deep_link`/`link`/`url` key, and one
///    must never be added, not even behind a prefix allowlist: a prefix
///    allowlist still lets the sender choose the query string, which is the
///    whole exploit (`/reset-password?token=<attacker-chosen>` renders the
///    password-reset screen primed with a sender-supplied token).
///  * every route this module can return is either a compile-time constant
///    ([PushIntent.baseRoute]) or one of those constants with a **validated**
///    identifier interpolated into a template owned here.
///  * an unrecognised, absent or malformed intent falls back to
///    [PushIntent.inbox] — the authenticated notifications inbox — never to
///    anything derived from payload text.
///
/// [isAppOwnedPushRoute] re-checks that invariant at the navigation call site
/// so the guarantee does not depend on this file alone.
enum PushIntent {
  /// Live support chat (customer portal).
  chat('/support/chat'),

  /// Live support chat inside the reseller portal.
  resellerChat('/reseller/chat'),

  /// Support ticket list; deep-links to `/support/<uuid>` when the payload
  /// carries a well-formed `ticket_id`.
  ticket('/support'),

  /// Self-serve installation quotes.
  quotes('/quotes'),

  /// Invoices / payments.
  billing('/billing'),

  /// Service + usage tab.
  usage('/usage'),

  /// The safe default: the in-app notifications inbox.
  inbox('/dashboard/notifications');

  const PushIntent(this.baseRoute);

  /// The constant route this intent opens when no identifier is supplied.
  final String baseRoute;
}

/// A resolved push destination: the intent that was recognised, and the route
/// the app will navigate to.
@immutable
class PushDestination {
  const PushDestination(this.intent, this.route);

  final PushIntent intent;

  /// Always an app-owned route — see the contract on [PushIntent].
  final String route;

  @override
  bool operator ==(Object other) =>
      other is PushDestination &&
      other.intent == intent &&
      other.route == route;

  @override
  int get hashCode => Object.hash(intent, route);

  @override
  String toString() => 'PushDestination($intent, $route)';
}

/// The closed set of intent codes the backend may name.
///
/// Keys are matched case-insensitively after trimming. Entries exist for the
/// codes Sub's senders actually emit today:
///  * `chat_message`   — app/api/crm_webhooks.py (CRM chat wake-up)
///  * `ticket`         — app/services/crm_ticket_pull.py (ticket closed)
///  * `quote`          — app/services/quotes_mirror.py (quote accepted)
/// plus the notification `event_type`/`category` vocabulary the queued-push
/// path (app/tasks/notifications.py) produces. Adding a code is a deliberate,
/// reviewed act: it must map to a route this app already owns.
const Map<String, PushIntent> _intentCodes = {
  // Live chat
  'chat.message': PushIntent.chat,
  'chat': PushIntent.chat,
  'chat_message': PushIntent.chat,
  'message.outbound': PushIntent.chat,
  'message_outbound': PushIntent.chat,
  'message_new': PushIntent.chat,
  'reseller_chat': PushIntent.resellerChat,
  'reseller_chat_message': PushIntent.resellerChat,
  // Support tickets
  'ticket.closed': PushIntent.ticket,
  'ticket': PushIntent.ticket,
  'ticket_closed': PushIntent.ticket,
  'ticket_updated': PushIntent.ticket,
  'support': PushIntent.ticket,
  // Quotes
  'quote.accepted': PushIntent.quotes,
  'quote': PushIntent.quotes,
  'quote_accepted': PushIntent.quotes,
  // Billing
  'billing': PushIntent.billing,
  'invoice': PushIntent.billing,
  'invoice_issued': PushIntent.billing,
  'payment': PushIntent.billing,
  'payment_failed': PushIntent.billing,
  'payment_received': PushIntent.billing,
  'suspension': PushIntent.billing,
  // Usage
  'usage': PushIntent.usage,
  'quota_threshold': PushIntent.usage,
  'data_cap': PushIntent.usage,
  // Explicit "just open the inbox"
  'notification.open': PushIntent.inbox,
  'operational.escalation': PushIntent.inbox,
  'notification': PushIntent.inbox,
  'account_notice': PushIntent.inbox,
};

/// Payload keys that may carry an intent code. Deliberately short, and
/// deliberately excludes anything that could name a location.
const List<String> _codeKeys = [
  'intent_code',
  'type',
  'intent',
  'event_type',
  'category',
];

/// Keys a payload must NEVER be able to navigate with. Kept as a named
/// constant so the regression test can assert each one is inert rather than
/// re-listing them, and so a future reader sees the refusal is intentional.
const List<String> forbiddenNavigationKeys = [
  'route',
  'path',
  'deep_link',
  'deeplink',
  'link',
  'url',
];

final RegExp _uuid = RegExp(
  r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
  r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$',
);

/// A payload identifier, accepted only when it is a well-formed UUID.
///
/// Anchored and character-class-restricted, so no `/`, `?`, `#`, `.` or
/// whitespace can survive into an interpolated route.
String? _validatedUuid(Object? value) {
  final raw = value?.toString().trim();
  if (raw == null || raw.isEmpty) return null;
  return _uuid.hasMatch(raw) ? raw : null;
}

/// Resolve an FCM data payload to an app-owned destination.
///
/// [title] and [body] are display-only compatibility parameters. They never
/// select navigation: PushIntentV1 requires the backend to state a typed
/// intent, and a code-less or malformed payload opens the authenticated inbox.
PushDestination resolvePushDestination(
  Map<String, dynamic> data, {
  String? title,
  String? body,
}) {
  final intent = _intentFor(data);
  return PushDestination(intent, _routeFor(intent, data));
}

PushIntent _intentFor(Map<String, dynamic> data) {
  final version = data['contract_version']?.toString().trim();
  if (version != null && version != 'PushIntentV1') return PushIntent.inbox;
  if (version == 'PushIntentV1') {
    if (!_isCompleteV1(data)) return PushIntent.inbox;
    final code = data['intent_code']?.toString().trim().toLowerCase();
    return _intentCodes[code] ?? PushIntent.inbox;
  }
  for (final key in _codeKeys) {
    final code = data[key]?.toString().trim().toLowerCase();
    if (code == null || code.isEmpty) continue;
    final intent = _intentCodes[code];
    if (intent != null) return intent;
  }
  return PushIntent.inbox;
}

bool _isCompleteV1(Map<String, dynamic> data) {
  for (final key in const [
    'intent_code',
    'subject_kind',
    'subject_id',
    'tenant_id',
    'principal_id',
    'issued_at',
  ]) {
    final value = data[key]?.toString().trim();
    if (value == null || value.isEmpty) return false;
  }
  return DateTime.tryParse(data['issued_at'].toString())?.isUtc ?? false;
}

/// Build the route for [intent], interpolating a payload identifier only from
/// a template owned here and only after validation.
String _routeFor(PushIntent intent, Map<String, dynamic> data) {
  switch (intent) {
    case PushIntent.ticket:
      final id = _validatedUuid(
        data['subject_kind'] == 'ticket'
            ? data['subject_id']
            : data['ticket_id'],
      );
      return id == null ? intent.baseRoute : '/support/$id';
    case PushIntent.chat:
    case PushIntent.resellerChat:
    case PushIntent.quotes:
    case PushIntent.billing:
    case PushIntent.usage:
    case PushIntent.inbox:
      return intent.baseRoute;
  }
}

/// True when [route] is one this app owns and a push is allowed to open.
///
/// Defence in depth for the navigation call site (see `app.dart`): even if a
/// future edit to the resolver regressed, a route that is not a declared base
/// route or a `/support/<uuid>` ticket deep link is refused rather than handed
/// to `GoRouter.go()`.
bool isAppOwnedPushRoute(String route) {
  for (final intent in PushIntent.values) {
    if (route == intent.baseRoute) return true;
  }
  if (route.startsWith('/support/')) {
    return _uuid.hasMatch(route.substring('/support/'.length));
  }
  return false;
}
