// ignore_for_file: avoid_print
// Live integration test — exercises the app's REAL networking layer
// (ApiClient + Dio interceptors, repositories, model fromJson) against a
// running backend, authenticated as a real SUBSCRIBER (customer portal model).
//
// Credentials are passed at run time so they are never baked into source:
//
//   flutter test test_live/live_backend_test.dart \
//     --dart-define=API_BASE_URL=http://localhost:8001 \
//     --dart-define=SUB_USER=<login> --dart-define=SUB_PASS=<password>
//
// Runs on the Dart VM (dart:io HttpClient) so there is no CORS restriction.
import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/core/api_client.dart';
import 'package:dotmac_portal/src/core/token_storage.dart';
import 'package:dotmac_portal/src/repositories/auth_repository.dart';
import 'package:dotmac_portal/src/repositories/billing_repository.dart';
import 'package:dotmac_portal/src/repositories/catalog_repository.dart';
import 'package:dotmac_portal/src/repositories/notification_repository.dart';
import 'package:dotmac_portal/src/repositories/usage_repository.dart';

const _user = String.fromEnvironment('SUB_USER');
const _pass = String.fromEnvironment('SUB_PASS');

/// In-memory TokenStorage so the test needs no flutter_secure_storage plugin.
class MemoryTokenStorage extends TokenStorage {
  final Map<String, String> _m = {};
  @override
  Future<void> save({required String accessToken, String? refreshToken}) async {
    _m['a'] = accessToken;
    if (refreshToken != null) _m['r'] = refreshToken;
  }

  @override
  Future<String?> readAccessToken() async => _m['a'];
  @override
  Future<String?> readRefreshToken() async => _m['r'];
  @override
  Future<void> clear() async => _m.clear();
}

void main() {
  late MemoryTokenStorage storage;
  late ApiClient api;
  late AuthRepository auth;
  late BillingRepository billing;
  late CatalogRepository catalog;
  late UsageRepository usage;

  setUp(() {
    storage = MemoryTokenStorage();
    api = ApiClient(storage: storage);
    auth = AuthRepository(dio: api.dio, storage: storage);
    billing = BillingRepository(api.dio);
    catalog = CatalogRepository(api.dio);
    usage = UsageRepository(api.dio);
  });

  test('subscriber logs in and reads their own data via /me/* endpoints',
      () async {
    // 1. Subscriber login (customer portal — provider=local).
    final result =
        await auth.login(username: _user, password: _pass, provider: 'local');
    expect(result.isAuthenticated, isTrue);
    // Mobile clients must receive (and persist) a refresh token — the app sends
    // the X-Auth-Refresh-In-Body header so it arrives in the JSON body.
    final storedRefresh = await storage.readRefreshToken();
    expect(storedRefresh, isNotNull,
        reason: 'refresh token must be delivered in-body to native clients');
    print('✅ login ok (subscriber) — refresh token stored');

    // 2. /auth/me — confirms this is a customer, not admin.
    final me = await auth.me();
    print('✅ /auth/me — ${me.fullName} <${me.email}> roles=${me.roles}');
    expect(me.roles, isEmpty,
        reason: 'a real subscriber carries no staff roles/scopes');

    // 3. Self-scoped reads — the exact repo calls the UI tabs make.
    final invoices = await billing.invoices(limit: 5);
    print(
        '✅ /me/invoices — count=${invoices.count}, fetched=${invoices.items.length}');
    if (invoices.items.isNotEmpty) {
      final inv = invoices.items.first;
      print('   e.g. ${inv.currency} ${inv.total} status=${inv.status} '
          'due=${inv.dueAt}');
    }
    expect(invoices.count, greaterThan(0),
        reason:
            'subscriber should see their OWN invoices without staff scopes');

    final subs = await catalog.subscriptions(limit: 5);
    print('✅ /me/subscriptions — count=${subs.count}');
    expect(subs.count, greaterThan(0));

    final payments = await billing.payments(limit: 5);
    print('✅ /me/payments — count=${payments.count}');

    final quota = await usage.quotaBuckets();
    print('✅ /me/quota-buckets — count=${quota.count} (empty: no quota data)');

    final sessions = await usage.sessions();
    final down =
        sessions.items.fold<int>(0, (s, e) => s + (e.outputOctets ?? 0));
    final up = sessions.items.fold<int>(0, (s, e) => s + (e.inputOctets ?? 0));
    print('✅ /me/radius-accounting-sessions — count=${sessions.count} '
        '(↓ ${(down / 1e9).toStringAsFixed(2)} GB ↑ ${(up / 1e9).toStringAsFixed(2)} GB)');

    final notifications = await NotificationRepository(api.dio).list(limit: 5);
    print('✅ /me/notifications — count=${notifications.count}');

    final ledger = await billing.ledger(limit: 10);
    print('✅ /me/ledger — count=${ledger.items.length}'
        '${ledger.items.isNotEmpty ? ' (e.g. ${ledger.items.first.entryType} '
            '${ledger.items.first.amount})' : ''}');

    final balance = await billing.balance();
    print('✅ /me/balance — credit=${balance.creditBalance} '
        '${balance.currency}');

    final topup = await billing.topupPage();
    print('✅ /me/topup — balance=${topup.prepaidBalance} '
        'presets=${topup.presetAmounts}');
    expect(topup.minAmount, greaterThan(0));

    if (subs.items.isNotEmpty) {
      final opts = await catalog.planChangeOptions(subs.items.first.id);
      print('✅ /me/.../plan-change — current=${opts.currentOffer?.name} '
          'available=${opts.availableOffers.length}');

      final addons = await catalog.addons(subs.items.first.id);
      print('✅ /me/.../add-ons — available=${addons.available.length} '
          'active=${addons.active.length}');
    }
  }, timeout: const Timeout(Duration(seconds: 30)), skip: _user.isEmpty);

  test('access token refreshes and rotates via the stored refresh token',
      () async {
    await auth.login(username: _user, password: _pass, provider: 'local');
    final oldRefresh = await storage.readRefreshToken();
    expect(oldRefresh, isNotNull);

    final res = await api.dio.post(
      '/auth/refresh',
      data: {'refresh_token': oldRefresh},
    );
    expect(res.statusCode, 200);
    expect(res.data['access_token'], isNotNull);
    final newRefresh = res.data['refresh_token'] as String?;
    expect(newRefresh, isNotNull,
        reason: 'refresh must rotate and return the new token in-body');
    expect(newRefresh, isNot(oldRefresh), reason: 'refresh token must rotate');
    print('✅ /auth/refresh — new access token issued, refresh rotated');
  }, timeout: const Timeout(Duration(seconds: 30)), skip: _user.isEmpty);

  test('lists the caller\'s active sessions with one marked current', () async {
    await auth.login(username: _user, password: _pass, provider: 'local');
    final sessions = await auth.sessions();
    expect(sessions, isNotEmpty);
    expect(sessions.where((s) => s.isCurrent).length, greaterThanOrEqualTo(1));
    print('✅ /auth/me/sessions — ${sessions.length} session(s), '
        '${sessions.where((s) => s.isCurrent).length} current');
  }, timeout: const Timeout(Duration(seconds: 30)), skip: _user.isEmpty);
}
