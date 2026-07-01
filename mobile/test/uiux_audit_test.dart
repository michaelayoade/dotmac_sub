import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/features/home/notifications_screen.dart';
import 'package:dotmac_portal/src/core/push_service.dart';
import 'package:dotmac_portal/src/models/auth.dart';
import 'package:dotmac_portal/src/models/invoice.dart';
import 'package:dotmac_portal/src/models/notification.dart';
import 'package:dotmac_portal/src/models/subscription.dart';
import 'package:dotmac_portal/src/providers/data_providers.dart';

void main() {
  group('Me profile cache round-trip', () {
    test('toJson/fromJson preserves identity for the cold-start cache', () {
      final me = Me(
        id: 'sub-1',
        firstName: 'Ada',
        lastName: 'Lovelace',
        email: 'ada@example.com',
        displayName: 'Ada L.',
        emailVerified: true,
        phone: '+2348000000000',
        locale: 'en',
        timezone: 'Africa/Lagos',
        roles: const ['customer'],
        scopes: const ['me:read'],
      );

      final restored = Me.fromJson(me.toJson());

      expect(restored.id, me.id);
      expect(restored.fullName, me.fullName);
      expect(restored.email, me.email);
      expect(restored.emailVerified, isTrue);
      expect(restored.phone, me.phone);
      expect(restored.locale, 'en');
      expect(restored.timezone, 'Africa/Lagos');
      expect(restored.roles, me.roles);
      expect(restored.scopes, me.scopes);
    });
  });

  group('InvoiceFilter', () {
    Invoice inv({required String status, double balance = 0, DateTime? due}) =>
        Invoice(
          id: 'inv',
          accountId: 'acct',
          status: status,
          currency: 'NGN',
          subtotal: 100,
          taxTotal: 0,
          total: 100,
          balanceDue: balance,
          dueAt: due,
        );

    final paid = inv(status: 'paid');
    final issued = inv(status: 'issued', balance: 100, due: _future);
    final overdue = inv(status: 'issued', balance: 100, due: _past);

    test('all matches everything', () {
      expect(InvoiceFilter.all.test(paid), isTrue);
      expect(InvoiceFilter.all.test(issued), isTrue);
    });

    test('unpaid excludes paid, includes anything owing', () {
      expect(InvoiceFilter.unpaid.test(paid), isFalse);
      expect(InvoiceFilter.unpaid.test(issued), isTrue);
      expect(InvoiceFilter.unpaid.test(overdue), isTrue);
    });

    test('overdue only past-due unpaid invoices', () {
      expect(InvoiceFilter.overdue.test(overdue), isTrue);
      expect(InvoiceFilter.overdue.test(issued), isFalse);
      expect(InvoiceFilter.overdue.test(paid), isFalse);
    });

    test('paid only settled invoices', () {
      expect(InvoiceFilter.paid.test(paid), isTrue);
      expect(InvoiceFilter.paid.test(overdue), isFalse);
    });
  });

  group('notificationRoute', () {
    AppNotification n({String? subject, String? eventType, String? category}) =>
        AppNotification(
          id: 'n',
          channel: 'push',
          status: 'delivered',
          subject: subject,
          eventType: eventType,
          category: category,
        );

    test('billing-flavoured notifications deep-link to /billing', () {
      expect(
          notificationRoute(n(subject: 'Your invoice is ready')), '/billing');
      expect(notificationRoute(n(eventType: 'service_suspended')), '/billing');
      expect(notificationRoute(n(subject: 'Payment received')), '/billing');
    });

    test('support and usage notifications route to their tabs', () {
      expect(notificationRoute(n(subject: 'Ticket #12 updated')), '/support');
      expect(notificationRoute(n(eventType: 'quota_threshold')), '/usage');
    });

    test('chat message notifications route to the live chat', () {
      expect(
        notificationRoute(n(eventType: 'message.outbound')),
        '/support/chat',
      );
      expect(
        notificationRoute(n(subject: 'New support message')),
        '/support/chat',
      );
    });

    test('returns null when nothing is actionable', () {
      expect(notificationRoute(n(subject: 'Welcome aboard')), isNull);
    });
  });

  group('push notification routing', () {
    test('honours explicit internal routes from FCM data', () {
      expect(
        PushService.routeForNotificationData({'route': '/support/chat'}),
        '/support/chat',
      );
      expect(
        PushService.routeForNotificationData(
          {'deep_link': 'dotmac://open/billing'},
        ),
        '/billing',
      );
    });

    test('routes chat-shaped push payloads to live chat', () {
      expect(
        PushService.routeForNotificationData(
          {'event_type': 'message.outbound'},
        ),
        '/support/chat',
      );
      expect(
        PushService.routeForNotificationData({'type': 'chat_message'}),
        '/support/chat',
      );
      expect(
        PushService.routeForNotificationData(
          const {},
          title: 'New support message',
        ),
        '/support/chat',
      );
    });

    test('routes generic push payloads to the notifications inbox', () {
      expect(
        PushService.routeForNotificationData({'type': 'account_notice'}),
        '/dashboard/notifications',
      );
    });
  });

  group('pickCurrentService', () {
    Subscription sub(String id, {required String status, DateTime? startAt}) =>
        Subscription(
          id: id,
          accountId: 'acct',
          offerId: 'offer',
          status: status,
          billingMode: 'prepaid',
          startAt: startAt,
        );

    test('prefers an active subscription over an inactive one', () {
      final services = [
        sub('a', status: 'suspended', startAt: _future),
        sub('b', status: 'active', startAt: _past),
      ];
      expect(pickCurrentService(services).id, 'b');
    });

    test('among active, prefers the most recently started', () {
      final services = [
        sub('old', status: 'active', startAt: _past),
        sub('new', status: 'active', startAt: _future),
      ];
      expect(pickCurrentService(services).id, 'new');
    });
  });
}

final _past = DateTime.now().subtract(const Duration(days: 5));
final _future = DateTime.now().add(const Duration(days: 5));
