import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:sentry/sentry.dart';

import '../config/env.dart';
import '../core/api_client.dart';
import '../core/api_exception.dart';
import '../core/biometric_service.dart';
import '../core/observability.dart';
import '../core/token_storage.dart';
import '../models/auth.dart';
import '../repositories/auth_repository.dart';

enum AuthStatus { unknown, authenticated, unauthenticated }

class AuthState {
  const AuthState({required this.status, this.me, this.locked = false});

  final AuthStatus status;
  final Me? me;

  /// True when the session is valid but held behind the biometric app-lock.
  /// The router keeps such a session on `/lock` until [AuthController.unlock].
  final bool locked;

  bool get isAuthenticated => status == AuthStatus.authenticated;
  bool get isKnown => status != AuthStatus.unknown;

  AuthState copyWith({AuthStatus? status, Me? me, bool? locked}) => AuthState(
        status: status ?? this.status,
        me: me ?? this.me,
        locked: locked ?? this.locked,
      );

  static const unknown = AuthState(status: AuthStatus.unknown);
  static const signedOut = AuthState(status: AuthStatus.unauthenticated);
}

// --- Infrastructure providers ---------------------------------------------

final tokenStorageProvider = Provider<TokenStorage>((ref) => TokenStorage());

final biometricServiceProvider =
    Provider<BiometricService>((ref) => BiometricService());

final apiClientProvider = Provider<ApiClient>((ref) {
  final client = ApiClient(
    storage: ref.watch(tokenStorageProvider),
    onSessionExpired: () {
      // Refresh failed irrecoverably: drop to the signed-out state so the
      // router redirects to /login.
      ref.read(authControllerProvider.notifier).onSessionExpired();
    },
  );
  return client;
});

final authRepositoryProvider = Provider<AuthRepository>((ref) {
  return AuthRepository(
    dio: ref.watch(apiClientProvider).dio,
    storage: ref.watch(tokenStorageProvider),
  );
});

// --- Auth state controller -------------------------------------------------

final authControllerProvider =
    StateNotifierProvider<AuthController, AuthState>((ref) {
  return AuthController(ref)..bootstrap();
});

/// Convenience: the signed-in user, or null.
final currentUserProvider = Provider<Me?>((ref) {
  return ref.watch(authControllerProvider).me;
});

class AuthController extends StateNotifier<AuthState> {
  AuthController(this._ref) : super(AuthState.unknown);

  final Ref _ref;

  AuthRepository get _repo => _ref.read(authRepositoryProvider);
  TokenStorage get _storage => _ref.read(tokenStorageProvider);
  BiometricService get _biometric => _ref.read(biometricServiceProvider);

  /// True while a biometric prompt is on screen. Resume-lock checks this so the
  /// prompt's own lifecycle transitions can't re-arm the lock under itself.
  bool _promptActive = false;

  /// Restore a previous session on cold start.
  ///
  /// Optimistic: if we hold a cached profile we render straight to the
  /// authenticated app and refresh `/auth/me` behind it, so the splash isn't
  /// blocked on a (potentially slow) network round-trip. We only force the user
  /// back to login when the server actively rejects the session (401/403) — a
  /// transient network failure keeps the cached session rather than logging a
  /// connected user out because their signal dropped.
  Future<void> bootstrap() async {
    final token = await _storage.readAccessToken();
    if (token == null) {
      state = AuthState.signedOut;
      return;
    }

    final locked = await _shouldLockOnLaunch();
    final cached = await _readCachedProfile();
    if (cached != null) {
      state = AuthState(
        status: AuthStatus.authenticated,
        me: cached,
        locked: locked,
      );
    }

    try {
      final me = await _repo.me();
      await _persistProfile(me);
      state = AuthState(
        status: AuthStatus.authenticated,
        me: me,
        locked: locked,
      );
    } on ApiException catch (e) {
      final rejected = e.statusCode == 401 || e.statusCode == 403;
      if (rejected || cached == null) {
        // Either the session is genuinely invalid, or we have no cached
        // identity to fall back on — send the user to login.
        await _storage.clear();
        state = AuthState.signedOut;
      }
      // Otherwise: keep the optimistic cached session (offline / server down).
    } catch (_) {
      if (cached == null) {
        await _storage.clear();
        state = AuthState.signedOut;
      }
    }
  }

  Future<Me?> _readCachedProfile() async {
    final raw = await _storage.readProfile();
    if (raw == null) return null;
    try {
      return Me.fromJson(jsonDecode(raw) as Map<String, dynamic>);
    } catch (_) {
      return null;
    }
  }

  Future<void> _persistProfile(Me me) async {
    try {
      await _storage.saveProfile(jsonEncode(me.toJson()));
    } catch (_) {
      // Caching the profile is best-effort; never fail bootstrap over it.
    }
  }

  /// Lock on launch only when the user opted in AND biometrics are still
  /// usable. If biometrics were removed since opt-in we proceed unlocked rather
  /// than stranding the user.
  Future<bool> _shouldLockOnLaunch() async {
    if (!await _storage.isBiometricEnabled()) return false;
    return _biometric.isAvailable();
  }

  // --- Biometric app-lock --------------------------------------------------

  /// Prompt to unlock a locked session. On success the router redirects off
  /// `/lock`. Does not store or replay any credential — it only reveals the
  /// session tokens already held in secure storage.
  Future<bool> unlock() async {
    _promptActive = true;
    try {
      final ok = await _biometric.authenticate(reason: 'Unlock ${Brand.name}');
      if (ok) state = state.copyWith(locked: false);
      return ok;
    } finally {
      _promptActive = false;
    }
  }

  /// Re-arm the lock when the app returns from the background. No-op unless the
  /// session is authenticated, currently unlocked, opted-in, and biometrics are
  /// available — and never while a prompt is already showing.
  Future<void> lockOnResume() async {
    if (_promptActive || state.locked || !state.isAuthenticated) return;
    if (!await _storage.isBiometricEnabled()) return;
    if (!await _biometric.isAvailable()) return;
    // Re-check after the awaits in case state changed meanwhile.
    if (_promptActive || state.locked || !state.isAuthenticated) return;
    state = state.copyWith(locked: true);
  }

  Future<bool> biometricAvailable() => _biometric.isAvailable();

  Future<bool> isBiometricLockEnabled() => _storage.isBiometricEnabled();

  /// Turn the lock on. Requires a successful biometric check first so the user
  /// proves they can satisfy the lock before we enable it. Returns false if
  /// unavailable or the check was cancelled/failed.
  Future<bool> enableBiometricLock() async {
    if (!await _biometric.isAvailable()) return false;
    _promptActive = true;
    try {
      final ok = await _biometric.authenticate(
          reason: 'Confirm to enable biometric unlock');
      if (ok) await _storage.setBiometricEnabled(true);
      return ok;
    } finally {
      _promptActive = false;
    }
  }

  Future<void> disableBiometricLock() => _storage.setBiometricEnabled(false);

  /// Returns the [LoginResult] so the UI can branch into the MFA flow.
  /// Breadcrumbs the attempt/outcome (provider only — never the credentials).
  Future<LoginResult> login({
    required String username,
    required String password,
    String? provider,
  }) async {
    Log.breadcrumb('login attempt',
        category: 'auth', data: {'provider': provider ?? 'local'});
    try {
      final result = await _repo.login(
        username: username,
        password: password,
        provider: provider,
      );
      if (result.isAuthenticated) {
        await _loadMe();
        Log.breadcrumb('login success', category: 'auth');
      } else if (result.mfaRequired) {
        Log.breadcrumb('login -> mfa required', category: 'auth');
      }
      return result;
    } catch (e) {
      Log.breadcrumb('login failed',
          category: 'auth', level: SentryLevel.warning, data: {'error': '$e'});
      rethrow;
    }
  }

  Future<void> verifyMfa(
      {required String mfaToken, required String code}) async {
    await _repo.verifyMfa(mfaToken: mfaToken, code: code);
    await _loadMe();
    Log.breadcrumb('mfa verified', category: 'auth');
  }

  Future<void> _loadMe() async {
    final me = await _repo.me();
    await _persistProfile(me);
    state = AuthState(status: AuthStatus.authenticated, me: me);
    await Sentry.configureScope(
        (scope) => scope.setUser(SentryUser(id: me.id)));
  }

  void setMe(Me me) {
    state = state.copyWith(status: AuthStatus.authenticated, me: me);
  }

  /// Re-fetch the profile (e.g. after an avatar or profile-field change).
  Future<void> reloadProfile() => _loadMe();

  Future<void> logout() async {
    Log.breadcrumb('logout', category: 'auth');
    await _repo.logout();
    // Explicit sign-out is a full reset: drop the biometric opt-in so the next
    // user starts from a clean password login.
    await _storage.setBiometricEnabled(false);
    state = AuthState.signedOut;
    await Sentry.configureScope((scope) => scope.setUser(null));
  }

  void onSessionExpired() {
    state = AuthState.signedOut;
  }
}
