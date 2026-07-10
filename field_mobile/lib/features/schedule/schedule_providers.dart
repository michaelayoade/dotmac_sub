import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/offline/database.dart';
import '../auth/auth_state.dart';
import '../execution/execution_controller.dart';

class ScheduleEntry {
  const ScheduleEntry({
    required this.type,
    required this.startAt,
    this.endAt,
    required this.title,
    required this.referenceId,
  });

  final String type; // shift | availability | job
  final DateTime startAt;
  final DateTime? endAt;
  final String title;
  final String referenceId;

  factory ScheduleEntry.fromJson(Map<String, dynamic> json) => ScheduleEntry(
    type: json['type'] as String,
    startAt: DateTime.parse(json['start_at'] as String),
    endAt: json['end_at'] != null
        ? DateTime.parse(json['end_at'] as String)
        : null,
    title: json['title'] as String? ?? '',
    referenceId: json['reference_id'] as String,
  );
}

/// Entries grouped by local calendar day, days sorted ascending.
List<(DateTime, List<ScheduleEntry>)> groupByDay(List<ScheduleEntry> entries) {
  final sorted = [...entries]..sort((a, b) => a.startAt.compareTo(b.startAt));
  final groups = <DateTime, List<ScheduleEntry>>{};
  for (final entry in sorted) {
    final local = entry.startAt.toLocal();
    final day = DateTime(local.year, local.month, local.day);
    groups.putIfAbsent(day, () => []).add(entry);
  }
  return [for (final day in groups.keys.toList()..sort()) (day, groups[day]!)];
}

/// Schedule entries plus whether they came from the offline cache (drives the
/// stale banner, mirroring the jobs list).
class ScheduleData {
  const ScheduleData(this.entries, {this.fromCache = false});

  final List<ScheduleEntry> entries;
  final bool fromCache;
}

ScheduleEntry _entryFromCache(CachedScheduleEntry row) => ScheduleEntry(
  type: row.type,
  startAt: row.startAt,
  endAt: row.endAt,
  title: row.title,
  referenceId: row.referenceId,
);

final scheduleProvider = FutureProvider<ScheduleData>((ref) async {
  final sync = ref.read(syncServiceProvider);
  try {
    final response = await ref
        .watch(apiClientProvider)
        .dio
        .get('/api/v1/field/schedule');
    final items = (response.data as List).cast<Map>();
    await sync.cacheSchedule(items); // keep the offline cache warm
    return ScheduleData(
      items
          .map((item) => ScheduleEntry.fromJson(item.cast<String, dynamic>()))
          .toList(),
    );
  } on DioException {
    // Offline / server unreachable: serve the cache so the tech still works.
    final cached = await sync.readCachedSchedule();
    if (cached.isEmpty) rethrow;
    return ScheduleData(cached.map(_entryFromCache).toList(), fromCache: true);
  }
});
