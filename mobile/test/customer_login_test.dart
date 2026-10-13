import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:dotmac_portal/src/core/api_client.dart';
import 'package:dotmac_portal/src/core/api_exception.dart';
import 'package:dotmac_portal/src/core/token_storage.dart';
import 'package:dotmac_portal/src/features/auth/login_screen.dart';
import 'package:dotmac_portal/src/models/auth.dart';
import 'package:dotmac_portal/src/providers/auth_controller.dart';
import 'package:dotmac_portal/src/repositories/auth_repository.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _Adapter implements HttpClientAdapter {
  _Adapter(this.respond);
  final ResponseBody Function(RequestOptions) respond;

  @override
  Future<ResponseBody> fetch(RequestOptions options,
          Stream<Uint8List>? requestStream, Future<void>? cancelFuture) async =>
      respond(options);

  @override
  void close({bool force = false}) {}
}

ResponseBody _json(Map<String, Object?> value, int status) =>
    ResponseBody.fromString(jsonEncode(value), status, headers: {
      Headers.contentTypeHeader: [Headers.jsonContentType]
    });

class _ProfileRetryController extends AuthController {
  _ProfileRetryController(super.ref);
  int logins = 0;
  int profileRetries = 0;

  @override
  Future<LoginResult> login(
      {required String username, required String password}) async {
    logins += 1;
    throw ApiException(
        'Login accepted, but your account could not be loaded. Please retry.',
        statusCode: 401,
        code: 'LOGIN_PROFILE_UNAVAILABLE');
  }

  @override
  Future<void> completeLoginProfile() async {
    profileRetries += 1;
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('plugins.it_nomads.com/flutter_secure_storage');
  late Map<String, String> store;

  setUp(() {
    store = {};
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      final args = (call.arguments as Map?) ?? const {};
      final key = args['key'] as String?;
      switch (call.method) {
        case 'read':
          return store[key];
        case 'write':
          store[key!] = args['value'] as String;
          return null;
        case 'delete':
          store.remove(key);
          return null;
        case 'deleteAll':
          store.clear();
          return null;
        case 'containsKey':
          return store.containsKey(key);
        case 'readAll':
          return Map<String, String>.from(store);
      }
      return null;
    });
  });
  tearDown(() => TestDefaultBinaryMessengerBinding
      .instance.defaultBinaryMessenger
      .setMockMethodCallHandler(channel, null));

  test('customer login uses the customer endpoint and preserves password bytes',
      () async {
    final storage = TokenStorage();
    final client = ApiClient(storage: storage);
    const password = '  Exact password!☃  ';
    client.dio.httpClientAdapter = _Adapter((options) {
      expect(options.path, '/customer/auth/login');
      expect(options.uri.path, '/api/v1/customer/auth/login');
      expect(options.data,
          {'username': 'customer@example.test', 'password': password});
      expect(options.headers['X-Auth-Refresh-In-Body'], 'true');
      expect(options.headers['Authorization'], isNull);
      expect(
          options.headers['X-Request-ID'], matches(RegExp(r'^[0-9a-f-]{36}$')));
      return _json({
        'access_token': 'customer-access',
        'refresh_token': 'customer-refresh'
      }, 200);
    });
    final result = await AuthRepository(dio: client.dio, storage: storage)
        .login(username: 'customer@example.test', password: password);
    expect(result.isAuthenticated, isTrue);
    expect(await storage.readAccessToken(), 'customer-access');
  });

  test('customer MFA uses the customer-bound verification endpoint', () async {
    final storage = TokenStorage();
    final client = ApiClient(storage: storage);
    client.dio.httpClientAdapter = _Adapter((options) {
      expect(options.path, '/customer/auth/mfa/verify');
      expect(options.data, {'mfa_token': 'bound-challenge', 'code': '123456'});
      expect(options.headers['Authorization'], isNull);
      return _json({
        'access_token': 'customer-access',
        'refresh_token': 'customer-refresh'
      }, 200);
    });
    await AuthRepository(dio: client.dio, storage: storage)
        .verifyMfa(mfaToken: 'bound-challenge', code: '123456');
    expect(await storage.readAccessToken(), 'customer-access');
  });

  test('customer login preserves safe server error code and correlation',
      () async {
    final storage = TokenStorage();
    final client = ApiClient(storage: storage);
    client.dio.httpClientAdapter = _Adapter((options) => _json({
          'code': 'auth.customer_authentication.locked',
          'message': 'Account locked. Try again in 15 minutes.',
          'request_id': options.headers['X-Request-ID'],
        }, 403));
    try {
      await AuthRepository(dio: client.dio, storage: storage).login(
          username: 'customer@example.test', password: 'synthetic-password');
      fail('Locked account must be refused');
    } on ApiException catch (error) {
      expect(error.statusCode, 403);
      expect(error.code, 'auth.customer_authentication.locked');
      expect(error.requestId, isNotEmpty);
    }
    expect(await storage.readAccessToken(), isNull);
  });

  test('missing, staff and reseller profile kinds are not customer identities',
      () {
    for (final kind in [null, 'system_user', 'vendor', 'reseller']) {
      final me = Me.fromJson(
          {'id': 'synthetic-id', if (kind != null) 'user_type': kind});
      expect(me.isCustomer, isFalse);
    }
    expect(
        Me.fromJson({'id': 'customer-id', 'user_type': 'customer'}).isCustomer,
        isTrue);
  });

  testWidgets(
      'profile failure retries the profile without another password attempt',
      (tester) async {
    late _ProfileRetryController controller;
    await tester.pumpWidget(ProviderScope(
      overrides: [
        authControllerProvider.overrideWith((ref) {
          controller = _ProfileRetryController(ref);
          return controller;
        })
      ],
      child: const MaterialApp(home: LoginScreen()),
    ));
    await tester.enterText(
        find.byType(TextFormField).at(0), 'customer@example.test');
    await tester.enterText(
        find.byType(TextFormField).at(1), 'synthetic-password');
    await tester.tap(find.text('Sign in'));
    await tester.pumpAndSettle();
    expect(find.text('Incorrect username or password.'), findsNothing);
    expect(find.text('Retry loading account'), findsOneWidget);
    await tester.tap(find.text('Retry loading account'));
    await tester.pumpAndSettle();
    expect(controller.logins, 1);
    expect(controller.profileRetries, 1);
  });
}
