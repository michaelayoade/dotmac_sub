import 'dart:math';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'credential_bundle.dart';
import 'data_scope.dart';
import 'observability.dart';

/// Persists the session in the platform secure store (Keychain on iOS,
/// EncryptedSharedPreferences on Android).
///
/// The session is one record — a [CredentialBundle] — written in a single call,
/// never a pair of independent keys. See that class for why: two writes can
/// tear, and a torn credential pair is an access token from one issue paired
/// with a refresh token from another.
///
/// Writes into a live session are *fenced*. [renewSession] and [adoptScope]
/// both take the generation their caller started under and refuse if the stored
/// record is gone or has moved on. That is the durable half of session fencing
/// (`SessionFence` is the in-memory half) and it is what stops a token refresh
/// that was in flight during a sign-out from writing live credentials back onto
/// a device the user believes they signed out of.
class TokenStorage {
  TokenStorage([FlutterSecureStorage? storage])
      : _storage = storage ??
            const FlutterSecureStorage(
                aOptions: AndroidOptions(encryptedSharedPreferences: true));

  final FlutterSecureStorage _storage;

  /// The one credential record.
  static const _kBundle = 'session_bundle_v2';

  /// Pre-bundle keys. Only read once, by [_migrateLegacy], and deleted by
  /// [clear] so an interrupted migration can never leave a second copy of a
  /// token behind.
  static const _kLegacyAccess = 'access_token';
  static const _kLegacyRefresh = 'refresh_token';

  /// Device-level monotonic session counter. Deliberately NOT cleared by
  /// [clear]: if it reset, a sign-out followed by a fresh sign-in would reuse
  /// generation 1, and a refresh left in flight by the *previous* session would
  /// match the new one's fence and be allowed to write. Generations must never
  /// repeat on a device.
  static const _kGeneration = 'session_generation';

  static const _kBiometric = 'biometric_lock_enabled';
  static const _kBiometricPromptSeen = 'biometric_prompt_seen';
  static const _kThemeMode = 'theme_mode';
  static const _kProfile = 'cached_profile';
  static const _kDeviceId = 'device_id';

  /// Start a new session: one new generation, one atomic write.
  ///
  /// Returns the generation the caller must fence its subsequent work with
  /// (`SessionFence.open`).
  Future<int> beginSession(
      {required String accessToken,
      String? refreshToken,
      MobileDataScope scope = MobileDataScope.anonymous}) async {
    final generation = await _nextGeneration();
    await _writeBundle(CredentialBundle(
        accessToken: accessToken,
        refreshToken: refreshToken,
        scope: scope,
        generation: generation,
        issuedAt: DateTime.now().toUtc()));
    return generation;
  }

  /// Replace the tokens of an existing session after a refresh.
  ///
  /// Returns false — and writes nothing — when [generation] is no longer the
  /// stored session: either it was wiped (sign-out, expiry, revocation) or a
  /// newer sign-in replaced it. A false here means "your result is stale,
  /// discard it", never "try again".
  Future<bool> renewSession(
      {required int generation,
      required String accessToken,
      String? refreshToken}) async {
    final current = await readBundle();
    if (current == null || current.generation != generation) {
      Log.breadcrumb('credential write refused: stale session generation',
          category: 'auth');
      return false;
    }
    await _writeBundle(current.copyWith(
        accessToken: accessToken,
        refreshToken: refreshToken ?? current.refreshToken,
        issuedAt: DateTime.now().toUtc()));
    return true;
  }

  /// Stamp the resolved identity onto the current session, once `/auth/me`
  /// has told us who the tokens belong to. Fenced exactly like [renewSession].
  Future<bool> adoptScope(
      {required int generation, required MobileDataScope scope}) async {
    final current = await readBundle();
    if (current == null || current.generation != generation) return false;
    if (current.scope == scope) return true;
    await _writeBundle(current.copyWith(scope: scope));
    return true;
  }

  /// The stored session, or null when there is none.
  ///
  /// Handles the two non-obvious cases deterministically:
  ///  * a record whose schema version this build cannot read (an app
  ///    downgrade) or that is corrupt is discarded *whole* — never partially
  ///    applied, because a half-understood credential record is exactly the
  ///    mismatched pair the bundle exists to prevent;
  ///  * a pre-bundle install (separate `access_token` / `refresh_token` keys)
  ///    is migrated in place, so upgrading users are not signed out.
  Future<CredentialBundle?> readBundle() async {
    // Single-flight. Every outgoing request reads the record to attach its
    // bearer token, and on a cold start several fire at once — without this,
    // concurrent readers of a legacy install would each run the migration and
    // each burn a generation. Cleared on completion, so a read that follows a
    // write still sees the write.
    final pending = _reading;
    if (pending != null) return pending;
    final run = _readBundle();
    _reading = run;
    try {
      return await run;
    } finally {
      if (identical(_reading, run)) _reading = null;
    }
  }

  Future<CredentialBundle?>? _reading;

  Future<CredentialBundle?> _readBundle() async {
    final decoded = CredentialBundle.decode(await _read(_kBundle));
    if (decoded.isOk) return decoded.bundle;
    if (decoded.mustDiscard) {
      Log.breadcrumb(
          'discarding unreadable credential record (${decoded.outcome.name})',
          category: 'auth');
      await clear();
      return null;
    }
    return _migrateLegacy();
  }

  /// One-way upgrade from the pre-bundle key layout.
  ///
  /// The legacy keys carry no identity and no generation, so the migrated
  /// record gets the anonymous scope (the next `/auth/me` stamps the real one
  /// via [adoptScope]) and the next generation from the device counter. The
  /// bundle is written *before* the old keys are deleted: a crash in between
  /// leaves a valid bundle plus two ignored leftovers, which [clear] also
  /// removes. The reverse order could lose the session.
  Future<CredentialBundle?> _migrateLegacy() async {
    final access = await _read(_kLegacyAccess);
    if (access == null || access.isEmpty) return null;
    final refresh = await _read(_kLegacyRefresh);
    final bundle = CredentialBundle(
        accessToken: access,
        refreshToken: refresh != null && refresh.isNotEmpty ? refresh : null,
        scope: MobileDataScope.anonymous,
        generation: await _nextGeneration(),
        issuedAt: DateTime.now().toUtc());
    await _writeBundle(bundle);
    await _delete(_kLegacyAccess);
    await _delete(_kLegacyRefresh);
    Log.breadcrumb('migrated legacy token keys to a credential bundle',
        category: 'auth');
    return bundle;
  }

  Future<String?> readAccessToken() async => (await readBundle())?.accessToken;

  Future<String?> readRefreshToken() async =>
      (await readBundle())?.refreshToken;

  /// The identity the stored session belongs to, or the anonymous scope when
  /// there is no session (or it has not been attributed yet).
  Future<MobileDataScope> readScope() async =>
      (await readBundle())?.scope ?? MobileDataScope.anonymous;

  /// Biometric app-lock preference. Deliberately kept out of [clear] so it
  /// survives a session-expiry token wipe (the user re-authenticates with their
  /// password and the lock stays enabled). Cleared explicitly on logout.
  Future<void> setBiometricEnabled(bool enabled) => enabled
      ? _storage.write(key: _kBiometric, value: 'true')
      : _storage.delete(key: _kBiometric);

  Future<bool> isBiometricEnabled() async =>
      (await _storage.read(key: _kBiometric)) == 'true';

  /// Whether we've already offered biometric sign-in enrollment once on this
  /// device (so the post-login prompt asks at most once). Device-level — kept
  /// out of [clear] so logging out/in doesn't nag a user who declined.
  Future<void> setBiometricPromptSeen() =>
      _storage.write(key: _kBiometricPromptSeen, value: 'true');

  Future<bool> biometricPromptSeen() async =>
      (await _storage.read(key: _kBiometricPromptSeen)) == 'true';

  /// Theme preference ('system' | 'light' | 'dark'). A device setting — kept out
  /// of [clear] so it survives logout.
  Future<void> setThemeMode(String mode) =>
      _storage.write(key: _kThemeMode, value: mode);

  Future<String?> readThemeMode() => _storage.read(key: _kThemeMode);

  /// The last-known profile, as a JSON string. Lets the app render the
  /// dashboard optimistically on cold start instead of blocking the splash on
  /// `/auth/me`. Carries PII, so it is wiped together with the tokens in
  /// [clear].
  ///
  /// Pass [generation] from any path that could still be in flight across a
  /// sign-out (the bootstrap `/auth/me`, a post-login profile load): a stale
  /// writer must not leave the previous account's name and address on a device
  /// whose session has already been torn down.
  Future<void> saveProfile(String json, {int? generation}) async {
    if (generation != null) {
      final current = await readBundle();
      if (current == null || current.generation != generation) return;
    }
    await _storage.write(key: _kProfile, value: json);
  }

  Future<String?> readProfile() => _storage.read(key: _kProfile);

  /// Stable per-install identifier sent as `X-Device-Id` so the backend can keep
  /// one session per device (re-login replaces this device's prior session).
  /// Generated once and kept out of [clear] so it survives logout — otherwise a
  /// sign-out/in cycle would look like a brand-new device every time.
  Future<String> deviceId() async {
    final existing = await _storage.read(key: _kDeviceId);
    if (existing != null && existing.isNotEmpty) return existing;
    final rnd = Random.secure();
    final id =
        List.generate(32, (_) => rnd.nextInt(16).toRadixString(16)).join();
    await _storage.write(key: _kDeviceId, value: id);
    return id;
  }

  /// Remove every trace of the session: the credential record, any leftover
  /// legacy keys, and the cached profile. Device-level preferences (biometric
  /// opt-in, theme, device id) and the monotonic generation counter survive by
  /// design — see the key declarations above.
  ///
  /// Call this through `SessionWipe`, not directly: credentials are only one of
  /// the things a session consists of.
  Future<void> clear() async {
    await _delete(_kBundle);
    await _delete(_kLegacyAccess);
    await _delete(_kLegacyRefresh);
    await _delete(_kProfile);
  }

  /// Next value of the device-level monotonic counter. Read-modify-write on the
  /// secure store; the app has exactly one writer (a session starts from a user
  /// action or a cold start), so there is no contention to guard against.
  Future<int> _nextGeneration() async {
    final raw = await _read(_kGeneration);
    final previous = int.tryParse(raw ?? '') ?? 0;
    final next = previous + 1;
    await _storage.write(key: _kGeneration, value: '$next');
    return next;
  }

  Future<void> _writeBundle(CredentialBundle bundle) =>
      _storage.write(key: _kBundle, value: bundle.encode());

  Future<String?> _read(String key) => _storage.read(key: key);

  Future<void> _delete(String key) => _storage.delete(key: key);
}
