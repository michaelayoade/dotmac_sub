import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:drift/drift.dart';
import 'package:path/path.dart' as p;
import 'package:sqlite3/sqlite3.dart' as sqlite;

import '../offline/database.dart';
import 'evidence_files.dart';
import 'offline_wipe.dart';
import 'secure_field_store.dart';

/// The durable checkpoints of the migration. A test injects a failure at one of
/// these to prove the run is resumable; production never passes a hook.
enum MigrationCheckpoint {
  beforeCarry,
  afterEvidenceFiles,
  afterRows,
  afterJournalCopied,
  afterPurge,
}

class PlaintextMigrationOutcome {
  const PlaintextMigrationOutcome({
    this.ranMigration = false,
    this.carriedMutations = 0,
    this.carriedPhotos = 0,
    this.carriedDrafts = 0,
    this.carriedLocationPings = 0,
    this.discardedProjectionRows = 0,
  });

  final bool ranMigration;
  final int carriedMutations;
  final int carriedPhotos;
  final int carriedDrafts;
  final int carriedLocationPings;

  /// Rows the server can send again, deliberately left behind.
  final int discardedProjectionRows;

  int get carriedTotal =>
      carriedMutations + carriedPhotos + carriedDrafts + carriedLocationPings;
}

/// Carries the pre-encryption plaintext store into the encrypted, scoped store.
///
/// The policy is the point: **unsent customer evidence is re-encrypted and
/// carried, everything the server can send again is destroyed.** Queued
/// mutations, un-uploaded photos and signatures, saved drafts and unsent
/// location pings move across; cached job lists, cached job detail, the
/// schedule, map assets and the evidence-map snapshots do not.
///
/// The run is crash-safe because the plaintext source is not touched until the
/// journal says every carried row has landed. An interruption before that
/// leaves the source intact and the next launch simply starts again — every
/// copy step is idempotent, keyed on the identifiers the server already
/// dedupes on. An interruption after it leaves the source partly deleted and
/// the destination complete, and the next launch finishes the deletion. There
/// is no ordering in which unsent evidence is lost or applied twice.
class PlaintextOfflineMigration {
  PlaintextOfflineMigration({
    required this.legacyPaths,
    this.onCheckpoint,
    sqlite.Database Function(String path)? openLegacyDatabase,
  }) : _openLegacyDatabase = openLegacyDatabase ?? _openFile;

  static sqlite.Database _openFile(String path) => sqlite.sqlite3.open(path);

  static const journalFileName = '.field_plaintext_migration.json';
  static const _phaseCopying = 'copying';
  static const _phaseCopied = 'copied';

  final LegacyPlaintextPaths legacyPaths;

  /// Test seam. Throwing from it simulates the process dying at that point.
  final Future<void> Function(MigrationCheckpoint checkpoint)? onCheckpoint;

  final sqlite.Database Function(String path) _openLegacyDatabase;

  Directory get _documents => legacyPaths.documents;

  File get journalFile => File(p.join(_documents.path, journalFileName));

  Future<PlaintextMigrationOutcome> run(SecureFieldStore store) async {
    final journal = await _readJournal();
    if (journal != null && journal['scope'] != store.scopeKey) {
      // A migration was started for a different principal and never finished.
      // Its source is plaintext belonging to someone else; it is destroyed, not
      // handed to whoever is holding the device now.
      await legacyPaths.destroy();
      await _clearJournal();
      return const PlaintextMigrationOutcome();
    }
    if (journal != null && journal['phase'] == _phaseCopied) {
      await legacyPaths.destroy();
      await _checkpoint(MigrationCheckpoint.afterPurge);
      await _clearJournal();
      return const PlaintextMigrationOutcome(ranMigration: true);
    }
    if (!await legacyPaths.exists) return const PlaintextMigrationOutcome();

    await _writeJournal(_phaseCopying, store.scopeKey);
    await _checkpoint(MigrationCheckpoint.beforeCarry);
    final outcome = await _carry(store);
    await _writeJournal(_phaseCopied, store.scopeKey);
    await _checkpoint(MigrationCheckpoint.afterJournalCopied);
    await legacyPaths.destroy();
    await _checkpoint(MigrationCheckpoint.afterPurge);
    await _clearJournal();
    return outcome;
  }

  Future<PlaintextMigrationOutcome> _carry(SecureFieldStore store) async {
    var mutations = 0;
    var photos = 0;
    var drafts = 0;
    var discarded = 0;

    if (await legacyPaths.database.exists()) {
      final legacy = _openLegacyDatabase(legacyPaths.database.path);
      try {
        discarded = _countProjectionRows(legacy);
        // Evidence files first: a row may only point at an envelope that is
        // already on disk, so a crash between the two re-does both.
        await _carryPhotoFiles(legacy, store);
        await _checkpoint(MigrationCheckpoint.afterEvidenceFiles);
        mutations = await _carryOutbox(legacy, store);
        photos = await _carryPhotoRows(legacy, store);
        drafts = await _carryDrafts(legacy, store);
        await _checkpoint(MigrationCheckpoint.afterRows);
      } finally {
        legacy.dispose();
      }
    }
    final pings = await _carryLocationQueue(store);
    return PlaintextMigrationOutcome(
      ranMigration: true,
      carriedMutations: mutations,
      carriedPhotos: photos,
      carriedDrafts: drafts,
      carriedLocationPings: pings,
      discardedProjectionRows: discarded,
    );
  }

  int _countProjectionRows(sqlite.Database legacy) {
    const projections = [
      'cached_jobs',
      'cached_schedule_entries',
      'cached_map_assets',
      'cached_map_asset_sync_cursors',
      'cached_work_order_evidence_maps',
    ];
    var total = 0;
    for (final table in projections) {
      if (!_hasTable(legacy, table)) continue;
      final rows = legacy.select('SELECT count(*) AS n FROM $table;');
      total += (rows.first['n'] as int?) ?? 0;
    }
    return total;
  }

  bool _hasTable(sqlite.Database legacy, String table) {
    final rows = legacy.select(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?;",
      [table],
    );
    return rows.isNotEmpty;
  }

  Future<int> _carryOutbox(
    sqlite.Database legacy,
    SecureFieldStore store,
  ) async {
    if (!_hasTable(legacy, 'outbox_entries')) return 0;
    final rows = legacy.select(
      "SELECT client_ref, kind, payload_json, status, attempts, last_error, "
      "created_at FROM outbox_entries WHERE status != 'sent';",
    );
    for (final row in rows) {
      await store.database
          .into(store.database.outboxEntries)
          .insert(
            OutboxEntriesCompanion.insert(
              scopeKey: store.scopeKey,
              clientRef: row['client_ref'] as String,
              kind: row['kind'] as String,
              payloadJson: store.cipher.sealText(
                row['payload_json'] as String,
                context: evidenceContext(
                  store.scopeKey,
                  'outbox',
                  row['client_ref'] as String,
                ),
              ),
              status: Value(row['status'] as String? ?? 'pending'),
              attempts: Value((row['attempts'] as int?) ?? 0),
              lastError: Value(row['last_error'] as String?),
              createdAt: _timestamp(row['created_at']),
            ),
            // The server dedupes on client_ref and so do we: a resumed
            // migration must not enqueue the same mutation twice.
            mode: InsertMode.insertOrIgnore,
          );
    }
    return rows.length;
  }

  Future<void> _carryPhotoFiles(
    sqlite.Database legacy,
    SecureFieldStore store,
  ) async {
    if (!_hasTable(legacy, 'pending_photos')) return;
    final rows = legacy.select(
      'SELECT client_ref, local_path FROM pending_photos WHERE uploaded = 0;',
    );
    for (final row in rows) {
      final clientRef = row['client_ref'] as String;
      final source = File(row['local_path'] as String);
      if (!await source.exists()) continue;
      await store.evidence.write(
        _evidenceFileName(clientRef),
        await source.readAsBytes(),
        purpose: 'photo',
        reference: clientRef,
      );
    }
  }

  Future<int> _carryPhotoRows(
    sqlite.Database legacy,
    SecureFieldStore store,
  ) async {
    if (!_hasTable(legacy, 'pending_photos')) return 0;
    final rows = legacy.select(
      'SELECT client_ref, local_path, kind, work_order_id, '
      'installation_project_id, latitude, longitude, captured_at, failed, '
      'last_error FROM pending_photos WHERE uploaded = 0;',
    );
    var carried = 0;
    for (final row in rows) {
      final clientRef = row['client_ref'] as String;
      final envelope = store.evidence.fileNamed(_evidenceFileName(clientRef));
      if (!await envelope.exists()) continue;
      await store.database
          .into(store.database.pendingPhotos)
          .insert(
            PendingPhotosCompanion.insert(
              scopeKey: store.scopeKey,
              clientRef: clientRef,
              localPath: envelope.path,
              kind: Value(row['kind'] as String? ?? 'photo'),
              workOrderId: Value(row['work_order_id'] as String?),
              installationProjectId: Value(
                row['installation_project_id'] as String?,
              ),
              latitude: Value((row['latitude'] as num?)?.toDouble()),
              longitude: Value((row['longitude'] as num?)?.toDouble()),
              capturedAt: _timestamp(row['captured_at']),
              failed: Value(((row['failed'] as int?) ?? 0) != 0),
              lastError: Value(row['last_error'] as String?),
            ),
            mode: InsertMode.insertOrIgnore,
          );
      carried++;
    }
    return carried;
  }

  Future<int> _carryDrafts(
    sqlite.Database legacy,
    SecureFieldStore store,
  ) async {
    if (!_hasTable(legacy, 'draft_entries')) return 0;
    final rows = legacy.select(
      'SELECT id, type, payload_json, updated_at FROM draft_entries;',
    );
    for (final row in rows) {
      final id = row['id'] as String;
      await store.database
          .into(store.database.draftEntries)
          .insertOnConflictUpdate(
            DraftEntriesCompanion.insert(
              scopeKey: store.scopeKey,
              id: id,
              type: row['type'] as String,
              payloadJson: store.cipher.sealText(
                row['payload_json'] as String,
                context: evidenceContext(store.scopeKey, 'draft', id),
              ),
              updatedAt: _timestamp(row['updated_at']),
            ),
          );
    }
    return rows.length;
  }

  Future<int> _carryLocationQueue(SecureFieldStore store) async {
    final legacyQueue = legacyPaths.locationQueue;
    if (!await legacyQueue.exists()) return 0;
    Object? decoded;
    try {
      decoded = jsonDecode(await legacyQueue.readAsString());
    } on Object {
      // A corrupt queue carries nothing; the legacy file is destroyed with the
      // rest of the plaintext store.
      return 0;
    }
    if (decoded is! List || decoded.isEmpty) return 0;
    final target = store.locationQueueFile;
    await target.parent.create(recursive: true);
    final temporary = File('${target.path}.tmp');
    await temporary.writeAsBytes(
      store.cipher.seal(
        Uint8List.fromList(utf8.encode(jsonEncode(decoded))),
        context: evidenceContext(store.scopeKey, 'location', 'queue'),
      ),
      flush: true,
    );
    await temporary.rename(target.path);
    return decoded.length;
  }

  String _evidenceFileName(String clientRef) => '$clientRef.evidence';

  DateTime _timestamp(Object? raw) {
    if (raw is int) {
      // drift stores DateTime columns as unix seconds.
      return DateTime.fromMillisecondsSinceEpoch(raw * 1000, isUtc: true);
    }
    return DateTime.tryParse(raw?.toString() ?? '')?.toUtc() ??
        DateTime.fromMillisecondsSinceEpoch(0, isUtc: true);
  }

  Future<Map<String, dynamic>?> _readJournal() async {
    if (!await journalFile.exists()) return null;
    try {
      final decoded = jsonDecode(await journalFile.readAsString());
      return decoded is Map ? decoded.cast<String, dynamic>() : null;
    } on Object {
      return null;
    }
  }

  Future<void> _writeJournal(String phase, String scopeKey) async {
    await _documents.create(recursive: true);
    await journalFile.writeAsString(
      jsonEncode({'phase': phase, 'scope': scopeKey}),
      flush: true,
    );
  }

  Future<void> _clearJournal() async {
    if (await journalFile.exists()) await journalFile.delete();
  }

  Future<void> _checkpoint(MigrationCheckpoint checkpoint) async {
    final hook = onCheckpoint;
    if (hook != null) await hook(checkpoint);
  }
}
