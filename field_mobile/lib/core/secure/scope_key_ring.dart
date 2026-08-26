import 'dart:math';
import 'dart:typed_data';

import 'data_scope.dart';
import 'evidence_cipher.dart';
import 'secret_vault.dart';

/// The two keys a scope needs: one for its SQLCipher database, one for the
/// evidence envelopes on disk. Separate keys so that a leak of either does not
/// hand over both stores.
class ScopeKeys {
  const ScopeKeys({required this.databaseKeyHex, required this.evidenceKey});

  final String databaseKeyHex;
  final Uint8List evidenceKey;
}

/// Generates, holds and destroys per-scope key material in the platform
/// keystore.
///
/// Destroying a scope's keys is the first step of a wipe, and the reason a wipe
/// cannot be partial: once the keys are gone, any file the deletion pass has
/// not reached yet is already unreadable ciphertext.
class ScopeKeyRing {
  ScopeKeyRing(this._vault, {Random? random})
    : _random = random ?? Random.secure();

  static const _prefix = 'field_scope';
  static const _databaseSuffix = 'db';
  static const _evidenceSuffix = 'evidence';

  final SecretVault _vault;
  final Random _random;

  static String databaseKeyName(String scopeKey) =>
      '$_prefix:$scopeKey:$_databaseSuffix';

  static String evidenceKeyName(String scopeKey) =>
      '$_prefix:$scopeKey:$_evidenceSuffix';

  /// Reads this install's keys for [scope], generating them on first use.
  Future<ScopeKeys> loadOrCreate(DataScope scope) async {
    if (!scope.isBound) {
      throw ArgumentError('refusing to key the unbound scope');
    }
    final databaseName = databaseKeyName(scope.key);
    final evidenceName = evidenceKeyName(scope.key);
    var databaseKey = await _vault.read(databaseName);
    if (databaseKey == null) {
      databaseKey = _randomHex(32);
      await _vault.write(databaseName, databaseKey);
    }
    var evidenceKey = await _vault.read(evidenceName);
    if (evidenceKey == null) {
      evidenceKey = _randomHex(EvidenceCipher.keyLength);
      await _vault.write(evidenceName, evidenceKey);
    }
    return ScopeKeys(
      databaseKeyHex: databaseKey,
      evidenceKey: _decodeHex(evidenceKey),
    );
  }

  /// Destroys every key belonging to [scopeKey]. Idempotent: a resumed wipe
  /// calls this again and must not fail because the keys already went.
  Future<void> destroy(String scopeKey) async {
    await _vault.delete(databaseKeyName(scopeKey));
    await _vault.delete(evidenceKeyName(scopeKey));
  }

  /// Every scope this device still holds key material for. The startup
  /// reconciler uses it to find a principal who is no longer signed in.
  Future<Set<String>> knownScopeKeys() async {
    final names = await _vault.names();
    return {
      for (final name in names)
        if (name.startsWith('$_prefix:')) name.split(':')[1],
    };
  }

  String _randomHex(int bytes) {
    final buffer = StringBuffer();
    for (var i = 0; i < bytes; i++) {
      buffer.write(_random.nextInt(256).toRadixString(16).padLeft(2, '0'));
    }
    return buffer.toString();
  }

  Uint8List _decodeHex(String hex) {
    final out = Uint8List(hex.length ~/ 2);
    for (var i = 0; i < out.length; i++) {
      out[i] = int.parse(hex.substring(i * 2, i * 2 + 2), radix: 16);
    }
    return out;
  }
}
