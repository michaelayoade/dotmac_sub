import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_client.dart';
import '../../core/api/token_store.dart';
import '../../core/secure/secure_field_store.dart';
import '../../core/secure/session_lifecycle.dart';
import 'auth_repository.dart';

const defaultBaseUrl = String.fromEnvironment(
  'API_BASE_URL',
  defaultValue: 'https://selfcare.dotmac.io',
);

final tokenStoreProvider = Provider<TokenStore>((ref) => SecureTokenStore());

/// Owns the encrypted, principal-scoped offline store. Null outside the real
/// app (widget tests wire their own stores), in which case the auth controller
/// falls back to clearing the session tokens and nothing else — there is no
/// local data to destroy when nothing opened any.
final sessionLifecycleProvider = Provider<SessionLifecycle?>((ref) => null);

/// The currently bound store, or null when nobody is signed in. Everything that
/// reads offline data watches this, so a wipe or an account switch rebuilds the
/// whole offline graph instead of leaving a stale handle behind.
final sessionStoreProvider =
    NotifierProvider<SessionStoreNotifier, SecureFieldStore?>(
      SessionStoreNotifier.new,
    );

class SessionStoreNotifier extends Notifier<SecureFieldStore?> {
  @override
  SecureFieldStore? build() => null;

  void adopt(SecureFieldStore? store) => state = store;
}

final apiClientProvider = Provider<ApiClient>((ref) {
  final client = ApiClient(
    baseUrl: defaultBaseUrl,
    tokenStore: ref.watch(tokenStoreProvider),
    onSessionExpired: () =>
        ref.read(authControllerProvider.notifier).sessionExpired(),
  );
  return client;
});

final authRepositoryProvider = Provider<AuthRepository>(
  (ref) => AuthRepository(ref.watch(apiClientProvider)),
);

sealed class AuthState {
  const AuthState();
}

class RestoringSession extends AuthState {
  const RestoringSession();
}

class Unauthenticated extends AuthState {
  const Unauthenticated({this.error});

  final String? error;
}

class AwaitingMfa extends AuthState {
  const AwaitingMfa(this.mfaToken, this.mode, {this.error});

  final String mfaToken;
  final LoginMode mode;
  final String? error;
}

class Authenticated extends AuthState {
  const Authenticated(this.mode, {this.vendorId});

  final LoginMode mode;
  final String? vendorId;
}

class UpgradeRequired extends AuthState {
  const UpgradeRequired(this.config);

  final AppConfig config;
}

class AuthController extends Notifier<AuthState> {
  @override
  AuthState build() {
    Future.microtask(_restoreSession);
    return const RestoringSession();
  }

  AuthRepository get _repo => ref.read(authRepositoryProvider);

  SessionLifecycle? get _session => ref.read(sessionLifecycleProvider);

  Future<void> _restoreSession() async {
    TokenStore? store;
    try {
      final currentStore = ref.read(tokenStoreProvider);
      store = currentStore;
      final mode = await currentStore.loginMode;
      if (mode == null) {
        state = const Unauthenticated();
        return;
      }
      final token = await ref.read(apiClientProvider).ensureFreshToken();
      if (token == null) {
        // Deliberately a token clear and nothing more. A refresh can fail
        // because the technician is in a coverage dead zone, and unsent
        // evidence must survive that. Only an authoritative refusal reaches
        // sessionExpired, and only that destroys local data.
        await currentStore.clear();
        state = const Unauthenticated();
        return;
      }
      state = Authenticated(mode);
    } catch (_) {
      await store?.clear();
      state = const Unauthenticated();
    }
  }

  /// Force-upgrade gate: checked before any login attempt.
  Future<bool> checkUpgradeGate() async {
    try {
      final config = await _repo.fetchConfig();
      if (config.upgradeRequired) {
        state = UpgradeRequired(config);
        return false;
      }
      return true;
    } catch (_) {
      // Config unreachable: allow login; the API itself still gates access.
      return true;
    }
  }

  Future<void> login(String username, String password, LoginMode mode) async {
    if (!await checkUpgradeGate()) return;
    final result = await _repo.login(
      username: username,
      password: password,
      mode: mode,
    );
    if (result is LoginSuccess) await _session?.beginSession();
    state = switch (result) {
      LoginSuccess(:final mode, :final vendorId) => Authenticated(
        mode,
        vendorId: vendorId,
      ),
      MfaRequired(:final mfaToken, :final mode) => AwaitingMfa(mfaToken, mode),
      LoginFailure(:final message) => Unauthenticated(error: message),
    };
  }

  Future<void> verifyMfa(String code) async {
    final current = state;
    if (current is! AwaitingMfa) return;
    final result = await _repo.verifyMfa(
      mfaToken: current.mfaToken,
      code: code,
      mode: current.mode,
    );
    if (result is LoginSuccess) await _session?.beginSession();
    state = switch (result) {
      LoginSuccess(:final mode, :final vendorId) => Authenticated(
        mode,
        vendorId: vendorId,
      ),
      MfaRequired(:final mfaToken, :final mode) => AwaitingMfa(mfaToken, mode),
      LoginFailure(:final message) => AwaitingMfa(
        current.mfaToken,
        current.mode,
        error: message,
      ),
    };
  }

  Future<void> logout() async {
    final session = _session;
    if (session != null) {
      await session.signOut();
    } else {
      await _repo.logout();
    }
    state = const Unauthenticated();
  }

  /// The server refused the session authoritatively. Local data belonging to a
  /// revoked principal is destroyed through the same wipe an explicit logout
  /// uses, not through a lighter "just forget the token" path.
  Future<void> sessionExpired() async {
    final session = _session;
    if (session != null) {
      await session.sessionRevoked();
    } else {
      await _repo.logout();
    }
    state = const Unauthenticated(error: 'Session expired — sign in again');
  }
}

final authControllerProvider = NotifierProvider<AuthController, AuthState>(
  AuthController.new,
);
