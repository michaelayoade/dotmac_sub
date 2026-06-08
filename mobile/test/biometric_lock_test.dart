import 'package:dio/dio.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/core/biometric_service.dart';
import 'package:dotmac_portal/src/core/token_storage.dart';
import 'package:dotmac_portal/src/models/auth.dart';
import 'package:dotmac_portal/src/providers/auth_controller.dart';
import 'package:dotmac_portal/src/repositories/auth_repository.dart';

/// Biometric service stub — no platform channel, scripted availability/result.
class _FakeBiometric extends BiometricService {
  _FakeBiometric({this.available = true, this.willAuthenticate = true});

  bool available;
  bool willAuthenticate;
  int authCalls = 0;

  @override
  Future<bool> isAvailable() async => available;

  @override
  Future<bool> authenticate({required String reason}) async {
    authCalls++;
    return willAuthenticate;
  }
}

/// Auth repository stub — returns a fixed user, never hits the network.
class _FakeAuthRepository extends AuthRepository {
  _FakeAuthRepository(TokenStorage storage)
      : super(dio: Dio(), storage: storage);

  int logoutCalls = 0;

  @override
  Future<Me> me() async =>
      Me(id: '1', firstName: 'A', lastName: 'B', email: 'a@b.c');

  @override
  Future<void> logout() async => logoutCalls++;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  // In-memory mock for flutter_secure_storage so the real TokenStorage runs.
  const channel = MethodChannel('plugins.it_nomads.com/flutter_secure_storage');
  late Map<String, String> store;

  setUp(() {
    store = <String, String>{};
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      final args = (call.arguments as Map?) ?? const {};
      switch (call.method) {
        case 'write':
          store[args['key'] as String] = args['value'] as String;
          return null;
        case 'read':
          return store[args['key'] as String];
        case 'readAll':
          return Map<String, String>.from(store);
        case 'delete':
          store.remove(args['key'] as String);
          return null;
        case 'deleteAll':
          store.clear();
          return null;
        case 'containsKey':
          return store.containsKey(args['key'] as String);
        default:
          return null;
      }
    });
  });

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null);
  });

  ProviderContainer build(
      TokenStorage ts, _FakeBiometric bio, _FakeAuthRepository repo) {
    final c = ProviderContainer(overrides: [
      tokenStorageProvider.overrideWithValue(ts),
      biometricServiceProvider.overrideWithValue(bio),
      authRepositoryProvider.overrideWithValue(repo),
    ]);
    addTearDown(c.dispose);
    return c;
  }

  group('TokenStorage biometric flag', () {
    test('survives a token clear (session-expiry) but disable removes it',
        () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a', refreshToken: 'r');
      await ts.setBiometricEnabled(true);

      await ts.clear();
      expect(await ts.readAccessToken(), isNull);
      expect(await ts.isBiometricEnabled(), isTrue,
          reason: 'lock opt-in must survive a token wipe');

      await ts.setBiometricEnabled(false);
      expect(await ts.isBiometricEnabled(), isFalse);
    });
  });

  group('AuthController biometric lock', () {
    test('no token -> signed out, not locked', () async {
      final ts = TokenStorage();
      final c = build(ts, _FakeBiometric(), _FakeAuthRepository(ts));
      await c.read(authControllerProvider.notifier).bootstrap();

      final s = c.read(authControllerProvider);
      expect(s.isAuthenticated, isFalse);
      expect(s.locked, isFalse);
    });

    test('token + opted-in + biometrics available -> locked on launch',
        () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      await ts.setBiometricEnabled(true);
      final c =
          build(ts, _FakeBiometric(available: true), _FakeAuthRepository(ts));
      await c.read(authControllerProvider.notifier).bootstrap();

      final s = c.read(authControllerProvider);
      expect(s.isAuthenticated, isTrue);
      expect(s.locked, isTrue);
    });

    test('token + not opted-in -> authenticated, not locked', () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      final c = build(ts, _FakeBiometric(), _FakeAuthRepository(ts));
      await c.read(authControllerProvider.notifier).bootstrap();

      final s = c.read(authControllerProvider);
      expect(s.isAuthenticated, isTrue);
      expect(s.locked, isFalse);
    });

    test('opted-in but biometrics unavailable -> not locked (no trap)',
        () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      await ts.setBiometricEnabled(true);
      final c =
          build(ts, _FakeBiometric(available: false), _FakeAuthRepository(ts));
      await c.read(authControllerProvider.notifier).bootstrap();

      expect(c.read(authControllerProvider).locked, isFalse);
    });

    test('unlock success clears the lock; failure keeps it', () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      await ts.setBiometricEnabled(true);

      final failBio = _FakeBiometric(willAuthenticate: false);
      final c = build(ts, failBio, _FakeAuthRepository(ts));
      final n = c.read(authControllerProvider.notifier);
      await n.bootstrap();
      expect(c.read(authControllerProvider).locked, isTrue);

      expect(await n.unlock(), isFalse);
      expect(c.read(authControllerProvider).locked, isTrue);

      failBio.willAuthenticate = true;
      expect(await n.unlock(), isTrue);
      expect(c.read(authControllerProvider).locked, isFalse);
    });

    test('lockOnResume re-locks an authenticated, unlocked session', () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      await ts.setBiometricEnabled(true);
      final c = build(ts, _FakeBiometric(), _FakeAuthRepository(ts));
      final n = c.read(authControllerProvider.notifier);
      await n.bootstrap();
      await n.unlock();
      expect(c.read(authControllerProvider).locked, isFalse);

      await n.lockOnResume();
      expect(c.read(authControllerProvider).locked, isTrue);
    });

    test('lockOnResume is a no-op when not opted-in', () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      final c = build(ts, _FakeBiometric(), _FakeAuthRepository(ts));
      final n = c.read(authControllerProvider.notifier);
      await n.bootstrap();

      await n.lockOnResume();
      expect(c.read(authControllerProvider).locked, isFalse);
    });

    test('logout clears the biometric opt-in', () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      await ts.setBiometricEnabled(true);
      final repo = _FakeAuthRepository(ts);
      final c = build(ts, _FakeBiometric(), repo);
      final n = c.read(authControllerProvider.notifier);
      await n.bootstrap();

      await n.logout();
      expect(c.read(authControllerProvider).isAuthenticated, isFalse);
      expect(await ts.isBiometricEnabled(), isFalse);
      expect(repo.logoutCalls, 1);
    });
  });
}
