import 'package:drift/drift.dart';

part 'database.g.dart';

/// Cached job snapshots: the list payload plus the full detail JSON so the
/// app works in coverage dead zones.
class CachedJobs extends Table {
  TextColumn get id => text()();
  TextColumn get title => text()();
  TextColumn get status => text()();
  TextColumn get workType => text()();
  TextColumn get priority => text()();
  DateTimeColumn get scheduledStart => dateTime().nullable()();
  TextColumn get detailJson => text().nullable()();
  DateTimeColumn get cachedAt => dateTime()();

  @override
  Set<Column> get primaryKey => {id};
}

class CachedScheduleEntries extends Table {
  TextColumn get referenceId => text()();
  TextColumn get type => text()();
  DateTimeColumn get startAt => dateTime()();
  DateTimeColumn get endAt => dateTime().nullable()();
  TextColumn get title => text()();

  @override
  Set<Column> get primaryKey => {referenceId, startAt};
}

class CachedMapAssets extends Table {
  TextColumn get assetType => text()();
  TextColumn get assetId => text()();
  TextColumn get title => text()();
  TextColumn get subtitle => text().nullable()();
  RealColumn get latitude => real()();
  RealColumn get longitude => real()();
  TextColumn get status => text().nullable()();
  DateTimeColumn get updatedAt => dateTime().nullable()();
  DateTimeColumn get cachedAt => dateTime()();

  @override
  Set<Column> get primaryKey => {assetType, assetId};
}

class CachedMapAssetSyncCursors extends Table {
  TextColumn get assetType => text()();
  DateTimeColumn get syncedAt => dateTime()();

  @override
  Set<Column> get primaryKey => {assetType};
}

/// Queued offline mutations, flushed FIFO. `clientRef` doubles as the
/// server-side idempotency key (client_event_id / client_ref).
class OutboxEntries extends Table {
  IntColumn get seq => integer().autoIncrement()();
  TextColumn get clientRef => text().unique()();
  TextColumn get kind =>
      text()(); // transition|note|worklog|material_consume|expense_request|as_built
  TextColumn get payloadJson => text()();
  TextColumn get status =>
      text().withDefault(const Constant('pending'))(); // pending|sent|conflict
  IntColumn get attempts => integer().withDefault(const Constant(0))();
  TextColumn get lastError => text().nullable()();
  DateTimeColumn get createdAt => dateTime()();
}

/// Photos captured offline, uploaded by the sync service.
class PendingPhotos extends Table {
  TextColumn get clientRef => text()();
  TextColumn get localPath => text()();
  TextColumn get kind => text().withDefault(const Constant('photo'))();
  TextColumn get workOrderId => text().nullable()();
  TextColumn get installationProjectId => text().nullable()();
  RealColumn get latitude => real().nullable()();
  RealColumn get longitude => real().nullable()();
  DateTimeColumn get capturedAt => dateTime()();
  BoolColumn get uploaded => boolean().withDefault(const Constant(false))();
  // Terminal rejection (permanent 4xx) — excluded from upload retries and
  // surfaced in the Profile conflict-review list. Distinct from `uploaded`.
  BoolColumn get failed => boolean().withDefault(const Constant(false))();
  TextColumn get lastError => text().nullable()();

  @override
  Set<Column> get primaryKey => {clientRef};
}

/// Local form drafts that have not been submitted yet. These are not synced
/// directly; the relevant form reloads them and submits through the normal API.
class DraftEntries extends Table {
  TextColumn get id => text()();
  TextColumn get type => text()(); // material_request|expense_request
  TextColumn get payloadJson => text()();
  DateTimeColumn get updatedAt => dateTime()();

  @override
  Set<Column> get primaryKey => {id};
}

@DriftDatabase(
  tables: [
    CachedJobs,
    CachedScheduleEntries,
    CachedMapAssets,
    CachedMapAssetSyncCursors,
    OutboxEntries,
    PendingPhotos,
    DraftEntries,
  ],
)
class AppDatabase extends _$AppDatabase {
  AppDatabase(super.executor);

  @override
  int get schemaVersion => 4;

  @override
  MigrationStrategy get migration => MigrationStrategy(
    onUpgrade: (m, from, to) async {
      if (from < 2) {
        await m.addColumn(pendingPhotos, pendingPhotos.failed);
      }
      if (from < 3) {
        await m.createTable(cachedMapAssets);
        await m.createTable(cachedMapAssetSyncCursors);
      }
      if (from < 4) {
        await m.createTable(draftEntries);
      }
    },
  );
}
