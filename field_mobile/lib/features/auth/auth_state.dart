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

/// How much of the session the client was able to confirm with the server.
///
/// Deliberately a field on [Authenticated] rather than a sibling state: an
/// offline technician is signed in. Everything that reads local data keys off
/// `is Authenticated`, and a separate state would have logged them out of their
/// own handset by another name.
enum SessionReach {
  /// The access token is current.
  online,

  /// The session is held on a stored refresh credential that could not be
  /// exchanged, because nothing authoritative answered. ADR-0067 § 4: that is a
  /// transport fact, not a revocation. The credential is intact and the next
  /// request — or [AuthController.retryRestore] — tries the exchange again.
  offline,
}

class Authenticated extends AuthState {
  const Authenticated(
    this.mode, {
    this.vendorId,
    this.reach = SessionReach.online,
  });

  final LoginMode mode;
  final String? vendorId;
  final SessionReach reach;

  bool get isOffline => reach == SessionReach.offline;
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

  /// Bumped by every ending. Asynchronous work captures it on entry and is
  /// refused if it comes back after the session it belonged to is gone: a
  /// refresh that was in flight when the technician signed out must not
  /// republish an authenticated state over the wipe that has just run.
  ///
  /// The durable half of the fence is the wipe itself (ADR-0067 §§ 2-3). The
  /// wipe journal survives the process and is replayed at launch, and the
  /// credential record it destroys is what the next cold start reads: a
  /// generation that lived only in memory would be defeated by a kill.
  int _generation = 0;

  /// The teardown currently running, if any. An explicit logout and an
  /// authoritative revocation destroy exactly the same things, so a second
  /// request arriving while the first is still running joins it rather than
  /// starting a competing wipe.
  Future<void>? _teardown;

  bool _stale(int generation) => generation != _generation;

  void _publish(int generation, AuthState next) {
    if (_stale(generation)) return;
    state = next;
  }

  Future<void> _restoreSession() => _restore(_generation);

  /// Try the restore again after it ended offline. Non-destructive by
  /// construction: it is the cold-start path re-run, and every branch of it
  /// that could destroy anything goes through [_endSession].
  Future<void> retryRestore() => _restore(_generation);

  Future<void> _restore(int generation) async {
    LoginMode? mode;
    SessionRefresh outcome;
    try {
      mode = await ref.read(tokenStoreProvider).loginMode;
      outcome = mode == null
          ? const SessionAbsent()
          : await ref.read(apiClientProvider).ensureFreshSession();
    } on Object {
      // A secure store that would not read, a platform channel that threw:
      // none of it is the server refusing this session, so none of it is
      // allowed to destroy a credential. It lands on exactly the same footing
      // as a failed exchange — unreachable, never refused.
      outcome = const SessionUnreachable();
    }
    if (_stale(generation)) {
      // Something ended this session while we were asking — the refresh path's
      // own expiry callback, most likely. Join whatever it started rather than
      // beginning a second teardown.
      await _teardown;
      return;
    }
    final restored = mode;
    if (restored == null) {
      // Nothing on file says anybody was signed in.
      _publish(generation, const Unauthenticated());
      return;
    }
    switch (outcome) {
      case SessionFresh():
        _publish(generation, Authenticated(restored));
      case SessionUnreachable():
        // ADR-0067 § 4. A failed restore is not a failed authentication. The
        // refresh credential is still on the device and still valid; the
        // device simply could not reach anything able to say otherwise.
        // Clearing it here is what turned a coverage hole into a lockout.
        _publish(
          generation,
          Authenticated(restored, reach: SessionReach.offline),
        );
      case SessionRefused() || SessionAbsent():
        // Refused: the server answered, authoritatively, on the refresh
        // exchange itself. Absent, with a login mode still on file: a
        // credential record torn in half, which no reader can act on. Both are
        // terminal, and both leave through the one wipe.
        await sessionExpired();
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
    final generation = _generation;
    final result = await _repo.login(
      username: username,
      password: password,
      mode: mode,
    );
    if (result is LoginSuccess && !await _bindSession(generation)) return;
    _publish(generation, switch (result) {
      LoginSuccess(:final mode, :final vendorId) => Authenticated(
        mode,
        vendorId: vendorId,
      ),
      MfaRequired(:final mfaToken, :final mode) => AwaitingMfa(mfaToken, mode),
      LoginFailure(:final message) => Unauthenticated(error: message),
    });
  }

  Future<void> verifyMfa(String code) async {
    final current = state;
    if (current is! AwaitingMfa) return;
    final generation = _generation;
    final result = await _repo.verifyMfa(
      mfaToken: current.mfaToken,
      code: code,
      mode: current.mode,
    );
    if (result is LoginSuccess && !await _bindSession(generation)) return;
    _publish(generation, switch (result) {
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
    });
  }

  /// Binds fresh credentials to local storage, unless the session they belong
  /// to has already ended. Returns false when the caller must publish nothing.
  Future<bool> _bindSession(int generation) async {
    if (_stale(generation)) {
      // The technician signed out while these credentials were being issued.
      // They belong to a session that no longer exists, so they leave through
      // the one wipe rather than being left behind on the handset.
      await _endSession((session) => session.signOut());
      return false;
    }
    await _session?.beginSession();
    return true;
  }

  Future<void> logout() async {
    final generation = await _endSession((session) => session.signOut());
    _publish(generation, const Unauthenticated());
  }

  /// The server refused the session authoritatively. Local data belonging to a
  /// revoked principal is destroyed through the same wipe an explicit logout
  /// uses, not through a lighter "just forget the token" path.
  Future<void> sessionExpired() async {
    final generation = await _endSession((session) => session.sessionRevoked());
    _publish(
      generation,
      const Unauthenticated(error: 'Session expired — sign in again'),
    );
  }

  /// The one ending. Every terminal transition goes through here: the
  /// generation moves first, so anything already in flight is fenced out
  /// before destruction begins, the API client is told to drop a refresh whose
  /// result would otherwise be written back after the wipe, and the wipe
  /// itself is [SessionLifecycle]'s — never a `clear()` at this call site.
  Future<int> _endSession(Future<void> Function(SessionLifecycle) end) async {
    final generation = ++_generation;
    ref.read(apiClientProvider).abandonSession();
    final running = _teardown;
    if (running != null) {
      await running;
      return generation;
    }
    final session = _session;
    // No lifecycle is wired outside the real app (widget tests bring their own
    // stores). There is no local data to destroy when nothing opened any, so
    // the credential record is the whole of it.
    final tracked = (session == null ? _repo.logout() : end(session))
        .whenComplete(() => _teardown = null);
    _teardown = tracked;
    await tracked;
    return generation;
  }
}

final authControllerProvider = NotifierProvider<AuthController, AuthState>(
  AuthController.new,
);
