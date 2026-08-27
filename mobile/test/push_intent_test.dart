import 'package:dotmac_portal/src/core/api_client.dart';
import 'package:dotmac_portal/src/core/push_intent.dart';
import 'package:dotmac_portal/src/core/push_service.dart';
import 'package:flutter_test/flutter_test.dart';

/// Security regression suite for push-payload navigation.
///
/// An FCM payload is attacker-controllable (anyone who can send to the
/// Firebase project). Before this suite, `PushService.routeForNotificationData`
/// returned any payload string starting with `/` verbatim and `app.dart` handed
/// it to `GoRouter.go()` — so `{"route": "/reset-password?token=attacker"}`
/// opened the password-reset screen primed with a sender-supplied token.
void main() {
  group('payload-supplied locations are inert', () {
    test('no forbidden navigation key can influence the route', () {
      const hostile = '/reset-password?token=attacker';
      for (final key in forbiddenNavigationKeys) {
        final route = PushService.routeForNotificationData({key: hostile});
        expect(
          route,
          PushIntent.inbox.baseRoute,
          reason: '"$key" must not steer navigation',
        );
        expect(route, isNot(contains('reset-password')));
        expect(route, isNot(contains('token')));
        expect(route, isNot(contains('?')));
      }
    });

    test('the reset-password exploit payload does not reach that screen', () {
      final route = PushService.routeForNotificationData(
        {'route': '/reset-password?token=attacker'},
      );
      expect(route, isNot(startsWith('/reset-password')));
      expect(route, PushIntent.inbox.baseRoute);
      expect(isAppOwnedPushRoute('/reset-password?token=attacker'), isFalse);
    });

    test('absolute URLs and custom schemes are inert', () {
      // Some of these carry a word the code-less classifier recognises
      // ('billing'), so the app may still open that TAB. What must never
      // happen is a payload contributing to the route STRING.
      for (final hostile in const [
        'https://evil.example/steal',
        'dotmac://open/billing',
        'dotmac://open/reset-password?token=attacker',
        '//evil.example/x',
        '/billing/../reset-password?token=attacker',
      ]) {
        for (final key in const ['url', 'deep_link', 'link', 'path']) {
          final route = PushService.routeForNotificationData({key: hostile});
          expect(
            isAppOwnedPushRoute(route),
            isTrue,
            reason: '$key=$hostile resolved to $route',
          );
          expect(route, isNot(contains('reset-password')));
          expect(route, isNot(contains('evil.example')));
          expect(route, isNot(contains('?')));
          expect(route, isNot(contains('..')));
        }
      }
    });

    test('a hostile location cannot ride along with a valid intent code', () {
      expect(
        PushService.routeForNotificationData({
          'type': 'ticket',
          'route': '/reset-password?token=attacker',
          'url': 'https://evil.example',
        }),
        PushIntent.ticket.baseRoute,
      );
    });

    test('every resolvable payload yields an app-owned route', () {
      const hostilePayloads = <Map<String, dynamic>>[
        {'route': '/reset-password?token=x'},
        {'type': 'chat_message', 'path': '/lock'},
        {'type': '../../reset-password'},
        {'intent': 'https://evil.example'},
        {'ticket_id': '/reset-password?token=x'},
        {'event_type': 'invoice', 'deep_link': '/pay'},
        {},
      ];
      for (final payload in hostilePayloads) {
        final route = PushService.routeForNotificationData(
          payload,
          title: '/reset-password?token=attacker',
          body: 'https://evil.example/steal',
        );
        expect(
          isAppOwnedPushRoute(route),
          isTrue,
          reason: '$payload resolved to a route this app does not own: $route',
        );
      }
    });
  });

  group('intent codes route to their screens', () {
    // The codes Sub's senders construct today (app/api/crm_webhooks.py,
    // app/services/crm_ticket_pull.py, app/services/quotes_mirror.py).
    test('chat_message opens live chat', () {
      expect(
        PushService.routeForNotificationData(
          {'type': 'chat_message', 'conversation_id': 'c-1'},
          title: 'New message from support',
        ),
        '/support/chat',
      );
    });

    test('quote opens the quotes screen', () {
      expect(
        PushService.routeForNotificationData(
          {'type': 'quote', 'quote_id': 'crm-42'},
          title: 'Quote accepted',
        ),
        '/quotes',
      );
    });

    test('ticket with a valid UUID deep-links to that ticket', () {
      expect(
        PushService.routeForNotificationData(
          {
            'type': 'ticket',
            'ticket_id': '3f1c2b4a-5d6e-4f70-8a91-b2c3d4e5f607'
          },
          title: 'Support ticket closed',
        ),
        '/support/3f1c2b4a-5d6e-4f70-8a91-b2c3d4e5f607',
      );
    });

    test('billing and usage codes reach their tabs', () {
      expect(
        PushService.routeForNotificationData({'type': 'invoice'}),
        '/billing',
      );
      expect(
        PushService.routeForNotificationData({'event_type': 'quota_threshold'}),
        '/usage',
      );
    });

    test('a reseller chat code stays in the reseller portal', () {
      expect(
        PushService.routeForNotificationData({'type': 'reseller_chat'}),
        '/reseller/chat',
      );
    });
  });

  group('malformed identifiers never build a route', () {
    test('a non-UUID ticket_id falls back to the ticket list', () {
      for (final bad in const [
        'not-a-uuid',
        '../../reset-password',
        '3f1c2b4a-5d6e-4f70-8a91-b2c3d4e5f607/../reset-password',
        '3f1c2b4a-5d6e-4f70-8a91-b2c3d4e5f607?token=attacker',
        '3f1c2b4a-5d6e-4f70-8a91-b2c3d4e5f60',
        '',
      ]) {
        expect(
          PushService.routeForNotificationData(
            {'type': 'ticket', 'ticket_id': bad},
          ),
          PushIntent.ticket.baseRoute,
          reason: 'ticket_id=$bad',
        );
      }
    });

    test('a valid-looking UUID with a suffix is rejected whole', () {
      expect(
        isAppOwnedPushRoute(
          '/support/3f1c2b4a-5d6e-4f70-8a91-b2c3d4e5f607?token=x',
        ),
        isFalse,
      );
    });
  });

  group('unknown or absent codes land on the safe default', () {
    test('an unrecognised code opens the notifications inbox', () {
      expect(
        PushService.routeForNotificationData({'type': 'account_notice'}),
        '/dashboard/notifications',
      );
      expect(
        PushService.routeForNotificationData({'type': 'totally_unknown_code'}),
        '/dashboard/notifications',
      );
    });

    test('an empty payload opens the notifications inbox', () {
      expect(
        PushService.routeForNotificationData(const {}),
        PushIntent.inbox.baseRoute,
      );
    });
  });

  group('code-less queued pushes keep their existing destinations', () {
    // app/tasks/notifications.py:1133 dispatches queued pushes with NO `data`
    // argument, so the device receives only `notification_id` plus the
    // subject/body text — text classification is the only routing signal that
    // fires for lifecycle traffic today. It may only SELECT from the closed
    // intent set; it never contributes to the route string.
    test('the real wire payload is notification_id plus text', () {
      expect(
        PushService.routeForNotificationData(
          {'notification_id': '3f1c2b4a-5d6e-4f70-8a91-b2c3d4e5f607'},
          title: 'Invoice #1042 is now due',
          body: 'A reminder about invoice #1042.',
        ),
        '/billing',
      );
    });

    test('billing, usage, support and chat alerts still reach their tabs', () {
      void expectText(String title, String expected) {
        expect(
          PushService.routeForNotificationData(const {}, title: title),
          expected,
          reason: title,
        );
      }

      expectText('New invoice #1042', '/billing');
      expectText('Payment failed — please retry', '/billing');
      expectText('Service suspended', '/billing');
      expectText('Data usage warning — 80% used', '/usage');
      expectText('Ticket #12 updated', '/support');
      expectText('New support message', '/support/chat');
      expectText('Welcome aboard', '/dashboard/notifications');
    });
  });

  group('isAppOwnedPushRoute', () {
    test('accepts every declared base route', () {
      for (final intent in PushIntent.values) {
        expect(
          isAppOwnedPushRoute(intent.baseRoute),
          isTrue,
          reason: intent.name,
        );
      }
    });

    test('rejects auth and payment routes a push must never open', () {
      for (final route in const [
        '/reset-password',
        '/reset-password?token=x',
        '/login',
        '/mfa',
        '/lock',
        '/pay',
        '/topup',
        '/support/chat/../../reset-password',
      ]) {
        expect(isAppOwnedPushRoute(route), isFalse, reason: route);
      }
    });
  });

  group('breadcrumb path redaction', () {
    test('the FCM token in the push-token DELETE path is redacted', () {
      expect(
        redactSensitivePath('/me/push-tokens/fcm-tok-123'),
        '/me/push-tokens/<redacted>',
      );
    });

    test('ordinary paths are left intact', () {
      for (final path in const [
        '/me/push-tokens',
        '/billing/invoices/abc',
        '/auth/refresh',
      ]) {
        expect(redactSensitivePath(path), path);
      }
    });
  });
}
