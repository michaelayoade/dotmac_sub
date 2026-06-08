import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:sentry/sentry.dart';

import '../core/api_client.dart';
import '../core/observability.dart';
import '../core/token_storage.dart';
import '../models/auth.dart';
import '../repositories/auth_repository.dart';

enum AuthStatus { unknown, authenticated, unauthenticated }

class AuthState {
  const AuthState({required this.status, this.me});

  final AuthStatus status;
  final Me? me;

  bool get isAuthenticated => status == AuthStatus.authenticated;
  bool get isKnown => status != AuthStatus.unknown;

  AuthState copyWith({AuthStatus? status, Me? me}) =>
      AuthState(status: status ?? this.status, me: me ?? this.me);

  static const unknown = AuthState(status: AuthStatus.unknown);
  static const signedOut = AuthState(status: AuthStatus.unauthenticated);
}

// --- Infrastructure providers ---------------------------------------------

final tokenStorageProvider = Provider<TokenStorage>((ref) => TokenStorage());

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

  /// Restore a previous session on cold start.
  Future<void> bootstrap() async {
    final token = await _storage.readAccessToken();
    if (token == null) {
      state = AuthState.signedOut;
      return;
    }
    try {
      final me = await _repo.me();
      state = AuthState(status: AuthStatus.authenticated, me: me);
    } catch (_) {
      await _storage.clear();
      state = AuthState.signedOut;
    }
  }

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
    state = AuthState.signedOut;
    await Sentry.configureScope((scope) => scope.setUser(null));
  }

  void onSessionExpired() {
    state = AuthState.signedOut;
  }
}
