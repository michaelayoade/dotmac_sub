import 'dart:async';

import 'package:drift/drift.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/offline/database.dart';
import '../auth/auth_state.dart';
import '../execution/execution_controller.dart';
import 'map_models.dart';

class MapAssetsRepository {
  const MapAssetsRepository(this._ref);

  final Ref _ref;

  static const _refreshInterval = Duration(minutes: 5);
  static final _refreshes = <String, Future<void>>{};

  Future<List<MapAsset>> fetchAssets(Set<String> types) async {
    final orderedTypes = _orderedTypes(types);
    if (orderedTypes.isEmpty) return [];

    final cached = await _readCachedAssets(orderedTypes);
    if (cached.isNotEmpty) {
      if (await _cacheNeedsRefresh(orderedTypes)) {
        _refreshInBackground(orderedTypes);
      }
      return cached;
    }

    return _refreshAssets(orderedTypes, incremental: false);
  }

  List<String> _orderedTypes(Set<String> types) {
    final orderedTypes = [
      for (final type in mapAssetTypeLabels.keys)
        if (types.contains(type)) type,
      for (final type in types)
        if (!mapAssetTypeLabels.containsKey(type)) type,
    ];
    return orderedTypes;
  }

  Future<_MapAssetFetch> _fetchAssetType(
    String type, {
    DateTime? updatedSince,
  }) async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .get(
          '/api/v1/field/map-assets',
          queryParameters: {
            'types': type,
            'limit': 1000,
            if (updatedSince != null)
              'updated_since': updatedSince.toIso8601String(),
          },
        );
    final items = (response.data['items'] as List).cast<Map>();
    final deletedItems =
        ((response.data as Map)['deleted'] as List? ?? const []).cast<Map>();
    final serverTimeRaw = (response.data as Map)['server_time'] as String?;
    return _MapAssetFetch(
      assets: items
          .map((item) => MapAsset.fromJson(item.cast<String, dynamic>()))
          .where((asset) => asset.hasValidCoordinates)
          .toList(),
      deleted: deletedItems
          .map(
            (item) => _DeletedMapAsset.fromJson(item.cast<String, dynamic>()),
          )
          .toList(),
      serverTime: serverTimeRaw != null
          ? DateTime.parse(serverTimeRaw).toUtc()
          : DateTime.now().toUtc(),
    );
  }

  Future<List<MapAsset>> _readCachedAssets(List<String> types) async {
    final db = _ref.read(syncServiceProvider).db;
    final rows = await (db.select(
      db.cachedMapAssets,
    )..where((row) => row.assetType.isIn(types))).get();
    rows.sort((a, b) {
      final typeOrder = types
          .indexOf(a.assetType)
          .compareTo(types.indexOf(b.assetType));
      if (typeOrder != 0) return typeOrder;
      return a.title.compareTo(b.title);
    });
    return rows
        .map(
          (row) => MapAsset(
            id: row.assetId,
            type: row.assetType,
            title: row.title,
            subtitle: row.subtitle,
            latitude: row.latitude,
            longitude: row.longitude,
            status: row.status,
            updatedAt: row.updatedAt,
          ),
        )
        .where((asset) => asset.hasValidCoordinates)
        .toList();
  }

  Future<bool> _cacheNeedsRefresh(List<String> types) async {
    final db = _ref.read(syncServiceProvider).db;
    final cursors = await (db.select(
      db.cachedMapAssetSyncCursors,
    )..where((row) => row.assetType.isIn(types))).get();
    if (cursors.isEmpty) return true;
    final cursorByType = {
      for (final cursor in cursors) cursor.assetType: cursor,
    };
    if (types.any((type) => !cursorByType.containsKey(type))) return true;
    final cutoff = DateTime.now().toUtc().subtract(_refreshInterval);
    return cursors.any((cursor) => cursor.syncedAt.isBefore(cutoff));
  }

  void _refreshInBackground(List<String> types) {
    final key = types.join(',');
    _refreshes[key] ??= _refreshAssets(types, incremental: true)
        .then((_) => _ref.invalidate(mapAssetsProvider))
        .whenComplete(() => _refreshes.remove(key));
  }

  Future<List<MapAsset>> _refreshAssets(
    List<String> types, {
    required bool incremental,
  }) async {
    final groups = <List<MapAsset>>[];
    for (final type in types) {
      final cursor = incremental ? await _readCursor(type) : null;
      final fetched = await _fetchAssetType(type, updatedSince: cursor);
      if (incremental) {
        await _deleteCachedAssets(fetched.deleted);
        await _upsertCachedAssets(fetched.assets);
      } else {
        await _replaceCachedAssetType(type, fetched.assets);
      }
      await _writeCursor(type, fetched.serverTime);
      groups.add(fetched.assets);
    }
    return [for (final group in groups) ...group];
  }

  Future<DateTime?> _readCursor(String type) async {
    final db = _ref.read(syncServiceProvider).db;
    final row = await (db.select(
      db.cachedMapAssetSyncCursors,
    )..where((row) => row.assetType.equals(type))).getSingleOrNull();
    return row?.syncedAt;
  }

  Future<void> _writeCursor(String type, DateTime serverTime) async {
    final db = _ref.read(syncServiceProvider).db;
    await db
        .into(db.cachedMapAssetSyncCursors)
        .insert(
          CachedMapAssetSyncCursorsCompanion.insert(
            assetType: type,
            syncedAt: serverTime,
          ),
          mode: InsertMode.insertOrReplace,
        );
  }

  Future<void> _replaceCachedAssetType(
    String type,
    List<MapAsset> assets,
  ) async {
    final db = _ref.read(syncServiceProvider).db;
    await (db.delete(
      db.cachedMapAssets,
    )..where((row) => row.assetType.equals(type))).go();
    await _upsertCachedAssets(assets);
  }

  Future<void> _deleteCachedAssets(List<_DeletedMapAsset> deleted) async {
    if (deleted.isEmpty) return;
    final db = _ref.read(syncServiceProvider).db;
    for (final asset in deleted) {
      await (db.delete(db.cachedMapAssets)..where(
            (row) =>
                row.assetType.equals(asset.type) & row.assetId.equals(asset.id),
          ))
          .go();
    }
  }

  Future<void> _upsertCachedAssets(List<MapAsset> assets) async {
    if (assets.isEmpty) return;
    final db = _ref.read(syncServiceProvider).db;
    final now = DateTime.now().toUtc();
    await db.batch((batch) {
      for (final asset in assets) {
        batch.insert(
          db.cachedMapAssets,
          CachedMapAssetsCompanion.insert(
            assetType: asset.type,
            assetId: asset.id,
            title: asset.title,
            subtitle: Value(asset.subtitle),
            latitude: asset.latitude,
            longitude: asset.longitude,
            status: Value(asset.status),
            updatedAt: Value(asset.updatedAt),
            cachedAt: now,
          ),
          mode: InsertMode.insertOrReplace,
        );
      }
    });
  }

  Future<MapAsset> updateLocation({
    required String type,
    required String id,
    required double latitude,
    required double longitude,
  }) async {
    final response = await _ref
        .read(apiClientProvider)
        .dio
        .patch(
          '/api/v1/field/map-assets/$type/$id/location',
          data: {'latitude': latitude, 'longitude': longitude},
        );
    final asset = MapAsset.fromJson(
      (response.data as Map).cast<String, dynamic>(),
    );
    await _upsertCachedAssets([asset]);
    return asset;
  }
}

class MapPlaceSearchRepository {
  const MapPlaceSearchRepository(this._ref);

  final Ref _ref;

  Future<List<MapPlaceSearchResult>> search(String query) async {
    final term = query.trim();
    if (term.length < 2) return const [];
    try {
      final sync = _ref.read(syncServiceProvider);
      if (!await sync.connectivity.isOnline) return const [];
      final response = await _ref
          .read(apiClientProvider)
          .dio
          .get(
            '/api/v1/field/map-assets/search',
            queryParameters: {'q': term, 'limit': 12},
          );
      return _items(response.data)
          .map(MapPlaceSearchResult.fromJson)
          .where((place) => place.hasValidCoordinates)
          .toList();
    } catch (_) {
      return const [];
    }
  }
}

List<Map<String, dynamic>> _items(Object? data) {
  if (data is Map && data['items'] is List) {
    return (data['items'] as List)
        .cast<Map>()
        .map((item) => item.cast<String, dynamic>())
        .toList();
  }
  if (data is List) {
    return data
        .cast<Map>()
        .map((item) => item.cast<String, dynamic>())
        .toList();
  }
  return const [];
}

class _MapAssetFetch {
  const _MapAssetFetch({
    required this.assets,
    required this.deleted,
    required this.serverTime,
  });

  final List<MapAsset> assets;
  final List<_DeletedMapAsset> deleted;
  final DateTime serverTime;
}

class _DeletedMapAsset {
  const _DeletedMapAsset({required this.type, required this.id});

  final String type;
  final String id;

  factory _DeletedMapAsset.fromJson(Map<String, dynamic> json) =>
      _DeletedMapAsset(type: json['type'] as String, id: json['id'] as String);
}

final mapAssetsRepositoryProvider = Provider<MapAssetsRepository>(
  MapAssetsRepository.new,
);

final mapPlaceSearchRepositoryProvider = Provider<MapPlaceSearchRepository>(
  MapPlaceSearchRepository.new,
);

final mapPlaceSearchProvider = FutureProvider.autoDispose
    .family<List<MapPlaceSearchResult>, String>((ref, query) {
      return ref.watch(mapPlaceSearchRepositoryProvider).search(query);
    });

final selectedMapAssetTypesProvider = StateProvider<Set<String>>(
  (ref) => {...defaultMapAssetTypes},
);

final mapAssetsProvider = FutureProvider<List<MapAsset>>((ref) {
  final types = ref.watch(selectedMapAssetTypesProvider);
  if (types.isEmpty) return Future.value([]);
  return ref.watch(mapAssetsRepositoryProvider).fetchAssets(types);
});
