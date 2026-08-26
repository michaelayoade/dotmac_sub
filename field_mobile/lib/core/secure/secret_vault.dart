import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Key material store. Abstract for the same reason the token store is: the
/// app uses the platform keystore/keychain, tests use the in-memory fake.
///
/// Only key material lives here. Customer data never does — the keystore is
/// small, slow, and not a database.
abstract class SecretVault {
  Future<String?> read(String key);

  Future<void> write(String key, String value);

  Future<void> delete(String key);

  /// Every stored name. The scope reconciler needs this to find key material
  /// belonging to a principal who is no longer signed in.
  Future<Set<String>> names();
}

class PlatformSecretVault implements SecretVault {
  PlatformSecretVault([FlutterSecureStorage? storage])
    : _storage = storage ?? const FlutterSecureStorage();

  final FlutterSecureStorage _storage;

  @override
  Future<String?> read(String key) => _storage.read(key: key);

  @override
  Future<void> write(String key, String value) =>
      _storage.write(key: key, value: value);

  @override
  Future<void> delete(String key) => _storage.delete(key: key);

  @override
  Future<Set<String>> names() async => (await _storage.readAll()).keys.toSet();
}

class InMemorySecretVault implements SecretVault {
  final Map<String, String> _values = {};

  @override
  Future<String?> read(String key) async => _values[key];

  @override
  Future<void> write(String key, String value) async => _values[key] = value;

  @override
  Future<void> delete(String key) async => _values.remove(key);

  @override
  Future<Set<String>> names() async => _values.keys.toSet();
}
