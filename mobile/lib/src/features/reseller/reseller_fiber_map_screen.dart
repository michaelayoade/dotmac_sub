import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:latlong2/latlong.dart';

import '../../core/semantic_colors.dart';
import '../../models/reseller.dart';
import '../../providers/data_providers.dart';
import '../../widgets/async_value_view.dart';

/// Fiber-plant / coverage map for the reseller portal: cabinets, closures and
/// access points on OSM tiles (GET /reseller/fiber-map — the same GeoJSON the
/// web map renders). Useful when checking whether a prospect's location is
/// near plant.
class ResellerFiberMapScreen extends ConsumerWidget {
  const ResellerFiberMapScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final map = ref.watch(resellerFiberMapProvider);

    return Scaffold(
      appBar: AppBar(title: const Text('Coverage map')),
      body: AsyncValueView<ResellerFiberMap>(
        value: map,
        onRetry: () => ref.invalidate(resellerFiberMapProvider),
        data: (m) {
          if (m.points.isEmpty && m.lines.isEmpty) {
            return const EmptyState(
              icon: Icons.map_outlined,
              message: 'No mapped fiber plant yet.',
            );
          }
          final bounds = _bounds(m);
          return FlutterMap(
            options: MapOptions(
              initialCameraFit: CameraFit.bounds(
                bounds: bounds,
                padding: const EdgeInsets.all(48),
              ),
            ),
            children: [
              TileLayer(
                urlTemplate: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                userAgentPackageName: 'io.dotmac.selfcare',
              ),
              if (m.lines.isNotEmpty)
                PolylineLayer(
                  polylines: [
                    for (final line in m.lines)
                      Polyline(
                        points: line,
                        strokeWidth: 3,
                        color: Theme.of(context).colorScheme.primary,
                      ),
                  ],
                ),
              MarkerLayer(
                markers: [
                  for (final p in m.points)
                    Marker(
                      point: LatLng(p.lat, p.lng),
                      width: 36,
                      height: 36,
                      child: Tooltip(
                        message: '${p.name ?? p.type}\n${p.type}',
                        child: Icon(
                          switch (p.type) {
                            'fdh_cabinet' => Icons.dns_outlined,
                            'splice_closure' => Icons.cable,
                            'access_point' => Icons.wifi_tethering,
                            _ => Icons.place,
                          },
                          color: switch (p.type) {
                            'fdh_cabinet' => context.semantic.success,
                            'splice_closure' => context.semantic.warning,
                            'access_point' => Colors.blue.shade800,
                            _ => Colors.grey.shade800,
                          },
                        ),
                      ),
                    ),
                ],
              ),
            ],
          );
        },
      ),
    );
  }

  LatLngBounds _bounds(ResellerFiberMap m) {
    final pts = [
      for (final p in m.points) LatLng(p.lat, p.lng),
      for (final line in m.lines) ...line,
    ];
    if (pts.isEmpty) {
      // Lagos fallback; only reached when there are lines/points mismatch.
      return LatLngBounds(const LatLng(6.4, 3.3), const LatLng(6.7, 3.6));
    }
    return LatLngBounds.fromPoints(pts);
  }
}
