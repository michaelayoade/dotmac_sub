import 'package:flutter_secure_storage/flutter_secure_storage.dart';

enum LoginMode { staff, vendor }

/// Persisted auth material. Abstract so tests use the in-memory fake and the
/// app uses secure storage.
abstract class TokenStore {
  Future<String?> get accessToken;
  Future<String?> get refreshToken;
  Future<LoginMode?> get loginMode;
  Future<void> save({
    required String accessToken,
    String? refreshToken,
    LoginMode? loginMode,
  });
  Future<void> clear();
}

class SecureTokenStore implements TokenStore {
  SecureTokenStore([FlutterSecureStorage? storage])
    : _storage = storage ?? const FlutterSecureStorage();

  final FlutterSecureStorage _storage;

  static const _kAccess = 'access_token';
  static const _kRefresh = 'refresh_token';
  static const _kMode = 'login_mode';

  @override
  Future<String?> get accessToken => _storage.read(key: _kAccess);

  @override
  Future<String?> get refreshToken => _storage.read(key: _kRefresh);

  @override
  Future<LoginMode?> get loginMode async {
    final raw = await _storage.read(key: _kMode);
    return switch (raw) {
      'staff' => LoginMode.staff,
      'vendor' => LoginMode.vendor,
      _ => null,
    };
  }

  @override
  Future<void> save({
    required String accessToken,
    String? refreshToken,
    LoginMode? loginMode,
  }) async {
    await _storage.write(key: _kAccess, value: accessToken);
    if (refreshToken != null) {
      await _storage.write(key: _kRefresh, value: refreshToken);
    }
    if (loginMode != null) {
      await _storage.write(key: _kMode, value: loginMode.name);
    }
  }

  @override
  Future<void> clear() async {
    await _storage.delete(key: _kAccess);
    await _storage.delete(key: _kRefresh);
    await _storage.delete(key: _kMode);
  }
}

class InMemoryTokenStore implements TokenStore {
  String? _access;
  String? _refresh;
  LoginMode? _mode;

  @override
  Future<String?> get accessToken async => _access;

  @override
  Future<String?> get refreshToken async => _refresh;

  @override
  Future<LoginMode?> get loginMode async => _mode;

  @override
  Future<void> save({
    required String accessToken,
    String? refreshToken,
    LoginMode? loginMode,
  }) async {
    _access = accessToken;
    if (refreshToken != null) _refresh = refreshToken;
    if (loginMode != null) _mode = loginMode;
  }

  @override
  Future<void> clear() async {
    _access = null;
    _refresh = null;
    _mode = null;
  }
}
