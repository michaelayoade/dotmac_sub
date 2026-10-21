import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:latlong2/latlong.dart';

import '../../app/theme.dart';
import '../../core/location/map_coordinates.dart';
import '../auth/auth_state.dart' show apiClientProvider;
import '../jobs/job_models.dart' show JobDestination;
import '../jobs/job_detail_screen.dart' show uriLauncherProvider;

class VendorJobDestination {
  const VendorJobDestination({required this.jobId, required this.destination});
  final String jobId;
  final JobDestination destination;
}

class VendorMapData {
  const VendorMapData({required this.destinations});
  final List<VendorJobDestination> destinations;
  LatLng get center =>
      destinations
          .map(
            (item) => safeLatLng(
              item.destination.latitude,
              item.destination.longitude,
            ),
          )
          .whereType<LatLng>()
          .firstOrNull ??
      defaultMapCenter;
}

/// Fresh assignment-authorized reads only. No device position or global plant
/// request determines which destinations a vendor may see.
final vendorJobMapProvider = FutureProvider.autoDispose<VendorMapData>((
  ref,
) async {
  final refresh = Timer.periodic(
    const Duration(seconds: 30),
    (_) => ref.invalidateSelf(),
  );
  ref.onDispose(refresh.cancel);
  final api = ref.read(apiClientProvider);
  final destinations = <VendorJobDestination>[];
  const limit = 200;
  for (var offset = 0; ; offset += limit) {
    final response = await api.dio.get(
      '/api/v1/field/jobs',
      queryParameters: {'limit': limit, 'offset': offset},
    );
    final jobs = (response.data['items'] as List).cast<Map>();
    for (final job in jobs) {
      final jobId = job['id'] as String;
      // Every job read rechecks current selection; if it changed during the
      // request, fail the map refresh without retaining an old snapshot.
      final detail = await api.dio.get(
        '/api/v1/field/jobs/$jobId/destinations',
      );
      for (final item in (detail.data['items'] as List).cast<Map>()) {
        destinations.add(
          VendorJobDestination(
            jobId: jobId,
            destination: JobDestination.fromJson(item.cast<String, dynamic>()),
          ),
        );
      }
    }
    if (jobs.length < limit) break;
  }
  return VendorMapData(destinations: destinations);
});

class VendorMapScreen extends ConsumerWidget {
  const VendorMapScreen({super.key, this.showTiles = true});
  final bool showTiles;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final data = ref.watch(vendorJobMapProvider);
    return Scaffold(
      appBar: AppBar(
        title: const Text('Assigned job locations'),
        actions: [
          IconButton(
            tooltip: 'Refresh',
            icon: const Icon(Icons.refresh),
            onPressed: () => ref.invalidate(vendorJobMapProvider),
          ),
        ],
      ),
      body: data.when(
        skipLoadingOnRefresh: false,
        skipError: false,
        data: (map) => map.destinations.isEmpty
            ? const Center(child: Text('No assigned job locations'))
            : Column(
                children: [
                  Expanded(
                    child: FlutterMap(
                      options: MapOptions(
                        initialCenter: map.center,
                        initialZoom: 14,
                        cameraConstraint: finiteMapCameraConstraint,
                      ),
                      children: [
                        if (showTiles)
                          TileLayer(
                            urlTemplate:
                                'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
                            userAgentPackageName: 'io.dotmac.dotmac_field',
                          ),
                        MarkerLayer(
                          markers: [
                            for (final item in map.destinations)
                              if (item.destination.hasCoordinates)
                                Marker(
                                  point: safeLatLng(
                                    item.destination.latitude,
                                    item.destination.longitude,
                                  )!,
                                  width: 38,
                                  height: 38,
                                  child: GestureDetector(
                                    key: Key(
                                      'vendor-job-${item.jobId}-${item.destination.destinationId}',
                                    ),
                                    onTap: () =>
                                        _showDestination(context, ref, item),
                                    child: Icon(
                                      Icons.place,
                                      size: 28,
                                      color: AppColors.primary,
                                    ),
                                  ),
                                ),
                          ],
                        ),
                      ],
                    ),
                  ),
                  SizedBox(
                    height: 140,
                    child: ListView(
                      children: [
                        for (final item in map.destinations)
                          ListTile(
                            title: Text(item.destination.label),
                            subtitle: Text(item.jobId),
                            onTap: () => _showDestination(context, ref, item),
                          ),
                      ],
                    ),
                  ),
                ],
              ),
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (_, _) =>
            const Center(child: Text('Could not load assigned job locations')),
      ),
    );
  }

  void _showDestination(
    BuildContext context,
    WidgetRef ref,
    VendorJobDestination item,
  ) {
    showModalBottomSheet<void>(
      context: context,
      builder: (_) => _DestinationSheet(item: item),
    );
  }
}

bool _sameDestination(VendorJobDestination left, VendorJobDestination right) =>
    left.jobId == right.jobId &&
    left.destination.destinationType == right.destination.destinationType &&
    left.destination.destinationId == right.destination.destinationId &&
    left.destination.label == right.destination.label &&
    left.destination.addressText == right.destination.addressText &&
    left.destination.latitude == right.destination.latitude &&
    left.destination.longitude == right.destination.longitude;

VendorJobDestination? _currentDestination(
  AsyncValue<VendorMapData> state,
  VendorJobDestination selected,
) {
  // Refreshes can retain previous data. Never use it while authorization is
  // loading or has failed, including inside an already-open modal route.
  if (state.isLoading || state.hasError) return null;
  return state.asData?.value.destinations
      .where((item) => _sameDestination(item, selected))
      .firstOrNull;
}

class _DestinationSheet extends ConsumerStatefulWidget {
  const _DestinationSheet({required this.item});
  final VendorJobDestination item;

  @override
  ConsumerState<_DestinationSheet> createState() => _DestinationSheetState();
}

class _DestinationSheetState extends ConsumerState<_DestinationSheet> {
  bool _navigating = false;

  Future<void> _navigate() async {
    setState(() => _navigating = true);
    try {
      // Re-fetch assignment-authorized destinations at the external navigation
      // boundary; an open sheet is never an authorization snapshot.
      final verified = _currentDestination(
        AsyncData(await ref.refresh(vendorJobMapProvider.future)),
        widget.item,
      );
      if (!mounted || verified == null) return;
      final current = _currentDestination(
        ref.read(vendorJobMapProvider),
        widget.item,
      );
      final uri = current?.destination.mapsUri;
      if (uri != null) await ref.read(uriLauncherProvider)(uri);
    } catch (_) {
      // The watched provider renders the refusal without retaining old data.
    } finally {
      if (mounted) setState(() => _navigating = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final current = _currentDestination(
      ref.watch(vendorJobMapProvider),
      widget.item,
    );
    return SafeArea(
      child: current == null
          ? const Padding(
              padding: EdgeInsets.all(24),
              child: Text('Destination unavailable. Refresh assigned jobs.'),
            )
          : ListTile(
              title: Text(current.destination.label),
              subtitle: Text(current.destination.addressText ?? current.jobId),
              trailing: current.destination.mapsUri == null
                  ? null
                  : TextButton.icon(
                      key: const Key('vendor-job-navigate'),
                      onPressed: _navigating ? null : _navigate,
                      icon: const Icon(Icons.navigation_outlined),
                      label: const Text('Navigate'),
                    ),
            ),
    );
  }
}
