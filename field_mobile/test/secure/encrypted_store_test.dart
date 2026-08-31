import 'package:dotmac_field/core/secure/encrypted_database.dart';
import 'package:flutter_test/flutter_test.dart';

/// CI's sqlite3 is not SQLCipher, so these tests cover the keying step against
/// a recording connection rather than a real database. That is the layer worth
/// pinning anyway: plain sqlite accepts `PRAGMA key` and silently writes a
/// readable file, so the only thing standing between a technician's evidence
/// and a plaintext database is this function refusing to continue.
void main() {
  const key =
      '00112233445566778899aabbccddeeff'
      '00112233445566778899aabbccddeeff';

  test('the key pragma is the first statement on the connection', () {
    final connection = _RecordingConnection(cipherVersion: '4.5.7');

    applySqlCipherKey(connection, key);

    expect(connection.statements.first, 'PRAGMA key = "x\'$key\'";');
    expect(
      connection.statements,
      containsAllInOrder([
        'PRAGMA key = "x\'$key\'";',
        'PRAGMA cipher_version;',
        'SELECT count(*) FROM sqlite_master;',
      ]),
      reason: 'SQLCipher accepts the key only before the first read',
    );
  });

  test('a build without SQLCipher is refused, not tolerated', () {
    final connection = _RecordingConnection(cipherVersion: null);

    expect(
      () => applySqlCipherKey(connection, key),
      throwsA(isA<PlaintextStorageRefused>()),
    );
    expect(
      connection.statements,
      isNot(contains('SELECT count(*) FROM sqlite_master;')),
      reason: 'nothing may touch the schema of a database we cannot encrypt',
    );
  });

  test('an empty cipher_version is refused like a missing one', () {
    final connection = _RecordingConnection(cipherVersion: '   ');

    expect(
      () => applySqlCipherKey(connection, key),
      throwsA(isA<PlaintextStorageRefused>()),
    );
  });

  test('a key that is not 32 raw bytes never reaches the connection', () {
    final connection = _RecordingConnection(cipherVersion: '4.5.7');

    expect(
      () => applySqlCipherKey(connection, 'not-a-key'),
      throwsA(isA<PlaintextStorageRefused>()),
    );
    expect(connection.statements, isEmpty);
  });
}

class _RecordingConnection implements CipherConnection {
  _RecordingConnection({required this.cipherVersion});

  /// What `PRAGMA cipher_version` reports. Null models plain sqlite3, which
  /// answers with no rows at all.
  final String? cipherVersion;

  final List<String> statements = [];

  @override
  void execute(String sql) => statements.add(sql);

  @override
  List<List<Object?>> select(String sql) {
    statements.add(sql);
    if (sql == 'PRAGMA cipher_version;') {
      final version = cipherVersion;
      if (version == null) return const [];
      return [
        [version],
      ];
    }
    return const [
      [0],
    ];
  }
}
