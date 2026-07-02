import 'dart:math';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Persists the access/refresh token pair in the platform secure store
/// (Keychain on iOS, EncryptedSharedPreferences on Android).
class TokenStorage {
  TokenStorage([FlutterSecureStorage? storage])
    : _storage =
          storage ??
          const FlutterSecureStorage(
            aOptions: AndroidOptions(encryptedSharedPreferences: true),
          );

  final FlutterSecureStorage _storage;

  static const _kAccess = 'access_token';
  static const _kRefresh = 'refresh_token';
  static const _kBiometric = 'biometric_lock_enabled';
  static const _kBiometricPromptSeen = 'biometric_prompt_seen';
  static const _kThemeMode = 'theme_mode';
  static const _kProfile = 'cached_profile';
  static const _kDeviceId = 'device_id';

  Future<void> save({required String accessToken, String? refreshToken}) async {
    await _storage.write(key: _kAccess, value: accessToken);
    if (refreshToken != null) {
      await _storage.write(key: _kRefresh, value: refreshToken);
    }
  }

  Future<String?> readAccessToken() => _storage.read(key: _kAccess);

  Future<String?> readRefreshToken() => _storage.read(key: _kRefresh);

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
  Future<void> saveProfile(String json) =>
      _storage.write(key: _kProfile, value: json);

  Future<String?> readProfile() => _storage.read(key: _kProfile);

  /// Stable per-install identifier sent as `X-Device-Id` so the backend can keep
  /// one session per device (re-login replaces this device's prior session).
  /// Generated once and kept out of [clear] so it survives logout — otherwise a
  /// sign-out/in cycle would look like a brand-new device every time.
  Future<String> deviceId() async {
    final existing = await _storage.read(key: _kDeviceId);
    if (existing != null && existing.isNotEmpty) return existing;
    final rnd = Random.secure();
    final id = List.generate(
      32,
      (_) => rnd.nextInt(16).toRadixString(16),
    ).join();
    await _storage.write(key: _kDeviceId, value: id);
    return id;
  }

  Future<void> clear() async {
    await _storage.delete(key: _kAccess);
    await _storage.delete(key: _kRefresh);
    await _storage.delete(key: _kProfile);
  }
}
