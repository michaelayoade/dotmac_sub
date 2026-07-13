import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/config/env.dart';
import 'package:dotmac_portal/src/models/auth.dart';
import 'package:dotmac_portal/src/models/addon.dart';
import 'package:dotmac_portal/src/models/invoice.dart';
import 'package:dotmac_portal/src/models/ledger.dart';
import 'package:dotmac_portal/src/models/notification.dart';
import 'package:dotmac_portal/src/models/page.dart';
import 'package:dotmac_portal/src/models/payment_method.dart';
import 'package:dotmac_portal/src/models/payment_flow.dart';
import 'package:dotmac_portal/src/models/plan_change.dart';
import 'package:dotmac_portal/src/models/service_status.dart';
import 'package:dotmac_portal/src/models/session.dart';
import 'package:dotmac_portal/src/models/subscription.dart';
import 'package:dotmac_portal/src/models/usage.dart';

void main() {
  group('LoginResult', () {
    test('parses a token response', () {
      final r = LoginResult.fromJson({
        'access_token': 'a',
        'refresh_token': 'b',
        'token_type': 'bearer',
      });
      expect(r.isAuthenticated, isTrue);
      expect(r.mfaRequired, isFalse);
    });

    test('parses an MFA challenge', () {
      final r = LoginResult.fromJson({
        'mfa_required': true,
        'mfa_token': 'tok',
      });
      expect(r.isAuthenticated, isFalse);
      expect(r.mfaToken, 'tok');
    });
  });

  group('Me', () {
    test('derives initials and full name', () {
      final me = Me.fromJson({
        'id': '11111111-1111-1111-1111-111111111111',
        'first_name': 'Ada',
        'last_name': 'Obi',
        'email': 'ada@example.com',
      });
      expect(me.initials, 'AO');
      expect(me.fullName, 'Ada Obi');
    });

    test('prefers display_name when present', () {
      final me = Me.fromJson({
        'id': '1',
        'first_name': 'Ada',
        'last_name': 'Obi',
        'email': 'a@b.c',
        'display_name': 'Ada O.',
      });
      expect(me.fullName, 'Ada O.');
    });
  });

  group('Invoice', () {
    test('parses Decimal-as-string amounts and overdue state', () {
      final inv = Invoice.fromJson({
        'id': 'i1',
        'account_id': 'a1',
        'status': 'issued',
        'currency': 'NGN',
        'subtotal': '1000.00',
        'tax_total': '75.00',
        'total': '1075.00',
        'balance_due': '1075.00',
        'due_at': '2000-01-01T00:00:00Z', // in the past
      });
      expect(inv.total, 1075.0);
      expect(inv.isPaid, isFalse);
      expect(inv.isOverdue, isTrue);
    });

    test('treats zero balance as paid', () {
      final inv = Invoice.fromJson({
        'id': 'i2',
        'account_id': 'a1',
        'status': 'issued',
        'balance_due': '0.00',
        'total': '500.00',
      });
      expect(inv.isPaid, isTrue);
    });
  });

  group('QuotaBucket', () {
    test('computes fraction and remaining for a capped plan', () {
      final b = QuotaBucket.fromJson({
        'id': 'b1',
        'subscription_id': 's1',
        'period_start': '2026-06-01T00:00:00Z',
        'period_end': '2026-07-01T00:00:00Z',
        'included_gb': '100',
        'used_gb': '25',
        'rollover_gb': '0',
        'overage_gb': '0',
      });
      expect(b.isUnlimited, isFalse);
      expect(b.allowanceGb, 100);
      expect(b.remainingGb, 75);
      expect(b.usedFraction, closeTo(0.25, 1e-9));
    });

    test('unlimited when included_gb is null', () {
      final b = QuotaBucket.fromJson({
        'id': 'b2',
        'subscription_id': 's1',
        'period_start': '2026-06-01T00:00:00Z',
        'period_end': '2026-07-01T00:00:00Z',
        'used_gb': '40',
      });
      expect(b.isUnlimited, isTrue);
      expect(b.usedFraction, isNull);
    });
  });

  group('Page', () {
    test('parses envelope and computes hasMore', () {
      final page = Page.fromJson(
        {
          'items': [
            {'id': 'i1', 'account_id': 'a', 'balance_due': '0', 'total': '0'},
          ],
          'count': 5,
          'limit': 1,
          'offset': 0,
        },
        Invoice.fromJson,
      );
      expect(page.items, hasLength(1));
      expect(page.hasMore, isTrue);
    });
  });

  group('Env.resolveUrl', () {
    test('prefixes relative paths with the base url', () {
      expect(Env.resolveUrl('/static/avatars/x.png'),
          '${Env.apiBaseUrl}/static/avatars/x.png');
    });
    test('leaves absolute urls unchanged', () {
      expect(Env.resolveUrl('https://cdn.example.com/a.png'),
          'https://cdn.example.com/a.png');
    });
  });

  group('Subscription', () {
    test('parses plan type, IP and prepaid flag', () {
      final s = Subscription.fromJson({
        'id': 's1',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'billing_mode': 'prepaid',
        'ipv4_address': '10.11.128.186',
        'offer': {
          'name': 'unlimited 3',
          'service_type': 'business',
          'access_type': 'fiber',
        },
      });
      expect(s.ipv4Address, '10.11.128.186');
      expect(s.isPrepaid, isTrue);
      expect(s.planType, 'business · fiber');
      expect(s.displayName, 'unlimited 3');
    });

    test('computes negative days for an expired service', () {
      final s = Subscription.fromJson({
        'id': 's2',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'suspended',
        'billing_mode': 'prepaid',
        'next_billing_at': '2021-03-16T00:00:00Z',
      });
      expect(s.expiresAt, isNotNull);
      expect(s.daysUntilExpiry, isNotNull);
      expect(s.daysUntilExpiry! < 0, isTrue);
    });

    test('postpaid never expires on next_billing_at (no false expiry)', () {
      final s = Subscription.fromJson({
        'id': 's3',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'billing_mode': 'postpaid',
        'next_billing_at': '2020-01-01T00:00:00Z',
      });
      expect(s.hasExpiry, isFalse);
      expect(s.expiresAt, isNull);
      expect(s.daysUntilExpiry, isNull);
      expect(s.isExpired, isFalse);
      expect(s.expiresSoon, isFalse);
    });

    test('active service is never expired despite a stale billing date', () {
      final s = Subscription.fromJson({
        'id': 's4',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'billing_mode': 'prepaid',
        'next_billing_at': '2020-01-01T00:00:00Z',
      });
      expect(s.daysUntilExpiry! < 0, isTrue);
      expect(s.isExpired, isFalse, reason: 'active = running, not expired');
    });

    test('non-active prepaid with a past validity date is expired', () {
      final s = Subscription.fromJson({
        'id': 's5',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'suspended',
        'billing_mode': 'prepaid',
        'next_billing_at': '2020-01-01T00:00:00Z',
      });
      expect(s.isExpired, isTrue);
    });

    test('postpaid honours an explicit past contract end_at as expiry', () {
      final s = Subscription.fromJson({
        'id': 's6',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'stopped',
        'billing_mode': 'postpaid',
        'end_at': '2020-01-01T00:00:00Z',
      });
      expect(s.hasExpiry, isTrue);
      expect(s.isExpired, isTrue);
    });

    test('prefers server is_expired/expires_at when the backend provides them',
        () {
      // Server says: active, no date expiry (prepaid lapses on balance, not
      // next_billing_at). Client must trust it over local date math.
      final s = Subscription.fromJson({
        'id': 's9',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'billing_mode': 'prepaid',
        'next_billing_at': '2020-01-01T00:00:00Z',
        'expires_at': null,
        'is_expired': false,
      });
      expect(s.hasServerExpiry, isTrue);
      expect(s.expiresAt, isNull);
      expect(s.isExpired, isFalse);
    });

    test('falls back to local logic when server fields are absent', () {
      final s = Subscription.fromJson({
        'id': 's10',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'billing_mode': 'prepaid',
        'next_billing_at': '2020-01-01T00:00:00Z',
      });
      expect(s.hasServerExpiry, isFalse);
      expect(
          s.expiresAt, isNotNull); // local fallback for older/offline backend
      expect(s.isExpired, isFalse); // active is never expired
    });

    test('expiresSoon only inside the 3-day window for date-based expiry', () {
      final soon = Subscription.fromJson({
        'id': 's7',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'billing_mode': 'prepaid',
        'next_billing_at':
            DateTime.now().add(const Duration(days: 2)).toIso8601String(),
      });
      expect(soon.expiresSoon, isTrue);
      final postpaid = Subscription.fromJson({
        'id': 's8',
        'account_id': 'a1',
        'offer_id': 'o1',
        'status': 'active',
        'billing_mode': 'postpaid',
        'next_billing_at':
            DateTime.now().add(const Duration(days: 2)).toIso8601String(),
      });
      expect(postpaid.expiresSoon, isFalse);
    });

    Subscription withStatus(String status) => Subscription(
          id: 's',
          accountId: 'a',
          offerId: 'o',
          status: status,
          billingMode: 'prepaid',
        );

    test('isCurrent excludes terminal/historical statuses', () {
      for (final status in [
        'pending',
        'active',
        'blocked',
        'suspended',
        'stopped'
      ]) {
        expect(withStatus(status).isCurrent, isTrue, reason: status);
      }
      for (final status in [
        'disabled',
        'canceled',
        'expired',
        'hidden',
        'archived'
      ]) {
        expect(withStatus(status).isCurrent, isFalse, reason: status);
      }
    });
  });

  group('PlanChangeQuote', () {
    test('parses a prepaid proration quote', () {
      final q = PlanChangeQuote.fromJson({
        'charge_amount': 5000.0,
        'net_amount': 2900.0,
        'current_balance': 2100.0,
        'shortfall': 0.0,
        'days_remaining': 12,
        'can_apply_immediately': true,
        'is_upgrade': true,
        'is_downgrade': false,
      });
      expect(q.hasProration, isTrue);
      expect(q.isUpgrade, isTrue);
      expect(q.needsTopUp, isFalse);
      expect(q.netAmount, 2900.0);
    });

    test('empty quote => no proration (postpaid)', () {
      final q = PlanChangeQuote.fromJson(const {});
      expect(q.hasProration, isFalse);
    });

    test('flags a shortfall as needing top-up', () {
      final q = PlanChangeQuote.fromJson({
        'net_amount': 5000.0,
        'shortfall': 3000.0,
      });
      expect(q.needsTopUp, isTrue);
    });
  });

  group('LedgerTxn', () {
    test('credit entry: sign/colour flag + payment title', () {
      final t = LedgerTxn.fromJson({
        'id': 'l1',
        'entry_type': 'credit',
        'source': 'payment',
        'amount': '52000.00',
        'currency': 'NGN',
        'memo': 'Zenith 461 Bank',
        'created_at': '2026-03-15T10:00:00Z',
      });
      expect(t.isCredit, isTrue);
      expect(t.amount, 52000.0);
      expect(t.title, 'Zenith 461 Bank');
    });

    test('debit falls back to source label when memo is empty', () {
      final t = LedgerTxn.fromJson({
        'id': 'l2',
        'entry_type': 'debit',
        'source': 'invoice',
        'amount': 56437.5,
        'currency': 'NGN',
      });
      expect(t.isCredit, isFalse);
      expect(t.title, 'Charge');
    });
  });

  group('AccountBalance', () {
    test('positive credit / negative owes', () {
      expect(AccountBalance.fromJson({'credit_balance': '2071.49'}).inCredit,
          isTrue);
      expect(AccountBalance.fromJson({'credit_balance': -500}).owes, isTrue);
      final zero = AccountBalance.fromJson({'credit_balance': '0.00'});
      expect(zero.inCredit, isFalse);
      expect(zero.owes, isFalse);
    });
  });

  group('Add-ons', () {
    test('AddonsAvailable parses options + wallet (Decimal-as-string)', () {
      final d = AddonsAvailable.fromJson({
        'available': [
          {
            'add_on_id': 'a1',
            'name': 'Static IP',
            'addon_type': 'static_ip',
            'amount': 2000.0,
            'currency': 'NGN',
            'min_quantity': 1,
            'max_quantity': 3,
          }
        ],
        'active': [
          {'id': 's1', 'add_on_id': 'a1', 'name': 'Static IP', 'quantity': 2}
        ],
        'wallet_balance': '2071.49',
        'currency': 'NGN',
      });
      expect(d.available.single.maxQuantity, 3);
      expect(d.active.single.quantity, 2);
      expect(d.walletBalance, 2071.49);
    });

    test('AddonPurchaseResult flags insufficient balance', () {
      final r = AddonPurchaseResult.fromJson({
        'success': false,
        'reason': 'insufficient_balance',
        'charge': '4000.00',
        'shortfall': '2000.00',
        'currency': 'NGN',
      });
      expect(r.success, isFalse);
      expect(r.insufficient, isTrue);
      expect(r.shortfall, 2000.0);
    });
  });

  group('SavedCard', () {
    test('derives title and expiry, defaults', () {
      final c = SavedCard.fromJson({
        'id': 'p1',
        'method_type': 'card',
        'label': 'Visa •••• 4081',
        'last4': '4081',
        'brand': 'visa',
        'expires_month': 8,
        'expires_year': 2030,
        'is_default': true,
      });
      expect(c.title, 'Visa •••• 4081');
      expect(c.expiry, '08/30');
      expect(c.isDefault, isTrue);
    });

    test('falls back to brand + last4 when no label', () {
      final c = SavedCard.fromJson(
          {'id': 'p2', 'brand': 'mastercard', 'last4': '1234'});
      expect(c.title, 'mastercard •••• 1234');
      expect(c.expiry, isNull);
    });
  });

  group('AppNotification', () {
    test('uses the subject as the title', () {
      final n = AppNotification.fromJson({
        'id': 'n1',
        'channel': 'email',
        'status': 'queued',
        'is_read': true,
        'subject': 'Service suspended — action required',
        'event_type': 'service_suspended',
      });
      expect(n.title, 'Service suspended — action required');
      expect(n.isRead, isTrue);
    });

    test('falls back to a humanised event_type when no subject', () {
      final n = AppNotification.fromJson({
        'id': 'n2',
        'channel': 'sms',
        'status': 'delivered',
        'event_type': 'payment_received',
      });
      expect(n.title, 'payment received');
      expect(n.isRead, isFalse);
    });
  });

  group('AuthSessionInfo', () {
    test('labels a native-app session and flags current', () {
      final s = AuthSessionInfo.fromJson({
        'id': 's1',
        'status': 'active',
        'is_current': true,
        'ip_address': '102.89.1.2',
        'user_agent': 'Dart/3.12 (dart:io)',
      });
      expect(s.isCurrent, isTrue);
      expect(s.deviceLabel, 'Mobile app');
    });

    test('derives os/browser from a desktop user agent', () {
      final s = AuthSessionInfo.fromJson({
        'id': 's2',
        'status': 'active',
        'user_agent':
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120 Safari/537.36',
      });
      expect(s.isCurrent, isFalse);
      expect(s.deviceLabel, contains('Macintosh'));
      expect(s.deviceLabel, contains('Chrome'));
    });
  });

  group('Payment flow', () {
    test('parses initiation', () {
      final init = PaymentInitiation.fromJson({
        'invoice_id': 'i1',
        'amount': '2500.00',
        'currency': 'NGN',
        'provider_type': 'paystack',
        'provider_public_key': 'pk',
        'payment_reference': 'ref',
        'customer_email': 'c@e.com',
      });
      expect(init.amount, 2500.0);
      expect(init.providerType, 'paystack');
      expect(init.paymentReference, 'ref');
    });

    test('parses verification success', () {
      final v = PaymentVerification.fromJson({
        'reference': 'ref',
        'payment_id': 'p1',
        'amount': '2500.00',
        'currency': 'NGN',
        'status': 'succeeded',
        'already_recorded': false,
      });
      expect(v.succeeded, isTrue);
      expect(v.paymentId, 'p1');
    });
  });

  group('AccountingSession framed IP + LiveBandwidth', () {
    test('parses framed_ip_address from the session payload', () {
      final s = AccountingSession.fromJson({
        'id': '1',
        'subscription_id': 'sub',
        'session_id': 'sess',
        'status_type': 'start',
        'framed_ip_address': '100.64.3.7',
      });
      expect(s.framedIpAddress, '100.64.3.7');
    });

    test('LiveBandwidth binds subscriber-perspective fields only', () {
      final b = LiveBandwidth.fromJson({
        'current_rx_bps': 1, // NAS perspective — intentionally ignored
        'download_bps': 12000000.0,
        'upload_bps': 3000000.0,
      });
      expect(b.downloadBps, 12000000.0);
      expect(b.uploadBps, 3000000.0);
      expect(b.hasSignal, isTrue);
      expect(LiveBandwidth.fromJson(const {}).hasSignal, isFalse);
    });
  });

  group('ServiceStatus', () {
    test('prepaid low balance flags a renewal with the grace cut-off', () {
      final s = ServiceStatus.fromJson({
        'billing_mode': 'prepaid',
        'currency': 'NGN',
        'balance': '50.00',
        'min_balance': '100.00',
        'low_balance': true,
        'grace_until': '2026-07-01T00:00:00Z',
        'primary_action': {
          'kind': 'top_up',
          'label': 'Top up',
          'message': 'Balance low — top up NGN 50.00 to keep your service.',
          'amount': '50.00',
          'currency': 'NGN',
          'restores_service': false,
        },
        'services': [
          {
            'subscription_id': 's1',
            'status': 'active',
            'billing_mode': 'prepaid',
            'usable': true,
            'reason': 'low_balance',
            'action': {
              'kind': 'top_up',
              'label': 'Top up',
              'message': 'Balance low — top up NGN 50.00 to keep your service.',
              'amount': '50.00',
              'currency': 'NGN',
              'restores_service': false,
            },
          }
        ],
      });
      expect(s.isPrepaid, isTrue);
      expect(s.balance, 50.0);
      expect(s.lowBalance, isTrue);
      expect(s.graceUntil, isNotNull);
      expect(s.needsRenewal, isTrue);
      expect(s.services.single.actionable, isTrue);
      expect(s.primaryAction?.kind, 'top_up');
      expect(s.primaryAction?.amount, 50.0);
      expect(s.primaryAction?.restoresService, isFalse);
    });

    test('healthy account does not flag a renewal', () {
      final s = ServiceStatus.fromJson({
        'billing_mode': 'postpaid',
        'in_dunning': false,
        'services': [
          {
            'subscription_id': 's1',
            'status': 'active',
            'billing_mode': 'postpaid',
            'usable': true,
            'reason': 'ok',
          }
        ],
      });
      expect(s.needsRenewal, isFalse);
      expect(s.services.single.actionable, isFalse);
    });

    test('manual suspension directs support and never implies payment restore',
        () {
      final s = ServiceStatus.fromJson({
        'billing_mode': 'postpaid',
        'primary_action': {
          'kind': 'contact_support',
          'label': 'Contact support',
          'message': 'This hold cannot be cleared by payment.',
          'currency': 'NGN',
          'restores_service': false,
        },
        'services': [
          {
            'subscription_id': 's1',
            'status': 'suspended',
            'billing_mode': 'postpaid',
            'usable': false,
            'reason': 'administrative_hold',
            'action': {
              'kind': 'contact_support',
              'label': 'Contact support',
              'message': 'This hold cannot be cleared by payment.',
              'currency': 'NGN',
              'restores_service': false,
            },
          }
        ],
      });

      expect(s.unavailableServices, hasLength(1));
      expect(s.needsRenewal, isFalse);
      expect(s.primaryAction?.kind, 'contact_support');
      expect(s.services.single.action?.isFinancial, isFalse);
      expect(s.services.single.action?.restoresService, isFalse);
    });
  });
}
