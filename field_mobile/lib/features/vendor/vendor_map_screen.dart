import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:latlong2/latlong.dart';

import '../../app/theme.dart';
import '../../core/location/map_coordinates.dart';
import '../auth/auth_state.dart' show apiClientProvider;
import '../execution/execution_controller.dart' show locationSourceProvider;
import '../today/map_models.dart';

/// Result of a vendor nearby-plant query: the assets and the centre they were
/// fetched around (the crew's location, or the default when unavailable).
class VendorMapData {
  const VendorMapData({required this.center, required this.assets});
  final LatLng center;
  final List<MapAsset> assets;
}

/// Fiber plant near the crew, from the vendor-scoped endpoint (no
/// require_technician). Proximity-scoped: the crew sees the plant around them.
final vendorNearbyPlantProvider = FutureProvider.autoDispose<VendorMapData>((
  ref,
) async {
  final here = await ref.read(locationSourceProvider).current();
  final center =
      safeLatLng(here?.latitude, here?.longitude) ?? defaultMapCenter;
  final response = await ref
      .read(apiClientProvider)
      .dio
      .get(
        '/api/v1/field/vendor/map-assets/nearby',
        queryParameters: {
          'lat': center.latitude,
          'lng': center.longitude,
          'radius_m': 2000,
          'limit': 300,
        },
      );
  final items = (response.data['items'] as List).cast<Map>();
  final assets = items
      .map((item) => MapAsset.fromJson(item.cast<String, dynamic>()))
      .where((asset) => asset.hasValidCoordinates)
      .toList();
  return VendorMapData(center: center, assets: assets);
});

IconData _assetIcon(String type) => switch (type) {
  'olt' => Icons.dns_outlined,
  'fdh_cabinet' => Icons.inbox_outlined,
  'splice_closure' => Icons.hub_outlined,
  'access_point' => Icons.wifi_tethering,
  'termination_point' => Icons.settings_input_hdmi_outlined,
  _ => Icons.circle,
};

/// Vendor-scoped map: the fiber plant around the crew's current location, so a
/// contractor building a route has the same situational context the web portal
/// gives — without the technician-only edit tools.
class VendorMapScreen extends ConsumerWidget {
  const VendorMapScreen({super.key, this.showTiles = true});

  final bool showTiles;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final data = ref.watch(vendorNearbyPlantProvider);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Nearby plant'),
        actions: [
          IconButton(
            tooltip: 'Refresh',
            icon: const Icon(Icons.refresh),
            onPressed: () => ref.invalidate(vendorNearbyPlantProvider),
          ),
        ],
      ),
      body: data.when(
        data: (map) => FlutterMap(
          options: MapOptions(
            initialCenter: map.center,
            initialZoom: 14,
            cameraConstraint: finiteMapCameraConstraint,
          ),
          children: [
            if (showTiles)
              TileLayer(
                urlTemplate: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                userAgentPackageName: 'io.dotmac.dotmac_field',
              ),
            MarkerLayer(
              markers: [
                for (final asset in map.assets)
                  Marker(
                    point: safeLatLng(asset.latitude, asset.longitude)!,
                    width: 38,
                    height: 38,
                    child: GestureDetector(
                      key: Key('vendor-asset-${asset.type}-${asset.id}'),
                      onTap: () => _showAssetSheet(context, asset),
                      child: Icon(
                        _assetIcon(asset.type),
                        size: 28,
                        color: AppColors.primary,
                      ),
                    ),
                  ),
              ],
            ),
            if (showTiles)
              const Align(
                alignment: Alignment.bottomLeft,
                child: Padding(
                  padding: EdgeInsets.all(4),
                  child: Text(
                    '© OpenStreetMap contributors',
                    style: TextStyle(fontSize: 10),
                  ),
                ),
              ),
          ],
        ),
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (_, _) => const Center(child: Text('Could not load the map')),
      ),
    );
  }

  void _showAssetSheet(BuildContext context, MapAsset asset) {
    showModalBottomSheet<void>(
      context: context,
      builder: (_) => SafeArea(
        child: ListTile(
          leading: Icon(_assetIcon(asset.type)),
          title: Text(asset.title),
          subtitle: Text(
            [
              asset.type.replaceAll('_', ' '),
              if (asset.subtitle != null) asset.subtitle!,
            ].join(' · '),
          ),
        ),
      ),
    );
  }
}
