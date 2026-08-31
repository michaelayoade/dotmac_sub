import 'dart:convert';
import 'dart:ffi';
import 'dart:io';

import 'package:dotmac_field/core/api/token_store.dart';
import 'package:dotmac_field/core/offline/database.dart';
import 'package:dotmac_field/core/secure/data_scope.dart';
import 'package:dotmac_field/core/secure/evidence_files.dart';
import 'package:dotmac_field/core/secure/offline_wipe.dart';
import 'package:dotmac_field/core/secure/plaintext_migration.dart';
import 'package:dotmac_field/core/secure/scope_key_ring.dart';
import 'package:dotmac_field/core/secure/scope_reconciler.dart';
import 'package:dotmac_field/core/secure/secret_vault.dart';
import 'package:dotmac_field/core/secure/secure_field_store.dart';
import 'package:dotmac_field/core/secure/session_lifecycle.dart';
import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqlite3/open.dart';

/// Host machines ship libsqlite3.so.0 without the unversioned symlink sqlite3
/// looks for by default. Every suite that touches a database calls this first,
/// the same way the pre-existing suites do.
void useHostSqlite3() {
  if (!Platform.isLinux) return;
  open.overrideFor(
    OperatingSystem.linux,
    () => DynamicLibrary.open('libsqlite3.so.0'),
  );
}

/// The two technicians and two deployments the isolation tests use. Distinct in
/// both dimensions so a test can cross either one on its own.
const techOne = DataScope(
  tenant: 'https://test.local',
  principal: 'system_user:tech-1',
);
const techTwo = DataScope(
  tenant: 'https://test.local',
  principal: 'system_user:tech-2',
);
const techOneOtherTenant = DataScope(
  tenant: 'https://other.local',
  principal: 'system_user:tech-1',
);

/// A whole device's secure storage on a temporary directory.
///
/// Everything is the production code path except the SQLCipher connection
/// itself: CI's sqlite3 is not SQLCipher, so the database here is an in-memory
/// drift database and the cipher layer is covered separately by
/// `encrypted_store_test.dart`, which proves the app refuses to open a store
/// without it.
class TestDevice {
  TestDevice({
    required this.documents,
    required this.vault,
    required this.keyRing,
    required this.opener,
    required this.legacyPaths,
    required this.wipe,
    required this.reconciler,
    required this.tokenStore,
  });

  final Directory documents;
  final InMemorySecretVault vault;
  final ScopeKeyRing keyRing;
  final SecureStoreOpener opener;
  final LegacyPlaintextPaths legacyPaths;
  final ScopedOfflineWipe wipe;
  final ScopeReconciler reconciler;
  final TokenStore tokenStore;

  final List<SecureFieldStore> _opened = [];

  Future<SecureFieldStore> open(DataScope scope) async {
    final store = await opener.open(scope);
    _opened.add(store);
    return store;
  }

  SessionLifecycle lifecycle({
    OfflineWipe? wipeOverride,
    PlaintextOfflineMigration? migration,
    String baseUrl = 'https://test.local',
  }) => SessionLifecycle(
    runtime: SecureRuntime(
      opener: opener,
      wipe: wipeOverride ?? wipe,
      reconciler: reconciler,
      migration:
          migration ?? PlaintextOfflineMigration(legacyPaths: legacyPaths),
      tokenStore: tokenStore,
      baseUrl: baseUrl,
    ),
  );

  Future<void> dispose() async {
    for (final store in _opened) {
      try {
        await store.database.close();
      } on Object {
        // Already closed by a wipe under test.
      }
    }
    if (documents.existsSync()) documents.deleteSync(recursive: true);
  }
}

/// Builds a device whose storage lives in a fresh temporary directory, torn
/// down at the end of the test.
TestDevice newTestDevice({TokenStore? tokenStore}) {
  useHostSqlite3();
  final documents = Directory.systemTemp.createTempSync('field-secure-test');
  final vault = InMemorySecretVault();
  final keyRing = ScopeKeyRing(vault);
  final tokens = tokenStore ?? InMemoryTokenStore();
  final legacyPaths = LegacyPlaintextPaths(documents);
  final wipe = ScopedOfflineWipe(
    documents: documents,
    keyRing: keyRing,
    tokenStore: tokens,
    legacyPaths: legacyPaths,
  );
  final device = TestDevice(
    documents: documents,
    vault: vault,
    keyRing: keyRing,
    opener: SecureStoreOpener(
      documents: documents,
      keyRing: keyRing,
      openDatabase: (file, keyHex) => AppDatabase(NativeDatabase.memory()),
    ),
    legacyPaths: legacyPaths,
    wipe: wipe,
    reconciler: ScopeReconciler(
      documents: documents,
      keyRing: keyRing,
      wipe: wipe,
    ),
    tokenStore: tokens,
  );
  addTearDown(device.dispose);
  return device;
}

/// One open store for tests that only need somewhere scoped to write.
Future<SecureFieldStore> openTestStore([DataScope scope = techOne]) =>
    newTestDevice().open(scope);

/// Reads back a queued mutation's payload. Queued payloads are envelopes, so
/// tests decode them the same way the sync service does.
Map<String, dynamic> readQueuedPayload(
  SecureFieldStore store,
  String clientRef,
  String envelope,
) {
  final json = store.cipher.openText(
    envelope,
    context: evidenceContext(store.scopeKey, 'outbox', clientRef),
  );
  return (jsonDecode(json) as Map).cast<String, dynamic>();
}
