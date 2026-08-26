import 'dart:convert';
import 'dart:io';

import 'package:dotmac_field/core/offline/draft_store.dart';
import 'package:dotmac_field/core/secure/evidence_files.dart';
import 'package:dotmac_field/core/secure/plaintext_migration.dart';
import 'package:dotmac_field/core/secure/secure_field_store.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:path/path.dart' as p;
import 'package:sqlite3/sqlite3.dart' as sqlite;

import '../helpers/secure_store.dart';

const _customerNote = 'Splice box behind the generator, ask for Chidi';
const _draftNote = 'Two drums of 12F drop cable';
const _photoBytes = 'the photograph the technician took';

void main() {
  late TestDevice device;

  /// Writes the exact plaintext store the app shipped before this change: an
  /// unencrypted drift database, loose JPEGs, and a JSON location queue.
  void seedLegacyStore({
    int sentMutations = 1,
    int uploadedPhotos = 1,
    bool locationQueue = true,
  }) {
    final legacy = sqlite.sqlite3.open(device.legacyPaths.database.path);
    legacy
      ..execute('''
        CREATE TABLE outbox_entries (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          client_ref TEXT NOT NULL UNIQUE,
          kind TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          created_at INTEGER NOT NULL);''')
      ..execute('''
        CREATE TABLE pending_photos (
          client_ref TEXT NOT NULL PRIMARY KEY,
          local_path TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'photo',
          work_order_id TEXT,
          installation_project_id TEXT,
          latitude REAL,
          longitude REAL,
          captured_at INTEGER NOT NULL,
          uploaded INTEGER NOT NULL DEFAULT 0,
          failed INTEGER NOT NULL DEFAULT 0,
          last_error TEXT);''')
      ..execute('''
        CREATE TABLE draft_entries (
          id TEXT NOT NULL PRIMARY KEY,
          type TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          updated_at INTEGER NOT NULL);''')
      ..execute('''
        CREATE TABLE cached_jobs (
          id TEXT NOT NULL PRIMARY KEY,
          title TEXT NOT NULL,
          status TEXT NOT NULL,
          work_type TEXT NOT NULL,
          priority TEXT NOT NULL,
          scheduled_start INTEGER,
          detail_json TEXT,
          cached_at INTEGER NOT NULL);''')
      ..execute('''
        CREATE TABLE cached_schedule_entries (
          reference_id TEXT NOT NULL,
          type TEXT NOT NULL,
          start_at INTEGER NOT NULL,
          end_at INTEGER,
          title TEXT NOT NULL,
          PRIMARY KEY (reference_id, start_at));''');

    legacy.execute(
      "INSERT INTO outbox_entries (client_ref, kind, payload_json, status, "
      "attempts, created_at) VALUES "
      "('ref-unsent', 'note', ?, 'pending', 2, 1755000000);",
      [
        jsonEncode({'work_order_id': 'wo-1', 'body': _customerNote}),
      ],
    );
    for (var i = 0; i < sentMutations; i++) {
      legacy.execute(
        "INSERT INTO outbox_entries (client_ref, kind, payload_json, status, "
        "created_at) VALUES ('ref-sent-$i', 'note', '{}', 'sent', 1755000000);",
      );
    }

    final photoDirectory = device.legacyPaths.photos..createSync();
    final photo = File(p.join(photoDirectory.path, 'ref-photo.jpg'))
      ..writeAsStringSync(_photoBytes);
    legacy.execute(
      "INSERT INTO pending_photos (client_ref, local_path, kind, "
      "work_order_id, latitude, longitude, captured_at, uploaded) VALUES "
      "('ref-photo', ?, 'photo', 'wo-1', 6.49, 3.35, 1755000000, 0);",
      [photo.path],
    );
    for (var i = 0; i < uploadedPhotos; i++) {
      final sent = File(p.join(photoDirectory.path, 'ref-gone-$i.jpg'))
        ..writeAsStringSync('already uploaded');
      legacy.execute(
        "INSERT INTO pending_photos (client_ref, local_path, captured_at, "
        "uploaded) VALUES ('ref-gone-$i', ?, 1755000000, 1);",
        [sent.path],
      );
    }

    legacy.execute(
      "INSERT INTO draft_entries (id, type, payload_json, updated_at) VALUES "
      "('material_request:new', 'material_request', ?, 1755000000);",
      [
        jsonEncode({'note': _draftNote}),
      ],
    );
    legacy.execute(
      "INSERT INTO cached_jobs (id, title, status, work_type, priority, "
      "cached_at) VALUES ('wo-1', 'Fibre drop', 'assigned', 'install', "
      "'high', 1755000000);",
    );
    legacy.execute(
      "INSERT INTO cached_schedule_entries (reference_id, type, start_at, "
      "title) VALUES ('sched-1', 'work_order', 1755000000, 'Ikeja splice');",
    );
    legacy.dispose();

    if (locationQueue) {
      device.legacyPaths.locationQueue.writeAsStringSync(
        jsonEncode([
          {
            'latitude': 6.49,
            'longitude': 3.35,
            'captured_at': '2026-08-12T09:00:00.000Z',
            'status': 'on_shift',
            'crm_work_order_id': 'wo-1',
          },
        ]),
      );
    }
  }

  Future<void> expectEvidenceCarried(SecureFieldStore store) async {
    final outbox = await store.database
        .select(store.database.outboxEntries)
        .get();
    expect(outbox.map((row) => row.clientRef), ['ref-unsent']);
    expect(outbox.single.scopeKey, store.scopeKey);
    expect(outbox.single.attempts, 2, reason: 'retry history must survive');
    expect(
      jsonDecode(
        store.cipher.openText(
          outbox.single.payloadJson,
          context: evidenceContext(store.scopeKey, 'outbox', 'ref-unsent'),
        ),
      ),
      {'work_order_id': 'wo-1', 'body': _customerNote},
    );

    final photos = await store.database
        .select(store.database.pendingPhotos)
        .get();
    expect(photos.map((row) => row.clientRef), ['ref-photo']);
    expect(photos.single.scopeKey, store.scopeKey);
    expect(
      utf8.decode(
        await store.evidence.read(
          File(photos.single.localPath),
          purpose: 'photo',
          reference: 'ref-photo',
        ),
      ),
      _photoBytes,
    );

    final drafts = DraftStore(
      db: store.database,
      cipher: store.cipher,
      scopeKey: store.scopeKey,
    );
    expect((await drafts.load('material_request:new'))!['note'], _draftNote);

    final queue = await store.locationQueueFile.readAsBytes();
    expect(
      jsonDecode(
        utf8.decode(
          store.cipher.open(
            queue,
            context: evidenceContext(store.scopeKey, 'location', 'queue'),
          ),
        ),
      ),
      hasLength(1),
    );
  }

  Future<void> expectProjectionsGone(SecureFieldStore store) async {
    for (final table in store.database.rebuildableProjections) {
      final rows = await store.database
          .customSelect('SELECT count(*) AS n FROM ${table.actualTableName};')
          .getSingle();
      expect(
        rows.read<int>('n'),
        0,
        reason: '${table.actualTableName} is rebuildable and must not survive',
      );
    }
  }

  setUp(() {
    device = newTestDevice();
  });

  test('unsent evidence is carried and projections are dropped', () async {
    seedLegacyStore();
    final store = await device.open(techOne);

    final outcome = await PlaintextOfflineMigration(
      legacyPaths: device.legacyPaths,
    ).run(store);

    expect(outcome.ranMigration, isTrue);
    expect(outcome.carriedMutations, 1, reason: 'sent entries stay behind');
    expect(outcome.carriedPhotos, 1, reason: 'uploaded photos stay behind');
    expect(outcome.carriedDrafts, 1);
    expect(outcome.carriedLocationPings, 1);
    expect(outcome.discardedProjectionRows, 2);

    await expectEvidenceCarried(store);
    await expectProjectionsGone(store);

    expect(await device.legacyPaths.exists, isFalse);
    expect(device.legacyPaths.photos.existsSync(), isFalse);
  });

  test('an interrupted carry restarts and loses nothing', () async {
    seedLegacyStore();
    final store = await device.open(techOne);
    final crashing = PlaintextOfflineMigration(
      legacyPaths: device.legacyPaths,
      onCheckpoint: (checkpoint) async {
        if (checkpoint == MigrationCheckpoint.afterRows) {
          throw const _SimulatedProcessDeath();
        }
      },
    );

    await expectLater(
      crashing.run(store),
      throwsA(isA<_SimulatedProcessDeath>()),
    );

    // The plaintext source is intact, because nothing is destroyed until the
    // journal says every carried row has landed.
    expect(await device.legacyPaths.exists, isTrue);
    expect(crashing.journalFile.existsSync(), isTrue);

    final outcome = await PlaintextOfflineMigration(
      legacyPaths: device.legacyPaths,
    ).run(store);

    expect(outcome.ranMigration, isTrue);
    // Re-running is idempotent: one mutation, one photo, one draft — not two.
    await expectEvidenceCarried(store);
    await expectProjectionsGone(store);
    expect(await device.legacyPaths.exists, isFalse);
    expect(crashing.journalFile.existsSync(), isFalse);
  });

  test('an interrupted purge finishes on the next launch', () async {
    seedLegacyStore();
    final store = await device.open(techOne);
    final crashing = PlaintextOfflineMigration(
      legacyPaths: device.legacyPaths,
      onCheckpoint: (checkpoint) async {
        if (checkpoint == MigrationCheckpoint.afterJournalCopied) {
          throw const _SimulatedProcessDeath();
        }
      },
    );

    await expectLater(
      crashing.run(store),
      throwsA(isA<_SimulatedProcessDeath>()),
    );
    expect(await device.legacyPaths.exists, isTrue);

    await PlaintextOfflineMigration(legacyPaths: device.legacyPaths).run(store);

    await expectEvidenceCarried(store);
    expect(await device.legacyPaths.exists, isFalse);
    expect(crashing.journalFile.existsSync(), isFalse);
  });

  test('a migration for one principal is never handed on', () async {
    seedLegacyStore();
    final first = await device.open(techOne);
    final crashing = PlaintextOfflineMigration(
      legacyPaths: device.legacyPaths,
      onCheckpoint: (checkpoint) async {
        if (checkpoint == MigrationCheckpoint.beforeCarry) {
          throw const _SimulatedProcessDeath();
        }
      },
    );
    await expectLater(
      crashing.run(first),
      throwsA(isA<_SimulatedProcessDeath>()),
    );

    // Someone else is holding the handset when it next starts.
    final second = await device.open(techTwo);
    final outcome = await PlaintextOfflineMigration(
      legacyPaths: device.legacyPaths,
    ).run(second);

    expect(outcome.ranMigration, isFalse);
    expect(outcome.carriedTotal, 0);
    expect(
      await device.legacyPaths.exists,
      isFalse,
      reason: 'the previous principal\'s plaintext is destroyed, not adopted',
    );
    final outbox = await second.database
        .select(second.database.outboxEntries)
        .get();
    expect(outbox, isEmpty);
  });

  test('a device with no legacy store runs no migration', () async {
    final store = await device.open(techOne);

    final outcome = await PlaintextOfflineMigration(
      legacyPaths: device.legacyPaths,
    ).run(store);

    expect(outcome.ranMigration, isFalse);
    expect(outcome.carriedTotal, 0);
  });
}

class _SimulatedProcessDeath implements Exception {
  const _SimulatedProcessDeath();
}
