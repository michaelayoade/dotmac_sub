import 'dart:io';

import 'package:drift/drift.dart';
import 'package:drift/native.dart';
import 'package:sqlite3/sqlite3.dart' as sqlite;

import '../offline/database.dart';

/// Raised when the sqlite3 library backing this build is not SQLCipher.
///
/// Plain sqlite3 accepts `PRAGMA key` silently and then writes a perfectly
/// readable database file, so an unchecked key pragma is a fail-open. The store
/// refuses to run at all rather than persist customer evidence in the clear.
class PlaintextStorageRefused implements Exception {
  const PlaintextStorageRefused(this.reason);

  final String reason;

  @override
  String toString() => 'PlaintextStorageRefused: $reason';
}

/// The subset of a raw sqlite connection the keying step needs. Narrow enough
/// that a test can record the exact statements, in order, without a database.
abstract class CipherConnection {
  void execute(String sql);

  List<List<Object?>> select(String sql);
}

/// Keys a freshly opened connection and proves the key took effect.
///
/// Order matters and is asserted by the tests: the key pragma must be the first
/// statement on the connection (SQLCipher only accepts it before the first
/// read), the cipher must identify itself, and only then may we touch the
/// schema — which is also what surfaces a wrong key, since SQLCipher cannot
/// decode the header and fails the read.
void applySqlCipherKey(CipherConnection db, String keyHex) {
  if (keyHex.length != 64 || !RegExp(r'^[0-9a-f]+$').hasMatch(keyHex)) {
    throw const PlaintextStorageRefused('database key is not 32 raw bytes');
  }
  // A raw `x'...'` key skips SQLCipher's KDF: the key is already 256 bits of
  // CSPRNG output held in the platform keystore, so stretching it would only
  // slow every launch down.
  db.execute('PRAGMA key = "x\'$keyHex\'";');
  final reported = db.select('PRAGMA cipher_version;');
  final version = reported.isEmpty || reported.first.isEmpty
      ? ''
      : '${reported.first.first ?? ''}'.trim();
  if (version.isEmpty) {
    throw const PlaintextStorageRefused(
      'sqlite3 reports no cipher_version: this build is not SQLCipher',
    );
  }
  db.select('SELECT count(*) FROM sqlite_master;');
}

class _SqliteCipherConnection implements CipherConnection {
  const _SqliteCipherConnection(this._db);

  final sqlite.Database _db;

  @override
  void execute(String sql) => _db.execute(sql);

  @override
  List<List<Object?>> select(String sql) =>
      _db.select(sql).map((row) => row.values.toList()).toList();
}

/// Opens the scope's drift database on a SQLCipher connection.
AppDatabase openEncryptedDatabase(File file, String keyHex) {
  return AppDatabase(
    NativeDatabase(
      file,
      setup: (db) => applySqlCipherKey(_SqliteCipherConnection(db), keyHex),
    ),
  );
}

/// Builds the scope's database. Injectable so tests can substitute an
/// unencrypted in-memory database for everything that is not about the cipher
/// itself, and so the cipher tests can substitute a recording connection.
typedef EncryptedDatabaseFactory =
    AppDatabase Function(File file, String keyHex);

/// Removes every row whose scope is not [scopeKey].
///
/// The store already opens one database per scope, so this should always find
/// nothing. It runs anyway, at every open, because "should always" is not a
/// guarantee and a foreign row is data we must never read.
Future<int> purgeForeignScopeRows(AppDatabase db, String scopeKey) async {
  var removed = 0;
  for (final table in db.allTables) {
    removed += await db.customUpdate(
      'DELETE FROM ${table.actualTableName} WHERE scope_key != ?',
      variables: [Variable<String>(scopeKey)],
      updates: {table},
    );
  }
  return removed;
}
