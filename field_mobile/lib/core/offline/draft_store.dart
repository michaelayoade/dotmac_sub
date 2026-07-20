import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'database.dart';

const materialRequestDraftId = 'material_request:new';
const expenseRequestDraftId = 'expense_request:new';

class DraftStore {
  const DraftStore(this.db);

  final AppDatabase? db;

  Future<Map<String, dynamic>?> load(String id) async {
    final database = db;
    if (database == null) return null;
    final row = await (database.select(
      database.draftEntries,
    )..where((entry) => entry.id.equals(id))).getSingleOrNull();
    if (row == null) return null;
    return (jsonDecode(row.payloadJson) as Map).cast<String, dynamic>();
  }

  Future<void> save({
    required String id,
    required String type,
    required Map<String, dynamic> payload,
  }) async {
    final database = db;
    if (database == null) return;
    final now = DateTime.now().toUtc();
    await database
        .into(database.draftEntries)
        .insertOnConflictUpdate(
          DraftEntriesCompanion.insert(
            id: id,
            type: type,
            payloadJson: jsonEncode(payload),
            updatedAt: now,
          ),
        );
  }

  Future<void> delete(String id) async {
    final database = db;
    if (database == null) return;
    await (database.delete(
      database.draftEntries,
    )..where((entry) => entry.id.equals(id))).go();
  }
}

final draftStoreProvider = Provider<DraftStore>(
  (ref) => const DraftStore(null),
);
