import '../api/api_client.dart';
import '../api/token_store.dart';
import 'data_scope.dart';
import 'offline_wipe.dart';
import 'plaintext_migration.dart';
import 'scope_reconciler.dart';
import 'secure_field_store.dart';

/// Everything the session lifecycle needs to open and destroy local storage.
class SecureRuntime {
  const SecureRuntime({
    required this.opener,
    required this.wipe,
    required this.reconciler,
    required this.migration,
    required this.tokenStore,
    required this.baseUrl,
  });

  final SecureStoreOpener opener;
  final OfflineWipe wipe;
  final ScopeReconciler reconciler;
  final PlaintextOfflineMigration migration;
  final TokenStore tokenStore;
  final String baseUrl;
}

/// Binds and unbinds the device's local storage to exactly one principal.
///
/// This is the only object that decides which store exists. All three ways a
/// session can end — the technician signing out, the server revoking the
/// token, and a different technician signing in on the same device — funnel
/// into [_endSession], which calls the one wipe. None of them has a shortcut,
/// and none can destroy less than the others.
class SessionLifecycle {
  SessionLifecycle({required this.runtime, this.onStoreChanged});

  final SecureRuntime runtime;

  /// Notified whenever the bound store changes, so the provider graph can
  /// rebuild everything that reads local storage. Settable because the store
  /// is resolved before the provider container exists.
  void Function(SecureFieldStore? store)? onStoreChanged;

  SecureFieldStore? _store;

  SecureFieldStore? get store => _store;

  /// Launch path. Finishes any wipe that a crash interrupted, resolves the
  /// stored session's scope, destroys anything belonging to anyone else, then
  /// opens the store and carries the pre-encryption data across.
  Future<SecureFieldStore?> restore() async {
    await runtime.wipe.resumeInterrupted();
    final scope = await _scopeFromStoredSession();
    if (scope == null) {
      // No session: sweep every scope on the device, since none of them can be
      // the current one, and leave the store unbound.
      await runtime.reconciler.reconcile(DataScope.unbound);
      return _adopt(null);
    }
    await runtime.reconciler.reconcile(scope);
    return _open(scope);
  }

  /// Called after a successful login or MFA verification, with the new
  /// credentials already in the token store.
  Future<SecureFieldStore?> beginSession() async {
    final accessToken = await runtime.tokenStore.accessToken;
    final refreshToken = await runtime.tokenStore.refreshToken;
    final loginMode = await runtime.tokenStore.loginMode;
    final scope = dataScopeFromClaims(
      accessToken == null ? null : jwtClaims(accessToken),
      baseUrl: runtime.baseUrl,
    );
    if (scope == null || accessToken == null) {
      // A token we cannot scope is a token we cannot store data for. Ending the
      // session is the only safe response.
      await signOut();
      return null;
    }
    final current = _store;
    if (current != null && current.scope != scope) {
      await _endSession(WipeTrigger.accountSwitch);
      // The wipe cleared the token store, as every wipe must. The credentials
      // the new session arrived with are re-applied afterwards rather than the
      // wipe being weakened to spare them.
      await runtime.tokenStore.save(
        accessToken: accessToken,
        refreshToken: refreshToken,
        loginMode: loginMode,
      );
    }
    if (_store != null && _store!.scope == scope) return _store;
    await runtime.reconciler.reconcile(scope);
    return _open(scope);
  }

  /// The technician signed out.
  Future<void> signOut() => _endSession(WipeTrigger.explicitLogout);

  /// The server refused this session authoritatively (401/403 on refresh).
  Future<void> sessionRevoked() => _endSession(WipeTrigger.tokenRevoked);

  Future<void> _endSession(WipeTrigger trigger) async {
    final current = _store;
    final scopeKey =
        current?.scopeKey ?? (await _scopeFromStoredSession())?.key;
    if (scopeKey == null) {
      // The session ended before we could tell whose it was. The tokens and the
      // legacy plaintext still have to go through the one wipe, and then every
      // scope on the device is swept, because none of them can be current.
      await runtime.wipe.wipe(
        WipeRequest(scopeKey: '', trigger: trigger),
      );
      await runtime.reconciler.reconcile(DataScope.unbound);
      _adopt(null);
      return;
    }
    await runtime.wipe.wipe(
      WipeRequest(scopeKey: scopeKey, trigger: trigger),
      live: current,
    );
    _adopt(null);
  }

  Future<SecureFieldStore?> _open(DataScope scope) async {
    final opened = await runtime.opener.open(scope);
    await runtime.migration.run(opened);
    return _adopt(opened);
  }

  Future<DataScope?> _scopeFromStoredSession() async {
    final token = await runtime.tokenStore.accessToken;
    if (token == null) return null;
    return dataScopeFromClaims(jwtClaims(token), baseUrl: runtime.baseUrl);
  }

  SecureFieldStore? _adopt(SecureFieldStore? store) {
    _store = store;
    onStoreChanged?.call(store);
    return store;
  }
}
