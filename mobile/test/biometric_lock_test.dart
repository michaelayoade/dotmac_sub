import 'dart:async';
import 'dart:convert';

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

/// Like [_FakeBiometric], but the prompt stays open until [gate] completes —
/// lets tests observe the controller while the prompt is in flight.
class _GatedBiometric extends _FakeBiometric {
  final gate = Completer<bool>();

  @override
  Future<bool> authenticate({required String reason}) {
    authCalls++;
    return gate.future;
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

/// Like [_FakeAuthRepository], but `/auth/me` hangs until [gate] completes —
/// simulates a slow network during bootstrap.
class _GatedAuthRepository extends _FakeAuthRepository {
  _GatedAuthRepository(super.storage);

  final gate = Completer<void>();

  @override
  Future<Me> me() async {
    await gate.future;
    return super.me();
  }
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

    test('logout clears the biometric opt-in and any stashed location',
        () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      await ts.setBiometricEnabled(true);
      final repo = _FakeAuthRepository(ts);
      final c = build(ts, _FakeBiometric(), repo);
      final n = c.read(authControllerProvider.notifier);
      await n.bootstrap();
      n.stashLockReturnLocation('/billing');

      await n.logout();
      expect(c.read(authControllerProvider).isAuthenticated, isFalse);
      expect(await ts.isBiometricEnabled(), isFalse);
      expect(repo.logoutCalls, 1);
      expect(n.takeLockReturnLocation(), isNull,
          reason: 'a later login must not bounce to a stale screen');
    });

    test('bootstrap resolving /auth/me after an unlock must not re-lock',
        () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      await ts.setBiometricEnabled(true);
      // Cached profile so bootstrap renders the optimistic locked session
      // while /auth/me is still in flight.
      await ts.saveProfile(jsonEncode(
          Me(id: '1', firstName: 'A', lastName: 'B', email: 'a@b.c').toJson()));

      final repo = _GatedAuthRepository(ts);
      final c = build(ts, _FakeBiometric(), repo);
      final n = c.read(authControllerProvider.notifier);

      final boot = n.bootstrap();
      await pumpEventQueue();
      expect(c.read(authControllerProvider).locked, isTrue,
          reason: 'optimistic cached session starts locked');

      expect(await n.unlock(), isTrue);
      expect(c.read(authControllerProvider).locked, isFalse);

      repo.gate.complete();
      await boot;
      final s = c.read(authControllerProvider);
      expect(s.isAuthenticated, isTrue);
      expect(s.locked, isFalse,
          reason: 'a late /auth/me must not re-apply the stale launch lock');
    });

    test('bootstrap does not resurrect a session signed out mid-flight',
        () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      await ts.setBiometricEnabled(true);
      await ts.saveProfile(jsonEncode(
          Me(id: '1', firstName: 'A', lastName: 'B', email: 'a@b.c').toJson()));

      final repo = _GatedAuthRepository(ts);
      final c = build(ts, _FakeBiometric(), repo);
      final n = c.read(authControllerProvider.notifier);

      final boot = n.bootstrap();
      await pumpEventQueue();
      expect(c.read(authControllerProvider).locked, isTrue);

      // "Sign out instead" from the lock screen while /auth/me is in flight.
      await n.logout();
      expect(c.read(authControllerProvider).isAuthenticated, isFalse);

      repo.gate.complete();
      await boot;
      expect(c.read(authControllerProvider).isAuthenticated, isFalse,
          reason: 'a late /auth/me must not revive a signed-out session');
    });

    test('promptActive is true only while the unlock prompt is in flight',
        () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      await ts.setBiometricEnabled(true);
      final bio = _GatedBiometric();
      final c = build(ts, bio, _FakeAuthRepository(ts));
      final n = c.read(authControllerProvider.notifier);
      await n.bootstrap();
      expect(n.promptActive, isFalse);

      // The lifecycle observer reads this to suppress pauses caused by the
      // prompt's own activity (Android prompt-loop fix).
      final unlocking = n.unlock();
      await pumpEventQueue();
      expect(n.promptActive, isTrue);

      bio.gate.complete(true);
      expect(await unlocking, isTrue);
      expect(n.promptActive, isFalse);
      expect(c.read(authControllerProvider).locked, isFalse);
    });

    test('lockOnResume locks synchronously when armed (no content flash)',
        () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      await ts.setBiometricEnabled(true);
      final c = build(ts, _FakeBiometric(), _FakeAuthRepository(ts));
      final n = c.read(authControllerProvider.notifier);
      await n.bootstrap();
      await n.unlock();

      final pending = n.lockOnResume();
      expect(c.read(authControllerProvider).locked, isTrue,
          reason: 'must lock before any storage/platform await');
      await pending;
      expect(c.read(authControllerProvider).locked, isTrue);
    });

    test('lockOnResume rolls back when biometrics became unavailable',
        () async {
      final ts = TokenStorage();
      await ts.save(accessToken: 'a');
      await ts.setBiometricEnabled(true);
      final bio = _FakeBiometric();
      final c = build(ts, bio, _FakeAuthRepository(ts));
      final n = c.read(authControllerProvider.notifier);
      await n.bootstrap();
      await n.unlock();

      bio.available = false;
      await n.lockOnResume();
      expect(c.read(authControllerProvider).locked, isFalse,
          reason: 'never trap the user behind a lock they cannot satisfy');
    });

    test('lock return location is one-shot and skips launch/auth routes', () {
      final ts = TokenStorage();
      final c = build(ts, _FakeBiometric(), _FakeAuthRepository(ts));
      final n = c.read(authControllerProvider.notifier);

      n.stashLockReturnLocation('/billing/invoices/42');
      expect(n.takeLockReturnLocation(), '/billing/invoices/42');
      expect(n.takeLockReturnLocation(), isNull, reason: 'consumed on read');

      for (final loc in ['/splash', '/login', '/lock', '/mfa']) {
        n.stashLockReturnLocation(loc);
        expect(n.takeLockReturnLocation(), isNull,
            reason: '$loc must fall back to the portal home');
      }
    });
  });
}
