import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:drift/drift.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_client.dart';
import '../../core/offline/database.dart';
import '../auth/auth_state.dart';
import '../execution/execution_controller.dart';
import 'work_order_evidence_map_models.dart';

/// Read-only projection/cache adapter for the server-owned work-order evidence
/// map. It never combines jobs, discovers assets, or queues topology changes.
class WorkOrderEvidenceMapRepository {
  const WorkOrderEvidenceMapRepository(this._ref);

  final Ref _ref;

  Future<WorkOrderEvidenceMapSnapshot> fetch(String workOrderPublicId) async {
    final principalScope = await _principalScope();
    try {
      final response = await _ref
          .read(apiClientProvider)
          .dio
          .get(
            '/api/v1/field/fiber/work-order-evidence-map',
            queryParameters: {'work_order_id': workOrderPublicId},
          );
      final payload = (response.data as Map).cast<String, dynamic>();
      final snapshot = WorkOrderEvidenceMapSnapshot.fromJson(
        payload,
        requestedWorkOrderPublicId: workOrderPublicId,
      );
      await _replaceCachedSnapshot(principalScope, snapshot, payload);
      return snapshot;
    } on DioException catch (error) {
      if (!_canUseOfflineCache(error)) rethrow;
      final cached = await _readCachedSnapshot(
        principalScope,
        workOrderPublicId,
      );
      if (cached == null) rethrow;
      return cached;
    }
  }

  Future<String> _principalScope() async {
    final token = await _ref.read(apiClientProvider).tokenStore.accessToken;
    final subject = token == null ? null : jwtSubject(token);
    if (subject == null) {
      throw const FormatException(
        'Authenticated subject is required for offline evidence isolation',
      );
    }
    return subject;
  }

  bool _canUseOfflineCache(DioException error) {
    final statusCode = error.response?.statusCode;
    return statusCode == null || statusCode >= 500;
  }

  Future<void> _replaceCachedSnapshot(
    String principalScope,
    WorkOrderEvidenceMapSnapshot snapshot,
    Map<String, dynamic> payload,
  ) async {
    final db = _ref.read(syncServiceProvider).db;
    final now = DateTime.now().toUtc();
    await db.transaction(() async {
      await (db.delete(db.cachedWorkOrderEvidenceMaps)..where(
            (row) =>
                row.principalScope.equals(principalScope) &
                row.workOrderPublicId.equals(snapshot.workOrderPublicId),
          ))
          .go();
      await db
          .into(db.cachedWorkOrderEvidenceMaps)
          .insert(
            CachedWorkOrderEvidenceMapsCompanion.insert(
              principalScope: principalScope,
              workOrderPublicId: snapshot.workOrderPublicId,
              reportSha256: snapshot.reportSha256,
              sourceOverlaySha256: snapshot.sourceOverlaySha256,
              payloadJson: jsonEncode(payload),
              cachedAt: now,
            ),
            mode: InsertMode.insertOrReplace,
          );
    });
  }

  Future<WorkOrderEvidenceMapSnapshot?> _readCachedSnapshot(
    String principalScope,
    String workOrderPublicId,
  ) async {
    final db = _ref.read(syncServiceProvider).db;
    final query = db.select(db.cachedWorkOrderEvidenceMaps)
      ..where(
        (row) =>
            row.principalScope.equals(principalScope) &
            row.workOrderPublicId.equals(workOrderPublicId),
      )
      ..orderBy([(row) => OrderingTerm.desc(row.cachedAt)])
      ..limit(1);
    final row = await query.getSingleOrNull();
    if (row == null) return null;
    final payload = (jsonDecode(row.payloadJson) as Map)
        .cast<String, dynamic>();
    final snapshot = WorkOrderEvidenceMapSnapshot.fromJson(
      payload,
      requestedWorkOrderPublicId: workOrderPublicId,
      fromCache: true,
      cachedAt: row.cachedAt,
    );
    if (snapshot.reportSha256 != row.reportSha256 ||
        snapshot.sourceOverlaySha256 != row.sourceOverlaySha256) {
      throw const FormatException(
        'Cached work-order evidence identity does not match its payload',
      );
    }
    return snapshot;
  }
}

final workOrderEvidenceMapRepositoryProvider =
    Provider<WorkOrderEvidenceMapRepository>(
      WorkOrderEvidenceMapRepository.new,
    );

final workOrderEvidenceMapProvider =
    FutureProvider.family<WorkOrderEvidenceMapSnapshot, String>(
      (ref, workOrderPublicId) => ref
          .watch(workOrderEvidenceMapRepositoryProvider)
          .fetch(workOrderPublicId),
    );
