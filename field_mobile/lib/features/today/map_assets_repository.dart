import 'dart:async';

import 'package:drift/drift.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/offline/database.dart';
import '../auth/auth_state.dart';
import '../execution/execution_controller.dart';
import 'map_models.dart';

class MapAssetsRepository {
  MapAssetsRepository(this._ref);

  final Ref _ref;

  static const _refreshInterval = Duration(minutes: 5);
  final _refreshes = <String, Future<void>>{};

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

    return _refreshAssets(orderedTypes);
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

  // The list API has no tombstones or snapshot cursor. Fetch every page before
  // replacing this rebuildable projection; updated_since cannot remove assets.
  Future<List<MapAsset>> _fetchAssetType(String type) async {
    const limit = 1000;
    final assets = <MapAsset>[];
    for (var offset = 0; ; offset += limit) {
      final response = await _ref
          .read(apiClientProvider)
          .dio
          .get(
            '/api/v1/field/map-assets',
            queryParameters: {
              'asset_type': [type],
              'limit': limit,
              'offset': offset,
            },
          );
      final page = _MapAssetPage.fromJson(
        (response.data as Map).cast<String, dynamic>(),
      );
      if (page.offset != offset ||
          page.limit != limit ||
          page.count > limit ||
          page.assets.any((asset) => asset.type != type)) {
        throw const FormatException('Unexpected map asset page');
      }
      assets.addAll(page.assets.where((asset) => asset.hasValidCoordinates));
      // Count is this page's raw row count, including invalid coordinates.
      if (page.count < limit) return assets;
    }
  }

  Future<List<MapAsset>> _readCachedAssets(List<String> types) async {
    final sync = _ref.read(syncServiceProvider);
    final db = sync.db;
    final rows =
        await (db.select(db.cachedMapAssets)..where(
              (row) =>
                  row.scopeKey.equals(sync.scopeKey) &
                  row.assetType.isIn(types),
            ))
            .get();
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
    final sync = _ref.read(syncServiceProvider);
    final db = sync.db;
    final cursors =
        await (db.select(db.cachedMapAssetSyncCursors)..where(
              (row) =>
                  row.scopeKey.equals(sync.scopeKey) &
                  row.assetType.isIn(types),
            ))
            .get();
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
    _refreshes[key] ??= _refreshAssets(types)
        .then((_) => _ref.invalidate(mapAssetsProvider))
        // A failed background read retains the last complete offline snapshot.
        .catchError((Object _, StackTrace _) {})
        .whenComplete(() => _refreshes.remove(key));
  }

  Future<List<MapAsset>> _refreshAssets(List<String> types) async {
    final groups = <List<MapAsset>>[];
    for (final type in types) {
      final fetched = await _fetchAssetType(type);
      final db = _ref.read(syncServiceProvider).db;
      await db.transaction(() async {
        await _replaceCachedAssetType(type, fetched);
        await _writeCursor(type, DateTime.now().toUtc());
      });
      groups.add(fetched);
    }
    return [for (final group in groups) ...group];
  }

  Future<void> _writeCursor(String type, DateTime completedAt) async {
    final sync = _ref.read(syncServiceProvider);
    final db = sync.db;
    await db
        .into(db.cachedMapAssetSyncCursors)
        .insert(
          CachedMapAssetSyncCursorsCompanion.insert(
            scopeKey: sync.scopeKey,
            assetType: type,
            syncedAt: completedAt,
          ),
          mode: InsertMode.insertOrReplace,
        );
  }

  Future<void> _replaceCachedAssetType(
    String type,
    List<MapAsset> assets,
  ) async {
    final sync = _ref.read(syncServiceProvider);
    final db = sync.db;
    await (db.delete(db.cachedMapAssets)..where(
          (row) =>
              row.scopeKey.equals(sync.scopeKey) & row.assetType.equals(type),
        ))
        .go();
    await _upsertCachedAssets(assets);
  }

  Future<void> _upsertCachedAssets(List<MapAsset> assets) async {
    if (assets.isEmpty) return;
    final sync = _ref.read(syncServiceProvider);
    final db = sync.db;
    final now = DateTime.now().toUtc();
    await db.batch((batch) {
      for (final asset in assets) {
        batch.insert(
          db.cachedMapAssets,
          CachedMapAssetsCompanion.insert(
            scopeKey: sync.scopeKey,
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

class _MapAssetPage {
  const _MapAssetPage({
    required this.assets,
    required this.count,
    required this.limit,
    required this.offset,
  });

  final List<MapAsset> assets;
  final int count;
  final int limit;
  final int offset;

  factory _MapAssetPage.fromJson(Map<String, dynamic> json) {
    final assets = (json['items'] as List)
        .cast<Map>()
        .map((item) => MapAsset.fromJson(item.cast<String, dynamic>()))
        .toList();
    final count = json['count'] as int;
    if (count != assets.length) {
      throw const FormatException('Unexpected map asset count');
    }
    return _MapAssetPage(
      assets: assets,
      count: count,
      limit: json['limit'] as int,
      offset: json['offset'] as int,
    );
  }
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
